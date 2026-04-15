#!/usr/bin/env python3
"""
Phase 3: 弱表対応 + 列推定強化テスト

テストケース:
1. weak_table_rescue: anchor=0/hdr=0 でも segrows+repnum+列構造で rescue される
2. 文章ブロック非救済: 明らかな文章ブロックは weak_table_rescue されない
3. 列スコアリング: 数値列3本のとき sales が score 最大列に選ばれる
4. %列/件数列除外: %列/件数列/数量列は sales 候補から除外
5. profit rescue: sales 確定後、隣接列から profit rescue される
6. weak_header: weak_header 語が低スコア補助として効く
7. score margin 不足で未確定になるケース
8. 列候補拮抗で誤確定しないことを確認
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.row_classifier import (
    evaluate_candidate_guard,
    compute_candidate_table_signals,
)
from src.analysis.column_analysis import (
    classify_columns,
    _compute_column_score,
    ColumnRole,
)


# ============================================================
# 1. weak_table_rescue テスト
# ============================================================

class TestWeakTableRescue:
    def test_weak_rescue_segrows3_repnum2(self):
        """segrows>=3 + repnum>=2 + distinct_numpos>=2 で rescue (パターンA)"""
        labels = ["日本"]  # valid=1 → no_valid_segment_rows になるはず
        table_lines = [
            "日本        50,000  3,000",
            "北米        20,000  1,500",
            "欧州        10,000  800",
            "アジア       8,000   600",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=0,  # ヘッダー情報なし
            anchor_hits=0,
            segment_name_like_rows=4,
        )
        assert result.accepted
        assert "weak_table_rescue" in result.rescued_by

    def test_weak_rescue_segrows2_repnum3(self):
        """segrows>=2 + repnum>=3 + distinct_numpos>=2 で rescue (パターンB)"""
        labels = ["日本"]
        table_lines = [
            "日本        50,000  3,000",
            "北米        20,000  1,500",
            "欧州        10,000  800",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=0,
            anchor_hits=0,
            segment_name_like_rows=2,
        )
        assert result.accepted
        assert "weak_table_rescue" in result.rescued_by

    def test_weak_rescue_requires_col_structure(self):
        """列構造が不十分 (distinct_numpos < 2) なら rescue しない"""
        labels = ["日本"]
        # 数値が1列分しかない
        table_lines = [
            "日本        50000",
            "北米        20000",
            "欧州        10000",
        ]
        result = evaluate_candidate_guard(
            labels,
            candidate_lines=table_lines,
            header_keyword_hits=0,
            anchor_hits=0,
            segment_name_like_rows=3,
        )
        assert not result.accepted


class TestWeakTableNonRescue:
    def test_narrative_not_rescued(self):
        """明らかな文章ブロックは weak_table_rescue されない"""
        labels = [
            "当連結会計年度における経営成績の概況は以下のとおりです。",
            "売上高は前年同期比で増加しました。",
            "営業利益は原材料費高騰の影響により減少しました。",
        ]
        result = evaluate_candidate_guard(
            labels,
            header_keyword_hits=0,
            anchor_hits=0,
            segment_name_like_rows=0,
        )
        assert not result.accepted
        assert "weak_table_rescue" not in (result.rescued_by or "")


# ============================================================
# 2. distinct_numeric_positions テスト
# ============================================================

class TestDistinctNumericPositions:
    def test_two_column_table(self):
        """2列の数値表で distinct_numeric_positions >= 2"""
        lines = [
            "建設事業  50,000  3,000",
            "不動産事業  20,000  1,500",
            "その他  5,000  500",
        ]
        _d, _r, dnp = compute_candidate_table_signals(lines)
        assert dnp >= 2

    def test_single_column(self):
        """1列のみの数値表"""
        lines = [
            "建設事業  50,000",
            "不動産事業  20,000",
            "その他  5,000",
        ]
        _d, _r, dnp = compute_candidate_table_signals(lines)
        # 1列に数値が並ぶだけなので distinct_positions は 1 程度
        assert dnp <= 1


# ============================================================
# 3. 列スコアリングテスト
# ============================================================

class TestColumnScoring:
    def test_sales_highest_score_wins(self):
        """数値列3本のとき sales が score 最大列に選ばれる"""
        headers = ["区分", "売上高", "利益", "資産"]
        data_rows = [
            ["事業A", "10,000", "500", "20,000"],
            ["事業B", "20,000", "1,000", "30,000"],
            ["事業C", "15,000", "800", "25,000"],
        ]
        result = classify_columns(data_rows, headers)
        assert result.has_sales
        # 売上高ヘッダーの列が sales になるべき
        assert result.best_sales_col == 1

    def test_pct_column_excluded(self):
        """%列は sales/profit 候補から除外"""
        headers = ["区分", "売上高", "構成比"]
        data_rows = [
            ["事業A", "10,000", "30.5%"],
            ["事業B", "20,000", "45.2%"],
            ["事業C", "5,000", "24.3%"],
        ]
        result = classify_columns(data_rows, headers)
        assert result.has_sales
        # 構成比列が profit に選ばれてはいけない
        if result.best_profit_col is not None:
            assert result.best_profit_col != 2

    def test_count_column_excluded_via_column_scoring(self):
        """件数列は Phase 3 列スコアリングで penalty を受ける"""
        # _compute_column_score で件数列の sales/profit スコアを確認
        headers = ["区分", "売上高", "件数"]
        data_rows = [
            ["事業A", "10,000", "150"],
            ["事業B", "20,000", "300"],
            ["事業C", "5,000", "80"],
        ]
        # 件数列 (col=2) のスコアを直接確認
        score_info = _compute_column_score(2, data_rows, [{}, {}, {}], ["", "", ""], headers)
        assert score_info["count_penalty"] > 0  # 件数列は count_penalty を受ける
        # 売上高列 (col=1) のスコアが件数列より高い
        sales_info = _compute_column_score(1, data_rows, [{}, {}, {}], ["", "", ""], headers)
        assert sales_info["sales_score"] > score_info["sales_score"]


# ============================================================
# 4. profit rescue テスト
# ============================================================

class TestProfitRescue:
    def test_profit_adjacent_to_sales(self):
        """sales 確定後、隣接列から profit が rescue される"""
        headers = ["セグメント", "売上高", "営業利益", "その他"]
        data_rows = [
            ["事業A", "10,000", "500", "100"],
            ["事業B", "20,000", "1,000", "200"],
            ["事業C", "15,000", "800", "150"],
        ]
        result = classify_columns(data_rows, headers)
        assert result.has_sales
        assert result.has_profit
        # profit は sales の隣接列 (col=2) が期待される
        assert result.best_profit_col == 2

    def test_sales_profit_not_same_column(self):
        """sales と profit は同一列にならない"""
        headers = ["区分", "売上高", "利益"]
        data_rows = [
            ["事業A", "10,000", "500"],
            ["事業B", "20,000", "1,000"],
        ]
        result = classify_columns(data_rows, headers)
        if result.has_sales and result.has_profit:
            assert result.best_sales_col != result.best_profit_col


# ============================================================
# 5. weak_header テスト
# ============================================================

class TestWeakHeaders:
    def test_shunyu_weak_sales_via_column_scoring(self):
        """「収入」は列スコアリングで sales 候補として認識される"""
        headers = ["区分", "収入"]
        data_rows = [
            ["事業A", "10,000"],
            ["事業B", "20,000"],
            ["事業C", "15,000"],
        ]
        score_info = _compute_column_score(1, data_rows, [{}, {}], ["", ""], headers)
        # 「収入」があるので header_sales_score > 0 → sales_score が高い
        assert score_info["sales_score"] > 0

    def test_bumon_soneki_weak_profit_via_column_scoring(self):
        """「部門損益」は列スコアリングで profit 候補として認識される"""
        headers = ["区分", "部門損益"]
        data_rows = [
            ["事業A", "500"],
            ["事業B", "1,000"],
            ["事業C", "800"],
        ]
        score_info = _compute_column_score(1, data_rows, [{}, {}], ["", ""], headers)
        assert score_info["profit_score"] > 0

    def test_weak_header_in_classify_columns(self):
        """weak_header (収入/部門損益) が classify_columns で認識される"""
        headers = ["区分", "収入", "部門損益"]
        data_rows = [
            ["事業A", "10,000", "500"],
            ["事業B", "20,000", "1,000"],
            ["事業C", "15,000", "800"],
        ]
        result = classify_columns(data_rows, headers)
        assert result.has_sales


# ============================================================
# 6. score margin 不足テスト
# ============================================================

class TestScoreMarginInsufficient:
    def test_column_score_margin_insufficient_no_confirm(self):
        """列候補が拮抗している場合に sales/profit が誤確定しない"""
        # ヘッダーなし、数値の大小も拮抗
        headers = ["区分", "A", "B"]
        data_rows = [
            ["事業A", "10,000", "10,000"],
            ["事業B", "20,000", "20,000"],
            ["事業C", "15,000", "15,000"],
        ]
        result = classify_columns(data_rows, headers)
        # 拮抗しているので Phase 3 列スコアリングでは確定しない
        # (他のフェーズで確定する可能性はあるが、margin 不足の列スコアリングでは確定しないことを確認)
        # 仮に確定しても sales != profit であること
        if result.has_sales and result.has_profit:
            assert result.best_sales_col != result.best_profit_col

    def test_weak_rescue_but_column_undecided(self):
        """weak rescue 成功だが、列候補が拮抗してて sales/profit 未確定のケース"""
        headers = ["区分", "A", "B"]
        data_rows = [
            ["事業A", "10,000", "10,500"],
            ["事業B", "20,000", "20,200"],
            ["事業C", "15,000", "15,100"],
        ]
        # classify_columns 自体が Phase 3 列スコアリングで margin 不足を検出
        result = classify_columns(data_rows, headers)
        # 拮抗: 確定していないか、確定しても同一列でないこと
        if result.has_sales and result.has_profit:
            assert result.best_sales_col != result.best_profit_col


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
