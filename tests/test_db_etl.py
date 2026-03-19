# ============================================================
# test_db_etl.py — XBRL ETL データベースパイプラインのテスト
# ============================================================
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.migrate_db import migrate
from tools.load_results_to_db import (
    load_single,
    load_from_path,
    _upsert_company,
    _upsert_period,
    _insert_fact_if_not_exists,
    _convert_to_jpy,
    METRIC_MAP,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def schema_path():
    """schema.sqlのパス"""
    return str(Path(__file__).resolve().parent.parent / "schema.sql")


@pytest.fixture
def mem_conn(schema_path):
    """in-memory SQLiteにスキーマ適用済みのコネクション"""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    yield conn
    conn.close()


@pytest.fixture
def tmp_db(schema_path, tmp_path):
    """一時ファイルにスキーマ適用済みのDBパス"""
    db_path = str(tmp_path / "test.db")
    migrate(db_path, schema_path)
    return db_path


def _sample_json(
    ticker: str = "0812",
    company_name: str = "タムラ製作所",
    title: str = "2025年3月期 第3四半期決算短信",
    disclosed_at: str = "2025-02-14T15:00:00+09:00",
    fiscal_year_end: str = "2025-03-31",
    quarter: int = 3,
    source_unit: str = "百万円",
    sales: int | None = 4915,
    gross_profit: int | None = 1200,
    op_income: int | None = 664,
    ordinary_income: int | None = None,
    scope: str = "CONSOLIDATED",
    metric_type: str = "actual",
    quality: str = "IXBRL",
    sha256: str | None = "abc123",
    url: str | None = "https://example.com/test.zip",
) -> dict:
    return {
        "ticker_code": ticker,
        "company_name": company_name,
        "title": title,
        "disclosed_at": disclosed_at,
        "url": url,
        "sha256": sha256,
        "doc_type": "TANSHIN",
        "source": "TDNET",
        "fiscal_year_end": fiscal_year_end,
        "quarter": quarter,
        "source_unit": source_unit,
        "values": {
            "sales": sales,
            "gross_profit": gross_profit,
            "op_income": op_income,
            "ordinary_income": ordinary_income,
        },
        "scope": scope,
        "metric_type": metric_type,
        "quality": quality,
    }


# ============================================================
# T1: スキーマ適用テスト
# ============================================================

class TestSchemaApply:
    """schema.sqlがin-memory SQLiteに正しく適用される"""

    def test_tables_created(self, mem_conn):
        """7テーブルが作成される"""
        cur = mem_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cur.fetchall()}
        expected = {
            "companies", "disclosures", "filing_artifacts",
            "periods", "facts", "guidance", "revisions",
        }
        assert expected.issubset(tables), f"不足テーブル: {expected - tables}"

    def test_views_created(self, mem_conn):
        """2ビューが作成される"""
        cur = mem_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        )
        views = {row[0] for row in cur.fetchall()}
        assert "v_latest_facts" in views
        assert "v_latest_guidance" in views

    def test_migrate_idempotent(self, tmp_db, schema_path):
        """2回適用しても問題ない"""
        # 2回目の適用
        migrate(tmp_db, schema_path)
        conn = sqlite3.connect(tmp_db)
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        )
        count = cur.fetchone()[0]
        conn.close()
        assert count >= 7


# ============================================================
# T2: 基本投入テスト
# ============================================================

class TestBasicInsert:
    """company→disclosure→period→facts が一連のフローで投入される"""

    def test_full_pipeline(self, mem_conn):
        """JSONデータの完全なパイプライン投入"""
        data = _sample_json()
        result = load_single(mem_conn, data)
        mem_conn.commit()

        assert result["status"] == "ok"
        assert result["inserted"] == 3  # sales, gross_profit, op_income

        # companyが入っている
        cur = mem_conn.execute("SELECT * FROM companies WHERE ticker_code = '0812'")
        company = cur.fetchone()
        assert company is not None

        # disclosureが入っている
        cur = mem_conn.execute("SELECT COUNT(*) FROM disclosures")
        assert cur.fetchone()[0] == 1

        # periodが入っている
        cur = mem_conn.execute("SELECT * FROM periods")
        period = cur.fetchone()
        assert period is not None

        # factsが入っている
        cur = mem_conn.execute("SELECT COUNT(*) FROM facts")
        assert cur.fetchone()[0] == 3

    def test_company_name_updated(self, mem_conn):
        """同一ticker_codeで異なるname_jaで投入 → 更新される"""
        data1 = _sample_json(company_name="旧名称")
        load_single(mem_conn, data1)
        mem_conn.commit()

        data2 = _sample_json(
            company_name="新名称",
            disclosed_at="2025-05-15T15:00:00+09:00",
            sha256="different_hash",
        )
        load_single(mem_conn, data2)
        mem_conn.commit()

        cur = mem_conn.execute(
            "SELECT name_ja FROM companies WHERE ticker_code = '0812'"
        )
        assert cur.fetchone()[0] == "新名称"

    def test_quarter_4_is_full_year(self, mem_conn):
        """quarter=4 → is_full_year=1"""
        data = _sample_json(quarter=4)
        load_single(mem_conn, data)
        mem_conn.commit()

        cur = mem_conn.execute(
            "SELECT is_full_year FROM periods WHERE quarter = 4"
        )
        assert cur.fetchone()[0] == 1

    def test_quarter_3_not_full_year(self, mem_conn):
        """quarter=3 → is_full_year=0"""
        data = _sample_json(quarter=3)
        load_single(mem_conn, data)
        mem_conn.commit()

        cur = mem_conn.execute(
            "SELECT is_full_year FROM periods WHERE quarter = 3"
        )
        assert cur.fetchone()[0] == 0


