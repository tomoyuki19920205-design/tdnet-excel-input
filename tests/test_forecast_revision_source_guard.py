"""Re-read the official 2737 revision PDF before canonical replay."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

import pdfplumber

from src.events.forecast_extractor import extract_forecast_revision, _extract_via_pdfplumber_table
from lib.pipeline.forecast_sync import (
    _verify_revision_source, expand_forecast_rows, load_revision_forecasts,
)


PDF = Path(__file__).parent / "fixtures" / "forecast_2737" / "jul30_revision.pdf"
PDF_SHA = "90e4a8857750ddbaa881db91b36cd366abf4de3b4c914ee61a268a568ba0e0f7"
FILING = "15244638dfd65bfb44b26be885912437512858f90d467ce35f696efd04245f19"
PREVIOUS = {"sales": 750000.0, "op": 18200.0, "ordinary": 14500.0,
            "net_income": 11000.0, "eps": 1617.43}
REVISED = {"sales": 1400000.0, "op": 48900.0, "ordinary": 40800.0,
           "net_income": 30000.0, "eps": 4411.22}


def payload_from_pdf():
    with pdfplumber.open(PDF) as document:
        text = "\n".join(page.extract_text() or "" for page in document.pages)
    return extract_forecast_revision(
        text, "2027年3月期連結業績予想の修正", pdf_path=str(PDF), doc_id=FILING,
        ticker="2737",
    ).to_dict()


class TestRevisionSourceGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = payload_from_pdf()
        cls.row = {"source_doc_id": FILING, "ticker": "2737", "event_id": "fixture-event"}

    def test_official_source_has_five_previous_and_revised_values(self):
        self.assertEqual(hashlib.sha256(PDF.read_bytes()).hexdigest(), PDF_SHA)
        table = _extract_via_pdfplumber_table(str(PDF))
        for side, expected in (("previous", PREVIOUS), ("revised", REVISED)):
            for metric, value in expected.items():
                self.assertAlmostEqual(table[f"{side}_{metric}"], value, places=2)
                self.assertAlmostEqual(self.payload[f"{side}_{metric}"], value, places=2)
        self.assertEqual(self.payload["basis"], "連結")
        self.assertEqual(self.payload["period_label"], "2027年3月期 通期")

    def test_valid_payload_passes_and_expands_with_correct_units(self):
        self.assertEqual(_verify_revision_source(self.row, self.payload, str(PDF)), (True, ""))
        conn = self._db(self.payload)
        candidates, quarantine = load_revision_forecasts(
            conn, disclosure_ids=[FILING], source_pdfs={FILING: str(PDF)})
        self.assertEqual(quarantine, [])
        self.assertEqual(len(candidates), 5)
        rows = expand_forecast_rows(candidates)
        self.assertEqual(set(rows[0]), {
            "ticker", "period", "quarter", "metric", "value", "unit", "source",
            "source_priority", "filing_id", "source_row_key", "disclosure_datetime",
            "correction_flag", "recency_key", "updated_at",
        })
        self.assertEqual({r["metric"]: r["value"] for r in rows}, {
            "sales": 1400000.0, "operating_profit": 48900.0,
            "ordinary_profit": 40800.0, "net_income": 30000.0, "eps": 4411.22,
        })
        self.assertEqual({r["unit"] for r in rows if r["metric"] != "eps"}, {"millions_jpy"})
        self.assertEqual({r["unit"] for r in rows if r["metric"] == "eps"}, {"yen_per_share"})
        self.assertEqual({r["filing_id"] for r in rows}, {FILING})

    def test_no_pdf_cannot_replay_even_a_plausible_json(self):
        conn = self._db(self.payload)
        candidates, quarantine = load_revision_forecasts(conn, disclosure_ids=[FILING])
        self.assertEqual(candidates, [])
        self.assertEqual(quarantine[0]["reason"], "revision_source_pdf_missing")

    def test_corrupt_json_is_rejected_before_canonical_rows(self):
        mutations = {
            "joined_column": ("revised_sales", 1.40000048900408e25, "revision_amount_row_malformed"),
            "plausible_wrong_amount": ("revised_sales", 1399999.0, "revision_source_value_mismatch"),
            "dividend_as_eps": ("revised_eps", 40.0, "revision_source_value_mismatch"),
            "missing_net_income": ("revised_net_income", None, "revision_source_metric_missing_or_extra"),
            "wrong_period": ("period_label", "2026年3月期 通期", "revision_source_period_mismatch"),
            "wrong_basis": ("basis", "個別", "revision_source_basis_mismatch"),
            "wrong_previous": ("previous_op", 1820.0, "revision_source_value_mismatch"),
        }
        for name, (field, value, reason) in mutations.items():
            with self.subTest(name=name):
                payload = copy.deepcopy(self.payload)
                payload[field] = value
                conn = self._db(payload)
                candidates, quarantine = load_revision_forecasts(
                    conn, disclosure_ids=[FILING], source_pdfs={FILING: str(PDF)})
                self.assertEqual(candidates, [])
                self.assertEqual(quarantine[0]["reason"], reason)

    def test_source_hash_and_filing_are_bound(self):
        bad_hash = copy.deepcopy(self.payload)
        bad_hash["revision_reason"]["pdf_sha256"] = "0" * 64
        self.assertEqual(_verify_revision_source(self.row, bad_hash, str(PDF))[1],
                         "revision_source_hash_mismatch")
        bad_filing = copy.deepcopy(self.payload)
        bad_filing["revision_reason"]["doc_id"] = "other"
        self.assertEqual(_verify_revision_source(self.row, bad_filing, str(PDF))[1],
                         "revision_source_filing_mismatch")

    @staticmethod
    def _db(payload):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE events (
            event_id TEXT, source_doc_id TEXT, ticker TEXT, disclosure_datetime TEXT,
            title TEXT, event_type TEXT, extracted_payload_json TEXT,
            created_at TEXT, doc_url TEXT)""")
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (
            "fixture-event", FILING, "2737", "2026-07-30 13:00",
            "2027年3月期連結業績予想の修正", "forecast_revision",
            json.dumps(payload, ensure_ascii=False), "2026-07-30T13:00:00+09:00", "",
        ))
        return conn
