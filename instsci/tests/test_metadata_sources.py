import json
import os
import requests
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from typer.testing import CliRunner

from instsci.cli import app
from instsci.sources import crossref, openalex
from instsci.sources.errors import ProviderSearchError


class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200, headers=None, url=""):
        self.payload = payload
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            suffix = f" for url: {self.url}" if self.url else ""
            raise requests.HTTPError(f"HTTP {self.status_code}{suffix}", response=self)
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

    def test_openalex_keyword_uses_environment_api_key(self) -> None:
        with patch.dict(os.environ, {"OPENALEX_API_KEY": "test-openalex-key"}), patch(
            "instsci.sources.openalex.request_with_retry",
            return_value=FakeResponse({"results": []}),
        ) as request:
            openalex.search("topic", limit=5)

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["api_key"], "test-openalex-key")

    def test_openalex_semantic_uses_environment_api_key(self) -> None:
        with patch.dict(os.environ, {"OPENALEX_API_KEY": "test-openalex-key"}), patch(
            "instsci.sources.openalex.request_with_retry",
            return_value=FakeResponse({"results": []}),
        ) as request:
            openalex.search_semantic("topic", limit=5, raise_on_error=True)

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["api_key"], "test-openalex-key")

    def test_openalex_429_with_empty_remaining_quota_reports_quota_exhausted(self) -> None:
        response = FakeResponse(
            {"error": "daily limit exceeded"},
            status_code=429,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"},
        )
        with patch("instsci.sources.openalex.request_with_retry", return_value=response):
            with self.assertRaises(ProviderSearchError) as error:
                openalex.search("topic", raise_on_error=True)

        self.assertEqual(error.exception.status, "quota_exhausted")
        self.assertIn("daily limit", error.exception.detail)

    def test_openalex_rate_limit_status_redacts_api_key(self) -> None:
        fake_key = "test-openalex-key"
        with patch(
            "instsci.sources.openalex.request_with_retry",
            return_value=FakeResponse(
                {"rate_limit": {"daily_remaining_usd": 0.5, "credits_remaining": 9999}},
                headers={"X-RateLimit-Remaining": "99999"},
            ),
        ) as request:
            report = openalex.get_rate_limit_status(api_key=fake_key)

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["api_key"], fake_key)
        self.assertTrue(report["api_key_configured"])
        self.assertNotIn(fake_key, json.dumps(report))

    def test_openalex_rate_limit_status_understands_official_quota_shape(self) -> None:
        fake_key = "test-openalex-key"
        with patch(
            "instsci.sources.openalex.request_with_retry",
            return_value=FakeResponse(
                {
                    "rate_limit": {
                        "daily_budget_usd": 1,
                        "daily_used_usd": 1,
                        "daily_remaining_usd": 0.0,
                        "prepaid_remaining_usd": 0,
                        "resets_in_seconds": 123,
                        "credits_remaining": 0,
                    }
                }
            ),
        ):
            report = openalex.get_rate_limit_status(api_key=fake_key)

        self.assertEqual(report["status"], "quota_exhausted")
        self.assertEqual(report["body"]["rate_limit"]["daily_remaining_usd"], 0.0)
        self.assertEqual(report["body"]["rate_limit"]["resets_in_seconds"], 123)

    def test_openalex_rate_limit_status_reports_authentication_required(self) -> None:
        response = FakeResponse(
            {"error": "Authentication required"},
            status_code=401,
        )
        with patch.dict(os.environ, {"OPENALEX_API_KEY": ""}), patch(
            "instsci.sources.openalex.request_with_retry",
            return_value=response,
        ):
            report = openalex.get_rate_limit_status()

        self.assertEqual(report["status"], "authentication_required")
        self.assertFalse(report["api_key_configured"])

    def test_openalex_403_rate_limit_is_not_authentication_required(self) -> None:
        fake_key = "test-openalex-key"
        response = FakeResponse(
            {"error": "Rate limit exceeded"},
            status_code=403,
        )
        with patch("instsci.sources.openalex.request_with_retry", return_value=response):
            with self.assertRaises(ProviderSearchError) as error:
                openalex.search("topic", api_key=fake_key, raise_on_error=True)

        self.assertEqual(error.exception.status, "quota_exhausted")

    def test_openalex_error_detail_and_logs_redact_api_key_from_url(self) -> None:
        fake_key = "test-openalex-key"
        response = FakeResponse(
            None,
            text="",
            status_code=500,
            url=f"https://api.openalex.org/works?search=topic&api_key={fake_key}",
        )
        with patch("instsci.sources.openalex.request_with_retry", return_value=response):
            with self.assertLogs("instsci.sources.openalex", level="WARNING") as logs:
                with self.assertRaises(ProviderSearchError) as error:
                    openalex.search("topic", api_key=fake_key, raise_on_error=True)

        self.assertNotIn(fake_key, error.exception.detail)
        self.assertNotIn(fake_key, "\n".join(logs.output))
        self.assertIn("api_key=<redacted>", error.exception.detail)

    def test_cli_openalex_rate_limit_writes_redacted_report(self) -> None:
        runner = CliRunner()
        with patch(
            "instsci.sources.openalex.get_rate_limit_status",
            return_value={
                "provider": "openalex",
                "status": "success",
                "api_key_configured": True,
                "body": {"remaining": 99999},
            },
        ):
            with TemporaryDirectory() as tmp:
                output = os.path.join(tmp, "openalex_rate_limit.json")
                result = runner.invoke(app, ["openalex-rate-limit", "--output", output])
                self.assertEqual(result.exit_code, 0)
                with open(output, encoding="utf-8") as handle:
                    payload = json.loads(handle.read())

        self.assertTrue(payload["api_key_configured"])
        self.assertNotIn("test-openalex-key", json.dumps(payload))

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

    def test_crossref_exact_title_uses_title_query_parameter(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1000/EXACT",
                        "title": ["Exact title paper"],
                        "issued": {"date-parts": [[2024]]},
                    }
                ]
            }
        }
        with patch("instsci.sources.crossref.request_with_retry", return_value=FakeResponse(payload)) as request:
            results = crossref.search_exact_title("Exact title paper", limit=3, email="reader@example.edu")

        self.assertEqual(results[0].doi, "10.1000/EXACT")
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["query.title"], "Exact title paper")
        self.assertNotIn("query.bibliographic", params)

    def test_crossref_resolve_identifier_uses_work_endpoint(self) -> None:
        payload = {
            "message": {
                "DOI": "10.1000/RESOLVE",
                "title": ["Resolved DOI paper"],
                "issued": {"date-parts": [[2024]]},
                "container-title": ["Journal"],
            }
        }
        with patch("instsci.sources.crossref.request_with_retry", return_value=FakeResponse(payload)) as request:
            results = crossref.resolve_identifier("https://doi.org/10.1000/RESOLVE", email="reader@example.edu")

        self.assertEqual(results[0].doi, "10.1000/RESOLVE")
        self.assertEqual(results[0].title, "Resolved DOI paper")
        url = request.call_args.args[1]
        params = request.call_args.kwargs["params"]
        self.assertTrue(url.endswith("/10.1000%2Fresolve"))
        self.assertEqual(params["mailto"], "reader@example.edu")
        self.assertNotIn("query.bibliographic", params)
