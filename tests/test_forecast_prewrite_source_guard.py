"""Forecast writes must agree with an independently read source table."""

from unittest.mock import patch

from src.events.earnings_guidance_extractor import GuidanceData
from src.events.earnings_production_pipeline import _sync_canonical_financials, _check_forecast_saved


def _sync(source_pdf_path):
    _sync_canonical_financials(
        ticker="2737", period="2026-03-31", quarter="1Q",
        sales_value=None, op_value=None, gross_value=None, sga_value=None,
        guidance={"forecast_period": "2027-03-31", "sales_forecast": 140_000_000_000.0,
                  "eps_forecast": 300.0},
        filing_id="a" * 64, dry_run=False, route="guard_test",
        source_pdf_path=source_pdf_path,
    )


def test_missing_source_blocks_forecast_write(monkeypatch):
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", raising=False)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", raising=False)
    with patch("lib.pipeline.canonical_writer.write_financials_canonical") as writer:
        _sync(None)
    assert [c.kwargs for c in writer.call_args_list if c.kwargs["source"] == "tdnet_forecast"] == []


def test_mismatched_source_blocks_forecast_write(tmp_path, monkeypatch):
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", raising=False)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", raising=False)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"fixture")
    source = GuidanceData(forecast_period="2027-03-31", sales_forecast=120_000_000_000.0,
                          eps_forecast=300.0)
    with patch("src.events.earnings_guidance_extractor._extract_guidance_from_pdf", return_value=source), \
         patch("lib.pipeline.canonical_writer.write_financials_canonical") as writer:
        _sync(str(pdf))
    assert [c.kwargs for c in writer.call_args_list if c.kwargs["source"] == "tdnet_forecast"] == []


def test_verified_source_writes_forecast_once_per_unit(tmp_path, monkeypatch):
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", raising=False)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", raising=False)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"fixture")
    source = GuidanceData(forecast_period="2027-03-31", sales_forecast=140_000_000_000.0,
                          eps_forecast=300.0)
    with patch("src.events.earnings_guidance_extractor._extract_guidance_from_pdf", return_value=source), \
         patch("lib.pipeline.canonical_writer.write_financials_canonical") as writer:
        _sync(str(pdf))
    forecast = [c.kwargs for c in writer.call_args_list if c.kwargs["source"] == "tdnet_forecast"]
    assert len(forecast) == 2
    assert {c["unit"] for c in forecast} == {"millions_jpy", "yen_per_share"}
    assert {c["period"] for c in forecast} == {"2027-03-31"}


def test_existing_wrong_row_is_not_accepted_as_complete(tmp_path):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"fixture")
    source = GuidanceData(forecast_period="2027-03-31", sales_forecast=140_000_000_000.0,
                          eps_forecast=300.0)
    proposed = {"forecast_period": "2027-03-31", "sales_forecast": 120_000_000_000.0,
                "eps_forecast": 300.0}
    with patch("src.events.earnings_guidance_extractor._extract_guidance_from_pdf", return_value=source), \
         patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True) as readback:
        complete = _check_forecast_saved(None, "2737", "2026-03-31", "1Q", "a" * 64,
                                         proposed, source_pdf_path=str(pdf))
    assert not complete
    readback.assert_not_called()


def test_source_verified_large_eps_is_preserved(tmp_path, monkeypatch):
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", raising=False)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", raising=False)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"fixture")
    source = GuidanceData(forecast_period="2027-03-31", sales_forecast=140_000_000_000.0,
                          eps_forecast=20_000.0)
    with patch("src.events.earnings_guidance_extractor._extract_guidance_from_pdf", return_value=source), \
         patch("lib.pipeline.canonical_writer.write_financials_canonical") as writer:
        _sync_canonical_financials(
            ticker="2737", period="2026-03-31", quarter="1Q",
            sales_value=None, op_value=None, gross_value=None, sga_value=None,
            guidance={"forecast_period": "2027-03-31", "sales_forecast": 140_000_000_000.0,
                      "eps_forecast": 20_000.0},
            filing_id="a" * 64, dry_run=False, route="guard_test",
            source_pdf_path=str(pdf),
        )
    assert any(c.kwargs["metrics_dict"] == {"eps": 20_000.0}
               for c in writer.call_args_list if c.kwargs["source"] == "tdnet_forecast")
