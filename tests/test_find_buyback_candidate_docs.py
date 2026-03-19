#!/usr/bin/env python3
"""test_find_buyback_candidate_docs.py — buyback 候補スキャンツールのテスト"""
import csv
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from tools.find_buyback_candidate_docs import (
    iter_pdf_files,
    find_keyword_hits,
    score_candidate,
    classify_review_priority,
    build_candidate_row,
    build_review_hint,
    derive_metadata,
    write_csv_file,
    write_jsonl_file,
    write_manifest,
    write_summary,
    scan_single_file,
    CandidateRow,
    FailureRow,
    STRONG_KEYWORDS,
    WEAK_KEYWORDS,
    EXCLUDE_HINTS,
    HIGH_PRIORITY_THRESHOLD,
    MEDIUM_PRIORITY_THRESHOLD,
    _CSV_COLUMNS,
    _FAILURE_COLUMNS,
    _MANIFEST_COLUMNS,
)


# ============================================================
# iter_pdf_files
# ============================================================
class TestIterPdfFiles:
    def test_finds_pdf(self, tmp_path):
        (tmp_path / "a.pdf").touch()
        (tmp_path / "b.txt").touch()
        (tmp_path / "c.pdf").touch()
        files = list(iter_pdf_files(str(tmp_path)))
        assert len(files) == 2
        assert all(f.endswith(".pdf") for f in files)

    def test_limit(self, tmp_path):
        for i in range(5):
            (tmp_path / f"doc{i}.pdf").touch()
        files = list(iter_pdf_files(str(tmp_path), limit=3))
        assert len(files) == 3

    def test_recursive(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "a.pdf").touch()
        (sub / "b.pdf").touch()
        files_flat = list(iter_pdf_files(str(tmp_path), recursive=False))
        files_rec = list(iter_pdf_files(str(tmp_path), recursive=True))
        assert len(files_flat) == 1
        assert len(files_rec) == 2

    def test_empty_dir(self, tmp_path):
        files = list(iter_pdf_files(str(tmp_path)))
        assert files == []


# ============================================================
# find_keyword_hits
# ============================================================
class TestFindKeywordHits:
    def test_strong_hit(self):
        text = "当社は自己株式の取得を決議しました"
        hits = find_keyword_hits(text)
        strong_hits = [h for h in hits if h.strength == "strong"]
        assert len(strong_hits) >= 1
        assert any("自己株式の取得" in h.keyword for h in strong_hits)

    def test_cancel_hit(self):
        text = "自己株式の消却に関するお知らせ"
        hits = find_keyword_hits(text)
        strong_hits = [h for h in hits if h.strength == "strong"]
        assert any("消却" in h.keyword for h in strong_hits)

    def test_no_hit(self):
        text = "決算短信の内容をお知らせします"
        hits = find_keyword_hits(text)
        non_exclude = [h for h in hits if h.strength != "exclude"]
        assert len(non_exclude) == 0

    def test_exclude_hit(self):
        text = "新株予約権の行使に関する 自己株式の消却"
        hits = find_keyword_hits(text)
        exclude_hits = [h for h in hits if h.strength == "exclude"]
        assert len(exclude_hits) >= 1

    def test_tostnet_hit(self):
        text = "ToSTNeT-3 により買付を行います"
        hits = find_keyword_hits(text)
        strong_hits = [h for h in hits if h.strength == "strong"]
        assert any("ToSTNeT" in h.keyword for h in strong_hits)

    def test_position_tracking(self):
        text = "xxxxx自己株式の取得yyyyy"
        hits = find_keyword_hits(text)
        strong_hits = [h for h in hits if h.strength == "strong"]
        assert len(strong_hits) >= 1
        assert strong_hits[0].position == 5


