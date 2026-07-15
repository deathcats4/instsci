"""Chinese literature portal routing catalog.

The catalog is deliberately conservative: it records safe entry points and
browser-route readiness without claiming unverified portals can download PDFs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


@dataclass(frozen=True)
class ChineseLiteraturePortal:
    """Route metadata for a Chinese literature database."""

    key: str
    label: str
    aliases: tuple[str, ...]
    hosts: tuple[str, ...]
    home_url: str
    search_entry_url: str
    search_query_param: str
    article_url_patterns: tuple[str, ...]
    pdf_controls: tuple[str, ...]
    verification_markers: tuple[str, ...]
    capability: str
    route_attempted: str
    default_navigation_mode: str
    result_evidence: str
    next_action: str
    notes: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return asdict(self)


CHINESE_LITERATURE_PORTALS: tuple[ChineseLiteraturePortal, ...] = (
    ChineseLiteraturePortal(
        key="cnki",
        label="CNKI / 中国知网",
        aliases=("cnki", "zh_cnki", "中国知网", "知网"),
        hosts=("cnki.net", "cnki.com.cn"),
        home_url="https://www.cnki.net/",
        search_entry_url="https://kns.cnki.net/kns8s/defaultresult/index",
        search_query_param="kw",
        article_url_patterns=("kcms", "detail", "filename=", "dbcode="),
        pdf_controls=("PDF下载",),
        verification_markers=("请完成安全验证", "请依次点击", "人机验证"),
        capability="browser_verified_search_first",
        route_attempted="persistent_cloakbrowser_search_pdf_button",
        default_navigation_mode="search",
        result_evidence="browser_verified",
        next_action="use_cnki_batch_search_mode",
        notes=(
            "Validated through visible CloakBrowser with homepage/search-result navigation.",
            "Direct detail-page navigation remains available only as a fallback.",
        ),
    ),
    ChineseLiteraturePortal(
        key="wanfang",
        label="Wanfang Data / 万方数据",
        aliases=("wanfang", "wanfangdata", "万方", "万方数据"),
        hosts=("wanfangdata.com.cn", "wanfangdata.com"),
        home_url="https://www.wanfangdata.com.cn/",
        search_entry_url="https://s.wanfangdata.com.cn/paper",
        search_query_param="q",
        article_url_patterns=("paper", "periodical", "thesis", "conference", "detail"),
        pdf_controls=("下载", "在线阅读", "全文快报"),
        verification_markers=("安全验证", "验证码", "人机验证"),
        capability="browser_verified_search_download",
        route_attempted="visible_cloakbrowser_search_download_popup_pdf",
        default_navigation_mode="search",
        result_evidence="browser_verified",
        next_action="use_wanfang_batch_search_download_flow",
        notes=(
            "Validated through visible CloakBrowser with search-result navigation.",
            "The PDF is delivered from the Fulltext/Download popup after clicking the result-row download control.",
        ),
    ),
    ChineseLiteraturePortal(
        key="cqvip",
        label="CQVIP / 维普中文科技期刊",
        aliases=("cqvip", "vip", "维普", "中文科技期刊", "qikan.cqvip"),
        hosts=("cqvip.com", "cqvip.com.cn", "qikan.cqvip.com"),
        home_url="https://www.cqvip.com/",
        search_entry_url="https://www.cqvip.com/search",
        search_query_param="k",
        article_url_patterns=("doc", "journal", "article", "detail", "search"),
        pdf_controls=("PDF下载", "智能阅读"),
        verification_markers=("安全验证", "验证码", "人机验证"),
        capability="browser_verified_manual_broker_waf_blocked",
        route_attempted="visible_cloakbrowser_search_pdf_button_ip_login_qikan",
        default_navigation_mode="search",
        result_evidence="browser_verified",
        next_action="skip_bulk_download_unless_entitlement_and_qikan_load_are_confirmed",
        notes=(
            "Visible browser probe reached a www.cqvip.com article page and the PDF下载 control.",
            "Manual IP登录 can redirect to qikan.cqvip.com, but qikan rendered blank in CloakBrowser and HTTP preflight showed CQVIP cache-server 403/412 challenge responses.",
            "The tested institution resource portal did not visibly list CQVIP; treat entitlement as unconfirmed and do not include CQVIP in default Chinese literature batch downloads.",
        ),
    ),
    ChineseLiteraturePortal(
        key="sinomed",
        label="SinoMed / 中国生物医学文献服务系统",
        aliases=("sinomed", "sino-med", "中国生物医学文献服务系统", "中国生物医学文献"),
        hosts=("sinomed.ac.cn",),
        home_url="https://www.sinomed.ac.cn/",
        search_entry_url="https://www.sinomed.ac.cn/",
        search_query_param="",
        article_url_patterns=("search", "detail", "article"),
        pdf_controls=("全文", "下载", "PDF"),
        verification_markers=("安全验证", "验证码", "人机验证"),
        capability="planned_search_first",
        route_attempted="planned_cloakbrowser_search_pdf_button",
        default_navigation_mode="search",
        result_evidence="not_verified",
        next_action="add_visible_browser_adapter_before_batch_use",
        notes=(
            "Useful for biomedical Chinese-language records; access mode varies by institution.",
            "Keep any institution checks in the visible browser.",
        ),
    ),
    ChineseLiteraturePortal(
        key="duxiu",
        label="Duxiu/Chaoxing / 读秀超星",
        aliases=("duxiu", "chaoxing", "读秀", "超星"),
        hosts=("duxiu.com", "chaoxing.com", "sslibrary.com"),
        home_url="https://www.duxiu.com/",
        search_entry_url="https://www.duxiu.com/",
        search_query_param="",
        article_url_patterns=("book", "detail", "search"),
        pdf_controls=("下载", "阅读", "全文"),
        verification_markers=("安全验证", "验证码", "人机验证"),
        capability="planned_manual_broker",
        route_attempted="planned_visible_browser_manual_broker",
        default_navigation_mode="search",
        result_evidence="not_verified",
        next_action="add_manual_broker_adapter_before_batch_use",
        notes=(
            "Often requires institution-specific reader or delivery flows.",
            "Do not automate credential, OTP, or human-verification steps.",
        ),
    ),
)

_PORTALS_BY_ALIAS = {
    alias.lower(): portal
    for portal in CHINESE_LITERATURE_PORTALS
    for alias in (portal.key, *portal.aliases)
}


def list_chinese_literature_portals() -> tuple[ChineseLiteraturePortal, ...]:
    """Return supported and planned Chinese literature portal profiles."""
    return CHINESE_LITERATURE_PORTALS


def get_chinese_literature_portal(key: str) -> ChineseLiteraturePortal:
    """Return a portal profile by key or alias."""
    normalized = str(key or "").strip().lower()
    try:
        return _PORTALS_BY_ALIAS[normalized]
    except KeyError as exc:
        known = ", ".join(portal.key for portal in CHINESE_LITERATURE_PORTALS)
        raise ValueError(f"Unknown Chinese literature portal '{key}'. Known portals: {known}") from exc


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    hostname = host.lower().lstrip(".")
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes)


def infer_chinese_literature_portal(value: str) -> ChineseLiteraturePortal | None:
    """Infer a portal from a URL host or a profile key/alias."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return get_chinese_literature_portal(text)
    except ValueError:
        pass
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or parsed.netloc or "").lower()
    if not host:
        return None
    for portal in CHINESE_LITERATURE_PORTALS:
        if _host_matches(host, portal.hosts):
            return portal
    return None


