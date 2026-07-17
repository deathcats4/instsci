try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI.
    import tomli as tomllib
import unittest
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click import unstyle
from typer.testing import CliRunner

import instsci.config as config_module
from instsci.cli import app
from instsci.config import Config
from instsci.tests.project_guards import find_project_reference_offenders


class PublicLanguageTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_cli_help_uses_institutional_access_branding(self):
        result = self.runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("instsci", result.output)
        self.assertIn("institutional access", result.output.lower())
        self.assertIn("federated-login", result.output)
        self.assertNotIn("WebVPN", result.output)
        self.assertNotIn("carsi-login", result.output)

    def test_config_help_prefers_access_url_over_legacy_gateway_option(self):
        result = self.runner.invoke(app, ["config-cmd", "--help"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--access-url", output)
        self.assertIn("--federated-enable", output)
        self.assertIn("--federated-school", output)
        self.assertNotIn("--webvpn-url", output)
        self.assertNotIn("--carsi-enable", output)

    def test_publisher_batch_help_exposes_profile_selection(self):
        result = self.runner.invoke(app, ["publisher-batch", "--help"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--publisher", output)
        self.assertIn("--institution", output)
        self.assertIn("profile", output.lower())
        self.assertIn("--carsi-portal", output)
        self.assertIn("resource portal", output)

    def test_papers_help_exposes_recommended_browser_workflow(self):
        result = self.runner.invoke(app, ["papers", "--help"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("oa-first", output.lower())
        self.assertIn("browser workflow", output.lower())
        self.assertIn("--publisher", output)
        self.assertIn("--institution", output)
        self.assertIn("--detach", output)
        self.assertIn("--carsi-portal", output)
        self.assertIn("resource portal", output)

    def test_jobs_help_exposes_long_running_controls(self):
        result = self.runner.invoke(app, ["jobs", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("long-running", result.output.lower())
        self.assertIn("status", result.output)
        self.assertIn("resume", result.output)
        self.assertIn("tail", result.output)

    def test_elsevier_setup_help_describes_global_config(self):
        result = self.runner.invoke(app, ["elsevier-setup", "--help"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("global", output.lower())
        self.assertIn("--test-doi", output)
        self.assertIn("does not bind", output)

    def test_publisher_doctor_help_exposes_reusable_verification_asset(self):
        result = self.runner.invoke(app, ["publisher-doctor", "--help"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--publisher", output)
        self.assertIn("--output", output)
        self.assertIn("verify", output.lower())
        self.assertIn("HTTP preflight", output)
        self.assertIn("browser", output.lower())

    def test_chinese_literature_sites_reports_download_and_route_statuses(self):
        result = self.runner.invoke(app, ["chinese-literature-sites", "--json"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("instsci.chinese_literature_portals.v1", output)
        self.assertIn("cnki", output)
        self.assertIn("browser_verified_search_first", output)
        self.assertIn("wanfang", output)
        self.assertIn("browser_verified_search_download", output)
        self.assertIn("cqvip", output)
        self.assertIn("browser_verified_manual_broker_waf_blocked", output)
        self.assertIn("download_verified_portals", output)

    def test_chinese_literature_sites_site_filter_does_not_crash(self):
        result = self.runner.invoke(app, ["chinese-literature-sites", "--site", "cnki"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("download-verified: cnki", output)
        self.assertIn("route-verified: cnki", output)
    def test_wanfang_batch_help_describes_search_download_popup_flow(self):
        result = self.runner.invoke(app, ["wanfang-batch", "--help"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Wanfang", output)
        self.assertIn("search-result", output)
        self.assertIn("download popups", output)
        self.assertIn("--profile-dir", output)
        self.assertIn("--verification-", output)
        self.assertIn("policy: stop or", output)
        self.assertIn("--daily-limit", output)
        self.assertIn("--no-daily-limit", output)

    def test_chinese_quota_help_exposes_safe_status_and_repair(self):
        result = self.runner.invoke(app, ["chinese-quota", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("status", result.output)
        self.assertIn("repair", result.output)
        self.assertIn("stale", result.output.lower())

    def test_config_help_exposes_optional_chinese_download_policy(self):
        result = self.runner.invoke(app, ["config-cmd", "--help"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--chinese-warning-thre", output)
        self.assertIn("--chinese-combined-dai", output)
        self.assertIn("--cnki-daily-limit", output)
        self.assertIn("--wanfang-daily-limit", output)

    def test_agents_requires_builtin_browser_for_publisher_pdf_verdicts(self):
        text = Path("AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("MUST use InstSci's built-in CloakBrowser", text)
        self.assertIn("publisher-doctor", text)
        self.assertIn("HTTP preflight", text)

    def test_agents_exposes_actionable_publisher_pdf_workflow(self):
        text = Path("AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("## Agent Workflow", text)
        self.assertIn("Classify the task", text)
        self.assertIn("instsci papers", text)
        self.assertIn("instsci publisher-batch", text)
        self.assertIn("Evidence Standard", text)
        self.assertIn("Report Template", text)

    def test_project_has_no_retired_package_name_references(self):
        root = Path(__file__).resolve().parents[2]
        retired_names = ("vpn" + "sci",)
        offenders = find_project_reference_offenders(root, retired_names, include_paths=True)

        self.assertEqual(offenders, [])

    def test_setup_help_exposes_one_step_environment_setup(self):
        result = self.runner.invoke(app, ["setup", "--help"])
        output = unstyle(result.output)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--school", output)
        self.assertIn("--check", output)
        self.assertIn("environment", output.lower())

    def test_setup_configures_school_federated_login_and_directories(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            output_dir = Path(tmp) / "papers"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                result = self.runner.invoke(
                    app,
                    [
                        "setup",
                        "--school",
                        "Example WebVPN University",
                        "--email",
                        "reader@example.edu",
                        "--output-dir",
                        str(output_dir),
                    ],
                )
                cfg = Config.load()

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(cfg.school, "Example WebVPN University")
            self.assertEqual(cfg.email, "reader@example.edu")
            self.assertTrue(cfg.carsi_enabled)
            self.assertEqual(cfg.carsi_idp_name, "Example WebVPN University")
            self.assertIn("webvpn.example.edu", cfg.webvpn_base_url)
            self.assertTrue(output_dir.exists())
            self.assertIn("Environment ready", result.output)

    def test_setup_configures_institution_names_without_requiring_campus_school(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                result = self.runner.invoke(
                    app,
                    [
                        "setup",
                        "--institution-en",
                        "Example University",
                        "--institution-cn",
                        "示例大学",
                    ],
                )
                cfg = Config.load()

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(cfg.carsi_enabled)
            self.assertEqual(cfg.carsi_idp_name, "Example University")
            self.assertEqual(cfg.institution_name_en, "Example University")
            self.assertEqual(cfg.institution_name_zh, "示例大学")
            self.assertFalse(cfg.school)
            self.assertIn("Environment ready", result.output)

    def test_setup_check_reports_missing_subscription_institution_without_saving_new_config(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                result = self.runner.invoke(app, ["setup", "--check"])

            self.assertEqual(result.exit_code, 2, result.output)
            self.assertIn("Subscription", result.output)
            self.assertIn("institution", result.output)
            self.assertIn("missing", result.output.lower())

    def test_package_exposes_inst_sci_console_scripts(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        scripts = pyproject["project"]["scripts"]
        self.assertIn("instsci", scripts)
        self.assertIn("instsci-mcp", scripts)
        self.assertEqual(scripts["instsci"], "instsci.cli:app")
        self.assertEqual(scripts["instsci-mcp"], "instsci.mcp_server:main")
        self.assertNotIn("VPN", pyproject["project"]["description"])

    def test_readme_install_points_to_github_until_pypi_publish(self):
        text = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("https://github.com/deathcats4/instsci-workflow.git", text)
        self.assertIn("python -m pip install -e .", text)
        self.assertNotIn("pipx install instsci", text)
        self.assertNotIn("uv tool install instsci", text)

    def test_readme_guides_elsevier_global_setup_without_requiring_inst_token(self):
        text = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("project-wide global setting", text)
        self.assertIn("Inst Token is optional", text)
        self.assertIn("view=FULL XML", text)
        self.assertIn("object/eid", text)

    def test_readme_describes_local_login_persistence_without_password_storage(self):
        text = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("Login persistence is local", text)
        self.assertIn("persistent CloakBrowser profile", text)
        self.assertIn("long-lived publisher broker", text)
        self.assertIn("does not store your institution password", text)
        self.assertIn("not treated as a complete login state", text)
        self.assertIn("ignored by Git", text)

    def test_inst_sci_skill_guides_elsevier_global_setup_without_requiring_inst_token(self):
        text = Path("skills/instsci/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Elsevier API", text)
        self.assertIn("global", text.lower())
        self.assertIn("Inst Token is optional", text)
        self.assertIn("view=FULL XML", text)
        self.assertIn("object/eid", text)
        self.assertIn("direct-first", text)

    def test_inst_sci_skill_guides_chinese_literature_portal_workflow(self):
        text = Path("skills/instsci/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Chinese Literature Portals", text)
        self.assertIn("instsci chinese-literature-sites", text)
        self.assertIn("--navigation-mode search", text)
        self.assertIn("Wanfang", text)
        self.assertIn("CQVIP", text)
        self.assertIn("Fulltext/Download", text)
        self.assertIn("wanfang-batch", text)
        self.assertIn("manual broker", text)
        self.assertIn("not download-verified", text)

    def test_readme_documents_chinese_author_disambiguation_and_download_policy(self):
        text = Path("README.md").read_text(encoding="utf-8")

        self.assertIn('"authors": ["张三", "李四"]', text)
        self.assertIn('"first_author": "Smith, John"', text)
        self.assertIn("Only the first author is used", text)
        self.assertIn("ambiguous_search_result", text)
        self.assertIn("do not have a default hard daily limit", text)
        self.assertIn("not a uniform official CNKI or", text)
        self.assertIn("--chinese-combined-daily-limit", text)
        self.assertIn("--cnki-daily-limit", text)
        self.assertIn("--no-daily-limit", text)
        self.assertRegex(text.lower(), r"failures\s+and retries\s+count")
        self.assertIn("record_id never overrides an exact-title mismatch", text)
        self.assertIn("first-page signature", text)
        self.assertIn("instsci chinese-quota status", text)
        self.assertIn("instsci chinese-quota repair", text)
        self.assertIn("relevance sorting", text)

    def test_inst_sci_skill_documents_chinese_author_and_quota_guards(self):
        text = Path("skills/instsci/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Only the first author is used", text)
        self.assertIn("ambiguous_search_result", text)
        self.assertIn("not a default shared hard limit", text)
        self.assertIn("conservative InstSci reminder", text)
        self.assertIn("Default hard limits are unset", text)
        self.assertRegex(text.lower(), r"failures\s+and retries\s+count")
        self.assertIn("record_id never overrides an exact-title mismatch", text)
        self.assertIn("first-page signature", text)
        self.assertIn("instsci chinese-quota status", text)
        self.assertIn("instsci chinese-quota repair", text)
        self.assertIn("relevance sorting", text)
        self.assertIn("later-coauthor negative", text)

    def test_inst_sci_module_entrypoint_is_available(self):
        result = subprocess.run(
            [sys.executable, "-m", "instsci.cli", "--help"],
            cwd=Path(__file__).resolve().parents[2],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("institutional access", result.stdout.lower())
        self.assertNotIn("WebVPN", result.stdout)


if __name__ == "__main__":
    unittest.main()
