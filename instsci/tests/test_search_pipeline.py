import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from typer.testing import CliRunner

from instsci.cli import app
from instsci.search_pipeline import (
    build_search_payload,
    load_search_payload,
    parse_selection_indices,
    select_doi_records,
    write_search_payload,
)
from instsci.sources.semantic_scholar import SearchResult


class SearchPipelineTests(TestCase):
    def test_search_payload_round_trips_json_and_csv(self) -> None:
        payload = build_search_payload(
            "pyrite uranium",
            [SearchResult(title="Paper", authors=["A", "B"], year=2024, doi="https://doi.org/10.1000/Test")],
            year_range="2020-",
        )
        with TemporaryDirectory() as tmp:
            json_path = write_search_payload(payload, Path(tmp) / "results.json")
            csv_path = write_search_payload(payload, Path(tmp) / "results.csv")
            json_loaded = load_search_payload(json_path)
            csv_loaded = load_search_payload(csv_path)

        self.assertEqual(json_loaded["results"][0]["doi"], "10.1000/test")
        self.assertEqual(csv_loaded["results"][0]["authors"], ["A", "B"])

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
        with TemporaryDirectory() as tmp, patch("instsci.cli.multi_search.search", return_value=results):
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
        self.assertEqual(dois, ["10.1000/selected"])

    def test_readme_and_skill_expose_discovery_to_zotero_flow(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        skill = Path("skill/SKILL.md").read_text(encoding="utf-8")
        for text in (readme, skill):
            self.assertIn("instsci search", text)
            self.assertIn("instsci select", text)
            self.assertIn("instsci papers", text)
            self.assertIn("instsci zotero sync", text)
