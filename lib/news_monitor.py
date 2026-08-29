"""Canonical qualitative company-news storage and company_news_v1 validation.

``direction`` describes qualitative impact on company earnings, never a share-price
forecast.  The transport (local inbox today) is deliberately outside this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMA_VERSION = "company_news_v1"
CATEGORIES = frozenset("orders backlog pricing demand volume product_mix margin raw_material labor_cost energy_cost logistics capacity utilization capex new_factory new_product customer supplier competitor industry regulation m_and_a divestiture reorganization large_project inventory fx shareholder_return guidance management_comment other".split())
DIRECTIONS = frozenset("positive negative mixed neutral unknown".split())
IMPORTANCE = frozenset("high medium low".split())
EARNINGS_RELEVANCE = frozenset("direct likely general context unknown".split())
TEMPORAL_SCOPE = frozenset("current quarter fiscal_year multi_year ongoing historical unknown".split())
TEMPORAL_STATUS = frozenset("current ongoing historical expired unknown".split())
SOURCE_TYPES = frozenset("news ir company government exchange research other".split())
_TICKER_RE = re.compile(r"^(?:\d{4}|\d{3}[A-Z])$")


class NewsValidationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: Any, field: str, max_length: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NewsValidationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise NewsValidationError(f"{field} exceeds {max_length} characters")
    return value


def _optional_text(value: Any, field: str, max_length: int = 8000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NewsValidationError(f"{field} must be a string or null")
    value = value.strip()
    if len(value) > max_length:
        raise NewsValidationError(f"{field} exceeds {max_length} characters")
    return value or None


def normalize_ticker(value: Any) -> str:
    ticker = _required_text(value, "ticker", 5).upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise NewsValidationError("ticker must be 4 digits or 3 digits plus A-Z")
    return ticker


def parse_datetime(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    text = _required_text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NewsValidationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise NewsValidationError(f"{field} must include a timezone")
    return parsed.isoformat()


def canonicalize_url(value: Any) -> str:
    text = _required_text(value, "source_url", 2048)
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise NewsValidationError("source_url must be an absolute http/https URL")
    host = parts.hostname.lower()
    port = parts.port
    netloc = host if not port or (parts.scheme.lower(), port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")))
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def normalize_headline(value: Any) -> str:
    return " ".join(_required_text(value, "headline", 500).casefold().split())


def make_dedupe_key(ticker: str, source_url: str, published_at: str, headline: str) -> str:
    material = "\n".join((ticker, canonicalize_url(source_url), published_at, normalize_headline(headline)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _enum(value: Any, field: str, choices: frozenset[str], default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    result = _required_text(value, field, 64)
    if result not in choices:
        raise NewsValidationError(f"{field} must be one of: {', '.join(sorted(choices))}")
    return result


def _temporal_status(item: dict[str, Any], checked_at: str) -> str:
    supplied = item.get("temporal_status")
    if supplied is not None:
        return _enum(supplied, "temporal_status", TEMPORAL_STATUS)
    scope = item.get("temporal_scope", "unknown")
    valid_until = parse_datetime(item.get("valid_until"), "valid_until", required=False)
    if valid_until and datetime.fromisoformat(valid_until) < datetime.fromisoformat(checked_at):
        return "expired"
    if scope == "historical":
        return "historical"
    if scope == "ongoing":
        return "ongoing"
    if scope in {"current", "quarter", "fiscal_year", "multi_year"}:
        return "current"
    return "unknown"


@dataclass(frozen=True)
class ValidatedRun:
    scan: dict[str, Any]
    events: list[dict[str, Any]]


def validate_payload(payload: Any) -> ValidatedRun:
    if not isinstance(payload, dict):
        raise NewsValidationError("payload must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise NewsValidationError(f"schema_version must be {SCHEMA_VERSION}")
    run_id = _required_text(payload.get("run_id"), "run_id", 200)
    ticker = normalize_ticker(payload.get("ticker"))
    checked_at = parse_datetime(payload.get("checked_at"), "checked_at")
    collector_type = _required_text(payload.get("collector_type"), "collector_type", 100)
    items = payload.get("items")
    if not isinstance(items, list):
        raise NewsValidationError("items must be an array")
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise NewsValidationError(f"items[{index}] must be an object")
        forbidden = {"body", "content", "article_body", "article_text", "full_text", "html", "raw_html"}
        present_forbidden = forbidden.intersection(key.casefold() for key in raw)
        if present_forbidden:
            raise NewsValidationError(f"items[{index}] contains prohibited full-article field(s): {', '.join(sorted(present_forbidden))}")
        if len(json.dumps(raw, ensure_ascii=False)) > 20_000:
            raise NewsValidationError(f"items[{index}] exceeds the structured payload size limit")
        headline = _required_text(raw.get("headline"), f"items[{index}].headline", 500)
        source_url = canonicalize_url(raw.get("source_url"))
        published_at = parse_datetime(raw.get("published_at"), f"items[{index}].published_at")
        valid_from = parse_datetime(raw.get("valid_from"), f"items[{index}].valid_from", required=False)
        valid_until = parse_datetime(raw.get("valid_until"), f"items[{index}].valid_until", required=False)
        if valid_from and valid_until and valid_until < valid_from:
            raise NewsValidationError(f"items[{index}].valid_until precedes valid_from")
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise NewsValidationError(f"items[{index}].tags must be an array of non-empty strings")
        tags = list(dict.fromkeys(tag.strip()[:100] for tag in tags))[:50]
        dedupe_key = make_dedupe_key(ticker, source_url, published_at, headline)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        source_type = _enum(raw.get("source_type"), "source_type", SOURCE_TYPES, "news")
        temporal_scope = _enum(raw.get("temporal_scope"), "temporal_scope", TEMPORAL_SCOPE, "unknown")
        source_hash = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        events.append({
            "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"company-news:{dedupe_key}")), "schema_version": SCHEMA_VERSION,
            "ticker": ticker, "headline": headline, "source_type": source_type,
            "source_name": _required_text(raw.get("source_name"), "source_name", 200), "source_url": source_url,
            "published_at": published_at, "first_seen_at": checked_at, "last_seen_at": checked_at, "checked_at": checked_at,
            "category": _enum(raw.get("category"), "category", CATEGORIES), "subcategory": _optional_text(raw.get("subcategory"), "subcategory", 200),
            "direction": _enum(raw.get("direction"), "direction", DIRECTIONS), "importance": _enum(raw.get("importance"), "importance", IMPORTANCE),
            "earnings_relevance": _enum(raw.get("earnings_relevance"), "earnings_relevance", EARNINGS_RELEVANCE),
            "summary": _required_text(raw.get("summary"), "summary", 4000), "why_it_matters": _required_text(raw.get("why_it_matters"), "why_it_matters", 4000),
            "evidence_excerpt": _optional_text(raw.get("evidence_excerpt"), "evidence_excerpt", 1000), "temporal_scope": temporal_scope,
            "valid_from": valid_from, "valid_until": valid_until, "temporal_status": _temporal_status(raw, checked_at),
            "tags": json.dumps(tags, ensure_ascii=False), "task_run_id": run_id, "collector_type": collector_type,
            "collector_version": _optional_text(payload.get("collector_version"), "collector_version", 100),
            "analysis_version": _optional_text(payload.get("analysis_version"), "analysis_version", 100),
            "dedupe_key": dedupe_key, "source_hash": source_hash, "raw_payload": json.dumps(raw, ensure_ascii=False, sort_keys=True),
        })
    scan = {"scan_run_id": run_id, "ticker": ticker, "checked_at": checked_at, "collector_type": collector_type,
            "task_id": _optional_text(payload.get("task_id"), "task_id", 200), "status": "completed", "items_found": len(events),
            "sources_checked_count": payload.get("sources_checked_count"), "error_code": None, "error_message": None}
    if scan["sources_checked_count"] is not None and (not isinstance(scan["sources_checked_count"], int) or scan["sources_checked_count"] < 0):
        raise NewsValidationError("sources_checked_count must be a non-negative integer or null")
    return ValidatedRun(scan=scan, events=events)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_news_events (
 event_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, ticker TEXT NOT NULL, headline TEXT NOT NULL,
 source_type TEXT NOT NULL, source_name TEXT NOT NULL, source_url TEXT NOT NULL, published_at TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, checked_at TEXT NOT NULL, category TEXT NOT NULL,
 subcategory TEXT, direction TEXT NOT NULL, importance TEXT NOT NULL, earnings_relevance TEXT NOT NULL,
 summary TEXT NOT NULL, why_it_matters TEXT NOT NULL, evidence_excerpt TEXT, temporal_scope TEXT NOT NULL,
 valid_from TEXT, valid_until TEXT, temporal_status TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', task_run_id TEXT NOT NULL,
 collector_type TEXT NOT NULL, collector_version TEXT, analysis_version TEXT, dedupe_key TEXT NOT NULL UNIQUE,
 source_hash TEXT NOT NULL, raw_payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_news_scan_runs (
 scan_run_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, checked_at TEXT NOT NULL, collector_type TEXT NOT NULL,
 task_id TEXT, status TEXT NOT NULL, items_found INTEGER NOT NULL DEFAULT 0, sources_checked_count INTEGER,
 error_code TEXT, error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_news_events_ticker_published ON canonical_news_events(ticker, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_events_created ON canonical_news_events(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_events_importance ON canonical_news_events(importance);
CREATE INDEX IF NOT EXISTS ix_news_events_direction ON canonical_news_events(direction);
CREATE INDEX IF NOT EXISTS ix_news_events_category ON canonical_news_events(category);
CREATE INDEX IF NOT EXISTS ix_news_scan_ticker_checked ON canonical_news_scan_runs(ticker, checked_at DESC);
"""


