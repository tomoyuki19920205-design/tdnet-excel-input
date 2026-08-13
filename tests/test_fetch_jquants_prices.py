import sqlite3

import pytest

from tools.fetch_jquants_prices import recalculate_market_caps, update_market_caps


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE market_data (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL,
            adj_factor REAL,
            market_cap REAL,
            UNIQUE(ticker, date)
        );
        CREATE TABLE per_share_data (
            ticker TEXT NOT NULL,
            period TEXT NOT NULL,
            quarter TEXT NOT NULL,
            disclosed_date TEXT,
            shares_outstanding INTEGER,
            treasury_stock INTEGER,
            UNIQUE(ticker, period, quarter)
        );
        """
    )
    return conn


def _market(
    conn: sqlite3.Connection,
    date: str,
    *,
    close: float = 10.0,
    factor: float = 1.0,
    old_cap: float | None = None,
    ticker: str = "2359",
) -> None:
    conn.execute(
        "INSERT INTO market_data VALUES (?, ?, ?, ?, ?)",
        (ticker, date, close, factor, old_cap),
    )


def _shares(
    conn: sqlite3.Connection,
    disclosed: str,
    shares: int | None,
    *,
    treasury: int = 0,
    period: str = "2026-03-31",
    quarter: str = "FY",
    ticker: str = "2359",
) -> None:
    conn.execute(
        "INSERT INTO per_share_data VALUES (?, ?, ?, ?, ?, ?)",
        (ticker, period, quarter, disclosed, shares, treasury),
    )


def _cap(conn: sqlite3.Connection, date: str, ticker: str = "2359") -> float | None:
    row = conn.execute(
        "SELECT market_cap FROM market_data WHERE ticker=? AND date=?",
        (ticker, date),
    ).fetchone()
    assert row is not None
    return row[0]


def test_ordinary_market_cap_uses_total_issued_shares() -> None:
    conn = _connection()
    _market(conn, "2026-07-13", close=12.5)
    _shares(conn, "2026-05-10", 1_234, treasury=234)

    update_market_caps(conn)

    assert _cap(conn, "2026-07-13") == 12.5 * 1_234


def test_large_treasury_position_is_not_subtracted() -> None:
    conn = _connection()
    _market(conn, "2026-07-13")
    _shares(conn, "2026-05-10", 1_000, treasury=600)

    update_market_caps(conn)

    assert _cap(conn, "2026-07-13") == 10_000


@pytest.mark.parametrize(
    ("factor", "share_multiple"),
    [(0.5, 2), (1 / 3, 3), (0.2, 5), (0.1, 10), (10.0, 0.1)],
)
def test_corporate_action_direction_is_fixed_from_jquants_examples(
    factor: float,
    share_multiple: float,
) -> None:
    conn = _connection()
    _shares(conn, "2026-05-10", 1_000)
    _market(conn, "2026-06-26")
    _market(conn, "2026-06-29", factor=factor)

    update_market_caps(conn)

    assert _cap(conn, "2026-06-29") == pytest.approx(10 * 1_000 * share_multiple)


def test_multiple_actions_are_cumulative() -> None:
    conn = _connection()
    _shares(conn, "2026-05-10", 1_000)
    _market(conn, "2026-06-01", factor=0.5)
    _market(conn, "2026-07-01", factor=1 / 3)

    update_market_caps(conn)

    assert _cap(conn, "2026-07-01") == pytest.approx(10 * 6_000)


def test_split_effective_date_uses_post_split_price_basis() -> None:
    conn = _connection()
    _shares(conn, "2026-05-10", 1_000)
    _market(conn, "2026-06-28", close=20)
    _market(conn, "2026-06-29", close=10, factor=0.5)

    update_market_caps(conn)

    assert _cap(conn, "2026-06-28") == 20_000
    assert _cap(conn, "2026-06-29") == 20_000


def test_same_day_new_share_disclosure_prevents_double_application() -> None:
    conn = _connection()
    _shares(conn, "2026-05-10", 1_000, period="2025-03-31")
    _shares(conn, "2026-06-29", 2_000, period="2026-03-31")
    _market(conn, "2026-06-29", factor=0.5)

    update_market_caps(conn)

    assert _cap(conn, "2026-06-29") == 20_000


def test_disclosure_after_split_resets_basis_without_double_application() -> None:
    conn = _connection()
    _shares(conn, "2026-05-10", 1_000, period="2025-03-31")
    _shares(conn, "2026-07-10", 2_000, period="2026-03-31")
    _market(conn, "2026-06-29", factor=0.5)
    _market(conn, "2026-07-09")
    _market(conn, "2026-07-10")

    update_market_caps(conn)

    assert _cap(conn, "2026-07-09") == 20_000
    assert _cap(conn, "2026-07-10") == 20_000


def test_future_disclosure_is_not_used_for_past_price() -> None:
    conn = _connection()
    _shares(conn, "2026-05-10", 1_000, period="2025-03-31")
    _shares(conn, "2026-08-01", 3_000, period="2027-03-31")
    _market(conn, "2026-07-31")
    _market(conn, "2026-08-01")

    update_market_caps(conn)

    assert _cap(conn, "2026-07-31") == 10_000
    assert _cap(conn, "2026-08-01") == 30_000


def test_invalid_or_missing_shares_leave_market_cap_null() -> None:
    conn = _connection()
    _shares(conn, "2026-05-10", 0)
    _market(conn, "2026-07-13", old_cap=123_456)

    update_market_caps(conn)

    assert _cap(conn, "2026-07-13") is None


def test_dry_run_reports_changes_without_writing() -> None:
    conn = _connection()
    _shares(conn, "2026-05-10", 1_000)
    _market(conn, "2026-07-13", old_cap=1)

    stats = recalculate_market_caps(conn, apply=False)

    assert stats.scanned_rows == 1
    assert stats.changed_rows == 1
    assert stats.changed_tickers == 1
    assert stats.errors == 0
    assert _cap(conn, "2026-07-13") == 1


@pytest.mark.parametrize(
    ("ticker", "close", "disclosed", "shares", "factor", "expected"),
    [
        ("2163", 981.0, "2026-06-10", 10_627_920, 0.5, 20_851_979_040),
        ("3193", 1_481.0, "2026-06-05", 11_622_300, 0.5, 34_425_252_600),
        ("4380", 645.0, "2026-06-15", 4_890_800, 0.5, 6_309_132_000),
        ("7678", 3_040.0, "2026-06-10", 5_385_020, 0.5, 32_740_921_600),
    ],
)
def test_known_two_for_one_split_regressions(
    ticker: str,
    close: float,
    disclosed: str,
    shares: int,
    factor: float,
    expected: float,
) -> None:
    conn = _connection()
    _shares(conn, disclosed, shares, ticker=ticker)
    _market(conn, "2026-07-30", factor=factor, ticker=ticker)
    _market(conn, "2026-08-12", close=close, ticker=ticker)

    update_market_caps(conn)

    assert _cap(conn, "2026-08-12", ticker) == expected


@pytest.mark.parametrize(
    ("ticker", "close", "shares", "treasury", "expected"),
    [
        ("1301", 4_605.0, 12_078_283, 198_406, 55_620_493_215),
        ("1332", 1_258.0, 305_265_402, 1_967_113, 384_023_875_716),
        ("2814", 3_170.0, 7_377_460, 3_788_429, 23_386_548_200),
        ("4762", 1_916.0, 8_261_600, 4_080_800, 15_829_225_600),
        ("1518", 2_313.0, 65_322_000, 28_294_040, 151_089_786_000),
    ],
)
def test_known_issued_share_definition_regressions(
    ticker: str,
    close: float,
    shares: int,
    treasury: int,
    expected: float,
) -> None:
    conn = _connection()
    _shares(conn, "2026-08-01", shares, treasury=treasury, ticker=ticker)
    _market(conn, "2026-08-12", close=close, ticker=ticker)

    update_market_caps(conn)

    assert _cap(conn, "2026-08-12", ticker) == expected
