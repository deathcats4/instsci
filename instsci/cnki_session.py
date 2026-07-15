"""Visible, persistent CNKI browser session setup.

This module deliberately stores the complete Chromium profile outside the
source tree.  It never exports cookies or tries to bypass CNKI verification.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .chinese_literature import get_chinese_literature_portal
from .cloakbrowser_compat import prepare_cloakbrowser_runtime
from .config import Config


CNKI_PORTAL = get_chinese_literature_portal("cnki")
CNKI_HOME_URL = CNKI_PORTAL.home_url
CNKI_SEARCH_URL = CNKI_PORTAL.search_entry_url
CNKI_HOST_SUFFIXES = CNKI_PORTAL.hosts
CNKI_VISIBLE_VERIFICATION_MARKERS = CNKI_PORTAL.verification_markers
CNKI_TITLE_VERIFICATION_MARKERS = (
    "安全验证",
    "人机验证",
)


def load_cnki_batch(path: str | Path) -> list[dict[str, str]]:
    """Load and validate a JSON array of CNKI article records."""
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("CNKI batch input must be a JSON array.")
    records: list[dict[str, str]] = []
    for index, raw in enumerate(payload, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"CNKI batch row {index} must be an object.")
        url = str(raw.get("url") or "").strip()
        record_id = str(raw.get("record_id") or "").strip()
        title = str(raw.get("title") or "").strip()
        host = (urlparse(url).hostname or "").lower()
        if not url or not any(host == suffix or host.endswith(f".{suffix}") for suffix in CNKI_HOST_SUFFIXES):
            raise ValueError(f"CNKI batch row {index} has an invalid CNKI URL.")
        if not record_id or not re.fullmatch(r"[A-Za-z0-9._-]+", record_id):
            raise ValueError(f"CNKI batch row {index} has an unsafe record_id.")
        if not title:
            raise ValueError(f"CNKI batch row {index} is missing title.")
        records.append(
            {
                "record_id": record_id,
                "title": title,
                "url": url,
                "zotero_item_key": str(raw.get("zotero_item_key") or "").strip(),
            }
        )
    return records


def safe_page_url(url: str) -> str:
    """Return a report-safe URL without query parameters or fragments."""
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def cnki_search_url(query: str, base_url: str = CNKI_SEARCH_URL) -> str:
    """Build a CNKI search entry URL with a title/query keyword."""
    parsed = urlparse(str(base_url or CNKI_SEARCH_URL))
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["kw"] = str(query or "").strip()
    return urlunparse(parsed._replace(query=urlencode(params)))


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def classify_cnki_session(url: str, title: str = "") -> str:
    """Classify visible CNKI state without treating it as a PDF verdict."""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    query = parsed.query.lower()
    title_text = str(title or "")
    if (
        "/verify/" in path
        or "captcha" in path
        or "captchatype=" in query
        or any(marker in title_text for marker in CNKI_TITLE_VERIFICATION_MARKERS)
    ):
        return "human_verification_required"
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in CNKI_HOST_SUFFIXES):
        return "session_ready"
    return "unexpected_page"


def _page_title(page: Any) -> str:
    try:
        return str(page.title() or "")
    except Exception:
        return ""


def cnki_verification_visible(page: Any) -> bool:
    """Return whether CNKI is visibly asking for a human verification step."""
    if classify_cnki_session(str(getattr(page, "url", "") or ""), _page_title(page)) == "human_verification_required":
        return True
    # CNKI keeps hidden CAPTCHA markup in otherwise authorized article pages.
    # Inspect visibility instead of searching the complete body text.
    for marker in CNKI_VISIBLE_VERIFICATION_MARKERS:
        try:
            matches = page.get_by_text(marker, exact=False)
            for index in range(min(matches.count(), 5)):
                if matches.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


def cnki_pdf_button_visible(page: Any) -> bool:
    """Return whether the visible CNKI article page exposes the PDF button."""
    try:
        return bool(page.get_by_text("PDF下载", exact=True).first.is_visible())
    except Exception:
        return False


def cnki_search_results_visible(page: Any) -> bool:
    """Return whether the current page shows plausible CNKI article results."""
    try:
        return bool(
            page.evaluate(
                """() => {
                  const links = [...document.querySelectorAll('a[href]')];
                  return links.some((a) => {
                    const href = a.href || "";
                    const text = (a.innerText || a.title || "").replace(/\\s+/g, " ").trim();
                    return /cnki\\.(net|com\\.cn)/i.test(href)
                      && /detail|kcms|kns8s\\/Detail|filename|dbcode/i.test(href)
                      && !/download|pdf|caj/i.test(href)
                      && text.length >= 4;
                  });
                }"""
            )
        )
    except Exception:
        return False


def _assign_or_commit(page: Any, url: str, detail: dict[str, object], *, timeout_ms: int) -> None:
    try:
        page.evaluate("target => { window.location.assign(target); }", url)
    except Exception as exc:
        detail["assign_error"] = f"{type(exc).__name__}: {exc}"
        try:
            page.goto(url, wait_until="commit", timeout=min(timeout_ms, 15_000))
            detail["navigation_method"] = "goto_commit"
        except Exception as fallback_exc:
            detail["goto_error"] = f"{type(fallback_exc).__name__}: {fallback_exc}"


def submit_cnki_search(page: Any, title: str) -> dict[str, object]:
    """Fill and submit the visible CNKI search box when possible."""
    try:
        raw = page.evaluate(
            """(value) => {
              const inputs = [...document.querySelectorAll('input[type="text"], input:not([type]), textarea')];
              const input = inputs.find((i) => /主题|篇名|关键词|检索|search|keyword|kw/i.test(
                [i.placeholder, i.name, i.id, i.className].join(" ")
              )) || inputs[0];
              if (!input) return { submitted: false, reason: "no_search_input" };
              const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value")?.set;
              if (setter) setter.call(input, value); else input.value = value;
              input.dispatchEvent(new Event("input", { bubbles: true }));
              input.dispatchEvent(new Event("change", { bubbles: true }));
              const controls = [...document.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]')];
              const button = document.querySelector('input.search-btn,.search-btn,#btnSearch') ||
                controls.find((e) => /检索|搜索|查询|Search/i.test(e.innerText || e.value || e.title || ""));
              if (button) {
                button.click();
                return { submitted: true, method: "button_click", controlText: (button.innerText || button.value || button.title || "").trim().slice(0, 80) };
              }
              input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
              return { submitted: true, method: "enter_key" };
            }""",
            title,
        )
        return dict(raw or {})
    except Exception as exc:
        return {"submitted": False, "error": f"{type(exc).__name__}: {exc}"}


def click_cnki_search_result(page: Any, *, title: str, record_id: str = "") -> dict[str, object]:
    """Click the best matching CNKI search result in the current tab."""
    expected = _compact_text(title)
    stable_id = str(record_id or "").upper()
    try:
        raw = page.evaluate(
            """({ expected, recordId }) => {
              const norm = (s) => String(s || "").replace(/\\s+/g, "").toLowerCase();
              const links = [...document.querySelectorAll('a[href]')].map((a, index) => {
                const href = a.href || "";
                const text = (a.innerText || a.title || "").replace(/\\s+/g, " ").trim();
                let score = 0;
                const ntext = norm(text);
                const upperHref = href.toUpperCase();
                if (!/cnki\\.(net|com\\.cn)/i.test(href)) score -= 100;
                if (/detail|kcms|kns8s\\/Detail|filename|dbcode/i.test(href)) score += 20;
                if (/download|pdf|caj|author|reference|rbt/i.test(href)) score -= 30;
                if (recordId && upperHref.includes(recordId)) score += 100;
                if (expected && ntext === expected) score += 90;
                else if (expected && ntext && (ntext.includes(expected) || expected.includes(ntext))) score += 60;
                return { a, index, href, text, score };
              }).filter((x) => x.href && x.text && x.score > 0);
              links.sort((a, b) => b.score - a.score);
              const best = links[0];
              if (!best || best.score < 20) {
                return { clicked: false, result_found: false, candidate_count: links.length };
              }
              best.a.target = "_self";
              best.a.click();
              return {
                clicked: true,
                result_found: true,
                href: best.href,
                text: best.text,
                score: best.score,
                candidate_count: links.length,
              };
            }""",
            {"expected": expected, "recordId": stable_id},
        )
        return dict(raw or {})
    except Exception as exc:
        return {"clicked": False, "result_found": False, "error": f"{type(exc).__name__}: {exc}"}


def navigate_cnki_article_via_search(
    page: Any,
    *,
    title: str,
    fallback_url: str = "",
    record_id: str = "",
    search_entry_url: str = CNKI_SEARCH_URL,
    timeout_ms: int = 60_000,
    settle_seconds: float = 2.0,
) -> dict[str, object]:
    """Reach a CNKI article through search results before falling back to a detail URL."""
    detail: dict[str, object] = {
        "requested_url": safe_page_url(fallback_url),
        "requested_title": title,
        "search_entry_url": safe_page_url(search_entry_url),
        "navigation_method": "search_result_click",
        "ready": False,
        "fallback_used": False,
    }
    _assign_or_commit(page, search_entry_url, detail, timeout_ms=timeout_ms)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass
    time.sleep(1.5)

    search = submit_cnki_search(page, title)
    detail["search_submission"] = search
    if not search.get("submitted"):
        search_url = cnki_search_url(title, search_entry_url)
        detail["search_url"] = safe_page_url(search_url)
        _assign_or_commit(page, search_url, detail, timeout_ms=timeout_ms)
    time.sleep(2.5)

    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if cnki_verification_visible(page):
            detail.update(
                {
                    "ready": True,
                    "session_status": "human_verification_required",
                    "page_url": safe_page_url(str(getattr(page, "url", "") or "")),
                    "verification_required": True,
                    "pdf_button_visible": False,
                }
            )
            return detail
        if cnki_search_results_visible(page):
            break
        time.sleep(1)

    result_click = click_cnki_search_result(page, title=title, record_id=record_id)
    detail["search_result"] = result_click
    if not result_click.get("clicked"):
        detail["fallback_used"] = bool(fallback_url)
        if fallback_url:
            fallback = navigate_cnki_article(page, fallback_url, timeout_ms=timeout_ms, settle_seconds=settle_seconds)
            fallback["navigation_method"] = "direct_detail_fallback"
            detail["fallback_navigation"] = fallback
            detail.update(
                {
                    "ready": fallback.get("ready", False),
                    "session_status": fallback.get("session_status", "unexpected_page"),
                    "page_url": fallback.get("page_url", ""),
                    "pdf_button_visible": fallback.get("pdf_button_visible", False),
                    "verification_required": fallback.get("verification_required", False),
                }
            )
        return detail

    while time.monotonic() < deadline:
        if cnki_verification_visible(page):
            detail["ready"] = True
            detail["session_status"] = "human_verification_required"
            break
        if cnki_pdf_button_visible(page):
            detail["ready"] = True
            detail["session_status"] = "session_ready"
            break
        time.sleep(1)

    try:
        page.evaluate("window.stop()")
    except Exception:
        pass
    if settle_seconds > 0:
        time.sleep(settle_seconds)

    current_url = str(getattr(page, "url", "") or "")
    detail.setdefault("session_status", classify_cnki_session(current_url, _page_title(page)))
    detail["page_url"] = safe_page_url(current_url)
    detail["pdf_button_visible"] = cnki_pdf_button_visible(page)
    detail["verification_required"] = cnki_verification_visible(page)
    if detail["verification_required"]:
        detail["ready"] = True
        detail["session_status"] = "human_verification_required"
    elif detail["pdf_button_visible"]:
        detail["ready"] = True
        detail["session_status"] = "session_ready"
    return detail


def navigate_cnki_article(
    page: Any,
    url: str,
    *,
    timeout_ms: int = 45_000,
    settle_seconds: float = 2.0,
) -> dict[str, object]:
    """Navigate to a CNKI article without waiting indefinitely for page load events.

    CNKI article pages can visibly render while Playwright is still waiting for
    ``domcontentloaded``. Treat the visible PDF button or a visible verification
    page as the actionable state and stop any lingering network activity.
    """
    requested_safe = safe_page_url(url)
    detail: dict[str, object] = {
        "requested_url": requested_safe,
        "navigation_method": "location_assign",
        "ready": False,
    }
    _assign_or_commit(page, url, detail, timeout_ms=timeout_ms)

    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        current_url = str(getattr(page, "url", "") or "")
        status = classify_cnki_session(current_url, "")
        if status == "human_verification_required":
            detail["ready"] = True
            detail["session_status"] = status
            break
        if cnki_pdf_button_visible(page):
            detail["ready"] = True
            detail["session_status"] = "session_ready"
            break
        if safe_page_url(current_url) == requested_safe:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=1_000)
            except Exception:
                pass
            if cnki_pdf_button_visible(page):
                detail["ready"] = True
                detail["session_status"] = "session_ready"
                break
        time.sleep(1)

    try:
        page.evaluate("window.stop()")
    except Exception:
        pass
    if settle_seconds > 0:
        time.sleep(settle_seconds)

    current_url = str(getattr(page, "url", "") or "")
    detail.setdefault("session_status", classify_cnki_session(current_url, _page_title(page)))
    detail["page_url"] = safe_page_url(current_url)
    detail["pdf_button_visible"] = cnki_pdf_button_visible(page)
    detail["verification_required"] = cnki_verification_visible(page)
    if detail["verification_required"]:
        detail["ready"] = True
        detail["session_status"] = "human_verification_required"
    elif detail["pdf_button_visible"]:
        detail["ready"] = True
        detail["session_status"] = "session_ready"
    return detail


def settle_cnki_after_manual_step(
    page: Any,
    *,
    resume_url: str = "",
    timeout_ms: int = 30_000,
) -> dict[str, object]:
    """Let a user-completed visible step settle, then optionally return to the article.

    The helper does not solve or bypass verification. It only preserves the live
    page after the user has acted and avoids retrying a PDF click while the tab is
    still on a CNKI verification or landing page.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    time.sleep(1)
    if resume_url and not cnki_verification_visible(page):
        current_safe = safe_page_url(str(getattr(page, "url", "") or ""))
        resume_safe = safe_page_url(resume_url)
        if current_safe != resume_safe:
            return navigate_cnki_article(page, resume_url, timeout_ms=timeout_ms)
    title = _page_title(page)
    current_url = str(getattr(page, "url", "") or "")
    return {
        "session_status": classify_cnki_session(current_url, title),
        "verification_required": cnki_verification_visible(page),
        "page_url": safe_page_url(current_url),
        "page_title": title,
    }


