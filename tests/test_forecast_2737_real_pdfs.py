"""Official Tomen Devices FY2027 forecast regression and saved-value checks."""
from __future__ import annotations

import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

import pdfplumber

from src.events.earnings_guidance_extractor import extract_guidance_from_zip
from src.events.earnings_production_pipeline import _canonical_forecast_metrics, _check_forecast_saved
from src.events.forecast_extractor import extract_forecast_revision, _extract_from_text
from tools.parse_tanshin_worker import _run_production_parse


FIXTURES = Path(__file__).parent / "fixtures" / "forecast_2737"
FILING = "a" * 64
EXPECTED = {"sales": 1_400_000.0, "operating_profit": 48_900.0,
            "ordinary_profit": 40_800.0, "net_income": 30_000.0, "eps": 4_411.22}


def pdf_text(name: str) -> str:
    with pdfplumber.open(FIXTURES / name) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


class _RowsQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters = {}

    def select(self, _fields):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def execute(self):
        class Result:
            pass
        result = Result()
        result.data = [row for row in self.rows if all(row.get(key) == value for key, value in self.filters.items())]
        return result


class _RowsClient:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "canonical_financials"
        return _RowsQuery(self.rows)


def rows_from_metrics(metrics=EXPECTED):
    return [{"ticker": "2737", "period": "2027-03-31", "quarter": "FY",
             "filing_id": FILING, "source": "tdnet_forecast", "metric": metric,
             "value": value, "unit": "yen_per_share" if metric == "eps" else "millions_jpy"}
            for metric, value in metrics.items()]


class TomenForecastOfficialPDFs(unittest.TestCase):
    def test_july_revision_uses_five_forecast_cells_and_previous_values(self):
        text = pdf_text("jul30_revision.pdf")
        event = extract_forecast_revision(text, "業績予想および配当予想の修正（増配）に関するお知らせ",
                                          pdf_path=str(FIXTURES / "jul30_revision.pdf"))
        self.assertEqual((event.period_label, event.basis), ("2027年3月期 通期", "連結"))
        self.assertEqual((event.revised_sales, event.revised_op, event.revised_ordinary,
                          event.revised_net_income, event.revised_eps), tuple(EXPECTED.values()))
        self.assertEqual((event.previous_sales, event.previous_op, event.previous_ordinary,
                          event.previous_net_income, event.previous_eps),
                         (750_000.0, 18_200.0, 14_500.0, 11_000.0, 1_617.43))
        self.assertTrue(event.eps_validated)
        self.assertNotEqual(event.revised_eps, 300.0)

    def test_without_table_pdf_dividend_floor_is_not_eps(self):
        event = _extract_from_text(pdf_text("jul30_revision.pdf"))
        self.assertNotEqual(event.revised_eps, 300.0)

    def test_first_and_second_quarter_pdf_and_xbrl_agree(self):
        for quarter in ("jul30", "oct28"):
            with self.subTest(quarter=quarter):
                guidance = extract_guidance_from_zip(
                    str(FIXTURES / f"{quarter}_xbrl.zip"),
                    pdf_path=str(FIXTURES / f"{quarter}_tanshin.pdf"),
                )
                self.assertEqual(guidance.forecast_period, "2027-03-31")
                self.assertEqual(_canonical_forecast_metrics(dataclasses.asdict(guidance)), EXPECTED)
                xbrl_only = extract_guidance_from_zip(str(FIXTURES / f"{quarter}_xbrl.zip"))
                self.assertEqual(_canonical_forecast_metrics(dataclasses.asdict(xbrl_only)), EXPECTED)
                pdf_only = extract_guidance_from_zip(None, pdf_path=str(FIXTURES / f"{quarter}_tanshin.pdf"))
                self.assertEqual(_canonical_forecast_metrics(dataclasses.asdict(pdf_only)), EXPECTED)

    def test_second_quarter_subprocess_worker_keeps_five_guidance_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "140120261019553024.pdf"
            shutil.copy2(FIXTURES / "oct28_tanshin.pdf", pdf)
            result = _run_production_parse(
                FIXTURES / "oct28_xbrl.zip",
                "2027年３月期 第２四半期（中間期）決算短信〔日本基準〕（連結）",
                "2737", disclosed_at="2026-10-28T13:00:00+09:00",
                pdf_path_override=str(pdf), source_doc_id_override="140120261019553024",
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["quarter"], "2Q")
        self.assertEqual(_canonical_forecast_metrics(result["extracted_payload"]["guidance"]), EXPECTED)

    def test_completion_reads_back_value_unit_period_and_filing(self):
        guidance = dataclasses.asdict(extract_guidance_from_zip(None, pdf_path=str(FIXTURES / "oct28_tanshin.pdf")))
        good = rows_from_metrics()
        self.assertTrue(_check_forecast_saved(_RowsClient(good), "2737", "2026-09-30", "2Q", FILING,
                                              guidance, source_pdf_path=str(FIXTURES / "oct28_tanshin.pdf")))
        for changed in (
            [{**r, "value": 1.40000048900408e25} if r["metric"] == "sales" else r for r in good],
            [{**r, "unit": "millions_jpy"} if r["metric"] == "eps" else r for r in good],
            [r for r in good if r["metric"] != "net_income"],
            [{**r, "period": "2026-03-31"} if r["metric"] == "sales" else r for r in good],
        ):
            self.assertFalse(_check_forecast_saved(_RowsClient(changed), "2737", "2026-09-30", "2Q", FILING,
                                                   guidance, source_pdf_path=str(FIXTURES / "oct28_tanshin.pdf")))

    def test_empty_extraction_requires_source_evidence(self):
        self.assertFalse(_check_forecast_saved(_RowsClient([]), "2737", "2026-09-30", "2Q", FILING, {},
                                                source_pdf_path=str(FIXTURES / "oct28_tanshin.pdf")))
        self.assertFalse(_check_forecast_saved(_RowsClient([]), "2737", "2026-09-30", "2Q", FILING, {}))


if __name__ == "__main__":
    unittest.main()
