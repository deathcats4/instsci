"""Live search evaluation runner for InstSci Search v2."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import multi_search
from .search_benchmark import (
    build_release_gate_report,
    build_relevance_pool,
    compare_ranked_results,
    load_judgments,
    release_gate_blockers_from_queries,
)
from .search_pipeline import (
    SEARCH_SCHEMA,
    SEARCH_SCHEMA_V2,
    build_search_payload,
    load_search_payload,
    normalize_doi,
    validate_search_payload_contract,
    write_search_payload,
)

LIVE_EVALUATION_SCHEMA = "instsci.search_live_evaluation.v1"
RELEVANCE_REVIEW_PACKET_SCHEMA = "instsci.relevance_review_packet.v1"
QUERY_SET_VALIDATION_SCHEMA = "instsci.search_query_set_validation.v1"
LIVE_EVALUATION_VALIDATION_SCHEMA = "instsci.search_live_evaluation_validation.v1"
RELEVANCE_REVIEW_PACKET_VALIDATION_SCHEMA = "instsci.relevance_review_packet_validation.v1"
SearchRunner = Callable[..., multi_search.MultiSearchResponse]


def load_query_set(path: str | Path) -> list[dict[str, str]]:
    """Load live-evaluation queries from JSON strings/objects or a text file."""
    source = Path(path)
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            items = payload.get("queries") or []
        elif isinstance(payload, list):
            items = payload
        else:
            raise ValueError("Query JSON must contain a list or an object with a queries list.")
    else:
        items = [line.strip() for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    rows: list[dict[str, str]] = []
    for index, item in enumerate(items, 1):
        if isinstance(item, str):
            query = item.strip()
            if query:
                rows.append({"id": f"q{index:04d}", "query": query, "year": ""})
            continue
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or item.get("text") or "").strip()
        if not query:
            continue
        rows.append(
            {
                "id": str(item.get("id") or f"q{index:04d}"),
                "query": query,
                "year": str(item.get("year") or item.get("year_range") or ""),
            }
        )
    if not rows:
        raise ValueError("Query set must contain at least one query.")
    return rows


def load_query_set_payload(path: str | Path) -> dict[str, Any]:
    """Load a live-eval query-set file without dropping invalid rows."""
    source = Path(path)
    if source.suffix.lower() == ".json":
        return {
            "source_format": "json",
            "payload": json.loads(source.read_text(encoding="utf-8-sig")),
        }
    rows = [line.strip() for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    return {"source_format": "text", "payload": rows}


def validate_live_query_set(payload: Any) -> dict[str, Any]:
    """Validate a live-evaluation query set before running provider APIs."""
    errors: list[str] = []
    warnings: list[str] = []
    source_schema = ""
    items_value: Any
    if isinstance(payload, dict) and "payload" in payload and "source_format" in payload:
        source_format = str(payload.get("source_format") or "")
        items_value = payload.get("payload")
    else:
        source_format = "json" if isinstance(payload, (dict, list)) else "unknown"
        items_value = payload
    if isinstance(items_value, dict):
        source_schema = str(items_value.get("schema") or "")
        items_value = items_value.get("queries")
    if not isinstance(items_value, list):
        errors.append("queries must be a list")
        items: list[Any] = []
    else:
        items = items_value

    normalized_ids: set[str] = set()
    normalized_query_keys: set[tuple[str, str]] = set()
    query_count = 0
    stable_id_count = 0
    for index, item in enumerate(items):
        prefix = f"queries[{index}]"
        explicit_id = ""
        query = ""
        year = ""
        if isinstance(item, str):
            query = item.strip()
            explicit_id = f"q{index + 1:04d}"
            warnings.append(f"{prefix} is a string row; explicit stable ids are recommended for live evaluation")
        elif isinstance(item, dict):
            explicit_id = str(item.get("id") or "").strip()
            query = str(item.get("query") or item.get("text") or "").strip()
            year = str(item.get("year") if item.get("year") is not None else item.get("year_range") or "")
            if not explicit_id:
                explicit_id = f"q{index + 1:04d}"
                warnings.append(f"{prefix}.id missing; generated ids are position-dependent")
            else:
                stable_id_count += 1
        else:
            errors.append(f"{prefix} must be a string or object")
            continue

        query_count += 1
        if not query:
            errors.append(f"{prefix}.query is required")
        normalized_id = _safe_query_id(explicit_id)
        if normalized_id in normalized_ids:
            errors.append(f"{prefix}.id duplicates normalized id {normalized_id}")
        normalized_ids.add(normalized_id)
        if year and not _looks_like_year_range(year):
            errors.append(
                f"{prefix}.year must be empty or a year/range like 2020, 2020-, -2024, or 2020-2024"
            )
        normalized_query_key = (" ".join(query.lower().split()), year)
        if query and normalized_query_key in normalized_query_keys:
            warnings.append(f"{prefix}.query duplicates an earlier query/year pair")
        normalized_query_keys.add(normalized_query_key)

    if query_count == 0:
        errors.append("query set must contain at least one query")
    if query_count < 10:
        warnings.append("query_count below recommended live-eval minimum of 10")
    if query_count > 20:
        warnings.append("query_count above recommended live-eval maximum of 20")

    return _validation_report(
        QUERY_SET_VALIDATION_SCHEMA,
        source_schema,
        errors,
        warnings,
        {
            "source_format": source_format,
            "query_count": query_count,
            "stable_id_count": stable_id_count,
        },
    )


def run_live_evaluation(
    queries: Iterable[dict[str, str]],
    output_dir: str | Path,
    *,
    search_runner: SearchRunner | None = None,
    limit: int = 50,
    sources: str = "semantic_scholar,openalex,crossref",
    email: str = "",
    legacy_top: int = 30,
    hybrid_top: int = 30,
    channel_top: int = 10,
    resume: bool = False,
) -> dict[str, Any]:
    """Run legacy and hybrid searches for a query set and write evaluation artifacts."""
    runner = search_runner or multi_search.search_with_status
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    reusable_queries = _load_reusable_manifest_queries(manifest_path) if resume else {}
    rows = list(queries)
    manifest_queries: list[dict[str, Any]] = []
    for index, query_row in enumerate(rows, 1):
        query_id = _safe_query_id(str(query_row.get("id") or f"q{index:04d}"))
        query = str(query_row.get("query") or "").strip()
        year = str(query_row.get("year") or "")
        query_dir = output / query_id
        query_dir.mkdir(parents=True, exist_ok=True)
        item: dict[str, Any] = {
            "id": query_id,
            "query": query,
            "year": year,
            "status": "pending",
            "legacy_count": 0,
            "hybrid_count": 0,
            "legacy_result": str(query_dir / "legacy.json"),
            "hybrid_result": str(query_dir / "hybrid.json"),
            "pool": str(query_dir / "judgments_pool.json"),
        }
        reusable_item = reusable_queries.get(query_id)
        if reusable_item and _can_resume_query(manifest_path, reusable_item, query=query, year=year):
            resumed = dict(reusable_item)
            resumed["resumed"] = True
            manifest_queries.append(resumed)
            continue
        try:
            legacy_response = runner(
                query,
                limit=limit,
                year_range=year or None,
                sources=sources,
                email=email,
                strategy="legacy",
            )
            hybrid_response = runner(
                query,
                limit=limit,
                year_range=year or None,
                sources=sources,
                email=email,
                strategy="hybrid",
            )
            legacy_payload = build_search_payload(
                query,
                legacy_response.results,
                year_range=year,
                source="multi_source",
                source_status=legacy_response.source_status,
                query_plan=legacy_response.query_plan,
            )
            hybrid_payload = build_search_payload(
                query,
                hybrid_response.results,
                year_range=year,
                source="multi_source",
                source_status=hybrid_response.source_status,
                query_plan=hybrid_response.query_plan,
            )
            hybrid_contract_report = validate_search_payload_contract(hybrid_payload)
            hybrid_contract_path = query_dir / "hybrid_contract_validation.json"
            hybrid_contract_path.write_text(
                json.dumps(hybrid_contract_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            write_search_payload(legacy_payload, item["legacy_result"])
            write_search_payload(hybrid_payload, item["hybrid_result"])
            pool = build_relevance_pool(
                {"legacy": legacy_payload, "hybrid": hybrid_payload},
                legacy_top=legacy_top,
                hybrid_top=hybrid_top,
                channel_top=channel_top,
            )
            Path(item["pool"]).write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
            item.update(
                {
                    "status": "success",
                    "legacy_count": len(legacy_payload.get("results") or []),
                    "hybrid_count": len(hybrid_payload.get("results") or []),
                    "pool_count": pool.get("count", 0),
                    "legacy_schema": str(legacy_payload.get("schema") or ""),
                    "hybrid_schema": str(hybrid_payload.get("schema") or ""),
                    "hybrid_contract_report": str(hybrid_contract_path),
                    "hybrid_contract_valid": bool(hybrid_contract_report.get("valid")),
                    "hybrid_contract_error_count": len(hybrid_contract_report.get("errors") or []),
                    "hybrid_contract_warning_count": len(hybrid_contract_report.get("warnings") or []),
                    "hybrid_contract_errors": list(hybrid_contract_report.get("errors") or []),
                    "legacy_source_status": legacy_payload.get("source_status") or {},
                    "hybrid_source_status": hybrid_payload.get("source_status") or {},
                }
            )
        except Exception as exc:
            item.update({"status": "failed", "error": str(exc), "error_type": type(exc).__name__})
        manifest_queries.append(item)
    manifest = {
        "schema": LIVE_EVALUATION_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query_count": len(manifest_queries),
        "limit": limit,
        "sources": sources,
        "strategies": ["legacy", "hybrid"],
        "pooling": {"legacy_top": legacy_top, "hybrid_top": hybrid_top, "channel_top": channel_top},
        "queries": manifest_queries,
    }
    review_packet_path = output / "judgments_review_packet.json"
    review_packet = build_relevance_review_packet(
        manifest_queries,
        manifest_path=manifest_path,
        pooling=manifest["pooling"],
    )
    review_packet_path.write_text(json.dumps(review_packet, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["review_packet"] = str(review_packet_path)
    manifest["review_packet_count"] = review_packet["judgment_count"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_relevance_review_packet(
    manifest_queries: Iterable[dict[str, Any]],
    *,
    manifest_path: str | Path,
    pooling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a single cross-query packet for blinded human relevance grading."""
    manifest_file = Path(manifest_path)
    query_packets: list[dict[str, Any]] = []
    skipped_queries: list[dict[str, str]] = []
    judgment_count = 0
    for index, item in enumerate(manifest_queries, 1):
        if not isinstance(item, dict):
            skipped_queries.append({"query_id": f"q{index:04d}", "reason": "invalid_query_row"})
            continue
        query_id = str(item.get("id") or f"q{index:04d}")
        if str(item.get("status") or "") != "success":
            skipped_queries.append({"query_id": query_id, "reason": "query_not_success"})
            continue
        try:
            pool_path = _resolve_manifest_path(manifest_file, item.get("pool"))
            pool_payload = json.loads(pool_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            skipped_queries.append({"query_id": query_id, "reason": "pool_read_error", "detail": str(exc)})
            continue
        rows = pool_payload.get("judgments") if isinstance(pool_payload, dict) else []
        judgments = [_review_judgment_row(row) for row in rows if isinstance(row, dict)]
        judgment_count += len(judgments)
        query_packets.append(
            {
                "query_id": query_id,
                "query": str(item.get("query") or pool_payload.get("query") or ""),
                "year": str(item.get("year") or ""),
                "pool": str(pool_path),
                "count": len(judgments),
                "judgments": judgments,
            }
        )
    return {
        "schema": RELEVANCE_REVIEW_PACKET_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "manifest": str(manifest_file),
        "query_count": len(query_packets),
        "judgment_count": judgment_count,
        "pooling": dict(pooling or {}),
        "review": {
            "anonymous": True,
            "grade_scale": {
                "3": "highly relevant",
                "2": "relevant",
                "1": "marginal",
                "0": "irrelevant",
            },
        },
        "skipped_queries": skipped_queries,
        "queries": query_packets,
    }


def _review_judgment_row(row: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "review_id",
        "id",
        "title",
        "authors",
        "year",
        "doi",
        "arxiv_id",
        "journal",
        "abstract",
        "grade",
        "notes",
    )
    return {key: row.get(key) for key in allowed_keys if key in row}


def validate_live_evaluation_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate a saved live-evaluation manifest without reading artifacts or calling providers."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(manifest, dict):
        errors.append("payload must be an object")
        manifest = {}

    source_schema = str(manifest.get("schema") or "")
    if not source_schema:
        errors.append("schema missing")
    elif source_schema != LIVE_EVALUATION_SCHEMA:
        errors.append(f"schema must be {LIVE_EVALUATION_SCHEMA}")

    queries_value = manifest.get("queries")
    if not isinstance(queries_value, list):
        errors.append("queries must be a list")
        queries: list[dict[str, Any]] = []
    else:
        queries = [item for item in queries_value if isinstance(item, dict)]
        for index, item in enumerate(queries_value):
            if not isinstance(item, dict):
                errors.append(f"queries[{index}] must be an object")

    query_count = _optional_non_negative_int(manifest.get("query_count"), "query_count", errors, required=True)
    if query_count is not None and query_count != len(queries):
        errors.append("query_count does not match queries length")

    if "limit" in manifest:
        _positive_int(manifest.get("limit"), "limit", errors)

    strategies = manifest.get("strategies")
    if strategies is not None:
        if not isinstance(strategies, list):
            errors.append("strategies must be a list")
        elif not {"legacy", "hybrid"}.issubset({str(item) for item in strategies}):
            errors.append("strategies must include legacy and hybrid")

    if not str(manifest.get("review_packet") or "").strip():
        errors.append("review_packet is required")

    if any(key in manifest for key in (
        "query_set_validation",
        "query_set_validation_valid",
        "query_set_validation_error_count",
        "query_set_validation_warning_count",
    )):
        if not str(manifest.get("query_set_validation") or "").strip():
            errors.append("query_set_validation is required when present")
        if not isinstance(manifest.get("query_set_validation_valid"), bool):
            errors.append("query_set_validation_valid must be a boolean")
        _optional_non_negative_int(
            manifest.get("query_set_validation_error_count"),
            "query_set_validation_error_count",
            errors,
            required=True,
        )
        _optional_non_negative_int(
            manifest.get("query_set_validation_warning_count"),
            "query_set_validation_warning_count",
            errors,
            required=True,
        )

    pooling = manifest.get("pooling")
    if pooling is not None:
        if not isinstance(pooling, dict):
            errors.append("pooling must be an object")
        else:
            for field in ("legacy_top", "hybrid_top", "channel_top"):
                if field in pooling:
                    _optional_non_negative_int(pooling.get(field), f"pooling.{field}", errors, required=True)

    pooled_query_rows = 0
    for index, item in enumerate(queries):
        prefix = f"queries[{index}]"
        query_id = str(item.get("id") or item.get("query_id") or "").strip()
        if not query_id:
            errors.append(f"{prefix}.id is required")
        status = str(item.get("status") or "").strip()
        if not status:
            errors.append(f"{prefix}.status is required")
        if item.get("acquisition_started") is True:
            errors.append(f"{prefix}.acquisition_started must be false when present")

        is_success = status == "success"
        if is_success and not str(item.get("query") or "").strip():
            errors.append(f"{prefix}.query is required for success rows")
        for field in ("legacy_count", "hybrid_count", "pool_count"):
            if field in item:
                value = _optional_non_negative_int(item.get(field), f"{prefix}.{field}", errors, required=True)
                if field == "pool_count" and value is not None:
                    pooled_query_rows += value
        if is_success:
            for field in ("legacy_result", "hybrid_result", "pool"):
                if not str(item.get(field) or "").strip():
                    errors.append(f"{prefix}.{field} is required for success rows")
        if "legacy_schema" in item and str(item.get("legacy_schema") or "") != SEARCH_SCHEMA:
            errors.append(f"{prefix}.legacy_schema must be {SEARCH_SCHEMA}")
        if "hybrid_schema" in item and str(item.get("hybrid_schema") or "") != SEARCH_SCHEMA_V2:
            errors.append(f"{prefix}.hybrid_schema must be {SEARCH_SCHEMA_V2}")
        if "hybrid_contract_valid" in item and not isinstance(item.get("hybrid_contract_valid"), bool):
            errors.append(f"{prefix}.hybrid_contract_valid must be a boolean")
        for status_field in ("legacy_source_status", "hybrid_source_status"):
            _validate_live_source_status_map(item.get(status_field), f"{prefix}.{status_field}", errors)

    review_packet_count = _optional_non_negative_int(
        manifest.get("review_packet_count"),
        "review_packet_count",
        errors,
        required=False,
    )
    if review_packet_count is not None and pooled_query_rows and review_packet_count > pooled_query_rows:
        errors.append("review_packet_count cannot exceed pooled query rows")

    return _validation_report(
        LIVE_EVALUATION_VALIDATION_SCHEMA,
        source_schema,
        errors,
        warnings,
        {"query_count": len(queries)},
    )


def validate_relevance_review_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Validate a blinded pooled relevance review packet without changing grades."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(packet, dict):
        errors.append("payload must be an object")
        packet = {}

    source_schema = str(packet.get("schema") or "")
    if not source_schema:
        errors.append("schema missing")
    elif source_schema != RELEVANCE_REVIEW_PACKET_SCHEMA:
        errors.append(f"schema must be {RELEVANCE_REVIEW_PACKET_SCHEMA}")

    review = packet.get("review")
    if not isinstance(review, dict):
        errors.append("review must be an object")
    else:
        if review.get("anonymous") is not True:
            errors.append("review.anonymous must be true")
        if not isinstance(review.get("grade_scale"), dict):
            errors.append("review.grade_scale must be an object")

    pooling = packet.get("pooling")
    if pooling is not None:
        if not isinstance(pooling, dict):
            errors.append("pooling must be an object")
        else:
            for field in ("legacy_top", "hybrid_top", "channel_top"):
                if field in pooling:
                    _optional_non_negative_int(pooling.get(field), f"pooling.{field}", errors, required=True)

    skipped = packet.get("skipped_queries")
    if skipped is not None:
        if not isinstance(skipped, list):
            errors.append("skipped_queries must be a list")
        else:
            for index, item in enumerate(skipped):
                if not isinstance(item, dict):
                    errors.append(f"skipped_queries[{index}] must be an object")
                    continue
                if not str(item.get("query_id") or "").strip():
                    errors.append(f"skipped_queries[{index}].query_id is required")
                if not str(item.get("reason") or "").strip():
                    errors.append(f"skipped_queries[{index}].reason is required")

    queries_value = packet.get("queries")
    if not isinstance(queries_value, list):
        errors.append("queries must be a list")
        queries: list[dict[str, Any]] = []
    else:
        queries = [item for item in queries_value if isinstance(item, dict)]
        for index, item in enumerate(queries_value):
            if not isinstance(item, dict):
                errors.append(f"queries[{index}] must be an object")

    query_count = _optional_non_negative_int(packet.get("query_count"), "query_count", errors, required=True)
    if query_count is not None and query_count != len(queries):
        errors.append("query_count does not match queries length")

    total_judgments = 0
    for query_index, query in enumerate(queries):
        prefix = f"queries[{query_index}]"
        if not str(query.get("query_id") or query.get("id") or "").strip():
            errors.append(f"{prefix}.query_id is required")
        if not str(query.get("query") or "").strip():
            errors.append(f"{prefix}.query is required")
        if query.get("acquisition_started") is True:
            errors.append(f"{prefix}.acquisition_started must be false when present")
        judgments_value = query.get("judgments")
        if not isinstance(judgments_value, list):
            errors.append(f"{prefix}.judgments must be a list")
            judgments: list[dict[str, Any]] = []
        else:
            judgments = [item for item in judgments_value if isinstance(item, dict)]
            for index, item in enumerate(judgments_value):
                if not isinstance(item, dict):
                    errors.append(f"{prefix}.judgments[{index}] must be an object")
        total_judgments += len(judgments)
        count = _optional_non_negative_int(query.get("count"), f"{prefix}.count", errors, required=False)
        if count is not None and count != len(judgments):
            errors.append(f"{prefix}.count does not match judgments length")
        seen_review_ids: set[str] = set()
        seen_ids: set[str] = set()
        for judgment_index, judgment in enumerate(judgments):
            judgment_prefix = f"{prefix}.judgments[{judgment_index}]"
            review_id = str(judgment.get("review_id") or "").strip()
            record_id = str(judgment.get("id") or "").strip()
            if not review_id:
                errors.append(f"{judgment_prefix}.review_id is required")
            elif review_id in seen_review_ids:
                errors.append(f"{judgment_prefix}.review_id duplicates {review_id}")
            seen_review_ids.add(review_id)
            if not record_id:
                errors.append(f"{judgment_prefix}.id is required")
            elif record_id in seen_ids:
                errors.append(f"{judgment_prefix}.id duplicates {record_id}")
            seen_ids.add(record_id)
            if not str(judgment.get("title") or "").strip():
                errors.append(f"{judgment_prefix}.title is required")
            _validate_grade(judgment.get("grade"), f"{judgment_prefix}.grade", errors)
            if "pool_sources" in judgment:
                errors.append(f"{judgment_prefix}.pool_sources must not be present in blinded review packets")
            if "retrieval_provenance" in judgment:
                errors.append(
                    f"{judgment_prefix}.retrieval_provenance must not be present in blinded review packets"
                )
            if judgment.get("acquisition_started") is True:
                errors.append(f"{judgment_prefix}.acquisition_started must be false when present")

    judgment_count = _optional_non_negative_int(packet.get("judgment_count"), "judgment_count", errors, required=True)
    if judgment_count is not None and judgment_count != total_judgments:
        errors.append("judgment_count does not match total query judgments")

    return _validation_report(
        RELEVANCE_REVIEW_PACKET_VALIDATION_SCHEMA,
        source_schema,
        errors,
        warnings,
        {"query_count": len(queries), "judgment_count": total_judgments},
    )


def _validate_live_source_status_map(value: Any, prefix: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{prefix} must be an object")
        return
    for source, row in value.items():
        row_prefix = f"{prefix}.{source}"
        if not isinstance(row, dict):
            errors.append(f"{row_prefix} must be an object")
            continue
        if not str(row.get("status") or "").strip():
            errors.append(f"{row_prefix}.status is required")
        if "count" in row:
            _optional_non_negative_int(row.get("count"), f"{row_prefix}.count", errors, required=True)
        if "retryable" in row and not isinstance(row.get("retryable"), bool):
            errors.append(f"{row_prefix}.retryable must be a boolean")


def _optional_non_negative_int(value: Any, label: str, errors: list[str], *, required: bool) -> int | None:
    if value is None:
        if required:
            errors.append(f"{label} must be a non-negative integer")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"{label} must be a non-negative integer")
        return None
    return value


def _positive_int(value: Any, label: str, errors: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        errors.append(f"{label} must be a positive integer")
        return None
    return value


def _validate_grade(value: Any, label: str, errors: list[str]) -> None:
    if value is None or value == "":
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 3:
        errors.append(f"{label} must be null or an integer from 0-3")


def _validation_report(
    schema: str,
    source_schema: str,
    errors: list[str],
    warnings: list[str],
    summary: dict[str, Any],
) -> dict[str, Any]:
    summary = dict(summary)
    summary["error_count"] = len(errors)
    summary["warning_count"] = len(warnings)
    return {
        "schema": schema,
        "valid": not errors,
        "source_schema": source_schema,
        "summary": summary,
        "errors": errors,
        "warnings": warnings,
    }


def _looks_like_year_range(value: str) -> bool:
    return bool(re.fullmatch(r"(?:\d{4}|\d{4}-|-\d{4}|\d{4}-\d{4})", value.strip()))


def _load_review_packet_judgments(manifest_file: Path, manifest: dict[str, Any]) -> dict[str, dict[str, int]]:
    review_packet_value = manifest.get("review_packet") if isinstance(manifest, dict) else None
    if not review_packet_value:
        return {}
    review_packet_path = _resolve_manifest_path(manifest_file, review_packet_value)
    payload = json.loads(review_packet_path.read_text(encoding="utf-8-sig"))
    query_rows = payload.get("queries") if isinstance(payload, dict) else []
    if not isinstance(query_rows, list):
        raise ValueError("Review packet must contain a queries list.")
    judgments_by_query: dict[str, dict[str, int]] = {}
    for index, query_row in enumerate(query_rows, 1):
        if not isinstance(query_row, dict):
            continue
        query_id = str(query_row.get("query_id") or query_row.get("id") or f"q{index:04d}")
        judgments_by_query[query_id] = _judgments_from_rows(query_row.get("judgments") or [])
    return judgments_by_query


def _load_review_packet_judgment_coverage(manifest_file: Path, manifest: dict[str, Any]) -> dict[str, dict[str, int]]:
    review_packet_value = manifest.get("review_packet") if isinstance(manifest, dict) else None
    if not review_packet_value:
        return {}
    review_packet_path = _resolve_manifest_path(manifest_file, review_packet_value)
    payload = json.loads(review_packet_path.read_text(encoding="utf-8-sig"))
    query_rows = payload.get("queries") if isinstance(payload, dict) else []
    if not isinstance(query_rows, list):
        raise ValueError("Review packet must contain a queries list.")
    coverage_by_query: dict[str, dict[str, int]] = {}
    for index, query_row in enumerate(query_rows, 1):
        if not isinstance(query_row, dict):
            continue
        query_id = str(query_row.get("query_id") or query_row.get("id") or f"q{index:04d}")
        coverage_by_query[query_id] = _judgment_coverage_from_rows(query_row.get("judgments") or [])
    return coverage_by_query


def _load_judgment_coverage(path: str | Path) -> dict[str, int]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        if isinstance(payload.get("judgments"), list):
            return _judgment_coverage_from_rows(payload["judgments"])
        rows = [{"id": key, "grade": value} for key, value in payload.items()]
        return _judgment_coverage_from_rows(rows)
    if isinstance(payload, list):
        return _judgment_coverage_from_rows(payload)
    return {"judgment_count": 0, "graded_judgment_count": 0, "ungraded_judgment_count": 0}


def _judgment_coverage_from_rows(rows: Iterable[Any]) -> dict[str, int]:
    total = 0
    graded = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or row.get("canonical_id") or row.get("canonical_work_id") or "").strip()
        if not key and row.get("doi"):
            key = f"doi:{normalize_doi(str(row.get('doi') or ''))}"
        if not key:
            continue
        total += 1
        grade_value = row.get("grade") if row.get("grade") is not None else row.get("relevance")
        if grade_value is not None and grade_value != "":
            graded += 1
    return {
        "judgment_count": total,
        "graded_judgment_count": graded,
        "ungraded_judgment_count": total - graded,
    }


def _add_judgment_totals(total: dict[str, int], coverage: dict[str, int]) -> None:
    for key in ("judgment_count", "graded_judgment_count", "ungraded_judgment_count"):
        total[key] = int(total.get(key) or 0) + int(coverage.get(key) or 0)


def _judgments_from_rows(rows: Iterable[Any]) -> dict[str, int]:
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
        judgments[key] = int(grade_value)
    return judgments


def _load_reusable_manifest_queries(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    queries = payload.get("queries") if isinstance(payload, dict) else []
    if not isinstance(queries, list):
        return {}
    reusable: dict[str, dict[str, Any]] = {}
    for item in queries:
        if not isinstance(item, dict):
            continue
        query_id = str(item.get("id") or "").strip()
        if query_id and str(item.get("status") or "") == "success":
            reusable[query_id] = dict(item)
    return reusable


def _can_resume_query(manifest_path: Path, item: dict[str, Any], *, query: str, year: str) -> bool:
    if str(item.get("query") or "").strip() != query:
        return False
    if str(item.get("year") or "") != year:
        return False
    for key in ("legacy_result", "hybrid_result", "pool"):
        try:
            if not _resolve_manifest_path(manifest_path, item.get(key)).exists():
                return False
        except ValueError:
            return False
    return True


def evaluate_live_evaluation_gate(manifest_path: str | Path) -> dict[str, Any]:
    """Build a release-gate report from a live-evaluation manifest.

    Each successful query row is compared using its saved hybrid result,
    legacy result, and graded pooled judgments. No provider APIs are called.
    """
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    queries = manifest.get("queries") if isinstance(manifest, dict) else []
    if not isinstance(queries, list):
        raise ValueError("Live-evaluation manifest must contain a queries list.")

    provider_status_summary = _summarize_provider_statuses(queries)
    comparisons: list[dict[str, Any]] = []
    data_issues: list[dict[str, str]] = _manifest_query_set_validation_issues(manifest)
    judgment_totals = {"judgment_count": 0, "graded_judgment_count": 0, "ungraded_judgment_count": 0}
    try:
        review_packet_judgments = _load_review_packet_judgments(manifest_file, manifest)
        review_packet_coverage = _load_review_packet_judgment_coverage(manifest_file, manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        review_packet_judgments = {}
        review_packet_coverage = {}
        data_issues.append({"query_id": "manifest", "reason": "review_packet_error", "detail": str(exc)})
    for index, item in enumerate(queries, 1):
        if not isinstance(item, dict):
            data_issues.append({"query_id": f"q{index:04d}", "reason": "invalid_query_row"})
            continue
        query_id = str(item.get("id") or f"q{index:04d}")
        if str(item.get("status") or "") != "success":
            data_issues.append({"query_id": query_id, "reason": "query_not_success"})
            continue
        try:
            legacy_path = _resolve_manifest_path(manifest_file, item.get("legacy_result"))
            hybrid_path = _resolve_manifest_path(manifest_file, item.get("hybrid_result"))
            judgments = review_packet_judgments.get(query_id, {})
            coverage = review_packet_coverage.get(query_id, {})
            if not judgments:
                judgments_path = _resolve_manifest_path(manifest_file, item.get("judgments") or item.get("pool"))
                judgments = load_judgments(judgments_path)
                coverage = _load_judgment_coverage(judgments_path)
            if coverage:
                _add_judgment_totals(judgment_totals, coverage)
                if int(coverage.get("ungraded_judgment_count") or 0) > 0:
                    data_issues.append(
                        {
                            "query_id": query_id,
                            "reason": "ungraded_judgments",
                            "judgment_count": str(int(coverage.get("judgment_count") or 0)),
                            "graded_judgment_count": str(int(coverage.get("graded_judgment_count") or 0)),
                            "ungraded_judgment_count": str(int(coverage.get("ungraded_judgment_count") or 0)),
                        }
                    )
            if not judgments:
                data_issues.append({"query_id": query_id, "reason": "missing_or_ungraded_judgments"})
                continue
            legacy_payload = load_search_payload(legacy_path)
            hybrid_payload = load_search_payload(hybrid_path)
            data_issues.extend(_live_query_contract_issues(query_id, legacy_payload, hybrid_payload))
            comparison = compare_ranked_results(
                hybrid_payload.get("results") or [],
                legacy_payload.get("results") or [],
                judgments,
            )
            comparison["query_id"] = query_id
            comparison["query"] = str(item.get("query") or hybrid_payload.get("query") or legacy_payload.get("query") or "")
            comparisons.append(comparison)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            data_issues.append(
                {
                    "query_id": query_id,
                    "reason": "benchmark_input_error",
                    "detail": str(exc),
                }
            )

    report = build_release_gate_report(comparisons)
    report["manifest"] = str(manifest_file)
    report["data_issues"] = data_issues
    report["provider_failures"] = provider_status_summary["failures"]
    report["checks"]["no_data_issues"] = not data_issues
    report["checks"]["all_judgments_graded"] = judgment_totals["ungraded_judgment_count"] == 0
    report["summary"]["data_issue_count"] = len(data_issues)
    report["summary"].update(judgment_totals)
    report["summary"]["provider_status_count"] = provider_status_summary["count"]
    report["summary"]["provider_failure_count"] = provider_status_summary["failure_count"]
    report["summary"]["provider_failure_rate"] = provider_status_summary["failure_rate"]
    aggregate_blockers = [
        dict(item)
        for item in report.get("release_gate_blockers") or []
        if isinstance(item, dict)
        and str(item.get("type") or "") not in {"recall_below_baseline", "manual_review_required"}
    ]
    blockers = aggregate_blockers + _build_release_gate_blockers(
        report.get("queries") or [],
        data_issues,
        provider_status_summary["failures"],
    )
    report["release_gate_blockers"] = blockers
    report["summary"]["release_gate_blocker_count"] = len(blockers)
    report["passed"] = bool(report.get("passed")) and not data_issues
    return report


def _live_query_contract_issues(
    query_id: str,
    legacy_payload: dict[str, Any],
    hybrid_payload: dict[str, Any],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    legacy_schema = str(legacy_payload.get("schema") or "")
    if legacy_schema != SEARCH_SCHEMA:
        issues.append(
            {
                "query_id": query_id,
                "reason": "legacy_contract_not_v1",
                "detail": f"legacy_result schema is {legacy_schema or 'missing'}",
            }
        )
    hybrid_schema = str(hybrid_payload.get("schema") or "")
    if hybrid_schema != SEARCH_SCHEMA_V2:
        issues.append(
            {
                "query_id": query_id,
                "reason": "hybrid_contract_not_v2",
                "detail": f"hybrid_result schema is {hybrid_schema or 'missing'}",
            }
        )
        return issues
    contract_report = validate_search_payload_contract(hybrid_payload)
    if not contract_report.get("valid"):
        errors = [str(item) for item in contract_report.get("errors") or []]
        issues.append(
            {
                "query_id": query_id,
                "reason": "hybrid_contract_invalid",
                "detail": "; ".join(errors[:5]),
                "error_count": str(len(errors)),
            }
        )
    return issues


def _manifest_query_set_validation_issues(manifest: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(manifest, dict):
        return []
    if not any(
        key in manifest
        for key in (
            "query_set_validation",
            "query_set_validation_valid",
            "query_set_validation_error_count",
        )
    ):
        return []
    valid = manifest.get("query_set_validation_valid")
    error_count_raw = manifest.get("query_set_validation_error_count")
    try:
        error_count = int(error_count_raw)
    except (TypeError, ValueError):
        error_count = 0
    if valid is True and error_count == 0:
        return []
    return [
        {
            "query_id": "manifest",
            "reason": "query_set_validation_invalid",
            "detail": str(manifest.get("query_set_validation") or ""),
            "error_count": str(error_count),
        }
    ]


def render_release_gate_markdown(
    report: dict[str, Any],
    *,
    title: str = "InstSci Search Release Gate",
) -> str:
    """Render a human-readable Search v2 release-gate summary."""
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
        "| metric | value |",
        "| --- | ---: |",
        f"| passed | {_format_markdown_value(bool(report.get('passed')))} |",
    ]
    for key in (
        "query_count",
        "ndcg_improved_queries",
        "ndcg_improved_share",
        "recall_failure_count",
        "severe_ndcg_regression_count",
        "manual_review_required_count",
        "judgment_count",
        "graded_judgment_count",
        "ungraded_judgment_count",
        "data_issue_count",
        "provider_status_count",
        "provider_failure_count",
        "provider_failure_rate",
        "release_gate_blocker_count",
    ):
        if key in summary:
            lines.append(f"| {_markdown_cell(key)} | {_format_markdown_value(summary.get(key))} |")

    lines.extend(["", "## Checks", ""])
    if checks:
        lines.extend(["| check | passed |", "| --- | ---: |"])
        for key in sorted(checks):
            lines.append(f"| {_markdown_cell(key)} | {_format_markdown_value(bool(checks.get(key)))} |")
    else:
        lines.append("- none")

    lines.extend(["", "## Release Gate Blockers", ""])
    blockers = [item for item in report.get("release_gate_blockers") or [] if isinstance(item, dict)]
    if blockers:
        lines.extend(
            [
                "| type | query_id | severity | blocks_gate | reason/status/action | target |",
                "| --- | --- | --- | ---: | --- | --- |",
            ]
        )
        for item in blockers:
            reason = item.get("reason") or item.get("status") or item.get("action") or ""
            target = item.get("metric") or item.get("source") or item.get("detail") or ""
            blocker_type = str(item.get("type") or "")
            severity = item.get("severity") or {
                "data_issue": "failure",
                "manual_review_required": "manual_review_required",
                "provider_failure": "diagnostic",
            }.get(blocker_type, "")
            lines.append(
                f"| {_markdown_cell(blocker_type)} | "
                f"{_markdown_cell(item.get('query_id') or '')} | "
                f"{_markdown_cell(severity)} | "
                f"{_format_markdown_value(bool(item.get('blocks_gate')))} | "
                f"{_markdown_cell(reason)} | {_markdown_cell(target)} |"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Query Results", ""])
    queries = [item for item in report.get("queries") or [] if isinstance(item, dict)]
    if queries:
        lines.extend(["| query_id | query | passed | failures |", "| --- | --- | ---: | --- |"])
        for item in queries:
            failures = ", ".join(str(value) for value in item.get("failures") or []) or ""
            lines.append(
                f"| {_markdown_cell(item.get('query_id') or item.get('id') or '')} | "
                f"{_markdown_cell(item.get('query') or '')} | "
                f"{_format_markdown_value(bool(item.get('passed')))} | {_markdown_cell(failures)} |"
            )
    else:
        lines.append("- none")

    diagnostics: list[dict[str, Any]] = []
    for item in queries:
        query_id = str(item.get("query_id") or item.get("id") or "")
        query_text = str(item.get("query") or "")
        for diagnostic in item.get("diagnostics") or []:
            if isinstance(diagnostic, dict):
                diagnostics.append({"query_id": query_id, "query": query_text, **diagnostic})

    lines.extend(["", "## Query Diagnostics", ""])
    if diagnostics:
        lines.extend(
            [
                "| query_id | metric | severity | action | candidate | baseline | relative_delta |",
                "| --- | --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for item in diagnostics:
            lines.append(
                f"| {_markdown_cell(item.get('query_id') or '')} | "
                f"{_markdown_cell(item.get('metric') or '')} | "
                f"{_markdown_cell(item.get('severity') or '')} | "
                f"{_markdown_cell(item.get('action') or '')} | "
                f"{_format_markdown_value(item.get('candidate'))} | "
                f"{_format_markdown_value(item.get('baseline'))} | "
                f"{_format_percent_value(item.get('relative_delta'))} |"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Data Issues", ""])
    data_issues = [item for item in report.get("data_issues") or [] if isinstance(item, dict)]
    if data_issues:
        lines.extend(["| query_id | reason | detail |", "| --- | --- | --- |"])
        for item in data_issues:
            lines.append(
                f"| {_markdown_cell(item.get('query_id') or '')} | "
                f"{_markdown_cell(item.get('reason') or '')} | {_markdown_cell(item.get('detail') or '')} |"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Provider Failures", ""])
    provider_failures = [item for item in report.get("provider_failures") or [] if isinstance(item, dict)]
    if provider_failures:
        lines.extend(["| query_id | strategy | source | status | detail |", "| --- | --- | --- | --- | --- |"])
        for item in provider_failures:
            lines.append(
                f"| {_markdown_cell(item.get('query_id') or '')} | "
                f"{_markdown_cell(item.get('strategy') or '')} | "
                f"{_markdown_cell(item.get('source') or '')} | "
                f"{_markdown_cell(item.get('status') or '')} | "
                f"{_markdown_cell(item.get('detail') or '')} |"
            )
    else:
        lines.append("- none")

    lines.append("")
    return "\n".join(lines)


def _summarize_provider_statuses(queries: Iterable[Any]) -> dict[str, Any]:
    count = 0
    failures: list[dict[str, Any]] = []
    for index, item in enumerate(queries, 1):
        if not isinstance(item, dict):
            continue
        query_id = str(item.get("id") or f"q{index:04d}")
        for strategy, status_key in (
            ("legacy", "legacy_source_status"),
            ("hybrid", "hybrid_source_status"),
        ):
            statuses = item.get(status_key) or {}
            if not isinstance(statuses, dict):
                continue
            for source, status_row in statuses.items():
                if not isinstance(status_row, dict):
                    continue
                status = str(status_row.get("status") or "").strip()
                if not status:
                    continue
                count += 1
                if status == "success":
                    continue
                failures.append(
                    {
                        "query_id": query_id,
                        "strategy": strategy,
                        "source": str(source),
                        "provider": str(status_row.get("provider") or ""),
                        "channel": str(status_row.get("channel") or ""),
                        "query_variant": str(status_row.get("query_variant") or ""),
                        "status": status,
                        "count": int(status_row.get("count") or 0),
                        "retryable": bool(status_row.get("retryable") or False),
                        "detail": str(status_row.get("detail") or ""),
                    }
                )
    failure_count = len(failures)
    return {
        "count": count,
        "failure_count": failure_count,
        "failure_rate": failure_count / count if count else 0.0,
        "failures": failures,
    }


def _build_release_gate_blockers(
    queries: Iterable[Any],
    data_issues: Iterable[Any],
    provider_failures: Iterable[Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for item in data_issues:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "")
        blocker_type = reason if reason in {
            "legacy_contract_not_v1",
            "hybrid_contract_not_v2",
            "hybrid_contract_invalid",
        } else "data_issue"
        blocker = {
            "type": blocker_type,
            "query_id": str(item.get("query_id") or ""),
            "reason": reason,
            "severity": "failure",
            "blocks_gate": True,
        }
        for key in ("detail", "error_count", "judgment_count", "graded_judgment_count", "ungraded_judgment_count"):
            if key in item:
                blocker[key] = item.get(key)
        blockers.append(blocker)
    blockers.extend(release_gate_blockers_from_queries(query for query in queries if isinstance(query, dict)))
    for item in provider_failures:
        if not isinstance(item, dict):
            continue
        blockers.append(
            {
                "type": "provider_failure",
                "query_id": str(item.get("query_id") or ""),
                "strategy": str(item.get("strategy") or ""),
                "source": str(item.get("source") or ""),
                "provider": str(item.get("provider") or ""),
                "channel": str(item.get("channel") or ""),
                "query_variant": str(item.get("query_variant") or ""),
                "status": str(item.get("status") or ""),
                "detail": str(item.get("detail") or ""),
                "severity": "diagnostic",
                "blocks_gate": False,
            }
        )
    return blockers


def _format_markdown_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if value is None:
        return ""
    return _markdown_cell(value)


def _format_percent_value(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return _format_markdown_value(value)
    text = f"{float(value) * 100:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _resolve_manifest_path(manifest_path: Path, value: Any) -> Path:
    if not value:
        raise ValueError("Manifest query row is missing a required artifact path.")
    path = Path(str(value))
    if path.is_absolute():
        return path
    if path.exists():
        return path
    direct = manifest_path.parent / path
    if direct.exists():
        return direct
    parts = list(path.parts)
    parent_name = manifest_path.parent.name
    if parent_name in parts:
        index = len(parts) - 1 - parts[::-1].index(parent_name)
        suffix = Path(*parts[index + 1 :])
        anchored = manifest_path.parent / suffix
        if anchored.exists():
            return anchored
    return direct


def _safe_query_id(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "_" for character in value.strip())
    normalized = "_".join(part for part in normalized.split("_") if part)
    return normalized or "query"
