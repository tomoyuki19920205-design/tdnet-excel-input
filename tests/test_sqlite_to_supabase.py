"""
test_sqlite_to_supabase.py — SQLite → Supabase push ツールのユニットテスト
"""
import json
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, MagicMock, call

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from tools.sqlite_to_supabase import (
    push_sqlite_to_supabase,
    _METRIC_MAP,
    _UNIT_MULTIPLIER,
    _load_checkpoint,
    _save_checkpoint,
    _clear_checkpoint,
    _detect_source_origin,
    _normalize_to_millions,
    _build_financials_rows_from_tdnet,
)


# ============================================================
# ヘルパー: テスト用SQLite DB作成
# ============================================================
def _create_test_db(tmpdir: str, rows: list[dict]) -> str:
    """テスト用のquarterly_resultsテーブルを持つSQLiteを作成"""
    db_path = os.path.join(tmpdir, "test_decision.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE quarterly_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL,
            fiscal_year_end TEXT NOT NULL,
            quarter TEXT NOT NULL,
            sales REAL,
            gross_profit REAL,
            gross_margin REAL,
            sga REAL,
            operating_profit REAL,
            unit TEXT DEFAULT '百万円',
            source_doc_id TEXT,
            source_url TEXT,
            zip_hash TEXT,
            parser_version TEXT DEFAULT 'v2',
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(company_code, fiscal_year_end, quarter)
        )
    """)

    for r in rows:
        conn.execute(
            """INSERT INTO quarterly_results
               (company_code, fiscal_year_end, quarter, sales, gross_profit,
                operating_profit, unit, source_doc_id, source_url, zip_hash,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       datetime('now'), datetime('now'))""",
            (
                r.get("company_code", "0812"),
                r.get("fiscal_year_end", "2026-03-31"),
                r.get("quarter", "2Q"),
                r.get("sales"),
                r.get("gross_profit"),
                r.get("operating_profit"),
                r.get("unit", "百万円"),
                r.get("source_doc_id"),
                r.get("source_url"),
                r.get("zip_hash"),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


def _mock_response(data):
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    resp.status_code = 200
    resp.text = ""
    return resp


# ============================================================
# テスト: 由来判定
# ============================================================
class TestDetectSourceOrigin:
    def test_tdnet_with_source_url(self):
        row = {"source_url": "https://webapi.yanoshin.jp/rd.php?..."}
        assert _detect_source_origin(row) == "tdnet"

    def test_jquants_no_metadata(self):
        row = {"source_url": None, "source_doc_id": None, "zip_hash": None}
        assert _detect_source_origin(row) == "jquants"

    def test_jquants_empty_strings(self):
        row = {"source_url": "", "source_doc_id": "", "zip_hash": ""}
        assert _detect_source_origin(row) == "jquants"

    def test_unknown_with_doc_id_only(self):
        row = {"source_url": None, "source_doc_id": "abc123", "zip_hash": None}
        assert _detect_source_origin(row) == "unknown"

    def test_unknown_with_zip_hash_only(self):
        row = {"source_url": None, "source_doc_id": None, "zip_hash": "sha256:..."}
        assert _detect_source_origin(row) == "unknown"

    def test_missing_keys_treated_as_jquants(self):
        """キーが存在しない場合は jquants (メタ情報なし)"""
        row = {}
        assert _detect_source_origin(row) == "jquants"


# ============================================================
# テスト: 単位正規化
# ============================================================
class TestNormalizeToMillions:
    def test_tdnet_yen_to_millions(self):
        """TDnet 円 → 百万円変換"""
        assert _normalize_to_millions(1_368_000_000, "tdnet") == 1368

    def test_tdnet_yen_to_millions_large(self):
        """TDnet 大企業の円 → 百万円変換"""
        assert _normalize_to_millions(4_197_922_000_000, "tdnet") == 4197922

    def test_tdnet_negative(self):
        """TDnet 負の値の変換"""
        assert _normalize_to_millions(-692_000_000, "tdnet") == -692

    def test_tdnet_with_remainder_rounds(self):
        """TDnet 端数は round() される (int切り捨てではない)"""
        # 1,500,000 → 1.5 百万円 → round(1.5) = 2
        assert _normalize_to_millions(1_500_000, "tdnet") == 2
        # 500,000 → 0.5 百万円 → round(0.5) = 0
        assert _normalize_to_millions(500_000, "tdnet") == 0

    def test_jquants_millions_unchanged(self):
        """J-Quants 百万円 → そのまま"""
        assert _normalize_to_millions(6485, "jquants") == 6485

    def test_jquants_large_unchanged(self):
        """J-Quants 大企業の百万円値もそのまま"""
        assert _normalize_to_millions(38_087_604, "jquants") == 38087604

    def test_none_stays_none_tdnet(self):
        assert _normalize_to_millions(None, "tdnet") is None

    def test_none_stays_none_jquants(self):
        assert _normalize_to_millions(None, "jquants") is None

    def test_none_stays_none_unknown(self):
        assert _normalize_to_millions(None, "unknown") is None

    def test_empty_string_is_none(self):
        assert _normalize_to_millions("", "jquants") is None

    def test_unknown_heuristic_large_value(self):
        """不明 + 大きい値(>10億) → 円→百万円変換"""
        assert _normalize_to_millions(1_368_000_000, "unknown") == 1368

    def test_unknown_heuristic_small_value(self):
        """不明 + 小さい値(<=10億) → 百万円とみなす"""
        assert _normalize_to_millions(6485, "unknown") == 6485

    def test_zero_stays_zero(self):
        """0 は 0 のまま (None にならない)"""
        assert _normalize_to_millions(0, "jquants") == 0
        assert _normalize_to_millions(0, "tdnet") == 0


# ============================================================
# テスト: _build_financials_rows_from_tdnet 統合
# ============================================================
class TestBuildFinancialsNormalization:
    def _make_row(self, **kwargs):
        """sqlite3.Row 互換の dict を作成"""
        base = {
            "company_code": "17360",
            "fiscal_year_end": "2026-03-31",
            "quarter": "1Q",
            "sales": 6485.0,
            "gross_profit": 3000.0,
            "operating_profit": 522.0,
            "unit": "百万円",
            "source_url": None,
            "source_doc_id": None,
            "zip_hash": None,
        }
        base.update(kwargs)
        # sqlite3.Row 互換: dict-like access
        class FakeRow(dict):
            def __getitem__(self, key):
                return self.get(key)
        return FakeRow(base)

    def test_jquants_row_unchanged(self):
        """J-Quants 行は百万円のまま"""
        rows = [self._make_row(
            company_code="17360", sales=6485.0, operating_profit=522.0,
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["ticker"] == "1736"
        assert result[0]["sales"] == 6485
        assert result[0]["operating_profit"] == 522

    def test_tdnet_row_yen_to_millions(self):
        """TDnet 行は円 → 百万円変換"""
        rows = [self._make_row(
            company_code="2301",
            fiscal_year_end="2026-10-31",
            sales=1_368_000_000.0,
            gross_profit=None,
            operating_profit=-692_000_000.0,
            source_url="https://webapi.yanoshin.jp/...",
            source_doc_id="doc123",
            zip_hash="sha256:abc",
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["ticker"] == "2301"
        assert result[0]["sales"] == 1368
        assert result[0]["gross_profit"] is None
        assert result[0]["operating_profit"] == -692

    def test_all_none_row_skipped(self):
        """全金額 None の行はスキップ"""
        rows = [self._make_row(
            sales=None, gross_profit=None, operating_profit=None,
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 0

    def test_null_not_converted_to_zero(self):
        """None は 0 に変換されない"""
        rows = [self._make_row(
            sales=6485.0, gross_profit=None, operating_profit=None,
        )]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        assert result[0]["sales"] == 6485
        assert result[0]["gross_profit"] is None
        assert result[0]["operating_profit"] is None

    def test_mixed_origins_in_batch(self):
        """同一バッチで異なる由来の行が正しく処理される"""
        rows = [
            self._make_row(
                company_code="17360", sales=6485.0, operating_profit=522.0,
            ),  # J-Quants
            self._make_row(
                company_code="2301",
                fiscal_year_end="2026-10-31",
                quarter="1Q",
                sales=1_368_000_000.0,
                operating_profit=-692_000_000.0,
                source_url="https://tdnet...",
                source_doc_id="doc123",
                zip_hash="sha256:abc",
            ),  # TDnet
        ]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 2

        jquants_row = [r for r in result if r["ticker"] == "1736"][0]
        tdnet_row = [r for r in result if r["ticker"] == "2301"][0]

        assert jquants_row["sales"] == 6485
        assert tdnet_row["sales"] == 1368

    # --- 衝突マージテスト ---

    def test_collision_jquants_wins_more_nonnull(self):
        """衝突: per-field confidence merge

        TDnet confidence(0.92) > J-Quants(0.85) のため、
        各 field は TDnet 値が優先される。ただし TDnet 側が None の
        field (gross_profit) は J-Quants 値が採用される。
        """
        rows = [
            # J-Quants (5桁): sales+gp+op = 3 非NULL
            self._make_row(
                company_code="42380",
                fiscal_year_end="2026-01-31",
                quarter="4Q",
                sales=12780.0, gross_profit=5000.0, operating_profit=640.0,
            ),
            # TDnet (4桁): sales+op = 2 非NULL (gp=None)
            self._make_row(
                company_code="4238",
                fiscal_year_end="2026-01-31",
                quarter="4Q",
                sales=12599000000.0, gross_profit=None, operating_profit=555000000.0,
                source_url="https://tdnet...",
            ),
        ]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        r = result[0]
        assert r["ticker"] == "4238"
        # TDnet 値が confidence 優先で採用 (百万円変換後)
        assert r["sales"] == 12599
        # gross_profit は TDnet=None → J-Quants値採用
        assert r["gross_profit"] == 5000
        assert r["operating_profit"] == 555

    def test_collision_tdnet_wins_more_nonnull(self):
        """衝突: TDnet の方が非NULL多い → TDnet 採用"""
        rows = [
            # J-Quants (5桁): sales のみ = 1 非NULL
            self._make_row(
                company_code="78040",
                fiscal_year_end="2026-10-31",
                quarter="4Q",
                sales=5000.0, gross_profit=None, operating_profit=None,
            ),
            # TDnet (4桁): sales+op = 2 非NULL
            self._make_row(
                company_code="7804",
                fiscal_year_end="2026-10-31",
                quarter="4Q",
                sales=1009000000.0, gross_profit=None, operating_profit=103000000.0,
                source_url="https://tdnet...",
            ),
        ]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        r = result[0]
        assert r["ticker"] == "7804"
        # TDnet 値 (百万円変換後) が採用される
        assert r["sales"] == 1009
        assert r["operating_profit"] == 103

    def test_collision_jquants_all_none_tdnet_adopted(self):
        """衝突: J-Quants 全 None → TDnet 採用"""
        rows = [
            # J-Quants (5桁): 全 None → _build で skip される
            self._make_row(
                company_code="23010",
                fiscal_year_end="2026-10-31",
                quarter="1Q",
                sales=None, gross_profit=None, operating_profit=None,
            ),
            # TDnet (4桁): 値あり
            self._make_row(
                company_code="2301",
                fiscal_year_end="2026-10-31",
                quarter="1Q",
                sales=1368000000.0, gross_profit=None, operating_profit=-692000000.0,
                source_url="https://tdnet...",
            ),
        ]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        r = result[0]
        assert r["ticker"] == "2301"
        assert r["sales"] == 1368
        assert r["operating_profit"] == -692

    def test_collision_tie_tdnet_wins_on_confidence(self):
        """衝突: per-field confidence merge (同数 non-NULL)

        TDnet confidence(0.92) > J-Quants(0.85) のため、
        field 単位で TDnet 値が優先される。
        """
        rows = [
            # TDnet (4桁): sales+op = 2 非NULL (先に処理)
            self._make_row(
                company_code="1928",
                fiscal_year_end="2026-01-31",
                quarter="4Q",
                sales=4197922000000.0, gross_profit=None, operating_profit=341402000000.0,
                source_url="https://tdnet...",
            ),
            # J-Quants (5桁): sales+op = 2 非NULL (同点)
            self._make_row(
                company_code="19280",
                fiscal_year_end="2026-01-31",
                quarter="4Q",
                sales=4753.0, gross_profit=None, operating_profit=159.0,
            ),
        ]
        result = _build_financials_rows_from_tdnet(rows)
        assert len(result) == 1
        r = result[0]
        assert r["ticker"] == "1928"
        # TDnet 値が confidence 優先で採用 (百万円変換後)
        assert r["sales"] == 4197922
        assert r["operating_profit"] == 341402

    def test_collision_produces_unique_keys(self):
        """衝突マージ後、payload key は全て一意"""
        rows = [
            self._make_row(
                company_code="42380", fiscal_year_end="2026-01-31",
                quarter="4Q", sales=12780.0, operating_profit=640.0,
            ),
            self._make_row(
                company_code="4238", fiscal_year_end="2026-01-31",
                quarter="4Q", sales=12599000000.0, operating_profit=555000000.0,
                source_url="https://tdnet...",
            ),
            self._make_row(
                company_code="17360", fiscal_year_end="2026-03-31",
                quarter="1Q", sales=6485.0, operating_profit=522.0,
            ),
        ]
        result = _build_financials_rows_from_tdnet(rows)
        keys = [(r["ticker"], r["period"], r["quarter"]) for r in result]
        assert len(keys) == len(set(keys)), f"Duplicate keys found: {keys}"


# ============================================================
# テスト: 既存
# ============================================================

class TestDryRun:
    def test_dry_run_reads_sqlite(self):
        """dry-runモードではSQLiteの行数を返し、Supabaseへは書き込まない"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _create_test_db(tmpdir, [
                {"company_code": "0812", "sales": 100.0, "operating_profit": 10.0},
                {"company_code": "3538", "fiscal_year_end": "2025-12-31",
                 "sales": 200.0, "operating_profit": 20.0},
            ])

            stats = push_sqlite_to_supabase(
                db_path=db_path,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
                dry_run=True,
            )

            assert stats["sqlite_rows"] == 2
            assert stats["target_rows"] == 2
            assert stats["facts_pushed"] == 0
            assert stats["complete"] is True


