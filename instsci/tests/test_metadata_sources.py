from unittest import TestCase
from unittest.mock import patch

from instsci.sources import crossref, openalex


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class MetadataSourceTests(TestCase):
    def test_openalex_parses_work_metadata_and_abstract(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "OpenAlex paper",
                    "publication_year": 2024,
                    "doi": "https://doi.org/10.1000/OA",
                    "authorships": [{"author": {"display_name": "A Author"}}],
                    "abstract_inverted_index": {"Hello": [0], "world": [1]},
                    "primary_location": {"source": {"display_name": "Journal"}},
                    "cited_by_count": 7,
                }
            ]
        }
        with patch("instsci.sources.openalex.request_with_retry", return_value=FakeResponse(payload)) as request:
            results = openalex.search("topic", limit=5, year_range="2020-2024", email="reader@example.edu")
        self.assertEqual(results[0].doi, "10.1000/OA")
        self.assertEqual(results[0].abstract, "Hello world")
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["per-page"], 5)
        self.assertIn("from_publication_date:2020-01-01", params["filter"])

    def test_crossref_parses_metadata_and_polite_parameters(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1000/CR",
                        "title": ["Crossref paper"],
                        "author": [{"given": "A", "family": "Author"}],
                        "published": {"date-parts": [[2023, 1, 2]]},
                        "container-title": ["Journal"],
                        "abstract": "<jats:p>Abstract text</jats:p>",
                        "is-referenced-by-count": 4,
                        "URL": "https://doi.org/10.1000/CR",
                    }
                ]
            }
        }
        with patch("instsci.sources.crossref.request_with_retry", return_value=FakeResponse(payload)) as request:
            results = crossref.search("topic", limit=5, year_range="2023-", email="reader@example.edu")
        self.assertEqual(results[0].authors, ["A Author"])
        self.assertEqual(results[0].abstract, "Abstract text")
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["mailto"], "reader@example.edu")
        self.assertEqual(params["filter"], "from-pub-date:2023-01-01")
