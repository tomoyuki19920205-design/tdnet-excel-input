"""Plan/apply versioned filing segment observations to an explicit DB copy."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from lib.backfill.filing_segment_lineage import (
    FilingLineageRecord,
    ObservationRecord,
    build_route_map,
    ensure_filing_segment_lineage,
    insert_lineage,
    insert_observation,
    pending_plan,
    segment_table_digest,
)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _baseline(conn: sqlite3.Connection, segment_id: int | None) -> dict | None:
    if segment_id is None:
        return None
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM segment_financials WHERE id=?", (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--expected-filings", type=int, required=True)
    parser.add_argument("--expected-observations", type=int, required=True)
    parser.add_argument("--apply-to-snapshot", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    plan_path = Path(args.plan)
    if _sha(plan_path) != args.plan_sha256.lower():
        raise SystemExit("plan_sha256_mismatch")
    records = _read_jsonl(plan_path)
    observations = [ObservationRecord(**row["observation"]) for row in records if row.get("observation")]
    filing_rows = [FilingLineageRecord(**row["lineage"]) for row in records]
    if len({row.filing_id for row in filing_rows}) != args.expected_filings:
        raise SystemExit("filing_count_mismatch")
    if len(observations) != args.expected_observations:
        raise SystemExit("observation_count_mismatch")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        before = segment_table_digest(conn)
        ensure_filing_segment_lineage(conn)
        initial = pending_plan(conn, observations, filing_rows)
        if args.apply_to_snapshot:
            with conn:
                observation_ids: dict[tuple[str, str], int] = {}
                for observation in observations:
                    sid = observation.canonical_segment_financial_id
                    oid = insert_observation(conn, observation, _baseline(conn, sid))
                    observation_ids[(observation.filing_id, observation.row_semantic_digest)] = oid
                for lineage in filing_rows:
                    if lineage.relation_role not in {"ZERO_PAYLOAD_NORMAL", "KNOWN_QUARANTINE_EMPTY"}:
                        key = (lineage.filing_id, lineage.row_semantic_digest)
                        if key in observation_ids:
                            lineage = FilingLineageRecord(
                                **{**lineage.__dict__, "observation_id": observation_ids[key]}
                            )
                    insert_lineage(conn, lineage)
        after = segment_table_digest(conn)
        second = pending_plan(conn, observations, filing_rows)
        lineage = [dict(row) for row in conn.execute("SELECT * FROM filing_segment_lineage ORDER BY filing_id,lineage_id")]
        route_map = build_route_map(lineage)
        result = {
            "mode": "APPLY_TO_SNAPSHOT" if args.apply_to_snapshot else "PLAN_ONLY",
            "filings": len({row.filing_id for row in filing_rows}),
            "observations": len(observations),
            "lineage_rows": len(filing_rows),
            "initial_plan": initial,
            "second_plan": second,
            "segment_financials_unchanged": before == after,
            "segment_financials_digest": before,
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
            "route_map": route_map,
        }
    finally:
        conn.close()
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8", newline="\n")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
