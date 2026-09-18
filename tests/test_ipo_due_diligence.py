from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile

import pytest

import src.ipo_analysis_report as report
import src.ipo_due_diligence as diligence


FIXTURE = Path(__file__).parent / "fixtures" / "ipo_627_edinet_excerpt.html"
FIXTURES = Path(__file__).parent / "fixtures"


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


def test_current_option_section_excludes_historical_financial_note_tables():
    current = """
    <html><body><p>第８回新株予約権</p><table>
      <tr><td>決議年月日</td><td>2024年11月15日</td></tr>
      <tr><td>新株予約権の目的となる株式の種類、内容及び数（株）</td><td>普通株式 709,893株</td></tr>
      <tr><td>新株予約権の行使時の払込金額（円）</td><td>550円</td></tr>
      <tr><td>新株予約権の行使期間</td><td>2026年11月30日～2034年11月14日</td></tr>
    </table></body></html>"""
    historical = """
    <html><body><table>
      <tr><th></th><th>第１回新株予約権（ストック・オプション）</th></tr>
      <tr><td>株式の種類別のストック・オプションの数</td><td>普通株式 231,000株</td></tr>
      <tr><td>権利行使価格（円）</td><td>287円</td></tr>
      <tr><td>権利行使期間</td><td>2017年2月28日～2025年2月23日</td></tr>
      <tr><td>失効</td><td>139,200</td></tr>
    </table></body></html>"""
    adjustment = """
    <html><body><p>第８回新株予約権については、退職による権利の喪失により、
    発行数は699,393株となっております。</p></body></html>"""
    data = BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("XBRL/PublicDoc/0204010_fixture_ixbrl.htm", current)
        archive.writestr("XBRL/PublicDoc/0205400_fixture_ixbrl.htm", historical)
        archive.writestr("XBRL/PublicDoc/0402010_fixture_ixbrl.htm", adjustment)
    rows = diligence.extract_stock_options(diligence._html_documents(data.getvalue()), "P01", b"")
    assert [(row["series"], row["forfeited_shares"], row["effective_potential_shares"]) for row in rows] == [
        ("第8回新株予約権", 10_500, 699_393),
    ]


def test_shareholder_header_whitespace_is_normalized():
    documents = html_documents("""
    <html><body><table><tr><th>氏名又は名称</th><th>住所</th><th>所有株式数</th><th>割合</th>
      <th>売出し後の 所有株式数</th><th>割合</th></tr>
      <tr><td>株主A</td><td>東京都</td><td>1,000</td><td>10</td><td>600</td><td>6</td></tr>
    </table></body></html>""")
    rows = diligence.extract_shareholders(documents, "P02", b"")
    assert rows[0]["sold_or_allotted_shares"] == 400


def test_627_bs_cf_are_kept_in_four_period_statement_groups():
    rows = diligence.extract_financial_facts(fixture_documents(), "P01", b"")
    by_key = {(row["as_of_date"], row["statement_type"], row["metric_name"]): row for row in rows}
    assert by_key[("2025-12-31", "BS", "total_assets_million_yen")]["value_million_yen"] == pytest.approx(1050.213)
    assert by_key[("2025-12-31", "CF", "operating_cf_million_yen")]["value_million_yen"] == pytest.approx(237.729)
    assert by_key[("2026-06-30", "BS", "total_assets_million_yen")]["value_million_yen"] == pytest.approx(1126.555)
    assert by_key[("2026-06-30", "CF", "operating_cf_million_yen")]["value_million_yen"] == pytest.approx(71.774)
    assert {row["source_page"] for row in rows} == {24}
    required = {"period_start", "period_end", "as_of_date", "period_type", "fiscal_year", "quarter",
                "consolidation_scope", "accounting_standard", "source_id", "source_page"}
    assert all(required <= row.keys() for row in rows)


def test_627_kpi_units_and_periods_are_preserved():
    facts = diligence.extract_kpis_and_narratives(fixture_documents(), "P01", b"")
    rows = facts["kpis"]
    cumulative = next(row for row in rows if row["metric_name_original"] == "累計ユーザー会員数")
    assert (cumulative["value_original"], cumulative["unit_original"], cumulative["value_normalized"]) == ("500", "万人", 5_000_000)
    monthly = next(row for row in rows if row["unit_original"] == "万台")
    assert monthly["value_normalized"] == 55_000
    annual = next(row for row in rows if row["metric_name_normalized"] == "期間ユニーク利用者数" and row["period_end"] == "2025-12-31")
    interim = next(row for row in rows if row["metric_name_normalized"] == "期間ユニーク利用者数" and row["period_end"] == "2026-06-30")
    assert (annual["value_normalized"], interim["value_normalized"]) == (993_000, 606_000)
    assert any(row["unit_original"] == "台" and row["value_normalized"] == 55_020 for row in rows)
    assert any(row["unit_original"] == "件" and row["value_normalized"] == 58_870 for row in rows)
    required = {"metric_name_original", "metric_name_normalized", "value_original", "unit_original",
                "value_normalized", "unit_normalized", "as_of_date", "period_start", "period_end",
                "cumulative_or_period", "definition_note", "source_id", "source_page"}
    assert all(required <= row.keys() for row in rows)
    assert facts["customer_concentration"] == "総販売実績の10%以上を占める相手先なし"