# ============================================================
# T3: 円整数変換テスト
# ============================================================

class TestJpyConversion:
    """source_unitごとの円整数変換"""

    def test_million_yen(self):
        """百万円 → 円"""
        assert _convert_to_jpy(4915, "百万円") == 4_915_000_000

    def test_thousand_yen(self):
        """千円 → 円"""
        assert _convert_to_jpy(12345, "千円") == 12_345_000

    def test_oku_yen(self):
        """億円 → 円"""
        assert _convert_to_jpy(10, "億円") == 1_000_000_000

    def test_yen(self):
        """円 → 円（そのまま）"""
        assert _convert_to_jpy(98765000, "円") == 98_765_000

    def test_values_stored_as_jpy(self, mem_conn):
        """DB投入時に百万円が円整数に変換されている"""
        data = _sample_json(sales=4915, source_unit="百万円")
        load_single(mem_conn, data)
        mem_conn.commit()

        cur = mem_conn.execute(
            "SELECT value FROM facts WHERE metric = 'NET_SALES'"
        )
        assert cur.fetchone()[0] == 4_915_000_000


# ============================================================
# T4: 冪等性テスト（二重投入）
# ============================================================

class TestIdempotency:
    """同一JSONを2回投入してもfactsが増えない"""

    def test_double_insert_no_duplicate(self, mem_conn):
        """同一JSONを2回投入 → factsは3行のまま"""
        data = _sample_json()

        result1 = load_single(mem_conn, data)
        mem_conn.commit()
        assert result1["status"] == "ok"
        assert result1["inserted"] == 3

        result2 = load_single(mem_conn, data)
        mem_conn.commit()
        assert result2["status"] == "skipped"
        assert result2["skipped"] == 3
        assert result2["inserted"] == 0

        # factsは3行だけ
        cur = mem_conn.execute("SELECT COUNT(*) FROM facts")
        assert cur.fetchone()[0] == 3

    def test_same_sha256_reuses_disclosure(self, mem_conn):
        """同一sha256で投入 → disclosureは1行のまま"""
        data = _sample_json(sha256="same_hash")
        load_single(mem_conn, data)
        load_single(mem_conn, data)
        mem_conn.commit()

        cur = mem_conn.execute("SELECT COUNT(*) FROM disclosures")
        assert cur.fetchone()[0] == 1


# ============================================================
# T5: 修正開示テスト（上書きなし・追加）
# ============================================================

class TestCorrectionDisclosure:
    """修正開示は別disclosureとして追加され、factsも別行になる"""

    def test_correction_adds_new_facts(self, mem_conn):
        """異なるdisclosure（修正開示）→ factsが追加される"""
        # 初回開示
        data1 = _sample_json(
            sha256="original_hash",
            disclosed_at="2025-02-14T15:00:00+09:00",
            sales=4915,
        )
        result1 = load_single(mem_conn, data1)
        mem_conn.commit()
        assert result1["inserted"] == 3

        # 修正開示（異なるsha256 → 新しいdisclosure）
        data2 = _sample_json(
            sha256="correction_hash",
            disclosed_at="2025-02-20T10:00:00+09:00",
            title="2025年3月期 第3四半期決算短信（訂正）",
            sales=5000,
        )
        result2 = load_single(mem_conn, data2)
        mem_conn.commit()

        # 2つのdisclosureが存在
        cur = mem_conn.execute("SELECT COUNT(*) FROM disclosures")
        assert cur.fetchone()[0] == 2

        # factsは6行（3 + 3）
        cur = mem_conn.execute("SELECT COUNT(*) FROM facts")
        assert cur.fetchone()[0] == 6


# ============================================================
# T6: v_latest_facts ビューテスト
# ============================================================

