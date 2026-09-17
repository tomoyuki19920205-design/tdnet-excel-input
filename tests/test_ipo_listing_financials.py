from pathlib import Path
from types import SimpleNamespace

import pytest

from src.fetcher import _fetch_via_jquants, _matches_watchlist_item, classify_disclosure
from src.models import DisclosureItem
from src.ipo_listing_financials import (
    IPO_LISTING_FINANCIALS,
    build_canonical_rows,
    build_ipo_notification_event,
    disclosure_identity,
    display_company_name,
    extract_ipo_listing_financials,
    is_ipo_listing_financial_title,
    normalize_disclosure_title,
    write_ipo_financials,
)
from src.models import DisclosureType
from src.events.tdnet_event_store import build_dedupe_key


FIXTURE = Path(__file__).parent / "fixtures" / "ipo_listing" / "140120260916537319.pdf"
URL = "https://www.release.tdnet.info/inbs/140120260916537319.pdf"
TITLE = "東京証券取引所グロース市場への上場に伴う当社決算情報等のお知らせ"


@pytest.fixture(scope="module")
def extraction():
    pytest.importorskip("pdfplumber")
    return extract_ipo_listing_financials(FIXTURE)


@pytest.mark.parametrize(
    "title",
    [
        TITLE,
        "東京証券取引所プライム市場への上場に伴う決算情報等のお知らせ",
        "東京証券取引所 スタンダード市場への上場に伴う 当社 決算情報等のお知らせ",
        "東京証券取引所\nグロース市場への上場に伴う当社決算情報等のお知らせ",
        "東京証券取引所　グロース市場への上場に伴う当社決算情報等のお知らせ",
    ],
)
def test_title_variants_are_classified(title):
    assert is_ipo_listing_financial_title(title)
    assert classify_disclosure(title) == IPO_LISTING_FINANCIALS


@pytest.mark.parametrize(
    "title",
    [
        "東京証券取引所グロース市場への上場のお知らせ",
        "新規上場承認に関するお知らせ",
        "決算情報等のお知らせ",
    ],
)
def test_plain_listing_or_non_listing_financial_title_is_not_classified(title):
    assert not is_ipo_listing_financial_title(title)


def test_title_normalization_absorbs_width_whitespace_and_line_breaks():
    assert normalize_disclosure_title(" 上場に伴う\n当社　決算情報等のお知らせ ") == (
        "上場に伴う当社決算情報等のお知らせ"
    )


def _period(extraction, period, quarter, kind):
    return next(
        row
        for row in extraction.periods
        if (row.period, row.quarter, row.kind) == (period, quarter, kind)
    )


def test_skyfall_identity_uses_alphanumeric_ticker_without_master(extraction):
    assert extraction.ticker == "625A"
    assert extraction.company_name == "Skyfall"


def test_ipo_metadata_bypasses_stale_watchlist():
    item = DisclosureItem(
        disclosure_id="id",
        ticker="625A",
        company_name="Skyfall",
        title=TITLE,
        doc_url=URL,
        published_at="2026-09-17 08:00",
        disclosure_type=IPO_LISTING_FINANCIALS,
    )
    assert _matches_watchlist_item(item, ["7203"])


def test_jquants_primary_uses_title_for_ipo_classification(monkeypatch):
    from src.jquants import adapter

    jq = SimpleNamespace(
        doc_url=URL,
        ticker="625A",
        company_name="Skyfall",
        title=TITLE,
        published_at="2026-09-17 08:00",
        xbrl_url=None,
        disclosure_type="",
        disclosure_id="140120260916537319",
        disc_items=(),
    )
    monkeypatch.setattr(adapter, "fetch_jquants_disclosures", lambda *a, **k: [jq])
    [item] = _fetch_via_jquants("20260917")
    assert item.disclosure_type == IPO_LISTING_FINANCIALS
    assert item.ticker == "625A"
    assert item.source_doc_id == "140120260916537319"


def test_skyfall_prior_fy_actual(extraction):
    actual = _period(extraction, "2025-09-30", "FY", "actual")
    assert actual.metrics == {
        "sales": 16215.0,
        "operating_profit": 515.0,
        "ordinary_profit": 553.0,
        "net_income": 446.0,
        "eps": 71.95,
    }


def test_skyfall_third_quarter_is_cumulative_with_exact_dates(extraction):
    q3 = _period(extraction, "2026-09-30", "3Q", "actual")
    assert q3.period_start == "2025-10-01"
    assert q3.period_end == "2026-06-30"
    assert q3.source_unit == "千円"
    assert q3.source_page == 12
    assert q3.metrics["sales"] == pytest.approx(14059.331)
    assert q3.metrics["operating_profit"] == pytest.approx(1013.201)
    assert q3.metrics["ordinary_profit"] == pytest.approx(1065.988)
    assert q3.metrics["net_income"] == pytest.approx(796.731)
    assert q3.metrics["eps"] == pytest.approx(128.35)


