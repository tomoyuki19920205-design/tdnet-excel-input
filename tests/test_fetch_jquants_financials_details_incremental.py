import json
import sqlite3

import pytest
import requests

import tools.fetch_jquants_financials as fetcher


def _summary(code: str, date: str, disc_no: str) -> dict:
    return {
        "Code": code,
        "DiscDate": date,
        "DiscNo": disc_no,
        "CurFYEn": "2027-03-31",
        "CurPerType": "1Q",
        "DocType": "1QFinancialStatements_Consolidated_JP",
    }


def _row(item: dict, *, period: str | None = None) -> dict:
    return {
        "local_code": item["Code"],
        "disclosed_date": item["DiscDate"],
        "current_fiscal_year_end_date": "2027-03-31",
        "type_of_current_period": period or "1Q",
        "type_of_document": item["DocType"],
        "net_sales": 100,
        "gross_profit": None,
        "operating_profit": 10,
        "profit_before_tax": None,
        "raw_json": json.dumps(item),
        "fetched_at": "2026-08-21T00:00:00+09:00",
    }


def _detail(item: dict, gross_profit: int = 40) -> dict:
    return {
        "Code": item["Code"],
        "DiscDate": item["DiscDate"],
        "DiscNo": item["DiscNo"],
        "DocType": item["DocType"],
        "FS": {"Gross profit (loss)": str(gross_profit)},
    }


def _db(tmp_path, items_with_periods):
    path = tmp_path / "jquants.db"
    conn = sqlite3.connect(path)
    fetcher._ensure_table(conn)
    fetcher.upsert_rows(
        conn, [_row(item, period=period) for item, period in items_with_periods]
    )
    return conn


def _run(conn, tmp_path, monkeypatch, fake_fetch):
    monkeypatch.setattr(fetcher, "_fetch_details_for_date", fake_fetch)
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)
    return fetcher._supplement_gross_profit_from_details(
        conn,
        object(),
        {},
        None,
        date_from="2026-08-01",
        date_to="2026-08-31",
        state_path=tmp_path / "details-state.json",
        budget_sec=60,
    )


def test_b1_fast_success_batches_all_documents_for_the_date(tmp_path, monkeypatch):
    first = _summary("10010", "2026-08-21", "doc-1")
    second = _summary("10020", "2026-08-21", "doc-2")
    conn = _db(tmp_path, [(first, "1Q"), (second, "1Q")])
    calls = []

    def fake_fetch(_session, _headers, date, **_kwargs):
        calls.append(date)
        return [_detail(first, 41), _detail(second, 42)], 1

    stats = _run(conn, tmp_path, monkeypatch, fake_fetch)

    assert calls == ["2026-08-21"]
    assert stats["documents_attempted"] == 2
    assert stats["details_success"] == 2
    assert stats["supplemented"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM jquants_financials_normalized WHERE gross_profit IS NOT NULL"
    ).fetchone()[0] == 2


def test_details_does_not_overwrite_existing_metric_without_force(tmp_path, monkeypatch):
    item = _summary("547A0", "2026-08-12", "bank-doc")
    conn = _db(tmp_path, [(item, "1Q")])
    detail = _detail(item)
    detail["FS"]["Operating income BNK"] = "9871000000"

    def fake_fetch(_session, _headers, _date, **_kwargs):
        return [detail], 1

    _run(conn, tmp_path, monkeypatch, fake_fetch)
    operating_profit = conn.execute(
        "SELECT operating_profit FROM jquants_financials_normalized"
    ).fetchone()[0]
    assert operating_profit == 10