def test_final_correction_offering_table_oa_lenders_and_greenshoe_terms():
    documents = html_documents("""
    <html><body><table>
      <tr><th>発行価格 (円)</th><th>引受価額 (円)</th><th>払込金額 (円)</th><th>資本 組入額 (円)</th></tr>
      <tr><td>570</td><td>524.40</td><td>459</td><td>262.20</td></tr>
    </table><p>差引手取概算額179,713千円については、駐車場マーケットプレイスの競争力強化及び顧客基盤の拡大を目的として充当する予定であります。プロダクト開発費用として179,713千円（2026年12月期：40,000千円、2027年12月期：70,000千円、2028年12月期：69,713千円）を充当する予定であります。</p>
    <p>グリーンシューオプションとシンジケートカバー取引について オーバーアロットメントによる売出しのために、主幹事会社が当社株主である株主A及び株主B(以下「貸株人」という。)より借入れる株式であります。主幹事会社は、331,600株について貸株人より追加的に当社株式を取得する権利(以下「グリーンシューオプション」という。)を、2026年10月16日を行使期限として貸株人より付与されております。主幹事会社は、2026年９月18日から2026年10月16日までの間、貸株人から借入れる株式の返却を目的として、シンジケートカバー取引を行う場合があります。</p>
    </body></html>""")
    terms = diligence.extract_offering_terms(documents, "P03", b"")
    assert terms["capital_per_share"] == pytest.approx(262.20)
    assert terms["oa_lenders"] == "株主A及び株主B"
    assert terms["greenshoe_shares"] == 331_600
    assert terms["greenshoe_exercise_deadline"] == "2026年10月16日"
    assert terms["syndicate_cover_period"] == "2026年９月18日から2026年10月16日までの間"
    assert terms["proceeds_use"].startswith("駐車場マーケットプレイス")


def test_corrected_offering_terms_overlay_initial_without_losing_unchanged_fields():
    merged = diligence.merge_offering_term_versions(
        {"source_id": "P01", "pdf_page": 10, "offering_price": 500,
         "underwriting_price": 460, "company_law_payment_price": 400,
         "capital_per_share": 230, "net_proceeds_thousand_yen": 100_000},
        {"source_id": "P03", "pdf_page": 2, "offering_price": 570,
         "underwriting_price": 524.4},
    )
    assert merged["offering_price"] == 570
    assert merged["capital_per_share"] == 230
    assert merged["field_sources"]["offering_price"] == {"source_id": "P03", "pdf_page": 2}
    assert merged["field_sources"]["capital_per_share"] == {"source_id": "P01", "pdf_page": 10}


def test_offering_terms_parse_proceeds_table_and_both_oa_structures(monkeypatch):
    monkeypatch.setattr(diligence, "locate_pdf_page_by_values", lambda *args, **kwargs: 12)
    documents = html_documents("""
    <html><body><table>
      <tr><th>払込金額の総額（円）</th><th>発行諸費用（円）</th><th>差引手取概算額（円）</th></tr>
      <tr><td>100,000,000</td><td>5,000,000</td><td>95,000,000</td></tr>
    </table>
    <p>オーバーアロットメントによる売出しのため、主幹事会社が当社株主である株主A（以下「貸株人」という。）より借入れる株式であります。</p>
    <p>また、主幹事会社は、2026年9月18日から2026年10月16日までの間、貸株人から借入れる株式の返還を目的として、シンジケートカバー取引を行う場合があります。</p>
    </body></html>""")
    terms = diligence.extract_offering_terms(documents, "P03", b"")
    assert terms["net_proceeds_thousand_yen"] == 95_000
    assert terms["oa_lenders"] == "株主A"
    assert terms["syndicate_cover_period"] == "2026年9月18日から2026年10月16日までの間"
    assert terms["greenshoe_option_applicable"] is False

    greenshoe = html_documents("""
    <html><body><p>オーバーアロットメントによる売出しのため、主幹事会社が当社株主である株主B（以下「貸株人」という。）より借り入れる当社普通株式（以下「借入株式」という。）288,900株の売出しを行います。</p>
    <p>グリーンシューオプションを、2026年10月16日を行使期限として付与します。</p>
    <p>上場（売買開始）日から2026年10月16日までの間、シンジケートカバー取引を行います。</p></body></html>
    """)
    terms = diligence.extract_offering_terms(greenshoe, "P04", b"")
    assert terms["greenshoe_shares"] == 288_900
    assert terms["greenshoe_exercise_deadline"] == "2026年10月16日"
    assert terms["greenshoe_option_applicable"] is True


