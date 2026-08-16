"""Safely remove an empty legacy period row after an exact-period replacement exists."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.common_ticker import normalize_ticker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="decision_db.db")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--phantom-period", required=True)
    parser.add_argument("--correct-period", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM quarterly_results WHERE fiscal_year_end IN (?, ?)",
        (args.phantom_period, args.correct_period),
    ) if normalize_ticker(row["company_code"]) == normalize_ticker(args.ticker)]
    correct = [row for row in rows if row["fiscal_year_end"] == args.correct_period]
    phantom = [row for row in rows if row["fiscal_year_end"] == args.phantom_period]
    if not correct:
        raise SystemExit("Refusing cleanup: exact-period replacement is absent")
    amount_fields = {
        "sales", "gross_profit", "gross_margin", "sga", "operating_profit",
        "profit_before_tax", "net_income",
    }
    nonnull_metrics = {
        row["id"]: {field: row.get(field) for field in amount_fields if row.get(field) not in (None, 0)}
        for row in phantom
    }
    nonnull_metrics = {key: value for key, value in nonnull_metrics.items() if value}
    has_provenance = any(
        row.get("source_doc_id") or row.get("source_url") or row.get("field_sources")
        for row in phantom
    )
    if nonnull_metrics or has_provenance:
        raise SystemExit(
            "Refusing cleanup: phantom candidate contains a non-null metric: "
            + json.dumps(nonnull_metrics, ensure_ascii=False)
        )

    if args.apply and phantom:
        ids = [row["id"] for row in phantom]
        conn.executemany("DELETE FROM quarterly_results WHERE id = ?", [(value,) for value in ids])
        conn.commit()
    conn.close()
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "ticker": normalize_ticker(args.ticker),
        "correct_period_rows": len(correct),
        "phantom_backup": phantom,
        "deleted": len(phantom) if args.apply else 0,
    }
    output = Path("artifacts/1967_local_phantom_cleanup_20260816.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
