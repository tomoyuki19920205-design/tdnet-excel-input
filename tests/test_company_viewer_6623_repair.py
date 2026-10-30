"""Regression checks for the audited 6623 repair path (no DB writes)."""
import json
from pathlib import Path

from lib.pipeline.source_priority import get_priority
from tools.repair_company_viewer_6623 import MANIFEST, build_rows
from tools.sqlite_to_supabase import _exclude_verified_financials, _quarterly_row_is_completed_actual


def test_forecast_template_cannot_become_actual():
    assert not _quarterly_row_is_completed_actual("2026-03-31", "FY", "2026-02-25 21:49:09")
    assert not _quarterly_row_is_completed_actual("2027-03-31", "3Q", "2026-10-30 14:00:00")
    assert _quarterly_row_is_completed_actual("2027-03-31", "1Q", "2026-08-08T16:16:43+09:00")
    assert _quarterly_row_is_completed_actual("2027-03-31", "2Q", "2026-10-30 14:42:45")


def test_manifest_is_repeatable_and_preserves_sales_bases():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    financial, segments, wide = build_rows(manifest)
    financial2, segments2, _ = build_rows(manifest)
    assert (len(financial), len(segments), len(wide)) == (193, 126, 14)
    assert {r["source_row_key"] for r in financial} == {r["source_row_key"] for r in financial2}
    assert {r["source_row_key"] for r in segments} == {r["source_row_key"] for r in segments2}
    assert len({r["source_row_key"] for r in financial}) == 193
    assert len({r["source_row_key"] for r in segments}) == 126
    assert get_priority("official_ir_verified") < get_priority("xbrl")
    assert all("https://" in r["filing_id"] and "|p" in r["filing_id"] for r in financial + segments)
    assert all(r["value"] is not None for r in financial + segments)
    fy = next(r for r in manifest if (r["period"], r["quarter"]) == ("2026-03-31", "FY"))
    assert [fy["pl"][k] for k in ("sales", "operating_profit", "ordinary_profit", "net_income")] == [129382, 11153, 11925, 8527]
    assert [(s["external_sales"], s["sales_with_internal"], s["profit"]) for s in fy["segments"]] == [
        (38004, 38032, 5750), (57167, 57196, 2101), (34210, 34282, 5735),
    ]
    assert all(r["pl"]["extraordinary_income"] is None for r in manifest if (r["period"], r["quarter"]) in {
        ("2025-03-31", "1Q"), ("2027-03-31", "1Q")
    })


def test_existing_view_columns_are_preserved():
    sql = (Path(__file__).resolve().parents[1] / "migrations/022_company_viewer_financial_detail.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW public.api_latest_financials_canonical" in sql
    assert "CREATE OR REPLACE VIEW public.api_latest_segments" in sql
    assert "sales_with_internal" in sql
    assert "cost_of_sales" in sql and "income_taxes" in sql


def test_legacy_sync_preserves_verified_wide_rows():
    class Response:
        def json(self):
            return [{"ticker": "6623", "period": "2026-03-31", "quarter": "FY"}]

    class API:
        rest_url = "https://example.invalid/rest/v1"
        headers = {}

        def _request(self, method, url, **kwargs):
            assert method == "GET"
            assert kwargs["params"]["source"] == "eq.official_ir_verified"
            return Response()

    rows = [
        {"ticker": "6623", "period": "2026-03-31", "quarter": "FY", "sales": 128000},
        {"ticker": "6623", "period": "2026-03-31", "quarter": "3Q", "sales": 92756},
    ]
    assert _exclude_verified_financials(API(), rows) == rows[1:]
