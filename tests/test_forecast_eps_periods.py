from src.events.forecast_eps_periods import forecast_eps_revisions
from src.events.forecast_extractor import _return_with_eps_log
from src.events.forecast_models import ForecastRevisionEvent
from src.events.common_models import DocumentMeta
from src.events.event_pipeline import _forecast_to_event_record
from src.events.tdnet_event_store import build_supabase_row
from src.events.common_storage import ensure_events_table, upsert_event
import json
import hashlib
from pathlib import Path
import sqlite3

import pdfplumber


def table(heading: str, previous: str = "12.34", revised: str = "27.32",
          previous_op: str = "700", revised_op: str = "1,900", unit: str = "百万円") -> str:
    return f"""{heading}
売上高 営業利益 経常利益 親会社株主に帰属する当期純利益 1株当たり当期純利益
{unit} {unit} {unit} {unit} 円 銭
前回発表予想（A） 52,000 {previous_op} 900 700 {previous}
今回修正予想（B） 53,600 {revised_op} 2,100 1,550 {revised}
増減額 1,600 1,200 1,200 850
(ご参考)前期実績 52,292 1,399 1,710 1,090 18.73
"""


HALF = "2027年3月期第2四半期（中間期）連結業績予想数値の修正（2026年4月1日～2026年9月30日）"
FULL = "2027年3月期通期連結業績予想数値の修正（2026年4月1日～2027年3月31日）"


def test_sekisui_original_pdf_shared_fiscal_heading_and_two_tables():
    pdf_path = Path(__file__).parent / "fixtures" / "sekisui_4228_20261028.pdf"
    assert hashlib.sha256(pdf_path.read_bytes()).hexdigest() == "7fef6c6f0ec4624c33ff109461a712d13bdfdd6f52941b79fbd14dbaebf0e7fc"
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert "１．2027年３月期業績予想の修正" in text
    assert "（１）第２四半期（中間期）連結業績予想の修正" in text
    assert "（２）通期連結業績予想の修正" in text
    rows = forecast_eps_revisions(text)
    assert [(row["fiscal_period"], row["period"], row["basis"], row["previous_op"],
             row["revised_op"], row["previous_eps"], row["revised_eps"])
            for row in rows] == [
        ("2027年3月期", "2Q_CUMULATIVE", "連結", 1600, 3000, 43.86, 54.79),
        ("2027年3月期", "FULL_YEAR", "連結", 3100, 5300, 54.83, 81.0),
    ]
    assert [(row["previous_eps_text"], row["revised_eps_text"]) for row in rows] == [
        ("43.86", "54.79"), ("54.83", "81.00"),
    ]
    extracted = _return_with_eps_log(ForecastRevisionEvent(period_label="2027年3月期"), text)
    doc = DocumentMeta(
        doc_id="sekisui-20261028", ticker="4228", company_name="積水化成",
        title="連結業績予想および配当予想の修正に関するお知らせ",
        disclosure_datetime="2026-10-28T16:00:00+09:00",
        doc_url="https://www.release.tdnet.info/inbs/140120261027557141.pdf",
    )
    record = _forecast_to_event_record(doc, extracted)
    stored = json.loads(record.extracted_payload_json)
    assert stored["eps_revisions"] == rows
    assert stored["latest_full_year_eps"] == 81.0
    row, raw, *_ = build_supabase_row(record)
    assert json.loads(row["raw_payload"])["extracted"]["eps_revisions"] == rows
    assert raw["extracted"]["eps_revisions"] == rows
    with sqlite3.connect(":memory:") as conn:
        ensure_events_table(conn)
        assert upsert_event(conn, record)[0] == "inserted"
        assert upsert_event(conn, record)[0] == "no_change"
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_shared_heading_does_not_guess_an_unlabelled_table():
    text = "2027年3月期業績予想の修正\n" + table("業績予想の修正")
    assert forecast_eps_revisions(text)[0]["period"] == "UNKNOWN"


def test_shared_fiscal_heading_ends_at_next_top_level_topic():
    text = ("1.2027年3月期業績予想の修正\n"
            + table("(1)第2四半期(中間期)連結業績予想の修正")
            + "2.別の事項\n"
            + table("(1)通期連結業績予想の修正"))
    rows = forecast_eps_revisions(text)
    assert [r["fiscal_period"] for r in rows] == ["2027年3月期", None]


def test_both_periods_and_reversed_table_order():
    half, full = table(HALF), table(FULL, "59.92", "81.06", "4,100", "5,700")
    for text in (half + full, full + half):
        result = _return_with_eps_log(ForecastRevisionEvent(period_label="2027年3月期 第2四半期"), text)
        assert [(row["period"], row["previous_eps"], row["revised_eps"]) for row in result.eps_revisions] == [
            ("2Q_CUMULATIVE", 12.34, 27.32), ("FULL_YEAR", 59.92, 81.06)
        ]
        assert result.latest_full_year_eps == 81.06
        assert result.period_label == "2027年3月期 第2四半期"
        assert [(row["previous_op"], row["revised_op"], row["op_unit"]) for row in result.eps_revisions] == [
            (700, 1900, "millions_jpy"), (4100, 5700, "millions_jpy")
        ]


