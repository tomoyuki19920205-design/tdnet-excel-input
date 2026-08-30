import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import tools.sector_weekly_scheduler as scheduler
from lib.sector_weekly import (
    JST, SECTORS, SectorValidationError, connect_sector_db, dedupe_key, scheduled_sector,
    sector_name, validate_report, weekly_window,
)
from tools.sector_weekly_scheduler import assemble_payload, run_scheduled, run_sector


def _content() -> dict:
    return {
        "importance": "A+",
        "direction": "mixed",
        "summary_bullets": ["料金改定の影響", "燃料価格の逆風", "原子力稼働率の改善"],
        "watchlist_companies": [{"code": "9503", "name": "関西電力", "direction": "positive"}],
        "next_week_watchpoints": ["燃料価格を確認"],
        "missed_candidates": ["地方自治体資料の更新"],
        "full_report_md": "# 【東証33業種週次】電気・ガス業\n\n結論：重要度A+。\n\n## 今週の要旨\n十分な長さのテスト本文です。" * 5,
        "sources": [{
            "title": "電気料金資料", "url": "https://example.com/primary.pdf", "source_name": "資源エネルギー庁",
            "source_type": "government", "published_at": "2026-08-28T10:00:00+09:00",
        }],
    }


def test_fixed_sector_mapping_boundaries():
    assert len(SECTORS) == 33
    assert sector_name(1) == "水産・農林業"
    assert sector_name(20) == "電気・ガス業"
    assert sector_name(33) == "サービス業"


@pytest.mark.parametrize(("at", "expected"), [
    ("2026-09-05T06:00:00+09:00", 1),
    ("2026-09-05T23:00:00+09:00", 18),
    ("2026-09-06T00:00:00+09:00", 19),
    ("2026-09-06T14:00:00+09:00", 33),
    ("2026-09-06T15:00:00+09:00", None),
])
def test_schedule_boundaries(at, expected):
    assert scheduled_sector(datetime.fromisoformat(at)) == expected


def test_all_33_sectors_share_the_same_weekly_period():
    expected = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    last = weekly_window(datetime.fromisoformat("2026-09-06T14:00:00+09:00"))
    assert expected == last
    assert expected.period_start.isoformat() == "2026-08-29T06:00:00+09:00"
    assert expected.period_end.isoformat() == "2026-09-05T05:59:59+09:00"


def test_validate_preserves_arrays_markdown_sources_and_stable_key():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    payload = assemble_payload(_content(), 20, window, datetime.fromisoformat("2026-09-06T01:02:33+09:00"))
    validated = validate_report(payload, expected_code=20, expected_window=window).report
    assert json.loads(validated["summary_bullets"])[0] == "料金改定の影響"
    assert json.loads(validated["sources"])[0]["source_type"] == "government"
    assert validated["full_report_md"].startswith("# 【東証33業種週次】")
    assert validated["dedupe_key"] == "sector_weekly:2026-09-05:20"


def test_wrong_period_and_invalid_json_shape_are_rejected():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    payload = assemble_payload(_content(), 20, window)
    payload["period_start"] = "2026-08-30T06:00:00+09:00"
    with pytest.raises(SectorValidationError, match="common weekly window"):
        validate_report(payload, expected_code=20, expected_window=window)


def test_source_date_precision_is_preserved_without_inventing_time():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["sources"][0]["published_at"] = "2026-08-28"
    payload = assemble_payload(content, 20, window)
    source = json.loads(validate_report(payload, expected_code=20, expected_window=window).report["sources"])[0]
    assert source["published_at"] == "2026-08-28"


def test_card_bullets_remove_web_citations_and_are_bounded():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["summary_bullets"][0] = "重要材料 " + "長" * 300 + " ([example.com](https://example.com/source))"
    payload = assemble_payload(content, 20, window)
    assert len(payload["summary_bullets"][0]) <= 240
    assert "https://" not in payload["summary_bullets"][0]
    payload = assemble_payload(_content(), 20, window)
    payload["summary_bullets"] = "not-an-array"
    with pytest.raises(SectorValidationError, match="summary_bullets"):
        validate_report(payload, expected_code=20, expected_window=window)


def test_upsert_and_retry_do_not_create_duplicate_cards(tmp_path: Path):
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    db = tmp_path / "news.db"
    inbox = tmp_path / "inbox"
    first = run_sector(20, window, db_path=db, inbox=inbox, log_path=tmp_path / "log.jsonl", dry_run_sync=True, research_func=lambda *_: _content())
    second = run_sector(20, window, db_path=db, inbox=inbox, log_path=tmp_path / "log.jsonl", dry_run_sync=True, research_func=lambda *_: _content())
    assert first["status"] == "success"
    assert second["status"] == "already_success"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM canonical_sector_report_runs WHERE run_id=?", (dedupe_key(window, 20),)).fetchone()[0] == "success"


def test_invalid_research_output_retries_without_creating_report(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(scheduler.time, "sleep", lambda *_: None)
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    bad = _content()
    bad["sources"] = []
    result = run_sector(20, window, db_path=tmp_path / "news.db", inbox=tmp_path / "inbox", log_path=tmp_path / "log.jsonl", dry_run_sync=True, research_func=lambda *_: bad)
    assert result["status"] == "retry_pending"
    conn = connect_sector_db(tmp_path / "news.db")
    assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 0
    assert conn.execute("SELECT attempt_count FROM canonical_sector_report_runs WHERE run_id=?", (dedupe_key(window, 20),)).fetchone()[0] == 3


def test_company_news_schema_is_not_modified_by_sector_migration():
    sql = (Path(__file__).parents[1] / "migrations" / "017_sector_weekly_reports.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE canonical_news_events" not in sql
    assert "CREATE OR REPLACE VIEW api_latest_news_events" not in sql
    assert "api_latest_news_stream" in sql


def test_scheduler_can_be_enabled_without_running_before_first_saturday(tmp_path: Path):
    result = run_scheduled(
        datetime.fromisoformat("2026-08-30T10:00:00+09:00"),
        db_path=tmp_path / "news.db", inbox=tmp_path / "inbox", log_path=tmp_path / "log.jsonl",
        lock_path=tmp_path / "lock", dry_run_sync=True,
        not_before=datetime.fromisoformat("2026-09-05T06:00:00+09:00"),
    )
    assert result["status"] == "not_started"


def test_stale_scheduler_lock_is_recovered(tmp_path: Path, monkeypatch):
    lock = tmp_path / "scheduler.lock"
    lock.write_text("pid=42424242\n", encoding="utf-8")
    monkeypatch.setattr(scheduler, "_pid_is_alive", lambda _pid: False)
    result = run_scheduled(
        datetime.fromisoformat("2026-08-30T10:00:00+09:00"), db_path=tmp_path / "news.db",
        inbox=tmp_path / "inbox", log_path=tmp_path / "log.jsonl", lock_path=lock, dry_run_sync=True,
        not_before=datetime.fromisoformat("2026-09-05T06:00:00+09:00"),
    )
    assert result["status"] == "not_started"
    assert not lock.exists()
