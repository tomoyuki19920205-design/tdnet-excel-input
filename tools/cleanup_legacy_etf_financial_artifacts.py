#!/usr/bin/env python3
"""Cleanup the second, independent 15-row legacy ETF financial manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.events.env_loader import load_project_env
from src.events.tdnet_event_store import _get_supabase
from src.security_eligibility import classify_security_eligibility

TARGET_TICKERS = ("2089", "447A", "448A")
CANONICAL_IDS = frozenset({
    30220019, 30220020, 30686965, 30686966,
    31436483, 31436484, 31436485, 31436486,
    31913191, 31913192, 31913193, 31913194,
})
FINANCIAL_KEYS = frozenset({
    ("2089", "2026-11-30", "2Q"),
    ("447A", "2026-10-31", "2Q"),
    ("448A", "2026-10-31", "2Q"),
})
EXPECTED_MANIFEST_SHA256 = "5e5c35952b92757a3326860838981050422b5311bc229f6311325690f21dbbc7"
MANIFEST_BASE_HEAD = "bcea4a7c1a83f4c6259d00ae6e158dfb055e5089"
STATE_DB = ROOT / "data" / "state.db"
MASTER_DB = ROOT / "data" / "jquants.db"
REASON = "authoritative_prodcat_014_legacy_corporate_financial_artifact"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: object) -> str:
    data = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _git_value(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return result.stdout.strip()


def _master_rows() -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{MASTER_DB.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute("SELECT MAX(date) FROM market_data_universe").fetchone()[0]
        marks = ",".join("?" for _ in TARGET_TICKERS)
        rows = conn.execute(
            f"SELECT date,ticker,code,company_name,product_category,market_code "
            f"FROM market_data_universe WHERE date=? AND ticker IN ({marks})",
            (latest, *TARGET_TICKERS),
        ).fetchall()
        return {row["ticker"]: dict(row) for row in rows}
    finally:
        conn.close()


def _collect(client) -> dict:
    canonical = (
        client.table("canonical_financials").select("*")
        .in_("ticker", list(TARGET_TICKERS)).execute().data or []
    )
    financials = (
        client.table("financials").select("*")
        .in_("ticker", list(TARGET_TICKERS)).execute().data or []
    )
    return {"canonical_financials": canonical, "financials": financials}


def _manifest(snapshot: dict, master: dict[str, dict]) -> dict:
    rows = []
    for row in sorted(snapshot["canonical_financials"], key=lambda item: int(item["id"])):
        rows.append({
            "database": "production_supabase",
            "table": "canonical_financials",
            "primary_key": {"id": row["id"]},
            "ticker": row["ticker"],
            "fiscal_period": row.get("period"),
            "quarter": row.get("quarter"),
            "disclosure_identifier": {
                "filing_id": row.get("filing_id"),
                "source_row_key": row.get("source_row_key"),
            },
            "row_identifier": str(row["id"]),
            "delete_reason": REASON,
            "snapshot": row,
        })
    for row in sorted(
        snapshot["financials"],
        key=lambda item: (item["ticker"], item["period"], item["quarter"]),
    ):
        primary_key = {
            "ticker": row["ticker"], "period": row["period"],
            "quarter": row["quarter"],
        }
        rows.append({
            "database": "production_supabase",
            "table": "financials",
            "primary_key": primary_key,
            "ticker": row["ticker"],
            "fiscal_period": row.get("period"),
            "quarter": row.get("quarter"),
            "disclosure_identifier": {
                "source": row.get("source"), "updated_at": row.get("updated_at"),
            },
            "row_identifier": "|".join(primary_key.values()),
            "delete_reason": REASON,
            "snapshot": row,
        })
    return {
        "manifest_version": "legacy_etf_financial_cleanup_v1",
        "created_from_head": MANIFEST_BASE_HEAD,
        "master_snapshot": next(iter(master.values()))["date"] if master else "",
        "classifications": [master[ticker] for ticker in TARGET_TICKERS if ticker in master],
        "rows": rows,
    }


def _validate(snapshot: dict, master: dict[str, dict], manifest: dict) -> dict:
    failures = []
    decisions = {}
    for ticker in TARGET_TICKERS:
        decision = classify_security_eligibility(ticker)
        decisions[ticker] = decision
        if not (decision.authoritative and decision.is_etf_like and decision.product_category == "014"):
            failures.append(f"{ticker}:authoritative_etf_check_failed")
        if ticker not in master:
            failures.append(f"{ticker}:latest_master_row_missing")

    canonical = snapshot["canonical_financials"]
    financials = snapshot["financials"]
    actual_ids = {int(row["id"]) for row in canonical}
    actual_keys = {(row["ticker"], row["period"], row["quarter"]) for row in financials}
    if len(canonical) != 12 or actual_ids != CANONICAL_IDS:
        failures.append(f"canonical_manifest_mismatch count={len(canonical)} ids={sorted(actual_ids)}")
    if len(financials) != 3 or actual_keys != FINANCIAL_KEYS:
        failures.append(f"financial_manifest_mismatch count={len(financials)} keys={sorted(actual_keys)}")
    if len(manifest["rows"]) != 15:
        failures.append(f"manifest_total={len(manifest['rows'])} expected=15")

    manifest_sha = _json_sha256(manifest)
    if EXPECTED_MANIFEST_SHA256 != "PENDING_DRY_RUN" and manifest_sha != EXPECTED_MANIFEST_SHA256:
        failures.append(f"manifest_sha256={manifest_sha} expected={EXPECTED_MANIFEST_SHA256}")
    return {
        "ok": not failures and EXPECTED_MANIFEST_SHA256 != "PENDING_DRY_RUN",
        "failures": failures + (["manifest_sha256_not_frozen"] if EXPECTED_MANIFEST_SHA256 == "PENDING_DRY_RUN" else []),
        "manifest_sha256": manifest_sha,
        "counts": {"canonical_financials": len(canonical), "financials": len(financials), "total": len(manifest["rows"])},
        "classifications": {
            ticker: {
                "company_name": master.get(ticker, {}).get("company_name", ""),
                "product_category": decision.product_category,
                "is_etf_like": decision.is_etf_like,
                "authoritative": decision.authoritative,
                "source": decision.source,
            }
            for ticker, decision in decisions.items()
        },
    }


def _backup_sqlite(source_path: Path, backup_path: Path) -> dict:
    source_integrity = _integrity(source_path)
    source = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    backup_integrity = _integrity(backup_path)
    if source_integrity != "ok" or backup_integrity != "ok":
        raise RuntimeError(f"SQLite integrity failure: {source_path}")
    return {
        "source_path": str(source_path), "source_size": source_path.stat().st_size,
        "source_sha256": _sha256(source_path), "source_integrity": source_integrity,
        "backup_path": str(backup_path), "backup_size": backup_path.stat().st_size,
        "backup_sha256": _sha256(backup_path), "backup_integrity": backup_integrity,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()

    load_project_env()
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase unavailable")
    decision_db = Path(load_config().decision_db_path).resolve()
    state_db = STATE_DB.resolve()
    snapshot = _collect(client)
    master = _master_rows()
    manifest = _manifest(snapshot, master)
    validation = _validate(snapshot, master, manifest)
    result = {
        "mode": "apply" if args.apply else "dry_run",
        "git_head": _git_value("rev-parse", "HEAD"),
        "validation": validation,
    }
    if not validation["ok"]:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    if not args.apply:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else (
        ROOT / "artifacts" / "production_backups" / f"legacy_etf_financial_cleanup_{stamp}"
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = backup_dir / "fixed_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    export_path = backup_dir / "pre_cleanup_15_rows.json"
    export_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    backup_data = {
        "created_at": datetime.now().astimezone().isoformat(),
        "git_head": result["git_head"],
        "fixed_manifest_path": str(manifest_path),
        "fixed_manifest_size": manifest_path.stat().st_size,
        "fixed_manifest_sha256": _json_sha256(manifest),
        "row_export_path": str(export_path),
        "row_export_size": export_path.stat().st_size,
        "row_export_sha256": _sha256(export_path),
        "sqlite": [
            _backup_sqlite(decision_db, backup_dir / "decision_db.db"),
            _backup_sqlite(state_db, backup_dir / "state.db"),
        ],
        "validation": validation,
    }
    backup_manifest_path = backup_dir / "backup_manifest.json"
    backup_manifest_path.write_text(json.dumps(backup_data, ensure_ascii=False, indent=2), encoding="utf-8")

    deleted_canonical = (
        client.table("canonical_financials").delete()
        .in_("id", sorted(CANONICAL_IDS)).execute().data or []
    )
    if len(deleted_canonical) != 12:
        raise RuntimeError(f"canonical delete returned {len(deleted_canonical)}, expected 12")
    deleted_financials = []
    for ticker, period, quarter in sorted(FINANCIAL_KEYS):
        rows = (
            client.table("financials").delete().eq("ticker", ticker)
            .eq("period", period).eq("quarter", quarter).execute().data or []
        )
        if len(rows) != 1:
            raise RuntimeError(f"financial delete {ticker}/{period}/{quarter} returned {len(rows)}")
        deleted_financials.extend(rows)

    after = _collect(client)
    post_counts = {
        "canonical_financials": len(after["canonical_financials"]),
        "financials": len(after["financials"]),
    }
    if any(post_counts.values()):
        raise RuntimeError(f"postflight residual rows: {post_counts}")
    integrity_after = {str(path): _integrity(path) for path in (decision_db, state_db)}
    if any(value != "ok" for value in integrity_after.values()):
        raise RuntimeError(f"postflight integrity failed: {integrity_after}")

    result.update({
        "backup_dir": str(backup_dir),
        "backup_manifest": str(backup_manifest_path),
        "backup_manifest_size": backup_manifest_path.stat().st_size,
        "backup_manifest_sha256": _sha256(backup_manifest_path),
        "deleted": {"canonical_financials": 12, "financials": 3, "total": 15},
        "post_counts": post_counts,
        "sqlite_integrity_after": integrity_after,
    })
    result_path = backup_dir / "cleanup_result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["cleanup_result"] = str(result_path)
    result["cleanup_result_sha256"] = _sha256(result_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