def test_detailed_thousand_yen_values_are_converted_before_storage(extraction):
    q3 = _period(extraction, "2026-09-30", "3Q", "actual")
    assert q3.metrics["cost_of_sales"] == pytest.approx(11696.197)
    assert q3.metrics["gross_profit"] == pytest.approx(2363.133)
    assert q3.metrics["sga"] == pytest.approx(1349.932)
    assert q3.metrics["non_operating_income"] == pytest.approx(69.030)
    assert q3.metrics["non_operating_expenses"] == pytest.approx(16.244)
    assert q3.metrics["profit_before_tax"] == pytest.approx(1065.988)
    assert q3.metrics["income_taxes"] == pytest.approx(269.256)


def test_skyfall_full_year_forecast_is_separate_from_actual(extraction):
    forecast = _period(extraction, "2026-09-30", "FY", "forecast")
    assert forecast.metrics == {
        "sales": 18497.0,
        "operating_profit": 1144.0,
        "ordinary_profit": 1176.0,
        "net_income": 807.0,
        "eps": 130.0,
    }


def test_pdf_tanshin_appendix_does_not_create_duplicate_periods(extraction):
    identities = [(row.period, row.quarter, row.kind) for row in extraction.periods]
    assert len(identities) == len(set(identities)) == 3
    assert ("2026-09-30", "3Q", "actual") in identities
    assert ("2026-09-30", "FY", "forecast") in identities
    assert ("2026-09-30", "3Q", "forecast") not in identities


def test_canonical_rows_keep_625a_and_split_actual_forecast_sources(extraction):
    rows = build_canonical_rows(
        extraction,
        ticker="625a",
        filing_id="140120260916537319",
        disclosed_at="2026-09-17 08:00",
    )
    assert {row["ticker"] for row in rows} == {"625A"}
    assert {
        (row["period"], row["quarter"], row["source"])
        for row in rows
        if row["metric"] == "sales"
    } == {
        ("2025-09-30", "FY", "official_pdf"),
        ("2026-09-30", "3Q", "official_pdf"),
        ("2026-09-30", "FY", "tdnet_forecast"),
    }
    q3_gross = next(
        row
        for row in rows
        if row["quarter"] == "3Q" and row["metric"] == "gross_profit"
    )
    assert q3_gross["value"] == pytest.approx(2363.133)
    assert q3_gross["period_start"] == "2025-10-01"
    assert q3_gross["period_end"] == "2026-06-30"


def test_repeated_upsert_has_stable_keys_and_row_count(extraction):
    stored = {}

    def fake_upsert(table, rows, **kwargs):
        assert table == "canonical_financials"
        assert kwargs["on_conflict"] == "source_row_key"
        for row in rows:
            stored[row["source_row_key"]] = row
        return {"ok": True, "count": len(rows), "status": 201, "error": None}

    kwargs = dict(
        ticker="625A",
        filing_id="140120260916537319",
        disclosed_at="2026-09-17 08:00",
        config={"test": True},
        upsert=fake_upsert,
    )
    first = write_ipo_financials(extraction, **kwargs)
    first_count = len(stored)
    second = write_ipo_financials(extraction, **kwargs)
    assert first["rows"] == second["rows"] == first_count
    assert len(stored) == first_count


def test_notification_card_title_link_and_dedupe_identity():
    item = SimpleNamespace(
        ticker="625a",
        company_name="株式会社Skyfall",
        title=TITLE,
        doc_url=URL,
        published_at="2026-09-17 08:00",
        source_doc_id=None,
    )
    event = build_ipo_notification_event(item)
    assert event.title == "新規上場 625A Skyfall"
    assert event.doc_url == URL
    assert event.summary_text == ""
    assert event.source_doc_id == "140120260916537319"
    assert build_dedupe_key(event) == build_dedupe_key(build_ipo_notification_event(item))


def test_disclosure_identity_prefers_provider_id_then_official_url_id():
    assert disclosure_identity(URL) == "140120260916537319"
    assert disclosure_identity(URL, "provider-123") == "provider-123"


def test_existing_tanshin_classification_regression():
    assert classify_disclosure("2026年3月期 第1四半期決算短信〔日本基準〕") == (
        DisclosureType.FINANCIAL_STATEMENT
    )


