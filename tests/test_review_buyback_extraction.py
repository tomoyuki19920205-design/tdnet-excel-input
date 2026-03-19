#!/usr/bin/env python3
"""test_review_buyback_extraction.py — 実データ一括検証ツールのテスト"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import csv
import json
import tempfile
from pathlib import Path

import pytest

# テスト対象の関数群を import
from tools.review_buyback_extraction import (
    iter_input_files,
    load_manifest,
    resolve_manifest_path,
    build_files_from_manifest,
    resolve_metadata,
    load_text_from_file,
    detect_title_from_html,
    guess_metadata_from_filename,
    compute_extracted_fields_count,
    compute_missing_key_fields,
    classify_review_bucket,
    run_buyback_review_for_file,
    write_csv,
    write_jsonl,
    generate_review_summary,
    _CSV_COLUMNS,
    _LOW_CONF_COLUMNS,
    _FAILURE_COLUMNS,
    _MANIFEST_COLUMNS,
)
from src.events.buyback_models import (
    BUYBACK_DECISION, BUYBACK_STATUS, BUYBACK_RESULT, TREASURY_CANCEL,
)


# ============================================================
# フィクスチャ: テスト用ファイル群
# ============================================================
SAMPLE_DECISION_HTML = """\
<html>
<head><title>自己株式取得に係る事項の決定に関するお知らせ</title></head>
<body>
<h1>自己株式取得に係る事項の決定に関するお知らせ</h1>
<p>当社は、本日開催の取締役会において、自己株式の取得に係る事項について決議いたしました。</p>
<p>1. 取得し得る株式の総数　3,000,000株（上限）</p>
<p>2. 取得価額の総額　50億円（上限）</p>
<p>3. 取得期間　2025年4月1日から2025年9月30日まで</p>
<p>4. 取得方法　東京証券取引所における市場買付</p>
<p>発行済株式総数に対する割合 2.35%</p>
</body>
</html>
"""

SAMPLE_RESULT_TXT = """\
自己株式の取得結果及び取得終了に関するお知らせ

