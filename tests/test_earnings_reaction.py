from __future__ import annotations

import sqlite3
from typing import Any

import pytest

import tools.fetch_jquants_prices as price_fetcher
from tools.earnings_reaction import build_reaction_rows, save_sqlite


def _population(code: str = "7203") -> list[dict[str, Any]]:
    return [{
        "source_event_id": "event-1", "code": code, "company_name": "テスト",
        "fiscal_quarter": "1Q", "earnings_date": "2026-07-15",
    }]


def _price(code: str, day: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "Code": code, "Date": day, "O": 100.0, "C": 100.0,
        "AdjO": 100.0, "AdjC": 100.0, "Vo": 1000, "Va": 100000,
        "UL": "0", "LL": "0",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("display_code", "jquants_code"),
    [("7203", "72030"), ("205A", "205A0"), ("367A", "367A0")],
)
def test_numeric_alpha_and_trailing_zero_code_join(display_code: str, jquants_code: str) -> None:
    rows = build_reaction_rows(
        _population(display_code),
        [_price(jquants_code, "2026-07-15")],
        [_price(jquants_code, "2026-07-16")],
    )
    assert rows[0]["data_status"] == "ok"
    assert rows[0]["jquants_code"] == jquants_code


def test_numeric_jquants_code_does_not_collide_with_alpha_ticker() -> None:
    population = _population("2050") + [
        {**_population("205A")[0], "source_event_id": "event-2"}
    ]
    rows = build_reaction_rows(
        population,
        [_price("20500", "2026-07-15"), _price("205A0", "2026-07-15")],
        [_price("20500", "2026-07-16"), _price("205A0", "2026-07-16")],
    )
    assert {row["code"]: row["jquants_code"] for row in rows} == {
        "2050": "20500", "205A": "205A0"
    }
    assert all(row["data_status"] == "ok" for row in rows)


def test_missing_price_is_recorded_without_zero_fill() -> None:
    rows = build_reaction_rows(
        _population(),
        [_price("72030", "2026-07-15", C=None, AdjC=None)],
        [_price("72030", "2026-07-16")],
    )
    assert rows[0]["data_status"] == "missing"
    assert "null_close_2026-07-15_raw" in rows[0]["missing_reason"]
    assert rows[0]["open_gap_return_pct"] is None


@pytest.mark.parametrize(
    ("prev_adj_close", "next_adj_open", "reason"),
    [(0.0, 100.0, "zero_close_2026-07-15_adjusted"),
     (100.0, 0.0, "zero_open_2026-07-16_adjusted")],
)
def test_zero_division_is_prevented(
    prev_adj_close: float, next_adj_open: float, reason: str
) -> None:
    rows = build_reaction_rows(
        _population(),
        [_price("72030", "2026-07-15", AdjC=prev_adj_close)],
        [_price("72030", "2026-07-16", AdjO=next_adj_open)],
    )
    assert reason in rows[0]["missing_reason"]
    assert rows[0]["open_gap_return_pct"] is None


def test_adjusted_returns_are_used() -> None:
    rows = build_reaction_rows(
        _population(),
        [_price("72030", "2026-07-15", C=50.0, AdjC=100.0)],
        [_price("72030", "2026-07-16", O=60.0, C=66.0, AdjO=110.0, AdjC=121.0)],
    )
    assert rows[0]["open_gap_return_pct"] == pytest.approx(0.10)
    assert rows[0]["next_close_return_pct"] == pytest.approx(0.21)
    assert rows[0]["intraday_return_pct"] == pytest.approx(0.10)


def test_duplicate_codes_are_preserved_and_marked() -> None:
    population = _population() + [{**_population()[0], "source_event_id": "event-2"}]
    rows = build_reaction_rows(
        population,
        [_price("72030", "2026-07-15")],
        [_price("72030", "2026-07-16")],
    )
    assert len(rows) == len(population)
    assert all(row["data_status"] == "duplicate_code" for row in rows)


def test_input_and_output_counts_match_with_missing_rows() -> None:
    population = _population("7203") + [{**_population("205A")[0], "source_event_id": "event-2"}]
    rows = build_reaction_rows(
        population,
        [_price("72030", "2026-07-15")],
        [_price("72030", "2026-07-16")],
    )
    assert len(rows) == 2
    assert sum(row["data_status"] == "ok" for row in rows) == 1
    assert sum(row["data_status"] == "missing" for row in rows) == 1


def test_pagination_key_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def json(self) -> dict[str, Any]:
            return self.payload

    calls: list[dict[str, Any]] = []
    responses = [
        Response({"data": [{"Code": "72030"}], "pagination_key": "next-page"}),
        Response({"data": [{"Code": "205A0"}]}),
    ]

    def fake_get(endpoint: str, params: dict[str, Any], auth_headers: dict[str, str]) -> Response:
        calls.append(dict(params))
        return responses.pop(0)

    monkeypatch.setattr(price_fetcher, "_api_get", fake_get)
    monkeypatch.setattr(price_fetcher, "SLEEP_BETWEEN_PAGES", 0)
    rows = price_fetcher.fetch_daily_quotes_by_date("2026-07-15", {"x-api-key": "secret"})
    assert [row["Code"] for row in rows] == ["72030", "205A0"]
    assert calls == [
        {"date": "2026-07-15"},
        {"date": "2026-07-15", "pagination_key": "next-page"},
    ]


def test_save_sqlite_rerun_updates_without_duplicates(tmp_path) -> None:
    rows = build_reaction_rows(
        _population(),
        [_price("72030", "2026-07-15")],
        [_price("72030", "2026-07-16")],
    )
    db_path = tmp_path / "jquants.db"

    save_sqlite(rows, db_path)
    save_sqlite([{**rows[0], "company_name": "更新後"}], db_path)

    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM earnings_reactions"
        ).fetchone()[0]
        company_name = connection.execute(
            "SELECT company_name FROM earnings_reactions"
        ).fetchone()[0]

    assert count == 1
    assert company_name == "更新後"
