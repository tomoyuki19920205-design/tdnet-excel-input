from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from src.edinet_orders.extractor import extract_from_company
from src.edinet_orders.transformer import transform_to_db_row
from src.edinet_orders.semantic_table import (
    BEGIN_CARRYOVER_KEYWORDS,
    END_CARRYOVER_KEYWORDS,
    EXPLICIT_BACKLOG_KEYWORDS,
)


def _write_zip(tmp_path: Path, doc_id: str, body: str) -> Path:
    cache = tmp_path / "cache"
    target = cache / doc_id
    target.mkdir(parents=True)
    html = f"""<?xml version="1.0" encoding="utf-8"?>
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>
    <ix:nonnumeric name="jpdei_cor:DocumentTypeDEI">第三号様式</ix:nonnumeric>
    <ix:nonnumeric name="jpdei_cor:TypeOfCurrentPeriodDEI">通期</ix:nonnumeric>
    <ix:nonnumeric name="jpdei_cor:CurrentPeriodEndDateDEI">2025-03-31</ix:nonnumeric>
    <ix:nonnumeric name="jpdei_cor:CurrentFiscalYearEndDateDEI">2025-03-31</ix:nonnumeric>
    {body}</body></html>"""
    with zipfile.ZipFile(target / "xbrl.zip", "w") as archive:
        archive.writestr("XBRL/PublicDoc/test.htm", html.encode("utf-8"))
    return cache


def _extract(tmp_path: Path, body: str, *, doc_id: str = "S100TEST", ticker: str = "1736", period: str = "2025-03-31", doc_type: str = "120"):
    cache = _write_zip(tmp_path, doc_id, body)
    return extract_from_company(
        {"ticker": ticker, "company": "fixture", "doc_id": doc_id, "period_end": period, "doc_type_code": doc_type},
        cache_dir=str(cache),
    )


CONSTRUCTION_HEADER = """
<tr><th rowspan="3">区分</th><th rowspan="3">期首繰越工事高(千円)</th>
<th rowspan="3">当期受注工事高(千円)</th><th rowspan="3">計(千円)</th>
<th rowspan="3">当期完成工事高(千円)</th><th colspan="3">期末繰越工事高</th></tr>
<tr><th rowspan="2">手持工事高(千円)</th><th colspan="2">うち施工高(千円)</th></tr>
<tr><th>(%)</th><th>金額(千円)</th></tr>
"""


def test_1736_all_tables_compared_and_amount_leaf_wins(tmp_path):
    body = f"""
    <table>{CONSTRUCTION_HEADER}<tr><td>工事合計</td><td>9,442,383</td><td>17,845,459</td><td>27,287,843</td><td>15,883,625</td><td>11,404,218</td><td>1.9</td><td>221,735</td></tr></table>
    <table>{CONSTRUCTION_HEADER}<tr><td>工事合計</td><td>11,404,218</td><td>18,896,479</td><td>30,300,698</td><td>19,592,449</td><td>10,708,248</td><td>1.8</td><td>188,993</td></tr></table>
    """
    result = _extract(tmp_path, body)
    assert result["orders_received"] == 18_896_479
    assert result["construction_carryover"] == 10_708_248
    assert result["order_backlog"] is None
    assert result["completed_construction"] == 19_592_449
    assert result["beginning_carryover"] == 11_404_218
    assert result["provenance"]["metrics"]["construction_carryover"]["column_index"] == 5
    assert result["provenance"]["metrics"]["construction_carryover"]["header_spans"][0]["colspan"] == 3


def test_1764_percentage_and_last_subcomponent_are_not_selected(tmp_path):
    body = f"""<table>{CONSTRUCTION_HEADER}
    <tr><td>当事業年度(自2024年4月1日至2025年3月31日)|計</td><td>8,752,964</td><td>16,195,763</td><td>24,948,727</td><td>10,603,204</td><td>14,345,523</td><td>0.6</td><td>91,338</td></tr>
    </table>"""
    result = _extract(tmp_path, body, ticker="1764")
    assert result["construction_carryover"] == 14_345_523
    assert not result["selected_percentage_leaf"]
    assert not result["selected_subcomponent_leaf"]


