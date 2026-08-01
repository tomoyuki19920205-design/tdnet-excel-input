import sqlite3

from tools.fetch_jquants_prices import is_common_stock, normalize_jquants_code, upsert_quotes


def _master(**overrides):
    row = {
        "Code": "72030", "CoName": "トヨタ自動車", "CoNameEn": "TOYOTA MOTOR CORPORATION",
        "ProdCat": "011", "Mkt": "0111",
    }
    row.update(overrides)
    return row


def test_tse_common_stock_is_in_scope():
    assert is_common_stock(_master())


def test_historical_tse_first_section_common_stock_is_in_scope():
    assert is_common_stock(_master(Mkt="0101"))


def test_etf_is_excluded_by_master_product_category_not_ticker_shape():
    assert not is_common_stock(_master(Code="13060", ProdCat="014", Mkt="0109"))


def test_reit_is_excluded_by_master_product_category_not_ticker_shape():
    assert not is_common_stock(_master(Code="89510", ProdCat="013", Mkt="0109"))


def test_tokyo_pro_market_equity_is_out_of_scope():
    assert not is_common_stock(_master(Mkt="0105"))


def test_preferred_share_is_explicitly_excluded_from_official_master_name():
    assert not is_common_stock(_master(Code="94345", CoName="ソフトバンク（優先株式）"))


def test_v2_numeric_and_alpha_security_codes_do_not_collide():
    assert normalize_jquants_code("13800") == "1380"
    assert normalize_jquants_code("138A0") == "138A"


def test_identical_quote_upsert_does_not_update_existing_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE market_data (
          ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
          volume INTEGER, turnover REAL, adj_factor REAL, adj_close REAL,
          adj_volume INTEGER, market_cap REAL, source TEXT, fetched_at TEXT,
          UNIQUE(ticker, date)
        );
    """)
    quote = {"Code": "72030", "Date": "2026-07-31", "O": 1, "H": 2,
             "L": 1, "C": 2, "Vo": 100, "Va": 200, "AdjFactor": 1,
             "AdjC": 2, "AdjVo": 100}
    assert upsert_quotes(conn, [quote]) == 1
    conn.execute("UPDATE market_data SET fetched_at = 'sentinel'")
    assert upsert_quotes(conn, [quote]) == 0
    assert conn.execute("SELECT fetched_at FROM market_data").fetchone()[0] == "sentinel"


def test_refetched_historical_adjusted_series_replaces_changed_value() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE market_data (
          ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
          volume INTEGER, turnover REAL, adj_factor REAL, adj_close REAL,
          adj_volume INTEGER, market_cap REAL, source TEXT, fetched_at TEXT,
          UNIQUE(ticker, date)
        );
    """)
    before = {"Code": "72030", "Date": "2021-09-29", "O": 1, "H": 2,
              "L": 1, "C": 2, "Vo": 100, "Va": 200, "AdjFactor": 1,
              "AdjC": 2, "AdjVo": 100}
    after = {**before, "AdjFactor": 0.5, "AdjC": 1, "AdjVo": 200}
    assert upsert_quotes(conn, [before]) == 1
    assert upsert_quotes(conn, [after]) == 1
    assert conn.execute(
        "SELECT adj_factor, adj_close, adj_volume FROM market_data"
    ).fetchone() == (0.5, 1.0, 200)
