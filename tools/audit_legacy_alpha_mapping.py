#!/usr/bin/env python3
"""Read-only audit for the retired numeric-to-alpha J-Quants mapping."""
from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2

from lib.pipeline.db import load_env
from src.common_ticker import normalize_ticker

LEGACY_REVISION = "96df6c0^"
LEGACY_PATH = "src/common_ticker.py"
PL_TABLES = {"canonical_financials", "financials"}


def load_legacy_mapping(repo: Path) -> dict[str, str]:
    source = subprocess.check_output(
        ["git", "show", f"{LEGACY_REVISION}:{LEGACY_PATH}"],
        cwd=repo, text=True, encoding="utf-8",
    )
    start = source.index("JQUANTS_ALPHA_MAP:")
    open_brace = source.index("{", start)
    close_brace = source.index("\n}", open_brace) + 2
    return ast.literal_eval(source[open_brace:close_brace])


def fingerprint(table: str, row: dict[str, Any]) -> tuple[Any, ...] | None:
    if table == "canonical_financials":
        return tuple(row.get(k) for k in ("period", "quarter", "metric", "value", "source"))
    if table == "financials":
        return tuple(row.get(k) for k in (
            "period", "quarter", "sales", "gross_profit", "operating_profit", "source"
        ))
    return None


def classify(candidate_count: int, exact_duplicate_count: int) -> str:
    if exact_duplicate_count:
        return "SUSPECTED_REQUIRES_LINEAGE"
    if candidate_count:
        return "CLEAN"
    return "NOT_APPLICABLE"


def audit(connection, mapping: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cur = connection.cursor()
    cur.execute("""
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema='public' AND column_name IN ('ticker','ticker_code','security_code','company_code')
        ORDER BY table_name, column_name
    """)
    identity_columns = cur.fetchall()
    cur.execute("SELECT ticker_code,name_ja FROM public.companies")
    companies = dict(cur.fetchall())

    pairs = [
        {
            "legacy_input_code": legacy_input,
            "alpha_code": alpha_code,
            "numeric_code": normalize_ticker(legacy_input),
            "tables": [],
        }
        for legacy_input, alpha_code in sorted(mapping.items())
    ]
    target_codes = sorted({
        code
        for pair in pairs
        for code in (pair["numeric_code"], pair["alpha_code"])
    })

    for table, column in identity_columns:
        cur.execute(
            f'SELECT "{column}"::text, count(*) FROM public."{table}" '
            f'WHERE "{column}" = ANY(%s) GROUP BY "{column}"',
            (target_codes,),
        )
        counts_by_code = {str(code): int(count) for code, count in cur.fetchall()}
        rows_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if table in PL_TABLES:
            cur.execute(
                f'SELECT to_jsonb(t) FROM public."{table}" t '
                f'WHERE "{column}" = ANY(%s)',
                (target_codes,),
            )
            for (row,) in cur.fetchall():
                rows_by_code[str(row.get(column))].append(row)

        for pair in pairs:
            numeric_code = pair["numeric_code"]
            alpha_code = pair["alpha_code"]
            duplicates = 0
            if table in PL_TABLES:
                numeric_fp = {fingerprint(table, row) for row in rows_by_code[numeric_code]}
                alpha_fp = {fingerprint(table, row) for row in rows_by_code[alpha_code]}
                duplicates = len(numeric_fp & alpha_fp)
            candidates = counts_by_code.get(alpha_code, 0)
            pair["tables"].append({
                "table": table,
                "identity_column": column,
                "numeric_rows": counts_by_code.get(numeric_code, 0),
                "alpha_rows": candidates,
                "exact_cross_code_duplicates": duplicates,
                "classification": classify(candidates, duplicates),
            })

    results: list[dict[str, Any]] = []
    for pair in pairs:
        total_candidates = sum(item["alpha_rows"] for item in pair["tables"])
        total_duplicates = sum(
            item["exact_cross_code_duplicates"] for item in pair["tables"]
        )
        status = classify(total_candidates, total_duplicates)
        results.append({
            "legacy_input_code": pair["legacy_input_code"],
            "legacy_mapped_code": pair["alpha_code"],
            "corrected_normalized_code": pair["numeric_code"],
            "numeric_company_code": pair["numeric_code"],
            "alpha_company_code": pair["alpha_code"],
            "numeric_company_name": companies.get(pair["numeric_code"]),
            "alpha_company_name": companies.get(pair["alpha_code"]),
            "candidate_rows": total_candidates,
            "exact_cross_code_duplicates": total_duplicates,
            "confirmed_contamination_rows": 0,
            "false_positive_rows": 0,
            "classification": status,
            "evidence": (
                "Read-only exact-code counts across every public identity column; "
                "identical PL fingerprints remain suspected unless lineage confirms contamination."
            ),
            "tables": pair["tables"],
        })

    counts = Counter(result["classification"] for result in results)
    by_alpha = {result["alpha_company_code"]: result for result in results}
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "legacy_revision": LEGACY_REVISION,
        "mapping_count": len(mapping),
        "audited_count": len(results),
        "unaudited_count": len(mapping) - len(results),
        "classification_counts": dict(counts),
        "confirmed_contamination_rows": sum(
            result["confirmed_contamination_rows"] for result in results
        ),
        "suspected_mapping_count": counts.get("SUSPECTED_REQUIRES_LINEAGE", 0),
        "suspected_fingerprint_count": sum(
            result["exact_cross_code_duplicates"] for result in results
        ),
        "418A_residual_contamination": by_alpha["418A"]["exact_cross_code_duplicates"],
        "472A_residual_contamination": by_alpha["472A"]["exact_cross_code_duplicates"],
        "tables_audited": [
            {"table": table, "identity_column": column}
            for table, column in identity_columns
        ],
    }
    return results, summary


def write_artifacts(output: Path, results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "full_legacy_mapping_audit.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    columns = [
        "legacy_input_code", "legacy_mapped_code", "corrected_normalized_code",
        "numeric_company_code", "alpha_company_code", "numeric_company_name",
        "alpha_company_name", "candidate_rows", "exact_cross_code_duplicates",
        "confirmed_contamination_rows", "false_positive_rows", "classification", "evidence",
    ]
    with (output / "full_legacy_mapping_audit.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader(); writer.writerows(results)
    (output / "mapping_audit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    mapping = load_legacy_mapping(PROJECT_ROOT)
    load_env()
    connection = psycopg2.connect(os.environ["SUPABASE_POSTGRES_URL"])
    try:
        connection.set_session(readonly=True, autocommit=False)
        results, summary = audit(connection, mapping)
    finally:
        connection.rollback(); connection.close()
    write_artifacts(args.output, results, summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