# ============================================================
# score_candidate
# ============================================================
class TestScoreCandidate:
    def test_strong_keyword_score(self):
        hits = find_keyword_hits("自己株式の取得に関するお知らせ")
        score = score_candidate(hits, {})
        assert score >= 3  # at least 1 strong keyword

    def test_decision_pair_bonus(self):
        hits = find_keyword_hits(
            "取得する株式の総数 3,000,000株 取得価額の総額 50億円"
        )
        score = score_candidate(hits, {})
        assert score >= 10  # 2 strong (6) + pair bonus (4)

    def test_cancel_shasai_penalty(self):
        hits = find_keyword_hits(
            "新株予約権付社債の消却 自己株式の消却"
        )
        score_with = score_candidate(hits, {})
        hits_clean = find_keyword_hits("自己株式の消却")
        score_clean = score_candidate(hits_clean, {})
        assert score_with < score_clean

    def test_metadata_bonus(self):
        hits = find_keyword_hits("自己株式の取得")
        score_no = score_candidate(hits, {})
        score_with = score_candidate(hits, {
            "derived_ticker": "6750",
            "derived_title": "お知らせ",
        })
        assert score_with == score_no + 2

    def test_min_zero(self):
        # Score should never be negative
        hits = find_keyword_hits("新株予約権付社債の消却")
        score = score_candidate(hits, {})
        assert score >= 0


# ============================================================
# classify_review_priority
# ============================================================
class TestClassifyPriority:
    def test_high(self):
        assert classify_review_priority(HIGH_PRIORITY_THRESHOLD) == "high"
        assert classify_review_priority(10) == "high"

    def test_medium(self):
        assert classify_review_priority(MEDIUM_PRIORITY_THRESHOLD) == "medium"
        assert classify_review_priority(5) == "medium"

    def test_low(self):
        assert classify_review_priority(0) == "low"
        assert classify_review_priority(2) == "low"


# ============================================================
# build_review_hint
# ============================================================
class TestBuildReviewHint:
    def test_decision_hint(self):
        hits = find_keyword_hits("取得する株式の総数 取得価額の総額")
        hint = build_review_hint(hits, {"derived_ticker": "1234", "derived_title": "test"})
        assert "decision候補" in hint

    def test_cancel_hint(self):
        hits = find_keyword_hits("自己株式の消却")
        hint = build_review_hint(hits, {})
        assert "cancel候補" in hint

    def test_missing_metadata(self):
        hits = find_keyword_hits("自己株式の取得")
        hint = build_review_hint(hits, {})
        assert "title空" in hint or "ticker未取得" in hint


# ============================================================
# build_candidate_row
# ============================================================
class TestBuildCandidateRow:
    def test_basic(self, tmp_path):
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"dummy")
        text = "自己株式の取得に関するお知らせ"
        hits = find_keyword_hits(text)
        metadata = {"derived_ticker": "6750", "derived_title": "test", "derived_disclosure_date": None}
        row = build_candidate_row(str(pdf), text, 1, hits, metadata)
        assert row.file_name == "test.pdf"
        assert row.text_extract_ok is True
        assert row.matched_keyword_count >= 1
        assert row.candidate_score >= 3

    def test_head_text_truncation(self, tmp_path):
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"dummy")
        text = "A" * 500
        hits = []
        row = build_candidate_row(str(pdf), text, 1, hits, {}, head_chars=200)
        assert len(row.text_head_200) == 200


# ============================================================
# derive_metadata
# ============================================================
class TestDeriveMetadata:
    def test_basic(self):
        text = "2026年2月24日\n（コード番号 2288 東証プライム）\n決算短信"
        result = derive_metadata(text)
        assert result.get("derived_ticker") == "2288"

    def test_no_metadata(self):
        result = derive_metadata("普通のテキスト")
        # Should not crash
        assert isinstance(result, dict)