def capture_cnki_pdf(
    page: Any,
    *,
    output_path: str | Path,
    timeout_ms: int = 90_000,
) -> dict[str, object]:
    """Click CNKI's visible PDF control and capture the browser download."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    button = page.get_by_text("PDF下载", exact=True).first
    button.wait_for(state="visible", timeout=30_000)
    detail = {
        "button_url": safe_page_url(button.get_attribute("href") or ""),
        "button_title": button.get_attribute("title") or "",
    }
    try:
        with page.expect_download(timeout=timeout_ms) as event:
            button.click(timeout=30_000)
        download = event.value
        download.save_as(str(target))
    except PlaywrightTimeoutError as exc:
        detail["error"] = f"TimeoutError: {exc}"
        detail["verification_required"] = cnki_verification_visible(page)
        return detail

    time.sleep(2)
    data = target.read_bytes() if target.exists() else b""
    detail.update(
        {
            "pdf_path": str(target),
            "size_bytes": len(data),
            "pdf_header_valid": data.startswith(b"%PDF-"),
        }
    )
    return detail


def open_cnki_login_session(
    config: Config,
    *,
    url: str = CNKI_HOME_URL,
    output_dir: str | Path,
) -> tuple[Any, Any, Path]:
    """Open a visible persistent CNKI session and return it to the CLI.

    The caller owns the returned browser context and must close it after the
    user finishes any CAPTCHA, institution check, or login step.
    """
    prepare_cloakbrowser_runtime()
    from cloakbrowser import launch_persistent_context

    profile_dir = Path(config.cnki_profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(output_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = run_dir / "browser-downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    context = launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=False,
        accept_downloads=True,
        downloads_path=str(downloads_dir),
    )
    # A persistent Chromium profile may restore an about:blank tab or a tab
    # from the previous session. Always create one deterministic target tab,
    # then close restored tabs so the user sees the page they were asked to
    # verify instead of an active blank tab.
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    for other in list(context.pages):
        if other is page:
            continue
        try:
            other.close()
        except Exception:
            pass
    page.bring_to_front()
    return context, page, run_dir


def write_cnki_session_report(page: Any, run_dir: Path, profile_dir: str | Path) -> dict[str, object]:
    """Save screenshot-backed session state without cookies or URL tokens."""
    screenshot = run_dir / "cnki_session.png"
    page.screenshot(path=str(screenshot), full_page=False)
    title = page.title()
    current_url = str(getattr(page, "url", "") or "")
    report: dict[str, object] = {
        "schema": "instsci.cnki_session.v1",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "session_status": classify_cnki_session(current_url, title),
        "page_url": safe_page_url(current_url),
        "page_title": title,
        "profile_dir": str(Path(profile_dir)),
        "screenshot": str(screenshot),
        "pdf_verdict": "not_tested",
        "cookies_exported": False,
    }
    report_path = run_dir / "cnki_session.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report"] = str(report_path)
    return report
