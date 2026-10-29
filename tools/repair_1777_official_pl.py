"""1777 official-PDF canonical repair. Dry-run by default; no broad backfill.

The checked fixture records each PDF page, SHA-256, disclosure date and column.
Re-downloads are hash-checked before any proposal. Apply inserts only absent
source_row_key rows and refuses conflicting higher-priority actuals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.pipeline.canonical_writer import expand_financials_rows

FIXTURE = ROOT / "tests" / "fixtures" / "verified_pl_1777.json"
METRICS = ("sales", "gross_profit", "selling_general_and_administrative_expenses",
           "operating_profit", "ordinary_profit", "net_income")
FORECAST = {"jquants_nxf", "jquants_forecast_fy", "jquants_forecast_next_fy",
            "jquants_forecast", "tdnet_forecast"}


def load_env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def verify_pdfs(fixture: dict, session: requests.Session) -> None:
    seen = {}
    for row in fixture["cumulative"]:
        url, expected = row["url"], row["sha256"]
        if url in seen:
            assert seen[url] == expected
            continue
        response = session.get(url, timeout=(8, 30))
        response.raise_for_status()
        if not response.content.startswith(b"%PDF"):
            raise ValueError(f"not a PDF: {url}")
        actual = hashlib.sha256(response.content).hexdigest()
        if actual != expected:
            raise ValueError(f"official PDF changed: {url}, expected {expected}, got {actual}")
        seen[url] = actual


def eligible(row: dict) -> bool:
    return (row.get("source") not in FORECAST
            and not (row.get("source") == "tdnet" and not row.get("filing_id")
                     and not row.get("disclosure_datetime")))


def normalized_metric(metric: str) -> str:
    return "selling_general_and_administrative_expenses" if metric == "sga" else metric


def plan(fixture: dict, existing: list[dict]) -> dict:
    by_key = defaultdict(list)
    by_source_key = {r["source_row_key"]: r for r in existing}
    for item in existing:
        if eligible(item):
            by_key[(item["period"], item["quarter"], normalized_metric(item["metric"]))].append(item)
    inserts, kept, conflicts = [], [], []
    for item in fixture["cumulative"]:
        measures = {"sales": item["sales"], "gross_profit": item["gross_profit"],
                    "selling_general_and_administrative_expenses": item["sga"],
                    "operating_profit": item["operating_profit"],
                    "ordinary_profit": item["ordinary_profit"], "net_income": item["net_income"]}
        rows, skipped = expand_financials_rows(
            ticker="1777", period=item["period"], quarter=item["quarter"],
            metrics_dict=measures, source="official_pdf", filing_id=item["sha256"],
            disclosure_datetime=item["disclosure_date"], unit="millions_jpy")
        if skipped or len(rows) != len(METRICS):
            raise ValueError(f"writer skipped {item['period']} {item['quarter']}")
        for row in rows:
            key = (row["period"], row["quarter"], row["metric"])
            existing_same = by_source_key.get(row["source_row_key"])
            if existing_same is not None:
                if existing_same.get("value") != row["value"]:
                    conflicts.append({"key": key, "reason": "existing official source key has different value"})
                else:
                    kept.append({"key": key, "reason": "same official source row already exists"})
                continue
            candidates = by_key[key]
            winner = max(
                (r for r in candidates if r.get("source_priority", 99) ==
                 min(x.get("source_priority", 99) for x in candidates)),
                key=lambda r: r.get("recency_key") or "", default=None,
            )
            if winner and winner.get("source_priority", 99) <= row["source_priority"]:
                if winner.get("value") == row["value"]:
                    kept.append({"key": key, "reason": "equal higher-priority actual", "source": winner["source"]})
                else:
                    conflicts.append({"key": key, "expected": row["value"],
                                      "existing": winner.get("value"), "source": winner["source"]})
                continue
            inserts.append(row)
    return {"ticker": "1777", "unit": "millions_jpy", "source": "official_pdf",
            "insert_count": len(inserts), "kept_count": len(kept), "conflict_count": len(conflicts),
            "inserts": inserts, "kept": kept, "conflicts": conflicts,
            "rollback_source_row_keys": [x["source_row_key"] for x in inserts]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-before-sha256", help="required for --apply; abort on any DB drift")
    args = parser.parse_args()
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["ticker"] == "1777" and len(fixture["cumulative"]) == 13
    with requests.Session() as session:
        verify_pdfs(fixture, session)
        env = load_env(args.env_file)
        key = env["SUPABASE_SERVICE_ROLE_KEY"]
        base = env["SUPABASE_URL"].rstrip("/") + "/rest/v1/canonical_financials"
        headers = {"apikey": key, "Authorization": "Bearer " + key}
        response = session.get(base, headers=headers,
                               params={"select": "*", "ticker": "eq.1777", "limit": "500"},
                               timeout=(8, 30))
        response.raise_for_status()
        before = response.json()
        result = plan(fixture, before)
        result["before_count"] = len(before)
        result["before_sha256"] = hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.apply:
            if not args.expected_before_sha256 or args.expected_before_sha256 != result["before_sha256"]:
                raise RuntimeError("before snapshot changed or expected hash missing; no write performed")
            if result["conflict_count"]:
                raise RuntimeError("conflicting higher-priority actuals; no write performed")
            if result["inserts"]:
                write = session.post(base, headers={**headers, "Content-Type": "application/json",
                    "Prefer": "resolution=ignore-duplicates,return=minimal"},
                    params={"on_conflict": "source_row_key"}, json=result["inserts"], timeout=(8, 30))
                write.raise_for_status()
            verified = session.get(base, headers=headers,
                                   params={"select": "source_row_key,value,unit,source", "ticker": "eq.1777", "limit": "500"},
                                   timeout=(8, 30))
            verified.raise_for_status()
            after = {r["source_row_key"]: r for r in verified.json()}
            for row in result["inserts"]:
                found = after.get(row["source_row_key"])
                if not found or any(found.get(k) != row[k] for k in ("value", "unit", "source")):
                    raise RuntimeError(f"post-write verification failed: {row['source_row_key']}")
            result["after_count"] = len(after)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print("applied", result["insert_count"], "rollback keys in", args.output)
        else:
            print("dry-run", result["insert_count"], "inserts,", result["kept_count"],
                  "kept,", result["conflict_count"], "conflicts; manifest", args.output)


if __name__ == "__main__":
    main()
