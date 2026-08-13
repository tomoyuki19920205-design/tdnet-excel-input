#!/usr/bin/env python3
"""Read-only manifest for auditing historical canonical forecast candidates."""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import (  # noqa: E402
    get_supabase_read_config,
    load_env,
)
from lib.pipeline.forecast_sync import (  # noqa: E402
    ForecastDTO,
    _as_utc_key,
    expand_forecast_rows,
    load_earnings_forecasts,
    load_revision_forecasts,
    select_latest_forecasts,
)
from tools.sync_financials import read_forecast_rows  # noqa: E402

JST = timezone(timedelta(hours=9))
FORECAST_SOURCES = (
    "jquants_nxf", "jquants_forecast_fy", "jquants_forecast_next_fy",
    "jquants_forecast", "tdnet_forecast",
)
VIEW_METRICS = ("sales", "operating_profit", "ordinary_profit", "net_income")
logger = logging.getLogger("repair_forecast_canonical")


def _paged_select(table: str, params: dict, *, page_size: int = 1000) -> list[dict]:
    import requests

    rows: list[dict] = []
    offset = 0
    config = get_supabase_read_config()
    while True:
        page_params = {**params, "limit": str(page_size), "offset": str(offset)}
        response = None
        for attempt in range(2):
            try:
                response = requests.get(
                    f"{config['rest_url']}/{table}", params=page_params,
                    headers=config["headers"], timeout=45,
                )
                break
            except requests.RequestException:
                if attempt:
                    raise
                time.sleep(2)
        assert response is not None
        if response.status_code != 200:
            raise RuntimeError(
                f"Supabase SELECT {table} failed: {response.status_code} {response.text[:300]}"
            )
        page = response.json()
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


def _load_candidates(db_path: str, jquants_db_path: str) -> tuple[list[ForecastDTO], list[dict], int]:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        earnings_documents = conn.execute(
            "SELECT count(*) FROM earnings_summaries "
            "WHERE guidance_sales IS NOT NULL OR guidance_op IS NOT NULL"
        ).fetchone()[0]
        revision_documents = conn.execute(
            "SELECT count(*) FROM events WHERE event_type='forecast_revision'"
        ).fetchone()[0]
        earnings, earnings_quarantine = load_earnings_forecasts(conn)
        revisions, revision_quarantine = load_revision_forecasts(conn)
    finally:
        conn.close()

    jq_candidates: list[ForecastDTO] = []
    for row in read_forecast_rows(jquants_db_path, recent_days=0):
        for metric in ("sales", "operating_profit"):
            value = row.get(metric)
            if value is None:
                continue
            jq_candidates.append(ForecastDTO(
                ticker=row["ticker"],
                forecast_period_end=row["period"],
                metric=metric,
                value=float(value),
                disclosure_datetime=row.get("disclosure_datetime") or "",
                filing_id="",
                source=row["source"],
                correction_flag=False,
                forecast_horizon=("next_fy" if row["source"] == "jquants_nxf" else "current_fy"),
                accounting_standard="UNKNOWN",
                document_type="jquants_forecast",
            ))
    return (
        earnings + revisions + jq_candidates,
        earnings_quarantine + revision_quarantine,
        int(earnings_documents) + int(revision_documents),
    )


def _current_remote(
    *, include_view: bool = True
) -> tuple[dict[str, dict], dict[tuple[str, str, str], float | None]]:
    source_filter = "in.(" + ",".join(FORECAST_SOURCES) + ")"
    canonical = _paged_select(
        "canonical_financials",
        {
            "select": "source_row_key,ticker,period,quarter,metric,value,unit,source,source_priority,"
                      "filing_id,disclosure_datetime,correction_flag,recency_key",
            "source": source_filter,
            "order": "source_row_key.asc",
        },
    )
    view = []
    if include_view:
        view = _paged_select(
            "api_latest_financials_canonical_forecast",
            {
                "select": "ticker,period,quarter,sales,operating_profit,ordinary_profit,net_income,source,updated_at",
                "order": "ticker.asc,period.asc",
            },
        )
    source_rows = {str(row.get("source_row_key")): row for row in canonical}
    current_values: dict[tuple[str, str, str], float | None] = {}
    for row in view:
        for metric in VIEW_METRICS:
            current_values[(str(row.get("ticker")), str(row.get("period")), metric)] = row.get(metric)
    return source_rows, current_values


