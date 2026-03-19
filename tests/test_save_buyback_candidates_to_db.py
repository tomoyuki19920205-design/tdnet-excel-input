"""test_save_buyback_candidates_to_db.py — 保存ツールのテスト"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import tempfile

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from tools.save_buyback_candidates_to_db import (
    load_save_candidates_csv,
    validate_row,
    row_to_buyback_event,
    build_raw_text_hash,
    infer_source_type,
    save_candidates_to_db,
    write_summary,
    _safe_str,
    _safe_int,
    _safe_float,
    EXTRACTOR_VERSION,
)


# ============================================================
# fixture: テスト用 CSV 行
# ============================================================
def _row(**kw) -> dict:
    base = {
        "file_path": "/tmp/test.pdf",
        "file_name": "test.pdf",
        "ticker": "2288",
        "disclosure_date": "2026-02-24",
        "title": "テスト文書",
        "event_type": "buyback_decision",
        "confidence_final": "1.0",
        "review_bucket": "high_confidence_extracted",
        "extracted_fields_count": "6",
        "shares_limit": "650000",
        "amount_limit_million_yen": "1300.0",
        "start_date": "2026-03-04",
        "end_date": "2026-03-09",
        "acquisition_method": "tostnet",
        "matched_keywords": "自己株式の取得",
        "manifest_candidate_score": "5",
        "manifest_review_priority": "medium",
        "missing_key_fields": "",
        "save_reason": "high_confidence_extracted_with_core_fields",
    }
    base.update(kw)
    return base


def _write_csv_file(rows: list[dict]) -> str:
    tmp = tempfile.NamedTemporaryFile(
        suffix=".csv", delete=False, mode="w",
        encoding="utf-8", newline="",
    )
    if rows:
        w = csv.DictWriter(tmp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    tmp.close()
    return tmp.name


def _temp_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return tmp.name


# ============================================================
# 安全変換
# ============================================================
class TestSafeConversions:
    def test_safe_int_valid(self):
        assert _safe_int("650000") == 650000

    def test_safe_int_none(self):
        assert _safe_int(None) is None

    def test_safe_int_empty(self):
        assert _safe_int("") is None

    def test_safe_float_valid(self):
        assert _safe_float("1300.0") == 1300.0

    def test_safe_float_none(self):
        assert _safe_float(None) is None


# ============================================================
# CSV 読み込み
# ============================================================
class TestLoadCSV:
    def test_load_valid(self):
        path = _write_csv_file([_row()])
        try:
            rows = load_save_candidates_csv(path)
            assert len(rows) == 1
            assert rows[0]["ticker"] == "2288"
        finally:
            os.unlink(path)

    def test_load_nonexistent(self):
        with pytest.raises(FileNotFoundError):
            load_save_candidates_csv("/nonexistent/path.csv")


# ============================================================
# バリデーション
# ============================================================
class TestValidation:
    def test_valid_row(self):
        ok, reason = validate_row(_row())
        assert ok is True

    def test_skip_non_hce_bucket(self):
        ok, reason = validate_row(_row(review_bucket="classifier_only"))
        assert ok is False
        assert "review_bucket" in reason

    def test_skip_empty_confidence(self):
        ok, reason = validate_row(_row(confidence_final=""))
        assert ok is False
        assert "confidence" in reason

    def test_skip_empty_event_type(self):
        ok, reason = validate_row(_row(event_type=""))
        assert ok is False

    def test_skip_empty_ticker(self):
        ok, reason = validate_row(_row(ticker=""))
        assert ok is False

    def test_skip_empty_file_path(self):
        ok, reason = validate_row(_row(file_path="", file_name=""))
        assert ok is False


# ============================================================
# raw_text_hash
# ============================================================
class TestHash:
    def test_deterministic(self):
        r = _row()
        h1 = build_raw_text_hash(r)
        h2 = build_raw_text_hash(r)
        assert h1 == h2

    def test_length(self):
        h = build_raw_text_hash(_row())
        assert len(h) == 16

    def test_different_rows(self):
        h1 = build_raw_text_hash(_row(ticker="1111"))
        h2 = build_raw_text_hash(_row(ticker="2222"))
        assert h1 != h2


# ============================================================
# source_type 推定
# ============================================================
class TestSourceType:
    def test_pdf(self):
        assert infer_source_type("/tmp/test.pdf") == "pdf"

    def test_html(self):
        assert infer_source_type("/tmp/test.html") == "html"

    def test_empty(self):
        assert infer_source_type("") == "unknown"


# ============================================================
# row → BuybackEvent
# ============================================================
class TestRowToEvent:
    def test_basic_mapping(self):
        ev = row_to_buyback_event(_row())
        assert ev.ticker == "2288"
        assert ev.event_type == "buyback_decision"
        assert ev.shares_limit == 650000
        assert ev.amount_limit_million_yen == 1300.0
        assert ev.extraction_confidence == 1.0
        assert ev.extractor_version == EXTRACTOR_VERSION
        assert ev.source_type == "pdf"

    def test_extracted_json_has_meta(self):
        ev = row_to_buyback_event(_row())
        meta = json.loads(ev.extracted_json)
        assert meta["source"] == "review_save_candidates_csv"
        assert "matched_keywords" in meta

    def test_raw_text_hash_set(self):
        ev = row_to_buyback_event(_row())
        assert ev.raw_text_hash
        assert len(ev.raw_text_hash) == 16


# ============================================================
# dry-run
# ============================================================
class TestDryRun:
    def test_no_db_write(self):
        db = _temp_db()
        csv_path = _write_csv_file([_row()])
        try:
            rows = load_save_candidates_csv(csv_path)
            result = save_candidates_to_db(rows, db, dry_run=True)
            assert result.inserted == 1
            assert result.updated == 0
            # DB should have table but no rows
            conn = sqlite3.connect(db)
            count = conn.execute(
                "SELECT COUNT(*) FROM buyback_events").fetchone()[0]
            conn.close()
            assert count == 0
        finally:
            os.unlink(db)
            os.unlink(csv_path)


# ============================================================
# 実保存
# ============================================================
class TestLiveSave:
    def test_insert(self):
        db = _temp_db()
        csv_path = _write_csv_file([_row()])
        try:
            rows = load_save_candidates_csv(csv_path)
            result = save_candidates_to_db(rows, db)
            assert result.inserted == 1
            assert result.updated == 0
            conn = sqlite3.connect(db)
            count = conn.execute(
                "SELECT COUNT(*) FROM buyback_events").fetchone()[0]
            conn.close()
            assert count == 1
        finally:
            os.unlink(db)
            os.unlink(csv_path)

    def test_upsert_same_row(self):
        db = _temp_db()
        csv_path = _write_csv_file([_row()])
        try:
            rows = load_save_candidates_csv(csv_path)
            # first save
            save_candidates_to_db(rows, db)
            # second save → update
            result = save_candidates_to_db(rows, db)
            assert result.inserted == 0
            assert result.updated == 1
            conn = sqlite3.connect(db)
            count = conn.execute(
                "SELECT COUNT(*) FROM buyback_events").fetchone()[0]
            conn.close()
            assert count == 1  # no duplicate
        finally:
            os.unlink(db)
            os.unlink(csv_path)

    def test_skip_invalid(self):
        db = _temp_db()
        csv_path = _write_csv_file([
            _row(),
            _row(review_bucket="classifier_only"),  # skip
            _row(confidence_final=""),  # skip
        ])
        try:
            rows = load_save_candidates_csv(csv_path)
            result = save_candidates_to_db(rows, db)
            assert result.inserted == 1
            assert result.skipped == 2
            assert len(result.skipped_rows) == 2
        finally:
            os.unlink(db)
            os.unlink(csv_path)

    def test_limit(self):
        db = _temp_db()
        csv_path = _write_csv_file([_row(), _row(ticker="9999")])
        try:
            rows = load_save_candidates_csv(csv_path)
            result = save_candidates_to_db(rows, db, limit=1)
            assert result.valid_rows == 1
        finally:
            os.unlink(db)
            os.unlink(csv_path)


# ============================================================
# summary 出力
# ============================================================
class TestSummary:
    def test_summary_contains_mode(self):
        from tools.save_buyback_candidates_to_db import SaveResult
        r = SaveResult()
        r.input_rows = 3
        r.valid_rows = 2
        r.inserted = 1
        r.updated = 1
        md = write_summary(r, input_path="test.csv",
                           db_path="test.db", dry_run=True)
        assert "DRY-RUN" in md
        assert "inserted" in md.lower()
