#!/usr/bin/env python3
"""Queue one TSE 33-sector weekly assignment on each scheduled hourly run."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.runtime_paths import runtime_path
from lib.sector_weekly import (
    JST, SCHEMA_VERSION, REPORT_TYPE, SectorValidationError, WeeklyWindow, connect_sector_db,
    dedupe_key, iso_seconds, now_jst, sector_name,
    sector_research_context, weekly_window,
)
from lib.sector_weekly_work import enqueue_assignment, enqueue_retry_candidate, get_assignment_by_key

PROMPT_PATH = ROOT / "config" / "sector_weekly_prompt.txt"
DEFAULT_LOG = ROOT / "data" / "sector_weekly" / "scheduler.jsonl"
DEFAULT_LOCK = ROOT / "data" / "sector_weekly" / "scheduler.lock"
SCHEDULER_CADENCE_MINUTES = 60
SCHEDULER_SLOT_COUNT = 51
SCHEDULER_FINAL_DAY = 0
SCHEDULER_FINAL_HOUR = 8
SCHEDULER_FINAL_MINUTE = 0


class SchedulerError(RuntimeError):
    pass


def _pid_is_alive(pid: int) -> bool:
    """Probe a scheduler PID without signalling it on Windows."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = open_process(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87
    try:
        return wait_for_single_object(handle, 0) == 0x00000102
    finally:
        close_handle(handle)




def _log(path: Path, event: str, **details: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": iso_seconds(now_jst()), "event": event, **details}, ensure_ascii=False, sort_keys=True) + "\n")


@contextmanager
def scheduler_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            match = path.read_text(encoding="utf-8").strip().removeprefix("pid=")
            active_pid = int(match)
        except (OSError, ValueError):
            active_pid = -1
        if active_pid > 0 and _pid_is_alive(active_pid):
            raise SchedulerError("another sector scheduler run is active")
        path.unlink(missing_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise SchedulerError("another sector scheduler run is active") from exc
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode())
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def build_prompt(code: int, window: WeeklyWindow) -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").format(
        sector_code=code, sector_name=sector_name(code), sector_context=sector_research_context(code),
        period_start=iso_seconds(window.period_start), period_end=iso_seconds(window.period_end),
    )


def clean_summary_bullets(bullets: Any) -> Any:
    if not isinstance(bullets, list):
        return bullets
    cleaned: list[Any] = []
    for bullet in bullets:
        if not isinstance(bullet, str):
            cleaned.append(bullet)
            continue
        value = re.sub(r"\s*\(\[[^\]]+\]\(https?://[^)]+\)\)\s*", " ", bullet)
        value = re.sub(r"\[[^\]]+\]\(https?://[^)]+\)", "", value)
        value = " ".join(value.split()).strip()
        cleaned.append(value if len(value) <= 240 else value[:237].rstrip() + "...")
    return cleaned


def clean_full_report_md(value: Any, code: int) -> Any:
    if not isinstance(value, str):
        return value
    report = value.strip()
    report = re.sub(r"\s*\(\[[^\]]+\]\(https?://[^)]+\)\)\s*", " ", report)
    report = re.sub(r"\[[^\]]+\]\(https?://[^)]+\)", "", report)
    # Generated reports occasionally concatenate a Markdown heading to the preceding
    # paragraph. Restore only unambiguous heading boundaries before validation.
    report = re.sub(r"(?<!\n) (?=#{1,3} [^#\n])", "\n\n", report)
    lines = report.splitlines()
    expected_title = f"# 【東証33業種週次】{sector_name(code)}"
    if lines and lines[0].startswith("# "):
        lines[0] = expected_title
    else:
        lines.insert(0, expected_title)
        lines.insert(1, "")
    return "\n".join(line.rstrip() for line in lines).strip()


def assemble_payload(content: dict[str, Any], code: int, window: WeeklyWindow, generated_at: datetime | None = None) -> dict[str, Any]:
    key = dedupe_key(window, code)
    content = dict(content)
    content["summary_bullets"] = clean_summary_bullets(content.get("summary_bullets"))
    content["full_report_md"] = clean_full_report_md(content.get("full_report_md"), code)
    if content.get("importance") != "A+" and isinstance(content["full_report_md"], str) and len(content["full_report_md"]) > 5500:
        raise SectorValidationError("full_report_md exceeds the 5,500-character normal limit")
    return {
        "schema_version": SCHEMA_VERSION, "report_type": REPORT_TYPE, "sector_code": code,
        "sector_name": sector_name(code), "period_start": iso_seconds(window.period_start), "period_end": iso_seconds(window.period_end),
        "generated_at": iso_seconds(generated_at or now_jst()), "run_id": key, "dedupe_key": key, **content,
    }


