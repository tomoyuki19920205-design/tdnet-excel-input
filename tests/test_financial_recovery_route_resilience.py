from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.models import ExtractedFinancials
from tools import financial_recovery_retry as retry


def test_earnings_v2_exception_does_not_block_official_pdf_fallback(tmp_path):
    payload = {
        "disclosure_id": "a" * 64,
        "code": "222A",
        "title": "2026年12月期 中間決算短信",
        "doc_url": "https://www.release.tdnet.info/inbs/140120260807514411.pdf",
        "published_at": "2026-08-07T15:30:00+09:00",
        "period": "2026-12-31",
        "quarter": "2Q",
    }
    financials = ExtractedFinancials(
        sales=362,
        operating_profit=16,
        ordinary_profit=17,
        net_income=10,
        fiscal_year="2026-12-31",
        quarter="2Q",
        source_unit="百万円",
        field_sources={
            "sales": "pdf_table",
            "operating_profit": "pdf_table",
            "ordinary_profit": "pdf_table",
            "net_income": "pdf_table",
        },
    )
    earnings = MagicMock(side_effect=RuntimeError("V2 unavailable"))
    with patch.object(retry, "canonical_is_resolved", side_effect=[False, False, True]), \
         patch.object(retry, "_canonical_rows", return_value=[]), \
         patch.object(retry, "get_supabase_write_config", return_value={"key": "test"}), \
         patch.object(retry, "download_document", return_value=str(tmp_path / "222A.pdf")), \
         patch.object(retry, "extract_financials", return_value=(financials, "")), \
         patch.object(retry, "write_financials_canonical", return_value={"written": 4, "errors": 0}) as write, \
         patch("src.events.earnings_production_pipeline.run_earnings_production", earnings):
        outcome = retry.recover_one(
            payload, decision_db_path=str(tmp_path / "decision.db")
        )

    assert outcome == {"resolved": True, "route": "official_pdf"}
    earnings.assert_called_once()
    write.assert_called_once()
