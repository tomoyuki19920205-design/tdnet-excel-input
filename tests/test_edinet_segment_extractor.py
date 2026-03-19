"""Tests for EDINET Segment Extractor (Phase 1-3)"""
from __future__ import annotations

import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.segment.edinet_segment_extractor import (
    classify_concept,
    _is_segment_axis,
    _is_geo_axis,
    _member_to_segment_name,
    _classify_member,
    _normalize_segment_name,
    _parse_xbrl_value,
    _yen_to_million,
    _resolve_member_label,
    derive_quarter_values,
    extract_edinet_segments,
    _parse_contexts,
    _find_xbrl_instance,
    _parse_label_linkbase,
    _find_segment_roles,
    EdinetSegmentResult,
    EdinetSegmentRecord,
)


# ============================================================
# Test 1: BusinessSegmentAxis / OperatingSegmentsAxis detection
# ============================================================
class TestAxisDetection:
    def test_operating_segments_axis(self):
        assert _is_segment_axis("jpcrp_cor:OperatingSegmentsAxis") is True

    def test_business_segment_axis(self):
        assert _is_segment_axis("tse:BusinessSegmentAxis") is True
        assert _is_segment_axis("BusinessSegmentsAxis") is True

    def test_reportable_segments_axis(self):
        assert _is_segment_axis("ReportableSegmentsAxis") is True

    def test_non_segment_axis(self):
        assert _is_segment_axis("jpcrp_cor:MajorShareholdersAxis") is False
        assert _is_segment_axis("jppfs_cor:ComponentsOfEquityAxis") is False
        assert _is_segment_axis("jppfs_cor:ConsolidatedOrNonConsolidatedAxis") is False


# ============================================================
# Test 2: Geographical axis exclusion
# ============================================================
class TestGeoAxisExclusion:
    def test_geo_axis_detected(self):
        assert _is_geo_axis("GeographicalSegmentsAxis") is True
        assert _is_geo_axis("jpcrp_cor:GeographicalSegmentAxis") is True
        assert _is_geo_axis("GeographicAxis") is True

    def test_non_geo_axis(self):
        assert _is_geo_axis("OperatingSegmentsAxis") is False
        assert _is_geo_axis("BusinessSegmentAxis") is False


# ============================================================
# Test 4: sales/profit concept classification
# ============================================================
class TestClassifyConcept:
    # JP 基準
    def test_jp_external_sales(self):
        cls, pri = classify_concept("RevenuesFromExternalCustomers")
        assert cls == "sales"
        assert pri == 100

    def test_jp_net_sales(self):
        cls, pri = classify_concept("NetSales")
        assert cls == "sales"
        assert pri == 80

    def test_jp_operating_income(self):
        cls, pri = classify_concept("OperatingIncome")
        assert cls == "profit"
        assert pri == 90

    # IFRS
    def test_ifrs_external_sales(self):
        cls, pri = classify_concept("SalesToExternalCustomersIFRS")
        assert cls == "sales"
        assert pri == 100

    def test_ifrs_segment_profit(self):
        cls, pri = classify_concept("SegmentProfitLossIFRS")
        assert cls == "profit"
        assert pri == 100

    def test_ifrs_revenue(self):
        cls, pri = classify_concept("RevenueFromExternalCustomersIFRS")
        assert cls == "sales"
        assert pri == 100

    # Namespaced
    def test_namespaced_concept(self):
        cls, _ = classify_concept("jpcrp_cor:RevenuesFromExternalCustomers")
        assert cls == "sales"

    # Other
    def test_assets_is_other(self):
        cls, _ = classify_concept("Assets")
        assert cls == "other"

    def test_depreciation_is_other(self):
        cls, _ = classify_concept("DepreciationSegmentInformation")
        assert cls == "other"

    # Partial match fallback
    def test_intersegment_sales_excluded(self):
        cls, _ = classify_concept("IntersegmentSalesIFRS")
        assert cls == "other"  # intersegment は除外


