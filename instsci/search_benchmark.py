"""Offline search-quality metrics for InstSci Search v2."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .search_pipeline import normalize_doi


DEFAULT_K_VALUES = (10, 20, 50)
SEARCH_BENCHMARK_VALIDATION_SCHEMA = "instsci.search_benchmark_validation.v1"
RELEVANCE_POOL_SCHEMA = "instsci.relevance_pool.v1"
RELEVANCE_POOL_VALIDATION_SCHEMA = "instsci.relevance_pool_validation.v1"
RELEASE_GATE_SCHEMA = "instsci.search_release_gate.v1"
RELEASE_GATE_VALIDATION_SCHEMA = "instsci.search_release_gate_validation.v1"
RANKING_SNAPSHOT_SCHEMA = "instsci.ranking_snapshot.v1"
RANKING_SNAPSHOT_VALIDATION_SCHEMA = "instsci.ranking_snapshot_validation.v1"
RANKING_SNAPSHOT_CHECK_SCHEMA = "instsci.ranking_snapshot_check.v1"
RANKING_SNAPSHOT_CHECK_VALIDATION_SCHEMA = "instsci.ranking_snapshot_check_validation.v1"


def canonical_result_id(record: dict[str, Any]) -> str:
    """Return the stable identifier used for search benchmark judgments."""
    canonical = str(record.get("canonical_work_id") or "").strip().lower()
    if canonical:
        return canonical
    doi = normalize_doi(str(record.get("doi") or ""))
    if doi:
        return f"doi:{doi}"
    arxiv_id = str(record.get("arxiv_id") or "").strip().lower()
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    pmid = str(record.get("pmid") or "").strip()
    if pmid:
        return f"pmid:{pmid}"
    paper_id = str(record.get("paper_id") or "").strip().lower()
    if paper_id:
        return f"paper:{paper_id}"
    title = " ".join(str(record.get("title") or "").lower().split())
    year = str(record.get("year") or "")
    return f"title:{title}|year:{year}" if title else ""


def build_relevance_pool(
    payloads: dict[str, dict[str, Any]],
    *,
    legacy_top: int = 30,
    hybrid_top: int = 30,
    channel_top: int = 10,
    seed: str = RELEVANCE_POOL_SCHEMA,
) -> dict[str, Any]:
    """Build a deduplicated, source-blinded relevance judgment template.

    The pool includes legacy top-N, hybrid top-N, and any channel-level top-N
    candidates exposed through retrieval provenance. The output intentionally
    excludes source labels and ranks from judgment rows so it can be handed to
    a reviewer without revealing which strategy found each paper.
    """
    pooled: dict[str, dict[str, Any]] = {}
    query = ""
    for label, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        query = query or str(payload.get("query") or "")
        records = [record for record in payload.get("results") or [] if isinstance(record, dict)]
        strategy = str((payload.get("query_plan") or {}).get("strategy") or label).lower()
        if "legacy" in strategy:
            _add_pool_records(pooled, records[: max(legacy_top, 0)])
        if "hybrid" in strategy:
            _add_pool_records(pooled, records[: max(hybrid_top, 0)])
        if str(label).lower().startswith("channel:"):
            _add_pool_records(pooled, records[: max(channel_top, 0)])
        _add_channel_pool_records(pooled, records, channel_top=max(channel_top, 0))

    ordered = sorted(
        pooled.values(),
        key=lambda item: hashlib.sha256(f"{seed}:{item['id']}".encode("utf-8")).hexdigest(),
    )
    judgments = []
    for position, item in enumerate(ordered, 1):
        judgments.append(
            {
                "review_id": f"P{position:04d}",
                "id": item["id"],
                "title": item.get("title", ""),
                "authors": item.get("authors", []),
                "year": item.get("year"),
                "doi": item.get("doi", ""),
                "arxiv_id": item.get("arxiv_id", ""),
                "journal": item.get("journal", ""),
                "abstract": item.get("abstract", ""),
                "grade": None,
                "notes": "",
            }
        )
    return {
        "schema": RELEVANCE_POOL_SCHEMA,
        "query": query,
        "pooling": {
            "legacy_top": legacy_top,
            "hybrid_top": hybrid_top,
            "channel_top": channel_top,
            "seed": seed,
            "anonymous": True,
            "grade_scale": {
                "3": "highly relevant",
                "2": "relevant",
                "1": "marginal",
                "0": "irrelevant",
            },
        },
        "count": len(judgments),
        "judgments": judgments,
    }


def _add_pool_records(pooled: dict[str, dict[str, Any]], records: Iterable[dict[str, Any]]) -> None:
    for record in records:
        item_id = canonical_result_id(record)
        if item_id and item_id not in pooled:
            pooled[item_id] = _pool_item(item_id, record)


def _add_channel_pool_records(
    pooled: dict[str, dict[str, Any]],
    records: Iterable[dict[str, Any]],
    *,
    channel_top: int,
) -> None:
    if channel_top <= 0:
        return
    for record in records:
        provenance = record.get("retrieval_provenance") or []
        if not isinstance(provenance, list):
            continue
        for item in provenance:
            if not isinstance(item, dict):
                continue
            rank = item.get("rank")
            if isinstance(rank, int) and 1 <= rank <= channel_top:
                _add_pool_records(pooled, [record])
                break


def _pool_item(item_id: str, record: dict[str, Any]) -> dict[str, Any]:
    authors = record.get("authors") or []
    if isinstance(authors, str):
        authors = [part.strip() for part in authors.split(";") if part.strip()]
    return {
        "id": item_id,
        "title": str(record.get("title") or ""),
        "authors": list(authors) if isinstance(authors, list) else [],
        "year": record.get("year"),
        "doi": normalize_doi(str(record.get("doi") or "")),
        "arxiv_id": str(record.get("arxiv_id") or ""),
        "journal": str(record.get("journal") or ""),
        "abstract": str(record.get("abstract") or ""),
    }


def validate_relevance_pool(pool: dict[str, Any]) -> dict[str, Any]:
    """Validate a pooled relevance-judgment template without changing grades."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(pool, dict):
        errors.append("payload must be an object")
        pool = {}

    source_schema = str(pool.get("schema") or "")
    if not source_schema:
        errors.append("schema missing")
    elif source_schema != RELEVANCE_POOL_SCHEMA:
        errors.append(f"schema must be {RELEVANCE_POOL_SCHEMA}")

    pooling = pool.get("pooling")
    anonymous = False
    if not isinstance(pooling, dict):
        errors.append("pooling must be an object")
    else:
        anonymous = pooling.get("anonymous") is True
        if not anonymous:
            errors.append("pooling.anonymous must be true")
        for field in ("legacy_top", "hybrid_top", "channel_top"):
            if field in pooling:
                _gate_non_negative_int(pooling.get(field), f"pooling.{field}", errors)
        grade_scale = pooling.get("grade_scale")
        if grade_scale is not None and not isinstance(grade_scale, dict):
            errors.append("pooling.grade_scale must be an object")

    judgments_value = pool.get("judgments")
    if not isinstance(judgments_value, list):
        errors.append("judgments must be a list")
        judgments: list[dict[str, Any]] = []
    else:
        judgments = [item for item in judgments_value if isinstance(item, dict)]
        for index, item in enumerate(judgments_value):
            if not isinstance(item, dict):
                errors.append(f"judgments[{index}] must be an object")

    try:
        count = int(pool.get("count"))
    except (TypeError, ValueError):
        errors.append("count must be an integer")
        count = len(judgments)
    if count != len(judgments):
        errors.append("count does not match judgments length")

    seen_review_ids: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(judgments):
        review_id = str(item.get("review_id") or "").strip()
        item_id = str(item.get("id") or "").strip().lower()
        if not review_id:
            errors.append(f"judgments[{index}].review_id is required")
        elif review_id in seen_review_ids:
            errors.append(f"judgments[{index}].review_id duplicates an earlier row")
        seen_review_ids.add(review_id)
        if not item_id:
            errors.append(f"judgments[{index}].id is required")
        elif item_id in seen_ids:
            errors.append(f"judgments[{index}].id duplicates an earlier row")
        seen_ids.add(item_id)
        if not str(item.get("title") or "").strip():
            errors.append(f"judgments[{index}].title is required")
        grade = item.get("grade") if item.get("grade") is not None else item.get("relevance")
        if grade is not None and grade != "":
            try:
                grade_value = int(grade)
            except (TypeError, ValueError):
                errors.append(f"judgments[{index}].grade must be null or an integer from 0-3")
            else:
                if grade_value < 0 or grade_value > 3:
                    errors.append(f"judgments[{index}].grade must be null or an integer from 0-3")
        if "pool_sources" in item:
            errors.append(f"judgments[{index}].pool_sources must not be present in blinded pools")
        if "retrieval_provenance" in item:
            errors.append(f"judgments[{index}].retrieval_provenance must not be present in blinded pools")

    return {
        "schema": RELEVANCE_POOL_VALIDATION_SCHEMA,
        "valid": not errors,
        "source_schema": source_schema,
        "summary": {
            "error_count": len(errors),
            "warning_count": len(warnings),
            "judgment_count": len(judgments),
        },
        "errors": errors,
        "warnings": warnings,
    }


