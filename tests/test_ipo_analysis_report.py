from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.ipo_analysis_report as report


FIXTURE = Path(__file__).parent / "fixtures" / "tdnet_20260918_0800.json"


def financial_rows(ticker: str = "627A") -> list[dict]:
    values = {
        ("2025-12-31", "FY", "official_pdf"): {"sales": 3828, "operating_profit": 201, "net_income": 223, "eps": 54.22},
        ("2026-12-31", "2Q", "official_pdf"): {"sales": 1915.622, "operating_profit": 119.344, "net_income": 104.659, "eps": 22.03},
        ("2026-12-31", "FY", "tdnet_forecast"): {"sales": 4300, "operating_profit": 340, "net_income": 302, "eps": 56.9},
    }
    rows = []
    for (period, quarter, source), metrics in values.items():
        for metric, value in metrics.items():
            rows.append({"ticker": ticker, "period": period, "quarter": quarter, "metric": metric,
                         "value": value, "source": source,
                         "source_row_key": f"cf|{ticker}|{period}|{quarter}|{metric}|{source}|doc"})
    return rows


def manifests() -> list[dict]:
    return [
        {"source_id": "P01", "title": "上場に伴う当社決算情報等のお知らせ", "url": "https://example.test/p01.pdf",
         "issuer": "akippa", "document_id": "d1", "published_at": "2026-09-18 08:00",
         "version_relation": "revision-1", "fetch_status": "success", "sha256": "a" * 64,
         "page_count": 10, "used_pages": [1]},
        {"source_id": "P02", "title": "会社説明及び今後の戦略概要", "url": "https://example.test/p02.pdf",
         "issuer": "akippa", "document_id": "d2", "published_at": "2026-09-18 08:00",
         "version_relation": "revision-1", "fetch_status": "success", "sha256": "b" * 64,
         "page_count": 20, "used_pages": []},
        {"source_id": "P03", "title": "新規上場会社情報", "url": "https://example.test/p03",
         "issuer": "JPX", "document_id": "d3", "published_at": "2026-09-18",
         "version_relation": "current", "fetch_status": "success", "sha256": "c" * 64,
         "page_count": None, "used_pages": []},
    ]


def offering() -> dict:
    return {"market": "スタンダード", "offering_price": 570, "public_offering_shares": 378000,
            "secondary_shares": 1833100, "oa_shares": 331600, "post_listing_shares": 6262140}


def test_real_627_fixture_bundles_three_official_tdnet_documents():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    selected = [row for row in rows if report.normalize_ticker(row["Code"]) == "627A" and report.is_related_source_title(row["Title"])]
    assert {row["DiscNo"] for row in selected} == {"20260915536640", "20260915536687", "20260915536689"}


def test_tdnet_alphanumeric_code_is_normalized():
    assert report.normalize_ticker("627A0") == "627A"


def test_same_time_unrelated_disclosures_are_not_mixed():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    selected = [row["Title"] for row in rows if report.normalize_ticker(row["Code"]) == "620A" and report.is_related_source_title(row["Title"])]
    assert "流動性プロバイダー指定のお知らせ" not in selected
    assert len(selected) == 3


@pytest.mark.parametrize("value,expected", [("△1,234", -1234), ("(12.5)", -12.5), ("1,234", 1234)])
def test_signed_number_notation(value, expected):
    assert report.parse_number(value) == expected


@pytest.mark.parametrize("value,expected", [(570, "570"), (378000, "378,000"), (1833100, "1,833,100")])
def test_integer_formatting_preserves_significant_trailing_zeroes(value, expected):
    assert report._fmt(value, 0) == expected


@pytest.mark.parametrize("value,unit,expected", [(2, "億円", 200_000_000), (2, "百万円", 2_000_000), (2, "千円", 2_000), (2, "千株", 2_000)])
def test_units_are_normalized(value, unit, expected):
    assert report.to_base_units(value, unit) == expected


def test_627_offering_calculations_match_official_inputs():
    calculations = {row["name"]: row for row in report.calculate_offering(offering())}
    assert calculations["public_shares_including_oa"]["value"] == 2_542_700
    assert calculations["absorption_amount_jpy"]["value"] == 1_449_339_000
    assert calculations["market_cap_jpy"]["value"] == 3_569_419_800
    assert calculations["public_float_ratio_pct"]["value"] == pytest.approx(40.6043301491)


def test_calculation_keeps_formula_inputs_and_unrounded_value():
    row = report.calculate_offering(offering())[0]
    assert row["formula"] and row["inputs"] and row["unrounded_value"] == row["value"]


def test_missing_offering_inputs_generate_no_guesses():
    assert report.calculate_offering({"offering_price": 570}) == []


