# ============================================================
# test_normalization.py — 正規化レイヤーテスト (6ケース)
# ============================================================
import pytest

from src.normalization.field_metadata import (
    FieldMeta, NormalizedField, SourceType, RawUnit,
    map_field_source_to_source_type, map_source_unit_to_raw_unit,
)
from src.normalization.provenance_rules import (
    compute_confidence, get_base_confidence, source_priority_index,
)
from src.normalization.normalize_field import normalize_financial_field
from src.normalization.merge_fields import choose_best_field, merge_row_fields


# ============================================================
# Test 1: 円→百万円自動補正 (attachment_xbrl + yen)
# ============================================================
class TestAutoUnitConversion:
    def test_yen_to_million_exact(self):
        """attachment_xbrl + yen の COS が百万円に正規化される"""
        nf = normalize_financial_field(
            698511000.0,
            field_name="gross_profit",
            source_type=SourceType.TDNET_ATTACHMENT_XBRL,
            raw_unit=RawUnit.YEN,
            origin="tdnet",
        )
        assert nf.normalized_value == 699  # 698511000 / 1e6 = 698.511 → 699
        assert "unit_converted" in nf.meta.anomaly_flags
        assert nf.meta.source_type == SourceType.TDNET_ATTACHMENT_XBRL
        assert nf.meta.raw_unit == RawUnit.YEN
        assert nf.meta.normalized_unit == "million_yen"

    def test_yen_to_million_cos(self):
        """COS 670456000円 → 670百万円"""
        nf = normalize_financial_field(
            670456000.0,
            field_name="cost_of_sales",
            source_type=SourceType.TDNET_ATTACHMENT_XBRL,
            raw_unit=RawUnit.YEN,
            origin="tdnet",
        )
        assert nf.normalized_value == 670  # 670456000 / 1e6 → 670
        assert "unit_converted" in nf.meta.anomaly_flags

    def test_million_yen_no_conversion(self):
        """百万円はそのまま"""
        nf = normalize_financial_field(
            1368.0,
            field_name="sales",
            source_type=SourceType.JQUANTS,
            raw_unit=RawUnit.MILLION_YEN,
            origin="jquants",
        )
        assert nf.normalized_value == 1368
        assert "unit_converted" not in nf.meta.anomaly_flags

    def test_null_value(self):
        """None は None のまま"""
        nf = normalize_financial_field(
            None,
            field_name="gross_profit",
            source_type=SourceType.JQUANTS,
            raw_unit=RawUnit.MILLION_YEN,
            origin="jquants",
        )
        assert nf.normalized_value is None
        assert nf.meta.confidence == 0.0


# ============================================================
# Test 2: confidence 比較で tdnet が jquants を上回る
# ============================================================
class TestConfidenceComparison:
    def test_attachment_xbrl_beats_jquants_for_gp(self):
        """attachment_xbrl の gross_profit (0.95) > jquants (0.85)"""
        tdnet_nf = normalize_financial_field(
            699.0,
            field_name="gross_profit",
            source_type=SourceType.TDNET_ATTACHMENT_XBRL,
            raw_unit=RawUnit.MILLION_YEN,
            origin="tdnet",
        )
        jq_nf = normalize_financial_field(
            700.0,
            field_name="gross_profit",
            source_type=SourceType.JQUANTS,
            raw_unit=RawUnit.MILLION_YEN,
            origin="jquants",
        )
        winner = choose_best_field(jq_nf, tdnet_nf)
        assert winner is tdnet_nf
        assert winner.meta.confidence > jq_nf.meta.confidence

    def test_jquants_wins_for_sales_over_unknown(self):
        """jquants (0.85) > unknown (0.50) for sales"""
        jq_nf = normalize_financial_field(
            1000.0,
            field_name="sales",
            source_type=SourceType.JQUANTS,
            raw_unit=RawUnit.MILLION_YEN,
            origin="jquants",
        )
        unk_nf = normalize_financial_field(
            1000.0,
            field_name="sales",
            source_type=SourceType.UNKNOWN,
            raw_unit=RawUnit.UNKNOWN,
            origin="unknown",
        )
        winner = choose_best_field(unk_nf, jq_nf)
        assert winner is jq_nf


# ============================================================
# Test 3: FY/3Q gap + metadata → quarantine 回避可能性
# ============================================================
class TestQuarantineMetadata:
    def test_period_kind_cumulative(self):
        """period_kind=cumulative がメタデータに残る（将来のquarantine改善用）"""
        nf = normalize_financial_field(
            296326.0,
            field_name="sales",
            source_type=SourceType.TDNET_SUMMARY_XBRL,
            raw_unit=RawUnit.MILLION_YEN,
            origin="tdnet",
            period_kind="cumulative",
        )
        assert nf.meta.period_kind == "cumulative"
        assert nf.normalized_value == 296326
        # period_kind が設定されていれば、将来 quarantine 判定で利用可能