def test_1820_half_year_is_rejected_for_annual_key(tmp_path):
    body = f"<table>{CONSTRUCTION_HEADER}<tr><td>工事合計</td><td>10,000</td><td>20,000</td><td>30,000</td><td>15,000</td><td>15,000</td><td>0.1</td><td>100</td></tr></table>"
    result = _extract(tmp_path, body, ticker="1820", doc_type="160")
    row = transform_to_db_row(result, "2025-03-31", enable_partial_save=True)
    assert result["report_type"] == "HY"
    assert row["classification"] == "DOCUMENT_PERIOD_TYPE_MISMATCH_REJECT"
    assert row["save_candidate"] is False


def test_1822_previous_table_does_not_stop_current_table(tmp_path):
    body = f"""
    <table>{CONSTRUCTION_HEADER}<tr><td>計</td><td>231,608</td><td>100,510</td><td>332,118</td><td>121,791</td><td>210,327</td><td>0.4</td><td>880</td></tr></table>
    <table>{CONSTRUCTION_HEADER}<tr><td>計</td><td>210,327</td><td>106,034</td><td>316,362</td><td>99,030</td><td>217,331</td><td>0.3</td><td>832</td></tr></table>
    """
    result = _extract(tmp_path, body, ticker="1822")
    assert (result["orders_received"], result["construction_carryover"]) == (106_034, 217_331)
    assert any(candidate["orders_received"] == 100_510 for candidate in result["candidate_evidence"])


def test_1827_simple_ending_carryover_is_not_backlog(tmp_path):
    body = """<table><tr><th>期別</th><th>区分</th><th>前期繰越工事高(百万円)</th><th>当期受注工事高(百万円)</th><th>計(百万円)</th><th>当期完成工事高(百万円)</th><th>次期繰越工事高(百万円)</th></tr>
    <tr><td>第83期(自2024年4月1日至2025年3月31日)</td><td>計</td><td>85,607</td><td>75,921</td><td>161,528</td><td>78,452</td><td>83,076</td></tr></table>"""
    result = _extract(tmp_path, body, ticker="1827")
    assert result["order_backlog"] is None
    assert result["construction_carryover"] == 83_076


def test_1828_company_total_and_thousand_yen_contract(tmp_path):
    body = """<table><tr><th>工事別</th><th>前期繰越工事高(千円)</th><th>当期受注工事高(千円)</th><th>計(千円)</th><th>当期完成工事高(千円)</th><th>次期繰越工事高(千円)</th></tr>
    <tr><td>電気計装工事</td><td>6,998,292</td><td>9,095,693</td><td>16,093,985</td><td>9,194,609</td><td>6,899,375</td></tr>
    <tr><td>計</td><td>28,454,955</td><td>51,406,759</td><td>79,861,715</td><td>49,097,760</td><td>30,763,955</td></tr></table>"""
    result = _extract(tmp_path, body, ticker="1828")
    row = transform_to_db_row(result, "2025-03-31", enable_partial_save=True)
    assert result["segment_name"] is None
    assert result["orders_received"] == 51_406_759
    assert result["unit"] == "千円"
    assert row["source_unit"] == "thousand_yen"
    assert row["raw_orders_received"] == 51_406_759
    assert row["orders_received"] == 51_406


def test_2467_current_orders_and_explicit_backlog_columns(tmp_path):
    body = """<table><tr><th rowspan="2">セグメントの名称</th><th colspan="2">受注高(千円)</th><th rowspan="2">受注残高(千円)</th></tr>
    <tr><th>前事業年度</th><th>当事業年度</th></tr>
    <tr><td>合計</td><td>1,254,968</td><td>1,475,506</td><td>254,635</td></tr></table>"""
    result = _extract(tmp_path, body, ticker="2467")
    assert result["orders_received"] == 1_475_506
    assert result["order_backlog"] == 254_635
    assert result["construction_carryover"] is None


