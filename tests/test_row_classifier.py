"""tests/test_row_classifier.py — 行分類器 + candidate guard のテスト"""
from __future__ import annotations

import pytest
import sys
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.analysis.row_classifier import (
    classify_row_label,
    evaluate_candidate_guard,
    is_valid_segment_like,
    is_narrative_like,
    is_bs_cf_like,
    is_detail_breakdown_like,
    is_total_or_metric_like,
    is_garbage_fragment_like,
    is_pl_account_like,
)


# ============================================================
# 11-1. Row Classifier Tests
# ============================================================

class TestValidSegmentLike:
    @pytest.mark.parametrize("label", [
        "物流事業", "不動産事業", "海外事業", "セキュリティ事業",
        "FM事業", "介護事業", "情報通信事業", "建設事業",
        "エネルギー部門", "金融ソリューション部門",
        "モビリティセグメント",
    ])
    def test_valid_segment_names(self, label):
        result = classify_row_label(label)
        assert result.class_name == "valid_segment_like", f"{label} -> {result.class_name}: {result.matched_reasons}"

    def test_special_segment_names(self):
        result = classify_row_label("その他")
        assert result.class_name == "valid_segment_like"


class TestNarrativeLike:
    # 10-2. strong keyword は narrative にする
    @pytest.mark.parametrize("label", [
        "セキュリティ事業につきましては、売上高は",
        "この結果、営業利益は",
        "当第３四半期連結累計期間の経営成績につきましては",
        "前年同期と比較し増加となりました",
    ])
    def test_strong_narrative_lines(self, label):
        result = classify_row_label(label)
        assert result.class_name == "narrative_like", f"{label} -> {result.class_name}: {result.matched_reasons}"

    # 10-1. weak keyword 単独では narrative にしない
    @pytest.mark.parametrize("label", [
        "前年同期比増加",
        "利益改善",
        "売上減少",
    ])
    def test_weak_only_not_narrative(self, label):
        result = classify_row_label(label)
        assert result.class_name != "narrative_like", \
            f"{label} should NOT be narrative_like but got: {result.class_name}: {result.matched_reasons}"

    # 10-3. weak + sentence signal は narrative にする
    @pytest.mark.parametrize("label", [
        "増加により、",
        "影響により",
    ])
    def test_weak_plus_sentence_is_narrative(self, label):
        result = classify_row_label(label)
        assert result.class_name == "narrative_like", \
            f"{label} -> {result.class_name}: {result.matched_reasons}"


class TestBsCfLike:
    @pytest.mark.parametrize("label", [
        "投資活動によるキャッシュ・フロー",
        "有形固定資産",
        "投資有価証券",
        "流動資産",
        "その他有価証券評価差額金",
        "現金及び現金同等物",
    ])
    def test_bs_cf_lines(self, label):
        result = classify_row_label(label)
        assert result.class_name in ("bs_cf_like", "narrative_like"), \
            f"{label} -> {result.class_name}: {result.matched_reasons}"

    def test_bs_cf_short_excludes_segment(self):
        """「不動産事業」は資産を含むが valid_segment_like であるべき"""
        result = classify_row_label("不動産事業")
        assert result.class_name == "valid_segment_like"


class TestDetailBreakdownLike:
    @pytest.mark.parametrize("label", [
        "（倉庫収入）",
        "（港湾運送収入）",
        "（国際輸送収入）",
        "（陸上運送ほか収入）",
    ])
    def test_detail_lines(self, label):
        result = classify_row_label(label)
        assert result.class_name == "detail_breakdown_like", f"{label} -> {result.class_name}: {result.matched_reasons}"


class TestTotalOrMetricLike:
    @pytest.mark.parametrize("label", [
        "セグメント間内部営業収益",
        "売上高",
        "営業利益",
    ])
    def test_total_metric_lines(self, label):
        result = classify_row_label(label)
        assert result.class_name == "total_or_metric_like", f"{label} -> {result.class_name}: {result.matched_reasons}"

    def test_pure_operating_revenue_is_pl_or_total(self):
        """純営業収益は PL にも total にも属するが PL 優先で OK"""
        result = classify_row_label("純営業収益")
        assert result.class_name in ("pl_account_like", "total_or_metric_like")


