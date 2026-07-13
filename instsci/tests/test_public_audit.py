from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from instsci.public_audit import audit_public_package, doctor_report


class PublicAuditTests(unittest.TestCase):
    def test_audit_passes_clean_public_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source_patched" / "instsci").mkdir(parents=True)
            (root / "source_patched" / "README.md").write_text("clean public package\n", encoding="utf-8")

            payload = audit_public_package(root)

            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["issue_count"], 0)

    def test_audit_flags_cache_and_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "source_patched" / "instsci" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "x.pyc").write_bytes(b"cache")
            (root / "README.md").write_text(r"C:\Users\Example\run", encoding="utf-8")

            payload = audit_public_package(root)

            self.assertEqual(payload["status"], "fail")
            self.assertGreaterEqual(payload["summary"].get("python_cache_dir", 0), 1)
            self.assertGreaterEqual(payload["summary"].get("windows_user_path", 0), 1)

    def test_audit_flags_cross_platform_paths_and_quoted_json_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                '{\n'
                '  "api_key": "real-secret",\n'
                '  "windows_path": "D:/InstSci/scripts/Check.ps1",\n'
                '  "linux_path": "/home/alice/private/run",\n'
                '  "mac_path": "/Users/alice/private/run"\n'
                '}\n',
                encoding="utf-8",
            )

            payload = audit_public_package(root)

            self.assertEqual(payload["status"], "fail")
            self.assertGreaterEqual(payload["summary"].get("local_drive_path", 0), 1)
            self.assertGreaterEqual(payload["summary"].get("posix_user_path", 0), 2)
            self.assertGreaterEqual(payload["summary"].get("cleartext_secret_assignment", 0), 1)

    def test_audit_rejects_browser_evidence_profiles_and_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "login.png").write_bytes(b"not-a-real-image")
            (root / "session.har").write_text("{}", encoding="utf-8")
            (root / "storage_state.json").write_text("{}", encoding="utf-8")
            (root / "private.pem").write_text("PRIVATE KEY", encoding="utf-8")
            profile = root / "nested" / "cnki-profile"
            profile.mkdir(parents=True)
            (profile / "Preferences").write_text("{}", encoding="utf-8")

            payload = audit_public_package(root)

            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["summary"].get("screenshot_or_image_included"), 1)
            self.assertEqual(payload["summary"].get("browser_session_asset_included"), 1)
            self.assertEqual(payload["summary"].get("certificate_or_private_key_included"), 1)
            self.assertEqual(payload["summary"].get("browser_profile_included"), 1)
            self.assertEqual(payload["summary"].get("local_secret_or_session_file"), 1)

    def test_institution_scan_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace.txt").write_text("legacy tsinghua route", encoding="utf-8")

            payload = audit_public_package(root)

            self.assertEqual(payload["summary"].get("specific_institution_trace"), 1)

    def test_git_audit_uses_tracked_and_unignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            tracked = root / "tracked.txt"
            ignored = root / "ignored.png"
            tracked.write_text("clean", encoding="utf-8")
            ignored.write_bytes(b"ignored private asset")
            with patch(
                "instsci.public_audit._git_public_paths",
                return_value=[tracked],
            ):
                payload = audit_public_package(root)

            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["file_count"], 1)

    def test_audit_allows_token_variable_names_and_regex_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source_patched").mkdir()
            (root / "source_patched" / "example.py").write_text(
                "token = context.set(value)\npattern = r\"C:\\\\Users\\\\[^\\\\]+\"\n",
                encoding="utf-8",
            )

            payload = audit_public_package(root)

            self.assertEqual(payload["status"], "pass")

    def test_audit_flags_root_historical_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source_patched" / "tests").mkdir(parents=True)

            payload = audit_public_package(root)

            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["summary"].get("root_historical_tests_included"), 1)

    def test_doctor_report_includes_core_checks(self) -> None:
        payload = doctor_report()
        names = {item["name"] for item in payload["checks"]}

        self.assertIn("runtime_dependencies", names)
        self.assertIn("browser_doctor_support", names)
        self.assertIn("publisher_matrix", names)
        self.assertIn("zotero_handoff", names)


if __name__ == "__main__":
    unittest.main()
