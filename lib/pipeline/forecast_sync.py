"""Normalize TDNET/J-Quants forecasts for realtime analysis and J-Quants sync.

Realtime TDNET forecasts are deliberately not written to ``canonical_financials``.
``expand_forecast_rows`` remains for the J-Quants Nightly canonical path only.
"""
from __future__ import annotations

import calendar
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from src.common_ticker import normalize_ticker

from .canonical_writer import _make_financials_row_key
from .source_priority import get_priority

logger = logging.getLogger("pipeline.forecast_sync")
JST = timezone(timedelta(hours=9))

_PERIOD_RE = re.compile(r"(?P<year>\d{4})年\s*(?P<month>\d{1,2})月期")
_METRIC_MAP = {
    "revised_sales": "sales",
    "revised_op": "operating_profit",
    "revised_ordinary": "ordinary_profit",
    "revised_net_income": "net_income",
}
_SOURCE_TIE = {"tdnet_forecast": 2, "jquants_forecast_fy": 1, "jquants_nxf": 1}


@dataclass(frozen=True)
class ForecastDTO:
    ticker: str
    forecast_period_end: str
    metric: str
    value: float
    disclosure_datetime: str
    filing_id: str
    source: str
    correction_flag: bool
    forecast_horizon: str
    accounting_standard: str
    document_type: str


def _as_utc_key(value: str) -> str:
    """Return a fixed-width UTC timestamp suitable for lexical ordering."""
    text = str(value or "").strip()
    if not text:
        return "0000-00-00T00:00:00.000000Z"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return f"{text}T00:00:00.000000Z"
        return "0000-00-00T00:00:00.000000Z"


def make_forecast_recency_key(dto: ForecastDTO, *, updated_at: str) -> str:
    """Newest disclosure wins; correction and source only break exact ties."""
    return (
        f"{_as_utc_key(dto.disclosure_datetime)}_"
        f"{1 if dto.correction_flag else 0}_"
        f"{_SOURCE_TIE.get(dto.source, 0):02d}_"
        f"{_as_utc_key(updated_at)}"
    )


def parse_forecast_period_end(period_label: str) -> str | None:
    match = _PERIOD_RE.search(unicodedata.normalize("NFKC", str(period_label or "")))
    if not match:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    if not 1 <= month <= 12:
        return None
    day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{day:02d}"