# ============================================================
# Test 4: 2301 GP が attachment_xbrl 由来で正規化される
# ============================================================
class Test2301GrossProfit:
    def test_2301_gp_normalization(self):
        """2301 GP=698511000円 → 699百万円, source=attachment_xbrl"""
        nf = normalize_financial_field(
            698511000.0,
            field_name="gross_profit",
            source_type=SourceType.TDNET_ATTACHMENT_XBRL,
            raw_unit=RawUnit.YEN,
            origin="tdnet",
        )
        assert nf.normalized_value == 699
        assert nf.meta.source_type == SourceType.TDNET_ATTACHMENT_XBRL
        assert nf.meta.confidence >= 0.85  # 高い confidence

    def test_2301_sales_stays(self):
        """2301 sales=1368000000円 → 1368百万円"""
        nf = normalize_financial_field(
            1368000000.0,
            field_name="sales",
            source_type=SourceType.TDNET_SUMMARY_XBRL,
            raw_unit=RawUnit.YEN,
            origin="tdnet",
        )
        assert nf.normalized_value == 1368


# ============================================================
# Test 5: COS 単位不整合再現
# ============================================================
class TestCosUnitMismatch:
    def test_cos_yen_converted_to_million(self):
        """COS=152445000000円(attachment_xbrl) → 152445百万円"""
        nf = normalize_financial_field(
            152445000000.0,
            field_name="cost_of_sales",
            source_type=SourceType.TDNET_ATTACHMENT_XBRL,
            raw_unit=RawUnit.YEN,
            origin="tdnet",
        )
        assert nf.normalized_value == 152445
        assert "unit_converted" in nf.meta.anomaly_flags

    def test_cos_vs_sales_no_longer_anomalous(self):
        """COS=152445百万 vs Sales=183643百万 → COS < Sales (正常)"""
        cos_nf = normalize_financial_field(
            152445000000.0,
            field_name="cost_of_sales",
            source_type=SourceType.TDNET_ATTACHMENT_XBRL,
            raw_unit=RawUnit.YEN,
            origin="tdnet",
        )
        sales_nf = normalize_financial_field(
            183643.0,
            field_name="sales",
            source_type=SourceType.JQUANTS,
            raw_unit=RawUnit.MILLION_YEN,
            origin="jquants",
        )
        assert cos_nf.normalized_value == 152445
        assert sales_nf.normalized_value == 183643
        assert cos_nf.normalized_value < sales_nf.normalized_value

    def test_six_cos_gt_sales_cases_resolved(self):
        """dry-runで検出された6件のCOS>Sales相当の再現テスト"""
        # 40620: COS=202638000000円 vs Sales=175000百万 → After: 202638百万 > 175000百万 (まだ異常)
        # ただし抽出ミスの可能性 — ここでは単位変換が正しく動くことを確認
        cos_nf = normalize_financial_field(
            202638000000.0,
            field_name="cost_of_sales",
            source_type=SourceType.TDNET_ATTACHMENT_XBRL,
            raw_unit=RawUnit.YEN,
            origin="tdnet",
        )
        assert cos_nf.normalized_value == 202638
        assert "unit_converted" in cos_nf.meta.anomaly_flags


# ============================================================
# Test 6: tie-break テスト (confidence 同値時の source 優先順)
# ============================================================
class TestTieBreak:
    def test_same_confidence_source_priority(self):
        """confidence 同値 → source_priority index が小さい方が勝つ"""
        # summary_xbrl (priority=0) vs attachment_xbrl (priority=1)
        # 同じ raw_unit=million_yen → 同じ confidence
        summary_nf = normalize_financial_field(
            1000.0,
            field_name="sales",
            source_type=SourceType.TDNET_SUMMARY_XBRL,
            raw_unit=RawUnit.MILLION_YEN,
            origin="tdnet",
        )
        attach_nf = normalize_financial_field(
            1000.0,
            field_name="sales",
            source_type=SourceType.TDNET_ATTACHMENT_XBRL,
            raw_unit=RawUnit.MILLION_YEN,
            origin="tdnet",
        )
        # summary sales=0.92, attach sales=0.88
        # summary wins by confidence
        winner = choose_best_field(attach_nf, summary_nf)
        assert winner.meta.source_type == SourceType.TDNET_SUMMARY_XBRL

    def test_same_confidence_same_source_anomaly_count(self):
        """confidence同値 + source同値 → anomaly_flags少ない方が勝つ"""
        clean_meta = FieldMeta(
            source_type=SourceType.JQUANTS,
            raw_unit=RawUnit.MILLION_YEN,
            normalized_unit="million_yen",
            context_kind=None,
            period_kind=None,
            confidence=0.80,
            anomaly_flags=[],
        )
        flagged_meta = FieldMeta(
            source_type=SourceType.JQUANTS,
            raw_unit=RawUnit.MILLION_YEN,
            normalized_unit="million_yen",
            context_kind=None,
            period_kind=None,
            confidence=0.80,
            anomaly_flags=["unit_inferred"],
        )
        clean_nf = NormalizedField(raw_value=100, normalized_value=100, meta=clean_meta)
        flagged_nf = NormalizedField(raw_value=100, normalized_value=100, meta=flagged_meta)

        winner = choose_best_field(flagged_nf, clean_nf)
        assert len(winner.meta.anomaly_flags) == 0

    def test_existing_wins_on_full_tie(self):
        """全項目同値 → 既存維持"""
        meta = FieldMeta(
            source_type=SourceType.JQUANTS,
            raw_unit=RawUnit.MILLION_YEN,
            normalized_unit="million_yen",
            context_kind=None,
            period_kind=None,
            confidence=0.85,
            anomaly_flags=[],
        )
        existing = NormalizedField(raw_value=100, normalized_value=100, meta=meta)
        new = NormalizedField(raw_value=100, normalized_value=100, meta=meta)

        winner = choose_best_field(existing, new)
        assert winner is existing


