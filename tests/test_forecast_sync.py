import json
import sqlite3
import uuid
from pathlib import Path

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
