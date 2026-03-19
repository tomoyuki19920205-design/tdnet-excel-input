#!/usr/bin/env python3
"""tests/test_tune_buyback_scanner_score.py

Scanner score tuning ツールの単体テスト。
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from tools.find_buyback_candidate_docs import (
    build_default_rules,
    load_scoring_rules,
    score_candidate_with_details,
    classify_review_priority,
    KeywordHit,
    STRONG_SCORE,
    HIGH_PRIORITY_THRESHOLD,
    MEDIUM_PRIORITY_THRESHOLD,
)
from tools.tune_buyback_scanner_score import (
    rescore_candidate,
    join_candidates_and_review,
    assign_tuning_label,
    suggest_keyword_adjustment,
    build_priority_comparison,
    build_priority_bucket_cross,
    build_keyword_adjustment_candidates,
    build_tuning_mismatch_focus,
    write_summary_md,
)
from tools.analyze_buyback_scanner_vs_review import load_csv


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _write_csv(path, rows, fieldnames=None):
    if not rows:
        return
    fieldnames = fieldnames or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


@pytest.fixture
def sample_candidates(temp_dir):
    rows = [
        {"file_path": "data/docs/001.pdf", "file_name": "001.pdf",
         "candidate_score": "8", "review_priority": "high",
         "matched_keywords": "自己株式取得|取得価額の総額",
         "matched_keyword_count": "2", "derived_ticker": "6750",
         "derived_title": "自己株式取得"},
        {"file_path": "data/docs/002.pdf", "file_name": "002.pdf",
         "candidate_score": "7", "review_priority": "high",
         "matched_keywords": "自己株式の消却",
         "matched_keyword_count": "1", "derived_ticker": "7203",
         "derived_title": "自己株式の消却"},
        {"file_path": "data/docs/003.pdf", "file_name": "003.pdf",
         "candidate_score": "4", "review_priority": "medium",
         "matched_keywords": "取得状況|自己株式",
         "matched_keyword_count": "2", "derived_ticker": "1801",
         "derived_title": "取得状況"},
        {"file_path": "data/docs/004.pdf", "file_name": "004.pdf",
         "candidate_score": "2", "review_priority": "low",
         "matched_keywords": "自己株式",
         "matched_keyword_count": "1", "derived_ticker": "9984",
         "derived_title": ""},
        {"file_path": "data/docs/005.pdf", "file_name": "005.pdf",
         "candidate_score": "10", "review_priority": "high",
         "matched_keywords": "自己株式取得|取得する株式の総数|取得価額の総額",
         "matched_keyword_count": "3", "derived_ticker": "2288",
         "derived_title": "自己株式取得決定"},
    ]
    path = os.path.join(temp_dir, "candidates.csv")
    _write_csv(path, rows)
    return path


@pytest.fixture
def sample_review(temp_dir):
    rows = [
        {"file_path": "data/docs/001.pdf", "file_name": "001.pdf",
         "is_buyback_related": "True", "event_type_candidate": "buyback_decision",
         "event_type": "buyback_decision",
         "confidence_final": "0.87", "review_bucket": "high_confidence_extracted",
         "extracted_fields_count": "8", "missing_key_fields": ""},
        {"file_path": "data/docs/002.pdf", "file_name": "002.pdf",
         "is_buyback_related": "False", "event_type_candidate": "",
         "event_type": "", "confidence_final": "0.2",
         "review_bucket": "non_buyback",
         "extracted_fields_count": "0", "missing_key_fields": ""},
        {"file_path": "data/docs/003.pdf", "file_name": "003.pdf",
         "is_buyback_related": "True", "event_type_candidate": "buyback_status",
         "event_type": "", "confidence_final": "0.7",
         "review_bucket": "classifier_only",
         "extracted_fields_count": "0", "missing_key_fields": "shares_acquired"},
        {"file_path": "data/docs/004.pdf", "file_name": "004.pdf",
         "is_buyback_related": "True", "event_type_candidate": "buyback_decision",
         "event_type": "buyback_decision",
         "confidence_final": "0.92", "review_bucket": "high_confidence_extracted",
         "extracted_fields_count": "10", "missing_key_fields": ""},
        {"file_path": "data/docs/005.pdf", "file_name": "005.pdf",
         "is_buyback_related": "True", "event_type_candidate": "buyback_decision",
         "event_type": "buyback_decision",
         "confidence_final": "0.92", "review_bucket": "high_confidence_extracted",
         "extracted_fields_count": "9", "missing_key_fields": ""},
    ]
    path = os.path.join(temp_dir, "review.csv")
    _write_csv(path, rows)
    return path


@pytest.fixture
def sample_rules(temp_dir):
    rules = build_default_rules()
    rules["priority_thresholds"]["high"] = 7
    rules["priority_thresholds"]["medium"] = 4
    path = os.path.join(temp_dir, "rules.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return path


# ============================================================
# Rules 読み込み
# ============================================================
class TestRulesLoading:
    def test_default_rules(self):
        rules = build_default_rules()
        assert "自己株式取得" in rules["strong_keywords"]
        assert rules["strong_keywords"]["自己株式取得"] == STRONG_SCORE
        assert rules["priority_thresholds"]["high"] == HIGH_PRIORITY_THRESHOLD

    def test_load_from_file(self, sample_rules):
        rules = load_scoring_rules(sample_rules)
        assert rules["priority_thresholds"]["high"] == 7
        assert rules["priority_thresholds"]["medium"] == 4
        # keywords should be merged
        assert "自己株式取得" in rules["strong_keywords"]

    def test_fallback_on_missing(self):
        rules = load_scoring_rules("/nonexistent/path.json")
        assert rules == build_default_rules()

    def test_fallback_on_none(self):
        rules = load_scoring_rules(None)
        assert rules == build_default_rules()


# ============================================================
# 再スコアリング
# ============================================================
class TestRescore:
    def test_basic(self):
        rules = build_default_rules()
        score, contribs = rescore_candidate(
            "自己株式取得|取得価額の総額",
            {"derived_ticker": "6750", "derived_title": "test"},
            rules,
        )
        assert score > 0
        assert any("自己株式取得" in c for c in contribs)

    def test_with_penalty(self):
        rules = build_default_rules()
        score_no_penalty, _ = rescore_candidate(
            "自己株式取得", {"derived_ticker": ""}, rules,
        )
        # penalty keyword direct via penalty_map
        rules2 = build_default_rules()
        rules2["strong_keywords"]["自己株式取得"] = 1  # weaken
        score_weak, _ = rescore_candidate(
            "自己株式取得", {"derived_ticker": ""}, rules2,
        )
        assert score_weak < score_no_penalty

    def test_empty_keywords(self):
        rules = build_default_rules()
        score, contribs = rescore_candidate("", {}, rules)
        assert score == 0
        assert contribs == []


# ============================================================
# Tuning label
# ============================================================
class TestTuningLabel:
    def test_unchanged(self):
        assert assign_tuning_label("high", "high") == "unchanged_high"

    def test_promoted(self):
        assert assign_tuning_label("medium", "high") == "promoted_to_high"
        assert assign_tuning_label("low", "medium") == "promoted_to_medium"

    def test_demoted(self):
        assert assign_tuning_label("high", "medium") == "demoted_to_medium"
        assert assign_tuning_label("medium", "low") == "demoted_to_low"


# ============================================================
# Join
# ============================================================
class TestJoin:
    def test_basic(self, sample_candidates, sample_review):
        cands = load_csv(sample_candidates)
        review = load_csv(sample_review)
        joined, failures = join_candidates_and_review(cands, review)
        assert len(joined) == 5
        assert len(failures) == 0

    def test_failure(self, temp_dir):
        c_path = os.path.join(temp_dir, "c.csv")
        r_path = os.path.join(temp_dir, "r.csv")
        _write_csv(c_path, [{"file_path": "a.pdf", "candidate_score": "5",
                             "review_priority": "high"}])
        _write_csv(r_path, [{"file_path": "b.pdf", "review_bucket": "non_buyback",
                             "is_buyback_related": "False", "confidence_final": "0",
                             "extracted_fields_count": "0"}])
        joined, failures = join_candidates_and_review(load_csv(c_path), load_csv(r_path))
        assert len(joined) == 0
        assert len(failures) == 2


# ============================================================
# Keyword adjustment suggestion
# ============================================================
class TestKeywordSuggestion:
    def test_high_fp(self):
        rules = build_default_rules()
        stats = {"total": 10, "high_confidence_extracted_count": 1,
                 "non_buyback_count": 6, "classifier_only_count": 3}
        direction, reason = suggest_keyword_adjustment("自己株式の消却", stats, rules)
        assert direction in ("decrease", "move_to_penalty")

    def test_high_tp(self):
        rules = build_default_rules()
        stats = {"total": 10, "high_confidence_extracted_count": 8,
                 "non_buyback_count": 0, "classifier_only_count": 2}
        direction, reason = suggest_keyword_adjustment("自己株式", stats, rules)
        assert direction in ("increase", "move_to_strong")

    def test_no_data(self):
        rules = build_default_rules()
        direction, reason = suggest_keyword_adjustment("test", {"total": 0}, rules)
        assert direction == "keep"


# ============================================================
# Priority comparison
# ============================================================
class TestPriorityComparison:
    def test_basic(self, sample_candidates, sample_review):
        cands = load_csv(sample_candidates)
        review = load_csv(sample_review)
        joined, _ = join_candidates_and_review(cands, review)
        rules = build_default_rules()
        for j in joined:
            metadata = {"derived_ticker": j.get("derived_ticker", ""),
                        "derived_title": j.get("derived_title", "")}
            new_score, _ = rescore_candidate(j["matched_keywords"], metadata, rules)
            j["new_priority"] = classify_review_priority(new_score, rules.get("priority_thresholds"))
            j["new_candidate_score"] = new_score
            j["tuning_label"] = assign_tuning_label(j["old_priority"], j["new_priority"])
        comp = build_priority_comparison(joined)
        assert len(comp) == 3
        labels = {c["priority_label"] for c in comp}
        assert labels == {"high", "medium", "low"}


# ============================================================
# Priority × bucket matrix
# ============================================================
class TestPriorityBucketCross:
    def test_basic(self, sample_candidates, sample_review):
        cands = load_csv(sample_candidates)
        review = load_csv(sample_review)
        joined, _ = join_candidates_and_review(cands, review)
        matrix = build_priority_bucket_cross(joined, "old_priority")
        assert len(matrix) > 0


# ============================================================
# Keyword adjustment candidates
# ============================================================
class TestKeywordAdjCandidates:
    def test_basic(self, sample_candidates, sample_review):
        cands = load_csv(sample_candidates)
        review = load_csv(sample_review)
        joined, _ = join_candidates_and_review(cands, review)
        rules = build_default_rules()
        kw_adj = build_keyword_adjustment_candidates(joined, rules)
        assert len(kw_adj) > 0
        assert all("suggested_direction" in k for k in kw_adj)


# ============================================================
# Mismatch focus
# ============================================================
class TestMismatchFocus:
    def test_detects(self, sample_candidates, sample_review):
        cands = load_csv(sample_candidates)
        review = load_csv(sample_review)
        joined, _ = join_candidates_and_review(cands, review)
        rules = build_default_rules()
        for j in joined:
            metadata = {"derived_ticker": j.get("derived_ticker", ""),
                        "derived_title": j.get("derived_title", "")}
            new_score, _ = rescore_candidate(j["matched_keywords"], metadata, rules)
            j["new_priority"] = classify_review_priority(new_score, rules.get("priority_thresholds"))
            j["new_candidate_score"] = new_score
            j["tuning_label"] = assign_tuning_label(j["old_priority"], j["new_priority"])
        mismatch = build_tuning_mismatch_focus(joined)
        # 002.pdf: high → non_buyback should be flagged
        assert any("old_high_non_buyback" in c["tuning_reason"] for c in mismatch)
        # 004.pdf: low → high_confidence_extracted
        assert any("old_low_high_confidence" in c["tuning_reason"] for c in mismatch)


# ============================================================
# Summary MD
# ============================================================
class TestSummaryMd:
    def test_writes(self, temp_dir, sample_candidates, sample_review):
        cands = load_csv(sample_candidates)
        review = load_csv(sample_review)
        joined, failures = join_candidates_and_review(cands, review)
        rules = build_default_rules()
        for j in joined:
            metadata = {"derived_ticker": j.get("derived_ticker", ""),
                        "derived_title": j.get("derived_title", "")}
            new_score, new_contribs = rescore_candidate(j["matched_keywords"], metadata, rules)
            j["new_priority"] = classify_review_priority(new_score, rules.get("priority_thresholds"))
            j["new_candidate_score"] = new_score
            j["score_delta"] = new_score - j["old_candidate_score"]
            j["score_contributions_new"] = "|".join(new_contribs)
            j["tuning_label"] = assign_tuning_label(j["old_priority"], j["new_priority"])
            j["is_buyback_related"] = j.get("is_buyback_related", False)

        comp = build_priority_comparison(joined)
        before = build_priority_bucket_cross(joined, "old_priority")
        after = build_priority_bucket_cross(joined, "new_priority")
        kw_adj = build_keyword_adjustment_candidates(joined, rules)
        mismatch = build_tuning_mismatch_focus(joined)

        out = os.path.join(temp_dir, "summary.md")
        write_summary_md(
            out,
            candidates_path="test.csv",
            review_path="review.csv",
            rules_path="",
            candidate_count=len(cands),
            join_ok=len(joined),
            join_fail=len(failures),
            joined=joined,
            comparison=comp,
            before_matrix=before,
            after_matrix=after,
            kw_adj=kw_adj,
            mismatch=mismatch,
            old_rules=rules,
            new_rules=rules,
        )
        assert os.path.isfile(out)
        content = open(out, encoding="utf-8").read()
        assert "Tuning" in content
        assert "before/after" in content


# ============================================================
# score_candidate backward compatibility
# ============================================================
class TestScoreCandidateBackwardCompat:
    def test_no_rules_arg(self):
        """既存テストと同じ呼び方で動くことを確認。"""
        from tools.find_buyback_candidate_docs import score_candidate
        hits = [
            KeywordHit("自己株式取得", 0, "strong"),
            KeywordHit("取得価額の総額", 100, "strong"),
        ]
        score = score_candidate(hits, {})
        assert score > 0

    def test_classify_no_thresholds(self):
        assert classify_review_priority(HIGH_PRIORITY_THRESHOLD) == "high"
        assert classify_review_priority(MEDIUM_PRIORITY_THRESHOLD) == "medium"
        assert classify_review_priority(0) == "low"

    def test_classify_with_thresholds(self):
        assert classify_review_priority(7, {"high": 7, "medium": 4}) == "high"
        assert classify_review_priority(6, {"high": 7, "medium": 4}) == "medium"
        assert classify_review_priority(3, {"high": 7, "medium": 4}) == "low"
