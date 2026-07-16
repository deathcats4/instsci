import json
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from typer.testing import CliRunner

from instsci import multi_search
from instsci.search_benchmark import (
    build_ranking_snapshot_payload,
    build_relevance_pool,
    build_release_gate_report,
    compare_ranking_snapshot_payload,
    compare_ranked_results,
    evaluate_ranked_results,
    load_judgments,
    load_must_find,
    render_benchmark_markdown,
    validate_benchmark_metrics_report,
    validate_relevance_pool,
    validate_ranking_snapshot_check,
    validate_ranking_snapshot_payload,
)
from instsci.cli import app
from instsci.search_pipeline import (
    build_search_payload,
    derive_work_identity,
    downgrade_record_to_v1,
    downgrade_search_payload_to_v1,
    load_search_payload,
    parse_selection_indices,
    result_to_record,
    select_doi_records,
    validate_search_payload_contract,
    write_search_payload,
)
from instsci.sources.semantic_scholar import SearchResult


class SearchPipelineTests(TestCase):
    def test_search_payload_round_trips_json_and_csv(self) -> None:
        payload = build_search_payload(
            "pyrite uranium",
            [SearchResult(title="Paper", authors=["A", "B"], year=2024, doi="https://doi.org/10.1000/Test")],
            year_range="2020-",
            source_status={"semantic_scholar": {"status": "success", "count": 1}},
        )
        with TemporaryDirectory() as tmp:
            json_path = write_search_payload(payload, Path(tmp) / "results.json")
            csv_path = write_search_payload(payload, Path(tmp) / "results.csv")
            json_loaded = load_search_payload(json_path)
            csv_loaded = load_search_payload(csv_path)

        self.assertEqual(json_loaded["results"][0]["doi"], "10.1000/test")
        self.assertEqual(json_loaded["source_status"]["semantic_scholar"]["count"], 1)
        self.assertEqual(csv_loaded["results"][0]["authors"], ["A", "B"])

    def test_search_payload_v2_preserves_provenance_and_version_placeholders(self) -> None:
        result = SimpleNamespace(
            title="Hybrid Paper",
            authors=[],
            year=2024,
            doi="10.1000/hybrid",
            arxiv_id="",
            journal="",
            sources=["openalex"],
            citation_count=0,
            citation_counts={},
            s2_url="",
            paper_id="",
            retrieval_provenance=[{"provider": "openalex", "channel": "openalex_semantic", "rank": 1}],
            fusion_score=0.018,
            rank_components={"rrf": 0.018},
            related_versions=[],
            discovery_reasons=[],
        )
        payload = build_search_payload(
            "topic",
            [result],
            source_status={"openalex_semantic:q_semantic_1": {"status": "success", "count": 1}},
            query_plan={"schema": "instsci.query_plan.v1", "strategy": "hybrid"},
        )
        with TemporaryDirectory() as tmp:
            json_path = write_search_payload(payload, Path(tmp) / "results.json")
            loaded = load_search_payload(json_path)

        record = loaded["results"][0]
        self.assertEqual(loaded["schema"], "instsci.search_results.v2")
        self.assertEqual(record["canonical_work_id"], "doi:10.1000/hybrid")
        self.assertEqual(record["version_type"], "unknown")
        self.assertEqual(record["retrieval_provenance"][0]["channel"], "openalex_semantic")
        self.assertEqual(record["rank_components"]["rrf"], 0.018)

    def test_search_payload_keeps_legacy_query_plan_as_v1_compat_output(self) -> None:
        result = SearchResult(title="Legacy Paper", authors=["A"], year=2024, doi="10.1000/legacy")
        payload = build_search_payload(
            "topic",
            [result],
            source_status={"semantic_scholar": {"status": "success", "count": 1}},
            query_plan={"schema": "instsci.query_plan.v1", "strategy": "legacy"},
        )

        self.assertEqual(payload["schema"], "instsci.search_results.v1")
        self.assertNotIn("query_plan", payload)

    def test_search_payload_v2_can_downgrade_to_v1_safe_record(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "source_status": {"openalex_semantic:q_semantic_1": {"status": "success"}},
            "query_plan": {"strategy": "hybrid"},
            "results": [
                {
                    "index": 1,
                    "source": "openalex",
                    "sources": ["openalex"],
                    "paper_id": "W1",
                    "title": "Hybrid Paper",
                    "authors": ["A Author"],
                    "year": 2024,
                    "abstract": "Abstract",
                    "doi": "https://doi.org/10.1000/HYBRID",
                    "arxiv_id": "arXiv:2401.00001v2",
                    "journal": "Journal",
                    "citation_count": 4,
                    "citation_counts": {"openalex": 4},
                    "url": "https://example.test/paper",
                    "canonical_work_id": "doi:10.1000/hybrid",
                    "version_family_id": "title:hybrid paper",
                    "version_type": "journal",
                    "retrieval_provenance": [{"channel": "openalex_semantic"}],
                    "fusion_score": 0.02,
                    "rank_components": {"rrf": 0.02},
                    "unexpected_future_field": "keep old consumers calm",
                }
            ],
        }

        downgraded_record = downgrade_record_to_v1(payload["results"][0])
        downgraded_payload = downgrade_search_payload_to_v1(payload)

        self.assertEqual(downgraded_record["doi"], "10.1000/hybrid")
        self.assertEqual(downgraded_record["arxiv_id"], "2401.00001")
        self.assertNotIn("canonical_work_id", downgraded_record)
        self.assertNotIn("retrieval_provenance", downgraded_record)
        self.assertNotIn("unexpected_future_field", downgraded_record)
        self.assertEqual(downgraded_payload["schema"], "instsci.search_results.v1")
        self.assertEqual(downgraded_payload["results"], [downgraded_record])
        self.assertNotIn("query_plan", downgraded_payload)

    def test_cli_search_downgrade_writes_v1_safe_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "search_v2.json"
            output_path = Path(tmp) / "search_v1.json"
            source_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_results.v2",
                        "query": "topic",
                        "query_plan": {"strategy": "hybrid"},
                        "results": [
                            {
                                "index": 1,
                                "source": "openalex",
                                "sources": ["openalex"],
                                "title": "Hybrid Paper",
                                "doi": "https://doi.org/10.1000/HYBRID",
                                "canonical_work_id": "doi:10.1000/hybrid",
                                "retrieval_provenance": [{"channel": "openalex_semantic"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-downgrade", str(source_path), "--output", str(output_path)])
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload["schema"], "instsci.search_results.v1")
        self.assertEqual(payload["results"][0]["doi"], "10.1000/hybrid")
        self.assertNotIn("query_plan", payload)
        self.assertNotIn("retrieval_provenance", payload["results"][0])

    def test_search_contract_validation_accepts_v2_with_future_fields(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "count": 1,
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "strategy": "hybrid",
                "channels": [
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    }
                ],
            },
            "source_status": {
                "openalex_semantic:q_semantic_1": {
                    "provider": "openalex",
                    "channel": "openalex_semantic",
                    "query_variant": "q_semantic_1",
                    "status": "success",
                    "count": 1,
                    "retryable": False,
                }
            },
            "results": [
                {
                    "index": 1,
                    "source": "openalex",
                    "sources": ["openalex"],
                    "title": "Hybrid Paper",
                    "doi": "10.1000/hybrid",
                    "canonical_work_id": "doi:10.1000/hybrid",
                    "version_family_id": "title:hybrid paper",
                    "version_type": "journal",
                    "related_versions": [],
                    "retrieval_provenance": [
                        {
                            "provider": "openalex",
                            "channel": "openalex_semantic",
                            "query_variant": "q_semantic_1",
                            "rank": 1,
                            "weight": 1.1,
                        }
                    ],
                    "unexpected_future_field": {"kept": True},
                }
            ],
        }

        report = validate_search_payload_contract(payload)

        self.assertTrue(report["valid"], report)
        self.assertEqual(report["schema"], "instsci.search_contract_validation.v1")
        self.assertEqual(report["summary"]["record_count"], 1)
        self.assertEqual(report["summary"]["channel_status_count"], 1)
        self.assertEqual(report["errors"], [])

    def test_search_contract_validation_checks_retrieval_provenance_rows(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "count": 1,
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "strategy": "hybrid",
                "channels": [
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    }
                ],
            },
            "source_status": {
                "openalex_semantic:q_semantic_1": {
                    "provider": "openalex",
                    "channel": "openalex_semantic",
                    "query_variant": "q_semantic_1",
                    "status": "success",
                    "count": 1,
                }
            },
            "results": [
                {
                    "index": 1,
                    "source": "openalex",
                    "sources": ["openalex"],
                    "title": "Malformed Provenance",
                    "doi": "10.1000/provenance",
                    "canonical_work_id": "doi:10.1000/provenance",
                    "version_family_id": "title:malformed provenance",
                    "version_type": "journal",
                    "related_versions": [],
                    "retrieval_provenance": [
                        {
                            "channel": "openalex_semantic",
                            "query_variant": "q_semantic_1",
                            "rank": "1",
                            "weight": "1.1",
                        }
                    ],
                }
            ],
        }

        report = validate_search_payload_contract(payload)

        self.assertFalse(report["valid"])
        self.assertIn("results[1].retrieval_provenance[1].provider missing", report["errors"])
        self.assertIn("results[1].retrieval_provenance[1].rank must be a positive integer", report["errors"])
        self.assertIn("results[1].retrieval_provenance[1].weight must be numeric", report["errors"])

    def test_search_contract_validation_requires_provenance_channel_in_query_plan(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "count": 1,
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "strategy": "hybrid",
                "channels": [
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    }
                ],
            },
            "source_status": {
                "openalex_semantic:q_semantic_1": {
                    "provider": "openalex",
                    "channel": "openalex_semantic",
                    "query_variant": "q_semantic_1",
                    "status": "success",
                    "count": 1,
                }
            },
            "results": [
                {
                    "index": 1,
                    "source": "openalex",
                    "sources": ["openalex"],
                    "title": "Mismatched Provenance",
                    "doi": "10.1000/mismatch",
                    "canonical_work_id": "doi:10.1000/mismatch",
                    "version_family_id": "title:mismatched provenance",
                    "version_type": "journal",
                    "related_versions": [],
                    "retrieval_provenance": [
                        {
                            "provider": "openalex",
                            "channel": "openalex_keyword",
                            "query_variant": "q_semantic_1",
                            "rank": 1,
                            "weight": 1.0,
                        }
                    ],
                }
            ],
        }

        report = validate_search_payload_contract(payload)

        self.assertFalse(report["valid"])
        self.assertIn(
            "results[1].retrieval_provenance[1] has no query_plan channel: openalex_keyword:q_semantic_1",
            report["errors"],
        )

    def test_search_contract_validation_requires_source_status_key_to_match_row_and_query_plan(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "count": 0,
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "strategy": "hybrid",
                "channels": [
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    }
                ],
            },
            "source_status": {
                "openalex_semantic:q_semantic_1": {
                    "provider": "crossref",
                    "channel": "openalex_keyword",
                    "query_variant": "q_semantic_1",
                    "status": "success",
                    "count": 0,
                }
            },
            "results": [],
        }

        report = validate_search_payload_contract(payload)

        self.assertFalse(report["valid"])
        self.assertIn(
            "source_status.openalex_semantic:q_semantic_1 key does not match row channel/query_variant: openalex_keyword:q_semantic_1",
            report["errors"],
        )
        self.assertIn(
            "source_status.openalex_semantic:q_semantic_1.provider does not match query_plan channel provider",
            report["errors"],
        )

    def test_search_contract_validation_requires_status_for_each_query_plan_channel(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "count": 0,
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "strategy": "hybrid",
                "channels": [
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    }
                ],
            },
            "source_status": {
                "crossref_keyword:q_keyword_1": {
                    "provider": "crossref",
                    "channel": "crossref_keyword",
                    "query_variant": "q_keyword_1",
                    "status": "success",
                    "count": 0,
                }
            },
            "results": [],
        }

        report = validate_search_payload_contract(payload)

        self.assertFalse(report["valid"])
        self.assertIn(
            "query_plan channel has no source_status entry: openalex_semantic:q_semantic_1",
            report["errors"],
        )
        self.assertIn(
            "source_status entry is not present in query_plan channels: crossref_keyword:q_keyword_1",
            report["warnings"],
        )

    def test_search_contract_validation_requires_query_plan_strategy(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "count": 0,
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "channels": [],
            },
            "source_status": {},
            "results": [],
        }

        report = validate_search_payload_contract(payload)

        self.assertFalse(report["valid"])
        self.assertIn("query_plan.strategy missing", report["errors"])

    def test_search_contract_validation_rejects_duplicate_query_plan_channel_keys(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "count": 0,
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "strategy": "hybrid",
                "channels": [
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    },
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    },
                ],
            },
            "source_status": {
                "openalex_semantic:q_semantic_1": {
                    "provider": "openalex",
                    "channel": "openalex_semantic",
                    "query_variant": "q_semantic_1",
                    "status": "success",
                    "count": 0,
                }
            },
            "results": [],
        }

        report = validate_search_payload_contract(payload)

        self.assertFalse(report["valid"])
        self.assertIn(
            "query_plan.channels[2] duplicates channel/query_variant: openalex_semantic:q_semantic_1",
            report["errors"],
        )

    def test_search_contract_validation_requires_provenance_weight_to_match_query_plan(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "count": 1,
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "strategy": "hybrid",
                "channels": [
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    }
                ],
            },
            "source_status": {
                "openalex_semantic:q_semantic_1": {
                    "provider": "openalex",
                    "channel": "openalex_semantic",
                    "query_variant": "q_semantic_1",
                    "status": "success",
                    "count": 1,
                }
            },
            "results": [
                {
                    "index": 1,
                    "source": "openalex",
                    "sources": ["openalex"],
                    "title": "Mismatched Weight",
                    "doi": "10.1000/weight",
                    "canonical_work_id": "doi:10.1000/weight",
                    "version_family_id": "title:mismatched weight",
                    "version_type": "journal",
                    "related_versions": [],
                    "retrieval_provenance": [
                        {
                            "provider": "openalex",
                            "channel": "openalex_semantic",
                            "query_variant": "q_semantic_1",
                            "rank": 1,
                            "weight": 1.0,
                        }
                    ],
                }
            ],
        }

        report = validate_search_payload_contract(payload)

        self.assertFalse(report["valid"])
        self.assertIn(
            "results[1].retrieval_provenance[1].weight does not match query_plan channel weight: openalex_semantic:q_semantic_1",
            report["errors"],
        )

    def test_search_contract_validation_reports_missing_v2_contract_fields(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "query_plan": {
                "schema": "instsci.query_plan.v1",
                "strategy": "hybrid",
                "channels": [
                    {
                        "provider": "openalex",
                        "channel": "openalex_semantic",
                        "query_variant": "q_semantic_1",
                        "weight": 1.1,
                    }
                ],
            },
            "source_status": {
                "openalex_semantic:q_semantic_1": {
                    "status": "success",
                    "count": 1,
                }
            },
            "results": [
                {
                    "index": 1,
                    "title": "Broken Hybrid Paper",
                    "doi": "10.1000/broken",
                    "canonical_work_id": "doi:10.1000/broken",
                    "retrieval_provenance": [{"channel": "openalex_semantic", "query_variant": "q_semantic_1"}],
                }
            ],
        }

        report = validate_search_payload_contract(payload)

        self.assertFalse(report["valid"])
        self.assertIn("source_status.openalex_semantic:q_semantic_1.provider missing", report["errors"])
        self.assertIn("source_status.openalex_semantic:q_semantic_1.channel missing", report["errors"])
        self.assertIn("source_status.openalex_semantic:q_semantic_1.query_variant missing", report["errors"])
        self.assertIn("results[1].version_family_id missing", report["errors"])
        self.assertIn("results[1].related_versions must be a list", report["errors"])

    def test_cli_search_validate_writes_contract_report_and_fails_invalid_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            search_path = Path(tmp) / "broken_search.json"
            output_path = Path(tmp) / "contract_report.json"
            search_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_results.v2",
                        "query_plan": {"schema": "instsci.query_plan.v1", "strategy": "hybrid", "channels": []},
                        "source_status": {},
                        "results": [{"index": 1, "title": "Missing contract fields"}],
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-validate", str(search_path), "--output", str(output_path)])
            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertFalse(report["valid"])
        self.assertIn("results[1].canonical_work_id missing", report["errors"])
        self.assertIn("Search contract validation", result.output)

    def test_cli_search_validate_rejects_non_search_result_schemas(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            expansion_path = Path(tmp) / "expanded.json"
            output_path = Path(tmp) / "contract_report.json"
            expansion_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.search_expansion.v1",
                        "candidate_count": 0,
                        "acquisition_started": False,
                        "results": [],
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-validate", str(expansion_path), "--output", str(output_path)])
            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertFalse(report["valid"])
        self.assertIn(
            "schema is not a search result contract: instsci.search_expansion.v1",
            report["errors"],
        )

    def test_cli_search_validate_rejects_missing_schema(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            search_path = Path(tmp) / "missing_schema.json"
            output_path = Path(tmp) / "contract_report.json"
            search_path.write_text(json.dumps({"results": []}), encoding="utf-8")

            result = runner.invoke(app, ["search-validate", str(search_path), "--output", str(output_path)])
            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertFalse(report["valid"])
        self.assertIn("schema missing", report["errors"])

    def test_work_identity_derives_canonical_family_and_version_type(self) -> None:
        identity = derive_work_identity(
            doi="https://doi.org/10.1000/JOURNAL",
            arxiv_id="2401.00001v2",
            title="Shared Version Study",
            journal="Journal of Testing",
        )

        self.assertEqual(identity["canonical_work_id"], "doi:10.1000/journal")
        self.assertEqual(identity["version_family_id"], "title:shared version study")
        self.assertEqual(identity["version_type"], "journal")

        record = result_to_record(SearchResult(title="Shared Version Study", arxiv_id="arXiv:2401.00001v3"), 1)
        self.assertEqual(record["canonical_work_id"], "arxiv:2401.00001")
        self.assertEqual(record["version_family_id"], "title:shared version study")
        self.assertEqual(record["version_type"], "preprint")

    def test_selection_parser_supports_ranges_and_rejects_out_of_range(self) -> None:
        self.assertEqual(parse_selection_indices("1,3-5", 5), [1, 3, 4, 5])
        self.assertEqual(parse_selection_indices("", 3), [1, 2, 3])
        with self.assertRaisesRegex(ValueError, "out of range"):
            parse_selection_indices("4", 3)

    def test_selection_keeps_unique_dois_and_reports_missing_values(self) -> None:
        payload = {
            "results": [
                {"index": 1, "title": "One", "doi": "10.1000/one"},
                {"index": 2, "title": "Duplicate", "doi": "https://doi.org/10.1000/ONE"},
                {"index": 3, "title": "No DOI", "doi": ""},
            ]
        }
        selected, skipped = select_doi_records(payload, [1, 2, 3])
        self.assertEqual([record["doi"] for record in selected], ["10.1000/one"])
        self.assertEqual([item["reason"] for item in skipped], ["duplicate_doi", "missing_doi"])

    def test_cli_search_export_then_select_writes_papers_input(self) -> None:
        runner = CliRunner()
        results = [
            SearchResult(title="Selected", authors=["A"], year=2024, doi="10.1000/selected"),
            SearchResult(title="No DOI", authors=["B"], year=2023, arxiv_id="2401.00001"),
        ]
        response = multi_search.MultiSearchResponse(
            results=[multi_search._from_provider(result, "semantic_scholar") for result in results],
            source_status={"semantic_scholar": {"status": "success", "count": 2}},
        )
        with TemporaryDirectory() as tmp, patch(
            "instsci.cli.multi_search.search_with_status", return_value=response
        ):
            search_path = Path(tmp) / "search.json"
            doi_path = Path(tmp) / "selected_dois.txt"
            search_result = runner.invoke(app, ["search", "topic", "--output", str(search_path)])
            select_result = runner.invoke(
                app,
                ["select", str(search_path), "--indices", "1-2", "--output", str(doi_path)],
            )
            payload = json.loads(search_path.read_text(encoding="utf-8"))
            dois = doi_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(search_result.exit_code, 0, search_result.output)
        self.assertEqual(select_result.exit_code, 0, select_result.output)
        self.assertEqual(payload["query"], "topic")
        self.assertEqual(payload["source_status"]["semantic_scholar"]["status"], "success")
        self.assertEqual(dois, ["10.1000/selected"])

    def test_offline_benchmark_metrics_are_stable(self) -> None:
        records = [
            {"doi": "10.1000/a", "title": "A"},
            {"doi": "10.1000/b", "title": "B"},
            {"doi": "10.1000/c", "title": "C"},
        ]
        judgments = {"doi:10.1000/a": 3, "doi:10.1000/b": 0, "doi:10.1000/c": 2}
        metrics = evaluate_ranked_results(records, judgments, k_values=(2, 3))

        self.assertEqual(metrics["count"], 3)
        self.assertEqual(metrics["precision@2"], 0.5)
        self.assertEqual(metrics["recall@3"], 1.0)
        self.assertGreater(metrics["ndcg@3"], metrics["ndcg@2"])

    def test_benchmark_reports_must_find_recall_separately(self) -> None:
        records = [
            {"doi": "10.1000/a", "title": "A"},
            {"doi": "10.1000/c", "title": "C"},
        ]
        judgments = {"doi:10.1000/a": 3}
        metrics = evaluate_ranked_results(
            records,
            judgments,
            k_values=(1, 2),
            must_find_ids=["doi:10.1000/a", "10.1000/b", {"doi": "10.1000/c"}],
        )

        self.assertEqual(metrics["relevant_total"], 1)
        self.assertEqual(metrics["recall@2"], 1.0)
        self.assertEqual(metrics["must_find_total"], 3)
        self.assertEqual(metrics["must_find_hits@1"], 1)
        self.assertEqual(metrics["must_find_recall@2"], 2 / 3)

    def test_load_must_find_accepts_json_contract(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "must_find.json"
            path.write_text(
                json.dumps(
                    {
                        "must_find": [
                            "doi:10.1000/a",
                            "10.1000/b",
                            {"doi": "10.1000/c"},
                            {"id": "arxiv:2401.00001"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            ids = load_must_find(path)

        self.assertEqual(ids, ["doi:10.1000/a", "doi:10.1000/b", "doi:10.1000/c", "arxiv:2401.00001"])

    def test_cli_search_benchmark_accepts_must_find_file(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "candidate.json"
            judgments_path = Path(tmp) / "judgments.json"
            must_find_path = Path(tmp) / "must_find.json"
            metrics_path = Path(tmp) / "metrics.json"
            candidate_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {"index": 1, "doi": "10.1000/a"},
                            {"index": 2, "doi": "10.1000/c"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            judgments_path.write_text(json.dumps({"doi:10.1000/a": 3}), encoding="utf-8")
            must_find_path.write_text(
                json.dumps({"must_find": ["doi:10.1000/a", "doi:10.1000/b", "doi:10.1000/c"]}),
                encoding="utf-8",
            )

            result = runner.invoke(
                app,
                [
                    "search-benchmark",
                    str(candidate_path),
                    str(judgments_path),
                    "--must-find",
                    str(must_find_path),
                    "--output",
                    str(metrics_path),
                ],
            )
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(metrics["must_find_total"], 3)
        self.assertEqual(metrics["must_find_recall@20"], 2 / 3)

    def test_cli_search_benchmark_reports_judgment_coverage(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "candidate.json"
            baseline_path = Path(tmp) / "baseline.json"
            judgments_path = Path(tmp) / "judgments.json"
            metrics_path = Path(tmp) / "metrics.json"
            candidate_path.write_text(
                json.dumps({"results": [{"index": 1, "doi": "10.1000/a"}]}),
                encoding="utf-8",
            )
            baseline_path.write_text(
                json.dumps({"results": [{"index": 1, "doi": "10.1000/b"}]}),
                encoding="utf-8",
            )
            judgments_path.write_text(
                json.dumps(
                    {
                        "judgments": [
                            {"id": "doi:10.1000/a", "grade": 3},
                            {"id": "doi:10.1000/b", "grade": None},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                app,
                [
                    "search-benchmark",
                    str(candidate_path),
                    str(judgments_path),
                    "--baseline",
                    str(baseline_path),
                    "--output",
                    str(metrics_path),
                ],
            )
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(metrics["judgment_count"], 2)
        self.assertEqual(metrics["graded_judgment_count"], 1)
        self.assertEqual(metrics["ungraded_judgment_count"], 1)
        self.assertFalse(metrics["all_judgments_graded"])
    def test_benchmark_fixture_compare_reports_hybrid_gain(self) -> None:
        fixture_dir = Path("instsci/tests/fixtures/search_benchmark")
        legacy = load_search_payload(fixture_dir / "legacy.json")
        hybrid = load_search_payload(fixture_dir / "hybrid.json")
        judgments = load_judgments(fixture_dir / "judgments.json")
        report = compare_ranked_results(hybrid["results"], legacy["results"], judgments, k_values=(2, 3))

        self.assertGreater(report["delta"]["ndcg@2"], 0)
        self.assertGreaterEqual(report["candidate"]["recall@3"], report["baseline"]["recall@3"])
        self.assertEqual(
            [item["id"] for item in report["baseline_ranking_snapshot"]],
            ["doi:10.1000/review", "doi:10.1000/core", "doi:10.1000/methods", "doi:10.1000/fluid"],
        )
        self.assertEqual(report["candidate_ranking_snapshot"][0]["id"], "doi:10.1000/core")
        self.assertEqual(report["baseline_ranking_snapshot"][0]["rank"], 1)

    def test_cli_search_benchmark_fixture_outputs_stable_metrics(self) -> None:
        runner = CliRunner()
        fixture_dir = Path("instsci/tests/fixtures/search_benchmark")
        with TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "metrics_first.json"
            second_path = Path(tmp) / "metrics_second.json"
            args = [
                "search-benchmark",
                str(fixture_dir / "hybrid.json"),
                str(fixture_dir / "judgments.json"),
                "--baseline",
                str(fixture_dir / "legacy.json"),
            ]

            first_result = runner.invoke(app, [*args, "--output", str(first_path)])
            second_result = runner.invoke(app, [*args, "--output", str(second_path)])
            first = json.loads(first_path.read_text(encoding="utf-8"))
            second = json.loads(second_path.read_text(encoding="utf-8"))

        self.assertEqual(first_result.exit_code, 0, first_result.output)
        self.assertEqual(second_result.exit_code, 0, second_result.output)
        self.assertEqual(first, second)
        self.assertGreater(first["delta"]["ndcg@10"], 0)
        self.assertEqual(first["candidate_ranking_snapshot"][0]["id"], "doi:10.1000/core")

    def test_validate_benchmark_metrics_report_reports_contract_errors(self) -> None:
        report = validate_benchmark_metrics_report(
            {
                "candidate": {
                    "count": 2,
                    "precision@10": 1.2,
                    "recall@20": -0.1,
                    "ndcg@20": 0.5,
                    "mrr": 2.0,
                    "duplicate_rate": 1.2,
                    "judged_count": 3,
                    "relevant_total": -1,
                },
                "baseline": {
                    "count": 2,
                    "precision@10": 0.2,
                    "recall@20": 0.4,
                    "ndcg@20": 0.4,
                    "mrr": 0.5,
                    "duplicate_rate": 0.0,
                    "judged_count": 1,
                    "relevant_total": 1,
                },
                "delta": {"precision@10": 0.1, "recall@20": 0.5, "ndcg@20": "bad"},
                "candidate_ranking_snapshot": [{"rank": 2, "id": "", "title": ""}],
                "baseline_ranking_snapshot": ["bad-row"],
                "judgment_count": 2,
                "graded_judgment_count": 2,
                "ungraded_judgment_count": 1,
                "all_judgments_graded": True,
            }
        )

        self.assertEqual(report["schema"], "instsci.search_benchmark_validation.v1")
        self.assertFalse(report["valid"])
        self.assertIn("candidate.precision@10 must be between 0 and 1", report["errors"])
        self.assertIn("candidate.recall@20 must be between 0 and 1", report["errors"])
        self.assertIn("candidate.mrr must be between 0 and 1", report["errors"])
        self.assertIn("candidate.duplicate_rate must be between 0 and 1", report["errors"])
        self.assertIn("candidate.judged_count cannot exceed count", report["errors"])
        self.assertIn("candidate.relevant_total must be non-negative", report["errors"])
        self.assertIn("delta.precision@10 does not match candidate-baseline", report["errors"])
        self.assertIn("delta.ndcg@20 must be a number", report["errors"])
        self.assertIn("candidate_ranking_snapshot[0].rank must be 1", report["errors"])
        self.assertIn("candidate_ranking_snapshot[0].id is required", report["errors"])
        self.assertIn("candidate_ranking_snapshot[0].title is required", report["errors"])
        self.assertIn("baseline_ranking_snapshot[0] must be an object", report["errors"])
        self.assertIn("graded_judgment_count + ungraded_judgment_count must equal judgment_count", report["errors"])
        self.assertIn("all_judgments_graded does not match ungraded_judgment_count", report["errors"])

    def test_cli_search_benchmark_validate_writes_report_and_fails_invalid_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            metrics_path = Path(tmp) / "metrics.json"
            validation_path = Path(tmp) / "metrics_validation.json"
            metrics_path.write_text(
                json.dumps(
                    {
                        "count": 1,
                        "precision@10": 1.5,
                        "recall@20": 0.5,
                        "ndcg@20": 0.5,
                        "mrr": 0.5,
                        "duplicate_rate": 0.0,
                        "judged_count": 1,
                        "relevant_total": 1,
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                app,
                ["search-benchmark-validate", str(metrics_path), "--output", str(validation_path)],
            )
            payload = json.loads(validation_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["schema"], "instsci.search_benchmark_validation.v1")
        self.assertFalse(payload["valid"])
        self.assertIn("precision@10 must be between 0 and 1", payload["errors"])

    def test_ranking_snapshot_payload_detects_rank_drift(self) -> None:
        payload = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "results": [
                {"index": 1, "title": "A", "doi": "10.1000/a"},
                {"index": 2, "title": "B", "doi": "10.1000/b"},
            ],
        }
        snapshot = build_ranking_snapshot_payload(payload, label="legacy")
        changed = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "results": [
                {"index": 1, "title": "B", "doi": "10.1000/b"},
                {"index": 2, "title": "C", "doi": "10.1000/c"},
            ],
        }
        report = compare_ranking_snapshot_payload(changed, snapshot)

        self.assertEqual(snapshot["schema"], "instsci.ranking_snapshot.v1")
        self.assertEqual(snapshot["label"], "legacy")
        self.assertEqual([item["id"] for item in snapshot["ranking"]], ["doi:10.1000/a", "doi:10.1000/b"])
        self.assertFalse(report["matched"])
        self.assertEqual(report["summary"]["rank_changed_count"], 1)
        self.assertEqual(report["summary"]["missing_count"], 1)
        self.assertEqual(report["summary"]["new_count"], 1)
        self.assertIn("doi:10.1000/a", report["missing_ids"])
        self.assertIn("doi:10.1000/c", report["new_ids"])

    def test_validate_ranking_snapshot_payload_reports_contract_errors(self) -> None:
        report = validate_ranking_snapshot_payload(
            {
                "schema": "instsci.ranking_snapshot.v1",
                "count": 3,
                "ranking": [
                    {"rank": 2, "id": "doi:10.1000/a", "title": "A"},
                    {"rank": 2, "id": "doi:10.1000/a", "title": ""},
                    "bad-row",
                ],
            }
        )

        self.assertEqual(report["schema"], "instsci.ranking_snapshot_validation.v1")
        self.assertFalse(report["valid"])
        self.assertIn("count does not match ranking length", report["errors"])
        self.assertIn("ranking[0].rank must be 1", report["errors"])
        self.assertIn("ranking[1].rank duplicates an earlier row", report["errors"])
        self.assertIn("ranking[1].id duplicates an earlier row", report["errors"])
        self.assertIn("ranking[1].title is required", report["errors"])
        self.assertIn("ranking[2] must be an object", report["errors"])

    def test_validate_ranking_snapshot_check_reports_contract_errors(self) -> None:
        report = validate_ranking_snapshot_check(
            {
                "schema": "instsci.ranking_snapshot_check.v1",
                "matched": True,
                "summary": {
                    "expected_count": 1,
                    "current_count": 1,
                    "rank_changed_count": 2,
                    "missing_count": 2,
                    "new_count": 2,
                },
                "rank_changes": [{"id": "", "expected_rank": 1, "current_rank": 1, "delta": "bad"}],
                "missing_ids": ["doi:10.1000/a"],
                "new_ids": ["doi:10.1000/b"],
                "current_ranking": [{"rank": 2, "id": "", "title": ""}],
            }
        )

        self.assertEqual(report["schema"], "instsci.ranking_snapshot_check_validation.v1")
        self.assertFalse(report["valid"])
        self.assertIn("matched must be false when drift arrays are non-empty", report["errors"])
        self.assertIn("summary.rank_changed_count does not match rank_changes length", report["errors"])
        self.assertIn("summary.missing_count does not match missing_ids length", report["errors"])
        self.assertIn("summary.new_count does not match new_ids length", report["errors"])
        self.assertIn("rank_changes[0].id is required", report["errors"])
        self.assertIn("rank_changes[0].delta must be an integer", report["errors"])
        self.assertIn("current_ranking[0].rank must be 1", report["errors"])
        self.assertIn("current_ranking[0].id is required", report["errors"])
        self.assertIn("current_ranking[0].title is required", report["errors"])

    def test_cli_search_snapshot_validators_write_reports_and_fail_invalid_payloads(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snapshot.json"
            snapshot_validation_path = Path(tmp) / "snapshot_validation.json"
            check_path = Path(tmp) / "snapshot_check.json"
            check_validation_path = Path(tmp) / "snapshot_check_validation.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.ranking_snapshot.v1",
                        "count": 1,
                        "ranking": [{"rank": 2, "id": "doi:10.1000/a", "title": "A"}],
                    }
                ),
                encoding="utf-8",
            )
            check_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.ranking_snapshot_check.v1",
                        "matched": True,
                        "summary": {
                            "expected_count": 1,
                            "current_count": 1,
                            "rank_changed_count": 0,
                            "missing_count": 1,
                            "new_count": 0,
                        },
                        "rank_changes": [],
                        "missing_ids": ["doi:10.1000/a"],
                        "new_ids": [],
                        "current_ranking": [],
                    }
                ),
                encoding="utf-8",
            )

            snapshot_result = runner.invoke(
                app,
                ["search-snapshot-validate", str(snapshot_path), "--output", str(snapshot_validation_path)],
            )
            check_result = runner.invoke(
                app,
                ["search-snapshot-check-validate", str(check_path), "--output", str(check_validation_path)],
            )
            snapshot_report = json.loads(snapshot_validation_path.read_text(encoding="utf-8"))
            check_report = json.loads(check_validation_path.read_text(encoding="utf-8"))

        self.assertEqual(snapshot_result.exit_code, 2)
        self.assertEqual(snapshot_report["schema"], "instsci.ranking_snapshot_validation.v1")
        self.assertIn("ranking[0].rank must be 1", snapshot_report["errors"])
        self.assertEqual(check_result.exit_code, 2)
        self.assertEqual(check_report["schema"], "instsci.ranking_snapshot_check_validation.v1")
        self.assertIn("matched must be false when drift arrays are non-empty", check_report["errors"])

    def test_release_gate_report_flags_recall_and_severe_ndcg_regressions(self) -> None:
        passing = {
            "query_id": "q_pass",
            "query": "pyrite sulfur isotope",
            "candidate": {"recall@20": 1.0, "recall@50": 1.0, "ndcg@20": 0.60},
            "baseline": {"recall@20": 0.8, "recall@50": 1.0, "ndcg@20": 0.50},
            "delta": {"recall@20": 0.2, "recall@50": 0.0, "ndcg@20": 0.10},
        }
        failing = {
            "query_id": "q_fail",
            "candidate": {"recall@20": 0.5, "recall@50": 0.7, "ndcg@20": 0.55},
            "baseline": {"recall@20": 0.6, "recall@50": 0.8, "ndcg@20": 0.80},
            "delta": {"recall@20": -0.1, "recall@50": -0.1, "ndcg@20": -0.25},
        }

        report = build_release_gate_report([passing, failing])

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["query_count"], 2)
        self.assertEqual(report["summary"]["ndcg_improved_share"], 0.5)
        self.assertEqual(report["summary"]["manual_review_required_count"], 1)
        passing_row = next(item for item in report["queries"] if item["query_id"] == "q_pass")
        self.assertEqual(passing_row["query"], "pyrite sulfur isotope")
        self.assertEqual(passing_row["diagnostics"], [])
        failing_row = next(item for item in report["queries"] if item["query_id"] == "q_fail")
        self.assertIn("recall@20_below_baseline", failing_row["failures"])
        self.assertIn("recall@50_below_baseline", failing_row["failures"])
        self.assertIn("severe_ndcg@20_regression", failing_row["failures"])
        diagnostics = failing_row["diagnostics"]
        self.assertEqual(
            [item["action"] for item in diagnostics],
            ["inspect_hybrid_recall_loss", "inspect_hybrid_recall_loss", "manual_relevance_review"],
        )
        severe = diagnostics[-1]
        self.assertEqual(severe["metric"], "ndcg@20")
        self.assertEqual(severe["severity"], "manual_review_required")
        self.assertAlmostEqual(severe["relative_delta"], -0.3125)
        blockers = report["release_gate_blockers"]
        self.assertEqual(report["summary"]["release_gate_blocker_count"], 3)
        self.assertEqual([item["type"] for item in blockers], ["recall_below_baseline", "recall_below_baseline", "manual_review_required"])
        self.assertEqual([item["metric"] for item in blockers], ["recall@20", "recall@50", "ndcg@20"])
        self.assertEqual([item["query_id"] for item in blockers], ["q_fail", "q_fail", "q_fail"])
        self.assertEqual(
            [item["action"] for item in blockers],
            ["inspect_hybrid_recall_loss", "inspect_hybrid_recall_loss", "manual_relevance_review"],
        )
        self.assertTrue(all(item["blocks_gate"] for item in blockers))

    def test_release_gate_report_blocks_when_ndcg_improved_share_is_too_low(self) -> None:
        flat_queries = [
            {
                "query_id": "q_one",
                "query": "pyrite sulfur isotope",
                "candidate": {"recall@20": 1.0, "recall@50": 1.0, "ndcg@20": 0.80},
                "baseline": {"recall@20": 1.0, "recall@50": 1.0, "ndcg@20": 0.80},
                "delta": {"recall@20": 0.0, "recall@50": 0.0, "ndcg@20": 0.0},
            },
            {
                "query_id": "q_two",
                "query": "uranium fluid inclusion",
                "candidate": {"recall@20": 1.0, "recall@50": 1.0, "ndcg@20": 0.70},
                "baseline": {"recall@20": 1.0, "recall@50": 1.0, "ndcg@20": 0.70},
                "delta": {"recall@20": 0.0, "recall@50": 0.0, "ndcg@20": 0.0},
            },
        ]

        report = build_release_gate_report(flat_queries)

        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["ndcg@20_improved_share"])
        self.assertEqual(report["summary"]["ndcg_improved_queries"], 0)
        self.assertEqual(report["summary"]["ndcg_improved_share"], 0.0)
        blockers = report["release_gate_blockers"]
        self.assertEqual(len(blockers), 1)
        blocker = blockers[0]
        self.assertEqual(blocker["type"], "ndcg_improved_share_below_threshold")
        self.assertEqual(blocker["metric"], "ndcg@20_improved_share")
        self.assertEqual(blocker["action"], "inspect_hybrid_ranking_quality")
        self.assertTrue(blocker["blocks_gate"])
        self.assertEqual(blocker["observed"], 0.0)
        self.assertEqual(blocker["minimum"], 0.5)
        self.assertNotIn("threshold", blocker)

    def test_release_gate_validation_allows_ungraded_gate_without_ndcg_blocker(self) -> None:
        from instsci.search_benchmark import validate_release_gate_report

        report = {
            "schema": "instsci.search_release_gate.v1",
            "passed": False,
            "config": {"min_ndcg_improved_share": 0.5},
            "summary": {
                "query_count": 0,
                "ndcg_improved_queries": 0,
                "ndcg_improved_share": 0.0,
                "release_gate_blocker_count": 1,
            },
            "checks": {"ndcg@20_improved_share": False},
            "queries": [],
            "release_gate_blockers": [
                {
                    "type": "data_issue",
                    "query_id": "q1",
                    "reason": "ungraded_judgments",
                    "severity": "failure",
                    "blocks_gate": True,
                }
            ],
            "data_issues": [{"query_id": "q1", "reason": "ungraded_judgments"}],
        }

        validation = validate_release_gate_report(report)

        self.assertTrue(validation["valid"], validation)

    def test_cli_search_benchmark_writes_gate_report_when_baseline_is_supplied(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "candidate.json"
            baseline_path = Path(tmp) / "baseline.json"
            judgments_path = Path(tmp) / "judgments.json"
            gate_path = Path(tmp) / "gate.json"
            candidate_path.write_text(
                json.dumps(
                    {
                        "query": "pyrite sulfur isotope",
                        "results": [
                            {"index": 1, "doi": "10.1000/a"},
                            {"index": 2, "doi": "10.1000/b"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            baseline_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {"index": 1, "doi": "10.1000/b"},
                            {"index": 2, "doi": "10.1000/a"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            judgments_path.write_text(json.dumps({"doi:10.1000/a": 3, "doi:10.1000/b": 2}), encoding="utf-8")

            result = runner.invoke(
                app,
                [
                    "search-benchmark",
                    str(candidate_path),
                    str(judgments_path),
                    "--baseline",
                    str(baseline_path),
                    "--gate-output",
                    str(gate_path),
                ],
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(gate["schema"], "instsci.search_release_gate.v1")
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["queries"][0]["baseline_ranking_snapshot"][0]["id"], "doi:10.1000/b")
        self.assertEqual(gate["queries"][0]["candidate_ranking_snapshot"][0]["id"], "doi:10.1000/a")
        self.assertEqual(gate["queries"][0]["query"], "pyrite sulfur isotope")

    def test_cli_search_benchmark_gate_blocks_ungraded_judgments(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "candidate.json"
            baseline_path = Path(tmp) / "baseline.json"
            judgments_path = Path(tmp) / "judgments.json"
            gate_path = Path(tmp) / "gate.json"
            candidate_path.write_text(
                json.dumps({"query": "pyrite sulfur isotope", "results": [{"index": 1, "doi": "10.1000/a"}]}),
                encoding="utf-8",
            )
            baseline_path.write_text(
                json.dumps({"results": [{"index": 1, "doi": "10.1000/b"}]}),
                encoding="utf-8",
            )
            judgments_path.write_text(
                json.dumps(
                    {
                        "judgments": [
                            {"id": "doi:10.1000/a", "grade": 3},
                            {"id": "doi:10.1000/b", "grade": None},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                app,
                [
                    "search-benchmark",
                    str(candidate_path),
                    str(judgments_path),
                    "--baseline",
                    str(baseline_path),
                    "--gate-output",
                    str(gate_path),
                ],
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["summary"]["judgment_count"], 2)
        self.assertEqual(gate["summary"]["graded_judgment_count"], 1)
        self.assertEqual(gate["summary"]["ungraded_judgment_count"], 1)
        self.assertFalse(gate["checks"]["all_judgments_graded"])
        self.assertIn({
            "query_id": str(candidate_path),
            "reason": "ungraded_judgments",
            "judgment_count": "2",
            "graded_judgment_count": "1",
            "ungraded_judgment_count": "1",
        }, gate["data_issues"])
        self.assertEqual(gate["release_gate_blockers"][0]["type"], "data_issue")
        self.assertTrue(gate["release_gate_blockers"][0]["blocks_gate"])

    def test_render_benchmark_markdown_summarizes_candidate_baseline_and_delta(self) -> None:
        report = {
            "candidate": {"count": 2, "precision@10": 0.2, "recall@20": 1.0, "ndcg@20": 0.75, "mrr": 1.0},
            "baseline": {"count": 2, "precision@10": 0.1, "recall@20": 0.5, "ndcg@20": 0.50, "mrr": 0.5},
            "delta": {"precision@10": 0.1, "recall@20": 0.5, "ndcg@20": 0.25, "mrr": 0.5},
            "candidate_ranking_snapshot": [{"rank": 1, "id": "doi:10.1000/a", "title": "Candidate A"}],
            "baseline_ranking_snapshot": [{"rank": 1, "id": "doi:10.1000/b", "title": "Baseline B"}],
        }

        markdown = render_benchmark_markdown(report, title="Hybrid vs legacy")

        self.assertIn("# Hybrid vs legacy", markdown)
        self.assertIn("| metric | candidate | baseline | delta |", markdown)
        self.assertIn("ndcg@20", markdown)
        self.assertIn("doi:10.1000/a", markdown)
        self.assertIn("doi:10.1000/b", markdown)

    def test_cli_search_benchmark_writes_markdown_report(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "candidate.json"
            baseline_path = Path(tmp) / "baseline.json"
            judgments_path = Path(tmp) / "judgments.json"
            markdown_path = Path(tmp) / "benchmark.md"
            candidate_path.write_text(
                json.dumps({"results": [{"index": 1, "title": "A", "doi": "10.1000/a"}]}),
                encoding="utf-8",
            )
            baseline_path.write_text(
                json.dumps({"results": [{"index": 1, "title": "B", "doi": "10.1000/b"}]}),
                encoding="utf-8",
            )
            judgments_path.write_text(
                json.dumps(
                    {
                        "judgments": [
                            {"id": "doi:10.1000/a", "grade": 3},
                            {"id": "doi:10.1000/b", "grade": 1},
                            {"id": "doi:10.1000/c", "grade": None},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                app,
                [
                    "search-benchmark",
                    str(candidate_path),
                    str(judgments_path),
                    "--baseline",
                    str(baseline_path),
                    "--markdown-output",
                    str(markdown_path),
                ],
            )
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("InstSci Search Benchmark", markdown)
        self.assertIn("doi:10.1000/a", markdown)
        self.assertIn("delta", markdown)
        self.assertIn("## Judgment Coverage", markdown)
        self.assertIn("| ungraded_judgment_count | 1 |", markdown)
        self.assertIn("| all_judgments_graded | false |", markdown)

    def test_cli_search_snapshot_and_check_write_reports(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            search_path = Path(tmp) / "search.json"
            changed_path = Path(tmp) / "changed.json"
            snapshot_path = Path(tmp) / "snapshot.json"
            check_path = Path(tmp) / "snapshot_check.json"
            search_path.write_text(
                json.dumps(
                    {
                        "query": "topic",
                        "results": [
                            {"index": 1, "title": "A", "doi": "10.1000/a"},
                            {"index": 2, "title": "B", "doi": "10.1000/b"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            changed_path.write_text(
                json.dumps(
                    {
                        "query": "topic",
                        "results": [
                            {"index": 1, "title": "B", "doi": "10.1000/b"},
                            {"index": 2, "title": "A", "doi": "10.1000/a"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot_result = runner.invoke(
                app,
                ["search-snapshot", str(search_path), "--output", str(snapshot_path), "--label", "legacy"],
            )
            check_result = runner.invoke(
                app,
                ["search-snapshot-check", str(changed_path), str(snapshot_path), "--output", str(check_path)],
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            check = json.loads(check_path.read_text(encoding="utf-8"))

        self.assertEqual(snapshot_result.exit_code, 0, snapshot_result.output)
        self.assertEqual(check_result.exit_code, 2)
        self.assertEqual(snapshot["schema"], "instsci.ranking_snapshot.v1")
        self.assertEqual(check["schema"], "instsci.ranking_snapshot_check.v1")
        self.assertFalse(check["matched"])
        self.assertEqual(check["summary"]["rank_changed_count"], 2)

    def test_relevance_pool_includes_strategy_and_channel_candidates_blindly(self) -> None:
        legacy = {
            "schema": "instsci.search_results.v1",
            "query": "topic",
            "results": [
                {"index": 1, "title": "Legacy A", "doi": "10.1000/a", "year": 2020},
                {"index": 2, "title": "Duplicate B", "doi": "10.1000/b", "year": 2021},
            ],
        }
        hybrid = {
            "schema": "instsci.search_results.v2",
            "query": "topic",
            "query_plan": {"strategy": "hybrid"},
            "results": [
                {"index": 1, "title": "Duplicate B", "doi": "10.1000/b", "year": 2021},
                {"index": 2, "title": "Hybrid C", "doi": "10.1000/c", "year": 2022},
                {
                    "index": 40,
                    "title": "Channel D",
                    "doi": "10.1000/d",
                    "year": 2023,
                    "retrieval_provenance": [
                        {"channel": "openalex_semantic", "rank": 3},
                    ],
                },
            ],
        }

        pool = build_relevance_pool({"legacy": legacy, "hybrid": hybrid}, legacy_top=2, hybrid_top=2, channel_top=10)
        judgments = pool["judgments"]

        self.assertEqual(pool["schema"], "instsci.relevance_pool.v1")
        self.assertEqual({item["id"] for item in judgments}, {"doi:10.1000/a", "doi:10.1000/b", "doi:10.1000/c", "doi:10.1000/d"})
        self.assertEqual(len(judgments), 4)
        self.assertTrue(all(item["grade"] is None for item in judgments))
        self.assertTrue(all("pool_sources" not in item for item in judgments))
        self.assertTrue(all("retrieval_provenance" not in item for item in judgments))

    def test_validate_relevance_pool_reports_contract_errors(self) -> None:
        report = validate_relevance_pool(
            {
                "schema": "instsci.relevance_pool.v1",
                "count": 4,
                "pooling": {"anonymous": False, "legacy_top": -1, "hybrid_top": -1, "channel_top": -1},
                "judgments": [
                    {
                        "review_id": "",
                        "id": "",
                        "title": "",
                        "grade": 4,
                        "pool_sources": ["legacy"],
                    },
                    {
                        "review_id": "P0002",
                        "id": "doi:10.1000/a",
                        "title": "A",
                        "grade": None,
                        "retrieval_provenance": [{"channel": "openalex"}],
                    },
                    {
                        "review_id": "P0002",
                        "id": "doi:10.1000/a",
                        "title": "Duplicate",
                        "grade": "bad",
                    },
                ],
            }
        )

        self.assertEqual(report["schema"], "instsci.relevance_pool_validation.v1")
        self.assertFalse(report["valid"])
        self.assertIn("pooling.anonymous must be true", report["errors"])
        self.assertIn("pooling.legacy_top must be non-negative", report["errors"])
        self.assertIn("pooling.hybrid_top must be non-negative", report["errors"])
        self.assertIn("pooling.channel_top must be non-negative", report["errors"])
        self.assertIn("count does not match judgments length", report["errors"])
        self.assertIn("judgments[0].review_id is required", report["errors"])
        self.assertIn("judgments[0].id is required", report["errors"])
        self.assertIn("judgments[0].title is required", report["errors"])
        self.assertIn("judgments[0].grade must be null or an integer from 0-3", report["errors"])
        self.assertIn("judgments[0].pool_sources must not be present in blinded pools", report["errors"])
        self.assertIn("judgments[1].retrieval_provenance must not be present in blinded pools", report["errors"])
        self.assertIn("judgments[2].review_id duplicates an earlier row", report["errors"])
        self.assertIn("judgments[2].id duplicates an earlier row", report["errors"])
        self.assertIn("judgments[2].grade must be null or an integer from 0-3", report["errors"])

    def test_cli_search_pool_validate_writes_report_and_fails_invalid_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "judgments_pool.json"
            output_path = Path(tmp) / "pool_validation.json"
            pool_path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_pool.v1",
                        "count": 1,
                        "pooling": {"anonymous": True, "legacy_top": 30, "hybrid_top": 30, "channel_top": 10},
                        "judgments": [{"review_id": "P0001", "id": "doi:10.1000/a", "title": "A", "grade": 9}],
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-pool-validate", str(pool_path), "--output", str(output_path)])
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["schema"], "instsci.relevance_pool_validation.v1")
        self.assertFalse(payload["valid"])
        self.assertIn("judgments[0].grade must be null or an integer from 0-3", payload["errors"])

    def test_load_judgments_skips_ungraded_pool_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "judgments.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_pool.v1",
                        "judgments": [
                            {"id": "doi:10.1000/a", "grade": 3},
                            {"id": "doi:10.1000/b", "grade": None},
                            {"id": "doi:10.1000/c", "relevance": 2},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            judgments = load_judgments(path)

        self.assertEqual(judgments, {"doi:10.1000/a": 3, "doi:10.1000/c": 2})

    def test_load_judgments_rejects_invalid_grade_values(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "judgments.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "instsci.relevance_pool.v1",
                        "judgments": [
                            {"id": "doi:10.1000/a", "grade": 4},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "0-3"):
                load_judgments(path)

    def test_cli_search_benchmark_rejects_invalid_judgment_grade(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "candidate.json"
            judgments_path = Path(tmp) / "judgments.json"
            candidate_path.write_text(
                json.dumps({"results": [{"index": 1, "doi": "10.1000/a"}]}),
                encoding="utf-8",
            )
            judgments_path.write_text(
                json.dumps({"judgments": [{"id": "doi:10.1000/a", "grade": -1}]}),
                encoding="utf-8",
            )

            result = runner.invoke(app, ["search-benchmark", str(candidate_path), str(judgments_path)])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("0-3", result.output)

    def test_cli_search_pool_writes_pooled_judgment_template(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            legacy_path = Path(tmp) / "legacy.json"
            hybrid_path = Path(tmp) / "hybrid.json"
            output_path = Path(tmp) / "pool.json"
            legacy_path.write_text(
                json.dumps({"query": "topic", "results": [{"index": 1, "title": "Legacy A", "doi": "10.1000/a"}]}),
                encoding="utf-8",
            )
            hybrid_path.write_text(
                json.dumps(
                    {
                        "query": "topic",
                        "query_plan": {"strategy": "hybrid"},
                        "results": [
                            {"index": 1, "title": "Hybrid B", "doi": "10.1000/b"},
                            {
                                "index": 40,
                                "title": "Channel C",
                                "doi": "10.1000/c",
                                "retrieval_provenance": [{"channel": "openalex_semantic", "rank": 2}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                app,
                [
                    "search-pool",
                    "--legacy",
                    str(legacy_path),
                    "--hybrid",
                    str(hybrid_path),
                    "--output",
                    str(output_path),
                    "--legacy-top",
                    "1",
                    "--hybrid-top",
                    "1",
                    "--channel-top",
                    "10",
                ],
            )
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload["schema"], "instsci.relevance_pool.v1")
        self.assertEqual({item["id"] for item in payload["judgments"]}, {"doi:10.1000/a", "doi:10.1000/b", "doi:10.1000/c"})

    def test_readme_and_skill_expose_discovery_to_zotero_flow(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        skill = Path("skills/instsci/SKILL.md").read_text(encoding="utf-8")
        for text in (readme, skill):
            self.assertIn("instsci search", text)
            self.assertIn("instsci select", text)
            self.assertIn("instsci papers", text)
            self.assertIn("instsci zotero sync", text)
