from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lib.ny_market import (
    NYMarketValidationError,
    connect_db,
    mark_run,
    rows_for_sync,
    upsert_report,
    validate_payload,
)
from tools.company_news_inbox_worker import WorkerPaths, run_once
from tools.write_ny_market_payload import publish
from tools.apply_ny_market_sqlite_migration import apply as apply_sqlite_migration


ROOT = Path(__file__).parents[1]


def payload(*, report_date: str = "2026-09-01", status: str = "open", headline: str = "NY morning"):
    stock = lambda index: {
        "ticker": f"T{index}", "company_name": f"Company {index}", "change_pct": float(30 - index),
        "market_cap": None if index == 0 else 1_000_000 + index, "reason": "大型当日材料未確認 / momentum",
    }
    return {
        "schema_version": "ny_market_daily_v1",
        "stable_key": f"ny_market_daily:{report_date}",
        "report_type": "ny_market_daily",
        "report_date_jst": report_date,
        "generated_at": f"{report_date}T07:05:00+09:00",
        "market_session_date": "2026-08-31" if report_date == "2026-09-01" else "2026-09-04",
        "market_status": status,
        "headline": headline,
        "summary_bullets": [f"summary {i}" for i in range(5)],
        "index_moves": {"SOX": {"change_pct": 1}, "S&P500": {"change_pct": 0.2}, "Dow": {"change_pct": -0.1}, "Russell 2000": {"change_pct": 0.4}},
        "sector_moves": [{"sector": f"s{i}", "change_pct": i} for i in range(11)],
        "notable_gainers": [{"ticker": f"G{i}", "change_pct": i, "reason": "news"} for i in range(10)],
        "notable_losers": [{"ticker": f"L{i}", "change_pct": -i, "reason": "news"} for i in range(10)],
        "top_gainers_20": [stock(i) for i in range(20)],
        "earnings": [],
        "after_hours_earnings": [],
        "major_news": [{"title": f"news {i}"} for i in range(10)],
        "commodities": [{"name": "WTI", "price": 70, "change_pct": 2, "reason": "supply"}],
        "report_markdown": "# NY市場モーニングレポート\n\n" + ("完成した長文レポート。" * 80),
        "sources": [{"title": "Official source", "publisher": "Publisher", "url": "https://example.com/source", "published_at": "2026-09-01T00:00:00Z"}],
    }


def test_schema_stable_key_and_weekend_payload():
    validated = validate_payload(payload())
    assert validated.report["stable_key"] == "ny_market_daily:2026-09-01"
    weekend = payload(report_date="2026-09-06", status="holiday_or_weekend")
    assert validate_payload(weekend).report["market_session_date"] == "2026-09-04"
    broken = payload()
    broken["stable_key"] = "ny_market_daily:2026-08-31"
    with pytest.raises(NYMarketValidationError, match="stable_key"):
        validate_payload(broken)


def test_platform_citations_are_rejected_but_normal_markdown_links_are_allowed():
    clean = payload()
    clean["report_markdown"] += "\n[Source](https://example.com/source)"
    validate_payload(clean)
    dirty = payload()
    dirty["report_markdown"] += " turn0search1"
    with pytest.raises(NYMarketValidationError, match="platform-specific"):
        validate_payload(dirty)


def test_same_day_reingest_is_upsert_and_updates_newer_report(tmp_path):
    db = tmp_path / "db.sqlite"
    first = validate_payload(payload(headline="first"))
    conn = connect_db(db)
    mark_run(conn, first.run, "running", increment=True)
    upsert_report(conn, first)
    mark_run(conn, first.run, "success")
    second_payload = payload(headline="newer")
    second_payload["generated_at"] = "2026-09-01T07:30:00+09:00"
    second = validate_payload(second_payload)
    mark_run(conn, second.run, "running", increment=True)
    upsert_report(conn, second)
    mark_run(conn, second.run, "success")
    row = conn.execute("SELECT count(*) AS c,max(headline) AS headline FROM canonical_ny_market_reports").fetchone()
    assert row["c"] == 1
    assert row["headline"] == "newer"
    older_payload = payload(headline="older retry")
    older_payload["generated_at"] = "2026-09-01T07:00:00+09:00"
    upsert_report(conn, validate_payload(older_payload))
    assert conn.execute("SELECT headline FROM canonical_ny_market_reports").fetchone()[0] == "newer"
    run = conn.execute("SELECT status,attempt FROM canonical_ny_market_report_runs").fetchone()
    assert (run["status"], run["attempt"]) == ("success", 2)
    assert isinstance(rows_for_sync(conn, "canonical_ny_market_reports")[0]["sources"], list)
    conn.close()