@pytest.mark.parametrize(
    ("doc_id", "ticker", "company", "period", "quarter", "sales", "net_income"),
    [
        ("140120260804508284", "614A", "山八商事株式会社", "2026-08-31", "2Q", 3188.448, 268.217),
        ("140120260821524549", "616A", "数理技研", "2026-09-30", "2Q", 530.146, 14.409),
        ("140120260827527377", "615A", "インターパーク", "2026-08-31", "2Q", 407.563, 5.230),
        ("140120260910534302", "618A", "KOMPEITO", "2026-08-31", "3Q", 7047.954, 554.959),
        ("140120260915536762", "619A", "オリバー", "2026-12-31", "2Q", 18645.0, 1444.0),
        ("140120260910533876", "621A", "オーディオストック", "2026-09-30", "3Q", 1310.617, 344.755),
    ],
)
def test_real_monthly_ipo_variants_extract_cumulative_detail(
    doc_id, ticker, company, period, quarter, sales, net_income,
):
    pytest.importorskip("pdfplumber")
    result = extract_ipo_listing_financials(FIXTURE.parent / f"{doc_id}.pdf")
    assert (result.ticker, result.company_name) == (ticker, company)
    cumulative = _period(result, period, quarter, "actual")
    assert cumulative.metrics["sales"] == pytest.approx(sales)
    assert cumulative.metrics["net_income"] == pytest.approx(net_income)
    assert cumulative.period_start is not None
    assert cumulative.period_end is not None


def test_thousand_yen_summary_values_are_converted_but_eps_is_not():
    pytest.importorskip("pdfplumber")
    result = extract_ipo_listing_financials(FIXTURE.parent / "140120260827527377.pdf")
    forecast = _period(result, "2026-08-31", "FY", "forecast")
    prior = _period(result, "2025-08-31", "FY", "actual")
    assert forecast.source_unit == "千円"
    assert forecast.metrics["sales"] == pytest.approx(823.463)
    assert forecast.metrics["eps"] == pytest.approx(9.17)
    assert prior.metrics["sales"] == pytest.approx(745.117)


def test_negative_yen_sen_eps_keeps_sign_on_fraction():
    pytest.importorskip("pdfplumber")
    result = extract_ipo_listing_financials(FIXTURE.parent / "140120260910534302.pdf")
    prior = _period(result, "2025-08-31", "FY", "actual")
    assert prior.metrics["eps"] == pytest.approx(-16.35)


def test_ifrs_midyear_uses_pbt_and_current_comparative_dates():
    pytest.importorskip("pdfplumber")
    result = extract_ipo_listing_financials(FIXTURE.parent / "140120260915536762.pdf")
    cumulative = _period(result, "2026-12-31", "2Q", "actual")
    assert cumulative.period_start == "2026-01-01"
    assert cumulative.period_end == "2026-06-30"
    assert cumulative.metrics["profit_before_tax"] == pytest.approx(2081.0)
    assert cumulative.metrics["non_operating_income"] == pytest.approx(17.0)
    assert cumulative.metrics["non_operating_expenses"] == pytest.approx(41.0)
    assert "ordinary_profit" not in cumulative.metrics


def test_midyear_detail_keeps_unrounded_profit_before_tax():
    pytest.importorskip("pdfplumber")
    result = extract_ipo_listing_financials(FIXTURE.parent / "140120260821524549.pdf")
    cumulative = _period(result, "2026-09-30", "2Q", "actual")
    assert cumulative.metrics["profit_before_tax"] == pytest.approx(22.664)


def test_full_year_only_listing_notice_saves_two_periods_without_inventing_cumulative():
    pytest.importorskip("pdfplumber")
    result = extract_ipo_listing_financials(FIXTURE.parent / "140120260825525642.pdf")
    assert result.ticker == "617A"
    assert {(p.period, p.quarter, p.kind) for p in result.periods} == {
        ("2026-06-30", "FY", "actual"),
        ("2027-06-30", "FY", "forecast"),
    }

    actual = _period(result, "2026-06-30", "FY", "actual")
    assert actual.period_start == "2025-07-01"
    assert actual.period_end == "2026-06-30"
    assert actual.source_unit == "千円"
    assert actual.metrics == {
        "sales": pytest.approx(18809.167),
        "operating_profit": pytest.approx(562.897),
        "ordinary_profit": pytest.approx(287.140),
        "net_income": pytest.approx(174.212),
        "eps": pytest.approx(316.75),
        "cost_of_sales": pytest.approx(15939.329),
        "gross_profit": pytest.approx(2869.837),
        "sga": pytest.approx(2306.939),
        "non_operating_income": pytest.approx(3.909),
        "non_operating_expenses": pytest.approx(279.667),
        "profit_before_tax": pytest.approx(267.140),
        "income_taxes": pytest.approx(92.928),
    }

    forecast = _period(result, "2027-06-30", "FY", "forecast")
    assert forecast.source_unit == "百万円"
    assert forecast.metrics == {
        "sales": pytest.approx(19463.0),
        "operating_profit": pytest.approx(818.0),
        "ordinary_profit": pytest.approx(588.0),
        "net_income": pytest.approx(366.0),
        "eps": pytest.approx(666.74),
        "cost_of_sales": pytest.approx(16176.0),
        "gross_profit": pytest.approx(3287.0),
        "sga": pytest.approx(2468.0),
    }

    rows = build_canonical_rows(
        result,
        ticker="617A",
        filing_id="140120260825525642",
        disclosed_at="2026-08-26 08:00",
    )
    assert len(rows) == 20
    assert {row["quarter"] for row in rows} == {"FY"}


