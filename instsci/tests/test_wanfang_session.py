from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import parse_qs, urlparse

from instsci.wanfang_session import (
    choose_wanfang_download_candidate,
    classify_wanfang_page,
    load_wanfang_batch,
    safe_wanfang_url,
    wanfang_downloaded_pdf_path,
    wanfang_next_action_for_result,
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

    def test_classify_wanfang_auth_page_requires_login(self) -> None:
        self.assertEqual(
            classify_wanfang_page("https://login.example.edu/sso", "统一身份认证", auth_domains=("login.example.edu",)),
            "auth_required",
        )

    def test_classify_wanfang_portal_is_ready_not_login_verified(self) -> None:
        self.assertEqual(
            classify_wanfang_page("https://s.wanfangdata.com.cn/paper?q=测试", "万方数据"),
            "portal_ready",
        )

    def test_choose_wanfang_download_candidate_requires_exact_result_row(self) -> None:
        candidates = [
            {
                "index": 1,
                "text": "下载",
                "cls": "wf-list-button",
                "row_title_match": False,
                "page_title_match": True,
                "title_y_distance": 40,
            },
            {
                "index": 2,
                "text": "下载",
                "cls": "wf-list-button",
                "row_title_match": True,
                "page_title_match": True,
                "title_y_distance": 300,
            },
        ]

        chosen = choose_wanfang_download_candidate(candidates, title="纳米矿物在地球科学的研究进展")

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["index"], 2)

    def test_choose_wanfang_download_candidate_rejects_page_level_only_match(self) -> None:
        candidates = [
            {
                "index": 1,
                "text": "下载",
                "cls": "wf-list-button",
                "row_title_match": False,
                "page_title_match": True,
                "title_y_distance": 40,
            }
        ]

        self.assertIsNone(
            choose_wanfang_download_candidate(candidates, title="深水页岩黄铁矿特征、形成及意义")
        )

    def test_wanfang_next_action_points_to_search_results_when_no_exact_title(self) -> None:
        self.assertEqual(
            wanfang_next_action_for_result("capture_failed", {"reason": "no_exact_title_result"}),
            "inspect_wanfang_search_results_or_refine_query",
        )
        self.assertEqual(
            wanfang_next_action_for_result("capture_failed", {"download_click": {"reason": "no_exact_title_result"}}),
            "inspect_wanfang_search_results_or_refine_query",
        )

    def test_wanfang_downloaded_pdf_path_ignores_empty_or_directory_paths(self) -> None:
        self.assertIsNone(wanfang_downloaded_pdf_path({}))
        self.assertIsNone(wanfang_downloaded_pdf_path({"pdf_path": ""}))
        self.assertIsNone(wanfang_downloaded_pdf_path({"pdf_path": "."}))

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
