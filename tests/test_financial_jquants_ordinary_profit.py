"""The summary's consolidated OdP is a separate profit metric, not OP."""
import json
import sqlite3

from lib.pipeline.canonical_sync import _FINANCIALS_METRIC_COLS
from tools.sync_financials import read_sqlite


def test_consolidated_ordinary_profit_survives_sync_without_op_proxy(tmp_path):
    db = tmp_path / "jquants.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE jquants_financials_normalized (
            local_code TEXT, disclosed_date TEXT,
            current_fiscal_year_end_date TEXT, type_of_current_period TEXT,
            type_of_document TEXT, net_sales INTEGER, gross_profit INTEGER,
            operating_profit INTEGER, profit_before_tax INTEGER,
            raw_json TEXT, fetched_at TEXT
        )"""
    )

    def put(ticker, quarter, sales, od_p, op, normalized_op, nc_op=None, doc=None):
        raw = {
            "Code": ticker + "0", "CurFYSt": "2025-04-01",
            "CurFYEn": "2026-03-31", "CurPerType": quarter,
            "Sales": str(sales), "OdP": "" if od_p is None else str(od_p),
            "OP": "" if op is None else str(op),
            "NCOP": "" if nc_op is None else str(nc_op),
        }
        conn.execute(
            """INSERT INTO jquants_financials_normalized VALUES
               (?, '2026-05-13', '2026-03-31', ?, ?, ?, NULL, ?, NULL, ?, '2026-05-13')""",
            (ticker + "0", quarter,
             doc or "FYFinancialStatements_Consolidated_JP",
             sales, normalized_op, json.dumps(raw)),
        )

    put("8316", "FY", 10_790_853_000_000, 2_303_350_000_000, None,
        2_303_350_000_000)  # historical proxy in normalized OP
    put("8750", "2Q", 5_119_595_000_000, 343_351_000_000, None, None,
        doc="2QFinancialStatements_Consolidated_JP")
    put("8591", "FY", 3_330_831_000_000, None, 456_248_000_000,
        456_248_000_000)
    put("9998", "FY", 100_000_000, 0, None, None)
    put("9999", "FY", 100_000_000, -2_000_000, None, None)
    put("8253", "FY", 343_857_000_000, None, None, 55_536_000_000,
        nc_op=55_536_000_000)
    conn.commit()
    conn.close()

    rows, _ = read_sqlite(str(db))
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["8316"]["ordinary_profit"] == 2_303_350
    assert by_ticker["8316"]["operating_profit"] is None
    assert by_ticker["8750"]["ordinary_profit"] == 343_351
    assert by_ticker["8591"]["operating_profit"] == 456_248
    assert by_ticker["9998"]["ordinary_profit"] == 0
    assert by_ticker["9999"]["ordinary_profit"] == -2
    assert by_ticker["8253"]["operating_profit"] is None
    assert _FINANCIALS_METRIC_COLS["ordinary_profit"] == "ordinary_profit"
