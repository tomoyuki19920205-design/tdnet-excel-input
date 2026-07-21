from __future__ import annotations

from copy import deepcopy

import pytest

from src.segment.extraction_result_validator import (
    ExtractionStatus,
    HardFailReason,
    validate_extraction_result,
)


def _record(
    name: str,
    *,
    role: str,
    sales: int | None,
    profit: int | None,
    member_kind: str = "reportable",
    period: str = "2026-03-31",
    quarter: str = "1Q",
) -> dict:
    return {
        "segment_name": name,
        "segment_sales": sales,
        "segment_profit": profit,
        "period": period,
        "quarter": quarter,
        "_segment_period_role": role,
        "_segment_member_kind": member_kind,
        "_sales_fact_numeric_present": sales is not None,
        "_context_evidence": {"date_guard_status": "PASS"},
    }


def _2158_records(quarter: str = "1Q") -> list[dict]:
    return [
        _record("Life Science AI", role="previous", sales=64, profit=-65, quarter=quarter),
        _record("Risk Management", role="previous", sales=1458, profit=200, quarter=quarter),
        _record("DX", role="previous", sales=58, profit=14, quarter=quarter),
        _record("Life Science AI", role="current", sales=107, profit=-138, quarter=quarter),
        _record("Risk Management", role="current", sales=966, profit=-8, quarter=quarter),
        _record("DX", role="current", sales=472, profit=56, quarter=quarter),
    ]


@pytest.mark.parametrize("quarter", ["1Q", "2Q", "3Q"])
def test_2158_reportable_dx_rows_are_valid(quarter):
    result = validate_extraction_result(_2158_records(quarter), source="xbrl")
    assert result.status is ExtractionStatus.SUCCESS
    assert result.hard_fail_reason is HardFailReason.NONE
    assert result.raw_segment_count == 6
    assert result.valid_segment_count == 6
    assert result.invalid_segment_count == 0
    assert [v.matched_rule for v in result.validations[2::3]] == [
        "xbrl_short_abbreviation_exempt",
        "xbrl_short_abbreviation_exempt",
    ]


def test_reportable_member_evidence_is_required_for_roman_like_abbreviation():
    records = _2158_records()
    records[2]["_segment_member_kind"] = "other"
    records[5]["_segment_member_kind"] = "other"
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.hard_fail_reason is HardFailReason.HIGH_INVALID_RATIO
    assert result.invalid_segment_count == 2


@pytest.mark.parametrize("name", ["I", "II", "IV", "X"])
def test_generic_roman_numeral_rows_remain_invalid(name):
    records = _2158_records()
    records[2] = _record(name, role="previous", sales=58, profit=14, member_kind="other")
    records[5] = _record(name, role="current", sales=472, profit=56, member_kind="other")
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.invalid_segment_count == 2


@pytest.mark.parametrize("missing_facts", [True, False])
def test_reportable_dx_without_required_evidence_fails_closed(missing_facts):
    records = _2158_records()
    for index in (2, 5):
        if missing_facts:
            records[index]["segment_sales"] = None
            records[index]["segment_profit"] = None
        else:
            records[index]["_segment_member_kind"] = "adjustment"
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.invalid_segment_count == 2


def test_non_xbrl_dx_rows_are_not_exempt():
    result = validate_extraction_result(_2158_records(), source="pdf")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.invalid_segment_count == 2


def test_genuine_high_invalid_ratio_still_quarantines():
    records = _2158_records()
    records[2]["segment_name"] = "販売費及び一般管理費"
    records[5]["segment_name"] = "支払利息"
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.QUARANTINE
    assert result.hard_fail_reason is HardFailReason.HIGH_INVALID_RATIO


def test_adjustment_rows_are_not_counted_as_valid_segments():
    records = _2158_records() + [
        _record("調整額", role="current", sales=-10, profit=2, member_kind="adjustment")
    ]
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.SUCCESS
    assert result.valid_segment_count == 6
    assert result.invalid_segment_count == 0


def test_total_rows_are_not_counted_as_valid_segments():
    records = _2158_records() + [
        _record("合計", role="current", sales=1600, profit=100, member_kind="other")
    ]
    result = validate_extraction_result(records, source="xbrl")
    assert result.status is ExtractionStatus.SUCCESS
    assert result.valid_segment_count == 6
    assert result.invalid_segment_count == 0


def test_period_and_quarter_metadata_are_not_rewritten():
    records = _2158_records("2Q")
    original = deepcopy(records)
    validate_extraction_result(records, source="xbrl")
    assert records == original
