#!/usr/bin/env python3
"""One-slot deterministic company queue for the Company News Work bridge.

Queue files are inert until an operator explicitly activates a pilot.  The inbox
worker calls :func:`reconcile_queue` after its normal bridge processing; when no
queue exists, the function is a read-only no-op.
"""
from __future__ import annotations

import argparse
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
_JST = timezone(timedelta(hours=9))
_LOCK_PID_RE = re.compile(r"pid=(\d+)")


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


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    body = "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values)
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, path)


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
    values: list[dict[str, Any]] = []
    seen_positions: set[int] = set()
    seen_tickers: set[str] = set()
    required = {
        "schema_version", "ticker", "company_name", "sector", "queue_position", "status",
        "last_checked_at", "next_eligible_at", "attempt_count", "last_error",
    }
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
        if value["queue_position"] in seen_positions or value["ticker"] in seen_tickers:
            raise QueueError("queue entries must have unique positions and tickers")
        seen_positions.add(value["queue_position"])
        seen_tickers.add(value["ticker"])
        values.append(value)
    return sorted(values, key=lambda item: item["queue_position"])


def _read_state(paths: QueuePaths) -> dict[str, Any]:
    state = _read_object(paths.state)
    if state.get("schema_version") != QUEUE_STATE_SCHEMA:
        raise QueueError(f"schema_version must be {QUEUE_STATE_SCHEMA}")
    if state.get("queue_status") not in QUEUE_STATUSES:
        raise QueueError(f"unsupported queue_status: {state.get('queue_status')}")
    if not isinstance(state.get("assignment_sequence"), int) or state["assignment_sequence"] < 0:
        raise QueueError("assignment_sequence must be a non-negative integer")
    return state


def _queue_exists(paths: QueuePaths) -> bool:
    return paths.entries.exists() and paths.state.exists()


def _master_sample(master_db: Path, limit: int = PILOT_SIZE) -> list[dict[str, Any]]:
    if limit < 1:
        raise QueueError("pilot size must be positive")
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

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            raw_ticker = str(row["ticker"]).upper()
            if len(raw_ticker) == 5 and raw_ticker.endswith("0"):
                raw_ticker = raw_ticker[:4]
            try:
                ticker = normalize_ticker(raw_ticker)
            except NewsValidationError:
                continue
            if ticker in seen:
                continue
            company_name = str(row["company_name"]).strip()
            if not company_name:
                continue
            selected.append({"ticker": ticker, "company_name": company_name, "sector": str(row["sector"] or "unknown").strip() or "unknown"})
            seen.add(ticker)
            if len(selected) == limit:
                break
        if len(selected) != limit:
            raise QueueError(f"company master provided only {len(selected)} eligible companies")
        return selected
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
    state: dict[str, Any], entry: dict[str, Any], db_path: Path, now: datetime
) -> tuple[dict[str, Any], int]:
    sequence = state["assignment_sequence"] + 1
    queue_token = str(state["queue_id"]).removeprefix("pilot5-")
    assignment_id = f"slot01-{queue_token}-{sequence:06d}"
    search_from, search_to = _search_period(db_path, entry["ticker"], now)
    assignment = {
        "schema_version": ASSIGNMENT_SCHEMA,
        "slot_id": "slot01",
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
    }
    return assignment, sequence


