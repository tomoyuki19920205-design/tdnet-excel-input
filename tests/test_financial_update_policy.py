"""Isolated save-path tests: the fake REST store uses the production keys."""
import json
import sqlite3

from lib.pipeline.financial_update_policy import (
    guarded_wide_upsert, plan_canonical_actual_updates,
    plan_canonical_forecast_updates, plan_wide_updates,
)
from tools.repair_company_viewer_6623 import MANIFEST, build_rows


class Response:
    def __init__(self, rows):
        self.rows = rows

    def json(self):
        return self.rows


class Store:
    rest_url = "https://isolated.invalid/rest/v1"
    headers = {}

    def __init__(self, wide=None, canonical=None):
        self.wide = {self.key(r): dict(r) for r in (wide or [])}
        self.canonical = list(canonical or [])
        self.posts = []

    @staticmethod
    def key(row):
        return row["ticker"], row["period"], row["quarter"]

    def _request(self, method, url, **kwargs):
        assert method == "GET"
        rows = self.canonical if url.endswith("/canonical_financials") else list(self.wide.values())
        params = kwargs["params"]
        tickers = set(params["ticker"][4:-1].split(","))
        rows = [r for r in rows if r["ticker"] in tickers]
        if "metric" in params:
            rows = [r for r in rows if r["metric"] in {"sales", "gross_profit", "operating_profit"}]
        return Response(rows)

    def upsert_batch(self, rows):
        for row in rows:
            self.wide[self.key(row)] = dict(row)
        self.posts.extend(rows)


def verified_store():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    canonical, _, _ = build_rows(manifest)
    wide = [{"ticker": "6623", "period": item["period"], "quarter": item["quarter"],
             "sales": item["pl"]["sales"], "gross_profit": item["pl"]["gross_profit"],
             "operating_profit": item["pl"]["operating_profit"], "source": "tdnet",
             "updated_at": "2026-10-01T00:00:00Z"} for item in manifest]
    return manifest, Store(wide, canonical)


def test_all_fourteen_periods_pass_save_decision_without_rewriting():
    manifest, store = verified_store()
    candidates = [dict(store.wide[("6623", item["period"], item["quarter"])],
                       source="tdnet", updated_at="2026-10-30T00:00:00Z") for item in manifest]
    candidate = next(r for r in candidates if (r["period"], r["quarter"]) == ("2026-03-31", "FY"))
    candidate["sales"] = 128000
    candidate["operating_profit"] = 10000
    candidate["gross_profit"] = None
    counts = guarded_wide_upsert(store, candidates, store.upsert_batch)
    assert counts["candidates"] == 14
    assert counts["write_rows"] == 0
    assert store.wide[("6623", "2026-03-31", "FY")]["sales"] == 129382
    assert not store.posts


def test_nightly_five_null_gross_profit_candidates_are_noops():
    _, store = verified_store()
    keys = [("2026-03-31", q) for q in ("1Q", "2Q", "3Q", "FY")] + [("2027-03-31", "1Q")]
    candidates = [dict(store.wide[("6623", *key)], source="jquants", gross_profit=None) for key in keys]
    counts = guarded_wide_upsert(store, candidates, store.upsert_batch)
    assert counts["candidates"] == 5 and counts["write_rows"] == 0
    assert all(store.wide[("6623", *key)]["gross_profit"] is not None for key in keys)


def test_missing_field_is_filled_from_verified_evidence_and_repeat_is_noop():
    _, store = verified_store()
    key = ("6623", "2026-03-31", "1Q")
    store.wide[key]["gross_profit"] = None
    incoming = dict(store.wide[key], source="jquants")
    first = guarded_wide_upsert(store, [incoming], store.upsert_batch)
    second = guarded_wide_upsert(store, [incoming], store.upsert_batch)
    assert first["write_rows"] == 1 and second["write_rows"] == 0
    assert store.wide[key]["gross_profit"] == 4810
    assert len(store.wide) == 14


def test_formal_correction_wins_but_newer_unmarked_filing_does_not():
    _, store = verified_store()
    key = ("6623", "2026-03-31", "FY")
    prior = next(r for r in store.canonical if Store.key(r) == key and r["metric"] == "sales")
    newer_unmarked = dict(prior, value=130000, filing_id="official_ir|new|https://issuer.example/new.pdf|p8",
                          disclosure_datetime="2026-06-01T00:00:00+09:00")
    store.canonical.append(newer_unmarked)
    candidate = dict(store.wide[key], sales=130000, source="tdnet")
    assert plan_wide_updates(store, [candidate])[1]["write_rows"] == 0
    store.canonical.pop()
    unrelated_correction = dict(newer_unmarked, correction_flag=True, document_type="correction",
                                corrects_filing_id="official_ir|unrelated|https://issuer.example/other.pdf|p8")
    store.canonical.append(unrelated_correction)
    assert plan_wide_updates(store, [candidate])[1]["write_rows"] == 0
    store.canonical.pop()
    correction = dict(newer_unmarked, correction_flag=True, document_type="correction")
    store.canonical.append(correction)
    counts = guarded_wide_upsert(store, [candidate], store.upsert_batch)
    assert counts["write_rows"] == 1 and store.wide[key]["sales"] == 130000


