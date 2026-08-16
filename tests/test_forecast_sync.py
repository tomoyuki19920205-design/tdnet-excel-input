import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from lib.pipeline.forecast_sync import (
    ForecastDTO,
    _effective_disclosure_datetime,
    expand_forecast_rows,
    forecast_period_from_actual,
    load_earnings_forecasts,
    load_revision_forecasts,
    parse_forecast_period_end,
    select_latest_forecasts,
)
from src.events.earnings_summary_storage import ensure_earnings_summary_table
from src.events.forecast_extractor import (
    _extract_from_horizontal_table,
    _normalize_text,
    _return_with_eps_log,
)
from src.events.forecast_models import ForecastRevisionEvent


def _dto(**overrides):
    data = dict(
        ticker="3032",
        forecast_period_end="2027-03-31",
        metric="sales",
        value=100.0,
        disclosure_datetime="2026-08-13T15:00:00+09:00",
        filing_id="doc-1",
        source="tdnet_forecast",
        correction_flag=False,
        forecast_horizon="current_fy",
        accounting_standard="J_GAAP",
        document_type="forecast_revision",
    )
    data.update(overrides)
    return ForecastDTO(**data)


def test_period_mapping_supports_non_march_year_ends_and_quarters():
    assert forecast_period_from_actual("2026-03-31", "FY") == ("2027-03-31", "next_fy")
    assert forecast_period_from_actual("2026-12-31", "4Q") == ("2027-12-31", "next_fy")
    assert forecast_period_from_actual("2026-02-28", "2Q") == ("2026-02-28", "current_fy")
    assert forecast_period_from_actual("2026-03-31", "1Q") == ("2026-03-31", "current_fy")
    assert forecast_period_from_actual("2026-03-31", "3Q") == ("2026-03-31", "current_fy")
    assert parse_forecast_period_end("2026年9月期 通期") == "2026-09-30"
    assert parse_forecast_period_end("2027年2月期") == "2027-02-28"
    assert _effective_disclosure_datetime(
        "", "16:00", "2026-05-01T21:54:26+09:00",
        "https://example/081220260401596757.zip",
    ) == "2026-04-01T16:00"


def test_latest_disclosure_wins_and_correction_only_breaks_exact_tie():
    stale_correction = _dto(
        value=90, disclosure_datetime="2026-08-12T16:00:00+09:00", correction_flag=True,
        filing_id="old-correction",
    )
    newer = _dto(value=110, filing_id="new")
    assert select_latest_forecasts([newer, stale_correction]) == [newer]

    correction = _dto(value=120, correction_flag=True, filing_id="correction")
    assert select_latest_forecasts([newer, correction]) == [correction]


def test_nightly_catchup_cannot_override_newer_tdnet_but_later_jquants_can():
    tdnet = _dto(value=110, source="tdnet_forecast", filing_id="tdnet")
    stale_jq = _dto(
        value=100, source="jquants_forecast_fy", filing_id="",
        disclosure_datetime="2026-08-12",
    )
    later_jq = _dto(
        value=120, source="jquants_forecast_fy", filing_id="",
        disclosure_datetime="2026-08-14",
    )
    assert select_latest_forecasts([stale_jq, tdnet]) == [tdnet]
    jq_catchup = _dto(
        value=110, source="jquants_forecast_fy", filing_id="",
        disclosure_datetime="2026-08-13T15:00:01+09:00",
    )
    assert select_latest_forecasts([tdnet, jq_catchup])[0].value == 110
    assert select_latest_forecasts([tdnet, later_jq]) == [later_jq]
    rows = expand_forecast_rows([tdnet, tdnet])
    assert rows[0]["source_row_key"] == rows[1]["source_row_key"]
    assert rows[0]["disclosure_datetime"] == "2026-08-13T06:00:00.000000Z"