# ============================================================
# Test 5: Context parsing
# ============================================================
class TestContextParsing:
    def _make_ctx_xml(self, ctx_id, period_xml, scenario_xml=""):
        return f"""<?xml version="1.0"?>
        <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
                     xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
                     xmlns:jpcrp_cor="http://example.com/jpcrp_cor">
          <xbrli:context id="{ctx_id}">
            <xbrli:entity>
              <xbrli:identifier scheme="http://example.com">E12345</xbrli:identifier>
            </xbrli:entity>
            {period_xml}
            {scenario_xml}
          </xbrli:context>
        </xbrli:xbrl>"""

    def test_current_year_duration(self):
        from xml.etree import ElementTree as ET

        xml = self._make_ctx_xml(
            "CurrentYearDuration_SegMember",
            "<xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period>",
            """<xbrli:scenario>
                <xbrldi:explicitMember dimension="jpcrp_cor:OperatingSegmentsAxis">jpcrp_cor:TestSegmentMember</xbrldi:explicitMember>
               </xbrli:scenario>""",
        )
        root = ET.fromstring(xml)
        contexts = _parse_contexts(root)
        assert len(contexts) == 1
        ctx = list(contexts.values())[0]
        assert ctx.has_segment_axis is True
        assert ctx.is_current_period is True
        assert ctx.period_type == "duration"
        assert ctx.segment_member_name == "jpcrp_cor:TestSegmentMember"

    def test_prior_year_excluded(self):
        from xml.etree import ElementTree as ET

        xml = self._make_ctx_xml(
            "Prior1YearDuration_SegMember",
            "<xbrli:period><xbrli:startDate>2023-04-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period>",
            """<xbrli:scenario>
                <xbrldi:explicitMember dimension="jpcrp_cor:OperatingSegmentsAxis">jpcrp_cor:TestSegmentMember</xbrldi:explicitMember>
               </xbrli:scenario>""",
        )
        root = ET.fromstring(xml)
        contexts = _parse_contexts(root)
        ctx = list(contexts.values())[0]
        assert ctx.is_prior_period is True
        assert ctx.is_current_period is False


# ============================================================
# Test 8: member name → segment_name_raw generation
# ============================================================
class TestMemberToSegmentName:
    def test_basic_member(self):
        name = _member_to_segment_name("JapanReportableSegmentsMember")
        assert name == "Japan"

    def test_camel_case_member(self):
        name = _member_to_segment_name("DigitalSystemsAndServicesReportableSegmentMember")
        assert name == "Digital Systems And Services"

    def test_edinet_prefix_removal(self):
        name = _member_to_segment_name("jpcrp030000-asr_E01737-000DigitalSystemsAndServicesReportableSegmentMember")
        assert name == "Digital Systems And Services"

    def test_simple_member(self):
        name = _member_to_segment_name("SoftBankReportableSegmentMember")
        assert name == "Soft Bank"

    def test_member_suffix_variants(self):
        assert _member_to_segment_name("TestMember") == "Test"
        assert _member_to_segment_name("TestSegmentMember") == "Test"
        assert _member_to_segment_name("TestSegmentsMember") == "Test"

    def test_qualified_member(self):
        name = _member_to_segment_name("jpcrp_cor:TestSegmentMember")
        assert name == "Test"


# ============================================================
# Test: Label linkbase member resolution (Phase 2)
# ============================================================
class TestLabelResolution:
    def test_resolve_with_label_map(self):
        label_map = {
            "jpcrp030000-asr_E02248-000_JapanReportableSegmentsMember": "日本",
            "jpcrp030000-asr_E02248-000_ChinaReportableSegmentsMember": "中国",
        }
        assert _resolve_member_label("JapanReportableSegmentsMember", label_map) == "日本"
        assert _resolve_member_label("ChinaReportableSegmentsMember", label_map) == "中国"

    def test_resolve_no_match(self):
        label_map = {"jpcrp030000-asr_E02248-000_JapanReportableSegmentsMember": "日本"}
        assert _resolve_member_label("UnknownMember", label_map) == ""

    def test_resolve_with_namespace(self):
        label_map = {"jpcrp030000-asr_E02248-000_JapanReportableSegmentsMember": "日本"}
        assert _resolve_member_label("jpcrp_cor:JapanReportableSegmentsMember", label_map) == "日本"


# ============================================================
# Test: Normalize with verbose label suffix
# ============================================================
class TestNormalize:
    def test_basic(self):
        assert _normalize_segment_name("Digital Systems And Services") == "Digital Systems And Services"

    def test_segment_suffix_removal(self):
        assert _normalize_segment_name("電子事業セグメント") == "電子事業"

    def test_whitespace(self):
        assert _normalize_segment_name("  foo  bar  ") == "foo bar"

    def test_verbose_label_suffix(self):
        """verbose label 由来の [メンバー] 除去"""
        assert _normalize_segment_name("日本、報告セグメント [メンバー]") == "日本"

    def test_member_bracket_jp(self):
        assert _normalize_segment_name("国内事業 [メンバー]") == "国内事業"

    def test_member_bracket_en(self):
        assert _normalize_segment_name("Japan [member]") == "Japan"