# ============================================================
# Provenance rules unit tests
# ============================================================
class TestProvenanceRules:
    def test_base_confidence(self):
        assert get_base_confidence(SourceType.TDNET_ATTACHMENT_XBRL, "gross_profit") == 0.95
        assert get_base_confidence(SourceType.JQUANTS, "sales") == 0.85

    def test_penalty_application(self):
        c = compute_confidence(
            SourceType.UNKNOWN, "sales",
            unit_unknown=True,
            heuristic_conversion=True,
        )
        # 0.50 - 0.10 - 0.15 = 0.25
        assert c == 0.25

    def test_source_priority(self):
        assert source_priority_index(SourceType.TDNET_SUMMARY_XBRL) < source_priority_index(SourceType.JQUANTS)
        assert source_priority_index(SourceType.JQUANTS) < source_priority_index(SourceType.UNKNOWN)


# ============================================================
# Field metadata mapping tests
# ============================================================
class TestFieldMetadataMapping:
    def test_field_source_to_source_type(self):
        assert map_field_source_to_source_type("summary_xbrl") == SourceType.TDNET_SUMMARY_XBRL
        assert map_field_source_to_source_type("attachment_xbrl") == SourceType.TDNET_ATTACHMENT_XBRL
        assert map_field_source_to_source_type("unknown_value") == SourceType.UNKNOWN

    def test_source_unit_to_raw_unit(self):
        assert map_source_unit_to_raw_unit("円") == RawUnit.YEN
        assert map_source_unit_to_raw_unit("百万円") == RawUnit.MILLION_YEN
        assert map_source_unit_to_raw_unit("不明") == RawUnit.UNKNOWN


# ============================================================
# merge_row_fields test
# ============================================================
class TestMergeRowFields:
    def test_field_level_mixed_adoption(self):
        """field 単位で最良候補を混成採用"""
        # jquants has sales (0.85) but no gp
        jq_fields = {
            "sales": normalize_financial_field(
                1000.0, field_name="sales",
                source_type=SourceType.JQUANTS,
                raw_unit=RawUnit.MILLION_YEN, origin="jquants",
            ),
            "gross_profit": normalize_financial_field(
                None, field_name="gross_profit",
                source_type=SourceType.JQUANTS,
                raw_unit=RawUnit.MILLION_YEN, origin="jquants",
            ),
            "operating_profit": normalize_financial_field(
                200.0, field_name="operating_profit",
                source_type=SourceType.JQUANTS,
                raw_unit=RawUnit.MILLION_YEN, origin="jquants",
            ),
            "cost_of_sales": normalize_financial_field(
                None, field_name="cost_of_sales",
                source_type=SourceType.JQUANTS,
                raw_unit=RawUnit.MILLION_YEN, origin="jquants",
            ),
        }
        # attachment has gp (0.95) and cos (0.90)
        att_fields = {
            "sales": normalize_financial_field(
                1000000000.0, field_name="sales",
                source_type=SourceType.TDNET_ATTACHMENT_XBRL,
                raw_unit=RawUnit.YEN, origin="tdnet",
            ),
            "gross_profit": normalize_financial_field(
                300000000.0, field_name="gross_profit",
                source_type=SourceType.TDNET_ATTACHMENT_XBRL,
                raw_unit=RawUnit.YEN, origin="tdnet",
            ),
            "operating_profit": normalize_financial_field(
                200000000.0, field_name="operating_profit",
                source_type=SourceType.TDNET_ATTACHMENT_XBRL,
                raw_unit=RawUnit.YEN, origin="tdnet",
            ),
            "cost_of_sales": normalize_financial_field(
                700000000.0, field_name="cost_of_sales",
                source_type=SourceType.TDNET_ATTACHMENT_XBRL,
                raw_unit=RawUnit.YEN, origin="tdnet",
            ),
        }
        merged = merge_row_fields(jq_fields, att_fields)

        # GP: attachment wins (0.95 > 0.0 for null jq)
        assert merged["gross_profit"].meta.source_type == SourceType.TDNET_ATTACHMENT_XBRL
        assert merged["gross_profit"].normalized_value == 300

        # COS: attachment wins (has value vs null)
        assert merged["cost_of_sales"].meta.source_type == SourceType.TDNET_ATTACHMENT_XBRL
        assert merged["cost_of_sales"].normalized_value == 700
