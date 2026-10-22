#!/usr/bin/env python3
"""Build true weekly margin aggregates and index candles from official dates."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
import logging

import requests

from tools.sync_margin_interest import (
    INDEX_CODES, api_pages, rpc, sector_index_bars, supabase_get,
)

LOG = logging.getLogger("margin_weekly_history")
HISTORY_START = date(2026, 7, 17)  # 7/22 target needs the complete preceding week.


def checked_bar(values: dict, label: str) -> dict[str, str] | None:
    if any(values.get(key) is None for key in ("open", "high", "low", "close")):
        return None
    bar = {key: Decimal(str(values[key])) for key in ("open", "high", "low", "close")}
    if any(not value.is_finite() or value <= 0 for value in bar.values()) or \
            bar["high"] < max(bar["open"], bar["close"]) or \
            bar["low"] > min(bar["open"], bar["close"]):
        raise ValueError(f"invalid daily index OHLC: {label}")
    return {key: str(value) for key, value in bar.items()}


def weekly_bar(days: list[date], daily: dict[date, dict[str, str]], label: str) -> dict | None:
    """Require every official trading day; never treat Friday's bar as a week."""
    bars = [checked_bar(daily[day], f"{label} {day}") if day in daily else None for day in days]
    if any(bar is None for bar in bars):
        return None
    complete = [bar for bar in bars if bar is not None]
    return {
        "open": complete[0]["open"],
        "high": str(max(Decimal(bar["high"]) for bar in complete)),
        "low": str(min(Decimal(bar["low"]) for bar in complete)),
        "close": complete[-1]["close"],
        "trading_days": len(days),
    }


def sync_weekly_history(session: requests.Session, cfg: dict, headers: dict[str, str],
                        calendar_days: list[date], *, apply: bool) -> tuple[int, int, int]:
    batches = supabase_get(session, cfg, "margin_weekly_batch", {
        "select": "date,row_count", "date": f"gte.{HISTORY_START.isoformat()}", "order": "date.asc",
    })
    aggregate_saved = {row["date"] for row in supabase_get(session, cfg, "margin_weekly_aggregate", {
        "select": "date", "date": f"gte.{HISTORY_START.isoformat()}", "order": "date.asc",
    })}
    index_saved = defaultdict(set)
    for row in supabase_get(session, cfg, "margin_index_weekly", {
        "select": "date,code", "date": f"gte.{HISTORY_START.isoformat()}", "order": "date.asc,code.asc",
    }):
        index_saved[row["date"]].add(row["code"])
    weeks = defaultdict(list)
    for day in calendar_days:
        weeks[day.isocalendar()[:2]].append(day)
    batches_by_date = {date.fromisoformat(row["date"]): row for row in batches}
    dates = sorted(batches_by_date)
    recent = set(dates[-2:])
    errors = 0
    aggregate_targets = [day for day in dates if day.isoformat() not in aggregate_saved or day in recent]
    for day in aggregate_targets:
        try:
            if apply:
                result = rpc(session, cfg, "refresh_margin_weekly_aggregate", {"p_date": day.isoformat()})
                LOG.info("aggregate date=%s result=%s", day, result)
            else:
                LOG.info("aggregate plan date=%s rows=%s", day, batches_by_date[day]["row_count"])
        except Exception as exc:
            errors += 1
            LOG.error("aggregate date=%s failed: %s", day, exc)

    required = INDEX_CODES | {"TOPIX"}
    targets = [day for day in dates if day in recent or len(index_saved[day.isoformat()] & required) < 34]
    if not targets:
        return len(aggregate_targets), 0, errors

    first_day = min(weeks[day.isocalendar()[:2]][0] for day in targets)
    last_day = max(targets)
    topix_raw = api_pages(session, "/indices/bars/daily/topix", {
        "from": first_day.isoformat(), "to": last_day.isoformat(),
    }, headers)
    topix_daily: dict[date, dict[str, str]] = {}
    for row in topix_raw:
        day = date.fromisoformat(row["Date"])
        if first_day <= day <= last_day:
            if day in topix_daily:
                raise ValueError(f"duplicate TOPIX date {day}")
            topix_daily[day] = {key: row.get(source) for source, key in
                                (("O", "open"), ("H", "high"), ("L", "low"), ("C", "close"))}

    saved_daily = defaultdict(dict)
    for row in supabase_get(session, cfg, "margin_index_daily", {
        "select": "date,code,open,high,low,close",
        "date": f"gte.{first_day.isoformat()}", "order": "date.asc,code.asc",
    }):
        day = date.fromisoformat(row["date"])
        if day <= last_day:
            saved_daily[day][row["code"]] = row
    needed_days = sorted({day for final in targets for day in weeks[final.isocalendar()[:2]] if day <= final})
    sector_daily = defaultdict(dict)
    refresh_days = set(needed_days[-5:])
    for day in needed_days:
        saved = saved_daily.get(day, {})
        if day in refresh_days or sum(
            checked_bar(saved.get(code, {}), f"saved {day} {code}") is not None
            for code in INDEX_CODES
        ) < 33:
            try:
                raw = api_pages(session, "/indices/bars/daily", {"date": day.isoformat()}, headers)
                fetched = sector_index_bars(raw, day)
                sector_daily[day] = {code: bar for code, bar in fetched.items()}
            except Exception as exc:
                errors += 1
                LOG.error("sector daily date=%s failed: %s", day, exc)
                sector_daily[day] = saved
        else:
            sector_daily[day] = saved

    missing = 0
    for final in targets:
        days = [day for day in weeks[final.isocalendar()[:2]] if day <= final]
        if not days or days[-1] != final:
            errors += 1
            LOG.error("weekly date=%s is not official final trading day", final)
            continue
        rows = []
        for code in sorted(required):
            source = topix_daily if code == "TOPIX" else {
                day: bars[code] for day, bars in sector_daily.items() if code in bars
            }
            bar = weekly_bar(days, source, f"{final} {code}")
            if bar is None:
                missing += 1
                continue
            rows.append({"date": final.isoformat(), "code": code, **bar,
                         "source": "J-Quants V2 /indices/bars/daily/topix" if code == "TOPIX"
                         else "J-Quants V2 /indices/bars/daily"})
        if len(rows) != 34:
            errors += 1
            LOG.error("weekly index date=%s complete=%s/34", final, len(rows))
            continue
        if apply:
            response = session.post(f"{cfg['rest_url']}/margin_index_weekly", json=rows,
                                    headers={**cfg["headers"], "Prefer": "resolution=merge-duplicates,return=minimal"},
                                    params={"on_conflict": "date,code"}, timeout=90)
            response.raise_for_status()
        LOG.info("weekly index date=%s days=%s complete=34/34 apply=%s", final, len(days), apply)
    return len(aggregate_targets), len(targets), errors + missing
