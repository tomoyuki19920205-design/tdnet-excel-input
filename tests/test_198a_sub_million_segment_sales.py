from __future__ import annotations

from copy import deepcopy

import pytest

from src.segment.extraction_result_validator import (
    ExtractionStatus,
    HardFailReason,
    validate_extraction_result,
)


def _records():
    common = {
        "period": "2026-05-31",
        "quarter": "1Q",
        "_segment_period_role": "current",
        "_segment_member_kind": "reportable",
        "_sales_fact_explicit_nil": False,
        "_sales_fact_numeric_present": True,
        "_sales_fact_names": ["jppfs_cor:netsales"],
        "_sales_fact_selected_name": "jppfs_cor:netsales",
        "_selected_reportable_sales_total_raw": 190_294,
        "_selected_sales_raw_sum": 190_294,
        "_sales_rounding_reconciliation_verified": True,
        "_context_evidence": {"date_guard_status": "PASS"},
    }
    return [
        {
            **common,
            "segment_name": "Financial And Economic Information Platform Business",
            "segment_sales": 189,
            "segment_profit": -8,
            "_sales_fact_selected_raw": 189_311,
            "_sales_fact_rounds_to_zero": False,
        },
        {
            **common,
            "segment_name": "Trade Platform Business",
            "segment_sales": 0,
            "segment_profit": -88,
            "_sales_fact_selected_raw": 983,
            "_sales_fact_rounds_to_zero": True,
        },
    ]


def test_198a_verified_sub_million_sales_is_partial():
    result = validate_extraction_result(_records(), source="xbrl")
    assert result.status is ExtractionStatus.PARTIAL
    assert result.hard_fail_reason is HardFailReason.NONE
    assert result.sales_non_null_count == 1
    assert "rounds to zero" in result.reason


@pytest.mark.parametrize(
    "row,field,value",
    [
        (1, "_sales_fact_numeric_present", False),
        (1, "_sales_fact_rounds_to_zero", False),
        (1, "_sales_fact_selected_raw", 0),
        (1, "_sales_fact_explicit_nil", True),
        (1, "_sales_fact_names", []),
        (1, "_sales_fact_selected_name", "other:revenue"),
        (0, "_selected_reportable_sales_total_raw", 190_295),
        (0, "_selected_sales_raw_sum", 190_293),
        (1, "_sales_rounding_reconciliation_verified", False),
        (1, "_segment_period_role", "previous"),
        (1, "_segment_member_kind", "adjustment"),
        (1, "quarter", "2Q"),
        (1, "segment_name", "Financial And Economic Information Platform Business"),
        (1, "segment_profit", None),
    ],
)
def test_sub_million_contract_fails_closed(row, field, value):
    records = deepcopy(_records())
    records[row][field] = value
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.hard_fail_reason is HardFailReason.TOO_FEW_SALES


def test_period_mismatch_fails_closed():
    records = _records()
    records[1]["period"] = "2025-05-31"
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE


def test_context_date_guard_must_pass():
    records = _records()
    records[1]["_context_evidence"] = {"date_guard_status": "MISMATCH"}
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE


def test_plain_zero_sales_without_raw_evidence_stays_quarantined():
    records = [
        {"segment_name": "Alpha Business", "segment_sales": 100, "segment_profit": 10},
        {"segment_name": "Beta Business", "segment_sales": 0, "segment_profit": 5},
    ]
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.hard_fail_reason is HardFailReason.TOO_FEW_SALES
