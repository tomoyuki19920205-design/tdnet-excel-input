"""test_2301_gross_profit_gap.py — 2301 gross_profit 欠損修正テスト

sync_financials.py の field-level COALESCE merge が
訂正開示の gross_profit=None を正しく扱うことを検証する。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


def _create_test_db(rows: list[dict]) -> str:
    """テスト用 jquants_financials_normalized テーブルを持つ DB を作成"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute("""
        CREATE TABLE jquants_financials_normalized (
            local_code TEXT NOT NULL,
            disclosed_date TEXT NOT NULL,
            current_fiscal_year_end_date TEXT NOT NULL,
            type_of_current_period TEXT NOT NULL,
            type_of_document TEXT,
            net_sales INTEGER,
            gross_profit INTEGER,
            operating_profit INTEGER,
            raw_json TEXT,
            fetched_at TEXT
        )
    """)
    for r in rows:
        conn.execute("""
            INSERT INTO jquants_financials_normalized
            (local_code, disclosed_date, current_fiscal_year_end_date,
             type_of_current_period, net_sales, gross_profit, operating_profit,
             fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r.get("local_code", "23010"),
            r["disclosed_date"],
            r["current_fiscal_year_end_date"],
            r["type_of_current_period"],
            r.get("net_sales"),
            r.get("gross_profit"),
            r.get("operating_profit"),
            r.get("fetched_at", "2026-03-02 12:00:00"),
        ))
    conn.commit()
    conn.close()
    return tmp.name


