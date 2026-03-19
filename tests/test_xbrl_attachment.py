"""Tests for XBRL Attachment PL complement logic."""
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extractor import (
    _extract_from_xbrl,
    _is_summary_file,
    _is_pl_attachment,
    _parse_xbrl_content,
)


# ─── Helper: iXBRL content builders ──────────────────────────────

def _make_ixbrl(tags: dict[str, dict]) -> bytes:
    """Create minimal iXBRL content with given tag→value mappings.
    
    tags: {concept_local: {"value": str, "context": str, "scale": str}}
    """
    parts = ['<html xmlns:ix="http://www.xbrl.org/2008/inlineXBRL">']
    for concept, info in tags.items():
        ctx = info.get("context", "CurrentYearDuration_ConsolidatedMember_ResultMember")
        val = info.get("value", "100")
        scale = info.get("scale", "6")
        sign = info.get("sign", "")
        sign_attr = f' sign="{sign}"' if sign else ""
        parts.append(
            f'<ix:nonFraction name="jppfs_cor:{concept}" '
            f'contextRef="{ctx}" scale="{scale}"{sign_attr}>{val}</ix:nonFraction>'
        )
    parts.append('</html>')
    return "\n".join(parts).encode("utf-8")


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Create in-memory ZIP with given filename→content mappings."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ─── Summary/Attachment file detection ────────────────────────────

class TestFileDetection:
    def test_summary_file(self):
        assert _is_summary_file("XBRLData/Summary/tse-qnedjpsm-23010-ixbrl.htm") is True
        assert _is_summary_file("XBRLData/Attachment/0102010-qnpl12-ixbrl.htm") is False

    def test_pl_attachment(self):
        assert _is_pl_attachment("XBRLData/Attachment/0102010-qnpl12-ixbrl.htm") is True
        assert _is_pl_attachment("XBRLData/Attachment/0600000-scpl15-ixbrl.htm") is True
        assert _is_pl_attachment("XBRLData/Attachment/0600000-qcpl11-ixbrl.htm") is True
        assert _is_pl_attachment("XBRLData/Summary/tse-qnedjpsm-23010-ixbrl.htm") is False
        assert _is_pl_attachment("XBRLData/Attachment/0101010-qnbs02-ixbrl.htm") is False


# ─── Core: Attachment complement tests ───────────────────────────

