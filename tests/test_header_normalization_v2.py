#!/usr/bin/env python3
"""test_header_normalization_v2.py — Phase 5: multi-line header 正規化テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.header_analysis import (
    normalize_header,
    normalize_header_for_role,
    reconstruct_header_grid,
    _is_unit_or_note_token,
    _is_unit_or_note_line,
)


class TestNormalizeHeaderForRole:
    def test_remove_unit_parenthesis(self):
        assert normalize_header_for_role("売上高(百万円)") == "売上高"

    def test_remove_unit_fullwidth_paren(self):
        assert normalize_header_for_role("営業利益（百万円）") == "営業利益"

    def test_remove_note_paren(self):
        assert normalize_header_for_role("セグメント利益（注）") == "セグメント利益"

    def test_remove_millions_of_yen(self):
        assert normalize_header_for_role("Amount(Millionsofyen)") == "Amount"

    def test_keep_non_unit_paren(self):
        # 「利益(損失)」は単位でなく意味のある括弧
        result = normalize_header_for_role("利益(損失)")
        assert "利益" in result
        assert "損失" in result

    def test_plain_text_unchanged(self):
        assert normalize_header_for_role("売上高") == "売上高"


class TestIsUnitOrNoteToken:
    def test_unit_hyakuman(self):
        assert _is_unit_or_note_token("（百万円）") is True

    def test_unit_oku(self):
        assert _is_unit_or_note_token("(億円)") is True

    def test_unit_sen(self):
        assert _is_unit_or_note_token("（千円）") is True

    def test_note_chuuki(self):
        assert _is_unit_or_note_token("（注）") is True

    def test_tani_colon(self):
        assert _is_unit_or_note_token("単位：百万円") is True

    def test_millions_of_yen(self):
        assert _is_unit_or_note_token("Millions of yen") is True

    def test_normal_text_is_not_unit(self):
        assert _is_unit_or_note_token("売上高") is False

    def test_empty_is_not_unit(self):
        assert _is_unit_or_note_token("") is False


class TestIsUnitOrNoteLine:
    def test_unit_line(self):
        assert _is_unit_or_note_line("  （百万円）  ") is True

    def test_normal_header_line(self):
        assert _is_unit_or_note_line("  売上高    営業利益  ") is False

    def test_empty_line(self):
        assert _is_unit_or_note_line("") is False


class TestReconstructHeaderGrid:
    def test_unit_line_removed(self):
        """単位行が除去されてヘッダーに含まれない"""
        lines = ["売上高    営業利益", "（百万円）"]
        result = reconstruct_header_grid(lines)
        assert len(result) >= 2
        assert "百万円" not in result[0]
        assert "百万円" not in result[1] if len(result) > 1 else True

    def test_split_header_restored(self):
        """上段「営業」+下段「利益」→「営業利益」復元"""
        lines = ["営業", "利益"]
        result = reconstruct_header_grid(lines)
        assert result[0] == "営業利益"

    def test_two_col_split_header(self):
        """2列の分割ヘッダー: 単位行除去"""
        lines = ["売上高    セグメント利益", "（百万円）"]
        result = reconstruct_header_grid(lines)
        # (百万円) 行は丸ごと除去される → 1行目だけ使用
        assert "売上高" in result[0]
        assert "セグメント利益" in result[1]

    def test_three_row_header(self):
        """3行ヘッダーの結合"""
        lines = [
            "報告セグメント",
            "  売上高    営業利益",
            "（百万円）",
        ]
        result = reconstruct_header_grid(lines)
        assert len(result) >= 1

    def test_empty_input(self):
        assert reconstruct_header_grid([]) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
