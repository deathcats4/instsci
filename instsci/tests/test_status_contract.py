from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from instsci import cli
from instsci.cli import (
    _build_workflow_plan,
    _load_manifest_rows,
    _normalize_manifest_row,
    _write_papers_manifest,
    _write_pending_browser_manifest,
    _write_unsupported_publisher_manifest,
    _write_unresolved_browser_manifest,
)
from instsci import browser_doctor
from instsci.publisher_batch import PublisherBatchDownloader
from instsci.publisher_pdf_router import belongs_to_current_article, build_pdf_candidates
from instsci.publisher_profiles import get_publisher_profile, infer_publisher_profile
from instsci.publisher_matrix import (
    PublisherMatrixEntry,
    build_publisher_matrix_report,
    get_publisher_matrix_entry,
    load_publisher_matrix,
    manifest_next_action,
    manifest_suggested_paths,
    manifest_workflow,
    normalize_failure_status,
)


class StatusContractTests(unittest.TestCase):
    def test_cli_manifest_normalizes_legacy_status_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            summary = _write_papers_manifest(
                tmp_path,
                [
                    {
                        "doi": "10.0000/example",
                        "published": "",
                        "title": "Example",
                        "status": "legacy_missing",
                        "standard_status": "captcha_or_waf",
                        "result_evidence": "legacy_evidence",
                        "verified_match": False,
                    }
                ],
            )

            rows = json.loads((tmp_path / "complete" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(rows[0]["status"], "missing")
            self.assertEqual(rows[0]["file_status"], "missing")
            self.assertEqual(rows[0]["standard_status"], "human_verification_required")
            self.assertEqual(rows[0]["result_evidence"], "not_verified")
            self.assertEqual(summary["standard_status_counts"], {"human_verification_required": 1})
            self.assertEqual(summary["result_evidence_counts"], {"not_verified": 1})

    def test_success_manifest_row_defaults_to_success_contract(self) -> None:
        row = _normalize_manifest_row(
            {
                "doi": "10.0000/example",
                "status": "success",
                "result_evidence": "browser_verified",
            }
        )

        self.assertEqual(row["status"], "success")
        self.assertEqual(row["file_status"], "success")
        self.assertEqual(row["standard_status"], "success")
        self.assertEqual(row["result_evidence"], "browser_verified")
        self.assertEqual(row["next_action"], "none")
        self.assertEqual(row["suggested_paths"], ["zotero_sync"])
        self.assertIs(row["verified_match"], True)

    def test_load_manifest_rows_ignores_empty_manifest_path(self) -> None:
        self.assertEqual(_load_manifest_rows({}), [])
        self.assertEqual(_load_manifest_rows({"manifest": ""}), [])
        self.assertEqual(_load_manifest_rows({"manifest": None}), [])

    def test_publisher_batch_manifest_item_normalizes_unknown_values(self) -> None:
        row = PublisherBatchDownloader._normalize_manifest_item(
            {
                "doi": "10.0000/example",
                "status": "unexpected",
                "standard_status": "unsupported",
                "result_evidence": "html_probe",
                "verified_match": False,
            }
        )

        self.assertEqual(row["status"], "missing")
        self.assertEqual(row["file_status"], "missing")
        self.assertEqual(row["standard_status"], "capture_failed")
        self.assertEqual(row["result_evidence"], "not_verified")
        self.assertEqual(row["next_action"], "rerun_with_mode_diagnose")

    def test_failure_status_mapping_keeps_verification_and_waf_distinct(self) -> None:
        self.assertEqual(normalize_failure_status(state="captcha_or_waf"), "human_verification_required")
        self.assertEqual(normalize_failure_status(reason="verify you are human"), "human_verification_required")
        self.assertEqual(normalize_failure_status(reason="Cloudflare Ray ID loop"), "waf_blocked")
        self.assertEqual(normalize_failure_status(reason="CPE00001 problem providing the content"), "publisher_error")
        self.assertEqual(normalize_failure_status(reason="not entitled purchase access"), "access_unavailable")

    def test_next_actions_cover_standard_statuses(self) -> None:
        statuses = {
            "success": "none",
            "auth_required": "complete_institution_login_in_visible_browser_then_retry",
            "human_verification_required": "complete_visible_human_verification_then_retry",
            "waf_blocked": "stop_batch_and_retry_later_or_use_manual_browser",
            "access_unavailable": "check_access_in_regular_browser_or_library_subscription",
            "publisher_error": "retry_later_or_test_another_doi",
            "pdf_candidate_conflict": "rerun_in_diagnose_mode_and_inspect_pdf_candidates",
            "browser_group_pending": "split_doi_list_by_publisher_then_rerun",
            "unsupported_publisher": "add_or_update_publisher_profile_before_retry",
            "ambiguous_search_result": "inspect_visible_search_results_and_select_manually",
            "daily_limit_reached": "review_configured_download_policy_or_resume_next_local_day",
            "quota_state_error": "inspect_or_repair_local_quota_state_before_retry",
        }
        for status, expected in statuses.items():
            with self.subTest(status=status):
                self.assertEqual(manifest_next_action(status), expected)

    def test_suggested_paths_cover_standard_statuses(self) -> None:
        expectations = {
            "success": ["zotero_sync"],
            "auth_required": ["complete_institution_login", "rerun_same_browser_profile"],
            "human_verification_required": ["complete_visible_human_verification", "rerun_same_browser_profile"],
            "waf_blocked": ["stop_batch", "retry_later", "manual_browser_single_doi"],
            "access_unavailable": ["oa_retry", "library_resolver", "ill_request", "author_email"],
            "publisher_error": ["retry_later", "test_another_doi", "oa_retry"],
            "pdf_candidate_conflict": ["diagnose_pdf_candidates", "manual_select_main_pdf"],
            "browser_group_pending": ["publisher_doctor_matrix", "split_by_publisher", "rerun_by_publisher", "workflow_plan"],
            "unsupported_publisher": ["add_publisher_profile", "oa_retry", "library_resolver", "ill_request"],
            "ambiguous_search_result": ["manual_browser_single_doi", "rerun_diagnose"],
            "daily_limit_reached": ["stop_batch", "review_local_policy", "retry_next_day"],
            "quota_state_error": ["inspect_local_state", "stop_batch"],
        }
        for status, expected in expectations.items():
            with self.subTest(status=status):
                self.assertEqual(manifest_suggested_paths(status), expected)
                self.assertEqual(manifest_workflow(status)["suggested_paths"], expected)

    def test_prewarm_next_action_only_applies_after_status_rules(self) -> None:
        entry = SimpleNamespace(prewarm=True)
        self.assertEqual(
            manifest_next_action("capture_failed", entry),
            "run_single_doi_prewarm_with_same_browser_profile",
        )
        self.assertEqual(
            manifest_suggested_paths("capture_failed", entry)[0],
            "single_doi_prewarm",
        )
        self.assertEqual(
            manifest_next_action("waf_blocked", entry),
            "stop_batch_and_retry_later_or_use_manual_browser",
        )

    def test_workflow_plan_excludes_success_by_default(self) -> None:
        manifest_path = Path("manifest.json")
        report = _build_workflow_plan(
            manifest_path,
            [
                {"doi": "10.0000/success", "status": "success", "standard_status": "success"},
                {"doi": "10.0000/no-access", "status": "missing", "standard_status": "access_unavailable"},
            ],
        )

        self.assertEqual(report["schema"], "instsci.workflow_plan.v1")
        self.assertEqual(report["summary"]["rows"], 2)
        self.assertEqual(report["summary"]["items"], 1)
        self.assertEqual(report["items"][0]["doi"], "10.0000/no-access")
        self.assertIn("ill_request", report["items"][0]["suggested_paths"])

    def test_pending_browser_manifest_keeps_oa_success_and_splits_publishers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            oa_pdf = tmp_path / "oa.pdf"
            oa_pdf.write_bytes(b"%PDF-1.4\n")
            summary = _write_pending_browser_manifest(
                tmp_path,
                oa_rows=[
                    {
                        "doi": "10.7554/eLife.12345",
                        "published": "2024",
                        "title": "OA paper",
                        "status": "success",
                        "file_status": "success",
                        "standard_status": "success",
                        "result_evidence": "oa_direct",
                        "reason": "oa_first",
                        "pdf_path": str(oa_pdf),
                        "pdf_url": "https://example.org/oa.pdf",
                        "diagnostic_path": "",
                        "next_action": "none",
                        "size_bytes": oa_pdf.stat().st_size,
                        "text_length": 100,
                        "verified_match": True,
                    }
                ],
                browser_records=[
                    SimpleNamespace(doi="10.1021/acs.example", published="2023", title="ACS paper"),
                    SimpleNamespace(doi="10.1016/j.example.2023.01.001", published="2023", title="Elsevier paper"),
                ],
                mode="user",
                oa_first=True,
                reason="mixed_publisher_browser_queue_not_attempted",
            )

            manifest_path = tmp_path / "complete" / "manifest.json"
            rows = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["count"], 3)
            self.assertEqual(summary["standard_status_counts"], {"success": 1, "browser_group_pending": 2})
            self.assertEqual(summary["browser_queue_status"], "mixed_publisher_browser_queue_not_attempted")
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["standard_status"], "success")
            pending_rows = [row for row in rows if row["standard_status"] != "success"]
            self.assertEqual(len(pending_rows), 2)
            for row in pending_rows:
                self.assertEqual(row["standard_status"], "browser_group_pending")
                self.assertEqual(row["next_action"], "split_doi_list_by_publisher_then_rerun")
                self.assertIn("rerun_by_publisher", row["suggested_paths"])
                self.assertTrue(row["diagnostic_path"])
                self.assertTrue(Path(row["diagnostic_path"]).exists())

            group_files = sorted((tmp_path / "browser_groups").glob("*_dois.txt"))
            self.assertGreaterEqual(len(group_files), 2)
            report = _build_workflow_plan(manifest_path, rows)
            self.assertEqual(report["summary"]["items"], 2)
            self.assertTrue(all("rerun_by_publisher" in item["suggested_paths"] for item in report["items"]))

    def test_unresolved_browser_manifest_writes_pending_rows_when_broker_has_no_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            summary = _write_unresolved_browser_manifest(
                tmp_path,
                oa_rows=[],
                browser_records=[
                    SimpleNamespace(doi="10.1007/s00126-019-00903-6", published="", title=""),
                    SimpleNamespace(doi="10.1007/s00126-018-0810-3", published="", title=""),
                ],
                mode="user",
                oa_first=False,
                publisher_matrix={"key": "springer-nature", "status": "ready"},
                reason="browser_workflow_no_manifest",
            )

            manifest_path = tmp_path / "complete" / "manifest.json"
            rows = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["missing"], 2)
            self.assertEqual(summary["browser_queue_status"], "browser_workflow_no_manifest")
            self.assertEqual(rows[0]["standard_status"], "capture_failed")
            self.assertEqual(rows[0]["reason"], "browser_workflow_no_manifest")
            self.assertEqual(rows[0]["next_action"], "rerun_with_mode_diagnose")

    def test_browser_exception_manifest_writes_diagnostic_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            try:
                raise TimeoutError("Broker job timed out after 240s: example")
            except TimeoutError as exc:
                summary = cli._write_browser_exception_manifest(
                    tmp_path,
                    oa_rows=[],
                    browser_records=[
                        SimpleNamespace(doi="10.1021/acs.est.6b05706", published="", title=""),
                    ],
                    mode="user",
                    oa_first=True,
                    publisher_matrix={"publisher": "acs"},
                    reason="browser_broker_timeout",
                    exc=exc,
                )

            rows = json.loads((tmp_path / "complete" / "manifest.json").read_text(encoding="utf-8"))
            diagnostic_path = Path(rows[0]["diagnostic_path"])
            self.assertEqual(summary["browser_queue_status"], "browser_broker_timeout")
            self.assertEqual(rows[0]["standard_status"], "capture_failed")
            self.assertEqual(rows[0]["reason"], "browser_broker_timeout")
            self.assertTrue(diagnostic_path.exists())
            packet = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            self.assertEqual(packet["schema"], "instsci.browser_exception.v1")
            self.assertEqual(packet["exception_type"], "TimeoutError")

    def test_unsupported_publisher_manifest_writes_plannable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            summary = _write_unsupported_publisher_manifest(
                tmp_path,
                oa_rows=[],
                browser_records=[
                    SimpleNamespace(doi="10.7554/elife.32822", published="", title=""),
                ],
                mode="user",
                oa_first=True,
                reason="publisher_auto_inference_failed",
            )

            manifest_path = tmp_path / "complete" / "manifest.json"
            rows = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["count"], 1)
            self.assertEqual(summary["missing"], 1)
            self.assertEqual(summary["standard_status_counts"], {"unsupported_publisher": 1})
            self.assertEqual(rows[0]["publisher"], "unknown")
            self.assertEqual(rows[0]["standard_status"], "unsupported_publisher")
            self.assertEqual(rows[0]["next_action"], "add_or_update_publisher_profile_before_retry")
            self.assertIn("add_publisher_profile", rows[0]["suggested_paths"])

    def test_springer_pdf_candidates_keep_springerlink_and_nature_routes_separate(self) -> None:
        profile = get_publisher_profile("springer")
        springer_doi = "10.1007/s00126-019-00903-6"
        springer_candidates = build_pdf_candidates(
            profile,
            springer_doi,
            source_url=f"https://link.springer.com/article/{springer_doi}",
        )
        self.assertTrue(any("link.springer.com/content/pdf/" in url for url in springer_candidates))
        self.assertFalse(any("nature.com/articles/" in url for url in springer_candidates))
        self.assertFalse(
            belongs_to_current_article(
                profile,
                "https://www.nature.com/articles/s00126-019-00903-6.pdf",
                doi=springer_doi,
                source_url=f"https://link.springer.com/article/{springer_doi}",
            )
        )

        nature_doi = "10.1038/s41586-020-2649-2"
        nature_candidates = build_pdf_candidates(
            profile,
            nature_doi,
            source_url="https://www.nature.com/articles/s41586-020-2649-2",
        )
        self.assertTrue(any("nature.com/articles/s41586-020-2649-2.pdf" in url for url in nature_candidates))
        self.assertFalse(any("link.springer.com/content/pdf/" in url for url in nature_candidates))
        self.assertFalse(
            belongs_to_current_article(
                profile,
                "https://link.springer.com/content/pdf/10.1038%2Fs41586-020-2649-2.pdf",
                doi=nature_doi,
                source_url="https://www.nature.com/articles/s41586-020-2649-2",
            )
        )

    def test_agu_doi_prefix_infers_wiley_profile(self) -> None:
        profile = infer_publisher_profile("10.1029/2023TC007998")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "Wiley")

        self.assertIs(get_publisher_profile("agu"), get_publisher_profile("wiley"))
        self.assertIs(get_publisher_profile("agupubs"), get_publisher_profile("wiley"))

    def test_elife_pdf_candidate_is_derived_from_doi(self) -> None:
        profile = get_publisher_profile("elife")
        candidates = build_pdf_candidates(
            profile,
            "10.7554/elife.32822",
            source_url="https://elifesciences.org/articles/32822",
        )
        self.assertIn("https://elifesciences.org/articles/32822.pdf", candidates)
        self.assertNotIn("https://elifesciences.org/articles/elife.32822.pdf", candidates)
        self.assertTrue(
            belongs_to_current_article(
                profile,
                "https://elifesciences.org/articles/32822.pdf",
                doi="10.7554/elife.32822",
                source_url="https://elifesciences.org/articles/32822",
            )
        )
        self.assertFalse(
            belongs_to_current_article(
                profile,
                "https://elifesciences.org/articles/99999.pdf",
                doi="10.7554/elife.32822",
                source_url="https://elifesciences.org/articles/32822",
            )
        )

    def test_mdpi_candidates_reject_external_pdf_links(self) -> None:
        profile = get_publisher_profile("mdpi")
        doi = "10.3390/min10010042"
        candidates = build_pdf_candidates(
            profile,
            doi,
            source_url="https://www.mdpi.com/2075-163X/10/1/42",
            discovered_urls=[
                "https://vtk.org/wp-content/uploads/2015/04/file-formats.pdf",
                "https://mdpi-res.com/d_attachment/minerals/minerals-10-00042/article_deploy/minerals-10-00042-v2.pdf",
            ],
        )
        self.assertIn("https://www.mdpi.com/2075-163X/10/1/42/pdf", candidates)
        self.assertIn(
            "https://mdpi-res.com/d_attachment/minerals/minerals-10-00042/article_deploy/minerals-10-00042-v2.pdf",
            candidates,
        )
        self.assertNotIn("https://vtk.org/wp-content/uploads/2015/04/file-formats.pdf", candidates)
        self.assertFalse(
            belongs_to_current_article(
                profile,
                "https://vtk.org/wp-content/uploads/2015/04/file-formats.pdf",
                doi=doi,
                source_url="https://www.mdpi.com/2075-163X/10/1/42",
            )
        )

    def test_aip_candidates_reject_platform_user_guides(self) -> None:
        profile = get_publisher_profile("aip")
        doi = "10.1063/5.0010285"
        candidates = build_pdf_candidates(
            profile,
            doi,
            source_url=f"https://pubs.aip.org/doi/{doi}",
            discovered_urls=[
                "https://publishing.aip.org/wp-content/uploads/ContentPlatform_UserGuide_FINAL.pdf",
                f"https://pubs.aip.org/doi/pdf/{doi}",
            ],
        )

        self.assertIn(f"https://pubs.aip.org/doi/pdf/{doi}", candidates)
        self.assertNotIn(
            "https://publishing.aip.org/wp-content/uploads/ContentPlatform_UserGuide_FINAL.pdf",
            candidates,
        )
        self.assertFalse(
            belongs_to_current_article(
                profile,
                "https://publishing.aip.org/wp-content/uploads/ContentPlatform_UserGuide_FINAL.pdf",
                doi=doi,
                source_url=f"https://pubs.aip.org/doi/{doi}",
            )
        )
        self.assertTrue(
            belongs_to_current_article(
                profile,
                f"https://pubs.aip.org/doi/pdf/{doi}",
                doi=doi,
                source_url=f"https://pubs.aip.org/doi/{doi}",
            )
        )

    def test_aps_profile_canonicalizes_lowercase_physrev_dois(self) -> None:
        profile = get_publisher_profile("aps")
        doi = "10.1103/physrevlett.121.251301"

        self.assertEqual(
            profile.article_url(doi),
            "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.121.251301",
        )
        self.assertIn(
            "https://link.aps.org/pdf/10.1103/PhysRevLett.121.251301",
            profile.pdf_urls(doi),
        )
        candidates = build_pdf_candidates(
            profile,
            doi,
            source_url=profile.article_url(doi),
        )
        self.assertIn("https://link.aps.org/pdf/10.1103/PhysRevLett.121.251301", candidates)

    def test_publisher_matrix_report_explains_batch_readiness(self) -> None:
        report = build_publisher_matrix_report(
            entries={
                "acs": PublisherMatrixEntry(key="acs", status="ready", batch_policy="allow"),
                "elsevier": PublisherMatrixEntry(
                    key="elsevier",
                    status="prewarm_required",
                    batch_policy="allow",
                    prewarm=True,
                ),
                "onepetro": PublisherMatrixEntry(
                    key="onepetro",
                    status="waf_risky",
                    batch_policy="single_only",
                    prewarm=True,
                    known_blocker="waf_blocked",
                ),
            }
        )

        self.assertEqual(report["schema"], "instsci.publisher_matrix_report.v1")
        self.assertEqual(report["summary"]["entries"], 3)
        self.assertEqual(report["summary"]["ready"], 1)
        self.assertEqual(report["summary"]["prewarm_required"], 1)
        self.assertEqual(report["summary"]["waf_risky"], 1)
        items = {item["publisher"]: item for item in report["items"]}
        self.assertEqual(items["acs"]["batch_recommendation"], "batch_ok")
        self.assertEqual(items["elsevier"]["batch_recommendation"], "single_doi_prewarm_then_batch")
        self.assertEqual(items["onepetro"]["batch_recommendation"], "single_doi_only")
        self.assertIn("manual_browser_single_doi", items["onepetro"]["suggested_paths"])

    def test_unknown_and_invalid_matrix_entries_are_not_batch_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            matrix_path = Path(tmp) / "matrix.json"
            matrix_path.write_text(
                json.dumps({"publishers": {"bad": {"status": "invalid"}}}),
                encoding="utf-8",
            )
            invalid = load_publisher_matrix(matrix_path)["bad"]

        self.assertEqual(invalid.status, "unclassified")
        self.assertEqual(invalid.batch_policy, "single_only")
        with patch("instsci.publisher_matrix.load_publisher_matrix", return_value={}):
            unknown = get_publisher_matrix_entry("unknown-publisher")
        report = build_publisher_matrix_report(
            "unknown-publisher",
            entries={},
        )
        self.assertEqual(unknown.status, "unclassified")
        self.assertEqual(report["items"][0]["batch_recommendation"], "single_doi_only")
        self.assertEqual(report["summary"]["batch_ok"], 0)

    def test_matrix_uses_canonical_publisher_key_for_aliases(self) -> None:
        springer = PublisherMatrixEntry(key="springer", status="ready")
        report = build_publisher_matrix_report("springer-nature", entries={"springer": springer})

        self.assertEqual(report["publisher"], "springer")
        self.assertEqual(report["items"][0]["publisher"], "springer")
        self.assertTrue(report["items"][0]["configured"])

    def test_browser_doctor_probe_keeps_entitlement_and_waf_distinct(self) -> None:
        probe = browser_doctor._POWERSHELL_PROBE
        waf_line = next(line for line in probe.splitlines() if 'return "waf_blocked"' in line)
        access_line = next(line for line in probe.splitlines() if 'return "access_unavailable"' in line)
        auth_line = next(line for line in probe.splitlines() if 'return "auth_required"' in line)

        self.assertNotIn("Access denied", waf_line)
        self.assertIn("Access denied", access_line)
        self.assertIn("Purchase PDF", access_line)
        self.assertNotIn("Purchase PDF", auth_line)
        self.assertNotIn("|Sign in", auth_line)
        self.assertIn("MDPI Open Access Journals", probe)

    def test_browser_doctor_probe_is_profile_aware(self) -> None:
        probe = browser_doctor._POWERSHELL_PROBE
        for marker in (
            "BrowserProfile",
            "Normalize-PathForCompare",
            "profile_match",
            "matching_window_count",
            "profile_filter",
            "other_windows_present",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, probe)

    def test_after_run_doctor_is_profile_aware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("instsci.cli._run_browser_doctor_checkpoint") as checkpoint:
                cli._run_after_run_doctor_if_needed(
                    publisher="acs",
                    run_dir=Path(tmp),
                    enabled=True,
                    summary={"missing": 1, "unverified": 0, "failed": 0},
                    mode="user",
                    browser_profile=r"profiles/acs",
                )

        checkpoint.assert_called_once()
        self.assertEqual(checkpoint.call_args.kwargs["browser_profile"], r"profiles/acs")
        self.assertEqual(checkpoint.call_args.kwargs["stage"], "after_run")

    def test_explicit_browser_doctor_keeps_success_after_run_evidence(self) -> None:
        with patch.object(cli.sys, "argv", ["instsci", "papers", "dois.txt", "--browser-doctor"]):
            runtime = cli._resolve_browser_runtime_options(
                mode="user",
                browser_doctor_gate=True,
                watch_browser="notify",
                pause_on_blocker=True,
            )
        self.assertTrue(runtime["after_run_on_success"])

        with tempfile.TemporaryDirectory() as tmp:
            with patch("instsci.cli._run_browser_doctor_checkpoint") as checkpoint:
                cli._run_after_run_doctor_if_needed(
                    publisher="acs",
                    run_dir=Path(tmp),
                    enabled=True,
                    summary={"missing": 0, "unverified": 0, "failed": 0},
                    mode="user",
                    browser_profile=r"profiles/acs",
                    after_run_on_success=bool(runtime["after_run_on_success"]),
                )

        checkpoint.assert_called_once()
        self.assertEqual(checkpoint.call_args.kwargs["stage"], "after_run")
        self.assertEqual(checkpoint.call_args.kwargs["browser_profile"], r"profiles/acs")

    def test_sso_entry_clicker_ignores_skip_links(self) -> None:
        source = Path(PublisherBatchDownloader.__module__.replace(".", "/") + ".py")
        if not source.exists():
            source = Path(cli.__file__).parent / "publisher_batch.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn("const isSkipLink", text)
        self.assertIn("skip to main content", text)
        self.assertIn("href.includes('#main-content-focus')", text)
        self.assertIn("if (isSkipLink(text, href)) return false;", text)

    def test_all_after_run_doctor_calls_pass_profile(self) -> None:
        source = Path(cli.__file__).read_text(encoding="utf-8")
        calls = source.count("_run_after_run_doctor_if_needed(") - 1
        profile_args = source.count("browser_profile=cfg.chrome_profile_dir")
        self.assertGreaterEqual(calls, 1)
        self.assertGreaterEqual(profile_args, calls)

    def test_session_broker_restarts_when_profile_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stop_path = Path(tmp) / "brokers" / "springer-nature" / "stop"
            cfg = SimpleNamespace(chrome_profile_dir=str(Path(tmp) / "profiles" / "new"))
            broker = SimpleNamespace(
                load_broker_state=MagicMock(return_value={"profile_dir": str(Path(tmp) / "profiles" / "old")}),
                broker_is_running=MagicMock(side_effect=[True, False, False, True, True]),
                broker_stop_path=MagicMock(return_value=stop_path),
                start_broker_process=MagicMock(),
            )

            with patch.dict("sys.modules", {"instsci.session_broker": broker}):
                ok = cli._ensure_session_broker(
                    broker_publisher="springer-nature",
                    cfg=cfg,
                    institution="",
                    broker_ttl=60,
                )

            self.assertTrue(ok)
            self.assertEqual(stop_path.read_text(encoding="utf-8"), "stop")
            broker.start_broker_process.assert_called_once()
            self.assertEqual(broker.start_broker_process.call_args.kwargs["browser_profile"], cfg.chrome_profile_dir)

    def test_pdf_entry_capture_listens_for_download_events_on_viewer_publishers(self) -> None:
        source = Path(PublisherBatchDownloader.__module__.replace(".", "/") + ".py")
        if not source.exists():
            source = Path(cli.__file__).parent / "publisher_batch.py"
        text = source.read_text(encoding="utf-8")

        self.assertIn('{"springer nature", "aps", "ieee", "aip publishing"}', text)
        self.assertIn("_click_pdf_entry_with_download_capture", text)
        self.assertIn("pdf_entry_download_captured", text)


if __name__ == "__main__":
    unittest.main()