def _current_from_applied_manifest(
    path: str,
) -> tuple[dict[str, dict], dict[tuple[str, str, str], float | None]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    source_rows: dict[str, dict] = {}
    current_values: dict[tuple[str, str, str], float | None] = {}
    for record in payload.get("records", []):
        row = {
            "ticker": record["ticker"], "period": record["forecast_period_end"],
            "quarter": "FY", "metric": record["metric"],
            "value": record["proposed_value"], "unit": "millions_jpy",
            "source": record["source"], "source_priority": 10,
            "filing_id": record.get("filing_id") or "",
            "disclosure_datetime": record.get("disclosure_datetime") or "",
            "correction_flag": record.get("correction_flag", False),
        }
        source_rows[record["source_row_key"]] = row
        current_values[(row["ticker"], row["period"], row["metric"])] = row["value"]
    return source_rows, current_values


def _same_semantics(current: dict, proposed: dict) -> bool:
    fields = (
        "ticker", "period", "quarter", "metric", "value", "unit", "source",
        "source_priority", "filing_id", "disclosure_datetime", "correction_flag",
    )
    for field in fields:
        current_value = current.get(field)
        proposed_value = proposed.get(field)
        if field == "disclosure_datetime":
            if _as_utc_key(str(current_value or "")) != _as_utc_key(str(proposed_value or "")):
                return False
        elif field == "filing_id":
            if str(current_value or "") != str(proposed_value or ""):
                return False
        elif current_value != proposed_value:
            return False
    return True


def build_manifest(
    db_path: str, jquants_db_path: str, tickers: set[str] | None = None,
    *, include_view: bool = True, baseline_manifest: str = "",
) -> tuple[dict, list[dict]]:
    candidates, quarantine, documents_scanned = _load_candidates(db_path, jquants_db_path)
    if tickers:
        candidates = [candidate for candidate in candidates if candidate.ticker in tickers]
        quarantine = [item for item in quarantine if str(item.get("ticker")) in tickers]
    winners = select_latest_forecasts(candidates)
    proposed_rows = expand_forecast_rows(winners)
    if baseline_manifest:
        source_rows, current_values = _current_from_applied_manifest(baseline_manifest)
    else:
        source_rows, current_values = _current_remote(include_view=include_view)

    records: list[dict] = []
    counts = {"would_insert": 0, "would_update": 0, "unchanged": 0}
    for dto, row in zip(winners, proposed_rows):
        current_source = source_rows.get(row["source_row_key"])
        if current_source is None:
            action = "insert"
            counts["would_insert"] += 1
        elif _same_semantics(current_source, row):
            action = "unchanged"
            counts["unchanged"] += 1
        else:
            action = "update"
            counts["would_update"] += 1
        records.append({
            "ticker": dto.ticker,
            "forecast_period_end": dto.forecast_period_end,
            "metric": dto.metric,
            "disclosure_datetime": dto.disclosure_datetime,
            "source": dto.source,
            "filing_id": dto.filing_id,
            "correction_flag": dto.correction_flag,
            "canonical_current_value": current_values.get(
                (dto.ticker, dto.forecast_period_end, dto.metric)
            ),
            "proposed_value": dto.value,
            "action": action,
            "source_row_key": row["source_row_key"],
        })
    summary = {
        "generated_at": datetime.now(JST).isoformat(),
        "mode": "read_only_manifest",
        "documents_scanned": documents_scanned,
        "forecast_candidates": len(candidates),
        "unique_forecast_keys": len(winners),
        **counts,
        "quarantine": len(quarantine),
        "mapping_failures": len(quarantine),
        "tickers": sorted(tickers) if tickers else None,
        "quarantine_rows": quarantine,
    }
    return {"summary": summary, "records": records}, proposed_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "decision_db.db"))
    parser.add_argument("--jquants-db", default=str(ROOT / "data" / "jquants.db"))
    parser.add_argument("--tickers", default="")
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "forecast_backlog_manifest.json"))
    parser.add_argument("--skip-view-readback", action="store_true")
    parser.add_argument("--baseline-manifest", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(str(ROOT))
    tickers = {item.strip().upper() for item in args.tickers.split(",") if item.strip()} or None
    manifest, _proposed_rows = build_manifest(
        args.db, args.jquants_db, tickers,
        include_view=not args.skip_view_readback,
        baseline_manifest=args.baseline_manifest,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    printable_summary = {
        key: value for key, value in manifest["summary"].items() if key != "quarantine_rows"
    }
    print(json.dumps(printable_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
