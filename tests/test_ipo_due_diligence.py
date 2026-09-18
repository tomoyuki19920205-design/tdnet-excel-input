from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile

import pytest

import src.ipo_analysis_report as report
import src.ipo_due_diligence as diligence


FIXTURE = Path(__file__).parent / "fixtures" / "ipo_627_edinet_excerpt.html"


def fixture_documents():
    data = BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("XBRL/PublicDoc/0101010_fixture_ixbrl.htm", FIXTURE.read_bytes())
    return diligence._html_documents(data.getvalue())


def html_documents(markup: str):
    data = BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("XBRL/PublicDoc/fixture_ixbrl.htm", markup)
    return diligence._html_documents(data.getvalue())


@pytest.fixture(autouse=True)
def fixed_page_locator(monkeypatch):
    monkeypatch.setattr(diligence, "locate_pdf_page_by_values", lambda *args, **kwargs: 24)


def test_627_major_shareholder_table_and_post_sale_balance():
    rows = diligence.extract_shareholders(fixture_documents(), "P03", b"")
    sompo = next(row for row in rows if "ＳＯＭＰＯ" in row["name"])
    parking = next(row for row in rows if "日本駐車場開発" in row["name"])
    assert (sompo["before_shares"], sompo["sold_or_allotted_shares"], sompo["after_shares"]) == (1_969_140, 1_124_600, 844_540)
    assert (parking["before_shares"], parking["sold_or_allotted_shares"], parking["after_shares"]) == (157_800, -368_000, 525_800)


def test_627_lockup_periods_and_price_release_are_separate():
    rows = diligence.extract_lockups(fixture_documents(), "P03", b"")
    assert any(row["days"] == 360 and row["price_release_multiple"] is None for row in rows)
    assert any(row["days"] == 180 and row["price_release_multiple"] == 1.5 for row in rows)
    assert any(row["days"] == 180 and row["price_release_multiple"] is None for row in rows)


def test_627_all_so_series_forfeiture_and_dilution():
    rows = diligence.extract_stock_options(fixture_documents(), "P01", b"")
    assert [(row["exercise_price_yen"], row["effective_potential_shares"]) for row in rows] == [(550, 699_393), (580, 178_100)]
    total = sum(row["effective_potential_shares"] for row in rows)
    assert total == 877_493
    assert total / 6_262_140 * 100 == pytest.approx(14.01266979)


def test_vertical_single_series_stock_option_table_is_supported():
    documents = html_documents("""
    <html><body><p>第１回新株予約権</p><table>
      <tr><td>決議年月日</td><td>2022年3月17日</td></tr>
      <tr><td>新株予約権の目的となる株式の種類、内容及び数（株）</td><td>普通株式 38,210 [187,900] 株</td></tr>
      <tr><td>新株予約権の行使時の払込金額（円）</td><td>3,300 [660] 円</td></tr>
      <tr><td>新株予約権の行使期間</td><td>2024年4月9日～2032年3月17日</td></tr>
    </table></body></html>""")
    rows = diligence.extract_stock_options(documents, "P01", b"")
    assert len(rows) == 1
    assert rows[0]["effective_potential_shares"] == 187_900
    assert rows[0]["exercise_price_yen"] == 660


def test_shareholder_header_whitespace_is_normalized():
    documents = html_documents("""
    <html><body><table><tr><th>氏名又は名称</th><th>住所</th><th>所有株式数</th><th>割合</th>
      <th>売出し後の 所有株式数</th><th>割合</th></tr>
      <tr><td>株主A</td><td>東京都</td><td>1,000</td><td>10</td><td>600</td><td>6</td></tr>
    </table></body></html>""")
    rows = diligence.extract_shareholders(documents, "P02", b"")
    assert rows[0]["sold_or_allotted_shares"] == 400


def test_627_bs_cf_and_tax_adjustment_use_latest_context():
    facts = diligence.extract_financial_position(fixture_documents(), "P01", b"")
    assert facts["total_assets_million_yen"] == pytest.approx(1126.555)
    assert facts["net_assets_million_yen"] == pytest.approx(629.229)
    assert facts["cash_million_yen"] == pytest.approx(714.438)
    assert facts["interest_bearing_debt_million_yen"] == pytest.approx(58.66)
    assert facts["operating_cf_million_yen"] == pytest.approx(237.729)
    assert facts["income_taxes_deferred_million_yen"] == pytest.approx(-22.857)