def test_actual_cumulative_and_forecast_remain_distinct():
    periods = report.group_financials(financial_rows())
    assert {(row["period"], row["quarter"], row["kind"]) for row in periods} == {
        ("2025-12-31", "FY", "actual"), ("2026-12-31", "2Q", "actual"), ("2026-12-31", "FY", "forecast")}


def test_no_fictional_quarter_is_created_when_absent():
    rows = [row for row in financial_rows("624A") if row["quarter"] == "FY"]
    assert all(period["quarter"] == "FY" for period in report.group_financials(rows))


def test_stable_key_contains_required_identity():
    assert report.stable_key("627A0", "2026-09-18") == "627A|2026-09-18|ipo_analysis|v1"


def test_report_id_is_idempotent():
    key = report.stable_key("627A", "2026-09-18")
    assert report.stable_report_id(key) == report.stable_report_id(key)


def test_compact_report_hides_empty_and_internal_sections(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: ("[分析] 一次資料の範囲で事業を確認しました。", None, "test-model"))
    payload = report.build_report_payload(event_id=str(report.uuid.uuid4()), ticker="627A", company_name="アキッパ",
                                          listing_date="2026-09-18", manifests=manifests(), offering=offering(),
                                          financial_rows=financial_rows())
    markdown = payload["report_markdown"]
    assert markdown.startswith("## 01 基本情報")
    assert "## 05 " not in markdown and "## 15 " not in markdown and "## 19 " not in markdown
    assert "## 目次" not in markdown and "作成日時:" not in markdown
    assert all(label not in markdown for label in ("[確認済み]", "[未確認]", "[計算値]", "[分析]"))
    assert payload["validation_result"]["checks"]["compact_display_policy"]


def test_source_shortage_results_in_partial_without_guess(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: (None, "AI unavailable", "test-model"))
    payload = report.build_report_payload(event_id=str(report.uuid.uuid4()), ticker="624A", company_name="かがやきHD",
                                          listing_date="2026-09-18", manifests=manifests()[:1], offering={},
                                          financial_rows=[])
    assert payload["status"] == "partial"
    assert "未確認" not in payload["report_markdown"]
    assert "PARSER_UNSUPPORTED" not in payload["report_markdown"]
    assert payload["report_markdown"].strip().endswith("上場日: 2026年9月18日")


def test_every_markdown_citation_resolves(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: ("[分析] 検証済みです。", None, "test-model"))
    payload = report.build_report_payload(event_id=str(report.uuid.uuid4()), ticker="627A", company_name="アキッパ",
                                          listing_date="2026-09-18", manifests=manifests(), offering=offering(),
                                          financial_rows=financial_rows())
    assert payload["validation_result"]["checks"]["all_citations_resolve"]


def test_validator_rejects_unknown_citation():
    result = report.validate_report("\n".join([f"## {title}" for title in report.SECTION_TITLES]) + "\n[P99 p.1]", manifests(), [], [])
    assert not result["passed"]


def test_validator_rejects_embedded_images():
    body = "\n".join([f"## {title}" for title in report.SECTION_TITLES]) + "\ndata:image/png;base64,AAA"
    assert "embedded_image_forbidden" in report.validate_report(body, manifests(), [], [])["errors"]


def test_storage_payload_contains_no_raw_pdf_text_or_images(monkeypatch):
    monkeypatch.setattr(report, "generate_ai_explanation", lambda facts: ("[分析] 検証済みです。", None, "test-model"))
    payload = report.build_report_payload(event_id=str(report.uuid.uuid4()), ticker="627A", company_name="アキッパ",
                                          listing_date="2026-09-18", manifests=manifests(), offering=offering(),
                                          financial_rows=financial_rows())
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "data:image" not in serialized and "base64" not in serialized.lower()
    assert report.payload_storage_bytes(payload) < len(serialized.encode("utf-8"))


def test_enqueue_does_not_touch_read_state_or_pl(monkeypatch):
    calls = []
    monkeypatch.setattr(report, "_rest_get", lambda table, params: [] if table == "ipo_analysis_reports" else [{"id": "event-id"}])
    monkeypatch.setattr("lib.pipeline.db.supabase_upsert", lambda table, row, **kwargs: calls.append((table, row)) or {"ok": True})
    item = {"ticker": "627A0", "published_at": "2026-09-18 08:00", "company_name": "アキッパ", "doc_url": "https://example.test/a.pdf"}
    report.ensure_pending_report(item)
    assert [table for table, _ in calls] == ["ipo_analysis_reports"]


