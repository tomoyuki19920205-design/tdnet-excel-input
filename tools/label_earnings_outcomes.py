#!/usr/bin/env python3
"""2026-07-15 earnings_reactionsへ確定open-gap正解ラベルを保存する。"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

EARNINGS_DATE = "2026-07-15"
LABEL_BASIS = "adjusted_close_to_next_adjusted_open"
LABEL_VERSION = "open_gap_v1_2pct_5pct"
DEFAULT_DB = PROJECT_ROOT / "data" / "jquants.db"
DEFAULT_INPUT_CSV = (
    PROJECT_ROOT / "output" /
    f"earnings_reaction_{EARNINGS_DATE}_with_release_time.csv"
)
DEFAULT_OUTPUT_CSV = (
    PROJECT_ROOT / "output" / f"earnings_reaction_{EARNINGS_DATE}_labeled.csv"
)
OUTCOME_COLUMNS = [
    "outcome_eligible",
    "primary_outcome_label",
    "primary_outcome_band",
    "outcome_label_basis",
    "outcome_label_version",
    "outcome_labeled_at",
    "outcome_exclusion_reason",
]
OUTCOME_COLUMN_TYPES = {
    "outcome_eligible": "INTEGER NOT NULL DEFAULT 0",
    "primary_outcome_label": "TEXT",
    "primary_outcome_band": "TEXT",
    "outcome_label_basis": "TEXT",
    "outcome_label_version": "TEXT",
    "outcome_labeled_at": "TEXT",
    "outcome_exclusion_reason": "TEXT",
}
EXPECTED = {
    "total": 121,
    "eligible": 101,
    "excluded": 20,
    "labels": {"success": 20, "neutral": 39, "failure": 42},
    "bands": {
        "strong_success": 15,
        "success": 5,
        "neutral": 39,
        "failure": 16,
        "large_failure": 26,
        "excluded": 20,
    },
}


def ensure_outcome_columns(conn: sqlite3.Connection) -> list[str]:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(earnings_reactions)")}
    if not existing:
        raise RuntimeError("earnings_reactions table not found")
    added: list[str] = []
    for column, sql_type in OUTCOME_COLUMN_TYPES.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE earnings_reactions ADD COLUMN {column} {sql_type}"
            )
            added.append(column)
    conn.commit()
    return added


def load_reaction_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [
        dict(row) for row in conn.execute(
            "SELECT * FROM earnings_reactions WHERE earnings_date = ? ORDER BY code",
            (EARNINGS_DATE,),
        ).fetchall()
    ]


def validate_ratio_scale(rows: list[dict[str, Any]]) -> None:
    """保存値がpercent数値でなく、価格から再計算した小数比率であることを確認。"""
    checked = 0
    for row in rows:
        stored = row.get("open_gap_return_pct")
        previous_close = row.get("close_2026_07_15_adjusted")
        next_open = row.get("open_2026_07_16_adjusted")
        if stored is None:
            continue
        if previous_close in (None, 0) or next_open is None:
            raise RuntimeError(f"ratio scale cannot be verified for {row.get('code')}")
        expected = float(next_open) / float(previous_close) - 1
        if not math.isclose(float(stored), expected, rel_tol=1e-12, abs_tol=1e-12):
            raise RuntimeError(
                f"open_gap scale mismatch for {row.get('code')}: "
                f"stored={stored} recomputed_ratio={expected}"
            )
        checked += 1
    if checked == 0:
        raise RuntimeError("no open_gap_return_pct values available for scale verification")


def classify_outcome(
    reaction_window_valid: bool,
    open_gap_return_ratio: float | None,
) -> tuple[bool, str, str]:
    if (
        not reaction_window_valid
        or open_gap_return_ratio is None
        or not math.isfinite(float(open_gap_return_ratio))
    ):
        return False, "excluded", "excluded"

    value = float(open_gap_return_ratio)
    if value >= 0.02:
        label = "success"
    elif value <= -0.02:
        label = "failure"
    else:
        label = "neutral"

    if value >= 0.05:
        band = "strong_success"
    elif value >= 0.02:
        band = "success"
    elif value <= -0.05:
        band = "large_failure"
    elif value <= -0.02:
        band = "failure"
    else:
        band = "neutral"
    return True, label, band


def exclusion_reason(row: dict[str, Any]) -> str:
    reasons: list[str] = []
    session = str(row.get("release_session") or "unknown")
    if session == "intraday":
        reasons.append("intraday_release")
    elif session == "pre_open":
        reasons.append("pre_open_release")
    elif session == "ambiguous":
        reasons.append("ambiguous_release")
    elif session == "unknown":
        reasons.append("market_or_release_session_unknown")
    elif session != "after_close":
        reasons.append("reaction_window_invalid")

    if row.get("open_gap_return_pct") is None:
        reasons.append("open_gap_return_missing")
    if (
        row.get("close_2026_07_15_adjusted") is None
        or row.get("open_2026_07_16_adjusted") is None
    ):
        reasons.append("price_data_missing")
    if not row.get("primary_earnings_published_at_jst"):
        reasons.append("release_time_missing")
    if not row.get("reaction_window_valid") and not reasons:
        reasons.append("reaction_window_invalid")
    return ";".join(dict.fromkeys(reasons))


def build_outcomes(
    rows: list[dict[str, Any]],
    labeled_at: str,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        eligible, label, band = classify_outcome(
            bool(row.get("reaction_window_valid")),
            row.get("open_gap_return_pct"),
        )
        outcomes.append({
            "source_event_id": row["source_event_id"],
            "code": row["code"],
            "company_name": row["company_name"],
            "outcome_eligible": eligible,
            "primary_outcome_label": label,
            "primary_outcome_band": band,
            "outcome_label_basis": LABEL_BASIS,
            "outcome_label_version": LABEL_VERSION,
            "outcome_labeled_at": labeled_at,
            "outcome_exclusion_reason": "" if eligible else exclusion_reason(row),
        })
    return outcomes


def outcome_counts(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = sum(bool(row["outcome_eligible"]) for row in outcomes)
    labels = Counter(
        row["primary_outcome_label"] for row in outcomes if row["outcome_eligible"]
    )
    bands = Counter(row["primary_outcome_band"] for row in outcomes)
    return {
        "total": len(outcomes),
        "eligible": eligible,
        "excluded": len(outcomes) - eligible,
        "labels": dict(labels),
        "bands": dict(bands),
    }


def validate_expected_counts(counts: dict[str, Any]) -> None:
    normalized_labels = {
        key: counts["labels"].get(key, 0) for key in EXPECTED["labels"]
    }
    normalized_bands = {
        key: counts["bands"].get(key, 0) for key in EXPECTED["bands"]
    }
    actual = {
        "total": counts["total"],
        "eligible": counts["eligible"],
        "excluded": counts["excluded"],
        "labels": normalized_labels,
        "bands": normalized_bands,
    }
    if actual != EXPECTED:
        raise RuntimeError(
            "expected outcome counts mismatch; save aborted: "
            + json.dumps({"expected": EXPECTED, "actual": actual}, ensure_ascii=False)
        )


def save_outcomes(conn: sqlite3.Connection, outcomes: list[dict[str, Any]]) -> int:
    sql = """
        UPDATE earnings_reactions SET
          outcome_eligible = ?, primary_outcome_label = ?,
          primary_outcome_band = ?, outcome_label_basis = ?,
          outcome_label_version = ?, outcome_labeled_at = ?,
          outcome_exclusion_reason = ?, updated_at = datetime('now')
        WHERE source_event_id = ? AND earnings_date = ?
    """
    before = conn.total_changes
    conn.executemany(sql, [(
        int(row["outcome_eligible"]), row["primary_outcome_label"],
        row["primary_outcome_band"], row["outcome_label_basis"],
        row["outcome_label_version"], row["outcome_labeled_at"],
        row["outcome_exclusion_reason"], row["source_event_id"], EARNINGS_DATE,
    ) for row in outcomes])
    changed = conn.total_changes - before
    if changed != len(outcomes):
        conn.rollback()
        raise RuntimeError(
            f"DB update count mismatch: expected={len(outcomes)} actual={changed}"
        )
    conn.commit()
    return changed


def write_labeled_csv(
    source_path: Path,
    output_path: Path,
    outcomes: list[dict[str, Any]],
) -> None:
    with source_path.open(encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
        source_columns = list(source_rows[0]) if source_rows else []
    by_code = {str(row["code"]): row for row in outcomes}
    if len(source_rows) != len(outcomes) or len(by_code) != len(outcomes):
        raise RuntimeError("CSV/DB count or code uniqueness mismatch")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*source_columns, *OUTCOME_COLUMNS])
        writer.writeheader()
        for source in source_rows:
            outcome = by_code[source["code"]]
            row = dict(source)
            for column in OUTCOME_COLUMNS:
                value = outcome[column]
                if column == "outcome_eligible":
                    value = "true" if value else "false"
                row[column] = value
            writer.writerow(row)


def exclusion_reason_counts(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in outcomes:
        if row["outcome_eligible"]:
            continue
        counts[row["outcome_exclusion_reason"] or "unspecified"] += 1
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT_CSV)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    args = parser.parse_args()

    labeled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(args.db) as conn:
        rows = load_reaction_rows(conn)
        validate_ratio_scale(rows)
        outcomes = build_outcomes(rows, labeled_at)
        counts = outcome_counts(outcomes)
        # 期待件数不一致時は、スキーマ変更・ラベル保存の前に停止する。
        validate_expected_counts(counts)
        added_columns = ensure_outcome_columns(conn)
        updated = save_outcomes(conn, outcomes)

    write_labeled_csv(args.input_csv, args.output_csv, outcomes)
    report = {
        **counts,
        "exclusion_reason_counts": exclusion_reason_counts(outcomes),
        "db_updated_count": updated,
        "added_db_columns": added_columns,
        "output_csv": str(args.output_csv.resolve()),
        "database": str(args.db.resolve()),
        "outcome_label_basis": LABEL_BASIS,
        "outcome_label_version": LABEL_VERSION,
        "outcome_labeled_at": labeled_at,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
