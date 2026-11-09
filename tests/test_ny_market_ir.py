import hashlib
import json

import pytest

from lib.ny_market_ir import IRFeedError, fetch_q4_ir_news


PAGE_URL = "https://investors.example.test/investor-news/default.aspx"
SCRIPT_URL = "https://investors.example.test/js/evergreen.q4Api.min.js"
DETAIL_URL = "https://investors.example.test/investor-news/results.aspx"
CATEGORY = "00000000-0000-0000-0000-000000000000"
PAGE = (
    '<title>Block, Inc. (XYZ) Investor Relations</title>'
    '<script src="/js/evergreen.q4Api.min.js"></script>'
    f"<script>$('.news').q4News({{category: '{CATEGORY}'}});</script>"
).encode()
SCRIPT = b'itemsUrl:["/Services/PressReleaseService.svc/GetPressReleaseList","/feed/PressRelease.svc/GetPressReleaseList"]'
RELEASES = [
    {"PressReleaseDate": "11/03/2026 16:05:00", "Headline": "Block Announces Results",
     "LinkToDetailPage": "/investor-news/results.aspx", "PressReleaseId": 1834},
    {"PressReleaseDate": "10/26/2026 09:30:00", "Headline": "Earlier Item",
     "LinkToDetailPage": "/investor-news/earlier.aspx", "PressReleaseId": 1833},
]


def make_fetch(releases=RELEASES, page=PAGE):
    def fetch(url):
        if url == PAGE_URL:
            return page, url
        if url == SCRIPT_URL:
            return SCRIPT, url
        if url.startswith("https://investors.example.test/feed/PressRelease.svc/GetPressReleaseList?"):
            assert "year=2026" in url
            assert "pageSize=-1" in url
            return json.dumps({"GetPressReleaseListResult": releases}).encode(), url
        if url == DETAIL_URL:
            return b"<title>Block Announces Results</title>", url
        raise AssertionError(url)
    return fetch


def test_q4_feed_captures_target_day_release_and_source_hashes(tmp_path):
    result = fetch_q4_ir_news(PAGE_URL, "Block, Inc.", "XYZ", "2026-11-03", tmp_path,
                              prefix="test", fetch=make_fetch())
    assert result["year_record_count"] == 2
    assert result["target_day_count"] == 1
    assert result["target_day_releases"][0]["detail_url"] == DETAIL_URL
    for key in ("page_raw", "widget_script_raw", "feed_raw"):
        item = result[key]
        assert hashlib.sha256((tmp_path / item["file"]).read_bytes()).hexdigest() == item["sha256"]
    assert json.loads((tmp_path / "test-summary.json").read_text())["target_day_count"] == 1


def test_q4_feed_rejects_wrong_issuer_before_publication(tmp_path):
    with pytest.raises(IRFeedError, match="issuer identity"):
        fetch_q4_ir_news(PAGE_URL, "Other Company", "XYZ", "2026-11-03", tmp_path,
                         fetch=make_fetch())
    assert not list(tmp_path.iterdir())


def test_q4_feed_rejects_empty_or_wrong_year_result(tmp_path):
    with pytest.raises(IRFeedError, match="no year records"):
        fetch_q4_ir_news(PAGE_URL, "Block, Inc.", "XYZ", "2026-11-03", tmp_path,
                         fetch=make_fetch([]))
    stale = [{**RELEASES[0], "PressReleaseDate": "11/03/2025 16:05:00"}]
    with pytest.raises(IRFeedError, match="ignored the requested year"):
        fetch_q4_ir_news(PAGE_URL, "Block, Inc.", "XYZ", "2026-11-03", tmp_path,
                         fetch=make_fetch(stale))