class TestUnitConversion:
    def test_million_yen_multiplier(self):
        assert _UNIT_MULTIPLIER["百万円"] == 1_000_000

    def test_thousand_yen_multiplier(self):
        assert _UNIT_MULTIPLIER["千円"] == 1_000

    def test_yen_multiplier(self):
        assert _UNIT_MULTIPLIER["円"] == 1


class TestMetricMap:
    def test_sales_mapping(self):
        assert _METRIC_MAP["sales"] == "NET_SALES"

    def test_gross_profit_mapping(self):
        assert _METRIC_MAP["gross_profit"] == "GROSS_PROFIT"

    def test_operating_profit_mapping(self):
        assert _METRIC_MAP["operating_profit"] == "OP_INCOME"


class TestCheckpoint:
    def test_save_and_load(self):
        """チェックポイントの保存と読み込み"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cp = os.path.join(tmpdir, "checkpoint.json")
            _save_checkpoint(cp, {"offset": 42, "processed": 10})
            loaded = _load_checkpoint(cp)
            assert loaded["offset"] == 42
            assert loaded["processed"] == 10

    def test_load_missing(self):
        """存在しないチェックポイントは空dictを返す"""
        assert _load_checkpoint("/nonexistent/checkpoint.json") == {}

    def test_clear(self):
        """チェックポイントの削除"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cp = os.path.join(tmpdir, "checkpoint.json")
            _save_checkpoint(cp, {"offset": 10})
            _clear_checkpoint(cp)
            assert _load_checkpoint(cp) == {}

    def test_clear_missing_no_error(self):
        """存在しないファイルのclearでエラーにならない"""
        _clear_checkpoint("/nonexistent/checkpoint.json")


