#!/usr/bin/env python3
"""Repair PDF-fallback PL rows from the matching official XBRL summary.

The tool is intentionally narrow: it selects only rows created on a supplied
date whose field provenance is PDF, verifies that a same-period earnings
summary has an XBRL archive, and changes only PL fields that differ.  It never
touches event/notification rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.events.summary_financials import extract_earnings_data


METRICS = {
    "sales": "sales_current",
    "gross_profit": "gross_profit_current",
    "operating_profit": "op_current",
}


def official_values(summary: sqlite3.Row, repo_root: Path) -> dict[str, float | None]:
    archive = Path(summary["archive_path"])
    if not archive.is_absolute():
        archive = repo_root / archive
    parsed = extract_earnings_data(str(archive), ticker=summary["ticker"], include_evidence=True)
    if parsed is None or parsed.source != "xbrl":
        raise RuntimeError(f"official XBRL extraction failed: {archive}")
    return {
        metric: (getattr(parsed, attr) / 1_000_000 if getattr(parsed, attr) is not None else None)
        for metric, attr in METRICS.items()
    }


def build_plan(conn: sqlite3.Connection, date: str, repo_root: Path) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM quarterly_results
           WHERE date(created_at)=? AND field_sources LIKE '%pdf%'""", (date,)
    ).fetchall()
    plan = []
    for row in rows:
        year = row["fiscal_year_end"][:4]
        summaries = conn.execute(
            """SELECT * FROM earnings_summaries
               WHERE ticker IN (?, ?) AND fiscal_year=? AND quarter=?
                 AND archive_path IS NOT NULL AND archive_path != ''
               ORDER BY id DESC""",
            (row["company_code"], row["company_code"].rstrip("0"), year, row["quarter"]),
        ).fetchall()
        if len(summaries) != 1:
            continue
        values = official_values(summaries[0], repo_root)
        changed = {m: values[m] for m in METRICS if values[m] is not None and row[m] != values[m]}
        if changed:
            plan.append({
                "id": row["id"], "ticker": row["company_code"].rstrip("0"),
                "period": row["fiscal_year_end"], "quarter": row["quarter"],
                "source_doc_id": row["source_doc_id"], "before": {m: row[m] for m in METRICS},
                "after": values, "changed": changed,
            })
    return plan


def apply_sqlite(conn: sqlite3.Connection, plan: list[dict]) -> None:
    for item in plan:
        sources = {metric: "official_xbrl_repair" for metric, value in item["after"].items() if value is not None}
        conn.execute(
            """UPDATE quarterly_results
               SET sales=?, gross_profit=?, operating_profit=?, field_sources=?, updated_at=datetime('now','localtime')
               WHERE id=?""",
            (item["after"]["sales"], item["after"]["gross_profit"], item["after"]["operating_profit"], json.dumps(sources), item["id"]),
        )
    conn.commit()


def apply_supabase(plan: list[dict], env_file: Path) -> None:
    load_dotenv(env_file)
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for item in plan:
        payload = {"ticker": item["ticker"], "period": item["period"], "quarter": item["quarter"], **item["after"], "unit": "million_yen", "source": "tdnet"}
        response = requests.post(url + "/financials?on_conflict=ticker,period,quarter", headers={**headers, "Prefer": "resolution=merge-duplicates"}, json=payload, timeout=30)
        response.raise_for_status()
        # Remove only the superseded low-confidence PDF facts.  XBRL-backed
        # canonical rows remain the source of truth; event rows are untouched.
        response = requests.delete(url + "/canonical_financials", headers=headers, params={
            "ticker": f"eq.{item['ticker']}", "period": f"eq.{item['period']}",
            "quarter": f"eq.{item['quarter']}", "source": "eq.pdf",
        }, timeout=30)
        response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--date", default="2026-08-03")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    plan = build_plan(conn, args.date, Path(args.repo_root))
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.apply:
        apply_sqlite(conn, plan)
        apply_supabase(plan, Path(args.env_file))
    conn.close()


if __name__ == "__main__":
    main()
