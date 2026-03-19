#!/usr/bin/env python3
"""tests/test_analyze_buyback_scanner_vs_review.py

Scanner vs Review 乖離分析ツールの単体テスト。
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from tools.analyze_buyback_scanner_vs_review import (
    normalize_join_path,
    parse_score_bins,
    assign_score_band,
    load_csv,
    join_manifest_and_review,
    derive_alignment_flags,
    expand_keywords,
    build_priority_bucket_matrix,
    build_score_band_summary,
    build_keyword_summary,
    build_mismatch_cases,
    write_csv,
    write_summary_md,
    DEFAULT_SCORE_BINS,
    _JOINED_COLUMNS,
)


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
def sample_manifest(temp_dir):
    """5 行のダミー manifest (buyback_candidates.csv 形式)。"""
    rows = [
        {"file_path": "data/docs/001.pdf", "candidate_score": "8",
         "review_priority": "high", "matched_keywords": "自己株式取得|取得価額の総額",
         "matched_keyword_count": "2", "derived_ticker": "6750",
         "derived_title": "自己株式取得", "derived_disclosure_date": "2025-04-01"},
        {"file_path": "data/docs/002.pdf", "candidate_score": "7",
         "review_priority": "high", "matched_keywords": "自己株式の消却",
         "matched_keyword_count": "1", "derived_ticker": "7203",
         "derived_title": "自己株式の消却", "derived_disclosure_date": "2025-05-01"},
        {"file_path": "data/docs/003.pdf", "candidate_score": "4",
         "review_priority": "medium", "matched_keywords": "取得状況|自己株式",
         "matched_keyword_count": "2", "derived_ticker": "1801",
         "derived_title": "取得状況", "derived_disclosure_date": "2025-06-01"},
        {"file_path": "data/docs/004.pdf", "candidate_score": "2",
         "review_priority": "low", "matched_keywords": "自己株式",
         "matched_keyword_count": "1", "derived_ticker": "9984",
         "derived_title": "", "derived_disclosure_date": ""},
        {"file_path": "data/docs/005.pdf", "candidate_score": "10",
         "review_priority": "high", "matched_keywords": "自己株式取得|取得する株式の総数|取得価額の総額",
         "matched_keyword_count": "3", "derived_ticker": "2288",
         "derived_title": "自己株式取得決定", "derived_disclosure_date": "2025-07-01"},
    ]
    path = os.path.join(temp_dir, "manifest.csv")
    _write_csv(path, rows)
    return path


@pytest.fixture
def sample_review(temp_dir):
    """5 行のダミー review (review_buyback_results.csv 形式)。"""
    rows = [
        {"file_path": "data/docs/001.pdf", "file_name": "001.pdf",
         "is_buyback_related": "True", "event_type_candidate": "buyback_decision",
         "event_type": "buyback_decision",
         "classification_confidence": "0.9", "extraction_confidence": "0.85",
         "confidence_final": "0.87", "review_bucket": "high_confidence_extracted",
         "extracted_fields_count": "8", "missing_key_fields": "",
         "manifest_candidate_score": "8", "manifest_review_priority": "high",
         "manifest_matched_keywords": "自己株式取得|取得価額の総額",
         "manifest_matched_keyword_count": "2"},
        {"file_path": "data/docs/002.pdf", "file_name": "002.pdf",
         "is_buyback_related": "False", "event_type_candidate": "",
         "event_type": "", "classification_confidence": "0.2",
         "extraction_confidence": "0", "confidence_final": "0.2",
         "review_bucket": "non_buyback",
         "extracted_fields_count": "0", "missing_key_fields": "",
         "manifest_candidate_score": "7", "manifest_review_priority": "high",
         "manifest_matched_keywords": "自己株式の消却",
         "manifest_matched_keyword_count": "1"},
        {"file_path": "data/docs/003.pdf", "file_name": "003.pdf",
         "is_buyback_related": "True", "event_type_candidate": "buyback_status",
         "event_type": "", "classification_confidence": "0.7",
         "extraction_confidence": "0", "confidence_final": "0.7",
         "review_bucket": "classifier_only",
         "extracted_fields_count": "0", "missing_key_fields": "shares_acquired",
         "manifest_candidate_score": "4", "manifest_review_priority": "medium",
         "manifest_matched_keywords": "取得状況|自己株式",
         "manifest_matched_keyword_count": "2"},
        {"file_path": "data/docs/004.pdf", "file_name": "004.pdf",
         "is_buyback_related": "True", "event_type_candidate": "buyback_decision",
         "event_type": "buyback_decision",
         "classification_confidence": "0.95", "extraction_confidence": "0.9",
         "confidence_final": "0.92", "review_bucket": "high_confidence_extracted",
         "extracted_fields_count": "10", "missing_key_fields": "",
         "manifest_candidate_score": "2", "manifest_review_priority": "low",
         "manifest_matched_keywords": "自己株式",
         "manifest_matched_keyword_count": "1"},
        {"file_path": "data/docs/005.pdf", "file_name": "005.pdf",
         "is_buyback_related": "True", "event_type_candidate": "buyback_decision",
         "event_type": "buyback_decision",
         "classification_confidence": "0.95", "extraction_confidence": "0.9",
         "confidence_final": "0.92", "review_bucket": "high_confidence_extracted",
         "extracted_fields_count": "9", "missing_key_fields": "",
         "manifest_candidate_score": "10", "manifest_review_priority": "high",
         "manifest_matched_keywords": "自己株式取得|取得する株式の総数|取得価額の総額",
         "manifest_matched_keyword_count": "3"},
    ]
    path = os.path.join(temp_dir, "review.csv")
    _write_csv(path, rows)
    return path


# ============================================================
# normalize_join_path
# ============================================================
class TestNormalizeJoinPath:
    def test_windows_path(self):
        assert normalize_join_path("C:\\Users\\data\\docs\\001.pdf") == "001.pdf"

    def test_unix_path(self):
        assert normalize_join_path("/home/user/data/docs/001.pdf") == "001.pdf"

    def test_relative_path(self):
        assert normalize_join_path("data/docs/001.pdf") == "001.pdf"

    def test_backslash_mixed(self):
        assert normalize_join_path("data\\docs/001.pdf") == "001.pdf"

    def test_empty(self):
        assert normalize_join_path("") == ""

    def test_basename_only(self):
        assert normalize_join_path("001.pdf") == "001.pdf"

    def test_case_insensitive(self):
        assert normalize_join_path("DATA/DOCS/Sample.PDF") == "sample.pdf"


# ============================================================
# parse_score_bins / assign_score_band
# ============================================================
class TestScoreBand:
    def test_default_bins(self):
        assert parse_score_bins("0,3,6,10,100") == [0, 3, 6, 10, 100]

    def test_custom_bins(self):
        assert parse_score_bins("0,5,10,100") == [0, 5, 10, 100]

    def test_invalid_fallback(self):
        assert parse_score_bins("abc") == DEFAULT_SCORE_BINS

    def test_assign_band_low(self):
        bins = [0, 3, 6, 10, 100]
        assert assign_score_band(0, bins) == "0-2"
        assert assign_score_band(2, bins) == "0-2"

    def test_assign_band_mid(self):
        bins = [0, 3, 6, 10, 100]
        assert assign_score_band(3, bins) == "3-5"
        assert assign_score_band(5, bins) == "3-5"

    def test_assign_band_high(self):
        bins = [0, 3, 6, 10, 100]
        assert assign_score_band(6, bins) == "6-9"
        assert assign_score_band(9, bins) == "6-9"

    def test_assign_band_top(self):
        bins = [0, 3, 6, 10, 100]
        assert assign_score_band(10, bins) == "10+"
        assert assign_score_band(15, bins) == "10+"


# ============================================================
# Join
# ============================================================
class TestJoin:
    def test_basic_join(self, sample_manifest, sample_review):
        manifest = load_csv(sample_manifest)
        review = load_csv(sample_review)
        joined, failures = join_manifest_and_review(manifest, review)
        assert len(joined) == 5
        assert len(failures) == 0

    def test_join_failure(self, temp_dir):
        m_path = os.path.join(temp_dir, "m.csv")
        r_path = os.path.join(temp_dir, "r.csv")
        _write_csv(m_path, [
            {"file_path": "data/docs/AAA.pdf", "candidate_score": "5",
             "review_priority": "medium", "matched_keywords": "test",
             "matched_keyword_count": "1"},
        ])
        _write_csv(r_path, [
            {"file_path": "data/docs/BBB.pdf", "file_name": "BBB.pdf",
             "review_bucket": "non_buyback", "is_buyback_related": "False",
             "confidence_final": "0.1", "extracted_fields_count": "0",
             "missing_key_fields": ""},
        ])
        manifest = load_csv(m_path)
        review = load_csv(r_path)
        joined, failures = join_manifest_and_review(manifest, review)
        assert len(joined) == 0
        assert len(failures) == 2  # 1 from manifest, 1 from review
        sides = {f["source_side"] for f in failures}
        assert "manifest" in sides
        assert "review" in sides

    def test_join_windows_vs_unix(self, temp_dir):
        m_path = os.path.join(temp_dir, "m.csv")
        r_path = os.path.join(temp_dir, "r.csv")
        _write_csv(m_path, [
            {"file_path": "C:\\data\\docs\\test.pdf", "candidate_score": "5",
             "review_priority": "high"},
        ])
        _write_csv(r_path, [
            {"file_path": "/home/user/data/docs/test.pdf", "file_name": "test.pdf",
             "review_bucket": "non_buyback", "is_buyback_related": "False",
             "confidence_final": "0", "extracted_fields_count": "0"},
        ])
        manifest = load_csv(m_path)
        review = load_csv(r_path)
        joined, failures = join_manifest_and_review(manifest, review)
        assert len(joined) == 1


# ============================================================
# Alignment flags
# ============================================================
class TestAlignmentFlags:
    def test_true_positive(self):
        row = {
            "manifest_candidate_score": 8,
            "manifest_review_priority": "high",
            "review_bucket": "high_confidence_extracted",
            "is_buyback_related": True,
            "extracted_fields_count": 8,
        }
        derive_alignment_flags(row, DEFAULT_SCORE_BINS)
        assert row["likely_true_positive"] is True
        assert row["likely_false_positive"] is False
        assert row["score_band"] == "6-9"

    def test_false_positive(self):
        row = {
            "manifest_candidate_score": 7,
            "manifest_review_priority": "high",
            "review_bucket": "non_buyback",
            "is_buyback_related": False,
            "extracted_fields_count": 0,
        }
        derive_alignment_flags(row, DEFAULT_SCORE_BINS)
        assert row["likely_false_positive"] is True

    def test_missed_candidate(self):
        row = {
            "manifest_candidate_score": 2,
            "manifest_review_priority": "low",
            "review_bucket": "high_confidence_extracted",
            "is_buyback_related": True,
            "extracted_fields_count": 10,
        }
        derive_alignment_flags(row, DEFAULT_SCORE_BINS)
        assert row["likely_missed_candidate"] is True
        assert row["score_band"] == "0-2"

    def test_needs_rule_improvement(self):
        row = {
            "manifest_candidate_score": 4,
            "manifest_review_priority": "medium",
            "review_bucket": "classifier_only",
            "is_buyback_related": True,
            "extracted_fields_count": 0,
        }
        derive_alignment_flags(row, DEFAULT_SCORE_BINS)
        assert row["likely_needs_rule_improvement"] is True

    def test_alignment_label(self):
        row = {
            "manifest_candidate_score": 5,
            "manifest_review_priority": "medium",
            "review_bucket": "classifier_only",
            "is_buyback_related": True,
            "extracted_fields_count": 0,
        }
        derive_alignment_flags(row, DEFAULT_SCORE_BINS)
        assert row["alignment_label"] == "scanner_medium__classifier_only"


# ============================================================
# Keyword 展開
# ============================================================
class TestExpandKeywords:
    def test_pipe_separated(self):
        joined = [{"manifest_matched_keywords": "kw1|kw2|kw3", "review_bucket": "non_buyback"}]
        expanded = expand_keywords(joined)
        assert len(expanded) == 3
        kws = {e["_keyword"] for e in expanded}
        assert kws == {"kw1", "kw2", "kw3"}

    def test_single_keyword(self):
        joined = [{"manifest_matched_keywords": "single_kw", "review_bucket": "non_buyback"}]
        expanded = expand_keywords(joined)
        assert len(expanded) == 1
        assert expanded[0]["_keyword"] == "single_kw"

    def test_empty(self):
        joined = [{"manifest_matched_keywords": "", "review_bucket": "non_buyback"}]
        expanded = expand_keywords(joined)
        assert len(expanded) == 0


# ============================================================
# priority × bucket matrix
# ============================================================
class TestPriorityBucketMatrix:
    def test_basic(self, sample_manifest, sample_review):
        manifest = load_csv(sample_manifest)
        review = load_csv(sample_review)
        joined, _ = join_manifest_and_review(manifest, review)
        for j in joined:
            derive_alignment_flags(j, DEFAULT_SCORE_BINS)
        matrix = build_priority_bucket_matrix(joined)
        assert len(matrix) > 0
        # high + high_confidence_extracted should be present
        hh = [m for m in matrix
              if m["manifest_review_priority"] == "high"
              and m["review_bucket"] == "high_confidence_extracted"]
        assert len(hh) == 1
        assert hh[0]["count"] == 2  # 001.pdf + 005.pdf


# ============================================================
# score_band summary
# ============================================================
class TestScoreBandSummary:
    def test_basic(self, sample_manifest, sample_review):
        manifest = load_csv(sample_manifest)
        review = load_csv(sample_review)
        joined, _ = join_manifest_and_review(manifest, review)
        for j in joined:
            derive_alignment_flags(j, DEFAULT_SCORE_BINS)
        bands = build_score_band_summary(joined)
        assert len(bands) > 0
        for b in bands:
            assert "buyback_related_rate" in b


# ============================================================
# keyword summary
# ============================================================
class TestKeywordSummary:
    def test_basic(self, sample_manifest, sample_review):
        manifest = load_csv(sample_manifest)
        review = load_csv(sample_review)
        joined, _ = join_manifest_and_review(manifest, review)
        for j in joined:
            derive_alignment_flags(j, DEFAULT_SCORE_BINS)
        expanded = expand_keywords(joined)
        kw_summary = build_keyword_summary(expanded)
        assert len(kw_summary) > 0
        # 自己株式取得 should be there
        kw_names = {k["keyword"] for k in kw_summary}
        assert "自己株式取得" in kw_names or "取得価額の総額" in kw_names


# ============================================================
# mismatch cases
# ============================================================
class TestMismatchCases:
    def test_detects_high_non_buyback(self, sample_manifest, sample_review):
        manifest = load_csv(sample_manifest)
        review = load_csv(sample_review)
        joined, _ = join_manifest_and_review(manifest, review)
        for j in joined:
            derive_alignment_flags(j, DEFAULT_SCORE_BINS)
        cases = build_mismatch_cases(joined)
        # 002.pdf: high → non_buyback
        hh_nb = [c for c in cases if "scanner_high_medium_but_non_buyback" in c["mismatch_reason"]]
        assert len(hh_nb) >= 1

    def test_detects_low_high_confidence(self, sample_manifest, sample_review):
        manifest = load_csv(sample_manifest)
        review = load_csv(sample_review)
        joined, _ = join_manifest_and_review(manifest, review)
        for j in joined:
            derive_alignment_flags(j, DEFAULT_SCORE_BINS)
        cases = build_mismatch_cases(joined)
        lh = [c for c in cases if "scanner_low_but_high_confidence" in c["mismatch_reason"]]
        assert len(lh) >= 1  # 004.pdf


# ============================================================
# Summary MD 出力
# ============================================================
class TestSummaryMd:
    def test_writes_file(self, temp_dir, sample_manifest, sample_review):
        manifest = load_csv(sample_manifest)
        review = load_csv(sample_review)
        joined, failures = join_manifest_and_review(manifest, review)
        for j in joined:
            derive_alignment_flags(j, DEFAULT_SCORE_BINS)
        matrix = build_priority_bucket_matrix(joined)
        score_bands = build_score_band_summary(joined)
        expanded = expand_keywords(joined)
        kw_summary = build_keyword_summary(expanded)
        mismatch = build_mismatch_cases(joined)

        out = os.path.join(temp_dir, "summary.md")
        write_summary_md(
            out,
            manifest_path="test_manifest.csv",
            review_path="test_review.csv",
            manifest_count=len(manifest),
            review_count=len(review),
            joined=joined,
            failures=failures,
            matrix=matrix,
            score_bands=score_bands,
            keyword_summary=kw_summary,
            mismatch_cases=mismatch,
            bins=DEFAULT_SCORE_BINS,
        )
        assert os.path.isfile(out)
        content = open(out, encoding="utf-8").read()
        assert "Alignment Summary" in content
        assert "priority" in content
        assert "score" in content


# ============================================================
# CSV 出力
# ============================================================
class TestWriteCsv:
    def test_writes_csv(self, temp_dir):
        out = os.path.join(temp_dir, "test.csv")
        rows = [{"a": 1, "b": 2}]
        write_csv(out, rows, ["a", "b"])
        assert os.path.isfile(out)
        loaded = load_csv(out)
        assert len(loaded) == 1
        assert loaded[0]["a"] == "1"
