import unittest

from typer.testing import CliRunner

from instsci.cli import app
from instsci.publisher_profiles import get_publisher_profile, list_publisher_profiles


class FakeResponse:
    def __init__(self, url, status_code=200, text="", headers=None, history=None):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"content-type": "text/html"}
        self.history = history or []

    def close(self):
        return None


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        if url.startswith("https://doi.org/"):
            return FakeResponse(
                "https://ieeexplore.ieee.org/document/9876543/",
                text="<html><a href='/stampPDF/getPDF.jsp?tp=&isnumber=&arnumber=9876543'>PDF</a></html>",
            )
        return FakeResponse(
            url,
            status_code=200,
            headers={"content-type": "application/pdf"},
        )


class PublisherAccessCatalogTests(unittest.TestCase):
    def test_access_catalog_covers_every_publisher_profile(self):
        from instsci.publisher_access import load_publisher_access_catalog

        catalog = load_publisher_access_catalog()

        self.assertEqual(set(catalog["publishers"]), set(list_publisher_profiles()))
        for key, entry in catalog["publishers"].items():
            profile = get_publisher_profile(key)
            self.assertEqual(entry["profile_key"], key)
            self.assertEqual(entry["verification"]["sample_doi"], profile.sample_dois[0])
            self.assertTrue(entry["pdf_route_strategy"])
            self.assertTrue(entry["identity"]["closed_access_requires"])
            self.assertIn("browser_profile_dir", entry["persistence"]["stores"])
            self.assertTrue(entry["link_characteristics"])

    def test_browser_verification_matrix_is_project_asset(self):
        from instsci.publisher_access import load_publisher_browser_verification_matrix

        matrix = load_publisher_browser_verification_matrix()

        self.assertTrue(set(matrix["publishers"]).issubset(set(list_publisher_profiles())))
        self.assertIn("detailed local evidence is intentionally omitted", matrix["verdict_source"])
        self.assertIn("fresh visible browser evidence", matrix["scope"])
        self.assertIn("note", matrix["summary"])
        self.assertGreaterEqual(len(matrix["publishers"]), 1)
        self.assertNotIn("observed_pdf_candidates", matrix["publishers"]["elsevier"])
        self.assertNotIn("observed_pdf_candidates", matrix["publishers"]["iop"])
        self.assertIn("known_blocker", matrix["publishers"]["elsevier"])

    def test_institutional_identity_policy_records_webvpn_limits(self):
        from instsci.publisher_access import load_institutional_identity_policy

        policy = load_institutional_identity_policy()

        self.assertEqual(policy["default_mode"], "auto")
        self.assertNotEqual(policy["default_identity"], "webvpn")
        self.assertEqual(policy["preferred_off_campus_access"], "shibboleth_or_openathens")
        self.assertTrue(policy["subscription_institution"]["required_for_closed_access"])
        self.assertEqual(policy["subscription_institution"]["hardcoded_default"], "")
        self.assertEqual(
            policy["subscription_institution"]["resolution_order"][:4],
            [
                "--institution",
                "config.carsi_idp_name",
                "config.institution_name_en",
                "config.institution_name_zh",
            ],
        )
        self.assertEqual(policy["final_pdf_verdict_requires"], "visible_cloakbrowser")
        self.assertIn("publisher_broker", policy["identity_order"])
        self.assertIn("webvpn_broker", policy["identity_order"])
        self.assertLess(
            policy["login_method_order"].index("wayfless_federated_sso"),
            policy["login_method_order"].index("webvpn_broker"),
        )
        self.assertIn("standard_federated_sso", policy["federated_login_methods"])
        self.assertIn("wayfless_federated_sso", policy["federated_login_methods"])
        self.assertIn("save it in local config", " ".join(policy["routing_rules"]))

        publisher_broker = policy["identities"]["publisher_broker"]
        self.assertIn("same_live_cloakbrowser_context", publisher_broker["recommended_persistence"])
        self.assertIn("browser_profile_dir", publisher_broker["recommended_persistence"])
        self.assertEqual(publisher_broker["final_verdict_scope"], "browser verified")

        webvpn = policy["identities"]["webvpn"]
        self.assertFalse(webvpn["global_default"])
        self.assertEqual(webvpn["recommended_role"], "optional_identity_layer")
        self.assertIn("cookie_store", webvpn["persistence_limits"])
        self.assertEqual(
            webvpn["persistence_limits"]["cookie_store"]["verdict_scope"],
            "HTTP preflight only",
        )
        self.assertIn("tls_session", webvpn["non_exportable_state"])
        self.assertIn("browser_fingerprint", webvpn["non_exportable_state"])
        self.assertIn(
            "same_live_cloakbrowser_context",
            webvpn["recommended_persistence"],
        )

        self.assertNotIn("institutional_findings", policy)
        self.assertIn("intentionally omitted", policy["public_preview_note"])

    def test_identity_policy_command_exposes_webvpn_as_optional(self):
        runner = CliRunner()

        result = runner.invoke(app, ["identity-policy"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Default mode: auto", result.output)
        self.assertIn("Default identity: publisher_broker", result.output)
        self.assertIn("Subscription institution: required", result.output)
        self.assertIn("Preferred off-campus access: shibboleth_or_openathens", result.output)
        self.assertIn("WebVPN is optional", result.output)
        self.assertIn("visible_cloakbrowser", result.output)

    def test_verify_publisher_access_builds_pdf_candidates_from_catalog(self):
        from instsci.publisher_access import verify_publisher_access

        result = verify_publisher_access("ieee", session=FakeSession(), probe_pdf=True)

        self.assertEqual(result["profile_key"], "ieee")
        self.assertEqual(result["landing_status"], 200)
        self.assertIn("ieeexplore.ieee.org/document/9876543", result["landing_url"])
        self.assertEqual(
            result["pdf_candidates"][0],
            "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&isnumber=&arnumber=9876543",
        )
        self.assertEqual(result["candidate_probes"][0]["classification"], "pdf_accessible")


if __name__ == "__main__":
    unittest.main()


