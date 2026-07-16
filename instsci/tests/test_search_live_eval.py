import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from typer.testing import CliRunner

from instsci import search_live_eval
from instsci import multi_search
from instsci.cli import app
from instsci.search_benchmark import validate_release_gate_report
from instsci.search_live_eval import (
    evaluate_live_evaluation_gate,
    load_query_set,
    run_live_evaluation,
    validate_live_query_set,
    validate_live_evaluation_manifest,
    validate_relevance_review_packet,
)


def _legacy_search_payload(results: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "instsci.search_results.v1",
        "count": len(results),
        "results": results,
    }


def _hybrid_search_payload(results: list[dict[str, object]]) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for position, result in enumerate(results, 1):
        record = dict(result)
        doi = str(record.get("doi") or "")
        canonical = str(record.get("canonical_work_id") or (f"doi:{doi}" if doi else f"title:record-{position}"))
        record.setdefault("index", position)
        record.setdefault("title", doi or f"Record {position}")
        record.setdefault("canonical_work_id", canonical)
        record.setdefault("version_family_id", canonical)
        record.setdefault("version_type", "unknown")
        record.setdefault("related_versions", [])
        record.setdefault("retrieval_provenance", [])
        records.append(record)
    return {
        "schema": "instsci.search_results.v2",
        "query": "topic",
        "count": len(records),
        "query_plan": {"schema": "instsci.query_plan.v1", "strategy": "hybrid", "channels": []},
        "source_status": {},
        "results": records,
    }