def test_single_period_unknown_missing_and_prior_actual():
    assert [row["period"] for row in forecast_eps_revisions(table(HALF))] == ["2Q_CUMULATIVE"]
    assert [row["period"] for row in forecast_eps_revisions(table(FULL))] == ["FULL_YEAR"]
    assert [row["period"] for row in forecast_eps_revisions(table("2027年3月期第2四半期連結業績予想数値の修正"))] == ["UNKNOWN"]
    assert forecast_eps_revisions(table(HALF, revised="-")) == []
    assert forecast_eps_revisions(table(FULL, previous="59.92", revised="59.92", previous_op="700", revised_op="700")) == []
    assert [row["period"] for row in forecast_eps_revisions(table(HALF) + table(FULL, "59.92", "59.92", "700", "700"))] == ["2Q_CUMULATIVE"]
    assert forecast_eps_revisions("2027年3月期通期\n1株当たり配当金\n前回発表予想 34\n今回修正予想 36") == []


def test_operating_unit_conversion_and_unchanged_value():
    for unit, previous, revised in (("百万円", "700", "1,900"),
                                    ("千円", "700,000", "1,900,000"),
                                    ("円", "700,000,000", "1,900,000,000")):
        result = forecast_eps_revisions(table(HALF, previous_op=previous, revised_op=revised, unit=unit))
        assert (result[0]["previous_op"], result[0]["revised_op"]) == (700, 1900)
    unchanged = forecast_eps_revisions(table(HALF, previous_op="700", revised_op="700"))
    assert (unchanged[0]["previous_op"], unchanged[0]["revised_op"]) == (700, 700)
    assert forecast_eps_revisions(table(HALF, "12.34", "12.34", "700", "1,900"))[0]["revised_op"] == 1900


def test_operating_missing_zero_and_negative_are_not_other_metrics():
    zero = forecast_eps_revisions(table(HALF, previous_op="0", revised_op="-100"))
    assert (zero[0]["previous_op"], zero[0]["revised_op"]) == (0, -100)
    deficit = forecast_eps_revisions(table(HALF, previous_op="△100", revised_op="▲50"))
    assert (deficit[0]["previous_op"], deficit[0]["revised_op"]) == (-100, -50)
    unknown_unit = forecast_eps_revisions(table(HALF, unit="単位不明"))
    assert unknown_unit[0]["previous_op"] is None
    assert unknown_unit[0]["revised_op"] is None


def test_second_quarter_standalone_is_not_cumulative():
    heading = "2027年3月期第2四半期連結業績予想数値の修正（2026年7月1日～2026年9月30日）"
    assert forecast_eps_revisions(table(heading))[0]["period"] == "2Q_STANDALONE"


def test_conflicting_heading_and_date_remains_unknown():
    heading = "2027年3月期通期連結業績予想数値の修正（2026年4月1日～2026年9月30日）"
    assert forecast_eps_revisions(table(heading))[0]["period"] == "UNKNOWN"


def test_normal_notification_record_and_supabase_row_keep_both_periods():
    extracted = _return_with_eps_log(
        ForecastRevisionEvent(period_label="2027年3月期 第2四半期", subtype="upward"),
        table(HALF) + table(FULL, "59.92", "81.06", "4,100", "5,700"),
    )
    doc = DocumentMeta(
        doc_id="test-disclosure", ticker="7971", company_name="東リ",
        title="業績予想及び配当予想の修正に関するお知らせ",
        disclosure_datetime="2026-10-28T14:00:00+09:00",
        doc_url="https://www.release.tdnet.info/inbs/140120261027557236.pdf",
    )
    record = _forecast_to_event_record(doc, extracted)
    assert len(json.loads(record.extracted_payload_json)["eps_revisions"]) == 2
    assert json.loads(record.extracted_payload_json)["eps_revisions"][1]["previous_op"] == 4100
    legacy = ForecastRevisionEvent(
        period_label=extracted.period_label, subtype=extracted.subtype,
        revised_sales=extracted.revised_sales, revised_op=extracted.revised_op,
        revised_net_income=extracted.revised_net_income, revised_eps=extracted.revised_eps,
    )
    assert _forecast_to_event_record(doc, legacy).fingerprint == record.fingerprint
    row, raw, *_ = build_supabase_row(record)
    assert row["event_type"] == "forecast"
    assert json.loads(row["raw_payload"])["extracted"]["eps_revisions"] == extracted.eps_revisions
    assert raw["extracted"]["eps_revisions"][1]["revised_op"] == 5700
    assert raw["extracted"]["latest_full_year_eps"] == 81.06