def test_deterministic_report_rejects_generic_ai_prose(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: ("一般論だけの分析", None, "test"))
    manifests = [{"source_id": "P01", "title": "上場に伴う決算情報等のお知らせ", "url": "https://example.test/p.pdf",
                  "fetch_status": "success", "sha256": "a" * 64, "used_pages": [], "page_count": 1}]
    payload = report.build_report_payload(event_id="e", ticker="627A", company_name="アキッパ", listing_date="2026-09-18",
                                          manifests=manifests, offering={}, financial_rows=[])
    assert "一般論だけの分析" not in payload["report_markdown"]


def test_business_model_prefers_operating_model_over_platform_dependency_risk():
    documents = html_documents("""
    <html><body>
      <p>当社はアプリを中心にサービスを展開しているため、外部プラットフォームへの依存リスクがあります。</p>
      <p>当社は駐車場を利用したいユーザーと空きスペースを提供するオーナーをマッチングし、自ら駐車場資産を保有せずに駐車場マーケットプレイスを運営しております。</p>
      <p>競合他社の参入により競争が激化した場合、経営成績に影響を及ぼす可能性があります。</p>
      <p>AIカメラを用いた予約不要なキャッシュレス駐車場の実証実験を行っています。</p>
    </body></html>""")
    facts = diligence.extract_kpis_and_narratives(documents, "P01", b"")
    assert "ユーザー" in facts["business_model"] and "オーナー" in facts["business_model"]
    assert "競合他社" in facts["risk_excerpt"]
    assert "AIカメラ" in facts["technology_excerpt"]
    assert facts["single_segment"] is None


def test_single_segment_name_is_taken_from_source_without_company_hardcode():
    documents = html_documents("""
    <html><body><p>当社は不動産サービスを運営し、「不動産事業」の単一セグメントとして事業展開しております。</p></body></html>
    """)
    facts = diligence.extract_kpis_and_narratives(documents, "P01", b"")
    assert facts["single_segment"] == "「不動産事業」の単一セグメント"
    assert "アキッパ" not in facts["single_segment"]


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
    assert markdown.index("2025/06期") < markdown.index("2026/06期") < markdown.index("2027/06期")
    assert "|2026/06期|直近通期実績|" in markdown


def test_completed_requires_both_gates(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: ("[分析] 検証済み。", None, "test"))
    gate = diligence.build_completeness(manifests=[], financials=[], offering={}, diligence={})
    assert not gate["passed"]


def test_integrity_gate_rejects_period_mixing_and_kpi_multiplier_errors():
    markdown = "\n".join(f"## {title}" for title in report.SECTION_TITLES)
    manifests = [{"source_id": "P01", "title": "official", "url": "https://example.test/p.pdf"}]
    diligence_data = {
        "financial_facts": [{
            "metric_name": "total_assets_million_yen", "value_million_yen": 1,
            "period_start": "2025-01-01", "period_end": "2025-12-31", "as_of_date": "2026-06-30",
            "period_type": "full_year", "fiscal_year": 2025, "quarter": "FY",
            "consolidation_scope": "consolidated", "accounting_standard": "J-GAAP",
            "source_id": "P01", "source_page": 10, "statement_type": "BS",
        }],
        "kpis": [{
            "metric_name_original": "掲載駐車場数", "metric_name_normalized": "掲載駐車区画数",
            "value_original": "5.5", "unit_original": "万台", "value_normalized": 5_500,
            "unit_normalized": "台", "as_of_date": "2025-12-31", "period_start": None,
            "period_end": None, "cumulative_or_period": "as_of", "definition_note": "月間平均",
            "source_id": "P01", "source_page": 20,
        }],
    }
    gate = report.validate_report(markdown, manifests, [], [], diligence_data)
    assert not gate["passed"]
    assert "balance_sheet_period_mismatch:0" in gate["errors"]
    assert "kpi_unit_multiplier_mismatch:0" in gate["errors"]


def test_no_images_or_base64_are_introduced_by_due_diligence_fixture():
    text = FIXTURE.read_text(encoding="utf-8")
    assert "base64" not in text.lower() and "data:image" not in text.lower()