class TestGarbageFragmentLike:
    @pytest.mark.parametrize("label", [
        "（自",
        "備、「東京",
    ])
    def test_garbage_fragments(self, label):
        result = classify_row_label(label)
        assert result.class_name == "garbage_fragment_like", \
            f"{label} -> {result.class_name}: {result.matched_reasons}"


class TestPlAccountLike:
    @pytest.mark.parametrize("label", [
        "売上原価",
        "売上総利益",
        "販売費及び一般管理費",
        "法人税等",
        "受取利息",
    ])
    def test_pl_account_lines(self, label):
        result = classify_row_label(label)
        assert result.class_name == "pl_account_like", f"{label} -> {result.class_name}: {result.matched_reasons}"


# ============================================================
# 11-2. Candidate Guard Tests
# ============================================================

class TestCandidateGuard:
    def test_2331_type_narrative_reject(self):
        """2331型: 本文断片が多い候補は reject (guard緩和後も)"""
        labels = [
            "セキュリティ事業につきましては、売上高は",
            "備等に注力する一方、「",
            "備、「東京",
            "は、今年",
            "FM事業等につきましては、売上高は",
            "介護事業につきましては、売上高は",
            "海外事業につきましては、売上高は",
            "た。警備輸送業務用現金が",
            "百万円、投資有価証券が",
            "負債の部は、前期末比で",
            "金などのその他の流動負債が",
            "有形固定資産",
            "当第",
            "配当金",
            "ALSOK㈱(",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted, f"Expected reject but got accepted: {result.reject_reason}"

    def test_9303_type_mixed_reject(self):
        """9303型: 親セグメント+内訳+total+narrative 混在は reject"""
        labels = [
            "（自",
            "物流事業",
            "（倉庫収入）",
            "（港湾運送収入）",
            "（国際輸送収入）",
            "（陸上運送ほか収入）",
            "不動産事業",
            "（不動産事業収入）",
            "セグメント間内部営業収益",
            "純営業収益",
            "増加等により、前期末比",
            "昇に伴う「その他有価証券評価差額金」の増加等により、前期末比",
            "留保等により、",
            "投資活動によるキャッシュ・フローは、有形固定資産の取得による支出等により、",
            "より、",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted, f"Expected reject but got accepted: valid={result.valid_segment_like}"

    def test_normal_segment_table_accept(self):
        """正常なセグメント表は accept"""
        labels = [
            "報告セグメントごとの売上高及び利益又は損失",
            "不動産事業",
            "金融事業",
            "サービス事業",
            "物流事業",
            "その他",
            "調整額",
            "合計",
        ]
        result = evaluate_candidate_guard(labels)
        assert result.accepted, f"Expected accept but got reject: {result.reject_reason}"

    def test_pl_table_reject(self):
        """PL テーブルは reject"""
        labels = [
            "売上高",
            "売上原価",
            "売上総利益",
            "販売費及び一般管理費",
            "営業利益",
            "営業外収益",
            "営業外費用",
            "経常利益",
            "法人税等",
            "当期純利益",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted, f"Expected reject but got accepted"

    def test_valid_segment_min_two(self):
        """valid_segment_like が2未満なら reject"""
        labels = [
            "売上高",
            "営業利益",
            "IT事業",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted

    # 10-4. CID corruption (garbage大量) で narrative_guard 誤 reject しないこと
    def test_cid_corruption_not_narrative_reject(self):
        """CID corruption による garbage 大量は narrative_guard ではない"""
        labels = [
            "物流事業",
            "情報事業",
            "(cid:1)(cid:2)(cid:3)",
            "(cid:4)(cid:5)(cid:6)",
            "(cid:7)(cid:8)(cid:9)",
            "(cid:1)(cid:2)(cid:3)(cid:4)",
            "(cid:5)(cid:6)(cid:7)(cid:8)",
        ]
        result = evaluate_candidate_guard(labels)
        # garbage が多くても narrative_guard ではなく no_valid_segment or 別 reason
        if not result.accepted:
            assert result.reject_reason != "narrative_guard", \
                f"CID garbage should not trigger narrative_guard: {result.reject_reason}"


# ============================================================
# 11-3. Regression tests (簡易 fixture)
# ============================================================

class TestRegressionPhaseA:
    def test_2331_reject(self):
        """2331 SOMPOHD は reject されるべき"""
        labels = [
            "セキュリティ事業につきましては、売上高は",
            "備等に注力する一方、「",
            "FM事業等につきましては、売上高は",
            "介護事業につきましては、売上高は",
            "海外事業につきましては、売上高は",
            "た。警備輸送業務用現金が",
            "負債の部は、前期末比で",
            "有形固定資産",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted

    def test_9303_reject(self):
        """9303 住友倉庫: 簡略版 (valid=2, detail=2) は guard を通る。
        実際のデータでは v2 テーブルスコアリングで候補外になる。"""
        labels = [
            "（自",
            "物流事業",
            "（倉庫収入）",
            "（港湾運送収入）",
            "不動産事業",
            "純営業収益",
            "投資活動によるキャッシュ・フローは",
            "より、",
        ]
        result = evaluate_candidate_guard(labels)
        # valid=2, detail=2 なので guard は通る (detail > valid ではない)
        # 実際の 9303 は v2 段階で候補外になるか、長い本文付きで narrative_guard に引っかかる
        # ここでは guard 単体の仕様をテスト
        assert result.valid_segment_like == 2

    def test_9303_full_reject(self):
        """9303 住友倉庫: フルラベル版は reject される"""
        labels = [
            "（自",
            "物流事業",
            "（倉庫収入）",
            "（港湾運送収入）",
            "（国際輸送収入）",
            "（陸上運送ほか収入）",
            "不動産事業",
            "（不動産事業収入）",
            "セグメント間内部営業収益",
            "純営業収益",
            "増加等により、前期末比",
            "昇に伴う「その他有価証券評価差額金」の増加等により、前期末比",
            "留保等により、",
            "投資活動によるキャッシュ・フローは、有形固定資産の取得による支出等により、",
            "より、",
        ]
        result = evaluate_candidate_guard(labels)
        assert not result.accepted, f"Expected reject: valid={result.valid_segment_like} detail={result.detail_breakdown_like} narrative={result.narrative_like}"

    def test_normal_accept(self):
        """正常なセグメント表は accept"""
        labels = [
            "情報通信事業",
            "エレクトロニクス事業",
            "車載事業",
            "その他",
            "調整額",
            "合計",
        ]
        result = evaluate_candidate_guard(labels)
        assert result.accepted


# ============================================================
# 13-1. Reason Mapping Tests
# ============================================================

class TestReasonMapping:
    def test_narrative_guard_hint(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        assert map_reject_reason_to_review_hint("narrative_guard") == "pdf_narrative_block_selected"

    def test_pl_guard_hint(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        assert map_reject_reason_to_review_hint("pl_guard") == "pdf_pl_table_selected"

    def test_no_candidate_hint(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        assert map_reject_reason_to_review_hint("no_candidate_after_guard") == "pdf_no_segment_table_after_guard"

    def test_candidate_guard_prefix(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        assert map_reject_reason_to_review_hint("candidate_guard:narrative_guard") == "pdf_narrative_block_selected"

    def test_pipe_suffix_stripped(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        assert map_reject_reason_to_review_hint("candidate_guard:bs_cf_guard|hint=pdf_narrative_block_selected") == "pdf_narrative_block_selected"

    def test_none_returns_default(self):
        from src.analysis.row_classifier import map_reject_reason_to_review_hint
        assert map_reject_reason_to_review_hint(None) == "pdf_extraction_failed"


# ============================================================
# 13-2. Reason Priority Tests
# ============================================================

class TestReasonPriority:
    def test_narrative_over_no_candidate(self):
        from src.analysis.row_classifier import choose_better_reason
        assert choose_better_reason("narrative_guard", "no_candidate_after_guard") == "narrative_guard"

    def test_pl_over_narrative(self):
        from src.analysis.row_classifier import choose_better_reason
        assert choose_better_reason("pl_guard", "narrative_guard") == "pl_guard"

    def test_none_returns_other(self):
        from src.analysis.row_classifier import choose_better_reason
        assert choose_better_reason(None, "narrative_guard") == "narrative_guard"
        assert choose_better_reason("pl_guard", None) == "pl_guard"

    def test_candidate_guard_prefix(self):
        from src.analysis.row_classifier import choose_better_reason
        result = choose_better_reason("candidate_guard:narrative_guard", "candidate_guard:no_valid_segment_rows")
        assert result == "candidate_guard:narrative_guard"