def test_rpo_is_not_classified_as_backlog_or_construction_carryover(tmp_path):
    body = """<table><tr><th>区分</th><th>残存履行義務(百万円)</th></tr>
    <tr><td>合計</td><td>12,345</td></tr></table>"""
    result = _extract(tmp_path, body, ticker="2467")
    row = transform_to_db_row(result, "2025-03-31", enable_partial_save=True)
    assert result["rpo"] == 12_345
    assert result["order_backlog"] is None
    assert result["construction_carryover"] is None
    assert row["classification"] == "PASS_SAVE_CANDIDATE"
    assert row["save_candidate"] is True


def test_source_table_exception_preserves_reported_values(tmp_path):
    body = """<table><tr><th>工事別</th><th>前期繰越工事高(千円)</th><th>当期受注工事高(千円)</th><th>計(千円)</th><th>当期完成工事高(千円)</th><th>次期繰越工事高(千円)</th></tr>
    <tr><td>計</td><td>15,297,139</td><td>44,644,696</td><td>59,941,835</td><td>39,623,200</td><td>19,877,182</td></tr></table>"""
    result = _extract(tmp_path, body, ticker="1828")
    row = transform_to_db_row(result, "2025-03-31", enable_partial_save=True)
    assert result["arithmetic_status"] == "SOURCE_TABLE_EXCEPTION"
    assert result["construction_carryover"] == 19_877_182
    assert row["classification"] == "SOURCE_TABLE_EXCEPTION"
    assert row["save_candidate"] is True


def test_ambiguous_arithmetic_mismatch_is_review_not_source_exception(tmp_path):
    body = f"""<table>{CONSTRUCTION_HEADER}
    <tr><td>工事合計</td><td>10,000</td><td>20,000</td><td>30,000</td><td>10,000</td><td>15,000</td><td>1.0</td><td>500</td></tr>
    </table>"""
    result = _extract(tmp_path, body)
    row = transform_to_db_row(result, "2025-03-31", enable_partial_save=True)
    assert result["arithmetic_status"] == "ARITHMETIC_REVIEW"
    assert row["classification"] == "ARITHMETIC_REVIEW"
    assert row["save_candidate"] is False


def test_keyword_sets_are_semantically_disjoint():
    assert not set(EXPLICIT_BACKLOG_KEYWORDS) & set(BEGIN_CARRYOVER_KEYWORDS)
    assert not set(EXPLICIT_BACKLOG_KEYWORDS) & set(END_CARRYOVER_KEYWORDS)
    assert not set(BEGIN_CARRYOVER_KEYWORDS) & set(END_CARRYOVER_KEYWORDS)


