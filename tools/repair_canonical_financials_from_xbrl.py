#!/usr/bin/env python3
"""Repair one canonical financial disclosure from its cached TDnet XBRL.

The command is deliberately narrow: ticker, fiscal-year end, cumulative period,
quarter, filing id and metrics are all mandatory.  It is dry-run by default and
uses optimistic guards when updating both SQLite and Supabase.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

JST = timezone(timedelta(hours=9))
_METRIC_ATTRS = {
    "sales": "sales",
    "gross_profit": "gross_profit",
    "operating_profit": "operating_profit",
    "ordinary_profit": "ordinary_profit",
    "net_income": "net_income",
}
_SQLITE_COLUMNS = {
    "sales": "sales",
    "gross_profit": "gross_profit",
    "operating_profit": "operating_profit",
    "net_income": "net_income",
}


@dataclass(frozen=True)
class Change:
    store: str
    primary_key: str
    metric: str
    before: dict[str, Any]
    after: dict[str, Any]


def _load_env_file(path: str | None) -> None:
    if not path:
        return
    for raw_line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _million_yen(value: int | float | None) -> float | None:
    if value is None:
        return None
    return float(value) / 1_000_000


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def extracted_metrics(extracted: Any, selected: Iterable[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    field_sources = getattr(extracted, "field_sources", {}) or {}
    for metric in selected:
        attr = _METRIC_ATTRS[metric]
        raw_value = getattr(extracted, attr, None)
        if raw_value is None:
            raise ValueError(f"selected metric is missing from XBRL: {metric}")
        source = str(field_sources.get(metric) or "xbrl").split("|", 1)[0]
        result[metric] = {
            "value": _clean_number(_million_yen(raw_value)),
            "unit": "millions_jpy",
            "source": source,
        }
    return result


def validate_period(
    *, fiscal_year_end: str, period_start: str, period_end: str, quarter: str
) -> None:
    from lib.pipeline.financial_integrity import duration_quarter

    actual = duration_quarter(
        period_start,
        period_end,
        fiscal_start=period_start,
        fiscal_end=fiscal_year_end,
    )
    if actual != quarter:
        raise ValueError(
            f"cumulative duration is {actual!r}, expected {quarter!r}: "
            f"{period_start}..{period_end} (fiscal year end {fiscal_year_end})"
        )


def build_canonical_plan(
    rows: list[dict[str, Any]],
    *,
    ticker: str,
    fiscal_year_end: str,
    quarter: str,
    filing_id: str,
    desired_metrics: dict[str, dict[str, Any]],
) -> list[Change]:
    from lib.pipeline.canonical_writer import _make_financials_row_key
    from lib.pipeline.source_priority import get_priority

    changes: list[Change] = []
    for metric, desired in desired_metrics.items():
        candidates = [r for r in rows if r.get("metric") == metric]
        if len(candidates) != 1:
            raise ValueError(
                f"expected exactly one canonical row for {metric}, found {len(candidates)}"
            )
        row = candidates[0]
        desired_key = _make_financials_row_key(
            ticker, fiscal_year_end, quarter, metric, desired["source"], filing_id
        )
        collision = [
            r for r in rows
            if r.get("source_row_key") == desired_key and r.get("id") != row.get("id")
        ]
        if collision:
            raise ValueError(f"desired source_row_key already exists: {desired_key}")

        before = {
            "value": row.get("value"),
            "unit": row.get("unit"),
            "source": row.get("source"),
            "source_priority": row.get("source_priority"),
            "source_row_key": row.get("source_row_key"),
            "updated_at": row.get("updated_at"),
        }
        after = {
            "value": desired["value"],
            "unit": desired["unit"],
            "source": desired["source"],
            "source_priority": get_priority(desired["source"]),
            "source_row_key": desired_key,
        }
        comparable_before = {key: before[key] for key in after}
        if comparable_before != after:
            changes.append(Change("canonical_financials", str(row["id"]), metric, before, after))
    return changes


def _fetch_canonical(api: Any, *, ticker: str, fiscal_year_end: str, quarter: str, filing_id: str) -> list[dict[str, Any]]:
    return api.get("canonical_financials", {
        "select": "id,ticker,period,quarter,metric,value,unit,source,source_priority,filing_id,source_row_key,updated_at",
        "ticker": f"eq.{ticker}",
        "period": f"eq.{fiscal_year_end}",
        "quarter": f"eq.{quarter}",
        "filing_id": f"eq.{filing_id}",
        "order": "id.asc",
    })


def _fetch_financials(api: Any, *, ticker: str, fiscal_year_end: str, quarter: str) -> list[dict[str, Any]]:
    return api.get("financials", {
        "select": "ticker,period,quarter,sales,gross_profit,operating_profit,source,unit,updated_at",
        "ticker": f"eq.{ticker}",
        "period": f"eq.{fiscal_year_end}",
        "quarter": f"eq.{quarter}",
    })


def build_financials_plan(
    rows: list[dict[str, Any]],
    *,
    ticker: str,
    fiscal_year_end: str,
    quarter: str,
    desired_metrics: dict[str, dict[str, Any]],
) -> list[Change]:
    if len(rows) != 1:
        raise ValueError(f"expected exactly one financials row, found {len(rows)}")
    row = rows[0]
    primary_key = f"{ticker}|{fiscal_year_end}|{quarter}"
    changes: list[Change] = []
    for metric, desired in desired_metrics.items():
        if metric not in _SQLITE_COLUMNS or metric not in row:
            continue
        before_value = row.get(metric)
        after_value = desired["value"]
        if before_value != after_value:
            changes.append(Change(
                "financials",
                primary_key,
                metric,
                {
                    "value": before_value,
                    "unit": row.get("unit"),
                    "source": row.get("source"),
                    "updated_at": row.get("updated_at"),
                },
                {"value": after_value, "unit": "million_yen"},
            ))
    return changes


def build_sqlite_plan(
    db_path: str,
    *,
    ticker: str,
    fiscal_year_end: str,
    quarter: str,
    filing_id: str,
    desired_metrics: dict[str, dict[str, Any]],
) -> tuple[int, list[Change]]:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    columns = {r[1] for r in conn.execute("PRAGMA table_info(quarterly_results)")}
    selected_columns = [c for c in _SQLITE_COLUMNS.values() if c in columns]
    rows = conn.execute(
        "SELECT id, company_code, fiscal_year_end, quarter, source_doc_id, unit, "
        + ", ".join(selected_columns)
        + " FROM quarterly_results WHERE company_code = ? AND fiscal_year_end = ? "
          "AND quarter = ? AND source_doc_id = ?",
        (ticker, fiscal_year_end, quarter, filing_id),
    ).fetchall()
    conn.close()
    if len(rows) != 1:
        raise ValueError(f"expected exactly one SQLite row, found {len(rows)}")
    row = dict(rows[0])
    changes: list[Change] = []
    for metric, desired in desired_metrics.items():
        column = _SQLITE_COLUMNS.get(metric)
        if not column or column not in columns:
            continue
        before_value = row.get(column)
        after_value = desired["value"]
        if before_value != after_value:
            changes.append(Change(
                "quarterly_results",
                str(row["id"]),
                metric,
                {"value": before_value, "unit": row.get("unit")},
                {"value": after_value, "unit": "百万円"},
            ))
    return int(row["id"]), changes


def apply_sqlite(db_path: str, row_id: int, changes: list[Change]) -> int:
    if not changes:
        return 0
    assignments: list[str] = []
    params: list[Any] = []
    guards: list[str] = ["id = ?"]
    guard_params: list[Any] = [row_id]
    for change in changes:
        column = _SQLITE_COLUMNS[change.metric]
        assignments.append(f"{column} = ?")
        params.append(change.after["value"])
        guards.append(f"{column} IS ?")
        guard_params.append(change.before["value"])
    assignments.append("unit = ?")
    params.append("百万円")
    params.extend(guard_params)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            f"UPDATE quarterly_results SET {', '.join(assignments)} "
            f"WHERE {' AND '.join(guards)}",
            params,
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"SQLite optimistic update affected {cursor.rowcount} rows")
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_canonical(api: Any, changes: list[Change]) -> int:
    count = 0
    now = datetime.now(JST).isoformat()
    for change in changes:
        result = api.patch(
            "canonical_financials",
            {"id": change.primary_key, "updated_at": change.before["updated_at"]},
            {**change.after, "updated_at": now},
        )
        if len(result) != 1:
            raise RuntimeError(
                f"canonical optimistic update for id={change.primary_key} affected {len(result)} rows"
            )
        count += 1
    return count


def apply_financials(
    api: Any,
    changes: list[Change],
    *,
    ticker: str,
    fiscal_year_end: str,
    quarter: str,
) -> int:
    if not changes:
        return 0
    updated_at_values = {change.before["updated_at"] for change in changes}
    if len(updated_at_values) != 1:
        raise RuntimeError("financials changes do not share one optimistic guard")
    payload = {change.metric: change.after["value"] for change in changes}
    payload.update({"unit": "million_yen", "updated_at": datetime.now(JST).isoformat()})
    result = api.patch(
        "financials",
        {
            "ticker": ticker,
            "period": fiscal_year_end,
            "quarter": quarter,
            "updated_at": next(iter(updated_at_values)),
        },
        payload,
    )
    if len(result) != 1:
        raise RuntimeError(f"financials optimistic update affected {len(result)} rows")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True, dest="zip_path")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--fiscal-year-end", required=True)
    parser.add_argument("--period-start", required=True)
    parser.add_argument("--period-end", required=True)
    parser.add_argument("--quarter", required=True, choices=("1Q", "2Q", "3Q", "FY"))
    parser.add_argument("--filing-id", required=True)
    parser.add_argument("--metrics", nargs="+", required=True, choices=tuple(_METRIC_ATTRS))
    parser.add_argument("--sqlite-db")
    parser.add_argument("--env-file")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_period(
        fiscal_year_end=args.fiscal_year_end,
        period_start=args.period_start,
        period_end=args.period_end,
        quarter=args.quarter,
    )
    from src.common_ticker import normalize_ticker
    from src.extractor import _extract_from_xbrl
    from tools.fix_unit_mismatch import API

    ticker = normalize_ticker(args.ticker)
    extracted = _extract_from_xbrl(args.zip_path)
    if extracted is None:
        raise RuntimeError("XBRL extraction returned no financials")
    if extracted.quarter and extracted.quarter != args.quarter:
        raise ValueError(f"XBRL quarter is {extracted.quarter}, requested {args.quarter}")
    desired = extracted_metrics(extracted, args.metrics)

    _load_env_file(args.env_file)
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    api = API(url, key)
    rows = _fetch_canonical(
        api,
        ticker=ticker,
        fiscal_year_end=args.fiscal_year_end,
        quarter=args.quarter,
        filing_id=args.filing_id,
    )
    canonical_changes = build_canonical_plan(
        rows,
        ticker=ticker,
        fiscal_year_end=args.fiscal_year_end,
        quarter=args.quarter,
        filing_id=args.filing_id,
        desired_metrics=desired,
    )
    financials_rows = _fetch_financials(
        api,
        ticker=ticker,
        fiscal_year_end=args.fiscal_year_end,
        quarter=args.quarter,
    )
    financials_changes = build_financials_plan(
        financials_rows,
        ticker=ticker,
        fiscal_year_end=args.fiscal_year_end,
        quarter=args.quarter,
        desired_metrics=desired,
    )
    sqlite_row_id = None
    sqlite_changes: list[Change] = []
    if args.sqlite_db:
        sqlite_row_id, sqlite_changes = build_sqlite_plan(
            args.sqlite_db,
            ticker=ticker,
            fiscal_year_end=args.fiscal_year_end,
            quarter=args.quarter,
            filing_id=args.filing_id,
            desired_metrics=desired,
        )

    report = {
        "mode": "APPLY" if args.apply else "DRY-RUN",
        "scope": {
            "ticker": ticker,
            "fiscal_year_end": args.fiscal_year_end,
            "period_start": args.period_start,
            "period_end": args.period_end,
            "quarter": args.quarter,
            "filing_id": args.filing_id,
            "metrics": args.metrics,
        },
        "desired": desired,
        "changes": [asdict(c) for c in [*sqlite_changes, *financials_changes, *canonical_changes]],
        "change_count": len(sqlite_changes) + len(financials_changes) + len(canonical_changes),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    sqlite_count = apply_sqlite(args.sqlite_db, sqlite_row_id, sqlite_changes) if args.sqlite_db else 0
    financials_count = apply_financials(
        api,
        financials_changes,
        ticker=ticker,
        fiscal_year_end=args.fiscal_year_end,
        quarter=args.quarter,
    )
    canonical_count = apply_canonical(api, canonical_changes)
    print(json.dumps({
        "sqlite_updated": sqlite_count,
        "financials_updated": financials_count,
        "canonical_updated": canonical_count,
        "total_updated": sqlite_count + financials_count + canonical_count,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
