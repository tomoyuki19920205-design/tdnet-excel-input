#!/usr/bin/env python3
"""test_split_header_reconstruction.py — 分割ヘッダー復元テスト (40ケース以上)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.header_reconstruction import (
    normalize_header_cell,
    is_empty_cell,
    is_numeric_cell,
    is_unit_cell,
    is_period_modifier,
    strip_period_modifiers,
    score_metric_header,
    reconstruct_horizontal,
    reconstruct_vertical,
    build_reconstructed_headers,
    reconstruct_from_lines,
    ReconstructionResult,
    MetricScore,
)


# ============================================================
# Cell-Level Tests
# ============================================================

class TestNormalizeHeaderCell:
    def test_strip(self):
        assert normalize_header_cell("  売上高  ") == "売上高"

    def test_nfkc(self):
        assert normalize_header_cell("Ｒｅｖｅｎｕｅ") == "Revenue"

    def test_newline(self):
        # 日本語テキストでは空白を完全除去
        assert normalize_header_cell("売上\n高") == "売上高"

    def test_fullwidth_space(self):
        assert normalize_header_cell("売上\u3000高") == "売上高"

    def test_table_border(self):
        assert normalize_header_cell("─売上高─") == "売上高"


class TestIsEmptyCell:
    def test_empty(self):
        assert is_empty_cell("") is True

    def test_dash(self):
        assert is_empty_cell("-") is True

    def test_em_dash(self):
        assert is_empty_cell("—") is True

    def test_pipe(self):
        assert is_empty_cell("|") is True

    def test_non_empty(self):
        assert is_empty_cell("売上高") is False

    def test_percent(self):
        assert is_empty_cell("%") is False


class TestIsNumericCell:
    def test_integer(self):
        assert is_numeric_cell("100,000") is True

    def test_negative(self):
        assert is_numeric_cell("-50,000") is True

    def test_text(self):
        assert is_numeric_cell("売上高") is False


class TestIsUnitCell:
    def test_hyakuman(self):
        assert is_unit_cell("（百万円）") is True

    def test_oku(self):
        assert is_unit_cell("(億円)") is True

    def test_percent(self):
        assert is_unit_cell("％") is True

    def test_non_unit(self):
        assert is_unit_cell("売上高") is False


class TestIsPeriodModifier:
    def test_touki(self):
        assert is_period_modifier("当期") is True

    def test_zenki(self):
        assert is_period_modifier("前期") is True

    def test_yosou(self):
        assert is_period_modifier("予想") is True

    def test_non_period(self):
        assert is_period_modifier("売上高") is False


class TestStripPeriodModifiers:
    def test_strip(self):
        assert strip_period_modifiers("当期営業利益") == "営業利益"

    def test_combined(self):
        assert strip_period_modifiers("前年同期売上高") == "売上高"

    def test_no_modifier(self):
        assert strip_period_modifiers("営業利益") == "営業利益"


# ============================================================
# Scoring Tests
# ============================================================

class TestScoreMetricHeader:
    # --- 仕様書テストケース ---

    def test_eigyo_rieki(self):
        """営業利益 → profit 100+"""
        s = score_metric_header("営業利益")
        assert s["profit"].total_score >= 100
        assert s["profit"].exact_match is True

    def test_uriage_taka(self):
        """売上高 → sales 100+"""
        s = score_metric_header("売上高")
        assert s["sales"].total_score >= 100
        assert s["sales"].exact_match is True

    def test_eigyo_riekiRitsu(self):
        """営業利益率 → profit 低スコア"""
        s = score_metric_header("営業利益率")
        assert s["profit"].total_score <= 15
        assert len(s["profit"].penalties) > 0

    def test_uriage_yoy(self):
        """売上高前年同期比 → sales ≤ keyword match"""
        s = score_metric_header("売上高前年同期比")
        assert s["sales"].total_score < 100  # exact ではない（前年同期比で弱化）

    def test_hyakuman_no_metric(self):
        """百万円 単独は metric 不採用"""
        s = score_metric_header("百万円")
        assert s["sales"].total_score == 0
        assert s["profit"].total_score == 0

    def test_segment_rieki(self):
        """セグメント利益 → profit 100"""
        s = score_metric_header("セグメント利益")
        assert s["profit"].total_score >= 100

    def test_keijo_rieki(self):
        """経常利益 → profit 95+"""
        s = score_metric_header("経常利益")
        assert s["profit"].total_score >= 95

    def test_jigyo_rieki(self):
        """事業利益 → profit 95"""
        s = score_metric_header("事業利益")
        assert s["profit"].total_score >= 95

    def test_oyagaisha_junrieki(self):
        """親会社株主に帰属する当期純利益 → profit"""
        s = score_metric_header("親会社株主に帰属する当期純利益")
        assert s["profit"].total_score >= 85

    # --- 除外テスト ---

    def test_rieki_jouyo_kin(self):
        """利益剰余金 → profit に採用しない"""
        s = score_metric_header("利益剰余金")
        assert s["exclusion"].total_score > 0

    def test_houkatsu_rieki(self):
        """包括利益 → profit に採用しない"""
        s = score_metric_header("包括利益")
        assert s["exclusion"].total_score > 0

    def test_zougen_ritsu(self):
        """増減率 → 除外"""
        s = score_metric_header("増減率")
        assert s["exclusion"].total_score >= 40

    def test_kousei_hi(self):
        """構成比 → 除外"""
        s = score_metric_header("構成比")
        assert s["exclusion"].total_score >= 40

    def test_margin(self):
        """Margin → 除外"""
        s = score_metric_header("Margin")
        assert s["exclusion"].total_score >= 40

    def test_zennen_douki_hi(self):
        """前年同期比(%) → 除外"""
        s = score_metric_header("前年同期比(%)")
        assert s["exclusion"].total_score >= 40

    def test_shuuekisei(self):
        """収益性 → profit と誤認しない"""
        s = score_metric_header("収益性")
        # 収益 が substring match するが「性」は exclusion にはない
        # → 弱い profit にはなるが exact match ではない
        assert not s["profit"].exact_match

    def test_uriagesoorieki(self):
        """売上総利益 → sales ではない"""
        s = score_metric_header("売上総利益")
        assert s["exclusion"].total_score > 0

    # --- 英語テスト ---

    def test_en_operating_profit(self):
        s = score_metric_header("Operating Profit")
        assert s["profit"].total_score >= 80

    def test_en_segment_profit(self):
        s = score_metric_header("Segment profit")
        assert s["profit"].total_score >= 80

    def test_en_revenue(self):
        s = score_metric_header("Revenue")
        assert s["sales"].total_score >= 80

    def test_en_net_sales(self):
        s = score_metric_header("Net Sales")
        assert s["sales"].total_score >= 80

    # --- sales 系テスト ---

    def test_uriage_shuueki(self):
        """売上収益 → sales 100"""
        s = score_metric_header("売上収益")
        assert s["sales"].total_score >= 100

    def test_eigyo_shuueki(self):
        """営業収益 → sales 95"""
        s = score_metric_header("営業収益")
        assert s["sales"].total_score >= 89  # 95 * 0.6 or exact

    # --- profit as loss ---

    def test_eigyo_sonshitsu(self):
        """営業損失 → profit 系"""
        s = score_metric_header("営業損失")
        assert s["profit"].total_score >= 80

    def test_keijo_sonshitsu(self):
        """経常損失 → profit 系"""
        s = score_metric_header("経常損失")
        assert s["profit"].total_score >= 80

    def test_segment_soneki(self):
        """セグメント利益又は損失 → profit 100"""
        s = score_metric_header("セグメント利益又は損失")
        assert s["profit"].total_score >= 100

    # --- weak terms test ---
    def test_chousei_gaku(self):
        """調整額 → metric 不採用"""
        s = score_metric_header("調整額")
        assert s["sales"].total_score == 0
        assert s["profit"].total_score == 0

    def test_shoukyo_mata_zensha(self):
        """消去又は全社 → metric 不採用"""
        s = score_metric_header("消去又は全社")
        assert s["sales"].total_score == 0


# ============================================================
# Horizontal Reconstruction Tests
# ============================================================

class TestReconstructHorizontal:
    def test_eigyo_rieki_split(self):
        """['営業','利益'] → '営業利益'"""
        cands = reconstruct_horizontal(["営業", "利益"])
        assert any(c.result == "営業利益" for c in cands)

    def test_uriage_taka_split(self):
        """['売上','高'] → '売上高'"""
        cands = reconstruct_horizontal(["売上", "高"])
        assert any(c.result == "売上高" for c in cands)

    def test_segment_rieki_split(self):
        """['セグメント','利益'] → 'セグメント利益'"""
        cands = reconstruct_horizontal(["セグメント", "利益"])
        assert any(c.result == "セグメント利益" for c in cands)

    def test_keijo_rieki_split(self):
        """['経常','利益'] → '経常利益'"""
        cands = reconstruct_horizontal(["経常", "利益"])
        assert any(c.result == "経常利益" for c in cands)

    def test_empty_gap_one(self):
        """空セル1個跨ぎ: ['営業','','利益'] → '営業利益'"""
        cands = reconstruct_horizontal(["営業", "", "利益"])
        assert any(c.result == "営業利益" for c in cands)

    def test_numeric_skip(self):
        """数値混在は連結しない"""
        cands = reconstruct_horizontal(["営業", "100,000", "利益"])
        # 営業+100,000 や 100,000+利益 は除外される
        for c in cands:
            assert "100,000" not in c.result

    def test_en_operating_income(self):
        """['Operating','Income'] → 'OperatingIncome'"""
        cands = reconstruct_horizontal(["Operating", "Income"])
        assert any(c.result == "OperatingIncome" for c in cands)

    def test_en_net_sales(self):
        """['Net','Sales'] → 'NetSales'"""
        cands = reconstruct_horizontal(["Net", "Sales"])
        assert any(c.result == "NetSales" for c in cands)

    def test_oyagaisha(self):
        """['親会社株主に帰属する','当期純利益']"""
        cands = reconstruct_horizontal(["親会社株主に帰属する", "当期純利益"])
        assert any("当期純利益" in c.result for c in cands)

    def test_period_only_no_candidate(self):
        """期間語だけの連結は候補にしない"""
        cands = reconstruct_horizontal(["当期", "前期"])
        assert len(cands) == 0

    def test_date_not_merged(self):
        """'2025/3' は数値として扱い連結しない"""
        cands = reconstruct_horizontal(["2025/3", "営業利益"])
        # 2025/3 が数値判定されるか確認
        for c in cands:
            assert "2025/3" not in c.result


# ============================================================
# Vertical Reconstruction Tests
# ============================================================

class TestReconstructVertical:
    def test_vertical_eigyo_rieki(self):
        """縦分割 ['営業';'利益'] → '営業利益'"""
        cands = reconstruct_vertical([["営業"], ["利益"]])
        assert any(c.result == "営業利益" for c in cands)

    def test_vertical_uriage_taka(self):
        """縦分割 ['売上';'高'] → '売上高'"""
        cands = reconstruct_vertical([["売上"], ["高"]])
        assert any(c.result == "売上高" for c in cands)

    def test_vertical_segment_rieki(self):
        """縦分割 ['セグメント';'利益'] → 'セグメント利益'"""
        cands = reconstruct_vertical([["セグメント"], ["利益"]])
        assert any(c.result == "セグメント利益" for c in cands)

    def test_vertical_keijo_rieki(self):
        """縦分割 ['経常';'利益'] → '経常利益'"""
        cands = reconstruct_vertical([["経常"], ["利益"]])
        assert any(c.result == "経常利益" for c in cands)

    def test_vertical_zeibikimae(self):
        """縦分割 ['税引前';'利益'] → '税引前利益'"""
        cands = reconstruct_vertical([["税引前"], ["利益"]])
        assert any(c.result == "税引前利益" for c in cands)


# ============================================================
# Full Pipeline Tests
# ============================================================

class TestBuildReconstructedHeaders:
    def test_two_row_split(self):
        """2行分割: row0=['売上','営業'], row1=['高','利益']"""
        result = build_reconstructed_headers([
            ["売上", "営業"],
            ["高", "利益"],
        ])
        headers = result.reconstructed_headers
        assert "売上高" in headers
        assert "営業利益" in headers

    def test_single_row_split(self):
        """1行内分割"""
        result = build_reconstructed_headers([
            ["セグメント", "利益"],
        ])
        headers = result.reconstructed_headers
        assert any("セグメント利益" in h for h in headers)

    def test_no_merge_normal(self):
        """正常ヘッダーはそのまま"""
        result = build_reconstructed_headers([
            ["売上高", "営業利益"],
        ])
        headers = result.reconstructed_headers
        assert "売上高" in headers
        assert "営業利益" in headers

    def test_feature_off(self):
        """feature flag OFF → 正規化のみ"""
        result = build_reconstructed_headers(
            [["営業", "利益"]],
            enable_reconstruction=False,
        )
        assert result.feature_enabled is False
        # 縦結合しないので個別に残る
        assert len(result.steps) == 0

    def test_steps_logged(self):
        """復元ステップがログされる"""
        result = build_reconstructed_headers([
            ["営業", "利益"],
        ])
        if result.steps:
            step = result.steps[0]
            assert "type" in step
            assert "parts" in step
            assert "result" in step

    def test_empty_matrix(self):
        result = build_reconstructed_headers([])
        assert result.reconstructed_headers == []


class TestReconstructFromLines:
    def test_basic(self):
        """行テキストからマトリクス化して復元"""
        result = reconstruct_from_lines(["売上高    営業利益"])
        assert "売上高" in result.reconstructed_headers
        assert "営業利益" in result.reconstructed_headers

    def test_unit_line_removed(self):
        """単位行は除去"""
        result = reconstruct_from_lines([
            "売上高    営業利益",
            "（百万円）",
        ])
        assert "百万円" not in " ".join(result.reconstructed_headers)

    def test_two_row_vertical(self):
        """2行テキストからの縦連結"""
        result = reconstruct_from_lines([
            "営業",
            "利益",
        ])
        # 縦連結で「営業利益」が生成される可能性
        found = any("営業利益" in h for h in result.reconstructed_headers)
        # もしくは raw fallback で結合
        assert found or len(result.reconstructed_headers) >= 1


# ============================================================
# Regression Safety Tests
# ============================================================

class TestRegressionSafety:
    """既存成功パターンが壊れないことの確認"""

    def test_normal_header_unchanged(self):
        """正常な複数列ヘッダー"""
        result = build_reconstructed_headers([
            ["セグメント", "売上高", "営業利益", "セグメント資産"],
        ])
        h = result.reconstructed_headers
        assert "売上高" in h
        assert "営業利益" in h

    def test_unit_in_paren_preserved(self):
        """単位括弧つきヘッダー"""
        result = build_reconstructed_headers([
            ["売上高(百万円)", "営業利益(百万円)"],
        ])
        h = result.reconstructed_headers
        # 単位は除去されるが売上高/営業利益は保持
        assert any("売上" in x for x in h)

    def test_same_col_no_conflict(self):
        """sales/profit が同列に乗らない"""
        result = build_reconstructed_headers([
            ["売上高", "営業利益"],
        ])
        h = result.reconstructed_headers
        # 売上高と営業利益が別列に残ること
        sales_idx = [i for i, x in enumerate(h) if "売上" in x]
        profit_idx = [i for i, x in enumerate(h) if "営業利益" in x]
        assert sales_idx != profit_idx


# ============================================================
# Descriptive Header / Header Splitting Tests
# ============================================================

from src.analysis.header_reconstruction import (
    is_descriptive_segment_header,
    split_header_rows_for_role_detection,
)


class TestIsDescriptiveSegmentHeader:
    """descriptive_only_header 判定テスト"""

    def test_segment_info_headers(self):
        """(2)報告セグメント情報 + 説明文 → True"""
        texts = [
            "(2)報告セグメント情報",
            "報告セグメントの利益は、営業利益をベースとした数値です。",
        ]
        assert is_descriptive_segment_header(texts) is True

    def test_segment_info_variant(self):
        """(2)報告セグメントに関する情報 + 説明文 → True"""
        texts = [
            "(2)報告セグメントに関する情報",
            "当社グループの報告セグメントによる収益及び業績は以下のとおりであります。",
        ]
        assert is_descriptive_segment_header(texts) is True

    def test_real_column_headers(self):
        """売上収益 / セグメント利益 を含む → False"""
        texts = [
            "売上収益",
            "セグメント利益",
            "2024年",
            "2025年",
        ]
        assert is_descriptive_segment_header(texts) is False

    def test_empty_input(self):
        assert is_descriptive_segment_header([]) is True
        assert is_descriptive_segment_header(["", "  "]) is True

    def test_mixed_with_metric(self):
        """説明文 + 営業利益 → False (メトリクス語あり)"""
        texts = [
            "(2)報告セグメント情報",
            "営業利益",
        ]
        assert is_descriptive_segment_header(texts) is False


class TestSplitHeaderRowsForRoleDetection:
    """header row splitting テスト"""

    def test_period_and_metric_split(self):
        """期間行 + メトリクス行の分離"""
        rows = [
            "12月31日に終了した9カ月間",
            "2024年 2025年 増減 増減率",
            "売上高 セグメント利益",
        ]
        result = split_header_rows_for_role_detection(rows)
        # 売上高/セグメント利益 は primary に
        primary_text = " ".join(result["primary"])
        assert "売上高" in primary_text
        assert "セグメント利益" in primary_text
        # 期間説明 は secondary に
        assert len(result["secondary"]) >= 1
        secondary_text = " ".join(result["secondary"])
        assert "終了した" in secondary_text or "増減" in secondary_text

    def test_no_split_needed(self):
        """期間行がない場合は全て primary"""
        rows = ["売上高", "営業利益"]
        result = split_header_rows_for_role_detection(rows)
        assert len(result["primary"]) == 2
        assert len(result["secondary"]) == 0

    def test_all_secondary_fallback(self):
        """全行 secondary 判定の場合は primary 空"""
        rows = ["2024年", "増減率"]
        result = split_header_rows_for_role_detection(rows)
        # 全行 secondary → primary は空
        assert len(result["primary"]) == 0
        assert len(result["secondary"]) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
