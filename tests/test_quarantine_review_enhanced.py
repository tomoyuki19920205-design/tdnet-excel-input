#!/usr/bin/env python3
"""quarantine review JSONL 強化版テスト"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.analysis.segment_detection_v2 import (
    V2DetectionResult, write_quarantine_review, set_review_output_dir,
    _build_column_diagnosis,
)


class TestQuarantineReviewEnhanced:
    def test_ticker_doc_id_always_filled(self, tmp_path):
        """ticker/doc_id が空でも '?' が入る"""
        set_review_output_dir(str(tmp_path))
        result = V2DetectionResult(quarantine_reason="test")
        write_quarantine_review(result)  # 引数なし
        path = tmp_path / "quarantine_review_segment.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert data["ticker"] == "?"
        assert data["doc_id"] == "?"
        assert data["source_file"] == "?"

    def test_ticker_doc_id_passed(self, tmp_path):
        """指定すればそのまま入る"""
        set_review_output_dir(str(tmp_path))
        result = V2DetectionResult(quarantine_reason="test")
        write_quarantine_review(result, doc_id="doc123", ticker="7804", source_file="test.pdf")
        path = tmp_path / "quarantine_review_segment.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert data["ticker"] == "7804"
        assert data["doc_id"] == "doc123"
        assert data["source_file"] == "test.pdf"

    def test_header_snapshot_10_lines(self, tmp_path):
        """header_snapshot が最大10行"""
        set_review_output_dir(str(tmp_path))
        lines = [f"ヘッダー行{i}" for i in range(15)]
        result = V2DetectionResult(quarantine_reason="test")
        write_quarantine_review(result, best_table_lines=lines)
        path = tmp_path / "quarantine_review_segment.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert len(data["header_snapshot"]) == 10

    def test_row_labels_20(self, tmp_path):
        """row_labels_sample が最大20件"""
        set_review_output_dir(str(tmp_path))
        lines = [f"セグメント{i}    {i*1000}" for i in range(25)]
        result = V2DetectionResult(quarantine_reason="test")
        write_quarantine_review(result, best_table_lines=lines)
        path = tmp_path / "quarantine_review_segment.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert len(data["row_labels_sample"]) == 20

    def test_table_index(self, tmp_path):
        """table_index が出力に含まれる"""
        set_review_output_dir(str(tmp_path))
        result = V2DetectionResult(quarantine_reason="test")
        write_quarantine_review(result, table_index=2)
        path = tmp_path / "quarantine_review_segment.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert data["table_index"] == 2

    def test_column_diagnosis_present(self, tmp_path):
        """column_diagnosis が渡された場合に出力に含まれる"""
        set_review_output_dir(str(tmp_path))
        result = V2DetectionResult(
            quarantine_reason="segment_table_found_but_no_sales_profit_columns",
            failed_stage="column_classification",
        )
        col_diag = {
            "reconstructed_headers": ["売上高", "利益率", "資産"],
            "column_labels": [["自動車", "50,000", "3.0%"]],
            "column_roles": ["sales", "margin_like", "assets_like"],
            "taxonomy_scores": [{"sales": 0.9}, {"margin_like": 0.8}],
            "header_role_scores": {"sales": 0.3, "operating_profit": 0.1},
        }
        write_quarantine_review(result, column_diagnosis=col_diag,
                                 doc_id="doc1", ticker="7804")
        path = tmp_path / "quarantine_review_segment.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert data["candidate_column_roles"] == ["sales", "margin_like", "assets_like"]
        assert data["reconstructed_headers"] == ["売上高", "利益率", "資産"]
        assert len(data["column_taxonomy_scores"]) == 2
        assert data["header_role_fallback_scores"]["sales"] == 0.3

    def test_column_diagnosis_absent(self, tmp_path):
        """column_diagnosis が渡されない場合はデフォルト"""
        set_review_output_dir(str(tmp_path))
        result = V2DetectionResult(quarantine_reason="test")
        write_quarantine_review(result)
        path = tmp_path / "quarantine_review_segment.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert data["candidate_column_labels"] == []
        assert data["candidate_column_roles"] == []


class TestBuildColumnDiagnosis:
    def test_basic(self):
        class FakeColResult:
            column_roles = ["sales", "unknown"]
            role_score_breakdown = [{"sales": 0.9}, {"unknown": 0.5}]
        cr = FakeColResult()
        diag = _build_column_diagnosis(cr, ["売上高", "不明列"], [["A", "500"]], {"sales": 0.3})
        assert diag["reconstructed_headers"] == ["売上高", "不明列"]
        assert len(diag["column_roles"]) == 2
        assert diag["header_role_scores"]["sales"] == 0.3

    def test_empty(self):
        class EmptyResult:
            column_roles = None
            role_score_breakdown = None
        diag = _build_column_diagnosis(EmptyResult(), [], [], None)
        assert diag["column_roles"] == []
        assert diag["taxonomy_scores"] == []
        assert diag["header_role_scores"] == {}


class TestSummaryInLog:
    """[SUMMARY] がログファイルにも残ることをテスト"""
    def test_summary_logged(self, capsys):
        import logging
        handler = logging.handlers.MemoryHandler(capacity=100) if hasattr(logging, 'handlers') else None
        # logger.info が呼ばれることを確認 (mock)
        from unittest.mock import patch
        with patch('tools.tdnet_ingest.logger') as mock_logger:
            from tools.tdnet_ingest import print_ingest_summary, build_ingest_summary
            s = build_ingest_summary([], [], [], 0, "test", 0.0)
            print_ingest_summary(s)
            mock_logger.info.assert_called_once()
            call_arg = mock_logger.info.call_args[0][0]
            assert call_arg.startswith("[SUMMARY]")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
