from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import instsci.config as config_module
from instsci.cli import app
from instsci.config import Config
from typer.testing import CliRunner


class ConfigPathTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_default_paths_use_inst_sci_directory(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                cfg = Config()

            self.assertEqual(Path(cfg.output_dir), base / "papers")
            self.assertEqual(Path(cfg.cache_dir), base / "cache")
            self.assertEqual(Path(cfg.cookie_path), base / "cookies.json")
            self.assertEqual(Path(cfg.chrome_profile_dir), base / "chrome-profile")
            self.assertEqual(Path(cfg.carsi_cookie_dir), base / "carsi_cookies")

    def test_load_uses_inst_sci_config_path(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            base.mkdir()
            (base / "config.json").write_text(
                '{"school": "InstSci University", "email": "reader@example.edu"}',
                encoding="utf-8",
            )

            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                cfg = Config.load()
                cfg.save()

            self.assertEqual(cfg.school, "InstSci University")
            self.assertEqual(cfg.email, "reader@example.edu")
            self.assertTrue((base / "config.json").exists())

    def test_chinese_download_policy_defaults_to_warning_without_hard_limits(self):
        cfg = Config()

        self.assertEqual(cfg.chinese_download_warning_threshold, 100)
        self.assertIsNone(cfg.chinese_download_combined_daily_limit)
        self.assertIsNone(cfg.cnki_daily_download_limit)
        self.assertIsNone(cfg.wanfang_daily_download_limit)

    def test_chinese_download_policy_round_trips_null_and_portal_limits(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            Config(
                chinese_download_warning_threshold=80,
                chinese_download_combined_daily_limit=None,
                cnki_daily_download_limit=30,
                wanfang_daily_download_limit=90,
            ).save(path)

            loaded = Config.load(path)

        self.assertEqual(loaded.chinese_download_warning_threshold, 80)
        self.assertIsNone(loaded.chinese_download_combined_daily_limit)
        self.assertEqual(loaded.cnki_daily_download_limit, 30)
        self.assertEqual(loaded.wanfang_daily_download_limit, 90)

    def test_config_command_sets_and_removes_chinese_hard_limits(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                set_result = self.runner.invoke(
                    app,
                    [
                        "config-cmd",
                        "--chinese-warning-threshold",
                        "80",
                        "--chinese-combined-daily-limit",
                        "200",
                        "--cnki-daily-limit",
                        "30",
                    ],
                )
                configured = Config.load()
                clear_result = self.runner.invoke(
                    app,
                    ["config-cmd", "--no-chinese-combined-daily-limit", "--no-cnki-daily-limit"],
                )
                cleared = Config.load()

        self.assertEqual(set_result.exit_code, 0, set_result.output)
        self.assertEqual(configured.chinese_download_warning_threshold, 80)
        self.assertEqual(configured.chinese_download_combined_daily_limit, 200)
        self.assertEqual(configured.cnki_daily_download_limit, 30)
        self.assertEqual(clear_result.exit_code, 0, clear_result.output)
        self.assertIsNone(cleared.chinese_download_combined_daily_limit)
        self.assertIsNone(cleared.cnki_daily_download_limit)

    def test_config_command_rejects_set_and_remove_conflict(self):
        with TemporaryDirectory() as tmp:
            with patch.object(config_module, "DEFAULT_BASE_DIR", Path(tmp) / ".instsci"):
                result = self.runner.invoke(
                    app,
                    ["config-cmd", "--cnki-daily-limit", "30", "--no-cnki-daily-limit"],
                )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("Cannot set and remove", result.output)


if __name__ == "__main__":
    unittest.main()

