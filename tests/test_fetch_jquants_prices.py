import sqlite3

from tools.fetch_jquants_prices import update_market_caps


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE market_data (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL,
            market_cap REAL,
            UNIQUE(ticker, date)
        );
        CREATE TABLE per_share_data (
            ticker TEXT NOT NULL,
            period TEXT NOT NULL,
            quarter TEXT NOT NULL,
            shares_outstanding INTEGER,
            treasury_stock INTEGER,
            UNIQUE(ticker, period, quarter)
        );
        INSERT INTO market_data (ticker, date, close, market_cap)
        VALUES ('2359', '2026-07-13', 10.0, NULL);
        """
    )
    return conn


def _insert_share_row(
    conn: sqlite3.Connection,
    period: str,
    shares_outstanding: int | None,
    treasury_stock: int | None = 0,
    quarter: str = "FY",
) -> None:
    conn.execute(
        """
        INSERT INTO per_share_data (
            ticker, period, quarter, shares_outstanding, treasury_stock
        ) VALUES ('2359', ?, ?, ?, ?)
        """,
        (period, quarter, shares_outstanding, treasury_stock),
    )


def _market_cap(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        "SELECT market_cap FROM market_data WHERE ticker = '2359'"
    ).fetchone()
    assert row is not None
    return row[0]


def test_market_cap_falls_back_when_latest_shares_are_null() -> None:
    conn = _connection()
    _insert_share_row(conn, "2027-03-31", None)
    _insert_share_row(conn, "2026-03-31", 1_000, 100)

    update_market_caps(conn)

    assert _market_cap(conn) == 10.0 * (1_000 - 100)


def test_market_cap_falls_back_when_latest_shares_are_zero() -> None:
    conn = _connection()
    _insert_share_row(conn, "2027-03-31", 0)
    _insert_share_row(conn, "2026-03-31", 1_000, 100)

    update_market_caps(conn)

    assert _market_cap(conn) == 10.0 * (1_000 - 100)


def test_market_cap_stays_null_without_positive_shares() -> None:
    conn = _connection()
    _insert_share_row(conn, "2027-03-31", None)
    _insert_share_row(conn, "2026-03-31", 0)
    _insert_share_row(conn, "2025-03-31", -100)

    update_market_caps(conn)

    assert _market_cap(conn) is None


def test_existing_zero_market_cap_is_cleared_when_no_valid_share_count() -> None:
    conn = _connection()
    conn.execute("UPDATE market_data SET market_cap = 0")
    _insert_share_row(conn, "2027-03-31", None)
    _insert_share_row(conn, "2026-03-31", 0)
    _insert_share_row(conn, "2025-03-31", -100)

    update_market_caps(conn)

    assert _market_cap(conn) is None


def test_existing_positive_market_cap_is_cleared_without_valid_shares() -> None:
    conn = _connection()
    conn.execute("UPDATE market_data SET market_cap = 123456")
    _insert_share_row(conn, "2027-03-31", None)
    _insert_share_row(conn, "2026-03-31", 0)

    update_market_caps(conn)

    assert _market_cap(conn) is None


def test_market_cap_uses_latest_positive_shares() -> None:
    conn = _connection()
    _insert_share_row(conn, "2027-03-31", 2_000, 200)
    _insert_share_row(conn, "2026-03-31", 1_000, 100)

    update_market_caps(conn)

    assert _market_cap(conn) == 10.0 * (2_000 - 200)


def test_market_cap_keeps_existing_formula_and_unit() -> None:
    conn = _connection()
    conn.execute("UPDATE market_data SET close = 12.5")
    _insert_share_row(conn, "2026-03-31", 1_234, 34)

    update_market_caps(conn)

    assert _market_cap(conn) == 12.5 * (1_234 - 34)