def test_existing_report_is_not_downgraded_to_pending(monkeypatch):
    monkeypatch.setattr(report, "_rest_get", lambda table, params: [{"id": "same", "status": "partial"}])
    result = report.ensure_pending_report({"ticker": "627A", "published_at": "2026-09-18", "company_name": "アキッパ"})
    assert result == {"ok": True, "action": "existing", "id": "same", "status": "partial"}


def test_all_source_manifest_ids_are_dynamic_and_unique():
    assert [source["source_id"] for source in manifests()] == ["P01", "P02", "P03"]


def test_tpm_source_discovery_uses_document_type_headers_without_ticker_hardcode():
    class Response:
        def __init__(self, text):
            self.text = text
            self.content = text.encode("utf-8")
            self.encoding = "utf-8"
            self.apparent_encoding = "utf-8"

        def raise_for_status(self):
            return None

    class Session:
        def get(self, url, timeout=60):
            if "jpx.co.jp" in url:
                return Response("""<table><tr><th>上場日</th><th>銘柄名</th><th>コード</th><th>申請日</th>
                    <th>特定証券情報</th><th>概要等</th><th>CG報告書</th><th>代表インタビュー</th></tr>
                    <tr><td rowspan='2'>2026/09/18</td><td>テスト社</td><td>620A</td><td>2026/08/14</td>
                    <td><a href='/specific.pdf'>PDF</a></td><td><a href='/outline.pdf'>PDF</a></td>
                    <td><a href='/cg.pdf'>PDF</a></td><td></td></tr>
                    <tr><td>J-Adviser</td><td>2026/08/28</td><td><a href='/declaration.pdf'>PDF</a></td>
                    <td><a href='/articles.pdf'>PDF</a></td></tr></table>""")
            return Response("<table><tr><td>別会社</td><td>9999</td></tr></table>")

    sources = report.collect_exchange_listing_sources("620A", Session())
    titles = [row["title"] for row in sources]
    assert any("特定証券情報（発行者情報）" in title for title in titles)
    assert any("新規上場会社概要" in title for title in titles)
    assert any("J-Adviser宣誓書" in title for title in titles)
    assert any("定款" in title for title in titles)


def test_ai_numeric_output_is_rejected(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"output_text": "[分析] 売上は123です。"}
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(report.requests, "post", lambda *args, **kwargs: Response())
    text, error, _ = report.generate_ai_explanation({"company_name": "例"})
    assert text is None and "numeric" in error


def test_ifrs_and_consolidation_are_not_inferred():
    markdown = report.render_report(ticker="627A", company_name="アキッパ", listing_date="2026-09-18",
                                    market="スタンダード", status="partial", manifests=manifests(),
                                    financials=[], offering={}, calculations=[], ai_text=None,
                                    missing=["会計基準"], generated_at="2026-09-18T10:00:00+09:00")
    assert "連結／単体" not in markdown
    assert "会計基準" not in markdown
    assert "資料上確認できず" not in markdown


def test_financial_tables_show_only_four_metrics_and_one_unit_note():
    payload = report.build_report_payload(event_id="e", ticker="627A", company_name="アキッパ",
                                          listing_date="2026-09-18", manifests=manifests(), offering=offering(),
                                          financial_rows=financial_rows())
    markdown = payload["report_markdown"]
    assert markdown.count("|期間|区分|売上高|営業利益|純利益|EPS|") == 2
    assert "売上総利益" not in markdown and "経常利益" not in markdown
    assert "未確認百万円" not in markdown
    assert "|2025/12期|直近通期実績|3,828|201|223|54.22|" in markdown


def test_sources_section_contains_only_used_human_readable_sources():
    payload = report.build_report_payload(event_id="e", ticker="627A", company_name="アキッパ",
                                          listing_date="2026-09-18", manifests=manifests(), offering=offering(),
                                          financial_rows=financial_rows())
    markdown = payload["report_markdown"]
    assert "## 20 出典" in markdown
    assert "上場に伴う当社決算情報等のお知らせ" in markdown
    assert "会社説明及び今後の戦略概要" not in markdown
    assert "SHA-256" not in markdown and "fetch_status" not in markdown


def test_ocr_gate_is_scoped_to_scanned_documents():
    diligence = {"business_model": "公式XBRLの表記上、語句間に 空白 がある説明です。"}
    result = report.validate_report("## 01 基本情報\n- 証券コード: 622A\n", manifests(), [], [], diligence)
    assert not any(error.startswith("ocr_readability:") for error in result["errors"])
    diligence["ocr_readability"] = {"passed": False, "issues": ["japanese_intra_character_space"]}
    result = report.validate_report("## 01 基本情報\n- 証券コード: 620A\n", manifests(), [], [], diligence)
    assert any(error.startswith("ocr_readability:") for error in result["errors"])
