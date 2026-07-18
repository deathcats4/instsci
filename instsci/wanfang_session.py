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

from .chinese_literature import (
    classify_chinese_literature_page,
    first_author_from_record,
    first_author_from_pdf_signature,
    get_chinese_literature_portal,
    normalize_author_name,
    ordered_author_names,
    pdf_title_matches_first_page_block,
)
from .cloakbrowser_compat import prepare_cloakbrowser_runtime
from .config import Config


WANFANG_PORTAL = get_chinese_literature_portal("wanfang")
WANFANG_HOME_URL = WANFANG_PORTAL.home_url
WANFANG_SEARCH_URL = WANFANG_PORTAL.search_entry_url
WANFANG_HOST_SUFFIXES = WANFANG_PORTAL.hosts
WANFANG_VISIBLE_VERIFICATION_MARKERS = WANFANG_PORTAL.verification_markers
WANFANG_DOWNLOAD_POPUP_HOST = "oss.wanfangdata.com.cn"
WANFANG_DOWNLOAD_POPUP_PATH = "/Fulltext/Download"
WANFANG_RESULT_ROW_SELECTOR = ".normal-list,.result-item,.wf-list-item,li[data-record-id]"
WANFANG_RESULT_TITLE_SELECTOR = ".title,a[title],h2,h3,h4"
WANFANG_RESULT_AUTHOR_SELECTOR = ".author,.authors,.writer,[class*='author'],[class*='writer']"
WANFANG_RESULT_AUTHOR_LINK_SELECTOR = (
    "a[href*='author'],a[href*='Author'],a[href*='writer'],a[href*='Writer']"
)
WANFANG_RESULT_AUTHOR_VALUE_SELECTOR = (
    "span.authors,span.author,span.writer,a.authors,a.author,a.writer"
)
WANFANG_DOWNLOAD_CONTROL_SELECTOR = "a,button,div,span"
WANFANG_DOWNLOAD_LABELS = {"下载", "整篇下载"}


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _capture_size_bytes(result: dict[str, object]) -> int:
    try:
        return int(result.get("size_bytes") or 0)
    except Exception:
        return 0


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
        try:
            first_author = first_author_from_record(raw)
        except ValueError as exc:
            raise ValueError(f"Wanfang batch row {index} {exc}.") from exc
        records.append(
            {
                "record_id": record_id,
                "title": title,
                "first_author": first_author,
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


def classify_wanfang_page(url: str, title: str = "", *, auth_domains: tuple[str, ...] = ()) -> str:
    """Classify visible Wanfang state without treating it as final evidence."""
    return classify_chinese_literature_page(url, portal=WANFANG_PORTAL, title=title, auth_domains=auth_domains)


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


def extract_wanfang_download_candidates_from_html(
    html: str,
    *,
    title: str = "",
    first_author: str = "",
) -> list[dict[str, object]]:
    """Extract Wanfang download candidates from explicit result rows in an HTML fixture."""
    from bs4 import BeautifulSoup

    expected = _compact_text(title)
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[dict[str, object]] = []
    for row_index, row in enumerate(soup.select(WANFANG_RESULT_ROW_SELECTOR)):
        row_titles: list[tuple[str, str]] = []
        for title_node in row.select(WANFANG_RESULT_TITLE_SELECTOR):
            raw_title = str(title_node.get("title") or title_node.get_text(" ", strip=True) or "").strip()
            compact_title = _compact_text(raw_title)
            if raw_title and compact_title and (raw_title, compact_title) not in row_titles:
                row_titles.append((raw_title, compact_title))
        matching_titles = [raw for raw, compact in row_titles if expected and compact == expected]
        row_title_match = bool(matching_titles)
        row_title = matching_titles[0] if matching_titles else (row_titles[0][0] if row_titles else "")
        linked_author_nodes = row.select(WANFANG_RESULT_AUTHOR_LINK_SELECTOR)
        leaf_author_nodes = row.select(WANFANG_RESULT_AUTHOR_VALUE_SELECTOR)
        author_nodes = linked_author_nodes or leaf_author_nodes or row.select(WANFANG_RESULT_AUTHOR_SELECTOR)
        author_values = [
            str(node.get_text(" ", strip=True) or node.get("title") or "").strip()
            for node in author_nodes
        ]
        row_authors = [
            author for author in ordered_author_names(author_values) if not re.match(r"^\d{4}年", author)
        ]
        row_first_author = row_authors[0] if row_authors else ""
        row_author_text = " ".join(row_authors)
        row_author_match = bool(
            normalize_author_name(first_author)
            and normalize_author_name(first_author) == normalize_author_name(row_first_author)
        )
        for control in row.select(WANFANG_DOWNLOAD_CONTROL_SELECTOR):
            text = _compact_text(str(control.get_text(" ", strip=True) or control.get("title") or ""))
            if text not in WANFANG_DOWNLOAD_LABELS:
                continue
            candidates.append(
                {
                    "index": len(candidates),
                    "text": text,
                    "cls": " ".join(control.get("class") or []),
                    "href": str(control.get("href") or ""),
                    "row_title_match": row_title_match,
                    "row_title": row_title,
                    "row_titles": [raw for raw, _compact in row_titles],
                    "row_index": row_index,
                    "row_author_text": row_author_text,
                    "row_authors": row_authors,
                    "row_first_author": row_first_author,
                    "row_author_match": row_author_match,
                    "page_title_match": bool(expected and expected in _compact_text(soup.get_text(" ", strip=True))),
                }
            )
    return candidates


def _select_wanfang_download_candidate(
    candidates: list[dict[str, object]],
    *,
    title: str = "",
    first_author: str = "",
) -> dict[str, object]:
    """Return a Wanfang candidate plus same-row identity evidence."""
    expected = _compact_text(title)
    exact_candidates = [candidate for candidate in candidates if not expected or bool(candidate.get("row_title_match"))]
    rows: dict[object, list[dict[str, object]]] = {}
    for candidate in exact_candidates:
        row_key = candidate.get("row_index", candidate.get("index"))
        rows.setdefault(row_key, []).append(candidate)
    title_candidate_count = len(rows)
    author_disambiguation_used = title_candidate_count > 1
    author_match_count = 0
    eligible_row_keys = set(rows)
    if author_disambiguation_used:
        normalized_author = normalize_author_name(first_author)
        if not normalized_author:
            eligible_row_keys = set()
        else:
            matched_row_keys = {
                row_key
                for row_key, row_candidates in rows.items()
                if any(
                    normalized_author == normalize_author_name(candidate.get("row_first_author"))
                    for candidate in row_candidates
                )
            }
            author_match_count = len(matched_row_keys)
            eligible_row_keys = matched_row_keys if len(matched_row_keys) == 1 else set()
    elif title_candidate_count == 0:
        eligible_row_keys = set()
    scored: list[tuple[int, dict[str, object]]] = []
    for candidate in candidates:
        text = _compact_text(str(candidate.get("text") or ""))
        if text not in {"下载", "整篇下载"}:
            continue
        if expected and not bool(candidate.get("row_title_match")):
            continue
        row_key = candidate.get("row_index", candidate.get("index"))
        if row_key not in eligible_row_keys:
            continue
        score = 0
        if text == "下载":
            score += 100
        elif text == "整篇下载":
            score += 90
        cls = str(candidate.get("cls") or "")
        if "wf-list-button" in cls:
            score += 50
        if "t-DIB" in cls:
            score += 20
        if bool(candidate.get("row_title_match")):
            score += 80
        try:
            distance = abs(float(candidate.get("title_y_distance")))
        except Exception:
            distance = 9999
        if distance <= 220:
            score += 20
        scored.append((score, candidate))
    if not scored:
        return {
            "selected": False,
            "candidate": None,
            "reason": "ambiguous_search_result" if title_candidate_count > 1 else "no_exact_title_result",
            "title_candidate_count": title_candidate_count,
            "author_match_count": author_match_count,
            "author_disambiguation_used": author_disambiguation_used,
        }
    scored.sort(key=lambda item: item[0], reverse=True)
    chosen = dict(scored[0][1])
    chosen["score"] = scored[0][0]
    chosen.update(
        {
            "title_candidate_count": title_candidate_count,
            "author_match_count": author_match_count,
            "author_disambiguation_used": author_disambiguation_used,
        }
    )
    return {
        "selected": True,
        "candidate": chosen,
        "reason": "",
        "title_candidate_count": title_candidate_count,
        "author_match_count": author_match_count,
        "author_disambiguation_used": author_disambiguation_used,
    }


def choose_wanfang_download_candidate(
    candidates: list[dict[str, object]],
    *,
    title: str = "",
    first_author: str = "",
) -> dict[str, object] | None:
    """Choose a Wanfang control only when one result-row identity remains."""
    selection = _select_wanfang_download_candidate(
        candidates,
        title=title,
        first_author=first_author,
    )
    candidate = selection.get("candidate")
    return dict(candidate) if isinstance(candidate, dict) else None


def summarize_wanfang_capture_result(
    result: dict[str, object],
    *,
    title: str,
    first_author: str = "",
    author_required: bool = False,
    text: str,
    author_signature_text: str = "",
    strict_title_match: bool,
    pdf_path: Path | None = None,
) -> dict[str, object]:
    """Return manifest status fields for a captured Wanfang download result."""
    resolved_pdf_path = wanfang_downloaded_pdf_path(result) if pdf_path is None else pdf_path
    title_match = pdf_title_matches_first_page_block(author_signature_text, title=title)
    normalized_author = normalize_author_name(first_author)
    pdf_first_author = first_author_from_pdf_signature(
        author_signature_text,
        title=title,
        expected_author=first_author,
    )
    author_match = bool(normalized_author and normalized_author == normalize_author_name(pdf_first_author))
    valid_pdf = (
        resolved_pdf_path is not None
        and bool(result.get("pdf_header_valid"))
        and _capture_size_bytes(result) > 10_000
    )
    success = valid_pdf and (title_match or not strict_title_match) and (author_match or not author_required)
    standard_status = (
        "success"
        if success
        else (
            "pdf_candidate_conflict"
            if valid_pdf
            else ("human_verification_required" if result.get("verification_required") else "capture_failed")
        )
    )
    return {
        "title_match": title_match,
        "author_match": author_match,
        "pdf_first_author": pdf_first_author,
        "author_required": author_required,
        "valid_pdf": valid_pdf,
        "text_length": len(text),
        "file_status": "success" if success else ("unverified" if valid_pdf else "missing"),
        "standard_status": standard_status,
    }


def wanfang_next_action_for_result(standard_status: str, result: dict[str, object]) -> str:
    """Return a Wanfang-specific next action for batch manifest rows."""
    if standard_status == "success":
        return "none"
    if standard_status == "human_verification_required":
        return "complete_visible_human_verification_then_rerun_same_output"
    download_click = result.get("download_click") if isinstance(result.get("download_click"), dict) else {}
    reason = str(result.get("reason") or download_click.get("reason") or "")
    if reason == "no_exact_title_result":
        return "inspect_wanfang_search_results_or_refine_query"
    if standard_status == "capture_failed" and (
        reason in {"no_download_control", "candidate_disappeared", "candidate_changed", "no_retry_control", "download_timeout"}
        or wanfang_downloaded_pdf_path(result) is None
    ):
        return "inspect_visible_wanfang_page_and_retry"
    return "inspect_downloaded_pdf"


def wanfang_downloaded_pdf_path(result: dict[str, object]) -> Path | None:
    """Return the captured PDF path only when it names a file."""
    raw_path = str(result.get("pdf_path") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.exists() or not path.is_file():
        return None
    return path


def navigate_wanfang_search(
    page: Any,
    *,
    query: str,
    title: str = "",
    timeout_ms: int = 45_000,
    settle_seconds: float = 2.0,
    auth_domains: tuple[str, ...] = (),
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
            detail["session_status"] = "portal_ready"
            break
        time.sleep(1)
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    current_url = str(getattr(page, "url", "") or "")
    detail.setdefault("session_status", classify_wanfang_page(current_url, _page_title(page), auth_domains=auth_domains))
    detail["page_url"] = safe_wanfang_url(current_url)
    detail["verification_required"] = wanfang_verification_visible(page)
    detail["download_control_visible"] = wanfang_search_results_visible(page, title=title)
    return detail


def inspect_wanfang_result_download(
    page: Any,
    *,
    title: str,
    first_author: str = "",
) -> dict[str, object]:
    """Inspect Wanfang result rows without clicking a download control."""
    expected = _compact_text(title)
    script_args = {
        "expected": expected,
        "row_selector": WANFANG_RESULT_ROW_SELECTOR,
        "title_selector": WANFANG_RESULT_TITLE_SELECTOR,
        "author_selector": WANFANG_RESULT_AUTHOR_SELECTOR,
        "author_link_selector": WANFANG_RESULT_AUTHOR_LINK_SELECTOR,
        "author_value_selector": WANFANG_RESULT_AUTHOR_VALUE_SELECTOR,
        "control_selector": WANFANG_DOWNLOAD_CONTROL_SELECTOR,
        "labels": sorted(WANFANG_DOWNLOAD_LABELS),
    }
    try:
        raw_candidates = page.evaluate(
            """(args) => {
              const expected = args.expected || "";
              const rowSelector = args.row_selector;
              const titleSelector = args.title_selector;
              const authorSelector = args.author_selector;
              const authorLinkSelector = args.author_link_selector;
              const authorValueSelector = args.author_value_selector;
              const controlSelector = args.control_selector;
              const labels = args.labels || [];
              const norm = (s) => String(s || "").replace(/\\s+/g, "").toLowerCase();
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 1 && r.height > 1 && s.display !== "none" && s.visibility !== "hidden";
              };
              const titleValue = (el) => String(el.getAttribute("title") || el.innerText || "").trim();
              const rowTitleValues = (row) => [...row.querySelectorAll(titleSelector)]
                .map((el) => ({ raw: titleValue(el), compact: norm(titleValue(el)) }))
                .filter((item) => item.raw && item.compact);
              const orderedAuthors = (row) => {
                if (!row) return [];
                const linked = [...row.querySelectorAll(authorLinkSelector)];
                const leaves = [...row.querySelectorAll(authorValueSelector)];
                const nodes = linked.length ? linked : (leaves.length ? leaves : [...row.querySelectorAll(authorSelector)]);
                const seen = new Set();
                const result = [];
                for (const node of nodes) {
                  const cleaned = String(node.innerText || node.title || "")
                    .replace(/^\\s*(?:作者|authors?|writers?)\\s*[:：]\\s*/i, "").trim();
                  if (!cleaned || /^(?:作者|authors?|writers?)\\s*[:：]?$/i.test(cleaned)) continue;
                  for (const part of cleaned.split(/[;；、，|\\/]+/)) {
                    const author = part.replace(/^[\\s,，;；、|\\/]+|[\\s,，;；、|\\/]+$/g, "");
                    if (/^\\d{4}年/.test(author)) continue;
                    const key = [...author.toLowerCase()].filter((c) => /[\\p{L}]/u.test(c)).join("");
                    if (key && !seen.has(key)) { seen.add(key); result.push(author); }
                  }
                }
                return result;
              };
              const candidateFromControl = (el, index, row) => {
                const r = el.getBoundingClientRect();
                const rowRect = row ? row.getBoundingClientRect() : null;
                const rowTitles = row ? rowTitleValues(row) : [];
                const rowAuthors = orderedAuthors(row);
                const rowAuthorText = rowAuthors.join(" ");
                const rowIndex = row ? [...document.querySelectorAll(rowSelector)].indexOf(row) : index;
                const matchingTitles = rowTitles.filter((item) => expected && item.compact === expected);
                const titleRects = row
                  ? [...row.querySelectorAll(titleSelector)]
                      .filter((node) => visible(node) && expected && norm(titleValue(node)) === expected)
                      .map((node) => node.getBoundingClientRect())
                  : [];
                const titleDistance = titleRects.length
                  ? Math.min(...titleRects.map((tr) => Math.abs((tr.y + tr.height / 2) - (r.y + r.height / 2))))
                  : 9999;
                const candidateId = `instsci-wanfang-${Date.now()}-${Math.random().toString(36).slice(2)}-${index}`;
                el.setAttribute("data-inst-candidate-id", candidateId);
                return {
                  index,
                  candidate_id: candidateId,
                  text: (el.innerText || el.title || "").replace(/\\s+/g, "").trim(),
                  cls: String(el.className || ""),
                  href: el.href || "",
                  x: Math.round(r.x),
                  y: Math.round(r.y),
                  w: Math.round(r.width),
                  h: Math.round(r.height),
                  row_title_match: Boolean(matchingTitles.length),
                  row_title: matchingTitles[0]?.raw || rowTitles[0]?.raw || "",
                  row_titles: rowTitles.map((item) => item.raw),
                  row_index: rowIndex,
                  row_author_text: rowAuthorText,
                  row_authors: rowAuthors,
                  row_first_author: rowAuthors[0] || "",
                  page_title_match: Boolean(expected && norm(document.body?.innerText || "").includes(expected)),
                  row_y: rowRect ? Math.round(rowRect.y) : null,
                  row_h: rowRect ? Math.round(rowRect.height) : null,
                  title_y_distance: Math.round(titleDistance),
                };
              };
              const allControls = [...document.querySelectorAll(controlSelector)];
              const bodyText = norm(document.body?.innerText || "");
              const candidates = allControls.map((el, index) => {
                const text = (el.innerText || el.title || "").replace(/\\s+/g, "").trim();
                const row = el.closest(rowSelector);
                if (!visible(el) || !labels.includes(text) || !row) return null;
                const candidate = candidateFromControl(el, index, row);
                candidate.page_title_match = Boolean(expected && bodyText.includes(expected));
                return candidate;
              }).filter(Boolean);
              return candidates;
            }""",
            script_args,
        )
        candidates = [dict(candidate) for candidate in (raw_candidates or [])]
        selection = _select_wanfang_download_candidate(
            candidates,
            title=title,
            first_author=first_author,
        )
        candidate = selection.get("candidate")
        evidence = {key: value for key, value in selection.items() if key != "candidate"}
        if not selection.get("selected") or not isinstance(candidate, dict):
            return {**evidence, "candidate_count": len(candidates)}
        return {**evidence, **dict(candidate), "candidate_count": len(candidates)}
    except Exception as exc:
        return {"selected": False, "error": f"{type(exc).__name__}: {exc}"}


def click_wanfang_result_download(
    page: Any,
    *,
    title: str = "",
    first_author: str = "",
    selection: dict[str, object] | None = None,
) -> dict[str, object]:
    """Revalidate and click an inspected Wanfang result-row control."""
    expected = _compact_text(title)
    script_args = {
        "expected": expected,
        "first_author": normalize_author_name(first_author),
        "row_selector": WANFANG_RESULT_ROW_SELECTOR,
        "title_selector": WANFANG_RESULT_TITLE_SELECTOR,
        "author_selector": WANFANG_RESULT_AUTHOR_SELECTOR,
        "author_link_selector": WANFANG_RESULT_AUTHOR_LINK_SELECTOR,
        "author_value_selector": WANFANG_RESULT_AUTHOR_VALUE_SELECTOR,
        "control_selector": WANFANG_DOWNLOAD_CONTROL_SELECTOR,
        "labels": sorted(WANFANG_DOWNLOAD_LABELS),
    }
    try:
        inspected = dict(selection) if isinstance(selection, dict) else inspect_wanfang_result_download(
            page,
            title=title,
            first_author=first_author,
        )
        if not inspected.get("selected"):
            return {
                **inspected,
                "clicked": False,
                "result_found": bool(inspected.get("title_candidate_count")),
            }
        raw = page.evaluate(
            """(selection) => {
              const expected = selection.expected || "";
              const rowSelector = selection.row_selector;
              const titleSelector = selection.title_selector;
              const authorSelector = selection.author_selector;
              const authorLinkSelector = selection.author_link_selector;
              const authorValueSelector = selection.author_value_selector;
              const controlSelector = selection.control_selector;
              const labels = selection.labels || [];
              const norm = (s) => String(s || "").replace(/\\s+/g, "").toLowerCase();
              const authorNorm = (s) => [...String(s || "").toLowerCase()].filter((c) => /[\\p{L}]/u.test(c)).join("");
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 1 && r.height > 1 && s.display !== "none" && s.visibility !== "hidden";
              };
              const titleValue = (el) => String(el.getAttribute("title") || el.innerText || "").trim();
              const rowTitleValues = (row) => [...row.querySelectorAll(titleSelector)]
                .map((el) => ({ raw: titleValue(el), compact: norm(titleValue(el)) }))
                .filter((item) => item.raw && item.compact);
              const firstAuthor = (row) => {
                if (!row) return "";
                const linked = [...row.querySelectorAll(authorLinkSelector)];
                const leaves = [...row.querySelectorAll(authorValueSelector)];
                const nodes = linked.length ? linked : (leaves.length ? leaves : [...row.querySelectorAll(authorSelector)]);
                for (const node of nodes) {
                  const cleaned = String(node.innerText || node.title || "")
                    .replace(/^\\s*(?:作者|authors?|writers?)\\s*[:：]\\s*/i, "").trim();
                  if (!cleaned || /^(?:作者|authors?|writers?)\\s*[:：]?$/i.test(cleaned)) continue;
                  const first = cleaned.split(/[;；、，|\\/]+/)[0]
                    .replace(/^[\\s,，;；、|\\/]+|[\\s,，;；、|\\/]+$/g, "");
                  if (authorNorm(first)) return first;
                }
                return "";
              };
              const controls = [...document.querySelectorAll(controlSelector)];
              const el = controls.find((node) => node.getAttribute("data-inst-candidate-id") === selection.candidate_id);
              if (!el) return { clicked: false, result_found: false, reason: "candidate_changed" };
              const row = el.closest(rowSelector);
              const text = (el.innerText || el.title || "").replace(/\\s+/g, "").trim();
              const href = el.href || "";
              const rowTitles = row ? rowTitleValues(row) : [];
              const rowTitleMatch = Boolean(rowTitles.find((item) => expected && item.compact === expected));
              const rowFirstAuthor = firstAuthor(row);
              const rowAuthorMatch = !selection.author_required || authorNorm(rowFirstAuthor) === selection.first_author;
              if (
                !visible(el) ||
                !row ||
                !labels.includes(text) ||
                text !== selection.text ||
                (selection.href && href !== selection.href) ||
                (expected && !rowTitleMatch) ||
                !rowAuthorMatch
              ) {
                return { clicked: false, result_found: false, reason: "candidate_changed" };
              }
              el.scrollIntoView({ block: "center", inline: "center" });
              el.click();
              return {
                clicked: true,
                result_found: true,
                text,
                href,
                row_title: rowTitles[0]?.raw || "",
                row_first_author: rowFirstAuthor,
              };
            }""",
            {
                **script_args,
                "candidate_id": str(inspected.get("candidate_id") or ""),
                "text": str(inspected.get("text") or ""),
                "href": str(inspected.get("href") or ""),
                "author_required": bool(inspected.get("author_disambiguation_used")),
            },
        )
        result = {**inspected, **dict(raw or {})}
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
    first_author: str = "",
    selection: dict[str, object] | None = None,
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
        "download_click": click_wanfang_result_download(
            page,
            title=title,
            first_author=first_author,
            selection=selection,
        ),
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

    profile = Path(profile_dir or config.wanfang_profile_dir).expanduser()
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
