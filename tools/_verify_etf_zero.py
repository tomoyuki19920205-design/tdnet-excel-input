#!/usr/bin/env python3
"""Broad, read-only Production audit for every ETF-like master security."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.events.env_loader import load_project_env
from src.events.tdnet_event_store import _get_supabase
from src.security_eligibility import (
    ETF_LIKE_PRODUCT_CATEGORIES,
    classify_security_eligibility,
)

MASTER_DB = ROOT / "data" / "jquants.db"
LOCAL_DBS = (ROOT / "decision_db.db", ROOT / "data" / "state.db")


def _all_rows(client, table: str, columns: str) -> list[dict]:
    rows = []
    size = 1000
    for start in range(0, 1_000_000, size):
        batch = client.table(table).select(columns).range(start, start + size - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < size:
            return rows
    raise RuntimeError(f"pagination limit reached: {table}")


def _latest_master_etfs() -> tuple[str, list[str]]:
    conn = sqlite3.connect(f"file:{MASTER_DB.resolve()}?mode=ro", uri=True)
    try:
        latest = str(conn.execute("SELECT MAX(date) FROM market_data_universe").fetchone()[0])
        marks = ",".join("?" for _ in ETF_LIKE_PRODUCT_CATEGORIES)
        rows = conn.execute(
            f"SELECT DISTINCT ticker FROM market_data_universe "
            f"WHERE date=? AND product_category IN ({marks}) ORDER BY ticker",
            (latest, *sorted(ETF_LIKE_PRODUCT_CATEGORIES)),
        ).fetchall()
        return latest, [str(row[0]) for row in rows]
    finally:
        conn.close()


def _master_rows(client, table: str, tickers: list[str]) -> list[dict]:
    rows = []
    for offset in range(0, len(tickers), 40):
        chunk = tickers[offset:offset + 40]
        rows.extend(client.table(table).select("*").in_("ticker", chunk).execute().data or [])
    return rows


def main() -> int:
    load_project_env()
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase unavailable")

    events = _all_rows(
        client, "tdnet_events",
        "id,ticker,company_name,source_title,headline,disclosed_at,event_type,source_url",
    )
    etf_events = []
    for row in events:
        decision = classify_security_eligibility(
            row.get("ticker"),
            as_of_date=str(row.get("disclosed_at") or "")[:10],
            title=row.get("source_title") or row.get("headline"),
            company_name=row.get("company_name"),
        )
        if decision.is_etf_like:
            etf_events.append({
                "id": row.get("id"), "ticker": row.get("ticker"),
                "source": decision.source, "authoritative": decision.authoritative,
            })

    master_date, etf_tickers = _latest_master_etfs()
    remote = {
        table: _master_rows(client, table, etf_tickers)
        for table in ("canonical_financials", "financials", "canonical_segments")
    }
    local = {}
    for path in LOCAL_DBS:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "earnings_summaries" not in tables:
                local[str(path)] = 0
            else:
                marks = ",".join("?" for _ in etf_tickers)
                local[str(path)] = int(conn.execute(
                    f"SELECT COUNT(*) FROM earnings_summaries WHERE ticker IN ({marks})",
                    etf_tickers,
                ).fetchone()[0])
        finally:
            conn.close()

    counts = {
        "etf_events": len(etf_events),
        "etf_notifications": len(etf_events),
        "etf_local_summaries": sum(local.values()),
        "etf_canonical_rows": len(remote["canonical_financials"]),
        "etf_financials_rows": len(remote["financials"]),
        "etf_canonical_segments": len(remote["canonical_segments"]),
    }
    result = {
        "scope": "all_tdnet_events_plus_all_latest_master_etf_tickers",
        "all_tdnet_events_scanned": len(events),
        "master_snapshot": master_date,
        "master_etf_tickers_scanned": len(etf_tickers),
        "counts": counts,
        "event_details": etf_events,
        "remote_details": remote,
        "local_details": local,
        "ok": all(value == 0 for value in counts.values()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
