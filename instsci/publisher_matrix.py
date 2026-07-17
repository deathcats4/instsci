"""Publisher capability matrix for lightweight InstSci run planning."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MATRIX_STATUSES = {
    "ready",
    "prewarm_required",
    "waf_risky",
    "access_side_check_needed",
    "route_not_published",
    "unclassified",
}

TERMINAL_BLOCKER_STATES = {
    "auth_required",
    "human_verification_required",
    "waf_blocked",
    "access_unavailable",
    "publisher_error",
    "unsupported_publisher",
    "ambiguous_search_result",
    "daily_limit_reached",
    "quota_state_error",
}

STATUS_SUGGESTED_PATHS = {
    "success": ["zotero_sync"],
    "unverified": ["inspect_pdf_text", "manual_verify", "rerun_diagnose"],
    "auth_required": ["complete_institution_login", "rerun_same_browser_profile"],
    "human_verification_required": ["complete_visible_human_verification", "rerun_same_browser_profile"],
    "waf_blocked": ["stop_batch", "retry_later", "manual_browser_single_doi"],
    "access_unavailable": ["oa_retry", "library_resolver", "ill_request", "author_email"],
    "publisher_error": ["retry_later", "test_another_doi", "oa_retry"],
    "pdf_candidate_conflict": ["diagnose_pdf_candidates", "manual_select_main_pdf"],
    "capture_failed": ["rerun_diagnose", "oa_retry", "library_resolver"],
    "browser_group_pending": ["publisher_doctor_matrix", "split_by_publisher", "rerun_by_publisher", "workflow_plan"],
    "unsupported_publisher": ["add_publisher_profile", "oa_retry", "library_resolver", "ill_request"],
    "ambiguous_search_result": ["manual_browser_single_doi", "rerun_diagnose"],
    "daily_limit_reached": ["stop_batch", "review_local_policy", "retry_next_day"],
    "quota_state_error": ["inspect_local_state", "stop_batch"],
}

MATRIX_STATUS_SUGGESTED_PATHS = {
    "ready": ["batch_run"],
    "prewarm_required": ["single_doi_prewarm", "rerun_same_browser_profile"],
    "waf_risky": ["manual_browser_single_doi", "stop_batch", "retry_later"],
    "access_side_check_needed": ["regular_browser_access_check", "library_resolver", "single_doi_prewarm"],
    "route_not_published": ["add_publisher_profile", "oa_retry", "library_resolver"],
    "unclassified": ["manual_browser_single_doi", "add_publisher_profile"],
}


@dataclass(frozen=True)
class PublisherMatrixEntry:
    key: str
    status: str
    batch_policy: str = "allow"
    prewarm: bool = False
    known_blocker: str = ""
    note: str = ""

    @property
    def should_skip_batch(self) -> bool:
        return self.batch_policy in {"skip", "single_only"} or self.status in {
            "waf_risky",
            "route_not_published",
            "unclassified",
        }


def _matrix_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "publisher_public_capability_summary.json"


def normalize_publisher_key(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "-")


def canonical_publisher_key(value: str) -> str:
    """Resolve known aliases to the canonical publisher-profile key."""
    normalized = normalize_publisher_key(value)
    try:
        from .publisher_profiles import get_publisher_profile, list_publisher_profiles

        profile = get_publisher_profile(normalized)
        for key in list_publisher_profiles():
            if get_publisher_profile(key) is profile:
                return key
    except ValueError:
        pass
    return normalized


def load_publisher_matrix(path: str | Path | None = None) -> dict[str, PublisherMatrixEntry]:
    matrix_path = Path(path) if path else _matrix_path()
    if not matrix_path.exists():
        return {}
    try:
        raw = json.loads(matrix_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Publisher matrix is invalid: {matrix_path}: {exc}") from exc
    entries: dict[str, PublisherMatrixEntry] = {}
    for key, item in (raw.get("publishers") or {}).items():
        if not isinstance(item, dict):
            continue
        normalized_key = normalize_publisher_key(key)
        status = str(item.get("status") or "unclassified")
        batch_policy = str(item.get("batch_policy") or "allow")
        if status not in MATRIX_STATUSES:
            status = "unclassified"
            batch_policy = "single_only"
        entries[normalized_key] = PublisherMatrixEntry(
            key=normalized_key,
            status=status,
            batch_policy=batch_policy,
            prewarm=bool(item.get("prewarm")),
            known_blocker=str(item.get("known_blocker") or ""),
            note=str(item.get("note") or ""),
        )
    return entries


def get_publisher_matrix_entry(publisher: str) -> PublisherMatrixEntry:
    key = canonical_publisher_key(publisher)
    matrix = load_publisher_matrix()
    return matrix.get(key, _unclassified_entry(key))


def _unclassified_entry(key: str) -> PublisherMatrixEntry:
    return PublisherMatrixEntry(
        key=key,
        status="unclassified",
        batch_policy="single_only",
        prewarm=True,
        note="Publisher route is not classified in the public capability summary.",
    )


def matrix_skip_reason(entry: PublisherMatrixEntry, *, count: int, force: bool = False) -> str:
    if force:
        return ""
    if entry.status == "route_not_published":
        return entry.note or "Publisher route is not published in the public capability summary."
    if entry.status == "waf_risky" and count > 1:
        return entry.note or "Publisher is WAF-risky; run a single DOI prewarm or use --force."
    if entry.batch_policy == "single_only" and count > 1:
        return entry.note or "Publisher is marked single-DOI only; run a prewarm or use --force."
    if entry.batch_policy == "skip":
        return entry.note or "Publisher is marked skip in the local capability matrix."
    return ""


def matrix_batch_recommendation(entry: PublisherMatrixEntry) -> str:
    """Return a compact batch recommendation for a publisher matrix entry."""
    if entry.status == "route_not_published" or entry.batch_policy == "skip":
        return "skip"
    if entry.status == "waf_risky" or entry.batch_policy == "single_only":
        return "single_doi_only"
    if entry.prewarm or entry.status == "prewarm_required":
        return "single_doi_prewarm_then_batch"
    return "batch_ok"


def matrix_risk_flags(entry: PublisherMatrixEntry) -> list[str]:
    """Return machine-readable risk flags for the publisher-doctor matrix panel."""
    flags: list[str] = []
    if entry.status != "ready":
        flags.append(entry.status)
    if entry.prewarm and "prewarm_required" not in flags:
        flags.append("prewarm_required")
    if entry.batch_policy in {"single_only", "skip"}:
        flags.append(f"batch_policy_{entry.batch_policy}")
    if entry.known_blocker:
        flags.append(f"known_blocker_{entry.known_blocker}")
    return list(dict.fromkeys(flags))


def matrix_suggested_paths(entry: PublisherMatrixEntry) -> list[str]:
    """Return next workflow paths for publisher matrix readiness planning."""
    paths = list(MATRIX_STATUS_SUGGESTED_PATHS.get(entry.status) or ["batch_run"])
    if entry.prewarm and "single_doi_prewarm" not in paths:
        paths.insert(0, "single_doi_prewarm")
    if entry.batch_policy == "single_only" and "manual_browser_single_doi" not in paths:
        paths.append("manual_browser_single_doi")
    if entry.batch_policy == "skip" and "stop_batch" not in paths:
        paths.insert(0, "stop_batch")
    return list(dict.fromkeys(paths))


def entry_to_panel_item(entry: PublisherMatrixEntry, *, configured: bool = True) -> dict[str, Any]:
    """Convert a matrix entry into the publisher-doctor panel contract."""
    return {
        "publisher": entry.key,
        "configured": configured,
        "status": entry.status,
        "batch_policy": entry.batch_policy,
        "batch_recommendation": matrix_batch_recommendation(entry),
        "prewarm": entry.prewarm,
        "known_blocker": entry.known_blocker,
        "risk_flags": matrix_risk_flags(entry),
        "suggested_paths": matrix_suggested_paths(entry),
        "note": entry.note,
    }


def build_publisher_matrix_report(
    publisher: str = "all",
    *,
    entries: dict[str, PublisherMatrixEntry] | None = None,
) -> dict[str, Any]:
    """Build the public publisher-doctor matrix report."""
    matrix = entries if entries is not None else load_publisher_matrix()
    wanted = canonical_publisher_key(publisher)
    if wanted and wanted != "all":
        configured = wanted in matrix
        selected = [matrix.get(wanted, _unclassified_entry(wanted))]
        publisher_value = wanted
    else:
        configured = True
        selected = [matrix[key] for key in sorted(matrix)]
        publisher_value = "all"

    items = [entry_to_panel_item(entry, configured=(configured if len(selected) == 1 else True)) for entry in selected]
    status_counts: dict[str, int] = {}
    recommendation_counts: dict[str, int] = {}
    for item in items:
        status = str(item["status"])
        recommendation = str(item["batch_recommendation"])
        status_counts[status] = status_counts.get(status, 0) + 1
        recommendation_counts[recommendation] = recommendation_counts.get(recommendation, 0) + 1

    return {
        "schema": "instsci.publisher_matrix_report.v1",
        "schema_version": 1,
        "publisher": publisher_value,
        "summary": {
            "entries": len(items),
            "status_counts": status_counts,
            "batch_recommendation_counts": recommendation_counts,
            "ready": status_counts.get("ready", 0),
            "prewarm_required": status_counts.get("prewarm_required", 0),
            "waf_risky": status_counts.get("waf_risky", 0),
            "route_not_published": status_counts.get("route_not_published", 0),
            "unclassified": status_counts.get("unclassified", 0),
            "batch_ok": recommendation_counts.get("batch_ok", 0),
            "batch_guarded": len(items) - recommendation_counts.get("batch_ok", 0),
        },
        "items": items,
    }


def normalize_failure_status(reason: str = "", state: str = "") -> str:
    haystack = f"{reason} {state}".lower()
    if "captcha_or_waf" in haystack:
        return "human_verification_required"
    if "publisher_error" in haystack or "cpe000" in haystack or "problem providing the content" in haystack:
        return "publisher_error"
    if "viewer_timeout" in haystack or "viewer timeout" in haystack or "challenge_or_viewer_timeout" in haystack:
        return "capture_failed"
    if any(marker in haystack for marker in ("entitlement", "not entitled", "no access", "purchase pdf", "purchase access", "article preview", "institution_not_registered", "not registered", "access_unavailable")):
        return "access_unavailable"
    if "human_verification" in haystack or "verify you are human" in haystack or "confirm you are human" in haystack or "captcha" in haystack:
        return "human_verification_required"
    if any(marker in haystack for marker in ("cloudflare", "waf", "ray id", "ddos", "too many requests", "security check")):
        return "waf_blocked"
    if any(marker in haystack for marker in ("sso", "idp", "sign in", "sign-in", "login", "auth", "institution selection", "institution login", "openathens", "shibboleth")):
        return "auth_required"
    if "candidate_conflict" in haystack or "supplement" in haystack:
        return "pdf_candidate_conflict"
    if "unsupported" in haystack:
        return "unsupported_publisher"
    return "capture_failed"


def manifest_next_action(status: str, entry: PublisherMatrixEntry | None = None) -> str:
    if status == "success":
        return "none"
    if status == "unverified":
        return "inspect_pdf_text_or_manual_verify"
    if status == "auth_required":
        return "complete_institution_login_in_visible_browser_then_retry"
    if status == "human_verification_required":
        return "complete_visible_human_verification_then_retry"
    if status == "waf_blocked":
        return "stop_batch_and_retry_later_or_use_manual_browser"
    if status == "access_unavailable":
        return "check_access_in_regular_browser_or_library_subscription"
    if status == "publisher_error":
        return "retry_later_or_test_another_doi"
    if status == "pdf_candidate_conflict":
        return "rerun_in_diagnose_mode_and_inspect_pdf_candidates"
    if status == "browser_group_pending":
        return "split_doi_list_by_publisher_then_rerun"
    if status == "unsupported_publisher":
        return "add_or_update_publisher_profile_before_retry"
    if status == "ambiguous_search_result":
        return "inspect_visible_search_results_and_select_manually"
    if status == "daily_limit_reached":
        return "review_configured_download_policy_or_resume_next_local_day"
    if status == "quota_state_error":
        return "inspect_or_repair_local_quota_state_before_retry"
    if entry and entry.prewarm:
        return "run_single_doi_prewarm_with_same_browser_profile"
    return "rerun_with_mode_diagnose"


def normalize_suggested_paths(value: Any) -> list[str]:
    """Normalize a manifest suggested_paths value into a list of path ids."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return normalize_suggested_paths(parsed)
        return list(dict.fromkeys(part.strip() for part in re.split(r"[|,;]", text) if part.strip()))
    return []


def manifest_suggested_paths(status: str, entry: PublisherMatrixEntry | None = None) -> list[str]:
    """Return structured workflow options for a normalized manifest status."""
    paths = list(STATUS_SUGGESTED_PATHS.get(status) or ["rerun_diagnose"])
    if entry and entry.prewarm and status not in TERMINAL_BLOCKER_STATES and "single_doi_prewarm" not in paths:
        paths.insert(0, "single_doi_prewarm")
    return list(dict.fromkeys(paths))


def manifest_workflow(status: str, entry: PublisherMatrixEntry | None = None) -> dict[str, Any]:
    """Return the user-facing next action plus machine-readable workflow paths."""
    return {
        "next_action": manifest_next_action(status, entry),
        "suggested_paths": manifest_suggested_paths(status, entry),
    }


def entry_to_json(entry: PublisherMatrixEntry) -> dict[str, Any]:
    return {
        "key": entry.key,
        "status": entry.status,
        "batch_policy": entry.batch_policy,
        "prewarm": entry.prewarm,
        "known_blocker": entry.known_blocker,
        "note": entry.note,
    }