def test_forecast_cannot_replace_actual_but_newer_forecast_can_update():
    _, store = verified_store()
    actual_key = ("6623", "2026-03-31", "FY")
    bad = dict(store.wide[actual_key], source="jquants_forecast_fy", sales=128000,
               disclosure_datetime="2026-02-25T00:00:00+09:00")
    assert guarded_wide_upsert(store, [bad], store.upsert_batch)["write_rows"] == 0
    future = {"ticker": "9999", "period": "2027-03-31", "quarter": "FY",
              "sales": 100, "gross_profit": None, "operating_profit": 10,
              "source": "jquants_nxf", "disclosure_datetime": "2026-05-01", "updated_at": "2026-05-02"}
    assert guarded_wide_upsert(store, [future], store.upsert_batch)["write_rows"] == 1
    store.canonical.append({**future, "metric": "sales", "value": 100, "source_priority": 10,
                            "filing_id": "forecast-old", "unit": "millions_jpy"})
    newer = dict(future, sales=120, disclosure_datetime="2026-10-30")
    assert guarded_wide_upsert(store, [newer], store.upsert_batch)["write_rows"] == 1
    assert store.wide[("9999", "2027-03-31", "FY")]["sales"] == 120
    assert guarded_wide_upsert(store, [newer], store.upsert_batch)["write_rows"] == 0


def test_unrelated_new_actual_is_inserted_and_manual_row_is_preserved():
    _, store = verified_store()
    fresh = {"ticker": "1234", "period": "2026-12-31", "quarter": "1Q", "sales": 50,
             "gross_profit": 12, "operating_profit": 4, "source": "tdnet"}
    assert guarded_wide_upsert(store, [fresh], store.upsert_batch)["write_rows"] == 1
    store.wide[Store.key(fresh)]["source"] = "manual"
    assert guarded_wide_upsert(store, [dict(fresh, sales=60)], store.upsert_batch)["write_rows"] == 0
    assert store.wide[Store.key(fresh)]["sales"] == 50


def test_nightly_sync_runs_the_real_wide_save_path_in_isolation(monkeypatch):
    from tools import sync_financials
    from lib.pipeline import db

    _, store = verified_store()
    key = ("6623", "2026-03-31", "1Q")
    candidate = dict(store.wide[key], source="jquants", gross_profit=None)
    monkeypatch.setattr(sync_financials, "read_sqlite", lambda *a, **kw: ([candidate], {"total_raw": 1, "codes": 1}))
    monkeypatch.setattr(sync_financials, "read_forecast_rows", lambda *a, **kw: [])
    monkeypatch.setattr(sync_financials, "_SupabaseAPI", lambda *a, **kw: store)
    monkeypatch.setattr(sync_financials, "_upsert_company_names", lambda *a, **kw: None)
    monkeypatch.setattr(db, "load_env", lambda *a, **kw: None)
    monkeypatch.setattr(db, "get_supabase_write_config", lambda *a, **kw: None)
    store.select_count = lambda table: {"total_rows": len(store.wide)}
    store.close = lambda: None
    original = store.upsert_batch
    store.upsert_batch = lambda table, rows, on_conflict="": (original(rows), len(rows))[1]

    stats = sync_financials.sync("isolated.db", "https://isolated.invalid", "dummy", dry_run=False, ticker="6623")
    assert stats["upserted"] == 0
    assert store.wide[key]["gross_profit"] == 4810
    assert store.posts == []

    fresh = {"ticker": "1234", "period": "2026-12-31", "quarter": "1Q", "sales": 50,
             "gross_profit": 12, "operating_profit": 4, "source": "jquants"}
    monkeypatch.setattr(sync_financials, "read_sqlite", lambda *a, **kw: ([fresh], {"total_raw": 1, "codes": 1}))
    stats = sync_financials.sync("isolated.db", "https://isolated.invalid", "dummy", dry_run=False, ticker="1234")
    assert stats["upserted"] == 1
    assert store.wide[Store.key(fresh)]["sales"] == 50


