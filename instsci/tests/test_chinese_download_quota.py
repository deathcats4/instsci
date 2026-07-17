import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from instsci.chinese_download_quota import (
    DEFAULT_DAILY_WARNING_THRESHOLD,
    ChineseDownloadPolicy,
    ChineseDownloadQuotaError,
    inspect_chinese_download_quota,
    repair_chinese_download_quota_lock,
    reserve_chinese_download,
)


class ChineseDownloadQuotaTests(TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / "chinese_download_quota.json"
        self.now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    def reserve(
        self,
        portal: str,
        record_id: str,
        *,
        now: datetime | None = None,
        policy: ChineseDownloadPolicy | None = None,
    ):
        return reserve_chinese_download(
            self.ledger,
            portal=portal,
            record_id=record_id,
            now=now or self.now,
            policy=policy,
            lock_timeout=0.05,
        )

    def test_default_policy_warns_at_100_without_a_hard_limit(self) -> None:
        policy = ChineseDownloadPolicy()

        self.assertEqual(DEFAULT_DAILY_WARNING_THRESHOLD, 100)
        self.assertEqual(policy.warning_threshold, 100)
        self.assertIsNone(policy.combined_daily_limit)
        self.assertIsNone(policy.portal_daily_limit)

    def test_first_reservation_writes_auditable_ledger(self) -> None:
        result = self.reserve("cnki", "CNKI-1")

        self.assertTrue(result.allowed)
        self.assertEqual(result.used, 1)
        self.assertIsNone(result.remaining)
        self.assertEqual(result.portal_used, 1)
        self.assertIsNone(result.portal_remaining)
        self.assertEqual(result.date, "2026-07-17")
        payload = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "instsci.chinese_download_quota.v1")
        self.assertEqual(payload["days"]["2026-07-17"][0]["portal"], "cnki")
        self.assertEqual(payload["days"]["2026-07-17"][0]["record_id"], "CNKI-1")

    def test_cnki_and_wanfang_share_audit_counts_without_a_default_hard_limit(self) -> None:
        first = self.reserve("cnki", "a")
        second = self.reserve("wanfang", "b")

        self.assertEqual((first.used, second.used), (1, 2))
        self.assertIsNone(second.remaining)
        self.assertEqual(second.portal_used, 1)

    def test_reservations_persist_across_independent_calls(self) -> None:
        self.reserve("cnki", "a")

        result = reserve_chinese_download(
            Path(str(self.ledger)),
            portal="wanfang",
            record_id="b",
            now=self.now,
            lock_timeout=0.05,
        )

        self.assertEqual(result.used, 2)

    def test_default_100th_attempt_warns_and_101st_is_still_allowed(self) -> None:
        result = None
        for index in range(100):
            result = self.reserve("cnki", str(index))

        assert result is not None
        self.assertTrue(result.allowed)
        self.assertTrue(result.warning_triggered)
        self.assertTrue(result.warning_reached)
        after = self.reserve("wanfang", "101")
        self.assertTrue(after.allowed)
        self.assertFalse(after.warning_triggered)
        self.assertTrue(after.warning_reached)

    def test_configured_combined_101st_attempt_is_blocked_without_appending(self) -> None:
        policy = ChineseDownloadPolicy(combined_daily_limit=100)
        for index in range(100):
            self.assertTrue(self.reserve("cnki", str(index), policy=policy).allowed)

        blocked = self.reserve("wanfang", "101", policy=policy)

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "daily_limit_reached")
        self.assertEqual(blocked.used, 100)
        self.assertEqual(blocked.remaining, 0)
        self.assertEqual(blocked.limit_scope, "combined")
        payload = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["days"]["2026-07-17"]), 100)

    def test_configured_portal_limit_does_not_block_the_other_portal(self) -> None:
        cnki_policy = ChineseDownloadPolicy(portal_daily_limit=1)
        wanfang_policy = ChineseDownloadPolicy(portal_daily_limit=1)
        self.assertTrue(self.reserve("cnki", "cnki-1", policy=cnki_policy).allowed)

        blocked = self.reserve("cnki", "cnki-2", policy=cnki_policy)
        other = self.reserve("wanfang", "wanfang-1", policy=wanfang_policy)

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.limit_scope, "portal")
        self.assertTrue(other.allowed)
        self.assertEqual(other.used, 2)
        self.assertEqual(other.portal_used, 1)

    def test_next_local_date_gets_fresh_allowance_and_keeps_prior_day(self) -> None:
        self.reserve("cnki", "old")

        next_day = self.reserve("wanfang", "new", now=self.now + timedelta(days=1))

        self.assertTrue(next_day.allowed)
        self.assertEqual(next_day.used, 1)
        payload = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(set(payload["days"]), {"2026-07-17", "2026-07-18"})

    def test_corrupt_ledger_fails_closed(self) -> None:
        self.ledger.write_text("{not-json", encoding="utf-8")

        with self.assertRaisesRegex(ChineseDownloadQuotaError, "invalid quota ledger"):
            self.reserve("cnki", "a")

        self.assertEqual(self.ledger.read_text(encoding="utf-8"), "{not-json")

    def test_unexpected_schema_fails_closed(self) -> None:
        self.ledger.write_text(json.dumps({"schema": "unexpected", "days": {}}), encoding="utf-8")

        with self.assertRaisesRegex(ChineseDownloadQuotaError, "unsupported quota ledger schema"):
            self.reserve("cnki", "a")

    def test_existing_lock_times_out_without_changing_ledger(self) -> None:
        self.reserve("cnki", "existing")
        before = self.ledger.read_bytes()
        lock_path = self.ledger.with_suffix(self.ledger.suffix + ".lock")
        lock_path.write_text("locked", encoding="utf-8")
        self.addCleanup(lock_path.unlink, missing_ok=True)

        with self.assertRaisesRegex(ChineseDownloadQuotaError, "quota ledger is locked"):
            self.reserve("wanfang", "blocked")

        self.assertEqual(self.ledger.read_bytes(), before)

    def test_unknown_portal_is_rejected_before_writing(self) -> None:
        with self.assertRaisesRegex(ValueError, "portal must be cnki or wanfang"):
            self.reserve("cqvip", "a")

        self.assertFalse(self.ledger.exists())

    def test_unusable_parent_path_fails_as_quota_state_error(self) -> None:
        blocker = Path(self.temp.name) / "not-a-directory"
        blocker.write_text("blocked", encoding="utf-8")

        with self.assertRaisesRegex(ChineseDownloadQuotaError, "could not prepare quota directory"):
            reserve_chinese_download(
                blocker / "quota.json",
                portal="cnki",
                record_id="a",
                now=self.now,
                lock_timeout=0.05,
            )

    def test_status_reports_combined_and_per_portal_policy(self) -> None:
        self.reserve("cnki", "one")

        status = inspect_chinese_download_quota(
            self.ledger,
            now=self.now,
            combined_limit=10,
            cnki_limit=4,
            wanfang_limit=6,
        )

        self.assertEqual(status["used"], 1)
        self.assertEqual(status["remaining"], 9)
        self.assertEqual(status["cnki_used"], 1)
        self.assertEqual(status["cnki_remaining"], 3)
        self.assertEqual(status["wanfang_used"], 0)
        self.assertEqual(status["wanfang_remaining"], 6)
        self.assertFalse(status["lock_exists"])
        self.assertFalse(status["stale_lock"])

    def test_repair_removes_only_a_stale_pid_lock(self) -> None:
        lock_path = self.ledger.with_suffix(self.ledger.suffix + ".lock")
        lock_path.write_text("pid=2147483647\n", encoding="ascii")

        status = inspect_chinese_download_quota(self.ledger, now=self.now)
        repaired = repair_chinese_download_quota_lock(self.ledger)

        self.assertTrue(status["stale_lock"])
        self.assertTrue(status["repairable"])
        self.assertTrue(repaired["removed"])
        self.assertFalse(lock_path.exists())

    def test_repair_refuses_a_live_pid_lock(self) -> None:
        lock_path = self.ledger.with_suffix(self.ledger.suffix + ".lock")
        lock_path.write_text(f"pid={os.getpid()}\n", encoding="ascii")

        with self.assertRaisesRegex(ChineseDownloadQuotaError, "active process"):
            repair_chinese_download_quota_lock(self.ledger)

        self.assertTrue(lock_path.exists())

    def test_repair_refuses_an_unparseable_lock(self) -> None:
        lock_path = self.ledger.with_suffix(self.ledger.suffix + ".lock")
        lock_path.write_text("unknown owner\n", encoding="ascii")

        with self.assertRaisesRegex(ChineseDownloadQuotaError, "invalid quota lock"):
            repair_chinese_download_quota_lock(self.ledger)

        self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
