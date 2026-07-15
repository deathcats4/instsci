from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import parse_qs, urlparse

from instsci.wanfang_session import (
    classify_wanfang_page,
    load_wanfang_batch,
    safe_wanfang_url,
    wanfang_search_url,
)


class WanfangSessionTests(TestCase):
    def test_wanfang_search_url_sets_query(self) -> None:
        url = wanfang_search_url("洪海沟 铀矿")
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.hostname, "s.wanfangdata.com.cn")
        self.assertEqual(parsed.path, "/paper")
        self.assertEqual(params["q"], ["洪海沟 铀矿"])

    def test_safe_wanfang_url_drops_generated_download_query(self) -> None:
        self.assertEqual(
            safe_wanfang_url("https://oss.wanfangdata.com.cn/Fulltext/Download?transaction=secret&authToken=secret"),
            "https://oss.wanfangdata.com.cn/Fulltext/Download",
        )

    def test_classify_wanfang_verification_page_requires_user(self) -> None:
        self.assertEqual(
            classify_wanfang_page("https://www.wanfangdata.com.cn/captcha", "安全验证"),
            "human_verification_required",
        )

    def test_load_wanfang_batch_validates_records(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "batch.json"
            source.write_text(
                '[{"record_id":"ykdz202305006","title":"伊犁盆地南缘洪海沟矿床头屯河组下段含矿砂体结构及氧化带分布特征","url":"https://s.wanfangdata.com.cn/paper?q=洪海沟","zotero_item_key":"JGN5J75A"}]',
                encoding="utf-8",
            )
            rows = load_wanfang_batch(source)

        self.assertEqual(rows[0]["record_id"], "ykdz202305006")
        self.assertEqual(rows[0]["query"], rows[0]["title"])
        self.assertEqual(rows[0]["zotero_item_key"], "JGN5J75A")

    def test_load_wanfang_batch_allows_explicit_query(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "batch.json"
            source.write_text(
                '[{"record_id":"ykdz202305006","title":"测试题名","query":"洪海沟"}]',
                encoding="utf-8",
            )
            rows = load_wanfang_batch(source)

        self.assertEqual(rows[0]["query"], "洪海沟")

    def test_load_wanfang_batch_rejects_unsafe_record_id(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "batch.json"
            source.write_text(
                '[{"record_id":"../escape","title":"测试题名"}]',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsafe record_id"):
                load_wanfang_batch(source)

    def test_load_wanfang_batch_rejects_non_wanfang_url(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "batch.json"
            source.write_text(
                '[{"record_id":"safe","title":"测试题名","url":"https://example.org/article"}]',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid Wanfang URL"):
                load_wanfang_batch(source)
