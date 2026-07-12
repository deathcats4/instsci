from unittest import TestCase
from unittest.mock import patch

from instsci import multi_search
from instsci.search_pipeline import result_to_record
from instsci.sources.semantic_scholar import SearchResult


class MultiSearchTests(TestCase):
    def test_merges_same_doi_and_preserves_source_citation_counts(self) -> None:
        s2 = SearchResult(title="Shared title", authors=["A"], year=2024, doi="10.1000/ABC", citation_count=5)
        oa = SearchResult(title="Shared title", authors=["A", "B"], year=2024, doi="https://doi.org/10.1000/abc", journal="Journal", citation_count=8)
        cr = SearchResult(title="Shared title", year=2024, doi="10.1000/abc", citation_count=3)
        with (
            patch("instsci.multi_search.semantic_scholar.search", return_value=[s2]),
            patch("instsci.multi_search.openalex.search", return_value=[oa]),
            patch("instsci.multi_search.crossref.search", return_value=[cr]),
        ):
            results = multi_search.search("topic", limit=10)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].doi, "10.1000/abc")
        self.assertEqual(results[0].sources, ["semantic_scholar", "openalex", "crossref"])
        self.assertEqual(results[0].citation_counts, {"semantic_scholar": 5, "openalex": 8, "crossref": 3})
        self.assertEqual(results[0].citation_count, 8)

    def test_title_and_year_merge_record_when_one_source_lacks_doi(self) -> None:
        without_doi = SearchResult(title="A Study: Example", year=2023)
        with_doi = SearchResult(title="A Study Example", year=2023, doi="10.1000/example")
        with (
            patch("instsci.multi_search.semantic_scholar.search", return_value=[without_doi]),
            patch("instsci.multi_search.openalex.search", return_value=[with_doi]),
        ):
            results = multi_search.search("topic", sources="semantic_scholar,openalex")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].doi, "10.1000/example")

    def test_provider_failure_degrades_to_remaining_sources(self) -> None:
        crossref_result = SearchResult(title="Available", doi="10.1000/available")
        with (
            patch("instsci.multi_search.semantic_scholar.search", side_effect=RuntimeError("offline")),
            patch("instsci.multi_search.crossref.search", return_value=[crossref_result]),
        ):
            results = multi_search.search("topic", sources="semantic_scholar,crossref")
        self.assertEqual([result.doi for result in results], ["10.1000/available"])

    def test_export_record_includes_sources_and_citation_counts(self) -> None:
        result = multi_search.MergedSearchResult(
            title="Paper",
            doi="10.1000/paper",
            sources=["openalex", "crossref"],
            citation_counts={"openalex": 4, "crossref": 2},
        )
        record = result_to_record(result, 1)
        self.assertEqual(record["sources"], ["openalex", "crossref"])
        self.assertEqual(record["citation_counts"], {"openalex": 4, "crossref": 2})
