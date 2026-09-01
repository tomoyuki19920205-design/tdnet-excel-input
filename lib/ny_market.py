"""Validation and canonical SQLite storage for daily NY market reports."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = "ny_market_daily_v1"
REPORT_TYPE = "ny_market_daily"
MARKET_STATUSES = frozenset({"open", "holiday_or_weekend"})
JSON_FIELDS = (
    "summary_bullets", "index_moves", "sector_moves", "notable_gainers",
    "notable_losers", "top_gainers_20", "earnings", "after_hours_earnings",
    "major_news", "commodities", "sources",
)
PLATFORM_CITATION_RE = re.compile(r"(?:cite|turn\d+(?:search|view|fetch)\d+|\u3010\d+\u2020[^\u3011]+\u3011)", re.IGNORECASE)


class NYMarketValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedNYMarketReport:
    report: dict[str, Any]
    run: dict[str, Any]


def _text(value: Any, field: str, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise NYMarketValidationError(f"{field} must be a string")
    result = value.strip()
    if not minimum <= len(result) <= maximum:
        raise NYMarketValidationError(f"{field} length must be {minimum}..{maximum}")
    return result


def _date(value: Any, field: str) -> str:
    text = _text(value, field, 10, 10)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise NYMarketValidationError(f"{field} must be YYYY-MM-DD") from exc


def _timestamp(value: Any, field: str) -> str:
    text = _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NYMarketValidationError(f"{field} must be ISO8601") from exc
    if parsed.tzinfo is None:
        raise NYMarketValidationError(f"{field} must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _http_url(value: Any, field: str) -> str:
    url = _text(value, field, 2048)
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise NYMarketValidationError(f"{field} must be an absolute http/https URL")
    return url


def _list(value: Any, field: str, exact: int | None = None, maximum: int = 200) -> list[Any]:
    if not isinstance(value, list):
        raise NYMarketValidationError(f"{field} must be an array")
    if exact is not None and len(value) != exact:
        raise NYMarketValidationError(f"{field} must contain exactly {exact} items")
    if len(value) > maximum:
        raise NYMarketValidationError(f"{field} must contain at most {maximum} items")
    return value


def stable_report_id(stable_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"company-viewer:{stable_key}"))


def validate_payload(payload: Any) -> ValidatedNYMarketReport:
    if not isinstance(payload, dict):
        raise NYMarketValidationError("payload must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("report_type") != REPORT_TYPE:
        raise NYMarketValidationError("unsupported NY market report schema")

    report_date = _date(payload.get("report_date_jst"), "report_date_jst")
    session_date = _date(payload.get("market_session_date"), "market_session_date")
    if session_date > report_date:
        raise NYMarketValidationError("market_session_date cannot follow report_date_jst")
    stable_key = _text(payload.get("stable_key"), "stable_key", 64)
    expected_key = f"ny_market_daily:{report_date}"
    if stable_key != expected_key:
        raise NYMarketValidationError(f"stable_key must be {expected_key}")
    generated_at = _timestamp(payload.get("generated_at"), "generated_at")
    market_status = payload.get("market_status")
    if market_status not in MARKET_STATUSES:
        raise NYMarketValidationError("market_status must be open or holiday_or_weekend")

    headline = _text(payload.get("headline"), "headline", 500)
    bullets = _list(payload.get("summary_bullets"), "summary_bullets")
    if not 5 <= len(bullets) <= 8:
        raise NYMarketValidationError("summary_bullets must contain 5..8 items")
    bullets = [_text(item, "summary_bullets[]", 500) for item in bullets]

    index_moves = payload.get("index_moves")
    if not isinstance(index_moves, dict):
        raise NYMarketValidationError("index_moves must be an object")
    normalized_keys = {re.sub(r"[^a-z0-9]", "", str(key).lower()) for key in index_moves}
    for aliases in ({"sox"}, {"sp500", "sandp500"}, {"dow", "dowjones"}, {"russell2000"}):
        if not normalized_keys.intersection(aliases):
            raise NYMarketValidationError("index_moves must include SOX, S&P500, Dow, and Russell 2000")

    sector_moves = _list(payload.get("sector_moves"), "sector_moves", exact=11)
    notable_gainers = _list(payload.get("notable_gainers"), "notable_gainers", exact=10)
    notable_losers = _list(payload.get("notable_losers"), "notable_losers", exact=10)
    top_gainers = _list(payload.get("top_gainers_20"), "top_gainers_20", exact=20)
    for index, item in enumerate(top_gainers):
        if not isinstance(item, dict):
            raise NYMarketValidationError(f"top_gainers_20[{index}] must be an object")
        for key in ("ticker", "company_name", "change_pct", "market_cap", "reason"):
            if key not in item:
                raise NYMarketValidationError(f"top_gainers_20[{index}].{key} is required")
        _text(item["ticker"], f"top_gainers_20[{index}].ticker", 32)
        _text(item["company_name"], f"top_gainers_20[{index}].company_name", 300)
        if not isinstance(item["change_pct"], (int, float)):
            raise NYMarketValidationError(f"top_gainers_20[{index}].change_pct must be numeric")
        if item["market_cap"] is not None and not isinstance(item["market_cap"], (int, float, str)):
            raise NYMarketValidationError(f"top_gainers_20[{index}].market_cap is invalid")
        _text(item["reason"], f"top_gainers_20[{index}].reason", 2000)

    earnings = _list(payload.get("earnings"), "earnings", maximum=100)
    after_hours = _list(payload.get("after_hours_earnings"), "after_hours_earnings", maximum=100)
    major_news = _list(payload.get("major_news"), "major_news", exact=10)
    commodities = _list(payload.get("commodities"), "commodities", maximum=100)
    report_markdown = _text(payload.get("report_markdown"), "report_markdown", 300_000, 500)

    raw_sources = _list(payload.get("sources"), "sources", maximum=300)
    if not raw_sources:
        raise NYMarketValidationError("sources must contain at least one item")
    sources: list[dict[str, Any]] = []
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise NYMarketValidationError(f"sources[{index}] must be an object")
        published_at = item.get("published_at")
        if published_at is not None:
            if isinstance(published_at, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at):
                published_at = _date(published_at, f"sources[{index}].published_at")
            else:
                published_at = _timestamp(published_at, f"sources[{index}].published_at")
        sources.append({
            "title": _text(item.get("title"), f"sources[{index}].title", 500),
            "publisher": _text(item.get("publisher"), f"sources[{index}].publisher", 200),
            "url": _http_url(item.get("url"), f"sources[{index}].url"),
            "published_at": published_at,
        })

    clean_payload = {**payload, "sources": sources, "report_markdown": report_markdown}
    if PLATFORM_CITATION_RE.search(json.dumps(clean_payload, ensure_ascii=False)):
        raise NYMarketValidationError("Company Viewer payload contains a platform-specific citation token")

    report: dict[str, Any] = {
        "id": stable_report_id(stable_key),
        "stable_key": stable_key,
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "report_date_jst": report_date,
        "market_session_date": session_date,
        "market_status": market_status,
        "generated_at": generated_at,
        "headline": headline,
        "summary_bullets": json.dumps(bullets, ensure_ascii=False),
        "index_moves": json.dumps(index_moves, ensure_ascii=False),
        "sector_moves": json.dumps(sector_moves, ensure_ascii=False),
        "notable_gainers": json.dumps(notable_gainers, ensure_ascii=False),
        "notable_losers": json.dumps(notable_losers, ensure_ascii=False),
        "top_gainers_20": json.dumps(top_gainers, ensure_ascii=False),
        "earnings": json.dumps(earnings, ensure_ascii=False),
        "after_hours_earnings": json.dumps(after_hours, ensure_ascii=False),
        "major_news": json.dumps(major_news, ensure_ascii=False),
        "commodities": json.dumps(commodities, ensure_ascii=False),
        "report_markdown": report_markdown,
        "sources": json.dumps(sources, ensure_ascii=False),
    }
    run = {
        "run_id": stable_report_id(f"run:{stable_key}"),
        "stable_key": stable_key,
        "report_date_jst": report_date,
    }
    return ValidatedNYMarketReport(report=report, run=run)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_ny_market_reports (
 id TEXT PRIMARY KEY, stable_key TEXT NOT NULL UNIQUE, schema_version TEXT NOT NULL, report_type TEXT NOT NULL,
 report_date_jst TEXT NOT NULL, market_session_date TEXT NOT NULL, market_status TEXT NOT NULL,
 generated_at TEXT NOT NULL, headline TEXT NOT NULL, summary_bullets TEXT NOT NULL, index_moves TEXT NOT NULL,
 sector_moves TEXT NOT NULL, notable_gainers TEXT NOT NULL, notable_losers TEXT NOT NULL,
 top_gainers_20 TEXT NOT NULL, earnings TEXT NOT NULL, after_hours_earnings TEXT NOT NULL,
 major_news TEXT NOT NULL, commodities TEXT NOT NULL, report_markdown TEXT NOT NULL, sources TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_ny_market_report_runs (
 run_id TEXT PRIMARY KEY, stable_key TEXT NOT NULL UNIQUE, report_date_jst TEXT NOT NULL, status TEXT NOT NULL,
 attempt INTEGER NOT NULL DEFAULT 0, started_at TEXT, completed_at TEXT, error_type TEXT, error_message TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ny_market_reports_generated ON canonical_ny_market_reports(generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_ny_market_runs_status ON canonical_ny_market_report_runs(status, report_date_jst DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SQLITE_SCHEMA)
    return conn


def mark_run(
    conn: sqlite3.Connection,
    run: dict[str, Any],
    status: str,
    *,
    error: Exception | None = None,
    increment: bool = False,
) -> None:
    if status not in {"pending", "running", "success", "failed", "retry_pending"}:
        raise ValueError("invalid NY market run status")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO canonical_ny_market_report_runs "
            "(run_id,stable_key,report_date_jst,status,attempt,started_at,completed_at,error_type,error_message,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(stable_key) DO UPDATE SET "
            "status=excluded.status,attempt=canonical_ny_market_report_runs.attempt+?,"
            "started_at=COALESCE(excluded.started_at,canonical_ny_market_report_runs.started_at),"
            "completed_at=excluded.completed_at,error_type=excluded.error_type,error_message=excluded.error_message,updated_at=excluded.updated_at",
            (
                run["run_id"], run["stable_key"], run["report_date_jst"], status, 1 if increment else 0,
                now if status == "running" else None, now if status in {"success", "failed"} else None,
                type(error).__name__ if error else None, str(error)[:2000] if error else None, now, now,
                1 if increment else 0,
            ),
        )


def upsert_report(conn: sqlite3.Connection, validated: ValidatedNYMarketReport) -> None:
    now = _now()
    report = validated.report
    columns = list(report) + ["created_at", "updated_at"]
    updates = ",".join(
        f"{name}=excluded.{name}" for name in report if name not in {"id", "stable_key"}
    ) + ",updated_at=excluded.updated_at"
    with conn:
        conn.execute(
            f"INSERT INTO canonical_ny_market_reports ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(stable_key) DO UPDATE SET {updates} "
            "WHERE excluded.generated_at >= canonical_ny_market_reports.generated_at",
            [report[name] for name in report] + [now, now],
        )


def rows_for_sync(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in {"canonical_ny_market_reports", "canonical_ny_market_report_runs"}:
        raise ValueError("unsupported NY market table")
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    if table == "canonical_ny_market_reports":
        for row in rows:
            for field in JSON_FIELDS:
                row[field] = json.loads(row[field])
    return rows
