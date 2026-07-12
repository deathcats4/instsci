"""Structured search-result export and DOI selection helpers."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SEARCH_SCHEMA = "instsci.search_results.v1"
SELECTION_SCHEMA = "instsci.search_selection.v1"


def normalize_doi(value: str) -> str:
    """Return a stable bare DOI value suitable for downstream DOI files."""
    normalized = str(value or "").strip()
    lower = normalized.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lower.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break
    return normalized.lower()


def result_to_record(result: Any, index: int, *, source: str = "semantic_scholar") -> dict[str, Any]:
    """Convert a provider result object into the public search-result contract."""
    return {
        "index": index,
        "source": (list(getattr(result, "sources", []) or [source]))[0],
        "sources": list(getattr(result, "sources", []) or [source]),
        "paper_id": str(getattr(result, "paper_id", "") or ""),
        "title": str(getattr(result, "title", "") or ""),
        "authors": list(getattr(result, "authors", []) or []),
        "year": getattr(result, "year", None),
        "abstract": str(getattr(result, "abstract", "") or ""),
        "doi": normalize_doi(str(getattr(result, "doi", "") or "")),
        "arxiv_id": str(getattr(result, "arxiv_id", "") or ""),
        "journal": str(getattr(result, "journal", "") or ""),
        "citation_count": int(getattr(result, "citation_count", 0) or 0),
        "citation_counts": dict(getattr(result, "citation_counts", {}) or {}),
        "url": str(getattr(result, "s2_url", "") or ""),
    }


def build_search_payload(
    query: str,
    results: Iterable[Any],
    *,
    year_range: str = "",
    source: str = "semantic_scholar",
) -> dict[str, Any]:
    records = [result_to_record(result, index, source=source) for index, result in enumerate(results, 1)]
    sources = list(
        dict.fromkeys(
            item
            for record in records
            for item in (record.get("sources") or [record.get("source")])
            if item
        )
    ) or [source]
    return {
        "schema": SEARCH_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query": query,
        "year_range": year_range,
        "sources": sources,
        "count": len(records),
        "results": records,
    }


def resolve_search_output(path: str | Path) -> Path:
    output = Path(path)
    if not output.suffix:
        output = output.with_suffix(".json")
    if output.suffix.lower() not in {".json", ".csv"}:
        raise ValueError("Search output must use .json or .csv.")
    return output


def write_search_payload(payload: dict[str, Any], path: str | Path) -> Path:
    output = resolve_search_output(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".json":
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return output

    fieldnames = [
        "index", "source", "sources", "paper_id", "title", "authors", "year", "abstract",
        "doi", "arxiv_id", "journal", "citation_count", "citation_counts", "url",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in payload.get("results", []):
            row = dict(record)
            row["authors"] = "; ".join(row.get("authors") or [])
            row["sources"] = "; ".join(row.get("sources") or [])
            row["citation_counts"] = json.dumps(row.get("citation_counts") or {}, ensure_ascii=False, sort_keys=True)
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return output


def load_search_payload(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("Search JSON must contain a results array.")
        return payload
    if source.suffix.lower() == ".csv":
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        records: list[dict[str, Any]] = []
        for position, row in enumerate(rows, 1):
            records.append(
                {
                    **row,
                    "index": int(row.get("index") or position),
                    "authors": [part.strip() for part in str(row.get("authors") or "").split(";") if part.strip()],
                    "sources": [part.strip() for part in str(row.get("sources") or "").split(";") if part.strip()],
                    "year": int(row["year"]) if str(row.get("year") or "").isdigit() else None,
                    "citation_count": int(row.get("citation_count") or 0),
                    "citation_counts": json.loads(row.get("citation_counts") or "{}"),
                    "doi": normalize_doi(str(row.get("doi") or "")),
                }
            )
        return {"schema": SEARCH_SCHEMA, "query": "", "count": len(records), "results": records}
    raise ValueError("Search input must use .json or .csv.")


def parse_selection_indices(value: str, total: int) -> list[int]:
    """Parse one-based values such as ``1,3-5``; an empty value selects all."""
    if total < 0:
        raise ValueError("Result count cannot be negative.")
    if not value.strip():
        return list(range(1, total + 1))
    selected: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"Invalid selection range: {token}")
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"Selection range is reversed: {token}")
            selected.update(range(start, end + 1))
        elif token.isdigit():
            selected.add(int(token))
        else:
            raise ValueError(f"Invalid selection index: {token}")
    invalid = sorted(index for index in selected if index < 1 or index > total)
    if invalid:
        raise ValueError(f"Selection index out of range: {invalid[0]}")
    return sorted(selected)


def select_doi_records(payload: dict[str, Any], indices: list[int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results = payload.get("results") or []
    by_index = {int(record.get("index") or position): record for position, record in enumerate(results, 1)}
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in indices:
        record = dict(by_index[index])
        doi = normalize_doi(str(record.get("doi") or ""))
        if not doi:
            skipped.append({"index": index, "reason": "missing_doi", "title": record.get("title", "")})
            continue
        key = doi.lower()
        if key in seen:
            skipped.append({"index": index, "reason": "duplicate_doi", "doi": doi, "title": record.get("title", "")})
            continue
        seen.add(key)
        record["doi"] = doi
        selected.append(record)
    return selected, skipped


def write_selection(
    search_path: str | Path,
    payload: dict[str, Any],
    indices: list[int],
    output: str | Path,
) -> tuple[Path, Path, dict[str, Any]]:
    selected, skipped = select_doi_records(payload, indices)
    doi_path = Path(output)
    doi_path.parent.mkdir(parents=True, exist_ok=True)
    doi_path.write_text("".join(f"{record['doi']}\n" for record in selected), encoding="utf-8")
    report = {
        "schema": SELECTION_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "search_input": str(Path(search_path)),
        "requested_indices": indices,
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "selected": selected,
        "skipped": skipped,
        "doi_file": str(doi_path),
    }
    report_path = doi_path.with_suffix(doi_path.suffix + ".selection.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return doi_path, report_path, report
