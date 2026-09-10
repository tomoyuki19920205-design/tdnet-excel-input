from copy import deepcopy

import pytest

from lib.pipeline.canonical_writer import expand_financials_rows
from lib.pipeline.financial_integrity import duration_quarter, normalize_amount
from src.extractor import _is_consolidated_preferred, _is_current_duration
from tools.repair_canonical_financials_from_xbrl import (
    build_canonical_plan,
    build_financials_plan,
    extracted_metrics,
    validate_period,
)


FILING_ID = "202d007a6c328685cf7e76aa406d3a8002cdef19dc8c2cfe7a36738c57ae48b2"
PERIODS = {
    "1Q": {"end": "2025-10-31", "sales": 489_000_000, "op": -4_000_000},
    "2Q": {"end": "2026-01-31", "sales": 1_035_000_000, "op": -18_000_000},
    "3Q": {"end": "2026-04-30", "sales": 1_443_000_000, "op": -35_000_000},
    "FY": {"end": "2026-07-31", "sales": 2_906_000_000, "op": 336_000_000},
}


class Extracted6577:
    sales = 1_035_000_000
    gross_profit = 232_660_000
    operating_profit = -18_000_000
    ordinary_profit = -31_000_000
    net_income = -34_000_000
    field_sources = {
        "sales": "summary_xbrl",
        "gross_profit": "attachment_xbrl",
        "operating_profit": "summary_xbrl",
        "ordinary_profit": "summary_xbrl",
        "net_income": "summary_xbrl",
    }


def _bad_rows():
    return [
        {
            "id": 265446,
            "metric": "gross_profit",
            "value": 260,
            "unit": "millions_jpy",
            "source": "summary_xbrl",
            "source_priority": 1,
            "source_row_key": f"cf|6577|2026-07-31|2Q|gross_profit|summary_xbrl|{FILING_ID}",
            "updated_at": "2026-03-12T08:50:00+09:00",
        },
        {
            "id": 265447,
            "metric": "operating_profit",
            "value": -18_000_000,
            "unit": "百万円",
            "source": "summary_xbrl",
            "source_priority": 1,
            "source_row_key": f"cf|6577|2026-07-31|2Q|operating_profit|summary_xbrl|{FILING_ID}",
            "updated_at": "2026-03-12T08:50:00+09:00",
        },
    ]


def test_2q_cumulative_is_preserved_and_negative_sign_survives():
    desired = extracted_metrics(Extracted6577(), ["sales", "operating_profit"])
    assert desired["sales"]["value"] == 1035
    assert desired["operating_profit"]["value"] == -18
    assert normalize_amount(desired["operating_profit"]["value"], "millions_jpy") == -18


def test_standalone_2q_is_cumulative_2q_minus_1q():
    assert (PERIODS["2Q"]["sales"] - PERIODS["1Q"]["sales"]) / 1_000_000 == 546
    assert (PERIODS["2Q"]["op"] - PERIODS["1Q"]["op"]) / 1_000_000 == -14


def test_yen_is_converted_once_and_millions_are_not_rescaled():
    desired = extracted_metrics(Extracted6577(), ["sales"])["sales"]["value"]
    assert desired == 1035
    rows, skipped = expand_financials_rows(
        ticker="6577",
        period="2026-07-31",
        quarter="2Q",
        metrics_dict={"sales": desired},
        source="summary_xbrl",
        filing_id=FILING_ID,
        unit="millions_jpy",
    )
    assert skipped == 0
    assert rows[0]["value"] == 1035


def test_fiscal_year_end_is_not_the_2q_period_end():
    validate_period(
        fiscal_year_end="2026-07-31",
        period_start="2025-08-01",
        period_end="2026-01-31",
        quarter="2Q",
    )
    assert duration_quarter("2025-08-01", "2026-01-31", "2025-08-01", "2026-07-31") == "2Q"
    assert duration_quarter("2025-08-01", "2026-07-31", "2025-08-01", "2026-07-31") == "FY"


def test_prior_and_nonconsolidated_contexts_cannot_override_current_consolidated():
    assert _is_current_duration("InterimDuration")
    assert not _is_current_duration("Prior1InterimDuration")
    assert _is_consolidated_preferred("InterimDuration_ConsolidatedMember_ResultMember")
    assert not _is_consolidated_preferred("InterimDuration_NonConsolidatedMember_ResultMember")


def test_repair_plan_corrects_prior_gp_and_yen_scaled_op_and_is_idempotent():
    desired = extracted_metrics(Extracted6577(), ["gross_profit", "operating_profit"])
    plan = build_canonical_plan(
        _bad_rows(),
        ticker="6577",
        fiscal_year_end="2026-07-31",
        quarter="2Q",
        filing_id=FILING_ID,
        desired_metrics=desired,
    )
    assert [(c.metric, c.after["value"]) for c in plan] == [
        ("gross_profit", 232.66),
        ("operating_profit", -18),
    ]
    repaired = deepcopy(_bad_rows())
    for change in plan:
        row = next(r for r in repaired if str(r["id"]) == change.primary_key)
        row.update(change.after)
    assert build_canonical_plan(
        repaired,
        ticker="6577",
        fiscal_year_end="2026-07-31",
        quarter="2Q",
        filing_id=FILING_ID,
        desired_metrics=desired,
    ) == []


def test_legacy_financials_plan_is_narrow_and_idempotent():
    row = {
        "ticker": "6577",
        "period": "2026-07-31",
        "quarter": "2Q",
        "sales": 1035,
        "gross_profit": 260,
        "operating_profit": -18,
        "source": "tdnet",
        "unit": "million_yen",
        "updated_at": "2026-09-09T19:01:29+09:00",
    }
    desired = extracted_metrics(Extracted6577(), ["gross_profit", "operating_profit"])
    plan = build_financials_plan(
        [row],
        ticker="6577",
        fiscal_year_end="2026-07-31",
        quarter="2Q",
        desired_metrics=desired,
    )
    assert len(plan) == 1
    assert plan[0].primary_key == "6577|2026-07-31|2Q"
    assert plan[0].metric == "gross_profit"
    repaired = {**row, "gross_profit": plan[0].after["value"]}
    assert build_financials_plan(
        [repaired],
        ticker="6577",
        fiscal_year_end="2026-07-31",
        quarter="2Q",
        desired_metrics=desired,
    ) == []


def test_repair_scope_leaves_1q_3q_and_fy_unchanged():
    controls = deepcopy({q: PERIODS[q] for q in ("1Q", "3Q", "FY")})
    desired = extracted_metrics(Extracted6577(), ["gross_profit", "operating_profit"])
    build_canonical_plan(
        _bad_rows(),
        ticker="6577",
        fiscal_year_end="2026-07-31",
        quarter="2Q",
        filing_id=FILING_ID,
        desired_metrics=desired,
    )
    assert {q: PERIODS[q] for q in ("1Q", "3Q", "FY")} == controls


def test_repair_rejects_duplicate_metric_rows_for_same_filing():
    rows = _bad_rows() + [{**_bad_rows()[0], "id": 999999}]
    with pytest.raises(ValueError, match="exactly one canonical row"):
        build_canonical_plan(
            rows,
            ticker="6577",
            fiscal_year_end="2026-07-31",
            quarter="2Q",
            filing_id=FILING_ID,
            desired_metrics=extracted_metrics(Extracted6577(), ["gross_profit"]),
        )
