#!/usr/bin/env python3
"""Quarantine Stage-aware モデルのテスト"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest
from src.analysis.candidate_models import ExtractionStage
from src.pipeline.quarantine_models import (
    QuarantineInfo,
    build_quarantine_info,
)
from src.migration.migration_db import MigrationDB


class TestExtractionStage:
    def test_stage_values(self):
        assert ExtractionStage.SOURCE_LOAD == "source_load"
        assert ExtractionStage.STRUCTURAL_PARSE == "structural_parse"
        assert ExtractionStage.CANDIDATE_DETECT == "candidate_detect"
        assert ExtractionStage.SEMANTIC_INTERPRET == "semantic_interpret"
        assert ExtractionStage.RECORD_BUILD == "record_build"


class TestQuarantineInfo:
    def test_basic_construction(self):
        info = QuarantineInfo(
            company_code="1801",
            reason="segment_table_found_but_no_sales_profit_columns",
            failed_stage="candidate_detect",
            review_hint="売上列候補が見つかりません。",
        )
        assert info.company_code == "1801"
        assert info.failed_stage == "candidate_detect"
        assert info.review_hint.startswith("売上列")

    def test_to_quarantine_kwargs(self):
        info = QuarantineInfo(
            company_code="1801",
            reason="test_reason",
            fiscal_year_end="2025-03-31",
            quarter="3Q",
            failed_stage="semantic_interpret",
            review_hint="テストヒント",
        )
        kwargs = info.to_quarantine_kwargs()
        assert kwargs["company_code"] == "1801"
        assert kwargs["reason"] == "test_reason"
        assert kwargs["failed_stage"] == "semantic_interpret"
        assert kwargs["review_hint"] == "テストヒント"


class TestBuildQuarantineInfo:
    def test_with_hint_key(self):
        info = build_quarantine_info(
            company_code="1801",
            reason="no_sales_col",
            failed_stage=ExtractionStage.SEMANTIC_INTERPRET,
            review_hint_key="no_sales_col",
        )
        assert "SALES_COL_KEYWORDS" in info.review_hint
        assert info.failed_stage == "semantic_interpret"

    def test_with_custom_hint(self):
        info = build_quarantine_info(
            company_code="1801",
            reason="custom",
            review_hint_custom="カスタムヒント",
        )
        assert info.review_hint == "カスタムヒント"

    def test_with_candidate_scores(self):
        info = build_quarantine_info(
            company_code="1801",
            reason="test",
            candidate_scores={"segment_table": 0.3, "sales_col": 0.0},
        )
        parsed = json.loads(info.candidate_score_json)
        assert parsed["segment_table"] == 0.3

    def test_with_rule_trace(self):
        info = build_quarantine_info(
            company_code="1801",
            reason="test",
            rule_trace=["rule1: matched 営業利益", "rule2: no sales"],
        )
        assert len(info.rule_trace) == 2


class TestQuarantineDBIntegration:
    """DB の quarantine テーブルに stage-aware データを書き込めるか"""

    def setup_method(self):
        from src.persist_policy import init_persist_policy, reset_persist_policy
        reset_persist_policy()
        init_persist_policy(cli_flag=True)

    def teardown_method(self):
        from src.persist_policy import reset_persist_policy
        reset_persist_policy()

    def test_quarantine_with_stage(self, tmp_path):
        db = MigrationDB(str(tmp_path / "test.db"))
        db.quarantine_record(
            "1801", "segment_table_found_but_no_sales_profit_columns",
            fiscal_year_end="2025-03-31", quarter="3Q",
            metric_type="segment",
            failed_stage="candidate_detect",
            review_hint="売上列候補が見つかりません。",
        )
        db.commit()

        cur = db._conn.execute("SELECT * FROM quarantine")
        rows = cur.fetchall()
        assert len(rows) == 1

        cols = [d[0] for d in cur.description]
        row_dict = dict(zip(cols, rows[0]))
        assert row_dict["failed_stage"] == "candidate_detect"
        assert "売上列" in row_dict["review_hint"]
        db.close()

    def test_quarantine_backward_compatible(self, tmp_path):
        """既存呼び出し (failed_stage/review_hint なし) でもエラーにならない"""
        db = MigrationDB(str(tmp_path / "test.db"))
        db.quarantine_record(
            "1801", "no_segment_table",
            fiscal_year_end="2025-03-31", quarter="3Q",
        )
        db.commit()

        cur = db._conn.execute("SELECT * FROM quarantine")
        rows = cur.fetchall()
        assert len(rows) == 1

        cols = [d[0] for d in cur.description]
        row_dict = dict(zip(cols, rows[0]))
        assert row_dict["failed_stage"] == ""
        assert row_dict["review_hint"] == ""
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
