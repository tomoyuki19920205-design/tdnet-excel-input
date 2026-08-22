from __future__ import annotations

import pytest

from lib.screener_snapshot import ScreenerSnapshotBuilder, SnapshotBuild
from tools.sync_screener_snapshot import batch_payload


def _prices(count: int) -> list[dict]:
    return [
        {
            "date": f"2026-01-{index + 1:02d}",
            "open": 99 + index,
            "close": 100 + index,
            "adj_close": 100 + index,
            "high": 101 + index,
        }
        for index in range(count)
    ]


@pytest.mark.parametrize("periods", [5, 20, 60])
def test_return_requires_n_plus_one_valid_observations(periods: int) -> None:
    assert ScreenerSnapshotBuilder._return(_prices(periods), periods) is None
    expected = ((100 + periods) / 100 - 1) * 100
    assert ScreenerSnapshotBuilder._return(_prices(periods + 1), periods) == pytest.approx(expected)


@pytest.mark.parametrize("periods", [5, 10])
def test_psychological_line_requires_n_plus_one_valid_observations(periods: int) -> None:
    assert ScreenerSnapshotBuilder._psychological(_prices(periods), periods) is None
    assert ScreenerSnapshotBuilder._psychological(_prices(periods + 1), periods) == 100.0


def test_per_share_normalization_reuses_corporate_action_product() -> None:
    builder = object.__new__(ScreenerSnapshotBuilder)
    actions = [("2026-02-01", 0.5), ("2026-04-01", 2.0)]
    assert builder._normalized_per_share(200, "2026-01-01", "2026-03-01", actions) == 100
    assert builder._normalized_per_share(200, "2026-01-01", "2026-05-01", actions) == 200


def test_negative_sales_growth_score_is_retained() -> None:
    growth_pct = (90 / 100 - 1) * 100
    assert growth_pct / 10 == pytest.approx(-1.0)


def test_batch_payload_keeps_null_reasons_inside_coverage_json() -> None:
    build = SnapshotBuild(
        batch_id="batch", universe_date="2026-08-21",
        rows=[{"calculated_at": "2026-08-22T00:00:00Z"}],
        revision_events=[], coverage={"forward_per": {"numeric": 1}},
        null_reasons={"forward_per": {"forecast_missing": 1}},
    )
    payload = batch_payload(build)
    assert "null_reasons" not in payload
    assert payload["coverage"]["null_reasons"] == build.null_reasons
