#!/usr/bin/env python3
"""Audit ambiguous numeric/alphanumeric J-Quants codes using local data only."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common_ticker import JQUANTS_ALPHA_MAP, normalize_ticker


def audit_local_codes(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """SELECT local_code, count(*)
               FROM jquants_financials_normalized
               WHERE length(local_code)=5
               GROUP BY local_code"""
        ).fetchall()
    counts = {str(code).upper(): int(count) for code, count in rows}
    pairs: list[dict[str, Any]] = []
    for numeric_raw, alpha_ticker in sorted(JQUANTS_ALPHA_MAP.items()):
        alpha_raw = f"{alpha_ticker}0"
        numeric_ticker = numeric_raw[:4]
        numeric_count = counts.get(numeric_raw, 0)
        alpha_count = counts.get(alpha_raw, 0)
        if not alpha_count:
            continue
        pairs.append({
            "alpha_ticker": alpha_ticker,
            "alpha_raw_code": alpha_raw,
            "alpha_raw_rows": alpha_count,
            "alpha_normalized": normalize_ticker(alpha_raw),
            "numeric_ticker": numeric_ticker,
            "numeric_raw_code": numeric_raw,
            "numeric_raw_rows": numeric_count,
            "numeric_normalized": normalize_ticker(numeric_raw),
            "both_raw_identities_present": bool(numeric_count),
            "normalization_collision": normalize_ticker(alpha_raw) == normalize_ticker(numeric_raw),
            "status": "ALREADY_SAFE" if normalize_ticker(alpha_raw) != normalize_ticker(numeric_raw) else "COLLISION",
        })
    collision_candidates = [p for p in pairs if p["both_raw_identities_present"]]
    return {
        "scope": "local SQLite only; no production scan",
        "database": str(db_path),
        "alphanumeric_tickers_checked": len(pairs),
        "collision_candidates": len(collision_candidates),
        "confirmed_normalizer_collisions": sum(p["normalization_collision"] for p in pairs),
        "already_safe": sum(not p["normalization_collision"] for p in pairs),
        "candidate_pairs": collision_candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "jquants.db")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_local_codes(args.db)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