def test_earnings_guidance_loader_maps_fy_and_never_turns_null_into_zero():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_earnings_summary_table(conn)
    conn.execute(
        "CREATE TABLE quarterly_results (company_code TEXT, fiscal_year_end TEXT, quarter TEXT, "
        "source_doc_id TEXT, source_url TEXT)"
    )
    for ticker, month, quarter in (("3032", "03-31", "FY"), ("3099", "12-31", "2Q"), ("5845", "02-28", "3Q")):
        doc_id = f"doc-{ticker}"
        conn.execute(
            "INSERT INTO quarterly_results VALUES (?,?,?,?,?)",
            (ticker, f"2026-{month}", quarter, doc_id, f"https://example/{doc_id}"),
        )
        conn.execute(
            "INSERT INTO earnings_summaries "
            "(ticker,fingerprint,created_at,source_doc_id,source_url,quarter,title,guidance_sales,guidance_op) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, f"fp-{ticker}", "2026-08-13T15:00:00+09:00", None if ticker == "3099" else doc_id,
             f"https://example/{doc_id}", quarter,
             "決算短信（IFRS）" if ticker == "5845" else "決算短信（日本基準）",
             12_000_000, None),
        )
    candidates, quarantine = load_earnings_forecasts(conn)
    assert quarantine == []
    by_ticker = {c.ticker: c for c in candidates}
    assert by_ticker["3032"].forecast_period_end == "2027-03-31"
    assert by_ticker["3099"].forecast_period_end == "2026-12-31"
    assert by_ticker["3099"].filing_id == "doc-3099"
    assert by_ticker["5845"].forecast_period_end == "2026-02-28"
    assert by_ticker["5845"].accounting_standard == "IFRS"
    assert all(c.metric == "sales" and c.value == 12 for c in candidates)


def test_revision_loader_maps_all_supported_metrics_and_quarantines_missing_period():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE events (event_id TEXT, source_doc_id TEXT, ticker TEXT, disclosure_datetime TEXT, "
        "created_at TEXT, title TEXT, event_type TEXT, extracted_payload_json TEXT)"
    )
    payload = {
        "period_label": "2027年3月期 通期",
        "revised_sales": 11000,
        "revised_op": 900,
        "revised_ordinary": 850,
        "revised_net_income": None,
    }
    conn.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
        ("ev-1", "doc-1", "3032", "2026-08-13T15:00:00+09:00", "", "業績予想の修正", "forecast_revision", json.dumps(payload)),
    )
    conn.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
        ("ev-2", "doc-2", "3099", "2026-08-13T15:01:00+09:00", "", "業績予想の修正", "forecast_revision", json.dumps({"revised_sales": 1})),
    )
    conn.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
        ("ev-3", "doc-3", "5845", "2026-08-13T15:02:00+09:00", "", "中間期及び通期業績予想の修正", "forecast_revision", json.dumps({"period_label": "2027年3月期 第2四半期", "revised_sales": 13459})),
    )
    candidates, quarantine = load_revision_forecasts(conn)
    assert {(c.metric, c.value) for c in candidates} == {
        ("sales", 11000), ("operating_profit", 900), ("ordinary_profit", 850),
    }
    assert [q["disclosure_id"] for q in quarantine] == ["doc-2", "doc-3"]


def test_nightly_jquants_rows_preserve_disclosure_date():
    from tools.sync_financials import read_forecast_rows

    db_path = Path.cwd() / f".test_forecast_sync_{uuid.uuid4().hex}.db"
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE jquants_financials_normalized ("
            "local_code TEXT, current_fiscal_year_end_date TEXT, "
            "type_of_current_period TEXT, raw_json TEXT, disclosed_date TEXT, fetched_at TEXT)"
        )
        conn.execute(
            "INSERT INTO jquants_financials_normalized VALUES (?,?,?,?,?,?)",
            ("30320", "2026-03-31", "FY", json.dumps({"NxFSales": 7_000_000_000, "NxFOP": 125_000_000}),
             "2026-05-13", "2026-05-13 15:00:00"),
        )
        conn.commit()
        conn.close()
        rows = read_forecast_rows(str(db_path), recent_days=0)
    finally:
        db_path.unlink(missing_ok=True)
    assert rows == [{
        "ticker": "3032", "period": "2027-03-31", "quarter": "FY",
        "sales": 7000, "gross_profit": None, "operating_profit": 125,
        "source": "jquants_nxf", "disclosure_datetime": "2026-05-13",
        "updated_at": rows[0]["updated_at"],
    }]


