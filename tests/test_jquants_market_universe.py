import sqlite3

from datetime import datetime

from tools.fetch_jquants_prices import (
    _ensure_table, is_common_stock, is_jquants_price_eligible,
    is_ordinary_stock, market_data_retention_start, normalize_jquants_code,
    store_universe_snapshot, upsert_quotes,
)


def _master(**overrides):
    row = {
        "Code": "72030", "CoName": "トヨタ自動車", "CoNameEn": "TOYOTA MOTOR CORPORATION",
        "ProdCat": "011", "Mkt": "0111",
    }
    row.update(overrides)
    return row


def test_store_universe_snapshot_persists_screening_classifications():
    conn = sqlite3.connect(":memory:")
    _ensure_table(conn)
    item = _master(
        S17="8", S17Nm="情報通信・サービスその他",
        S33="5250", S33Nm="情報・通信業", MktNm="プライム",
    )

    store_universe_snapshot(conn, "2026-08-21", [item])

    row = conn.execute(
        "SELECT sector17_code,sector17_name,sector33_code,sector33_name,market_name "
        "FROM market_data_universe"
    ).fetchone()
    assert row == ("8", "情報通信・サービスその他", "5250", "情報・通信業", "プライム")


def test_tse_common_stock_is_in_scope():
    assert is_ordinary_stock(_master())
    assert is_jquants_price_eligible(_master())


def test_historical_tse_first_section_common_stock_is_in_scope():
    assert is_ordinary_stock(_master(Mkt="0101"))
    assert is_jquants_price_eligible(_master(Mkt="0101"))


def test_regional_ordinary_stock_is_not_jquants_price_eligible():
    regional = _master(Code="66230", CoName="愛知電機", Mkt="0501")
    assert is_ordinary_stock(regional)
    assert is_common_stock(regional)  # compatibility alias keeps security semantics
    assert not is_jquants_price_eligible(regional)


def test_etf_is_excluded_by_master_product_category_not_ticker_shape():
    etf = _master(Code="13060", ProdCat="014", Mkt="0109")
    assert not is_ordinary_stock(etf)
    assert not is_jquants_price_eligible(etf)


def test_reit_is_excluded_by_master_product_category_not_ticker_shape():
    reit = _master(Code="89510", ProdCat="013", Mkt="0109")
    assert not is_ordinary_stock(reit)
    assert not is_jquants_price_eligible(reit)


def test_tokyo_pro_market_equity_is_ordinary_but_not_price_eligible():
    tokyo_pro = _master(Mkt="0105")
    assert is_ordinary_stock(tokyo_pro)
    assert not is_jquants_price_eligible(tokyo_pro)


def test_preferred_share_is_explicitly_excluded_from_official_master_name():
    preferred = _master(Code="94345", CoName="ソフトバンク（優先株式）")
    assert not is_ordinary_stock(preferred)
    assert not is_jquants_price_eligible(preferred)


def test_universe_snapshot_persists_both_decisions_and_queries_only_eligible_codes():
    conn = sqlite3.connect(":memory:")
    _ensure_table(conn)
    tse = _master()
    regional = _master(Code="66230", CoName="愛知電機", Mkt="0501")

    eligible = store_universe_snapshot(conn, "2026-08-14", [tse, regional])

    assert eligible == {"72030"}
    assert conn.execute(
        "SELECT ticker,is_ordinary_stock,is_jquants_price_eligible "
        "FROM market_data_universe ORDER BY ticker"
    ).fetchall() == [("6623", 1, 0), ("7203", 1, 1)]


def test_v2_numeric_and_alpha_security_codes_do_not_collide():
    assert normalize_jquants_code("13800") == "1380"
    assert normalize_jquants_code("138A0") == "138A"


def test_one_year_retention_boundary_is_inclusive_from_august_third():
    assert market_data_retention_start(datetime(2026, 8, 2)) == "2025-08-03"


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
    before = {"Code": "72030", "Date": "2026-07-29", "O": 1, "H": 2,
              "L": 1, "C": 2, "Vo": 100, "Va": 200, "AdjFactor": 1,
              "AdjC": 2, "AdjVo": 100}
    after = {**before, "AdjFactor": 0.5, "AdjC": 1, "AdjVo": 200}
    assert upsert_quotes(conn, [before]) == 1
    assert upsert_quotes(conn, [after]) == 1
    assert conn.execute(
        "SELECT adj_factor, adj_close, adj_volume FROM market_data"
    ).fetchone() == (0.5, 1.0, 200)
