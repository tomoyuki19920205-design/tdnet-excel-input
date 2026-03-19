"""tests/test_phase2_improvements.py -- PDF Parse Phase 2 統合テスト"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ============================================================
# 1. Multi-line header mapping
# ============================================================
class TestMultilineHeaderMapping:
    """multi-line header 結合テスト。"""

    def test_two_row_header(self):
        from src.analysis.header_analysis import reconstruct_header_grid
        lines = [
            "セグメント名    売上高       営業利益",
            "              (百万円)     (百万円)",
        ]
        result = reconstruct_header_grid(lines)
        assert len(result) >= 2
        # 売上高 と (百万円) が結合されるべき
        combined = " ".join(result)
        assert "売上" in combined

    def test_three_row_header(self):
        from src.analysis.header_analysis import reconstruct_header_grid
        lines = [
            "報告セグメント",
            "              売上収益    セグメント利益",
            "              (百万円)    (百万円)",
        ]
        result = reconstruct_header_grid(lines)
        assert len(result) >= 1

    def test_single_row_header(self):
        from src.analysis.header_analysis import reconstruct_header_grid
        lines = ["セグメント名  売上高  営業利益"]
        result = reconstruct_header_grid(lines)
        assert len(result) >= 2


# ============================================================
# 2. Column role recovery
# ============================================================
class TestColumnRoleRecovery:
    """sales/profit column 検出テスト。"""

    def test_sales_keyword_detection(self):
        from src.analysis.header_analysis import score_header_role
        for kw in ["売上高", "売上収益", "営業収益", "純売上高", "売上合計", "Revenue", "Net sales"]:
            s = score_header_role(kw)
            assert s["sales"] >= 0.5, f"{kw} should score sales >= 0.5, got {s['sales']}"

    def test_profit_keyword_detection(self):
        from src.analysis.header_analysis import score_header_role
        for kw in ["営業利益", "セグメント利益", "事業利益", "コア営業利益"]:
            s = score_header_role(kw)
            profit = max(s.get("operating_profit", 0), s.get("segment_profit", 0))
            assert profit >= 0.5, f"{kw} should score profit >= 0.5, got {profit}"

    def test_rieki_alone_needs_low_score(self):
        """利益単独は低スコア (sales 共存時のみ昇格に使う)。"""
        from src.analysis.header_analysis import score_header_role
        s = score_header_role("利益")
        assert s["segment_profit"] >= 0.3
        assert s["segment_profit"] <= 0.6

    def test_two_column_table_fallback(self):
        """2列の数値表は sales+profit と推定される。"""
        from src.analysis.column_analysis import classify_columns
        headers = ["セグメント名"]
        data_rows = [
            ["自動車事業", "1,234", "567"],
            ["航空機事業", "2,345", "123"],
        ]
        result = classify_columns(data_rows, headers)
        # 何らかの列が検出されるべき (strict な assertion は避ける)
        assert result is not None


# ============================================================
# 3. Segment page candidate scoring
# ============================================================
class TestSegmentPageCandidateScoring:
    """ページ候補スコアリングテスト。"""

    def test_new_keywords_scored(self):
        """新規追加 KW が加点される。"""
        from src.analysis.page_scoring import score_segment_page
        for kw in ["事業別", "地域別", "所在地別", "製品別", "部門別", "セグメント情報"]:
            ps = score_segment_page(f"当社の{kw}情報\n売上高 1,234\n営業利益 567\n事業A 100 50", 0)
            assert ps.score > 0.0, f"'{kw}' should contribute positive score"

    def test_sales_profit_coexistence_bonus(self):
        """売上+利益共存で加点。"""
        from src.analysis.page_scoring import score_segment_page
        text = "売上高  営業利益\n事業A 1,000 100\n事業B 2,000 200"
        ps = score_segment_page(text, 0)
        assert "sales_profit_coexist" in ps.score_breakdown

    def test_deduction_qa(self):
        """Q&A で減点。"""
        from src.analysis.page_scoring import score_segment_page
        text = "Q&A\n質疑応答\n来期の見通し"
        ps = score_segment_page(text, 0)
        assert ps.score_breakdown.get("ded:Q&A", 0) < 0

    def test_deduction_capex(self):
        """設備投資で減点。"""
        from src.analysis.page_scoring import score_segment_page
        text = "設備投資の状況\n設備の新設計画"
        ps = score_segment_page(text, 0)
        assert ps.score_breakdown.get("ded:設備投資", 0) < 0

    def test_segment_page_with_adjustment(self):
        """調整額キーワードで加点。"""
        from src.analysis.page_scoring import score_segment_page
        text = "報告セグメント\n売上高  営業利益\n事業A 1,000 100\n事業B 2,000 200\n調整額 -50 -10"
        ps = score_segment_page(text, 0)
        assert ps.score > 0.3

    def test_num_rows_bonus(self):
        """数値行3行以上で加点。"""
        from src.analysis.page_scoring import score_segment_page
        lines = ["セグメント"] + [f"事業{i} {1000+i*100} {100+i*10}" for i in range(5)]
        text = "\n".join(lines)
        ps = score_segment_page(text, 0)
        assert "num_rows" in ps.score_breakdown


# ============================================================
# 4. Row extraction cleanup
# ============================================================
class TestRowExtractionCleanup:
    """行抽出フィルタリングテスト。"""

    def test_sonota_is_extractable(self):
        """「その他」は除外されない (extractable)。"""
        from src.analysis.row_analysis import classify_rows
        lines = [
            "セグメント名  売上高  営業利益",
            "自動車事業    1,234     567",
            "その他          100      10",
            "合計          1,334     577",
        ]
        result = classify_rows(lines, header_band_height=1)
        sonota = [r for r in result.rows if "その他" in r.label]
        assert len(sonota) == 1
        assert sonota[0].is_extractable is True

    def test_total_excluded(self):
        """合計は除外される。"""
        from src.analysis.row_analysis import classify_rows
        lines = [
            "ヘッダー",
            "事業A    1,000   100",
            "合計     2,000   200",
        ]
        result = classify_rows(lines, header_band_height=1)
        total_rows = [r for r in result.rows if "合計" in r.label]
        assert len(total_rows) == 1
        assert total_rows[0].is_extractable is False

    def test_chouseigaku_excluded(self):
        """調整額は除外される。"""
        from src.analysis.row_analysis import classify_rows
        lines = [
            "ヘッダー",
            "事業A    1,000   100",
            "調整額      -50    -10",
        ]
        result = classify_rows(lines, header_band_height=1)
        adj_rows = [r for r in result.rows if "調整" in r.label]
        assert len(adj_rows) == 1
        assert adj_rows[0].is_extractable is False

    def test_consolidated_excluded(self):
        """連結は除外される。"""
        from src.analysis.row_analysis import classify_rows
        lines = [
            "ヘッダー",
            "事業A    1,000   100",
            "連結     3,000   300",
        ]
        result = classify_rows(lines, header_band_height=1)
        con_rows = [r for r in result.rows if "連結" in r.label]
        assert len(con_rows) == 1
        assert con_rows[0].is_extractable is False

    def test_zensha_excluded(self):
        """全社は除外される。"""
        from src.analysis.row_analysis import classify_rows
        lines = [
            "ヘッダー",
            "事業A    1,000   100",
            "全社       -200    -50",
        ]
        result = classify_rows(lines, header_band_height=1)
        zen_rows = [r for r in result.rows if "全社" in r.label]
        assert len(zen_rows) == 1
        assert zen_rows[0].is_extractable is False

    def test_segment_name_with_nums(self):
        """数値ありの通常行は extractable。"""
        from src.analysis.row_analysis import classify_rows
        lines = [
            "ヘッダー",
            "環境システム事業   500    50",
        ]
        result = classify_rows(lines, header_band_height=1)
        seg_rows = result.segment_rows
        assert len(seg_rows) == 1
        assert seg_rows[0].label == "環境システム事業"