def test_620_tpm_scanned_primary_document_uses_generic_ocr_parser():
    text = (FIXTURES / "ipo_620_tpm_ocr_excerpt.txt").read_text(encoding="utf-8")
    facts = diligence.extract_exchange_text_due_diligence(text, "P02")
    finance = {(row["statement_type"], row["metric_name"]): row for row in facts["financial_facts"]}
    assert finance[("BS", "total_assets_million_yen")]["value_million_yen"] == 18_798
    assert finance[("BS", "net_assets_million_yen")]["value_million_yen"] == 10_980
    assert finance[("CF", "operating_cf_million_yen")]["value_million_yen"] == 1_513
    assert finance[("CF", "investing_cf_million_yen")]["value_million_yen"] == -1_376
    assert [row["after_shares"] for row in facts["shareholders"]] == [2_600_000, 1_980_000, 10_000, 10_000]
    assert {row["value_normalized"] for row in facts["kpis"]} == {122.9, 110.4, 101.5}
    assert facts["absence_evidence"]["stock_options"]["reason_code"] == "SOURCE_ACTUALLY_ABSENT"
    assert facts["risk_excerpt"]
    assert facts["business_model"] == (
        "財務会計、人事労務、販売管理、顧客管理などの基幹業務ソフトブランド「大臣シリーズ」を開発しています。"
        "パッケージソフトとクラウドサービスを、全国の販売代理店網を通じて企業へ提供する間接販売モデルを主軸としています。"
    )
    assert facts["ocr_readability"] == {"passed": True, "issues": []}
    assert "シ・リ・ズ" not in facts["business_model"] and "フ・ランド" not in facts["business_model"]


def test_ocr_readability_gate_rejects_raw_split_japanese_and_broken_katakana():
    raw = "財 務 会 計 ソ フ トの大 臣 シ・リ・ズを提供します 。"
    assert {"japanese_intra_character_space", "broken_katakana", "space_before_punctuation"} <= set(
        diligence.ocr_readability_issues(raw))
    normalized = diligence.normalize_ocr_japanese(raw)
    assert normalized == "財務会計ソフトの大臣シリーズを提供します。"
    assert diligence.ocr_readability_issues(normalized) == []


def test_622_separate_ownership_and_seller_tables_are_joined_deterministically():
    documents = html_documents((FIXTURES / "ipo_622_latest_shareholders_excerpt.html").read_text(encoding="utf-8"))
    rows = diligence.extract_shareholders(documents, "P03", b"")
    by_name = {row["name"]: row for row in rows}
    assert by_name["P&C株式会社"]["after_shares"] == 1_691_600
    assert by_name["新生TC成長支援投資事業有限責任組合"]["after_shares"] == 0
    assert diligence.extract_lockups(documents, "P03", b"")[0]["days"] == 180


def test_623_kpi_prose_preserves_original_units_dates_and_definitions():
    documents = html_documents((FIXTURES / "ipo_623_kpi_excerpt.html").read_text(encoding="utf-8"))
    rows = diligence.extract_kpis_and_narratives(documents, "P01", b"")["kpis"]
    assert any(row["metric_name_original"] == "入居率" and row["value_normalized"] == 99.8 and row["as_of_date"] == "2026-06-30" for row in rows)
    assert any(row["metric_name_original"] == "賃貸管理戸数" and row["value_normalized"] == 5_259 and row["unit_original"] == "戸" for row in rows)
    assert any(row["metric_name_original"] == "自社ブランドマンション供給戸数" and row["value_normalized"] == 336 for row in rows)
    latest_transactions = next(row for row in rows if row["metric_name_normalized"] == "取引件数" and row["period_end"] == "2025-11-30")
    assert latest_transactions["value_normalized"] == 1_026
    assert latest_transactions["unit_original"] == "戸"
    assert latest_transactions["yoy_pct"] == pytest.approx((1_026 / 846 - 1) * 100)


def test_623_vertical_stock_option_table_accepts_spaced_bracket_values(monkeypatch):
    documents = html_documents((FIXTURES / "ipo_623_kpi_excerpt.html").read_text(encoding="utf-8"))
    monkeypatch.setattr(diligence, "locate_pdf_page_by_values", lambda *args, **kwargs: 54)
    rows = diligence.extract_stock_options(documents, "P09", b"")
    assert len(rows) == 1
    assert rows[0]["series"] == "第1回新株予約権"
    assert rows[0]["effective_potential_shares"] == 72_300
    assert rows[0]["exercise_price_yen"] == 1
    assert rows[0]["pdf_page"] == 54


def test_624_kpi_prose_preserves_conversion_rate_definition():
    documents = html_documents((FIXTURES / "ipo_624_kpi_excerpt.html").read_text(encoding="utf-8"))
    facts = diligence.extract_kpis_and_narratives(documents, "P01", b"")
    rows = facts["kpis"]
    row = next(item for item in rows if item["metric_name_original"] == "トスアップによる成約率")
    assert row["value_normalized"] == 44.8
    assert row["as_of_date"] == "2026-04-30"
    assert "成約" in row["definition_note"]
    assert {item["metric_name_original"] for item in facts["kpi_definitions"]} >= {
        "付加価値総額", "従業員1人当たり付加価値", "契約件数"}