def forecast_period_from_actual(actual_period_end: str, quarter: str) -> tuple[str | None, str]:
    """Map FY/4Q guidance to next FY; Q1-Q3 guidance to current FY."""
    try:
        period = datetime.strptime(actual_period_end, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None, "unknown"
    q = str(quarter or "").upper()
    if q in {"FY", "4Q"}:
        year = period.year + 1
        day = min(period.day, calendar.monthrange(year, period.month)[1])
        return f"{year:04d}-{period.month:02d}-{day:02d}", "next_fy"
    if q in {"1Q", "2Q", "3Q"}:
        return period.isoformat(), "current_fy"
    return None, "unknown"


def _accounting_standard(title: str, stored: str = "") -> str:
    if stored and stored != "UNKNOWN":
        return stored
    upper = str(title or "").upper()
    if "IFRS" in upper:
        return "IFRS"
    if "米国基準" in str(title or ""):
        return "US_GAAP"
    if "日本基準" in str(title or ""):
        return "J_GAAP"
    return "UNKNOWN"


def _effective_disclosure_datetime(
    exact: str = "", disclosure_date: str = "", created_at: str = "", source_url: str = ""
) -> str:
    exact_text = str(exact or "").strip()
    date_text = str(disclosure_date or "").strip()
    created_text = str(created_at or "").strip()
    url_matches = re.findall(r"\d{18,20}", str(source_url or ""))
    url_digits = url_matches[-1][-14:-6] if url_matches else ""
    url_date = (
        f"{url_digits[:4]}-{url_digits[4:6]}-{url_digits[6:8]}"
        if re.fullmatch(r"20\d{6}", url_digits) else ""
    )
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", exact_text):
        date_part = (
            date_text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", date_text)
            else url_date or created_text[:10]
        )
        return f"{date_part}T{exact_text}" if date_part else exact_text
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", date_text):
        date_part = url_date or created_text[:10]
        return f"{date_part}T{date_text}" if date_part else date_text
    return exact_text or date_text or created_text


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _tdnet_url_key(url: str) -> str:
    matches = re.findall(r"\d{18,20}", str(url or ""))
    return matches[-1][-14:] if matches else ""


def load_earnings_forecasts(
    conn: sqlite3.Connection,
    *,
    disclosure_ids: Iterable[str] | None = None,
) -> tuple[list[ForecastDTO], list[dict]]:
    ids = {str(item) for item in (disclosure_ids or []) if str(item)}
    q_rows = [dict(row) for row in conn.execute("SELECT * FROM quarterly_results")]
    if ids:
        q_rows = [row for row in q_rows if str(row.get("source_doc_id") or "") in ids]
    q_by_doc = {
        str(row.get("source_doc_id")): row for row in q_rows if row.get("source_doc_id")
    }
    q_by_url = {str(row.get("source_url") or ""): row for row in q_rows if row.get("source_url")}
    q_by_url_key = {
        _tdnet_url_key(str(row.get("source_url") or "")): row
        for row in q_rows if _tdnet_url_key(str(row.get("source_url") or ""))
    }

    columns = _table_columns(conn, "earnings_summaries")
    select_cols = [
        "id", "ticker", "fiscal_year", "quarter", "title", "disclosure_date",
        "guidance_sales", "guidance_op", "source_url", "created_at",
    ]
    for optional in ("source_doc_id", "disclosure_datetime", "accounting_standard"):
        if optional in columns:
            select_cols.append(optional)
    rows = [dict(row) for row in conn.execute(
        f"SELECT {','.join(select_cols)} FROM earnings_summaries "
        "WHERE guidance_sales IS NOT NULL OR guidance_op IS NOT NULL"
    )]

    candidates: list[ForecastDTO] = []
    quarantine: list[dict] = []
    for row in rows:
        source_doc_id = str(row.get("source_doc_id") or "")
        source_url = str(row.get("source_url") or "")
        q_row = (
            q_by_doc.get(source_doc_id)
            or q_by_url.get(source_url)
            or q_by_url_key.get(_tdnet_url_key(source_url))
        )
        if ids and q_row is None:
            continue
        if q_row is None:
            quarantine.append({
                "ticker": row.get("ticker"), "disclosure_id": source_doc_id,
                "reason": "earnings_actual_period_not_found", "row_id": row.get("id"),
            })
            continue
        filing_id = str(q_row.get("source_doc_id") or source_doc_id)
        period, horizon = forecast_period_from_actual(
            str(q_row.get("fiscal_year_end") or ""), str(q_row.get("quarter") or row.get("quarter") or "")
        )
        if not period:
            quarantine.append({
                "ticker": row.get("ticker"), "disclosure_id": filing_id,
                "reason": "earnings_forecast_period_unresolved", "row_id": row.get("id"),
            })
            continue
        disclosure_dt = _effective_disclosure_datetime(
            str(row.get("disclosure_datetime") or ""),
            str(row.get("disclosure_date") or ""),
            str(row.get("created_at") or ""),
            str(row.get("source_url") or ""),
        )
        title = str(row.get("title") or "")
        for field, metric in (("guidance_sales", "sales"), ("guidance_op", "operating_profit")):
            value = row.get(field)
            if value is None:
                continue
            candidates.append(ForecastDTO(
                ticker=normalize_ticker(str(row.get("ticker") or "")),
                forecast_period_end=period,
                metric=metric,
                value=float(value) / 1_000_000,
                disclosure_datetime=disclosure_dt,
                filing_id=filing_id,
                source="tdnet_forecast",
                correction_flag="訂正" in title,
                forecast_horizon=horizon,
                accounting_standard=_accounting_standard(title, str(row.get("accounting_standard") or "")),
                document_type="earnings_guidance",
            ))
    return candidates, quarantine


def load_revision_forecasts(
    conn: sqlite3.Connection,
    *,
    disclosure_ids: Iterable[str] | None = None,
) -> tuple[list[ForecastDTO], list[dict]]:
    ids = {str(item) for item in (disclosure_ids or []) if str(item)}
    sql = "SELECT * FROM events WHERE event_type='forecast_revision'"
    params: list[str] = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql += f" AND source_doc_id IN ({placeholders})"
        params.extend(sorted(ids))
    rows = [dict(row) for row in conn.execute(sql, params)]
    candidates: list[ForecastDTO] = []
    quarantine: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row.get("extracted_payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        period_label = unicodedata.normalize("NFKC", str(payload.get("period_label") or ""))
        if payload.get("is_difference_disclosure"):
            continue
        if re.search(r"第?[123]四半期|中間期", period_label) and "通期" not in period_label:
            quarantine.append({
                "ticker": row.get("ticker"), "disclosure_id": row.get("source_doc_id"),
                "reason": "revision_not_full_year", "event_id": row.get("event_id"),
            })
            continue
        period = parse_forecast_period_end(period_label)
        if not period:
            quarantine.append({
                "ticker": row.get("ticker"), "disclosure_id": row.get("source_doc_id"),
                "reason": "revision_forecast_period_unresolved", "event_id": row.get("event_id"),
            })
            continue
        title = str(row.get("title") or "")
        for payload_name, canonical_metric in _METRIC_MAP.items():
            value = payload.get(payload_name)
            if value is None:
                continue
            candidates.append(ForecastDTO(
                ticker=normalize_ticker(str(row.get("ticker") or "")),
                forecast_period_end=period,
                metric=canonical_metric,
                value=float(value),
                disclosure_datetime=_effective_disclosure_datetime(
                    str(row.get("disclosure_datetime") or ""), "", str(row.get("created_at") or ""),
                    str(row.get("doc_url") or ""),
                ),
                filing_id=str(row.get("source_doc_id") or row.get("event_id") or ""),
                source="tdnet_forecast",
                correction_flag="訂正" in title,
                forecast_horizon="specified_period",
                accounting_standard=_accounting_standard(title),
                document_type="forecast_revision",
            ))
    return candidates, quarantine


def select_latest_forecasts(candidates: Iterable[ForecastDTO]) -> list[ForecastDTO]:
    winners: dict[tuple[str, str, str], ForecastDTO] = {}
    for dto in candidates:
        key = (dto.ticker, dto.forecast_period_end, dto.metric)
        current = winners.get(key)
        score = (
            _as_utc_key(dto.disclosure_datetime),
            1 if dto.correction_flag else 0,
            _SOURCE_TIE.get(dto.source, 0),
            dto.filing_id,
        )
        if current is None:
            winners[key] = dto
            continue
        current_score = (
            _as_utc_key(current.disclosure_datetime),
            1 if current.correction_flag else 0,
            _SOURCE_TIE.get(current.source, 0),
            current.filing_id,
        )
        if score > current_score:
            winners[key] = dto
    return sorted(winners.values(), key=lambda d: (d.ticker, d.forecast_period_end, d.metric))


def expand_forecast_rows(candidates: Iterable[ForecastDTO]) -> list[dict]:
    now_iso = datetime.now(JST).isoformat()
    rows: list[dict] = []
    for dto in candidates:
        normalized_disclosure = _as_utc_key(dto.disclosure_datetime)
        rows.append({
            "ticker": dto.ticker,
            "period": dto.forecast_period_end,
            "quarter": "FY",
            "metric": dto.metric,
            "value": dto.value,
            "unit": "millions_jpy",
            "source": dto.source,
            "source_priority": get_priority(dto.source),
            "filing_id": dto.filing_id,
            "source_row_key": _make_financials_row_key(
                dto.ticker, dto.forecast_period_end, "FY", dto.metric, dto.source, dto.filing_id
            ),
            "disclosure_datetime": (
                None if normalized_disclosure.startswith("0000-") else normalized_disclosure
            ),
            "correction_flag": dto.correction_flag,
            "recency_key": make_forecast_recency_key(dto, updated_at=now_iso),
            "updated_at": now_iso,
        })
    return rows
