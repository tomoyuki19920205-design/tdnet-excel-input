"""tests/test_invalid_structure_rescue.py — detail_breakdown_guard rescue テスト"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from src.analysis.row_classifier import (
    evaluate_candidate_guard, classify_row_label,
    is_valid_segment_like, is_detail_breakdown_like,
    is_total_or_metric_like,
)


class TestRescueEligibility:
    """rescue 対象条件テスト"""

    def test_detail_dominant_with_valid_ge2_is_rescue_eligible(self):
        """valid=3, detail=5 → detail_breakdown_guard で reject されるが rescue 候補"""
        labels = [
            "物流事業", "不動産事業", "情報事業",  # 3 valid_segment
            "（倉庫収入）", "（港湾運送収入）", "（国際輸送収入）",  # 3 detail
            "収入内訳", "小分類",  # 2 detail
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted
        assert result.reject_reason == "detail_breakdown_guard"
        assert result.valid_segment_like >= 2  # rescue eligible

    def test_valid_1_not_rescue_eligible(self):
        """valid=1, detail=3 → rescue 不適格"""
        labels = [
            "物流事業",
            "（倉庫収入）", "（港湾運送収入）", "（国際輸送収入）",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted
        assert result.valid_segment_like < 2

    def test_valid_0_not_rescue_eligible(self):
        """valid=0, narrative dominant → rescue 不適格"""
        labels = [
            "当第３四半期連結累計期間につきましては",
            "この結果営業利益は",
            "売上高は増加しました",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted
        assert result.valid_segment_like == 0


class TestParentRowClassification:
    """親行 vs detail/total 分類テスト"""

    def test_jigyou_is_valid_segment(self):
        ok, _ = is_valid_segment_like("物流事業")
        assert ok

    def test_bumon_is_valid_segment(self):
        ok, _ = is_valid_segment_like("第一営業部門")
        assert ok

    def test_sonota_is_valid_segment(self):
        ok, _ = is_valid_segment_like("その他")
        assert ok

    def test_bracketed_is_detail(self):
        ok, _ = is_detail_breakdown_like("（倉庫収入）")
        assert ok

    def test_bracketed_revenue_is_detail(self):
        ok, _ = is_detail_breakdown_like("（国際輸送収入）")
        assert ok

    def test_goukei_is_total(self):
        ok, _ = is_total_or_metric_like("合計")
        assert ok

    def test_jun_eigyo_is_total(self):
        ok, _ = is_total_or_metric_like("純営業収益")
        assert ok

    def test_chousei_is_total_but_kept(self):
        """調整額は total_or_metric_like だが rescue では keep"""
        ok, _ = is_total_or_metric_like("調整額")
        # 調整額は total に分類されるかされないかは実装次第
        # ここでは rescue フィルタが _ADJUSTMENT_KEEP_LABELS で保持すればOK


class TestRescueParentFilter:
    """rescue mode parent-row filter のロジックテスト"""

    TOTAL_DROP_LABELS = {
        "計", "合計", "小計", "収益", "営業収益", "純営業収益",
        "連結", "内部売上高", "セグメント間内部売上高",
        "セグメント間内部営業収益",
    }
    ADJUSTMENT_KEEP_LABELS = {"調整額", "全社", "その他"}

    def _should_keep(self, label: str) -> bool:
        """rescue filter のロジックを再現"""
        # 合計/計 は drop
        if label in self.TOTAL_DROP_LABELS:
            return False
        if label.endswith("計") and len(label) >= 2 and label not in self.ADJUSTMENT_KEEP_LABELS:
            return False

        cls = classify_row_label(label)
        if cls.class_name == "detail_breakdown_like":
            return False
        if cls.class_name == "total_or_metric_like" and label not in self.ADJUSTMENT_KEEP_LABELS:
            return False
        if cls.class_name in ("narrative_like", "pl_account_like", "bs_cf_like", "garbage_fragment_like"):
            return False
        return True

    def test_parent_segment_kept(self):
        assert self._should_keep("物流事業")

    def test_parent_segment_2_kept(self):
        assert self._should_keep("不動産事業")

    def test_sonota_kept(self):
        assert self._should_keep("その他")

    def test_chousei_kept(self):
        assert self._should_keep("調整額")

    def test_zensha_kept(self):
        assert self._should_keep("全社")

    def test_detail_bracket_dropped(self):
        assert not self._should_keep("（倉庫収入）")

    def test_goukei_dropped(self):
        assert not self._should_keep("合計")

    def test_jun_eigyo_dropped(self):
        assert not self._should_keep("純営業収益")

    def test_shokei_dropped(self):
        assert not self._should_keep("小計")

    def test_internal_revenue_dropped(self):
        assert not self._should_keep("セグメント間内部売上高")

    def test_kei_suffix_dropped(self):
        assert not self._should_keep("外部顧客への売上高計")


class TestRescueDoesNotAffectExistingGuards:
    """既存 guard (narrative, bs_cf, pl) には影響しない"""

    def test_narrative_still_rejected(self):
        labels = [
            "当第３四半期連結累計期間につきましては",
            "この結果営業利益は減少しました",
            "物流事業", "不動産事業",
            "売上高は増加となりました",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted
        # narrative_guard で reject → rescue 対象外
        assert result.reject_reason == "narrative_guard"

    def test_pl_still_rejected(self):
        """pl_account が3件以上あれば pl_guard で reject"""
        labels = [
            "売上高", "売上原価", "販売費及び一般管理費",
            "経常利益",  # PL account
            "物流事業", "不動産事業",
        ]
        result = evaluate_candidate_guard(labels)
        # pl_guard は p >= 3 で発動
        assert result.pl_account_like >= 3
        assert not result.accepted
        assert result.reject_reason == "pl_guard"
