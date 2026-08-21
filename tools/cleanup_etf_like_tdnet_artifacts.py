#!/usr/bin/env python3
"""Remove formally classified ETF/ETN artifacts from the corporate pipeline.

Dry-run is the default. ``--apply`` is intentionally guarded by an exact
production manifest, authoritative security-master classification, complete
row exports, SQLite backups with matching SHA-256 hashes, and integrity checks.
Raw TDnet documents and processed-disclosure history are retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config
from src.events.env_loader import load_project_env
from src.events.tdnet_event_store import _get_supabase
from src.security_eligibility import classify_security_eligibility

# This is an exact cleanup manifest, not an eligibility blacklist. Runtime
# eligibility always comes from the official master/TDnet classification.
TARGET_TICKERS = tuple("""
1473 1474 1482 1484 1493 1496 1497 1498 1551 1563 1596 1656 170A 2012
2014 2255 2256 2257 2258 2259 236A 237A 238A 2516 2527 2553 2556 2561
257A 258A 2620 2621 2622 2642 2649 2853 2856 2857 394A 395A 396A 435A
526A 539A 541A
""".split())
EXPECTED_EVENT_MANIFEST_SHA256 = "80153354f97d890e06382f67d00c73514805502ebcccdf49b094d78f30fad73c"
EXPECTED_COUNTS = {
    "tdnet_events": 45,
    "tdnet_event_reads": 1,
    "tdnet_event_stars": 0,
    "tdnet_event_comments": 0,
    "canonical_financials": 88,
    "financials": 0,
    "canonical_segments": 0,
    "decision_earnings_summaries": 44,
    "state_earnings_summaries": 0,
}
STATE_DB = PROJECT_ROOT / "data" / "state.db"


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


def _select_ticker_rows(client, table: str) -> list[dict]:
    rows: list[dict] = []
    page_size = 500
    for start in range(0, 10_000, page_size):
        batch = (
            client.table(table).select("*").in_("ticker", list(TARGET_TICKERS))
            .range(start, start + page_size - 1).execute().data or []
        )
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
    raise RuntimeError(f"pagination limit reached for {table}")


def _query_local_earnings(db_path: Path) -> tuple[list[str], list[dict]]:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "earnings_summaries" not in tables:
            return [], []
        columns = [row[1] for row in conn.execute("PRAGMA table_info(earnings_summaries)")]
        marks = ",".join("?" for _ in TARGET_TICKERS)
        rows = conn.execute(
            f"SELECT * FROM earnings_summaries WHERE ticker IN ({marks}) ORDER BY ticker",
            TARGET_TICKERS,
        ).fetchall()
        return columns, [dict(row) for row in rows]
    finally:
        conn.close()


def _collect(client, decision_db: Path, state_db: Path) -> dict:
    events = _select_ticker_rows(client, "tdnet_events")
    event_ids = [row["id"] for row in events]
    references = {}
    for table in ("tdnet_event_reads", "tdnet_event_stars", "tdnet_event_comments"):
        references[table] = (
            client.table(table).select("*").in_("event_id", event_ids).execute().data or []
        ) if event_ids else []

    local = {}
    for label, path in (("decision", decision_db), ("state", state_db)):
        columns, rows = _query_local_earnings(path)
        local[label] = {"path": str(path), "columns": columns, "rows": rows}
    return {
        "tdnet_events": events,
        "references": references,
        "canonical_financials": _select_ticker_rows(client, "canonical_financials"),
        "financials": _select_ticker_rows(client, "financials"),
        "canonical_segments": _select_ticker_rows(client, "canonical_segments"),
        "local": local,
    }


def _event_manifest(events: list[dict]) -> list[dict]:
    return sorted(({
        "ticker": row.get("ticker"),
        "source_url": row.get("source_url"),
        "disclosed_at": row.get("disclosed_at"),
        "event_type": row.get("event_type"),
        "event_subtype": row.get("event_subtype"),
    } for row in events), key=lambda row: (row["ticker"], row["source_url"]))


def _manifest_sha256(manifest: list[dict]) -> str:
    payload = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _counts(snapshot: dict) -> dict[str, int]:
    return {
        "tdnet_events": len(snapshot["tdnet_events"]),
        **{table: len(rows) for table, rows in snapshot["references"].items()},
        "canonical_financials": len(snapshot["canonical_financials"]),
        "financials": len(snapshot["financials"]),
        "canonical_segments": len(snapshot["canonical_segments"]),
        "decision_earnings_summaries": len(snapshot["local"]["decision"]["rows"]),
        "state_earnings_summaries": len(snapshot["local"]["state"]["rows"]),
    }


def _validate(snapshot: dict) -> dict:
    failures: list[str] = []
    counts = _counts(snapshot)
    for name, expected in EXPECTED_COUNTS.items():
        if counts[name] != expected:
            failures.append(f"{name}={counts[name]} expected={expected}")

    events = snapshot["tdnet_events"]
    manifest = _event_manifest(events)
    manifest_sha = _manifest_sha256(manifest)
    if manifest_sha != EXPECTED_EVENT_MANIFEST_SHA256:
        failures.append(f"event_manifest_sha256={manifest_sha} expected={EXPECTED_EVENT_MANIFEST_SHA256}")
    if {row.get("ticker") for row in events} != set(TARGET_TICKERS):
        failures.append("tdnet_events_ticker_set_mismatch")

    classifications = {}
    for row in events:
        ticker = str(row.get("ticker") or "")
        as_of_date = str(row.get("disclosed_at") or "")[:10]
        decision = classify_security_eligibility(ticker, as_of_date=as_of_date)
        classifications[ticker] = decision
        if not (decision.is_etf_like and decision.authoritative):
            failures.append(f"{ticker}:not_authoritative_etf")

    expected_type_counts = Counter({("earnings", "FY"): 28, ("earnings", None): 16, ("dividend", "undecided"): 1})
    actual_type_counts = Counter((row.get("event_type"), row.get("event_subtype")) for row in events)
    if actual_type_counts != expected_type_counts:
        failures.append(f"event_type_distribution={dict(actual_type_counts)}")

    canonical = snapshot["canonical_financials"]
    earnings_tickers = {row["ticker"] for row in events if row.get("event_type") == "earnings"}
    expected_pairs = {(ticker, metric) for ticker in earnings_tickers for metric in ("sales", "operating_profit")}
    actual_pairs = {(row.get("ticker"), row.get("metric")) for row in canonical}
    if actual_pairs != expected_pairs:
        failures.append("canonical_ticker_metric_set_mismatch")
    if any(row.get("source") != "jquants_earnings_summary" for row in canonical):
        failures.append("canonical_source_mismatch")
    if any(row.get("period") != "2026-07-31" or row.get("quarter") not in ("FY", "") for row in canonical):
        failures.append("canonical_period_or_quarter_mismatch")

    decision_rows = snapshot["local"]["decision"]["rows"]
    if {row.get("ticker") for row in decision_rows} != earnings_tickers:
        failures.append("decision_earnings_ticker_set_mismatch")

    return {
        "ok": not failures,
        "failures": failures,
        "counts": counts,
        "event_manifest_sha256": manifest_sha,
        "event_dates": dict(Counter(str(row.get("disclosed_at"))[:10] for row in events)),
        "discord_sent_count": sum(bool(row.get("discord_sent_at")) for row in events),
        "classifications": {
            ticker: {
                "is_etf_like": decision.is_etf_like,
                "authoritative": decision.authoritative,
                "source": decision.source,
                "product_category": decision.product_category,
                "master_date": decision.master_date,
            }
            for ticker, decision in sorted(classifications.items())
        },
    }


def _delete_local_earnings(db_path: Path, fingerprints: list[str], expected: int) -> int:
    if not fingerprints:
        if expected:
            raise RuntimeError(f"no fingerprints for expected local delete count {expected}")
        return 0
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in fingerprints)
        cursor = conn.execute(
            f"DELETE FROM earnings_summaries WHERE fingerprint IN ({marks})", fingerprints,
        )
        if cursor.rowcount != expected:
            raise RuntimeError(f"local delete count {cursor.rowcount}, expected {expected}")
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _backup_sqlite(source: Path, backup_dir: Path) -> dict:
    backup = backup_dir / source.name
    shutil.copy2(source, backup)
    source_integrity = _integrity(source)
    backup_integrity = _integrity(backup)
    source_sha = _sha256(source)
    backup_sha = _sha256(backup)
    if source_integrity != "ok" or backup_integrity != "ok":
        raise RuntimeError(f"SQLite integrity failed: {source}")
    if source_sha != backup_sha:
        raise RuntimeError(f"SQLite backup SHA-256 mismatch: {source}")
    return {
        "source": str(source), "backup": str(backup),
        "source_sha256_before": source_sha, "backup_sha256": backup_sha,
        "source_integrity_before": source_integrity, "backup_integrity": backup_integrity,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()

    load_project_env()
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase client unavailable")
    decision_db = Path(load_config().decision_db_path).resolve()
    state_db = STATE_DB.resolve()

    before = _collect(client, decision_db, state_db)
    validation = _validate(before)
    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "git_branch": _git_value("branch", "--show-current"),
        "git_head": _git_value("rev-parse", "HEAD"),
        "validation": validation,
        "raw_tdnet_documents_action": "retained",
        "processed_disclosure_history_action": "retained",
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

    export_path = backup_dir / "pre_cleanup_rows.json"
    export_path.write_text(
        json.dumps(before, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    backup_manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "git_branch": summary["git_branch"], "git_head": summary["git_head"],
        "sqlite": [_backup_sqlite(path, backup_dir) for path in (decision_db, state_db)],
        "row_export": str(export_path), "row_export_sha256": _sha256(export_path),
        "validation": validation,
    }
    manifest_path = backup_dir / "backup_manifest.json"
    manifest_path.write_text(
        json.dumps(backup_manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    event_ids = [row["id"] for row in before["tdnet_events"]]
    deleted = {}
    for table, rows in before["references"].items():
        if rows:
            result = client.table(table).delete().in_("event_id", event_ids).execute().data or []
            if len(result) != len(rows):
                raise RuntimeError(f"{table} delete returned {len(result)}, expected {len(rows)}")
            deleted[table] = len(result)
        else:
            deleted[table] = 0

    canonical_ids = [row["id"] for row in before["canonical_financials"]]
    result = client.table("canonical_financials").delete().in_("id", canonical_ids).execute().data or []
    if len(result) != EXPECTED_COUNTS["canonical_financials"]:
        raise RuntimeError(f"canonical delete returned {len(result)}")
    deleted["canonical_financials"] = len(result)

    result = client.table("tdnet_events").delete().in_("id", event_ids).execute().data or []
    if len(result) != EXPECTED_COUNTS["tdnet_events"]:
        raise RuntimeError(f"event delete returned {len(result)}")
    deleted["tdnet_events"] = len(result)

    for label, path in (("decision", decision_db), ("state", state_db)):
        rows = before["local"][label]["rows"]
        fingerprints = [row["fingerprint"] for row in rows]
        deleted[f"{label}_earnings_summaries"] = _delete_local_earnings(
            path, fingerprints, len(rows),
        )

    after = _collect(client, decision_db, state_db)
    post_counts = _counts(after)
    if any(post_counts.values()):
        raise RuntimeError(f"postflight residual rows: {post_counts}")
    post_integrity = {str(path): _integrity(path) for path in (decision_db, state_db)}
    if any(value != "ok" for value in post_integrity.values()):
        raise RuntimeError(f"post-cleanup SQLite integrity failed: {post_integrity}")

    result = {
        **summary, "backup_dir": str(backup_dir),
        "backup_manifest": str(manifest_path),
        "backup_manifest_sha256": _sha256(manifest_path),
        "deleted": deleted, "post_counts": post_counts,
        "sqlite_integrity_after": post_integrity,
    }
    result_path = backup_dir / "cleanup_result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["cleanup_result"] = str(result_path)
    result["cleanup_result_sha256"] = _sha256(result_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