def test_q4_brand_title_with_corporate_footer_and_stock_link(tmp_path):
    page = (
        '<title>Investor Relations | Airbnb | Press Releases</title>'
        '<script src="/js/evergreen.q4Api.min.js"></script>'
        f"<script>$('.news').q4News({{category: '{CATEGORY}'}});</script>"
        '<a href="https://quotes.example.test/?Ticker=ABNB&amp;Exchange=NASDAQ">Stock</a>'
        '<footer>© Airbnb, Inc.</footer>'
    ).encode()
    result = fetch_q4_ir_news(PAGE_URL, "Airbnb, Inc.", "ABNB", "2026-11-05", tmp_path,
                              prefix="brand", fetch=make_fetch(page=page))
    assert result["official_page_identity_evidence"] == "visible_corporate_name_and_stock_link"
    assert result["target_day_count"] == 0
    assert hashlib.sha256((tmp_path / result["page_raw"]["file"]).read_bytes()).hexdigest() == result["page_raw"]["sha256"]


@pytest.mark.parametrize("corporate,ticker,link", [
    ("Other, Inc.", "ABNB", "Ticker=ABNB&amp;Exchange=NASDAQ"),
    ("Airbnb, Inc.", "OTHER", "Ticker=ABNB&amp;Exchange=NASDAQ"),
    ("Airbnb, Inc.", "ABNB", "Ticker=ABNB"),
])
def test_q4_brand_page_rejects_unverified_identity(tmp_path, corporate, ticker, link):
    page = (
        '<title>Investor Relations | Airbnb | Press Releases</title>'
        '<script src="/js/evergreen.q4Api.min.js"></script>'
        f"<script>$('.news').q4News({{category: '{CATEGORY}'}});</script>"
        f'<a href="https://quotes.example.test/?{link}">Stock</a>'
        '<footer>© Airbnb, Inc.</footer>'
    ).encode()
    with pytest.raises(IRFeedError, match="identity"):
        fetch_q4_ir_news(PAGE_URL, corporate, ticker, "2026-11-05", tmp_path,
                         fetch=make_fetch(page=page))
    assert not list(tmp_path.iterdir())


def test_q4_stock_widget_verifies_official_issuer_and_listing(tmp_path):
    page = (
        '<title>Micron Technology, Inc. - Investor Relations</title>'
        '<script src="/js/evergreen.q4Api.min.js"></script>'
        f"<script>$('.news').q4News({{category: '{CATEGORY}'}});</script>"
        '<div>Micron Technology, Inc.</div>'
        '<script>$(".stock").q4StockSummary({tickerList: '
        '[{"StockSymbol":"MU","StockExchange":"XNAS"}]});</script>'
    ).encode()
    result = fetch_q4_ir_news(PAGE_URL, "Micron Technology, Inc.", "MU", "2026-11-03", tmp_path,
                              prefix="widget", fetch=make_fetch(page=page))
    assert result["official_page_identity_evidence"] == "visible_corporate_name_and_official_q4_stock_widget"
    assert result["target_day_count"] == 1


@pytest.mark.parametrize("issuer,ticker,stock_symbol,exchange", [
    ("Other Inc.", "MU", "MU", "XNAS"),
    ("Micron Technology, Inc.", "MUS", "MU", "XNAS"),
    ("Micron Technology, Inc.", "MU", "MU", "UNKNOWN"),
])
def test_q4_stock_widget_rejects_identity_mismatch(tmp_path, issuer, ticker, stock_symbol, exchange):
    page = (
        '<title>Micron Technology, Inc. - Investor Relations</title>'
        '<script src="/js/evergreen.q4Api.min.js"></script>'
        f"<script>$('.news').q4News({{category: '{CATEGORY}'}});</script>"
        '<div>Micron Technology, Inc.</div>'
        f'<script>$(".stock").q4StockSummary({{tickerList: [{{"StockSymbol":"{stock_symbol}",'
        f'"StockExchange":"{exchange}"}}]}});</script>'
    ).encode()
    with pytest.raises(IRFeedError, match="identity"):
        fetch_q4_ir_news(PAGE_URL, issuer, ticker, "2026-11-03", tmp_path,
                         fetch=make_fetch(page=page))
    assert not list(tmp_path.iterdir())