def test_b2_b6_interruption_preserves_success_and_resume_fetches_remaining_only(
    tmp_path, monkeypatch
):
    newest = _summary("10010", "2026-08-02", "doc-new")
    older = _summary("10020", "2026-08-01", "doc-old")
    conn = _db(tmp_path, [(newest, "1Q"), (older, "1Q")])
    first_run_calls = []

    def interrupted(_session, _headers, date, **_kwargs):
        first_run_calls.append(date)
        if date == "2026-08-01":
            raise fetcher.DetailsBudgetExhausted("simulated interruption")
        return [_detail(newest)], 1

    first_stats = _run(conn, tmp_path, monkeypatch, interrupted)
    assert first_run_calls == ["2026-08-02", "2026-08-01"]
    assert first_stats["details_pending"] == 1
    assert conn.execute(
        "SELECT gross_profit FROM jquants_financials_normalized WHERE local_code='10010'"
    ).fetchone()[0] == 40

    resumed_calls = []

    def resumed(_session, _headers, date, **_kwargs):
        resumed_calls.append(date)
        return [_detail(older)], 1

    second_stats = _run(conn, tmp_path, monkeypatch, resumed)
    assert resumed_calls == ["2026-08-01"]
    assert second_stats["details_cache_hits"] == 1
    assert second_stats["details_pending"] == 0


def test_b3_429_honors_retry_after(monkeypatch):
    responses = []
    for status in (429, 200):
        response = requests.Response()
        response.status_code = status
        response.headers["Retry-After"] = "7" if status == 429 else ""
        responses.append(response)

    class Session:
        def get(self, *_args, **_kwargs):
            return responses.pop(0)

    waits = []
    monkeypatch.setattr(fetcher, "_bounded_sleep", lambda wait, _deadline: waits.append(wait))
    result = fetcher._api_get(Session(), "/fins/details", {}, {})
    assert result.status_code == 200
    assert waits == [7.0]


def test_b4_no_detail_is_terminal_and_not_retried(tmp_path, monkeypatch):
    item = _summary("10010", "2026-08-21", "forecast-only")
    conn = _db(tmp_path, [(item, "1Q")])
    calls = []

    def no_detail(_session, _headers, date, **_kwargs):
        calls.append(date)
        return [], 1

    first = _run(conn, tmp_path, monkeypatch, no_detail)
    second = _run(conn, tmp_path, monkeypatch, no_detail)
    assert first["details_no_detail"] == 1
    assert second["details_cache_hits"] == 1
    assert calls == ["2026-08-21"]


def test_b5_5xx_retry_is_bounded(monkeypatch):
    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            response = requests.Response()
            response.status_code = 503
            return response

    session = Session()
    monkeypatch.setattr(fetcher, "_bounded_sleep", lambda _wait, _deadline: None)
    with pytest.raises(RuntimeError, match="リトライ後"):
        fetcher._api_get(session, "/fins/details", {}, {})
    assert session.calls == fetcher._MAX_RETRIES + 1


def test_b7_duplicate_document_ids_are_fetched_once(tmp_path, monkeypatch):
    item = _summary("10010", "2026-08-21", "shared-doc")
    conn = _db(tmp_path, [(item, "1Q"), (item, "2Q")])
    calls = []

    def fake_fetch(_session, _headers, date, **_kwargs):
        calls.append(date)
        return [_detail(item)], 1

    stats = _run(conn, tmp_path, monkeypatch, fake_fetch)
    assert calls == ["2026-08-21"]
    assert stats["documents_total"] == 1
    assert stats["documents_attempted"] == 1
    assert stats["supplemented"] == 2


def test_b8_nightly_rerun_uses_persistent_document_cache(tmp_path, monkeypatch):
    item = _summary("10010", "2026-08-21", "cached-doc")
    conn = _db(tmp_path, [(item, "1Q")])
    calls = []

    def fake_fetch(_session, _headers, date, **_kwargs):
        calls.append(date)
        return [_detail(item)], 1

    _run(conn, tmp_path, monkeypatch, fake_fetch)
    rerun = _run(conn, tmp_path, monkeypatch, fake_fetch)
    assert calls == ["2026-08-21"]
    assert rerun["details_api_calls"] == 0
    assert rerun["details_cache_hits"] == 1
