#!/usr/bin/env python3
"""Read-only Company News soak metrics and storage snapshots.

The only writes performed by this tool are sanitized JSON audit artifacts under
``output/news_soak_audit``. Queue state, assignments, canonical databases, and
Supabase are always opened read-only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import load_env
from tools.company_news_atomic import atomic_write_json
from tools.company_news_queue import QueuePaths, queue_status
from tools.company_news_work_bridge import BridgePaths

STORAGE_SCHEMA = "company_news_storage_snapshot_v1"
AUDIT_SCHEMA = "company_news_soak_audit_v1"
NEWS_TABLES = ("canonical_news_events", "canonical_news_scan_runs")
PROVISIONED_DATABASE_DISK_BYTES = 8_000_000_000
_JST = timezone(timedelta(hours=9))


class SoakAuditError(RuntimeError):
    pass


def _iso(now: datetime | None = None) -> str:
    value = now or datetime.now(_JST)
    if value.tzinfo is None:
        raise SoakAuditError("audit timestamps must include a timezone")
    return value.astimezone(_JST).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SoakAuditError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SoakAuditError(f"{path} must contain an object")
    return value


def _queue_id(paths: QueuePaths) -> str:
    state = _read_json(paths.state)
    queue_id = state.get("queue_id")
    if not isinstance(queue_id, str) or not queue_id:
        raise SoakAuditError("current queue has no queue_id")
    return queue_id


def _sqlite_table_metrics(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not table_exists:
        return {"rows": 0, "table_bytes": 0, "index_bytes": 0, "total_relation_bytes": 0}
    rows = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    indexes = [str(row[1]) for row in connection.execute(f"PRAGMA index_list({table})")]
    try:
        table_bytes = int(
            connection.execute("SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name=?", (table,)).fetchone()[0]
        )
        index_bytes = sum(
            int(connection.execute("SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name=?", (name,)).fetchone()[0])
            for name in indexes
        )
    except sqlite3.Error:
        table_bytes = 0
        index_bytes = 0
    return {
        "rows": rows,
        "table_bytes": table_bytes,
        "index_bytes": index_bytes,
        "total_relation_bytes": table_bytes + index_bytes,
    }


def sqlite_storage_snapshot(db_path: Path) -> dict[str, Any]:
    db_path = db_path.resolve()
    if not db_path.exists():
        raise SoakAuditError(f"SQLite database does not exist: {db_path}")
    connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {table: _sqlite_table_metrics(connection, table) for table in NEWS_TABLES}
    finally:
        connection.close()
    return {
        "available": True,
        "database_bytes": db_path.stat().st_size,
        "tables": tables,
        "relevant_index_bytes": sum(value["index_bytes"] for value in tables.values()),
        "relevant_total_relation_bytes": sum(value["total_relation_bytes"] for value in tables.values()),
    }


def supabase_storage_snapshot(root: Path) -> dict[str, Any]:
    load_env(str(root))
    dsn = os.environ.get("SUPABASE_POSTGRES_URL")
    if not dsn:
        return {"available": False, "error_type": "not_configured"}
    try:
        import psycopg2

        connection = psycopg2.connect(dsn, connect_timeout=15)
        connection.set_session(readonly=True, autocommit=True)
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT pg_database_size(current_database())")
            database_bytes = int(cursor.fetchone()[0])
            tables: dict[str, dict[str, int]] = {}
            for table in NEWS_TABLES:
                cursor.execute(
                    "SELECT pg_relation_size(%s::regclass), pg_indexes_size(%s::regclass), "
                    "pg_total_relation_size(%s::regclass)",
                    (f"public.{table}", f"public.{table}", f"public.{table}"),
                )
                table_bytes, index_bytes, total_bytes = (int(value) for value in cursor.fetchone())
                cursor.execute(f"SELECT COUNT(*) FROM public.{table}")
                tables[table] = {
                    "rows": int(cursor.fetchone()[0]),
                    "table_bytes": table_bytes,
                    "index_bytes": index_bytes,
                    "total_relation_bytes": total_bytes,
                }
        finally:
            cursor.close()
            connection.close()
        return {
            "available": True,
            "database_bytes": database_bytes,
            "tables": tables,
            "relevant_index_bytes": sum(value["index_bytes"] for value in tables.values()),
            "relevant_total_relation_bytes": sum(value["total_relation_bytes"] for value in tables.values()),
        }
    except Exception as exc:
        # Do not serialize connection strings, hostnames, or driver messages.
        return {"available": False, "error_type": type(exc).__name__}


def capture_storage(
    root: Path,
    db_path: Path,
    *,
    include_supabase: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    queue_paths = QueuePaths.from_values(root)
    result: dict[str, Any] = {
        "schema_version": STORAGE_SCHEMA,
        "captured_at": _iso(now),
        "queue_id": _queue_id(queue_paths),
        "database_disk_baseline": {
            "plan": "Pro",
            "provisioned_bytes": PROVISIONED_DATABASE_DISK_BYTES,
            "overage_observed_by_operator": 0,
            "spend_cap_status": "UNKNOWN",
            "caveat": "pg_database_size and provisioned GP3 disk are not identical capacity measures",
        },
        "sqlite": sqlite_storage_snapshot(db_path),
    }
    result["supabase"] = (
        supabase_storage_snapshot(root) if include_supabase else {"available": False, "error_type": "disabled"}
    )
    return result


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _task_run_metrics(root: Path, queue_id: str, started_at: str, completed_at: str) -> dict[str, Any]:
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(completed_at)
    records: dict[str, dict[str, Any]] = {}
    for log_path in (root / "data" / "news_work" / "task_runs").glob("task??/runs.jsonl"):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                run_start = datetime.fromisoformat(record["started_at"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if record.get("queue_id") == queue_id and start <= run_start <= end and record.get("run_id"):
                records[str(record["run_id"])] = record

    durations: list[float] = []
    overlaps = 0
    by_task: dict[str, list[dict[str, Any]]] = {}
    for record in records.values():
        by_task.setdefault(str(record["task_id"]), []).append(record)
        try:
            durations.append(
                (datetime.fromisoformat(record["completed_at"]) - datetime.fromisoformat(record["started_at"])).total_seconds()
            )
        except (KeyError, TypeError, ValueError):
            pass
    for task_records in by_task.values():
        ordered = sorted(task_records, key=lambda value: value["started_at"])
        for previous, current in zip(ordered, ordered[1:]):
            if datetime.fromisoformat(current["started_at"]) < datetime.fromisoformat(previous["completed_at"]):
                overlaps += 1

    task_events: Counter[str] = Counter()
    for event_path in (root / "data" / "news_work" / "task_runs").glob("task??/events.jsonl"):
        for line in event_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                event_at = datetime.fromisoformat(event["at"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if event.get("queue_id") == queue_id and start <= event_at <= end:
                task_events[str(event.get("event"))] += 1

    productive = sum(record.get("snapshot_count", 0) > 0 for record in records.values())
    companies = sum(int(record.get("snapshot_count", 0)) for record in records.values())
    return {
        "total_scheduled_runs": len(records),
        "productive_runs": productive,
        "empty_runs": sum(record.get("snapshot_count") == 0 for record in records.values()),
        "busy_skipped_runs_observed": task_events["busy_skip"],
        "stale_guard_recovery_count": task_events["stale_guard_recovered"],
        "overlap_count": overlaps,
        "snapshot_company_count": companies,
        "success_count": sum(int(record.get("success_count", 0)) for record in records.values()),
        "failure_count": sum(int(record.get("failure_count", 0)) for record in records.values()),
        "companies_per_productive_run": companies / productive if productive else None,
        "run_duration_seconds": {
            "average": statistics.mean(durations) if durations else None,
            "p50": statistics.median(durations) if durations else None,
            "p95": _percentile(durations, 0.95),
            "max": max(durations) if durations else None,
        },
        "completions_per_task": {
            task_id: sum(int(record.get("success_count", 0)) for record in task_records)
            for task_id, task_records in sorted(by_task.items())
        },
    }


def _storage_delta(before: dict[str, Any], after: dict[str, Any], store: str) -> dict[str, Any]:
    old = before.get(store, {})
    new = after.get(store, {})
    if not old.get("available") or not new.get("available"):
        return {"available": False}
    tables: dict[str, dict[str, int]] = {}
    for table in NEWS_TABLES:
        tables[table] = {
            key: int(new["tables"][table][key]) - int(old["tables"][table][key])
            for key in ("rows", "table_bytes", "index_bytes", "total_relation_bytes")
        }
    return {
        "available": True,
        "database_bytes": int(new["database_bytes"]) - int(old["database_bytes"]),
        "relevant_index_bytes": int(new["relevant_index_bytes"]) - int(old["relevant_index_bytes"]),
        "relevant_total_relation_bytes": (
            int(new["relevant_total_relation_bytes"]) - int(old["relevant_total_relation_bytes"])
        ),
        "tables": tables,
    }


def _projection(delta: dict[str, Any], completed: int, news_present: int, events: int, after: dict[str, Any]) -> dict[str, Any]:
    if not delta.get("available") or completed <= 0:
        return {"available": False}
    relation_delta = int(delta["relevant_total_relation_bytes"])
    per_company = relation_delta / completed
    weekly = per_company * 3800
    event_delta = delta["tables"]["canonical_news_events"]
    scan_delta = delta["tables"]["canonical_news_scan_runs"]
    current_database_bytes = int(after.get("database_bytes", 0))
    remaining = max(0, PROVISIONED_DATABASE_DISK_BYTES - current_database_bytes)
    weeks_to_8gb = remaining / weekly if weekly > 0 else None
    return {
        "available": True,
        "bytes_per_scanned_company": per_company,
        "bytes_per_news_present_company": relation_delta / news_present if news_present else None,
        "bytes_per_event": (
            event_delta["total_relation_bytes"] / event_delta["rows"] if event_delta["rows"] > 0 else None
        ),
        "bytes_per_scan": (
            scan_delta["total_relation_bytes"] / scan_delta["rows"] if scan_delta["rows"] > 0 else None
        ),
        "observed_event_count": events,
        "growth": {
            "week_raw": weekly,
            "week_safety_2x": weekly * 2,
            "four_weeks_raw": weekly * 4,
            "four_weeks_safety_2x": weekly * 8,
            "year_raw": weekly * 52,
            "year_safety_2x": weekly * 104,
            "three_year_raw": weekly * 156,
            "three_year_safety_2x": weekly * 312,
            "five_year_raw": weekly * 260,
            "five_year_safety_2x": weekly * 520,
        },
        "news_only_time_to_8gb_estimate": {
            "weeks": weeks_to_8gb,
            "months": weeks_to_8gb * 12 / 52 if weeks_to_8gb is not None else None,
            "years": weeks_to_8gb / 52 if weeks_to_8gb is not None else None,
            "caveat": "database size, WAL/system overhead, and provisioned GP3 disk are not identical",
        },
    }


def build_audit(
    root: Path,
    db_path: Path,
    before: dict[str, Any],
    *,
    include_supabase: bool = True,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    queue_paths = QueuePaths.from_values(root)
    bridge = BridgePaths.from_root(root)
    status = queue_status(queue_paths, bridge)
    if status["queue_status"] != "completed":
        raise SoakAuditError("audit-soak requires a completed queue")
    if before.get("queue_id") != status["queue_id"]:
        raise SoakAuditError("storage-before queue_id does not match the current queue")
    after = capture_storage(root, db_path, include_supabase=include_supabase, now=now)
    task_metrics = _task_run_metrics(
        root,
        status["queue_id"],
        status["metrics"]["queue_started_at"],
        status["metrics"]["queue_completed_at"],
    )
    storage_delta = {store: _storage_delta(before, after, store) for store in ("sqlite", "supabase")}
    completed = int(status["completed"])
    news_present = int(status["metrics"]["news_present_count"])
    events = sum(
        int(value.get("news_item_count", 0) or 0)
        for value in (
            json.loads(line)
            for line in queue_paths.entries.read_text(encoding="utf-8").splitlines()
            if line
        )
        if value.get("status") == "completed"
    )
    audit = {
        "schema_version": AUDIT_SCHEMA,
        "captured_at": _iso(now),
        "queue_id": status["queue_id"],
        "queue": {
            key: status[key]
            for key in ("queue_status", "task_count", "batch_size", "logical_slot_count", "completed", "total", "pending", "active", "failed")
        },
        "reliability": {
            "retry_count": status["metrics"]["retry_count"],
            "stale_payload_count": status["metrics"]["stale_payload_count"],
            "validation_failure_count": status["metrics"]["validation_failure_count"],
            "sync_retry_count": status["metrics"]["sync_retry_count"],
            "research_failure_count": task_metrics["failure_count"],
        },
        "news": {
            "no_news_count": status["metrics"]["no_news_count"],
            "news_present_count": news_present,
            "event_count": events,
        },
        "task_runs": task_metrics,
        "throughput": {
            key: status["metrics"][key]
            for key in (
                "queue_started_at", "queue_completed_at", "elapsed_seconds", "companies_per_hour",
                "companies_per_day", "estimated_hours_for_3800", "estimated_days_for_3800",
            )
        },
        "storage_before": before,
        "storage_after": after,
        "storage_delta": storage_delta,
        "storage_projection": {
            store: _projection(storage_delta[store], completed, news_present, events, after.get(store, {}))
            for store in ("sqlite", "supabase")
        },
    }
    return audit, after


def _default_directory(root: Path, queue_id: str) -> Path:
    return root / "output" / "news_soak_audit" / queue_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("storage-snapshot")
    snapshot.add_argument("--label", choices=("before", "after"), required=True)
    snapshot.add_argument("--output", type=Path)
    snapshot.add_argument("--local-only", action="store_true")
    audit = commands.add_parser("audit-soak")
    audit.add_argument("--storage-before", type=Path, required=True)
    audit.add_argument("--output", type=Path)
    audit.add_argument("--local-only", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    db_path = (args.db or root / "decision_db.db").resolve()
    try:
        if args.command == "storage-snapshot":
            result = capture_storage(root, db_path, include_supabase=not args.local_only)
            output = args.output or _default_directory(root, result["queue_id"]) / f"storage_{args.label}.json"
            atomic_write_json(output.resolve(), result)
        else:
            before = _read_json(args.storage_before.resolve())
            result, after = build_audit(root, db_path, before, include_supabase=not args.local_only)
            directory = _default_directory(root, result["queue_id"])
            atomic_write_json(directory / "storage_after.json", after)
            output = args.output or directory / "SOAK_METRICS.json"
            atomic_write_json(output.resolve(), result)
        print(json.dumps({"status": "written", "output": str(output.resolve()), "queue_id": result["queue_id"]}, ensure_ascii=False))
        return 0
    except (SoakAuditError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
