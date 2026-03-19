#!/usr/bin/env python3
"""quarantine review jsonl 出力テスト"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.segment_detection_v2 import (
    V2DetectionResult, write_quarantine_review, set_review_output_dir,
)


class TestQuarantineReviewOutput:
    def test_writes_jsonl(self, tmp_path):
        """1件追記される"""
        set_review_output_dir(str(tmp_path))

        result = V2DetectionResult(
            quarantine_reason="no_segment_page_candidate",
            failed_stage="page_scoring",
            review_hint="テスト",
        )
        write_quarantine_review(result, doc_id="doc1", ticker="1234", source_file="test.pdf")

        path = tmp_path / "quarantine_review_segment.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["doc_id"] == "doc1"
        assert data["ticker"] == "1234"
        assert data["failed_stage"] == "page_scoring"

    def test_utf8(self, tmp_path):
        """日本語が文字化けしない"""
        set_review_output_dir(str(tmp_path))

        result = V2DetectionResult(
            quarantine_reason="テスト理由",
            review_hint="日本語テスト",
        )
        write_quarantine_review(result, doc_id="doc2")

        path = tmp_path / "quarantine_review_segment.jsonl"
        text = path.read_text(encoding="utf-8")
        assert "テスト理由" in text

    def test_append(self, tmp_path):
        """複数件追記"""
        set_review_output_dir(str(tmp_path))

        for i in range(3):
            result = V2DetectionResult(quarantine_reason=f"reason_{i}")
            write_quarantine_review(result, doc_id=f"doc_{i}")

        path = tmp_path / "quarantine_review_segment.jsonl"
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

    def test_no_crash_on_error(self, tmp_path):
        """書き込みエラーでも例外が出ない"""
        set_review_output_dir("/nonexistent/dir/that/wont/exist")
        result = V2DetectionResult(quarantine_reason="test")
        # 例外が出ないこと
        write_quarantine_review(result)

    def test_header_row_snapshot(self, tmp_path):
        """header_snapshot/row_labels_sample が含まれる"""
        set_review_output_dir(str(tmp_path))

        result = V2DetectionResult(
            quarantine_reason="test",
            failed_stage="column_classification",
        )
        lines = ["売上高  利益", "自動車  50,000  3,000", "電子  30,000  2,000"]
        write_quarantine_review(result, best_table_lines=lines)

        path = tmp_path / "quarantine_review_segment.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert len(data["header_snapshot"]) > 0
        assert len(data["row_labels_sample"]) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
