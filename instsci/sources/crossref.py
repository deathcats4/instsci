"""Paper metadata search through the Crossref REST API."""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from ..http_utils import request_with_retry
from .semantic_scholar import SearchResult


logger = logging.getLogger(__name__)
CROSSREF_WORKS_API = "https://api.crossref.org/works"


def _first(value: object) -> str:
    if isinstance(value, list) and value:
        return str(value[0] or "")
    return str(value or "")


def _year(item: dict) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        date_parts = (item.get(key) or {}).get("date-parts") or []
        if date_parts and date_parts[0] and isinstance(date_parts[0][0], int):
            return date_parts[0][0]
    return None


def _year_filter(year_range: str | None) -> str:
    if not year_range:
        return ""
    start, separator, end = year_range.partition("-")
    values: list[str] = []
    if start.strip().isdigit():
        values.append(f"from-pub-date:{start.strip()}-01-01")
    if separator and end.strip().isdigit():
        values.append(f"until-pub-date:{end.strip()}-12-31")
    return ",".join(values)


def search(query: str, limit: int = 10, year_range: str | None = None, *, email: str = "") -> list[SearchResult]:
    params: dict[str, object] = {"query.bibliographic": query, "rows": min(max(limit, 1), 100)}
    filter_value = _year_filter(year_range)
    if filter_value:
        params["filter"] = filter_value
    if email:
        params["mailto"] = email
    headers = {"User-Agent": f"instsci/0.1{f' (mailto:{email})' if email else ''}"}
    try:
        response = request_with_retry("GET", CROSSREF_WORKS_API, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        items = (response.json().get("message") or {}).get("items") or []
    except Exception as exc:
        logger.warning("Crossref search failed: %s", exc)
        return []

    results: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        authors = []
        for author in item.get("author") or []:
            name = " ".join(part for part in (str(author.get("given") or ""), str(author.get("family") or "")) if part)
            if name:
                authors.append(name)
        abstract_html = str(item.get("abstract") or "")
        abstract = BeautifulSoup(abstract_html, "html.parser").get_text(" ", strip=True) if abstract_html else ""
        results.append(
            SearchResult(
                title=_first(item.get("title")),
                authors=authors,
                year=_year(item),
                abstract=abstract,
                doi=str(item.get("DOI") or ""),
                journal=_first(item.get("container-title")),
                citation_count=int(item.get("is-referenced-by-count") or 0),
                s2_url=str(item.get("URL") or ""),
                paper_id=str(item.get("DOI") or ""),
            )
        )
    return results