def load_judgments(path: str | Path) -> dict[str, int]:
    """Load pooled relevance judgments from a JSON mapping or record list.

    Accepted formats:
    - {"doi:10.x/a": 3, "doi:10.x/b": 0}
    - [{"id": "doi:10.x/a", "grade": 3}, ...]
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        if isinstance(payload.get("judgments"), list):
            return _judgment_list_to_map(payload["judgments"])
        return {str(key).lower(): _coerce_relevance_grade(value, str(key)) for key, value in payload.items()}
    if isinstance(payload, list):
        return _judgment_list_to_map(payload)
    raise ValueError("Judgment file must contain a JSON object or array.")


def load_judgment_coverage(path: str | Path) -> dict[str, int | bool]:
    """Count total, graded, and ungraded rows in a relevance judgment file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        if isinstance(payload.get("judgments"), list):
            return _judgment_coverage_from_rows(payload["judgments"])
        total = len(payload)
        return {
            "judgment_count": total,
            "graded_judgment_count": total,
            "ungraded_judgment_count": 0,
            "all_judgments_graded": True,
        }
    if isinstance(payload, list):
        return _judgment_coverage_from_rows(payload)
    return {
        "judgment_count": 0,
        "graded_judgment_count": 0,
        "ungraded_judgment_count": 0,
        "all_judgments_graded": True,
    }


def _judgment_coverage_from_rows(rows: Iterable[Any]) -> dict[str, int | bool]:
    total = 0
    graded = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or row.get("canonical_id") or row.get("canonical_work_id") or "").strip()
        if not key and row.get("doi"):
            key = normalize_doi(str(row.get("doi") or ""))
        if not key:
            continue
        total += 1
        grade_value = row.get("grade") if row.get("grade") is not None else row.get("relevance")
        if grade_value is not None and grade_value != "":
            graded += 1
    ungraded = total - graded
    return {
        "judgment_count": total,
        "graded_judgment_count": graded,
        "ungraded_judgment_count": ungraded,
        "all_judgments_graded": ungraded == 0,
    }

def _judgment_list_to_map(rows: Iterable[Any]) -> dict[str, int]:
    judgments: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or row.get("canonical_id") or row.get("canonical_work_id") or "").strip().lower()
        if not key and row.get("doi"):
            key = f"doi:{normalize_doi(str(row.get('doi') or ''))}"
        if not key:
            continue
        grade_value = row.get("grade") if row.get("grade") is not None else row.get("relevance")
        if grade_value is None or grade_value == "":
            continue
        judgments[key] = _coerce_relevance_grade(grade_value, key)
    return judgments


def _coerce_relevance_grade(value: Any, item_id: str) -> int:
    try:
        grade = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Relevance grade for {item_id or ""} must be an integer from 0-3.") from exc
    if grade < 0 or grade > 3:
        raise ValueError(f"Relevance grade for {item_id or ""} must be an integer from 0-3.")
    return grade


def load_must_find(path: str | Path) -> list[str]:
    """Load known must-find identifiers for recall-only benchmark diagnostics."""
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        rows = payload.get("must_find") or payload.get("ids") or payload.get("items") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("Must-find file must contain a JSON object or array.")
    return _normalize_must_find_ids(rows)


def _normalize_must_find_ids(rows: Iterable[Any] | None) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        item_id = _must_find_id(row)
        if item_id and item_id not in seen:
            ids.append(item_id)
            seen.add(item_id)
    return ids


def _must_find_id(row: Any) -> str:
    if isinstance(row, dict):
        explicit = str(row.get("id") or row.get("canonical_id") or row.get("canonical_work_id") or "").strip()
        if explicit:
            return _normalize_external_identifier(explicit)
        item_id = canonical_result_id(row)
        return item_id
    return _normalize_external_identifier(str(row or ""))


def _normalize_external_identifier(value: str) -> str:
    text = value.strip()
    lowered = text.lower()
    if not text:
        return ""
    if lowered.startswith("doi:"):
        doi = normalize_doi(text.split(":", 1)[1])
        return f"doi:{doi}" if doi else lowered
    if lowered.startswith(("arxiv:", "pmid:", "paper:", "title:")):
        return lowered
    doi = normalize_doi(text)
    if doi:
        return f"doi:{doi}"
    return lowered