def test_full_year_only_listing_notice_repeated_upsert_is_idempotent():
    pytest.importorskip("pdfplumber")
    result = extract_ipo_listing_financials(FIXTURE.parent / "140120260825525642.pdf")
    stored = {}

    def fake_upsert(table, rows, **kwargs):
        for row in rows:
            stored[row["source_row_key"]] = row
        return {"ok": True, "count": len(rows), "status": 201, "error": None}

    kwargs = dict(
        ticker="617A",
        filing_id="140120260825525642",
        disclosed_at="2026-08-26 08:00",
        config={"test": True},
        upsert=fake_upsert,
    )
    first = write_ipo_financials(result, **kwargs)
    second = write_ipo_financials(result, **kwargs)
    assert first["rows"] == second["rows"] == 20
    assert len(stored) == 20


def test_market_prefix_is_not_shown_in_notification_company_name():
    assert display_company_name("Ｐ－株式会社レイシャス") == "レイシャス"
    assert display_company_name("Ｇ－Ｓｋｙｆａｌｌ") == "Skyfall"


def test_ingest_notification_survives_pl_parse_failure(monkeypatch):
    from tools import tdnet_ingest

    calls = []
    monkeypatch.setattr(
        tdnet_ingest,
        "classify_disclosure_security",
        lambda item: SimpleNamespace(is_etf_like=False, source="test", product_category=None),
    )
    monkeypatch.setattr(
        tdnet_ingest,
        "save_ipo_notification",
        lambda item, dry_run=False: calls.append("card") or {"action": "inserted"},
    )
    monkeypatch.setattr(
        tdnet_ingest,
        "extract_ipo_listing_financials",
        lambda path: (_ for _ in ()).throw(ValueError("broken PL")),
    )

    class State:
        def is_processed(self, disclosure_id):
            return False

        def record(self, **kwargs):
            calls.append(("state", kwargs["status"]))

    item = SimpleNamespace(
        disclosure_id="hash",
        ticker="625A",
        company_name="Skyfall",
        title=TITLE,
        doc_url=URL,
        published_at="2026-09-17 08:00",
        disclosure_type=IPO_LISTING_FINANCIALS,
        source_doc_id=None,
        xbrl_url=None,
    )
    config = SimpleNamespace(state_db_path=str(FIXTURE.parent / "state.db"))
    result = tdnet_ingest._process_single(
        item,
        config,
        State(),
        SimpleNamespace(),
        "run",
        pre_fetched={"doc_path": str(FIXTURE)},
    )
    assert result["status"] == "error"
    assert calls[0] == "card"
    assert ("state", "parse_failed") in calls


def test_ingest_retry_attempts_idempotent_card_before_processed_short_circuit(monkeypatch):
    from tools import tdnet_ingest

    calls = []
    monkeypatch.setattr(
        tdnet_ingest,
        "classify_disclosure_security",
        lambda item: SimpleNamespace(is_etf_like=False, source="test", product_category=None),
    )
    monkeypatch.setattr(
        tdnet_ingest,
        "save_ipo_notification",
        lambda item, dry_run=False: calls.append("card") or {"action": "dedup_skipped"},
    )

    class State:
        def is_processed(self, disclosure_id):
            return True

    item = SimpleNamespace(
        disclosure_id="hash",
        ticker="625A",
        company_name="Skyfall",
        title=TITLE,
        doc_url=URL,
        published_at="2026-09-17 08:00",
        disclosure_type=IPO_LISTING_FINANCIALS,
        source_doc_id=None,
    )
    result = tdnet_ingest._process_single(
        item,
        SimpleNamespace(state_db_path="unused"),
        State(),
        SimpleNamespace(),
        "run",
    )
    assert result == {"status": "skipped", "detail": "処理済み", "code": "625A"}
    assert calls == ["card"]
