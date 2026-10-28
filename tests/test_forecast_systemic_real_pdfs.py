"""Official PDFs that exposed unit and non-financial table confusion."""
from __future__ import annotations

import unittest
from pathlib import Path

import pdfplumber

from src.events.forecast_extractor import _extract_from_text

FIXTURES = Path(__file__).parent / "fixtures" / "forecast_systemic"


def parse(name):
    path = FIXTURES / name
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return _extract_from_text(text, pdf_path=str(path))


class OfficialPDFSystemicRegression(unittest.TestCase):
    def test_1911_eps_yen_header_does_not_scale_million_yen_amounts(self):
        event = parse("1911_revision.pdf")
        self.assertEqual((event.revised_sales, event.revised_op, event.revised_ordinary,
                          event.revised_net_income, event.revised_eps),
                         (2_950_000.0, 143_000.0, 115_000.0, 60_000.0, 97.85))
        self.assertEqual((event.period_label, event.basis), ("2026年12月期 通期", "連結"))

    def test_1980_order_forecast_is_not_sales(self):
        event = parse("1980_revision.pdf")
        self.assertEqual((event.revised_sales, event.revised_op, event.revised_ordinary,
                          event.revised_net_income, event.revised_eps),
                         (255_000.0, 34_000.0, 35_000.0, 26_000.0, 201.35))
        self.assertNotEqual(event.revised_sales, 350_000.0)
        self.assertEqual((event.period_label, event.basis), ("2026年3月期 通期", "連結"))

    def test_1997_standalone_shifted_header_and_spurious_prose_eps(self):
        event = parse("1997_revision.pdf")
        self.assertEqual(event.period_label, "2026年8月期")
        self.assertEqual((event.revised_sales, event.revised_op, event.revised_ordinary,
                          event.revised_net_income, event.revised_eps),
                         (9_500.0, 1_350.0, 1_400.0, 950.0, 469.85))
        self.assertEqual(event.basis, "個別")

    def test_2120_split_revised_cells_and_eps_footnote(self):
        event = parse("2120_revision.pdf")
        self.assertEqual(event.period_label, "2026年9月期 通期")
        self.assertEqual((event.revised_sales, event.revised_op,
                          event.revised_net_income, event.revised_eps),
                         (29_300.0, 3_900.0, 2_500.0, 19.50))
        self.assertIsNone(event.revised_ordinary)

    def test_3563_adjusted_ebitda_does_not_shift_profit_columns(self):
        event = parse("3563_revision.pdf")
        self.assertEqual(event.period_label, "2026年9月期 通期")
        self.assertEqual((event.revised_sales, event.revised_op,
                          event.revised_net_income, event.revised_eps),
                         (505_000.0, 50_500.0, 31_500.0, 138.73))
        self.assertIsNone(event.revised_ordinary)

    def test_4058_ebitda_does_not_shift_forecast_columns(self):
        event = parse("4058_revision.pdf")
        self.assertEqual(event.period_label, "2026年12月期 通期")
        self.assertEqual((event.revised_sales, event.revised_op, event.revised_ordinary,
                          event.revised_net_income, event.revised_eps),
                         (6_000.0, 2_100.0, 2_100.0, 1_400.0, 128.87))


if __name__ == "__main__":
    unittest.main()
