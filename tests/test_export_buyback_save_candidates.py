"""test_export_buyback_save_candidates.py — 保存候補切り出しツールのテスト"""
from __future__ import annotations

import csv
import os
import sys
import tempfile

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from tools.export_buyback_save_candidates import (
    is_save_candidate,
    is_manual_review_candidate,
    split_candidates,
    generate_summary,
    load_review_results,
    _safe_float,
    _safe_int,
    _safe_bool,
)


# ============================================================
# Fixture: テスト用行データ
# ============================================================

def _row(
    *,
    review_bucket="high_confidence_extracted",
    is_buyback_related="True",
    confidence_final="1.0",
    extracted_fields_count="6",
    manifest_review_priority="medium",
    event_type="buyback_decision",
    missing_key_fields="",
    **kw,
) -> dict:
    return {
        "file_path": "/tmp/test.pdf",
        "file_name": "test.pdf",
        "ticker": "1234",
        "disclosure_date": "2026-01-01",
        "title": "テスト文書",
        "review_bucket": review_bucket,
        "is_buyback_related": is_buyback_related,
        "confidence_final": confidence_final,
        "extracted_fields_count": extracted_fields_count,
        "manifest_review_priority": manifest_review_priority,
        "event_type": event_type,
        "missing_key_fields": missing_key_fields,
        "matched_keywords": "自己株式の取得",
        "manifest_candidate_score": "5",
        **kw,
    }


# ============================================================
# 安全変換
# ============================================================

class TestSafeConversions:
    def test_safe_float_normal(self):
        assert _safe_float("1.5") == 1.5

    def test_safe_float_none(self):
        assert _safe_float(None) == 0.0

    def test_safe_float_empty(self):
        assert _safe_float("") == 0.0

    def test_safe_int_normal(self):
        assert _safe_int("6") == 6

    def test_safe_int_float_str(self):
        assert _safe_int("6.0") == 6

    def test_safe_bool_true(self):
        assert _safe_bool("True") is True

    def test_safe_bool_false(self):
        assert _safe_bool("False") is False

    def test_safe_bool_empty(self):
        assert _safe_bool("") is False


# ============================================================
# is_save_candidate
# ============================================================

class TestIsSaveCandidate:
    def test_high_confidence_extracted_is_save(self):
        r = _row()
        ok, reason = is_save_candidate(r)
        assert ok is True
        assert "high_confidence_extracted" in reason

    def test_classifier_only_not_save(self):
        r = _row(review_bucket="classifier_only")
        ok, _ = is_save_candidate(r)
        assert ok is False

    def test_non_buyback_not_save(self):
        r = _row(is_buyback_related="False")
        ok, _ = is_save_candidate(r)
        assert ok is False

    def test_low_confidence_not_save(self):
        r = _row(confidence_final="0.3")
        ok, _ = is_save_candidate(r)
        assert ok is False

    def test_zero_fields_not_save(self):
        r = _row(extracted_fields_count="0")
        ok, _ = is_save_candidate(r)
        assert ok is False

    def test_confidence_threshold_exact(self):
        r = _row(confidence_final="0.60")
        ok, _ = is_save_candidate(r, min_confidence=0.60)
        assert ok is True

    def test_confidence_threshold_below(self):
        r = _row(confidence_final="0.59")
        ok, _ = is_save_candidate(r, min_confidence=0.60)
        assert ok is False

    def test_with_core_fields_reason(self):
        r = _row(extracted_fields_count="3")
        ok, reason = is_save_candidate(r)
        assert ok is True
        assert reason == "high_confidence_extracted_with_core_fields"

    def test_without_core_fields_reason(self):
        r = _row(extracted_fields_count="1")
        ok, reason = is_save_candidate(r)
        assert ok is True
        assert reason == "high_confidence_extracted"


# ============================================================
# is_manual_review_candidate
# ============================================================

class TestIsManualReviewCandidate:
    def test_classifier_only_is_review(self):
        r = _row(review_bucket="classifier_only")
        ok, reason = is_manual_review_candidate(r)
        assert ok is True
        assert reason == "classifier_only"

    def test_low_confidence_is_review(self):
        r = _row(review_bucket="low_confidence")
        ok, reason = is_manual_review_candidate(r)
        assert ok is True
        assert reason == "low_confidence"

    def test_extraction_failed_is_review(self):
        r = _row(review_bucket="extraction_failed")
        ok, reason = is_manual_review_candidate(r)
        assert ok is True
        assert reason == "extraction_failed"

    def test_cancel_missing_fields(self):
        r = _row(
            review_bucket="classifier_only",
            event_type="treasury_cancel",
            extracted_fields_count="0",
        )
        ok, reason = is_manual_review_candidate(r)
        assert ok is True
        # classifier_only takes priority in the logic
        assert "classifier_only" in reason or "cancel" in reason

    def test_non_buyback_not_review(self):
        r = _row(review_bucket="non_buyback", is_buyback_related="False")
        ok, _ = is_manual_review_candidate(r)
        assert ok is False

    def test_low_priority_excluded(self):
        r = _row(
            review_bucket="classifier_only",
            manifest_review_priority="low",
        )
        ok, _ = is_manual_review_candidate(r)
        assert ok is False

    def test_include_priority_custom(self):
        r = _row(
            review_bucket="classifier_only",
            manifest_review_priority="high",
        )
        ok, _ = is_manual_review_candidate(r, include_priorities={"high"})
        assert ok is True

    def test_excluded_bucket_not_review(self):
        r = _row(review_bucket="excluded", is_buyback_related="True")
        ok, _ = is_manual_review_candidate(r)
        assert ok is False


