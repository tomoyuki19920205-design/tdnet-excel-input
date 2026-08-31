import json
import sqlite3
from datetime import datetime, timedelta, timezone

from tools.company_news_soak_audit import (
    _projection,
    _storage_delta,
    _task_run_metrics,
    sqlite_storage_snapshot,
)

JST = timezone(timedelta(hours=9))


def test_sqlite_storage_snapshot_is_read_only_and_reports_news_relations(tmp_path):
    db = tmp_path / "news.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE canonical_news_events(event_id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE);
        CREATE TABLE canonical_news_scan_runs(scan_run_id TEXT PRIMARY KEY);
        CREATE INDEX ix_test_news_event ON canonical_news_events(dedupe_key);
        """
    )
    connection.execute("INSERT INTO canonical_news_events VALUES('event-1','key-1')")
    connection.executemany(
        "INSERT INTO canonical_news_scan_runs VALUES(?)",
        [("run-1",), ("run-2",)],
    )
    connection.commit()
    connection.close()

    before = db.read_bytes()
    result = sqlite_storage_snapshot(db)

    assert db.read_bytes() == before
    assert result["database_bytes"] == len(before)
    assert result["tables"]["canonical_news_events"]["rows"] == 1
    assert result["tables"]["canonical_news_scan_runs"]["rows"] == 2
    assert result["relevant_total_relation_bytes"] >= result["relevant_index_bytes"] > 0


def test_storage_delta_and_projection_use_relation_growth_not_database_total():
    before = {
        "supabase": {
            "available": True,
            "database_bytes": 2_000,
            "relevant_index_bytes": 200,
            "relevant_total_relation_bytes": 1_000,
            "tables": {
                "canonical_news_events": {"rows": 10, "table_bytes": 400, "index_bytes": 100, "total_relation_bytes": 600},
                "canonical_news_scan_runs": {"rows": 20, "table_bytes": 300, "index_bytes": 100, "total_relation_bytes": 400},
            },
        }
    }
    after = {
        "supabase": {
            "available": True,
            "database_bytes": 9_999,
            "relevant_index_bytes": 260,
            "relevant_total_relation_bytes": 2_200,
            "tables": {
                "canonical_news_events": {"rows": 14, "table_bytes": 800, "index_bytes": 140, "total_relation_bytes": 1_100},
                "canonical_news_scan_runs": {"rows": 30, "table_bytes": 700, "index_bytes": 120, "total_relation_bytes": 1_100},
            },
        }
    }

    delta = _storage_delta(before, after, "supabase")
    projection = _projection(delta, completed=10, news_present=4, events=4, after=after["supabase"])

    assert delta["database_bytes"] == 7_999
    assert delta["relevant_total_relation_bytes"] == 1_200
    assert projection["bytes_per_scanned_company"] == 120
    assert projection["bytes_per_news_present_company"] == 300
    assert projection["bytes_per_event"] == 125
    assert projection["bytes_per_scan"] == 70
    assert projection["growth"]["week_raw"] == 456_000
    assert projection["growth"]["week_safety_2x"] == 912_000


def test_task_run_metrics_include_duration_percentiles_and_overlap(tmp_path):
    queue_id = "pilot200-fixture"
    runs = tmp_path / "data" / "news_work" / "task_runs" / "task01" / "runs.jsonl"
    runs.parent.mkdir(parents=True)
    start = datetime(2026, 8, 31, 10, 0, tzinfo=JST)
    records = []
    for index, duration in enumerate((600, 1_200, 2_700, 3_300), start=1):
        run_start = start + timedelta(hours=index - 1)
        records.append(
            {
                "schema_version": "company_news_task_run_v1",
                "task_id": "task01",
                "run_id": f"run-{index}",
                "queue_id": queue_id,
                "started_at": run_start.isoformat(),
                "completed_at": (run_start + timedelta(seconds=duration)).isoformat(),
                "snapshot_count": 4,
                "success_count": 4,
                "failure_count": 0,
                "no_news_count": 2,
                "news_present_count": 2,
            }
        )
    runs.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    events = runs.with_name("events.jsonl")
    events.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": "company_news_task_event_v1",
                    "at": (start + timedelta(minutes=index)).isoformat(),
                    "event": event,
                    "task_id": "task01",
                    "queue_id": queue_id,
                }
            )
            + "\n"
            for index, event in enumerate(("busy_skip", "stale_guard_recovered"), start=1)
        ),
        encoding="utf-8",
    )

    result = _task_run_metrics(
        tmp_path,
        queue_id,
        start.isoformat(),
        (start + timedelta(hours=4)).isoformat(),
    )

    assert result["total_scheduled_runs"] == 4
    assert result["productive_runs"] == 4
    assert result["companies_per_productive_run"] == 4
    assert result["run_duration_seconds"] == {
        "average": 1_950,
        "p50": 1_950,
        "p95": 3_300,
        "max": 3_300,
    }
    assert result["overlap_count"] == 0
    assert result["busy_skipped_runs_observed"] == 1
    assert result["stale_guard_recovery_count"] == 1
