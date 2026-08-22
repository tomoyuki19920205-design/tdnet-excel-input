"""Remove month-end PL aliases when an authoritative exact fiscal period exists.

The command is ticker-scoped and dry-run by default.  A row is eligible only
when local J-Quants has the same fiscal year/month and quarter at a different
exact date, and the exact replacement already exists in canonical_financials.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline.db import get_supabase_write_config, load_env
from src.common_ticker import normalize_ticker


def _authoritative_keys(db_path: Path, ticker: str) -> dict[tuple[str, str], str]:
    local_code = normalize_ticker(ticker) + "0"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        """
        SELECT DISTINCT current_fiscal_year_end_date, type_of_current_period
        FROM jquants_financials_normalized
        WHERE local_code IN (?, ?)
          AND type_of_document LIKE '%FinancialStatements%'
        """,
        (normalize_ticker(ticker), local_code),
    ).fetchall()
    conn.close()
    result: dict[tuple[str, str], str] = {}
    for period, quarter in rows:
        key = (str(period)[:7], str(quarter))
        existing = result.get(key)
        if existing and existing != period:
            raise SystemExit(f"Ambiguous authoritative periods for {key}: {existing}, {period}")
        result[key] = str(period)
    if not result:
        raise SystemExit(f"No authoritative J-Quants periods found for {ticker}")
    return result


def _select_ticker(config: dict, table: str, ticker: str) -> list[dict]:
    response = requests.get(
        f"{config['rest_url']}/{table}",
        headers=config["headers"],
        params={"select": "*", "ticker": f"eq.{ticker}", "limit": "10000"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json() or []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "jquants.db")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    ticker = normalize_ticker(args.ticker)
    load_env()
    config = get_supabase_write_config()
    if not config:
        raise SystemExit("Supabase write config missing")

    authoritative = _authoritative_keys(args.db, ticker)
    before = {
        table: _select_ticker(config, table, ticker)
        for table in ("financials", "canonical_financials")
    }
    canonical_exact = {
        (row.get("period"), row.get("quarter"))
        for row in before["canonical_financials"]
        if row.get("source") == "jquants"
    }

    candidates: dict[str, list[dict]] = {"financials": [], "canonical_financials": []}
    for table, rows in before.items():
        for row in rows:
            period = str(row.get("period") or "")
            quarter = str(row.get("quarter") or "")
            correct = authoritative.get((period[:7], quarter))
            if not correct or correct == period:
                continue
            if (correct, quarter) not in canonical_exact:
                raise SystemExit(
                    f"Refusing cleanup: exact canonical replacement absent for {correct}/{quarter}"
                )
            candidates[table].append(row)

    result: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "ticker": ticker,
        "authoritative_periods": [
            {"year_month": key[0], "quarter": key[1], "period": value}
            for key, value in sorted(authoritative.items())
        ],
        "before_counts": {table: len(rows) for table, rows in before.items()},
        "candidate_counts": {table: len(rows) for table, rows in candidates.items()},
        "candidate_backup": candidates,
        "deleted": {"financials": 0, "canonical_financials": 0},
    }

    if args.apply:
        session = requests.Session()
        headers = {**config["headers"], "Prefer": "return=representation"}
        for table, rows in candidates.items():
            keys = sorted({(row["period"], row["quarter"]) for row in rows})
            for period, quarter in keys:
                response = session.delete(
                    f"{config['rest_url']}/{table}",
                    headers=headers,
                    params={
                        "ticker": f"eq.{ticker}",
                        "period": f"eq.{period}",
                        "quarter": f"eq.{quarter}",
                    },
                    timeout=30,
                )
                response.raise_for_status()
                result["deleted"][table] += len(response.json() or [])  # type: ignore[index]

        after = {
            table: _select_ticker(config, table, ticker)
            for table in ("financials", "canonical_financials")
        }
        result["after_counts"] = {table: len(rows) for table, rows in after.items()}

    output = args.output or ROOT / "artifacts" / f"{ticker}_duplicate_period_cleanup.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