def _run_sync_query(db_path: str) -> list[dict]:
    """sync_financials.py の _QUERY_BASE を実行して結果を返す"""
    from tools.sync_financials import _QUERY_BASE

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = _QUERY_BASE.format(where_clause="")
    rows = conn.execute(query).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class TestFieldLevelCoalesceMerge:
    """field-level COALESCE merge テスト"""

    def test_2301_2024_10_31_2q_gross_profit_preserved(self):
        """2024-10-31 2Q: 先行開示にgross_profit有→訂正開示にNULL→有値が保持される"""
        db = _create_test_db([
            {
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2498461000,
                "operating_profit": 615000000,
            },
            {
                "disclosed_date": "2024-06-11",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": None,  # 訂正開示でNULL
                "operating_profit": 615000000,
            },
        ])
        try:
            results = _run_sync_query(db)
            assert len(results) == 1
            r = results[0]
            assert r["gross_profit"] == 2498461000, (
                f"gross_profit should be preserved from earlier filing, got {r['gross_profit']}"
            )
            assert r["sales"] == 4024000000
            assert r["operating_profit"] == 615000000
        finally:
            os.unlink(db)

    def test_2301_2022_10_31_fy_gross_profit_preserved(self):
        """2022-10-31 FY: 先行開示にgross_profit有→訂正開示にNULL→有値が保持される"""
        db = _create_test_db([
            {
                "disclosed_date": "2022-12-12",
                "current_fiscal_year_end_date": "2022-10-31",
                "type_of_current_period": "FY",
                "net_sales": 6773000000,
                "gross_profit": 4506272000,
                "operating_profit": 1621000000,
            },
            {
                "disclosed_date": "2022-12-13",
                "current_fiscal_year_end_date": "2022-10-31",
                "type_of_current_period": "FY",
                "net_sales": 6773000000,
                "gross_profit": None,  # 訂正開示でNULL
                "operating_profit": 1621000000,
            },
        ])
        try:
            results = _run_sync_query(db)
            assert len(results) == 1
            r = results[0]
            assert r["gross_profit"] == 4506272000, (
                f"gross_profit should be preserved from earlier filing, got {r['gross_profit']}"
            )
        finally:
            os.unlink(db)

    def test_latest_gross_profit_wins_when_both_non_null(self):
        """両方の開示にgross_profit有→最新の値が採用される"""
        db = _create_test_db([
            {
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2498461000,
                "operating_profit": 615000000,
            },
            {
                "disclosed_date": "2024-06-11",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2600000000,  # 訂正値
                "operating_profit": 615000000,
            },
        ])
        try:
            results = _run_sync_query(db)
            assert len(results) == 1
            r = results[0]
            assert r["gross_profit"] == 2600000000, "latest non-NULL should win"
        finally:
            os.unlink(db)

    def test_single_row_unaffected(self):
        """単一行の場合は変わらない"""
        db = _create_test_db([
            {
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2498461000,
                "operating_profit": 615000000,
            },
        ])
        try:
            results = _run_sync_query(db)
            assert len(results) == 1
            assert results[0]["gross_profit"] == 2498461000
        finally:
            os.unlink(db)

    def test_null_does_not_overwrite_existing(self):
        """値ありデータが None で上書きされないテスト (汎用)"""
        db = _create_test_db([
            {
                "local_code": "99990",
                "disclosed_date": "2025-01-10",
                "current_fiscal_year_end_date": "2025-03-31",
                "type_of_current_period": "3Q",
                "net_sales": 10000000000,
                "gross_profit": 5000000000,
                "operating_profit": 2000000000,
            },
            {
                "local_code": "99990",
                "disclosed_date": "2025-01-11",
                "current_fiscal_year_end_date": "2025-03-31",
                "type_of_current_period": "3Q",
                "net_sales": 10000000000,
                "gross_profit": None,  # 訂正でNULL
                "operating_profit": None,  # operating_profitもNULL
            },
        ])
        try:
            results = _run_sync_query(db)
            r = [x for x in results if x["ticker"] == "99990"][0]
            assert r["gross_profit"] == 5000000000
            assert r["operating_profit"] == 2000000000
        finally:
            os.unlink(db)

    def test_all_null_stays_null(self):
        """全行がNULLなら結果もNULL"""
        db = _create_test_db([
            {
                "disclosed_date": "2025-01-10",
                "current_fiscal_year_end_date": "2025-03-31",
                "type_of_current_period": "1Q",
                "net_sales": 1000000000,
                "gross_profit": None,
                "operating_profit": -100000000,
            },
            {
                "disclosed_date": "2025-01-11",
                "current_fiscal_year_end_date": "2025-03-31",
                "type_of_current_period": "1Q",
                "net_sales": 1000000000,
                "gross_profit": None,
                "operating_profit": -100000000,
            },
        ])
        try:
            results = _run_sync_query(db)
            assert len(results) == 1
            assert results[0]["gross_profit"] is None
        finally:
            os.unlink(db)

    def test_quarter_normalization_2q_fy(self):
        """2Q / FY の quarter が正しく出力される"""
        db = _create_test_db([
            {
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2498461000,
                "operating_profit": 615000000,
            },
            {
                "disclosed_date": "2022-12-12",
                "current_fiscal_year_end_date": "2022-10-31",
                "type_of_current_period": "FY",
                "net_sales": 6773000000,
                "gross_profit": 4506272000,
                "operating_profit": 1621000000,
            },
        ])
        try:
            results = _run_sync_query(db)
            quarters = {r["quarter"] for r in results}
            assert "2Q" in quarters
            assert "FY" in quarters
        finally:
            os.unlink(db)

    def test_multiple_tickers_independent(self):
        """複数の ticker が互いに影響しない"""
        db = _create_test_db([
            {
                "local_code": "23010",
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2498461000,
                "operating_profit": 615000000,
            },
            {
                "local_code": "23010",
                "disclosed_date": "2024-06-11",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": None,
                "operating_profit": 615000000,
            },
            {
                "local_code": "17360",
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-03-31",
                "type_of_current_period": "FY",
                "net_sales": 50000000000,
                "gross_profit": 20000000000,
                "operating_profit": 5000000000,
            },
        ])
        try:
            results = _run_sync_query(db)
            r_2301 = [x for x in results if x["ticker"] == "23010"][0]
            r_1736 = [x for x in results if x["ticker"] == "17360"][0]
            assert r_2301["gross_profit"] == 2498461000
            assert r_1736["gross_profit"] == 20000000000
        finally:
            os.unlink(db)