def test_q4_companion_official_release_verifies_brand_only_page(tmp_path):
    page = (
        '<title>Serina Therapeutics - News</title>'
        '<script src="/js/evergreen.q4Api.min.js"></script>'
        f"<script>$('.news').q4News({{category: '{CATEGORY}'}});</script>"
        '<footer>Serina Therapeutics, Inc.</footer>'
    ).encode()
    identity_url = "https://investors.example.test/news/news-details/2026/older/default.aspx"
    release = b"<article>Serina Therapeutics, Inc. (NYSE American: SER) reported prior results.</article>"
    releases = [{"PressReleaseDate": "10/26/2026 09:30:00", "Headline": "Prior Serina release",
                 "LinkToDetailPage": "/news/news-details/2026/older/default.aspx"}]
    base_fetch = make_fetch(releases, page)

    def fetch(url):
        return (release, url) if url == identity_url else base_fetch(url)

    result = fetch_q4_ir_news(PAGE_URL, "Serina Therapeutics, Inc.", "SER", "2026-11-06", tmp_path,
                              prefix="ser", identity_url=identity_url, fetch=fetch)
    assert result["official_page_identity_evidence"] == "same_origin_official_release_name_and_exchange_ticker"
    assert result["official_identity_page_url"] == identity_url
    assert hashlib.sha256((tmp_path / result["official_identity_page_raw"]["file"]).read_bytes()).hexdigest() == result["official_identity_page_raw"]["sha256"]
    assert result["target_day_count"] == 0


@pytest.mark.parametrize("identity_url,release", [
    ("https://other.example.test/issuer.html", b"Serina Therapeutics, Inc. (NYSE American: SER)"),
    ("https://investors.example.test/issuer.html", b"Serina Therapeutics, Inc. (NYSE American: OTHER)"),
    ("https://investors.example.test/issuer.html", b"Other Therapeutics, Inc. (NYSE American: SER)"),
])
def test_q4_companion_identity_rejects_wrong_origin_or_issuer(tmp_path, identity_url, release):
    page = (
        '<title>Serina Therapeutics - News</title>'
        '<script src="/js/evergreen.q4Api.min.js"></script>'
        f"<script>$('.news').q4News({{category: '{CATEGORY}'}});</script>"
        '<footer>Serina Therapeutics, Inc.</footer>'
    ).encode()
    base_fetch = make_fetch(page=page)

    def fetch(url):
        return (release, url) if url == identity_url else base_fetch(url)

    with pytest.raises(IRFeedError, match="identity"):
        fetch_q4_ir_news(PAGE_URL, "Serina Therapeutics, Inc.", "SER", "2026-11-06", tmp_path,
                         identity_url=identity_url, fetch=fetch)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("about,quote,accepted", [
    ("About Unity Software Inc. (Unity)", "Unity [NYSE: U]", True),
    ("About Unity Software Inc. (Unity)", "Other [NYSE: U]", False),
    ("About Unity Software Inc. (Unity)", "Unity [NYSE: OTHER]", False),
    ("About Other Software Inc. (Unity)", "Unity [NYSE: U]", False),
])
def test_q4_companion_about_alias_requires_matching_issuer_alias_and_ticker(tmp_path, about, quote, accepted):
    page = (
        '<title>Unity Technologies - News</title>'
        '<script src="/js/evergreen.q4Api.min.js"></script>'
        f"<script>$('.news').q4News({{category: '{CATEGORY}'}});</script>"
    ).encode()
    identity_url = "https://investors.example.test/news/news-details/2026/older/default.aspx"
    release = f"<article>{about} {quote} provides tools for creators.</article>".encode()
    releases = [{"PressReleaseDate": "10/26/2026 09:30:00", "Headline": "Prior Unity release",
                 "LinkToDetailPage": "/news/news-details/2026/older/default.aspx"}]
    base_fetch = make_fetch(releases, page)

    def fetch(url):
        return (release, url) if url == identity_url else base_fetch(url)

    if accepted:
        result = fetch_q4_ir_news(PAGE_URL, "Unity Software Inc.", "U", "2026-11-06", tmp_path,
                                  identity_url=identity_url, fetch=fetch)
        assert result["official_page_identity_evidence"] == "same_origin_official_release_about_alias_and_exchange_ticker"
    else:
        with pytest.raises(IRFeedError, match="identity"):
            fetch_q4_ir_news(PAGE_URL, "Unity Software Inc.", "U", "2026-11-06", tmp_path,
                             identity_url=identity_url, fetch=fetch)
        assert not list(tmp_path.iterdir())
