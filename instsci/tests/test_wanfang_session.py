from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import parse_qs, urlparse

from instsci.wanfang_session import (
    choose_wanfang_download_candidate,
    classify_wanfang_page,
    extract_wanfang_download_candidates_from_html,
    load_wanfang_batch,
    safe_wanfang_url,
    summarize_wanfang_capture_result,
    wanfang_downloaded_pdf_path,
    wanfang_next_action_for_result,
    wanfang_search_url,
    click_wanfang_result_download,
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

    def test_extract_wanfang_download_candidates_requires_exact_title_in_same_result_row(self) -> None:
        html = """
        <section class="search-results">
          <div class="result-item">
            <a class="title" title="纳米矿物在地球科学的研究进展">纳米矿物在地球科学的研究进展</a>
            <button class="wf-list-button">下载</button>
          </div>
          <div class="result-item">
            <a class="title" title="纳米矿物在地球科学的研究进展述评">纳米矿物在地球科学的研究进展述评</a>
            <button class="wf-list-button">下载</button>
          </div>
        </section>
        """

        candidates = extract_wanfang_download_candidates_from_html(
            html,
            title="纳米矿物在地球科学的研究进展",
        )
        chosen = choose_wanfang_download_candidate(candidates, title="纳米矿物在地球科学的研究进展")

        self.assertEqual([candidate["row_title_match"] for candidate in candidates], [True, False])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["row_title"], "纳米矿物在地球科学的研究进展")

    def test_extract_wanfang_download_candidates_ignores_multi_result_right_list_container(self) -> None:
        html = """
        <section class="right-list">
          <div class="normal-list periodical-list">
            <a class="title" title="纳米矿物在地球科学的研究进展">纳米矿物在地球科学的研究进展</a>
          </div>
          <div class="normal-list periodical-list">
            <a class="title" title="磁铁矿纳米矿物学研究进展">磁铁矿纳米矿物学研究进展</a>
            <div class="wf-list-button">下载</div>
          </div>
        </section>
        """

        candidates = extract_wanfang_download_candidates_from_html(
            html,
            title="纳米矿物在地球科学的研究进展",
        )

        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0]["row_title_match"])
        self.assertIsNone(choose_wanfang_download_candidate(candidates, title="纳米矿物在地球科学的研究进展"))

    def test_click_wanfang_result_download_rejects_changed_candidate_before_click(self) -> None:
        class DriftPage:
            def __init__(self) -> None:
                self.calls = 0

            def evaluate(self, _script: str, arg: object) -> object:
                self.calls += 1
                if self.calls == 1:
                    return [
                        {
                            "index": 0,
                            "candidate_id": "candidate-a",
                            "text": "下载",
                            "href": "https://example.test/download-a",
                            "cls": "wf-list-button",
                            "row_title_match": True,
                            "row_title": "目标题名",
                        }
                    ]
                if isinstance(arg, dict) and arg.get("candidate_id") == "candidate-a":
                    return {"clicked": False, "result_found": False, "reason": "candidate_changed"}
                return {"clicked": True, "result_found": True}

        result = click_wanfang_result_download(DriftPage(), title="目标题名")

        self.assertFalse(result["clicked"])
        self.assertEqual(result["reason"], "candidate_changed")

    def test_wanfang_next_action_points_to_search_results_when_no_exact_title(self) -> None:
        self.assertEqual(
            wanfang_next_action_for_result("capture_failed", {"reason": "no_exact_title_result"}),
            "inspect_wanfang_search_results_or_refine_query",
        )
        self.assertEqual(
            wanfang_next_action_for_result("capture_failed", {"download_click": {"reason": "no_exact_title_result"}}),
            "inspect_wanfang_search_results_or_refine_query",
        )

    def test_wanfang_next_action_points_to_visible_page_when_no_pdf_exists(self) -> None:
        self.assertEqual(
            wanfang_next_action_for_result("capture_failed", {"download_click": {"reason": "no_download_control"}}),
            "inspect_visible_wanfang_page_and_retry",
        )
        self.assertEqual(
            wanfang_next_action_for_result("capture_failed", {"download_click": {"reason": "candidate_changed"}}),
            "inspect_visible_wanfang_page_and_retry",
        )

    def test_summarize_wanfang_capture_requires_existing_pdf_file(self) -> None:
        summary = summarize_wanfang_capture_result(
            {
                "pdf_path": "",
                "pdf_header_valid": True,
                "size_bytes": 50000,
                "filename_title_match": True,
            },
            title="目标题名",
            text="",
            strict_title_match=True,
        )

        self.assertEqual(summary["file_status"], "missing")
        self.assertEqual(summary["standard_status"], "capture_failed")

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

    def test_load_wanfang_batch_preserves_explicit_first_author(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "batch.json"
            source.write_text(
                '[{"record_id":"safe","title":"测试题名","first_author":"张三","authors":["李四"]}]',
                encoding="utf-8",
            )

            rows = load_wanfang_batch(source)

        self.assertEqual(rows[0]["first_author"], "张三")

    def test_load_wanfang_batch_reports_invalid_authors_with_row_number(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "batch.json"
            source.write_text(
                '[{"record_id":"safe","title":"测试题名","authors":"张三;李四"}]',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Wanfang batch row 1.*ordered JSON array"):
                load_wanfang_batch(source)

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
