from __future__ import annotations

from pathlib import Path

import pytest

from lib.forecast_revision_canonical import (
    canonical_forecast_anchors,
    canonicalize_statement_rows,
    is_forecast_retraction,
    metadata_role,
)
from lib.jquants_values import parse_optional_boolean
from lib.screener_snapshot import ScreenerSnapshotBuilder, SnapshotBuild
from tools.sync_screener_snapshot import batch_payload, publish
from tools.sync_jquants_tdnet_metadata import _unmatched_economic_date


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


@pytest.mark.parametrize(
    ("forward_per", "growth_pct", "expected"),
    [(10, 20, 0.5), (20, 20, 1.0), (10, 10, 1.0), (10, 5, 2.0)],
)
def test_forward_per_per_forecast_sales_growth_examples(
    forward_per: float, growth_pct: float, expected: float
) -> None:
    assert forward_per / growth_pct == pytest.approx(expected)


@pytest.mark.parametrize("growth_pct", [0, -0.01, -10])
def test_nonpositive_forecast_sales_growth_has_no_score(growth_pct: float) -> None:
    score = 10 / growth_pct if growth_pct > 0 else None
    assert score is None


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


def test_publish_stages_revision_events_before_atomic_pointer_swap() -> None:
    class Writer:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def upsert(self, table: str, rows: list[dict], on_conflict: str) -> None:
            self.calls.append(("upsert", table, rows, on_conflict))

        def request(self, method: str, path: str, **kwargs: object) -> object:
            self.calls.append(("request", method, path, kwargs))
            return object()

    build = SnapshotBuild(
        batch_id="batch", universe_date="2026-08-21",
        rows=[{"ticker": "1234", "calculated_at": "2026-08-22T00:00:00Z"}],
        revision_events=[{"batch_id": "batch", "ticker": "1234"}],
        coverage={}, null_reasons={},
    )
    writer = Writer()
    publish(build, writer)  # type: ignore[arg-type]
    assert writer.calls[1][1] == "forecast_revision_events_staging"
    assert writer.calls[2][1] == "screener_metrics"
    assert writer.calls[3][2] == "/rpc/publish_screener_batch"
    assert writer.calls[3][3]["json"]["p_expected_revision_events"] == 1


def test_atomic_publish_migration_replaces_jquants_events_inside_rpc() -> None:
    sql = (Path(__file__).parents[1] / "migrations" / "013_atomic_revision_event_publish.sql").read_text(
        encoding="utf-8"
    )
    function = sql.split("CREATE OR REPLACE FUNCTION", 1)[1]
    assert "DELETE FROM public.forecast_revision_events WHERE source = 'jquants'" in function
    assert "INSERT INTO public.forecast_revision_events" in function
    assert "INSERT INTO public.screener_current_batch" in function
    assert function.index("DELETE FROM public.forecast_revision_events") < function.index(
        "INSERT INTO public.screener_current_batch"
    )


def test_historical_republication_uses_disclosure_number_date() -> None:
    assert _unmatched_economic_date("20230818544110", "2023-11-02") == (
        "2023-08-18", "historical_republication"
    )
    assert _unmatched_economic_date("20231101577737", "2023-11-02") == (
        "2023-11-02", "statements_only"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True), ("true", True), (" TRUE ", True), ("1", True), (1, True),
        (False, False), ("false", False), (" FALSE ", False), ("0", False), (0, False),
        ("", None), ("   ", None), (None, None),
    ],
)
def test_jquants_optional_boolean_is_strict(raw: object, expected: bool | None) -> None:
    assert parse_optional_boolean(raw) is expected


