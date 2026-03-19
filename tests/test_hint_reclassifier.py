"""tests/test_hint_reclassifier.py — review_hint 再分類テスト"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from analysis.hint_reclassifier import reclassify_candidate_failure


class TestReclassifyToNarrativePage:
    """9-1: detail_breakdown_guard -> no_segment_narrative_page"""

    def test_valid0_noise_high_no_header(self):
        """valid=0, noise=8, header なし → no_segment_narrative_page"""
        r = reclassify_candidate_failure(
            raw_reason="detail_breakdown_guard",
            raw_hint="pdf_segment_like_but_invalid_structure",
            valid_segment=0, narrative=2, garbage=6,
            detail_breakdown=1, bs_cf=0, pl_account=0, total_or_metric=0,
            has_sales_header=False, has_profit_header=False,
        )
        assert r.reclassified is True
        assert r.final_hint == "pdf_no_segment_narrative_page"
        assert r.final_reason == "no_segment_narrative_page"

    def test_valid1_noise_dominant_no_header(self):
        """valid=1, noise=7, header なし → no_segment_narrative_page"""
        r = reclassify_candidate_failure(
            raw_reason="detail_breakdown_guard",
            raw_hint="pdf_segment_like_but_invalid_structure",
            valid_segment=1, narrative=1, garbage=6,
            detail_breakdown=0, bs_cf=0, pl_account=0, total_or_metric=0,
            has_sales_header=False, has_profit_header=False,
        )
        assert r.reclassified is True
        assert r.final_hint == "pdf_no_segment_narrative_page"

    def test_valid0_noise_5_with_header(self):
        """valid=0, noise=5, header あり → still reclassified (Rule 3)"""
        r = reclassify_candidate_failure(
            raw_reason="detail_breakdown_guard",
            raw_hint="pdf_segment_like_but_invalid_structure",
            valid_segment=0, narrative=1, garbage=4,
            detail_breakdown=0, bs_cf=0, pl_account=0, total_or_metric=0,
            has_sales_header=True, has_profit_header=False,
        )
        assert r.reclassified is True
        assert r.final_hint == "pdf_no_segment_narrative_page"


class TestKeepInvalidStructure:
    """9-2: invalid_structure のまま維持"""

    def test_valid2_with_detail(self):
        """valid=2 + detail 多い → invalid_structure 維持"""
        r = reclassify_candidate_failure(
            raw_reason="detail_breakdown_guard",
            raw_hint="pdf_segment_like_but_invalid_structure",
            valid_segment=2, narrative=1, garbage=3,
            detail_breakdown=5, bs_cf=0, pl_account=0, total_or_metric=0,
            has_sales_header=True, has_profit_header=True,
        )
        assert r.reclassified is False
        assert r.final_hint == "pdf_segment_like_but_invalid_structure"

    def test_valid1_with_header(self):
        """valid=1 + header あり + noise 少 → invalid_structure 維持"""
        r = reclassify_candidate_failure(
            raw_reason="detail_breakdown_guard",
            raw_hint="pdf_segment_like_but_invalid_structure",
            valid_segment=1, narrative=0, garbage=2,
            detail_breakdown=3, bs_cf=0, pl_account=0, total_or_metric=0,
            has_sales_header=True, has_profit_header=False,
        )
        assert r.reclassified is False


class TestNonTargetReasons:
    """9-3: 対象外の reason はそのまま返す"""

    def test_narrative_guard_unchanged(self):
        """narrative_guard はそのまま"""
        r = reclassify_candidate_failure(
            raw_reason="narrative_guard",
            raw_hint="pdf_narrative_block_selected",
            valid_segment=0, narrative=5, garbage=2,
            detail_breakdown=0, bs_cf=0, pl_account=0, total_or_metric=0,
            has_sales_header=False, has_profit_header=False,
        )
        assert r.reclassified is False
        assert r.final_hint == "pdf_narrative_block_selected"

    def test_pl_guard_unchanged(self):
        """pl_guard はそのまま"""
        r = reclassify_candidate_failure(
            raw_reason="pl_guard",
            raw_hint="pdf_pl_table_selected",
            valid_segment=0, narrative=0, garbage=0,
            detail_breakdown=0, bs_cf=0, pl_account=5, total_or_metric=0,
            has_sales_header=True, has_profit_header=True,
        )
        assert r.reclassified is False
        assert r.final_hint == "pdf_pl_table_selected"