def _read_assignment_if_present(bridge: BridgePaths) -> dict[str, Any] | None:
    if not bridge.assignment.exists():
        return None
    assignment = _read_object(bridge.assignment)
    try:
        return validate_assignment(assignment, bridge)
    except BridgeError:
        # Queue activation must fail closed for legacy/non-canonical in-flight
        # status values such as ``running`` or ``processing``.  They are not
        # accepted by the bridge contract, but retaining the minimal identity
        # lets the queue report ``unmanaged_assignment`` instead of overwriting
        # the slot or mistaking a validation error for an empty slot.
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
    assignment: dict[str, Any], entries: list[dict[str, Any]], state: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the owning queue entry using explicit queue metadata."""
    if assignment.get("queue_id") != state.get("queue_id"):
        return None
    position = assignment.get("queue_position")
    assignment_id = assignment.get("assignment_id")
    if not isinstance(position, int) or not isinstance(assignment_id, str):
        return None
    entry = next((item for item in entries if item["queue_position"] == position), None)
    if entry is None:
        return None
    if assignment_id not in {entry.get("assignment_id"), state.get("current_assignment_id")}:
        return None
    return entry


def _archive_terminal_unmanaged_assignment(
    bridge: BridgePaths, assignment: dict[str, Any]
) -> Path:
    """Preserve a terminal legacy assignment before replacing the slot."""
    assignment_id = assignment.get("assignment_id")
    if not isinstance(assignment_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", assignment_id):
        raise QueueError("terminal unmanaged assignment has an invalid assignment_id")
    preserved_assignment = assignment
    if bridge.assignment.exists():
        source_assignment = _read_object(bridge.assignment)
        if source_assignment.get("assignment_id") == assignment_id:
            preserved_assignment = source_assignment
    history = bridge.assignment.parent / "history"
    target = history / f"{assignment_id}.json"
    if target.exists():
        preserved = _read_object(target)
        if preserved != preserved_assignment:
            raise QueueError(f"assignment history conflict: {target}")
        return target
    _atomic_json(target, preserved_assignment)
    return target


def _commit_assignment_transition(
    paths: QueuePaths,
    bridge: BridgePaths,
    entries: list[dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    transition = state.get("transition")
    if not isinstance(transition, dict) or transition.get("kind") != "assign":
        raise QueueError("queue assignment transition is missing")
    assignment = transition.get("assignment")
    position = transition.get("queue_position")
    sequence = transition.get("assignment_sequence")
    if not isinstance(assignment, dict) or not isinstance(position, int) or not isinstance(sequence, int):
        raise QueueError("queue assignment transition is invalid")
    validate_assignment(dict(assignment), bridge)

    current = _read_assignment_if_present(bridge)
    if current and current["assignment_id"] != assignment["assignment_id"]:
        if current.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
            raise QueueError(f"slot01 already has an unfinished assignment: {current['assignment_id']}")
        if _assignment_entry(current, entries, state) is None:
            _archive_terminal_unmanaged_assignment(bridge, current)
    if not current or current["assignment_id"] != assignment["assignment_id"]:
        _atomic_json(bridge.assignment, assignment)

    matching = next((entry for entry in entries if entry["queue_position"] == position), None)
    if matching is None:
        raise QueueError(f"queue position {position} is absent")
    matching.update({
        "status": "assigned",
        "attempt_count": assignment["queue_attempt"],
        "assignment_id": assignment["assignment_id"],
        "next_eligible_at": None,
    })
    state.update({
        "assignment_sequence": sequence,
        "current_queue_position": position,
        "current_assignment_id": assignment["assignment_id"],
        "slot_status": "ready",
        "next_assignment": assignment["assignment_id"],
        "updated_at": _iso(),
    })
    if transition.get("activate_queue"):
        state.update({"queue_status": "active", "fixture_mode": False})
    state.pop("transition", None)
    _atomic_jsonl(paths.entries, entries)
    _atomic_json(paths.state, state)
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
    assignment, sequence = _assignment_for_entry(state, entry, db_path, now)
    state["transition"] = {
        "kind": "assign",
        "queue_position": entry["queue_position"],
        "assignment_sequence": sequence,
        "assignment": assignment,
        "activate_queue": activate_queue,
    }
    state["updated_at"] = _iso(now)
    _atomic_json(paths.state, state)
    return _commit_assignment_transition(paths, bridge, entries, state)


def _recover_transition(
    paths: QueuePaths, bridge: BridgePaths, entries: list[dict[str, Any]], state: dict[str, Any]
) -> dict[str, Any] | None:
    if state.get("transition") is None:
        return None
    return _commit_assignment_transition(paths, bridge, entries, state)


def _set_bridge_idle(bridge: BridgePaths, state: dict[str, Any], now: datetime) -> None:
    bridge_state = {
        "schema_version": "company_news_bridge_state_v1",
        "slot_id": "slot01",
        "assignment_id": state.get("last_assignment_id"),
        "phase": "idle",
        "queue_id": state["queue_id"],
        "updated_at": _iso(now),
    }
    _atomic_json(bridge.state, bridge_state)


def _finish_or_assign_next(
    paths: QueuePaths,
    bridge: BridgePaths,
    entries: list[dict[str, Any]],
    state: dict[str, Any],
    db_path: Path,
    now: datetime,
) -> dict[str, Any]:
    pending = next((entry for entry in entries if entry["status"] == "pending"), None)
    if state["queue_status"] == "paused":
        state.update({"current_queue_position": None, "current_assignment_id": None, "slot_status": "paused", "next_assignment": None, "updated_at": _iso(now)})
        _atomic_json(paths.state, state)
        return {"status": "paused"}
    if pending is None:
        state.update({"queue_status": "completed", "current_queue_position": None, "current_assignment_id": None, "slot_status": "idle", "next_assignment": None, "updated_at": _iso(now)})
        _atomic_json(paths.state, state)
        _set_bridge_idle(bridge, state, now)
        return {"status": "queue_completed"}
    assignment = _plan_assignment(paths, bridge, entries, state, pending, db_path, now)
    return {"status": "assigned", "assignment": assignment}


def _activate_queue_locked(
    paths: QueuePaths,
    bridge: BridgePaths,
    entries: list[dict[str, Any]],
    state: dict[str, Any],
    db_path: Path,
    now: datetime,
) -> dict[str, Any]:
    """Activate an inert queue while both the queue and slot locks are held."""
    recovered = _recover_transition(paths, bridge, entries, state)
    if recovered:
        return {"status": "transition_recovered", "assignment": recovered}

    current = _read_assignment_if_present(bridge)
    if current is not None:
        owner = _assignment_entry(current, entries, state)
        if owner is None:
            if current.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
                return {"status": "unmanaged_assignment", "assignment_id": current.get("assignment_id")}
            _archive_terminal_unmanaged_assignment(bridge, current)
        else:
            # A paused queue may already own an assignment.  Resuming it does
            # not create another assignment; only the queue state changes.
            for entry in entries:
                if entry["status"] == "paused":
                    entry["status"] = "pending"
            state.update({"queue_status": "active", "fixture_mode": False, "updated_at": _iso(now)})
            _atomic_jsonl(paths.entries, entries)
            _atomic_json(paths.state, state)
            if current.get("status") in TERMINAL_ASSIGNMENT_STATUSES:
                return {"status": "activation_reconcile_required"}
            return {"status": "waiting", "assignment_id": current["assignment_id"]}

    for entry in entries:
        if entry["status"] == "paused":
            entry["status"] = "pending"
    pending = next((entry for entry in entries if entry["status"] == "pending"), None)
    if pending is None:
        state.update({
            "queue_status": "completed",
            "fixture_mode": False,
            "current_queue_position": None,
            "current_assignment_id": None,
            "slot_status": "idle",
            "next_assignment": None,
            "updated_at": _iso(now),
        })
        _atomic_jsonl(paths.entries, entries)
        _atomic_json(paths.state, state)
        _set_bridge_idle(bridge, state, now)
        return {"status": "queue_completed"}

    assignment = _plan_assignment(
        paths,
        bridge,
        entries,
        state,
        pending,
        db_path,
        now,
        activate_queue=True,
    )
    return {"status": "assigned", "assignment": assignment}


def initialize_pilot(
    paths: QueuePaths,
    bridge: BridgePaths,
    master_db: Path,
    db_path: Path,
    *,
    activate: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _now(now)
    with _queue_lock(paths):
        if paths.entries.exists() or paths.state.exists():
            raise QueueError("company queue already exists; inspect status or use reset-pilot")
        companies = _master_sample(master_db, PILOT_SIZE)
        entries = [
            {
                "schema_version": QUEUE_SCHEMA,
                **company,
                "queue_position": index,
                "status": "pending",
                "last_checked_at": None,
                "next_eligible_at": None,
                "attempt_count": 0,
                "last_error": None,
                "assignment_id": None,
            }
            for index, company in enumerate(companies, start=1)
        ]
        state = {
            "schema_version": QUEUE_STATE_SCHEMA,
            "queue_id": f"pilot5-{timestamp:%Y%m%dT%H%M%S%f}",
            # Even explicit activation starts from an inert state.  The final
            # active state is committed only after the slot assignment has
            # been atomically written.
            "queue_status": "fixture_ready",
            "fixture_mode": True,
            "total": len(entries),
            "max_attempts": MAX_ATTEMPTS,
            "assignment_sequence": 0,
            "current_queue_position": None,
            "current_assignment_id": None,
            "slot_status": "idle",
            "last_completed": None,
            "last_assignment_id": None,
            "next_assignment": None,
            "created_at": _iso(timestamp),
            "updated_at": _iso(timestamp),
        }
        _atomic_jsonl(paths.entries, entries)
        _atomic_json(paths.state, state)
        assignment = None
        if activate:
            with _slot_lock(bridge):
                activation = _activate_queue_locked(paths, bridge, entries, state, db_path, timestamp)
                assignment = activation.get("assignment")
        return {"queue_id": state["queue_id"], "queue_status": state["queue_status"], "companies": companies, "assignment": assignment}


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
        with _slot_lock(bridge):
            recovered = _recover_transition(paths, bridge, entries, state)
            if recovered:
                return {"status": "transition_recovered", "assignment": recovered}
            if state["queue_status"] == "fixture_ready":
                return {"status": "fixture_ready"}
            if state["queue_status"] == "completed":
                return {"status": "queue_completed"}
            assignment = _read_assignment_if_present(bridge)
            if assignment is None:
                if state["queue_status"] == "active":
                    return _finish_or_assign_next(paths, bridge, entries, state, db_path, timestamp)
                return {"status": state["queue_status"]}
            entry = _assignment_entry(assignment, entries, state)
            if entry is None:
                if assignment.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
                    return {"status": "unmanaged_assignment", "assignment_id": assignment.get("assignment_id")}
                _archive_terminal_unmanaged_assignment(bridge, assignment)
                if state["queue_status"] == "active":
                    return _finish_or_assign_next(paths, bridge, entries, state, db_path, timestamp)
                return {"status": state["queue_status"], "archived_assignment_id": assignment.get("assignment_id")}

            if assignment.get("status") not in TERMINAL_ASSIGNMENT_STATUSES:
                return {"status": "waiting", "assignment_id": assignment["assignment_id"]}

            if assignment["status"] == "completed":
                checked_at = _successful_checked_at(db_path, run_id=assignment["assignment_id"])
                if checked_at is None:
                    return {"status": "blocked_missing_canonical_run", "assignment_id": assignment["assignment_id"]}
                entry.update({"status": "completed", "last_checked_at": checked_at, "next_eligible_at": None, "last_error": None})
                state.update({
                    "last_completed": {"ticker": entry["ticker"], "company_name": entry["company_name"], "checked_at": checked_at},
                    "last_assignment_id": assignment["assignment_id"],
                    "current_queue_position": None,
                    "current_assignment_id": None,
                    "slot_status": "completed",
                    "updated_at": _iso(timestamp),
                })
                _atomic_jsonl(paths.entries, entries)
                _atomic_json(paths.state, state)
                return _finish_or_assign_next(paths, bridge, entries, state, db_path, timestamp)

            bridge_state = _read_object(bridge.state) if bridge.state.exists() else {}
            error = str(assignment.get("error_message") or bridge_state.get("last_error") or "assignment failed")[:1000]
            entry["last_error"] = error
            entry["next_eligible_at"] = _iso(timestamp)
            state.update({"last_assignment_id": assignment["assignment_id"], "updated_at": _iso(timestamp)})
            if bridge_state.get("phase") in {"ingested", "synced"}:
                _atomic_jsonl(paths.entries, entries)
                _atomic_json(paths.state, state)
                return {"status": "blocked_sync_retry", "assignment_id": assignment["assignment_id"]}
            if state["queue_status"] == "paused":
                _atomic_jsonl(paths.entries, entries)
                _atomic_json(paths.state, state)
                return {"status": "paused", "assignment_id": assignment["assignment_id"]}
            if entry["attempt_count"] < state.get("max_attempts", MAX_ATTEMPTS):
                _atomic_jsonl(paths.entries, entries)
                _atomic_json(paths.state, state)
                retry = _plan_assignment(paths, bridge, entries, state, entry, db_path, timestamp)
                return {"status": "retry_assigned", "assignment": retry}

            entry.update({"status": "failed", "next_eligible_at": None})
            state.update({"current_queue_position": None, "current_assignment_id": None, "slot_status": "failed", "updated_at": _iso(timestamp)})
            _atomic_jsonl(paths.entries, entries)
            _atomic_json(paths.state, state)
            return _finish_or_assign_next(paths, bridge, entries, state, db_path, timestamp)


def queue_status(paths: QueuePaths, bridge: BridgePaths) -> dict[str, Any]:
    if not _queue_exists(paths):
        return {"initialized": False, "queue_status": "absent"}
    entries = _read_entries(paths)
    state = _read_state(paths)
    current = next((entry for entry in entries if entry["queue_position"] == state.get("current_queue_position")), None)
    completed = [entry for entry in entries if entry["status"] == "completed"]
    failed = [entry for entry in entries if entry["status"] == "failed"]
    pending = [entry for entry in entries if entry["status"] in {"pending", "paused"}]
    assignment = _read_assignment_if_present(bridge)
    return {
        "initialized": True,
        "queue_id": state["queue_id"],
        "queue_status": state["queue_status"],
        "current_company": current,
        "completed": len(completed),
        "total": len(entries),
        "pending": len(pending),
        "failed": len(failed),
        "last_completed": state.get("last_completed"),
        "next_assignment": assignment if assignment and assignment.get("status") == "ready" else None,
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
        _atomic_json(paths.state, state)
        return {"queue_status": "paused"}


def resume_queue(paths: QueuePaths, bridge: BridgePaths, db_path: Path, *, activate: bool = False) -> dict[str, Any]:
    reconcile_active = False
    activation_result: dict[str, Any] | None = None
    with _queue_lock(paths):
        entries = _read_entries(paths)
        state = _read_state(paths)
        if state.get("fixture_mode") and not activate:
            raise QueueError("fixture queue requires resume --activate before it can write slot01")
        if state["queue_status"] == "active":
            reconcile_active = True
        elif state["queue_status"] not in {"paused", "fixture_ready"}:
            return {"queue_status": state["queue_status"]}
        else:
            with _slot_lock(bridge):
                activation_result = _activate_queue_locked(paths, bridge, entries, state, db_path, _now())
    if reconcile_active or (
        activation_result is not None and activation_result.get("status") == "activation_reconcile_required"
    ):
        return reconcile_queue(paths, bridge, db_path)
    if activation_result is not None:
        return activation_result
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
    init.add_argument("--activate", action="store_true", help="explicitly create the first real slot01 assignment")
    commands.add_parser("status")
    commands.add_parser("pause")
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
            result = initialize_pilot(paths, bridge, master_db, db_path, activate=args.activate)
        elif args.command == "status":
            result = queue_status(paths, bridge)
        elif args.command == "pause":
            result = pause_queue(paths)
        elif args.command == "resume":
            result = resume_queue(paths, bridge, db_path, activate=args.activate)
        else:
            result = reset_pilot(paths, confirmation=args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (QueueError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
