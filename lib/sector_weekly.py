"""Canonical storage and validation for TSE 33-sector weekly reports."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = "sector_weekly_v1"
REPORT_TYPE = "sector_weekly"
JST = timezone(timedelta(hours=9))
IMPORTANCE = frozenset({"A+", "A", "B", "C"})
DIRECTIONS = frozenset({"positive", "negative", "mixed", "neutral"})
SOURCE_TYPES = frozenset({
    "company_ir", "government", "regulator", "industry_association", "news", "market_data", "other"
})

SECTORS: tuple[str, ...] = (
    "水産・農林業", "鉱業", "建設業", "食料品", "繊維製品", "パルプ・紙", "化学", "医薬品",
    "石油・石炭製品", "ゴム製品", "ガラス・土石製品", "鉄鋼", "非鉄金属", "金属製品", "機械",
    "電気機器", "輸送用機器", "精密機器", "その他製品", "電気・ガス業", "陸運業", "海運業",
    "空運業", "倉庫・運輸関連業", "情報・通信業", "卸売業", "小売業", "銀行業",
    "証券・商品先物取引業", "保険業", "その他金融業", "不動産業", "サービス業",
)


class SectorValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WeeklyWindow:
    period_start: datetime
    period_end: datetime
    week_key: str


@dataclass(frozen=True)
class ValidatedSectorReport:
    report: dict[str, Any]


def now_jst() -> datetime:
    return datetime.now(JST)


def iso_seconds(value: datetime) -> str:
    if value.tzinfo is None:
        raise SectorValidationError("datetime must include a timezone")
    return value.isoformat(timespec="seconds")


def parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SectorValidationError(f"{field} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SectorValidationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SectorValidationError(f"{field} must include a timezone")
    return parsed


def weekly_window(value: datetime) -> WeeklyWindow:
    """Return the common prior-Saturday 06:00 through current-Saturday 05:59:59 JST window."""
    local = value.astimezone(JST)
    days_since_saturday = (local.weekday() - 5) % 7
    current_saturday = (local - timedelta(days=days_since_saturday)).date()
    cutoff = datetime.combine(current_saturday, datetime.min.time(), JST) + timedelta(hours=6)
    if local < cutoff:
        cutoff -= timedelta(days=7)
    return WeeklyWindow(
        period_start=cutoff - timedelta(days=7),
        period_end=cutoff - timedelta(seconds=1),
        week_key=(cutoff - timedelta(seconds=1)).date().isoformat(),
    )


def scheduled_sector(value: datetime) -> int | None:
    local = value.astimezone(JST)
    if local.weekday() == 5 and 6 <= local.hour <= 23:
        return local.hour - 5
    if local.weekday() == 6 and 0 <= local.hour <= 14:
        return 19 + local.hour
    return None


def in_retry_window(value: datetime) -> bool:
    local = value.astimezone(JST)
    return local.weekday() == 6 and 15 <= local.hour <= 23


def sector_name(code: int) -> str:
    if not isinstance(code, int) or not 1 <= code <= len(SECTORS):
        raise SectorValidationError("sector_code must be between 1 and 33")
    return SECTORS[code - 1]


def dedupe_key(window: WeeklyWindow, code: int) -> str:
    sector_name(code)
    return f"sector_weekly:{window.week_key}:{code:02d}"


def stable_report_id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tse-sector-report:{key}"))


def _text(value: Any, field: str, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise SectorValidationError(f"{field} must be a string")
    result = value.strip()
    if len(result) < minimum or len(result) > maximum:
        raise SectorValidationError(f"{field} length must be {minimum}..{maximum}")
    return result


def _string_list(value: Any, field: str, minimum: int, maximum: int, item_max: int = 1000) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise SectorValidationError(f"{field} must be an array with {minimum}..{maximum} items")
    return [_text(item, f"{field}[]", item_max) for item in value]


def _http_url(value: Any, field: str) -> str:
    url = _text(value, field, 2048)
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise SectorValidationError(f"{field} must be an absolute http/https URL")
    return url


def validate_report(
    payload: Any,
    *,
    expected_code: int | None = None,
    expected_window: WeeklyWindow | None = None,
) -> ValidatedSectorReport:
    if not isinstance(payload, dict):
        raise SectorValidationError("payload must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("report_type") != REPORT_TYPE:
        raise SectorValidationError("unsupported sector report schema")
    code = payload.get("sector_code")
    name = sector_name(code)
    if expected_code is not None and code != expected_code:
        raise SectorValidationError("sector_code does not match assignment")
    if payload.get("sector_name") != name:
        raise SectorValidationError("sector_name does not match fixed TSE mapping")
    start = parse_datetime(payload.get("period_start"), "period_start")
    end = parse_datetime(payload.get("period_end"), "period_end")
    generated = parse_datetime(payload.get("generated_at"), "generated_at")
    if end < start:
        raise SectorValidationError("period_end precedes period_start")
    if expected_window and (
        start.astimezone(JST) != expected_window.period_start or end.astimezone(JST) != expected_window.period_end
    ):
        raise SectorValidationError("report period does not match the common weekly window")
    importance = payload.get("importance")
    direction = payload.get("direction")
    if importance not in IMPORTANCE:
        raise SectorValidationError("importance must be A+, A, B, or C")
    if direction not in DIRECTIONS:
        raise SectorValidationError("direction must be positive, negative, mixed, or neutral")
    bullets = _string_list(payload.get("summary_bullets"), "summary_bullets", 3, 6, 240)
    full_report = _text(payload.get("full_report_md"), "full_report_md", 100_000, 200)
    watchlist_raw = payload.get("watchlist_companies")
    if not isinstance(watchlist_raw, list) or len(watchlist_raw) > 20:
        raise SectorValidationError("watchlist_companies must be an array of at most 20 items")
    watchlist: list[dict[str, str]] = []
    for item in watchlist_raw:
        if not isinstance(item, dict):
            raise SectorValidationError("watchlist company must be an object")
        ticker = _text(item.get("code"), "watchlist.code", 5)
        if not re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", ticker):
            raise SectorValidationError("watchlist.code must be a TSE ticker")
        item_direction = item.get("direction")
        if item_direction not in DIRECTIONS:
            raise SectorValidationError("watchlist.direction is invalid")
        watchlist.append({"code": ticker, "name": _text(item.get("name"), "watchlist.name", 200), "direction": item_direction})
    watchpoints = _string_list(payload.get("next_week_watchpoints", []), "next_week_watchpoints", 0, 20, 1000)
    missed = _string_list(payload.get("missed_candidates", []), "missed_candidates", 0, 20, 1000)
    sources_raw = payload.get("sources")
    if not isinstance(sources_raw, list) or not 1 <= len(sources_raw) <= 100:
        raise SectorValidationError("sources must contain 1..100 items")
    sources: list[dict[str, Any]] = []
    for item in sources_raw:
        if not isinstance(item, dict):
            raise SectorValidationError("source must be an object")
        source_type = item.get("source_type")
        if source_type not in SOURCE_TYPES:
            raise SectorValidationError("source_type is invalid")
        published_at = item.get("published_at")
        if published_at is not None:
            if isinstance(published_at, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at):
                # Preserve source date precision; do not invent a publication time.
                published_at = published_at
            else:
                published_at = iso_seconds(parse_datetime(published_at, "sources.published_at"))
        sources.append({
            "title": _text(item.get("title"), "sources.title", 500),
            "url": _http_url(item.get("url"), "sources.url"),
            "source_name": _text(item.get("source_name"), "sources.source_name", 200),
            "source_type": source_type,
            "published_at": published_at,
        })
    run_id = _text(payload.get("run_id"), "run_id", 200)
    key = _text(payload.get("dedupe_key"), "dedupe_key", 200)
    expected_key = dedupe_key(expected_window or WeeklyWindow(start, end, end.astimezone(JST).date().isoformat()), code)
    if key != expected_key or run_id != key:
        raise SectorValidationError("run_id/dedupe_key does not match week and sector")
    report = {
        "id": stable_report_id(key), "schema_version": SCHEMA_VERSION, "report_type": REPORT_TYPE,
        "sector_code": code, "sector_name": name, "period_start": iso_seconds(start), "period_end": iso_seconds(end),
        "generated_at": iso_seconds(generated), "importance": importance, "direction": direction,
        "summary_bullets": json.dumps(bullets, ensure_ascii=False), "full_report_md": full_report,
        "watchlist_companies": json.dumps(watchlist, ensure_ascii=False),
        "next_week_watchpoints": json.dumps(watchpoints, ensure_ascii=False),
        "missed_candidates": json.dumps(missed, ensure_ascii=False), "sources": json.dumps(sources, ensure_ascii=False),
        "run_id": run_id, "dedupe_key": key,
    }
    return ValidatedSectorReport(report=report)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_sector_reports (
 id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, report_type TEXT NOT NULL, sector_code INTEGER NOT NULL,
 sector_name TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, generated_at TEXT NOT NULL,
 importance TEXT NOT NULL, direction TEXT NOT NULL, summary_bullets TEXT NOT NULL, full_report_md TEXT NOT NULL,
 watchlist_companies TEXT NOT NULL, next_week_watchpoints TEXT NOT NULL, missed_candidates TEXT NOT NULL,
 sources TEXT NOT NULL, run_id TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_sector_report_runs (
 run_id TEXT PRIMARY KEY, report_type TEXT NOT NULL, sector_code INTEGER NOT NULL, sector_name TEXT NOT NULL,
 period_start TEXT NOT NULL, period_end TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0, last_error_type TEXT, last_error_message TEXT, started_at TEXT,
 completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sector_reports_generated ON canonical_sector_reports(generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_sector_runs_status ON canonical_sector_report_runs(status, period_end DESC);
"""


