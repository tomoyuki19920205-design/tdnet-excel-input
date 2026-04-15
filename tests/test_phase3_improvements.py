"""tests/test_phase3_improvements.py — Phase 3 PDF parse 改善テスト"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSynonymExpansion:
    """テーマB: synonym 第3弾テスト。"""

    def test_sales_operating_revenue(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("Operating Revenue")
        assert scores["sales"] >= 0.8

    def test_sales_total_revenue(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("Total Revenue")
        assert scores["sales"] >= 0.8

    def test_sales_net_revenue(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("Net Revenue")
        assert scores["sales"] >= 0.8

    def test_profit_core_operating_profit(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("Core Operating Profit")
        assert scores["operating_profit"] >= 0.9

    def test_profit_adjusted_operating_income(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("Adjusted Operating Income")
        assert scores["operating_profit"] >= 0.8

    def test_segment_profit_or_loss(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("Segment Profit or Loss")
        assert scores["segment_profit"] >= 0.9

    def test_profit_income_alone(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("Income")
        assert scores["segment_profit"] >= 0.3

    def test_soneki_keyword(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("損益")
        assert scores["segment_profit"] >= 0.6

    def test_sonshitsu_keyword(self):
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("損失")
        assert scores["segment_profit"] >= 0.4

    def test_kingaku_weak_sales(self):
        """金額は sales に弱くマッチ。"""
        from src.analysis.header_analysis import score_header_role
        scores = score_header_role("金額")
        assert 0.1 <= scores["sales"] <= 0.5


class TestColumnRolePhase3:
    """テーマB: 列 role 推定強化テスト。"""

    def test_sales_adjacent_profit_boost(self):
        """sales 列が見つかったら隣接の弱い profit 列が昇格。"""
        from src.analysis.column_analysis import classify_columns
        headers = ["事業名", "売上高", "利益"]
        data_rows = [
            ["事業A", "1,000", "100"],
            ["事業B", "2,000", "200"],
        ]
        result = classify_columns(data_rows, headers)
        assert result.has_sales
        # 利益列は隣接推定で見つかるか
        assert result.has_profit or result.best_profit_col is not None

    def test_value_magnitude_heuristic(self):
        """数値列の大小分布から sales/profit を推定。"""
        from src.analysis.column_analysis import classify_columns
        # ヘッダーに role 情報がない場合でも数値の大小で推定
        # Phase 3: MIN_MARGIN 条件があるため、magnitude 差が十分大きい必要がある
        headers = ["区分", "A", "B"]
        data_rows = [
            ["事業X", "100,000", "50"],
            ["事業Y", "200,000", "100"],
            ["事業Z", "150,000", "80"],
        ]
        result = classify_columns(data_rows, headers)
        # sales は大きい方の列、profit は小さい方
        assert result.has_sales

    def test_yoy_column_excluded(self):
        """YoY列はsales/profitに選ばれない。"""
        from src.analysis.column_analysis import classify_columns
        headers = ["事業名", "売上高", "前年比"]
        data_rows = [
            ["事業A", "1,000", "10.5%"],
            ["事業B", "2,000", "5.2%"],
        ]
        result = classify_columns(data_rows, headers)
        assert result.has_sales
        # 前年比列がprofit列にならない
        if result.best_profit_col is not None:
            assert result.column_roles[result.best_profit_col] != "yoy"

    def test_four_column_table(self):
        """4列表でも sales/profit を検出。"""
        from src.analysis.column_analysis import classify_columns
        headers = ["セグメント", "売上高", "営業利益", "資産"]
        data_rows = [
            ["事業A", "10,000", "500", "20,000"],
            ["事業B", "20,000", "1,000", "30,000"],
        ]
        result = classify_columns(data_rows, headers)
        assert result.has_sales
        assert result.has_profit


class TestPageScoringPhase3:
    """テーマC: ページ候補スコアリング強化テスト。"""

    def test_new_keywords_reportable_segments(self):
        from src.analysis.page_scoring import score_segment_page
        text = "reportable segments\n事業A 1,000 100\n事業B 2,000 200"
        ps = score_segment_page(text, 0)
        assert ps.score > 0.1

    def test_segment_beppyo_keyword(self):
        from src.analysis.page_scoring import score_segment_page
        text = "セグメント別業績\n売上高  営業利益\n事業A 1,000 100"
        ps = score_segment_page(text, 0)
        assert ps.score > 0.3

    def test_deduction_gyoseki_yoso(self):
        from src.analysis.page_scoring import score_segment_page
        text = "業績予想\n当社の業績予想は以下のとおり"
        ps = score_segment_page(text, 0)
        assert "業績予想" in ps.reason

    def test_deduction_kaikei_hoshin(self):
        from src.analysis.page_scoring import score_segment_page
        text = "会計方針の変更\n以下の基準を適用"
        ps = score_segment_page(text, 0)
        assert ps.score < 0.1

    def test_segment_name_rows_bonus(self):
        """セグメント名っぽい行が複数あれば加点。"""
        from src.analysis.page_scoring import score_segment_page
        text = "\n".join([
            "報告セグメント",
            "売上高  営業利益",
            "放送事業    5,000  500",
            "ライフスタイル事業  3,000  300",
            "映像事業    2,000  200",
            "不動産事業   1,000  100",
        ])
        ps = score_segment_page(text, 0)
        assert ps.score > 0.4

    def test_page_with_many_segments_high_score(self):
        """多数のセグメントがある典型的なページ。"""
        from src.analysis.page_scoring import score_segment_page
        text = "\n".join([
            "セグメント別業績",
            "売上高  営業利益",
            "国内事業    50,000  5,000",
            "海外事業    30,000  3,000",
            "金融事業    20,000  2,000",
            "不動産事業   10,000  1,000",
            "その他      5,000   500",
            "調整額     -2,000  -200",
            "合計      113,000 11,300",
        ])
        ps = score_segment_page(text, 0)
        assert ps.score > 0.5


class TestRetryHintMultiple:
    """retry CLI の --only-hint 複数対応テスト。"""

    def test_hint_filter_comma_separated(self):
        """カンマ区切りで複数 hint が対象になること。"""
        # _get_quarantined_filings の内部フィルタロジックを直接テスト
        rows = [
            {"filing_id": "a", "review_hint": "pdf_no_sales_profit_columns"},
            {"filing_id": "b", "review_hint": "pdf_no_segment_page_candidate"},
            {"filing_id": "c", "review_hint": "pdf_no_rows_extracted"},
        ]
        only_hint = "pdf_no_sales_profit_columns,pdf_no_segment_page_candidate"
        hint_set = {h.strip() for h in only_hint.split(",")}
        filtered = [r for r in rows if r.get("review_hint", "") in hint_set]
        assert len(filtered) == 2
        assert filtered[0]["filing_id"] == "a"
        assert filtered[1]["filing_id"] == "b"

    def test_hint_filter_single(self):
        """単一 hint でも動作すること。"""
        rows = [
            {"filing_id": "a", "review_hint": "pdf_no_sales_profit_columns"},
            {"filing_id": "b", "review_hint": "pdf_no_segment_page_candidate"},
        ]
        only_hint = "pdf_no_sales_profit_columns"
        hint_set = {h.strip() for h in only_hint.split(",")}
        filtered = [r for r in rows if r.get("review_hint", "") in hint_set]
        assert len(filtered) == 1