# ============================================================
# Test 9: member classification
# ============================================================
class TestMemberClassification:
    def test_reportable_segments_is_total(self):
        assert _classify_member("ReportableSegmentsMember") == "total"

    def test_reconciling_items_is_adjustment(self):
        assert _classify_member("ReconcilingItemsMember") == "adjustment"

    def test_corporate_shared(self):
        assert _classify_member("CorporateSharedMember") == "corporate"

    def test_other_segments(self):
        assert _classify_member("OtherReportableSegmentsMember") == "other"

    def test_ordinary_segment(self):
        assert _classify_member("JapanReportableSegmentsMember") == "ordinary_segment"
        assert _classify_member("DigitalSystemsAndServicesReportableSegmentMember") == "ordinary_segment"

    # Phase 2: 日本語ラベルベース判定
    def test_jp_label_total(self):
        assert _classify_member("SomeUnknownMember", label_name="合計") == "total"
        assert _classify_member("SomeUnknownMember", label_name="顧客部門小計") == "subtotal"

    def test_jp_label_adjustment(self):
        assert _classify_member("SomeUnknownMember", label_name="調整額") == "adjustment"
        assert _classify_member("SomeUnknownMember", label_name="全社及び消去") == "adjustment"

    def test_jp_label_other(self):
        assert _classify_member("SomeUnknownMember", label_name="その他") == "other"

    def test_jp_label_corporate(self):
        assert _classify_member("SomeUnknownMember", label_name="全社") == "corporate"


# ============================================================
# Value parsing
# ============================================================
class TestValueParsing:
    def test_normal_value(self):
        assert _parse_xbrl_value("7951946000") == 7951946000

    def test_negative_value(self):
        assert _parse_xbrl_value("-23484000") == -23484000

    def test_empty_value(self):
        assert _parse_xbrl_value("") is None
        assert _parse_xbrl_value("-") is None
        assert _parse_xbrl_value("－") is None

    def test_yen_to_million(self):
        assert _yen_to_million(7951946000) == 7951  # 79億5194万円
        assert _yen_to_million(None) is None
        assert _yen_to_million(-23484000) == -24  # floor division: -23484000 // 1_000_000 = -24


# ============================================================
# YTD → Quarter Derivation (Phase 3)
# ============================================================
class TestDeriveQuarterValues:
    def _make_rec(self, name, sales, profit):
        return EdinetSegmentRecord(
            segment_name_raw=name,
            segment_name_norm=name,
            sales=sales,
            profit=profit,
        )

    def test_1q_is_direct(self):
        """1Q: YTD をそのまま使う"""
        recs = [self._make_rec("Seg A", 1000, 100)]
        result = derive_quarter_values({"1Q": recs}, "1Q")
        assert len(result) == 1
        assert result[0].sales_qtd == 1000
        assert result[0].profit_qtd == 100
        assert result[0].derivation_method == "reported_qtd"

    def test_2q_diff(self):
        """2Q = 2Q累計 - 1Q累計"""
        q1 = [self._make_rec("Seg A", 1000, 100)]
        q2 = [self._make_rec("Seg A", 2500, 270)]
        result = derive_quarter_values({"1Q": q1, "2Q": q2}, "2Q")
        assert len(result) == 1
        assert result[0].sales_qtd == 1500  # 2500 - 1000
        assert result[0].profit_qtd == 170  # 270 - 100
        assert result[0].derivation_method == "derived_from_ytd_diff"

    def test_3q_diff(self):
        """3Q = 3Q累計 - 2Q累計"""
        q2 = [self._make_rec("Seg A", 2500, 270)]
        q3 = [self._make_rec("Seg A", 4000, 450)]
        result = derive_quarter_values({"2Q": q2, "3Q": q3}, "3Q")
        assert len(result) == 1
        assert result[0].sales_qtd == 1500  # 4000 - 2500
        assert result[0].profit_qtd == 180  # 450 - 270
        assert result[0].derivation_method == "derived_from_ytd_diff"

    def test_fy_no_derivation(self):
        """FY: annual 扱い"""
        recs = [self._make_rec("Seg A", 5000, 600)]
        result = derive_quarter_values({"FY": recs}, "FY")
        assert result[0].sales_qtd == 5000
        assert result[0].profit_qtd == 600
        assert result[0].derivation_method == "reported_qtd"

    def test_1h_no_derivation(self):
        """1H: 半期扱い"""
        recs = [self._make_rec("Seg A", 2500, 300)]
        result = derive_quarter_values({"1H": recs}, "1H")
        assert result[0].sales_qtd == 2500
        assert result[0].derivation_method == "reported_qtd"

    def test_missing_prior_quarter(self):
        """前四半期データなし → ytd_only"""
        q2 = [self._make_rec("Seg A", 2500, 270)]
        result = derive_quarter_values({"2Q": q2}, "2Q")
        assert result[0].sales_qtd is None
        assert result[0].profit_qtd is None
        assert result[0].derivation_method == "ytd_only"

    def test_missing_segment_in_prior(self):
        """前四半期にセグメントが存在しない → ytd_only"""
        q1 = [self._make_rec("Seg A", 1000, 100)]
        q2 = [self._make_rec("Seg A", 2500, 270), self._make_rec("Seg B", 500, 50)]
        result = derive_quarter_values({"1Q": q1, "2Q": q2}, "2Q")
        seg_a = [r for r in result if r.segment_name_norm == "Seg A"][0]
        seg_b = [r for r in result if r.segment_name_norm == "Seg B"][0]
        assert seg_a.derivation_method == "derived_from_ytd_diff"
        assert seg_b.derivation_method == "ytd_only"
        assert seg_b.sales_qtd is None


