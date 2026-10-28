"""Regression checks against China Marine Paints' official 2026 disclosures.

The PDFs are retained in fixtures so these tests do not depend on the IR site
being available at test time.  See fixtures/forecast_4617/README.md.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import pdfplumber

from src.events.earnings_guidance_extractor import extract_guidance_from_zip
from src.events.earnings_production_pipeline import (
    _canonical_forecast_metrics, _sync_canonical_financials,
)
from src.events.forecast_extractor import (
    _extract_numbers_from_line,
    _normalize_ocr_text,
    _normalize_text,
    extract_forecast_revision,
)
from lib.pipeline.canonical_writer import expand_financials_rows


FIXTURES = Path(__file__).parent / "fixtures" / "forecast_4617"


def first_page(name: str) -> str:
    with pdfplumber.open(FIXTURES / name) as pdf:
        return pdf.pages[0].extract_text() or ""


class TestChinaMarinePaintsForecast(unittest.TestCase):
    def test_fullwidth_negative_undetermined_and_ocr_column_boundaries(self):
        self.assertEqual(
            _extract_numbers_from_line(
                "今回修正予想（Ｂ） １６０，０００ △１，２００ −３００ 未定 ２２１円７１銭"
            ),
            [160000.0, -1200.0, -300.0, None, 221.71],
        )
        ocr = _normalize_ocr_text(
            "今回修正予想（Ｂ） 160,000 17,500 18,000 11,000 221円71銭"
        )
        self.assertIn("160,000 17,500", ocr)
        self.assertNotIn("160,00017,500", ocr)

    def test_may_11_range_is_not_a_point_forecast(self):
        text = first_page("may11.pdf")
        self.assertIn("2026年5月11日", text)
        self.assertIn("2027年3月期の連結業績予想", text)
        self.assertIn("140,000", text)
        self.assertIn("～160,000", text)
        guidance = extract_guidance_from_zip(None, pdf_path=str(FIXTURES / "may11.pdf"))
        for field in ("sales_forecast", "op_forecast", "ordinary_forecast",
                      "net_income_forecast", "eps_forecast"):
            self.assertIsNone(getattr(guidance, field), field)

    def test_july_3_revision_extracts_five_cells_and_no_prior_profit(self):
        text = first_page("jul03.pdf")
        self.assertIn("2026年7月3日", _normalize_text(text))
        row = next(line for line in text.splitlines() if "今回修正予想" in line)
        self.assertEqual(
            _extract_numbers_from_line(row),
            [160000.0, 17500.0, 18000.0, 11000.0, 221.71],
        )
        event = extract_forecast_revision(
            text, "業績予想の修正に関するお知らせ",
            pdf_path=str(FIXTURES / "jul03.pdf"),
        )
        self.assertEqual((event.period_label, event.basis), ("2027年3月期 通期", "連結"))
        self.assertEqual(
            (event.revised_sales, event.revised_op, event.revised_ordinary,
             event.revised_net_income, event.revised_eps),
            (160000.0, 17500.0, 18000.0, 11000.0, 221.71),
        )
        self.assertTrue(event.eps_validated)
        self.assertEqual(event.extracted_metrics_count, 5)
        for field in ("previous_sales", "previous_op", "previous_ordinary",
                      "previous_net_income", "previous_eps"):
            self.assertIsNone(getattr(event, field), field)

    def test_july_31_same_five_values_with_one_unit_conversion(self):
        text = first_page("jul31.pdf")
        self.assertIn("2026年7月31日", text)
        self.assertIn("2027年3月期の連結業績予想", text)
        self.assertIn("通期 160,000 14.8 17,500", text)
        guidance = extract_guidance_from_zip(
            str(FIXTURES / "jul31_xbrl.zip"), pdf_path=str(FIXTURES / "jul31.pdf")
        )
        self.assertEqual(guidance.forecast_period, "2027-03-31")
        self.assertEqual(
            (guidance.sales_forecast, guidance.op_forecast,
             guidance.ordinary_forecast, guidance.net_income_forecast,
             guidance.eps_forecast),
            (160_000_000_000.0, 17_500_000_000.0, 18_000_000_000.0,
             11_000_000_000.0, 221.71),
        )
        metrics = _canonical_forecast_metrics(vars(guidance))
        self.assertEqual(metrics, {
            "sales": 160000.0, "operating_profit": 17500.0,
            "ordinary_profit": 18000.0, "net_income": 11000.0,
            "eps": 221.71,
        })
        self.assertEqual(metrics, _canonical_forecast_metrics(vars(guidance)))

    def test_forecast_writer_keeps_eps_in_yen_per_share(self):
        guidance = extract_guidance_from_zip(
            str(FIXTURES / "jul31_xbrl.zip"), pdf_path=str(FIXTURES / "jul31.pdf")
        )
        with patch("lib.pipeline.canonical_writer.write_financials_canonical") as write:
            _sync_canonical_financials(
                ticker="4617", period="2026-06-30", quarter="1Q",
                sales_value=None, op_value=None, gross_value=None, sga_value=None,
                guidance=vars(guidance), filing_id="a" * 64,
                dry_run=False, route="regression", disclosure_datetime="2026-07-31T06:30:00Z",
            )
        forecast = [call.kwargs for call in write.call_args_list
                    if call.kwargs["source"] == "tdnet_forecast"]
        self.assertEqual(len(forecast), 2)
        self.assertEqual(forecast[0]["unit"], "millions_jpy")
        self.assertEqual(forecast[0]["metrics_dict"]["net_income"], 11000.0)
        self.assertEqual(forecast[1]["unit"], "yen_per_share")
        self.assertEqual(forecast[1]["metrics_dict"], {"eps": 221.71})
        rows, skipped = expand_financials_rows(
            ticker="4617", period="2027-03-31", quarter="FY",
            metrics_dict={"eps": 221.71}, source="tdnet_forecast",
            filing_id="a" * 64, unit="yen_per_share",
        )
        self.assertEqual(skipped, 0)
        self.assertEqual([(r["metric"], r["value"], r["unit"]) for r in rows],
                         [("eps", 221.71, "yen_per_share")])

    def test_abnormal_forecast_value_is_blocked_before_writer(self):
        self.assertEqual(_canonical_forecast_metrics({
            "sales_forecast": 1.600001750018e29,
            "op_forecast": 17_500_000_000,
            "eps_forecast": 221.71,
        }), {"operating_profit": 17500.0, "eps": 221.71})


if __name__ == "__main__":
    unittest.main()
