import unittest
from urllib.parse import parse_qs, urlparse

from instsci.chinese_literature import (
    build_chinese_literature_search_url,
    chinese_literature_portal_report,
    chinese_literature_session_domains,
    get_chinese_literature_portal,
    infer_chinese_literature_portal,
    list_chinese_literature_portals,
)


class ChineseLiteraturePortalTests(unittest.TestCase):
    def test_catalog_includes_common_chinese_literature_portals(self):
        keys = {portal.key for portal in list_chinese_literature_portals()}

        self.assertIn("cnki", keys)
        self.assertIn("wanfang", keys)
        self.assertIn("cqvip", keys)
        self.assertIn("duxiu", keys)

    def test_infers_portal_from_key_alias_and_url(self):
        self.assertEqual(get_chinese_literature_portal("知网").key, "cnki")
        self.assertEqual(get_chinese_literature_portal("万方").key, "wanfang")
        self.assertEqual(infer_chinese_literature_portal("https://qikan.cqvip.com/article/detail?id=1").key, "cqvip")
        self.assertEqual(infer_chinese_literature_portal("https://www.wanfangdata.com.cn/details/detail.do").key, "wanfang")
        self.assertIsNone(infer_chinese_literature_portal("https://example.org/article"))

    def test_cnki_search_url_uses_stable_query_param(self):
        portal = get_chinese_literature_portal("cnki")
        url = build_chinese_literature_search_url(portal, "洪海沟 铀矿")

        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.hostname, "kns.cnki.net")
        self.assertEqual(params["kw"], ["洪海沟 铀矿"])

    def test_wanfang_and_cqvip_search_urls_use_verified_entry_points(self):
        wanfang_url = build_chinese_literature_search_url(get_chinese_literature_portal("wanfang"), "洪海沟")
        cqvip_url = build_chinese_literature_search_url(get_chinese_literature_portal("cqvip"), "洪海沟")

        wanfang = urlparse(wanfang_url)
        cqvip = urlparse(cqvip_url)

        self.assertEqual(wanfang.hostname, "s.wanfangdata.com.cn")
        self.assertEqual(parse_qs(wanfang.query)["q"], ["洪海沟"])
        self.assertEqual(cqvip.hostname, "www.cqvip.com")
        self.assertEqual(parse_qs(cqvip.query)["k"], ["洪海沟"])

    def test_wanfang_has_browser_verified_download_and_cqvip_is_manual_only(self):
        wanfang = get_chinese_literature_portal("wanfang")
        cqvip = get_chinese_literature_portal("cqvip")

        self.assertEqual(wanfang.capability, "browser_verified_search_download")
        self.assertEqual(cqvip.capability, "browser_verified_manual_broker_waf_blocked")
        self.assertEqual(wanfang.result_evidence, "browser_verified")
        self.assertEqual(cqvip.result_evidence, "browser_verified")
        self.assertEqual(wanfang.default_navigation_mode, "search")
        self.assertEqual(cqvip.default_navigation_mode, "search")
        self.assertIn("wanfang", wanfang.next_action)
        self.assertIn("skip_bulk_download", cqvip.next_action)
        self.assertTrue(any("qikan.cqvip.com" in note for note in cqvip.notes))
        self.assertTrue(any("entitlement" in note for note in cqvip.notes))

    def test_report_separates_download_verified_from_route_verified_portals(self):
        report = chinese_literature_portal_report()

        self.assertEqual(report["summary"]["download_verified_portals"], ["cnki", "wanfang"])
        self.assertIn("cnki", report["summary"]["route_verified_portals"])
        self.assertIn("wanfang", report["summary"]["route_verified_portals"])
        self.assertIn("cqvip", report["summary"]["route_verified_portals"])
        self.assertEqual(report["summary"]["capability_counts"]["browser_verified_search_first"], 1)
        self.assertEqual(report["summary"]["capability_counts"]["browser_verified_manual_broker_waf_blocked"], 1)

    def test_session_domains_cover_chinese_literature_portals(self):
        domains = chinese_literature_session_domains()

        self.assertIn("cnki.net", domains)
        self.assertIn("wanfangdata.com.cn", domains)
        self.assertIn("cqvip.com", domains)
        self.assertIn("duxiu.com", domains)


if __name__ == "__main__":
    unittest.main()
