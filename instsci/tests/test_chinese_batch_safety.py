from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

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
        reference_only = _verify_chinese_pdf_identity(
            "同题研究",
            "李四",
            "同题研究 王五 摘要内容 参考文献 李四，另一项研究",
            author_required=True,
            author_signature_text="同题研究\n王五，张三\n某大学\n摘要",
        )

        self.assertTrue(optional["verified"])
        self.assertFalse(required["verified"])
        self.assertFalse(required["author_match"])
        self.assertFalse(reference_only["verified"])
        self.assertFalse(reference_only["author_match"])

if __name__ == "__main__":
    import unittest

    unittest.main()