def test_627_kpi_units_and_periods_are_preserved():
    facts = diligence.extract_kpis_and_narratives(fixture_documents(), "P01", b"")
    assert [(row["name"], row["value"], row["unit"]) for row in facts["kpis"]] == [
        ("cumulative_users", 500.0, "万人"), ("available_parking_spaces", 5.5, "万台")]
    assert facts["customer_concentration"] == "10%以上の販売先なし"


def test_reason_code_distinguishes_parser_failure_from_absence():
    gate = diligence.build_completeness(manifests=[], financials=[], offering={}, diligence={},
                                        discovery={"reason_code": "SOURCE_NOT_DISCOVERED"})
    assert gate["groups"]["shareholders_sellers"]["reason_code"] == "SOURCE_NOT_DISCOVERED"
    gate = diligence.build_completeness(manifests=[{"title": "有価証券届出書"}], financials=[], offering={}, diligence={})
    assert gate["groups"]["shareholders_sellers"]["reason_code"] == "PARSER_UNSUPPORTED"
    assert "SOURCE_ACTUALLY_ABSENT" not in {item["reason_code"] for item in gate["missing"]}


def test_offering_not_applicable_passes_only_with_official_evidence():
    offering = {"not_applicable_evidence": {"reason_code": "NOT_APPLICABLE", "source_id": "P01"}}
    gate = diligence.build_completeness(manifests=[], financials=[], offering=offering, diligence={})
    assert gate["groups"]["offering"] == {
        "passed": True, "reason_code": "NOT_APPLICABLE", "evidence_count": 1}


def test_integrity_and_completeness_gates_are_independent(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: ("[事業モデル] 検証済みfactに基づく。", None, "test"))
    manifests = [{"source_id": "P01", "title": "上場に伴う決算情報等のお知らせ", "url": "https://example.test/p.pdf",
                  "fetch_status": "success", "sha256": "a" * 64, "used_pages": [], "page_count": 1}]
    payload = report.build_report_payload(event_id="e", ticker="627A", company_name="アキッパ", listing_date="2026-09-18",
                                          manifests=manifests, offering={}, financial_rows=[])
    assert payload["validation_result"]["integrity_gate"]["passed"]
    assert not payload["validation_result"]["completeness_gate"]["passed"]
    assert payload["status"] == "partial"


def test_624_latest_full_year_actual_is_selected_after_comparative_year(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: (None, "disabled", "test"))
    rows = []
    for period, source, sales in (("2025-06-30", "official_pdf", 100), ("2026-06-30", "official_pdf", 120), ("2027-06-30", "tdnet_forecast", 140)):
        rows.append({"period": period, "quarter": "FY", "metric": "sales", "value": sales,
                     "source": source, "source_row_key": period})
    manifests = [{"source_id": "P01", "title": "上場に伴う決算情報等のお知らせ", "url": "https://example.test/p.pdf",
                  "fetch_status": "success", "sha256": "a" * 64, "used_pages": [], "page_count": 1}]
    payload = report.build_report_payload(event_id="e", ticker="624A", company_name="かがやきHD", listing_date="2026-09-18",
                                          manifests=manifests, offering={}, financial_rows=rows)
    markdown = payload["report_markdown"]
    assert markdown.index("2025-06-30 FY") < markdown.index("2026-06-30 FY") < markdown.index("2027-06-30 FY")
    assert "|2026-06-30 FY|直近通期実績|" in markdown


def test_completed_requires_both_gates(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: ("[分析] 検証済み。", None, "test"))
    gate = diligence.build_completeness(manifests=[], financials=[], offering={}, diligence={})
    assert not gate["passed"]


def test_no_images_or_base64_are_introduced_by_due_diligence_fixture():
    text = FIXTURE.read_text(encoding="utf-8")
    assert "base64" not in text.lower() and "data:image" not in text.lower()
