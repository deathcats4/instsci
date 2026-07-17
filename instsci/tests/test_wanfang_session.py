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
    def _duplicate_title_author_html(self, *, second_author: str = "李四") -> str:
        return f"""
        <section class="search-results">
          <div class="result-item">
            <a class="title" title="同题研究">同题研究</a>
            <span class="author">张三</span>
            <button class="wf-list-button">下载</button>
          </div>
          <div class="result-item">
            <a class="title" title="同题研究">同题研究</a>
            <span class="author">{second_author}</span>
            <button class="wf-list-button">下载</button>
          </div>
        </section>
        """

    def test_wanfang_duplicate_titles_require_unique_same_row_first_author(self) -> None:
        candidates = extract_wanfang_download_candidates_from_html(
            self._duplicate_title_author_html(),
            title="同题研究",
            first_author="李四",
        )

        chosen = choose_wanfang_download_candidate(
            candidates,
            title="同题研究",
            first_author="李四",
        )

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["row_index"], 1)
        self.assertEqual(chosen["row_author_text"], "李四")
        self.assertEqual(chosen["title_candidate_count"], 2)
        self.assertEqual(chosen["author_match_count"], 1)
        self.assertTrue(chosen["author_disambiguation_used"])

    def test_wanfang_duplicate_titles_without_author_are_ambiguous(self) -> None:
        candidates = extract_wanfang_download_candidates_from_html(
            self._duplicate_title_author_html(),
            title="同题研究",
        )

        chosen = choose_wanfang_download_candidate(candidates, title="同题研究")

        self.assertIsNone(chosen)

    def test_wanfang_duplicate_titles_with_repeated_author_are_ambiguous(self) -> None:
        candidates = extract_wanfang_download_candidates_from_html(
            self._duplicate_title_author_html(second_author="张三"),
            title="同题研究",
            first_author="张三",
        )

        chosen = choose_wanfang_download_candidate(
            candidates,
            title="同题研究",
            first_author="张三",
        )

        self.assertIsNone(chosen)

    def test_wanfang_target_author_only_in_second_position_is_ambiguous(self) -> None:
        html = """
        <section class="search-results">
          <div class="result-item">
            <a class="title" title="同题研究">同题研究</a>
            <span class="authors"><a href="/author/王五">王五</a>，<a href="/author/李四">李四</a></span>
            <button class="wf-list-button">下载</button>
          </div>
          <div class="result-item">
            <a class="title" title="同题研究">同题研究</a>
            <span class="authors"><a href="/author/赵六">赵六</a>，<a href="/author/孙七">孙七</a></span>
            <button class="wf-list-button">下载</button>
          </div>
        </section>
        """
        candidates = extract_wanfang_download_candidates_from_html(
            html,
            title="同题研究",
            first_author="李四",
        )

        chosen = choose_wanfang_download_candidate(
            candidates,
            title="同题研究",
            first_author="李四",
        )

        self.assertIsNone(chosen)
        self.assertEqual(candidates[0]["row_first_author"], "王五")

    def test_wanfang_unique_title_remains_compatible_without_author(self) -> None:
        html = """
        <div class="result-item">
          <a class="title" title="唯一题名">唯一题名</a>
          <button class="wf-list-button">下载</button>
        </div>
        """
        candidates = extract_wanfang_download_candidates_from_html(html, title="唯一题名")

        chosen = choose_wanfang_download_candidate(candidates, title="唯一题名")

        self.assertIsNotNone(chosen)
        self.assertFalse(chosen["author_disambiguation_used"])

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

    def test_click_wanfang_result_download_rejects_changed_author_before_click(self) -> None:
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
                            "row_title": "同题研究",
                            "row_index": 0,
                            "row_author_text": "张三",
                            "row_authors": ["张三"],
                            "row_first_author": "张三",
                        },
                        {
                            "index": 1,
                            "candidate_id": "candidate-b",
                            "text": "下载",
                            "href": "https://example.test/download-b",
                            "cls": "wf-list-button",
                            "row_title_match": True,
                            "row_title": "同题研究",
                            "row_index": 1,
                            "row_author_text": "李四",
                            "row_authors": ["李四"],
                            "row_first_author": "李四",
                        },
                    ]
                if isinstance(arg, dict) and arg.get("candidate_id") == "candidate-b":
                    return {"clicked": False, "result_found": False, "reason": "candidate_changed"}
                return {"clicked": True, "result_found": True}

        result = click_wanfang_result_download(
            DriftPage(),
            title="同题研究",
            first_author="李四",
        )

        self.assertFalse(result["clicked"])
        self.assertEqual(result["reason"], "candidate_changed")
        self.assertTrue(result["author_disambiguation_used"])

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

    def test_summarize_wanfang_capture_requires_author_after_disambiguation(self) -> None:
        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-" + b"x" * 12000)
            result = {
                "pdf_path": str(pdf_path),
                "pdf_header_valid": True,
                "size_bytes": pdf_path.stat().st_size,
            }

            conflict = summarize_wanfang_capture_result(
                result,
                title="同题研究",
                first_author="李四",
                author_required=True,
                text="同题研究 张三",
                author_signature_text="同题研究\n张三\n摘要",
                strict_title_match=True,
            )
            success = summarize_wanfang_capture_result(
                result,
                title="同题研究",
                first_author="李四",
                author_required=True,
                text="同题研究 李四",
                author_signature_text="同题研究\n李四\n摘要",
                strict_title_match=True,
            )

            reference_only = summarize_wanfang_capture_result(
                result,
                title="同题研究",
                first_author="李四",
                author_required=True,
                text="同题研究 王五 摘要内容 参考文献 李四，另一项研究",
                author_signature_text="同题研究\n王五，张三\n某大学\n摘要",
                strict_title_match=True,
            )

        self.assertFalse(conflict["author_match"])
        self.assertEqual(conflict["file_status"], "unverified")
        self.assertEqual(conflict["standard_status"], "pdf_candidate_conflict")
        self.assertTrue(success["author_match"])
        self.assertEqual(success["file_status"], "success")
        self.assertFalse(reference_only["author_match"])
        self.assertEqual(reference_only["standard_status"], "pdf_candidate_conflict")

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