@pytest.mark.parametrize("raw", ["yes", "no", "unknown", 2, -1, object()])
def test_jquants_optional_boolean_rejects_unknown_nonblank_values(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_optional_boolean(raw)


def _statement(disclosure_id: str, disclosed: str, profit: str) -> dict:
    return {
        "Code": "75090", "DiscNo": disclosure_id, "DiscDate": disclosed,
        "DiscTime": "15:45:00", "DocType": "1QFinancialStatements_Consolidated_JP",
        "CurPerSt": "2026-04-01", "CurPerEn": "2026-06-30",
        "CurFYEn": "2027-03-31", "FNP": profit, "RetroRst": "false",
    }


def test_corrected_statement_replaces_values_but_keeps_original_event_anchor() -> None:
    original = _statement("original", "2026-08-07", "1370000000")
    correction = _statement("correction", "2026-08-21", "1230000000")
    metadata = {
        "original": {"title": "2027年3月期 第1四半期決算短信", "disc_items": ["11304"]},
        "correction": {"title": "（訂正・数値データ訂正）決算短信の一部訂正", "disc_items": ["11323", "11741"]},
    }
    rows = canonicalize_statement_rows([original, correction], metadata)
    assert len(rows) == 1
    assert rows[0]["DiscNo"] == "original"
    assert rows[0]["DiscDate"] == "2026-08-07"
    assert rows[0]["FNP"] == "1230000000"
    assert rows[0]["_retrospective_restatement"] is False


def test_review_completion_copy_is_one_statement_event() -> None:
    original = _statement("original", "2026-08-07", "1370000000")
    review = _statement("review", "2026-08-20", "1370000000")
    metadata = {
        "review": {"title": "決算短信（期中レビューの完了）", "disc_items": ["11326"]}
    }
    rows = canonicalize_statement_rows([original, review], metadata)
    assert len(rows) == 1
    assert rows[0]["DiscNo"] == "original"


def test_forecast_revision_correction_targets_prior_original_disclosure() -> None:
    rows = [
        {"Code": "12340", "DiscNo": "a", "DiscDate": "2026-01-01", "DiscTime": "10:00",
         "DocType": "EarnForecastRevision", "CurFYEn": "2026-03-31"},
        {"Code": "12340", "DiscNo": "b", "DiscDate": "2026-01-02", "DiscTime": "10:00",
         "DocType": "EarnForecastRevision", "CurFYEn": "2026-03-31"},
    ]
    metadata = {"b": {"title": "（訂正）業績予想の修正に関するお知らせ", "disc_items": ["11323"]}}
    anchors, retracted = canonical_forecast_anchors(rows, metadata)
    assert anchors[("b", "2026-03-31", "F")] == "a"
    assert retracted == set()


def test_retraction_removes_prior_original_disclosure() -> None:
    rows = [
        {"Code": "12340", "DiscNo": "a", "DiscDate": "2026-01-01", "DiscTime": "10:00",
         "DocType": "EarnForecastRevision", "CurFYEn": "2026-03-31"},
        {"Code": "12340", "DiscNo": "b", "DiscDate": "2026-01-02", "DiscTime": "10:00",
         "DocType": "EarnForecastRevision", "CurFYEn": "2026-03-31"},
    ]
    metadata = {"b": {"title": "業績予想修正開示の撤回について", "disc_items": []}}
    anchors, retracted = canonical_forecast_anchors(rows, metadata)
    assert anchors == {}
    assert retracted == {"a"}
    assert metadata_role(metadata["b"]) == "original"
    assert is_forecast_retraction(rows[1], metadata["b"], "F") is True


def test_retraction_removes_latest_forecast_even_when_document_type_differs() -> None:
    rows = [
        {"Code": "12340", "DiscNo": "a", "DiscDate": "2026-01-01",
         "DocType": "3QFinancialStatements_Consolidated_JP",
         "CurFYEn": "2026-03-31", "FOP": "100"},
        {"Code": "12340", "DiscNo": "b", "DiscDate": "2026-01-02",
         "DocType": "EarnForecastRevision", "CurFYEn": "2026-03-31"},
    ]
    metadata = {"b": {"title": "通期業績予想の取り下げに関するお知らせ"}}
    _anchors, retracted = canonical_forecast_anchors(rows, metadata)
    assert retracted == {"a"}


def test_medium_term_plan_withdrawal_does_not_retract_populated_forecast() -> None:
    raw = {
        "FSales": "1000000000", "FOP": "100000000", "FOdP": "90000000",
        "FNP": "60000000", "FEPS": "25.0",
    }
    metadata = {
        "title": "業績予想の修正及び中期経営計画の取り下げに関するお知らせ"
    }
    assert is_forecast_retraction(raw, metadata, "F") is False


def test_revision_counts_are_distinct_per_disclosure_for_op_and_any_metric() -> None:
    events = []
    for disclosure_id in ("fy24", "fy25", "fy26"):
        for metric in ("sales", "operating_profit", "ordinary_profit", "net_income", "eps"):
            events.append({
                "ticker": "1234", "disclosure_id": disclosure_id,
                "disclosed_at": "2026-01-01", "metric": metric,
                "direction": "upward", "is_correction": False,
                "is_split_only_change": False,
            })
    # A future schema variant may emit OP rows for two target FYs in one disclosure.
    events.append({**events[1], "target_fiscal_year": "2027-03-31"})
    points = {("1234", "2026-03-31", "sales"): [{"disclosed_at": "2026-01-01"}]}
    counts = ScreenerSnapshotBuilder._revision_counts(events, points, "2026-08-21")
    assert counts["1234"] == (3, 3)


def test_nine_distinct_upward_disclosures_count_as_nine_events() -> None:
    events = [
        {
            "ticker": "1234", "disclosure_id": f"event-{index}",
            "disclosed_at": "2026-01-01", "metric": "operating_profit",
            "direction": "upward", "is_correction": False,
            "is_split_only_change": False,
        }
        for index in range(9)
    ]
    points = {("1234", "2026-03-31", "operating_profit"): [{"disclosed_at": "2026-01-01"}]}
    assert ScreenerSnapshotBuilder._revision_counts(events, points, "2026-08-21")["1234"] == (9, 9)
