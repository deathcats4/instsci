"""CLI interface for InstSci."""

import csv
import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import typer
from rich.console import Console
from rich.table import Table

from .config import Config
from .fetcher import PaperFetcher
from . import multi_search
from .publisher_matrix import manifest_next_action, manifest_suggested_paths, normalize_suggested_paths
from .search_pipeline import (
    build_search_payload,
    load_search_payload,
    parse_selection_indices,
    write_search_payload,
    write_selection,
)
from .schools import get_school, list_schools, search_schools

app = typer.Typer(
    name="instsci",
    help="Fetch academic papers via institutional access, Open Access, or arXiv.",
    no_args_is_help=True,
)
jobs_app = typer.Typer(help="Manage long-running InstSci browser jobs.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")
zotero_app = typer.Typer(help="Prepare Zotero MCP import handoffs.", no_args_is_help=True)
app.add_typer(zotero_app, name="zotero")
evidence_app = typer.Typer(help="Manage the external private-evidence index.", no_args_is_help=True)
app.add_typer(evidence_app, name="evidence")
console = Console()


RUN_MODES = {"user", "diagnose", "dev"}
DEFAULT_RUN_MODE = "user"
STANDARD_STATUSES = {
    "success",
    "auth_required",
    "human_verification_required",
    "waf_blocked",
    "access_unavailable",
    "publisher_error",
    "pdf_candidate_conflict",
    "capture_failed",
    "browser_group_pending",
    "unsupported_publisher",
}
FILE_STATUSES = {"success", "unverified", "missing"}
RESULT_EVIDENCE_VALUES = {
    "oa_direct",
    "publisher_open_pdf",
    "browser_verified",
    "http_preflight",
    "not_verified",
}


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _ensure_email(config: Config):
    """Prompt user to set email if not configured (needed for Unpaywall)."""
    if not config.email:
        console.print("[yellow]Email not configured (needed for Unpaywall OA detection).[/yellow]")
        email = typer.prompt("Enter your email address")
        config.email = email
        config.save()
        console.print(f"[green]Email saved: {email}[/green]")


def _school_type_label(school_type: str) -> str:
    return {
        "webvpn": "CampusPortal",
        "easyconnect": "CampusConnector",
        "atrust": "CampusConnector",
        "ezproxy": "LibraryPortal",
    }.get(school_type, school_type)


def _apply_school_config(cfg: Config, school: str):
    entry = get_school(school)
    cfg.school = entry.name
    if entry.school_type == "ezproxy":
        cfg.ezproxy_base_url = entry.host
        cfg.webvpn_base_url = ""
    else:
        cfg.webvpn_base_url = entry.host
        cfg.ezproxy_base_url = ""
    return entry


def _access_url(cfg: Config) -> str:
    return cfg.ezproxy_base_url or cfg.webvpn_base_url


