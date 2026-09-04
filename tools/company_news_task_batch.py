#!/usr/bin/env python3
"""Atomic Scheduled Work batch snapshots and per-task overlap guards."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.runtime_paths import runtime_path
from tools.company_news_atomic import atomic_write_json, replace_with_retry
from tools.company_news_queue import QueueError, QueuePaths, _read_state
from tools.company_news_work_bridge import BridgePaths, BridgeError, validate_assignment

SNAPSHOT_SCHEMA = "company_news_task_snapshot_v1"
RUN_RECORD_SCHEMA = "company_news_task_run_v1"
TASK_EVENT_SCHEMA = "company_news_task_event_v1"
_JST = timezone(timedelta(hours=9))
_TASK_ID_PATTERN = re.compile(r"^task\d{2}$")


class TaskBatchError(RuntimeError):
    pass


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(_JST)
    if value.tzinfo is None:
        raise TaskBatchError("task batch timestamps must include a timezone")
    return value.astimezone(_JST)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskBatchError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskBatchError(f"{path} must contain an object")
    return value


def _task_paths(root: Path, task_id: str) -> tuple[Path, Path, Path]:
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise TaskBatchError("task_id must be taskNN")
    directory = runtime_path(root / "data" / "news_work" / "task_runs" / task_id, code_root=root)
    return directory, directory / "active.json", directory / "runs.jsonl"


def _append_task_event(directory: Path, timestamp: datetime, event: str, **details: Any) -> None:
    record = {
        "schema_version": TASK_EVENT_SCHEMA,
        "at": timestamp.isoformat(timespec="seconds"),
        "event": event,
        **details,
    }
    with (directory / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def snapshot_task(
    root: Path,
    task_id: str,
    *,
    now: datetime | None = None,
    guard_ttl: timedelta = timedelta(hours=2),
) -> dict[str, Any]:
    root = root.resolve()
    timestamp = _now(now)
    queue_paths = QueuePaths.from_values(root)
    state = _read_state(queue_paths)
    slots = state["task_slots"].get(task_id)
    if not isinstance(slots, list):
        raise TaskBatchError(f"{task_id} is not configured for queue {state['queue_id']}")

    directory, active_path, _ = _task_paths(root, task_id)
    directory.mkdir(parents=True, exist_ok=True)
    run_token = secrets.token_hex(16)
    placeholder = {
        "schema_version": SNAPSHOT_SCHEMA,
        "task_id": task_id,
        "run_token": run_token,
        "queue_id": state["queue_id"],
        "started_at": timestamp.isoformat(timespec="seconds"),
        "expires_at": (timestamp + guard_ttl).isoformat(timespec="seconds"),
        "assignments": [],
    }
    encoded = (json.dumps(placeholder, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(active_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, encoded)
            os.close(descriptor)
            descriptor = None
            break
        except FileExistsError:
            existing = _read_json(active_path)
            try:
                expires_at = datetime.fromisoformat(str(existing["expires_at"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError) as exc:
                raise TaskBatchError(f"invalid active task guard {active_path}") from exc
            if expires_at > timestamp:
                _append_task_event(
                    directory,
                    timestamp,
                    "busy_skip",
                    task_id=task_id,
                    queue_id=state["queue_id"],
                    active_run_id=existing.get("run_id"),
                    expires_at=existing["expires_at"],
                )
                return {
                    "status": "busy",
                    "task_id": task_id,
                    "active_run_id": existing.get("run_id"),
                    "expires_at": existing["expires_at"],
                    "assignments": [],
                }
            if attempt == 0:
                history = directory / "history"
                history.mkdir(parents=True, exist_ok=True)
                stale = history / f"{existing.get('run_id', 'unknown')}.stale.json"
                replace_with_retry(active_path, stale)
                _append_task_event(
                    directory,
                    timestamp,
                    "stale_guard_recovered",
                    task_id=task_id,
                    queue_id=state["queue_id"],
                    recovered_run_id=existing.get("run_id"),
                    stale_guard=stale.name,
                )
                continue
            raise TaskBatchError(f"could not recover stale guard {active_path}")
    if descriptor is not None:
        os.close(descriptor)
    if not active_path.exists():
        raise TaskBatchError(f"could not acquire task guard {active_path}")

    try:
        assignments: list[dict[str, Any]] = []
        for slot_id in slots:
            bridge = BridgePaths.from_root(root, slot_id=slot_id)
            if not bridge.assignment.exists():
                continue
            try:
                assignment = validate_assignment(_read_json(bridge.assignment), bridge)
            except (BridgeError, TaskBatchError):
                continue
            if (
                assignment.get("status") == "ready"
                and assignment.get("queue_id") == state["queue_id"]
                and assignment.get("scheduled_task_id", task_id) == task_id
            ):
                assignments.append({
                    key: assignment[key]
                    for key in (
                        "slot_id", "assignment_id", "ticker", "company_name", "search_from",
                        "search_to", "queue_id", "output_directory",
                    )
                })
        run_id = f"{task_id}-{timestamp:%Y%m%dT%H%M%S%f}-{run_token[:8]}"
        snapshot = {**placeholder, "run_id": run_id, "assignments": assignments}
        atomic_write_json(active_path, snapshot)
        return {"status": "snapshot_created", **snapshot}
    except Exception:
        current = _read_json(active_path) if active_path.exists() else {}
        if current.get("run_token") == run_token:
            active_path.unlink(missing_ok=True)
        raise


def release_task(
    root: Path,
    task_id: str,
    run_token: str,
    *,
    success_count: int = 0,
    failure_count: int = 0,
    no_news_count: int = 0,
    news_present_count: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    timestamp = _now(now)
    _, active_path, runs_path = _task_paths(root, task_id)
    active = _read_json(active_path)
    if active.get("run_token") != run_token:
        raise TaskBatchError("run_token does not own active task guard")
    counts = (success_count, failure_count, no_news_count, news_present_count)
    if any(not isinstance(value, int) or value < 0 for value in counts):
        raise TaskBatchError("task run counts must be non-negative integers")
    record = {
        "schema_version": RUN_RECORD_SCHEMA,
        "task_id": task_id,
        "run_id": active["run_id"],
        "queue_id": active["queue_id"],
        "started_at": active["started_at"],
        "completed_at": timestamp.isoformat(timespec="seconds"),
        "snapshot_count": len(active.get("assignments", [])),
        "success_count": success_count,
        "failure_count": failure_count,
        "no_news_count": no_news_count,
        "news_present_count": news_present_count,
    }
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    with runs_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    active_path.unlink()
    return {"status": "released", **record}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--task-id", required=True)
    release = commands.add_parser("release")
    release.add_argument("--task-id", required=True)
    release.add_argument("--run-token", required=True)
    release.add_argument("--success-count", type=int, default=0)
    release.add_argument("--failure-count", type=int, default=0)
    release.add_argument("--no-news-count", type=int, default=0)
    release.add_argument("--news-present-count", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot_task(args.root, args.task_id)
        else:
            result = release_task(
                args.root,
                args.task_id,
                args.run_token,
                success_count=args.success_count,
                failure_count=args.failure_count,
                no_news_count=args.no_news_count,
                news_present_count=args.news_present_count,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TaskBatchError, QueueError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
