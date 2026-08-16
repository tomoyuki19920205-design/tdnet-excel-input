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


def test_sparse_financial_statement_blank_op_preserves_original(tmp_path):
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

    assert _read_actual(db_path)["operating_profit"] == 9_407_000_000


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


def test_upsert_sparse_correction_preserves_op_without_touching_sibling_document_type(tmp_path):
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

    assert values["FYFinancialStatements_Consolidated_IFRS"] == 9_407_000_000
    assert values["EarnForecastRevision"] == 123_000_000


def test_5125_sparse_correction_preserves_gross_profit(tmp_path):
    db_path = _create_db(
        tmp_path,
        [
            {
                "local_code": "51250",
                "disclosed_date": "2026-06-30T15:30:00",
                "fiscal_year": "2026-06-30",
                "quarter": "3Q",
                "type_of_document": "3QFinancialStatements_Consolidated_JP",
                "gross_profit": 1_486_077_000,
                "operating_profit": 57_286_000,
            },
            {
                "local_code": "51250",
                "disclosed_date": "2026-06-30T16:00:00",
                "fiscal_year": "2026-06-30",
                "quarter": "3Q",
                "type_of_document": "3QFinancialStatements_Consolidated_JP",
                "gross_profit": None,
                "operating_profit": 57_000_000,
            },
        ],
    )

    actual = _read_actual(db_path)
    assert actual["gross_profit"] == 1_486_077_000
    assert actual["operating_profit"] == 57_000_000


def test_3925_sparse_correction_keeps_complete_1q(tmp_path):
    db_path = _create_db(
        tmp_path,
        [
            {
                "local_code": "39250",
                "disclosed_date": "2025-08-13",
                "fiscal_year": "2026-03-31",
                "quarter": "1Q",
                "type_of_document": "1QFinancialStatements_Consolidated_JP",
                "net_sales": 1_408_000_000,
                "gross_profit": 626_241_000,
                "operating_profit": 327_000_000,
            },
            {
                "local_code": "39250",
                "disclosed_date": "2025-08-19",
                "fiscal_year": "2026-03-31",
                "quarter": "1Q",
                "type_of_document": "1QFinancialStatements_Consolidated_JP",
                "net_sales": None,
                "gross_profit": None,
                "operating_profit": None,
            },
        ],
    )

    actual = _read_actual(db_path)
    assert actual["sales"] == 1_408_000_000
    assert actual["gross_profit"] == 626_241_000
    assert actual["operating_profit"] == 327_000_000
