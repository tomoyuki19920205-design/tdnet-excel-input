#!/usr/bin/env python3
"""Read-only TDNET forecast screening and J-Quants reconciliation manifests."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common_ticker import normalize_ticker
from tools.sync_financials import read_forecast_rows

FORECAST_SOURCES = (
    "jquants_nxf",
    "jquants_forecast_fy",
    "jquants_forecast_next_fy",
    "jquants_forecast",
    "tdnet_forecast",
)
JQUANTS_SOURCES = FORECAST_SOURCES[:-1]
VIEWER_METRICS = ("sales", "gross_profit", "operating_profit", "profit_before_tax")


def _load_env() -> None:
    for path in (ROOT / ".env.local", ROOT / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _ratio_bucket(tdnet: float, jquants: float) -> str:
    if jquants == 0:
        return "jq_zero"
    ratio = abs(tdnet / jquants)
    if ratio >= 1000:
        return ">=1000x"
    if ratio >= 100:
        return "100-1000x"
    if ratio >= 10:
        return "10-100x"
    if ratio > 0.1:
        return "0.1-10x"
    if ratio > 0.01:
        return "0.01-0.1x"
    if ratio > 0.001:
        return "0.001-0.01x"
    return "<=0.001x"


def _add_one_year(period: str) -> str:
    parts = period.split("-")
    if len(parts) != 3:
        return period
    return f"{int(parts[0]) + 1:04d}-{parts[1]}-{parts[2]}"


def _fetch_canonical() -> list[dict]:
    url = os.environ.get("SUPABASE_POSTGRES_URL")
    if not url:
        raise RuntimeError("SUPABASE_POSTGRES_URL is required")
    connection = psycopg2.connect(url, connect_timeout=8)
    connection.set_session(readonly=True, autocommit=False)
    try:
        cursor = connection.cursor()
        cursor.execute("SET LOCAL statement_timeout='15000ms'")
        cursor.execute(
            """
            SELECT ticker,period,quarter,metric,value,source,source_priority,
                   filing_id,disclosure_datetime,recency_key,updated_at,source_row_key
            FROM public.canonical_financials
            WHERE source = ANY(%s)
            ORDER BY ticker,period,quarter,metric,source_priority ASC,
                     recency_key DESC,updated_at DESC,source ASC
            """,
            (list(FORECAST_SOURCES),),
        )
        columns = [item[0] for item in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        connection.rollback()
        return rows
    finally:
        connection.close()


def _local_jquants_context(tickers: set[str]) -> tuple[dict[str, str], set[str], dict[str, list[dict]]]:
    db_path = ROOT / "data" / "jquants.db"
    connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in tickers)
        local_codes = sorted({ticker + "0" for ticker in tickers})
        companies: dict[str, str] = {}
        for row in connection.execute(
            f"""
            SELECT ticker,company_name FROM market_data_universe
            WHERE ticker IN ({placeholders}) AND company_name IS NOT NULL
            ORDER BY date DESC
            """,
            sorted(tickers),
        ):
            companies.setdefault(normalize_ticker(row["ticker"]), row["company_name"])
        per_share = {
            normalize_ticker(row["ticker"])
            for row in connection.execute(
                f"SELECT DISTINCT ticker FROM per_share_data WHERE ticker IN ({placeholders})",
                sorted(tickers),
            )
        }
        raw_by_ticker: dict[str, list[dict]] = defaultdict(list)
        raw_placeholders = ",".join("?" for _ in local_codes)
        for row in connection.execute(
            f"""
            SELECT local_code,current_fiscal_year_end_date,type_of_current_period,
                   disclosed_date,raw_json
            FROM jquants_financials_normalized
            WHERE local_code IN ({raw_placeholders})
            """,
            local_codes,
        ):
            payload = json.loads(row["raw_json"] or "{}")
            forecast_fields = {
                key: value for key, value in payload.items()
                if value is not None and (
                    key in {"FSales", "FOP", "NxFSales", "NxFOP"}
                    or "forecast" in key.lower()
                )
            }
            raw_by_ticker[normalize_ticker(row["local_code"])].append({
                "period": row["current_fiscal_year_end_date"],
                "quarter": row["type_of_current_period"],
                "disclosed_date": row["disclosed_date"],
                "forecast_fields": forecast_fields,
            })
        return companies, per_share, raw_by_ticker
    finally:
        connection.close()


def main() -> int:
    _load_env()
    rows = _fetch_canonical()
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["ticker"], row["period"], row["quarter"], row["metric"])].append(row)
    winners = {key: candidates[0] for key, candidates in grouped.items()}
    jq_winners = {
        key: next((row for row in candidates if row["source"] in JQUANTS_SOURCES), None)
        for key, candidates in grouped.items()
    }
    tdnet_rows = [row for row in rows if row["source"] == "tdnet_forecast"]

    ratio_buckets: Counter[str] = Counter()
    abnormal: list[dict] = []
    tdnet_sales = {
        (row["ticker"], row["period"], row["quarter"]): float(row["value"])
        for row in tdnet_rows if row["metric"] == "sales" and row["value"] is not None
    }
    for row in tdnet_rows:
        value = float(row["value"]) if row["value"] is not None else None
        key = (row["ticker"], row["period"], row["quarter"], row["metric"])
        jq = jq_winners.get(key)
        reasons: list[str] = []
        ratio = None
        if jq and jq["value"] is not None and value is not None:
            jq_value = float(jq["value"])
            ratio = None if jq_value == 0 else abs(value / jq_value)
            ratio_buckets[_ratio_bucket(value, jq_value)] += 1
            if ratio is not None and (ratio >= 100 or ratio <= 0.01):
                reasons.append("jquants_ratio_100x")
        if value is not None:
            if row["metric"] == "sales":
                if value <= 0:
                    reasons.append("sales_non_positive")
                if 0 < abs(value) < 0.01:
                    reasons.append("sales_extremely_small")
                if abs(value) > 100_000_000:
                    reasons.append("sales_over_100_trillion_yen")
            if row["metric"] == "operating_profit":
                if 0 < abs(value) < 0.01:
                    reasons.append("operating_profit_extremely_small")
                if abs(value) > 100_000_000:
                    reasons.append("operating_profit_over_100_trillion_yen")
                sales = tdnet_sales.get((row["ticker"], row["period"], row["quarter"]))
                if sales not in (None, 0) and abs(value) > abs(sales) * 2:
                    reasons.append("operating_profit_over_2x_sales")
        if reasons:
            abnormal.append({
                "ticker": row["ticker"], "period": str(row["period"]),
                "quarter": row["quarter"], "metric": row["metric"],
                "tdnet_value": value, "filing_id": row["filing_id"],
                "disclosure_datetime": str(row["disclosure_datetime"] or ""),
                "jquants_source": jq["source"] if jq else None,
                "jquants_value": float(jq["value"]) if jq and jq["value"] is not None else None,
                "ratio": ratio, "reasons": reasons,
            })

    missing_keys = [
        key for key, winner in winners.items()
        if winner["source"] == "tdnet_forecast"
        and key[3] in VIEWER_METRICS
        and jq_winners.get(key) is None
    ]
    affected_tickers = {key[0] for key in missing_keys}
    companies, per_share_tickers, raw_by_ticker = _local_jquants_context(affected_tickers)
    generated = read_forecast_rows(str(ROOT / "data" / "jquants.db"), recent_days=0)
    generated_metrics: dict[tuple, dict] = {}
    generated_by_ticker_metric: dict[tuple, list[dict]] = defaultdict(list)
    for row in generated:
        for metric in VIEWER_METRICS:
            if row.get(metric) is None:
                continue
            key = (row["ticker"], row["period"], row["quarter"], metric)
            generated_metrics[key] = row
            generated_by_ticker_metric[(row["ticker"], metric)].append(row)

    reconciliation: list[dict] = []
    reason_counts: Counter[str] = Counter()
    for key in sorted(missing_keys):
        ticker, period, quarter, metric = key
        winner = winners[key]
        exact_raw = generated_metrics.get(key)
        alternatives = generated_by_ticker_metric.get((ticker, metric), [])
        raw_rows = raw_by_ticker.get(ticker, [])
        relevant_raw_keys = (
            {"FSales", "NxFSales"} if metric == "sales"
            else {"FOP", "NxFOP"} if metric == "operating_profit"
            else set()
        )
        relevant_raw_forecast = any(
            relevant_raw_keys.intersection(item["forecast_fields"])
            for item in raw_rows
        )
        raw_candidate_periods: set[str] = set()
        f_key, nxf_key = (
            ("FSales", "NxFSales") if metric == "sales" else ("FOP", "NxFOP")
        )
        for item in raw_rows:
            if item["forecast_fields"].get(f_key) not in (None, ""):
                raw_candidate_periods.add(item["period"])
            if item["forecast_fields"].get(nxf_key) not in (None, ""):
                raw_candidate_periods.add(_add_one_year(item["period"]))
        if exact_raw:
            reason = "A_JQUANTS_RAW_EXISTS_CANONICAL_MISSING"
        elif alternatives:
            reason = "C_PERIOD_MAPPING_MISMATCH_OR_DIFFERENT_FORECAST_PERIOD"
        elif str(period) in raw_candidate_periods:
            reason = "H_JQUANTS_RAW_HISTORICAL_OR_FY_ROW_NOT_CANONICALIZED"
        elif raw_candidate_periods:
            reason = "C_PERIOD_MAPPING_MISMATCH_OR_DIFFERENT_FORECAST_PERIOD"
        else:
            reason = "G_TDNET_ONLY_NO_JQUANTS_FORECAST"
        reason_counts[reason] += 1
        reconciliation.append({
            "ticker": ticker, "company": companies.get(ticker), "metric": metric,
            "period": str(period), "quarter": quarter,
            "current_tdnet_value": float(winner["value"]),
            "tdnet_disclosed_date": str(winner["disclosure_datetime"] or ""),
            "tdnet_filing_id": winner["filing_id"],
            "jquants_generated_exact": exact_raw,
            "jquants_alternative_periods": sorted({row["period"] for row in alternatives}),
            "jquants_raw_source_exists": bool(raw_rows),
            "jquants_raw_forecast_fields_exist": relevant_raw_forecast,
            "jquants_raw_candidate_periods": sorted(raw_candidate_periods),
            "jquants_per_share_source_exists": ticker in per_share_tickers,
            "canonical_jquants_row_exists": False,
            "missing_reason": reason,
        })

    def latest_periods(source_filter) -> dict[str, tuple[str, str]]:
        periods: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for key, winner in winners.items():
            ticker, period, quarter, metric = key
            if metric in VIEWER_METRICS and source_filter(key, winner):
                periods[ticker].add((str(period), quarter))
        return {ticker: max(items) for ticker, items in periods.items()}

    current_latest = latest_periods(lambda _key, _winner: True)
    jq_latest: dict[str, tuple[str, str]] = {}
    jq_periods: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for key, jq in jq_winners.items():
        if jq and key[3] in VIEWER_METRICS:
            jq_periods[key[0]].add((str(key[1]), key[2]))
    jq_latest = {ticker: max(items) for ticker, items in jq_periods.items()}
    loses_all = sorted(ticker for ticker in current_latest if ticker not in jq_latest)
    period_changes = [
        {"ticker": ticker, "company": companies.get(ticker),
         "current_latest": current_latest[ticker], "jquants_latest": jq_latest[ticker],
         "classification": (
             "JQUANTS_FALLBACK_TO_OLDER_PERIOD"
             if jq_latest[ticker] < current_latest[ticker]
             else "JQUANTS_FALLBACK_TO_NEWER_PERIOD"
         )}
        for ticker in sorted(current_latest)
        if ticker in jq_latest and current_latest[ticker] != jq_latest[ticker]
    ]

    generated_at = datetime.now(timezone.utc).isoformat()
    timeout_partial_commit = [
        {
            "ticker": row["ticker"], "period": str(row["period"]),
            "quarter": row["quarter"], "metric": row["metric"],
            "value": float(row["value"]) if row["value"] is not None else None,
            "filing_id": row["filing_id"], "source_row_key": row["source_row_key"],
            "updated_at": str(row["updated_at"]), "cleanup_action": "delete_with_tdnet_forecast",
        }
        for row in tdnet_rows
        if str(row["updated_at"]).startswith("2026-08-13 12:52:41.724237")
    ]
    screening = {
        "generated_at": generated_at, "mode": "read_only",
        "tdnet_rows": len(tdnet_rows),
        "ratio_comparable_rows": sum(ratio_buckets.values()),
        "ratio_buckets": dict(sorted(ratio_buckets.items())),
        "abnormal_rows": len(abnormal),
        "abnormal_tickers": len({row["ticker"] for row in abnormal}),
        "abnormal_reason_counts": dict(Counter(reason for row in abnormal for reason in row["reasons"])),
        "timeout_partial_commit_rows": len(timeout_partial_commit),
        "timeout_partial_commit_records": timeout_partial_commit,
        "records": abnormal,
    }
    reconciliation_manifest = {
        "generated_at": generated_at, "mode": "read_only",
        "missing_viewer_keys": len(reconciliation),
        "missing_reason_counts": dict(reason_counts),
        "forecast_loses_all_tickers": len(loses_all),
        "loses_all_tickers": [
            {"ticker": ticker, "company": companies.get(ticker)} for ticker in loses_all
        ],
        "latest_period_changes": len(period_changes),
        "period_change_records": period_changes,
        "records": reconciliation,
    }
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "tdnet_forecast_abnormal_screening_20260814.json").write_text(
        json.dumps(screening, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (artifacts / "forecast_jquants_reconciliation_20260814.json").write_text(
        json.dumps(reconciliation_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({
        "screening": {key: value for key, value in screening.items() if key != "records"},
        "reconciliation": {
            key: value for key, value in reconciliation_manifest.items()
            if key not in {"records", "loses_all_tickers", "period_change_records"}
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
