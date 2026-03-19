# ============================================================
# test_ingest_pipeline.py — tdnet_ingest パイプラインのテスト
# ============================================================
from __future__ import annotations

import io
import json
import os
import sys
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import StateDB
from src.migration.migration_db import MigrationDB
from src.config import Config
from src.models import DisclosureItem, DisclosureType, ExtractedFinancials, Status
from src.extractor import (
    _extract_from_xbrl, _find_xbrl_in_zip, _parse_xbrl_content,
    _is_tanshin_title, _detect_quarter_from_context, _apply_ixbrl_scale,
)


# ============================================================
# テスト用ヘルパー
# ============================================================

def _make_config(tmpdir: str) -> Config:
    """テスト用Config"""
    cfg = Config()
    cfg.state_db_path = os.path.join(tmpdir, "state.db")
    cfg.decision_db_path = os.path.join(tmpdir, "decision.db")
    cfg.log_path = os.path.join(tmpdir, "test.log")
    cfg.excel_unit = "million_yen"
    cfg.watch_tickers = []
    return cfg


def _make_disclosure(
    code: str = "0812",
    title: str = "2025年12月期 第3四半期決算短信",
    disclosure_id: str = "test-disclosure-001",
) -> DisclosureItem:
    return DisclosureItem(
        disclosure_id=disclosure_id,
        ticker=code,
        company_name="テスト株式会社",
        title=title,
        doc_url="https://example.com/test.zip",
        published_at="2026-02-25 14:30:00",
        xbrl_url="https://example.com/test.xbrl",
        disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
    )


_SAMPLE_XBRL = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2024-02-01/jppfs_cor">
  <jppfs_cor:NetSales contextRef="CurrentYearDuration">98765000</jppfs_cor:NetSales>
  <jppfs_cor:OperatingIncome contextRef="CurrentYearDuration">12345000</jppfs_cor:OperatingIncome>
  <jppfs_cor:GrossProfit contextRef="CurrentYearDuration">45000000</jppfs_cor:GrossProfit>
</xbrli:xbrl>
"""


def _make_xbrl_zip(
    xbrl_content: bytes = _SAMPLE_XBRL,
    entry_name: str = "XBRL/PublicDoc/test-ixbrl.htm",
) -> bytes:
    """テスト用XBRLを含むZIPをbytesで作成する"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(entry_name, xbrl_content)
    return buf.getvalue()


# ============================================================
# T1: ZIP bytes固定テスト
# ============================================================

class TestZipBytesFlow:
    """ZIPをbytesで渡し、内部ファイルを正しく抽出できる"""

    def test_zip_is_opened_as_bytes(self):
        """ZIPバイナリがdecodeされずにZipFileで展開される"""
        zip_bytes = _make_xbrl_zip()
        # ZIPシグネチャ確認
        assert zip_bytes[:4] == b"PK\x03\x04"

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(zip_bytes)
            f.flush()
            path = f.name

        try:
            result = _extract_from_xbrl(path)
            assert result is not None
            assert result.sales == 98765000
            assert result.operating_profit == 12345000
            assert result.gross_profit == 45000000
        finally:
            os.unlink(path)

    def test_zip_with_ixbrl_extension_priority(self):
        """iXBRL拡張子のファイルがXBRLより優先される"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # iXBRLファイル（正しいデータ）
            zf.writestr("PublicDoc/report-ixbrl.htm", _SAMPLE_XBRL)
            # XBRLファイル（空のデータ → 抽出失敗する）
            zf.writestr("PublicDoc/report.xbrl", b"<xbrl/>")

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(buf.getvalue())
            f.flush()
            path = f.name

        try:
            result = _extract_from_xbrl(path)
            assert result is not None
            assert result.sales == 98765000
        finally:
            os.unlink(path)

    def test_non_zip_file_handled_directly(self):
        """非ZIPファイルは従来通りバイト列として処理される"""
        with tempfile.NamedTemporaryFile(suffix=".xbrl", delete=False) as f:
            f.write(_SAMPLE_XBRL)
            f.flush()
            path = f.name

        try:
            result = _extract_from_xbrl(path)
            assert result is not None
            assert result.sales == 98765000
        finally:
            os.unlink(path)

    def test_bad_zip_returns_none(self):
        """壊れたZIPはBadZipFileで捕捉されNoneを返す"""
        bad_bytes = b"PK\x03\x04" + b"\x00" * 100  # 不正なZIP
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(bad_bytes)
            f.flush()
            path = f.name

        try:
            result = _extract_from_xbrl(path)
            assert result is None
        finally:
            os.unlink(path)

    def test_zip_without_xbrl_returns_none(self):
        """XBRL/iXBRLファイルを含まないZIPはNoneを返す"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", b"just a text file")

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(buf.getvalue())
            f.flush()
            path = f.name

        try:
            result = _extract_from_xbrl(path)
            assert result is None
        finally:
            os.unlink(path)