REAL_CACHE_CASES = [
    ("1736", 2022, "S100OJ3H", 15_360_678, 7_567_199), ("1736", 2023, "S100QXFG", 16_147_747, 9_442_383),
    ("1736", 2024, "S100TRQW", 17_845_459, 11_404_218), ("1736", 2025, "S100W6KN", 18_896_479, 10_708_248),
    ("1736", 2026, "S100YKWY", 23_337_419, 13_124_391), ("1764", 2022, "S100RXVJ", 12_061_471, 10_055_890),
    ("1764", 2023, "S100RXVJ", 8_689_946, 8_752_964), ("1764", 2024, "S100V92X", 16_195_763, 14_345_523),
    ("1764", 2025, "S100WRZI", 14_146_487, 15_620_555), ("1820", 2022, "S100R536", 328_093, 564_018),
    ("1820", 2023, "S100R536", 327_401, 595_777), ("1820", 2024, "S100TRPW", 351_245, 585_463),
    ("1820", 2025, "S100WA0O", 409_904, 674_074), ("1820", 2026, "S100YEDU", 369_819, 688_654),
    ("1822", 2022, "S100R8L3", 113_010, 225_461), ("1822", 2023, "S100R8L3", 121_855, 231_608),
    ("1822", 2024, "S100TV9Q", 100_510, 210_327), ("1822", 2025, "S100W3CM", 106_034, 217_331),
    ("1822", 2026, "S100YI2K", 93_470, 207_864), ("1827", 2022, "S100R9JL", 74_242, 70_234),
    ("1827", 2023, "S100R9JL", 97_452, 92_987), ("1827", 2024, "S100TWBK", 74_113, 85_607),
    ("1827", 2025, "S100W82M", 75_921, 83_076), ("1827", 2026, "S100YHMT", 84_233, 94_653),
    ("1828", 2022, "S100R82V", 44_644_696, 19_877_182), ("1828", 2023, "S100R82V", 46_376_160, 25_513_697),
    ("1828", 2024, "S100TW8D", 52_944_110, 28_454_955), ("1828", 2025, "S100W32Y", 51_406_759, 30_763_955),
    ("1828", 2026, "S100YHGU", 43_721_266, 24_552_172),
]


@pytest.mark.parametrize("ticker,fy,doc_id,orders,ending", REAL_CACHE_CASES)
def test_real_edinet_cache_29_expected(ticker, fy, doc_id, orders, ending):
    cache = Path(__file__).resolve().parents[1] / "data" / "edinet_cache"
    if not (cache / doc_id / "xbrl.zip").exists():
        pytest.skip("official EDINET cache not available")
    period = f"{fy}-{'06-30' if ticker == '1764' else '03-31'}"
    result = extract_from_company(
        {"ticker": ticker, "company": "regression", "doc_id": doc_id, "period_end": period, "doc_type_code": "120"},
        cache_dir=str(cache),
    )
    assert result["orders_received"] == orders
    assert result["construction_carryover"] == ending
    assert result["order_backlog"] is None


def test_real_1820_half_year_cannot_block_annual_filing():
    cache = Path(__file__).resolve().parents[1] / "data" / "edinet_cache"
    if not all((cache / doc_id / "xbrl.zip").exists() for doc_id in ("S100WZLS", "S100YEDU")):
        pytest.skip("official EDINET cache not available")
    half_year = extract_from_company(
        {"ticker": "1820", "company": "西松建設", "doc_id": "S100WZLS", "period_end": "2026-03-31", "doc_type_code": "160"},
        cache_dir=str(cache),
    )
    annual = extract_from_company(
        {"ticker": "1820", "company": "西松建設", "doc_id": "S100YEDU", "period_end": "2026-03-31", "doc_type_code": "120"},
        cache_dir=str(cache),
    )
    half_year_row = transform_to_db_row(half_year, "2026-03-31", enable_partial_save=True)
    annual_row = transform_to_db_row(annual, "2026-03-31", enable_partial_save=True)
    assert half_year_row["classification"] == "DOCUMENT_PERIOD_TYPE_MISMATCH_REJECT"
    assert half_year_row["save_candidate"] is False
    assert annual_row["doc_id"] == "S100YEDU"
    assert annual_row["raw_orders_received"] == 369_819
    assert annual_row["save_candidate"] is True


def test_real_2467_current_orders_and_explicit_backlog_regression():
    cache = Path(__file__).resolve().parents[1] / "data" / "edinet_cache"
    if not (cache / "S100W96H" / "xbrl.zip").exists():
        pytest.skip("official EDINET cache not available")
    result = extract_from_company(
        {"ticker": "2467", "company": "バルクホールディングス", "doc_id": "S100W96H", "period_end": "2025-03-31", "doc_type_code": "120"},
        cache_dir=str(cache),
    )
    assert result["orders_received"] == 1_475_506
    assert result["order_backlog"] == 254_635
    assert result["construction_carryover"] is None
