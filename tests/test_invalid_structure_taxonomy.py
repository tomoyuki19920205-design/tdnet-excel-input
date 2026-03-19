"""tests/test_invalid_structure_taxonomy.py — taxonomy 分類ロジックの最小テスト"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from analysis.invalid_structure_taxonomy import classify_invalid_structure


class TestTaxonomyClassification:
    """各カテゴリの分類が安定して動作する"""

    def test_parent_detail_mixed(self):
        """valid>=2 → parent_detail_mixed"""
        r = classify_invalid_structure(valid=3, narr=2, garbage=7)
        assert r["primary_category"] == "parent_detail_mixed"

    def test_no_true_parent_rows_garbage_dominant(self):
        """valid=0, garbage>=3 → no_true_parent_rows"""
        r = classify_invalid_structure(valid=0, narr=0, garbage=8)
        assert r["primary_category"] == "no_true_parent_rows"

    def test_no_true_parent_rows_mixed(self):
        """valid=0, narr+garbage mixed → no_true_parent_rows"""
        r = classify_invalid_structure(valid=0, narr=1, garbage=6)
        assert r["primary_category"] == "no_true_parent_rows"

    def test_sparse_or_shifted_table(self):
        """valid=1 drowned in noise → sparse_or_shifted_table"""
        r = classify_invalid_structure(valid=1, narr=1, garbage=5)
        assert r["primary_category"] == "sparse_or_shifted_table"

    def test_header_broken(self):
        """valid=0, narr=0, garbage=0 → header_broken"""
        r = classify_invalid_structure(valid=0, narr=0, garbage=0)
        assert r["primary_category"] == "header_broken"

    def test_narrative_table_like(self):
        """narr dominant → narrative_table_like"""
        r = classify_invalid_structure(valid=0, narr=3, garbage=0)
        assert r["primary_category"] == "narrative_table_like"

    def test_toc_page_misdetected(self):
        """has_toc_pattern=True → toc_page_misdetected"""
        r = classify_invalid_structure(valid=0, narr=0, garbage=6, has_toc_pattern=True)
        assert r["primary_category"] == "toc_page_misdetected"

    def test_secondary_category_when_narr_high(self):
        """parent_detail_mixed + narr>=2 → secondary=narrative_table_like"""
        r = classify_invalid_structure(valid=3, narr=3, garbage=5)
        assert r["secondary_category"] == "narrative_table_like"

    def test_returns_reasons(self):
        """分類理由が空でない"""
        r = classify_invalid_structure(valid=2, narr=1, garbage=3)
        assert len(r["reasons"]) >= 1
