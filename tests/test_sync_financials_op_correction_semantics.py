"""Regression tests for latest-effective operating-profit semantics."""

from __future__ import annotations

import json
import sqlite3

from tools.fetch_jquants_financials import upsert_rows
from tools.sync_financials import _QUERY_BASE


def _create_db(tmp_path, rows: list[dict]) -> str:
    db_path = tmp_path / "jquants.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE jquants_financials_normalized (
            local_code TEXT NOT NULL,
            disclosed_date TEXT NOT NULL,
            current_fiscal_year_end_date TEXT NOT NULL,
            type_of_current_period TEXT NOT NULL,
            type_of_document TEXT NOT NULL,
            net_sales INTEGER,
            gross_profit INTEGER,
            operating_profit INTEGER,
            raw_json TEXT,
            fetched_at TEXT,
            UNIQUE (
                local_code,
                disclosed_date,
                type_of_document,
                type_of_current_period,
                current_fiscal_year_end_date
            )
        )
        """
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO jquants_financials_normalized VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("local_code", "76160"),
                row["disclosed_date"],
                row.get("fiscal_year", "2026-03-31"),
                row.get("quarter", "FY"),
                row["type_of_document"],
                row.get("net_sales", 300_090_000_000),
                row.get("gross_profit"),
                row.get("operating_profit"),
                json.dumps(row.get("raw_json", {})),
                row.get("fetched_at", "2026-05-18T13:01:00+09:00"),
            ),
        )
    conn.commit()
    conn.close()
    return str(db_path)


def _read_actual(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(_QUERY_BASE.format(where_clause="")).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def test_7616_latest_financial_statement_blank_op_tombstones_original(tmp_path):
    db_path = _create_db(
        tmp_path,
        [
            {
                "disclosed_date": "2026-05-08",
                "type_of_document": "FYFinancialStatements_Consolidated_IFRS",
                "operating_profit": 9_407_000_000,
            },
            {
                "disclosed_date": "2026-05-18",
                "type_of_document": "FYFinancialStatements_Consolidated_IFRS",
                "operating_profit": None,
            },
        ],
    )

    assert _read_actual(db_path)["operating_profit"] is None


def test_supplemental_forecast_revision_does_not_tombstone_actual_op(tmp_path):
    db_path = _create_db(
        tmp_path,
        [
            {
                "disclosed_date": "2026-05-08",
                "type_of_document": "FYFinancialStatements_Consolidated_IFRS",
                "operating_profit": 9_407_000_000,
            },
            {
                "disclosed_date": "2026-05-20",
                "type_of_document": "EarnForecastRevision",
                "operating_profit": None,
            },
        ],
    )

    assert _read_actual(db_path)["operating_profit"] == 9_407_000_000


def test_latest_financial_statement_nonnull_correction_wins(tmp_path):
    db_path = _create_db(
        tmp_path,
        [
            {
                "disclosed_date": "2026-05-08",
                "type_of_document": "FYFinancialStatements_Consolidated_IFRS",
                "operating_profit": 9_407_000_000,
            },
            {
                "disclosed_date": "2026-05-18",
                "type_of_document": "FYFinancialStatements_Consolidated_IFRS",
                "operating_profit": 9_500_000_000,
            },
        ],
    )

    assert _read_actual(db_path)["operating_profit"] == 9_500_000_000


def test_upsert_can_clear_op_without_updating_sibling_document_type(tmp_path):
    db_path = _create_db(
        tmp_path,
        [
            {
                "disclosed_date": "2026-05-18",
                "type_of_document": "FYFinancialStatements_Consolidated_IFRS",
                "operating_profit": 9_407_000_000,
            },
            {
                "disclosed_date": "2026-05-18",
                "type_of_document": "EarnForecastRevision",
                "operating_profit": 123_000_000,
            },
        ],
    )
    conn = sqlite3.connect(db_path)
    row = {
        "local_code": "76160",
        "disclosed_date": "2026-05-18",
        "current_fiscal_year_end_date": "2026-03-31",
        "type_of_current_period": "FY",
        "type_of_document": "FYFinancialStatements_Consolidated_IFRS",
        "net_sales": None,
        "gross_profit": None,
        "operating_profit": None,
        "raw_json": "{}",
        "fetched_at": "2026-05-18T14:00:00+09:00",
    }
    assert upsert_rows(conn, [row]) == 1
    values = dict(
        conn.execute(
            "SELECT type_of_document, operating_profit "
            "FROM jquants_financials_normalized"
        ).fetchall()
    )
    conn.close()

    assert values["FYFinancialStatements_Consolidated_IFRS"] is None
    assert values["EarnForecastRevision"] == 123_000_000
