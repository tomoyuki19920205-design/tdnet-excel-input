"""Field-level decision for writes to the existing ``financials`` wide table.

Both TDnet and J-Quants use this policy. The canonical table supplies audited
per-metric evidence; ingestion timestamps are never treated as disclosure dates.
The returned payload contains only existing financials columns.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .source_priority import get_priority

AMOUNTS = ("sales", "gross_profit", "operating_profit")
KEY = ("ticker", "period", "quarter")
FORECAST_SOURCES = {
    "jquants_nxf", "jquants_forecast_fy", "jquants_forecast_next_fy",
    "jquants_forecast", "tdnet_forecast",
}
MANUAL_SOURCES = {"manual", "manual_override", "user_override"}
CORRECTION_TYPES = {"correction", "corrected_tanshin", "tanshin_correction", "amended_filing"}


def _key(row: dict) -> tuple[str, str, str]:
    return tuple(str(row[name]) for name in KEY)


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _is_verified(row: dict) -> bool:
    filing = str(row.get("filing_id") or "")
    return (
        row.get("source") == "official_ir_verified"
        and row.get("source_priority") == 0
        and filing.startswith("official_ir|") and "|https://" in filing and "|p" in filing
        and _when(row.get("disclosure_datetime")) is not None
        and row.get("unit") in ("millions_jpy", "yen_per_share")
        and row.get("value") is not None
    )


def _is_formal_correction(candidate: dict, prior: dict | None) -> bool:
    """Require source, document identity, period relation and later disclosure."""
    if prior is None or candidate.get("source") != "official_ir_verified":
        return False
    if not candidate.get("correction_flag"):
        return False
    if str(candidate.get("document_type") or "").lower() not in CORRECTION_TYPES:
        return False
    filing = str(candidate.get("filing_id") or "")
    if not filing.startswith("official_ir|") or "|https://" not in filing or filing == prior.get("filing_id"):
        return False
    linked = candidate.get("corrects_filing_id")
    if linked and linked != prior.get("filing_id"):
        return False
    current_when, old_when = _when(candidate.get("disclosure_datetime")), _when(prior.get("disclosure_datetime"))
    return current_when is not None and old_when is not None and current_when > old_when


def _verified_winners(rows: list[dict]) -> dict[tuple[str, str, str, str], dict]:
    winners: dict[tuple[str, str, str, str], dict] = {}
    for row in sorted((r for r in rows if _is_verified(r)), key=lambda r: _when(r["disclosure_datetime"])):
        key = (*_key(row), row["metric"])
        prior = winners.get(key)
        if prior is None or _is_formal_correction(row, prior):
            winners[key] = row
    return winners


def _forecast_latest(rows: list[dict]) -> dict[tuple[str, str, str], datetime]:
    latest: dict[tuple[str, str, str], datetime] = {}
    for row in rows:
        if row.get("source") not in FORECAST_SOURCES:
            continue
        when = _when(row.get("disclosure_datetime"))
        if when is None:
            continue
        key = _key(row)
        if key not in latest or when > latest[key]:
            latest[key] = when
    return latest


def decide_wide_row(candidate: dict, existing: dict | None,
                    verified: dict[tuple[str, str, str, str], dict],
                    forecast_latest: dict[tuple[str, str, str], datetime]) -> tuple[dict | None, str]:
    """Return a safe whole-row payload or ``None``; never clear actual values."""
    key = _key(candidate)
    incoming_forecast = candidate.get("source") in FORECAST_SOURCES
    old_source = (existing or {}).get("source")
    old_forecast = old_source in FORECAST_SOURCES
    if old_source in MANUAL_SOURCES:
        return None, "manual"
    anchors = {metric: verified.get((*key, metric)) for metric in AMOUNTS}
    if incoming_forecast:
        if any(anchors.values()) or (existing is not None and not old_forecast):
            return None, "forecast_vs_actual"
        incoming_when = _when(candidate.get("disclosure_datetime"))
        if incoming_when is None:
            return None, "undated_forecast"
        previous_when = forecast_latest.get(key)
        if existing is not None and previous_when and incoming_when <= previous_when:
            return None, "older_forecast"
    elif old_forecast:
        # Do not retain a forecast metric in a newly actual wide row.
        if any(candidate.get(metric) is None and anchors[metric] is None
               and existing.get(metric) is not None for metric in AMOUNTS):
            return None, "actual_incomplete"

    result = dict(existing or {})
    changed = existing is None
    blocked = False
    for metric in AMOUNTS:
        current = (existing or {}).get(metric)
        incoming = candidate.get(metric)
        anchor = anchors[metric]
        if incoming_forecast:
            selected = current if incoming is None else incoming
        elif anchor is not None:
            selected = anchor["value"]
            if incoming is not None and _is_formal_correction(candidate, anchor):
                selected = incoming
            if incoming is not None and incoming != selected:
                blocked = True
        elif incoming is None:
            selected = current
        elif current is None or old_forecast:
            selected = incoming
        elif incoming == current:
            selected = current
        elif get_priority(str(candidate.get("source"))) < get_priority(str(old_source)):
            selected = incoming
        else:
            selected = current
            blocked = True
        if selected != current:
            changed = True
        result[metric] = selected

    if all(result.get(metric) is None for metric in AMOUNTS):
        return None, "empty"
    for name in KEY:
        result[name] = candidate[name]
    if existing is None and any(anchors.values()):
        result["source"] = "official_ir_verified"
    elif existing is None or old_forecast or (not blocked and not any(anchors.values())):
        result["source"] = candidate["source"]
    else:
        result["source"] = old_source
    if existing is not None and result["source"] != old_source:
        changed = True
    if not changed:
        return None, "same" if not blocked else "lower_priority"
    result["updated_at"] = candidate.get("updated_at") or datetime.now().isoformat()
    # Existing wide columns only. In particular, private evidence does not leak
    # into PostgREST and unit/normalized_at are left untouched when absent.
    return {k: result[k] for k in (*KEY, *AMOUNTS, "source", "updated_at", "unit", "normalized_at") if k in result}, "update" if existing else "insert"


def _pages(api, table: str, tickers: list[str], periods: tuple[str, str], select: str,
           extra: dict[str, str] | None = None) -> list[dict]:
    result: list[dict] = []
    for i in range(0, len(tickers), 25):
        ticker_chunk = tickers[i:i + 25]
        offset = 0
        while True:
            params = {"select": select, "ticker": f"in.({','.join(ticker_chunk)})",
                      "and": f"(period.gte.{periods[0]},period.lte.{periods[1]})"}
            if extra:
                params.update(extra)
            response = api._request(
                "GET", f"{api.rest_url}/{table}",
                headers={**api.headers, "Prefer": "", "Range": f"{offset}-{offset + 999}"},
                params=params,
            )
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError(f"Invalid {table} response")
            result.extend(rows)
            if len(rows) < 1000:
                break
            offset += 1000
    return result


def plan_wide_updates(api, candidates: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Read existing rows and canonical evidence, then plan field-safe writes.

    A failed evidence query raises before any caller can POST. Each input key is
    evaluated, including rows that need no write.
    """
    if not candidates:
        return [], {"candidates": 0, "write_rows": 0}
    keys = {_key(r) for r in candidates}
    tickers = sorted({k[0] for k in keys})
    periods = min(k[1] for k in keys), max(k[1] for k in keys)
    wide_rows = _pages(api, "financials", tickers, periods,
                       "ticker,period,quarter,sales,gross_profit,operating_profit,source,updated_at,unit,normalized_at")
    canonical_rows = _pages(api, "canonical_financials", tickers, periods,
                            "ticker,period,quarter,metric,value,unit,source,source_priority,filing_id,disclosure_datetime,correction_flag,document_type",
                            {"metric": "in.(sales,gross_profit,operating_profit)"})
    existing = {_key(row): row for row in wide_rows if _key(row) in keys}
    canonical = [r for r in canonical_rows if _key(r) in keys]
    verified = _verified_winners(canonical)
    forecast_latest = _forecast_latest(canonical)
    planned: list[dict] = []
    counts: Counter = Counter()
    for row in candidates:
        payload, reason = decide_wide_row(row, existing.get(_key(row)), verified, forecast_latest)
        counts[reason] += 1
        if payload is not None:
            planned.append(payload)
            existing[_key(row)] = payload
    counts["candidates"] = len(candidates)
    counts["write_rows"] = len(planned)
    return planned, dict(counts)


