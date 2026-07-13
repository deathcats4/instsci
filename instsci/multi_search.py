"""Multi-provider paper search with DOI-aware metadata merging."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from .search_pipeline import normalize_doi
from .sources import crossref, openalex, semantic_scholar
from .sources.errors import ProviderSearchError, classify_provider_exception


DEFAULT_SOURCES = ("semantic_scholar", "openalex", "crossref")


@dataclass
class MergedSearchResult:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    abstract: str = ""
    doi: str = ""
    arxiv_id: str = ""
    journal: str = ""
    citation_count: int = 0
    s2_url: str = ""
    paper_id: str = ""
    sources: list[str] = field(default_factory=list)
    citation_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class MultiSearchResponse:
    results: list[MergedSearchResult] = field(default_factory=list)
    source_status: dict[str, dict[str, object]] = field(default_factory=dict)


def parse_sources(value: str | None) -> list[str]:
    requested = [item.strip().lower().replace("-", "_") for item in (value or "").split(",") if item.strip()]
    sources = requested or list(DEFAULT_SOURCES)
    unknown = [source for source in sources if source not in DEFAULT_SOURCES]
    if unknown:
        raise ValueError(f"Unknown search source: {unknown[0]}")
    return list(dict.fromkeys(sources))


def _title_key(title: str, year: int | None) -> str:
    normalized = " ".join("".join(character.lower() if character.isalnum() else " " for character in title).split())
    return f"title:{normalized}|year:{year or ''}" if normalized else ""


def _from_provider(result: object, source: str) -> MergedSearchResult:
    citations = int(getattr(result, "citation_count", 0) or 0)
    return MergedSearchResult(
        title=str(getattr(result, "title", "") or ""),
        authors=list(getattr(result, "authors", []) or []),
        year=getattr(result, "year", None),
        abstract=str(getattr(result, "abstract", "") or ""),
        doi=normalize_doi(str(getattr(result, "doi", "") or "")),
        arxiv_id=str(getattr(result, "arxiv_id", "") or ""),
        journal=str(getattr(result, "journal", "") or ""),
        citation_count=citations,
        s2_url=str(getattr(result, "s2_url", "") or ""),
        paper_id=str(getattr(result, "paper_id", "") or ""),
        sources=[source],
        citation_counts={source: citations},
    )


def _merge(target: MergedSearchResult, incoming: MergedSearchResult) -> None:
    for field_name in ("title", "authors", "year", "abstract", "doi", "arxiv_id", "journal", "s2_url", "paper_id"):
        if not getattr(target, field_name) and getattr(incoming, field_name):
            setattr(target, field_name, getattr(incoming, field_name))
    for source in incoming.sources:
        if source not in target.sources:
            target.sources.append(source)
    target.citation_counts.update(incoming.citation_counts)
    target.citation_count = max(target.citation_counts.values(), default=0)


def search_with_status(
    query: str,
    limit: int = 10,
    year_range: str | None = None,
    *,
    sources: str | None = None,
    email: str = "",
) -> MultiSearchResponse:
    selected_sources = parse_sources(sources)
    providers: dict[str, Callable[[], list[object]]] = {
        "semantic_scholar": lambda: semantic_scholar.search(
            query, limit=limit, year_range=year_range, raise_on_error=True
        ),
        "openalex": lambda: openalex.search(
            query, limit=limit, year_range=year_range, email=email, raise_on_error=True
        ),
        "crossref": lambda: crossref.search(
            query, limit=limit, year_range=year_range, email=email, raise_on_error=True
        ),
    }
    provider_results: dict[str, list[object]] = {source: [] for source in selected_sources}
    source_status: dict[str, dict[str, object]] = {
        source: {"status": "pending", "count": 0} for source in selected_sources
    }
    with ThreadPoolExecutor(max_workers=len(selected_sources)) as executor:
        futures = {executor.submit(providers[source]): source for source in selected_sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                provider_results[source] = future.result()
                source_status[source] = {"status": "success", "count": len(provider_results[source])}
            except ProviderSearchError as exc:
                source_status[source] = {"status": exc.status, "count": 0}
                provider_results[source] = []
            except Exception as exc:
                source_status[source] = {
                    "status": classify_provider_exception(exc),
                    "count": 0,
                }
                provider_results[source] = []

    merged: list[MergedSearchResult] = []
    doi_aliases: dict[str, MergedSearchResult] = {}
    title_aliases: dict[str, list[MergedSearchResult]] = {}
    max_results = max((len(items) for items in provider_results.values()), default=0)
    for position in range(max_results):
        for source in selected_sources:
            if position >= len(provider_results[source]):
                continue
            raw_result = provider_results[source][position]
            incoming = _from_provider(raw_result, source)
            doi_key = f"doi:{incoming.doi.lower()}" if incoming.doi else ""
            title_key = _title_key(incoming.title, incoming.year)
            target = doi_aliases.get(doi_key) if doi_key else None
            if target is None and title_key:
                eligible = [
                    candidate
                    for candidate in title_aliases.get(title_key, [])
                    if not (candidate.doi and incoming.doi and candidate.doi != incoming.doi)
                ]
                if len(eligible) == 1:
                    target = eligible[0]
            if target is None:
                target = incoming
                merged.append(target)
                if title_key:
                    title_aliases.setdefault(title_key, []).append(target)
            else:
                _merge(target, incoming)
            if doi_key:
                doi_aliases[doi_key] = target
            if title_key and target not in title_aliases.setdefault(title_key, []):
                title_aliases[title_key].append(target)
    return MultiSearchResponse(results=merged[: max(limit, 0)], source_status=source_status)


def search(
    query: str,
    limit: int = 10,
    year_range: str | None = None,
    *,
    sources: str | None = None,
    email: str = "",
) -> list[MergedSearchResult]:
    """Compatibility wrapper returning only merged results."""
    return search_with_status(
        query,
        limit=limit,
        year_range=year_range,
        sources=sources,
        email=email,
    ).results
