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
from datetime import date, datetime, timezone
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
PUBLICATION_PRECISIONS = frozenset({"datetime", "date", "unknown"})
EFFECTIVE_DATE_PRECISIONS = frozenset({"datetime", "date", "unknown"})
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


def parse_date(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    text = _required_text(value, field, 10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise NewsValidationError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise NewsValidationError(f"{field} must be canonical YYYY-MM-DD")
    return text


def normalize_publication_fields(item: Any, field: str = "item", *, allow_legacy: bool = True) -> dict[str, str | None]:
    """Validate publication precision without inventing a time or timezone.

    Legacy payloads containing only an exact ``published_at`` remain readable.
    New payloads must provide both nullable value fields and an explicit
    ``published_precision``.
    """
    if not isinstance(item, dict):
        raise NewsValidationError(f"{field} must be an object")
    explicit = any(key in item for key in ("published_date", "published_precision"))
    if not explicit:
        if not allow_legacy:
            raise NewsValidationError(f"{field} missing publication precision fields")
        published_at = parse_datetime(item.get("published_at"), f"{field}.published_at")
        return {"published_at": published_at, "published_date": None,
                "published_precision": "datetime"}

    if "published_date" not in item or "published_precision" not in item or "published_at" not in item:
        raise NewsValidationError(f"{field} must include published_at, published_date, and published_precision")
    precision = _enum(item.get("published_precision"), f"{field}.published_precision", PUBLICATION_PRECISIONS)
    raw_at = item.get("published_at")
    published_date = parse_date(item.get("published_date"), f"{field}.published_date", required=False)
    if precision == "datetime":
        published_at = parse_datetime(raw_at, f"{field}.published_at")
    elif precision == "date":
        if raw_at is not None or published_date is None:
            raise NewsValidationError(f"{field} date precision requires null published_at and a published_date")
        published_at = None
    else:
        if raw_at is not None or published_date is not None:
            raise NewsValidationError(f"{field} unknown precision requires null publication values")
        published_at = None
    return {"published_at": published_at, "published_date": published_date,
            "published_precision": precision}


def publication_calendar_date(item: Any, field: str = "item") -> str | None:
    """Return a comparison date without persisting a fabricated value."""
    publication = normalize_publication_fields(item, field)
    if publication["published_date"] is not None:
        return publication["published_date"]
    if publication["published_at"] is not None:
        return publication["published_at"][:10]
    return None


def publication_display_value(item: Any, *, unknown: str | None = None) -> str | None:
    """Reader-first display adapter for legacy and precision-aware rows."""
    publication = normalize_publication_fields(item, "event")
    if publication["published_precision"] == "datetime":
        return publication["published_at"]
    if publication["published_precision"] == "date":
        return publication["published_date"]
    return unknown


def normalize_effective_fields(item: Any, boundary: str, *, allow_legacy: bool = True) -> dict[str, str | None]:
    """Validate one effective-date boundary without fabricating an instant.

    ``boundary`` is ``valid_from`` or ``valid_until``. The existing boundary
    column remains the exact-instant field for reader compatibility; its
    companion ``*_date`` and ``*_precision`` fields carry calendar dates and
    explicit absence. Legacy exact/null payloads remain readable.
    """
    if boundary not in {"valid_from", "valid_until"}:
        raise ValueError("unsupported effective-date boundary")
    if not isinstance(item, dict):
        raise NewsValidationError("item must be an object")
    date_key = f"{boundary}_date"
    precision_key = f"{boundary}_precision"
    explicit = date_key in item or precision_key in item
    if not explicit:
        if not allow_legacy:
            raise NewsValidationError(f"item missing {boundary} precision fields")
        raw = item.get(boundary)
        if raw is None:
            return {boundary: None, date_key: None, precision_key: "unknown"}
        return {boundary: parse_datetime(raw, boundary), date_key: None,
                precision_key: "datetime"}
    if boundary not in item or date_key not in item or precision_key not in item:
        raise NewsValidationError(
            f"item must include {boundary}, {date_key}, and {precision_key}")
    precision = _enum(item.get(precision_key), precision_key, EFFECTIVE_DATE_PRECISIONS)
    effective_date = parse_date(item.get(date_key), date_key, required=False)
    raw_at = item.get(boundary)
    if precision == "datetime":
        effective_at = parse_datetime(raw_at, boundary)
    elif precision == "date":
        if raw_at is not None or effective_date is None:
            raise NewsValidationError(
                f"{boundary} date precision requires null {boundary} and a {date_key}")
        effective_at = None
    else:
        if raw_at is not None or effective_date is not None:
            raise NewsValidationError(
                f"{boundary} unknown precision requires null effective-date values")
        effective_at = None
    return {boundary: effective_at, date_key: effective_date, precision_key: precision}


def effective_calendar_date(item: Any, boundary: str) -> str | None:
    """Return a comparison date while preserving the boundary's precision."""
    effective = normalize_effective_fields(item, boundary)
    return effective[f"{boundary}_date"] or (
        effective[boundary][:10] if effective[boundary] is not None else None)


def effective_display_value(item: Any, boundary: str, *, unknown: str | None = None) -> str | None:
    """Reader-first adapter for exact, date-only, and unknown effective dates."""
    effective = normalize_effective_fields(item, boundary)
    if effective[f"{boundary}_precision"] == "datetime":
        return effective[boundary]
    if effective[f"{boundary}_precision"] == "date":
        return effective[f"{boundary}_date"]
    return unknown


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


def make_dedupe_key(ticker: str, source_url: str, published_at: str | None, headline: str,
                    *, published_date: str | None = None,
                    published_precision: str | None = None) -> str:
    # Preserve existing exact-datetime keys byte-for-byte.  Date-only and
    # unknown records use tagged values so null never causes duplicate churn.
    if published_precision in (None, "datetime") and published_at is not None:
        publication = published_at
    elif published_precision == "date" and published_date is not None:
        publication = f"date:{published_date}"
    elif published_precision == "unknown" and published_at is None and published_date is None:
        publication = "unknown"
    else:
        raise NewsValidationError("invalid publication fields for dedupe")
    material = "\n".join((ticker, canonicalize_url(source_url), publication, normalize_headline(headline)))
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
    effective = normalize_effective_fields(item, "valid_until")
    valid_until = effective["valid_until"]
    valid_until_date = effective["valid_until_date"]
    if valid_until and datetime.fromisoformat(valid_until) < datetime.fromisoformat(checked_at):
        return "expired"
    if valid_until_date and date.fromisoformat(valid_until_date) < datetime.fromisoformat(checked_at).date():
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
        publication = normalize_publication_fields(raw, f"items[{index}]")
        published_at = publication["published_at"]
        valid_from_fields = normalize_effective_fields(raw, "valid_from")
        valid_until_fields = normalize_effective_fields(raw, "valid_until")
        valid_from = valid_from_fields["valid_from"]
        valid_until = valid_until_fields["valid_until"]
        from_date = valid_from_fields["valid_from_date"] or (valid_from[:10] if valid_from else None)
        until_date = valid_until_fields["valid_until_date"] or (valid_until[:10] if valid_until else None)
        if from_date and until_date and until_date < from_date:
            raise NewsValidationError(f"items[{index}].valid_until precedes valid_from")
        if valid_from and valid_until and valid_until < valid_from:
            raise NewsValidationError(f"items[{index}].valid_until precedes valid_from")
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise NewsValidationError(f"items[{index}].tags must be an array of non-empty strings")
        tags = list(dict.fromkeys(tag.strip()[:100] for tag in tags))[:50]
        dedupe_key = make_dedupe_key(ticker, source_url, published_at, headline,
                                     published_date=publication["published_date"],
                                     published_precision=publication["published_precision"])
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
            "published_at": published_at, "published_date": publication["published_date"],
            "published_precision": publication["published_precision"],
            "first_seen_at": checked_at, "last_seen_at": checked_at, "checked_at": checked_at,
            "category": _enum(raw.get("category"), "category", CATEGORIES), "subcategory": _optional_text(raw.get("subcategory"), "subcategory", 200),
            "direction": _enum(raw.get("direction"), "direction", DIRECTIONS), "importance": _enum(raw.get("importance"), "importance", IMPORTANCE),
            "earnings_relevance": _enum(raw.get("earnings_relevance"), "earnings_relevance", EARNINGS_RELEVANCE),
            "summary": _required_text(raw.get("summary"), "summary", 4000), "why_it_matters": _required_text(raw.get("why_it_matters"), "why_it_matters", 4000),
            "evidence_excerpt": _optional_text(raw.get("evidence_excerpt"), "evidence_excerpt", 1000), "temporal_scope": temporal_scope,
            **valid_from_fields, **valid_until_fields, "temporal_status": _temporal_status(raw, checked_at),
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
 source_type TEXT NOT NULL, source_name TEXT NOT NULL, source_url TEXT NOT NULL, published_at TEXT,
 published_date TEXT, published_precision TEXT NOT NULL DEFAULT 'datetime'
 CHECK (published_precision IN ('datetime','date','unknown')),
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, checked_at TEXT NOT NULL, category TEXT NOT NULL,
 subcategory TEXT, direction TEXT NOT NULL, importance TEXT NOT NULL, earnings_relevance TEXT NOT NULL,
 summary TEXT NOT NULL, why_it_matters TEXT NOT NULL, evidence_excerpt TEXT, temporal_scope TEXT NOT NULL,
 valid_from TEXT, valid_from_date TEXT, valid_from_precision TEXT NOT NULL DEFAULT 'unknown'
 CHECK (valid_from_precision IN ('datetime','date','unknown')),
 valid_until TEXT, valid_until_date TEXT, valid_until_precision TEXT NOT NULL DEFAULT 'unknown'
 CHECK (valid_until_precision IN ('datetime','date','unknown')),
 temporal_status TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', task_run_id TEXT NOT NULL,
 collector_type TEXT NOT NULL, collector_version TEXT, analysis_version TEXT, dedupe_key TEXT NOT NULL UNIQUE,
 source_hash TEXT NOT NULL, raw_payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK ((published_precision='datetime' AND published_at IS NOT NULL)
     OR (published_precision='date' AND published_at IS NULL AND published_date IS NOT NULL)
     OR (published_precision='unknown' AND published_at IS NULL AND published_date IS NULL)),
 CHECK ((valid_from_precision='datetime' AND valid_from IS NOT NULL)
     OR (valid_from_precision='date' AND valid_from IS NULL AND valid_from_date IS NOT NULL)
     OR (valid_from_precision='unknown' AND valid_from IS NULL AND valid_from_date IS NULL)),
 CHECK ((valid_until_precision='datetime' AND valid_until IS NOT NULL)
     OR (valid_until_precision='date' AND valid_until IS NULL AND valid_until_date IS NOT NULL)
     OR (valid_until_precision='unknown' AND valid_until IS NULL AND valid_until_date IS NULL))
);
CREATE TABLE IF NOT EXISTS canonical_news_scan_runs (
 scan_run_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, checked_at TEXT NOT NULL, collector_type TEXT NOT NULL,
 task_id TEXT, status TEXT NOT NULL, items_found INTEGER NOT NULL DEFAULT 0, sources_checked_count INTEGER,
 error_code TEXT, error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_news_events_ticker_published ON canonical_news_events(ticker, published_date DESC, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_events_valid_from ON canonical_news_events(valid_from_date DESC, valid_from DESC);
CREATE INDEX IF NOT EXISTS ix_news_events_valid_until ON canonical_news_events(valid_until_date DESC, valid_until DESC);
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