def connect_sector_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SQLITE_SCHEMA)
    return conn


def ensure_week_runs(conn: sqlite3.Connection, window: WeeklyWindow) -> None:
    now = iso_seconds(now_jst())
    with conn:
        for code in range(1, 34):
            key = dedupe_key(window, code)
            conn.execute(
                "INSERT OR IGNORE INTO canonical_sector_report_runs "
                "(run_id,report_type,sector_code,sector_name,period_start,period_end,dedupe_key,status,attempt_count,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, REPORT_TYPE, code, sector_name(code), iso_seconds(window.period_start), iso_seconds(window.period_end), key, "pending", 0, now, now),
            )


def mark_run(conn: sqlite3.Connection, key: str, status: str, *, error: Exception | None = None, increment: bool = False) -> None:
    if status not in {"pending", "running", "success", "failed", "retry_pending"}:
        raise ValueError("invalid sector run status")
    now = iso_seconds(now_jst())
    started = now if status == "running" else None
    completed = now if status in {"success", "failed"} else None
    error_type = type(error).__name__ if error else None
    error_message = str(error)[:2000] if error else None
    with conn:
        conn.execute(
            "UPDATE canonical_sector_report_runs SET status=?, attempt_count=attempt_count+?, last_error_type=?, "
            "last_error_message=?, started_at=COALESCE(?,started_at), completed_at=?, updated_at=? WHERE run_id=?",
            (status, 1 if increment else 0, error_type, error_message, started, completed, now, key),
        )


def upsert_report(conn: sqlite3.Connection, validated: ValidatedSectorReport) -> None:
    now = iso_seconds(now_jst())
    report = validated.report
    columns = list(report) + ["created_at", "updated_at"]
    values = [report[name] for name in report] + [now, now]
    updates = ",".join(
        f"{name}=excluded.{name}" for name in report
        if name not in {"id", "dedupe_key", "created_at"}
    ) + ",updated_at=excluded.updated_at"
    with conn:
        conn.execute(
            f"INSERT INTO canonical_sector_reports ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(dedupe_key) DO UPDATE SET {updates}", values,
        )


def rows_for_sync(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in {"canonical_sector_reports", "canonical_sector_report_runs"}:
        raise ValueError("unsupported sector table")
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    if table == "canonical_sector_reports":
        for row in rows:
            for field in ("summary_bullets", "watchlist_companies", "next_week_watchpoints", "missed_candidates", "sources"):
                row[field] = json.loads(row[field])
    return rows