def connect_news_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SQLITE_SCHEMA)
    return conn


def upsert_run(conn: sqlite3.Connection, run: ValidatedRun) -> tuple[int, int]:
    now = _now()
    before = conn.total_changes
    with conn:
        for event in run.events:
            columns = list(event) + ["created_at", "updated_at"]
            values = [event[key] for key in event] + [now, now]
            updates = "last_seen_at=excluded.last_seen_at, checked_at=excluded.checked_at, source_hash=excluded.source_hash, raw_payload=excluded.raw_payload, updated_at=excluded.updated_at"
            conn.execute(f"INSERT INTO canonical_news_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(dedupe_key) DO UPDATE SET {updates}", values)
        scan_columns = list(run.scan) + ["created_at", "updated_at"]
        conn.execute(f"INSERT INTO canonical_news_scan_runs ({','.join(scan_columns)}) VALUES ({','.join('?' for _ in scan_columns)}) ON CONFLICT(scan_run_id) DO UPDATE SET checked_at=excluded.checked_at,status=excluded.status,items_found=excluded.items_found,sources_checked_count=excluded.sources_checked_count,error_code=excluded.error_code,error_message=excluded.error_message,updated_at=excluded.updated_at", [run.scan[k] for k in run.scan] + [now, now])
    return len(run.events), conn.total_changes - before


def record_failed_run(conn: sqlite3.Connection, payload: Any, error: Exception) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("run_id"), str):
        return
    try:
        ticker = normalize_ticker(payload.get("ticker"))
        checked_at = parse_datetime(payload.get("checked_at"), "checked_at")
        collector = _required_text(payload.get("collector_type", "unknown"), "collector_type", 100)
    except NewsValidationError:
        return
    now = _now()
    with conn:
        conn.execute("INSERT INTO canonical_news_scan_runs(scan_run_id,ticker,checked_at,collector_type,status,items_found,error_code,error_message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scan_run_id) DO UPDATE SET status='failed',error_code=excluded.error_code,error_message=excluded.error_message,updated_at=excluded.updated_at", (payload["run_id"], ticker, checked_at, collector, "failed", 0, "validation_error", str(error)[:1000], now, now))


def rows_for_sync(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in {"canonical_news_events", "canonical_news_scan_runs"}:
        raise ValueError("unsupported news table")
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    for row in rows:
        if table == "canonical_news_events":
            row["tags"] = json.loads(row["tags"])
            row["raw_payload"] = json.loads(row["raw_payload"])
    return rows
