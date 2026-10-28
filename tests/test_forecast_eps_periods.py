from src.events.forecast_eps_periods import forecast_eps_revisions
from src.events.forecast_extractor import _return_with_eps_log
from src.events.forecast_models import ForecastRevisionEvent
from src.events.common_models import DocumentMeta
from src.events.event_pipeline import _forecast_to_event_record
from src.events.tdnet_event_store import build_supabase_row
import json


def table(heading: str, previous: str = "12.34", revised: str = "27.32") -> str:
    return f"""{heading}
売上高 営業利益 経常利益 親会社株主に帰属する当期純利益 1株当たり当期純利益
前回発表予想（A） 52,000 700 900 700 {previous}
今回修正予想（B） 53,600 1,900 2,100 1,550 {revised}
増減額 1,600 1,200 1,200 850
(ご参考)前期実績 52,292 1,399 1,710 1,090 18.73
"""


HALF = "2027年3月期第2四半期（中間期）連結業績予想数値の修正（2026年4月1日～2026年9月30日）"
FULL = "2027年3月期通期連結業績予想数値の修正（2026年4月1日～2027年3月31日）"


def test_both_periods_and_reversed_table_order():
    half, full = table(HALF), table(FULL, "59.92", "81.06")
    for text in (half + full, full + half):
        result = _return_with_eps_log(ForecastRevisionEvent(period_label="2027年3月期 第2四半期"), text)
        assert [(row["period"], row["previous_eps"], row["revised_eps"]) for row in result.eps_revisions] == [
            ("2Q_CUMULATIVE", 12.34, 27.32), ("FULL_YEAR", 59.92, 81.06)
        ]
        assert result.latest_full_year_eps == 81.06
        assert result.period_label == "2027年3月期 第2四半期"


def test_single_period_unknown_missing_and_prior_actual():
    assert [row["period"] for row in forecast_eps_revisions(table(HALF))] == ["2Q_CUMULATIVE"]
    assert [row["period"] for row in forecast_eps_revisions(table(FULL))] == ["FULL_YEAR"]
    assert [row["period"] for row in forecast_eps_revisions(table("2027年3月期第2四半期連結業績予想数値の修正"))] == ["UNKNOWN"]
    assert forecast_eps_revisions(table(HALF, revised="-")) == []
    assert forecast_eps_revisions(table(FULL, previous="59.92", revised="59.92")) == []
    assert [row["period"] for row in forecast_eps_revisions(table(HALF) + table(FULL, "59.92", "59.92"))] == ["2Q_CUMULATIVE"]
    assert forecast_eps_revisions("2027年3月期通期\n1株当たり配当金\n前回発表予想 34\n今回修正予想 36") == []


def test_second_quarter_standalone_is_not_cumulative():
    heading = "2027年3月期第2四半期連結業績予想数値の修正（2026年7月1日～2026年9月30日）"
    assert forecast_eps_revisions(table(heading))[0]["period"] == "2Q_STANDALONE"


def test_conflicting_heading_and_date_remains_unknown():
    heading = "2027年3月期通期連結業績予想数値の修正（2026年4月1日～2026年9月30日）"
    assert forecast_eps_revisions(table(heading))[0]["period"] == "UNKNOWN"


def test_normal_notification_record_and_supabase_row_keep_both_periods():
    extracted = _return_with_eps_log(
        ForecastRevisionEvent(period_label="2027年3月期 第2四半期", subtype="upward"),
        table(HALF) + table(FULL, "59.92", "81.06"),
    )
    doc = DocumentMeta(
        doc_id="test-disclosure", ticker="7971", company_name="東リ",
        title="業績予想及び配当予想の修正に関するお知らせ",
        disclosure_datetime="2026-10-28T14:00:00+09:00",
        doc_url="https://www.release.tdnet.info/inbs/140120261027557236.pdf",
    )
    record = _forecast_to_event_record(doc, extracted)
    assert len(json.loads(record.extracted_payload_json)["eps_revisions"]) == 2
    row, raw, *_ = build_supabase_row(record)
    assert row["event_type"] == "forecast"
    assert json.loads(row["raw_payload"])["extracted"]["eps_revisions"] == extracted.eps_revisions
    assert raw["extracted"]["latest_full_year_eps"] == 81.06