class TestCheckMissingGrossProfit:
    """check_missing_gross_profit ツールの3分類テスト"""

    def _make_db(self, rows):
        """テスト用 DB を作成する"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute("""
            CREATE TABLE jquants_financials_normalized (
                local_code TEXT, disclosed_date TEXT,
                current_fiscal_year_end_date TEXT,
                type_of_current_period TEXT,
                net_sales INTEGER, gross_profit INTEGER,
                operating_profit INTEGER, fetched_at TEXT
            )
        """)
        for r in rows:
            conn.execute("""
                INSERT INTO jquants_financials_normalized
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r.get("local_code", "23010"),
                r["disclosed_date"],
                r["current_fiscal_year_end_date"],
                r["type_of_current_period"],
                r.get("net_sales"),
                r.get("gross_profit"),
                r.get("operating_profit"),
                r.get("fetched_at", "2026-03-02"),
            ))
        conn.commit()
        conn.close()
        return tmp.name

    def test_suspicious_missing_detects_gp_null(self):
        """sales+op有+gp=NULLの行を検出する"""
        from tools.check_missing_gross_profit import check_suspicious_missing

        db = self._make_db([{
            "disclosed_date": "2024-06-11",
            "current_fiscal_year_end_date": "2024-10-31",
            "type_of_current_period": "2Q",
            "net_sales": 4024000000,
            "gross_profit": None,
            "operating_profit": 615000000,
        }])
        try:
            results = check_suspicious_missing(
                db, "jquants_financials_normalized")
            assert len(results) == 1
            assert results[0]["ticker"] == "23010"
        finally:
            os.unlink(db)

    def test_suspicious_missing_no_false_positive(self):
        """gross_profit があれば検出しない"""
        from tools.check_missing_gross_profit import check_suspicious_missing

        db = self._make_db([{
            "disclosed_date": "2024-06-10",
            "current_fiscal_year_end_date": "2024-10-31",
            "type_of_current_period": "2Q",
            "net_sales": 4024000000,
            "gross_profit": 2498461000,
            "operating_profit": 615000000,
        }])
        try:
            results = check_suspicious_missing(
                db, "jquants_financials_normalized")
            assert len(results) == 0
        finally:
            os.unlink(db)

    def test_raw_missing_detects_all_gp_null(self):
        """gross_profit IS NULL の全行を検出する"""
        from tools.check_missing_gross_profit import check_raw_missing

        db = self._make_db([
            {
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": None,
                "gross_profit": None,
                "operating_profit": None,
            },
            {
                "disclosed_date": "2024-06-11",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": None,
                "operating_profit": 615000000,
            },
        ])
        try:
            results = check_raw_missing(
                db, "jquants_financials_normalized")
            assert len(results) == 2
        finally:
            os.unlink(db)

    def test_overwrite_risk_detects_2301(self):
        """overwrite_risk: 2301 パターンを検出する"""
        from tools.check_missing_gross_profit import check_overwrite_risk

        db = self._make_db([
            {
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2498461000,
                "operating_profit": 615000000,
            },
            {
                "disclosed_date": "2024-06-11",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": None,
                "operating_profit": 615000000,
            },
        ])
        try:
            results = check_overwrite_risk(
                db, "jquants_financials_normalized")
            assert len(results) == 1
            r = results[0]
            assert r["ticker"] == "23010"
            assert r["gp_nonnull_rows"] == 1
            assert r["gp_null_rows"] == 1
            assert r["nonnull_gross_profit"] == 2498461000
        finally:
            os.unlink(db)

    def test_overwrite_risk_multi_ticker(self):
        """overwrite_risk: 複数銘柄を検出する"""
        from tools.check_missing_gross_profit import check_overwrite_risk

        db = self._make_db([
            # ticker 23010
            {
                "local_code": "23010",
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2498461000,
                "operating_profit": 615000000,
            },
            {
                "local_code": "23010",
                "disclosed_date": "2024-06-11",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": None,
                "operating_profit": 615000000,
            },
            # ticker 95530
            {
                "local_code": "95530",
                "disclosed_date": "2026-07-10",
                "current_fiscal_year_end_date": "2026-09-30",
                "type_of_current_period": "1Q",
                "net_sales": 8000000000,
                "gross_profit": 3000000000,
                "operating_profit": 1000000000,
            },
            {
                "local_code": "95530",
                "disclosed_date": "2026-07-11",
                "current_fiscal_year_end_date": "2026-09-30",
                "type_of_current_period": "1Q",
                "net_sales": 8000000000,
                "gross_profit": None,
                "operating_profit": 1000000000,
            },
            # ticker 17680 — no overwrite risk (both have gp)
            {
                "local_code": "17680",
                "disclosed_date": "2026-01-10",
                "current_fiscal_year_end_date": "2026-03-31",
                "type_of_current_period": "3Q",
                "net_sales": 5000000000,
                "gross_profit": 2000000000,
                "operating_profit": 500000000,
            },
        ])
        try:
            results = check_overwrite_risk(
                db, "jquants_financials_normalized")
            tickers = {r["ticker"] for r in results}
            assert "23010" in tickers
            assert "95530" in tickers
            assert "17680" not in tickers  # no risk
            assert len(results) == 2
        finally:
            os.unlink(db)

    def test_overwrite_risk_no_false_positive(self):
        """overwrite_risk: 全行にgpがある場合は検出しない"""
        from tools.check_missing_gross_profit import check_overwrite_risk

        db = self._make_db([
            {
                "disclosed_date": "2024-06-10",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2498461000,
                "operating_profit": 615000000,
            },
            {
                "disclosed_date": "2024-06-11",
                "current_fiscal_year_end_date": "2024-10-31",
                "type_of_current_period": "2Q",
                "net_sales": 4024000000,
                "gross_profit": 2600000000,
                "operating_profit": 615000000,
            },
        ])
        try:
            results = check_overwrite_risk(
                db, "jquants_financials_normalized")
            assert len(results) == 0
        finally:
            os.unlink(db)


