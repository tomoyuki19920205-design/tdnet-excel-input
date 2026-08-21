#!/usr/bin/env python3
"""Targeted cleanup for the 2026-08-21 11:00 ETF earnings incident.

The command is dry-run by default.  ``--apply`` requires exact preflight
counts, authoritative ETF classification, a JSON export, a full SQLite backup,
matching SHA-256 hashes and successful SQLite integrity checks.  Raw TDnet
documents and processed-disclosure history are deliberately left untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config
from src.events.env_loader import load_project_env
from src.events.tdnet_event_store import _get_supabase
from src.security_eligibility import classify_security_eligibility

TARGET_DATE = "2026-08-21"
TARGET_PERIOD = "2026-07-31"
TARGET_TICKERS = (
    "1482", "1496", "1497", "1656", "2012", "2255", "2256",
    "2257", "2258", "2259", "236A", "237A", "238A",
)
TARGET_SOURCE_URLS = frozenset({
    "https://www.release.tdnet.info/inbs/140120260821524027.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524084.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524087.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524109.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524113.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524115.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524122.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524125.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524144.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524150.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524156.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524163.pdf",
    "https://www.release.tdnet.info/inbs/140120260821524167.pdf",
})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _git_value(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, text=True,
        capture_output=True, check=False,
    )
    return result.stdout.strip()


def _query_local_earnings(db_path: Path) -> tuple[list[str], list[dict]]:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(earnings_summaries)")]
        marks = ",".join("?" for _ in TARGET_TICKERS)
        rows = conn.execute(
            f"SELECT * FROM earnings_summaries WHERE ticker IN ({marks}) "
            "AND disclosure_date=? ORDER BY ticker",
            (*TARGET_TICKERS, TARGET_DATE),
        ).fetchall()
        return columns, [dict(row) for row in rows]
    finally:
        conn.close()


def _collect(client, decision_db: Path) -> dict:
    events = (
        client.table("tdnet_events")
        .select("*")
        .in_("ticker", list(TARGET_TICKERS))
        .gte("disclosed_at", "2026-08-20T15:00:00Z")
        .lt("disclosed_at", "2026-08-21T15:00:00Z")
        .execute().data or []
    )
    event_ids = [row["id"] for row in events]
    references = {}
    for table in ("tdnet_event_reads", "tdnet_event_stars", "tdnet_event_comments"):
        references[table] = (
            client.table(table).select("*").in_("event_id", event_ids).execute().data or []
        ) if event_ids else []

    canonical = (
        client.table("canonical_financials").select("*")
        .in_("ticker", list(TARGET_TICKERS))
        .eq("period", TARGET_PERIOD).eq("quarter", "FY")
        .eq("source", "jquants_earnings_summary")
        .execute().data or []
    )
    financials = (
        client.table("financials").select("*")
        .in_("ticker", list(TARGET_TICKERS))
        .eq("period", TARGET_PERIOD).eq("quarter", "FY")
        .execute().data or []
    )
    try:
        segments = (
            client.table("canonical_segments").select("*")
            .in_("ticker", list(TARGET_TICKERS))
            .eq("period", TARGET_PERIOD).eq("quarter", "FY")
            .execute().data or []
        )
    except Exception as exc:
        segments = [{"query_error": f"{type(exc).__name__}:{exc}"}]

    local_columns, local_earnings = _query_local_earnings(decision_db)
    return {
        "tdnet_events": events,
        "references": references,
        "canonical_financials": canonical,
        "financials": financials,
        "canonical_segments": segments,
        "local_earnings_columns": local_columns,
        "local_earnings_summaries": local_earnings,
    }


def _validate(snapshot: dict) -> dict:
    classifications = {
        ticker: classify_security_eligibility(ticker, as_of_date=TARGET_DATE)
        for ticker in TARGET_TICKERS
    }
    failures = []
    for ticker, decision in classifications.items():
        if not (decision.is_etf_like and decision.authoritative):
            failures.append(f"{ticker}:not_authoritative_etf")

    events = snapshot["tdnet_events"]
    if len(events) != 13:
        failures.append(f"tdnet_events_count={len(events)} expected=13")
    if {row.get("ticker") for row in events} != set(TARGET_TICKERS):
        failures.append("tdnet_events_ticker_set_mismatch")
    if {row.get("source_url") for row in events} != TARGET_SOURCE_URLS:
        failures.append("tdnet_events_source_url_set_mismatch")
    if any(row.get("event_type") != "earnings" or row.get("event_subtype") != "FY" for row in events):
        failures.append("tdnet_events_type_mismatch")
    if any(not str(row.get("disclosed_at") or "").startswith("2026-08-21T02:00:00") for row in events):
        failures.append("tdnet_events_time_mismatch")

    ref_count = sum(len(rows) for rows in snapshot["references"].values())
    if ref_count:
        failures.append(f"event_reference_count={ref_count} expected=0")

    canonical = snapshot["canonical_financials"]
    if len(canonical) != 26:
        failures.append(f"canonical_count={len(canonical)} expected=26")
    expected_pairs = {(ticker, metric) for ticker in TARGET_TICKERS for metric in ("sales", "operating_profit")}
    actual_pairs = {(row.get("ticker"), row.get("metric")) for row in canonical}
    if actual_pairs != expected_pairs:
        failures.append("canonical_ticker_metric_set_mismatch")

    if snapshot["financials"]:
        failures.append(f"unexpected_financials={len(snapshot['financials'])}")
    if snapshot["canonical_segments"]:
        failures.append(f"unexpected_canonical_segments={len(snapshot['canonical_segments'])}")

    local_rows = snapshot["local_earnings_summaries"]
    if len(local_rows) != 13:
        failures.append(f"local_earnings_count={len(local_rows)} expected=13")
    if {row.get("ticker") for row in local_rows} != set(TARGET_TICKERS):
        failures.append("local_earnings_ticker_set_mismatch")

    return {
        "ok": not failures,
        "failures": failures,
        "classifications": {
            ticker: {
                "is_etf_like": decision.is_etf_like,
                "authoritative": decision.authoritative,
                "source": decision.source,
                "product_category": decision.product_category,
                "master_date": decision.master_date,
            }
            for ticker, decision in classifications.items()
        },
    }


def _delete_local_earnings(db_path: Path, fingerprints: list[str]) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in fingerprints)
        cursor = conn.execute(
            f"DELETE FROM earnings_summaries WHERE fingerprint IN ({marks})",
            fingerprints,
        )
        if cursor.rowcount != 13:
            raise RuntimeError(f"local delete count {cursor.rowcount}, expected 13")
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()

    load_project_env()
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase client unavailable")
    cfg = load_config()
    decision_db = Path(cfg.decision_db_path).resolve()

    before = _collect(client, decision_db)
    validation = _validate(before)
    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "git_branch": _git_value("branch", "--show-current"),
        "git_head": _git_value("rev-parse", "HEAD"),
        "validation": validation,
        "counts": {
            "tdnet_events": len(before["tdnet_events"]),
            "event_references": sum(len(v) for v in before["references"].values()),
            "canonical_financials": len(before["canonical_financials"]),
            "financials": len(before["financials"]),
            "canonical_segments": len(before["canonical_segments"]),
            "local_earnings_summaries": len(before["local_earnings_summaries"]),
        },
        "raw_tdnet_documents_action": "retained",
    }
    if not validation["ok"]:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else (
        PROJECT_ROOT / "artifacts" / "production_backups" / f"etf_cleanup_{stamp}"
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    sqlite_backup = backup_dir / decision_db.name
    shutil.copy2(decision_db, sqlite_backup)
    integrity_source_before = _integrity(decision_db)
    integrity_backup = _integrity(sqlite_backup)
    if integrity_source_before != "ok" or integrity_backup != "ok":
        raise RuntimeError("SQLite integrity check failed before cleanup")

    export_path = backup_dir / "pre_cleanup_rows.json"
    export_path.write_text(json.dumps(before, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    backup_manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "git_branch": summary["git_branch"],
        "git_head": summary["git_head"],
        "decision_db_source": str(decision_db),
        "decision_db_backup": str(sqlite_backup),
        "decision_db_source_sha256_before": _sha256(decision_db),
        "decision_db_backup_sha256": _sha256(sqlite_backup),
        "decision_db_source_integrity_before": integrity_source_before,
        "decision_db_backup_integrity": integrity_backup,
        "row_export": str(export_path),
        "row_export_sha256": _sha256(export_path),
        "validation": validation,
    }
    if backup_manifest["decision_db_source_sha256_before"] != backup_manifest["decision_db_backup_sha256"]:
        raise RuntimeError("SQLite backup SHA-256 mismatch")
    manifest_path = backup_dir / "backup_manifest.json"
    manifest_path.write_text(json.dumps(backup_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    canonical_ids = [row["id"] for row in before["canonical_financials"]]
    event_ids = [row["id"] for row in before["tdnet_events"]]
    fingerprints = [row["fingerprint"] for row in before["local_earnings_summaries"]]

    deleted_canonical = client.table("canonical_financials").delete().in_("id", canonical_ids).execute().data or []
    if len(deleted_canonical) != 26:
        raise RuntimeError(f"canonical delete returned {len(deleted_canonical)}, expected 26")
    deleted_events = client.table("tdnet_events").delete().in_("id", event_ids).execute().data or []
    if len(deleted_events) != 13:
        raise RuntimeError(f"event delete returned {len(deleted_events)}, expected 13")
    deleted_local = _delete_local_earnings(decision_db, fingerprints)

    after = _collect(client, decision_db)
    post_counts = {
        "tdnet_events": len(after["tdnet_events"]),
        "canonical_financials": len(after["canonical_financials"]),
        "financials": len(after["financials"]),
        "canonical_segments": len(after["canonical_segments"]),
        "local_earnings_summaries": len(after["local_earnings_summaries"]),
    }
    if any(post_counts.values()):
        raise RuntimeError(f"postflight residual rows: {post_counts}")
    integrity_after = _integrity(decision_db)
    if integrity_after != "ok":
        raise RuntimeError(f"post-cleanup SQLite integrity: {integrity_after}")

    result = {
        **summary,
        "backup_dir": str(backup_dir),
        "backup_manifest": str(manifest_path),
        "backup_manifest_sha256": _sha256(manifest_path),
        "deleted": {
            "tdnet_events": len(deleted_events),
            "canonical_financials": len(deleted_canonical),
            "local_earnings_summaries": deleted_local,
        },
        "post_counts": post_counts,
        "decision_db_integrity_after": integrity_after,
    }
    result_path = backup_dir / "cleanup_result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["cleanup_result"] = str(result_path)
    result["cleanup_result_sha256"] = _sha256(result_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