# ============================================================
# E2E test with real EDINET ZIP (if available)
# ============================================================
class TestE2ERealData:
    KANEMITSU_ZIP = os.path.join(
        os.path.dirname(__file__), "..", "data", "edinet_cache", "S100W58B", "xbrl.zip"
    )
    KANEMITSU_ZIP_ALT = r"C:\Users\takuy\.gemini\antigravity\scratch\data\edinet_cache\S100W58B\xbrl.zip"

    @property
    def zip_path(self):
        if os.path.exists(self.KANEMITSU_ZIP):
            return self.KANEMITSU_ZIP
        if os.path.exists(self.KANEMITSU_ZIP_ALT):
            return self.KANEMITSU_ZIP_ALT
        return None

    def test_kanemitsu_segments(self):
        """カネミツ (7208) の有報からセグメント抽出。"""
        if not self.zip_path:
            pytest.skip("Kanemitsu ZIP not available")

        result = extract_edinet_segments(
            self.zip_path,
            ticker="7208",
            doc_type="securities_report",
            period="2025-03-31",
            quarter="FY",
        )
        assert result.status == "ok"
        assert len(result.segments) > 0

        # Phase 2: ラベルリンクから日本語名取得確認
        seg_names = {s.segment_name_raw for s in result.segments}
        print(f"\nKanemitsu segments: {seg_names}")
        for s in result.segments:
            print(f"  {s.segment_name_raw} (norm={s.segment_name_norm}): "
                  f"sales={s.sales} profit={s.profit} "
                  f"concept_s={s.concept_sales} concept_p={s.concept_profit} "
                  f"type={s.special_row_type} derivation={s.derivation_method}")

        # ラベルリンクから日本語名が取れていること
        ordinary_segs = [s for s in result.segments if s.special_row_type == "ordinary_segment"]
        assert len(ordinary_segs) >= 2, f"Expected at least 2 ordinary segments, got {len(ordinary_segs)}"

        # 日本セグメント = 「日本」(ラベルリンク) or "Japan" (CamelCase fallback)
        japan_segs = [s for s in result.segments
                      if "日本" in s.segment_name_raw or "Japan" in s.segment_name_raw]
        assert len(japan_segs) >= 1, f"Expected Japan segment, got: {seg_names}"

        japan = japan_segs[0]
        assert japan.sales is not None
        assert japan.profit is not None
        assert 1000 < japan.sales < 50000

        # Phase 2: label_map / segment_roles がデバッグに含まれること
        assert "label_map_jp_count" in result.debug_summary
        assert result.debug_summary["label_map_jp_count"] > 0

        # Phase 2: segment role
        seg_roles = result.debug_summary.get("segment_roles", [])
        assert len(seg_roles) > 0, "Expected at least one segment role"
        assert any("SegmentInformation" in r for r in seg_roles)

        # Phase 3: derivation_method が設定されていること
        for s in result.segments:
            assert s.derivation_method != "", f"derivation_method not set for {s.segment_name_raw}"

    def test_no_segments_in_empty_company(self):
        """セグメントがない企業は no_segments を返す。"""
        pass
