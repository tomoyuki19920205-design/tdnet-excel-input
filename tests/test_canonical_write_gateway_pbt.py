from src.events.canonical_write_gateway import validate_canonical_write_plan
from src.events.pipeline_context import CanonicalWritePlan


def test_official_tdnet_pbt_is_allowed_as_actual_canonical_metric():
    plan = CanonicalWritePlan(
        ticker="5713",
        period="2026-03-31",
        quarter="FY",
        metric="profit_before_tax",
        value=255_680,
        unit="millions_jpy",
        source="tdnet_xbrl",
        filing_id="20260511521788",
    )
    plan.validate_and_prepare()
    validate_canonical_write_plan(plan)
    assert plan.write_allowed is True
