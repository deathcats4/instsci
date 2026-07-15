"""Visible, persistent Wanfang browser download workflow.

Wanfang PDF links are generated inside the browser session.  This module keeps
the flow search-first and never stores cookies or generated download tokens.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .chinese_literature import get_chinese_literature_portal
from .cloakbrowser_compat import prepare_cloakbrowser_runtime
from .config import Config


WANFANG_PORTAL = get_chinese_literature_portal("wanfang")
WANFANG_HOME_URL = WANFANG_PORTAL.home_url
WANFANG_SEARCH_URL = WANFANG_PORTAL.search_entry_url
WANFANG_HOST_SUFFIXES = WANFANG_PORTAL.hosts
WANFANG_VISIBLE_VERIFICATION_MARKERS = WANFANG_PORTAL.verification_markers
WANFANG_DOWNLOAD_POPUP_HOST = "oss.wanfangdata.com.cn"
WANFANG_DOWNLOAD_POPUP_PATH = "/Fulltext/Download"


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    hostname = host.lower().lstrip(".")
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes)


def load_wanfang_batch(path: str | Path) -> list[dict[str, str]]:
    """Load and validate a JSON array of Wanfang article records."""
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("Wanfang batch input must be a JSON array.")
    records: list[dict[str, str]] = []
    for index, raw in enumerate(payload, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Wanfang batch row {index} must be an object.")
        record_id = str(raw.get("record_id") or "").strip()
        title = str(raw.get("title") or "").strip()
        query = str(raw.get("query") or title).strip()
        url = str(raw.get("url") or "").strip()
        if not record_id or not re.fullmatch(r"[A-Za-z0-9._-]+", record_id):
            raise ValueError(f"Wanfang batch row {index} has an unsafe record_id.")
        if not title:
            raise ValueError(f"Wanfang batch row {index} is missing title.")
        if not query:
            raise ValueError(f"Wanfang batch row {index} is missing query.")
        if url:
            host = (urlparse(url).hostname or "").lower()
            if not _host_matches(host, WANFANG_HOST_SUFFIXES):
                raise ValueError(f"Wanfang batch row {index} has an invalid Wanfang URL.")
        records.append(
            {
                "record_id": record_id,
                "title": title,
                "query": query,
                "url": url,
                "zotero_item_key": str(raw.get("zotero_item_key") or "").strip(),
            }
        )
    return records


def safe_wanfang_url(url: str) -> str:
    """Return a report-safe Wanfang URL without session query parameters."""
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def wanfang_search_url(query: str, base_url: str = WANFANG_SEARCH_URL) -> str:
    """Build a Wanfang search URL using the verified paper search endpoint."""
    parsed = urlparse(str(base_url or WANFANG_SEARCH_URL))
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["q"] = str(query or "").strip()
    return urlunparse(parsed._replace(query=urlencode(params)))


def classify_wanfang_page(url: str, title: str = "") -> str:
    """Classify visible Wanfang state without treating it as final evidence."""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    title_text = str(title or "")
    if "captcha" in path or any(marker in title_text for marker in ("安全验证", "验证码", "人机验证")):
        return "human_verification_required"
    if _host_matches(host, WANFANG_HOST_SUFFIXES):
        return "session_ready"
    return "unexpected_page"


def _page_title(page: Any) -> str:
    try:
        return str(page.title() or "")
    except Exception:
        return ""


def wanfang_verification_visible(page: Any) -> bool:
    """Return whether Wanfang visibly asks for a human verification step."""
    if classify_wanfang_page(str(getattr(page, "url", "") or ""), _page_title(page)) == "human_verification_required":
        return True
    for marker in WANFANG_VISIBLE_VERIFICATION_MARKERS:
        try:
            matches = page.get_by_text(marker, exact=False)
            for index in range(min(matches.count(), 5)):
                if matches.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


def wanfang_search_results_visible(page: Any, *, title: str = "") -> bool:
    """Return whether the current page shows Wanfang result controls."""
    expected = _compact_text(title)
    try:
        return bool(
            page.evaluate(
                """(expected) => {
                  const norm = (s) => String(s || "").replace(/\\s+/g, "").toLowerCase();
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 1 && r.height > 1 && s.display !== "none" && s.visibility !== "hidden";
                  };
                  const bodyText = norm(document.body?.innerText || "");
                  const hasTitle = !expected || bodyText.includes(expected);
                  const buttons = [...document.querySelectorAll("a,button,div,span")]
                    .filter((el) => visible(el) && (el.innerText || el.title || "").replace(/\\s+/g, "") === "下载");
                  return hasTitle && buttons.length > 0;
                }""",
                expected,
            )
        )
    except Exception:
        return False


def navigate_wanfang_search(
    page: Any,
    *,
    query: str,
    title: str = "",
    timeout_ms: int = 45_000,
    settle_seconds: float = 2.0,
) -> dict[str, object]:
    """Navigate to Wanfang search results for a title/query."""
    search_url = wanfang_search_url(query)
    detail: dict[str, object] = {
        "requested_title": title,
        "requested_query": query,
        "search_url": safe_wanfang_url(search_url),
        "navigation_method": "search_result_download",
        "ready": False,
    }
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        detail["goto_error"] = f"{type(exc).__name__}: {exc}"
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if wanfang_verification_visible(page):
            detail["ready"] = True
            detail["session_status"] = "human_verification_required"
            break
        if wanfang_search_results_visible(page, title=title):
            detail["ready"] = True
            detail["session_status"] = "session_ready"
            break
        time.sleep(1)
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    current_url = str(getattr(page, "url", "") or "")
    detail.setdefault("session_status", classify_wanfang_page(current_url, _page_title(page)))
    detail["page_url"] = safe_wanfang_url(current_url)
    detail["verification_required"] = wanfang_verification_visible(page)
    detail["download_control_visible"] = wanfang_search_results_visible(page, title=title)
    return detail


def click_wanfang_result_download(page: Any, *, title: str = "") -> dict[str, object]:
    """Click the best matching result-row Wanfang download control."""
    expected = _compact_text(title)
    try:
        raw = page.evaluate(
            """(expected) => {
              const norm = (s) => String(s || "").replace(/\\s+/g, "").toLowerCase();
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 1 && r.height > 1 && s.display !== "none" && s.visibility !== "hidden";
              };
              const titleRects = [...document.querySelectorAll("a,div,span")]
                .filter((el) => expected && norm(el.innerText || el.title || "").includes(expected) && visible(el))
                .map((el) => el.getBoundingClientRect());
              const candidates = [...document.querySelectorAll("a,button,div,span")].map((el, index) => {
                const r = el.getBoundingClientRect();
                const text = (el.innerText || el.title || "").replace(/\\s+/g, "").trim();
                const cls = String(el.className || "");
                const containerText = norm(el.closest(".right-list,.result-item,.wf-list-item,li,.me-container")?.innerText || "");
                let score = 0;
                if (text === "下载") score += 100;
                if (cls.includes("wf-list-button")) score += 50;
                if (cls.includes("t-DIB")) score += 20;
                if (containerText && expected && containerText.includes(expected)) score += 60;
                if (titleRects.some((tr) => Math.abs((tr.y + tr.height / 2) - (r.y + r.height / 2)) < 180)) score += 40;
                if (text.includes("批量") || text.includes("下载：")) score -= 100;
                return {
                  el, index, text, cls, href: el.href || "",
                  x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                  score
                };
              }).filter((x) => visible(x.el) && x.score > 0).sort((a, b) => b.score - a.score);
              const best = candidates[0];
              if (!best || best.score < 80) {
                return { clicked: false, result_found: false, candidate_count: candidates.length };
              }
              best.el.scrollIntoView({ block: "center", inline: "center" });
              best.el.click();
              return {
                clicked: true,
                result_found: true,
                text: best.text,
                href: best.href,
                cls: best.cls,
                x: best.x,
                y: best.y,
                w: best.w,
                h: best.h,
                score: best.score,
                candidate_count: candidates.length,
              };
            }""",
            expected,
        )
        result = dict(raw or {})
        if result.get("href"):
            result["href"] = safe_wanfang_url(str(result["href"]))
        return result
    except Exception as exc:
        return {"clicked": False, "result_found": False, "error": f"{type(exc).__name__}: {exc}"}


def _click_retry_download_control(page: Any) -> dict[str, object]:
    try:
        return dict(
            page.evaluate(
                """() => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 1 && r.height > 1 && s.display !== "none" && s.visibility !== "hidden";
                  };
                  const candidates = [...document.querySelectorAll("a,button,div,span")].map((el, index) => {
                    const r = el.getBoundingClientRect();
                    const text = (el.innerText || el.title || "").replace(/\\s+/g, " ").trim();
                    let score = 0;
                    if (/点击此处|重新下载|下载/.test(text)) score += 100;
                    if (el.href) score += 20;
                    return { el, index, text, href: el.href || "", x: Math.round(r.x), y: Math.round(r.y), score };
                  }).filter((x) => visible(x.el) && x.score > 0).sort((a, b) => b.score - a.score);
                  const best = candidates[0];
                  if (!best) return { clicked: false, reason: "no_retry_control" };
                  best.el.click();
                  return { clicked: true, text: best.text, href: best.href, x: best.x, y: best.y, score: best.score };
                }"""
            )
            or {}
        )
    except Exception as exc:
        return {"clicked": False, "error": f"{type(exc).__name__}: {exc}"}


def _wait_for_download(page: Any, downloads: list[Any], *, timeout_ms: int) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if downloads:
            return True
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    return bool(downloads)


def capture_wanfang_pdf(
    page: Any,
    *,
    title: str,
    output_path: str | Path,
    timeout_ms: int = 90_000,
) -> dict[str, object]:
    """Click Wanfang's result-row download control and capture the PDF download."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    context = page.context
    downloads: list[Any] = []
    attached: set[int] = set()
    popup_pages: list[Any] = []

    def attach(download_page: Any) -> None:
        marker = id(download_page)
        if marker in attached:
            return
        attached.add(marker)
        try:
            download_page.on("download", lambda download: downloads.append(download))
        except Exception:
            pass

    def on_page(new_page: Any) -> None:
        popup_pages.append(new_page)
        attach(new_page)

    for existing in list(context.pages):
        attach(existing)
    try:
        context.on("page", on_page)
    except Exception:
        pass

    detail: dict[str, object] = {
        "download_click": click_wanfang_result_download(page, title=title),
        "popup_pages": [],
    }
    if not detail["download_click"].get("clicked"):
        detail["verification_required"] = wanfang_verification_visible(page)
        return detail

    _wait_for_download(page, downloads, timeout_ms=min(timeout_ms, 15_000))
    if not downloads:
        for popup in list(context.pages):
            url = str(getattr(popup, "url", "") or "")
            if WANFANG_DOWNLOAD_POPUP_HOST in url and WANFANG_DOWNLOAD_POPUP_PATH.lower() in urlparse(url).path.lower():
                attach(popup)
                detail["popup_pages"].append(safe_wanfang_url(url))
                detail["retry_click"] = _click_retry_download_control(popup)
                _wait_for_download(popup, downloads, timeout_ms=min(timeout_ms, 20_000))
                if downloads:
                    break

    if not downloads:
        detail["verification_required"] = wanfang_verification_visible(page)
        return detail

    download = downloads[0]
    suggested = str(download.suggested_filename or target.name)
    download.save_as(str(target))
    data = target.read_bytes() if target.exists() else b""
    compact_title = _compact_text(title)
    compact_filename = _compact_text(suggested)
    detail.update(
        {
            "suggested_filename": suggested,
            "pdf_path": str(target),
            "size_bytes": len(data),
            "pdf_header_valid": data.startswith(b"%PDF-"),
            "filename_title_match": bool(compact_title and compact_title in compact_filename),
            "verification_required": False,
        }
    )
    return detail


def open_wanfang_session(
    config: Config,
    *,
    url: str = WANFANG_HOME_URL,
    output_dir: str | Path,
    profile_dir: str | Path | None = None,
) -> tuple[Any, Any, Path]:
    """Open a visible persistent Wanfang session and return the context/page."""
    prepare_cloakbrowser_runtime()
    from cloakbrowser import launch_persistent_context

    profile = Path(profile_dir or config.chrome_profile_dir).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    run_dir = Path(output_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = run_dir / "browser-downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    context = launch_persistent_context(
        user_data_dir=str(profile),
        headless=False,
        accept_downloads=True,
        downloads_path=str(downloads_dir),
    )
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception:
        pass
    for other in list(context.pages):
        if other is page:
            continue
        try:
            other.close()
        except Exception:
            pass
    page.bring_to_front()
    return context, page, run_dir