class SearchLiveEvaluationTests(TestCase):
    def test_load_query_set_accepts_strings_and_objects(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.json"
            path.write_text(
                json.dumps(
                    [
                        "sulfur isotope uranium",
                        {"id": "q_custom", "query": "pyrite geochemistry", "year": "2020-"},
                    ]
                ),
                encoding="utf-8",
            )

            rows = load_query_set(path)

        self.assertEqual(rows[0], {"id": "q0001", "query": "sulfur isotope uranium", "year": ""})
        self.assertEqual(rows[1], {"id": "q_custom", "query": "pyrite geochemistry", "year": "2020-"})

    def test_validate_live_query_set_reports_contract_errors_and_warnings(self) -> None:
        report = validate_live_query_set(
            {
                "queries": [
                    "",
                    {"id": "Q 1", "query": "pyrite sulfur isotope", "year": 2020},
                    {"id": "Q-1", "query": "pyrite sulfur isotope", "year": "2020-"},
                    {"id": "q2", "text": "uranium fluid inclusion", "year_range": "bad/year"},
                    42,
                ]
            }
        )

        self.assertEqual(report["schema"], "instsci.search_query_set_validation.v1")
        self.assertFalse(report["valid"])
        self.assertIn("queries[0].query is required", report["errors"])
        self.assertIn("queries[2].id duplicates normalized id q_1", report["errors"])
        self.assertIn("queries[3].year must be empty or a year/range like 2020, 2020-, -2024, or 2020-2024", report["errors"])
        self.assertIn("queries[4] must be a string or object", report["errors"])
        self.assertIn("queries[0] is a string row; explicit stable ids are recommended for live evaluation", report["warnings"])
        self.assertIn("query_count below recommended live-eval minimum of 10", report["warnings"])
        self.assertEqual(report["summary"]["query_count"], 4)
        self.assertEqual(report["summary"]["stable_id_count"], 3)

    def test_cli_search_query_set_validate_writes_report_and_fails_invalid_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            query_path = Path(tmp) / "queries.json"
            output_path = Path(tmp) / "query_set_validation.json"
            query_path.write_text(
                json.dumps({"queries": [{"id": "q1", "query": "topic"}, {"id": "q1", "query": "topic again"}]}),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-query-set-validate", str(query_path), "--output", str(output_path)])
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["schema"], "instsci.search_query_set_validation.v1")
        self.assertFalse(payload["valid"])
        self.assertIn("queries[1].id duplicates normalized id q1", payload["errors"])

    def test_run_live_evaluation_writes_manifest_results_and_pool(self) -> None:
        def fake_runner(query, *, limit, year_range, sources, email, strategy):
            del limit, year_range, sources, email
            suffix = "legacy" if strategy == "legacy" else "hybrid"
            result = multi_search.MergedSearchResult(
                title=f"{query} {suffix}",
                doi=f"10.1000/{suffix}",
                sources=["semantic_scholar"],
            )
            return multi_search.MultiSearchResponse(
                results=[result],
                source_status={"semantic_scholar": {"status": "success", "count": 1}},
                query_plan={"schema": "instsci.query_plan.v1", "strategy": strategy},
            )

        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "live_eval"
            manifest = run_live_evaluation(
                [{"id": "q1", "query": "topic", "year": ""}],
                output_dir,
                search_runner=fake_runner,
                limit=5,
                sources="semantic_scholar",
                email="reader@example.edu",
            )

            manifest_path = output_dir / "manifest.json"
            legacy_path = Path(manifest["queries"][0]["legacy_result"])
            hybrid_path = Path(manifest["queries"][0]["hybrid_result"])
            pool_path = Path(manifest["queries"][0]["pool"])

            loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
            pool_payload = json.loads(pool_path.read_text(encoding="utf-8"))
            review_packet_path = Path(manifest["review_packet"])
            review_packet = json.loads(review_packet_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema"], "instsci.search_live_evaluation.v1")
        self.assertEqual(loaded_manifest["query_count"], 1)
        self.assertEqual(legacy_payload["query"], "topic")
        self.assertEqual(pool_payload["schema"], "instsci.relevance_pool.v1")
        self.assertEqual(review_packet["schema"], "instsci.relevance_review_packet.v1")
        self.assertEqual(review_packet["query_count"], 1)
        self.assertEqual(review_packet["judgment_count"], 2)
        self.assertEqual(review_packet["queries"][0]["query_id"], "q1")
        self.assertEqual(review_packet["queries"][0]["query"], "topic")
        self.assertTrue(review_packet["review"]["anonymous"])
        self.assertTrue(all("pool_sources" not in item for item in review_packet["queries"][0]["judgments"]))
        self.assertTrue(all("retrieval_provenance" not in item for item in review_packet["queries"][0]["judgments"]))
        self.assertEqual(manifest["queries"][0]["status"], "success")
        self.assertEqual(manifest["queries"][0]["legacy_count"], 1)
        self.assertEqual(manifest["queries"][0]["hybrid_count"], 1)

    def test_run_live_evaluation_records_search_contract_artifacts(self) -> None:
        def fake_runner(query, *, limit, year_range, sources, email, strategy):
            del limit, year_range, sources, email
            if strategy == "hybrid":
                result = multi_search.MergedSearchResult(
                    title=f"{query} hybrid",
                    doi="10.1000/hybrid",
                    sources=["openalex"],
                    retrieval_provenance=[
                        {
                            "provider": "openalex",
                            "channel": "openalex_keyword",
                            "query_variant": "q_keyword_1",
                            "rank": 1,
                            "weight": 1.0,
                        }
                    ],
                    fusion_score=1.0 / 61,
                    rank_components={"rrf": 1.0 / 61},
                )
                return multi_search.MultiSearchResponse(
                    results=[result],
                    source_status={
                        "openalex_keyword:q_keyword_1": {
                            "provider": "openalex",
                            "channel": "openalex_keyword",
                            "query_variant": "q_keyword_1",
                            "status": "success",
                            "count": 1,
                            "retryable": False,
                        }
                    },
                    query_plan={
                        "schema": "instsci.query_plan.v1",
                        "strategy": "hybrid",
                        "channels": [
                            {
                                "provider": "openalex",
                                "channel": "openalex_keyword",
                                "query_variant": "q_keyword_1",
                                "weight": 1.0,
                            }
                        ],
                    },
                )
            return multi_search.MultiSearchResponse(
                results=[multi_search.MergedSearchResult(title=f"{query} legacy", doi="10.1000/legacy")],
                source_status={"semantic_scholar": {"status": "success", "count": 1}},
                query_plan={"schema": "instsci.query_plan.v1", "strategy": "legacy"},
            )

        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "live_eval"
            manifest = run_live_evaluation(
                [{"id": "q1", "query": "topic", "year": ""}],
                output_dir,
                search_runner=fake_runner,
                limit=5,
                sources="semantic_scholar,openalex",
            )
            item = manifest["queries"][0]
            contract_path = Path(item["hybrid_contract_report"])
            contract_report = json.loads(contract_path.read_text(encoding="utf-8"))

        self.assertEqual(item["legacy_schema"], "instsci.search_results.v1")
        self.assertEqual(item["hybrid_schema"], "instsci.search_results.v2")
        self.assertTrue(item["hybrid_contract_valid"])
        self.assertEqual(item["hybrid_contract_errors"], [])
        self.assertEqual(contract_report["schema"], "instsci.search_contract_validation.v1")
        self.assertTrue(contract_report["valid"], contract_report)

    def test_run_live_evaluation_resume_reuses_successful_query_artifacts(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_runner(query, *, limit, year_range, sources, email, strategy):
            del limit, year_range, sources, email
            calls.append((query, strategy))
            return multi_search.MultiSearchResponse(
                results=[multi_search.MergedSearchResult(title=query, doi=f"10.1000/{query}-{strategy}")],
                source_status={"semantic_scholar": {"status": "success", "count": 1}},
                query_plan={"schema": "instsci.query_plan.v1", "strategy": strategy},
            )

        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "live_eval"
            existing_query = output_dir / "q1"
            existing_query.mkdir(parents=True)
            (existing_query / "legacy.json").write_text(
                json.dumps({"results": [{"doi": "10.1000/legacy"}]}),
                encoding="utf-8",
            )
            (existing_query / "hybrid.json").write_text(
                json.dumps({"results": [{"doi": "10.1000/hybrid"}]}),
                encoding="utf-8",
            )
            (existing_query / "judgments_pool.json").write_text(
                json.dumps({"judgments": [{"id": "doi:10.1000/hybrid", "grade": None}]}),
                encoding="utf-8",
            )
            (output_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "queries": [
                            {
                                "id": "q1",
                                "query": "old topic",
                                "status": "success",
                                "legacy_count": 1,
                                "hybrid_count": 1,
                                "legacy_result": str(existing_query / "legacy.json"),
                                "hybrid_result": str(existing_query / "hybrid.json"),
                                "pool": str(existing_query / "judgments_pool.json"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            manifest = run_live_evaluation(
                [
                    {"id": "q1", "query": "old topic", "year": ""},
                    {"id": "q2", "query": "new topic", "year": ""},
                ],
                output_dir,
                search_runner=fake_runner,
                sources="semantic_scholar",
                resume=True,
            )

        self.assertEqual(calls, [("new topic", "legacy"), ("new topic", "hybrid")])
        self.assertEqual(manifest["queries"][0]["status"], "success")
        self.assertTrue(manifest["queries"][0]["resumed"])
        self.assertEqual(manifest["queries"][1]["status"], "success")

    def test_cli_search_live_eval_writes_manifest_without_acquisition(self) -> None:
        runner = CliRunner()

        def fake_runner(query, *, limit, year_range, sources, email, strategy):
            del limit, year_range, sources, email
            return multi_search.MultiSearchResponse(
                results=[multi_search.MergedSearchResult(title=query, doi=f"10.1000/{strategy}")],
                source_status={"semantic_scholar": {"status": "success", "count": 1}},
                query_plan={"schema": "instsci.query_plan.v1", "strategy": strategy},
            )

        with TemporaryDirectory() as tmp:
            queries_path = Path(tmp) / "queries.json"
            output_dir = Path(tmp) / "eval"
            queries_path.write_text(json.dumps(["topic"]), encoding="utf-8")
            with patch("instsci.search_live_eval.multi_search.search_with_status", side_effect=fake_runner):
                result = runner.invoke(
                    app,
                    [
                        "search-live-eval",
                        str(queries_path),
                        "--output",
                        str(output_dir),
                        "--sources",
                        "semantic_scholar",
                    ],
                )
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("judgments_review_packet.json", result.output)
        self.assertEqual(manifest["schema"], "instsci.search_live_evaluation.v1")
        self.assertEqual(manifest["query_set_validation"], str(output_dir / "query_set_validation.json"))
        self.assertTrue(manifest["query_set_validation_valid"])
        self.assertGreaterEqual(manifest["query_set_validation_warning_count"], 1)
        self.assertIn("legacy_result", manifest["queries"][0])
        self.assertIn("hybrid_result", manifest["queries"][0])

    def test_cli_search_live_eval_rejects_invalid_query_set_before_provider_calls(self) -> None:
        runner = CliRunner()

        with TemporaryDirectory() as tmp:
            queries_path = Path(tmp) / "queries.json"
            output_dir = Path(tmp) / "eval"
            queries_path.write_text(
                json.dumps({"queries": [{"id": "q1", "query": ""}]}),
                encoding="utf-8",
            )
            with patch("instsci.search_live_eval.multi_search.search_with_status") as search_with_status:
                result = runner.invoke(
                    app,
                    [
                        "search-live-eval",
                        str(queries_path),
                        "--output",
                        str(output_dir),
                    ],
                )
            validation = json.loads((output_dir / "query_set_validation.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertFalse(search_with_status.called)
        self.assertEqual(validation["schema"], "instsci.search_query_set_validation.v1")
        self.assertFalse(validation["valid"])
        self.assertIn("queries[0].query is required", validation["errors"])
        self.assertFalse((output_dir / "manifest.json").exists())

    def test_cli_search_live_eval_accepts_resume(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            queries_path = Path(tmp) / "queries.json"
            output_dir = Path(tmp) / "eval"
            queries_path.write_text(json.dumps(["topic"]), encoding="utf-8")
            with patch("instsci.search_live_eval.run_live_evaluation") as run_eval:
                run_eval.return_value = {
                    "schema": "instsci.search_live_evaluation.v1",
                    "query_count": 1,
                    "queries": [{"status": "success"}],
                }
                result = runner.invoke(
                    app,
                    [
                        "search-live-eval",
                        str(queries_path),
                        "--output",
                        str(output_dir),
                        "--resume",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(run_eval.call_args.kwargs["resume"])

    def test_evaluate_live_evaluation_gate_aggregates_per_query_comparisons(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            q1 = output_dir / "q1"
            q2 = output_dir / "q2"
            q1.mkdir(parents=True)
            q2.mkdir(parents=True)
            (q1 / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/b"}, {"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            (q1 / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/a"}, {"doi": "10.1000/b"}])),
                encoding="utf-8",
            )
            (q1 / "judgments_pool.json").write_text(
                json.dumps({"doi:10.1000/a": 3, "doi:10.1000/b": 2}),
                encoding="utf-8",
            )
            (q2 / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/c"}, {"doi": "10.1000/d"}])),
                encoding="utf-8",
            )
            (q2 / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/c"}, {"doi": "10.1000/d"}])),
                encoding="utf-8",
            )
            (q2 / "judgments_pool.json").write_text(
                json.dumps({"doi:10.1000/c": 3, "doi:10.1000/d": 2}),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "queries": [
                            {
                                "id": "q1",
                                "query": "pyrite sulfur isotope",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                                "pool": "q1/judgments_pool.json",
                            },
                            {
                                "id": "q2",
                                "status": "success",
                                "legacy_result": "q2/legacy.json",
                                "hybrid_result": "q2/hybrid.json",
                                "pool": "q2/judgments_pool.json",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        self.assertEqual(report["schema"], "instsci.search_release_gate.v1")
        self.assertTrue(report["passed"])
        self.assertEqual(report["summary"]["query_count"], 2)
        self.assertEqual(report["summary"]["ndcg_improved_share"], 0.5)
        self.assertEqual(report["summary"]["data_issue_count"], 0)
        q1 = next(item for item in report["queries"] if item["query_id"] == "q1")
        self.assertEqual(q1["query"], "pyrite sulfur isotope")

    def test_evaluate_live_evaluation_gate_uses_graded_review_packet(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/b"}, {"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/a"}, {"doi": "10.1000/b"}])),
                encoding="utf-8",
            )
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [
                                    {"id": "doi:10.1000/a", "grade": 3},
                                    {"id": "doi:10.1000/b", "grade": 2},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "queries": [
                            {
                                "id": "q1",
                                "query": "pyrite sulfur isotope",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        self.assertTrue(report["passed"])
        self.assertEqual(report["summary"]["data_issue_count"], 0)
        self.assertEqual(report["summary"]["query_count"], 1)
        self.assertEqual(report["queries"][0]["query_id"], "q1")

    def test_evaluate_live_evaluation_gate_flags_invalid_hybrid_contract_artifact(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_results.v1",
                        "results": [{"index": 1, "doi": "10.1000/a"}],
                    }
                ),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_results.v2",
                        "query": "topic",
                        "count": 1,
                        "query_plan": {"schema": "instsci.query_plan.v1", "strategy": "hybrid", "channels": []},
                        "source_status": {},
                        "results": [{"index": 1, "doi": "10.1000/a", "title": "A"}],
                    }
                ),
                encoding="utf-8",
            )
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [{"id": "doi:10.1000/a", "grade": 3}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "queries": [
                            {
                                "id": "q1",
                                "query": "topic",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        self.assertFalse(report["passed"])
        self.assertIn(
            {"query_id": "q1", "reason": "hybrid_contract_invalid"},
            [
                {"query_id": item["query_id"], "reason": item["reason"]}
                for item in report["data_issues"]
            ],
        )
        self.assertIn("hybrid_contract_invalid", {item["type"] for item in report["release_gate_blockers"]})

    def test_evaluate_live_evaluation_gate_flags_hybrid_artifact_without_v2_schema(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_results.v1",
                        "results": [{"index": 1, "doi": "10.1000/a"}],
                    }
                ),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps({"results": [{"index": 1, "doi": "10.1000/a"}]}),
                encoding="utf-8",
            )
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [{"id": "doi:10.1000/a", "grade": 3}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "queries": [
                            {
                                "id": "q1",
                                "query": "topic",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        self.assertFalse(report["passed"])
        self.assertIn(
            {"query_id": "q1", "reason": "hybrid_contract_not_v2"},
            [
                {"query_id": item["query_id"], "reason": item["reason"]}
                for item in report["data_issues"]
            ],
        )
        self.assertIn("hybrid_contract_not_v2", {item["type"] for item in report["release_gate_blockers"]})

    def test_evaluate_live_evaluation_gate_flags_legacy_artifact_without_v1_schema(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps({"results": [{"index": 1, "doi": "10.1000/a"}]}),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"index": 1, "doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [{"id": "doi:10.1000/a", "grade": 3}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "queries": [
                            {
                                "id": "q1",
                                "query": "topic",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        self.assertFalse(report["passed"])
        self.assertIn(
            {"query_id": "q1", "reason": "legacy_contract_not_v1"},
            [
                {"query_id": item["query_id"], "reason": item["reason"]}
                for item in report["data_issues"]
            ],
        )
        self.assertIn("legacy_contract_not_v1", {item["type"] for item in report["release_gate_blockers"]})

    def test_evaluate_live_evaluation_gate_flags_ungraded_review_packet_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/b"}, {"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/a"}, {"doi": "10.1000/b"}])),
                encoding="utf-8",
            )
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [
                                    {"id": "doi:10.1000/a", "grade": 3},
                                    {"id": "doi:10.1000/b", "grade": None},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "queries": [
                            {
                                "id": "q1",
                                "query": "pyrite sulfur isotope",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["judgment_count"], 2)
        self.assertEqual(report["summary"]["graded_judgment_count"], 1)
        self.assertEqual(report["summary"]["ungraded_judgment_count"], 1)
        self.assertIn({"query_id": "q1", "reason": "ungraded_judgments", "judgment_count": "2", "graded_judgment_count": "1", "ungraded_judgment_count": "1"}, report["data_issues"])
        self.assertFalse(report["checks"]["all_judgments_graded"])

    def test_evaluate_live_evaluation_gate_blocks_invalid_query_set_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [{"id": "doi:10.1000/a", "grade": 3}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "query_set_validation": str(output_dir / "query_set_validation.json"),
                        "query_set_validation_valid": False,
                        "query_set_validation_error_count": 1,
                        "query_set_validation_warning_count": 0,
                        "queries": [
                            {
                                "id": "q1",
                                "query": "topic",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        self.assertFalse(report["passed"])
        self.assertIn("query_set_validation_invalid", [item["reason"] for item in report["data_issues"]])
        self.assertTrue(
            any(
                item["type"] == "data_issue" and item["reason"] == "query_set_validation_invalid"
                for item in report["release_gate_blockers"]
            )
        )

    def test_evaluate_live_evaluation_gate_reports_provider_failure_rate(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/b"}, {"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/a"}, {"doi": "10.1000/b"}])),
                encoding="utf-8",
            )
            (query_dir / "judgments_pool.json").write_text(
                json.dumps({"doi:10.1000/a": 3, "doi:10.1000/b": 2}),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "queries": [
                            {
                                "id": "q1",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                                "pool": "q1/judgments_pool.json",
                                "legacy_source_status": {
                                    "semantic_scholar": {"status": "success", "count": 2},
                                    "openalex": {"status": "rate_limited", "count": 0},
                                },
                                "hybrid_source_status": {
                                    "openalex_keyword:q_keyword_1": {"status": "success", "count": 2},
                                    "openalex_semantic:q_semantic_1": {
                                        "status": "authentication_required",
                                        "count": 0,
                                    },
                                    "crossref_keyword:q_keyword_1": {"status": "success", "count": 1},
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        self.assertEqual(report["summary"]["provider_status_count"], 5)
        self.assertEqual(report["summary"]["provider_failure_count"], 2)
        self.assertEqual(report["summary"]["provider_failure_rate"], 0.4)
        self.assertEqual(
            {(item["strategy"], item["source"], item["status"]) for item in report["provider_failures"]},
            {
                ("legacy", "openalex", "rate_limited"),
                ("hybrid", "openalex_semantic:q_semantic_1", "authentication_required"),
            },
        )

    def test_evaluate_live_evaluation_gate_builds_machine_readable_blockers(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(
                    _legacy_search_payload(
                        [
                            {"doi": "10.1000/a"},
                            {"doi": "10.1000/b"},
                            {"doi": "10.1000/c"},
                        ]
                    )
                ),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(
                    _hybrid_search_payload(
                        [
                            {"doi": "10.1000/c"},
                            {"doi": "10.1000/b"},
                            {"doi": "10.1000/a"},
                        ]
                    )
                ),
                encoding="utf-8",
            )
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [
                                    {"id": "doi:10.1000/a", "grade": 3},
                                    {"id": "doi:10.1000/b", "grade": 2},
                                    {"id": "doi:10.1000/c", "grade": 0},
                                    {"id": "doi:10.1000/d", "grade": None},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "queries": [
                            {
                                "id": "q1",
                                "query": "pyrite sulfur isotope",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                                "hybrid_source_status": {
                                    "openalex_semantic:q_semantic_1": {
                                        "status": "authentication_required",
                                        "count": 0,
                                    }
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        blockers = report["release_gate_blockers"]
        self.assertEqual(report["summary"]["release_gate_blocker_count"], 4)
        self.assertEqual(
            {(item["type"], item["query_id"]) for item in blockers},
            {
                ("ndcg_improved_share_below_threshold", "aggregate"),
                ("data_issue", "q1"),
                ("manual_review_required", "q1"),
                ("provider_failure", "q1"),
            },
        )
        improved_share = next(item for item in blockers if item["type"] == "ndcg_improved_share_below_threshold")
        self.assertEqual(improved_share["metric"], "ndcg@20_improved_share")
        self.assertTrue(improved_share["blocks_gate"])
        data_issue = next(item for item in blockers if item["type"] == "data_issue")
        self.assertEqual(data_issue["reason"], "ungraded_judgments")
        self.assertTrue(data_issue["blocks_gate"])
        manual_review = next(item for item in blockers if item["type"] == "manual_review_required")
        self.assertEqual(manual_review["action"], "manual_relevance_review")
        self.assertEqual(manual_review["metric"], "ndcg@20")
        self.assertTrue(manual_review["blocks_gate"])
        provider_failure = next(item for item in blockers if item["type"] == "provider_failure")
        self.assertEqual(provider_failure["source"], "openalex_semantic:q_semantic_1")
        self.assertEqual(provider_failure["status"], "authentication_required")
        self.assertFalse(provider_failure["blocks_gate"])

    def test_evaluate_live_evaluation_gate_preserves_recall_blockers(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/b"}, {"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/a"}, {"doi": "10.1000/c"}])),
                encoding="utf-8",
            )
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [
                                    {"id": "doi:10.1000/a", "grade": 3},
                                    {"id": "doi:10.1000/b", "grade": 2},
                                    {"id": "doi:10.1000/c", "grade": 0},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "queries": [
                            {
                                "id": "q1",
                                "query": "pyrite sulfur isotope",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        blockers = report["release_gate_blockers"]
        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["release_gate_blocker_count"], 3)
        self.assertEqual(
            [item["type"] for item in blockers],
            ["ndcg_improved_share_below_threshold", "recall_below_baseline", "recall_below_baseline"],
        )
        self.assertEqual([item["metric"] for item in blockers], ["ndcg@20_improved_share", "recall@20", "recall@50"])
        self.assertTrue(all(item["blocks_gate"] for item in blockers))

    def test_evaluate_live_evaluation_gate_preserves_ndcg_improved_share_blocker(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            for query_id in ("q1", "q2"):
                query_dir = output_dir / query_id
                query_dir.mkdir(parents=True)
                results = [
                    {"doi": "10.1000/a"},
                    {"doi": "10.1000/b"},
                ]
                (query_dir / "legacy.json").write_text(json.dumps(_legacy_search_payload(results)), encoding="utf-8")
                (query_dir / "hybrid.json").write_text(json.dumps(_hybrid_search_payload(results)), encoding="utf-8")
            review_packet_path = output_dir / "judgments_review_packet.json"
            review_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_review_packet.v1",
                        "queries": [
                            {
                                "query_id": "q1",
                                "judgments": [
                                    {"id": "doi:10.1000/a", "grade": 3},
                                    {"id": "doi:10.1000/b", "grade": 2},
                                ],
                            },
                            {
                                "query_id": "q2",
                                "judgments": [
                                    {"id": "doi:10.1000/a", "grade": 3},
                                    {"id": "doi:10.1000/b", "grade": 2},
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "review_packet": str(review_packet_path),
                        "queries": [
                            {
                                "id": "q1",
                                "query": "pyrite sulfur isotope",
                                "status": "success",
                                "legacy_result": "q1/legacy.json",
                                "hybrid_result": "q1/hybrid.json",
                            },
                            {
                                "id": "q2",
                                "query": "uranium fluid inclusion",
                                "status": "success",
                                "legacy_result": "q2/legacy.json",
                                "hybrid_result": "q2/hybrid.json",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_live_evaluation_gate(manifest_path)

        blockers = report["release_gate_blockers"]
        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["release_gate_blocker_count"], 1)
        self.assertEqual(blockers[0]["type"], "ndcg_improved_share_below_threshold")
        self.assertEqual(blockers[0]["metric"], "ndcg@20_improved_share")
        self.assertEqual(blockers[0]["observed"], 0.0)
        self.assertEqual(blockers[0]["minimum"], 0.5)
        self.assertNotIn("threshold", blockers[0])
        self.assertTrue(blockers[0]["blocks_gate"])

    def test_render_release_gate_markdown_summarizes_gate_status_and_diagnostics(self) -> None:
        render = getattr(search_live_eval, "render_release_gate_markdown", None)
        self.assertIsNotNone(render)
        report = {
            "passed": False,
            "summary": {
                "query_count": 2,
                "ndcg_improved_queries": 1,
                "ndcg_improved_share": 0.5,
                "provider_failure_rate": 0.25,
                "provider_failure_count": 1,
                "data_issue_count": 1,
                "judgment_count": 4,
                "graded_judgment_count": 3,
                "ungraded_judgment_count": 1,
                "manual_review_required_count": 1,
                "release_gate_blocker_count": 3,
            },
            "checks": {
                "recall_not_below_baseline": True,
                "ndcg@20_improved_share": True,
                "no_severe_ndcg@20_regressions": False,
                "no_data_issues": False,
            },
            "data_issues": [{"query_id": "q2", "reason": "missing_or_ungraded_judgments"}],
            "provider_failures": [
                {
                    "query_id": "q1",
                    "strategy": "hybrid",
                    "source": "openalex_semantic:q_semantic_1",
                    "status": "authentication_required",
                }
            ],
            "queries": [
                {"query_id": "q1", "query": "pyrite sulfur isotope", "passed": True, "failures": []},
                {
                    "query_id": "q2",
                    "query": "uranium fluid inclusion",
                    "passed": False,
                    "failures": ["severe_ndcg@20_regression"],
                    "diagnostics": [
                        {
                            "metric": "ndcg@20",
                            "severity": "manual_review_required",
                            "action": "manual_relevance_review",
                            "candidate": 0.55,
                            "baseline": 0.80,
                            "relative_delta": -0.3125,
                        }
                    ],
                },
            ],
            "release_gate_blockers": [
                {
                    "type": "data_issue",
                    "query_id": "q2",
                    "reason": "missing_or_ungraded_judgments",
                    "blocks_gate": True,
                },
                {
                    "type": "manual_review_required",
                    "query_id": "q2",
                    "action": "manual_relevance_review",
                    "metric": "ndcg@20",
                    "blocks_gate": True,
                },
                {
                    "type": "provider_failure",
                    "query_id": "q1",
                    "source": "openalex_semantic:q_semantic_1",
                    "status": "authentication_required",
                    "blocks_gate": False,
                },
            ],
        }

        markdown = render(report)

        self.assertIn("# InstSci Search Release Gate", markdown)
        self.assertIn("| passed | false |", markdown)
        self.assertIn("| query_count | 2 |", markdown)
        self.assertIn("| ndcg_improved_share | 0.5 |", markdown)
        self.assertIn("| provider_failure_rate | 0.25 |", markdown)
        self.assertIn("| manual_review_required_count | 1 |", markdown)
        self.assertIn("| release_gate_blocker_count | 3 |", markdown)
        self.assertIn("| judgment_count | 4 |", markdown)
        self.assertIn("| graded_judgment_count | 3 |", markdown)
        self.assertIn("| ungraded_judgment_count | 1 |", markdown)
        self.assertIn("## Release Gate Blockers", markdown)
        self.assertIn("| data_issue | q2 | failure | true | missing_or_ungraded_judgments |  |", markdown)
        self.assertIn("| manual_review_required | q2 | manual_review_required | true | manual_relevance_review | ndcg@20 |", markdown)
        self.assertIn("| provider_failure | q1 | diagnostic | false | authentication_required | openalex_semantic:q_semantic_1 |", markdown)
        self.assertIn("missing_or_ungraded_judgments", markdown)
        self.assertIn("authentication_required", markdown)
        self.assertIn("pyrite sulfur isotope", markdown)
        self.assertIn("uranium fluid inclusion", markdown)
        self.assertIn("severe_ndcg@20_regression", markdown)
        self.assertIn("manual_relevance_review", markdown)
        self.assertIn("| q2 | ndcg@20 | manual_review_required | manual_relevance_review | 0.55 | 0.8 | -31.25% |", markdown)

    def test_cli_search_gate_writes_manifest_level_release_gate(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/b"}, {"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/a"}, {"doi": "10.1000/b"}])),
                encoding="utf-8",
            )
            (query_dir / "judgments_pool.json").write_text(
                json.dumps({"doi:10.1000/a": 3, "doi:10.1000/b": 2}),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            gate_path = output_dir / "release_gate.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "queries": [
                            {
                                "id": "q1",
                                "status": "success",
                                "legacy_result": str(query_dir / "legacy.json"),
                                "hybrid_result": str(query_dir / "hybrid.json"),
                                "pool": str(query_dir / "judgments_pool.json"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-gate", str(manifest_path), "--output", str(gate_path)])
            report = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(report["schema"], "instsci.search_release_gate.v1")
        self.assertTrue(report["passed"])

    def test_cli_search_gate_writes_markdown_summary_when_requested(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval"
            query_dir = output_dir / "q1"
            query_dir.mkdir(parents=True)
            (query_dir / "legacy.json").write_text(
                json.dumps(_legacy_search_payload([{"doi": "10.1000/b"}, {"doi": "10.1000/a"}])),
                encoding="utf-8",
            )
            (query_dir / "hybrid.json").write_text(
                json.dumps(_hybrid_search_payload([{"doi": "10.1000/a"}, {"doi": "10.1000/b"}])),
                encoding="utf-8",
            )
            (query_dir / "judgments_pool.json").write_text(
                json.dumps({"doi:10.1000/a": 3, "doi:10.1000/b": 2}),
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            gate_path = output_dir / "release_gate.json"
            markdown_path = output_dir / "release_gate.md"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_live_evaluation.v1",
                        "queries": [
                            {
                                "id": "q1",
                                "status": "success",
                                "legacy_result": str(query_dir / "legacy.json"),
                                "hybrid_result": str(query_dir / "hybrid.json"),
                                "pool": str(query_dir / "judgments_pool.json"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                app,
                [
                    "search-gate",
                    str(manifest_path),
                    "--output",
                    str(gate_path),
                    "--markdown-output",
                    str(markdown_path),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(markdown_path.exists())
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertIn("# InstSci Search Release Gate", markdown)
        self.assertIn("| passed | true |", markdown)
        self.assertIn("| query_count | 1 |", markdown)

    def test_validate_release_gate_report_reports_contract_errors(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": True,
                "summary": {
                    "query_count": 2,
                    "ndcg_improved_queries": 3,
                    "ndcg_improved_share": 1.2,
                    "recall_failure_count": 0,
                    "severe_ndcg_regression_count": 0,
                    "manual_review_required_count": 0,
                    "data_issue_count": 2,
                    "provider_failure_count": 2,
                    "provider_failure_rate": 1.4,
                    "judgment_count": 3,
                    "graded_judgment_count": 2,
                    "ungraded_judgment_count": 2,
                    "release_gate_blocker_count": 2,
                },
                "checks": {"no_data_issues": "false", "all_judgments_graded": False},
                "queries": [
                    {
                        "query_id": "",
                        "passed": "yes",
                        "failures": "recall@20_below_baseline",
                        "diagnostics": ["bad"],
                    }
                ],
                "data_issues": [{"query_id": "", "reason": ""}],
                "provider_failures": [{"query_id": "q1", "strategy": "", "source": "", "status": ""}],
                "release_gate_blockers": [
                    {"type": "", "blocks_gate": "yes"},
                    {"type": "provider_failure", "blocks_gate": True},
                ],
            }
        )

        self.assertEqual(report["schema"], "instsci.search_release_gate_validation.v1")
        self.assertFalse(report["valid"])
        self.assertIn("passed must be false when blocking release_gate_blockers are present", report["errors"])
        self.assertIn("summary.query_count does not match queries length", report["errors"])
        self.assertIn("summary.ndcg_improved_queries cannot exceed query_count", report["errors"])
        self.assertIn("summary.ndcg_improved_share must be between 0 and 1", report["errors"])
        self.assertIn("summary.data_issue_count does not match data_issues length", report["errors"])
        self.assertIn("summary.provider_failure_count does not match provider_failures length", report["errors"])
        self.assertIn("summary.provider_failure_rate must be between 0 and 1", report["errors"])
        self.assertIn("summary.graded_judgment_count + ungraded_judgment_count must equal judgment_count", report["errors"])
        self.assertIn("checks.no_data_issues must be a boolean", report["errors"])
        self.assertIn("queries[0].query_id is required", report["errors"])
        self.assertIn("queries[0].passed must be a boolean", report["errors"])
        self.assertIn("queries[0].failures must be a list", report["errors"])
        self.assertIn("queries[0].diagnostics[0] must be an object", report["errors"])
        self.assertIn("data_issues[0].query_id is required", report["errors"])
        self.assertIn("data_issues[0].reason is required", report["errors"])
        self.assertIn("provider_failures[0].strategy is required", report["errors"])
        self.assertIn("provider_failures[0].source is required", report["errors"])
        self.assertIn("provider_failures[0].status is required", report["errors"])
        self.assertIn("release_gate_blockers[0].type is required", report["errors"])
        self.assertIn("release_gate_blockers[0].blocks_gate must be a boolean", report["errors"])
        self.assertIn("release_gate_blockers[1].blocks_gate must be false for provider_failure", report["errors"])

    def test_validate_release_gate_report_rejects_non_provider_blocker_with_blocks_gate_false(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": True,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "release_gate_blocker_count": 1,
                },
                "checks": {},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "data_issue",
                        "query_id": "q1",
                        "reason": "missing_or_ungraded_judgments",
                        "blocks_gate": False,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("release_gate_blockers[0].blocks_gate must be true for data_issue", report["errors"])

    def test_validate_release_gate_report_requires_blocker_fields_by_type(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "release_gate_blocker_count": 5,
                },
                "checks": {},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {"type": "data_issue", "blocks_gate": True},
                    {"type": "provider_failure", "blocks_gate": False},
                    {"type": "recall_below_baseline", "blocks_gate": True},
                    {"type": "manual_review_required", "blocks_gate": True},
                    {"type": "ndcg_improved_share_below_threshold", "blocks_gate": True},
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("release_gate_blockers[0].query_id is required for data_issue", report["errors"])
        self.assertIn("release_gate_blockers[0].reason is required for data_issue", report["errors"])
        self.assertIn("release_gate_blockers[1].strategy is required for provider_failure", report["errors"])
        self.assertIn("release_gate_blockers[1].source is required for provider_failure", report["errors"])
        self.assertIn("release_gate_blockers[1].status is required for provider_failure", report["errors"])
        self.assertIn("release_gate_blockers[2].metric is required for recall_below_baseline", report["errors"])
        self.assertIn("release_gate_blockers[2].action is required for recall_below_baseline", report["errors"])
        self.assertIn("release_gate_blockers[3].metric is required for manual_review_required", report["errors"])
        self.assertIn("release_gate_blockers[3].action is required for manual_review_required", report["errors"])
        self.assertIn(
            "release_gate_blockers[4].metric is required for ndcg_improved_share_below_threshold",
            report["errors"],
        )
        self.assertIn(
            "release_gate_blockers[4].minimum is required for ndcg_improved_share_below_threshold",
            report["errors"],
        )
        self.assertIn(
            "release_gate_blockers[4].observed is required for ndcg_improved_share_below_threshold",
            report["errors"],
        )
        self.assertIn(
            "release_gate_blockers[4].improved_queries is required for ndcg_improved_share_below_threshold",
            report["errors"],
        )
        self.assertIn(
            "release_gate_blockers[4].query_count is required for ndcg_improved_share_below_threshold",
            report["errors"],
        )

    def test_validate_release_gate_report_rejects_checks_that_conflict_with_evidence(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "data_issue_count": 1,
                    "judgment_count": 3,
                    "graded_judgment_count": 2,
                    "ungraded_judgment_count": 1,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"no_data_issues": True, "all_judgments_graded": True},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [{"query_id": "q1", "reason": "ungraded_judgments"}],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "data_issue",
                        "query_id": "q1",
                        "reason": "ungraded_judgments",
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("checks.no_data_issues does not match data_issues", report["errors"])
        self.assertIn("checks.all_judgments_graded does not match ungraded_judgment_count", report["errors"])

    def test_validate_release_gate_report_rejects_provider_failure_rate_mismatch(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": True,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "provider_status_count": 4,
                    "provider_failure_count": 1,
                    "provider_failure_rate": 0.75,
                    "release_gate_blocker_count": 1,
                },
                "checks": {},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [
                    {
                        "query_id": "q1",
                        "strategy": "hybrid",
                        "source": "openalex_semantic:q_semantic_1",
                        "status": "authentication_required",
                    }
                ],
                "release_gate_blockers": [
                    {
                        "type": "provider_failure",
                        "query_id": "q1",
                        "source": "openalex_semantic:q_semantic_1",
                        "status": "authentication_required",
                        "blocks_gate": False,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("summary.provider_failure_rate does not match provider_failure_count/provider_status_count", report["errors"])

    def test_validate_release_gate_report_requires_provider_failure_blockers(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": True,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "provider_status_count": 2,
                    "provider_failure_count": 1,
                    "provider_failure_rate": 0.5,
                    "release_gate_blocker_count": 0,
                },
                "checks": {},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [
                    {
                        "query_id": "q1",
                        "strategy": "hybrid",
                        "source": "openalex_semantic:q_semantic_1",
                        "status": "authentication_required",
                    }
                ],
                "release_gate_blockers": [],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("provider_failures must have matching provider_failure release_gate_blockers", report["errors"])

    def test_validate_release_gate_report_rejects_provider_blockers_without_failures(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": True,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "provider_status_count": 2,
                    "provider_failure_count": 0,
                    "provider_failure_rate": 0.0,
                    "release_gate_blocker_count": 1,
                },
                "checks": {},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "provider_failure",
                        "query_id": "q1",
                        "strategy": "hybrid",
                        "source": "openalex_semantic:q_semantic_1",
                        "status": "authentication_required",
                        "blocks_gate": False,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("provider_failure release_gate_blockers must have matching provider_failures", report["errors"])

    def test_validate_release_gate_report_requires_data_issue_blockers(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "data_issue_count": 1,
                    "release_gate_blocker_count": 0,
                },
                "checks": {"no_data_issues": False},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [{"query_id": "q1", "reason": "missing_or_ungraded_judgments"}],
                "provider_failures": [],
                "release_gate_blockers": [],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("data_issues must have matching blocking release_gate_blockers", report["errors"])

    def test_validate_release_gate_report_rejects_data_issue_blockers_without_issues(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "data_issue_count": 0,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"no_data_issues": True},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "data_issue",
                        "query_id": "q1",
                        "reason": "missing_or_ungraded_judgments",
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("data_issue release_gate_blockers must have matching data_issues", report["errors"])

    def test_validate_release_gate_report_requires_query_diagnostic_blockers(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "manual_review_required_count": 1,
                    "release_gate_blocker_count": 0,
                },
                "checks": {},
                "queries": [
                    {
                        "query_id": "q1",
                        "query": "topic",
                        "passed": False,
                        "failures": ["severe_ndcg@20_regression"],
                        "diagnostics": [
                            {
                                "metric": "ndcg@20",
                                "action": "manual_relevance_review",
                                "severity": "manual_review_required",
                            }
                        ],
                    }
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("query diagnostics must have matching blocking release_gate_blockers", report["errors"])

    def test_validate_release_gate_report_rejects_query_blockers_without_diagnostics(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "recall_failure_count": 0,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"recall_not_below_baseline": True},
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "recall_below_baseline",
                        "query_id": "q1",
                        "metric": "recall@20",
                        "action": "inspect_hybrid_recall_loss",
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("query release_gate_blockers must have matching diagnostics", report["errors"])

    def test_validate_release_gate_report_requires_aggregate_ndcg_share_blocker(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "config": {"min_ndcg_improved_share": 0.5},
                "summary": {
                    "query_count": 2,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "release_gate_blocker_count": 0,
                },
                "checks": {"ndcg@20_improved_share": False},
                "queries": [
                    {"query_id": "q1", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q2", "passed": True, "failures": [], "diagnostics": []},
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "ndcg_improved_share below threshold requires ndcg_improved_share_below_threshold blocker",
            report["errors"],
        )

    def test_validate_release_gate_report_rejects_ndcg_share_check_mismatch(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "config": {"min_ndcg_improved_share": 0.5},
                "summary": {
                    "query_count": 2,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"ndcg@20_improved_share": True},
                "queries": [
                    {"query_id": "q1", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q2", "passed": True, "failures": [], "diagnostics": []},
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "ndcg_improved_share_below_threshold",
                        "metric": "ndcg@20_improved_share",
                        "observed": 0.0,
                        "minimum": 0.5,
                        "improved_queries": 0,
                        "query_count": 2,
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "checks.ndcg@20_improved_share does not match ndcg_improved_share threshold",
            report["errors"],
        )

    def test_validate_release_gate_report_rejects_ndcg_share_blocker_value_mismatch(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "config": {"min_ndcg_improved_share": 0.5},
                "summary": {
                    "query_count": 2,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"ndcg@20_improved_share": False},
                "queries": [
                    {"query_id": "q1", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q2", "passed": True, "failures": [], "diagnostics": []},
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "ndcg_improved_share_below_threshold",
                        "metric": "ndcg@20_improved_share",
                        "observed": 0.25,
                        "minimum": 0.6,
                        "improved_queries": 0,
                        "query_count": 2,
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "ndcg_improved_share_below_threshold blocker values do not match summary/config",
            report["errors"],
        )

    def test_validate_release_gate_report_rejects_ndcg_share_blocker_count_mismatch(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "config": {"min_ndcg_improved_share": 0.5},
                "summary": {
                    "query_count": 2,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"ndcg@20_improved_share": False},
                "queries": [
                    {"query_id": "q1", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q2", "passed": True, "failures": [], "diagnostics": []},
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "ndcg_improved_share_below_threshold",
                        "metric": "ndcg@20_improved_share",
                        "observed": 0.0,
                        "minimum": 0.5,
                        "improved_queries": 1,
                        "query_count": 3,
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "ndcg_improved_share_below_threshold blocker counts do not match summary",
            report["errors"],
        )

    def test_validate_release_gate_report_rejects_ndcg_share_blocker_identity_mismatch(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "config": {"min_ndcg_improved_share": 0.5},
                "summary": {
                    "query_count": 2,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"ndcg@20_improved_share": False},
                "queries": [
                    {"query_id": "q1", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q2", "passed": True, "failures": [], "diagnostics": []},
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "ndcg_improved_share_below_threshold",
                        "query_id": "q1",
                        "metric": "precision@10_improved_share",
                        "action": "manual_relevance_review",
                        "observed": 0.0,
                        "minimum": 0.5,
                        "improved_queries": 0,
                        "query_count": 2,
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "ndcg_improved_share_below_threshold blocker identity does not match aggregate nDCG gate",
            report["errors"],
        )

    def test_validate_release_gate_report_rejects_ndcg_share_blocker_when_threshold_is_met(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "config": {"min_ndcg_improved_share": 0.5},
                "summary": {
                    "query_count": 2,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 0.5,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"ndcg@20_improved_share": True},
                "queries": [
                    {"query_id": "q1", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q2", "passed": True, "failures": [], "diagnostics": []},
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "ndcg_improved_share_below_threshold",
                        "query_id": "aggregate",
                        "metric": "ndcg@20_improved_share",
                        "action": "inspect_hybrid_ranking_quality",
                        "observed": 0.5,
                        "minimum": 0.5,
                        "improved_queries": 1,
                        "query_count": 2,
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "ndcg_improved_share_below_threshold blocker is present even though threshold is met",
            report["errors"],
        )

    def test_validate_release_gate_report_rejects_ndcg_share_arithmetic_mismatch(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": True,
                "summary": {
                    "query_count": 4,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 0.75,
                    "release_gate_blocker_count": 0,
                },
                "checks": {"ndcg@20_improved_share": True},
                "queries": [
                    {"query_id": "q1", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q2", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q3", "passed": True, "failures": [], "diagnostics": []},
                    {"query_id": "q4", "passed": True, "failures": [], "diagnostics": []},
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("summary.ndcg_improved_share does not match ndcg_improved_queries/query_count", report["errors"])

    def test_validate_release_gate_report_rejects_passed_with_failed_checks_or_queries(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": True,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 1,
                    "ndcg_improved_share": 1.0,
                    "release_gate_blocker_count": 0,
                },
                "checks": {"recall@20_not_below_baseline": False},
                "queries": [{"query_id": "q1", "passed": False, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("passed must be false when any check is false", report["errors"])
        self.assertIn("passed must be false when any query failed", report["errors"])

    def test_validate_release_gate_report_rejects_query_passed_with_failures(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "recall_failure_count": 1,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"recall@20_not_below_baseline": False},
                "queries": [
                    {
                        "query_id": "q1",
                        "passed": True,
                        "failures": ["recall@20_below_baseline"],
                        "diagnostics": [
                            {
                                "metric": "recall@20",
                                "action": "inspect_hybrid_recall_loss",
                            }
                        ],
                    }
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "recall_below_baseline",
                        "query_id": "q1",
                        "metric": "recall@20",
                        "action": "inspect_hybrid_recall_loss",
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("queries[0].passed must be false when failures are present", report["errors"])

    def test_validate_release_gate_report_rejects_recall_check_mismatch(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "recall_failure_count": 1,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"recall_not_below_baseline": True},
                "queries": [
                    {
                        "query_id": "q1",
                        "passed": False,
                        "failures": ["recall@20_below_baseline"],
                        "diagnostics": [
                            {
                                "metric": "recall@20",
                                "action": "inspect_hybrid_recall_loss",
                            }
                        ],
                    }
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "recall_below_baseline",
                        "query_id": "q1",
                        "metric": "recall@20",
                        "action": "inspect_hybrid_recall_loss",
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("checks.recall_not_below_baseline does not match recall_failure_count", report["errors"])

    def test_validate_release_gate_report_rejects_severe_ndcg_check_mismatch(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "severe_ndcg_regression_count": 1,
                    "manual_review_required_count": 1,
                    "release_gate_blocker_count": 1,
                },
                "checks": {"no_severe_ndcg@20_regressions": True},
                "queries": [
                    {
                        "query_id": "q1",
                        "passed": False,
                        "failures": ["severe_ndcg@20_regression"],
                        "diagnostics": [
                            {
                                "metric": "ndcg@20",
                                "action": "manual_relevance_review",
                            }
                        ],
                    }
                ],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [
                    {
                        "type": "manual_review_required",
                        "query_id": "q1",
                        "metric": "ndcg@20",
                        "action": "manual_relevance_review",
                        "blocks_gate": True,
                    }
                ],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "checks.no_severe_ndcg@20_regressions does not match severe_ndcg_regression_count",
            report["errors"],
        )

    def test_validate_release_gate_report_rejects_summary_counts_without_query_evidence(self) -> None:
        report = validate_release_gate_report(
            {
                "schema": "instsci.search_release_gate.v1",
                "passed": False,
                "summary": {
                    "query_count": 1,
                    "ndcg_improved_queries": 0,
                    "ndcg_improved_share": 0.0,
                    "recall_failure_count": 1,
                    "severe_ndcg_regression_count": 1,
                    "manual_review_required_count": 1,
                    "release_gate_blocker_count": 0,
                },
                "checks": {
                    "recall_not_below_baseline": False,
                    "no_severe_ndcg@20_regressions": False,
                },
                "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                "data_issues": [],
                "provider_failures": [],
                "release_gate_blockers": [],
            }
        )

        self.assertFalse(report["valid"])
        self.assertIn("summary.recall_failure_count does not match query failures", report["errors"])
        self.assertIn("summary.severe_ndcg_regression_count does not match query failures", report["errors"])
        self.assertIn("summary.manual_review_required_count does not match diagnostics", report["errors"])

    def test_cli_search_gate_validate_writes_report_and_fails_invalid_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            gate_path = Path(tmp) / "release_gate.json"
            output_path = Path(tmp) / "release_gate_validation.json"
            gate_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_release_gate.v1",
                        "passed": True,
                        "summary": {
                            "query_count": 1,
                            "ndcg_improved_queries": 1,
                            "ndcg_improved_share": 1.0,
                            "release_gate_blocker_count": 1,
                        },
                        "checks": {},
                        "queries": [{"query_id": "q1", "passed": True, "failures": [], "diagnostics": []}],
                        "release_gate_blockers": [{"type": "data_issue", "query_id": "q1", "blocks_gate": True}],
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-gate-validate", str(gate_path), "--output", str(output_path)])
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["schema"], "instsci.search_release_gate_validation.v1")
        self.assertFalse(payload["valid"])
        self.assertIn("passed must be false when blocking release_gate_blockers are present", payload["errors"])

    def test_validate_live_evaluation_manifest_reports_contract_errors(self) -> None:
        report = validate_live_evaluation_manifest(
            {
                "schema": "instsci.search_live_evaluation.v1",
                "query_count": 2,
                "limit": 0,
                "strategies": ["legacy"],
                "review_packet_count": 3,
                "query_set_validation": "",
                "query_set_validation_valid": "yes",
                "query_set_validation_error_count": -1,
                "query_set_validation_warning_count": "1",
                "pooling": {"legacy_top": -1, "hybrid_top": 30, "channel_top": "10"},
                "queries": [
                    {
                        "id": "",
                        "query": "",
                        "status": "success",
                        "legacy_count": -1,
                        "hybrid_count": "2",
                        "pool_count": 2,
                        "legacy_result": "",
                        "hybrid_result": "",
                        "pool": "",
                        "legacy_schema": "instsci.search_results.v2",
                        "hybrid_schema": "instsci.search_results.v1",
                        "hybrid_contract_valid": "yes",
                        "legacy_source_status": {"semantic_scholar": {"status": "", "count": -1}},
                        "hybrid_source_status": {"openalex_keyword:q1": "bad"},
                        "acquisition_started": True,
                    }
                ],
            }
        )

        self.assertEqual(report["schema"], "instsci.search_live_evaluation_validation.v1")
        self.assertFalse(report["valid"])
        self.assertIn("query_count does not match queries length", report["errors"])
        self.assertIn("limit must be a positive integer", report["errors"])
        self.assertIn("strategies must include legacy and hybrid", report["errors"])
        self.assertIn("review_packet is required", report["errors"])
        self.assertIn("query_set_validation is required when present", report["errors"])
        self.assertIn("query_set_validation_valid must be a boolean", report["errors"])
        self.assertIn("query_set_validation_error_count must be a non-negative integer", report["errors"])
        self.assertIn("query_set_validation_warning_count must be a non-negative integer", report["errors"])
        self.assertIn("review_packet_count cannot exceed pooled query rows", report["errors"])
        self.assertIn("pooling.legacy_top must be a non-negative integer", report["errors"])
        self.assertIn("pooling.channel_top must be a non-negative integer", report["errors"])
        self.assertIn("queries[0].id is required", report["errors"])
        self.assertIn("queries[0].query is required for success rows", report["errors"])
        self.assertIn("queries[0].legacy_count must be a non-negative integer", report["errors"])
        self.assertIn("queries[0].hybrid_count must be a non-negative integer", report["errors"])
        self.assertIn("queries[0].legacy_result is required for success rows", report["errors"])
        self.assertIn("queries[0].hybrid_result is required for success rows", report["errors"])
        self.assertIn("queries[0].pool is required for success rows", report["errors"])
        self.assertIn("queries[0].legacy_schema must be instsci.search_results.v1", report["errors"])
        self.assertIn("queries[0].hybrid_schema must be instsci.search_results.v2", report["errors"])
        self.assertIn("queries[0].hybrid_contract_valid must be a boolean", report["errors"])
        self.assertIn("queries[0].legacy_source_status.semantic_scholar.status is required", report["errors"])
        self.assertIn("queries[0].legacy_source_status.semantic_scholar.count must be a non-negative integer", report["errors"])
        self.assertIn("queries[0].hybrid_source_status.openalex_keyword:q1 must be an object", report["errors"])
        self.assertIn("queries[0].acquisition_started must be false when present", report["errors"])

    def test_validate_relevance_review_packet_reports_contract_errors(self) -> None:
        report = validate_relevance_review_packet(
            {
                "schema": "instsci.relevance_review_packet.v1",
                "query_count": 2,
                "judgment_count": 2,
                "review": {"anonymous": False, "grade_scale": []},
                "pooling": {"legacy_top": 30, "hybrid_top": -1, "channel_top": "10"},
                "skipped_queries": [{"query_id": "", "reason": ""}],
                "queries": [
                    {
                        "query_id": "",
                        "query": "",
                        "count": 2,
                        "judgments": [
                            {"review_id": "", "id": "", "title": "", "grade": 4, "pool_sources": ["legacy"]},
                            {"review_id": "r1", "id": "doi:10.1000/a", "title": "A", "grade": 2},
                            {"review_id": "r1", "id": "doi:10.1000/a", "title": "A", "retrieval_provenance": []},
                        ],
                    }
                ],
            }
        )

        self.assertEqual(report["schema"], "instsci.relevance_review_packet_validation.v1")
        self.assertFalse(report["valid"])
        self.assertIn("query_count does not match queries length", report["errors"])
        self.assertIn("judgment_count does not match total query judgments", report["errors"])
        self.assertIn("review.anonymous must be true", report["errors"])
        self.assertIn("review.grade_scale must be an object", report["errors"])
        self.assertIn("pooling.hybrid_top must be a non-negative integer", report["errors"])
        self.assertIn("pooling.channel_top must be a non-negative integer", report["errors"])
        self.assertIn("skipped_queries[0].query_id is required", report["errors"])
        self.assertIn("skipped_queries[0].reason is required", report["errors"])
        self.assertIn("queries[0].query_id is required", report["errors"])
        self.assertIn("queries[0].query is required", report["errors"])
        self.assertIn("queries[0].count does not match judgments length", report["errors"])
        self.assertIn("queries[0].judgments[0].review_id is required", report["errors"])
        self.assertIn("queries[0].judgments[0].id is required", report["errors"])
        self.assertIn("queries[0].judgments[0].title is required", report["errors"])
        self.assertIn("queries[0].judgments[0].grade must be null or an integer from 0-3", report["errors"])
        self.assertIn("queries[0].judgments[0].pool_sources must not be present in blinded review packets", report["errors"])
        self.assertIn("queries[0].judgments[2].retrieval_provenance must not be present in blinded review packets", report["errors"])
        self.assertIn("queries[0].judgments[2].review_id duplicates r1", report["errors"])
        self.assertIn("queries[0].judgments[2].id duplicates doi:10.1000/a", report["errors"])

    def test_cli_search_live_eval_validate_writes_report_and_fails_invalid_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            output_path = Path(tmp) / "live_eval_validation.json"
            manifest_path.write_text(
                json.dumps({"schema": "instsci.search_live_evaluation.v1", "query_count": 1, "queries": []}),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-live-eval-validate", str(manifest_path), "--output", str(output_path)])
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["schema"], "instsci.search_live_evaluation_validation.v1")
        self.assertFalse(payload["valid"])
        self.assertIn("query_count does not match queries length", payload["errors"])

    def test_cli_search_review_packet_validate_writes_report_and_fails_invalid_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            packet_path = Path(tmp) / "judgments_review_packet.json"
            output_path = Path(tmp) / "review_packet_validation.json"
            packet_path.write_text(
                json.dumps({"schema": "instsci.relevance_review_packet.v1", "query_count": 1, "judgment_count": 0, "queries": []}),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-review-packet-validate", str(packet_path), "--output", str(output_path)])
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["schema"], "instsci.relevance_review_packet_validation.v1")
        self.assertFalse(payload["valid"])
        self.assertIn("query_count does not match queries length", payload["errors"])
