"""Paper metadata search through the OpenAlex Works API."""

from __future__ import annotations

import logging
import os

from ..http_utils import request_with_retry
from .semantic_scholar import SearchResult


logger = logging.getLogger(__name__)
OPENALEX_WORKS_API = "https://api.openalex.org/works"


def _abstract_from_inverted_index(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in value.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                positioned.append((position, str(word)))
    return " ".join(word for _, word in sorted(positioned))


def _year_filter(year_range: str | None) -> str:
    if not year_range:
        return ""
    start, separator, end = year_range.partition("-")
    filters: list[str] = []
    if start.strip().isdigit():
        filters.append(f"from_publication_date:{start.strip()}-01-01")
    if separator and end.strip().isdigit():
        filters.append(f"to_publication_date:{end.strip()}-12-31")
    return ",".join(filters)


def search(
    query: str,
    limit: int = 10,
    year_range: str | None = None,
    *,
    email: str = "",
    api_key: str = "",
) -> list[SearchResult]:
    params: dict[str, object] = {"search": query, "per-page": min(max(limit, 1), 100)}
    filter_value = _year_filter(year_range)
    if filter_value:
        params["filter"] = filter_value
    api_key = api_key or os.environ.get("OPENALEX_API_KEY", "")
    if api_key:
        params["api_key"] = api_key
    if email:
        params["mailto"] = email
    headers = {"User-Agent": f"instsci/0.1{f' (mailto:{email})' if email else ''}"}
    try:
        response = request_with_retry("GET", OPENALEX_WORKS_API, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("OpenAlex search failed: %s", exc)
        return []

    results: list[SearchResult] = []
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        ids = item.get("ids") or {}
        doi = str(item.get("doi") or ids.get("doi") or "")
        doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
        authors = []
        for authorship in item.get("authorships") or []:
            author = authorship.get("author") or {}
            name = str(author.get("display_name") or "")
            if name:
                authors.append(name)
        location = item.get("primary_location") or {}
        source = location.get("source") or {}
        results.append(
            SearchResult(
                title=str(item.get("display_name") or item.get("title") or ""),
                authors=authors,
                year=item.get("publication_year"),
                abstract=_abstract_from_inverted_index(item.get("abstract_inverted_index")),
                doi=doi,
                journal=str(source.get("display_name") or ""),
                citation_count=int(item.get("cited_by_count") or 0),
                s2_url=str(item.get("id") or ""),
                paper_id=str(item.get("id") or "").rsplit("/", 1)[-1],
            )
        )
    return results
