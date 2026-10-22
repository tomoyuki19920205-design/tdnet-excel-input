#!/usr/bin/env python3
"""Persist J-Quants margin interest, historical sectors and cash index closes.

Run without --apply to inspect the plan. Nightly uses --apply under the existing
TDNET_Nightly lock. No raw API response is written to disk.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import get_supabase_write_config, load_env  # noqa: E402
from tools.jquants_auth import get_auth_headers  # noqa: E402

LOG = logging.getLogger("margin_interest")
JST = ZoneInfo("Asia/Tokyo")
BASE = "https://api.jquants.com/v2"
NIKKEI_CSV = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv"
MONEY_START = date(2026, 9, 25)
SECTORS = (
    "0050", "1050", "2050", "3050", "3100", "3150", "3200", "3250", "3300", "3350", "3400",
    "3450", "3500", "3550", "3600", "3650", "3700", "3750", "3800", "4050", "5050", "5100",
    "5150", "5200", "5250", "6050", "6100", "7050", "7100", "7150", "7200", "8050", "9050",
)
# Credit balances use S33 codes; J-Quants sector price indices use separate
# codes. The order follows the official 33-sector price-index list.
SECTOR_INDEX_CODES = {
    "0050": "0040", "1050": "0041", "2050": "0042", "3050": "0043", "3100": "0044",
    "3150": "0045", "3200": "0046", "3250": "0047", "3300": "0048", "3350": "0049",
    "3400": "004A", "3450": "004B", "3500": "004C", "3550": "004D", "3600": "004E",
    "3650": "004F", "3700": "0050", "3750": "0051", "3800": "0052", "4050": "0053",
    "5050": "0054", "5100": "0055", "5150": "0056", "5200": "0057", "5250": "0058",
    "6050": "0059", "6100": "005A", "7050": "005B", "7100": "005C", "7150": "005D",
    "7200": "005E", "8050": "005F", "9050": "0060",
}
INDEX_CODES = set(SECTOR_INDEX_CODES.values())
assert tuple(SECTOR_INDEX_CODES) == SECTORS and len(INDEX_CODES) == 33


def subtract_months(day: date, count: int) -> date:
    """Calendar months, inclusive retention boundary; clamp the day at month end."""
    year, month = divmod(day.year * 12 + day.month - 1 - count, 12)
    month += 1
    from calendar import monthrange
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def as_int(value: object, field: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field} missing")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} invalid") from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError(f"{field} must be a nonnegative integer")
    return int(number)


def api_pages(session: requests.Session, endpoint: str, params: dict[str, str], headers: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    seen_keys: set[str] = set()
    key: str | None = None
    while True:
        query = {**params, **({"pagination_key": key} if key else {})}
        response = None
        for attempt in range(5):
            try:
                response = session.get(BASE + endpoint, params=query, headers=headers, timeout=60)
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 4:
                    raise
            time.sleep(min(30, 2 ** (attempt + 1)))
        if response is None:
            raise RuntimeError(f"{endpoint}: no response")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError(f"{endpoint}: invalid response shape")
        rows.extend(body["data"])
        next_key = body.get("pagination_key")
        if not next_key:
            return rows
        if not isinstance(next_key, str) or next_key in seen_keys:
            raise ValueError(f"{endpoint}: repeated pagination_key")
        seen_keys.add(next_key)
        key = next_key


def trading_days(session: requests.Session, start: date, end: date, headers: dict[str, str]) -> list[date]:
    rows = api_pages(session, "/markets/calendar", {"from": start.isoformat(), "to": end.isoformat()}, headers)
    days = sorted(date.fromisoformat(row["Date"]) for row in rows if row.get("HolDiv") in ("1", "2"))
    if not days or len(set(days)) != len(days):
        raise ValueError("calendar missing or duplicate trading days")
    return days


def upsert_trading_days(session: requests.Session, cfg: dict, days: list[date]) -> None:
    """Keep the official calendar for exact daily and weekly comparisons."""
    for offset in range(0, len(days), 250):
        rows = [{"date": day.isoformat()} for day in days[offset:offset + 250]]
        response = session.post(f"{cfg['rest_url']}/margin_trading_day", json=rows,
                                headers={**cfg["headers"], "Prefer": "resolution=merge-duplicates,return=minimal"},
                                params={"on_conflict": "date"}, timeout=90)
        response.raise_for_status()


def last_days_of_weeks(days: list[date]) -> set[date]:
    by_week: dict[tuple[int, int], list[date]] = {}
    for day in days:
        iso = day.isocalendar()
        by_week.setdefault((iso.year, iso.week), []).append(day)
    # J-Quants has no old weekly record for weeks of two or fewer market days.
    return {week[-1] for week in by_week.values()
            if week[-1] >= MONEY_START or len(week) >= 3}


def normalize_snapshot(raw: list[dict], target: date, master: dict[str, dict]) -> tuple[list[dict], date | None]:
    if not raw:
        raise ValueError(f"{target}: empty margin result")
    if not master:
        raise ValueError(f"{target}: empty historical master")
    seen: set[str] = set()
    pubdates: set[str] = set()
    result: list[dict] = []
    for item in raw:
        if item.get("Date") != target.isoformat():
            raise ValueError(f"{target}: mixed application date")
        code = str(item.get("Code") or "")
        if not re.fullmatch(r"[0-9A-Z]{5}", code) or code in seen:
            raise ValueError(f"{target}: invalid or duplicate code {code}")
        seen.add(code)
        if item.get("PubDate"):
            pubdates.add(str(item["PubDate"]))
        before_money = target < MONEY_START
        long_val = as_int(item.get("LongVal"), "LongVal", nullable=before_money)
        short_val = as_int(item.get("ShrtVal"), "ShrtVal", nullable=before_money)
        as_int(item.get("ShrtVol"), "ShrtVol")
        if before_money and (long_val is not None or short_val is not None):
            raise ValueError(f"{target}: old values must be null")
        mapped = master.get(code)
        s33 = str(mapped.get("S33") or "") if mapped else ""
        if mapped is None:
            category, sector_code = "unknown", None
        elif s33 in SECTORS:
            category, sector_code = "sector", s33
        elif s33 == "9999":
            category, sector_code = "outside", None
        else:
            category, sector_code = "unknown", None
        result.append({
            "code": code, "long_vol": as_int(item.get("LongVol"), "LongVol"),
            "long_val": long_val, "short_val": short_val,
            "sector_code": sector_code, "category": category,
        })
    if len(pubdates) > 1 or (target >= MONEY_START and len(pubdates) != 1):
        raise ValueError(f"{target}: inconsistent publication dates")
    published = date.fromisoformat(next(iter(pubdates))) if pubdates else None
    if published is not None and published < target:
        raise ValueError(f"{target}: publication before application date")
    return result, published


def official_nikkei_closes(session: requests.Session) -> dict[date, str]:
    response = session.get(NIKKEI_CSV, timeout=60)
    response.raise_for_status()
    closes: dict[date, str] = {}
    for row in csv.reader(io.StringIO(response.content.decode("cp932"))):
        if len(row) < 2 or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", row[0]):
            continue  # header and the publisher's closing notice
        day = date.fromisoformat(row[0].replace("/", "-"))
        close = Decimal(row[1])
        if close <= 0 or not close.is_finite() or day in closes:
            raise ValueError("invalid or duplicate Nikkei official close")
        closes[day] = str(close)
    if not closes:
        raise ValueError("Nikkei official CSV has no closes")
    return closes


def supabase_get(session: requests.Session, cfg: dict, table: str, params: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        response = session.get(f"{cfg['rest_url']}/{table}", headers=cfg["headers"],
                               params={**params, "limit": "500", "offset": str(offset)}, timeout=45)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            raise ValueError(f"{table}: unexpected database response")
        rows.extend(body)
        if len(body) < 500:
            return rows
        offset += 500


def rpc(session: requests.Session, cfg: dict, name: str, payload: dict) -> object:
    response = session.post(f"{cfg['rest_url']}/rpc/{name}", json=payload, headers=cfg["headers"], timeout=180)
    response.raise_for_status()
    return response.json()


def sector_index_bars(raw: list[dict], target: date,
                      allowed_codes: set[str] = INDEX_CODES) -> dict[str, dict[str, str | None]]:
    """Select exact-date price-index bars without inventing missing OHLC."""
    bars: dict[str, dict[str, str | None]] = {}
    for row in raw:
        code = str(row.get("Code") or "")
        if code not in allowed_codes:
            continue
        if row.get("Date") != target.isoformat() or code in bars:
            raise ValueError(f"{target}: mixed date or duplicate sector index {code}")
        if row.get("C") is None or row.get("C") == "":
            continue
        values: dict[str, str | None] = {}
        for source, dest in (("O", "open"), ("H", "high"), ("L", "low"), ("C", "close")):
            value = row.get(source)
            if value is None or value == "":
                values[dest] = None
                continue
            try:
                number = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError(f"{target}: invalid index {dest} {code}") from exc
            if not number.is_finite() or number <= 0:
                raise ValueError(f"{target}: nonpositive index {dest} {code}")
            values[dest] = str(number)
        if all(values[name] is not None for name in ("open", "high", "low")):
            if Decimal(values["high"]) < max(Decimal(values["open"]), Decimal(values["close"])) or \
                    Decimal(values["low"]) > min(Decimal(values["open"]), Decimal(values["close"])):
                raise ValueError(f"{target}: inconsistent index OHLC {code}")
        bars[code] = values
    return bars


def sector_index_closes(raw: list[dict], target: date) -> dict[str, str]:
    """Compatibility helper for exact-date close checks."""
    return {code: bar["close"] for code, bar in sector_index_bars(raw, target).items()}


def upsert_sector_indices(session: requests.Session, cfg: dict, target: date,
                          bars: dict[str, dict[str, str | None]]) -> None:
    if not bars:
        return
    rows = [{"date": target.isoformat(), "code": code, **bar}
            for code, bar in sorted(bars.items())]
    response = session.post(f"{cfg['rest_url']}/margin_index_daily", json=rows,
                            headers={**cfg["headers"], "Prefer": "resolution=merge-duplicates,return=minimal"},
                            params={"on_conflict": "date,code"}, timeout=90)
    response.raise_for_status()


def sync_sector_indices(session: requests.Session, cfg: dict, headers: dict[str, str],
                        refresh: set[date], *, apply: bool, chosen: date | None = None) -> tuple[int, int, int]:
    """Backfill every saved aggregate date and refresh the last five trading days."""
    aggregates = {row["date"] for row in supabase_get(session, cfg, "margin_daily", {
        "select": "date", "date": f"gte.{MONEY_START.isoformat()}", "order": "date.asc",
    })}
    saved_rows = supabase_get(session, cfg, "margin_index_daily", {
        "select": "date,code,open,high,low", "date": f"gte.{MONEY_START.isoformat()}", "order": "date.asc,code.asc",
    })
    saved: dict[str, set[str]] = {}
    for row in saved_rows:
        if all(row.get(field) is not None for field in ("open", "high", "low")):
            saved.setdefault(row["date"], set()).add(row["code"])
    targets = sorted(date.fromisoformat(day) for day in aggregates
                     if (chosen is None or day == chosen.isoformat())
                     and (date.fromisoformat(day) in refresh or len(saved.get(day, set()) & INDEX_CODES) < 33))
    errors = 0
    for day in targets:
        try:
            raw = api_pages(session, "/indices/bars/daily", {"date": day.isoformat()}, headers)
            bars = sector_index_bars(raw, day)
            if apply:
                upsert_sector_indices(session, cfg, day, bars)
            complete = sum(all(bar.get(field) is not None for field in ("open", "high", "low")) for bar in bars.values())
            LOG.info("sector indices date=%s closes=%s/33 ohlc=%s/33 apply=%s", day, len(bars), complete, apply)
        except Exception as exc:
            errors += 1
            LOG.error("sector indices date=%s failed: %s", day, exc)
    if apply:
        saved_rows = supabase_get(session, cfg, "margin_index_daily", {
            "select": "date,code,open,high,low", "date": f"gte.{MONEY_START.isoformat()}", "order": "date.asc,code.asc",
        })
        saved = {}
        for row in saved_rows:
            if all(row.get(field) is not None for field in ("open", "high", "low")):
                saved.setdefault(row["date"], set()).add(row["code"])
    missing = sum(33 - len(saved.get(day, set()) & INDEX_CODES) for day in aggregates)
    LOG.info("sector indices targets=%s saved_dates=%s missing=%s errors=%s",
             len(targets), len(aggregates), missing, errors)
    return len(targets), missing, errors


def sync_topix_indices(session: requests.Session, cfg: dict, raw: list[dict],
                       refresh: set[date], *, apply: bool,
                       chosen: date | None = None) -> tuple[int, int, int]:
    """Save TOPIX daily OHLC for saved credit dates; refresh recent corrections."""
    aggregates = {row["date"] for row in supabase_get(session, cfg, "margin_daily", {
        "select": "date", "date": f"gte.{MONEY_START.isoformat()}", "order": "date.asc",
    })}
    saved_rows = supabase_get(session, cfg, "margin_index_daily", {
        "select": "date,open,high,low,close", "code": "eq.TOPIX",
        "date": f"gte.{MONEY_START.isoformat()}", "order": "date.asc",
    })
    saved = {row["date"] for row in saved_rows
             if all(row.get(field) is not None for field in ("open", "high", "low", "close"))}
    targets = sorted(date.fromisoformat(day) for day in aggregates
                     if (chosen is None or day == chosen.isoformat())
                     and (date.fromisoformat(day) in refresh or day not in saved))
    by_date: dict[str, list[dict]] = {}
    for row in raw:
        day = row.get("Date")
        if day in aggregates:
            by_date.setdefault(day, []).append({**row, "Code": "TOPIX"})
    errors = 0
    for day in targets:
        try:
            bar = sector_index_bars(by_date.get(day.isoformat(), []), day, {"TOPIX"}).get("TOPIX")
            if bar is None or any(bar.get(field) is None for field in ("open", "high", "low", "close")):
                raise ValueError("complete TOPIX OHLC unavailable")
            if apply:
                upsert_sector_indices(session, cfg, day, {"TOPIX": bar})
            LOG.info("TOPIX daily OHLC date=%s apply=%s", day, apply)
        except Exception as exc:
            errors += 1
            LOG.error("TOPIX daily OHLC date=%s failed: %s", day, exc)
    if apply:
        saved_rows = supabase_get(session, cfg, "margin_index_daily", {
            "select": "date,open,high,low,close", "code": "eq.TOPIX",
            "date": f"gte.{MONEY_START.isoformat()}", "order": "date.asc",
        })
        saved = {row["date"] for row in saved_rows
                 if all(row.get(field) is not None for field in ("open", "high", "low", "close"))}
    missing = len(aggregates - saved)
    LOG.info("TOPIX daily OHLC targets=%s saved_dates=%s missing=%s errors=%s",
             len(targets), len(aggregates), missing, errors)
    return len(targets), missing, errors


def run(args: argparse.Namespace, *, session: requests.Session | None = None) -> int:
    load_env()
    cfg = get_supabase_write_config()
    if cfg is None:
        raise RuntimeError("Supabase service role connection is required")
    headers = get_auth_headers()
    session = session or requests.Session()
    today = datetime.now(JST).date()
    daily_cutoff = subtract_months(today, 1)
    weekly_cutoff = subtract_months(today, 6)
    calendar_start = min(MONEY_START, weekly_cutoff - timedelta(days=7))
    if args.date:
        calendar_start = min(calendar_start, date.fromisoformat(args.date) - timedelta(days=7))
    calendar_days = trading_days(session, calendar_start, today + timedelta(days=7), headers)
    calendar_errors = 0
    if args.apply:
        try:
            upsert_trading_days(session, cfg, calendar_days)
            LOG.info("trading calendar stored days=%s from=%s to=%s",
                     len(calendar_days), calendar_days[0], calendar_days[-1])
        except Exception as exc:
            calendar_errors = 1
            LOG.error("trading calendar persistence failed: %s", exc)
    if args.weekly_history_only:
        from tools.sync_margin_weekly_history import sync_weekly_history
        aggregates, indices, weekly_errors = sync_weekly_history(
            session, cfg, headers, calendar_days, apply=args.apply)
        LOG.info("weekly history finished aggregates=%s index_weeks=%s errors=%s",
                 aggregates, indices, weekly_errors + calendar_errors)
        return 1 if weekly_errors or calendar_errors else 0
    prior_days = [day for day in calendar_days if day < today]
    # Friday's application data is published on the next market day, not on
    # Saturday/Sunday/holiday. On a closed day, use the preceding two days.
    final_day = prior_days[-1] if today in calendar_days else prior_days[-2]
    days = [day for day in calendar_days if day <= final_day]
    # Use the complete week in the official calendar, including future weekdays.
    # A Thursday is never substituted merely because Friday is not published yet.
    week_finals = last_days_of_weeks(calendar_days)
    existing = {row["date"]: row for row in supabase_get(session, cfg, "margin_daily", {
        "select": "date,topix_close,nikkei_close", "date": f"gte.{MONEY_START.isoformat()}", "order": "date.asc",
    })}
    weekly_existing = {row["date"] for row in supabase_get(session, cfg, "margin_weekly_batch", {
        "select": "date", "date": f"gte.{weekly_cutoff.isoformat()}", "order": "date.asc",
    })}
    money_days = [day for day in days if day >= MONEY_START]
    refresh = set(money_days[-5:])
    targets = {day for day in money_days if day.isoformat() not in existing} | refresh
    targets |= {day for day in week_finals if weekly_cutoff <= day <= final_day and day.isoformat() not in weekly_existing}
    if args.backfill:
        targets |= set(money_days) | {day for day in week_finals if weekly_cutoff <= day <= final_day}
    if args.date:
        chosen = date.fromisoformat(args.date)
        if chosen not in days:
            raise ValueError("specified date is not a completed TSE trading date")
        targets = {chosen}
    targets = sorted(targets)
    LOG.info("plan dates=%s daily_cutoff=%s weekly_cutoff=%s", len(targets), daily_cutoff, weekly_cutoff)

    try:
        topix_raw = api_pages(session, "/indices/bars/daily/topix", {
            "from": MONEY_START.isoformat(), "to": final_day.isoformat(),
        }, headers)
        topix = {date.fromisoformat(row["Date"]): str(Decimal(str(row["C"])))
                 for row in topix_raw if row.get("C") is not None}
    except Exception as exc:
        LOG.warning("TOPIX bars unavailable: %s", exc)
        topix_raw, topix = [], {}

    if args.indices_only:
        target_count, missing_indices, errors = sync_sector_indices(
            session, cfg, headers, refresh, apply=args.apply,
            chosen=date.fromisoformat(args.date) if args.date else None)
        topix_targets, missing_topix_bars, topix_errors = sync_topix_indices(
            session, cfg, topix_raw, refresh, apply=args.apply,
            chosen=date.fromisoformat(args.date) if args.date else None)
        errors += topix_errors
        errors += calendar_errors
        LOG.info("finished indices_only sector_targets=%s missing_sectors=%s TOPIX_targets=%s missing_TOPIX=%s errors=%s",
                 target_count, missing_indices, topix_targets, missing_topix_bars, errors)
        return 1 if errors else 0

    try:
        nikkei = official_nikkei_closes(session)
    except Exception as exc:
        LOG.warning("Nikkei close unavailable: %s", exc)
        nikkei = {}
    errors = calendar_errors
    for day in targets:
        try:
            raw = api_pages(session, "/markets/margin-interest", {"date": day.isoformat()}, headers)
            if not raw and day < MONEY_START:
                LOG.info("no historical weekly record date=%s", day)
                continue
            master_rows = api_pages(session, "/equities/master", {"date": day.isoformat()}, headers)
            master = {str(item["Code"]): item for item in master_rows}
            if len(master) != len(master_rows):
                raise ValueError("duplicate historical master codes")
            rows, published = normalize_snapshot(raw, day, master)
            store_daily = day >= daily_cutoff and day >= MONEY_START
            store_weekly = day in week_finals and day >= weekly_cutoff
            payload = {
                "p_date": day.isoformat(), "p_published_date": published.isoformat() if published else None,
                "p_rows": rows, "p_store_daily": store_daily, "p_store_weekly": store_weekly,
                "p_is_week_final": day in week_finals,
                "p_topix_close": topix.get(day), "p_nikkei_close": nikkei.get(day),
            }
            if args.apply:
                result = rpc(session, cfg, "publish_margin_snapshot", payload)
                LOG.info("published date=%s rows=%s result=%s", day, len(rows), result)
                if store_weekly and day >= date(2026, 7, 17):
                    weekly_result = rpc(session, cfg, "refresh_margin_weekly_aggregate", {
                        "p_date": day.isoformat(),
                    })
                    LOG.info("weekly aggregate refreshed date=%s result=%s", day, weekly_result)
            else:
                LOG.info("validated date=%s rows=%s daily=%s weekly=%s", day, len(rows), store_daily, store_weekly)
        except Exception as exc:
            errors += 1
            LOG.error("date=%s failed: %s", day, exc)

    # Index failures are independent of a successfully published credit snapshot.
    for day_text, old in existing.items():
        day = date.fromisoformat(day_text)
        if old.get("topix_close") is None or old.get("nikkei_close") is None:
            if args.apply and (topix.get(day) or nikkei.get(day)):
                try:
                    rpc(session, cfg, "fill_margin_indices", {
                        "p_date": day_text, "p_topix_close": topix.get(day), "p_nikkei_close": nikkei.get(day),
                    })
                except Exception as exc:
                    LOG.warning("index repair failed date=%s: %s", day, exc)
                    errors += 1

    if args.apply and errors == 0:
        try:
            LOG.info("cleanup=%s", rpc(session, cfg, "cleanup_margin_security", {}))
        except Exception as exc:
            LOG.error("retention cleanup failed: %s", exc)
            errors += 1
    if args.apply:
        after = supabase_get(session, cfg, "margin_daily", {
            "select": "date,topix_close,nikkei_close", "date": f"gte.{MONEY_START.isoformat()}", "order": "date.asc",
        })
        missing_topix = sum(row["topix_close"] is None for row in after)
        missing_nikkei = sum(row["nikkei_close"] is None for row in after)
        if missing_topix or missing_nikkei:
            errors += 1
    else:
        missing_topix = sum(day not in topix for day in money_days)
        missing_nikkei = sum(day not in nikkei for day in money_days)
    index_targets, missing_indices, index_errors = sync_sector_indices(
        session, cfg, headers, refresh, apply=args.apply,
        chosen=date.fromisoformat(args.date) if args.date else None)
    errors += index_errors
    topix_targets, missing_topix_bars, topix_errors = sync_topix_indices(
        session, cfg, topix_raw, refresh, apply=args.apply,
        chosen=date.fromisoformat(args.date) if args.date else None)
    errors += topix_errors
    try:
        from tools.sync_margin_weekly_history import sync_weekly_history
        weekly_aggregates, weekly_indices, weekly_errors = sync_weekly_history(
            session, cfg, headers, calendar_days, apply=args.apply)
        errors += weekly_errors
        LOG.info("weekly history aggregates=%s index_weeks=%s errors=%s",
                 weekly_aggregates, weekly_indices, weekly_errors)
    except Exception as exc:
        errors += 1
        LOG.error("weekly history failed: %s", exc)
    LOG.info("finished targets=%s errors=%s missing_topix=%s missing_nikkei=%s sector_index_targets=%s missing_sector_indices=%s TOPIX_bar_targets=%s missing_TOPIX_bars=%s",
             len(targets), errors, missing_topix, missing_nikkei, index_targets, missing_indices,
             topix_targets, missing_topix_bars)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write verified snapshots and apply retention")
    parser.add_argument("--backfill", action="store_true", help="revisit every available date in the retention window")
    parser.add_argument("--date", help="re-fetch one application date (YYYY-MM-DD)")
    parser.add_argument("--indices-only", action="store_true", help="backfill or refresh saved sector OHLC without re-fetching margin securities")
    parser.add_argument("--weekly-history-only", action="store_true", help="aggregate saved weekly securities and backfill weekly index candles")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run(args)
    except Exception as exc:
        LOG.error("margin synchronization failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