class TestAttachmentComplement:
    """Summary → Attachment/PL 補完のテスト"""

    def test_summary_only_sales_op_complement_gp_from_attachment(self):
        """Summary で sales/op のみ取得、Attachment で gp を補完"""
        summary_content = _make_ixbrl({
            "NetSales": {"value": "1,368", "scale": "6"},
            "OperatingIncome": {"value": "692", "scale": "6", "sign": "-"},
        })
        pl_content = _make_ixbrl({
            "NetSales": {"value": "1,368,000", "scale": "3",
                         "context": "CurrentYTDDuration"},
            "GrossProfit": {"value": "500,000", "scale": "3",
                           "context": "CurrentYTDDuration"},
            "CostOfSales": {"value": "868,000", "scale": "3",
                           "context": "CurrentYTDDuration"},
            "OperatingIncome": {"value": "692,000", "scale": "3",
                              "context": "CurrentYTDDuration", "sign": "-"},
        })

        zip_bytes = _make_zip({
            "XBRLData/Summary/tse-qnedjpsm-23010-ixbrl.htm": summary_content,
            "XBRLData/Attachment/0102010-qnpl12-tse-23010-ixbrl.htm": pl_content,
        })

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(zip_bytes)
            tmp_path = f.name

        try:
            result = _extract_from_xbrl(tmp_path)
            assert result is not None
            # Summary 値を優先（上書きしない）
            assert result.sales == 1_368_000_000  # from Summary (1,368 * 10^6)
            assert result.operating_profit == -692_000_000  # from Summary
            # Attachment から補完
            assert result.gross_profit == 500_000_000  # from Attachment (500,000 * 10^3)
            assert result.cost_of_sales == 868_000_000
            # field_sources の確認
            assert result.field_sources["sales"] == "summary_xbrl"
            assert result.field_sources["operating_profit"] == "summary_xbrl"
            assert result.field_sources["gross_profit"] == "attachment_xbrl"
            assert result.field_sources["cost_of_sales"] == "attachment_xbrl"
        finally:
            os.unlink(tmp_path)

    def test_summary_all_fields_no_overwrite(self):
        """Summary で全取得時は Attachment で上書きしない"""
        summary_content = _make_ixbrl({
            "NetSales": {"value": "1,000"},
            "GrossProfit": {"value": "500"},
            "OperatingIncome": {"value": "200"},
        })
        pl_content = _make_ixbrl({
            "NetSales": {"value": "999,999", "scale": "3",
                         "context": "CurrentYTDDuration"},
            "GrossProfit": {"value": "999", "scale": "3",
                           "context": "CurrentYTDDuration"},
            "OperatingIncome": {"value": "111", "scale": "3",
                              "context": "CurrentYTDDuration"},
        })

        zip_bytes = _make_zip({
            "XBRLData/Summary/tse-qnedjpsm-99990-ixbrl.htm": summary_content,
            "XBRLData/Attachment/0600000-qcpl11-tse-99990-ixbrl.htm": pl_content,
        })

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(zip_bytes)
            tmp_path = f.name

        try:
            result = _extract_from_xbrl(tmp_path)
            assert result is not None
            # Summary の値が保持される
            assert result.sales == 1_000_000_000  # 1,000 * 10^6
            assert result.gross_profit == 500_000_000
            assert result.operating_profit == 200_000_000
            # field_sources はすべて summary_xbrl
            assert result.field_sources["sales"] == "summary_xbrl"
            assert result.field_sources["gross_profit"] == "summary_xbrl"
            assert result.field_sources["operating_profit"] == "summary_xbrl"
        finally:
            os.unlink(tmp_path)

    def test_interim_duration_context_ref(self):
        """InterimDuration を current period として拾える"""
        content = _make_ixbrl({
            "NetSales": {"value": "5,329", "context": "InterimDuration"},
            "GrossProfit": {"value": "2,045", "context": "InterimDuration"},
            "CostOfSales": {"value": "3,283", "context": "InterimDuration"},
            "OperatingIncome": {"value": "158", "context": "InterimDuration"},
        })

        result = _parse_xbrl_content(content, source_label="attachment_xbrl")
        assert result is not None
        assert result.sales == 5_329_000_000
        assert result.gross_profit == 2_045_000_000
        assert result.cost_of_sales == 3_283_000_000
        assert result.operating_profit == 158_000_000
        # field_sources
        assert result.field_sources["sales"] == "attachment_xbrl"
        assert result.field_sources["gross_profit"] == "attachment_xbrl"

    def test_field_sources_correct_labels(self):
        """field_sources が summary_xbrl / attachment_xbrl を正しく記録する"""
        summary_content = _make_ixbrl({
            "NetSales": {"value": "100"},
            "OperatingIncome": {"value": "10"},
        })
        pl_content = _make_ixbrl({
            "NetSales": {"value": "100,000", "scale": "3",
                         "context": "CurrentYTDDuration"},
            "GrossProfit": {"value": "40,000", "scale": "3",
                           "context": "CurrentYTDDuration"},
            "CostOfSales": {"value": "60,000", "scale": "3",
                           "context": "CurrentYTDDuration"},
        })

        zip_bytes = _make_zip({
            "XBRLData/Summary/tse-sm-ixbrl.htm": summary_content,
            "XBRLData/Attachment/0600000-scpl15-ixbrl.htm": pl_content,
        })

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(zip_bytes)
            tmp_path = f.name

        try:
            result = _extract_from_xbrl(tmp_path)
            assert result is not None
            expected_sources = {
                "sales": "summary_xbrl",
                "operating_profit": "summary_xbrl",
                "gross_profit": "attachment_xbrl",
                "cost_of_sales": "attachment_xbrl",
            }
            assert result.field_sources == expected_sources
        finally:
            os.unlink(tmp_path)

    def test_no_attachment_gp_stays_none(self):
        """PL Attachment がない ZIP では gross_profit は None のまま"""
        summary_content = _make_ixbrl({
            "NetSales": {"value": "100"},
            "OperatingIncome": {"value": "10"},
        })

        zip_bytes = _make_zip({
            "XBRLData/Summary/tse-sm-ixbrl.htm": summary_content,
            # PL Attachment なし
        })

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(zip_bytes)
            tmp_path = f.name

        try:
            result = _extract_from_xbrl(tmp_path)
            assert result is not None
            assert result.sales == 100_000_000
            assert result.gross_profit is None
            assert "gross_profit" not in result.field_sources
        finally:
            os.unlink(tmp_path)

    def test_summary_failure_falls_back_to_other(self):
        """Summary がパース失敗 → 他の候補を順に試行"""
        # Summary は空（パース失敗する）
        pl_content = _make_ixbrl({
            "NetSales": {"value": "200", "context": "CurrentYTDDuration"},
            "GrossProfit": {"value": "80", "context": "CurrentYTDDuration"},
            "OperatingIncome": {"value": "30", "context": "CurrentYTDDuration"},
        })

        zip_bytes = _make_zip({
            "XBRLData/Summary/tse-sm-ixbrl.htm": b"<html>no data</html>",
            "XBRLData/Attachment/0600000-qcpl11-ixbrl.htm": pl_content,
        })

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(zip_bytes)
            tmp_path = f.name

        try:
            result = _extract_from_xbrl(tmp_path)
            assert result is not None
            assert result.sales == 200_000_000
            assert result.gross_profit == 80_000_000
            # Summary 失敗なので source_label は "xbrl"（デフォルト）
            assert result.field_sources["sales"] == "xbrl"
        finally:
            os.unlink(tmp_path)

    def test_prior1_duration_excluded(self):
        """Prior1YTDDuration は当期ではないのでスキップされる"""
        content = _make_ixbrl({
            "NetSales": {"value": "100", "context": "Prior1YTDDuration"},
        })
        result = _parse_xbrl_content(content)
        assert result is None  # 当期データがないので None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
