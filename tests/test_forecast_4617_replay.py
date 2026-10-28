"""Guard historical 4617 payload replay and forecast row identity."""
from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
import unittest
from pathlib import Path

import pdfplumber

from lib.pipeline.forecast_sync import (
    expand_forecast_rows, load_revision_forecasts, select_latest_forecasts,
)
from src.events.forecast_extractor import extract_forecast_revision


FIXTURE = Path(__file__).parent / "fixtures" / "forecast_4617" / "jul03.pdf"
FILING = "9857690d65fffa058fc4cbbec284fca462c32b201c8416592b43618fe9c33612"


def event_payload() -> dict:
    with pdfplumber.open(FIXTURE) as pdf:
        text = pdf.pages[0].extract_text() or ""
    return extract_forecast_revision(
        text, "業績予想の修正に関するお知らせ", pdf_path=str(FIXTURE), doc_id=FILING
    ).to_dict()


class TestForecastReplay(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE events (
            event_id TEXT, source_doc_id TEXT, ticker TEXT,
            disclosure_datetime TEXT, title TEXT, event_type TEXT,
            extracted_payload_json TEXT, created_at TEXT, doc_url TEXT
        )""")

    def insert(self, payload: dict):
        self.conn.execute("DELETE FROM events")
        self.conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            ("test-event", FILING, "4617", "2026-07-03 15:30:00",
             "業績予想の修正に関するお知らせ", "forecast_revision",
             json.dumps(payload, ensure_ascii=False), "2026-07-03T16:00:00+09:00", ""),
        )

    def test_stale_merged_columns_are_quarantined_before_replay(self):
        self.insert({
            "period_label": "2027年3月期 通期", "basis": "連結",
            "revised_sales": 1.600001750018e23, "revised_op": 71.0,
        })
        rows, quarantine = load_revision_forecasts(self.conn, disclosure_ids=[FILING])
        self.assertEqual(rows, [])
        self.assertEqual(quarantine[0]["reason"], "revision_amount_row_malformed")

    def test_correct_replay_has_five_stable_rows_and_newer_wins(self):
        self.insert(event_payload())
        candidates, quarantine = load_revision_forecasts(
            self.conn, disclosure_ids=[FILING], source_pdfs={FILING: str(FIXTURE)})
        self.assertEqual(quarantine, [])
        self.assertEqual(len(candidates), 5)
        first = expand_forecast_rows(candidates)
        second = expand_forecast_rows(candidates)
        self.assertEqual(
            {(row["metric"], row["source_row_key"], row["value"], row["unit"]) for row in first},
            {(row["metric"], row["source_row_key"], row["value"], row["unit"]) for row in second},
        )
        self.assertEqual(len({row["source_row_key"] for row in first}), 5)
        self.assertEqual({row["metric"]: (row["value"], row["unit"]) for row in first}, {
            "sales": (160000.0, "millions_jpy"),
            "operating_profit": (17500.0, "millions_jpy"),
            "ordinary_profit": (18000.0, "millions_jpy"),
            "net_income": (11000.0, "millions_jpy"),
            "eps": (221.71, "yen_per_share"),
        })
        newer = [replace(dto, disclosure_datetime="2026-07-31T15:30:00+09:00",
                         filing_id="b" * 64, document_type="earnings_guidance")
                 for dto in candidates]
        selected = select_latest_forecasts(candidates + newer)
        self.assertEqual(len(selected), 5)
        self.assertTrue(all(dto.filing_id == "b" * 64 for dto in selected))


if __name__ == "__main__":
    unittest.main()
