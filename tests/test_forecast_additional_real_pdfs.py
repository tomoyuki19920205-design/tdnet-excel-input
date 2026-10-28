"""Official PDF regressions for rows left unresolved by the initial audit."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

import fitz

from src.events.forecast_extractor import extract_forecast_revision


FIXTURES = Path(__file__).parent / "fixtures" / "forecast_systemic_additional"

# sales, operating profit, ordinary profit, net income (million yen), EPS (yen)
CASES = {
    "212A": (14322, 3506, 3558, 2473, 148.58,
             "6029b0e6147d37683aaf9134086121811a0214ed51609e3933132e102358e7ba"),
    "2269": (1177000, 91000, 93000, 36500, 134.68,
             "79ade570382d041b31d4bc66925b67ffa3b19dae48df9e76a45aa0582436c025"),
    "2341": (4719, 160, 169, 189, 10.33,
             "5abd8eb9206892fab0fde11b39a56c3088ba37b496df25c9bf6ef7e26b6547e7"),
    "434A": (2784, 285, 245, 162, 162.24,
             "3da1add73642be42db6fe684f0c033af95ca258d35e78778f64a970a8be3e0df"),
    "4488": (4747, 310, 487, 351, 87.85,
             "467a1c40852cbe0d5a5665c2ddea2cb4f2a7abb2f2d760d7cd51d0524e0255cc"),
    "525A": (1205, 20, 9, 14, 5.81,
             "5418581b2bb65b881d3bd4d2b08852fad93b00f5850309dc4bede5c0d48cf99d"),
    "6111": (13393, 123, 177, 803, 337.04,
             "b4d734891e83fce8ce567daa9221863478eb991a83156c050d53c72a92e8d1a7"),
    "7673": (21900, 1227, 1260, 878, 164.65,
             "4a464d6863a95c9b37ed64396055d90ebedc7c0ea83662287dc8105599e2b509"),
}


class TestAdditionalOfficialPDFs(unittest.TestCase):
    def test_full_forecast_columns(self):
        for ticker, (sales, op, ordinary, net_income, eps, sha256) in CASES.items():
            with self.subTest(ticker=ticker):
                pdf = FIXTURES / f"{ticker}.pdf"
                raw = pdf.read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), sha256)
                document = fitz.open(stream=raw, filetype="pdf")
                text = "\n".join(page.get_text() for page in document)
                event = extract_forecast_revision(
                    text, title="業績予想の修正に関するお知らせ",
                    pdf_path=str(pdf), ticker=ticker,
                )
                for field, expected in (
                    ("revised_sales", sales), ("revised_op", op),
                    ("revised_ordinary", ordinary),
                    ("revised_net_income", net_income), ("revised_eps", eps),
                ):
                    self.assertAlmostEqual(getattr(event, field), expected, places=2, msg=field)
                self.assertGreaterEqual(event.extracted_metrics_count, 5)


if __name__ == "__main__":
    unittest.main()
