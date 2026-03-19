#!/usr/bin/env python3
"""tests/test_section_diff.py — 差分抽出テスト"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.filing_diff.section_diff import (
    diff_sections,
    split_into_sentences_ja,
    tag_diff_keywords,
    _normalize_for_compare,
)


class TestSplitSentences:
    """文分割"""

    def test_basic(self):
        result = split_into_sentences_ja("売上は増加しました。利益も拡大しました。")
        assert len(result) == 2

    def test_short_fragment_excluded(self):
        result = split_into_sentences_ja("短い。これは十分長い文です。")
        # 「短い。」は5文字以下で除外
        assert len(result) == 1

    def test_empty(self):
        result = split_into_sentences_ja("")
        assert result == []


class TestDiffSections:
    """差分計算"""

    def test_identical_text(self):
        text = "売上高は前年同期比で増加しました。営業利益は堅調に推移しました。"
        diff = diff_sections(text, text)
        assert diff.diff_score == 0.0
        assert len(diff.added_sentences) == 0
        assert len(diff.removed_sentences) == 0

    def test_added_sentence(self):
        prev = "売上高は前年同期比で増加しました。"
        curr = "売上高は前年同期比で増加しました。在庫調整の影響が見られました。"
        diff = diff_sections(prev, curr)
        assert len(diff.added_sentences) > 0

    def test_removed_sentence(self):
        prev = "売上高は増加しました。原材料高が利益を圧迫しました。"
        curr = "売上高は増加しました。"
        diff = diff_sections(prev, curr)
        assert len(diff.removed_sentences) > 0

    def test_changed_sentence(self):
        prev = "需要は堅調に推移しました。"
        curr = "需要は一部分野で弱含みとなりました。"
        diff = diff_sections(prev, curr)
        # 類似度が閾値以上なら changed, 以下なら added+removed
        assert len(diff.changed_pairs) > 0 or (
            len(diff.added_sentences) > 0 and len(diff.removed_sentences) > 0
        )

    def test_number_only_diff_normalized(self):
        prev = "売上高は12,345百万円となりました。"
        curr = "売上高は13,456百万円となりました。"
        diff = diff_sections(prev, curr)
        # 数値差のみ → ノイズとして同一扱い
        assert diff.diff_score < 0.1

    def test_diff_score_range(self):
        prev = "完全に異なるテキストが前回にはありました。"
        curr = "今回は全く新しい内容に置き換わっています。"
        diff = diff_sections(prev, curr)
        assert 0.0 <= diff.diff_score <= 1.0


class TestNormalizeForCompare:
    """比較用正規化"""

    def test_numbers_replaced(self):
        result = _normalize_for_compare("売上高は12,345百万円")
        assert "12,345" not in result
        assert "NUM" in result

    def test_date_replaced(self):
        result = _normalize_for_compare("2026年3月期")
        assert "DATE" in result


class TestTagKeywords:
    """キーワードタグ付け"""

    def test_demand(self):
        tags = tag_diff_keywords("需要は堅調に推移しました")
        assert "需要" in tags
        assert "堅調" in tags

    def test_inventory(self):
        tags = tag_diff_keywords("在庫調整の影響が見られました")
        assert "在庫調整" in tags

    def test_price_revision(self):
        tags = tag_diff_keywords("価格改定の寄与がありました")
        assert "価格改定" in tags

    def test_raw_materials(self):
        tags = tag_diff_keywords("原材料高が利益を圧迫しました")
        assert "原材料高" in tags

    def test_uncertainty(self):
        tags = tag_diff_keywords("先行き不透明感が高まっている")
        assert "不透明感" in tags

    def test_downward_revision(self):
        tags = tag_diff_keywords("通期予想を下方修正しました")
        assert "下方修正" in tags

    def test_empty(self):
        tags = tag_diff_keywords("特になし")
        assert tags == []

    def test_multiple_keywords(self):
        tags = tag_diff_keywords("円安の影響と原材料高により減益となりました")
        assert "円安" in tags
        assert "原材料高" in tags
        assert "減益" in tags
