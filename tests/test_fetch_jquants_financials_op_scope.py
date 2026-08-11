"""Regression tests for J-Quants operating-profit consolidation scope."""

import pytest

from tools.fetch_jquants_financials import _row_to_db


def _summary_row(**overrides):
    row = {
        "Code": "99990",
        "DiscDate": "2026-05-11",
        "CurFYEn": "2026-03-31",
        "CurPerType": "FY",
        "DocType": "FYFinancialStatements_Consolidated_IFRS",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("op", "ncop", "expected"),
    [
        ("42000000000", "133617000000", 42_000_000_000),
        ("", "133617000000", None),
        (None, None, None),
    ],
    ids=[
        "consolidated_op_wins_over_ncop",
        "blank_op_does_not_fall_back_to_ncop",
        "missing_op_and_ncop_stays_null",
    ],
)
def test_consolidated_operating_profit_uses_only_op(op, ncop, expected):
    result = _row_to_db(_summary_row(OP=op, NCOP=ncop))

    assert result is not None
    assert result["operating_profit"] == expected


def test_5713_fy2026_non_consolidated_op_is_not_promoted():
    payload = _summary_row(
        Code="57130",
        DiscNo="20260511521788",
        Sales="1741586000000",
        OP="",
        NCOP="133617000000",
        **{
            "Gross profit (IFRS)": "274503000000",
            "Profit (loss) before tax from continuing operations (IFRS)":
                "255680000000",
        },
    )

    result = _row_to_db(payload)

    assert result is not None
    assert result["local_code"] == "57130"
    assert result["current_fiscal_year_end_date"] == "2026-03-31"
    assert result["type_of_current_period"] == "FY"
    assert result["operating_profit"] is None


def test_rejects_9249_style_stale_interim_period_metadata():
    payload = _summary_row(
        Code="92490",
        DiscDate="2026-05-15",
        CurFYEn="2025-09-30",
        CurPerType="2Q",
        DocType="2QFinancialStatements_Consolidated_JP",
        Sales="7882000000",
        OP="1019000000",
    )

    assert _row_to_db(payload) is None


def test_accepts_interim_result_before_current_fiscal_year_end():
    payload = _summary_row(
        DiscDate="2026-05-15",
        CurFYEn="2026-09-30",
        CurPerType="2Q",
        DocType="2QFinancialStatements_Consolidated_JP",
        Sales="7882000000",
        OP="1019000000",
    )

    assert _row_to_db(payload) is not None