def test_nightly_forecast_save_uses_disclosure_order_in_isolation(monkeypatch):
    from tools import sync_financials
    from lib.pipeline import db

    _, store = verified_store()
    actual = dict(store.wide[("6623", "2026-03-31", "FY")], source="jquants")
    future = {"ticker": "9999", "period": "2027-03-31", "quarter": "FY",
              "sales": 100, "gross_profit": None, "operating_profit": 10,
              "source": "jquants_nxf", "disclosure_datetime": "2026-05-01", "updated_at": "2026-05-02"}
    store.wide[Store.key(future)] = dict(future)
    store.canonical.append({**future, "metric": "sales", "value": 100, "source_priority": 10,
                            "filing_id": "forecast-old", "unit": "millions_jpy"})
    newer = dict(future, sales=120, disclosure_datetime="2026-10-30")
    monkeypatch.setattr(sync_financials, "read_sqlite", lambda *a, **kw: ([actual], {"total_raw": 1, "codes": 1}))
    monkeypatch.setattr(sync_financials, "read_forecast_rows", lambda *a, **kw: [newer])
    monkeypatch.setattr(sync_financials, "_SupabaseAPI", lambda *a, **kw: store)
    monkeypatch.setattr(sync_financials, "_upsert_company_names", lambda *a, **kw: None)
    monkeypatch.setattr(db, "load_env", lambda *a, **kw: None)
    monkeypatch.setattr(db, "get_supabase_write_config", lambda *a, **kw: None)
    store.select_count = lambda table: {"total_rows": len(store.wide)}
    store.close = lambda: None
    original = store.upsert_batch
    store.upsert_batch = lambda table, rows, on_conflict="": (original(rows), len(rows))[1]

    sync_financials.sync("isolated.db", "https://isolated.invalid", "dummy", dry_run=False)
    assert store.wide[Store.key(future)]["sales"] == 120
    assert store.wide[("6623", "2026-03-31", "FY")]["sales"] == 129382
    assert len(store.posts) == 1

    monkeypatch.setattr(sync_financials, "read_sqlite", lambda *a, **kw: ([], {"total_raw": 0, "codes": 0}))
    newest = dict(newer, sales=130, disclosure_datetime="2026-10-30T12:00:00")
    monkeypatch.setattr(sync_financials, "read_forecast_rows", lambda *a, **kw: [newest])
    sync_financials.sync("isolated.db", "https://isolated.invalid", "dummy", dry_run=False)
    assert store.wide[Store.key(future)]["sales"] == 130


def test_canonical_reimport_skips_preperiod_forecast(tmp_path):
    from lib.pipeline.canonical_sync import _select_financials_rows

    db_path = tmp_path / "quarterly.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE quarterly_results (company_code TEXT, fiscal_year_end TEXT, quarter TEXT, updated_at TEXT, sales INTEGER)")
        conn.executemany("INSERT INTO quarterly_results VALUES (?,?,?,?,?)", [
            ("6623", "2026-03-31", "FY", "2026-02-25", 128000),
            ("6623", "2026-03-31", "FY", "2026-05-15", 129382),
        ])
    rows, fallback = _select_financials_rows(str(db_path), target_keys=[("6623", "2026-03-31", "FY")], allow_fallback=False)
    assert not fallback
    assert [r["sales"] for r in rows] == [129382]


def test_canonical_forecast_upsert_keeps_newer_disclosure():
    old = {"ticker": "9999", "period": "2027-03-31", "quarter": "FY", "metric": "sales",
           "value": 100, "source": "jquants_nxf", "source_row_key": "fc-9999-sales",
           "disclosure_datetime": "2026-05-01"}
    store = Store(canonical=[old])
    stale = dict(old, value=90, disclosure_datetime="2026-04-30")
    newer = dict(old, value=120, disclosure_datetime="2026-10-30")
    assert plan_canonical_forecast_updates(store, [stale])[1]["write_rows"] == 0
    assert plan_canonical_forecast_updates(store, [newer])[1]["write_rows"] == 1
    store.canonical.append({**old, "source": "jquants", "source_row_key": "actual-9999-sales"})
    assert plan_canonical_forecast_updates(store, [newer])[1]["write_rows"] == 0


def test_canonical_actual_reimport_is_idempotent_and_missing_metric_inserts():
    old = {"ticker": "6623", "period": "2026-03-31", "quarter": "FY", "metric": "sales",
           "value": 129382, "unit": "millions_jpy", "source": "jquants",
           "source_row_key": "jquants-6623-sales", "source_priority": 2}
    store = Store(canonical=[old])
    assert plan_canonical_actual_updates(store, [dict(old)])[1]["write_rows"] == 0
    assert plan_canonical_actual_updates(store, [dict(old, value=128000)])[1]["write_rows"] == 0
    missing = dict(old, metric="gross_profit", value=21533, source_row_key="jquants-6623-gp")
    assert plan_canonical_actual_updates(store, [missing])[1]["write_rows"] == 1
