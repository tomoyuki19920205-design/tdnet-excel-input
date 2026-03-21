#!/usr/bin/env python3
"""tests/test_ir_extraction.py — IR文書抽出パイプラインのテスト"""
import json
import os
import sqlite3
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from src.extraction.ir_doc_schema import (
    ensure_tables,
    insert_document,
    insert_facts,
)
from tools.classify_documents import classify_doc_type, classify_file_type
from tools.extract_html import (
    extract_facts_from_html,
    detect_unit,
    match_metric_name,
    parse_number,
)
from tools.normalize_extracted_facts import (
    normalize_metric_name,
    normalize_unit,
    normalize_quarter,
    determine_confidence,
    normalize_facts,
)


# ============================================================
# DB Schema Tests
# ============================================================

class TestIrDocSchema:

    def test_ensure_tables(self):
        """テーブルとインデックスが作成される"""
        conn = sqlite3.connect(":memory:")
        ensure_tables(conn)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "documents" in tables
        assert "extracted_facts" in tables
        conn.close()

    def test_insert_document(self):
        """document が挿入され、IDが返る"""
        conn = sqlite3.connect(":memory:")
        ensure_tables(conn)
        doc_id = insert_document(
            conn, ticker="4062", pubdate="2027-01-01",
            title="決算短信", doc_type="earnings", file_type="pdf",
            url="https://example.com/4062.pdf",
        )
        assert doc_id is not None
        assert doc_id > 0
        conn.close()

    def test_insert_document_duplicate(self):
        """重複は既存IDを返す"""
        conn = sqlite3.connect(":memory:")
        ensure_tables(conn)
        id1 = insert_document(
            conn, ticker="4062", pubdate="2027-01-01",
            title="決算短信", doc_type="earnings", file_type="pdf",
            url="https://example.com/4062.pdf",
        )
        id2 = insert_document(
            conn, ticker="4062", pubdate="2027-01-01",
            title="決算短信", doc_type="earnings", file_type="pdf",
            url="https://example.com/4062.pdf",
        )
        assert id1 == id2
        conn.close()

    def test_insert_facts(self):
        """facts が挿入される (persist_policy ON が必要)"""
        from src.persist_policy import init_persist_policy, reset_persist_policy
        init_persist_policy(cli_flag=True)
        try:
            conn = sqlite3.connect(":memory:")
            ensure_tables(conn)
            facts = [
                {
                    "document_id": 1, "ticker": "4062", "period": "2027-03-31",
                    "quarter": "3Q", "metric_name": "sales", "metric_value": 100000,
                    "unit": "円", "segment_name": "", "source_type": "html",
                    "confidence": "high",
                },
            ]
            inserted = insert_facts(conn, facts)
            assert inserted == 1
            conn.close()
        finally:
            reset_persist_policy()

    def test_insert_facts_duplicate_skipped(self):
        """重複 facts はスキップ"""
        conn = sqlite3.connect(":memory:")
        ensure_tables(conn)
        fact = {
            "document_id": 1, "ticker": "4062", "period": "2027-03-31",
            "quarter": "3Q", "metric_name": "sales", "metric_value": 100000,
            "unit": "円", "segment_name": "", "source_type": "html",
            "confidence": "high",
        }
        insert_facts(conn, [fact])
        inserted2 = insert_facts(conn, [fact])
        assert inserted2 == 0  # 重複スキップ
        conn.close()


# ============================================================
# Classifier Tests
# ============================================================

class TestClassifier:

    @pytest.mark.parametrize("title,expected", [
        ("2027年3月期 第3四半期決算短信", "earnings"),
        ("業績予想の修正に関するお知らせ", "forecast_revision"),
        ("2027年1月 月次売上速報", "monthly"),
        ("2027年3月期 決算説明資料", "presentation"),
        ("2027年3月期 決算説明会資料", "presentation"),
        ("2027年3月期 補足資料", "supplement"),
        ("KPI一覧", "kpi"),
        ("セグメント別業績", "segment"),
        ("株主優待のご案内", "other"),
    ])
    def test_classify_doc_type(self, title, expected):
        assert classify_doc_type(title) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://example.com/140120260304575305.pdf", "pdf"),
        ("https://example.com/doc.html", "html"),
        ("https://example.com/doc.htm", "html"),
        ("https://example.com/doc.xbrl", "xbrl"),
        ("https://example.com/doc.zip", "xbrl"),
        ("https://example.com/doc.xlsx", "other"),
        ("", "other"),
    ])
    def test_classify_file_type(self, url, expected):
        assert classify_file_type(url) == expected


# ============================================================
# HTML Extraction Tests
# ============================================================