def test_atomic_publisher_leaves_only_complete_json(tmp_path):
    input_path = tmp_path / "payload.tmp.json"
    input_path.write_text(json.dumps(payload(), ensure_ascii=False), encoding="utf-8")
    inbox = tmp_path / "inbox"
    target = publish(input_path, inbox)
    assert validate_payload(json.loads(target.read_text(encoding="utf-8")))
    assert list(inbox.glob("*.tmp")) == []


def test_worker_dispatch_retry_and_company_non_interference(tmp_path):
    root = tmp_path
    inbox = root / "data" / "news_inbox"
    inbox.mkdir(parents=True)
    path = inbox / "ny_market_daily_20260901_first.json"
    path.write_text(json.dumps(payload(), ensure_ascii=False), encoding="utf-8")
    paths = WorkerPaths.from_values(root=root, db=root / "decision.db")
    company_calls = []
    ny_calls = []

    def fail_sync(db_path, dry_run):
        ny_calls.append((db_path, dry_run))
        raise RuntimeError("temporary 503")

    first = run_once(paths, sync_func=lambda *args: company_calls.append(args) or {}, ny_sync_func=fail_sync)
    assert first["status"] == "completed_with_errors"
    assert company_calls == []
    conn = sqlite3.connect(root / "decision.db")
    assert conn.execute("SELECT count(*) FROM canonical_ny_market_reports").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM canonical_ny_market_report_runs").fetchone()[0] == "retry_pending"
    conn.close()

    second = run_once(paths, sync_func=lambda *args: company_calls.append(args) or {}, ny_sync_func=lambda *args: ny_calls.append(args) or {"canonical_ny_market_reports": 1})
    assert second["status"] == "completed"
    assert company_calls == []
    conn = sqlite3.connect(root / "decision.db")
    assert conn.execute("SELECT status FROM canonical_ny_market_report_runs").fetchone()[0] == "success"
    conn.close()


def test_malformed_ny_payload_is_quarantined_not_company_ingested(tmp_path):
    inbox = tmp_path / "data" / "news_inbox"
    inbox.mkdir(parents=True)
    malformed = payload()
    malformed["sector_moves"] = []
    path = inbox / "ny_market_daily_20260901_bad.json"
    path.write_text(json.dumps(malformed), encoding="utf-8")
    company_calls = []
    result = run_once(
        WorkerPaths.from_values(root=tmp_path, db=tmp_path / "db.sqlite"),
        sync_func=lambda *args: company_calls.append(args) or {},
        ny_sync_func=lambda *args: {},
    )
    assert result["quarantined"] == 1
    assert company_calls == []
    assert (inbox / "quarantine" / path.name).exists()


def test_postgres_migration_unions_third_stream_without_company_detail_view_change():
    sql = (ROOT / "migrations" / "019_ny_market_daily.sql").read_text(encoding="utf-8")
    assert "canonical_ny_market_reports" in sql
    assert "api_latest_news_stream" in sql
    assert "'ny_market_daily'" in sql
    assert "CREATE OR REPLACE VIEW api_latest_news_events" not in sql
    assert "FROM canonical_news_events" in sql and "FROM canonical_sector_reports" in sql


def test_sqlite_migration_is_additive_and_requires_exact_target(tmp_path):
    db = tmp_path / "production.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE canonical_news_events (id TEXT)")
    conn.execute("INSERT INTO canonical_news_events VALUES ('keep')")
    conn.commit()
    conn.close()
    result = apply_sqlite_migration(db, expected=db, backup_dir=tmp_path / "backup")
    assert result["status"] == "applied"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM canonical_news_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM canonical_ny_market_reports").fetchone()[0] == 0
    conn.close()
    with pytest.raises(Exception, match="unsafe DB path"):
        apply_sqlite_migration(db, expected=tmp_path / "other.db", backup_dir=tmp_path / "backup2")


def test_scheduled_prompt_requires_report_output_on_storage_failure_and_closed_days():
    prompt = (ROOT / "config" / "ny_market_daily_scheduled_prompt.txt").read_text(encoding="utf-8")
    for required in ("土日", "holiday_or_weekend", "Company Viewer保存に失敗", "完成レポート全文", "write_ny_market_payload.py"):
        assert required in prompt