# ============================================================
# split_candidates
# ============================================================

class TestSplitCandidates:
    def _make_rows(self):
        return [
            _row(),  # save candidate
            _row(review_bucket="classifier_only"),  # manual review
            _row(manifest_review_priority="low"),  # skipped (low)
            _row(is_buyback_related="False",
                 review_bucket="non_buyback"),  # skipped (non_buyback)
            _row(review_bucket="classifier_only",
                 event_type="treasury_cancel",
                 extracted_fields_count="0"),  # manual review
        ]

    def test_save_count(self):
        save, _, _ = split_candidates(self._make_rows())
        assert len(save) == 1

    def test_manual_review_count(self):
        _, review, _ = split_candidates(self._make_rows())
        assert len(review) == 2

    def test_skipped_count(self):
        _, _, skip = split_candidates(self._make_rows())
        assert len(skip) == 2

    def test_save_has_reason(self):
        save, _, _ = split_candidates(self._make_rows())
        assert save[0].get("save_reason")

    def test_review_has_reason(self):
        _, review, _ = split_candidates(self._make_rows())
        assert all(r.get("review_reason") for r in review)

    def test_strict_confidence(self):
        rows = [_row(confidence_final="0.70")]
        save, review, _ = split_candidates(
            rows, min_confidence=0.80,
        )
        assert len(save) == 0
        # goes to manual_review or skipped depending on priority

    def test_strict_fields(self):
        rows = [_row(extracted_fields_count="0")]
        save, _, _ = split_candidates(
            rows, min_extracted_fields=1,
        )
        assert len(save) == 0


# ============================================================
# generate_summary
# ============================================================

class TestGenerateSummary:
    def test_summary_not_empty(self):
        md = generate_summary(
            review_path="test.csv",
            total_rows=5,
            save_candidates=[_row()],
            manual_review=[_row(review_bucket="classifier_only",
                                review_reason="classifier_only")],
            skipped=[_row(manifest_review_priority="low")],
            min_confidence=0.60,
            min_extracted_fields=1,
            include_priorities={"medium", "high"},
        )
        assert "Buyback Review Operation" in md
        assert "save candidates" in md.lower() or "save_candidates" in md

    def test_summary_includes_params(self):
        md = generate_summary(
            review_path="test.csv",
            total_rows=1,
            save_candidates=[],
            manual_review=[],
            skipped=[],
            min_confidence=0.80,
            min_extracted_fields=2,
            include_priorities={"high"},
        )
        assert "0.8" in md
        assert "high" in md


# ============================================================
# load_review_results
# ============================================================

class TestLoadReviewResults:
    def test_load_valid_csv(self):
        tmp = tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False, mode="w",
            encoding="utf-8", newline="",
        )
        writer = csv.DictWriter(tmp, fieldnames=[
            "file_path", "review_bucket", "is_buyback_related",
            "confidence_final", "extracted_fields_count",
            "manifest_review_priority", "event_type",
        ])
        writer.writeheader()
        writer.writerow({
            "file_path": "/tmp/test.pdf",
            "review_bucket": "high_confidence_extracted",
            "is_buyback_related": "True",
            "confidence_final": "1.0",
            "extracted_fields_count": "6",
            "manifest_review_priority": "medium",
            "event_type": "buyback_decision",
        })
        tmp.close()
        try:
            rows = load_review_results(tmp.name)
            assert len(rows) == 1
        finally:
            os.unlink(tmp.name)

    def test_load_nonexistent(self):
        with pytest.raises(FileNotFoundError):
            load_review_results("/nonexistent/path.csv")


# ============================================================
# 回帰テスト: review 本体に影響がないこと
# ============================================================

class TestExistingReviewUnaffected:
    """export ツールのインポートが既存モジュールを壊さない"""

    def test_import_review(self):
        """review_buyback_extraction.py がインポートできる"""
        import tools.review_buyback_extraction  # noqa: F401

    def test_import_export(self):
        """export_buyback_save_candidates.py がインポートできる"""
        import tools.export_buyback_save_candidates  # noqa: F401
