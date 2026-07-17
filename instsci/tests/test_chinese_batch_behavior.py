import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from typer.testing import CliRunner

from instsci.chinese_download_quota import ChineseDownloadQuotaError, QuotaReservation
from instsci.cli import app
from instsci.config import Config


class _FakePage:
    url = "https://s.wanfangdata.com.cn/paper"

    def screenshot(self, *, path: str, full_page: bool = False) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test screenshot")


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.pages = [page]

    def close(self) -> None:
        return None


class ChineseBatchBehaviorTests(TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(
            output_dir=str(self.root / "papers"),
            cache_dir=str(self.root / "cache"),
            cookie_path=str(self.root / "cookies.json"),
            chrome_profile_dir=str(self.root / "chrome"),
            cnki_profile_dir=str(self.root / "cnki-profile"),
            wanfang_profile_dir=str(self.root / "wanfang-profile"),
            carsi_cookie_dir=str(self.root / "carsi"),
        )

    def _input(self, name: str, portal: str) -> Path:
        path = self.root / f"{name}.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "record_id": f"{portal}-1",
                        "title": "同题研究",
                        "authors": ["李四", "张三"],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def _reservation(self, portal: str, used: int, *, allowed: bool = True) -> QuotaReservation:
        return QuotaReservation(
            allowed=allowed,
            date="2026-07-17",
            limit=100,
            used=used,
            remaining=max(100 - used, 0),
            portal=portal,
            record_id=f"{portal}-1",
            reason="" if allowed else "daily_limit_reached",
        )

    def test_cnki_ambiguous_candidate_is_browser_verified_and_consumes_no_quota(self) -> None:
        source = self._input("cnki-ambiguous", "cnki")
        run_dir = self.root / "run-cnki-ambiguous"
        page = _FakePage()
        context = _FakeContext(page)
        navigation = {
            "session_status": "ambiguous_search_result",
            "search_result": {
                "selected": False,
                "reason": "ambiguous_search_result",
                "title_candidate_count": 2,
                "author_match_count": 0,
                "author_disambiguation_used": True,
            },
        }
        with (
            patch("instsci.cli.Config.load", return_value=self.config),
            patch("instsci.cnki_session.open_cnki_login_session", return_value=(context, page, run_dir)),
            patch("instsci.cnki_session.navigate_cnki_article_via_search", return_value=navigation),
            patch("instsci.chinese_download_quota.reserve_chinese_download") as reserve,
            patch("instsci.cnki_session.capture_cnki_pdf") as capture,
        ):
            result = self.runner.invoke(
                app,
                ["cnki-batch", str(source), "--output", str(run_dir), "--delay", "2"],
            )

        self.assertEqual(result.exit_code, 2, result.output)
        reserve.assert_not_called()
        capture.assert_not_called()
        row = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(row["standard_status"], "ambiguous_search_result")
        self.assertEqual(row["result_evidence"], "browser_verified")

    def test_wanfang_ambiguous_candidate_is_browser_verified_and_consumes_no_quota(self) -> None:
        source = self._input("wanfang-ambiguous", "wanfang")
        run_dir = self.root / "run-wanfang-ambiguous"
        page = _FakePage()
        context = _FakeContext(page)
        with (
            patch("instsci.cli.Config.load", return_value=self.config),
            patch("instsci.wanfang_session.open_wanfang_session", return_value=(context, page, run_dir)),
            patch(
                "instsci.wanfang_session.navigate_wanfang_search",
                return_value={"session_status": "portal_ready", "verification_required": False},
            ),
            patch(
                "instsci.wanfang_session.inspect_wanfang_result_download",
                return_value={
                    "selected": False,
                    "reason": "ambiguous_search_result",
                    "title_candidate_count": 2,
                    "author_match_count": 0,
                    "author_disambiguation_used": True,
                },
            ),
            patch("instsci.chinese_download_quota.reserve_chinese_download") as reserve,
            patch("instsci.wanfang_session.capture_wanfang_pdf") as capture,
        ):
            result = self.runner.invoke(
                app,
                ["wanfang-batch", str(source), "--output", str(run_dir), "--delay", "2"],
            )

        self.assertEqual(result.exit_code, 2, result.output)
        reserve.assert_not_called()
        capture.assert_not_called()
        row = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(row["standard_status"], "ambiguous_search_result")
        self.assertEqual(row["result_evidence"], "browser_verified")

    def test_daily_limit_blocks_capture(self) -> None:
        source = self._input("wanfang-limit", "wanfang")
        run_dir = self.root / "run-wanfang-limit"
        page = _FakePage()
        context = _FakeContext(page)
        with (
            patch("instsci.cli.Config.load", return_value=self.config),
            patch("instsci.wanfang_session.open_wanfang_session", return_value=(context, page, run_dir)),
            patch(
                "instsci.wanfang_session.navigate_wanfang_search",
                return_value={"session_status": "portal_ready", "verification_required": False},
            ),
            patch(
                "instsci.wanfang_session.inspect_wanfang_result_download",
                return_value={"selected": True, "author_disambiguation_used": False},
            ),
            patch(
                "instsci.chinese_download_quota.reserve_chinese_download",
                return_value=self._reservation("wanfang", 100, allowed=False),
            ),
            patch("instsci.wanfang_session.capture_wanfang_pdf") as capture,
        ):
            result = self.runner.invoke(
                app,
                ["wanfang-batch", str(source), "--output", str(run_dir), "--delay", "2"],
            )

        self.assertEqual(result.exit_code, 2, result.output)
        capture.assert_not_called()
        row = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(row["standard_status"], "daily_limit_reached")

    def test_corrupt_quota_state_blocks_capture(self) -> None:
        source = self._input("cnki-corrupt", "cnki")
        run_dir = self.root / "run-cnki-corrupt"
        page = _FakePage()
        context = _FakeContext(page)
        navigation = {
            "session_status": "portal_ready",
            "search_result": {"selected": True, "reason": "", "author_disambiguation_used": False},
        }
        with (
            patch("instsci.cli.Config.load", return_value=self.config),
            patch("instsci.cnki_session.open_cnki_login_session", return_value=(context, page, run_dir)),
            patch("instsci.cnki_session.navigate_cnki_article_via_search", return_value=navigation),
            patch(
                "instsci.chinese_download_quota.reserve_chinese_download",
                side_effect=ChineseDownloadQuotaError("invalid quota ledger"),
            ),
            patch("instsci.cnki_session.capture_cnki_pdf") as capture,
        ):
            result = self.runner.invoke(
                app,
                ["cnki-batch", str(source), "--output", str(run_dir), "--delay", "2"],
            )

        self.assertEqual(result.exit_code, 2, result.output)
        capture.assert_not_called()
        row = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(row["standard_status"], "quota_state_error")

    def test_wanfang_verification_retry_reserves_a_second_attempt(self) -> None:
        source = self._input("wanfang-retry", "wanfang")
        run_dir = self.root / "run-wanfang-retry"
        page = _FakePage()
        context = _FakeContext(page)
        selection = {"selected": True, "author_disambiguation_used": False}
        with (
            patch("instsci.cli.Config.load", return_value=self.config),
            patch("instsci.wanfang_session.open_wanfang_session", return_value=(context, page, run_dir)),
            patch(
                "instsci.wanfang_session.navigate_wanfang_search",
                return_value={"session_status": "portal_ready", "verification_required": False},
            ),
            patch("instsci.wanfang_session.inspect_wanfang_result_download", return_value=selection),
            patch("instsci.wanfang_session.wanfang_verification_visible", return_value=False),
            patch(
                "instsci.chinese_download_quota.reserve_chinese_download",
                side_effect=[self._reservation("wanfang", 1), self._reservation("wanfang", 2)],
            ) as reserve,
            patch(
                "instsci.wanfang_session.capture_wanfang_pdf",
                side_effect=[
                    {"verification_required": True},
                    {"verification_required": False, "pdf_path": "", "pdf_header_valid": False, "size_bytes": 0},
                ],
            ) as capture,
        ):
            result = self.runner.invoke(
                app,
                [
                    "wanfang-batch",
                    str(source),
                    "--output",
                    str(run_dir),
                    "--delay",
                    "2",
                    "--verification-policy",
                    "prompt",
                ],
                input="\n",
            )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(reserve.call_count, 2)
        self.assertEqual(capture.call_count, 2)
        row = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(len(row["quota_attempts"]), 2)

    def test_independent_cnki_and_wanfang_commands_share_the_ledger(self) -> None:
        page = _FakePage()
        context = _FakeContext(page)
        cnki_source = self._input("cnki-shared", "cnki")
        cnki_run = self.root / "run-cnki-shared"
        cnki_navigation = {
            "session_status": "portal_ready",
            "search_result": {"selected": True, "reason": "", "author_disambiguation_used": False},
        }
        with (
            patch("instsci.cli.Config.load", return_value=self.config),
            patch("instsci.cnki_session.open_cnki_login_session", return_value=(context, page, cnki_run)),
            patch("instsci.cnki_session.navigate_cnki_article_via_search", return_value=cnki_navigation),
            patch(
                "instsci.cnki_session.capture_cnki_pdf",
                return_value={
                    "verification_required": False,
                    "pdf_path": str(self.root / "missing-cnki.pdf"),
                    "pdf_header_valid": False,
                    "size_bytes": 0,
                },
            ),
        ):
            cnki_result = self.runner.invoke(
                app,
                ["cnki-batch", str(cnki_source), "--output", str(cnki_run), "--delay", "2"],
            )

        wanfang_source = self._input("wanfang-shared", "wanfang")
        wanfang_run = self.root / "run-wanfang-shared"
        with (
            patch("instsci.cli.Config.load", return_value=self.config),
            patch("instsci.wanfang_session.open_wanfang_session", return_value=(context, page, wanfang_run)),
            patch(
                "instsci.wanfang_session.navigate_wanfang_search",
                return_value={"session_status": "portal_ready", "verification_required": False},
            ),
            patch(
                "instsci.wanfang_session.inspect_wanfang_result_download",
                return_value={"selected": True, "author_disambiguation_used": False},
            ),
            patch(
                "instsci.wanfang_session.capture_wanfang_pdf",
                return_value={"verification_required": False, "pdf_path": "", "pdf_header_valid": False, "size_bytes": 0},
            ),
        ):
            wanfang_result = self.runner.invoke(
                app,
                ["wanfang-batch", str(wanfang_source), "--output", str(wanfang_run), "--delay", "2"],
            )

        self.assertEqual(cnki_result.exit_code, 2, cnki_result.output)
        self.assertEqual(wanfang_result.exit_code, 2, wanfang_result.output)
        ledger = json.loads((Path(self.config.cache_dir) / "chinese_download_quota.json").read_text(encoding="utf-8"))
        reservations = [entry for entries in ledger["days"].values() for entry in entries]
        self.assertEqual([entry["portal"] for entry in reservations], ["cnki", "wanfang"])

    def test_quota_status_command_reports_shared_count(self) -> None:
        from instsci.chinese_download_quota import reserve_chinese_download

        ledger = Path(self.config.cache_dir) / "chinese_download_quota.json"
        reserve_chinese_download(ledger, portal="cnki", record_id="cnki-1")
        with patch("instsci.cli.Config.load", return_value=self.config):
            result = self.runner.invoke(app, ["chinese-quota", "status", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"used": 1', result.output)

    def test_quota_repair_command_removes_only_verified_stale_lock(self) -> None:
        ledger = Path(self.config.cache_dir) / "chinese_download_quota.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        lock = ledger.with_suffix(ledger.suffix + ".lock")
        lock.write_text("pid=2147483647\n", encoding="ascii")
        with patch("instsci.cli.Config.load", return_value=self.config):
            result = self.runner.invoke(app, ["chinese-quota", "repair", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(lock.exists())
        self.assertIn('"removed": true', result.output)


if __name__ == "__main__":
    import unittest

    unittest.main()
