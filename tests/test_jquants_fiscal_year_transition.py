import json
import sqlite3
from datetime import datetime

from tools.rebuild_canonical_financials import _read_jquants_source
from tools.repair_4331_fiscal_year_transition import local_transition_audit
from tools.sync_financials import read_sqlite


def _make_db(path):
    conn = sqlite3.connect(path)
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
            profit_before_tax INTEGER,
            raw_json TEXT,
            fetched_at TEXT NOT NULL
        )
        """
    )
    return conn


def _insert(conn, *, disclosed, fy_start, fy_end, period_end, quarter, sales, op,
            fetched_at="2020-01-01 00:00:00"):
    raw = {
        "Code": "43310",
        "CurFYSt": fy_start,
        "CurFYEn": fy_end,
        "CurPerSt": fy_start,
        "CurPerEn": period_end,
        "CurPerType": quarter,
    }
    conn.execute(
        """
        INSERT INTO jquants_financials_normalized VALUES
        (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            "43310",
            disclosed,
            fy_end,
            quarter,
            f"{quarter}FinancialStatements_Consolidated_JP",
            sales,
            op,
            json.dumps(raw),
            fetched_at,
        ),
    )


def _transition_rows(conn, fy_fetched_at="2020-01-01 00:00:00"):
    _insert(
        conn,
        disclosed="2025-08-13",
        fy_start="2025-04-01",
        fy_end="2026-03-31",
        period_end="2025-06-30",
        quarter="1Q",
        sales=11_100_000_000,
        op=12_000_000,
    )
    _insert(
        conn,
        disclosed="2025-11-12",
        fy_start="2025-04-01",
        fy_end="2026-03-31",
        period_end="2025-09-30",
        quarter="2Q",
        sales=21_306_000_000,
        op=-465_000_000,
    )
    _insert(
        conn,
        disclosed="2026-03-06",
        fy_start="2025-04-01",
        fy_end="2025-12-31",
        period_end="2025-12-31",
        quarter="FY",
        sales=35_709_000_000,
        op=1_622_000_000,
        fetched_at=fy_fetched_at,
    )
    _insert(
        conn,
        disclosed="2026-05-13",
        fy_start="2026-01-01",
        fy_end="2026-12-31",
        period_end="2026-03-31",
        quarter="1Q",
        sales=11_763_000_000,
        op=339_000_000,
    )
    conn.commit()


def test_transition_fy_resolves_interims_without_assuming_twelve_months(tmp_path):
    db = str(tmp_path / "jquants.db")
    conn = _make_db(db)
    _transition_rows(conn)
    conn.close()

    rows, _ = read_sqlite(db, ticker="4331")
    keyed = {(row["period"], row["quarter"]): row for row in rows}

    assert keyed[("2025-12-31", "1Q")]["sales"] == 11_100
    assert keyed[("2025-12-31", "1Q")]["operating_profit"] == 12
    assert keyed[("2025-12-31", "2Q")]["sales"] == 21_306
    assert keyed[("2025-12-31", "2Q")]["operating_profit"] == -465
    assert keyed[("2025-12-31", "FY")]["operating_profit"] == 1_622
    assert keyed[("2026-12-31", "1Q")]["operating_profit"] == 339
    assert not any(period == "2026-03-31" for period, _ in keyed)
    assert not any(quarter == "3Q" for _, quarter in keyed)


def test_recent_fy_reselects_older_transition_interims(tmp_path):
    db = str(tmp_path / "jquants.db")
    conn = _make_db(db)
    _transition_rows(conn, fy_fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    conn.close()

    rows, _ = read_sqlite(db, recent_days=1, ticker="4331")
    keys = {(row["period"], row["quarter"]) for row in rows}

    assert keys == {
        ("2025-12-31", "1Q"),
        ("2025-12-31", "2Q"),
        ("2025-12-31", "FY"),
    }


def test_without_matching_fy_start_source_period_is_preserved(tmp_path):
    db = str(tmp_path / "jquants.db")
    conn = _make_db(db)
    _insert(
        conn,
        disclosed="2025-08-13",
        fy_start="2025-04-01",
        fy_end="2026-03-31",
        period_end="2025-06-30",
        quarter="1Q",
        sales=11_100_000_000,
        op=12_000_000,
    )
    _insert(
        conn,
        disclosed="2026-05-13",
        fy_start="2026-01-01",
        fy_end="2026-12-31",
        period_end="2026-03-31",
        quarter="FY",
        sales=1,
        op=1,
    )
    conn.commit()
    conn.close()

    rows, _ = read_sqlite(db, ticker="4331")
    assert rows[0]["period"] == "2026-03-31"


def test_rebuild_path_uses_same_transition_resolution(tmp_path):
    db = str(tmp_path / "jquants.db")
    conn = _make_db(db)
    _transition_rows(conn)
    conn.close()

    rows = _read_jquants_source(db)
    keys = {(row["period"], row["quarter"]) for row in rows}

    assert ("2025-12-31", "1Q") in keys
    assert ("2025-12-31", "2Q") in keys
    assert ("2025-12-31", "FY") in keys
    assert ("2026-12-31", "1Q") in keys
    assert not any(period == "2026-03-31" for period, _ in keys)


def test_local_audit_reports_candidates_without_mutating_source(tmp_path):
    db = str(tmp_path / "jquants.db")
    conn = _make_db(db)
    _transition_rows(conn)
    conn.close()

    candidates = local_transition_audit(tmp_path / "jquants.db")

    assert [(row["quarter"], row["raw_fiscal_year_end"], row["resolved_fiscal_year_end"])
            for row in candidates] == [
        ("1Q", "2026-03-31", "2025-12-31"),
        ("2Q", "2026-03-31", "2025-12-31"),
    ]
    with sqlite3.connect(db) as conn:
        stored = conn.execute(
            "SELECT DISTINCT current_fiscal_year_end_date "
            "FROM jquants_financials_normalized "
            "WHERE type_of_current_period IN ('1Q', '2Q') "
            "AND json_extract(raw_json, '$.CurFYSt') = '2025-04-01'"
        ).fetchall()
    assert stored == [("2026-03-31",)]
