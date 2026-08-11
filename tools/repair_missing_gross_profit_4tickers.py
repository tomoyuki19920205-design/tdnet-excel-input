#!/usr/bin/env python3
"""Hash-locked repair for bounded GP gaps in 6268/6503/1892/2148.

Values come only from an exact official XBRL package or an exact matching
J-Quants consolidated actual details row.  The tool never derives GP from
sales and cost of sales.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.canonical_writer import expand_financials_rows
from lib.pipeline.db import (
    get_supabase_read_config,
    get_supabase_write_config,
    load_env,
    supabase_upsert,
)
from src.events.summary_financials import extract_earnings_data
from tools.backfill_structural_no_op_jquants_profit_before_tax import _manifest_hash
from tools.fetch_jquants_financials import _fetch_details_actual_metrics, _get_auth_headers


SOURCE_OFFICIAL = "tdnet_xbrl"
APPLY_TOKEN = "I_UNDERSTAND_4TICKER_EXACT_GP_REPAIR"


@dataclass(frozen=True)
class Target:
    ticker: str
    local_code: str
    disclosure_id: str
    disclosure_date: str
    disclosure_time: str
    period: str
    quarter: str
    period_start: str
    period_end: str
    package_sha256: str

    @property
    def disclosure_datetime(self) -> str:
        return f"{self.disclosure_date}T{self.disclosure_time}+09:00"

    @property
    def package_path(self) -> Path:
        return ROOT / "data" / "tdnet_cache" / self.disclosure_id / "xbrl.zip"


TARGETS = (
    Target("1892", "18920", "20250808537826", "2025-08-12", "13:00:00", "2026-03-31", "1Q", "2025-04-01", "2025-06-30", "dd142870715cdcb3a1ce748b8f5225fe2ab2d308f616072e4922aad122ddb26d"),
    Target("1892", "18920", "20251113599261", "2025-11-14", "13:20:00", "2026-03-31", "2Q", "2025-04-01", "2025-09-30", "31b3144a0122e060979aaadc57efae570606f6b13a021ec5df2c3a27dea78032"),
    Target("1892", "18920", "20260212557102", "2026-02-13", "13:00:00", "2026-03-31", "3Q", "2025-04-01", "2025-12-31", "44976be1f988d50d8a745c018df0076b735eeeafabf9067f252edaa9aeecbb85"),
    Target("2148", "21480", "20260430515635", "2026-05-01", "17:00:00", "2026-03-31", "FY", "2025-04-01", "2026-03-31", "979f87a6e034f38eb2d5cf4a7b9f26dc86437d973f648a74198e1138289b3b58"),
    Target("6268", "62680", "20260212556894", "2026-02-12", "16:00:00", "2025-12-31", "FY", "2025-01-01", "2025-12-31", "43513cc80b570123eadb08d67140a23ec8c2d6846b7947e981d12bf8b22c7a58"),
    Target("6268", "62680", "20260430514688", "2026-04-30", "16:00:00", "2026-12-31", "1Q", "2026-01-01", "2026-03-31", "a0ad4971657691590968cff22ff3d1f90d5b6bded2f04944f19e52eded9adcb3"),
    Target("6503", "65030", "20260427512041", "2026-04-28", "15:30:00", "2026-03-31", "FY", "2025-04-01", "2026-03-31", "8b1701ea5cae5f5b77f1bcf7feadf2b50b7bf9198d918a0956220ee6fac89400"),
    Target("6503", "65030", "20260721596543", "2026-07-31", "15:30:00", "2027-03-31", "1Q", "2026-04-01", "2026-06-30", "31f03dda9c5de30fab61960b7071d5c2521c9d900bf614c1644a816671fd5f29"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _millions(value_jpy: int) -> int | float:
    value = Decimal(value_jpy) / Decimal(1_000_000)
    return int(value) if value == value.to_integral() else float(value)


def _load_summary(connection: sqlite3.Connection, target: Target) -> dict[str, Any]:
    rows = connection.execute(
        """SELECT raw_json FROM jquants_financials_normalized
           WHERE local_code=? AND disclosed_date=?
             AND current_fiscal_year_end_date=? AND type_of_current_period=?
             AND type_of_document LIKE '%FinancialStatements%'
           ORDER BY type_of_document""",
        (target.local_code, target.disclosure_date, target.period, target.quarter),
    ).fetchall()
    matching = []
    for row in rows:
        raw = json.loads(row[0] or "{}")
        if str(raw.get("DiscNo") or "") == target.disclosure_id:
            matching.append(raw)
    if len(matching) != 1:
        raise RuntimeError(f"{target.ticker} {target.period} {target.quarter}: exact summary match={len(matching)}")
    raw = matching[0]
    expected = {
        "Code": target.local_code,
        "DiscNo": target.disclosure_id,
        "DiscDate": target.disclosure_date,
        "DiscTime": target.disclosure_time,
        "CurFYEn": target.period,
        "CurPerType": target.quarter,
        "CurPerSt": target.period_start,
        "CurPerEn": target.period_end,
    }
    for key, value in expected.items():
        if str(raw.get(key) or "") != value:
            raise RuntimeError(f"{target.ticker}: summary identity mismatch {key}")
    if "Consolidated" not in str(raw.get("DocType") or ""):
        raise RuntimeError(f"{target.ticker}: non-consolidated document rejected")
    return raw


def _official_gp(target: Target) -> dict[str, Any] | None:
    if not target.package_path.is_file():
        raise RuntimeError(f"official package missing: {target.package_path}")
    actual_hash = _sha256(target.package_path)
    if actual_hash != target.package_sha256:
        raise RuntimeError(f"official package hash mismatch: {target.disclosure_id}")
    result = extract_earnings_data(
        xbrl_path=str(target.package_path),
        title=f"{target.period} {target.quarter} 決算短信（連結）",
        ticker=target.ticker,
        include_evidence=True,
    )
    if result is None or result.gross_profit_current is None:
        return None
    evidence = [
        item for item in result.evidences
        if item.metric == "gross_profit"
        and item.value == result.gross_profit_current
    ]
    if len(evidence) != 1:
        raise RuntimeError(f"{target.ticker}: accepted official GP evidence count={len(evidence)}")
    fact = evidence[0]
    if "grossprofit" not in fact.tag_name.lower():
        raise RuntimeError(f"{target.ticker}: unexpected GP QName {fact.qname}")
    if "member" in fact.context_ref.lower():
        raise RuntimeError(f"{target.ticker}: dimensioned GP context rejected")
    return {
        "raw_value_jpy": int(result.gross_profit_current),
        "normalized_value_millions_jpy": _millions(int(result.gross_profit_current)),
        "qname": fact.qname,
        "namespace": fact.namespace,
        "context_ref": fact.context_ref,
        "unit_ref": "JPY",
        "scale": fact.scale,
        "package_sha256": actual_hash,
    }


def read_canonical(config: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.get(
        f"{config['rest_url']}/canonical_financials",
        params={
            "select": "id,ticker,period,quarter,metric,value,unit,source,source_priority,filing_id,source_row_key,disclosure_datetime,recency_key",
            "ticker": "in.(1892,2148,6268,6503)",
            "metric": "eq.gross_profit",
            "limit": "1000",
        },
        headers=config["headers"],
        timeout=(3, 30),
    )
    response.raise_for_status()
    rows = response.json()
    allowed = {target.ticker for target in TARGETS}
    if any(row.get("ticker") not in allowed for row in rows):
        raise RuntimeError("canonical read escaped exact ticker scope")
    return rows


def _action_for(
    *, official: dict[str, Any] | None, details_gp: int | None,
    existing: list[dict[str, Any]], disclosure_id: str,
) -> str:
    if official is not None:
        exact = [
            row for row in existing
            if row.get("source") == SOURCE_OFFICIAL
            and row.get("filing_id") == disclosure_id
            and Decimal(str(row.get("value"))) == Decimal(str(official["normalized_value_millions_jpy"]))
        ]
        return "NO_ACTION_OFFICIAL_XBRL_EXISTS" if exact else "INSERT_OFFICIAL_XBRL_GP"
    if details_gp is not None:
        exact = [
            row for row in existing
            if row.get("source") == "jquants"
            and Decimal(str(row.get("value"))) == Decimal(str(_millions(details_gp)))
        ]
        return "NO_ACTION_JQUANTS_GP_EXISTS" if exact else "INSERT_JQUANTS_GP"
    return "NO_ACTION_NO_VALID_GP"


def build_manifest(config: dict[str, Any]) -> dict[str, Any]:
    canonical = read_canonical(config)
    connection = sqlite3.connect(ROOT / "data" / "jquants.db")
    auth = _get_auth_headers()
    session = requests.Session()
    rows = []
    try:
        for target in TARGETS:
            summary = _load_summary(connection, target)
            official = _official_gp(target)
            details = _fetch_details_actual_metrics(session, auth, summary)
            details_gp = details.get("gross_profit")
            current = [
                row for row in canonical
                if row.get("ticker") == target.ticker
                and row.get("period") == target.period
                and row.get("quarter") == target.quarter
            ]
            action = _action_for(
                official=official,
                details_gp=details_gp,
                existing=current,
                disclosure_id=target.disclosure_id,
            )
            canonical_row = None
            chosen_source = None
            chosen_value_jpy = None
            chosen_value_millions = None
            if action == "INSERT_OFFICIAL_XBRL_GP":
                chosen_source = SOURCE_OFFICIAL
                chosen_value_jpy = official["raw_value_jpy"]
                chosen_value_millions = official["normalized_value_millions_jpy"]
            elif action == "INSERT_JQUANTS_GP":
                chosen_source = "jquants"
                chosen_value_jpy = int(details_gp)
                chosen_value_millions = _millions(int(details_gp))
            if chosen_source is not None:
                expanded, skipped = expand_financials_rows(
                    ticker=target.ticker,
                    period=target.period,
                    quarter=target.quarter,
                    metrics_dict={"gross_profit": chosen_value_millions},
                    source=chosen_source,
                    filing_id=target.disclosure_id,
                    disclosure_datetime=target.disclosure_datetime,
                    correction_flag=False,
                    unit="millions_jpy",
                )
                if skipped or len(expanded) != 1:
                    raise RuntimeError(f"{target.ticker}: canonical expansion failed")
                canonical_row = expanded[0]
            rows.append({
                **asdict(target),
                "disclosure_datetime": target.disclosure_datetime,
                "official_xbrl_fact": official,
                "jquants_details_gp_jpy": details_gp,
                "existing_canonical_gp": current,
                "chosen_source": chosen_source,
                "chosen_raw_value_jpy": chosen_value_jpy,
                "chosen_normalized_value_millions_jpy": chosen_value_millions,
                "canonical_row": canonical_row,
                "intended_action": action,
            })
    finally:
        connection.close()
        session.close()

    manifest: dict[str, Any] = {
        "scope": {"tickers": ["6268", "6503", "1892", "2148"], "metric": "gross_profit"},
        "forbidden_derivation": "sales_minus_cost_of_sales",
        "expected_insert_count": sum(row["intended_action"].startswith("INSERT_") for row in rows),
        "expected_update_count": 0,
        "expected_delete_count": 0,
        "rows": rows,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    return manifest


def apply_manifest(
    manifest: dict[str, Any], *, expected_hash: str, expected_insert: int,
    apply_token: str,
) -> dict[str, Any]:
    if apply_token != APPLY_TOKEN:
        raise RuntimeError("invalid apply token")
    if manifest.get("manifest_sha256") != expected_hash or _manifest_hash(manifest) != expected_hash:
        raise RuntimeError("manifest hash mismatch")
    if manifest.get("scope") != {"tickers": ["6268", "6503", "1892", "2148"], "metric": "gross_profit"}:
        raise RuntimeError("manifest scope mismatch")
    inserts = [row for row in manifest["rows"] if row["intended_action"].startswith("INSERT_")]
    if len(inserts) != expected_insert or expected_insert != manifest.get("expected_insert_count"):
        raise RuntimeError("expected insert count mismatch")
    if manifest.get("expected_update_count") != 0 or manifest.get("expected_delete_count") != 0:
        raise RuntimeError("unexpected destructive action")
    if any(row["ticker"] not in {"6268", "6503", "1892", "2148"} for row in manifest["rows"]):
        raise RuntimeError("manifest escaped exact ticker scope")

    config = get_supabase_write_config()
    if not config:
        raise RuntimeError("Supabase write configuration unavailable")
    current = build_manifest(config)
    current_actions = [
        (row["ticker"], row["period"], row["quarter"], row["intended_action"], row["chosen_raw_value_jpy"])
        for row in current["rows"]
    ]
    locked_actions = [
        (row["ticker"], row["period"], row["quarter"], row["intended_action"], row["chosen_raw_value_jpy"])
        for row in manifest["rows"]
    ]
    if current_actions != locked_actions:
        raise RuntimeError("source or production state changed after manifest lock")

    result = supabase_upsert(
        "canonical_financials",
        [row["canonical_row"] for row in inserts],
        config=config,
        on_conflict="source_row_key",
        max_retries=1,
    )
    if not result.get("ok") or result.get("count") != expected_insert:
        raise RuntimeError(f"canonical insert failed: {result}")

    post = build_manifest(config)
    remaining = [row for row in post["rows"] if row["intended_action"].startswith("INSERT_")]
    if remaining:
        raise RuntimeError(f"post-repair candidates remain: {len(remaining)}")
    return {
        "manifest_sha256": expected_hash,
        "inserted": expected_insert,
        "updated": 0,
        "deleted": 0,
        "post_actions": [
            {key: row[key] for key in ("ticker", "period", "quarter", "intended_action")}
            for row in post["rows"]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-hash")
    parser.add_argument("--expected-insert", type=int)
    parser.add_argument("--apply-token")
    args = parser.parse_args()

    load_env(ROOT)
    config = get_supabase_read_config()
    if not config:
        raise RuntimeError("Supabase read configuration unavailable")
    if args.dry_run:
        manifest = build_manifest(config)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({
            "manifest": str(args.manifest),
            "manifest_sha256": manifest["manifest_sha256"],
            "expected_insert_count": manifest["expected_insert_count"],
            "expected_update_count": 0,
            "expected_delete_count": 0,
            "actions": [
                {key: row[key] for key in ("ticker", "period", "quarter", "intended_action", "chosen_normalized_value_millions_jpy")}
                for row in manifest["rows"]
            ],
        }, ensure_ascii=False, indent=2))
        return

    if not args.expected_hash or args.expected_insert is None:
        raise RuntimeError("--expected-hash and --expected-insert are required for apply")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    print(json.dumps(apply_manifest(
        manifest,
        expected_hash=args.expected_hash,
        expected_insert=args.expected_insert,
        apply_token=args.apply_token or "",
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