def evaluate_ranked_results(
    records: Iterable[dict[str, Any]],
    judgments: dict[str, int],
    *,
    k_values: Iterable[int] = DEFAULT_K_VALUES,
    relevant_threshold: int = 2,
    must_find_ids: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a ranked candidate list against pooled relevance judgments."""
    ranked = [record for record in records if isinstance(record, dict)]
    normalized_judgments = {str(key).lower(): int(value) for key, value in judgments.items()}
    ids = [canonical_result_id(record) for record in ranked]
    relevant_total = sum(1 for grade in normalized_judgments.values() if grade >= relevant_threshold)
    metrics: dict[str, Any] = {
        "count": len(ranked),
        "judged_count": sum(1 for item_id in ids if item_id in normalized_judgments),
        "relevant_total": relevant_total,
        "duplicate_rate": duplicate_rate(ids),
    }
    normalized_must_find = _normalize_must_find_ids(must_find_ids)
    must_find_set = set(normalized_must_find)
    if must_find_set:
        metrics["must_find_total"] = len(must_find_set)

    for k in sorted({int(value) for value in k_values if int(value) > 0}):
        top_ids = ids[:k]
        relevant_hits = sum(1 for item_id in top_ids if normalized_judgments.get(item_id, 0) >= relevant_threshold)
        metrics[f"precision@{k}"] = relevant_hits / k
        metrics[f"recall@{k}"] = relevant_hits / relevant_total if relevant_total else 0.0
        metrics[f"ndcg@{k}"] = ndcg_at_k(top_ids, normalized_judgments, k)
        if must_find_set:
            must_find_hits = sum(1 for item_id in top_ids if item_id in must_find_set)
            metrics[f"must_find_hits@{k}"] = must_find_hits
            metrics[f"must_find_recall@{k}"] = must_find_hits / len(must_find_set)

    metrics["mrr"] = mean_reciprocal_rank(ids, normalized_judgments, relevant_threshold=relevant_threshold)
    return metrics


def compare_ranked_results(
    candidate_records: Iterable[dict[str, Any]],
    baseline_records: Iterable[dict[str, Any]],
    judgments: dict[str, int],
    *,
    k_values: Iterable[int] = DEFAULT_K_VALUES,
    relevant_threshold: int = 2,
    must_find_ids: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Compare a candidate ranking against a baseline ranking with same judgments."""
    candidate_records = [record for record in candidate_records if isinstance(record, dict)]
    baseline_records = [record for record in baseline_records if isinstance(record, dict)]
    candidate = evaluate_ranked_results(
        candidate_records,
        judgments,
        k_values=k_values,
        relevant_threshold=relevant_threshold,
        must_find_ids=must_find_ids,
    )
    baseline = evaluate_ranked_results(
        baseline_records,
        judgments,
        k_values=k_values,
        relevant_threshold=relevant_threshold,
        must_find_ids=must_find_ids,
    )
    deltas: dict[str, float] = {}
    for key, value in candidate.items():
        if isinstance(value, (int, float)) and isinstance(baseline.get(key), (int, float)):
            deltas[key] = float(value) - float(baseline[key])
    return {
        "candidate": candidate,
        "baseline": baseline,
        "delta": deltas,
        "candidate_ranking_snapshot": ranking_snapshot(candidate_records),
        "baseline_ranking_snapshot": ranking_snapshot(baseline_records),
    }


def validate_benchmark_metrics_report(report: dict[str, Any]) -> dict[str, Any]:
    """Validate saved offline benchmark metrics without recomputing rankings."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(report, dict):
        errors.append("payload must be an object")
        report = {}

    is_comparison = isinstance(report.get("candidate"), dict) or isinstance(report.get("baseline"), dict)
    if is_comparison:
        candidate = report.get("candidate") if isinstance(report.get("candidate"), dict) else {}
        baseline = report.get("baseline") if isinstance(report.get("baseline"), dict) else {}
        delta = report.get("delta") if isinstance(report.get("delta"), dict) else {}
        if not isinstance(report.get("candidate"), dict):
            errors.append("candidate must be an object")
        if not isinstance(report.get("baseline"), dict):
            errors.append("baseline must be an object")
        if not isinstance(report.get("delta"), dict):
            errors.append("delta must be an object")
        _validate_metric_block(candidate, "candidate", errors)
        _validate_metric_block(baseline, "baseline", errors)
        _validate_delta_block(candidate, baseline, delta, errors)
        _validate_snapshot_field(report, "candidate_ranking_snapshot", errors)
        _validate_snapshot_field(report, "baseline_ranking_snapshot", errors)
    else:
        _validate_metric_block(report, "", errors)

    _validate_benchmark_coverage(report, errors)
    return {
        "schema": SEARCH_BENCHMARK_VALIDATION_SCHEMA,
        "valid": not errors,
        "source_schema": str(report.get("schema") or ""),
        "summary": {
            "error_count": len(errors),
            "warning_count": len(warnings),
            "comparison": is_comparison,
        },
        "errors": errors,
        "warnings": warnings,
    }


def ranking_snapshot(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a compact deterministic ranked-ID snapshot for audit reports."""
    snapshot: list[dict[str, Any]] = []
    for rank, record in enumerate((item for item in records if isinstance(item, dict)), 1):
        snapshot.append(
            {
                "rank": rank,
                "id": canonical_result_id(record),
                "title": str(record.get("title") or ""),
                "doi": normalize_doi(str(record.get("doi") or "")),
                "arxiv_id": str(record.get("arxiv_id") or ""),
                "year": record.get("year"),
            }
        )
    return snapshot


def build_ranking_snapshot_payload(payload: dict[str, Any], *, label: str = "") -> dict[str, Any]:
    """Build a persistent ranked-ID snapshot from a saved search payload."""
    records = [record for record in payload.get("results") or [] if isinstance(record, dict)]
    snapshot = ranking_snapshot(records)
    return {
        "schema": RANKING_SNAPSHOT_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": str(label or ""),
        "query": str(payload.get("query") or ""),
        "source_schema": str(payload.get("schema") or ""),
        "count": len(snapshot),
        "ranking": snapshot,
    }


def compare_ranking_snapshot_payload(payload: dict[str, Any], snapshot_payload: dict[str, Any]) -> dict[str, Any]:
    """Compare a saved search payload against a previous ranking snapshot."""
    current = ranking_snapshot(payload.get("results") or [])
    expected = [item for item in snapshot_payload.get("ranking") or [] if isinstance(item, dict)]
    current_rank_by_id = {str(item.get("id") or ""): int(item.get("rank") or 0) for item in current if item.get("id")}
    expected_rank_by_id = {str(item.get("id") or ""): int(item.get("rank") or 0) for item in expected if item.get("id")}
    missing_ids = sorted(item_id for item_id in expected_rank_by_id if item_id not in current_rank_by_id)
    new_ids = sorted(item_id for item_id in current_rank_by_id if item_id not in expected_rank_by_id)
    rank_changes = []
    for item_id in sorted(set(current_rank_by_id) & set(expected_rank_by_id)):
        expected_rank = expected_rank_by_id[item_id]
        current_rank = current_rank_by_id[item_id]
        if expected_rank != current_rank:
            rank_changes.append(
                {
                    "id": item_id,
                    "expected_rank": expected_rank,
                    "current_rank": current_rank,
                    "delta": current_rank - expected_rank,
                }
            )
    matched = not missing_ids and not new_ids and not rank_changes and len(current) == len(expected)
    return {
        "schema": RANKING_SNAPSHOT_CHECK_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "matched": matched,
        "query": str(payload.get("query") or ""),
        "snapshot_label": str(snapshot_payload.get("label") or ""),
        "summary": {
            "expected_count": len(expected),
            "current_count": len(current),
            "rank_changed_count": len(rank_changes),
            "missing_count": len(missing_ids),
            "new_count": len(new_ids),
        },
        "rank_changes": rank_changes,
        "missing_ids": missing_ids,
        "new_ids": new_ids,
        "current_ranking": current,
    }


def validate_ranking_snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate a frozen ranking snapshot without re-reading search results."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(snapshot, dict):
        errors.append("payload must be an object")
        snapshot = {}

    source_schema = str(snapshot.get("schema") or "")
    if not source_schema:
        errors.append("schema missing")
    elif source_schema != RANKING_SNAPSHOT_SCHEMA:
        errors.append(f"schema must be {RANKING_SNAPSHOT_SCHEMA}")

    ranking_value = snapshot.get("ranking")
    if not isinstance(ranking_value, list):
        errors.append("ranking must be a list")
        ranking: list[dict[str, Any]] = []
    else:
        ranking = [item for item in ranking_value if isinstance(item, dict)]
        for index, item in enumerate(ranking_value):
            if not isinstance(item, dict):
                errors.append(f"ranking[{index}] must be an object")
    try:
        count = int(snapshot.get("count"))
    except (TypeError, ValueError):
        errors.append("count must be an integer")
        count = len(ranking)
    if count != len(ranking):
        errors.append("count does not match ranking length")
    _validate_ranking_rows(ranking, "ranking", errors)

    return {
        "schema": RANKING_SNAPSHOT_VALIDATION_SCHEMA,
        "valid": not errors,
        "source_schema": source_schema,
        "summary": {
            "error_count": len(errors),
            "warning_count": len(warnings),
            "ranking_count": len(ranking),
        },
        "errors": errors,
        "warnings": warnings,
    }


def validate_ranking_snapshot_check(report: dict[str, Any]) -> dict[str, Any]:
    """Validate a ranking snapshot drift report without comparing inputs again."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(report, dict):
        errors.append("payload must be an object")
        report = {}

    source_schema = str(report.get("schema") or "")
    if not source_schema:
        errors.append("schema missing")
    elif source_schema != RANKING_SNAPSHOT_CHECK_SCHEMA:
        errors.append(f"schema must be {RANKING_SNAPSHOT_CHECK_SCHEMA}")
    matched = report.get("matched")
    if not isinstance(matched, bool):
        errors.append("matched must be a boolean")
        matched = False

    rank_changes = _gate_object_list(report.get("rank_changes", []), "rank_changes", errors)
    for index, item in enumerate(rank_changes):
        if not str(item.get("id") or "").strip():
            errors.append(f"rank_changes[{index}].id is required")
        for field in ("expected_rank", "current_rank", "delta"):
            _gate_non_negative_int(item.get(field), f"rank_changes[{index}].{field}", errors)
    missing_ids = _snapshot_string_list(report.get("missing_ids", []), "missing_ids", errors)
    new_ids = _snapshot_string_list(report.get("new_ids", []), "new_ids", errors)
    current_value = report.get("current_ranking")
    if not isinstance(current_value, list):
        errors.append("current_ranking must be a list")
        current_ranking: list[dict[str, Any]] = []
    else:
        current_ranking = [item for item in current_value if isinstance(item, dict)]
        for index, item in enumerate(current_value):
            if not isinstance(item, dict):
                errors.append(f"current_ranking[{index}] must be an object")
    _validate_ranking_rows(current_ranking, "current_ranking", errors)
    if matched and (rank_changes or missing_ids or new_ids):
        errors.append("matched must be false when drift arrays are non-empty")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        for field in ("expected_count", "current_count"):
            _gate_non_negative_int(summary.get(field), f"summary.{field}", errors)
        rank_changed_count = _gate_non_negative_int(
            summary.get("rank_changed_count"),
            "summary.rank_changed_count",
            errors,
        )
        if rank_changed_count != len(rank_changes):
            errors.append("summary.rank_changed_count does not match rank_changes length")
        missing_count = _gate_non_negative_int(summary.get("missing_count"), "summary.missing_count", errors)
        if missing_count != len(missing_ids):
            errors.append("summary.missing_count does not match missing_ids length")
        new_count = _gate_non_negative_int(summary.get("new_count"), "summary.new_count", errors)
        if new_count != len(new_ids):
            errors.append("summary.new_count does not match new_ids length")

    return {
        "schema": RANKING_SNAPSHOT_CHECK_VALIDATION_SCHEMA,
        "valid": not errors,
        "source_schema": source_schema,
        "summary": {
            "error_count": len(errors),
            "warning_count": len(warnings),
            "rank_changed_count": len(rank_changes),
            "missing_count": len(missing_ids),
            "new_count": len(new_ids),
        },
        "errors": errors,
        "warnings": warnings,
    }


def write_ranking_snapshot(
    search_payload: dict[str, Any],
    output_path: str | Path,
    *,
    label: str = "",
) -> dict[str, Any]:
    """Write a persistent ranked-ID snapshot."""
    snapshot = build_ranking_snapshot_payload(search_payload, label=label)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


def write_ranking_snapshot_check(
    search_payload: dict[str, Any],
    snapshot_payload: dict[str, Any],
    output_path: str | Path,
) -> dict[str, Any]:
    """Write a ranking snapshot drift report."""
    report = compare_ranking_snapshot_payload(search_payload, snapshot_payload)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def build_release_gate_report(
    comparisons: Iterable[dict[str, Any]],
    *,
    recall_ks: Iterable[int] = (20, 50),
    ndcg_k: int = 20,
    min_ndcg_improved_share: float = 0.50,
    severe_ndcg_drop: float = 0.20,
) -> dict[str, Any]:
    """Evaluate candidate-vs-baseline benchmark reports against Search v2 release gates."""
    rows = [dict(item) for item in comparisons if isinstance(item, dict)]
    recall_keys = [f"recall@{int(k)}" for k in recall_ks]
    ndcg_key = f"ndcg@{int(ndcg_k)}"
    query_reports: list[dict[str, Any]] = []
    improved = 0
    severe_regressions = 0
    recall_failures = 0
    manual_review_required = 0
    for position, item in enumerate(rows, 1):
        candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
        baseline = item.get("baseline") if isinstance(item.get("baseline"), dict) else {}
        delta = item.get("delta") if isinstance(item.get("delta"), dict) else {}
        query_id = str(item.get("query_id") or item.get("id") or f"q{position:04d}")
        query = str(item.get("query") or "")
        failures: list[str] = []
        diagnostics: list[dict[str, Any]] = []
        for key in recall_keys:
            candidate_value = float(candidate.get(key) or 0.0)
            baseline_value = float(baseline.get(key) or 0.0)
            if candidate_value + 1e-12 < baseline_value:
                failures.append(f"{key}_below_baseline")
                recall_failures += 1
                diagnostics.append(
                    {
                        "check": "recall_not_below_baseline",
                        "metric": key,
                        "candidate": candidate_value,
                        "baseline": baseline_value,
                        "delta": candidate_value - baseline_value,
                        "severity": "failure",
                        "action": "inspect_hybrid_recall_loss",
                    }
                )
        ndcg_candidate = float(candidate.get(ndcg_key) or 0.0)
        ndcg_baseline = float(baseline.get(ndcg_key) or 0.0)
        ndcg_delta = float(delta.get(ndcg_key, ndcg_candidate - ndcg_baseline) or 0.0)
        if ndcg_delta > 0:
            improved += 1
        relative_ndcg_delta = ndcg_delta / ndcg_baseline if ndcg_baseline else (0.0 if ndcg_delta == 0 else 1.0)
        if ndcg_baseline > 0 and relative_ndcg_delta < -abs(severe_ndcg_drop):
            failures.append(f"severe_{ndcg_key}_regression")
            severe_regressions += 1
            diagnostics.append(
                {
                    "check": f"no_severe_{ndcg_key}_regressions",
                    "metric": ndcg_key,
                    "candidate": ndcg_candidate,
                    "baseline": ndcg_baseline,
                    "delta": ndcg_delta,
                    "relative_delta": relative_ndcg_delta,
                    "threshold": -abs(severe_ndcg_drop),
                    "severity": "manual_review_required",
                    "action": "manual_relevance_review",
                }
            )
        if any(item.get("severity") == "manual_review_required" for item in diagnostics):
            manual_review_required += 1
        query_reports.append(
            {
                "query_id": query_id,
                "query": query,
                "passed": not failures,
                "failures": failures,
                "diagnostics": diagnostics,
                "candidate": candidate,
                "baseline": baseline,
                "delta": delta,
                "candidate_ranking_snapshot": list(item.get("candidate_ranking_snapshot") or []),
                "baseline_ranking_snapshot": list(item.get("baseline_ranking_snapshot") or []),
                "relative_ndcg_delta": relative_ndcg_delta,
            }
        )
    query_count = len(query_reports)
    improved_share = improved / query_count if query_count else 0.0
    checks = {
        "recall_not_below_baseline": recall_failures == 0,
        f"{ndcg_key}_improved_share": improved_share >= min_ndcg_improved_share,
        f"no_severe_{ndcg_key}_regressions": severe_regressions == 0,
    }
    passed = bool(query_count) and all(checks.values()) and all(item["passed"] for item in query_reports)
    blockers = release_gate_summary_blockers(
        improved_share=improved_share,
        min_ndcg_improved_share=min_ndcg_improved_share,
        improved_queries=improved,
        query_count=query_count,
        ndcg_key=ndcg_key,
    )
    blockers.extend(release_gate_blockers_from_queries(query_reports))
    return {
        "schema": RELEASE_GATE_SCHEMA,
        "passed": passed,
        "config": {
            "recall_ks": [int(k) for k in recall_ks],
            "ndcg_k": int(ndcg_k),
            "min_ndcg_improved_share": min_ndcg_improved_share,
            "severe_ndcg_drop": severe_ndcg_drop,
        },
        "summary": {
            "query_count": query_count,
            "ndcg_improved_queries": improved,
            "ndcg_improved_share": improved_share,
            "recall_failure_count": recall_failures,
            "severe_ndcg_regression_count": severe_regressions,
            "manual_review_required_count": manual_review_required,
            "release_gate_blocker_count": len(blockers),
        },
        "checks": checks,
        "queries": query_reports,
        "release_gate_blockers": blockers,
    }


def release_gate_blockers_from_queries(queries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build machine-readable release blockers from per-query diagnostics."""
    blockers: list[dict[str, Any]] = []
    for query in queries:
        query_id = str(query.get("query_id") or query.get("id") or "")
        query_text = str(query.get("query") or "")
        for diagnostic in query.get("diagnostics") or []:
            if not isinstance(diagnostic, dict):
                continue
            action = str(diagnostic.get("action") or "")
            if action == "inspect_hybrid_recall_loss":
                blocker_type = "recall_below_baseline"
                severity = str(diagnostic.get("severity") or "failure")
            elif action == "manual_relevance_review":
                blocker_type = "manual_review_required"
                severity = str(diagnostic.get("severity") or "manual_review_required")
            else:
                continue
            blockers.append(
                {
                    "type": blocker_type,
                    "query_id": query_id,
                    "query": query_text,
                    "metric": str(diagnostic.get("metric") or ""),
                    "action": action,
                    "severity": severity,
                    "candidate": diagnostic.get("candidate"),
                    "baseline": diagnostic.get("baseline"),
                    "delta": diagnostic.get("delta"),
                    "relative_delta": diagnostic.get("relative_delta"),
                    "blocks_gate": True,
                }
            )
    return blockers


def release_gate_summary_blockers(
    *,
    improved_share: float,
    min_ndcg_improved_share: float,
    improved_queries: int,
    query_count: int,
    ndcg_key: str,
) -> list[dict[str, Any]]:
    """Build aggregate release blockers that are not tied to one query."""
    if query_count <= 0 or improved_share >= min_ndcg_improved_share:
        return []
    return [
        {
            "type": "ndcg_improved_share_below_threshold",
            "query_id": "aggregate",
            "metric": f"{ndcg_key}_improved_share",
            "action": "inspect_hybrid_ranking_quality",
            "severity": "failure",
            "observed": improved_share,
            "minimum": min_ndcg_improved_share,
            "improved_queries": improved_queries,
            "query_count": query_count,
            "blocks_gate": True,
        }
    ]


def apply_judgment_coverage_to_release_gate(
    report: dict[str, Any],
    coverage: dict[str, Any],
    *,
    query_id: str = "",
) -> dict[str, Any]:
    """Attach offline judgment coverage to a release-gate report."""
    summary = report.setdefault("summary", {})
    checks = report.setdefault("checks", {})
    data_issues = report.setdefault("data_issues", [])
    blockers = report.setdefault("release_gate_blockers", [])
    judgment_count = int(coverage.get("judgment_count") or 0)
    graded_count = int(coverage.get("graded_judgment_count") or 0)
    ungraded_count = int(coverage.get("ungraded_judgment_count") or 0)
    all_graded = bool(coverage.get("all_judgments_graded"))
    summary["judgment_count"] = judgment_count
    summary["graded_judgment_count"] = graded_count
    summary["ungraded_judgment_count"] = ungraded_count
    checks["all_judgments_graded"] = all_graded
    if ungraded_count > 0:
        issue = {
            "query_id": str(query_id or ""),
            "reason": "ungraded_judgments",
            "judgment_count": str(judgment_count),
            "graded_judgment_count": str(graded_count),
            "ungraded_judgment_count": str(ungraded_count),
        }
        data_issues.append(issue)
        blockers.append(
            {
                "type": "data_issue",
                "query_id": issue["query_id"],
                "reason": issue["reason"],
                "severity": "failure",
                "blocks_gate": True,
                "judgment_count": issue["judgment_count"],
                "graded_judgment_count": issue["graded_judgment_count"],
                "ungraded_judgment_count": issue["ungraded_judgment_count"],
            }
        )
        report["passed"] = False
    summary["data_issue_count"] = len(data_issues)
    summary["release_gate_blocker_count"] = len(blockers)
    return report


def validate_release_gate_report(report: dict[str, Any]) -> dict[str, Any]:
    """Validate a saved Search release-gate report without re-running benchmarks."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(report, dict):
        errors.append("payload must be an object")
        report = {}

    source_schema = str(report.get("schema") or "")
    if not source_schema:
        errors.append("schema missing")
    elif source_schema != RELEASE_GATE_SCHEMA:
        errors.append(f"schema must be {RELEASE_GATE_SCHEMA}")

    passed = report.get("passed")
    if not isinstance(passed, bool):
        errors.append("passed must be a boolean")
        passed = False

    checks = report.get("checks")
    if not isinstance(checks, dict):
        errors.append("checks must be an object")
    else:
        for key, value in checks.items():
            if not isinstance(value, bool):
                errors.append(f"checks.{key} must be a boolean")

    queries_value = report.get("queries")
    if not isinstance(queries_value, list):
        errors.append("queries must be a list")
        queries: list[dict[str, Any]] = []
    else:
        queries = [query for query in queries_value if isinstance(query, dict)]
        for index, query in enumerate(queries_value):
            if not isinstance(query, dict):
                errors.append(f"queries[{index}] must be an object")

    manual_review_required_count = 0
    recall_failure_count = 0
    severe_ndcg_regression_count = 0
    for index, query in enumerate(queries):
        if not str(query.get("query_id") or query.get("id") or "").strip():
            errors.append(f"queries[{index}].query_id is required")
        if not isinstance(query.get("passed"), bool):
            errors.append(f"queries[{index}].passed must be a boolean")
        failures = query.get("failures")
        if not isinstance(failures, list):
            errors.append(f"queries[{index}].failures must be a list")
            failures = []
        if query.get("passed") is True and failures:
            errors.append(f"queries[{index}].passed must be false when failures are present")
        for failure in failures:
            failure_text = str(failure)
            if "recall@" in failure_text and "below_baseline" in failure_text:
                recall_failure_count += 1
            if "severe_ndcg" in failure_text:
                severe_ndcg_regression_count += 1
        diagnostics = query.get("diagnostics")
        if not isinstance(diagnostics, list):
            errors.append(f"queries[{index}].diagnostics must be a list")
        else:
            for diagnostic_index, diagnostic in enumerate(diagnostics):
                if not isinstance(diagnostic, dict):
                    errors.append(f"queries[{index}].diagnostics[{diagnostic_index}] must be an object")
                    continue
                if str(diagnostic.get("action") or "") == "manual_relevance_review":
                    manual_review_required_count += 1

    data_issues = _gate_object_list(report.get("data_issues", []), "data_issues", errors)
    for index, item in enumerate(data_issues):
        if not str(item.get("query_id") or "").strip():
            errors.append(f"data_issues[{index}].query_id is required")
        if not str(item.get("reason") or "").strip():
            errors.append(f"data_issues[{index}].reason is required")

    provider_failures = _gate_object_list(report.get("provider_failures", []), "provider_failures", errors)
    for index, item in enumerate(provider_failures):
        for field in ("query_id", "strategy", "source", "status"):
            if not str(item.get(field) or "").strip():
                errors.append(f"provider_failures[{index}].{field} is required")

    blockers = _gate_object_list(report.get("release_gate_blockers", []), "release_gate_blockers", errors)
    blocking_blockers = 0
    for index, item in enumerate(blockers):
        blocker_type = str(item.get("type") or "").strip()
        if not blocker_type:
            errors.append(f"release_gate_blockers[{index}].type is required")
        blocks_gate = item.get("blocks_gate")
        if not isinstance(blocks_gate, bool):
            errors.append(f"release_gate_blockers[{index}].blocks_gate must be a boolean")
        elif blocks_gate:
            blocking_blockers += 1
        if blocker_type == "provider_failure" and blocks_gate is not False:
            errors.append(f"release_gate_blockers[{index}].blocks_gate must be false for provider_failure")
        elif blocker_type and blocker_type != "provider_failure" and blocks_gate is False:
            errors.append(f"release_gate_blockers[{index}].blocks_gate must be true for {blocker_type}")
        _validate_release_gate_blocker_fields(index, item, blocker_type, errors)
    if passed is True and blocking_blockers:
        errors.append("passed must be false when blocking release_gate_blockers are present")
    if passed is True and isinstance(checks, dict) and any(value is False for value in checks.values()):
        errors.append("passed must be false when any check is false")
    if passed is True and any(query.get("passed") is False for query in queries):
        errors.append("passed must be false when any query failed")
    _validate_query_diagnostic_blocker_coverage(queries, blockers, errors)
    _validate_data_issue_blocker_coverage(data_issues, blockers, errors)
    _validate_provider_failure_blocker_coverage(provider_failures, blockers, errors)

    summary = report.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        query_count = _gate_non_negative_int(summary.get("query_count"), "summary.query_count", errors)
        if query_count != len(queries):
            errors.append("summary.query_count does not match queries length")
        ndcg_improved_queries = _gate_non_negative_int(
            summary.get("ndcg_improved_queries"),
            "summary.ndcg_improved_queries",
            errors,
        )
        if ndcg_improved_queries > query_count:
            errors.append("summary.ndcg_improved_queries cannot exceed query_count")
        ndcg_improved_share = _gate_float_between_zero_one(
            summary.get("ndcg_improved_share"),
            "summary.ndcg_improved_share",
            errors,
        )
        expected_ndcg_share = ndcg_improved_queries / query_count if query_count else 0.0
        if abs(ndcg_improved_share - expected_ndcg_share) > 1e-9:
            errors.append("summary.ndcg_improved_share does not match ndcg_improved_queries/query_count")
        if "recall_failure_count" in summary:
            summary_recall_failures = _gate_non_negative_int(
                summary.get("recall_failure_count"),
                "summary.recall_failure_count",
                errors,
            )
            if isinstance(checks, dict) and isinstance(checks.get("recall_not_below_baseline"), bool):
                if checks["recall_not_below_baseline"] != (summary_recall_failures == 0):
                    errors.append("checks.recall_not_below_baseline does not match recall_failure_count")
            if summary_recall_failures != recall_failure_count:
                errors.append("summary.recall_failure_count does not match query failures")
        if "severe_ndcg_regression_count" in summary:
            summary_severe = _gate_non_negative_int(
                summary.get("severe_ndcg_regression_count"),
                "summary.severe_ndcg_regression_count",
                errors,
            )
            if isinstance(checks, dict) and isinstance(checks.get("no_severe_ndcg@20_regressions"), bool):
                if checks["no_severe_ndcg@20_regressions"] != (summary_severe == 0):
                    errors.append(
                        "checks.no_severe_ndcg@20_regressions does not match severe_ndcg_regression_count"
                    )
            if summary_severe != severe_ndcg_regression_count:
                errors.append("summary.severe_ndcg_regression_count does not match query failures")
        if "manual_review_required_count" in summary:
            summary_manual = _gate_non_negative_int(
                summary.get("manual_review_required_count"),
                "summary.manual_review_required_count",
                errors,
            )
            if summary_manual != manual_review_required_count:
                errors.append("summary.manual_review_required_count does not match diagnostics")
        if "data_issue_count" in summary:
            data_issue_count = _gate_non_negative_int(
                summary.get("data_issue_count"),
                "summary.data_issue_count",
                errors,
            )
            if data_issue_count != len(data_issues):
                errors.append("summary.data_issue_count does not match data_issues length")
        if "provider_failure_count" in summary:
            provider_failure_count = _gate_non_negative_int(
                summary.get("provider_failure_count"),
                "summary.provider_failure_count",
                errors,
            )
            if provider_failure_count != len(provider_failures):
                errors.append("summary.provider_failure_count does not match provider_failures length")
        else:
            provider_failure_count = len(provider_failures)
        provider_status_count = None
        if "provider_status_count" in summary:
            provider_status_count = _gate_non_negative_int(
                summary.get("provider_status_count"),
                "summary.provider_status_count",
                errors,
            )
            if provider_failure_count > provider_status_count:
                errors.append("summary.provider_failure_count cannot exceed provider_status_count")
        if "provider_failure_rate" in summary:
            provider_failure_rate = _gate_float_between_zero_one(
                summary.get("provider_failure_rate"),
                "summary.provider_failure_rate",
                errors,
            )
            if provider_status_count is not None:
                expected_rate = provider_failure_count / provider_status_count if provider_status_count else 0.0
                if abs(provider_failure_rate - expected_rate) > 1e-9:
                    errors.append(
                        "summary.provider_failure_rate does not match provider_failure_count/provider_status_count"
                    )
        if {"judgment_count", "graded_judgment_count", "ungraded_judgment_count"} <= set(summary):
            judgment_count = _gate_non_negative_int(summary.get("judgment_count"), "summary.judgment_count", errors)
            graded_count = _gate_non_negative_int(
                summary.get("graded_judgment_count"),
                "summary.graded_judgment_count",
                errors,
            )
            ungraded_count = _gate_non_negative_int(
                summary.get("ungraded_judgment_count"),
                "summary.ungraded_judgment_count",
                errors,
            )
            if graded_count + ungraded_count != judgment_count:
                errors.append("summary.graded_judgment_count + ungraded_judgment_count must equal judgment_count")
            if isinstance(checks, dict) and isinstance(checks.get("all_judgments_graded"), bool):
                if checks["all_judgments_graded"] != (ungraded_count == 0):
                    errors.append("checks.all_judgments_graded does not match ungraded_judgment_count")
        blocker_count = _gate_non_negative_int(
            summary.get("release_gate_blocker_count"),
            "summary.release_gate_blocker_count",
            errors,
        )
        if blocker_count != len(blockers):
            errors.append("summary.release_gate_blocker_count does not match release_gate_blockers length")
        _validate_aggregate_ndcg_gate_consistency(report.get("config"), summary, checks, blockers, errors)

    if isinstance(checks, dict) and isinstance(checks.get("no_data_issues"), bool):
        if checks["no_data_issues"] != (len(data_issues) == 0):
            errors.append("checks.no_data_issues does not match data_issues")

    return {
        "schema": RELEASE_GATE_VALIDATION_SCHEMA,
        "valid": not errors,
        "source_schema": source_schema,
        "summary": {
            "error_count": len(errors),
            "warning_count": len(warnings),
            "query_count": len(queries),
            "release_gate_blocker_count": len(blockers),
        },
        "errors": errors,
        "warnings": warnings,
    }


def render_benchmark_markdown(report: dict[str, Any], *, title: str = "InstSci Search Benchmark") -> str:
    """Render a human-readable benchmark summary from metrics or comparison output."""
    lines = [
        f"# {title}",
        "",
        "## Metrics",
        "",
    ]
    if isinstance(report.get("candidate"), dict) or isinstance(report.get("baseline"), dict):
        candidate = report.get("candidate") if isinstance(report.get("candidate"), dict) else {}
        baseline = report.get("baseline") if isinstance(report.get("baseline"), dict) else {}
        delta = report.get("delta") if isinstance(report.get("delta"), dict) else {}
        keys = _metric_keys(candidate, baseline, delta)
        lines.extend(["| metric | candidate | baseline | delta |", "| --- | ---: | ---: | ---: |"])
        for key in keys:
            lines.append(
                f"| {key} | {_format_metric(candidate.get(key))} | "
                f"{_format_metric(baseline.get(key))} | {_format_metric(delta.get(key), signed=True)} |"
            )
        lines.extend(["", "## Candidate Ranking", ""])
        lines.extend(_ranking_lines(report.get("candidate_ranking_snapshot") or []))
        lines.extend(["", "## Baseline Ranking", ""])
        lines.extend(_ranking_lines(report.get("baseline_ranking_snapshot") or []))
    else:
        keys = _metric_keys(report)
        lines.extend(["| metric | value |", "| --- | ---: |"])
        for key in keys:
            lines.append(f"| {key} | {_format_metric(report.get(key))} |")
    coverage_keys = [
        "judgment_count",
        "graded_judgment_count",
        "ungraded_judgment_count",
        "all_judgments_graded",
    ]
    if any(key in report for key in coverage_keys):
        lines.extend(["", "## Judgment Coverage", "", "| metric | value |", "| --- | ---: |"])
        for key in coverage_keys:
            if key in report:
                lines.append(f"| {key} | {_format_metric(report.get(key))} |")
    lines.append("")
    return "\n".join(lines)


def _metric_keys(*reports: dict[str, Any]) -> list[str]:
    keys: set[str] = set()
    for report in reports:
        for key, value in report.items():
            if isinstance(value, (int, float)) and key != "count":
                keys.add(str(key))
    preferred_prefixes = ("precision@", "recall@", "ndcg@", "must_find_recall@", "must_find_hits@")
    ordered = [
        key
        for prefix in preferred_prefixes
        for key in sorted(keys, key=_metric_sort_key)
        if key.startswith(prefix)
    ]
    for key in ("mrr", "duplicate_rate", "judged_count", "relevant_total", "must_find_total", "count"):
        if any(key in report for report in reports) and key not in ordered:
            ordered.append(key)
    return ordered


def _metric_sort_key(key: str) -> tuple[str, int, str]:
    if "@" in key:
        prefix, suffix = key.split("@", 1)
        return prefix, int(suffix) if suffix.isdigit() else 0, key
    return key, 0, key


def _format_metric(value: Any, *, signed: bool = False) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        text = f"{value:+.4f}" if signed else f"{value:.4f}"
        return text.rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:+d}" if signed else str(value)
    return str(value)


def _ranking_lines(rows: Iterable[Any], *, limit: int = 10) -> list[str]:
    items = [row for row in rows if isinstance(row, dict)]
    if not items:
        return ["- none"]
    lines = []
    for row in items[:limit]:
        title = str(row.get("title") or "")
        item_id = str(row.get("id") or "")
        lines.append(f"- {int(row.get('rank') or 0)}. {item_id} {title}".rstrip())
    if len(items) > limit:
        lines.append(f"- ... {len(items) - limit} more")
    return lines


def _gate_object_list(value: Any, field: str, errors: list[str]) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return []
    rows = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"{field}[{index}] must be an object")
            continue
        rows.append(item)
    return rows


def _gate_non_negative_int(value: Any, field: str, errors: list[str]) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be an integer")
        return 0
    if number < 0:
        errors.append(f"{field} must be non-negative")
    return number


def _gate_float_between_zero_one(value: Any, field: str, errors: list[str]) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be a number")
        return 0.0
    if number < 0.0 or number > 1.0:
        errors.append(f"{field} must be between 0 and 1")
    return number


def _validate_release_gate_blocker_fields(
    index: int,
    item: dict[str, Any],
    blocker_type: str,
    errors: list[str],
) -> None:
    def require_text(field: str) -> None:
        if not str(item.get(field) or "").strip():
            errors.append(f"release_gate_blockers[{index}].{field} is required for {blocker_type}")

    def require_value(field: str) -> None:
        if field not in item or item.get(field) is None:
            errors.append(f"release_gate_blockers[{index}].{field} is required for {blocker_type}")

    if blocker_type in {
        "data_issue",
        "legacy_contract_not_v1",
        "hybrid_contract_not_v2",
        "hybrid_contract_invalid",
    }:
        require_text("query_id")
        require_text("reason")
    elif blocker_type == "provider_failure":
        for field in ("query_id", "strategy", "source", "status"):
            require_text(field)
    elif blocker_type in {"recall_below_baseline", "manual_review_required"}:
        require_text("query_id")
        require_text("metric")
        require_text("action")
    elif blocker_type == "ndcg_improved_share_below_threshold":
        require_text("metric")
        require_value("minimum")
        require_value("observed")
        require_value("improved_queries")
        require_value("query_count")


def _validate_provider_failure_blocker_coverage(
    provider_failures: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    errors: list[str],
) -> None:
    blocker_keys = {
        (
            str(item.get("query_id") or ""),
            str(item.get("strategy") or ""),
            str(item.get("source") or ""),
            str(item.get("status") or ""),
        )
        for item in blockers
        if str(item.get("type") or "") == "provider_failure" and item.get("blocks_gate") is False
    }
    failure_keys = {
        (
            str(item.get("query_id") or ""),
            str(item.get("strategy") or ""),
            str(item.get("source") or ""),
            str(item.get("status") or ""),
        )
        for item in provider_failures
    }
    if not failure_keys <= blocker_keys:
        errors.append("provider_failures must have matching provider_failure release_gate_blockers")
    if not blocker_keys <= failure_keys:
        errors.append("provider_failure release_gate_blockers must have matching provider_failures")


def _validate_data_issue_blocker_coverage(
    data_issues: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    errors: list[str],
) -> None:
    blocker_keys = {
        (
            str(item.get("query_id") or ""),
            str(item.get("reason") or ""),
        )
        for item in blockers
        if str(item.get("type") or "") in {
            "data_issue",
            "legacy_contract_not_v1",
            "hybrid_contract_not_v2",
            "hybrid_contract_invalid",
        }
        and item.get("blocks_gate") is True
    }
    issue_keys = {
        (
            str(item.get("query_id") or ""),
            str(item.get("reason") or ""),
        )
        for item in data_issues
    }
    if not issue_keys <= blocker_keys:
        errors.append("data_issues must have matching blocking release_gate_blockers")
    data_issue_blocker_keys = {
        (
            str(item.get("query_id") or ""),
            str(item.get("reason") or ""),
        )
        for item in blockers
        if str(item.get("type") or "") == "data_issue" and item.get("blocks_gate") is True
    }
    if not data_issue_blocker_keys <= issue_keys:
        errors.append("data_issue release_gate_blockers must have matching data_issues")


def _validate_query_diagnostic_blocker_coverage(
    queries: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    errors: list[str],
) -> None:
    diagnostic_keys: set[tuple[str, str, str]] = set()
    for query in queries:
        query_id = str(query.get("query_id") or query.get("id") or "")
        for diagnostic in query.get("diagnostics") or []:
            if not isinstance(diagnostic, dict):
                continue
            action = str(diagnostic.get("action") or "")
            if action not in {"inspect_hybrid_recall_loss", "manual_relevance_review"}:
                continue
            diagnostic_keys.add((query_id, str(diagnostic.get("metric") or ""), action))
    if not diagnostic_keys:
        diagnostic_keys = set()
    blocker_keys = {
        (
            str(item.get("query_id") or ""),
            str(item.get("metric") or ""),
            str(item.get("action") or ""),
        )
        for item in blockers
        if str(item.get("type") or "") in {"recall_below_baseline", "manual_review_required"}
        and item.get("blocks_gate") is True
    }
    if not diagnostic_keys <= blocker_keys:
        errors.append("query diagnostics must have matching blocking release_gate_blockers")
    if not blocker_keys <= diagnostic_keys:
        errors.append("query release_gate_blockers must have matching diagnostics")


def _validate_aggregate_ndcg_gate_consistency(
    config: Any,
    summary: dict[str, Any],
    checks: Any,
    blockers: list[dict[str, Any]],
    errors: list[str],
) -> None:
    if not isinstance(config, dict):
        return
    if "min_ndcg_improved_share" not in config or "ndcg_improved_share" not in summary:
        return
    try:
        minimum = float(config.get("min_ndcg_improved_share"))
        observed = float(summary.get("ndcg_improved_share"))
        improved_queries = int(summary.get("ndcg_improved_queries"))
        query_count = int(summary.get("query_count"))
    except (TypeError, ValueError):
        return
    meets_threshold = observed >= minimum
    if isinstance(checks, dict) and isinstance(checks.get("ndcg@20_improved_share"), bool):
        if checks["ndcg@20_improved_share"] != meets_threshold:
            errors.append("checks.ndcg@20_improved_share does not match ndcg_improved_share threshold")
    matching_blockers = [
        item
        for item in blockers
        if str(item.get("type") or "") == "ndcg_improved_share_below_threshold" and item.get("blocks_gate") is True
    ]
    if meets_threshold:
        if matching_blockers:
            errors.append("ndcg_improved_share_below_threshold blocker is present even though threshold is met")
        return
    if query_count <= 0:
        return
    has_blocker = bool(matching_blockers)
    if not has_blocker:
        errors.append("ndcg_improved_share below threshold requires ndcg_improved_share_below_threshold blocker")
        return
    identity_matches = any(
        str(item.get("query_id") or "") == "aggregate"
        and str(item.get("metric") or "") == "ndcg@20_improved_share"
        and str(item.get("action") or "") == "inspect_hybrid_ranking_quality"
        for item in matching_blockers
    )
    if not identity_matches:
        errors.append("ndcg_improved_share_below_threshold blocker identity does not match aggregate nDCG gate")
    value_matches = False
    for item in matching_blockers:
        try:
            blocker_observed = float(item.get("observed"))
            blocker_minimum = float(item.get("minimum"))
        except (TypeError, ValueError):
            continue
        if abs(blocker_observed - observed) <= 1e-9 and abs(blocker_minimum - minimum) <= 1e-9:
            value_matches = True
            break
    if not value_matches:
        errors.append("ndcg_improved_share_below_threshold blocker values do not match summary/config")
    count_matches = False
    for item in matching_blockers:
        try:
            blocker_improved_queries = int(item.get("improved_queries"))
            blocker_query_count = int(item.get("query_count"))
        except (TypeError, ValueError):
            continue
        if blocker_improved_queries == improved_queries and blocker_query_count == query_count:
            count_matches = True
            break
    if not count_matches:
        errors.append("ndcg_improved_share_below_threshold blocker counts do not match summary")


def _validate_ranking_rows(rows: list[dict[str, Any]], field: str, errors: list[str]) -> None:
    seen_ranks: set[int] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(rows):
        try:
            rank = int(item.get("rank"))
        except (TypeError, ValueError):
            errors.append(f"{field}[{index}].rank must be an integer")
            rank = 0
        expected_rank = index + 1
        if rank != expected_rank:
            errors.append(f"{field}[{index}].rank must be {expected_rank}")
        if rank in seen_ranks:
            errors.append(f"{field}[{index}].rank duplicates an earlier row")
        seen_ranks.add(rank)
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            errors.append(f"{field}[{index}].id is required")
        elif item_id in seen_ids:
            errors.append(f"{field}[{index}].id duplicates an earlier row")
        seen_ids.add(item_id)
        if not str(item.get("title") or "").strip():
            errors.append(f"{field}[{index}].title is required")


def _snapshot_string_list(value: Any, field: str, errors: list[str]) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return []
    rows = []
    for index, item in enumerate(value):
        text = str(item or "").strip()
        if not text:
            errors.append(f"{field}[{index}] is required")
            continue
        rows.append(text)
    return rows


def _validate_metric_block(metrics: dict[str, Any], prefix: str, errors: list[str]) -> None:
    label = f"{prefix}." if prefix else ""
    count = _optional_non_negative_int(metrics, "count", label, errors)
    judged_count = _optional_non_negative_int(metrics, "judged_count", label, errors)
    relevant_total = _optional_non_negative_int(metrics, "relevant_total", label, errors)
    if count is not None and judged_count is not None and judged_count > count:
        errors.append(f"{label}judged_count cannot exceed count")
    del relevant_total
    for key, value in metrics.items():
        key_text = str(key)
        field = f"{label}{key_text}"
        if key_text.startswith(("precision@", "recall@", "ndcg@", "must_find_recall@")) or key_text in {
            "mrr",
            "duplicate_rate",
        }:
            _metric_float_between_zero_one(value, field, errors)
        elif key_text.startswith("must_find_hits@") or key_text in {"must_find_total"}:
            _optional_non_negative_int(metrics, key_text, label, errors)


def _validate_delta_block(candidate: dict[str, Any], baseline: dict[str, Any], delta: dict[str, Any], errors: list[str]) -> None:
    for key, value in delta.items():
        field = f"delta.{key}"
        try:
            delta_value = float(value)
        except (TypeError, ValueError):
            errors.append(f"{field} must be a number")
            continue
        candidate_value = candidate.get(key)
        baseline_value = baseline.get(key)
        if isinstance(candidate_value, (int, float)) and isinstance(baseline_value, (int, float)):
            expected = float(candidate_value) - float(baseline_value)
            if abs(delta_value - expected) > 1e-9:
                errors.append(f"{field} does not match candidate-baseline")


def _validate_snapshot_field(report: dict[str, Any], field: str, errors: list[str]) -> None:
    value = report.get(field)
    if value is None:
        errors.append(f"{field} must be a list")
        return
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return
    rows = [item for item in value if isinstance(item, dict)]
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"{field}[{index}] must be an object")
    _validate_ranking_rows(rows, field, errors)


def _validate_benchmark_coverage(report: dict[str, Any], errors: list[str]) -> None:
    coverage_keys = {"judgment_count", "graded_judgment_count", "ungraded_judgment_count"}
    if not coverage_keys <= set(report):
        return
    judgment_count = _gate_non_negative_int(report.get("judgment_count"), "judgment_count", errors)
    graded_count = _gate_non_negative_int(report.get("graded_judgment_count"), "graded_judgment_count", errors)
    ungraded_count = _gate_non_negative_int(report.get("ungraded_judgment_count"), "ungraded_judgment_count", errors)
    if graded_count + ungraded_count != judgment_count:
        errors.append("graded_judgment_count + ungraded_judgment_count must equal judgment_count")
    if "all_judgments_graded" in report:
        if not isinstance(report.get("all_judgments_graded"), bool):
            errors.append("all_judgments_graded must be a boolean")
        elif bool(report.get("all_judgments_graded")) != (ungraded_count == 0):
            errors.append("all_judgments_graded does not match ungraded_judgment_count")


def _optional_non_negative_int(metrics: dict[str, Any], key: str, label: str, errors: list[str]) -> int | None:
    if key not in metrics:
        return None
    field = f"{label}{key}"
    try:
        number = int(metrics.get(key))
    except (TypeError, ValueError):
        errors.append(f"{field} must be an integer")
        return None
    if number < 0:
        errors.append(f"{field} must be non-negative")
    return number


def _metric_float_between_zero_one(value: Any, field: str, errors: list[str]) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be a number")
        return 0.0
    if number < 0.0 or number > 1.0:
        errors.append(f"{field} must be between 0 and 1")
    return number


def duplicate_rate(ids: Iterable[str]) -> float:
    values = [value for value in ids if value]
    if not values:
        return 0.0
    return (len(values) - len(set(values))) / len(values)


def ndcg_at_k(ids: list[str], judgments: dict[str, int], k: int) -> float:
    gains = [max(judgments.get(item_id, 0), 0) for item_id in ids[:k]]
    dcg = _dcg(gains)
    ideal = _dcg(sorted((max(value, 0) for value in judgments.values()), reverse=True)[:k])
    return dcg / ideal if ideal else 0.0


def _dcg(gains: list[int]) -> float:
    return sum((2**gain - 1) / math.log2(position + 2) for position, gain in enumerate(gains))


def mean_reciprocal_rank(ids: list[str], judgments: dict[str, int], *, relevant_threshold: int = 2) -> float:
    for position, item_id in enumerate(ids, 1):
        if judgments.get(item_id, 0) >= relevant_threshold:
            return 1 / position
    return 0.0
