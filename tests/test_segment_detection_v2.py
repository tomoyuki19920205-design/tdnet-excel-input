#!/usr/bin/env python3
"""Phase F-G: v2 統合テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.segment_detection_v2 import (
    run_segment_detection_v2,
    V2DetectionResult,
    _extract_numbers_from_line,
    _normalize_unit,
    _compute_confidence,
    _is_column_as_segment_table,
    _extract_col_as_segment,
    _find_matching_raw_table,
)


class TestExtractNumbers:
    def test_basic(self):
        nums = _extract_numbers_from_line("建設事業  50,000  3,000")
        assert nums == [50000.0, 3000.0]

    def test_negative(self):
        nums = _extract_numbers_from_line("調整額  △500")
        assert nums == [-500.0]

    def test_no_numbers(self):
        nums = _extract_numbers_from_line("売上高  セグメント利益")
        assert nums == []


class TestNormalizeUnit:
    def test_oku(self):
        assert _normalize_unit(10.0, "億円") == 1000.0

    def test_sen(self):
        assert _normalize_unit(5000.0, "千円") == 5.0

    def test_million(self):
        assert _normalize_unit(50000.0, "百万円") == 50000.0

    def test_no_unit(self):
        assert _normalize_unit(100.0, "") == 100.0


class TestComputeConfidence:
    def test_high_confidence(self):
        conf = _compute_confidence(0.8, 0.7, 0.8, 0.6, 0.7)
        assert conf >= 0.5

    def test_low_confidence(self):
        conf = _compute_confidence(0.1, 0.1, 0.3, 0.1, 0.1)
        assert conf < 0.3


class TestV2DetectionResult:
    def test_empty_result(self):
        r = V2DetectionResult()
        assert not r.success
        assert r.segments == []

    def test_with_quarantine(self):
        r = V2DetectionResult(
            quarantine_reason="no_segment_page_candidate",
            failed_stage="page_scoring",
        )
        assert not r.success


# ============================================================
# TestColumnAsSegment: column-as-segment モードの単体テスト
# ============================================================

# 地域別3セグメント横持ち表のモック (pdfplumber raw_table 形式)
_MOCK_RAW_TABLE_REGION = [
    # ヘッダー行: 日本 / 北米 / 欧州 / 合計 / 調整額 / 連結
    [None,   "日本",  "北米",  "欧州",  "合計",   "調整額",  "連結"],
    # 売上高行
    ["売上高", "50,000", "30,000", "20,000", "100,000", "△1,000", "99,000"],
    # セグメント利益行
    ["セグメント利益", "5,000", "3,000", "2,000", "10,000", "△500", "9,500"],
    # セグメント資産行（指標だが利益系でないため profit 対象外）
    ["セグメント資産", "200,000", "100,000", "80,000", "380,000", "5,000", "385,000"],
]

_MOCK_TABLE_LINES = [
    "         日本  北米  欧州  合計  調整額  連結",
    "売上高   50,000  30,000  20,000  100,000  △1,000  99,000",
    "セグメント利益  5,000  3,000  2,000  10,000  △500  9,500",
    "セグメント資産  200,000  100,000  80,000  380,000  5,000  385,000",
]


class TestColumnAsSegment:
    def test_detection_with_raw_table(self):
        """横持ち表を正しく検出し、セグメント列を特定できる。"""
        info = _is_column_as_segment_table(_MOCK_RAW_TABLE_REGION, [])
        assert info["is_col_seg"] is True
        # 日本(1)/北米(2)/欧州(3) が検出され、合計・調整額・連結は除外される
        assert 1 in info["seg_col_indices"]
        assert 2 in info["seg_col_indices"]
        assert 3 in info["seg_col_indices"]
        assert 4 not in info["seg_col_indices"]  # 合計 除外
        assert 5 not in info["seg_col_indices"]  # 調整額 除外
        assert 6 not in info["seg_col_indices"]  # 連結 除外

    def test_detection_with_text_lines(self):
        """raw_table=None のとき text_lines でも検出できる。"""
        info = _is_column_as_segment_table(None, _MOCK_TABLE_LINES)
        assert info["is_col_seg"] is True

    def test_sales_profit_row_detection(self):
        """売上行・利益行のインデックスが正しく検出される。"""
        info = _is_column_as_segment_table(_MOCK_RAW_TABLE_REGION, [])
        assert info["sales_row_idx"] is not None   # 売上高行が検出
        assert info["profit_row_idx"] is not None  # セグメント利益行が検出

    def test_extraction_segment_names(self):
        """抽出されるセグメント名が列ヘッダー（日本/北米/欧州）のみ。"""
        info = _is_column_as_segment_table(_MOCK_RAW_TABLE_REGION, [])
        records = _extract_col_as_segment(_MOCK_RAW_TABLE_REGION, [], info)
        names = [r["segment_name"] for r in records]
        assert "日本" in names
        assert "北米" in names
        assert "欧州" in names
        # 除外ラベルはセグメントとして出てこない
        assert "合計" not in names
        assert "調整額" not in names
        assert "連結" not in names
        # 指標名がセグメント名に混入しない
        assert "売上高" not in names
        assert "セグメント利益" not in names

    def test_extraction_values(self):
        """日本セグメントの売上・利益が正しく抽出される。"""
        info = _is_column_as_segment_table(_MOCK_RAW_TABLE_REGION, [])
        records = _extract_col_as_segment(_MOCK_RAW_TABLE_REGION, [], info)
        japan = next(r for r in records if r["segment_name"] == "日本")
        assert japan["sales"] == 50000.0
        assert japan["profit"] == 5000.0

    def test_no_detection_for_row_based_table(self):
        """縦持ち（行=セグメント）表では column-as-segment を検出しない。"""
        row_based_lines = [
            "           売上高  セグメント利益",
            "日本事業   50,000  5,000",
            "北米事業   30,000  3,000",
            "欧州事業   20,000  2,000",
        ]
        info = _is_column_as_segment_table(None, row_based_lines)
        assert info["is_col_seg"] is False

    def test_find_matching_raw_table(self):
        """複数テーブルから best_table_lines に最も一致するものを選ぶ。"""
        other_table = [["無関係", "テーブル"], ["A", "B"]]
        tables_on_page = [other_table, _MOCK_RAW_TABLE_REGION]
        matched = _find_matching_raw_table(_MOCK_TABLE_LINES, tables_on_page)
        assert matched is _MOCK_RAW_TABLE_REGION


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