def test_1967_targeted_forecast_preserves_march_20_and_filters_other_tickers(tmp_path):
    from tools.sync_financials import read_forecast_rows

    db_path = tmp_path / "forecast.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE jquants_financials_normalized ("
        "local_code TEXT, current_fiscal_year_end_date TEXT, "
        "type_of_current_period TEXT, raw_json TEXT, disclosed_date TEXT, fetched_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO jquants_financials_normalized VALUES (?,?,?,?,?,?)",
        [
            ("19670", "2026-03-20", "FY", json.dumps({"NxFSales": 55_000_000_000, "NxFOP": 4_800_000_000}), "2026-05-13", "2026-05-13"),
            ("99990", "2026-03-31", "FY", json.dumps({"NxFSales": 1_000_000_000}), "2026-05-13", "2026-05-13"),
        ],
    )
    conn.commit()
    conn.close()

    rows = read_forecast_rows(str(db_path), ticker="1967")
    assert [(row["ticker"], row["period"]) for row in rows] == [("1967", "2027-03-20")]


@pytest.mark.parametrize(
    ("ticker_case", "sales", "operating_profit"),
    [
        ("4058", 60_002_370_210_021_000_000_000, None),
        ("5025", 1_601_748_216_262.72, None),
        ("6090", 142_021_021_020_035.6, None),
        ("4076", None, 0.0000550055),
        ("1663", 0.000029, None),
    ],
)
def test_final_forecast_guard_rejects_scale_and_concatenation_anomalies(
    ticker_case, sales, operating_profit,
):
    event = ForecastRevisionEvent(
        revised_sales=sales,
        revised_op=operating_profit,
        previous_op=14.0,
        extraction_source="prose",
        extracted_metrics_count=2,
        subtype="upward",
        importance=90,
        confidence=0.45,
    )
    sanitized = _return_with_eps_log(event)
    assert sanitized.revised_sales is None, ticker_case
    assert sanitized.previous_op is None, ticker_case
    assert sanitized.revised_op is None, ticker_case
    assert sanitized.extracted_metrics_count == 0
    assert sanitized.importance == 0


def test_final_forecast_guard_keeps_plausible_million_yen_amounts():
    event = ForecastRevisionEvent(
        previous_sales=5900,
        revised_sales=6000,
        previous_op=650,
        revised_op=710,
        extraction_source="pdfplumber_table_fitz",
        extracted_metrics_count=2,
    )
    sanitized = _return_with_eps_log(event)
    assert sanitized.revised_sales == 6000
    assert sanitized.revised_op == 710
    assert sanitized.extracted_metrics_count == 2


@pytest.mark.parametrize(
    ("ticker_case", "text", "expected"),
    [
        (
            "4058",
            "EBITDA 1株当たり\n売上高 営業利益 経常利益 当期純利益\n"
            "前回発表予想(A) 5,800 2,170 1,900 1,900 1,300 119.67\n"
            "今回修正予想(B) 6,000 2,370 2,100 2,100 1,400 128.87",
            (5800, 6000, 1900, 2100),
        ),
        (
            "1663",
            "売上高 営業利益 経常利益 当期純利益\n"
            "前回発表予想(A) 87,000 9,200 10,300 6,300\n"
            "今回修正予想(B) 99,900 9,600 10,900 6,800",
            (87000, 99900, 9200, 9600),
        ),
        (
            "2120",
            "売上収益 営業利益 当期利益\n"
            "前回発表予想(A) 29,700 3,000 1,900\n"
            "今回修正予想(B) 29,300 3,900 2,500",
            (29700, 29300, 3000, 3900),
        ),
        (
            "3160",
            "売上高 営業利益 経常利益 当期純利益\n"
            "前回発表予想(A) 78,600 660 820 550\n"
            "今回修正予想(B) 79,549 159 339 56",
            (78600, 79549, 660, 159),
        ),
    ],
)
def test_native_table_keeps_single_space_column_boundaries(ticker_case, text, expected):
    result = _extract_from_horizontal_table(_normalize_text(text).splitlines())
    actual = (
        result["previous_sales"], result["revised_sales"],
        result["previous_op"], result["revised_op"],
    )
    assert actual == expected, ticker_case


def test_ifrs_header_maps_attributable_profit_and_basic_eps_columns():
    text = (
        "単位:百万円\n"
        "親会社の所有者に帰属する当期利益 基本的1株当たり当期利益(円)\n"
        "売上収益 営業利益\n"
        "前回発表予想(A) 29,700 3,000 1,900 14.10\n"
        "今回修正予想(B) 29,300 3,900 2,500 19.50"
    )
    result = _extract_from_horizontal_table(_normalize_text(text).splitlines())
    assert result["previous_net_income"] == 1900
    assert result["revised_net_income"] == 2500
    assert result["previous_eps"] == 14.10
    assert result["revised_eps"] == 19.50