class TestLimit:
    def test_limit_restricts_rows(self):
        """--limit で処理行数が制限される"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _create_test_db(tmpdir, [
                {"company_code": f"{i:04d}", "sales": float(i * 100)}
                for i in range(1, 11)  # 10行
            ])

            stats = push_sqlite_to_supabase(
                db_path=db_path,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
                dry_run=True,
                limit=3,
            )

            assert stats["sqlite_rows"] == 10
            assert stats["target_rows"] == 3


class TestPush:
    @staticmethod
    def _dynamic_mock(**kwargs):
        """URL/method パターンで適切な応答を返す dynamic mock。

        内部 API 呼び出し順序の変更に耐性がある。
        """
        _id_counter = {"company": 0, "period": 0, "disc": 0, "fact": 0}

        def _handle(method, url, **kw):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.status_code = 200
            resp.text = ""

            url_lower = url.lower()
            payload = kw.get("json", [])
            if isinstance(payload, list) and len(payload) > 0:
                first_item = payload[0]
            elif isinstance(payload, dict):
                first_item = payload
            else:
                first_item = {}

            if "companies" in url_lower:
                if method.upper() == "GET":
                    resp.json.return_value = []
                else:
                    result = []
                    items = payload if isinstance(payload, list) else [payload]
                    for item in items:
                        _id_counter["company"] += 1
                        result.append({"company_id": _id_counter["company"], **item})
                    resp.json.return_value = result
            elif "periods" in url_lower:
                if method.upper() == "GET":
                    resp.json.return_value = []
                else:
                    result = []
                    items = payload if isinstance(payload, list) else [payload]
                    for item in items:
                        _id_counter["period"] += 1
                        result.append({"period_id": _id_counter["period"], **item})
                    resp.json.return_value = result
            elif "disclosures" in url_lower:
                if method.upper() == "GET":
                    resp.json.return_value = []
                else:
                    result = []
                    items = payload if isinstance(payload, list) else [payload]
                    for item in items:
                        _id_counter["disc"] += 1
                        result.append({"disclosure_id": _id_counter["disc"], **item})
                    resp.json.return_value = result
            elif "facts" in url_lower:
                if method.upper() == "GET":
                    resp.json.return_value = []
                else:
                    items = payload if isinstance(payload, list) else [payload]
                    result = []
                    for item in items:
                        _id_counter["fact"] += 1
                        result.append({"fact_id": _id_counter["fact"], **item})
                    resp.json.return_value = result
            elif "financials" in url_lower:
                if method.upper() == "GET":
                    resp.json.return_value = []
                else:
                    resp.json.return_value = payload if isinstance(payload, list) else [payload]
            else:
                resp.json.return_value = []

            return resp
        return _handle

    @patch("lib.pipeline.db.get_supabase_write_config", return_value=None)
    @patch("tools.sqlite_to_supabase.requests.request")
    def test_single_row_push(self, mock_request, mock_write_config):
        """1行のquarterly_resultがSupabaseにpushされる"""
        mock_request.side_effect = self._dynamic_mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _create_test_db(tmpdir, [
                {"company_code": "0812", "sales": 100.0, "operating_profit": 10.0},
            ])
            cp = os.path.join(tmpdir, "cp.json")

            stats = push_sqlite_to_supabase(
                db_path=db_path,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
                checkpoint_path=cp,
            )

            assert stats["target_rows"] == 1
            assert stats["companies_upserted"] >= 1
            assert stats["periods_upserted"] >= 1
            assert stats["facts_pushed"] >= 1
            assert stats["errors"] == 0
            assert stats["complete"] is True

    @patch("lib.pipeline.db.get_supabase_write_config", return_value=None)
    @patch("tools.sqlite_to_supabase.requests.request")
    def test_null_values_skipped(self, mock_request, mock_write_config):
        """NULLの列はfacts pushされない"""
        mock_request.side_effect = self._dynamic_mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _create_test_db(tmpdir, [
                {"company_code": "0812", "sales": 100.0,
                 "gross_profit": None, "operating_profit": None},
            ])
            cp = os.path.join(tmpdir, "cp.json")

            stats = push_sqlite_to_supabase(
                db_path=db_path,
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
                checkpoint_path=cp,
            )

            assert stats["facts_pushed"] >= 1  # sales at minimum

    @patch("tools.sqlite_to_supabase._load_dotenv")
    @patch.dict(os.environ, {}, clear=True)
    def test_missing_credentials_raises(self, mock_dotenv):
        """接続情報未設定でValueError"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _create_test_db(tmpdir, [])
            with pytest.raises(ValueError, match="接続情報"):
                push_sqlite_to_supabase(
                    db_path=db_path,
                    supabase_url="",
                    supabase_key="",
                )

    def test_missing_db_raises(self):
        """DBファイルが存在しないとFileNotFoundError"""
        with pytest.raises(FileNotFoundError):
            push_sqlite_to_supabase(
                db_path="/nonexistent/test.db",
                supabase_url="https://test.supabase.co",
                supabase_key="test-key",
            )
