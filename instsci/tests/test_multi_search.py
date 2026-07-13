import asyncio
from unittest import TestCase
from unittest.mock import patch

from instsci import multi_search
from instsci.search_pipeline import result_to_record
from instsci.sources.semantic_scholar import SearchResult
from instsci.sources.errors import ProviderSearchError


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

    def test_same_title_and_year_with_different_dois_stays_separate(self) -> None:
        first = SearchResult(title="Same title", year=2024, doi="10.1000/first")
        second = SearchResult(title="Same title", year=2024, doi="10.1000/second")
        with (
            patch("instsci.multi_search.semantic_scholar.search", return_value=[first]),
            patch("instsci.multi_search.openalex.search", return_value=[second]),
        ):
            results = multi_search.search("topic", sources="semantic_scholar,openalex")

        self.assertEqual([result.doi for result in results], ["10.1000/first", "10.1000/second"])

    def test_provider_failure_degrades_to_remaining_sources(self) -> None:
        crossref_result = SearchResult(title="Available", doi="10.1000/available")
        with (
            patch("instsci.multi_search.semantic_scholar.search", side_effect=RuntimeError("offline")),
            patch("instsci.multi_search.crossref.search", return_value=[crossref_result]),
        ):
            response = multi_search.search_with_status("topic", sources="semantic_scholar,crossref")
        self.assertEqual([result.doi for result in response.results], ["10.1000/available"])
        self.assertEqual(response.source_status["semantic_scholar"]["status"], "error")
        self.assertEqual(response.source_status["crossref"], {"status": "success", "count": 1})

    def test_provider_rate_limit_is_distinct_from_zero_results(self) -> None:
        with patch(
            "instsci.multi_search.semantic_scholar.search",
            side_effect=ProviderSearchError("semantic_scholar", "rate_limited", "HTTP 429"),
        ):
            response = multi_search.search_with_status("topic", sources="semantic_scholar")

        self.assertEqual(response.results, [])
        self.assertEqual(response.source_status["semantic_scholar"]["status"], "rate_limited")

    def test_mcp_reports_source_specific_citations_and_provider_status(self) -> None:
        from instsci import mcp_server

        response = multi_search.MultiSearchResponse(
            results=[
                multi_search.MergedSearchResult(
                    title="Paper",
                    doi="10.1000/paper",
                    sources=["semantic_scholar", "openalex"],
                    citation_count=15,
                    citation_counts={"semantic_scholar": 12, "openalex": 15},
                )
            ],
            source_status={
                "semantic_scholar": {"status": "rate_limited", "count": 0},
                "openalex": {"status": "success", "count": 1},
            },
        )
        with patch("instsci.mcp_server.multi_search.search_with_status", return_value=response):
            text = asyncio.run(mcp_server.search_papers("topic"))

        self.assertIn("semantic_scholar: rate_limited", text)
        self.assertIn("Semantic Scholar 12; Openalex 15", text)
        self.assertNotIn("**Citations:** 15", text)

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