def _mask_secret(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def _status_label(status: str) -> str:
    if status == "pass":
        return "[green]pass[/green]"
    if status == "warn":
        return "[yellow]warn[/yellow]"
    return "[red]fail[/red]"


def _configured_subscription_institution(cfg: Config) -> str:
    """Return the configured subscription institution search text, if any."""
    return (
        cfg.carsi_idp_name
        or cfg.institution_name_en
        or cfg.institution_name_zh
        or cfg.school
        or ""
    ).strip()


def _configured_institution_aliases(cfg: Config, primary: str = "") -> tuple[str, ...]:
    values = [
        primary,
        cfg.carsi_idp_name,
        cfg.institution_name_en,
        cfg.institution_name_zh,
        cfg.school,
    ]
    return tuple(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def _resolve_subscription_institution(
    cfg: Config,
    institution: str,
    *,
    prompt: bool = True,
) -> str:
    """Resolve institution text without hard-coding any school as the default."""
    explicit = institution.strip()
    if explicit:
        cfg.carsi_enabled = True
        cfg.carsi_idp_name = explicit
        if explicit.isascii():
            cfg.institution_name_en = explicit
        else:
            cfg.institution_name_zh = explicit
        cfg.save()
        return explicit

    configured = _configured_subscription_institution(cfg)
    if configured:
        return configured

    if not prompt:
        console.print(
            "[red]Subscription institution is required.[/red] "
            "Pass --institution or run: instsci setup --school \"Your Institution\""
        )
        raise typer.Exit(1)

    console.print(
        "[yellow]Subscription institution is required for closed-access publisher PDFs.[/yellow]"
    )
    console.print(
        "[dim]Use the institution that owns your subscription, e.g. the name shown in "
        "OpenAthens/Shibboleth/CARSI login pages.[/dim]"
    )
    value = typer.prompt("Subscription institution").strip()
    if not value:
        console.print("[red]Subscription institution cannot be empty.[/red]")
        raise typer.Exit(1)
    english_name = typer.prompt("Institution English name (optional)", default="", show_default=False).strip()
    local_name = typer.prompt("Institution Chinese/local name (optional)", default="", show_default=False).strip()

    cfg.carsi_enabled = True
    cfg.carsi_idp_name = english_name or local_name or value
    cfg.institution_name_en = english_name or (value if value.isascii() else cfg.institution_name_en)
    cfg.institution_name_zh = local_name or (value if not value.isascii() else cfg.institution_name_zh)
    cfg.save()
    return cfg.carsi_idp_name


def _read_paper_records(file: Path):
    from .publisher_batch import PaperRecord

    records = []
    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if line and not line.startswith("#"):
            records.append(PaperRecord(doi=line))
    return records


def _record_payload(records) -> list[dict[str, str]]:
    return [
        {"doi": record.doi, "title": record.title, "published": record.published, "url": record.url}
        for record in records
    ]


def _write_papers_manifest(run_dir: Path, manifest: list[dict[str, object]]) -> dict[str, object]:
    """Write the common papers manifest format used by OA and browser routes."""
    complete_dir = run_dir / "complete"
    pdf_dir = complete_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    manifest = [_normalize_manifest_row(row) for row in manifest]
    manifest_json = complete_dir / "manifest.json"
    manifest_csv = complete_dir / "manifest.csv"
    manifest_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if manifest:
        fieldnames = list(manifest[0].keys())
        for row in manifest[1:]:
            for key in row.keys():
                if key not in fieldnames:
                    fieldnames.append(key)
        with manifest_csv.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(manifest)
    else:
        manifest_csv.write_text("", encoding="utf-8-sig")
    return {
        "count": len(manifest),
        "success": sum(1 for item in manifest if item.get("status") == "success"),
        "missing": sum(1 for item in manifest if item.get("status") == "missing"),
        "unverified": sum(1 for item in manifest if item.get("status") == "unverified"),
        "verified_match": sum(1 for item in manifest if item.get("verified_match")),
        "standard_status_counts": _count_by_manifest_field(manifest, "standard_status"),
        "result_evidence_counts": _count_by_manifest_field(manifest, "result_evidence"),
        "pdf_dir": str(pdf_dir),
        "manifest": str(manifest_csv),
    }


def _normalize_manifest_row(row: dict[str, object]) -> dict[str, object]:
    normalized = dict(row)
    file_status = str(normalized.get("file_status") or normalized.get("status") or "missing")
    if file_status not in FILE_STATUSES:
        file_status = "missing"
    standard_status = str(normalized.get("standard_status") or "")
    if standard_status == "captcha_or_waf":
        standard_status = "human_verification_required"
    if standard_status not in STANDARD_STATUSES:
        standard_status = "success" if file_status == "success" else "capture_failed"
    evidence = str(normalized.get("result_evidence") or "not_verified")
    if evidence not in RESULT_EVIDENCE_VALUES:
        evidence = "not_verified"
    normalized["status"] = file_status
    normalized["file_status"] = file_status
    normalized["standard_status"] = standard_status
    normalized["result_evidence"] = evidence
    normalized.setdefault("next_action", manifest_next_action(standard_status))
    suggested_paths = normalize_suggested_paths(normalized.get("suggested_paths"))
    normalized["suggested_paths"] = suggested_paths or manifest_suggested_paths(standard_status)
    normalized.setdefault("verified_match", file_status == "success")
    return normalized


def _build_workflow_plan(
    manifest_path: Path,
    rows: list[dict[str, object]],
    *,
    include_success: bool = False,
) -> dict[str, object]:
    """Build a low-noise workflow plan from normalized manifest rows."""
    normalized_rows = [_normalize_manifest_row(row) for row in rows]
    items: list[dict[str, object]] = []
    status_counts: dict[str, int] = {}
    suggested_path_counts: dict[str, int] = {}
    for index, row in enumerate(normalized_rows, 1):
        status = str(row.get("standard_status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        paths = normalize_suggested_paths(row.get("suggested_paths")) or manifest_suggested_paths(status)
        for path in paths:
            suggested_path_counts[path] = suggested_path_counts.get(path, 0) + 1
        if status == "success" and not include_success:
            continue
        items.append(
            {
                "index": index,
                "doi": row.get("doi") or "",
                "title": row.get("title") or "",
                "file_status": row.get("file_status") or row.get("status") or "",
                "standard_status": status,
                "result_evidence": row.get("result_evidence") or "",
                "next_action": row.get("next_action") or manifest_next_action(status),
                "suggested_paths": paths,
                "pdf_path": row.get("pdf_path") or "",
                "diagnostic_path": row.get("diagnostic_path") or "",
            }
        )
    return {
        "schema": "instsci.workflow_plan.v1",
        "schema_version": 1,
        "manifest": str(manifest_path),
        "include_success": include_success,
        "summary": {
            "rows": len(normalized_rows),
            "items": len(items),
            "status_counts": status_counts,
            "suggested_path_counts": suggested_path_counts,
        },
        "items": items,
    }


def _print_publisher_matrix_report(report: dict[str, object]) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    console.print("[bold]Publisher capability matrix:[/bold] browser-workflow planning, not a fresh access verdict.")
    console.print(
        "[dim]"
        f"Entries: {summary.get('entries', 0)} | "
        f"ready={summary.get('ready', 0)} | "
        f"prewarm={summary.get('prewarm_required', 0)} | "
        f"waf-risk={summary.get('waf_risky', 0)} | "
        f"route-not-published={summary.get('route_not_published', 0)} | "
        f"unclassified={summary.get('unclassified', 0)} | "
        f"batch-ok={summary.get('batch_ok', 0)} | "
        f"guarded={summary.get('batch_guarded', 0)}"
        "[/dim]"
    )

    table = Table(title="Publisher Matrix")
    table.add_column("Publisher", width=18)
    table.add_column("State", width=22)
    table.add_column("Batch", width=28)
    table.add_column("Blocker", width=18)
    table.add_column("Next Paths", overflow="fold")
    table.add_column("Note", overflow="fold")
    for item in report.get("items") or []:
        if not isinstance(item, dict):
            continue
        table.add_row(
            str(item.get("publisher") or ""),
            str(item.get("status") or ""),
            str(item.get("batch_recommendation") or ""),
            str(item.get("known_blocker") or ""),
            ", ".join(str(path) for path in item.get("suggested_paths") or []),
            str(item.get("note") or ""),
        )
    console.print(table)


def _count_by_manifest_field(manifest: list[dict[str, object]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in manifest:
        value = str(item.get(field) or "")
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _manifest_rows_from_records(records) -> list[dict[str, object]]:
    return [
        {
            "doi": record.doi,
            "published": record.published,
            "title": record.title,
            "status": "missing",
            "file_status": "missing",
            "standard_status": "capture_failed",
            "result_evidence": "not_verified",
            "reason": "not_attempted",
            "pdf_path": "",
            "pdf_url": "",
            "diagnostic_path": "",
            "next_action": "rerun_with_mode_diagnose",
            "size_bytes": 0,
            "text_length": 0,
            "verified_match": False,
        }
        for record in records
    ]


def _publisher_key_for_record(record) -> str:
    from .publisher_profiles import get_publisher_profile, infer_publisher_profile, list_publisher_profiles

    profile = infer_publisher_profile(record.doi)
    if profile is None:
        return "unknown"
    for key in list_publisher_profiles():
        if get_publisher_profile(key) is profile:
            return key
    return profile.name.lower().replace(" ", "-")


def _write_browser_group_files(run_dir: Path, records) -> dict[str, dict[str, object]]:
    groups: dict[str, list] = {}
    for record in records:
        groups.setdefault(_publisher_key_for_record(record), []).append(record)

    group_dir = run_dir / "browser_groups"
    group_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, dict[str, object]] = {}
    for publisher, grouped_records in sorted(groups.items()):
        path = group_dir / f"{publisher}_dois.txt"
        path.write_text(
            "\n".join(record.doi for record in grouped_records) + "\n",
            encoding="utf-8",
        )
        written[publisher] = {
            "count": len(grouped_records),
            "path": str(path),
        }
    return written


def _pending_browser_rows_from_records(
    records,
    *,
    group_files: dict[str, dict[str, object]] | None = None,
    reason: str = "browser_workflow_not_attempted",
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in records:
        publisher = _publisher_key_for_record(record)
        group_file = (group_files or {}).get(publisher) or {}
        rows.append(
            {
                "doi": record.doi,
                "published": record.published,
                "title": record.title,
                "publisher": publisher,
                "status": "missing",
                "file_status": "missing",
                "standard_status": "browser_group_pending",
                "result_evidence": "not_verified",
                "reason": reason,
                "pdf_path": "",
                "pdf_url": "",
                "diagnostic_path": str(group_file.get("path") or ""),
                "next_action": "split_doi_list_by_publisher_then_rerun",
                "suggested_paths": [
                    "publisher_doctor_matrix",
                    "split_by_publisher",
                    "rerun_by_publisher",
                    "workflow_plan",
                ],
                "size_bytes": 0,
                "text_length": 0,
                "verified_match": False,
            }
        )
    return rows


def _write_pending_browser_manifest(
    run_dir: Path,
    *,
    oa_rows: list[dict[str, object]],
    browser_records,
    mode: str,
    oa_first: bool,
    reason: str,
) -> dict[str, object]:
    group_files = _write_browser_group_files(run_dir, browser_records)
    pending_rows = _pending_browser_rows_from_records(
        browser_records,
        group_files=group_files,
        reason=reason,
    )
    summary = _write_papers_manifest(run_dir, _merge_manifest_rows(oa_rows, pending_rows))
    summary["run_mode"] = _normalize_run_mode(mode)
    summary["publisher_matrix"] = None
    summary["oa_first"] = bool(oa_first)
    summary["browser_queue_status"] = reason
    summary["browser_group_files"] = group_files
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _write_unresolved_browser_manifest(
    run_dir: Path,
    *,
    oa_rows: list[dict[str, object]],
    browser_records,
    mode: str,
    oa_first: bool,
    publisher_matrix: dict[str, object] | None = None,
    reason: str = "browser_workflow_no_manifest",
) -> dict[str, object]:
    pending_rows = _manifest_rows_from_records(browser_records)
    for row in pending_rows:
        row["reason"] = reason
        row["next_action"] = "rerun_with_mode_diagnose"
    summary = _write_papers_manifest(run_dir, _merge_manifest_rows(oa_rows, pending_rows))
    summary["run_mode"] = _normalize_run_mode(mode)
    summary["publisher_matrix"] = publisher_matrix
    summary["oa_first"] = bool(oa_first)
    summary["browser_queue_status"] = reason
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _write_browser_exception_manifest(
    run_dir: Path,
    *,
    oa_rows: list[dict[str, object]],
    browser_records,
    mode: str,
    oa_first: bool,
    publisher_matrix: dict[str, object] | None = None,
    reason: str,
    exc: BaseException,
) -> dict[str, object]:
    diagnostics_dir = run_dir / "diagnostics" / "browser_exception"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    diagnostic_path = diagnostics_dir / "diagnostic.json"
    diagnostic_path.write_text(
        json.dumps(
            {
                "schema": "instsci.browser_exception.v1",
                "schema_version": 1,
                "reason": reason,
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": traceback.format_exc(),
                "created_at": datetime.now().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    summary = _write_unresolved_browser_manifest(
        run_dir,
        oa_rows=oa_rows,
        browser_records=browser_records,
        mode=mode,
        oa_first=oa_first,
        publisher_matrix=publisher_matrix,
        reason=reason,
    )
    rows = _load_manifest_rows(summary)
    patched_rows = []
    for row in rows:
        if str(row.get("standard_status") or "") != "success":
            row["diagnostic_path"] = str(diagnostic_path)
        patched_rows.append(row)
    summary.update(_write_papers_manifest(run_dir, patched_rows))
    summary["run_mode"] = _normalize_run_mode(mode)
    summary["publisher_matrix"] = publisher_matrix
    summary["oa_first"] = bool(oa_first)
    summary["browser_queue_status"] = reason
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _write_unsupported_publisher_manifest(
    run_dir: Path,
    *,
    oa_rows: list[dict[str, object]],
    browser_records,
    mode: str,
    oa_first: bool,
    reason: str = "publisher_auto_inference_failed",
) -> dict[str, object]:
    rows = _manifest_rows_from_records(browser_records)
    for row in rows:
        row["publisher"] = "unknown"
        row["standard_status"] = "unsupported_publisher"
        row["reason"] = reason
        row["next_action"] = manifest_next_action("unsupported_publisher")
        row["suggested_paths"] = manifest_suggested_paths("unsupported_publisher")
    summary = _write_papers_manifest(run_dir, _merge_manifest_rows(oa_rows, rows))
    summary["run_mode"] = _normalize_run_mode(mode)
    summary["publisher_matrix"] = None
    summary["oa_first"] = bool(oa_first)
    summary["browser_queue_status"] = reason
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _merge_manifest_rows(*row_groups: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    for rows in row_groups:
        merged.extend(rows or [])
    return merged


def _load_manifest_rows(summary: dict | None) -> list[dict[str, object]]:
    if not summary:
        return []
    manifest_value = str(summary.get("manifest") or "").strip()
    if not manifest_value:
        return []
    manifest_path = Path(manifest_value)
    manifest_json = manifest_path.with_suffix(".json")
    if not manifest_json.exists():
        return []
    try:
        data = json.loads(manifest_json.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _papers_oa_first(
    *,
    records,
    cfg: Config,
    run_dir: Path,
    use_cache: bool = True,
) -> tuple[list[dict[str, object]], list]:
    """Download OA records before visible browser work; return manifest rows and leftovers."""
    from .extractors import pdf_extractor
    from .pdf_bytes import is_plausible_pdf_bytes

    oa_dir = run_dir / "oa_first"
    pdf_dir = run_dir / "complete" / "pdfs"
    oa_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    previous_output_dir = cfg.output_dir
    cfg.output_dir = str(oa_dir / "pdfs")
    fetcher = PaperFetcher(cfg)
    oa_rows: list[dict[str, object]] = []
    remaining = []
    try:
        for record in records:
            result = fetcher.fetch_oa_only(record.doi, use_cache=use_cache)
            paper = result.paper
            source = (paper.source or "").lower()
            pdf_path = Path(paper.pdf_path) if paper.pdf_path else None
            is_open_source = source in {"open_access", "arxiv", "publisher_open_pdf"}
            if (
                result.status == "success"
                and is_open_source
                and pdf_path
                and pdf_path.exists()
            ):
                pdf_bytes = pdf_path.read_bytes()
                if is_plausible_pdf_bytes(pdf_bytes):
                    dst = pdf_dir / f"{record.doi.replace('/', '_').replace(':', '_')}.pdf"
                    dst.write_bytes(pdf_bytes)
                    text = paper.full_text or pdf_extractor.extract_text(dst)
                    evidence = "oa_direct" if source in {"open_access", "arxiv"} else "publisher_open_pdf"
                    oa_rows.append(
                        {
                            "doi": record.doi,
                            "published": record.published or str(paper.year or ""),
                            "title": record.title or paper.title,
                            "status": "success",
                            "file_status": "success",
                            "standard_status": "success",
                            "result_evidence": evidence,
                            "reason": "",
                            "pdf_path": str(dst),
                            "pdf_url": paper.url,
                            "diagnostic_path": "",
                            "next_action": "none",
                            "size_bytes": dst.stat().st_size,
                            "text_length": len(text or ""),
                            "verified_match": True,
                        }
                    )
                    continue
            remaining.append(record)
    finally:
        fetcher.close()
        cfg.output_dir = previous_output_dir
    return oa_rows, remaining


def _resolve_papers_profile(records, publisher: str):
    from .publisher_profiles import get_publisher_profile, infer_publisher_profile, list_publisher_profiles

    if publisher.strip().lower() == "auto":
        inferred = [infer_publisher_profile(record.doi) for record in records]
        profiles = {profile for profile in inferred if profile is not None}
        if len(profiles) != 1 or len(profiles) != len(set(inferred)):
            console.print("[red]Could not infer one publisher for all DOIs.[/red]")
            console.print(f"[yellow]Use --publisher with one of: {', '.join(list_publisher_profiles())}.[/yellow]")
            raise typer.Exit(1)
        return profiles.pop()

    try:
        return get_publisher_profile(publisher)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _broker_key_for_profile(profile, publisher: str) -> str:
    return profile.name.lower().replace(" ", "-")


def _ensure_session_broker(
    *,
    broker_publisher: str,
    cfg: Config,
    institution: str,
    broker_ttl: int,
) -> bool:
    import importlib

    session_broker = importlib.import_module("instsci.session_broker")

    state = session_broker.load_broker_state(broker_publisher)
    if state and session_broker.broker_is_running(broker_publisher):
        broker_profile = str(state.get("profile_dir") or "")
        if broker_profile and not _same_path(broker_profile, cfg.chrome_profile_dir):
            console.print(
                "[yellow]Existing session broker uses a different browser profile; restarting broker for this run.[/yellow]"
            )
            session_broker.broker_stop_path(broker_publisher).parent.mkdir(parents=True, exist_ok=True)
            session_broker.broker_stop_path(broker_publisher).write_text("stop", encoding="utf-8")
            deadline = time.time() + 30
            while time.time() < deadline and session_broker.broker_is_running(broker_publisher):
                time.sleep(1)

    if not session_broker.broker_is_running(broker_publisher):
        console.print(f"[dim]Starting publisher session broker: {broker_publisher}[/dim]")
        session_broker.start_broker_process(
            publisher=broker_publisher,
            browser_profile=cfg.chrome_profile_dir,
            institution=institution,
            ttl_seconds=broker_ttl,
            cwd=Path.cwd(),
        )
        deadline = time.time() + 30
        while time.time() < deadline and not session_broker.broker_is_running(broker_publisher):
            time.sleep(1)
    return session_broker.broker_is_running(broker_publisher)


def _enqueue_papers_job(
    *,
    profile,
    broker_publisher: str,
    records,
    run_dir: Path,
    cfg: Config,
    institution: str,
    institution_aliases: tuple[str, ...],
    login_timeout: int,
    pdf_timeout: int,
    post_login_hold: int,
    post_run_hold: int,
    carsi_portal_preauth: bool,
    pause_on_blocker: bool,
    command: str,
    parent_job_id: str = "",
) -> dict:
    from . import job_store, session_broker

    broker_job = session_broker.enqueue_broker_job(
        publisher=broker_publisher,
        records=_record_payload(records),
        output_dir=str(run_dir),
        institution=institution,
        institution_aliases=list(institution_aliases),
        login_timeout=login_timeout,
        pdf_timeout=pdf_timeout,
        post_login_hold=post_login_hold,
        post_run_hold=post_run_hold,
        carsi_portal_preauth=carsi_portal_preauth,
        pause_on_blocker=pause_on_blocker,
    )
    return job_store.create_job(
        publisher=profile.name,
        broker_publisher=broker_publisher,
        records=_record_payload(records),
        output_dir=str(run_dir),
        institution=institution,
        institution_aliases=list(institution_aliases),
        browser_profile=cfg.chrome_profile_dir,
        broker_job=broker_job,
        command=command,
        login_timeout=login_timeout,
        pdf_timeout=pdf_timeout,
        post_login_hold=post_login_hold,
        post_run_hold=post_run_hold,
        carsi_portal_preauth=carsi_portal_preauth,
        pause_on_blocker=pause_on_blocker,
        parent_job_id=parent_job_id,
    )


def _print_job_submitted(job: dict) -> None:
    console.print(f"[green]Job submitted:[/green] {job['id']}")
    console.print(f"[dim]Status:[/dim] instsci jobs status {job['id']}")
    console.print(f"[dim]Tail:[/dim] instsci jobs tail {job['id']}")
    console.print(f"[dim]Output:[/dim] {job['output_dir']}")


def _print_jobs_table(jobs: list[dict]) -> None:
    table = Table(title="InstSci Jobs")
    table.add_column("Job")
    table.add_column("Status")
    table.add_column("Publisher")
    table.add_column("Records")
    table.add_column("Output", overflow="fold")
    for job in jobs:
        table.add_row(
            str(job.get("id", "")),
            str(job.get("status", "")),
            str(job.get("publisher", "")),
            str(job.get("record_count") or len(job.get("records") or [])),
            str(job.get("output_dir", "")),
        )
    console.print(table)


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
    except Exception:
        return str(left).strip().lower() == str(right).strip().lower()


def _browser_doctor_dir(run_dir: Path, stage: str) -> Path:
    return run_dir / "diagnostics" / f"browser_{stage}"


def _normalize_run_mode(mode: str = "") -> str:
    normalized = (mode or os.environ.get("INSTSCI_MODE") or DEFAULT_RUN_MODE).strip().lower()
    aliases = {
        "": DEFAULT_RUN_MODE,
        "normal": "user",
        "default": "user",
        "light": "user",
        "diagnostic": "diagnose",
        "debug": "dev",
        "developer": "dev",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in RUN_MODES:
        console.print("[red]Invalid --mode value.[/red] Use: user, diagnose, or dev.")
        raise typer.Exit(1)
    return normalized


def _mode_defaults(mode: str) -> dict[str, object]:
    mode = _normalize_run_mode(mode)
    if mode == "dev":
        return {
            "browser_doctor_gate": True,
            "preflight_screenshots": True,
            "after_run_on_success": True,
            "watch_browser": "notify",
            "pause_on_blocker": True,
            "verbose_summary": True,
        }
    if mode == "diagnose":
        return {
            "browser_doctor_gate": True,
            "preflight_screenshots": False,
            "after_run_on_success": True,
            "watch_browser": "notify",
            "pause_on_blocker": True,
            "verbose_summary": True,
        }
    return {
        "browser_doctor_gate": True,
        "preflight_screenshots": False,
        "after_run_on_success": False,
        "watch_browser": "quiet",
        "pause_on_blocker": True,
        "verbose_summary": False,
    }


def _has_cli_option(option: str) -> bool:
    return any(arg == option or arg.startswith(f"{option}=") for arg in sys.argv[1:])


def _resolve_browser_runtime_options(
    *,
    mode: str,
    browser_doctor_gate: bool,
    watch_browser: str,
    pause_on_blocker: bool,
) -> dict[str, object]:
    mode = _normalize_run_mode(mode)
    defaults = _mode_defaults(mode)
    explicit_browser_doctor = _has_cli_option("--browser-doctor")
    resolved_watch = watch_browser
    if not _has_cli_option("--watch-browser"):
        resolved_watch = str(defaults["watch_browser"])
    resolved_pause = pause_on_blocker
    if not _has_cli_option("--pause-on-blocker") and not _has_cli_option("--no-pause-on-blocker"):
        resolved_pause = bool(defaults["pause_on_blocker"])
    resolved_doctor = browser_doctor_gate
    if not _has_cli_option("--browser-doctor") and not _has_cli_option("--no-browser-doctor"):
        resolved_doctor = bool(defaults["browser_doctor_gate"])
    after_run_on_success = bool(defaults["after_run_on_success"])
    if explicit_browser_doctor and resolved_doctor:
        after_run_on_success = True
    return {
        "mode": mode,
        "browser_doctor_gate": resolved_doctor,
        "watch_browser": resolved_watch,
        "pause_on_blocker": resolved_pause,
        "preflight_screenshots": bool(defaults["preflight_screenshots"]),
        "after_run_on_success": after_run_on_success,
        "verbose_summary": bool(defaults["verbose_summary"]),
    }


def _summary_needs_attention(summary: dict) -> bool:
    return bool(summary.get("missing") or summary.get("unverified") or summary.get("failed"))


def _persist_cli_summary_metadata(run_dir: Path, summary: dict) -> None:
    path = run_dir / "summary.json"
    if not path.exists():
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        existing.update({
            key: value
            for key, value in summary.items()
            if key in {"run_mode", "publisher_matrix", "oa_first"}
        })
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def _run_after_run_doctor_if_needed(
    *,
    publisher: str,
    run_dir: Path,
    enabled: bool,
    summary: dict | None,
    mode: str,
    browser_profile: str = "",
    after_run_on_success: bool | None = None,
) -> None:
    if not enabled:
        return
    needs_attention = summary is None or _summary_needs_attention(summary)
    defaults = _mode_defaults(mode)
    success_doctor = bool(defaults["after_run_on_success"]) if after_run_on_success is None else bool(after_run_on_success)
    if not needs_attention and not success_doctor:
        return
    _run_browser_doctor_checkpoint(
        publisher=publisher,
        run_dir=run_dir,
        stage="after_run",
        enabled=True,
        capture_screenshots=needs_attention or mode in {"diagnose", "dev"},
        focus_windows=needs_attention and mode in {"diagnose", "dev"},
        browser_profile=browser_profile,
        print_result=True,
    )


def _print_download_summary(summary: dict, *, verbose: bool = False) -> None:
    console.print(
        f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
        f"{summary.get('unverified', 0)} unverified PDFs."
    )
    if summary.get("standard_status_counts"):
        status_bits = ", ".join(
            f"{key}={value}" for key, value in sorted(summary["standard_status_counts"].items())
        )
        console.print(f"[dim]Standard status: {status_bits}[/dim]")
    console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
    if verbose:
        console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
        if summary.get("attempt_cache"):
            console.print(f"[dim]Attempt cache: {summary['attempt_cache']}[/dim]")
        if summary.get("publisher_matrix"):
            console.print(f"[dim]Publisher matrix: {summary['publisher_matrix']}[/dim]")


def _enforce_publisher_matrix(
    *,
    publisher: str,
    count: int,
    force: bool,
) -> object:
    from .publisher_matrix import entry_to_json, get_publisher_matrix_entry, matrix_skip_reason

    entry = get_publisher_matrix_entry(publisher)
    reason = matrix_skip_reason(entry, count=count, force=force)
    if reason:
        console.print(f"[yellow]Publisher matrix stopped this batch:[/yellow] {reason}")
        console.print("[dim]Use one DOI prewarm first, or pass --force if you intentionally want to test it.[/dim]")
        raise typer.Exit(3)
    if entry.status != "ready" or entry.prewarm:
        console.print(f"[dim]Publisher matrix: {entry.status}; {entry.note}[/dim]")
    return entry_to_json(entry)


def _run_browser_doctor_checkpoint(
    *,
    publisher: str,
    run_dir: Path,
    stage: str,
    enabled: bool,
    capture_screenshots: bool = True,
    focus_windows: bool = False,
    browser_profile: str = "",
    print_result: bool = True,
) -> dict | None:
    if not enabled:
        return None
    from .browser_doctor import inspect_cloakbrowser

    output_dir = _browser_doctor_dir(run_dir, stage)
    report = inspect_cloakbrowser(
        output_dir=output_dir,
        publisher=publisher,
        capture_screenshots=capture_screenshots,
        focus_windows=focus_windows,
        browser_profile=browser_profile,
    )
    report_path = Path(str(report.get("output_dir") or output_dir)) / "inspection.json"
    if print_result:
        console.print(
            f"[dim]Browser doctor {stage}: {report.get('state')} "
            f"({report_path})[/dim]"
        )
    return report


def _normalize_watch_mode(mode: str) -> str:
    normalized = (mode or "notify").strip().lower()
    aliases = {
        "off": "off",
        "none": "off",
        "false": "off",
        "0": "off",
        "quiet": "quiet",
        "silent": "quiet",
        "record": "quiet",
        "notify": "notify",
        "warn": "notify",
        "focus": "focus",
        "foreground": "focus",
        "strong": "focus",
    }
    if normalized not in aliases:
        console.print("[red]Invalid --watch-browser value.[/red] Use: off, quiet, notify, or focus.")
        raise typer.Exit(1)
    return aliases[normalized]


def _append_watch_event(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _start_browser_watchdog(
    *,
    publisher: str,
    run_dir: Path,
    mode: str,
    interval: float,
    browser_profile: str = "",
) -> tuple[threading.Event | None, threading.Thread | None]:
    mode = _normalize_watch_mode(mode)
    if mode == "off":
        return None, None

    from .browser_doctor import inspect_cloakbrowser

    watch_dir = run_dir / "diagnostics" / "browser_watch"
    latest_dir = watch_dir / "latest"
    events_path = watch_dir / "events.jsonl"
    stop_event = threading.Event()
    blocker_states = {
        "human_verification_required",
        "waf_blocked",
        "auth_required",
        "access_unavailable",
        "publisher_error",
    }
    poll_interval = max(2.0, float(interval or 5))
    startup_grace_seconds = 8.0

    def worker() -> None:
        last_state = ""
        last_url = ""
        started_at = time.time()
        while not stop_event.wait(poll_interval):
            try:
                report = inspect_cloakbrowser(
                    output_dir=latest_dir,
                    publisher=publisher,
                    capture_screenshots=False,
                    focus_windows=False,
                    browser_profile=browser_profile,
                )
                windows = list(report.get("windows") or [])
                matching_windows = [window for window in windows if bool(window.get("profile_match", True))]
                if browser_profile and windows and not matching_windows:
                    url = ""
                    state = "other_windows_present"
                else:
                    url = str(matching_windows[0].get("url") or "") if matching_windows else ""
                    state = str(report.get("state") or "")
                changed = state != last_state or url != last_url
                if not changed:
                    continue

                event = {
                    "generated_at": datetime.now().isoformat(),
                    "publisher": publisher,
                    "state": state,
                    "url": url,
                    "window_count": int(report.get("window_count") or 0),
                    "matching_window_count": len(matching_windows),
                    "recommendation": str(report.get("recommendation") or ""),
                }
                _append_watch_event(events_path, event)
                last_state = state
                last_url = url

                in_startup_grace = state in {"no_window", "blank"} and (time.time() - started_at) < startup_grace_seconds
                if mode in {"notify", "focus"} and state in blocker_states and not in_startup_grace:
                    console.print(
                        f"[yellow]Browser watch:[/yellow] {state}. "
                        f"{event['recommendation']} "
                        f"[dim]({latest_dir / 'inspection.json'})[/dim]"
                    )

                if mode == "focus" and state in blocker_states and not in_startup_grace:
                    focus_dir = watch_dir / f"focus_{datetime.now():%Y%m%d_%H%M%S}"
                    inspect_cloakbrowser(
                        output_dir=focus_dir,
                        publisher=publisher,
                        capture_screenshots=True,
                        focus_windows=True,
                        browser_profile=browser_profile,
                    )
                    console.print(f"[yellow]Browser watch focused CloakBrowser:[/yellow] {focus_dir}")
            except Exception as exc:
                _append_watch_event(
                    events_path,
                    {
                        "generated_at": datetime.now().isoformat(),
                        "publisher": publisher,
                        "state": "watch_error",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )

    thread = threading.Thread(target=worker, name="instsci-browser-watchdog", daemon=True)
    thread.start()
    console.print(f"[dim]Browser watch: {mode} every {poll_interval:g}s ({watch_dir})[/dim]")
    return stop_event, thread


def _stop_browser_watchdog(stop_event: threading.Event | None, thread: threading.Thread | None) -> None:
    if stop_event is None or thread is None:
        return
    stop_event.set()
    thread.join(timeout=5)


def _run_browser_preflight_gate(
    *,
    publisher: str,
    run_dir: Path,
    browser_profile: str,
    enabled: bool,
    allow_running_broker: bool = False,
    broker_publisher: str = "",
    capture_screenshots: bool = False,
    print_result: bool = True,
) -> dict | None:
    report = _run_browser_doctor_checkpoint(
        publisher=publisher,
        run_dir=run_dir,
        stage="preflight",
        enabled=enabled,
        capture_screenshots=capture_screenshots,
        browser_profile=browser_profile,
        print_result=print_result,
    )
    if not enabled or report is None:
        return report

    state = str(report.get("state") or "")
    windows = list(report.get("windows") or [])
    matching_windows = [
        window for window in windows
        if _same_path(str(window.get("profile_dir") or ""), browser_profile)
    ]

    broker_running = False
    if allow_running_broker and broker_publisher:
        try:
            from .session_broker import broker_is_running

            broker_running = broker_is_running(broker_publisher)
        except Exception:
            broker_running = False

    blocker_states = {
        "human_verification_required",
        "waf_blocked",
        "auth_required",
        "access_unavailable",
        "publisher_error",
    }
    block_reason = ""
    if matching_windows and state == "blank" and not broker_running:
        block_reason = "The selected browser profile is on about:blank; this usually indicates profile contention or a stalled launch."
    elif matching_windows and state in blocker_states:
        block_reason = f"The existing CloakBrowser window for this profile is in state: {state}."
    elif matching_windows and not broker_running:
        block_reason = "The selected browser profile is already open in CloakBrowser."

    if block_reason:
        report_path = Path(str(report.get("output_dir") or _browser_doctor_dir(run_dir, "preflight"))) / "inspection.json"
        console.print(f"[red]Browser preflight stopped this run:[/red] {block_reason}")
        console.print(f"[yellow]Inspect or resolve the visible browser state, then retry. Report: {report_path}[/yellow]")
        raise typer.Exit(3)

    if windows and not matching_windows:
        console.print("[yellow]Browser preflight found other CloakBrowser windows, but none use this run's profile.[/yellow]")
    return report


def _path_status(path_value: str) -> tuple[str, str]:
    if not path_value:
        return "missing", ""
    path = Path(path_value)
    return ("ok" if path.exists() else "missing", str(path))


def _show_setup_check(cfg: Config) -> bool:
    checks: list[tuple[str, str, str]] = []
    subscription_institution = _configured_subscription_institution(cfg)
    checks.append((
        "Subscription institution",
        "ok" if subscription_institution else "missing",
        subscription_institution or "set with --institution-en/--institution-cn or --federated-school",
    ))
    checks.append(("Campus school", "ok" if cfg.school else "optional", cfg.school or "optional; set with --school for campus gateways"))
    checks.append(("Access URL", "ok" if _access_url(cfg) else "optional", _access_url(cfg) or "optional; derived from --school"))
    federated_ready = (not cfg.carsi_enabled) or bool(subscription_institution)
    checks.append((
        "Federated login",
        "ok" if federated_ready else "missing",
        subscription_institution or ("disabled" if not cfg.carsi_enabled else "set with --federated-school"),
    ))
    aliases = ", ".join(_configured_institution_aliases(cfg))
    checks.append((
        "Institution names",
        "ok" if aliases else "missing",
        aliases or "set with --institution-en and/or --institution-cn",
    ))
    for label, path_value in [
        ("Output dir", cfg.output_dir),
        ("Cache dir", cfg.cache_dir),
        ("Chrome profile", cfg.chrome_profile_dir),
        ("Session dir", cfg.carsi_cookie_dir),
    ]:
        status, detail = _path_status(path_value)
        checks.append((label, status, detail))

    table = Table(title="InstSci Environment Check")
    table.add_column("Item", width=18)
    table.add_column("Status", width=10)
    table.add_column("Detail", overflow="fold")
    ready = True
    for label, status, detail in checks:
        if status == "missing":
            ready = False
        style = "green" if status == "ok" else ("cyan" if status == "optional" else "yellow")
        table.add_row(label, f"[{style}]{status}[/{style}]", detail)
    console.print(table)
    return ready


@app.command()
def setup(
    school: str = typer.Option("", "--school", help="Set institution by school name or partial match."),
    institution_cn: str = typer.Option("", "--institution-cn", "--school-cn", help="Set the institution's Chinese/local name for publisher login matching."),
    institution_en: str = typer.Option("", "--institution-en", "--school-en", help="Set the institution's English name for publisher login matching."),
    email: str = typer.Option("", "--email", help="Set email for Open Access metadata services."),
    output_dir: str = typer.Option("", "--output-dir", help="Set the default PDF output directory."),
    federated: bool = typer.Option(True, "--federated/--no-federated", help="Enable browser federated institutional login."),
    federated_school: str = typer.Option("", "--federated-school", help="Override the school name shown in publisher login pages."),
    check: bool = typer.Option(False, "--check", help="Check environment without changing configuration."),
):
    """One-step environment setup for institutional paper downloads."""
    cfg = Config.load()
    changed = False
    school_entry = None

    has_setter = any([school, institution_cn, institution_en, email, output_dir, federated_school]) or not federated
    if check and not has_setter:
        if not _show_setup_check(cfg):
            raise typer.Exit(2)
        return

    if school:
        try:
            school_entry = _apply_school_config(cfg, school)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        changed = True

    if email:
        cfg.email = email
        changed = True

    if institution_cn:
        cfg.institution_name_zh = institution_cn
        changed = True

    if institution_en:
        cfg.institution_name_en = institution_en
        changed = True

    if output_dir:
        cfg.output_dir = output_dir
        changed = True

    if federated and (school or federated_school or institution_en or institution_cn or cfg.carsi_idp_name or cfg.school):
        cfg.carsi_enabled = True
        if federated_school:
            cfg.carsi_idp_name = federated_school
        elif institution_en:
            cfg.carsi_idp_name = institution_en
        elif institution_cn:
            cfg.carsi_idp_name = institution_cn
        elif school_entry is not None:
            cfg.carsi_idp_name = school_entry.name
        elif cfg.school and not cfg.carsi_idp_name:
            cfg.carsi_idp_name = cfg.school
        changed = True
    elif not federated:
        cfg.carsi_enabled = False
        changed = True

    cfg.ensure_dirs()
    if changed:
        cfg.save()

    ready = bool(_configured_subscription_institution(cfg))
    if ready:
        console.print("[green]Environment ready.[/green]")
    else:
        console.print("[yellow]Environment prepared, but institution access is incomplete.[/yellow]")
    if school_entry is not None:
        type_label = _school_type_label(school_entry.school_type)
        console.print(f"  School:       {school_entry.name} ({type_label})")
        console.print(f"  Access URL:   {_access_url(cfg)}")
        if school_entry.school_type in {"easyconnect", "atrust"}:
            console.print("[yellow]This school needs a local campus connector before downloading.[/yellow]")
            console.print("  Set it with: [cyan]instsci config-cmd --connector-url socks5://127.0.0.1:1080[/cyan]")
    if cfg.institution_name_en:
        console.print(f"  Institution EN: {cfg.institution_name_en}")
    if cfg.institution_name_zh:
        console.print(f"  Institution CN: {cfg.institution_name_zh}")
    console.print(f"  Output dir:   {cfg.output_dir}")
    console.print(f"  Browser dir:  {cfg.chrome_profile_dir}")
    console.print(f"  Sessions dir: {cfg.carsi_cookie_dir}")
    console.print("[dim]Next: instsci papers dois.txt --publisher auto[/dim]")
    console.print("[dim]If SSO, 2FA, or CAPTCHA appears, complete it once in the opened browser window.[/dim]")

    if (check or not ready) and not _show_setup_check(cfg):
        raise typer.Exit(2)


@app.command()
def login(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login even if session is valid."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Initialize or refresh institutional access session."""
    _setup_logging(verbose)
    config = Config.load()
    fetcher = PaperFetcher(config)

    console.print("[bold]Checking institutional access session...[/bold]")
    if fetcher.auth.login(force=force):
        console.print("[green]Institutional access session is active.[/green]")
    else:
        console.print("[red]Failed to authenticate institutional access.[/red]")
        raise typer.Exit(1)


@app.command("chinese-literature-sites")
def chinese_literature_sites(
    site: str = typer.Option("", "--site", "-s", help="Filter by Chinese literature portal key or alias."),
    json_report: bool = typer.Option(False, "--json", help="Print JSON instead of a compact table."),
):
    """Show Chinese literature portal routing support and browser-readiness."""
    from .chinese_literature import (
        chinese_literature_portal_report,
        get_chinese_literature_portal,
    )

    report = chinese_literature_portal_report()
    if site:
        try:
            portal = get_chinese_literature_portal(site)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        report["portals"] = [portal.to_json()]
        report["summary"] = {
            "portals": 1,
            "capability_counts": {portal.capability: 1},
            "verified_portals": [portal.key] if portal.result_evidence == "browser_verified" else [],
        }

    if json_report:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return

    summary = report["summary"]
    console.print("[bold]Chinese literature portal support[/bold]")
    console.print(
        f"Portals: {summary['portals']} | "
        f"download-verified: {', '.join(summary['download_verified_portals']) or 'none'} | "
        f"route-verified: {', '.join(summary['route_verified_portals']) or 'none'}"
    )
    table = Table(title="Chinese Literature Sites")
    table.add_column("Site", overflow="fold")
    table.add_column("Capability", overflow="fold")
    table.add_column("Evidence", overflow="fold")
    table.add_column("Navigation", overflow="fold")
    table.add_column("Next Action", overflow="fold")
    for portal in report["portals"]:
        table.add_row(
            str(portal["key"]),
            str(portal["capability"]),
            str(portal["result_evidence"]),
            str(portal["default_navigation_mode"]),
            str(portal["next_action"]),
        )
    console.print(table)
    console.print("[dim]Only download-verified portals should be used for unattended batch PDF capture.[/dim]")


@app.command("cnki-login")
def cnki_login(
    url: str = typer.Option("https://www.cnki.net/", "--url", "-u", help="CNKI page to open in the persistent visible browser."),
    output: str = typer.Option("", "--output", "-o", help="Private run directory for the screenshot-backed session report."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Open or refresh the persistent visible CNKI session.

    Complete CAPTCHA, institution checks, or login manually. The full browser
    profile is reused on later runs; cookies are never exported by this command.
    """
    from .cnki_session import open_cnki_login_session, write_cnki_session_report

    _setup_logging(verbose)
    cfg = Config.load()
    cfg.ensure_dirs()
    run_dir = Path(output) if output else Path(cfg.output_dir).parent / "runs" / f"{datetime.now():%Y%m%d_%H%M%S}_cnki_login"
    context = None
    try:
        console.print("[bold]Opening persistent CNKI browser session...[/bold]")
        console.print(f"[dim]Profile: {cfg.cnki_profile_dir}[/dim]")
        context, page, run_dir = open_cnki_login_session(cfg, url=url, output_dir=run_dir)
        console.print("[yellow]Complete any CNKI CAPTCHA, institution check, or login in the visible browser.[/yellow]")
        typer.prompt("Press Enter here after the CNKI page is ready", default="", show_default=False)
        report = write_cnki_session_report(page, run_dir, cfg.cnki_profile_dir)
        status = str(report["session_status"])
        if status == "session_ready":
            console.print("[green]CNKI persistent session is ready.[/green]")
        elif status == "human_verification_required":
            console.print("[yellow]CNKI still requires visible human verification.[/yellow]")
        else:
            console.print("[yellow]CNKI browser ended on an unexpected page.[/yellow]")
        console.print(f"[dim]Report: {report['report']}[/dim]")
    finally:
        if context is not None:
            context.close()


@app.command("cnki-fetch")
def cnki_fetch(
    url: str = typer.Argument(help="CNKI article URL saved in Zotero or copied from the article page."),
    record_id: str = typer.Option("cnki_article", "--record-id", help="Stable local identifier used for the output PDF name."),
    output: str = typer.Option("", "--output", "-o", help="Private run directory for PDF and browser evidence."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Download one CNKI PDF through the persistent visible browser session."""
    from .cnki_session import (
        capture_cnki_pdf,
        classify_cnki_session,
        cnki_verification_visible,
        navigate_cnki_article,
        open_cnki_login_session,
        safe_page_url,
        settle_cnki_after_manual_step,
    )

    _setup_logging(verbose)
    cfg = Config.load()
    cfg.ensure_dirs()
    run_dir = Path(output) if output else Path(cfg.output_dir).parent / "runs" / f"{datetime.now():%Y%m%d_%H%M%S}_cnki_fetch"
    context = None
    report: dict[str, object] = {
        "publisher": "CNKI",
        "record_id": record_id,
        "route_attempted": "persistent_cloakbrowser_pdf_button",
        "institution": _configured_subscription_institution(cfg),
        "file_status": "missing",
        "standard_status": "capture_failed",
        "result_evidence": "browser_verified",
        "next_action": "inspect_visible_cnki_page",
    }
    try:
        context, page, run_dir = open_cnki_login_session(cfg, url="https://www.cnki.net/", output_dir=run_dir)
        navigation = navigate_cnki_article(page, url)
        report["navigation"] = navigation
        if cnki_verification_visible(page):
            console.print("[yellow]Complete the visible CNKI verification; InstSci will handle the PDF click.[/yellow]")
            typer.prompt("Press Enter here after verification", default="", show_default=False)
            settle = settle_cnki_after_manual_step(page, resume_url=url)
            report["manual_verification_settle"] = settle
        before = run_dir / "before_pdf_click.png"
        page.screenshot(path=str(before), full_page=False)
        result = capture_cnki_pdf(page, output_path=run_dir / "pdfs" / f"{record_id}.pdf")
        if result.get("verification_required"):
            console.print("[yellow]CNKI requested another verification before download. Complete it in the browser.[/yellow]")
            typer.prompt("Press Enter here after verification", default="", show_default=False)
            settle = settle_cnki_after_manual_step(page, resume_url=url)
            report["download_verification_settle"] = settle
            if not settle.get("verification_required"):
                result = capture_cnki_pdf(page, output_path=run_dir / "pdfs" / f"{record_id}.pdf")
        after = run_dir / "after_pdf_click.png"
        page.screenshot(path=str(after), full_page=False)
        valid = bool(result.get("pdf_header_valid")) and int(result.get("size_bytes") or 0) > 10_000
        report.update(result)
        report.update(
            {
                "article_url": safe_page_url(str(getattr(page, "url", "") or "")),
                "file_status": "success" if valid else "missing",
                "standard_status": "success" if valid else ("human_verification_required" if result.get("verification_required") else "capture_failed"),
                "evidence": str(after),
                "next_action": "verify_pdf_text" if valid else "inspect_visible_cnki_page",
            }
        )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "manifest.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    if report["file_status"] == "success":
        console.print(f"[green]CNKI PDF captured: {report['pdf_path']}[/green]")
        console.print(f"[dim]Manifest: {report_path}[/dim]")
        return
    console.print(f"[yellow]CNKI PDF was not captured ({report['standard_status']}).[/yellow]")
    console.print(f"[dim]Manifest: {report_path}[/dim]")
    raise typer.Exit(2)


@app.command("cnki-batch")
def cnki_batch(
    file: Path = typer.Argument(help="JSON array of CNKI records: record_id, title, url, and optional zotero_item_key."),
    output: str = typer.Option("", "--output", "-o", help="Private run directory for PDFs, screenshots, and manifests."),
    delay: float = typer.Option(30.0, "--delay", min=2.0, help="Seconds between article downloads."),
    verification_policy: str = typer.Option("stop", "--verification-policy", help="Human-verification policy: stop or prompt."),
    navigation_mode: str = typer.Option("search", "--navigation-mode", help="CNKI article navigation mode: search or direct."),
    skip_completed: bool = typer.Option(True, "--skip-completed/--no-skip-completed", help="When --output has a prior manifest, keep successful rows and skip them."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Download a small CNKI batch in one persistent visible browser session."""
    from .cnki_session import (
        capture_cnki_pdf,
        cnki_pdf_button_visible,
        classify_cnki_session,
        cnki_verification_visible,
        load_cnki_batch,
        navigate_cnki_article,
        navigate_cnki_article_via_search,
        open_cnki_login_session,
        safe_page_url,
        settle_cnki_after_manual_step,
    )
    from .extractors import pdf_extractor

    _setup_logging(verbose)
    try:
        records = load_cnki_batch(file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Invalid CNKI batch input: {exc}[/red]")
        raise typer.Exit(2)
    if not records:
        console.print("[yellow]CNKI batch is empty.[/yellow]")
        return
    verification_policy = verification_policy.strip().lower()
    if verification_policy not in {"stop", "prompt"}:
        console.print("[red]--verification-policy must be 'stop' or 'prompt'.[/red]")
        raise typer.Exit(2)
    navigation_mode = navigation_mode.strip().lower()
    if navigation_mode not in {"search", "direct"}:
        console.print("[red]--navigation-mode must be 'search' or 'direct'.[/red]")
        raise typer.Exit(2)

    cfg = Config.load()
    cfg.ensure_dirs()
    run_dir = Path(output) if output else Path(cfg.output_dir).parent / "runs" / f"{datetime.now():%Y%m%d_%H%M%S}_cnki_batch"
    pdf_dir = run_dir / "pdfs"
    evidence_dir = run_dir / "evidence"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    rows: list[dict[str, object]] = []
    context = None
    completed_ids: set[str] = set()
    if skip_completed and manifest_path.exists():
        try:
            previous_rows = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(previous_rows, list):
                previous_success: dict[str, dict[str, object]] = {}
                for previous in previous_rows:
                    if not isinstance(previous, dict):
                        continue
                    previous_id = str(previous.get("record_id") or "")
                    if previous_id and previous.get("file_status") == "success":
                        previous_success[previous_id] = previous
                for record in records:
                    previous = previous_success.get(record["record_id"])
                    if previous:
                        rows.append(previous)
                        completed_ids.add(record["record_id"])
                if completed_ids:
                    console.print(f"[green]Skipping {len(completed_ids)} previously verified CNKI PDFs from {manifest_path}.[/green]")
        except (OSError, json.JSONDecodeError) as exc:
            console.print(f"[yellow]Could not load prior CNKI manifest for resume: {exc}[/yellow]")

    def write_checkpoint() -> None:
        manifest_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "publisher": "CNKI",
            "total": len(records),
            "processed": len(rows),
            "success": sum(row.get("file_status") == "success" for row in rows),
            "unverified": sum(row.get("file_status") == "unverified" for row in rows),
            "missing": sum(row.get("file_status") == "missing" for row in rows),
            "result_evidence": "browser_verified",
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        context, page, _ = open_cnki_login_session(cfg, url="https://www.cnki.net/", output_dir=run_dir)
        for index, record in enumerate(records, 1):
            record_id = record["record_id"]
            if record_id in completed_ids:
                continue
            title = record["title"]
            item_evidence = evidence_dir / record_id
            item_evidence.mkdir(parents=True, exist_ok=True)
            row: dict[str, object] = {
                "publisher": "CNKI",
                "record_id": record_id,
                "title": title,
                "zotero_item_key": record["zotero_item_key"],
                "route_attempted": (
                    "persistent_cloakbrowser_search_pdf_button"
                    if navigation_mode == "search"
                    else "persistent_cloakbrowser_pdf_button"
                ),
                "institution": _configured_subscription_institution(cfg),
                "file_status": "missing",
                "standard_status": "capture_failed",
                "result_evidence": "browser_verified",
                "next_action": "inspect_visible_cnki_page",
            }
            console.print(f"[bold][{index}/{len(records)}][/bold] {title}")
            try:
                if navigation_mode == "search":
                    navigation = navigate_cnki_article_via_search(
                        page,
                        title=title,
                        fallback_url=record["url"],
                        record_id=record_id,
                    )
                else:
                    navigation = navigate_cnki_article(page, record["url"])
                row["navigation"] = navigation
                if (
                    navigation.get("session_status") == "human_verification_required"
                    or navigation.get("verification_required")
                    or classify_cnki_session(str(getattr(page, "url", "") or ""), "") == "human_verification_required"
                ):
                    if verification_policy == "stop":
                        row.update(
                            {
                                "standard_status": "human_verification_required",
                                "next_action": "resume_same_output_with_verification_policy_prompt",
                            }
                        )
                    else:
                        console.print("[yellow]Complete the visible CNKI verification; InstSci will continue the batch.[/yellow]")
                        typer.prompt("Press Enter here after verification", default="", show_default=False)
                        settle = settle_cnki_after_manual_step(
                            page,
                            resume_url=record["url"] if navigation_mode == "direct" else "",
                        )
                        row["manual_verification_settle"] = settle
                        if (
                            navigation_mode == "search"
                            and not settle.get("verification_required")
                            and not cnki_verification_visible(page)
                            and not cnki_pdf_button_visible(page)
                        ):
                            renavigation = navigate_cnki_article_via_search(
                                page,
                                title=title,
                                fallback_url=record["url"],
                                record_id=record_id,
                            )
                            row["post_verification_navigation"] = renavigation
                        if settle.get("verification_required"):
                            row.update(
                                {
                                    "standard_status": "human_verification_required",
                                    "next_action": "complete_visible_human_verification_then_rerun_same_output",
                                }
                            )
                if row["standard_status"] == "human_verification_required":
                    failure = item_evidence / "human_verification_required.png"
                    page.screenshot(path=str(failure), full_page=False)
                    row["evidence"] = str(failure)
                    rows.append(row)
                    write_checkpoint()
                    break
                before = item_evidence / "before_pdf_click.png"
                page.screenshot(path=str(before), full_page=False)
                result = capture_cnki_pdf(page, output_path=pdf_dir / f"{record_id}.pdf", timeout_ms=45_000)
                if result.get("verification_required"):
                    if verification_policy == "prompt":
                        console.print("[yellow]CNKI requested verification before this download. Complete it in the browser.[/yellow]")
                        typer.prompt("Press Enter here after verification", default="", show_default=False)
                        settle = settle_cnki_after_manual_step(
                            page,
                            resume_url=record["url"] if navigation_mode == "direct" else "",
                        )
                        row["download_verification_settle"] = settle
                        if (
                            navigation_mode == "search"
                            and not settle.get("verification_required")
                            and not cnki_verification_visible(page)
                            and not cnki_pdf_button_visible(page)
                        ):
                            renavigation = navigate_cnki_article_via_search(
                                page,
                                title=title,
                                fallback_url=record["url"],
                                record_id=record_id,
                            )
                            row["download_post_verification_navigation"] = renavigation
                        if not settle.get("verification_required"):
                            result = capture_cnki_pdf(page, output_path=pdf_dir / f"{record_id}.pdf", timeout_ms=45_000)
                after = item_evidence / "after_pdf_click.png"
                page.screenshot(path=str(after), full_page=False)
                pdf_path = Path(str(result.get("pdf_path") or ""))
                text = pdf_extractor.extract_text(pdf_path) if pdf_path.exists() else ""
                title_match = "".join(title.split()) in "".join(text.split())
                valid_pdf = bool(result.get("pdf_header_valid")) and int(result.get("size_bytes") or 0) > 10_000
                standard_status = (
                    "success"
                    if valid_pdf and title_match
                    else (
                        "pdf_candidate_conflict"
                        if valid_pdf
                        else ("human_verification_required" if result.get("verification_required") else "capture_failed")
                    )
                )
                row.update(result)
                row.update(
                    {
                        "article_url": safe_page_url(str(getattr(page, "url", "") or "")),
                        "title_match": title_match,
                        "text_length": len(text),
                        "file_status": "success" if valid_pdf and title_match else ("unverified" if valid_pdf else "missing"),
                        "standard_status": standard_status,
                        "evidence": str(after),
                        "next_action": (
                            "none"
                            if standard_status == "success"
                            else (
                                "complete_visible_human_verification_then_rerun_same_output"
                                if standard_status == "human_verification_required"
                                else "inspect_downloaded_pdf"
                            )
                        ),
                    }
                )
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                try:
                    failure = item_evidence / "failure.png"
                    page.screenshot(path=str(failure), full_page=False)
                    row["evidence"] = str(failure)
                except Exception:
                    pass
            rows.append(row)
            write_checkpoint()
            if row.get("standard_status") == "human_verification_required" and verification_policy == "stop":
                break
            if index < len(records):
                time.sleep(delay)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    success = sum(row.get("file_status") == "success" for row in rows)
    console.print(f"[bold]CNKI batch complete:[/bold] {success}/{len(records)} verified PDFs")
    console.print(f"[dim]Manifest: {manifest_path}[/dim]")
    if success != len(records):
        raise typer.Exit(2)


@app.command("wanfang-batch")
def wanfang_batch(
    file: Path = typer.Argument(help="JSON array of Wanfang records: record_id, title, optional query/url/zotero_item_key."),
    output: str = typer.Option("", "--output", "-o", help="Private run directory for PDFs, screenshots, and manifests."),
    delay: float = typer.Option(10.0, "--delay", min=2.0, help="Seconds between article downloads."),
    verification_policy: str = typer.Option("stop", "--verification-policy", help="Human-verification policy: stop or prompt."),
    profile_dir: str = typer.Option("", "--profile-dir", help="Persistent visible browser profile. Defaults to config chrome_profile_dir."),
    skip_completed: bool = typer.Option(True, "--skip-completed/--no-skip-completed", help="When --output has a prior manifest, keep successful rows and skip them."),
    strict_title_match: bool = typer.Option(True, "--strict-title-match/--no-strict-title-match", help="Require filename or extracted text to match the requested title."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Download a small Wanfang batch through search-result download popups."""
    from .extractors import pdf_extractor
    from .wanfang_session import (
        capture_wanfang_pdf,
        load_wanfang_batch,
        navigate_wanfang_search,
        open_wanfang_session,
        safe_wanfang_url,
        wanfang_verification_visible,
    )

    _setup_logging(verbose)
    try:
        records = load_wanfang_batch(file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Invalid Wanfang batch input: {exc}[/red]")
        raise typer.Exit(2)
    if not records:
        console.print("[yellow]Wanfang batch is empty.[/yellow]")
        return
    verification_policy = verification_policy.strip().lower()
    if verification_policy not in {"stop", "prompt"}:
        console.print("[red]--verification-policy must be 'stop' or 'prompt'.[/red]")
        raise typer.Exit(2)

    cfg = Config.load()
    cfg.ensure_dirs()
    run_dir = Path(output) if output else Path(cfg.output_dir).parent / "runs" / f"{datetime.now():%Y%m%d_%H%M%S}_wanfang_batch"
    pdf_dir = run_dir / "pdfs"
    evidence_dir = run_dir / "evidence"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    rows: list[dict[str, object]] = []
    completed_ids: set[str] = set()
    context = None

    if skip_completed and manifest_path.exists():
        try:
            previous_rows = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(previous_rows, list):
                previous_success: dict[str, dict[str, object]] = {}
                for previous in previous_rows:
                    if not isinstance(previous, dict):
                        continue
                    previous_id = str(previous.get("record_id") or "")
                    if previous_id and previous.get("file_status") == "success":
                        previous_success[previous_id] = previous
                for record in records:
                    previous = previous_success.get(record["record_id"])
                    if previous:
                        rows.append(previous)
                        completed_ids.add(record["record_id"])
                if completed_ids:
                    console.print(f"[green]Skipping {len(completed_ids)} previously verified Wanfang PDFs from {manifest_path}.[/green]")
        except (OSError, json.JSONDecodeError) as exc:
            console.print(f"[yellow]Could not load prior Wanfang manifest for resume: {exc}[/yellow]")

    def write_checkpoint() -> None:
        manifest_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "publisher": "Wanfang",
            "total": len(records),
            "processed": len(rows),
            "success": sum(row.get("file_status") == "success" for row in rows),
            "unverified": sum(row.get("file_status") == "unverified" for row in rows),
            "missing": sum(row.get("file_status") == "missing" for row in rows),
            "result_evidence": "browser_verified",
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    def screenshot_pages(item_evidence: Path, label: str) -> list[str]:
        screenshots: list[str] = []
        if context is None:
            return screenshots
        for page_index, browser_page in enumerate(list(context.pages), 1):
            try:
                shot = item_evidence / f"{label}_page_{page_index}.png"
                browser_page.screenshot(path=str(shot), full_page=False)
                screenshots.append(str(shot))
            except Exception:
                continue
        return screenshots

    try:
        context, page, _ = open_wanfang_session(
            cfg,
            output_dir=run_dir,
            profile_dir=profile_dir or None,
        )
        for index, record in enumerate(records, 1):
            record_id = record["record_id"]
            if record_id in completed_ids:
                continue
            title = record["title"]
            item_evidence = evidence_dir / record_id
            item_evidence.mkdir(parents=True, exist_ok=True)
            row: dict[str, object] = {
                "publisher": "Wanfang",
                "record_id": record_id,
                "title": title,
                "query": record["query"],
                "zotero_item_key": record["zotero_item_key"],
                "route_attempted": "visible_cloakbrowser_search_download_popup_pdf",
                "institution": _configured_subscription_institution(cfg),
                "file_status": "missing",
                "standard_status": "capture_failed",
                "result_evidence": "browser_verified",
                "next_action": "inspect_visible_wanfang_page",
            }
            console.print(f"[bold][{index}/{len(records)}][/bold] {title}")
            try:
                navigation = navigate_wanfang_search(page, query=record["query"], title=title)
                row["navigation"] = navigation
                row["before_screenshots"] = screenshot_pages(item_evidence, "before_download")
                if navigation.get("verification_required") or navigation.get("session_status") == "human_verification_required":
                    if verification_policy == "prompt":
                        console.print("[yellow]Complete the visible Wanfang verification; InstSci will retry this record.[/yellow]")
                        typer.prompt("Press Enter here after verification", default="", show_default=False)
                        navigation = navigate_wanfang_search(page, query=record["query"], title=title)
                        row["post_verification_navigation"] = navigation
                    if navigation.get("verification_required") or navigation.get("session_status") == "human_verification_required":
                        row.update(
                            {
                                "standard_status": "human_verification_required",
                                "next_action": "complete_visible_human_verification_then_rerun_same_output",
                                "evidence": (row.get("before_screenshots") or [""])[0],
                            }
                        )
                        rows.append(row)
                        write_checkpoint()
                        break

                result = capture_wanfang_pdf(
                    page,
                    title=title,
                    output_path=pdf_dir / f"{record_id}.pdf",
                    timeout_ms=75_000,
                )
                if result.get("verification_required") and verification_policy == "prompt":
                    console.print("[yellow]Wanfang requested verification before download. Complete it in the browser.[/yellow]")
                    typer.prompt("Press Enter here after verification", default="", show_default=False)
                    if not wanfang_verification_visible(page):
                        result = capture_wanfang_pdf(
                            page,
                            title=title,
                            output_path=pdf_dir / f"{record_id}.pdf",
                            timeout_ms=75_000,
                        )
                row.update(result)
                row["after_screenshots"] = screenshot_pages(item_evidence, "after_download")
                pdf_path = Path(str(result.get("pdf_path") or ""))
                text = pdf_extractor.extract_text(pdf_path) if pdf_path.exists() else ""
                title_match = bool(result.get("filename_title_match")) or ("".join(title.split()) in "".join(text.split()))
                valid_pdf = bool(result.get("pdf_header_valid")) and int(result.get("size_bytes") or 0) > 10_000
                success = valid_pdf and (title_match or not strict_title_match)
                standard_status = (
                    "success"
                    if success
                    else (
                        "pdf_candidate_conflict"
                        if valid_pdf
                        else ("human_verification_required" if result.get("verification_required") else "capture_failed")
                    )
                )
                row.update(
                    {
                        "article_url": safe_wanfang_url(str(getattr(page, "url", "") or "")),
                        "title_match": title_match,
                        "text_length": len(text),
                        "file_status": "success" if success else ("unverified" if valid_pdf else "missing"),
                        "standard_status": standard_status,
                        "evidence": (row.get("after_screenshots") or row.get("before_screenshots") or [""])[0],
                        "next_action": (
                            "none"
                            if standard_status == "success"
                            else (
                                "complete_visible_human_verification_then_rerun_same_output"
                                if standard_status == "human_verification_required"
                                else "inspect_downloaded_pdf"
                            )
                        ),
                    }
                )
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                row["after_screenshots"] = screenshot_pages(item_evidence, "failure")
                row["evidence"] = (row.get("after_screenshots") or [""])[0]
            rows.append(row)
            write_checkpoint()
            if row.get("standard_status") == "human_verification_required" and verification_policy == "stop":
                break
            if index < len(records):
                time.sleep(delay)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    success_count = sum(row.get("file_status") == "success" for row in rows)
    console.print(f"[bold]Wanfang batch complete:[/bold] {success_count}/{len(records)} verified PDFs")
    console.print(f"[dim]Manifest: {manifest_path}[/dim]")
    if success_count != len(records):
        raise typer.Exit(2)


@app.command()
def fetch(
    identifier: str = typer.Argument(help="DOI or URL of the paper to fetch."),
    output: str = typer.Option("", "--output", "-o", help="Output directory for PDFs."),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, markdown, text."),
    text_only: bool = typer.Option(False, "--text-only", "-t", help="Output only plain text (minimal tokens)."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Fetch a single paper by DOI or URL."""
    _setup_logging(verbose)
    config = Config.load()
    _ensure_email(config)
    if output:
        config.output_dir = output

    fetcher = PaperFetcher(config)
    try:
        console.print(f"[bold]Fetching:[/bold] {identifier}")
        result = fetcher.fetch_with_result(identifier, use_cache=not no_cache)
        paper = result.paper

        if result.status != "success":
            console.print(f"[yellow]Status: {result.status} ({result.reason or result.quality})[/yellow]")
            if result.next_action:
                console.print(f"[yellow]Next: {result.next_action.message}[/yellow]")
                if result.next_action.command:
                    console.print(f"[dim]{result.next_action.command}[/dim]")

        if text_only:
            console.print(result.to_text())
        elif format == "markdown":
            console.print(result.to_markdown())
        elif format == "text":
            console.print(result.to_text())
        else:
            console.print(result.to_json())

        if paper.pdf_path:
            console.print(f"\n[dim]PDF saved to: {paper.pdf_path}[/dim]")
        console.print(f"[dim]Source: {paper.source}[/dim]")

    finally:
        fetcher.close()


@app.command()
def batch(
    file: Path = typer.Argument(help="File containing DOIs (one per line)."),
    output: str = typer.Option("", "--output", "-o", help="Output directory."),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, markdown, text."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Fetch multiple papers from a file of DOIs."""
    _setup_logging(verbose)

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    dois = [
        line.strip()
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    if not dois:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    console.print(f"[bold]Found {len(dois)} DOIs to fetch.[/bold]")

    config = Config.load()
    if output:
        config.output_dir = output

    fetcher = PaperFetcher(config)
    results_dir = Path(config.output_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    succeeded = 0
    failed = 0

    try:
        for i, doi in enumerate(dois, 1):
            console.print(f"\n[bold][{i}/{len(dois)}][/bold] Fetching: {doi}")
            try:
                paper = fetcher.fetch(doi)
                if paper.full_text:
                    succeeded += 1
                    # Save result
                    safe_name = doi.replace("/", "_").replace(":", "_")
                    if format == "markdown":
                        out_file = results_dir / f"{safe_name}.md"
                        out_file.write_text(paper.to_markdown(), encoding="utf-8")
                    elif format == "text":
                        out_file = results_dir / f"{safe_name}.txt"
                        out_file.write_text(paper.to_text(), encoding="utf-8")
                    else:
                        out_file = results_dir / f"{safe_name}.json"
                        out_file.write_text(paper.to_json(), encoding="utf-8")
                    console.print(f"  [green]OK[/green] → {out_file.name}")
                else:
                    failed += 1
                    console.print("  [yellow]No full text extracted[/yellow]")
            except Exception as e:
                failed += 1
                console.print(f"  [red]Error: {e}[/red]")

        console.print(f"\n[bold]Done:[/bold] {succeeded} succeeded, {failed} failed out of {len(dois)}.")

    finally:
        fetcher.close()


@app.command("est-batch")
def est_batch(
    year: int = typer.Option(2026, "--year", help="Publication year."),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of EST articles."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/2FA completion."),
    pdf_timeout: int = typer.Option(60, "--pdf-timeout", help="Seconds to wait for each candidate PDF navigation."),
    post_login_hold: int = typer.Option(0, "--post-login-hold", help="Seconds to keep the authorized article page open before PDF capture."),
    post_run_hold: int = typer.Option(0, "--post-run-hold", help="Seconds to keep the browser page open after capture or failure."),
    target_verified: int = typer.Option(0, "--target-verified", help="Stop after this many verified PDFs. Zero disables early stop."),
    attempt_cache: str = typer.Option("", "--attempt-cache", help="JSONL attempt cache path. Defaults to attempts.jsonl in the run directory."),
    skip_attempted: bool = typer.Option(False, "--skip-attempted", help="Skip DOIs already present in the attempt cache."),
):
    """Download recent Environmental Science & Technology articles through ACS/CloakBrowser."""
    from .acs_batch import ACSCloakBatchDownloader, fetch_est_records

    cfg = Config.load()
    institution = _resolve_subscription_institution(cfg, institution)
    institution_aliases = _configured_institution_aliases(cfg, institution)
    run_dir = Path(output) if output else Path("downloads") / f"est_{year}_{limit}" / f"acs_cloak_{datetime.now():%Y%m%d_%H%M%S}"
    console.print(f"[bold]Fetching EST metadata:[/bold] year={year}, limit={limit}")
    records = fetch_est_records(year=year, limit=limit, email=cfg.email)
    if not records:
        console.print("[red]No EST records found.[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Found {len(records)} DOI records.[/green]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print("[dim]If a CloakBrowser window stops on SSO or 2FA, complete it there and leave the window open.[/dim]")

    downloader = ACSCloakBatchDownloader(
        cfg,
        institution_query=institution,
        institution_aliases=institution_aliases,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
        post_login_hold_sec=post_login_hold,
        post_run_hold_sec=post_run_hold,
    )
    summary = downloader.run_records(
        records,
        run_dir,
        retry_failed=retry_failed,
        target_verified=target_verified or None,
        attempt_cache=attempt_cache or None,
        skip_attempted=skip_attempted,
    )
    console.print(
        f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
        f"{summary.get('unverified', 0)} unverified PDFs."
    )
    console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
    console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
    console.print(f"[dim]Attempt cache: {summary['attempt_cache']}[/dim]")
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items failed or were unverified; see the run manifest and diagnostics folders.[/yellow]")
        raise typer.Exit(2)


@app.command("publisher-batch")
def publisher_batch(
    file: Path = typer.Argument(help="File containing DOI values (one per line)."),
    publisher: str = typer.Option("acs", "--publisher", "-p", help="Publisher profile key, e.g. acs, elsevier, wiley, or ieee."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Override the persistent CloakBrowser profile directory."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/2FA completion."),
    pdf_timeout: int = typer.Option(60, "--pdf-timeout", help="Seconds to wait for each candidate PDF navigation."),
    carsi_portal_preauth: bool = typer.Option(False, "--carsi-portal-preauth/--no-carsi-portal-preauth", help="Open the CARSI resource portal first in the same visible CloakBrowser profile."),
    target_verified: int = typer.Option(0, "--target-verified", help="Stop after this many verified PDFs. Zero disables early stop."),
    attempt_cache: str = typer.Option("", "--attempt-cache", help="JSONL attempt cache path. Defaults to attempts.jsonl in the run directory."),
    skip_attempted: bool = typer.Option(False, "--skip-attempted", help="Skip DOIs already present in the attempt cache."),
    mode: str = typer.Option(DEFAULT_RUN_MODE, "--mode", help="Run mode: user, diagnose, or dev."),
    force: bool = typer.Option(False, "--force", help="Bypass publisher capability-matrix batch guards."),
    browser_doctor_gate: bool = typer.Option(True, "--browser-doctor/--no-browser-doctor", help="Run visible browser preflight and after-run diagnostics."),
    watch_browser: str = typer.Option("notify", "--watch-browser", help="Runtime browser watch mode: off, quiet, notify, or focus."),
    watch_interval: float = typer.Option(4.0, "--watch-interval", min=2.0, help="Seconds between runtime browser watch checks."),
    pause_on_blocker: bool = typer.Option(True, "--pause-on-blocker/--no-pause-on-blocker", help="Pause visible browser workflow on CAPTCHA/WAF before continuing PDF capture."),
):
    """Download a DOI list through a named publisher profile and CloakBrowser."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    records = [
        PaperRecord(doi=line.strip())
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not records:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    try:
        profile = get_publisher_profile(publisher)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    institution = _resolve_subscription_institution(cfg, institution)
    institution_aliases = _configured_institution_aliases(cfg, institution)
    profile_key = _broker_key_for_profile(profile, publisher)
    run_dir = Path(output) if output else Path("downloads") / f"{profile_key}_{len(records)}" / f"cloak_{datetime.now():%Y%m%d_%H%M%S}"
    runtime = _resolve_browser_runtime_options(
        mode=mode,
        browser_doctor_gate=browser_doctor_gate,
        watch_browser=watch_browser,
        pause_on_blocker=pause_on_blocker,
    )
    publisher_matrix = _enforce_publisher_matrix(
        publisher=profile_key,
        count=len(records),
        force=force,
    )
    console.print(f"[bold]Publisher profile:[/bold] {profile.name}")
    console.print(f"[bold]Found {len(records)} DOI records.[/bold]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print(f"[bold]Browser profile:[/bold] {cfg.chrome_profile_dir}")
    console.print(f"[dim]Mode: {runtime['mode']} | watch: {runtime['watch_browser']}[/dim]")
    console.print("[dim]If a CloakBrowser window stops on SSO, 2FA, CAPTCHA, or human verification, complete it there and leave the window open.[/dim]")
    _run_browser_preflight_gate(
        publisher=profile_key,
        run_dir=run_dir,
        browser_profile=cfg.chrome_profile_dir,
        enabled=bool(runtime["browser_doctor_gate"]),
        capture_screenshots=bool(runtime["preflight_screenshots"]),
        print_result=bool(runtime["verbose_summary"]),
    )

    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        institution_aliases=institution_aliases,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
        carsi_portal_preauth=carsi_portal_preauth,
        pause_on_blocker=bool(runtime["pause_on_blocker"]),
    )
    summary = None
    try:
        watch_stop, watch_thread = _start_browser_watchdog(
            publisher=profile_key,
            run_dir=run_dir,
            mode=str(runtime["watch_browser"]),
            interval=watch_interval,
            browser_profile=cfg.chrome_profile_dir,
        )
        summary = downloader.run_records(
            records,
            run_dir,
            retry_failed=retry_failed,
            target_verified=target_verified or None,
            attempt_cache=attempt_cache or None,
            skip_attempted=skip_attempted,
        )
        summary["run_mode"] = runtime["mode"]
        summary["publisher_matrix"] = publisher_matrix
        _persist_cli_summary_metadata(run_dir, summary)
    finally:
        _stop_browser_watchdog(locals().get("watch_stop"), locals().get("watch_thread"))
        _run_after_run_doctor_if_needed(
            publisher=profile_key,
            run_dir=run_dir,
            enabled=bool(runtime["browser_doctor_gate"]),
            summary=summary,
            mode=str(runtime["mode"]),
            browser_profile=cfg.chrome_profile_dir,
            after_run_on_success=bool(runtime["after_run_on_success"]),
        )
    _print_download_summary(summary, verbose=bool(runtime["verbose_summary"]))
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items failed or were unverified; rerun with --mode diagnose for the saved browser evidence and next action.[/yellow]")
        raise typer.Exit(2)


@zotero_app.command("handoff")
def zotero_handoff(
    manifest: Path = typer.Argument(help="InstSci run directory, complete directory, manifest.json, or manifest.csv."),
    output: str = typer.Option("", "--output", "-o", help="Output JSON path. Defaults to zotero_mcp_handoff.json beside the manifest."),
    statuses: str = typer.Option("success", "--statuses", help="Comma-separated standard_status values to include."),
    tags: str = typer.Option("", "--tags", help="Comma-separated extra Zotero tags."),
    collections: str = typer.Option("", "--collections", help="Comma-separated Zotero collection keys or names."),
    attach_mode: str = typer.Option("none", "--attach-mode", help="Zotero MCP attach_mode: none, auto, or required."),
    include_missing: bool = typer.Option(False, "--include-missing", help="Include non-success rows for review/import planning."),
):
    """Build a Zotero MCP import queue from an InstSci manifest.

    This command does not write directly to Zotero. It creates a handoff JSON
    that an agent can execute with Zotero MCP tools such as zotero_add_by_url.
    """
    from .zotero_mcp import build_zotero_mcp_handoff, resolve_manifest_path, write_zotero_mcp_handoff

    selected_statuses = [part.strip() for part in statuses.split(",") if part.strip()]
    extra_tags = [part.strip() for part in tags.split(",") if part.strip()]
    collection_values = [part.strip() for part in collections.split(",") if part.strip()]
    manifest_path = resolve_manifest_path(manifest)
    output_path = Path(output) if output else manifest_path.parent / "zotero_mcp_handoff.json"
    payload = build_zotero_mcp_handoff(
        manifest,
        statuses=selected_statuses,
        tags=extra_tags,
        collections=collection_values,
        attach_mode=attach_mode,
        include_missing=include_missing,
    )
    written = write_zotero_mcp_handoff(payload, output_path)
    summary = payload["summary"]
    console.print("[green]Zotero MCP handoff written.[/green]")
    console.print(f"[dim]Path: {written}[/dim]")
    console.print(
        f"Rows: {summary['rows']} | metadata imports: {summary['metadata_imports']} | "
        f"skipped: {summary['skipped']}"
    )
    if summary["metadata_imports"]:
        console.print("[dim]Run `instsci zotero sync` to create/match Zotero items, link PDFs, and write keys back to the manifest.[/dim]")
    else:
        console.print("[yellow]No selected metadata imports. Check --statuses or --include-missing.[/yellow]")


@zotero_app.command("sync")
def zotero_sync(
    manifest: Path = typer.Argument(help="InstSci run directory, complete directory, manifest.json, manifest.csv, or zotero_mcp_handoff.json."),
    output: str = typer.Option("", "--output", "-o", help="Optional Zotero sync report path. Defaults to zotero_sync_report.json beside the manifest."),
    statuses: str = typer.Option("success", "--statuses", help="Comma-separated standard_status values to include when building a handoff from a manifest."),
    tags: str = typer.Option("", "--tags", help="Comma-separated extra Zotero tags."),
    collections: str = typer.Option("", "--collections", help="Comma-separated Zotero collection keys or names."),
    attach_mode: str = typer.Option("required", "--attach-mode", help="Manifest selection mode: none, auto, or required. Required skips rows without an existing PDF."),
    attachment_mode: str = typer.Option("linked_file", "--attachment-mode", help="Zotero PDF attachment mode. Currently only linked_file is supported."),
    include_missing: bool = typer.Option(False, "--include-missing", help="Include non-success rows for explicit review/import workflows."),
    write_back: bool = typer.Option(True, "--write-back/--no-write-back", help="Write Zotero item and attachment keys back into the InstSci manifest."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate selection and PDF paths without writing to Zotero or the manifest."),
):
    """Sync successful InstSci rows into Zotero and link the matching local PDF.

    This command keeps Zotero clean: it creates or matches the bibliographic
    item, creates a linked_file PDF attachment, and stores process state only in
    the InstSci manifest/report. It does not create Zotero notes.
    """
    from .zotero_mcp import execute_zotero_sync, resolve_manifest_path

    selected_statuses = [part.strip() for part in statuses.split(",") if part.strip()]
    extra_tags = [part.strip() for part in tags.split(",") if part.strip()]
    collection_values = [part.strip() for part in collections.split(",") if part.strip()]
    manifest_path = resolve_manifest_path(manifest)
    if output:
        report_path = Path(output)
    elif manifest_path.name == "zotero_mcp_handoff.json":
        report_path = manifest_path.with_name("zotero_sync_report.json")
    else:
        report_path = manifest_path.parent / "zotero_sync_report.json"

    try:
        report = execute_zotero_sync(
            manifest,
            statuses=selected_statuses,
            tags=extra_tags,
            collections=collection_values,
            attach_mode=attach_mode,
            attachment_mode=attachment_mode,
            include_missing=include_missing,
            write_back=write_back,
            dry_run=dry_run,
            report_path=report_path,
        )
    except Exception as exc:
        console.print(f"[red]Zotero sync failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    summary = report["summary"]
    if summary["errors"]:
        label = "[red]Zotero sync completed with errors.[/red]"
        exit_code = 2
    elif summary["skipped"]:
        label = "[yellow]Zotero sync completed with skipped rows.[/yellow]"
        exit_code = 2
    elif dry_run:
        label = "[green]Zotero sync dry run completed.[/green]"
        exit_code = 0
    else:
        label = "[green]Zotero sync completed.[/green]"
        exit_code = 0

    console.print(label)
    console.print(f"[dim]Report: {report_path}[/dim]")
    console.print(
        f"Actions: {summary['actions']} | success: {summary['success']} | "
        f"dry-run: {summary['dry_run']} | skipped: {summary['skipped']} | errors: {summary['errors']}"
    )
    if report["write_back"]:
        console.print(f"[dim]Manifest updated: {report['manifest']}[/dim]")
    elif dry_run:
        console.print("[dim]Dry run only: Zotero and manifest were not modified.[/dim]")
    if exit_code:
        raise typer.Exit(exit_code)


@app.command("workflow-plan")
def workflow_plan(
    manifest: Path = typer.Argument(help="InstSci run directory, complete directory, manifest.json, or manifest.csv."),
    output: str = typer.Option("", "--output", "-o", help="Optional workflow plan JSON path. Defaults to workflow_plan.json beside the manifest."),
    include_success: bool = typer.Option(False, "--include-success", help="Include success rows, normally useful for Zotero sync planning only."),
    json_report: bool = typer.Option(False, "--json", help="Print JSON instead of a compact table."),
):
    """Build the next-step acquisition workflow for failed or unresolved rows."""
    from .zotero_mcp import load_manifest_rows

    manifest_path, rows = load_manifest_rows(manifest)
    report = _build_workflow_plan(manifest_path, rows, include_success=include_success)
    output_path = Path(output) if output else manifest_path.parent / "workflow_plan.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if json_report:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return

    summary = report["summary"]
    console.print("[green]Workflow plan written.[/green]")
    console.print(f"[dim]Path: {output_path}[/dim]")
    console.print(f"Rows: {summary['rows']} | attention items: {summary['items']}")
    table = Table(title="InstSci Workflow Plan")
    table.add_column("#", justify="right")
    table.add_column("DOI", overflow="fold")
    table.add_column("Status")
    table.add_column("Next Action", overflow="fold")
    table.add_column("Suggested Paths", overflow="fold")
    for item in report["items"]:
        table.add_row(
            str(item["index"]),
            str(item["doi"]),
            str(item["standard_status"]),
            str(item["next_action"]),
            ", ".join(str(path) for path in item["suggested_paths"]),
        )
    console.print(table)


@app.command("public-audit")
def public_audit(
    package_path: Path = typer.Argument(help="Public review package directory to audit."),
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
    include_institution_scan: bool = typer.Option(True, "--institution-scan/--no-institution-scan", help="Scan for institution-specific public package traces."),
    json_report: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
):
    """Audit a public review package for cache files, local paths, and sensitive traces."""
    from .public_audit import audit_public_package, write_json_report

    report = audit_public_package(package_path, include_institution_scan=include_institution_scan)
    if output:
        written = write_json_report(report, output)
        report["written"] = str(written)

    if json_report:
        console.print_json(json.dumps(report, ensure_ascii=False))
    else:
        console.print(f"[bold]Public package audit:[/bold] {_status_label(report['status'])}")
        console.print(f"[dim]Path: {report['path']}[/dim]")
        console.print(f"Files: {report['file_count']} | Issues: {report['issue_count']}")
        if output:
            console.print(f"[dim]Report: {report['written']}[/dim]")

        if report["summary"]:
            summary_table = Table(title="Issue Summary")
            summary_table.add_column("Issue")
            summary_table.add_column("Count", justify="right")
            for code, count in report["summary"].items():
                summary_table.add_row(str(code), str(count))
            console.print(summary_table)

        if report["issues"]:
            issue_table = Table(title="First Issues")
            issue_table.add_column("Code", width=28)
            issue_table.add_column("Location", overflow="fold")
            issue_table.add_column("Text", overflow="fold")
            for issue in report["issues"][:20]:
                location = issue["path"] if not issue["line"] else f"{issue['path']}:{issue['line']}"
                issue_table.add_row(str(issue["code"]), location, str(issue["text"]))
            console.print(issue_table)

    if report["status"] == "fail":
        raise typer.Exit(2)


@app.command("doctor")
def doctor(
    full: bool = typer.Option(False, "--full", help="Include public package audit and broader readiness checks."),
    package_path: str = typer.Option("", "--package-path", "--package", help="Package directory to audit when --full is used. Defaults to the current directory."),
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
    json_report: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
):
    """Run a lightweight InstSci environment and package self-check."""
    from .public_audit import doctor_report, write_json_report

    audit_path = package_path or (str(Path.cwd()) if full else "")
    report = doctor_report(package_path=audit_path or None, full=full)
    if output:
        written = write_json_report(report, output)
        report["written"] = str(written)

    if json_report:
        console.print_json(json.dumps(report, ensure_ascii=False))
    else:
        console.print(f"[bold]InstSci doctor:[/bold] {_status_label(report['status'])}")
        if output:
            console.print(f"[dim]Report: {report['written']}[/dim]")
        table = Table(title="Doctor Checks")
        table.add_column("Check", width=28)
        table.add_column("Status", width=8)
        table.add_column("Details", overflow="fold")
        for check in report["checks"]:
            details = ""
            if check["name"] == "runtime_dependencies":
                missing = check.get("missing") or []
                details = "missing: " + ", ".join(missing) if missing else f"python {check.get('python')}"
            elif check["name"] == "browser_doctor_support":
                details = "supported" if check.get("supported") else str(check.get("note") or "")
            elif check["name"] == "publisher_matrix":
                details = f"entries={check.get('entries')} statuses={check.get('status_counts')}"
            elif check["name"] == "zotero_handoff":
                details = str(check.get("error") or "smoke ok")
            elif check["name"] == "public_package_audit":
                details = f"issues={check.get('issue_count')} path={check.get('path')}"
            table.add_row(str(check["name"]), _status_label(str(check.get("status") or "")), details)
        console.print(table)

    if report["status"] == "fail":
        raise typer.Exit(2)


@app.command("papers")
def papers(
    file: Path = typer.Argument(help="File containing DOI values (one per line)."),
    publisher: str = typer.Option("auto", "--publisher", "-p", help="Publisher profile, or 'auto' to infer from DOI prefixes."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Override the persistent CloakBrowser profile directory."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/CAPTCHA completion."),
    pdf_timeout: int = typer.Option(90, "--pdf-timeout", help="Seconds to wait for each PDF navigation."),
    post_login_hold: int = typer.Option(0, "--post-login-hold", help="Seconds to keep the authorized article page open before PDF capture."),
    post_run_hold: int = typer.Option(0, "--post-run-hold", help="Seconds to keep the browser page open after capture or failure."),
    carsi_portal_preauth: bool = typer.Option(False, "--carsi-portal-preauth/--no-carsi-portal-preauth", help="Open the CARSI resource portal first in the same visible CloakBrowser profile."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    concurrency: int = typer.Option(1, "--concurrency", "-j", min=1, max=4, help="Parallel browser workers. Use 2 for ScienceDirect; higher values may trigger publisher checks."),
    broker: bool = typer.Option(True, "--broker/--no-broker", help="Use the long-lived publisher session broker by default."),
    broker_ttl: int = typer.Option(259200, "--broker-ttl", help="Seconds to keep an auto-started broker alive."),
    detach: bool = typer.Option(False, "--detach", help="Submit to the long-lived broker and return immediately."),
    oa_first: bool = typer.Option(True, "--oa-first/--no-oa-first", help="Try cache/OA/open-publisher PDF routes before visible browser workflow."),
    mode: str = typer.Option(DEFAULT_RUN_MODE, "--mode", help="Run mode: user, diagnose, or dev."),
    force: bool = typer.Option(False, "--force", help="Bypass publisher capability-matrix batch guards."),
    browser_doctor_gate: bool = typer.Option(True, "--browser-doctor/--no-browser-doctor", help="Run visible browser preflight and after-run diagnostics."),
    watch_browser: str = typer.Option("notify", "--watch-browser", help="Runtime browser watch mode: off, quiet, notify, or focus."),
    watch_interval: float = typer.Option(4.0, "--watch-interval", min=2.0, help="Seconds between runtime browser watch checks."),
    pause_on_blocker: bool = typer.Option(True, "--pause-on-blocker/--no-pause-on-blocker", help="Pause visible browser workflow on CAPTCHA/WAF before continuing PDF capture."),
):
    """OA-first papers workflow; browser workflow is used only for remaining closed-access PDFs."""
    from .publisher_batch import PublisherBatchDownloader

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    records = _read_paper_records(file)
    if not records:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    run_dir = Path(output) if output else Path("downloads") / f"papers_{len(records)}" / f"run_{datetime.now():%Y%m%d_%H%M%S}"

    oa_rows: list[dict[str, object]] = []
    browser_records = records
    if oa_first:
        console.print(f"[bold]OA first:[/bold] checking {len(records)} DOI records before browser workflow.")
        oa_rows, browser_records = _papers_oa_first(records=records, cfg=cfg, run_dir=run_dir, use_cache=True)
        console.print(f"[bold]OA first:[/bold] {len(oa_rows)}/{len(records)} resolved; {len(browser_records)} need browser workflow.")
        if not browser_records:
            summary = _write_papers_manifest(run_dir, oa_rows)
            summary["run_mode"] = _normalize_run_mode(mode)
            summary["oa_first"] = True
            summary["publisher_matrix"] = None
            (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            _print_download_summary(summary, verbose=False)
            return

    try:
        profile = _resolve_papers_profile(browser_records, publisher)
    except typer.Exit:
        if publisher.strip().lower() == "auto" and oa_rows:
            summary = _write_pending_browser_manifest(
                run_dir,
                oa_rows=oa_rows,
                browser_records=browser_records,
                mode=mode,
                oa_first=oa_first,
                reason="mixed_publisher_browser_queue_not_attempted",
            )
            _print_download_summary(summary, verbose=False)
            console.print(
                "[yellow]Remaining browser records were split by publisher under "
                f"{run_dir / 'browser_groups'}; rerun each group with --publisher.[/yellow]"
            )
            raise typer.Exit(2)
        if publisher.strip().lower() == "auto" and browser_records:
            summary = _write_unsupported_publisher_manifest(
                run_dir,
                oa_rows=oa_rows,
                browser_records=browser_records,
                mode=mode,
                oa_first=oa_first,
                reason="publisher_auto_inference_failed",
            )
            _print_download_summary(summary, verbose=False)
            console.print(
                "[yellow]Could not infer a supported publisher profile; manifest was written "
                "with unsupported_publisher rows for workflow planning.[/yellow]"
            )
            raise typer.Exit(2)
        raise
    institution = _resolve_subscription_institution(cfg, institution)
    institution_aliases = _configured_institution_aliases(cfg, institution)
    profile_key = profile.name.lower().replace(" ", "-")
    if not output:
        run_dir = Path("downloads") / f"papers_{profile_key}_{len(records)}" / f"browser_{datetime.now():%Y%m%d_%H%M%S}"
    broker_publisher = _broker_key_for_profile(profile, publisher)
    runtime = _resolve_browser_runtime_options(
        mode=mode,
        browser_doctor_gate=browser_doctor_gate,
        watch_browser=watch_browser,
        pause_on_blocker=pause_on_blocker,
    )
    publisher_matrix = _enforce_publisher_matrix(
        publisher=broker_publisher,
        count=len(browser_records),
        force=force,
    )

    console.print(f"[bold]Recommended route:[/bold] OA-first + browser workflow for remaining PDFs ({profile.name})")
    console.print("[dim]Complete SSO, 2FA, CAPTCHA, or human verification in the opened browser window; InstSci continues automatically.[/dim]")
    console.print(f"[bold]Found {len(records)} DOI records.[/bold]")
    if oa_first:
        console.print(f"[bold]Browser queue:[/bold] {len(browser_records)} remaining after OA-first.")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print(f"[bold]Browser profile:[/bold] {cfg.chrome_profile_dir}")
    console.print(f"[dim]Mode: {runtime['mode']} | watch: {runtime['watch_browser']}[/dim]")

    _run_browser_preflight_gate(
        publisher=broker_publisher,
        run_dir=run_dir,
        browser_profile=cfg.chrome_profile_dir,
        enabled=bool(runtime["browser_doctor_gate"]),
        allow_running_broker=broker,
        broker_publisher=broker_publisher,
        capture_screenshots=bool(runtime["preflight_screenshots"]),
        print_result=bool(runtime["verbose_summary"]),
    )
    if detach and not broker:
        console.print("[red]--detach requires the long-lived session broker. Remove --no-broker.[/red]")
        raise typer.Exit(1)
    if broker:
        from . import session_broker

        if _ensure_session_broker(
            broker_publisher=broker_publisher,
            cfg=cfg,
            institution=institution,
            broker_ttl=broker_ttl,
        ):
            console.print(f"[bold]Session broker:[/bold] running ({broker_publisher})")
            if detach:
                job = _enqueue_papers_job(
                    profile=profile,
                    broker_publisher=broker_publisher,
                    records=browser_records,
                    run_dir=run_dir,
                    cfg=cfg,
                    institution=institution,
                    institution_aliases=institution_aliases,
                    login_timeout=login_timeout,
                    pdf_timeout=pdf_timeout,
                    post_login_hold=post_login_hold,
                    post_run_hold=post_run_hold,
                    carsi_portal_preauth=carsi_portal_preauth,
                    pause_on_blocker=bool(runtime["pause_on_blocker"]),
                    command=" ".join(sys.argv),
                )
                if oa_rows:
                    summary = _write_papers_manifest(run_dir, oa_rows)
                    summary["run_mode"] = runtime["mode"]
                    summary["publisher_matrix"] = publisher_matrix
                    summary["oa_first"] = True
                    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                    console.print(f"[dim]OA-first manifest saved before detached browser job: {summary['manifest']}[/dim]")
                _print_job_submitted(job)
                return

            timeout_seconds = max(
                120,
                login_timeout + (login_timeout if carsi_portal_preauth else 0) + len(browser_records) * (pdf_timeout + post_login_hold + post_run_hold + 60),
            )
            summary = None
            try:
                watch_stop, watch_thread = _start_browser_watchdog(
                    publisher=broker_publisher,
                    run_dir=run_dir,
                    mode=str(runtime["watch_browser"]),
                    interval=watch_interval,
                    browser_profile=cfg.chrome_profile_dir,
                )
                try:
                    summary = session_broker.submit_broker_job(
                        publisher=broker_publisher,
                        records=_record_payload(browser_records),
                        output_dir=str(run_dir),
                        institution=institution,
                        institution_aliases=list(institution_aliases),
                        login_timeout=login_timeout,
                        pdf_timeout=pdf_timeout,
                        post_login_hold=post_login_hold,
                        post_run_hold=post_run_hold,
                        carsi_portal_preauth=carsi_portal_preauth,
                        pause_on_blocker=bool(runtime["pause_on_blocker"]),
                        timeout_seconds=timeout_seconds,
                    )
                except TimeoutError as exc:
                    summary = _write_browser_exception_manifest(
                        run_dir,
                        oa_rows=oa_rows,
                        browser_records=browser_records,
                        mode=str(runtime["mode"]),
                        oa_first=bool(oa_first),
                        publisher_matrix=publisher_matrix,
                        reason="browser_broker_timeout",
                        exc=exc,
                    )
                except RuntimeError as exc:
                    summary = _write_browser_exception_manifest(
                        run_dir,
                        oa_rows=oa_rows,
                        browser_records=browser_records,
                        mode=str(runtime["mode"]),
                        oa_first=bool(oa_first),
                        publisher_matrix=publisher_matrix,
                        reason="browser_broker_error",
                        exc=exc,
                    )
                browser_rows = _load_manifest_rows(summary)
                if not browser_rows and not str(summary.get("manifest") or "").strip() and browser_records:
                    summary.update(
                        _write_unresolved_browser_manifest(
                            run_dir,
                            oa_rows=oa_rows,
                            browser_records=browser_records,
                            mode=str(runtime["mode"]),
                            oa_first=bool(oa_first),
                            publisher_matrix=publisher_matrix,
                            reason="browser_workflow_no_manifest",
                        )
                    )
                elif oa_rows:
                    combined_rows = _merge_manifest_rows(oa_rows, browser_rows)
                    summary.update(_write_papers_manifest(run_dir, combined_rows))
                summary["run_mode"] = runtime["mode"]
                summary["publisher_matrix"] = publisher_matrix
                summary["oa_first"] = bool(oa_first)
                _persist_cli_summary_metadata(run_dir, summary)
            finally:
                _stop_browser_watchdog(locals().get("watch_stop"), locals().get("watch_thread"))
                _run_after_run_doctor_if_needed(
                    publisher=broker_publisher,
                    run_dir=run_dir,
                    enabled=bool(runtime["browser_doctor_gate"]),
                    summary=summary,
                    mode=str(runtime["mode"]),
                    browser_profile=cfg.chrome_profile_dir,
                    after_run_on_success=bool(runtime["after_run_on_success"]),
                )
            _print_download_summary(summary, verbose=bool(runtime["verbose_summary"]))
            if summary["missing"] or summary.get("unverified", 0):
                console.print("[yellow]Some items need manual attention; rerun with --mode diagnose after completing visible browser steps.[/yellow]")
                raise typer.Exit(2)
            return
        console.print("[yellow]Session broker did not start; falling back to one-shot browser workflow.[/yellow]")

    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        institution_aliases=institution_aliases,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
        post_login_hold_sec=post_login_hold,
        post_run_hold_sec=post_run_hold,
        carsi_portal_preauth=carsi_portal_preauth,
        pause_on_blocker=bool(runtime["pause_on_blocker"]),
    )
    summary = None
    try:
        watch_stop, watch_thread = _start_browser_watchdog(
            publisher=broker_publisher,
            run_dir=run_dir,
            mode=str(runtime["watch_browser"]),
            interval=watch_interval,
            browser_profile=cfg.chrome_profile_dir,
        )
        summary = downloader.run_records(
            browser_records,
            run_dir,
            retry_failed=retry_failed,
            concurrency=concurrency,
        )
        browser_rows = _load_manifest_rows(summary)
        if oa_rows:
            combined_rows = _merge_manifest_rows(oa_rows, browser_rows)
            summary.update(_write_papers_manifest(run_dir, combined_rows))
        summary["run_mode"] = runtime["mode"]
        summary["publisher_matrix"] = publisher_matrix
        summary["oa_first"] = bool(oa_first)
        _persist_cli_summary_metadata(run_dir, summary)
    finally:
        _stop_browser_watchdog(locals().get("watch_stop"), locals().get("watch_thread"))
        _run_after_run_doctor_if_needed(
            publisher=broker_publisher,
            run_dir=run_dir,
            enabled=bool(runtime["browser_doctor_gate"]),
            summary=summary,
            mode=str(runtime["mode"]),
            browser_profile=cfg.chrome_profile_dir,
            after_run_on_success=bool(runtime["after_run_on_success"]),
        )
    _print_download_summary(summary, verbose=bool(runtime["verbose_summary"]))
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items need manual attention; rerun with --mode diagnose after completing visible browser steps.[/yellow]")
        raise typer.Exit(2)


@jobs_app.command("list")
def jobs_list(
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="Number of recent jobs to show."),
    json_output: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
):
    """List recent long-running InstSci jobs."""
    from . import job_store

    jobs = [job_store.refresh_job(job) for job in job_store.list_jobs(limit=limit)]
    if json_output:
        console.print(json.dumps(jobs, ensure_ascii=False, indent=2))
        return
    _print_jobs_table(jobs)


@jobs_app.command("status")
def jobs_status(
    job_id: str = typer.Argument("", help="Job id. Omit to show recent jobs."),
    json_output: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
):
    """Show one job status, or recent jobs when no id is given."""
    from . import job_store

    if not job_id:
        jobs = [job_store.refresh_job(job) for job in job_store.list_jobs(limit=20)]
        if json_output:
            console.print(json.dumps(jobs, ensure_ascii=False, indent=2))
        else:
            _print_jobs_table(jobs)
        return

    try:
        job = job_store.refresh_job(job_store.load_job(job_id))
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if json_output:
        console.print(json.dumps(job, ensure_ascii=False, indent=2))
        return

    _print_jobs_table([job])
    summary = job.get("summary") or {}
    if summary:
        console.print(
            f"[dim]Summary:[/dim] success={summary.get('success', 0)} "
            f"unverified={summary.get('unverified', 0)} missing={summary.get('missing', 0)}"
        )
    if job.get("status") == "needs_attention":
        console.print(f"[yellow]Resume:[/yellow] instsci jobs resume {job['id']}")


@jobs_app.command("tail")
def jobs_tail(
    job_id: str = typer.Argument(help="Job id."),
    lines: int = typer.Option(40, "--lines", "-n", min=1, help="Number of log lines per file."),
):
    """Print the latest broker logs and partial summary for a job."""
    from . import job_store

    try:
        job = job_store.refresh_job(job_store.load_job(job_id))
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    output_dir = Path(str(job.get("output_dir") or ""))
    partial_path = output_dir / "primary" / "summary_partial.json"
    if partial_path.exists():
        partial = json.loads(partial_path.read_text(encoding="utf-8"))
        console.print(f"[bold]Partial results:[/bold] {len(partial)} records ({partial_path})")

    for path in job_store.broker_log_paths(str(job.get("broker_publisher") or job.get("publisher") or "")):
        tail = job_store.read_tail(path, lines=lines)
        if not tail:
            continue
        console.print(f"\n[bold]{path}[/bold]")
        for line in tail:
            console.print(line)


@jobs_app.command("resume")
def jobs_resume(
    job_id: str = typer.Argument(help="Job id to resume."),
    output: str = typer.Option("", "--output", "-o", help="Output directory for the resumed run."),
    broker_ttl: int = typer.Option(259200, "--broker-ttl", help="Seconds to keep an auto-started broker alive."),
):
    """Submit a follow-up job for missing or unverified DOI records."""
    from . import job_store
    from .publisher_batch import PaperRecord
    from .publisher_profiles import get_publisher_profile

    try:
        job = job_store.refresh_job(job_store.load_job(job_id))
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    retry_payload = job_store.retry_records(job)
    if not retry_payload:
        console.print("[green]No missing or unverified records need resuming.[/green]")
        return

    broker_publisher = str(job.get("broker_publisher") or "").strip()
    if not broker_publisher:
        console.print("[red]Job is missing broker publisher metadata.[/red]")
        raise typer.Exit(1)

    cfg = Config.load()
    browser_profile = str(job.get("browser_profile") or "")
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    institution = str(job.get("institution") or _configured_subscription_institution(cfg))
    if not institution:
        console.print("[red]Job has no institution metadata. Pass a new papers command with --institution.[/red]")
        raise typer.Exit(1)
    institution_aliases = tuple(job.get("institution_aliases") or _configured_institution_aliases(cfg, institution))

    if not _ensure_session_broker(
        broker_publisher=broker_publisher,
        cfg=cfg,
        institution=institution,
        broker_ttl=broker_ttl,
    ):
        console.print(f"[red]Session broker did not start: {broker_publisher}[/red]")
        raise typer.Exit(1)

    old_output = Path(str(job.get("output_dir") or "runs"))
    run_dir = Path(output) if output else old_output.with_name(f"{old_output.name}_resume_{datetime.now():%Y%m%d_%H%M%S}")
    records = [PaperRecord(**record) for record in retry_payload]
    profile = get_publisher_profile(broker_publisher)
    resumed = _enqueue_papers_job(
        profile=profile,
        broker_publisher=broker_publisher,
        records=records,
        run_dir=run_dir,
        cfg=cfg,
        institution=institution,
        institution_aliases=institution_aliases,
        login_timeout=int(job.get("login_timeout") or 900),
        pdf_timeout=int(job.get("pdf_timeout") or 90),
        post_login_hold=int(job.get("post_login_hold") or 0),
        post_run_hold=int(job.get("post_run_hold") or 0),
        carsi_portal_preauth=bool(job.get("carsi_portal_preauth")),
        pause_on_blocker=bool(job.get("pause_on_blocker", True)),
        command=f"instsci jobs resume {job_id}",
        parent_job_id=job_id,
    )
    _print_job_submitted(resumed)


@jobs_app.command("cancel")
def jobs_cancel(job_id: str = typer.Argument(help="Queued job id to cancel.")):
    """Cancel a queued job record."""
    from . import job_store

    try:
        job = job_store.refresh_job(job_store.load_job(job_id), persist=False)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    status = str(job.get("status") or "")
    job = job_store.cancel_job(job)
    console.print(f"[green]Canceled job:[/green] {job['id']}")
    if status == "running":
        console.print("[yellow]The broker may already be processing this job. Use session-broker-stop if you need to stop the browser worker.[/yellow]")


@app.command("session-broker-status")
def session_broker_status(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
    json_output: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
):
    """Show a long-lived publisher browser session broker."""
    from . import session_broker

    state = session_broker.load_broker_state(publisher)
    running = session_broker.broker_is_running(publisher)
    payload = {
        "publisher": publisher,
        "status": "running" if running else "stopped",
        "pid": state.get("pid", "") if state else "",
        "profile_dir": state.get("profile_dir", "") if state else "",
        "queue_dir": state.get("queue_dir", "") if state else "",
        "heartbeat_at": state.get("heartbeat_at", "") if state else "",
    }
    if json_output:
        console.print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    table = Table(title="InstSci Session Broker")
    table.add_column("Publisher")
    table.add_column("Status")
    table.add_column("PID")
    table.add_column("Profile", overflow="fold")
    table.add_column("Queue", overflow="fold")
    table.add_row(
        str(payload["publisher"]),
        str(payload["status"]),
        str(payload["pid"]),
        str(payload["profile_dir"]),
        str(payload["queue_dir"]),
    )
    console.print(table)


@app.command("session-broker-stop")
def session_broker_stop(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
):
    """Ask a long-lived publisher broker to stop."""
    from . import session_broker

    session_broker.broker_stop_path(publisher).parent.mkdir(parents=True, exist_ok=True)
    session_broker.broker_stop_path(publisher).write_text("stop", encoding="utf-8")
    console.print(f"[green]Stop requested for broker:[/green] {publisher}")


@app.command("session-broker-run", hidden=True)
def session_broker_run(
    publisher: str = typer.Option(..., "--publisher", "-p"),
    browser_profile: str = typer.Option("", "--browser-profile"),
    institution: str = typer.Option("", "--institution"),
    ttl: int = typer.Option(259200, "--ttl"),
):
    """Run the long-lived broker loop. Internal command."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile
    from .session_broker import BrokerState, broker_dir, broker_stop_path, write_broker_state

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    institution = _resolve_subscription_institution(cfg, institution, prompt=False)
    institution_aliases = _configured_institution_aliases(cfg, institution)
    profile = get_publisher_profile(publisher)
    root = broker_dir(publisher)
    queue_dir = root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    state = BrokerState(
        publisher=publisher,
        profile_dir=cfg.chrome_profile_dir,
        pid=os.getpid(),
        queue_dir=str(queue_dir),
        started_at=datetime.now().isoformat(timespec="seconds"),
        ttl_seconds=ttl,
        heartbeat_at=datetime.now().isoformat(timespec="seconds"),
    )
    write_broker_state(state)
    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        institution_aliases=institution_aliases,
        login_timeout_sec=900,
        pdf_timeout_sec=90,
    )
    context = downloader._launch_context()
    deadline = time.time() + max(1, ttl)
    try:
        while time.time() < deadline and not broker_stop_path(publisher).exists():
            state.heartbeat_at = datetime.now().isoformat(timespec="seconds")
            write_broker_state(state)
            jobs = sorted(queue_dir.glob("*.json"))
            for job_path in jobs:
                if job_path.name.endswith(".done.json"):
                    continue
                try:
                    job = json.loads(job_path.read_text(encoding="utf-8"))
                    job["started_at"] = job.get("started_at") or datetime.now().isoformat(timespec="seconds")
                    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
                    run_dir = Path(str(job["output_dir"]))
                    primary_dir = run_dir / "primary"
                    primary_dir.mkdir(parents=True, exist_ok=True)
                    job_downloader = PublisherBatchDownloader(
                        cfg,
                        profile=profile,
                        institution_query=str(job.get("institution") or institution),
                        institution_aliases=tuple(job.get("institution_aliases") or institution_aliases),
                        login_timeout_sec=int(job.get("login_timeout") or 900),
                        pdf_timeout_sec=int(job.get("pdf_timeout") or 90),
                        post_login_hold_sec=int(job.get("post_login_hold") or 0),
                        post_run_hold_sec=int(job.get("post_run_hold") or 0),
                        carsi_portal_preauth=bool(job.get("carsi_portal_preauth")),
                        pause_on_blocker=bool(job.get("pause_on_blocker", True)),
                    )
                    job_downloader._preauthenticate_carsi_portal(context)
                    records = [PaperRecord(**record) for record in job.get("records", [])]
                    results = []
                    for index, record in enumerate(records, 1):
                        console.print(f"[dim]Broker {publisher}: {index}/{len(records)} start {record.doi}[/dim]")
                        result = job_downloader.fetch_one(context, record, primary_dir)
                        results.append(result)
                        job_downloader._write_results(primary_dir / "summary_partial.json", results)
                        status = "success" if result.ok and result.verified_match else (result.reason or result.state or "failed")
                        console.print(f"[dim]Broker {publisher}: {index}/{len(records)} {status} {record.doi}[/dim]")
                    job_downloader._write_results(primary_dir / "summary.json", results)
                    summary = job_downloader._write_complete_artifacts(records, results, run_dir)
                    summary["publisher"] = profile.name
                    summary["broker"] = True
                    summary["job_id"] = job.get("id", "")
                    summary["browser_profile_dir"] = cfg.chrome_profile_dir
                    (run_dir / "summary.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    (queue_dir / f"{job['id']}.done.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    payload = {"count": 0, "success": 0, "missing": 1, "unverified": 0, "error": f"{type(exc).__name__}: {exc}"}
                    done_name = f"{job_path.stem}.done.json"
                    (queue_dir / done_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                finally:
                    job_path.unlink(missing_ok=True)
            time.sleep(2)
    finally:
        try:
            context.close()
        except Exception:
            pass


@app.command("session-doctor")
def session_doctor(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher profile key to include publisher domains."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Inspect one browser profile instead of known candidates."),
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
):
    """Inspect local browser profiles for institution/publisher session presence."""
    from .profile_health import candidate_profile_dirs, configured_session_domains, inspect_browser_profile
    from .publisher_profiles import get_publisher_profile

    cfg = Config.load()
    profile = None
    domains = list(configured_session_domains(cfg))
    if publisher:
        try:
            profile = get_publisher_profile(publisher)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        domains.extend(profile.base_domains)
    domains = list(dict.fromkeys(domain for domain in domains if domain))

    profiles = [Path(browser_profile)] if browser_profile else candidate_profile_dirs(cfg, workspace=Path.cwd())
    reports = [inspect_browser_profile(path, domains) for path in profiles]

    table = Table(title="InstSci Browser Session Doctor")
    table.add_column("Profile", overflow="fold")
    table.add_column("Exists", width=8)
    table.add_column("Session Hosts", overflow="fold")
    table.add_column("Latest Expiry", overflow="fold")
    table.add_column("Notes", overflow="fold")
    for report in reports:
        present = []
        expiries = []
        seen_hosts: set[str] = set()
        for domain, info in report["domains"].items():
            latest = str(info.get("latest_expires_at") or "")
            if latest:
                expiries.append(f"{domain}: {latest}")
            for host in info.get("hosts", []):
                host_name = str(host.get("host") or "")
                if host_name in seen_hosts:
                    continue
                seen_hosts.add(host_name)
                count = int(host.get("cookie_count") or 0)
                if count:
                    session_count = int(host.get("session_cookie_count") or 0)
                    suffix = f", session={session_count}" if session_count else ""
                    present.append(f"{host_name}({count}{suffix})")
        notes = report.get("error") or ("cookie DB missing" if report["exists"] and not report["cookies_db_exists"] else "")
        table.add_row(
            report["profile_dir"],
            "yes" if report["exists"] else "no",
            ", ".join(present) or "-",
            ", ".join(expiries) or "-",
            notes,
        )
    console.print(table)

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "publisher": profile.name if profile else "",
            "domains": domains,
            "reports": reports,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")


@app.command("browser-doctor")
def browser_doctor(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher key for labeling and broker status, e.g. elsevier or wiley."),
    output: str = typer.Option("", "--output", "-o", help="Directory for inspection.json and screenshots."),
    browser_profile: str = typer.Option("", "--browser-profile", "--profile-dir", help="Only summarize windows matching this CloakBrowser user-data-dir."),
    json_report: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
):
    """Inspect visible InstSci CloakBrowser windows and save screenshot-backed state."""
    from .browser_doctor import inspect_cloakbrowser
    from .session_broker import BROKER_ROOT, broker_is_running, load_broker_state

    output_dir = Path(output) if output else Path.cwd() / f"browser_doctor_{datetime.now():%Y%m%d_%H%M%S}"
    report = inspect_cloakbrowser(output_dir=output_dir, publisher=publisher, browser_profile=browser_profile)

    broker_keys: list[str] = []
    if publisher.strip():
        broker_keys = [publisher.strip()]
    elif BROKER_ROOT.exists():
        broker_keys = [path.name for path in BROKER_ROOT.iterdir() if path.is_dir()]

    brokers = []
    for key in sorted(set(broker_keys)):
        state = load_broker_state(key) or {}
        brokers.append(
            {
                "publisher": key,
                "running": broker_is_running(key),
                "pid": int(state.get("pid") or 0),
                "profile_dir": str(state.get("profile_dir") or ""),
                "queue_dir": str(state.get("queue_dir") or ""),
                "heartbeat_at": str(state.get("heartbeat_at") or ""),
            }
        )
    report["brokers"] = brokers

    report_path = Path(str(report.get("output_dir") or output_dir)) / "inspection.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if json_report:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return

    console.print(f"[bold]Browser state:[/bold] {report.get('state')}")
    console.print(f"[bold]Recommendation:[/bold] {report.get('recommendation')}")
    console.print(f"[dim]Report: {report_path}[/dim]")

    windows = list(report.get("windows") or [])
    table = Table(title="InstSci CloakBrowser Windows")
    table.add_column("#", width=4)
    table.add_column("PID", width=8)
    table.add_column("Title", overflow="fold")
    table.add_column("URL", overflow="fold")
    table.add_column("Profile", overflow="fold")
    table.add_column("Match", width=6)
    table.add_column("Screenshot", overflow="fold")
    if windows:
        for item in windows:
            table.add_row(
                str(item.get("index") or ""),
                str(item.get("pid") or ""),
                str(item.get("title") or ""),
                str(item.get("url") or ""),
                str(item.get("profile_dir") or ""),
                "yes" if item.get("profile_match", True) else "no",
                str(item.get("screenshot") or ""),
            )
    else:
        table.add_row("-", "-", "-", "-", "-", "-", "-")
    console.print(table)

    if brokers:
        broker_table = Table(title="Publisher Session Brokers")
        broker_table.add_column("Publisher")
        broker_table.add_column("Running")
        broker_table.add_column("PID")
        broker_table.add_column("Profile", overflow="fold")
        broker_table.add_column("Heartbeat", overflow="fold")
        for broker in brokers:
            broker_table.add_row(
                str(broker["publisher"]),
                "yes" if broker["running"] else "no",
                str(broker["pid"] or ""),
                str(broker["profile_dir"] or ""),
                str(broker["heartbeat_at"] or ""),
            )
        console.print(broker_table)


@app.command("publisher-doctor")
def publisher_doctor(
    publisher: str = typer.Option("all", "--publisher", "-p", help="Publisher profile key, or 'all'."),
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
    matrix: bool = typer.Option(False, "--matrix", help="Show publisher capability matrix readiness instead of HTTP preflight."),
    json_report: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
    probe_pdf: bool = typer.Option(True, "--probe-pdf/--no-probe-pdf", help="Probe PDF candidate URLs without saving files."),
    max_candidates: int = typer.Option(4, "--max-candidates", min=0, max=10, help="Maximum PDF candidates to probe per publisher."),
    timeout: int = typer.Option(20, "--timeout", min=3, max=120, help="Network timeout in seconds."),
):
    """HTTP preflight to verify reusable publisher PDF routes.

    Browser-backed InstSci workflows are authoritative for publisher PDF
    capability verdicts; this command only checks route templates and blockers.
    """
    if matrix:
        from .publisher_matrix import build_publisher_matrix_report

        report = build_publisher_matrix_report(publisher)
        if output:
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        if json_report:
            console.print_json(json.dumps(report, ensure_ascii=False))
        else:
            _print_publisher_matrix_report(report)
            if output:
                console.print(f"[dim]Report: {output_path}[/dim]")
        return

    from .publisher_access import verify_publishers
    from .publisher_profiles import list_publisher_profiles

    keys = list_publisher_profiles() if publisher.strip().lower() == "all" else [publisher.strip()]
    console.print(f"[bold]Verifying publisher access assets:[/bold] {', '.join(keys)}")
    console.print(
        "[yellow]HTTP preflight only:[/yellow] use the built-in browser workflow "
        "for final publisher PDF capability verdicts."
    )
    results = verify_publishers(
        keys,
        probe_pdf=probe_pdf,
        max_candidates=max_candidates,
        timeout=timeout,
    )

    table = Table(title="Publisher Access Verification")
    table.add_column("Publisher", width=18)
    table.add_column("Landing", width=8)
    table.add_column("PDF Links", width=9, justify="right")
    table.add_column("Observed", width=22)
    table.add_column("Final Host", overflow="fold")
    needs_attention = False
    for result in results:
        if result["landing_status"] == 404 or not result["pdf_candidates"]:
            needs_attention = True
        table.add_row(
            result["profile_key"],
            str(result["landing_status"]),
            str(len(result["pdf_candidates"])),
            result["observed_access"],
            urlparse(result["landing_url"]).hostname or result["landing_url"],
        )
    console.print(table)

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")

    if json_report:
        console.print_json(json.dumps(results, ensure_ascii=False))

    if needs_attention:
        raise typer.Exit(2)


@app.command("identity-policy")
def identity_policy(
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
):
    """Show the institutional identity routing policy for publisher PDFs."""
    from .publisher_access import load_institutional_identity_policy

    policy = load_institutional_identity_policy()
    console.print("[bold]InstSci Institutional Identity Policy[/bold]")
    console.print(f"Default mode: [cyan]{policy['default_mode']}[/cyan]")
    console.print(f"Default identity: [cyan]{policy['default_identity']}[/cyan]")
    required = "required" if policy["subscription_institution"]["required_for_closed_access"] else "optional"
    console.print(f"Subscription institution: [cyan]{required}[/cyan]")
    console.print(f"Preferred off-campus access: [cyan]{policy['preferred_off_campus_access']}[/cyan]")
    console.print(f"Final PDF verdict requires: [cyan]{policy['final_pdf_verdict_requires']}[/cyan]")

    table = Table(title="Identity Route Order")
    table.add_column("Order", width=5, justify="right")
    table.add_column("Identity", width=22)
    table.add_column("Role", overflow="fold")
    table.add_column("Global default", width=14)
    for index, identity_key in enumerate(policy["identity_order"], 1):
        section_key = "webvpn" if identity_key == "webvpn_broker" else identity_key
        identity = policy["identities"].get(section_key, {})
        table.add_row(
            str(index),
            identity_key,
            str(identity.get("recommended_role", "")).replace("_", " "),
            "yes" if identity.get("global_default") else "no",
        )
    console.print(table)

    webvpn = policy["identities"]["webvpn"]
    console.print(
        "[yellow]WebVPN is optional:[/yellow] "
        f"{webvpn['persistence_limits']['cookie_store']['notes']}"
    )
    console.print(
        "[yellow]Use visible CloakBrowser:[/yellow] "
        "keep the same live context for SSO, CAPTCHA, Cloudflare, and PDF-token flows."
    )

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")


@evidence_app.command("policy")
def evidence_policy(
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
):
    """Show which data belongs in the public package and private evidence index."""
    from .evidence_store import load_public_data_policy, private_evidence_root

    policy = load_public_data_policy()
    cfg = Config.load()
    payload = {**policy, "private_evidence_root": str(private_evidence_root(cfg))}
    if format.lower() == "json":
        console.print_json(data=payload)
        return
    if format.lower() != "text":
        console.print("[red]--format must be text or json.[/red]")
        raise typer.Exit(2)
    console.print("[bold]Public assets[/bold]")
    for name in payload["public_assets"]:
        console.print(f"  - {name}")
    console.print(f"[bold]Private evidence root:[/bold] {payload['private_evidence_root']}")
    console.print("[yellow]Private index is reference-only; PDFs, cookies, and browser profiles are not copied.[/yellow]")


@evidence_app.command("register")
def evidence_register(
    run_dir: Path = typer.Argument(help="Existing private InstSci run directory."),
    publisher: str = typer.Option("", "--publisher", "-p", help="Optional publisher key."),
    private_comment: str = typer.Option("", "--private-comment", help="Optional private operator comment."),
):
    """Register a private run by path and manifest hash without copying artifacts."""
    from .evidence_store import register_private_run

    try:
        entry = register_private_run(Config.load(), run_dir, publisher=publisher, notes=private_comment)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Could not register private evidence: {exc}[/red]")
        raise typer.Exit(2)
    console.print(f"[green]Registered private evidence:[/green] {entry['id']}")
    console.print(f"[dim]Index: {entry['index_path']}[/dim]")


@evidence_app.command("list")
def evidence_list(
    format: str = typer.Option("table", "--format", help="Output format: table or json."),
):
    """List references in the external private-evidence index."""
    from .evidence_store import load_private_index

    payload = load_private_index(Config.load())
    if format.lower() == "json":
        console.print_json(data=payload)
        return
    if format.lower() != "table":
        console.print("[red]--format must be table or json.[/red]")
        raise typer.Exit(2)
    table = Table(title=f"Private Evidence Runs ({len(payload['runs'])})")
    table.add_column("ID")
    table.add_column("Publisher")
    table.add_column("Registered")
    table.add_column("Manifest SHA-256", max_width=18)
    for entry in payload["runs"]:
        table.add_row(
            str(entry.get("id") or ""),
            str(entry.get("publisher") or ""),
            str(entry.get("registered_at") or ""),
            str(entry.get("manifest_sha256") or "")[:16],
        )
    console.print(table)


@app.command()
def search(
    query: str = typer.Argument(help="Search query."),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results."),
    year: str = typer.Option("", "--year", "-y", help="Year range, e.g., '2020-2024' or '2020-'."),
    sources: str = typer.Option("semantic_scholar,openalex,crossref", "--sources", help="Comma-separated metadata sources."),
    do_fetch: bool = typer.Option(False, "--fetch", help="Also fetch full text for results with DOIs."),
    output: str = typer.Option("", "--output", "-o", help="Write structured search results to a .json or .csv file."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Search and merge paper metadata from multiple scholarly indexes."""
    _setup_logging(verbose)

    console.print(f"[bold]Searching:[/bold] {query}")
    try:
        config = Config.load()
        search_response = multi_search.search_with_status(
            query,
            limit=limit,
            year_range=year or None,
            sources=sources,
            email=config.email,
        )
        results = search_response.results
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    for source_name, status in search_response.source_status.items():
        if status.get("status") != "success":
            console.print(
                f"[yellow]{source_name}: {status.get('status')}"
                f"{f' ({status.get('detail')})' if status.get('detail') else ''}[/yellow]"
            )

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit(0)

    # Display results in a table
    table = Table(title=f"Search Results ({len(results)})")
    table.add_column("#", style="dim", width=3)
    table.add_column("Year", width=5)
    table.add_column("Title", max_width=60)
    table.add_column("Authors", max_width=30)
    table.add_column("DOI", max_width=25)
    table.add_column("Cites by source", max_width=32)
    table.add_column("Sources", max_width=28)

    for i, r in enumerate(results, 1):
        authors_str = ", ".join(r.authors[:3])
        if len(r.authors) > 3:
            authors_str += " et al."
        table.add_row(
            str(i),
            str(r.year or ""),
            r.title[:60],
            authors_str[:30],
            r.doi[:25] if r.doi else r.arxiv_id[:25] if r.arxiv_id else "",
            "; ".join(f"{name}: {count}" for name, count in r.citation_counts.items()),
            ", ".join(getattr(r, "sources", []) or ["semantic_scholar"]),
        )

    console.print(table)

    if output:
        try:
            payload = build_search_payload(
                query,
                results,
                year_range=year,
                source="multi_source",
                source_status=search_response.source_status,
            )
            output_path = write_search_payload(payload, output)
        except (OSError, ValueError) as exc:
            console.print(f"[red]Could not write search results: {exc}[/red]")
            raise typer.Exit(2)
        console.print(f"[green]Search results:[/green] {output_path}")

    # Optionally fetch full texts
    if do_fetch:
        fetchable = [r for r in results if r.doi or r.arxiv_id]
        if fetchable:
            console.print(f"\n[bold]Fetching {len(fetchable)} papers...[/bold]")
            fetcher = PaperFetcher(config)
            try:
                for r in fetchable:
                    identifier = r.doi or f"arxiv:{r.arxiv_id}"
                    console.print(f"  Fetching: {identifier}")
                    try:
                        paper = fetcher.fetch(identifier)
                        status = "[green]OK[/green]" if paper.full_text else "[yellow]No text[/yellow]"
                        console.print(f"    {status}")
                    except Exception as e:
                        console.print(f"    [red]Error: {e}[/red]")
            finally:
                fetcher.close()


@app.command("select")
def select_search_results(
    search_file: Path = typer.Argument(help="Structured search results from 'instsci search --output'."),
    indices: str = typer.Option("", "--indices", "-i", help="One-based result indices, e.g. '1,3-5'; omit to select all DOI records."),
    output: Path = typer.Option(Path("selected_dois.txt"), "--output", "-o", help="DOI file for 'instsci papers'."),
):
    """Select DOI-bearing search results and create an auditable papers input file."""
    try:
        payload = load_search_payload(search_file)
        results = payload.get("results") or []
        selected_indices = parse_selection_indices(indices, len(results))
        doi_path, report_path, report = write_selection(
            search_file,
            payload,
            selected_indices,
            output,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Could not select search results: {exc}[/red]")
        raise typer.Exit(2)

    console.print(
        f"[green]Selected {report['selected_count']} unique DOI records[/green] "
        f"({report['skipped_count']} skipped)."
    )
    console.print(f"[dim]DOI file: {doi_path}[/dim]")
    console.print(f"[dim]Selection report: {report_path}[/dim]")


@app.command()
def cache(
    action: str = typer.Argument(help="Action: 'clear' to remove cached results."),
):
    """Manage the paper cache."""
    if action == "clear":
        config = Config.load()
        fetcher = PaperFetcher(config)
        fetcher.clear_cache()
        console.print("[green]Cache cleared.[/green]")
    else:
        console.print(f"[red]Unknown action: {action}. Use 'clear'.[/red]")
        raise typer.Exit(1)


@app.command()
def schools(
    query: str = typer.Argument("", help="Search query (name, province, or host). Omit to list all."),
):
    """List or search supported universities."""
    if query:
        results = search_schools(query)
    else:
        results = list_schools()

    if not results:
        console.print(f"[yellow]No schools found matching '{query}'.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Supported Schools ({len(results)})")
    table.add_column("#", style="dim", width=4)
    table.add_column("Province", width=10)
    table.add_column("School", max_width=25)
    table.add_column("Type", width=12)
    table.add_column("Host", max_width=40)
    table.add_column("Custom Key", width=5, justify="center")

    from .schools import WEBVPN_DEFAULT_KEY
    for i, s in enumerate(results, 1):
        has_custom = "Y" if s.key != WEBVPN_DEFAULT_KEY else ""
        type_label = {
            "webvpn": "CampusPortal",
            "easyconnect": "CampusConnector",
            "atrust": "CampusConnector",
            "ezproxy": "LibraryPortal",
        }.get(s.school_type, s.school_type)
        table.add_row(str(i), s.province, s.name, type_label, s.host, has_custom)

    console.print(table)


@app.command()
def config_cmd(
    show: bool = typer.Option(True, "--show", help="Show current config."),
    set_email: str = typer.Option("", "--email", help="Set email for Unpaywall API."),
    set_output: str = typer.Option("", "--output-dir", help="Set default output directory."),
    set_access_url: str = typer.Option("", "--access-url", help="Set institutional access gateway URL."),
    set_webvpn_url: str = typer.Option("", "--webvpn-url", help="Legacy gateway URL option.", hidden=True),
    set_school: str = typer.Option("", "--school", help="Set school (use 'instsci schools' to list)."),
    set_institution_cn: str = typer.Option("", "--institution-cn", "--school-cn", help="Set institution Chinese/local name for publisher login matching."),
    set_institution_en: str = typer.Option("", "--institution-en", "--school-en", help="Set institution English name for publisher login matching."),
    set_connector_url: str = typer.Option("", "--connector-url", help="Set local SOCKS5 connector URL for EasyConnect."),
    set_proxy_url: str = typer.Option("", "--proxy-url", help="Legacy local connector URL option.", hidden=True),
    set_elsevier_key: str = typer.Option("", "--elsevier-api-key", help="Set Elsevier API key."),
    set_elsevier_token: str = typer.Option("", "--elsevier-inst-token", help="Set Elsevier institutional token."),
    set_federated_enable: bool = typer.Option(False, "--federated-enable", help="Enable federated institutional auth."),
    set_federated_disable: bool = typer.Option(False, "--federated-disable", help="Disable federated institutional auth."),
    set_federated_school: str = typer.Option("", "--federated-school", help="Set school name for federated login."),
    set_carsi_enable: bool = typer.Option(False, "--carsi-enable", help="Legacy federated auth option.", hidden=True),
    set_carsi_disable: bool = typer.Option(False, "--carsi-disable", help="Legacy federated auth option.", hidden=True),
    set_carsi_school: str = typer.Option("", "--carsi-school", help="Legacy federated school option.", hidden=True),
):
    """View or update configuration."""
    cfg = Config.load()
    changed = False

    if set_email:
        cfg.email = set_email
        changed = True
        console.print(f"[green]Email set to: {set_email}[/green]")

    if set_output:
        cfg.output_dir = set_output
        changed = True
        console.print(f"[green]Output dir set to: {set_output}[/green]")

    access_url = set_access_url or set_webvpn_url
    if access_url:
        cfg.webvpn_base_url = access_url.rstrip("/")
        changed = True
        console.print(f"[green]Institutional access URL set to: {access_url}[/green]")

    if set_school:
        try:
            entry = _apply_school_config(cfg, set_school)
            changed = True
            type_label = _school_type_label(entry.school_type)
            console.print(f"[green]School set to: {entry.name} ({type_label}, {entry.host})[/green]")
            if entry.school_type == "easyconnect":
                console.print("[yellow]This school uses a local campus connector. Please:[/yellow]")
                console.print("  1. Connect via zju-connect: [cyan]zju-connect -server {0}[/cyan]".format(entry.host))
                console.print("  2. Set connector: [cyan]instsci config-cmd --connector-url socks5://127.0.0.1:1080[/cyan]")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    if set_institution_cn:
        cfg.institution_name_zh = set_institution_cn
        if not cfg.carsi_idp_name:
            cfg.carsi_idp_name = set_institution_cn
        cfg.carsi_enabled = True
        changed = True
        console.print(f"[green]Institution Chinese/local name set to: {set_institution_cn}[/green]")

    if set_institution_en:
        cfg.institution_name_en = set_institution_en
        if not cfg.carsi_idp_name:
            cfg.carsi_idp_name = set_institution_en
        cfg.carsi_enabled = True
        changed = True
        console.print(f"[green]Institution English name set to: {set_institution_en}[/green]")

    connector_url = set_connector_url or set_proxy_url
    if connector_url:
        cfg.proxy_url = connector_url
        changed = True
        console.print(f"[green]Connector URL set to: {connector_url}[/green]")

    if set_elsevier_key:
        cfg.elsevier_api_key = set_elsevier_key
        changed = True
        console.print("[green]Elsevier API key saved.[/green]")

    if set_elsevier_token:
        cfg.elsevier_inst_token = set_elsevier_token
        changed = True
        console.print("[green]Elsevier institutional token saved.[/green]")

    federated_enable = set_federated_enable or set_carsi_enable
    federated_disable = set_federated_disable or set_carsi_disable
    federated_school = set_federated_school or set_carsi_school

    if federated_enable:
        cfg.carsi_enabled = True
        changed = True
        console.print("[green]Federated institutional auth enabled.[/green]")

    if federated_disable:
        cfg.carsi_enabled = False
        changed = True
        console.print("[yellow]Federated institutional auth disabled.[/yellow]")

    if federated_school:
        cfg.carsi_idp_name = federated_school
        changed = True
        console.print(f"[green]Federated login school set to: {federated_school}[/green]")

    if changed:
        cfg.save()

    has_setter = any([set_email, set_output, set_access_url, set_webvpn_url, set_school,
                      set_institution_cn, set_institution_en,
                      set_connector_url, set_proxy_url,
                       set_elsevier_key, set_elsevier_token,
                       set_federated_enable, set_federated_disable, set_federated_school,
                       set_carsi_enable, set_carsi_disable, set_carsi_school])
    if show and not has_setter:
        # Determine school type
        try:
            from .schools import get_school as _get_school
            school_entry = _get_school(cfg.school)
            school_type = school_entry.school_type
        except ValueError:
            school_type = "unknown"

        console.print("[bold]Current configuration:[/bold]")
        console.print(f"  School:            {cfg.school} ({school_type})")
        console.print(f"  Access URL:        {_access_url(cfg)}")
        console.print(f"  Connector URL:     {cfg.proxy_url or '(not set)'}")
        console.print(f"  Email:             {cfg.email}")
        console.print(f"  Elsevier API key:  {'****' if cfg.elsevier_api_key else '(not set)'}")
        console.print(f"  Elsevier inst tok: {'****' if cfg.elsevier_inst_token else '(not set)'}")
        console.print(f"  Federated login:   {'Yes' if cfg.carsi_enabled else 'No'}")
        console.print(f"  Federated school:  {cfg.carsi_idp_name or '(not set)'}")
        console.print(f"  Institution EN:    {cfg.institution_name_en or '(not set)'}")
        console.print(f"  Institution CN:    {cfg.institution_name_zh or '(not set)'}")
        console.print(f"  Output dir:        {cfg.output_dir}")
        console.print(f"  Cache dir:         {cfg.cache_dir}")
        console.print(f"  Cookie path:       {cfg.cookie_path}")


def _run_federated_login(
    publisher: str,
    url: str,
    force: bool,
    verbose: bool,
) -> None:
    """Run the federated institutional login flow."""
    _setup_logging(verbose)
    config = Config.load()

    if not config.carsi_enabled:
        console.print("[red]Federated login is not enabled. Run: instsci config-cmd --federated-enable --federated-school \"你的学校名\"[/red]")
        raise typer.Exit(1)

    if not config.carsi_idp_name:
        console.print("[red]Federated login school not set. Run: instsci config-cmd --federated-school \"你的学校名\"[/red]")
        raise typer.Exit(1)

    if not publisher and url:
        from .carsi import detect_publisher
        publisher = detect_publisher(url) or ""

    if not publisher:
        console.print("[yellow]Available publishers:[/yellow]")
        console.print("  sciencedirect, springer, wiley, ieee, tandfonline, nature")
        publisher = typer.prompt("Enter publisher name")

    from .carsi import CARSIClient
    carsi = CARSIClient(config)
    try:
        console.print(f"[bold]Federated login for: {publisher}[/bold]")
        console.print(f"[dim]School: {config.carsi_idp_name}[/dim]")
        if carsi.login(publisher, force=force):
            console.print("[green]Federated access session established![/green]")
        else:
            console.print("[red]Federated login failed.[/red]")
            raise typer.Exit(1)
    finally:
        carsi.close()


@app.command("federated-login")
def federated_login(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher (sciencedirect, springer, wiley, ieee, tandfonline, nature). Omit to pick from article URL."),
    url: str = typer.Option("", "--url", "-u", help="Article URL to auto-detect publisher."),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Authenticate via federated institutional login."""
    _run_federated_login(publisher, url, force, verbose)


@app.command("carsi-login", hidden=True)
def carsi_login(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher (sciencedirect, springer, wiley, ieee, tandfonline, nature). Omit to pick from article URL."),
    url: str = typer.Option("", "--url", "-u", help="Article URL to auto-detect publisher."),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Legacy alias for federated-login."""
    _run_federated_login(publisher, url, force, verbose)


@app.command()
def elsevier_setup(
    api_key: str = typer.Option("", "--api-key", help="Global Elsevier API key saved in the InstSci config."),
    inst_token: str = typer.Option("", "--inst-token", help="Global Elsevier institutional token, if your library provides one."),
    validate: bool = typer.Option(False, "--validate", help="Validate the saved global Elsevier API configuration."),
    test_doi: str = typer.Option(
        "10.1016/j.watres.2024.121507",
        "--test-doi",
        help="Validation-only Elsevier DOI. This does not bind the config to one article.",
    ),
):
    """Save the global Elsevier API config for ScienceDirect XML/object-eid PDF download.

    Get a free key at: https://dev.elsevier.com/
    """
    cfg = Config.load()

    if api_key:
        cfg.elsevier_api_key = api_key
        cfg.save()
        console.print("[green]Global Elsevier API key saved.[/green]")

    if inst_token:
        cfg.elsevier_inst_token = inst_token
        cfg.save()
        console.print("[green]Global Elsevier institutional token saved.[/green]")

    key = cfg.elsevier_api_key
    if not key:
        console.print("[yellow]No Elsevier API key configured.[/yellow]")
        console.print()
        console.print("Configure the project-wide Elsevier API key before testing ScienceDirect API retrieval:")
        console.print("  1. Go to [cyan]https://dev.elsevier.com/[/cyan]")
        console.print("  2. Register or sign in")
        console.print("  3. My API Key / API Key Settings -> create an API key")
        console.print("  4. If prompted, choose ScienceDirect / Article Retrieval permissions")
        console.print("  5. Run once: [cyan]instsci elsevier-setup --api-key YOUR_KEY --validate[/cyan]")
        console.print()
        console.print("Institutional token is optional and should be configured only if your library provides it:")
        console.print("  [cyan]instsci elsevier-setup --api-key KEY --inst-token TOKEN[/cyan]")
        raise typer.Exit(1 if validate else 0)

    if validate:
        from .sources import elsevier_api

        console.print("[bold]Validating global Elsevier XML/object-eid PDF retrieval...[/bold]")
        if cfg.proxy_url:
            console.print("[dim]Route order: direct first, configured connector fallback.[/dim]")
        else:
            console.print("[dim]Route order: direct route.[/dim]")
        console.print(f"[dim]Validation DOI only: {test_doi}[/dim]")

        data = elsevier_api.fetch_fulltext(
            test_doi,
            api_key=key,
            inst_token=cfg.elsevier_inst_token,
            proxy_url=cfg.proxy_url,
        )
        if not data:
            console.print("[red]XML retrieval failed.[/red]")
            console.print(
                "[yellow]Check that the API key is valid and that api.elsevier.com "
                "uses your campus, library VPN, rule VPN, or institutional exit.[/yellow]"
            )
            raise typer.Exit(2)

        eids = data.get("pdf_eids", [])
        route = data.get("api_route", "")
        console.print(f"[green]XML retrieval: OK[/green] route={route or 'unknown'}")
        console.print(f"  Title: {data.get('title') or '(unknown)'}")
        console.print(f"  MAIN PDF object EIDs: {len(eids)}")
        if not eids:
            console.print("[red]No MAIN PDF object EID found in XML.[/red]")
            raise typer.Exit(2)

        pdf = elsevier_api.fetch_pdf(
            test_doi,
            api_key=key,
            inst_token=cfg.elsevier_inst_token,
            proxy_url=cfg.proxy_url,
            pdf_eids=eids,
            preferred_route=route,
        )
        if not pdf:
            console.print("[red]Object PDF retrieval failed.[/red]")
            console.print(
                "[yellow]If XML worked but object/eid failed, the current route is usually "
                "not entitled for this closed-access PDF. Prefer direct campus/rule VPN "
                "routing before configured connector fallback.[/yellow]"
            )
            raise typer.Exit(2)

        console.print(f"[green]Object PDF retrieval: OK ({len(pdf)} bytes)[/green]")

    console.print()
    console.print(f"  API Key:        {_mask_secret(key)}")
    console.print(f"  Inst Token:     {'****' if cfg.elsevier_inst_token else '(not set)'}")


if __name__ == "__main__":
    app()
