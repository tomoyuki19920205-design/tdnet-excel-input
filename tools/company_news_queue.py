#!/usr/bin/env python3
"""Crash-recoverable global company queue with configurable parallel slots.

Lock order is always worker lock -> global queue lock -> one slot lock.  Queue
code never holds two slot locks at once.  Queue files remain inert until an
operator explicitly activates them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.news_monitor import NewsValidationError, normalize_ticker
from tools.company_news_atomic import atomic_write_json, atomic_write_jsonl, atomic_write_text
from tools.company_news_work_bridge import (
    ASSIGNMENT_SCHEMA,
    ASSIGNMENT_STATUSES,
    BridgeError,
    BridgePaths,
    _pid_is_alive,
    _slot_lock,
    validate_assignment,
)

QUEUE_SCHEMA = "company_news_queue_v1"
QUEUE_STATE_SCHEMA = "company_news_queue_state_v1"
QUEUE_STATUSES = frozenset({"fixture_ready", "active", "paused", "completed"})
ENTRY_STATUSES = frozenset({"pending", "assigned", "completed", "failed", "paused"})
TERMINAL_ASSIGNMENT_STATUSES = frozenset({"completed", "failed"})
MAX_ATTEMPTS = 2
PILOT_SIZE = 5
DEFAULT_SLOT_COUNT = 1
MAX_SLOT_COUNT = 99
DEFAULT_BATCH_SIZE = 1
SOAK_TASK_COUNT = 8
SOAK_BATCH_SIZE = 3
SOAK_COMPANY_COUNT = 100
_JST = timezone(timedelta(hours=9))
_LOCK_PID_RE = re.compile(r"pid=(\d+)")
_ASSIGNMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class QueueError(RuntimeError):
    pass


@dataclass(frozen=True)
class QueuePaths:
    root: Path
    work_dir: Path
    queue_dir: Path
    entries: Path
    state: Path
    lock: Path

    @classmethod
    def from_values(cls, root: Path = ROOT, work_dir: Path | None = None) -> "QueuePaths":
        root = root.resolve()
        work_dir = (work_dir or root / "data" / "news_work").resolve()
        queue_dir = work_dir / "queue"
        return cls(
            root=root,
            work_dir=work_dir,
            queue_dir=queue_dir,
            entries=queue_dir / "company_queue.jsonl",
            state=queue_dir / "queue_state.json",
            lock=queue_dir / "queue.lock",
        )


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(_JST)
    if value.tzinfo is None:
        raise QueueError("queue timestamps must include a timezone")
    return value.astimezone(_JST)


def _iso(now: datetime | None = None) -> str:
    return _now(now).isoformat(timespec="seconds")


def _slot_ids(slot_count: int) -> list[str]:
    if not isinstance(slot_count, int) or not 1 <= slot_count <= MAX_SLOT_COUNT:
        raise QueueError(f"slot_count must be between 1 and {MAX_SLOT_COUNT}")
    return [f"slot{index:02d}" for index in range(1, slot_count + 1)]


def task_slot_mapping(task_count: int, batch_size: int) -> dict[str, list[str]]:
    if not isinstance(task_count, int) or task_count < 1:
        raise QueueError("task_count must be a positive integer")
    if not isinstance(batch_size, int) or batch_size < 1:
        raise QueueError("batch_size must be a positive integer")
    slot_ids = _slot_ids(task_count * batch_size)
    return {
        f"task{index:02d}": slot_ids[(index - 1) * batch_size:index * batch_size]
        for index in range(1, task_count + 1)
    }


def _task_for_slot(state: dict[str, Any], slot_id: str) -> str:
    for task_id, slots in state["task_slots"].items():
        if slot_id in slots:
            return task_id
    raise QueueError(f"no scheduled task owns {slot_id}")


def _empty_slot() -> dict[str, Any]:
    return {"queue_position": None, "assignment_id": None, "status": "idle", "next_assignment": None}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def _atomic_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    atomic_write_jsonl(path, values)


def _atomic_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)


@contextmanager
def _queue_lock(paths: QueuePaths):
    paths.queue_dir.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            try:
                match = _LOCK_PID_RE.search(paths.lock.read_text(encoding="utf-8"))
                pid = int(match.group(1)) if match else 0
            except (OSError, ValueError):
                pid = 0
            if (not pid or not _pid_is_alive(pid)) and attempt == 0:
                paths.lock.unlink(missing_ok=True)
                continue
            raise QueueError(f"company queue is already being processed ({paths.lock})") from exc
    if descriptor is None:
        raise QueueError("could not acquire company queue lock")
    try:
        os.write(descriptor, f"pid={os.getpid()} at={_iso()}\n".encode())
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        paths.lock.unlink(missing_ok=True)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QueueError(f"{path} must contain a JSON object")
    return value


def _read_entries(paths: QueuePaths) -> list[dict[str, Any]]:
    try:
        lines = paths.entries.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QueueError(f"cannot read queue {paths.entries}: {exc}") from exc
    required = {
        "schema_version", "ticker", "company_name", "sector", "queue_position", "status",
        "last_checked_at", "next_eligible_at", "attempt_count", "last_error",
    }
    values: list[dict[str, Any]] = []
    positions: set[int] = set()
    tickers: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise QueueError(f"invalid queue JSONL line {line_number}: {exc}") from exc
        if not isinstance(value, dict) or required.difference(value):
            raise QueueError(f"invalid queue entry at line {line_number}")
        if value["schema_version"] != QUEUE_SCHEMA:
            raise QueueError(f"queue entry schema_version must be {QUEUE_SCHEMA}")
        try:
            value["ticker"] = normalize_ticker(value["ticker"])
        except NewsValidationError as exc:
            raise QueueError(str(exc)) from exc
        if not isinstance(value["queue_position"], int) or value["queue_position"] < 1:
            raise QueueError("queue_position must be a positive integer")
        if value["status"] not in ENTRY_STATUSES:
            raise QueueError(f"unsupported queue entry status: {value['status']}")
        if not isinstance(value["attempt_count"], int) or value["attempt_count"] < 0:
            raise QueueError("attempt_count must be a non-negative integer")
        value.setdefault("assignment_id", None)
        value.setdefault("assigned_slot", None)
        value.setdefault("assigned_task", None)
        value.setdefault("completed_slot", None)
        value.setdefault("completed_task", None)
        value.setdefault("first_assigned_at", None)
        value.setdefault("completed_at", None)
        value.setdefault("news_item_count", None)
        if value["status"] == "assigned" and not value["assigned_slot"]:
            value["assigned_slot"] = "slot01"  # v3.1 compatibility
        if value["assigned_slot"] is not None and not re.fullmatch(r"slot\d{2}", str(value["assigned_slot"])):
            raise QueueError("assigned_slot must be null or slotNN")
        if value["queue_position"] in positions or value["ticker"] in tickers:
            raise QueueError("queue entries must have unique positions and tickers")
        positions.add(value["queue_position"])
        tickers.add(value["ticker"])
        values.append(value)
    return sorted(values, key=lambda item: item["queue_position"])


def _sync_legacy_slot01_fields(state: dict[str, Any]) -> None:
    slot = state["slots"].get("slot01", _empty_slot())
    state.update({
        "current_queue_position": slot.get("queue_position"),
        "current_assignment_id": slot.get("assignment_id"),
        "slot_status": slot.get("status", "idle"),
        "next_assignment": slot.get("next_assignment"),
    })
    slot01_transition = state.get("transitions", {}).get("slot01")
    if state.get("slot_count", 1) == 1 and isinstance(slot01_transition, dict):
        state["transition"] = slot01_transition
    else:
        state.pop("transition", None)


def _read_state(paths: QueuePaths) -> dict[str, Any]:
    state = _read_object(paths.state)
    if state.get("schema_version") != QUEUE_STATE_SCHEMA:
        raise QueueError(f"schema_version must be {QUEUE_STATE_SCHEMA}")
    if state.get("queue_status") not in QUEUE_STATUSES:
        raise QueueError(f"unsupported queue_status: {state.get('queue_status')}")
    if not isinstance(state.get("assignment_sequence"), int) or state["assignment_sequence"] < 0:
        raise QueueError("assignment_sequence must be a non-negative integer")
    slot_count = state.get("slot_count", DEFAULT_SLOT_COUNT)
    if not isinstance(slot_count, int) or not 1 <= slot_count <= MAX_SLOT_COUNT:
        raise QueueError(f"slot_count must be between 1 and {MAX_SLOT_COUNT}")
    state["slot_count"] = slot_count
    task_count = state.get("task_count", slot_count)
    batch_size = state.get("batch_size", DEFAULT_BATCH_SIZE)
    if not isinstance(task_count, int) or not isinstance(batch_size, int):
        raise QueueError("task_count and batch_size must be integers")
    if task_count * batch_size != slot_count:
        raise QueueError("task_count * batch_size must equal slot_count")
    expected_mapping = task_slot_mapping(task_count, batch_size)
    task_slots = state.get("task_slots", expected_mapping)
    if task_slots != expected_mapping:
        raise QueueError("task_slots does not match task_count and batch_size")
    state.update({
        "task_count": task_count,
        "batch_size": batch_size,
        "logical_slot_count": slot_count,
        "task_slots": task_slots,
    })
    slots = state.get("slots")
    if not isinstance(slots, dict):
        slots = {
            "slot01": {
                "queue_position": state.get("current_queue_position"),
                "assignment_id": state.get("current_assignment_id"),
                "status": state.get("slot_status", "idle"),
                "next_assignment": state.get("next_assignment"),
            }
        }
    for slot_id in _slot_ids(slot_count):
        slots.setdefault(slot_id, _empty_slot())
    state["slots"] = slots
    raw_transitions = state.get("transitions")
    transitions = dict(raw_transitions) if isinstance(raw_transitions, dict) else {}
    if isinstance(state.get("transition"), dict):
        legacy = dict(state["transition"])
        assignment = legacy.get("assignment")
        slot_id = legacy.get("slot_id")
        if not isinstance(slot_id, str) and isinstance(assignment, dict):
            slot_id = assignment.get("slot_id")
        if not isinstance(slot_id, str):
            slot_id = "slot01"
        existing = transitions.get(slot_id)
        if existing is None:
            transitions[slot_id] = legacy
        elif existing != legacy:
            raise QueueError(f"conflicting assignment transitions for {slot_id}")
    state["transitions"] = transitions
    metrics = state.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    for name in ("total_scheduled_runs", "stale_payload_count", "validation_failure_count", "sync_retry_count"):
        metrics.setdefault(name, 0)
    state["metrics"] = metrics
    return state


def _write_state(paths: QueuePaths, state: dict[str, Any]) -> None:
    _sync_legacy_slot01_fields(state)
    _atomic_json(paths.state, state)


def _queue_exists(paths: QueuePaths) -> bool:
    return paths.entries.exists() and paths.state.exists()


def _archive_completed_queue(paths: QueuePaths, state: dict[str, Any]) -> Path:
    if state["queue_status"] != "completed":
        raise QueueError("only a completed queue can be archived for replacement")
    queue_id = state.get("queue_id")
    if not isinstance(queue_id, str) or not _ASSIGNMENT_ID_RE.fullmatch(queue_id):
        raise QueueError("completed queue has an invalid queue_id")
    target = paths.queue_dir / "history" / queue_id
    sources = {
        "company_queue.jsonl": paths.entries.read_text(encoding="utf-8"),
        "queue_state.json": paths.state.read_text(encoding="utf-8"),
    }
    for name, body in sources.items():
        destination = target / name
        if destination.exists():
            if destination.read_text(encoding="utf-8") != body:
                raise QueueError(f"completed queue history conflict: {destination}")
            continue
        _atomic_text(destination, body)
    return target


def configured_slot_ids(paths: QueuePaths) -> list[str]:
    if _queue_exists(paths):
        return _slot_ids(_read_state(paths)["slot_count"])
    discovered = sorted(
        path.parent.name
        for path in (paths.work_dir / "slots").glob("slot??/assignment.json")
        if re.fullmatch(r"slot\d{2}", path.parent.name)
    )
    return discovered or ["slot01"]


def _bridges(base: BridgePaths, slot_count: int) -> dict[str, BridgePaths]:
    return {
        slot_id: BridgePaths.from_root(base.root, base.work_dir, base.inbox, slot_id=slot_id)
        for slot_id in _slot_ids(slot_count)
    }


def _master_sample(
    master_db: Path,
    limit: int,
    *,
    db_path: Path | None = None,
    stratified: bool = False,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise QueueError("company count must be positive")
    connection = sqlite3.connect(master_db)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "companies" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(companies)")}
            sector_column = next((name for name in ("sector33_name", "sector", "market") if name in columns), None)
            sector_sql = f"COALESCE({sector_column}, 'unknown')" if sector_column else "'unknown'"
            rows = connection.execute(
                f"SELECT ticker_code AS ticker,name_ja AS company_name,{sector_sql} AS sector "
                "FROM companies WHERE name_ja IS NOT NULL AND TRIM(name_ja)<>'' ORDER BY ticker_code"
            )
        elif "market_data_universe" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(market_data_universe)")}
            ordinary = "AND is_ordinary_stock=1" if "is_ordinary_stock" in columns else ""
            sector_parts = [name for name in ("sector33_name", "sector17_name", "market_name") if name in columns]
            sector_sql = f"COALESCE({','.join(sector_parts)}, 'unknown')" if sector_parts else "'unknown'"
            rows = connection.execute(
                f"SELECT ticker,company_name,{sector_sql} AS sector FROM market_data_universe "
                "WHERE date=(SELECT MAX(date) FROM market_data_universe) "
                f"AND company_name IS NOT NULL AND TRIM(company_name)<>'' {ordinary} ORDER BY ticker"
            )
        else:
            raise QueueError(f"no supported company master table in {master_db}")
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            raw_ticker = str(row["ticker"]).upper()
            if len(raw_ticker) == 5 and raw_ticker.endswith("0"):
                raw_ticker = raw_ticker[:4]
            try:
                ticker = normalize_ticker(raw_ticker)
            except NewsValidationError:
                continue
            company_name = str(row["company_name"]).strip()
            if ticker in seen or not company_name:
                continue
            candidates.append({
                "ticker": ticker,
                "company_name": company_name,
                "sector": str(row["sector"] or "unknown").strip() or "unknown",
            })
            seen.add(ticker)
        if len(candidates) < limit:
            raise QueueError(f"company master provided only {len(candidates)} eligible companies")
        if not stratified:
            return candidates[:limit]

        successful: set[str] = set()
        if db_path and db_path.exists():
            news = sqlite3.connect(db_path)
            try:
                table = news.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_news_scan_runs'"
                ).fetchone()
                if table:
                    for row in news.execute(
                        "SELECT DISTINCT ticker FROM canonical_news_scan_runs WHERE status='completed'"
                    ):
                        try:
                            successful.add(normalize_ticker(row[0]))
                        except NewsValidationError:
                            continue
            finally:
                news.close()

        def round_robin(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
            sectors: dict[str, list[dict[str, Any]]] = {}
            for company in values:
                sectors.setdefault(company["sector"], []).append(company)
            for sector_values in sectors.values():
                sector_values.sort(
                    key=lambda company: hashlib.sha256(
                        f"company-news-soak-v1:{company['ticker']}".encode()
                    ).hexdigest()
                )
            ordered: list[dict[str, Any]] = []
            sector_names = sorted(sectors)
            index = 0
            while len(ordered) < len(values):
                for sector in sector_names:
                    if index < len(sectors[sector]):
                        ordered.append(sectors[sector][index])
                index += 1
            return ordered

        never_scanned = round_robin([company for company in candidates if company["ticker"] not in successful])
        previously_scanned = round_robin([company for company in candidates if company["ticker"] in successful])
        return (never_scanned + previously_scanned)[:limit]
    finally:
        connection.close()


def _successful_checked_at(db_path: Path, *, ticker: str | None = None, run_id: str | None = None) -> str | None:
    if not db_path.exists():
        return None
    connection = sqlite3.connect(db_path)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_news_scan_runs'"
        ).fetchone()
        if not table:
            return None
        if run_id:
            row = connection.execute(
                "SELECT checked_at FROM canonical_news_scan_runs WHERE scan_run_id=? AND status='completed'",
                (run_id,),
            ).fetchone()
        elif ticker:
            row = connection.execute(
                "SELECT checked_at FROM canonical_news_scan_runs WHERE ticker=? AND status='completed' "
                "ORDER BY checked_at DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        else:
            return None
        return str(row[0]) if row else None
    finally:
        connection.close()


def _search_period(db_path: Path, ticker: str, now: datetime) -> tuple[date, date]:
    search_to = _now(now).date()
    previous = _successful_checked_at(db_path, ticker=ticker)
    if previous:
        try:
            previous_date = datetime.fromisoformat(previous.replace("Z", "+00:00")).astimezone(_JST).date()
        except (TypeError, ValueError):
            previous_date = search_to - timedelta(days=6)
        search_from = min(previous_date, search_to)
    else:
        search_from = search_to - timedelta(days=6)
    return search_from, search_to


def _assignment_for_entry(
    state: dict[str, Any], entry: dict[str, Any], slot_id: str, db_path: Path, now: datetime
) -> tuple[dict[str, Any], int]:
    sequence = state["assignment_sequence"] + 1
    task_id = _task_for_slot(state, slot_id)
    queue_token = str(state["queue_id"]).split("-", 1)[-1]
    assignment_id = f"{slot_id}-{queue_token}-{sequence:06d}"
    search_from, search_to = _search_period(db_path, entry["ticker"], now)
    return ({
        "schema_version": ASSIGNMENT_SCHEMA,
        "slot_id": slot_id,
        "scheduled_task_id": task_id,
        "assignment_id": assignment_id,
        "ticker": entry["ticker"],
        "company_name": entry["company_name"],
        "search_from": search_from.isoformat(),
        "search_to": search_to.isoformat(),
        "status": "ready",
        "output_directory": "data/news_inbox",
        "created_at": _iso(now),
        "queue_id": state["queue_id"],
        "queue_position": entry["queue_position"],
        "queue_attempt": entry["attempt_count"] + 1,
    }, sequence)


def _read_assignment_if_present(bridge: BridgePaths) -> dict[str, Any] | None:
    if not bridge.assignment.exists():
        return None
    assignment = _read_object(bridge.assignment)
    try:
        return validate_assignment(assignment, bridge)
    except BridgeError:
        status = assignment.get("status")
        assignment_id = assignment.get("assignment_id")
        if (
            isinstance(status, str)
            and status not in ASSIGNMENT_STATUSES
            and isinstance(assignment_id, str)
            and assignment_id
        ):
            return assignment
        raise


def _assignment_entry(
    assignment: dict[str, Any], entries: list[dict[str, Any]], state: dict[str, Any], slot_id: str
) -> dict[str, Any] | None:
    if assignment.get("queue_id") != state.get("queue_id") or assignment.get("slot_id") != slot_id:
        return None
    if assignment.get("scheduled_task_id", _task_for_slot(state, slot_id)) != _task_for_slot(state, slot_id):
        return None
    position = assignment.get("queue_position")
    assignment_id = assignment.get("assignment_id")
    if not isinstance(position, int) or not isinstance(assignment_id, str):
        return None
    entry = next((item for item in entries if item["queue_position"] == position), None)
    slot = state["slots"].get(slot_id, {})
    if entry is None or entry.get("assigned_slot") not in {None, slot_id}:
        return None
    if entry.get("assigned_task") not in {None, _task_for_slot(state, slot_id)}:
        return None
    if assignment.get("ticker") != entry.get("ticker") or assignment.get("company_name") != entry.get("company_name"):
        return None
    if assignment_id not in {entry.get("assignment_id"), slot.get("assignment_id")}:
        return None
    return entry


def _archive_terminal_unmanaged_assignment(bridge: BridgePaths, assignment: dict[str, Any]) -> Path:
    assignment_id = assignment.get("assignment_id")
    if not isinstance(assignment_id, str) or not _ASSIGNMENT_ID_RE.fullmatch(assignment_id):
        raise QueueError("terminal unmanaged assignment has an invalid assignment_id")
    preserved = assignment
    if bridge.assignment.exists():
        source = _read_object(bridge.assignment)
        if source.get("assignment_id") == assignment_id:
            preserved = source
    target = bridge.assignment.parent / "history" / f"{assignment_id}.json"
    if target.exists():
        if _read_object(target) != preserved:
            raise QueueError(f"assignment history conflict: {target}")
        return target
    _atomic_json(target, preserved)
    return target


def _commit_transition(
    paths: QueuePaths,
    bridge: BridgePaths,
    entries: list[dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    transition = state["transitions"].get(bridge.slot_id)
    if not isinstance(transition, dict) or transition.get("kind") != "assign":
        raise QueueError(f"queue assignment transition is missing for {bridge.slot_id}")
    assignment = transition.get("assignment")
    position = transition.get("queue_position")
    sequence = transition.get("assignment_sequence")
    if not isinstance(assignment, dict) or not isinstance(position, int) or not isinstance(sequence, int):
        raise QueueError("queue assignment transition is invalid")
    validate_assignment(dict(assignment), bridge)
    current = _read_assignment_if_present(bridge)
    if current and current.get("assignment_id") != assignment["assignment_id"]:
        if current.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
            raise QueueError(f"{bridge.slot_id} already has an unfinished assignment: {current.get('assignment_id')}")
        if _assignment_entry(current, entries, state, bridge.slot_id) is None:
            _archive_terminal_unmanaged_assignment(bridge, current)
    if not current or current.get("assignment_id") != assignment["assignment_id"]:
        _atomic_json(bridge.assignment, assignment)
    entry = next((item for item in entries if item["queue_position"] == position), None)
    if entry is None:
        raise QueueError(f"queue position {position} is absent")
    entry.update({
        "status": "assigned",
        "assigned_slot": bridge.slot_id,
        "assigned_task": _task_for_slot(state, bridge.slot_id),
        "attempt_count": assignment["queue_attempt"],
        "assignment_id": assignment["assignment_id"],
        "next_eligible_at": None,
    })
    entry["first_assigned_at"] = entry.get("first_assigned_at") or assignment["created_at"]
    state["assignment_sequence"] = sequence
    state["slots"][bridge.slot_id] = {
        "queue_position": position,
        "assignment_id": assignment["assignment_id"],
        "status": "ready",
        "next_assignment": assignment["assignment_id"],
    }
    if transition.get("activate_queue"):
        state.update({
            "queue_status": "active",
            "fixture_mode": False,
            "started_at": state.get("started_at") or _iso(),
        })
    state["transitions"].pop(bridge.slot_id, None)
    state["updated_at"] = _iso()
    _atomic_jsonl(paths.entries, entries)
    _write_state(paths, state)
    return assignment


def _plan_assignment(
    paths: QueuePaths,
    bridge: BridgePaths,
    entries: list[dict[str, Any]],
    state: dict[str, Any],
    entry: dict[str, Any],
    db_path: Path,
    now: datetime,
    *,
    activate_queue: bool = False,
) -> dict[str, Any]:
    assignment, sequence = _assignment_for_entry(state, entry, bridge.slot_id, db_path, now)
    state["transitions"][bridge.slot_id] = {
        "kind": "assign",
        "slot_id": bridge.slot_id,
        "queue_position": entry["queue_position"],
        "assignment_sequence": sequence,
        "assignment": assignment,
        "activate_queue": activate_queue,
    }
    state["updated_at"] = _iso(now)
    _write_state(paths, state)
    return _commit_transition(paths, bridge, entries, state)


def _recover_transitions(
    paths: QueuePaths,
    bridges: dict[str, BridgePaths],
    entries: list[dict[str, Any]],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    recovered: list[dict[str, Any]] = []
    for slot_id in sorted(list(state["transitions"])):
        bridge = bridges.get(slot_id)
        if bridge is None:
            raise QueueError(f"transition references unconfigured slot: {slot_id}")
        with _slot_lock(bridge):
            recovered.append(_commit_transition(paths, bridge, entries, state))
    return recovered


def _clear_slot(state: dict[str, Any], slot_id: str, status: str = "idle") -> None:
    state["slots"][slot_id] = {**_empty_slot(), "status": status}


def _set_bridge_idle(bridge: BridgePaths, state: dict[str, Any], now: datetime) -> None:
    _atomic_json(bridge.state, {
        "schema_version": "company_news_bridge_state_v1",
        "slot_id": bridge.slot_id,
        "assignment_id": state.get("last_assignment_id"),
        "phase": "idle",
        "queue_id": state["queue_id"],
        "updated_at": _iso(now),
    })


def _fill_idle_slots(
    paths: QueuePaths,
    bridges: dict[str, BridgePaths],
    entries: list[dict[str, Any]],
    state: dict[str, Any],
    db_path: Path,
    now: datetime,
    *,
    activate_queue: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assignments: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    activation_pending = activate_queue and state["queue_status"] != "active"
    for slot_id, bridge in bridges.items():
        slot = state["slots"][slot_id]
        if slot.get("assignment_id"):
            continue
        with _slot_lock(bridge):
            current = _read_assignment_if_present(bridge)
            if current is not None:
                owner = _assignment_entry(current, entries, state, slot_id)
                if owner is None and current.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
                    blocked.append({"slot_id": slot_id, "assignment_id": current.get("assignment_id")})
                    continue
                if owner is not None and current.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
                    state["slots"][slot_id] = {
                        "queue_position": owner["queue_position"],
                        "assignment_id": current["assignment_id"],
                        "status": current.get("status", "ready"),
                        "next_assignment": current["assignment_id"],
                    }
                    state["updated_at"] = _iso(now)
                    _write_state(paths, state)
                    continue
                if owner is None:
                    _archive_terminal_unmanaged_assignment(bridge, current)
            pending = next((entry for entry in entries if entry["status"] == "pending"), None)
            if pending is None:
                continue
            assignment = _plan_assignment(
                paths,
                bridge,
                entries,
                state,
                pending,
                db_path,
                now,
                activate_queue=activation_pending,
            )
            activation_pending = False
            assignments.append(assignment)
    return assignments, blocked


def _all_slots_inactive(state: dict[str, Any]) -> bool:
    return all(not slot.get("assignment_id") for slot in state["slots"].values())


def _maybe_complete(
    paths: QueuePaths,
    bridges: dict[str, BridgePaths],
    entries: list[dict[str, Any]],
    state: dict[str, Any],
    now: datetime,
) -> bool:
    terminal = all(entry["status"] in {"completed", "failed"} for entry in entries)
    if not terminal or state["transitions"] or not _all_slots_inactive(state):
        return False
    state.update({
        "queue_status": "completed",
        "fixture_mode": False,
        "completed_at": _iso(now),
        "updated_at": _iso(now),
    })
    _write_state(paths, state)
    for bridge in bridges.values():
        _set_bridge_idle(bridge, state, now)
    return True


def initialize_pilot(
    paths: QueuePaths,
    bridge: BridgePaths,
    master_db: Path,
    db_path: Path,
    *,
    activate: bool = False,
    now: datetime | None = None,
    slot_count: int | None = None,
    count: int = PILOT_SIZE,
    task_count: int | None = None,
    batch_size: int | None = None,
    stratified: bool = False,
) -> dict[str, Any]:
    timestamp = _now(now)
    if task_count is None and batch_size is None:
        slot_count = slot_count or DEFAULT_SLOT_COUNT
        task_count = slot_count
        batch_size = DEFAULT_BATCH_SIZE
    elif task_count is None or batch_size is None:
        raise QueueError("task_count and batch_size must be specified together")
    else:
        calculated_slots = task_count * batch_size
        if slot_count is not None and slot_count != calculated_slots:
            raise QueueError("--slots conflicts with --tasks * --batch-size")
        slot_count = calculated_slots
    task_slots = task_slot_mapping(task_count, batch_size)
    slot_ids = _slot_ids(slot_count)
    if count < slot_count:
        raise QueueError("company count must be at least slot_count")
    with _queue_lock(paths):
        if paths.entries.exists() != paths.state.exists():
            raise QueueError("incomplete queue runtime files require manual review")
        archived_queue: str | None = None
        if _queue_exists(paths):
            previous_state = _read_state(paths)
            archived_queue = str(_archive_completed_queue(paths, previous_state))
        companies = _master_sample(master_db, count, db_path=db_path, stratified=stratified)
        entries = [{
            "schema_version": QUEUE_SCHEMA,
            **company,
            "queue_position": index,
            "status": "pending",
            "assigned_slot": None,
            "assigned_task": None,
            "assignment_id": None,
            "last_checked_at": None,
            "next_eligible_at": None,
            "attempt_count": 0,
            "last_error": None,
            "completed_slot": None,
            "completed_task": None,
            "first_assigned_at": None,
            "completed_at": None,
            "news_item_count": None,
        } for index, company in enumerate(companies, start=1)]
        state = {
            "schema_version": QUEUE_STATE_SCHEMA,
            "queue_id": f"pilot{count}-{timestamp:%Y%m%dT%H%M%S%f}",
            "queue_status": "fixture_ready",
            "fixture_mode": True,
            "slot_count": slot_count,
            "logical_slot_count": slot_count,
            "task_count": task_count,
            "batch_size": batch_size,
            "task_slots": task_slots,
            "slots": {slot_id: _empty_slot() for slot_id in slot_ids},
            "transitions": {},
            "total": len(entries),
            "max_attempts": MAX_ATTEMPTS,
            "assignment_sequence": 0,
            "last_completed": None,
            "last_assignment_id": None,
            "created_at": _iso(timestamp),
            "started_at": None,
            "completed_at": None,
            "metrics": {
                "total_scheduled_runs": 0,
                "stale_payload_count": 0,
                "validation_failure_count": 0,
                "sync_retry_count": 0,
            },
            "updated_at": _iso(timestamp),
        }
        _sync_legacy_slot01_fields(state)
        _atomic_jsonl(paths.entries, entries)
        _write_state(paths, state)
        assignments: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        if activate:
            assignments, blocked = _fill_idle_slots(
                paths, _bridges(bridge, slot_count), entries, state, db_path, timestamp, activate_queue=True
            )
        return {
            "queue_id": state["queue_id"],
            "queue_status": state["queue_status"],
            "slot_count": slot_count,
            "task_count": task_count,
            "batch_size": batch_size,
            "logical_slot_count": slot_count,
            "task_slots": task_slots,
            "companies": companies,
            "assignment": assignments[0] if slot_count == 1 and assignments else None,
            "assignments": assignments,
            "blocked_slots": blocked,
            "archived_queue": archived_queue,
        }


def reconcile_queue(
    paths: QueuePaths,
    bridge: BridgePaths,
    db_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not _queue_exists(paths):
        return {"status": "inactive"}
    timestamp = _now(now)
    with _queue_lock(paths):
        entries = _read_entries(paths)
        state = _read_state(paths)
        bridges = _bridges(bridge, state["slot_count"])
        recovered = _recover_transitions(paths, bridges, entries, state)
        if state["queue_status"] == "fixture_ready":
            return {"status": "fixture_ready", "recovered": recovered}
        if state["queue_status"] == "completed":
            return {"status": "queue_completed", "recovered": recovered}

        slot_results: dict[str, dict[str, Any]] = {}
        for slot_id, slot_bridge in bridges.items():
            with _slot_lock(slot_bridge):
                assignment = _read_assignment_if_present(slot_bridge)
                if assignment is None:
                    _clear_slot(state, slot_id)
                    slot_results[slot_id] = {"status": "idle"}
                    continue
                entry = _assignment_entry(assignment, entries, state, slot_id)
                if entry is None:
                    if assignment.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
                        slot_results[slot_id] = {
                            "status": "unmanaged_assignment",
                            "assignment_id": assignment.get("assignment_id"),
                        }
                        continue
                    expected_assignment_id = state["slots"][slot_id].get("assignment_id")
                    if expected_assignment_id:
                        slot_results[slot_id] = {
                            "status": "assignment_mismatch",
                            "expected_assignment_id": expected_assignment_id,
                            "actual_assignment_id": assignment.get("assignment_id"),
                        }
                        continue
                    _archive_terminal_unmanaged_assignment(slot_bridge, assignment)
                    _clear_slot(state, slot_id)
                    slot_results[slot_id] = {
                        "status": "terminal_unmanaged_archived",
                        "assignment_id": assignment.get("assignment_id"),
                    }
                    continue
                if assignment.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
                    state["slots"][slot_id]["status"] = assignment.get("status", "ready")
                    slot_results[slot_id] = {"status": "waiting", "assignment_id": assignment["assignment_id"]}
                    continue
                if assignment["status"] == "completed":
                    checked_at = _successful_checked_at(db_path, run_id=assignment["assignment_id"])
                    if checked_at is None:
                        slot_results[slot_id] = {
                            "status": "blocked_missing_canonical_run",
                            "assignment_id": assignment["assignment_id"],
                        }
                        continue
                    entry.update({
                        "status": "completed",
                        "assigned_slot": None,
                        "assigned_task": None,
                        "completed_slot": slot_id,
                        "completed_task": _task_for_slot(state, slot_id),
                        "completed_at": checked_at,
                        "news_item_count": assignment.get("news_item_count"),
                        "last_checked_at": checked_at,
                        "next_eligible_at": None,
                        "last_error": None,
                    })
                    state.update({
                        "last_completed": {
                            "ticker": entry["ticker"],
                            "company_name": entry["company_name"],
                            "checked_at": checked_at,
                            "slot_id": slot_id,
                            "task_id": _task_for_slot(state, slot_id),
                        },
                        "last_assignment_id": assignment["assignment_id"],
                        "updated_at": _iso(timestamp),
                    })
                    _clear_slot(state, slot_id)
                    _atomic_jsonl(paths.entries, entries)
                    _write_state(paths, state)
                    slot_results[slot_id] = {"status": "completed", "assignment_id": assignment["assignment_id"]}
                    continue

                bridge_state = _read_object(slot_bridge.state) if slot_bridge.state.exists() else {}
                error = str(assignment.get("error_message") or bridge_state.get("last_error") or "assignment failed")[:1000]
                entry.update({"last_error": error, "next_eligible_at": _iso(timestamp)})
                state.update({"last_assignment_id": assignment["assignment_id"], "updated_at": _iso(timestamp)})
                if bridge_state.get("phase") in {"ingested", "synced"}:
                    _atomic_jsonl(paths.entries, entries)
                    _write_state(paths, state)
                    slot_results[slot_id] = {
                        "status": "blocked_sync_retry",
                        "assignment_id": assignment["assignment_id"],
                    }
                    continue
                if state["queue_status"] == "paused":
                    _atomic_jsonl(paths.entries, entries)
                    _write_state(paths, state)
                    slot_results[slot_id] = {"status": "paused", "assignment_id": assignment["assignment_id"]}
                    continue
                if entry["attempt_count"] < state.get("max_attempts", MAX_ATTEMPTS):
                    _atomic_jsonl(paths.entries, entries)
                    _write_state(paths, state)
                    retry = _plan_assignment(paths, slot_bridge, entries, state, entry, db_path, timestamp)
                    slot_results[slot_id] = {"status": "retry_assigned", "assignment": retry}
                    continue
                entry.update({
                    "status": "failed",
                    "assigned_slot": None,
                    "assigned_task": None,
                    "completed_slot": slot_id,
                    "completed_task": _task_for_slot(state, slot_id),
                    "completed_at": _iso(timestamp),
                    "next_eligible_at": None,
                })
                _clear_slot(state, slot_id, "failed")
                _atomic_jsonl(paths.entries, entries)
                _write_state(paths, state)
                slot_results[slot_id] = {"status": "failed", "assignment_id": assignment["assignment_id"]}

        if state["queue_status"] == "paused":
            _write_state(paths, state)
            return {"status": "paused", "slots": slot_results, "recovered": recovered}
        assignments, blocked = _fill_idle_slots(paths, bridges, entries, state, db_path, timestamp)
        unmanaged_active = any(
            value.get("status") in {"unmanaged_assignment", "assignment_mismatch"}
            for value in slot_results.values()
        )
        if not blocked and not unmanaged_active and _maybe_complete(paths, bridges, entries, state, timestamp):
            return {"status": "queue_completed", "slots": slot_results, "recovered": recovered}
        if assignments:
            return {
                "status": "assigned",
                "assignment": assignments[0] if state["slot_count"] == 1 else None,
                "assignments": assignments,
                "slots": slot_results,
                "blocked_slots": blocked,
                "recovered": recovered,
            }
        retries = [
            value["assignment"]
            for value in slot_results.values()
            if value.get("status") == "retry_assigned" and isinstance(value.get("assignment"), dict)
        ]
        if retries:
            return {
                "status": "retry_assigned",
                "assignment": retries[0] if state["slot_count"] == 1 else None,
                "assignments": retries,
                "slots": slot_results,
                "recovered": recovered,
            }
        sync_blocks = [value for value in slot_results.values() if value.get("status") == "blocked_sync_retry"]
        if sync_blocks:
            return {
                "status": "blocked_sync_retry",
                "assignment_id": sync_blocks[0].get("assignment_id") if state["slot_count"] == 1 else None,
                "slots": slot_results,
                "recovered": recovered,
            }
        if blocked:
            response = {"status": "unmanaged_assignment", "blocked_slots": blocked, "slots": slot_results}
            if state["slot_count"] == 1:
                response["assignment_id"] = blocked[0].get("assignment_id")
            return response
        if recovered:
            return {
                "status": "transition_recovered",
                "assignment": recovered[0] if state["slot_count"] == 1 else None,
                "assignments": recovered,
                "slots": slot_results,
            }
        if state["slot_count"] == 1 and slot_results.get("slot01", {}).get("status") == "waiting":
            return {
                "status": "waiting",
                "assignment_id": slot_results["slot01"].get("assignment_id"),
            }
        return {"status": "waiting", "slots": slot_results, "recovered": recovered}


def queue_status(paths: QueuePaths, bridge: BridgePaths) -> dict[str, Any]:
    if not _queue_exists(paths):
        return {"initialized": False, "queue_status": "absent"}
    entries = _read_entries(paths)
    state = _read_state(paths)
    bridges = _bridges(bridge, state["slot_count"])
    slots: dict[str, Any] = {}
    for slot_id, slot_bridge in bridges.items():
        assignment = _read_assignment_if_present(slot_bridge)
        slot = state["slots"][slot_id]
        entry = next((item for item in entries if item["queue_position"] == slot.get("queue_position")), None)
        assignment_matches = bool(
            assignment and assignment.get("assignment_id") == slot.get("assignment_id")
        )
        unmanaged_active = bool(
            assignment
            and not assignment_matches
            and assignment.get("status") not in TERMINAL_ASSIGNMENT_STATUSES
        )
        slots[slot_id] = {
            "status": "unmanaged_assignment" if unmanaged_active else slot.get("status", "idle"),
            "company": entry["company_name"] if entry else None,
            "ticker": entry["ticker"] if entry else None,
            "queue_position": entry["queue_position"] if entry else None,
            "assignment_id": assignment.get("assignment_id") if unmanaged_active else slot.get("assignment_id"),
            "assignment_status": assignment.get("status") if assignment_matches or unmanaged_active else None,
        }
    completed = [entry for entry in entries if entry["status"] == "completed"]
    failed = [entry for entry in entries if entry["status"] == "failed"]
    pending = [entry for entry in entries if entry["status"] in {"pending", "paused"}]
    active_entries = [entry for entry in entries if entry["status"] == "assigned"]
    current = active_entries[0] if active_entries else None
    slot01_assignment = _read_assignment_if_present(bridges["slot01"])
    tasks = {
        task_id: {slot_id: slots[slot_id] for slot_id in task_slots}
        for task_id, task_slots in state["task_slots"].items()
    }
    started_at = state.get("started_at")
    ended_at = state.get("completed_at") or _iso()
    elapsed_seconds: float | None = None
    if started_at:
        try:
            elapsed_seconds = max(
                0.0,
                (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds(),
            )
        except (TypeError, ValueError):
            elapsed_seconds = None
    latencies: list[float] = []
    for entry in completed:
        if entry.get("first_assigned_at") and entry.get("completed_at"):
            try:
                latencies.append(
                    max(
                        0.0,
                        (
                            datetime.fromisoformat(entry["completed_at"])
                            - datetime.fromisoformat(entry["first_assigned_at"])
                        ).total_seconds(),
                    )
                )
            except (TypeError, ValueError):
                pass
    completions_per_task = {
        task_id: sum(
            entry["status"] == "completed" and entry.get("completed_task") == task_id
            for entry in entries
        )
        for task_id in state["task_slots"]
    }
    completions_per_slot = {
        slot_id: sum(
            entry["status"] == "completed" and entry.get("completed_slot") == slot_id
            for entry in entries
        )
        for slot_id in slots
    }
    task_run_records: dict[str, dict[str, Any]] = {}
    for log_path in (paths.work_dir / "task_runs").glob("task??/runs.jsonl"):
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("queue_id") == state["queue_id"] and record.get("run_id"):
                    task_run_records[str(record["run_id"])] = record
        except (OSError, json.JSONDecodeError):
            continue
    guard_paths = list((paths.work_dir / "task_runs").glob("task??/active.json"))
    guard_paths.extend((paths.work_dir / "task_runs").glob("task??/history/*.stale.json"))
    for guard_path in guard_paths:
        try:
            record = json.loads(guard_path.read_text(encoding="utf-8"))
            if record.get("queue_id") == state["queue_id"] and record.get("run_id"):
                task_run_records.setdefault(str(record["run_id"]), record)
        except (OSError, json.JSONDecodeError):
            continue
    task_run_count = len(task_run_records)
    productive_run_count = sum(
        isinstance(record.get("snapshot_count"), int) and record["snapshot_count"] > 0
        for record in task_run_records.values()
    )
    empty_run_count = sum(record.get("snapshot_count") == 0 for record in task_run_records.values())
    completed_count = len(completed)
    companies_per_hour = (
        completed_count / (elapsed_seconds / 3600)
        if elapsed_seconds and completed_count
        else None
    )
    metrics = {
        **state["metrics"],
        "queue_started_at": started_at,
        "queue_completed_at": state.get("completed_at"),
        "elapsed_seconds": elapsed_seconds,
        "retry_count": sum(max(0, entry["attempt_count"] - 1) for entry in entries),
        "total_scheduled_runs": task_run_count,
        "productive_runs": productive_run_count,
        "empty_runs": empty_run_count,
        "company_completions_per_task": completions_per_task,
        "company_completions_per_slot": completions_per_slot,
        "average_companies_per_run": completed_count / task_run_count if task_run_count else None,
        "companies_per_productive_run": (
            completed_count / productive_run_count if productive_run_count else None
        ),
        "average_completion_latency_seconds": sum(latencies) / len(latencies) if latencies else None,
        "max_completion_latency_seconds": max(latencies) if latencies else None,
        "no_news_count": sum(entry.get("news_item_count") == 0 for entry in completed),
        "news_present_count": sum(
            isinstance(entry.get("news_item_count"), int) and entry["news_item_count"] > 0
            for entry in completed
        ),
        "companies_per_hour": companies_per_hour,
        "companies_per_day": companies_per_hour * 24 if companies_per_hour is not None else None,
        "estimated_hours_for_3800": 3800 / companies_per_hour if companies_per_hour else None,
        "estimated_days_for_3800": 3800 / companies_per_hour / 24 if companies_per_hour else None,
    }
    return {
        "initialized": True,
        "queue_id": state["queue_id"],
        "queue_status": state["queue_status"],
        "slot_count": state["slot_count"],
        "logical_slot_count": state["slot_count"],
        "task_count": state["task_count"],
        "batch_size": state["batch_size"],
        "current_company": current,
        "completed": len(completed),
        "total": len(entries),
        "pending": len(pending),
        "active": len(active_entries),
        "failed": len(failed),
        "last_completed": state.get("last_completed"),
        "next_assignment": slot01_assignment if slot01_assignment and slot01_assignment.get("status") == "ready" else None,
        "slots": slots,
        "tasks": tasks,
        "metrics": metrics,
    }


def pause_queue(paths: QueuePaths) -> dict[str, Any]:
    with _queue_lock(paths):
        entries = _read_entries(paths)
        state = _read_state(paths)
        if state["queue_status"] == "completed":
            raise QueueError("completed queue cannot be paused")
        for entry in entries:
            if entry["status"] == "pending":
                entry["status"] = "paused"
        state.update({"queue_status": "paused", "updated_at": _iso()})
        _atomic_jsonl(paths.entries, entries)
        _write_state(paths, state)
        return {"queue_status": "paused"}


def increment_queue_metrics(paths: QueuePaths, **increments: int) -> None:
    if not _queue_exists(paths):
        return
    allowed = {"stale_payload_count", "validation_failure_count", "sync_retry_count"}
    if set(increments).difference(allowed) or any(
        not isinstance(value, int) or value < 0 for value in increments.values()
    ):
        raise QueueError("invalid queue metric increment")
    if not any(increments.values()):
        return
    with _queue_lock(paths):
        state = _read_state(paths)
        for name, value in increments.items():
            state["metrics"][name] = state["metrics"].get(name, 0) + value
        state["updated_at"] = _iso()
        _write_state(paths, state)


def resume_queue(paths: QueuePaths, bridge: BridgePaths, db_path: Path, *, activate: bool = False) -> dict[str, Any]:
    reconcile_after = False
    result: dict[str, Any] | None = None
    with _queue_lock(paths):
        entries = _read_entries(paths)
        state = _read_state(paths)
        if state.get("fixture_mode") and not activate:
            raise QueueError("fixture queue requires resume --activate before it can write slots")
        if state["queue_status"] == "active":
            reconcile_after = True
        elif state["queue_status"] not in {"paused", "fixture_ready"}:
            return {"queue_status": state["queue_status"]}
        else:
            bridges = _bridges(bridge, state["slot_count"])
            recovered = _recover_transitions(paths, bridges, entries, state)
            for entry in entries:
                if entry["status"] == "paused":
                    entry["status"] = "pending"
            existing_active_slot = any(
                slot.get("assignment_id") for slot in state["slots"].values()
            )
            if existing_active_slot:
                # Existing durable assignments make this a valid active state;
                # no new slot write is required to complete the resume.
                state.update({
                    "queue_status": "active",
                    "fixture_mode": False,
                    "started_at": state.get("started_at") or _iso(),
                    "updated_at": _iso(),
                })
                _atomic_jsonl(paths.entries, entries)
                _write_state(paths, state)
            assignments, blocked = _fill_idle_slots(
                paths,
                bridges,
                entries,
                state,
                db_path,
                _now(),
                activate_queue=not existing_active_slot,
            )
            if assignments:
                result = {
                    "status": "assigned",
                    "assignment": assignments[0] if state["slot_count"] == 1 else None,
                    "assignments": assignments,
                    "blocked_slots": blocked,
                    "recovered": recovered,
                }
            elif recovered:
                result = {"status": "transition_recovered", "assignments": recovered}
                reconcile_after = True
            elif blocked:
                if state["slot_count"] == 1:
                    result = {"status": "unmanaged_assignment", "assignment_id": blocked[0].get("assignment_id")}
                else:
                    result = {"status": "unmanaged_assignment", "blocked_slots": blocked}
            elif not blocked and _maybe_complete(paths, bridges, entries, state, _now()):
                result = {"status": "queue_completed"}
            else:
                reconcile_after = True
    if reconcile_after:
        return reconcile_queue(paths, bridge, db_path)
    if result is not None:
        return result
    raise QueueError("queue resume reached an invalid state")


def reset_pilot(paths: QueuePaths, *, confirmation: str) -> dict[str, Any]:
    if confirmation != "RESET-PILOT":
        raise QueueError("reset-pilot requires --confirm RESET-PILOT")
    with _queue_lock(paths):
        state = _read_state(paths)
        if not state.get("fixture_mode") or state["queue_status"] not in {"fixture_ready", "paused"}:
            raise QueueError("reset-pilot only removes an inactive fixture queue")
        paths.entries.unlink(missing_ok=True)
        paths.state.unlink(missing_ok=True)
    return {"status": "reset", "removed": [str(paths.entries), str(paths.state)]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--inbox", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--master-db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-pilot")
    init.add_argument("--activate", action="store_true", help="explicitly create real slot assignments")
    init.add_argument("--slots", type=int)
    init.add_argument("--tasks", type=int)
    init.add_argument("--batch-size", type=int)
    init.add_argument("--count", type=int, default=PILOT_SIZE)
    soak = commands.add_parser("init-soak")
    soak.add_argument("--count", type=int, default=SOAK_COMPANY_COUNT)
    soak.add_argument("--tasks", type=int, default=SOAK_TASK_COUNT)
    soak.add_argument("--batch-size", type=int, default=SOAK_BATCH_SIZE)
    commands.add_parser("status")
    commands.add_parser("pause")
    commands.add_parser("activate")
    resume = commands.add_parser("resume")
    resume.add_argument("--activate", action="store_true", help="activate an inert fixture queue")
    reset = commands.add_parser("reset-pilot")
    reset.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    work_dir = (args.work_dir or root / "data" / "news_work").resolve()
    inbox = (args.inbox or root / "data" / "news_inbox").resolve()
    db_path = (args.db or root / "decision_db.db").resolve()
    master_db = (args.master_db or root / "data" / "jquants.db").resolve()
    paths = QueuePaths.from_values(root, work_dir)
    bridge = BridgePaths.from_root(root, work_dir, inbox)
    try:
        if args.command == "init-pilot":
            result = initialize_pilot(
                paths,
                bridge,
                master_db,
                db_path,
                activate=args.activate,
                slot_count=args.slots,
                count=args.count,
                task_count=args.tasks,
                batch_size=args.batch_size,
            )
        elif args.command == "init-soak":
            result = initialize_pilot(
                paths,
                bridge,
                master_db,
                db_path,
                activate=False,
                count=args.count,
                task_count=args.tasks,
                batch_size=args.batch_size,
                stratified=True,
            )
        elif args.command == "status":
            result = queue_status(paths, bridge)
        elif args.command == "pause":
            result = pause_queue(paths)
        elif args.command == "activate":
            result = resume_queue(paths, bridge, db_path, activate=True)
        elif args.command == "resume":
            result = resume_queue(paths, bridge, db_path, activate=args.activate)
        else:
            result = reset_pilot(paths, confirmation=args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (QueueError, BridgeError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
