import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from instsci.chinese_download_quota import (
    DAILY_DOWNLOAD_LIMIT,
    ChineseDownloadQuotaError,
    reserve_chinese_download,
)


class ChineseDownloadQuotaTests(TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / "chinese_download_quota.json"
        self.now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    def reserve(self, portal: str, record_id: str, *, now: datetime | None = None, limit: int = 100):
        return reserve_chinese_download(
            self.ledger,
            portal=portal,
            record_id=record_id,
            now=now or self.now,
            limit=limit,
            lock_timeout=0.05,
        )

    def test_default_daily_limit_is_100(self) -> None:
        self.assertEqual(DAILY_DOWNLOAD_LIMIT, 100)

    def test_first_reservation_writes_auditable_ledger(self) -> None:
        result = self.reserve("cnki", "CNKI-1")

        self.assertTrue(result.allowed)
        self.assertEqual(result.used, 1)
        self.assertEqual(result.remaining, 99)
        self.assertEqual(result.date, "2026-07-17")
        payload = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "instsci.chinese_download_quota.v1")
        self.assertEqual(payload["days"]["2026-07-17"][0]["portal"], "cnki")
        self.assertEqual(payload["days"]["2026-07-17"][0]["record_id"], "CNKI-1")

    def test_cnki_and_wanfang_share_one_daily_limit(self) -> None:
        first = self.reserve("cnki", "a")
        second = self.reserve("wanfang", "b")

        self.assertEqual((first.used, second.used), (1, 2))
        self.assertEqual(second.remaining, 98)

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

    def test_combined_101st_attempt_is_blocked_without_appending(self) -> None:
        for index in range(100):
            self.assertTrue(self.reserve("cnki", str(index)).allowed)

        blocked = self.reserve("wanfang", "101")

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "daily_limit_reached")
        self.assertEqual(blocked.used, 100)
        self.assertEqual(blocked.remaining, 0)
        payload = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["days"]["2026-07-17"]), 100)

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


if __name__ == "__main__":
    import unittest

    unittest.main()