class TestLatestFactsView:
    """v_latest_factsが最新のdisclosureのfactsだけ返す"""

    def test_view_returns_latest(self, mem_conn):
        """複数disclosure → v_latest_factsは最新値のみ"""
        # 初回: sales=4915百万円 → 4,915,000,000円
        data1 = _sample_json(
            sha256="hash_v1",
            disclosed_at="2025-02-14T15:00:00+09:00",
            sales=4915,
        )
        load_single(mem_conn, data1)
        mem_conn.commit()

        # 訂正: sales=5000百万円 → 5,000,000,000円
        data2 = _sample_json(
            sha256="hash_v2",
            disclosed_at="2025-02-20T10:00:00+09:00",
            title="2025年3月期 第3四半期決算短信（訂正）",
            sales=5000,
        )
        load_single(mem_conn, data2)
        mem_conn.commit()

        # v_latest_factsで最新値を取得
        cur = mem_conn.execute(
            "SELECT value FROM v_latest_facts WHERE metric = 'NET_SALES'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 5_000_000_000  # 訂正後の値


# ============================================================
# T7: メトリクスマッピングテスト
# ============================================================

class TestMetricMapping:
    """メトリクスキーが正しくマッピングされる"""

    def test_mapping(self):
        assert METRIC_MAP["sales"] == "NET_SALES"
        assert METRIC_MAP["gross_profit"] == "GROSS_PROFIT"
        assert METRIC_MAP["op_income"] == "OP_INCOME"
        assert METRIC_MAP["ordinary_income"] == "ORDINARY_INCOME"

    def test_all_metrics_stored(self, mem_conn):
        """全メトリクスがfactsに格納される"""
        data = _sample_json(
            sales=100, gross_profit=30, op_income=20, ordinary_income=10,
            source_unit="円",
        )
        load_single(mem_conn, data)
        mem_conn.commit()

        cur = mem_conn.execute(
            "SELECT metric FROM facts ORDER BY metric"
        )
        metrics = {row[0] for row in cur.fetchall()}
        assert metrics == {"NET_SALES", "GROSS_PROFIT", "OP_INCOME", "ORDINARY_INCOME"}


# ============================================================
# T8: バッチ（ファイル/ディレクトリ）投入テスト
# ============================================================

class TestBatchLoad:
    """ファイル/ディレクトリからの一括投入"""

    def test_load_single_file(self, tmp_db, tmp_path):
        """単一JSONファイルの投入"""
        json_path = str(tmp_path / "test.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(_sample_json(), f, ensure_ascii=False)

        summary = load_from_path(tmp_db, json_path)
        assert summary["processed"] == 1
        assert summary["inserted"] == 3
        assert summary["errors"] == 0

    def test_load_directory(self, tmp_db, tmp_path):
        """ディレクトリ内の複数JSONファイルの投入"""
        input_dir = tmp_path / "input"
        input_dir.mkdir()

        for i in range(3):
            with open(input_dir / f"data_{i}.json", "w", encoding="utf-8") as f:
                json.dump(
                    _sample_json(
                        ticker=f"000{i}",
                        sha256=f"hash_{i}",
                        disclosed_at=f"2025-02-{14+i}T15:00:00+09:00",
                    ),
                    f,
                    ensure_ascii=False,
                )

        summary = load_from_path(tmp_db, str(input_dir))
        assert summary["processed"] == 3
        assert summary["inserted"] == 9  # 3ファイル × 3メトリクス
        assert summary["errors"] == 0

    def test_load_list_json(self, tmp_db, tmp_path):
        """リスト形式のJSON（複数開示を1ファイル）"""
        json_path = str(tmp_path / "multi.json")
        items = [
            _sample_json(ticker="0001", sha256="h1"),
            _sample_json(ticker="0002", sha256="h2"),
        ]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)

        summary = load_from_path(tmp_db, json_path)
        assert summary["processed"] == 2
        assert summary["inserted"] == 6  # 2社 × 3メトリクス


# ============================================================
# T9: バリデーションエラーテスト
# ============================================================

class TestValidation:
    """不正なJSONデータのエラーハンドリング"""

    def test_empty_ticker(self, mem_conn):
        data = _sample_json(ticker="")
        result = load_single(mem_conn, data)
        assert result["status"] == "error"

    def test_invalid_quarter(self, mem_conn):
        data = _sample_json(quarter=5)
        result = load_single(mem_conn, data)
        assert result["status"] == "error"

    def test_empty_values(self, mem_conn):
        data = _sample_json(sales=None, gross_profit=None, op_income=None)
        result = load_single(mem_conn, data)
        assert result["status"] == "error"


# ============================================================
# T10: guidance投入テスト
# ============================================================

class TestGuidance:
    """metric_type='guidance' の投入"""

    def test_guidance_inserted(self, mem_conn):
        data = _sample_json(metric_type="guidance", sales=5500, source_unit="百万円")
        result = load_single(mem_conn, data)
        mem_conn.commit()

        assert result["status"] == "ok"

        cur = mem_conn.execute("SELECT COUNT(*) FROM guidance")
        count = cur.fetchone()[0]
        assert count >= 1

        cur = mem_conn.execute(
            "SELECT value FROM guidance WHERE metric = 'NET_SALES'"
        )
        assert cur.fetchone()[0] == 5_500_000_000
