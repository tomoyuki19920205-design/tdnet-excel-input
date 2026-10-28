#!/usr/bin/env python3
"""Process only NY report inbox files with resumable, bounded delivery."""
from __future__ import annotations

import argparse
import json
import msvcrt
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market import connect_db, mark_run, stable_report_id, upsert_report, validate_payload
from tools.company_news_atomic import atomic_write_json
from tools.sync_ny_market import sync


@dataclass(frozen=True)
class WorkerPaths:
    root: Path
    db: Path
    inbox: Path
    work_dir: Path

    @classmethod
    def from_values(cls, root: Path, db: Path, inbox: Path | None = None,
                    work_dir: Path | None = None) -> "WorkerPaths":
        root = Path(root)
        return cls(root, Path(db), Path(inbox) if inbox else root / "data" / "news_inbox",
                   Path(work_dir) if work_dir else root / "data" / "ny_market_worker")


def _archive(path: Path, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    if target.exists():
        from uuid import uuid4
        target = directory / f"{path.stem}.{uuid4().hex}{path.suffix}"
    shutil.move(str(path), str(target))


def run_once(paths: WorkerPaths, *, sync_func: Callable | None = None,
             ny_sync_func: Callable | None = None) -> dict:
    """A failure for one date cannot prevent another date from being delivered."""
    paths.work_dir.mkdir(parents=True, exist_ok=True)
    lock = paths.work_dir / "ny_market_worker.lock"
    with lock.open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return {"status": "busy", "detected": 0, "completed": 0, "failed": 0, "quarantined": 0}
        try:
            return _run_locked(paths, ny_sync_func)
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _run_locked(paths: WorkerPaths, ny_sync_func: Callable | None) -> dict:
    state_path = paths.work_dir / "checkpoint.json"
    state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {"pending": {}}
    pending = state.setdefault("pending", {})
    files = sorted(paths.inbox.glob("ny_market_daily_*.json")) if paths.inbox.exists() else []
    result = {"status": "completed", "detected": len(files), "completed": 0, "failed": 0,
              "quarantined": 0, "results": []}
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            validated = validate_payload(payload)
            key = validated.run["stable_key"]
            conn = connect_db(paths.db)
            try:
                existing = conn.execute(
                    "SELECT quality_status, report_markdown FROM canonical_ny_market_reports WHERE stable_key=?", (key,),
                ).fetchone()
                if existing is not None and (existing["report_markdown"] == validated.report["report_markdown"]
                                             or (existing["quality_status"] == "complete" and validated.report["quality_status"] == "provisional")):
                    _archive(path, paths.inbox / "processed" / "duplicates")
                    result["results"].append({"file": path.name, "status": "already_completed", "run_id": key})
                    continue
                mark_run(conn, validated.run, "running", increment=True)
                upsert_report(conn, validated)
                mark_run(conn, validated.run, "retry_pending")
                pending[key] = {"quality_status": validated.report["quality_status"], "sync_attempts": 0}
                atomic_write_json(state_path, state)
                _archive(path, paths.inbox / "processed")
                result["results"].append({"file": path.name, "status": "ingested", "run_id": key})
            finally:
                conn.close()
        except (OSError, ValueError, sqlite3.Error) as exc:
            _archive(path, paths.inbox / "quarantine")
            result["quarantined"] += 1
            result["results"].append({"file": path.name, "status": "quarantined", "error": str(exc)})
    # Recover a crash between SQLite commit and checkpoint persistence.
    conn = connect_db(paths.db)
    try:
        for row in conn.execute("SELECT stable_key FROM canonical_ny_market_report_runs WHERE status='retry_pending'"):
            key = row[0]
            if key not in pending:
                quality = conn.execute("SELECT quality_status FROM canonical_ny_market_reports WHERE stable_key=?", (key,)).fetchone()
                if quality:
                    pending[key] = {"quality_status": quality[0], "sync_attempts": 0}
    finally:
        conn.close()
    atomic_write_json(state_path, state)
    for key in sorted(list(pending)):
        entry = pending[key]
        run = {"run_id": stable_report_id(f"run:{key}"), "stable_key": key,
               "report_date_jst": key.removeprefix("ny_market_daily:")}
        if entry["sync_attempts"] >= 4:
            result["failed"] += 1
            continue
        try:
            conn = connect_db(paths.db)
            try:
                mark_run(conn, run, "success" if entry["quality_status"] == "complete" else "provisional")
            finally:
                conn.close()
            if ny_sync_func is None:
                sync(paths.db, stable_keys=[key], production_root=paths.root)
            else:
                ny_sync_func(paths.db, False)
            del pending[key]
            result["completed"] += 1
            for item in result["results"]:
                if item.get("run_id") == key and item["status"] == "ingested":
                    item["status"] = "completed"
        except Exception as exc:
            entry["sync_attempts"] += 1
            entry["last_error"] = str(exc)
            conn = connect_db(paths.db)
            try:
                mark_run(conn, run, "retry_pending", error=exc)
            finally:
                conn.close()
            result["failed"] += 1
            result["results"].append({"run_id": key, "status": "failed", "error": str(exc)})
        atomic_write_json(state_path, state)
    if result["failed"] or result["quarantined"]:
        result["status"] = "completed_with_errors"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    outcome = run_once(WorkerPaths.from_values(args.root, args.db, args.inbox, args.work_dir))
    print(json.dumps(outcome, ensure_ascii=False))
    return 1 if outcome["failed"] or outcome["quarantined"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