# ============================================================
# CSV / JSONL 出力
# ============================================================
class TestWriteOutputs:
    def test_write_csv(self, tmp_path):
        path = str(tmp_path / "test.csv")
        rows = [{"file_path": "a.pdf", "file_name": "a.pdf", "file_size": 100}]
        write_csv_file(path, rows, ["file_path", "file_name", "file_size"])
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            data = list(reader)
        assert len(data) == 1
        assert data[0]["file_path"] == "a.pdf"

    def test_write_jsonl(self, tmp_path):
        path = str(tmp_path / "test.jsonl")
        rows = [{"file_path": "a.pdf", "score": 5}]
        write_jsonl_file(path, rows)
        with open(path, encoding="utf-8") as f:
            data = [json.loads(line) for line in f]
        assert len(data) == 1
        assert data[0]["score"] == 5

    def test_write_manifest(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        candidates = [CandidateRow(
            file_path="test.pdf",
            derived_ticker="6750",
            derived_title="test title",
            derived_disclosure_date="2026-01-01",
        )]
        write_manifest(path, candidates)
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            data = list(reader)
        assert len(data) == 1
        assert data[0]["ticker"] == "6750"
        assert data[0]["path"] == "test.pdf"

    def test_write_summary(self, tmp_path):
        path = str(tmp_path / "summary.md")
        candidates = [CandidateRow(
            matched_keywords="自己株式の取得",
            review_priority="high",
            derived_ticker="6750",
            derived_title="test",
            derived_disclosure_date="2026-01-01",
        )]
        write_summary(
            path,
            input_dir="data/docs",
            total_files=100,
            success_count=95,
            failure_count=5,
            candidate_count=1,
            candidates=candidates,
            elapsed_sec=3.5,
        )
        content = open(path, encoding="utf-8").read()
        assert "100" in content
        assert "候補" in content


# ============================================================
# scan_single_file (with mock)
# ============================================================
class TestScanSingleFile:
    def test_nonexistent_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.pdf")
        row, fail = scan_single_file(path)
        assert row is None
        assert fail is not None
        assert fail.stage == "extract_text"

    def test_text_extract_ok_false(self, tmp_path, monkeypatch):
        """テキスト抽出が空の場合"""
        pdf = tmp_path / "empty.pdf"
        pdf.write_bytes(b"dummy")

        from tools import find_buyback_candidate_docs as mod
        monkeypatch.setattr(mod, "extract_pdf_head_text",
                            lambda path, max_pages=2: ("", 1, ""))
        row, fail = scan_single_file(str(pdf))
        assert row is None
        assert fail is not None
        assert fail.error_type == "empty_text"

    def test_buyback_hit(self, tmp_path, monkeypatch):
        """自己株式取得のヒット"""
        pdf = tmp_path / "buyback.pdf"
        pdf.write_bytes(b"dummy")

        text = "自己株式の取得に関するお知らせ。取得する株式の総数 300万株。取得価額の総額 50億円。"
        from tools import find_buyback_candidate_docs as mod
        monkeypatch.setattr(mod, "extract_pdf_head_text",
                            lambda path, max_pages=2: (text, 2, ""))
        monkeypatch.setattr(mod, "derive_metadata",
                            lambda text: {"derived_ticker": "6750", "derived_title": "お知らせ",
                                          "derived_disclosure_date": "2026-01-01"})
        row, fail = scan_single_file(str(pdf))
        assert fail is None
        assert row is not None
        assert row.candidate_score >= 6
        assert row.review_priority == "high"

    def test_non_buyback(self, tmp_path, monkeypatch):
        """決算短信のみでヒットなし"""
        pdf = tmp_path / "tanshin.pdf"
        pdf.write_bytes(b"dummy")

        text = "2026年3月期第3四半期決算短信。売上高は前年同期比10%増加しました。"
        from tools import find_buyback_candidate_docs as mod
        monkeypatch.setattr(mod, "extract_pdf_head_text",
                            lambda path, max_pages=2: (text, 2, ""))
        row, fail = scan_single_file(str(pdf))
        assert row is None
        assert fail is None  # 候補でも失敗でもない

    def test_shasai_cancel_suppression(self, tmp_path, monkeypatch):
        """社債買入消却は score 抑制"""
        pdf = tmp_path / "shasai.pdf"
        pdf.write_bytes(b"dummy")

        text = "新株予約権付社債の消却 自己株式の消却"
        from tools import find_buyback_candidate_docs as mod
        monkeypatch.setattr(mod, "extract_pdf_head_text",
                            lambda path, max_pages=2: (text, 1, ""))
        monkeypatch.setattr(mod, "derive_metadata",
                            lambda text: {"derived_ticker": None, "derived_title": None,
                                          "derived_disclosure_date": None})
        row, fail = scan_single_file(str(pdf))
        if row:
            assert row.candidate_score < HIGH_PRIORITY_THRESHOLD

    def test_metadata_from_text(self, tmp_path, monkeypatch):
        """metadata 補完成功"""
        pdf = tmp_path / "meta.pdf"
        pdf.write_bytes(b"dummy")

        text = "2026年2月24日\n（コード番号 2288）\n自己株式の取得に関するお知らせ"
        from tools import find_buyback_candidate_docs as mod
        monkeypatch.setattr(mod, "extract_pdf_head_text",
                            lambda path, max_pages=2: (text, 1, ""))
        row, fail = scan_single_file(str(pdf))
        assert row is not None
        assert row.derived_ticker == "2288"