def _public_assignment(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "assignment_id": row["assignment_id"],
        "stable_key": row["stable_key"],
        "sector_code": row["sector_code"],
        "sector_name": row["sector_name"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "assignment_status": row["status"],
        "attempt_count": row["attempt_count"],
    }


def in_catchup_window(value: datetime) -> bool:
    local = value.astimezone(JST)
    if local.weekday() == 5:
        return local.hour >= 6
    if local.weekday() == 6:
        return True
    return local.weekday() == SCHEDULER_FINAL_DAY and (
        local.hour, local.minute, local.second, local.microsecond
    ) <= (SCHEDULER_FINAL_HOUR, SCHEDULER_FINAL_MINUTE, 0, 0)


def in_worker_window(value: datetime) -> bool:
    local = value.astimezone(JST)
    if local.weekday() == 5:
        return (local.hour, local.minute, local.second, local.microsecond) >= (6, 5, 0, 0)
    if local.weekday() == 6:
        return True
    return local.weekday() == SCHEDULER_FINAL_DAY and (
        local.hour, local.minute, local.second, local.microsecond
    ) <= (8, 5, 0, 0)


def scheduler_slots(saturday_start: datetime) -> tuple[datetime, ...]:
    """Return the 51 eligible hourly invocations for one recovery window."""
    local = saturday_start.astimezone(JST)
    if (local.weekday(), local.hour, local.minute, local.second, local.microsecond) != (5, 6, 0, 0, 0):
        raise SchedulerError("scheduler slot window must start Saturday 06:00:00 JST")
    return tuple(local + timedelta(hours=index) for index in range(SCHEDULER_SLOT_COUNT))


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_scheduled(conn: Any, at: datetime, window: WeeklyWindow) -> dict[str, Any]:
    """Return the next action without mutating the queue."""
    if not in_catchup_window(at):
        return {"action": "none", "event": "not_scheduled"}
    local = at.astimezone(JST)
    same_slot = conn.execute(
        "SELECT * FROM sector_weekly_work_assignments WHERE period_start=? AND period_end=? "
        "AND created_at=? ORDER BY created_at LIMIT 1",
        (
            _utc_text(window.period_start), _utc_text(window.period_end),
            _utc_text(local),
        ),
    ).fetchone()
    if same_slot is not None:
        return {"action": "none", "event": "slot_already_processed", "assignment": dict(same_slot)}
    for code in range(1, 34):
        if get_assignment_by_key(conn, dedupe_key(window, code)) is None:
            return {"action": "enqueue", "event": "assignment_queued", "sector_code": code}
    return {"action": "retry", "event": "retry_queued"}


def run_scheduled(
    at: datetime,
    *,
    db_path: Path,
    log_path: Path = DEFAULT_LOG,
    lock_path: Path = DEFAULT_LOCK,
    not_before: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    log_path = runtime_path(log_path)
    lock_path = runtime_path(lock_path)
    if dry_run:
        window = weekly_window(at)
        if not_before is not None and at.astimezone(JST) < not_before.astimezone(JST):
            return {"status": "not_started", "at": iso_seconds(at), "not_before": iso_seconds(not_before)}
        conn = connect_sector_db(db_path)
        try:
            plan = plan_scheduled(conn, at, window)
        finally:
            conn.close()
        return {
            "status": "dry_run", "created": False, "at": iso_seconds(at),
            "period_start": iso_seconds(window.period_start), "period_end": iso_seconds(window.period_end),
            **plan,
        }
    with scheduler_lock(lock_path):
        window = weekly_window(at)
        if not_before is not None and at.astimezone(JST) < not_before.astimezone(JST):
            return {"status": "not_started", "at": iso_seconds(at), "not_before": iso_seconds(not_before)}
        conn = connect_sector_db(db_path)
        try:
            plan = plan_scheduled(conn, at, window)
            if plan["action"] == "enqueue":
                assignment, created = enqueue_assignment(conn, int(plan["sector_code"]), window, now=at)
                event = "assignment_queued" if created else "assignment_already_exists"
            elif plan["action"] == "retry":
                assignment, created = enqueue_retry_candidate(conn, window, now=at)
                event = "retry_queued" if assignment else "retry_complete"
            else:
                assignment = plan.get("assignment")
                created = False
                event = str(plan["event"])
        finally:
            conn.close()
        result = {
            "status": "queued" if created or (event == "retry_queued" and assignment) else event,
            "created": created,
            "at": iso_seconds(at),
            "period_start": iso_seconds(window.period_start),
            "period_end": iso_seconds(window.period_end),
            **_public_assignment(assignment),
        }
        _log(log_path, event, **result)
        return result


def enqueue_manual(code: int, at: datetime, *, db_path: Path, log_path: Path = DEFAULT_LOG) -> dict[str, Any]:
    log_path = runtime_path(log_path)
    window = weekly_window(at)
    conn = connect_sector_db(db_path)
    try:
        assignment, created = enqueue_assignment(conn, code, window, now=at)
    finally:
        conn.close()
    result = {"status": "queued", "created": created, **_public_assignment(assignment)}
    _log(log_path, "manual_assignment_queued" if created else "manual_assignment_already_exists", **result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=runtime_path(ROOT / "decision_db.db", code_root=ROOT))
    parser.add_argument("--log", type=Path, default=runtime_path(DEFAULT_LOG))
    parser.add_argument("--lock", type=Path, default=runtime_path(DEFAULT_LOCK))
    parser.add_argument("--at", help="timezone-aware ISO-8601 test time")
    parser.add_argument("--sector", type=int, help="manual assignment override")
    parser.add_argument("--not-before", help="do not schedule automatic sectors before this timezone-aware timestamp")
    parser.add_argument("--dry-run", action="store_true", help="show the next queue decision without writing DB, log, or lock")
    args = parser.parse_args()
    at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else now_jst()
    if at.tzinfo is None:
        parser.error("--at must include a timezone")
    not_before = datetime.fromisoformat(args.not_before.replace("Z", "+00:00")) if args.not_before else None
    if not_before is not None and not_before.tzinfo is None:
        parser.error("--not-before must include a timezone")
    try:
        if args.sector:
            result = enqueue_manual(args.sector, at, db_path=args.db, log_path=args.log)
        else:
            result = run_scheduled(
                at, db_path=args.db, log_path=args.log, lock_path=args.lock,
                not_before=not_before, dry_run=args.dry_run,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SchedulerError, SectorValidationError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
