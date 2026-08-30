#!/usr/bin/env python3
"""Independent hourly scheduler for TSE 33-sector weekly research."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline.db import load_env
from lib.sector_weekly import (
    JST, SCHEMA_VERSION, REPORT_TYPE, SectorValidationError, WeeklyWindow, connect_sector_db,
    dedupe_key, ensure_week_runs, in_retry_window, iso_seconds, mark_run, now_jst,
    scheduled_sector, sector_name, upsert_report, validate_report, weekly_window,
)
from tools.sync_sector_weekly import sync as sync_sector_weekly
from tools.company_news_work_bridge import _pid_is_alive

PROMPT_PATH = ROOT / "config" / "sector_weekly_prompt.txt"
DEFAULT_INBOX = ROOT / "data" / "sector_report_inbox"
DEFAULT_LOG = ROOT / "data" / "sector_weekly" / "scheduler.jsonl"
DEFAULT_LOCK = ROOT / "data" / "sector_weekly" / "scheduler.lock"


class SchedulerError(RuntimeError):
    pass


class SyncError(SchedulerError):
    pass


REPORT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["importance", "direction", "summary_bullets", "watchlist_companies", "next_week_watchpoints", "missed_candidates", "full_report_md", "sources"],
    "properties": {
        "importance": {"type": "string", "enum": ["A+", "A", "B", "C"]},
        "direction": {"type": "string", "enum": ["positive", "negative", "mixed", "neutral"]},
        "summary_bullets": {"type": "array", "minItems": 3, "maxItems": 6, "items": {"type": "string"}},
        "watchlist_companies": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False, "required": ["code", "name", "direction"],
            "properties": {"code": {"type": "string"}, "name": {"type": "string"}, "direction": {"type": "string", "enum": ["positive", "negative", "mixed", "neutral"]}},
        }},
        "next_week_watchpoints": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        "missed_candidates": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        "full_report_md": {"type": "string"},
        "sources": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "url", "source_name", "source_type", "published_at"],
            "properties": {
                "title": {"type": "string"}, "url": {"type": "string"}, "source_name": {"type": "string"},
                "source_type": {"type": "string", "enum": ["company_ir", "government", "regulator", "industry_association", "news", "market_data", "other"]},
                "published_at": {"type": ["string", "null"]},
            },
        }},
    },
}


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
        sector_code=code, sector_name=sector_name(code), period_start=iso_seconds(window.period_start), period_end=iso_seconds(window.period_end)
    )


def call_openai(code: int, window: WeeklyWindow, *, model: str | None = None) -> dict[str, Any]:
    load_env()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SchedulerError("OPENAI_API_KEY is not configured")
    from openai import OpenAI

    client = OpenAI(timeout=300.0, max_retries=0)
    response = client.responses.create(
        model=model or os.environ.get("SECTOR_WEEKLY_MODEL", "gpt-5.4"),
        instructions="一次資料中心の日本株業界アナリストとして、指定期間とJSON schemaを厳守してください。",
        input=build_prompt(code, window),
        tools=[{"type": "web_search"}],
        tool_choice="auto",
        include=["web_search_call.action.sources"],
        reasoning={"effort": "high"},
        max_tool_calls=40,
        max_output_tokens=30000,
        text={"format": {"type": "json_schema", "name": "sector_weekly_report", "strict": True, "schema": REPORT_JSON_SCHEMA}},
        store=False,
    )
    if getattr(response, "status", None) != "completed" or not response.output_text:
        raise SchedulerError(f"incomplete OpenAI response: {getattr(response, 'status', None)}")
    try:
        value = json.loads(response.output_text)
    except json.JSONDecodeError as exc:
        raise SectorValidationError("OpenAI response was not valid JSON") from exc
    if not isinstance(value, dict):
        raise SectorValidationError("OpenAI response must be an object")
    return value


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


def assemble_payload(content: dict[str, Any], code: int, window: WeeklyWindow, generated_at: datetime | None = None) -> dict[str, Any]:
    key = dedupe_key(window, code)
    content = dict(content)
    content["summary_bullets"] = clean_summary_bullets(content.get("summary_bullets"))
    return {
        "schema_version": SCHEMA_VERSION, "report_type": REPORT_TYPE, "sector_code": code,
        "sector_name": sector_name(code), "period_start": iso_seconds(window.period_start), "period_end": iso_seconds(window.period_end),
        "generated_at": iso_seconds(generated_at or now_jst()), "run_id": key, "dedupe_key": key, **content,
    }


def _atomic_payload(inbox: Path, payload: dict[str, Any]) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"sector_weekly_{payload['dedupe_key'].replace(':', '_')}.json"
    target = inbox / name
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _archive(path: Path, inbox: Path) -> Path:
    destination = inbox / "processed"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / path.name
    if target.exists():
        target = destination / f"{path.stem}.{int(time.time())}{path.suffix}"
    shutil.move(str(path), str(target))
    return target


def _sync(db_path: Path, dry_run_sync: bool) -> dict[str, int]:
    try:
        return sync_sector_weekly(db_path, dry_run_sync)
    except Exception as exc:
        raise SyncError(str(exc)) from exc


def choose_retry(conn, window: WeeklyWindow) -> int | None:
    row = conn.execute(
        "SELECT sector_code FROM canonical_sector_report_runs WHERE period_end=? AND status IN ('pending','retry_pending','failed') "
        "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'retry_pending' THEN 1 ELSE 2 END, sector_code LIMIT 1",
        (iso_seconds(window.period_end),),
    ).fetchone()
    return int(row[0]) if row else None


def run_sector(
    code: int,
    window: WeeklyWindow,
    *,
    db_path: Path,
    inbox: Path = DEFAULT_INBOX,
    log_path: Path = DEFAULT_LOG,
    model: str | None = None,
    dry_run_sync: bool = False,
    research_func: Callable[[int, WeeklyWindow], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    key = dedupe_key(window, code)
    conn = connect_sector_db(db_path)
    try:
        ensure_week_runs(conn, window)
        existing = conn.execute("SELECT status,last_error_type FROM canonical_sector_report_runs WHERE run_id=?", (key,)).fetchone()
        if existing and existing[0] == "success":
            return {"status": "already_success", "sector_code": code, "dedupe_key": key}
        if existing and existing[0] == "retry_pending" and existing[1] == "SyncError":
            sync_result = _sync(db_path, dry_run_sync)
            mark_run(conn, key, "success")
            sync_result = _sync(db_path, dry_run_sync)
            return {"status": "success", "sector_code": code, "dedupe_key": key, "sync_result": sync_result, "resumed_sync": True}
        last_error: Exception | None = None
        for attempt in range(1, 4):
            mark_run(conn, key, "running", increment=True)
            try:
                content = research_func(code, window) if research_func else call_openai(code, window, model=model)
                payload = assemble_payload(content, code, window)
                validated = validate_report(payload, expected_code=code, expected_window=window)
                path = _atomic_payload(inbox, payload)
                upsert_report(conn, validated)
                archived = _archive(path, inbox)
                mark_run(conn, key, "success")
                sync_result = _sync(db_path, dry_run_sync)
                _log(log_path, "success", sector_code=code, dedupe_key=key, attempt=attempt, archived=str(archived), sync_result=sync_result)
                return {"status": "success", "sector_code": code, "dedupe_key": key, "attempt": attempt, "sync_result": sync_result}
            except Exception as exc:
                last_error = exc
                _log(log_path, "attempt_failed", sector_code=code, dedupe_key=key, attempt=attempt, error_type=type(exc).__name__, error=str(exc))
                if attempt < 3:
                    time.sleep(5 * attempt)
        row = conn.execute("SELECT attempt_count FROM canonical_sector_report_runs WHERE run_id=?", (key,)).fetchone()
        terminal = bool(row and int(row[0]) >= 6)
        mark_run(conn, key, "failed" if terminal else "retry_pending", error=last_error)
        try:
            _sync(db_path, dry_run_sync)
        except Exception as sync_exc:
            _log(log_path, "failure_sync_failed", sector_code=code, error=str(sync_exc))
        return {"status": "failed" if terminal else "retry_pending", "sector_code": code, "dedupe_key": key, "error": str(last_error)}
    finally:
        conn.close()


def run_scheduled(
    at: datetime,
    *,
    db_path: Path,
    inbox: Path = DEFAULT_INBOX,
    log_path: Path = DEFAULT_LOG,
    lock_path: Path = DEFAULT_LOCK,
    model: str | None = None,
    dry_run_sync: bool = False,
    not_before: datetime | None = None,
) -> dict[str, Any]:
    with scheduler_lock(lock_path):
        window = weekly_window(at)
        if not_before is not None and at.astimezone(JST) < not_before.astimezone(JST):
            return {"status": "not_started", "at": iso_seconds(at), "not_before": iso_seconds(not_before)}
        code = scheduled_sector(at)
        conn = connect_sector_db(db_path)
        try:
            ensure_week_runs(conn, window)
            if code is None and in_retry_window(at):
                code = choose_retry(conn, window)
        finally:
            conn.close()
        if code is None:
            return {"status": "not_scheduled", "at": iso_seconds(at), "period_start": iso_seconds(window.period_start), "period_end": iso_seconds(window.period_end)}
        return run_sector(code, window, db_path=db_path, inbox=inbox, log_path=log_path, model=model, dry_run_sync=dry_run_sync)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "decision_db.db")
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--at", help="timezone-aware ISO-8601 test time")
    parser.add_argument("--sector", type=int, help="manual/smoke sector override")
    parser.add_argument("--model")
    parser.add_argument("--not-before", help="do not schedule automatic sectors before this timezone-aware timestamp")
    parser.add_argument("--dry-run-sync", action="store_true")
    args = parser.parse_args()
    at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else now_jst()
    if at.tzinfo is None:
        parser.error("--at must include a timezone")
    not_before = datetime.fromisoformat(args.not_before.replace("Z", "+00:00")) if args.not_before else None
    if not_before is not None and not_before.tzinfo is None:
        parser.error("--not-before must include a timezone")
    try:
        if args.sector:
            result = run_sector(args.sector, weekly_window(at), db_path=args.db, inbox=args.inbox, log_path=args.log, model=args.model, dry_run_sync=args.dry_run_sync)
        else:
            result = run_scheduled(at, db_path=args.db, inbox=args.inbox, log_path=args.log, lock_path=args.lock, model=args.model, dry_run_sync=args.dry_run_sync, not_before=not_before)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] not in {"failed", "retry_pending"} else 1
    except SchedulerError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
