from __future__ import annotations

from unittest.mock import patch

from lib.pipeline.financial_recovery_enqueue import _canonical_already_present


def test_exact_period_canonical_guard_accepts_sales_plus_valid_profit():
    row = {"code": "4263", "period": "2026-03-31", "quarter": "1Q"}
    canonical = [
        {"metric": "sales", "value": 100, "source": "jquants"},
        {"metric": "net_income", "value": 10, "source": "jquants"},
    ]
    with patch(
        "lib.pipeline.financial_recovery_enqueue.supabase_select",
        return_value=canonical,
    ) as select:
        assert _canonical_already_present(row) is True
    assert select.call_args.kwargs["params"]["ticker"] == "eq.4263"
    assert select.call_args.kwargs["params"]["period"] == "eq.2026-03-31"
    assert select.call_args.kwargs["params"]["quarter"] == "eq.1Q"


def test_canonical_guard_requires_complete_identity_and_core_metrics():
    with patch(
        "lib.pipeline.financial_recovery_enqueue.supabase_select",
        return_value=[{"metric": "sales"}],
    ):
        assert _canonical_already_present(
            {"code": "222A", "period": "2026-12-31", "quarter": "2Q"}
        ) is False
    with patch(
        "lib.pipeline.financial_recovery_enqueue.supabase_select"
    ) as select:
        assert _canonical_already_present({"code": "222A"}) is False
    select.assert_not_called()
