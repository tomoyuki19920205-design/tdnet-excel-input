"""IPO PL regressions without checking official PDFs into Git.

Source: J-Quants DiscNo 20260928541337, TDnet PDF
https://www.release.tdnet.info/inbs/140120260928541337.pdf
The 2026 H1 consolidated statement reports 2,531 million yen of total
interim profit and 2,621 million yen attributable to owners.
"""
from types import SimpleNamespace

from src.db import StateDB
from src.fetcher import _fetch_via_jquants, _matches_watchlist_item, classify_disclosure
from src.ipo_listing_financials import (
    IpoFinancialPeriod, IpoListingExtraction, _extract_detailed_cumulative,
    _validate_summary_detail, build_canonical_rows, ensure_ipo_company,
)
from src.models import DisclosureItem, DisclosureType, Status


TITLE = "東京証券取引所スタンダード市場への上場に伴う当社決算情報等のお知らせ"
URL = "https://www.release.tdnet.info/inbs/140120260928541337.pdf"


def test_listing_title_and_alphanumeric_identity_bypass_stale_watchlist():
    item = DisclosureItem(
        disclosure_id="id", ticker="646A", company_name="クラサスケミカル",
        title=TITLE, doc_url=URL, published_at="2026-09-29 08:00",
        disclosure_type=classify_disclosure(TITLE),
    )
    assert item.disclosure_type == "ipo_listing_financials"
    assert _matches_watchlist_item(item, ["4004"])
    assert classify_disclosure("2026年3月期 第1四半期決算短信〔日本基準〕") == DisclosureType.FINANCIAL_STATEMENT


def test_jquants_disclosure_keeps_646a_and_classifies_listing_financials(monkeypatch):
    from src.jquants import adapter

    jq = SimpleNamespace(
        doc_url=URL, ticker="646A", company_name="クラサスケミカル",
        title=TITLE, published_at="2026-09-29 08:00", xbrl_url=None,
        disclosure_type="", disclosure_id="20260928541337",
    )
    monkeypatch.setattr(adapter, "fetch_jquants_disclosures", lambda *args, **kwargs: [jq])
    [item] = _fetch_via_jquants("20260929")
    assert (item.ticker, item.disclosure_type, item.source_doc_id) == (
        "646A", "ipo_listing_financials", "20260928541337",
    )


def test_consolidated_detail_uses_owners_profit_for_summary_and_pl():
    rows = [
        ("売上高", "132,009"), ("売上原価", "119,805"),
        ("売上総利益", "12,204"), ("販売費及び一般管理費", "8,640"),
        ("営業利益", "3,564"), ("経常利益", "4,134"),
        ("中間純利益", "2,531"),
        ("親会社株主に帰属する中間純利益", "2,621"),
    ]
    words = [{"text": "2026年12月期中間連結損益計算書", "x0": 10, "top": 0}]
    for index, (label, value) in enumerate(rows, 1):
        words.extend([
            {"text": label, "x0": 10, "top": index * 10},
            {"text": value, "x0": 300, "top": index * 10},
        ])
    page = SimpleNamespace(extract_words=lambda: words)
    detail = _extract_detailed_cumulative(
        SimpleNamespace(pages=[page]), expected_period="2026-12-31", expected_quarter="2Q",
    )
    assert detail is not None
    assert detail.metrics["sales"] == 132009
    assert detail.metrics["net_income"] == 2621
    summary = IpoFinancialPeriod(
        period="2026-12-31", quarter="2Q", kind="actual",
        metrics={"sales": 132009, "operating_profit": 3564,
                 "ordinary_profit": 4134, "net_income": 2621},
    )
    _validate_summary_detail(summary, detail)


def test_canonical_rows_separate_646a_actual_and_forecast():
    periods = (
        IpoFinancialPeriod("2025-12-31", "FY", "actual", {"sales": 303926, "net_income": 3096}),
        IpoFinancialPeriod("2026-12-31", "2Q", "actual", {"sales": 132009, "net_income": 2621},
                           period_start="2026-01-01", period_end="2026-06-30"),
        IpoFinancialPeriod("2026-12-31", "FY", "forecast", {"sales": 323400, "net_income": 1700}),
    )
    rows = build_canonical_rows(
        IpoListingExtraction("646A", "クラサスケミカル", periods),
        ticker="646a0", filing_id="20260928541337", disclosed_at="2026-09-29 08:00",
    )
    assert len(rows) == 6
    assert {row["ticker"] for row in rows} == {"646A"}
    assert {row["filing_id"] for row in rows} == {"20260928541337"}
    assert {(r["quarter"], r["source"], r["metric"], r["value"]) for r in rows if r["metric"] == "net_income"} == {
        ("FY", "official_pdf", "net_income", 3096),
        ("2Q", "official_pdf", "net_income", 2621),
        ("FY", "tdnet_forecast", "net_income", 1700),
    }


def test_ipo_failure_retries_without_changing_normal_earnings(tmp_path):
    db = StateDB(str(tmp_path / "state.db"))
    db.record("ipo", "646A", "", "MULTI", Status.PARSE_FAILED)
    db.record("normal", "4004", "", "", Status.PARSE_FAILED)
    assert not db.is_processed("ipo")
    assert db.is_processed("normal")
    db.record("ipo", "646A", "", "MULTI", Status.SUCCESS)
    assert db.is_processed("ipo")
    db.close()


def test_listing_day_master_insert_is_idempotent_and_non_destructive():
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): return None
        def json(self): return self.value

    class Session:
        def __init__(self): self.rows = {}; self.posts = []
        def get(self, url, *, params, headers, timeout):
            ticker = params["ticker_code"].removeprefix("eq.")
            return Response([self.rows[ticker]] if ticker in self.rows else [])
        def post(self, url, *, params, json, headers, timeout):
            assert params == {"on_conflict": "ticker_code"}
            assert "ignore-duplicates" in headers["Prefer"]
            self.posts.append(json)
            self.rows.setdefault(json["ticker_code"], json)
            return Response([json])

    session = Session()
    config = {"rest_url": "https://example.invalid/rest/v1", "headers": {}}
    assert ensure_ipo_company("646a0", "株式会社クラサスケミカル", config=config, session=session)["action"] == "inserted"
    assert ensure_ipo_company("646A", "別名", config=config, session=session)["action"] == "existing"
    assert session.posts == [{"ticker_code": "646A", "name_ja": "クラサスケミカル", "is_active": True}]