def guarded_wide_upsert(api, candidates: list[dict], writer) -> dict[str, int]:
    """Run the same plan used by dry-run and submit only safe whole rows."""
    planned, counts = plan_wide_updates(api, candidates)
    if planned:
        writer(planned)
    return counts


def plan_canonical_forecast_updates(api, candidates: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Prevent an older forecast from replacing the same canonical source row."""
    if not candidates:
        return [], {"candidates": 0, "write_rows": 0}
    tickers = sorted({str(row["ticker"]) for row in candidates})
    periods = min(str(row["period"]) for row in candidates), max(str(row["period"]) for row in candidates)
    existing = _pages(
        api, "canonical_financials", tickers, periods,
        "ticker,period,quarter,metric,value,source,source_row_key,disclosure_datetime",
        {"metric": "in.(sales,operating_profit)"},
    )
    by_source_key = {row["source_row_key"]: row for row in existing if row.get("source_row_key")}
    actual_keys = {(*_key(row), row["metric"]) for row in existing if row.get("source") not in FORECAST_SOURCES}
    planned: list[dict] = []
    counts: Counter = Counter()
    for row in candidates:
        key = (*_key(row), row["metric"])
        prior = by_source_key.get(row["source_row_key"])
        incoming_when = _when(row.get("disclosure_datetime"))
        if row.get("source") not in FORECAST_SOURCES or row.get("value") is None or incoming_when is None:
            reason = "invalid_forecast"
        elif key in actual_keys:
            reason = "forecast_vs_actual"
        elif prior is not None and _when(prior.get("disclosure_datetime")) and incoming_when <= _when(prior.get("disclosure_datetime")):
            reason = "same" if row.get("value") == prior.get("value") else "older_forecast"
        else:
            reason = "update" if prior else "insert"
            planned.append(row)
            by_source_key[row["source_row_key"]] = row
        counts[reason] += 1
    counts["candidates"] = len(candidates)
    counts["write_rows"] = len(planned)
    return planned, dict(counts)


def plan_canonical_actual_updates(api, candidates: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Keep idempotent actual provenance rows and reject ambiguous same-source downgrades."""
    if not candidates:
        return [], {"candidates": 0, "write_rows": 0}
    tickers = sorted({str(row["ticker"]) for row in candidates})
    periods = min(str(row["period"]) for row in candidates), max(str(row["period"]) for row in candidates)
    metrics = sorted({str(row["metric"]) for row in candidates})
    existing = _pages(
        api, "canonical_financials", tickers, periods,
        "ticker,period,quarter,metric,value,unit,source,source_priority,source_row_key,filing_id,disclosure_datetime,correction_flag,document_type",
        {"metric": f"in.({','.join(metrics)})"},
    )
    by_source_key = {row["source_row_key"]: row for row in existing if row.get("source_row_key")}
    planned: list[dict] = []
    counts: Counter = Counter()
    for row in candidates:
        prior = by_source_key.get(row["source_row_key"])
        if row.get("source") in FORECAST_SOURCES or row.get("value") is None:
            reason = "invalid_actual"
        elif prior is None:
            reason = "insert"
        elif row.get("value") == prior.get("value") and row.get("unit") == prior.get("unit"):
            reason = "same"
        elif _is_formal_correction(row, prior):
            reason = "correction"
        else:
            reason = "unverified_change"
        counts[reason] += 1
        if reason in ("insert", "correction"):
            planned.append(row)
            by_source_key[row["source_row_key"]] = row
    counts["candidates"] = len(candidates)
    counts["write_rows"] = len(planned)
    return planned, dict(counts)
