#!/usr/bin/env python3
"""決算発表銘柄とJ-Quants V2日足を結合し、翌営業日反応を検証する。"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from src.common_ticker import (
    is_valid_ticker,
    normalize_ticker,
    strip_tdnet_trailing_zero,
)
from src.events.env_loader import load_project_env
from src.events.tdnet_event_store import _get_supabase
from tools.fetch_jquants_prices import fetch_daily_quotes_by_date
from tools.jquants_auth import get_auth_headers

EARNINGS_DATE = "2026-07-15"
NEXT_TRADING_DATE = "2026-07-16"
DEFAULT_CSV = PROJECT_ROOT / "output" / f"earnings_reaction_{EARNINGS_DATE}.csv"
DEFAULT_DB = PROJECT_ROOT / "data" / "jquants.db"
MIGRATION = PROJECT_ROOT / "migrations" / "004_earnings_reactions.sql"

OUTPUT_COLUMNS = [
    "code", "jquants_code", "company_name", "fiscal_quarter", "earnings_date",
    "close_2026_07_15_raw", "open_2026_07_16_raw", "close_2026_07_16_raw",
    "close_2026_07_15_adjusted", "open_2026_07_16_adjusted",
    "close_2026_07_16_adjusted", "open_gap_return_pct",
    "next_close_return_pct", "intraday_return_pct", "volume_2026_07_16",
    "trading_value_2026_07_16", "upper_limit_flag", "lower_limit_flag",
    "data_status", "missing_reason",
]
PERCENT_COLUMNS = {
    "open_gap_return_pct", "next_close_return_pct", "intraday_return_pct"
}
VIEWER_EXCLUSIONS = (
    "訂正・数値データ訂正", "一部訂正", "一部変更", "再訂正",
    "定時株主総会", "継続開催",
)


def _safe_float(value: Any) -> float | None:
    if value is None or value == "" or str(value).lower() == "null":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    number = _safe_float(value)
    return None if number is None else int(number)


def _flag(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return 0 if str(value).strip().lower() in {"0", "false", "none", "null"} else 1


def _is_viewer_earnings_row(row: dict[str, Any]) -> bool:
    headline = str(row.get("headline") or "")
    if any(term in headline for term in VIEWER_EXCLUSIONS):
        return False
    return not ("決算短信" in headline and "訂正" in headline)


def fetch_viewer_population(earnings_date: str) -> tuple[list[dict[str, Any]], int]:
    """Company Viewerのtdnet_eventsから、画面と同じ条件で母集団を得る。"""
    day = date.fromisoformat(earnings_date)
    # JST 00:00 は前日15:00 UTC。PostgRESTにはUTC境界を明示する。
    start_utc = f"{day - timedelta(days=1)}T15:00:00Z"
    end_utc = f"{day}T15:00:00Z"
    client = _get_supabase()
    response = (
        client.table("tdnet_events")
        .select(
            "id,ticker,company_name,event_type,event_subtype,headline,"
            "disclosed_at,status,raw_payload"
        )
        .eq("event_type", "earnings")
        .eq("status", "active")
        .gte("disclosed_at", start_utc)
        .lt("disclosed_at", end_utc)
        .order("disclosed_at")
        .execute()
    )
    raw_rows = response.data or []
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        if not _is_viewer_earnings_row(row):
            continue
        payload = row.get("raw_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        extracted = (payload or {}).get("extracted") or {}
        rows.append({
            "source_event_id": str(row.get("id") or ""),
            "code": str(row.get("ticker") or "").strip().upper(),
            "company_name": str(row.get("company_name") or ""),
            "fiscal_quarter": row.get("event_subtype") or extracted.get("quarter"),
            "earnings_date": earnings_date,
        })
    return rows, len(raw_rows)


def _price_index(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        raw_code = str(row.get("Code") or "").strip().upper()
        # V2日足は英字コードを 205A0 のように英字のまま返す。
        # 20500（銘柄2050）を205Aへ変換する旧財務API用mapは適用しない。
        code = strip_tdnet_trailing_zero(raw_code)
        if code:
            index[code].append(row)
    return dict(index)


def build_reaction_rows(
    population: list[dict[str, Any]],
    prices_0715: list[dict[str, Any]],
    prices_0716: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """入力を1件も落とさず、価格結合・欠損判定・調整済みリターン計算を行う。"""
    prev_index = _price_index(prices_0715)
    next_index = _price_index(prices_0716)
    normalized_codes = [normalize_ticker(row.get("code", "")) for row in population]
    duplicate_counts = Counter(code for code in normalized_codes if code)
    results: list[dict[str, Any]] = []

    for position, source in enumerate(population):
        code = normalized_codes[position]
        reasons: list[str] = []
        prev_matches = prev_index.get(code, [])
        next_matches = next_index.get(code, [])

        if not code or not is_valid_ticker(code):
            reasons.append("invalid_code")
        if duplicate_counts.get(code, 0) > 1:
            reasons.append("duplicate_code")
        if len(prev_matches) > 1 or len(next_matches) > 1:
            reasons.append("ambiguous_jquants_code")

        prev = prev_matches[0] if len(prev_matches) == 1 else None
        nxt = next_matches[0] if len(next_matches) == 1 else None
        if prev is None:
            reasons.append("no_price_2026-07-15")
        if nxt is None:
            reasons.append("no_price_2026-07-16")

        raw_prev_close = _safe_float(prev.get("C")) if prev else None
        raw_next_open = _safe_float(nxt.get("O")) if nxt else None
        raw_next_close = _safe_float(nxt.get("C")) if nxt else None
        adj_prev_close = _safe_float(prev.get("AdjC")) if prev else None
        adj_next_open = _safe_float(nxt.get("AdjO")) if nxt else None
        adj_next_close = _safe_float(nxt.get("AdjC")) if nxt else None
        volume = _safe_int(nxt.get("Vo")) if nxt else None

        required = (
            (raw_prev_close, "null_close_2026-07-15_raw"),
            (raw_next_open, "null_open_2026-07-16_raw"),
            (raw_next_close, "null_close_2026-07-16_raw"),
            (adj_prev_close, "null_close_2026-07-15_adjusted"),
            (adj_next_open, "null_open_2026-07-16_adjusted"),
            (adj_next_close, "null_close_2026-07-16_adjusted"),
        )
        for value, reason in required:
            if (prev is not None or "2026-07-15" not in reason) and (
                nxt is not None or "2026-07-16" not in reason
            ) and value is None:
                reasons.append(reason)
        if nxt is not None and (volume is None or volume == 0):
            reasons.append("no_trade_2026-07-16")
        if adj_prev_close == 0:
            reasons.append("zero_close_2026-07-15_adjusted")
        if adj_next_open == 0:
            reasons.append("zero_open_2026-07-16_adjusted")

        can_calc = (
            adj_prev_close is not None and adj_prev_close != 0
            and adj_next_open is not None and adj_next_open != 0
            and adj_next_close is not None
            and volume is not None and volume > 0
        )
        open_gap = adj_next_open / adj_prev_close - 1 if can_calc else None
        next_close = adj_next_close / adj_prev_close - 1 if can_calc else None
        intraday = adj_next_close / adj_next_open - 1 if can_calc else None
        jq_source = nxt or prev or {}

        non_duplicate_reasons = [r for r in reasons if r != "duplicate_code"]
        if not reasons:
            status = "ok"
        elif not non_duplicate_reasons:
            status = "duplicate_code"
        else:
            status = "missing"

        results.append({
            "source_event_id": source.get("source_event_id") or f"row-{position}",
            "code": code or str(source.get("code") or ""),
            "jquants_code": jq_source.get("Code"),
            "company_name": source.get("company_name") or "",
            "fiscal_quarter": source.get("fiscal_quarter"),
            "earnings_date": source.get("earnings_date") or EARNINGS_DATE,
            "close_2026_07_15_raw": raw_prev_close,
            "open_2026_07_16_raw": raw_next_open,
            "close_2026_07_16_raw": raw_next_close,
            "close_2026_07_15_adjusted": adj_prev_close,
            "open_2026_07_16_adjusted": adj_next_open,
            "close_2026_07_16_adjusted": adj_next_close,
            "open_gap_return_pct": open_gap,
            "next_close_return_pct": next_close,
            "intraday_return_pct": intraday,
            "volume_2026_07_16": volume,
            "trading_value_2026_07_16": _safe_float(nxt.get("Va")) if nxt else None,
            "upper_limit_flag": _flag(nxt.get("UL")) if nxt else None,
            "lower_limit_flag": _flag(nxt.get("LL")) if nxt else None,
            "data_status": status,
            "missing_reason": ";".join(dict.fromkeys(reasons)),
        })

    return sorted(
        results,
        key=lambda row: (
            row["open_gap_return_pct"] is None,
            -(row["open_gap_return_pct"] or 0.0),
            row["code"],
        ),
    )


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            output = {column: row.get(column) for column in OUTPUT_COLUMNS}
            for column in PERCENT_COLUMNS:
                value = output[column]
                output[column] = "" if value is None else f"{value:.2%}"
            writer.writerow(output)


def save_sqlite(rows: list[dict[str, Any]], db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["source_event_id", *OUTPUT_COLUMNS]
    update_columns = [column for column in OUTPUT_COLUMNS]
    placeholders = ",".join("?" for _ in columns)
    assignments = ",".join(f"{column}=excluded.{column}" for column in update_columns)
    sql = (
        f"INSERT INTO earnings_reactions ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(source_event_id) DO UPDATE SET {assignments},updated_at=datetime('now')"
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(MIGRATION.read_text(encoding="utf-8"))
        conn.executemany(sql, [[row.get(column) for column in columns] for row in rows])
        conn.commit()


def _auth_headers_without_secret_logging() -> dict[str, str]:
    auth_logger = logging.getLogger("jquants_auth")
    previous_disabled = auth_logger.disabled
    auth_logger.disabled = True
    try:
        return get_auth_headers()
    finally:
        auth_logger.disabled = previous_disabled


def summarize(rows: list[dict[str, Any]], raw_population_count: int) -> dict[str, Any]:
    duplicates = sum(
        count - 1 for count in Counter(row["code"] for row in rows).values() if count > 1
    )
    missing = [row for row in rows if row["data_status"] != "ok"]
    return {
        "raw_population_count": raw_population_count,
        "input_count": len(rows),
        "output_count": len(rows),
        "joined_ok_count": sum(row["data_status"] == "ok" for row in rows),
        "missing_count": len(missing),
        "unprocessed_count": 0,
        "duplicate_count": duplicates,
        "missing": [
            {"code": row["code"], "company_name": row["company_name"],
             "reason": row["missing_reason"]}
            for row in missing
        ],
        "top10": [
            {"code": row["code"], "company_name": row["company_name"],
             "open_gap_return_pct": row["open_gap_return_pct"],
             "next_close_return_pct": row["next_close_return_pct"]}
            for row in rows if row["open_gap_return_pct"] is not None
        ][:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_project_env()
    population, raw_count = fetch_viewer_population(EARNINGS_DATE)
    headers = _auth_headers_without_secret_logging()
    prices_0715 = fetch_daily_quotes_by_date(EARNINGS_DATE, headers)
    prices_0716 = fetch_daily_quotes_by_date(NEXT_TRADING_DATE, headers)
    rows = build_reaction_rows(population, prices_0715, prices_0716)
    if len(rows) != len(population):
        raise RuntimeError(f"件数不一致: input={len(population)}, output={len(rows)}")
    write_csv(rows, args.csv)
    save_sqlite(rows, args.db)
    report = summarize(rows, raw_count)
    report.update({"csv": str(args.csv.resolve()), "db": str(args.db.resolve())})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
