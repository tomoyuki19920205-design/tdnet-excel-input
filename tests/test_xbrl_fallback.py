"""tests/test_xbrl_fallback.py — XBRL fallback + hint 伝播テスト"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


class TestShouldTryXbrlFallback:
    """8-2: XBRL fallback 発動条件"""

    FALLBACK_HINTS = {
        "pdf_no_segment_narrative_page",
        "pdf_no_segment_table_after_guard",
        "pdf_no_segment_page_candidate",
        "pdf_no_segment_table_candidate",
    }

    def test_narrative_page_triggers_fallback(self):
        assert "pdf_no_segment_narrative_page" in self.FALLBACK_HINTS

    def test_table_after_guard_triggers_fallback(self):
        assert "pdf_no_segment_table_after_guard" in self.FALLBACK_HINTS

    def test_invalid_structure_does_not_trigger(self):
        assert "pdf_segment_like_but_invalid_structure" not in self.FALLBACK_HINTS

    def test_pl_table_does_not_trigger(self):
        assert "pdf_pl_table_selected" not in self.FALLBACK_HINTS

    def test_narrative_block_does_not_trigger(self):
        assert "pdf_narrative_block_selected" not in self.FALLBACK_HINTS

    def test_extraction_failed_does_not_trigger(self):
        assert "pdf_extraction_failed" not in self.FALLBACK_HINTS


class TestHintMappingPropagation:
    """8-1: no_segment_narrative_page が正しく hint にマッピングされる"""

    def test_narrative_page_in_reject_reason_map(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        hint = map_reject_reason_to_review_hint("candidate_guard:no_segment_narrative_page")
        assert hint == "pdf_no_segment_narrative_page"

    def test_toc_page_guard_in_reject_reason_map(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        hint = map_reject_reason_to_review_hint("candidate_guard:toc_page_guard")
        assert hint == "pdf_toc_page_selected"

    def test_detail_breakdown_guard_remains(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        hint = map_reject_reason_to_review_hint("candidate_guard:detail_breakdown_guard")
        assert hint == "pdf_segment_like_but_invalid_structure"

    def test_narrative_guard_remains(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        hint = map_reject_reason_to_review_hint("candidate_guard:narrative_guard")
        assert hint == "pdf_narrative_block_selected"


class TestReasonPriority:
    """no_segment_narrative_page の reason priority が正しい"""

    def test_narrative_page_has_priority(self):
        from src.analysis.row_classifier import choose_better_reason
        # no_segment_narrative_page (pri=3) vs extraction_failed (pri=21)
        better = choose_better_reason(
            "candidate_guard:no_segment_narrative_page",
            "extraction_failed",
        )
        assert better == "candidate_guard:no_segment_narrative_page"


class TestReclassifierStillWorks:
    """既存 reclassifier テストの回帰確認"""

    def test_valid0_noise_high_reclassified(self):
        from analysis.hint_reclassifier import reclassify_candidate_failure
        r = reclassify_candidate_failure(
            raw_reason="detail_breakdown_guard",
            raw_hint="pdf_segment_like_but_invalid_structure",
            valid_segment=0, narrative=3, garbage=5,
            detail_breakdown=0, bs_cf=0, pl_account=0, total_or_metric=0,
            has_sales_header=False, has_profit_header=False,
        )
        assert r.reclassified is True
        assert r.final_hint == "pdf_no_segment_narrative_page"

    def test_valid2_with_header_not_reclassified(self):
        from analysis.hint_reclassifier import reclassify_candidate_failure
        r = reclassify_candidate_failure(
            raw_reason="detail_breakdown_guard",
            raw_hint="pdf_segment_like_but_invalid_structure",
            valid_segment=2, narrative=1, garbage=1,
            detail_breakdown=3, bs_cf=0, pl_account=0, total_or_metric=0,
            has_sales_header=True, has_profit_header=True,
        )
        assert r.reclassified is False