1. 取得した株式の種類　当社普通株式
2. 取得した株式の総数　2,800,000株
3. 取得価額の総額　48億円
4. 取得期間　自 2025年4月1日 至 2025年9月30日
5. 取得方法　東京証券取引所における市場買付
"""

SAMPLE_NON_BUYBACK = """\
<html>
<head><title>業績予想の修正に関するお知らせ</title></head>
<body>
<h1>業績予想の修正に関するお知らせ</h1>
<p>当社は業績予想を修正しました。売上高は1,000億円の見通しです。</p>
</body>
</html>
"""

SAMPLE_EXCLUDED = """\
<html>
<head><title>ストックオプション（新株予約権）の付与に関するお知らせ</title></head>
<body>
<h1>ストックオプション（新株予約権）の付与に関するお知らせ</h1>
<p>新株予約権を付与しました。</p>
</body>
</html>
"""


@pytest.fixture
def temp_dir():
    """テスト用の一時ディレクトリを作成"""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def sample_files(temp_dir):
    """テスト用のサンプルファイルを作成"""
    files = {}

    # HTML decision
    p = os.path.join(temp_dir, "6750_2025-04-01_decision.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(SAMPLE_DECISION_HTML)
    files["decision_html"] = p

    # TXT result
    p = os.path.join(temp_dir, "6750_2025-09-30_result.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(SAMPLE_RESULT_TXT)
    files["result_txt"] = p

    # HTML non-buyback
    p = os.path.join(temp_dir, "1234_forecast.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(SAMPLE_NON_BUYBACK)
    files["non_buyback"] = p

    # HTML excluded
    p = os.path.join(temp_dir, "5678_stock_option.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(SAMPLE_EXCLUDED)
    files["excluded"] = p

    # サブディレクトリ
    subdir = os.path.join(temp_dir, "sub")
    os.makedirs(subdir)
    p = os.path.join(subdir, "nested.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(SAMPLE_DECISION_HTML)
    files["nested"] = p

    # 空ファイル(text_extract_failed)
    p = os.path.join(temp_dir, "empty.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write("")
    files["empty"] = p

    return files


# ============================================================
# iter_input_files
# ============================================================
class TestIterInputFiles:
    def test_finds_html_files(self, temp_dir, sample_files):
        files = iter_input_files(temp_dir, globs=["*.html"])
        assert len(files) >= 3  # decision, non_buyback, excluded, empty

    def test_recursive(self, temp_dir, sample_files):
        non_recursive = iter_input_files(temp_dir, globs=["*.html"], recursive=False)
        recursive = iter_input_files(temp_dir, globs=["*.html"], recursive=True)
        assert len(recursive) > len(non_recursive)

    def test_limit(self, temp_dir, sample_files):
        files = iter_input_files(temp_dir, recursive=True, limit=2)
        assert len(files) == 2

    def test_empty_dir(self, temp_dir):
        empty = os.path.join(temp_dir, "empty_dir")
        os.makedirs(empty)
        files = iter_input_files(empty)
        assert len(files) == 0


# ============================================================
# guess_metadata_from_filename
# ============================================================
class TestGuessMetadata:
    def test_ticker_and_date(self):
        m = guess_metadata_from_filename("6750_2025-04-01_decision.html")
        assert m["ticker"] == "6750"
        assert m["disclosure_date"] == "2025-04-01"

    def test_date_without_hyphen(self):
        m = guess_metadata_from_filename("20250401_6750_buyback.pdf")
        assert m["disclosure_date"] == "2025-04-01"

    def test_no_metadata(self):
        m = guess_metadata_from_filename("report.html")
        assert m["ticker"] == ""
        assert m["disclosure_date"] == ""


# ============================================================
# load_manifest
# ============================================================
class TestLoadManifest:
    def test_load_valid_csv(self, temp_dir):
        manifest_path = os.path.join(temp_dir, "manifest.csv")
        with open(manifest_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["path", "ticker", "title", "disclosure_date"])
            writer.writeheader()
            writer.writerow({"path": "test.html", "ticker": "6750",
                            "title": "自己株式取得", "disclosure_date": "2025-04-01"})
        index, all_rows = load_manifest(manifest_path)
        assert "test.html" in index
        assert index["test.html"]["ticker"] == "6750"
        assert len(all_rows) == 1

    def test_load_nonexistent(self):
        index, all_rows = load_manifest("/nonexistent/path.csv")
        assert index == {}
        assert all_rows == []

    def test_load_empty(self):
        index, all_rows = load_manifest("")
        assert index == {}
        assert all_rows == []


# ============================================================
# resolve_metadata
# ============================================================
class TestResolveMetadata:
    def test_manifest_priority(self, temp_dir):
        manifest = {"test.html": {"ticker": "9999", "title": "Manifest Title",
                                   "disclosure_date": "2025-01-01"}}
        meta = resolve_metadata(
            os.path.join(temp_dir, "test.html"),
            manifest, {"ticker": "0000", "disclosure_date": ""},
        )
        assert meta["ticker"] == "9999"
        assert meta["title"] == "Manifest Title"

    def test_filename_fallback(self, temp_dir):
        meta = resolve_metadata(
            os.path.join(temp_dir, "6750_2025-04-01_test.html"),
            {}, {"ticker": "", "disclosure_date": ""},
        )
        assert meta["ticker"] == "6750"
        assert meta["disclosure_date"] == "2025-04-01"

    def test_default_fallback(self, temp_dir):
        meta = resolve_metadata(
            os.path.join(temp_dir, "report.html"),
            {}, {"ticker": "1111", "disclosure_date": "2025-06-01"},
        )
        assert meta["ticker"] == "1111"


# ============================================================
# load_text_from_file
# ============================================================
class TestLoadText:
    def test_html(self, sample_files):
        ok, text, err = load_text_from_file(sample_files["decision_html"], "html")
        assert ok is True
        assert "自己株式" in text
        assert err == ""

    def test_text(self, sample_files):
        ok, text, err = load_text_from_file(sample_files["result_txt"], "txt")
        assert ok is True
        assert "取得結果" in text

    def test_empty_html(self, sample_files):
        ok, text, err = load_text_from_file(sample_files["empty"], "html")
        assert ok is False

    def test_nonexistent(self, temp_dir):
        ok, text, err = load_text_from_file(os.path.join(temp_dir, "nope.html"), "html")
        assert ok is False


# ============================================================
# detect_title_from_html
# ============================================================
class TestDetectTitle:
    def test_title_tag(self, sample_files):
        title = detect_title_from_html(sample_files["decision_html"])
        assert "自己株式取得" in title

    def test_no_title(self, sample_files):
        title = detect_title_from_html(sample_files["empty"])
        assert title == ""


# ============================================================
# compute_extracted_fields_count
# ============================================================
class TestExtractedFieldsCount:
    def test_all_none(self):
        assert compute_extracted_fields_count({}) == 0

    def test_some_fields(self):
        d = {"shares_limit": 1000, "amount_limit_million_yen": 50.0, "start_date": "2025-04-01"}
        assert compute_extracted_fields_count(d) == 3

    def test_none_values_not_counted(self):
        d = {"shares_limit": None, "amount_limit_million_yen": 50.0}
        assert compute_extracted_fields_count(d) == 1


# ============================================================
# compute_missing_key_fields
# ============================================================
class TestMissingKeyFields:
    def test_decision_all_missing(self):
        missing = compute_missing_key_fields(BUYBACK_DECISION, {})
        assert "shares_limit" in missing
        assert "amount_limit_million_yen" in missing

    def test_decision_none_missing(self):
        d = {"shares_limit": 1000, "amount_limit_million_yen": 50.0,
             "start_date": "2025-04-01", "end_date": "2025-09-30"}
        missing = compute_missing_key_fields(BUYBACK_DECISION, d)
        assert missing == []

    def test_cancel(self):
        missing = compute_missing_key_fields(TREASURY_CANCEL, {"shares_cancelled": 5000})
        assert "cancel_date" in missing
        assert "shares_cancelled" not in missing

    def test_unknown_type(self):
        missing = compute_missing_key_fields("unknown", {})
        assert missing == []


# ============================================================
# classify_review_bucket
# ============================================================
class TestReviewBucket:
    def test_text_extract_failed(self):
        b = classify_review_bucket(False, False, "", 0.0, 0.6, 0, False)
        assert b == "text_extract_failed"

    def test_excluded(self):
        b = classify_review_bucket(True, False, "EXCLUDED:ストックオプション", 0.0, 0.6, 0, False)
        assert b == "excluded"

    def test_non_buyback(self):
        b = classify_review_bucket(True, False, "", 0.1, 0.6, 0, False)
        assert b == "non_buyback"

    def test_extraction_failed(self):
        b = classify_review_bucket(True, True, "", 0.8, 0.6, 0, True)
        assert b == "extraction_failed"

    def test_classifier_only(self):
        b = classify_review_bucket(True, True, "", 0.8, 0.6, 0, False)
        assert b == "classifier_only"

    def test_low_confidence(self):
        b = classify_review_bucket(True, True, "", 0.4, 0.6, 3, False)
        assert b == "low_confidence"

    def test_high_confidence_extracted(self):
        b = classify_review_bucket(True, True, "", 0.8, 0.6, 3, False)
        assert b == "high_confidence_extracted"


# ============================================================
# run_buyback_review_for_file — 統合テスト
# ============================================================
class TestRunReviewForFile:
    def test_decision_html(self, sample_files):
        meta = {"ticker": "6750", "disclosure_date": "2025-04-01", "title": ""}
        row, fail = run_buyback_review_for_file(sample_files["decision_html"], meta)
        assert row["is_buyback_related"] is True
        assert row["event_type"] == BUYBACK_DECISION
        assert row["review_bucket"] == "high_confidence_extracted"
        assert fail is None

    def test_result_txt(self, sample_files):
        meta = {"ticker": "6750", "disclosure_date": "2025-09-30",
                "title": "自己株式の取得結果及び取得終了に関するお知らせ"}
        row, fail = run_buyback_review_for_file(sample_files["result_txt"], meta)
        assert row["is_buyback_related"] is True
        assert row["event_type"] == BUYBACK_RESULT
        assert row["shares_acquired"] == 2_800_000
        assert fail is None

    def test_non_buyback(self, sample_files):
        meta = {"ticker": "1234", "disclosure_date": "", "title": ""}
        row, fail = run_buyback_review_for_file(sample_files["non_buyback"], meta)
        assert row["is_buyback_related"] is False
        assert row["review_bucket"] == "non_buyback"

    def test_excluded(self, sample_files):
        meta = {"ticker": "5678", "disclosure_date": "", "title": ""}
        row, fail = run_buyback_review_for_file(sample_files["excluded"], meta)
        assert row["is_buyback_related"] is False
        assert row["review_bucket"] == "excluded"
        assert row["exclusion_reason"] != ""

    def test_empty_html(self, sample_files):
        meta = {"ticker": "", "disclosure_date": "", "title": ""}
        row, fail = run_buyback_review_for_file(sample_files["empty"], meta)
        assert row["review_bucket"] == "text_extract_failed"
        assert fail is not None
        assert fail["stage"] == "load_text"

    def test_title_detected_from_html(self, sample_files):
        meta = {"ticker": "6750", "disclosure_date": "", "title": ""}
        row, fail = run_buyback_review_for_file(sample_files["decision_html"], meta)
        assert "自己株式" in row["title"]


# ============================================================
# 出力テスト
# ============================================================
class TestOutputs:
    def test_write_csv(self, temp_dir):
        rows = [{"file_path": "test.html", "review_bucket": "high_confidence_extracted"}]
        path = os.path.join(temp_dir, "out", "test.csv")
        write_csv(path, rows, ["file_path", "review_bucket"])
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            data = list(reader)
        assert len(data) == 1
        assert data[0]["review_bucket"] == "high_confidence_extracted"

    def test_write_jsonl(self, temp_dir):
        rows = [{"file_path": "test.html", "count": 42}]
        path = os.path.join(temp_dir, "out", "test.jsonl")
        write_jsonl(path, rows)
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            data = [json.loads(line) for line in f]
        assert len(data) == 1
        assert data[0]["count"] == 42

    def test_generate_summary(self):
        rows = [
            {"file_type": "html", "text_extract_ok": True,
             "is_buyback_related": True, "exclusion_reason": "",
             "event_type": BUYBACK_DECISION,
             "confidence_final": 0.85, "review_bucket": "high_confidence_extracted",
             "missing_key_fields": "", "shares_limit": 3000000,
             "amount_limit_million_yen": 5000.0, "start_date": "2025-04-01",
             "end_date": "2025-09-30", "cancel_date": None,
             "shares_acquired": None, "shares_cancelled": None,
             "amount_acquired_million_yen": None, "ratio_to_outstanding": 2.35,
             "acquisition_method": "market_purchase",
             "board_resolution_date": None, "status_period_label": None},
            {"file_type": "html", "text_extract_ok": True,
             "is_buyback_related": False, "exclusion_reason": "",
             "event_type": "", "confidence_final": 0.0,
             "review_bucket": "non_buyback", "missing_key_fields": "",
             "shares_limit": None, "shares_acquired": None,
             "shares_cancelled": None, "amount_limit_million_yen": None,
             "amount_acquired_million_yen": None, "start_date": None,
             "end_date": None, "cancel_date": None, "ratio_to_outstanding": None,
             "acquisition_method": None, "board_resolution_date": None,
             "status_period_label": None},
        ]
        md = generate_review_summary(rows, [], "data/docs", True, 0.60)
        assert "# 自社株買い抽出エンジン" in md
        assert "対象ファイル数" in md
        assert "buyback_related" in md
        assert "event_type" in md

    def test_summary_with_failures(self):
        rows = [
            {"file_type": "pdf", "text_extract_ok": False,
             "is_buyback_related": False, "exclusion_reason": "",
             "event_type": "", "confidence_final": 0.0,
             "review_bucket": "text_extract_failed",
             "missing_key_fields": "",
             "shares_limit": None, "shares_acquired": None,
             "shares_cancelled": None, "amount_limit_million_yen": None,
             "amount_acquired_million_yen": None, "start_date": None,
             "end_date": None, "cancel_date": None, "ratio_to_outstanding": None,
             "acquisition_method": None, "board_resolution_date": None,
             "status_period_label": None},
        ]
        failures = [
            {"stage": "load_text", "error_type": "text_extract_failed",
             "error_message": "empty text"},
        ]
        md = generate_review_summary(rows, failures, "data/docs", True, 0.60)
        assert "テキスト取得失敗" in md
        assert "extraction_failures" in md


# ============================================================
# review_bucket 件数集計テスト
# ============================================================
class TestBucketCounts:
    def test_counts(self, temp_dir, sample_files):
        """複数ファイルを処理して review_bucket 集計"""
        meta_empty = {"ticker": "", "disclosure_date": "", "title": ""}
        meta_6750 = {"ticker": "6750", "disclosure_date": "2025-04-01", "title": ""}

        rows = []
        for path, meta in [
            (sample_files["decision_html"], meta_6750),
            (sample_files["non_buyback"], meta_empty),
            (sample_files["excluded"], meta_empty),
            (sample_files["empty"], meta_empty),
        ]:
            row, _ = run_buyback_review_for_file(path, meta)
            rows.append(row)

        buckets = [r["review_bucket"] for r in rows]
        assert "high_confidence_extracted" in buckets
        assert "non_buyback" in buckets
        assert "excluded" in buckets
        assert "text_extract_failed" in buckets


# ============================================================
# manifest メタデータ統合テスト
# ============================================================
class TestManifestIntegration:
    def test_manifest_overrides_filename(self, temp_dir, sample_files):
        manifest_path = os.path.join(temp_dir, "manifest.csv")
        fname = os.path.basename(sample_files["decision_html"])
        with open(manifest_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["path", "ticker", "title", "disclosure_date"])
            writer.writeheader()
            writer.writerow({
                "path": fname,
                "ticker": "9999",
                "title": "Manifest で指定したタイトル",
                "disclosure_date": "2026-01-01",
            })
        index, _ = load_manifest(manifest_path)
        meta = resolve_metadata(sample_files["decision_html"], index, {"ticker": ""})
        assert meta["ticker"] == "9999"
        assert meta["title"] == "Manifest で指定したタイトル"
        assert meta["disclosure_date"] == "2026-01-01"


# ============================================================
# resolve_manifest_path
# ============================================================
class TestResolveManifestPath:
    def test_absolute_path(self, sample_files):
        path = sample_files["decision_html"]
        resolved = resolve_manifest_path(path)
        assert resolved == path

    def test_absolute_nonexistent(self, temp_dir):
        resolved = resolve_manifest_path(os.path.join(temp_dir, "nope.pdf"))
        assert resolved is None

    def test_relative_with_input_dir(self, temp_dir, sample_files):
        fname = os.path.basename(sample_files["decision_html"])
        resolved = resolve_manifest_path(fname, input_dir=temp_dir)
        assert resolved is not None
        assert os.path.exists(resolved)

    def test_relative_not_found(self, temp_dir):
        resolved = resolve_manifest_path("nofile.pdf", input_dir=temp_dir)
        assert resolved is None

    def test_empty_path(self):
        resolved = resolve_manifest_path("")
        assert resolved is None


# ============================================================
# build_files_from_manifest
# ============================================================
class TestBuildFilesFromManifest:
    def test_resolves_files(self, temp_dir, sample_files):
        fname = os.path.basename(sample_files["decision_html"])
        manifest_rows = [{"path": fname, "_manifest_row_number": 1}]
        files, failures, pmap = build_files_from_manifest(
            manifest_rows, input_dir=temp_dir
        )
        assert len(files) == 1
        assert len(failures) == 0
        assert files[0] in pmap

    def test_failure_for_missing(self, temp_dir):
        manifest_rows = [
            {"path": "nonexistent.pdf", "_manifest_row_number": 1},
        ]
        files, failures, _ = build_files_from_manifest(
            manifest_rows, input_dir=temp_dir
        )
        assert len(files) == 0
        assert len(failures) == 1
        assert failures[0]["failure_reason"] == "file_not_found"

    def test_limit(self, temp_dir, sample_files):
        rows = [
            {"path": os.path.basename(sample_files["decision_html"]), "_manifest_row_number": 1},
            {"path": os.path.basename(sample_files["non_buyback"]), "_manifest_row_number": 2},
        ]
        files, _, _ = build_files_from_manifest(rows, input_dir=temp_dir, limit=1)
        assert len(files) == 1


# ============================================================
# derived_* フォールバック
# ============================================================
class TestDerivedFallback:
    def test_derived_ticker_fallback(self, temp_dir):
        manifest = {
            "test.pdf": {
                "ticker": "",
                "derived_ticker": "2288",
                "title": "",
                "derived_title": "自己株式の取得",
                "disclosure_date": "",
                "derived_disclosure_date": "2026-02-24",
            }
        }
        meta = resolve_metadata(
            os.path.join(temp_dir, "test.pdf"),
            manifest, {"ticker": ""},
        )
        assert meta["ticker"] == "2288"
        assert meta["title"] == "自己株式の取得"
        assert meta["disclosure_date"] == "2026-02-24"

    def test_direct_ticker_over_derived(self, temp_dir):
        manifest = {
            "test.pdf": {
                "ticker": "6750",
                "derived_ticker": "2288",
            }
        }
        meta = resolve_metadata(
            os.path.join(temp_dir, "test.pdf"),
            manifest, {"ticker": ""},
        )
        assert meta["ticker"] == "6750"  # direct overrides derived


# ============================================================
# manifest 列引継
# ============================================================
class TestManifestColumnsCarryover:
    def test_manifest_columns_in_meta(self, temp_dir):
        manifest = {
            "test.pdf": {
                "ticker": "6750",
                "candidate_score": "8",
                "review_priority": "high",
                "matched_keywords": "自己株式の取得|取得価額の総額",
                "matched_keyword_count": "2",
            }
        }
        meta = resolve_metadata(
            os.path.join(temp_dir, "test.pdf"),
            manifest, {"ticker": ""},
        )
        assert meta["manifest_candidate_score"] == "8"
        assert meta["manifest_review_priority"] == "high"
        assert meta["manifest_matched_keywords"] == "自己株式の取得|取得価額の総額"


# ============================================================
# summary with manifest
# ============================================================
class TestSummaryManifest:
    def test_summary_includes_manifest_info(self):
        rows = [
            {"file_type": "pdf", "text_extract_ok": True,
             "is_buyback_related": True, "exclusion_reason": "",
             "event_type": "buyback_decision", "confidence_final": 0.85,
             "review_bucket": "high_confidence_extracted",
             "missing_key_fields": "",
             "manifest_review_priority": "high",
             "manifest_candidate_score": "8",
             **{f: None for f in [
                 "shares_limit", "shares_acquired", "shares_cancelled",
                 "amount_limit_million_yen", "amount_acquired_million_yen",
                 "ratio_to_outstanding", "start_date", "end_date", "cancel_date",
                 "acquisition_method", "board_resolution_date", "status_period_label",
             ]}},
        ]
        md = generate_review_summary(
            rows, [], "data/docs", False, 0.60,
            manifest_path="manifest.csv",
            manifest_total=10,
            manifest_resolved=9,
            manifest_resolve_failures=1,
        )
        assert "manifest" in md
        assert "manifest_review_priority" in md
        assert "candidate_score" in md

    def test_summary_no_manifest_section_without_manifest(self):
        rows = [
            {"file_type": "html", "text_extract_ok": True,
             "is_buyback_related": False, "exclusion_reason": "",
             "event_type": "", "confidence_final": 0.0,
             "review_bucket": "non_buyback", "missing_key_fields": "",
             **{f: None for f in [
                 "shares_limit", "shares_acquired", "shares_cancelled",
                 "amount_limit_million_yen", "amount_acquired_million_yen",
                 "ratio_to_outstanding", "start_date", "end_date", "cancel_date",
                 "acquisition_method", "board_resolution_date", "status_period_label",
             ]}},
        ]
        md = generate_review_summary(rows, [], "data/docs", False, 0.60)
        assert "manifest 連携集計" not in md


# ============================================================
# 既存 manifest なし実行が壊れていない
# ============================================================
class TestNoManifestStillWorks:
    def test_resolve_metadata_no_manifest(self, temp_dir):
        meta = resolve_metadata(
            os.path.join(temp_dir, "6750_2025-04-01_test.html"),
            {}, {"ticker": "", "disclosure_date": ""},
        )
        assert meta["ticker"] == "6750"
        assert meta["manifest_candidate_score"] == ""