class TestHtmlExtraction:

    _SAMPLE_HTML = """
    <html><body>
    <p>（単位：百万円）</p>
    <table>
      <tr><th>項目</th><th>2026年度</th><th>2027年度</th></tr>
      <tr><td>売上高</td><td>1,234</td><td>1,567</td></tr>
      <tr><td>営業利益</td><td>100</td><td>150</td></tr>
      <tr><td>経常利益</td><td>110</td><td>160</td></tr>
      <tr><td>当期純利益</td><td>80</td><td>120</td></tr>
    </table>
    </body></html>
    """

    def test_html_table_extraction(self):
        """HTML テーブルから facts が抽出される"""
        facts = extract_facts_from_html(
            self._SAMPLE_HTML, ticker="4062", period="2027-03-31", quarter="3Q",
        )
        assert len(facts) > 0
        metrics = {f["metric_name"] for f in facts}
        assert "sales" in metrics
        assert "operating_profit" in metrics

    def test_html_unit_conversion(self):
        """百万円 → 円 変換"""
        facts = extract_facts_from_html(
            self._SAMPLE_HTML, ticker="4062",
        )
        sales_facts = [f for f in facts if f["metric_name"] == "sales"]
        # 1,234 百万円 = 1,234,000,000 円
        assert any(f["metric_value"] == 1_234_000_000 for f in sales_facts)

    def test_html_empty(self):
        """空のHTMLには空リスト"""
        facts = extract_facts_from_html("<html></html>", ticker="4062")
        assert facts == []


# ============================================================
# PDF Extraction Tests
# ============================================================

class TestPdfExtraction:

    @pytest.fixture
    def has_pdfplumber(self):
        try:
            import pdfplumber
            return True
        except ImportError:
            return False

    def test_pdf_requires_pdfplumber(self, has_pdfplumber):
        """pdfplumber が無い場合はスキップ"""
        if not has_pdfplumber:
            pytest.skip("pdfplumber not installed")

        from tools.extract_pdf import extract_facts_from_pdf
        # 存在しないファイルでも例外にならない
        facts = extract_facts_from_pdf("/nonexistent.pdf", ticker="4062")
        assert facts == []


# ============================================================
# Normalization Tests
# ============================================================

class TestNormalization:

    def test_normalize_metric_name(self):
        assert normalize_metric_name("売上高") == "sales"
        assert normalize_metric_name("営業利益") == "operating_profit"
        assert normalize_metric_name("当期純利益") == "net_income"
        assert normalize_metric_name("受注残高") == "order_backlog"
        assert normalize_metric_name("不明なラベル") == "不明なラベル"

    def test_normalize_unit_millions(self):
        unit, val = normalize_unit("百万円", 100)
        assert unit == "円"
        assert val == 100_000_000

    def test_normalize_unit_billions(self):
        unit, val = normalize_unit("億円", 10)
        assert unit == "円"
        assert val == 1_000_000_000

    def test_normalize_unit_thousands(self):
        unit, val = normalize_unit("千円", 500)
        assert unit == "円"
        assert val == 500_000

    def test_normalize_unit_percent(self):
        unit, val = normalize_unit("%", 15.5)
        assert unit == "%"
        assert val == 15.5

    def test_normalize_quarter(self):
        assert normalize_quarter("第1四半期") == "1Q"
        assert normalize_quarter("3Q") == "3Q"
        assert normalize_quarter("通期") == "4Q"
        assert normalize_quarter("中間") == "2Q"

    def test_confidence_xbrl_high(self):
        assert determine_confidence("xbrl", "sales") == "high"
        assert determine_confidence("xbrl", "orders") == "high"

    def test_confidence_html_by_metric(self):
        assert determine_confidence("html", "sales") == "high"
        assert determine_confidence("html", "orders") == "medium"

    def test_confidence_pdf_low(self):
        assert determine_confidence("pdf", "orders") == "low"
        assert determine_confidence("pdf", "sales", has_table_title=True) == "medium"

    def test_normalize_facts_full(self):
        """normalize_facts が一括正規化できる"""
        facts = [{
            "ticker": "40620",
            "period": "2027-03-31",
            "quarter": "第3四半期",
            "metric_name": "",
            "raw_label": "売上高",
            "source_type": "html",
            "table_title": "連結業績",
        }]
        result = normalize_facts(facts)
        assert result[0]["ticker"] == "4062"
        assert result[0]["quarter"] == "3Q"
        assert result[0]["metric_name"] == "sales"
        assert result[0]["confidence"] == "high"  # html + sales


# ============================================================
# Utility Tests
# ============================================================

class TestUtilities:

    def test_detect_unit(self):
        assert detect_unit("（単位：百万円）")[0] == "百万円"
        assert detect_unit("（百万円）")[0] == "百万円"
        assert detect_unit("no unit") == ("円", 1)

    def test_match_metric_name(self):
        assert match_metric_name("売上高") == "sales"
        assert match_metric_name("営業利益") == "operating_profit"
        assert match_metric_name("不明") is None

    def test_parse_number(self):
        assert parse_number("1,234") == 1234
        assert parse_number("△100") == -100
        assert parse_number("▲50") == -50
        assert parse_number("-") is None
        assert parse_number("") is None
        assert parse_number("12.5") == 12.5
