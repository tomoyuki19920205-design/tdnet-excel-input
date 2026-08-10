"""Safety contracts for the exact non-consolidated OP repair manifest."""
from __future__ import annotations

import copy

import pytest

from tools.repair_nonconsolidated_operating_profit_leak import (
    _validate_manifest,
    manifest_sha256,
)


def _row(row_id: int) -> dict:
    ticker = str(8000 + row_id)
    return {
        "id": row_id,
        "ticker": ticker,
        "period": "2026-03-31",
        "quarter": "FY",
        "metric": "operating_profit",
        "source": "summary_xbrl",
        "value": float(row_id),
        "source_row_key": f"cf|{ticker}|2026-03-31|FY|operating_profit|summary_xbrl|",
        "official_context": "CurrentYearDuration_NonConsolidatedMember_ResultMember",
    }


def _manifest() -> dict:
    return {"expected_delete_count": 3, "rows": [_row(1), _row(2), _row(3)]}


def test_manifest_hash_does_not_hash_itself():
    manifest = _manifest()
    digest = manifest_sha256(manifest)
    manifest["manifest_sha256"] = digest
    assert manifest_sha256(manifest) == digest


def test_exact_three_nonconsolidated_summary_op_rows_are_allowed():
    assert len(_validate_manifest(_manifest())) == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [("metric", "profit_before_tax"), ("source", "tdnet_xbrl")],
)
def test_out_of_scope_metric_or_source_is_rejected(field: str, value: str):
    manifest = _manifest()
    manifest["rows"][0][field] = value
    with pytest.raises(RuntimeError, match="out-of-scope"):
        _validate_manifest(manifest)


def test_count_mismatch_stops_before_delete():
    manifest = _manifest()
    manifest["rows"] = copy.deepcopy(manifest["rows"][:2])
    manifest["expected_delete_count"] = 2
    with pytest.raises(RuntimeError, match="exactly three"):
        _validate_manifest(manifest)
