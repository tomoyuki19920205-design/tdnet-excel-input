"""Main-branch acquisition contract for distinct actual profit metrics."""
import json
import sqlite3

from tools import fetch_jquants_financials as fetcher


def _summary(**changes):
    row = {
        "Code": "57130", "DiscDate": "2026-05-11", "DiscNo": "filing-1",
        "CurFYEn": "2026-03-31", "CurPerType": "FY",
        "DocType": "FYFinancialStatements_Consolidated_IFRS",
        "Sales": "1741586000000", "OP": "", "NCOP": "133617000000",
    }
    row.update(changes)
    return row


def _database():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE jquants_financials_normalized (
        local_code TEXT NOT NULL, disclosed_date TEXT NOT NULL,
        current_fiscal_year_end_date TEXT NOT NULL,
        type_of_current_period TEXT NOT NULL, type_of_document TEXT NOT NULL,
        net_sales INTEGER, gross_profit INTEGER, operating_profit INTEGER,
        raw_json TEXT, fetched_at TEXT NOT NULL)""")
    fetcher._ensure_table(conn)
    return conn


def test_legacy_schema_migration_and_distinct_profit_storage():
    conn = _database()
    assert "profit_before_tax" in {
        row[1] for row in conn.execute("PRAGMA table_info(jquants_financials_normalized)")
    }
    row = fetcher._row_to_db(_summary(_profit_before_tax="255680000000"))
    assert row["operating_profit"] is None
    assert row["profit_before_tax"] == 255_680_000_000
    assert fetcher.upsert_rows(conn, [row]) == 1
    assert conn.execute(
        "SELECT operating_profit, profit_before_tax FROM jquants_financials_normalized"
    ).fetchone() == (None, 255_680_000_000)
    assert fetcher._row_to_db(_summary(OP="0"))["operating_profit"] == 0
    assert fetcher._row_to_db(_summary(OP="-1000000"))["operating_profit"] == -1_000_000


def test_exact_actual_detail_saves_pbt_and_provenance(monkeypatch):
    conn = _database()
    summary = _summary()
    fetcher.upsert_rows(conn, [fetcher._row_to_db(summary)])
    detail = {
        "Code": summary["Code"], "DiscNo": summary["DiscNo"],
        "DiscDate": summary["DiscDate"], "DocType": summary["DocType"],
        "FS": {"Profit (loss) before tax from continuing operations (IFRS)": "255680000000"},
    }
    monkeypatch.setattr(fetcher, "_fetch_details_for_date", lambda *_args: ([detail], 1))
    result = fetcher._supplement_profit_before_tax_from_details(conn, None, {}, "57130")
    assert result["supplemented"] == 1
    op, pbt, raw = conn.execute(
        "SELECT operating_profit, profit_before_tax, raw_json FROM jquants_financials_normalized"
    ).fetchone()
    assert op is None
    assert pbt == 255_680_000_000
    assert json.loads(raw)["_profit_before_tax_source"] == (
        "Profit (loss) before tax from continuing operations (IFRS)"
    )
    # A later summary refresh cannot erase the verified details value or its source.
    fetcher.upsert_rows(conn, [fetcher._row_to_db(summary)])
    refreshed = conn.execute(
        "SELECT profit_before_tax, raw_json FROM jquants_financials_normalized"
    ).fetchone()
    assert refreshed[0] == pbt
    assert json.loads(refreshed[1])["_profit_before_tax_source"] == (
        "Profit (loss) before tax from continuing operations (IFRS)"
    )


def test_detail_from_another_disclosure_does_not_fill_pbt(monkeypatch):
    conn = _database()
    summary = _summary()
    fetcher.upsert_rows(conn, [fetcher._row_to_db(summary)])
    other = {
        "Code": summary["Code"], "DiscNo": "older-filing",
        "DiscDate": summary["DiscDate"], "DocType": summary["DocType"],
        "FS": {"Profit before tax (IFRS)": "255680000000"},
    }
    monkeypatch.setattr(fetcher, "_fetch_details_for_date", lambda *_args: ([other], 1))
    result = fetcher._supplement_profit_before_tax_from_details(conn, None, {}, "57130")
    assert result["supplemented"] == 0
    assert conn.execute(
        "SELECT profit_before_tax FROM jquants_financials_normalized"
    ).fetchone()[0] is None


def test_direct_bank_operating_income_keeps_its_own_metric(monkeypatch):
    conn = _database()
    summary = _summary(
        Code="73500", DocType="FYFinancialStatements_Consolidated_JP",
        NCOP="", OP="",
    )
    fetcher.upsert_rows(conn, [fetcher._row_to_db(summary)])
    detail = {
        "Code": summary["Code"], "DiscNo": summary["DiscNo"],
        "DiscDate": summary["DiscDate"], "DocType": summary["DocType"],
        "FS": {"Operating income BNK": "-1000000", "Ordinary profit BNK": "2000000"},
    }
    monkeypatch.setattr(fetcher, "_fetch_details_for_date", lambda *_args: ([detail], 1))
    stats = fetcher._supplement_profit_before_tax_from_details(conn, None, {}, "73500")
    assert stats["bank_op_supplemented"] == 1
    op, pbt, raw = conn.execute(
        "SELECT operating_profit, profit_before_tax, raw_json FROM jquants_financials_normalized"
    ).fetchone()
    assert (op, pbt) == (-1_000_000, None)
    assert json.loads(raw)["_operating_profit_proxy"]["reason"].startswith("OperatingIncomeBNK:")
    fetcher.upsert_rows(conn, [fetcher._row_to_db(summary)])
    raw = conn.execute("SELECT raw_json FROM jquants_financials_normalized").fetchone()[0]
    assert json.loads(raw)["_operating_profit_proxy"]["reason"].startswith("OperatingIncomeBNK:")