class TestExistingAttachmentFallbackRegression:
    """既存の attachment fallback との回帰テスト"""

    def test_single_filing_with_gross_profit_unchanged(self):
        """単一開示（訂正なし）で gross_profit が正常に通る"""
        db = _create_test_db([
            {
                "local_code": "54610",
                "disclosed_date": "2025-01-15",
                "current_fiscal_year_end_date": "2025-03-31",
                "type_of_current_period": "3Q",
                "net_sales": 100000000000,
                "gross_profit": 30000000000,
                "operating_profit": 10000000000,
            },
        ])
        try:
            results = _run_sync_query(db)
            r = results[0]
            assert r["gross_profit"] == 30000000000
        finally:
            os.unlink(db)

    def test_three_filings_picks_best_per_field(self):
        """3回開示があった場合、各 field の最新非NULL値が採用される"""
        db = _create_test_db([
            {
                "local_code": "54610",
                "disclosed_date": "2025-01-15",
                "current_fiscal_year_end_date": "2025-03-31",
                "type_of_current_period": "3Q",
                "net_sales": 100000000000,
                "gross_profit": 30000000000,
                "operating_profit": 10000000000,
            },
            {
                "local_code": "54610",
                "disclosed_date": "2025-01-16",
                "current_fiscal_year_end_date": "2025-03-31",
                "type_of_current_period": "3Q",
                "net_sales": 100000000000,
                "gross_profit": None,
                "operating_profit": 10500000000,  # 訂正で OP 更新
            },
            {
                "local_code": "54610",
                "disclosed_date": "2025-01-17",
                "current_fiscal_year_end_date": "2025-03-31",
                "type_of_current_period": "3Q",
                "net_sales": 101000000000,  # 訂正で sales 更新
                "gross_profit": None,
                "operating_profit": None,
            },
        ])
        try:
            results = _run_sync_query(db)
            r = results[0]
            assert r["sales"] == 101000000000, "latest sales should be used"
            assert r["gross_profit"] == 30000000000, "first filing's gp should be kept"
            assert r["operating_profit"] == 10500000000, "second filing's OP should be used"
        finally:
            os.unlink(db)

