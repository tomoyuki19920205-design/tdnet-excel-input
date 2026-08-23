import sqlite3

from tools.sync_per_share_data import read_sqlite


def test_basis_adjusted_only_filters_default_rows(tmp_path):
    db = tmp_path / "per_share.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE per_share_data (
            ticker TEXT, period TEXT, quarter TEXT, disclosed_date TEXT,
            eps REAL, diluted_eps REAL, bps REAL,
            dividend_q1 REAL, dividend_q2 REAL, dividend_q3 REAL,
            dividend_fy_end REAL, dividend_annual REAL, payout_ratio REAL,
            forecast_eps REAL, forecast_eps_basis_factor REAL,
            initial_forecast_eps REAL, forecast_dividend_annual REAL,
            forecast_payout_ratio REAL, shares_outstanding INTEGER,
            treasury_stock INTEGER, avg_shares INTEGER, total_assets INTEGER,
            equity INTEGER, equity_ratio REAL, source TEXT, updated_at TEXT
        )
        """
    )
    for ticker, factor in (("1111", 1), ("2222", 2)):
        connection.execute(
            "INSERT INTO per_share_data "
            "(ticker,period,quarter,forecast_eps_basis_factor,source,updated_at) "
            "VALUES (?, '2027-03-31', '1Q', ?, 'jquants', 'now')",
            (ticker, factor),
        )
    connection.commit()
    connection.close()

    rows = read_sqlite(str(db), basis_adjusted_only=True)

    assert [row["ticker"] for row in rows] == ["2222"]