def chinese_literature_session_domains() -> tuple[str, ...]:
    """Return portal domains useful for browser-profile session diagnostics."""
    domains: list[str] = []
    for portal in CHINESE_LITERATURE_PORTALS:
        domains.extend(portal.hosts)
    return tuple(dict.fromkeys(domains))


def build_chinese_literature_search_url(portal: ChineseLiteraturePortal, query: str) -> str:
    """Build a conservative search URL when the portal has a stable query param."""
    base_url = portal.search_entry_url or portal.home_url
    if not portal.search_query_param:
        return base_url
    parsed = urlparse(base_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params[portal.search_query_param] = str(query or "").strip()
    return urlunparse(parsed._replace(query=urlencode(params)))


def chinese_literature_portal_report() -> dict[str, object]:
    """Return a public, secret-free report of Chinese literature portal support."""
    portals = [portal.to_json() for portal in CHINESE_LITERATURE_PORTALS]
    capability_counts: dict[str, int] = {}
    for portal in CHINESE_LITERATURE_PORTALS:
        capability_counts[portal.capability] = capability_counts.get(portal.capability, 0) + 1
    route_verified = [
        portal.key
        for portal in CHINESE_LITERATURE_PORTALS
        if str(portal.capability).startswith("browser_verified")
    ]
    download_verified_capabilities = {
        "browser_verified_search_first",
        "browser_verified_search_download",
    }
    download_verified = [
        portal.key
        for portal in CHINESE_LITERATURE_PORTALS
        if portal.capability in download_verified_capabilities
    ]
    return {
        "schema": "instsci.chinese_literature_portals.v1",
        "summary": {
            "portals": len(portals),
            "capability_counts": capability_counts,
            "route_verified_portals": route_verified,
            "download_verified_portals": download_verified,
            "verified_portals": download_verified,
        },
        "portals": portals,
    }
