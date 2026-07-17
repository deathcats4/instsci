import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from instsci import cli
from instsci.cli import _chinese_quota_ledger_path, _verify_chinese_pdf_identity
from instsci.config import Config


class ChineseBatchSafetyTests(TestCase):
    def test_quota_ledger_lives_under_config_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            config = Config(cache_dir=str(Path(tmp) / "cache"))

            path = _chinese_quota_ledger_path(config)

        self.assertEqual(path, Path(tmp) / "cache" / "chinese_download_quota.json")

    def test_pdf_identity_requires_author_only_after_disambiguation(self) -> None:
        optional = _verify_chinese_pdf_identity(
            "同题研究",
            "李四",
            "同题研究 张三",
            author_required=False,
        )
        required = _verify_chinese_pdf_identity(
            "同题研究",
            "李四",
            "同题研究 张三",
            author_required=True,
        )

        self.assertTrue(optional["verified"])
        self.assertFalse(required["verified"])
        self.assertFalse(required["author_match"])

    def test_cnki_batch_reserves_quota_and_passes_first_author(self) -> None:
        source = inspect.getsource(cli.cnki_batch)

        self.assertIn("reserve_chinese_download", source)
        self.assertLess(source.index("quota = reserve_chinese_download"), source.index("result = capture_cnki_pdf"))
        self.assertIn("first_author=first_author", source)
        self.assertIn('"daily_limit_reached"', source)
        self.assertIn('"quota_state_error"', source)
        self.assertIn('"ambiguous_search_result"', source)

    def test_wanfang_batch_inspects_then_reserves_before_capture(self) -> None:
        source = inspect.getsource(cli.wanfang_batch)

        self.assertIn("inspect_wanfang_result_download", source)
        self.assertLess(source.index("selection = inspect_wanfang_result_download"), source.index("quota = reserve_chinese_download"))
        self.assertLess(source.index("quota = reserve_chinese_download"), source.index("result = capture_wanfang_pdf"))
        self.assertIn("first_author=first_author", source)
        self.assertIn("selection=selection", source)


if __name__ == "__main__":
    import unittest

    unittest.main()