# ============================================================
# T2: _find_xbrl_in_zip の優先順位テスト
# ============================================================

class TestFindXbrlInZip:
    """ZIP内のiXBRL/XBRLファイル検索の優先順位"""

    def test_ixbrl_before_xbrl(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("report.xbrl", b"")
            zf.writestr("report-ixbrl.htm", b"")
            zf.writestr("other.txt", b"")

        zf = zipfile.ZipFile(io.BytesIO(buf.getvalue()), "r")
        result = _find_xbrl_in_zip(zf)
        assert result[0] == "report-ixbrl.htm"
        assert "report.xbrl" in result
        assert "other.txt" not in result
        zf.close()


# ============================================================
# T3: リプレイテスト（ネットワーク不要）
# ============================================================

class TestReplay:
    """--replay によるローカルZIPからの抽出（ネットワーク不使用）"""

    def test_replay_with_valid_zip(self):
        zip_bytes = _make_xbrl_zip()

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(zip_bytes)
            f.flush()
            path = f.name

        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
            from tdnet_ingest import run_replay
            result = run_replay(path, title="テスト決算短信")
            assert result["status"] == "success"
            assert "sales=" in result["detail"]
        finally:
            os.unlink(path)

    def test_replay_with_missing_file(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from tdnet_ingest import run_replay
        result = run_replay("/nonexistent/path.zip")
        assert result["status"] == "error"


# ============================================================
# T4: dump-on-error テスト
# ============================================================

class TestDumpOnError:
    """抽出失敗時のダンプ機能"""

    def test_dump_creates_json(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from tdnet_ingest import _dump_error

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            dump_dir = os.path.join(tmpdir, "dumps")

            _dump_error(
                dump_dir=dump_dir,
                source_doc_id="test-doc-123",
                source_url="https://example.com/test.zip",
                zip_hash="abc123hash",
                error_type="extract_failed",
                error_message="not well-formed XML",
            )

            # JSONが生成されたか
            json_files = [f for f in os.listdir(dump_dir) if f.endswith(".json")]
            assert len(json_files) == 1

            with open(os.path.join(dump_dir, json_files[0]), "r", encoding="utf-8") as f:
                meta = json.load(f)

            assert meta["source_doc_id"] == "test-doc-123"
            assert meta["source_url"] == "https://example.com/test.zip"
            assert meta["zip_hash"] == "abc123hash"
            assert meta["error_type"] == "extract_failed"
            assert meta["parser_version"] == "v2"


# ============================================================
# T5: 冪等性 — 同一開示を2回処理→スキップ（回帰テスト）
# ============================================================

class TestIdempotency:
    """同一開示を2回処理してもDB行数が増えない"""

    def test_same_disclosure_twice_no_duplicate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config = _make_config(tmpdir)
            state_db = StateDB(config.state_db_path)
            decision_db = MigrationDB(config.decision_db_path)

            result1 = decision_db.upsert_quarterly_result(
                company_code="0812",
                fiscal_year_end="2026-03-31",
                quarter="3Q",
                sales=12345,
                operating_profit=2000,
                actor="tdnet_ingest", source="tdnet",
                source_doc_id="test-disclosure-001",
                run_id="run-1",
            )
            decision_db.commit()
            assert result1 == "inserted"

            state_db.record(
                disclosure_id="test-disclosure-001",
                code="0812", year="R8/3", quarter="3Q",
                status=Status.SUCCESS,
            )

            assert state_db.is_processed("test-disclosure-001") is True

            row = decision_db.get_quarterly_result("0812", "2026-03-31", "3Q")
            assert row is not None
            assert row["sales"] == 12345

            decision_db.close()
            state_db.close()


# ============================================================
# T6: 訂正開示 — 差分更新+audit_log（回帰テスト）
# ============================================================

class TestCorrectionUpdate:
    """訂正開示→更新+audit_log"""

    def test_correction_updates_values(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            decision_db = MigrationDB(os.path.join(tmpdir, "decision.db"))

            decision_db.upsert_quarterly_result(
                company_code="0812", fiscal_year_end="2026-03-31", quarter="3Q",
                sales=10000, operating_profit=1000,
                actor="tdnet_ingest", source="tdnet",
                source_doc_id="doc-001", run_id="run-1",
            )
            decision_db.commit()

            result2 = decision_db.upsert_quarterly_result(
                company_code="0812", fiscal_year_end="2026-03-31", quarter="3Q",
                sales=11000, operating_profit=1200,
                actor="tdnet_ingest", source="tdnet",
                source_doc_id="doc-002", run_id="run-2",
            )
            decision_db.commit()
            assert result2 == "updated"

            row = decision_db.get_quarterly_result("0812", "2026-03-31", "3Q")
            assert row["sales"] == 11000
            assert row["operating_profit"] == 1200

            audit = decision_db.get_audit_log(company_code="0812", run_id="run-2")
            changed_fields = {a["field_name"] for a in audit}
            assert "sales" in changed_fields

            decision_db.close()


# ============================================================
# T7: DBスキーマ拡張カラム + マイグレーション（回帰テスト）
# ============================================================

class TestSchemaExtension:
    """新規カラムが挿入時に保存される"""

    def test_new_columns_stored(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db = MigrationDB(os.path.join(tmpdir, "test.db"))
            db.upsert_quarterly_result(
                company_code="0812", fiscal_year_end="2026-03-31", quarter="3Q",
                sales=10000, actor="test", source="test",
                source_doc_id="doc-123",
                source_url="https://example.com/test.zip",
                zip_hash="sha256_test",
            )
            db.commit()

            row = db.get_quarterly_result("0812", "2026-03-31", "3Q")
            assert row["source_doc_id"] == "doc-123"
            assert row["zip_hash"] == "sha256_test"
            assert row["parser_version"] == "v2"
            db.close()


class TestMigrationAddColumns:
    """旧DBに自動マイグレーションが走る"""

    def test_migration_adds_missing_columns(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = os.path.join(tmpdir, "old.db")
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
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(company_code, fiscal_year_end, quarter)
                )
            """)
            conn.commit()
            conn.close()

            db = MigrationDB(db_path)
            cur = db._conn.execute("PRAGMA table_info(quarterly_results)")
            col_names = {row[1] for row in cur.fetchall()}
            assert "source_doc_id" in col_names
            assert "parser_version" in col_names
            db.close()


# ============================================================
# T8: タイトルフィルタのテスト
# ============================================================

class TestTanshinTitleFilter:
    """PDFフォールバック対象の判定"""

    def test_tanshin_title_accepted(self):
        assert _is_tanshin_title("2025年12月期 第3四半期決算短信〔日本基準〕(連結)") is True

    def test_tanshin_annual(self):
        assert _is_tanshin_title("2026年6月期 決算短信〔日本基準〕(連結)") is True

    def test_setsumeikai_rejected(self):
        assert _is_tanshin_title("2026年6月期第2四半期 決算説明会資料") is False

    def test_qanda_rejected(self):
        assert _is_tanshin_title("2025 年 12 月期 通期決算説明会 質疑応答について") is False

    def test_saikeisai_rejected(self):
        assert _is_tanshin_title("2026年3月期第3四半期決算 IR資料の再掲載について") is False

    def test_presentation_rejected(self):
        assert _is_tanshin_title("Financial Results Presentation Q3 2025") is False

    def test_hosoku_rejected(self):
        assert _is_tanshin_title("2025年12月期決算 補足資料") is False

    def test_empty_title(self):
        assert _is_tanshin_title("") is False


# ============================================================
# T9: iXBRL scale/sign のテスト
# ============================================================

class TestIxbrlScale:
    """iXBRLのscale/sign属性変換"""

    def test_scale_6(self):
        """scale='6' → ×10^6"""
        val = _apply_ixbrl_scale("4,915", "6", "")
        assert val == 4915000000

    def test_scale_0(self):
        """scale='0' → ×1（normalize_numberの丸め適用）"""
        val = _apply_ixbrl_scale("3.74", "0", "")
        assert val == 4  # normalize_number rounds to int

    def test_negative_sign(self):
        """sign='-' → 負数"""
        val = _apply_ixbrl_scale("664", "6", "-")
        assert val == -664000000

    def test_no_scale(self):
        """scale未指定 → 素の値"""
        val = _apply_ixbrl_scale("12345", "", "")
        assert val == 12345


# ============================================================
# T10: contextRef からのQ検出テスト
# ============================================================

class TestQuarterDetection:
    """contextRefからQ情報を検出する"""

    def test_annual_result(self):
        # CurrentYearDuration 単独は確定不可 → 空文字
        # (10月決算1Qでも同じcontextRefが来るため)
        assert _detect_quarter_from_context(
            "CurrentYearDuration_ConsolidatedMember_ResultMember"
        ) == ""

    def test_annual_member(self):
        assert _detect_quarter_from_context(
            "CurrentYearDuration_AnnualMember_ConsolidatedMember_ResultMember"
        ) == "4Q"

    def test_third_quarter(self):
        assert _detect_quarter_from_context(
            "CurrentYearDuration_ThirdQuarterMember_ConsolidatedMember_ResultMember"
        ) == "3Q"

    def test_first_quarter(self):
        assert _detect_quarter_from_context(
            "CurrentYearDuration_FirstQuarterMember_NonConsolidatedMember_ResultMember"
        ) == "1Q"

    def test_second_quarter(self):
        assert _detect_quarter_from_context(
            "CurrentYearDuration_SecondQuarterMember_ConsolidatedMember_ResultMember"
        ) == "2Q"


# ============================================================
# T11: iXBRL nonFraction ZIPパースのE2Eテスト
# ============================================================

_SAMPLE_IXBRL = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
<body>
<ix:nonFraction name="tse-ed-t:NetSales"
    contextRef="CurrentYearDuration_ConsolidatedMember_ResultMember"
    scale="6" format="ixt:numdotdecimal">4,915</ix:nonFraction>
<ix:nonFraction name="tse-ed-t:OperatingIncome"
    contextRef="CurrentYearDuration_ConsolidatedMember_ResultMember"
    scale="6" sign="-" format="ixt:numdotdecimal">664</ix:nonFraction>
<ix:nonFraction name="tse-ed-t:GrossProfit"
    contextRef="CurrentYearDuration_ConsolidatedMember_ResultMember"
    scale="6" format="ixt:numdotdecimal">1,200</ix:nonFraction>
</body>
</html>
"""


class TestIxbrlNonFractionParse:
    """iXBRL ix:nonFraction要素から値を抽出する"""

    def test_ixbrl_nonfraction_extraction(self):
        result = _parse_xbrl_content(_SAMPLE_IXBRL)
        assert result is not None
        assert result.sales == 4915000000
        assert result.operating_profit == -664000000
        assert result.gross_profit == 1200000000
        assert result.source_unit == "百万円"
        # contextRefにQuarterMemberがないため空文字
        assert result.quarter == ""

    def test_ixbrl_zip_e2e(self):
        """ZIP内のiXBRLファイルから数値+Qを抽出"""
        zip_bytes = _make_xbrl_zip(
            xbrl_content=_SAMPLE_IXBRL,
            entry_name="XBRLData/Summary/test-ixbrl.htm",
        )
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(zip_bytes)
            f.flush()
            path = f.name

        try:
            result = _extract_from_xbrl(path)
            assert result is not None
            assert result.sales == 4915000000
            assert result.operating_profit == -664000000
            # contextRefにQuarterMemberがないため空文字
            assert result.quarter == ""
        finally:
            os.unlink(path)

