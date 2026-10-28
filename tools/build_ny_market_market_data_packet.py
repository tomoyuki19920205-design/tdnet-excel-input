#!/usr/bin/env python3
"""Build a deterministic canonical NY market-data packet before LLM research."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market_market_data import (
    MarketDataError, YahooChartProvider, build_index_sector_snapshot,
    build_canonical_market_data_packet, LiveDiscrepancyArbitrator,
)
from lib.ny_market_research import market_data_packet_sha256
from tools.company_news_atomic import atomic_write_json


@contextmanager
def _single_builder(lock_path: Path):
    """Keep one writer per report date without changing scheduled tasks."""
    import msvcrt
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(f"NY builder already running: {lock_path}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _checkpoint(path: Path, *, session: str, stage: str, snapshot: dict | None,
                error: str | None = None) -> None:
    current = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if current and current.get("market_session_date") != session:
        raise ValueError("checkpoint belongs to a different NY session")
    state = {"market_session_date": session, "stage": stage,
             "updated_at_utc": datetime.now(timezone.utc).isoformat(),
             "snapshot": snapshot,
             "snapshot_sha256": market_data_packet_sha256(snapshot) if snapshot else None,
             "attempts": int(current.get("attempts", 0)) + int(stage in {"complete", "provisional", "blocked"}),
             "last_error": error}
    atomic_write_json(path, state)


def _snapshot_from_checkpoint(path: Path, session: str) -> dict | None:
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8-sig"))
    if state.get("market_session_date") != session:
        raise ValueError("checkpoint belongs to a different NY session")
    snapshot = state.get("snapshot")
    if snapshot is not None and market_data_packet_sha256(snapshot) != state.get("snapshot_sha256"):
        raise ValueError("checkpoint snapshot hash mismatch")
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-session-date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--issuer-components", type=Path)
    parser.add_argument("--corporate-action-notices", type=Path, help="ticker-to-official-Nasdaq-Trader-notice-URL JSON")
    parser.add_argument("--corporate-action-evidence", type=Path, help="ticker-to-dated-SEC-corporate-action-evidence JSON")
    parser.add_argument("--independent-snapshots", type=Path, help="ticker-to-captured-independent-EOD-quote JSON")
    parser.add_argument("--checkpoint", type=Path, help="stable per-session checkpoint path")
    parser.add_argument("--allow-provisional", action="store_true", help="emit verified sections with explicit unresolved reasons")
    args = parser.parse_args()
    issuer_components = None
    if args.issuer_components:
        issuer_components = json.loads(args.issuer_components.read_text(encoding="utf-8"))
        if not isinstance(issuer_components, dict):
            raise ValueError("issuer-components must be a ticker-to-components object")
    notices = json.loads(args.corporate_action_notices.read_text(encoding="utf-8")) if args.corporate_action_notices else {}
    actions = json.loads(args.corporate_action_evidence.read_text(encoding="utf-8")) if args.corporate_action_evidence else {}
    snapshots = json.loads(args.independent_snapshots.read_text(encoding="utf-8")) if args.independent_snapshots else {}
    if not isinstance(snapshots, dict):
        raise ValueError("independent-snapshots must be a ticker-to-snapshot object")
    if args.independent_snapshots:
        for entry in snapshots.values():
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("source_url"), str):
                raise ValueError("independent-snapshots entries need path and source_url")
            entry["path"] = str((args.independent_snapshots.parent / entry["path"]).resolve())
    if not isinstance(notices, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in notices.items()):
        raise ValueError("corporate-action-notices must map tickers to official URLs")
    if not isinstance(actions, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in actions.items()):
        raise ValueError("corporate-action-evidence must map tickers to evidence objects")
    if args.output.exists():
        raise ValueError(f"refusing to overwrite existing packet: {args.output}")
    checkpoint_path = args.checkpoint or args.output.with_suffix(".checkpoint.json")
    with _single_builder(checkpoint_path.with_suffix(".lock")):
        os.environ["NY_MARKET_EVIDENCE_DIR"] = str(checkpoint_path.parent / f"ny-{args.market_session_date}-raw-responses")
        verified_snapshot = _snapshot_from_checkpoint(checkpoint_path, args.market_session_date.isoformat())
        if verified_snapshot is None:
            providers = (
                YahooChartProvider(host="query1.finance.yahoo.com"),
                YahooChartProvider(host="query2.finance.yahoo.com"),
                YahooChartProvider(host="query1.finance.yahoo.com", closed_meta_fallback=True),
                YahooChartProvider(host="query2.finance.yahoo.com", closed_meta_fallback=True),
            )
            verified_snapshot = build_index_sector_snapshot(providers, args.market_session_date)
            _checkpoint(checkpoint_path, session=args.market_session_date.isoformat(),
                        stage="verified_index_sector", snapshot=verified_snapshot)
        try:
            packet = build_canonical_market_data_packet(
                args.market_session_date, issuer_components=issuer_components,
                discrepancy_arbitrator=LiveDiscrepancyArbitrator(
                    corporate_action_notices=notices, corporate_action_evidence=actions,
                    independent_snapshots=snapshots,
                ), verified_snapshot=verified_snapshot,
            )
        except MarketDataError as exc:
            reason = str(exc)
            _checkpoint(checkpoint_path, session=args.market_session_date.isoformat(),
                        stage="provisional" if args.allow_provisional else "blocked",
                        snapshot=verified_snapshot, error=reason)
            if not args.allow_provisional:
                raise
            packet = {**verified_snapshot, "market_data_contract_version": "ny_market_data_provisional_v1",
                      "market_data_generated_at": verified_snapshot["source"]["retrieved_at"],
                      "providers": [verified_snapshot["source"]["name"]],
                      "raw_response_hashes": sorted({row["raw_response_sha256"] for row in
                                                      verified_snapshot["indexes"] + verified_snapshot["sectors"]}),
                      "discrepancy_count": 0, "top_gainers_20": [],
                      "quality_status": "provisional",
                      "unresolved_items": [{"field": "純粋上昇率Top20", "reason": reason}],
                      "ranking_status": "unconfirmed"}
        else:
            _checkpoint(checkpoint_path, session=args.market_session_date.isoformat(),
                        stage="complete", snapshot=verified_snapshot)
    atomic_write_json(args.output, packet)
    print(json.dumps({
        "status": "provisional" if packet.get("quality_status") == "provisional" else "created", "output": str(args.output),
        "market_data_packet_sha256": market_data_packet_sha256(packet),
        "indices": len(packet["indexes"]), "sectors": len(packet["sectors"]),
        "top20": len(packet["top_gainers_20"]),
        "discrepancy_count": packet["discrepancy_count"],
        "excluded_corporate_action_count": packet["excluded_corporate_action_count"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
