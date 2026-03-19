#!/usr/bin/env python3
"""tdnet_ingest.py のサマリメトリクス拡張テスト"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from tools.tdnet_ingest import build_ingest_summary, print_ingest_summary


# テスト用ダミー結果
def _make_results(
    n_success=3, n_error=1, n_skip=2,
    seg_records=5, v2_adopted=True,
    quarantine_reason="",
):
    results = []
    for i in range(n_success):
        sm = {
            "segment_records": seg_records,
            "segment_detected": seg_records > 0,
            "v2_adopted": v2_adopted,
            "v1_fallback": not v2_adopted,
            "candidate_tables": 3,
            "scored_pages": 2,
            "unit_unknown": False,
            "profit_col_role": "operating_profit_like",
            "non_reportable_rows": 2,
        }
        results.append({
            "status": "inserted",
            "detail": f"7203 2025-03-31 {i+1}Q sales=10000",
            "code": "7203",
            "seg_metrics": sm,
            "source_type": "zip",
        })
    for _ in range(n_error):
        results.append({
            "status": "error",
            "detail": "抽出失敗",
            "code": "9999",
            "seg_metrics": {"segment_detected": False, "quarantine_reason": quarantine_reason} if quarantine_reason else {},
            "source_type": "zip",
        })
    for _ in range(n_skip):
        results.append({
            "status": "skipped",
            "detail": "処理済み",
            "code": "1234",
        })
    return results


class TestBuildIngestSummary:
    def test_basic_metrics(self):
        results = _make_results()
        s = build_ingest_summary(results, results, results, 3, "test-run", 1.5)
        assert s["files_total"] == 6
        assert s["success"] == 3
        assert s["errors"] == 1
        assert s["skipped"] == 2
        assert s["segment_records"] == 15  # 5 * 3
        assert s["segment_detected_docs"] == 3
        assert s["v2_adopted"] == 3
        assert s["v1_fallback"] == 0
        assert s["elapsed"] == 1.5

    def test_v1_fallback(self):
        results = _make_results(v2_adopted=False)
        s = build_ingest_summary(results, results, results, 3, "test", 1.0)
        assert s["v1_fallback"] == 3
        assert s["v2_adopted"] == 0

    def test_quarantine_reason_top(self):
        results = _make_results(n_error=3, quarantine_reason="no_sales_columns")
        s = build_ingest_summary(results, results, results, 3, "test", 1.0)
        assert "no_sales_columns" in s["quarantine_reason_top"]
        assert s["quarantine_reason_top"]["no_sales_columns"] == 3

    def test_empty_results(self):
        s = build_ingest_summary([], [], [], 0, "test", 0.0)
        assert s["files_total"] == 0
        assert s["segment_records"] == 0
        assert s["quarantine_reason_top"] == {}
        assert s["elapsed"] == 0.0

    def test_missing_seg_metrics(self):
        """seg_metrics が無い result でも落ちない"""
        results = [{"status": "inserted", "detail": "test", "code": "1234"}]
        s = build_ingest_summary(results, results, results, 1, "test", 0.5)
        assert s["segment_records"] == 0
        assert s["v2_adopted"] == 0

    def test_unit_unknown(self):
        results = _make_results(n_success=1)
        results[0]["seg_metrics"]["unit_unknown"] = True
        s = build_ingest_summary(results, results, results, 1, "test", 0.1)
        assert s["unit_unknown_count"] == 1

    def test_profit_col_unknown(self):
        results = _make_results(n_success=1)
        results[0]["seg_metrics"]["profit_col_role"] = ""
        s = build_ingest_summary(results, results, results, 1, "test", 0.1)
        assert s["profit_col_unknown_count"] == 1

    def test_avg_metrics(self):
        results = _make_results(n_success=2)
        s = build_ingest_summary(results, results, results, 2, "test", 0.1)
        assert s["avg_candidate_tables_per_doc"] == 3.0
        assert s["avg_scored_pages_per_doc"] == 2.0


class TestPrintIngestSummary:
    def test_output_contains_kv_summary(self, capsys):
        results = _make_results()
        s = build_ingest_summary(results, results, results, 3, "test", 1.5)
        print_ingest_summary(s)
        out = capsys.readouterr().out
        assert "[SUMMARY]" in out
        assert "files_total=6" in out
        assert "v2_adopted=3" in out
        assert "elapsed=1.5" in out

    def test_output_human_readable(self, capsys):
        results = _make_results()
        s = build_ingest_summary(results, results, results, 3, "test", 1.5)
        print_ingest_summary(s)
        out = capsys.readouterr().out
        assert "結果サマリ" in out
        assert "Segment v2 メトリクス" in out

    def test_quarantine_top_in_output(self, capsys):
        results = _make_results(n_error=2, quarantine_reason="no_sales_columns")
        s = build_ingest_summary(results, results, results, 3, "test", 1.5)
        print_ingest_summary(s)
        out = capsys.readouterr().out
        assert "quarantine_reason_top" in out
        assert "no_sales_columns" in out

    def test_empty_does_not_crash(self, capsys):
        s = build_ingest_summary([], [], [], 0, "empty", 0.0)
        print_ingest_summary(s)  # crash しないこと

    def test_kv_format_grep(self, capsys):
        """key=value 形式が grep できる"""
        results = _make_results()
        s = build_ingest_summary(results, results, results, 3, "test", 1.0)
        print_ingest_summary(s)
        out = capsys.readouterr().out
        # 少なくとも 17 のキーが [SUMMARY] 行にある
        summary_line = [l for l in out.split("\n") if l.startswith("[SUMMARY]")]
        assert len(summary_line) == 1
        kv_count = summary_line[0].count("=")
        assert kv_count >= 17


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
