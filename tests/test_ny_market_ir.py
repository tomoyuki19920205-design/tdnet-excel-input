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


def make_fetch(releases=RELEASES):
    def fetch(url):
        if url == PAGE_URL:
            return PAGE, url
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
