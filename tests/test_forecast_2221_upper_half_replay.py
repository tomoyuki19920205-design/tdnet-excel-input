"""Real 2221 filing: the revised upper-half row must not replace FY guidance."""
from dataclasses import replace
import json
import sqlite3
import unittest
from pathlib import Path

import pdfplumber

from lib.pipeline.forecast_sync import (
    ForecastDTO, expand_forecast_rows, forecast_quarter_from_label,
    load_revision_forecasts, select_latest_forecasts,
)
from src.events.forecast_extractor import extract_forecast_revision


PDF = Path(__file__).parent / "fixtures/forecast_2221/2221_20261030.pdf"
FILING = "2221-20261030-official"


class UpperHalfReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with pdfplumber.open(PDF) as document:
            source_text = "\n".join(page.extract_text() or "" for page in document.pages)
        cls.payload = extract_forecast_revision(
            source_text, "業績予想の修正に関するお知らせ", pdf_path=str(PDF),
            doc_id=FILING, ticker="2221",
        ).to_dict()

    def test_period_classification_rejects_single_quarter_and_ambiguous(self):
        self.assertEqual(forecast_quarter_from_label("2027年3月期 第2四半期累計"), "2Q")
        self.assertEqual(forecast_quarter_from_label("2027年3月期 中間期"), "2Q")
        self.assertEqual(forecast_quarter_from_label("2027年3月期 通期"), "FY")
        self.assertIsNone(forecast_quarter_from_label("2027年3月期 第2四半期単独"))
        self.assertIsNone(forecast_quarter_from_label("2027年3月期 第2四半期・通期"))

    def test_source_verified_upper_half_keeps_fy_independent(self):
        self.assertEqual(self.payload["period_label"], "2027年3月期 第2四半期")
        self.assertEqual(self.payload["basis"], "連結")
        self.assertEqual({key: self.payload[key] for key in (
            "revised_sales", "revised_op", "revised_ordinary", "revised_net_income")}, {
            "revised_sales": 14880.0, "revised_op": -260.0,
            "revised_ordinary": 1100.0, "revised_net_income": 770.0,
        })
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE events (
            event_id TEXT, source_doc_id TEXT, ticker TEXT, disclosure_datetime TEXT,
            title TEXT, event_type TEXT, extracted_payload_json TEXT,
            created_at TEXT, doc_url TEXT)""")
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (
            "2221-event", FILING, "2221", "2026-10-30T15:00:00+09:00",
            "業績予想の修正に関するお知らせ", "forecast_revision",
            json.dumps(self.payload, ensure_ascii=False),
            "2026-10-30T15:10:00+09:00",
            "https://www2.jpx.co.jp/disc/22210/140120261030560533.pdf",
        ))
        candidates, quarantine = load_revision_forecasts(
            conn, disclosure_ids=[FILING], source_pdfs={FILING: str(PDF)},
        )
        self.assertEqual(quarantine, [])
        self.assertEqual(len(candidates), 4)
        self.assertEqual({candidate.quarter for candidate in candidates}, {"2Q"})
        rows = expand_forecast_rows(candidates)
        self.assertEqual({row["quarter"] for row in rows}, {"2Q"})
        self.assertEqual({row["metric"]: row["value"] for row in rows}, {
            "sales": 14880.0, "operating_profit": -260.0,
            "ordinary_profit": 1100.0, "net_income": 770.0,
        })
        fy = replace(candidates[0], quarter="FY", value=30600.0,
                     filing_id="fy-independent", metric="sales")
        selected = select_latest_forecasts(candidates + [fy])
        self.assertEqual(len(selected), 5)
        self.assertEqual({(x.quarter, x.metric): x.value for x in selected}[("FY", "sales")], 30600.0)


if __name__ == "__main__":
    unittest.main()
