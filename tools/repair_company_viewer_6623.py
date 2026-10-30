"""Audited, repeatable Company Viewer repair for Aichi Electric (6623).

Dry run by default. `--apply` updates the existing canonical and wide tables
and their existing API views in one transaction. No forecast or future period
is synthesized. See the adjacent reference JSON for per-cell PDF provenance.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
from datetime import date
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

from lib.pipeline.canonical_writer import expand_financials_rows, expand_segments_rows

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tools/reference/aichi_6623_2024_2027.json"
MIGRATION = ROOT / "migrations/022_company_viewer_financial_detail.sql"
SOURCE = "official_ir_verified"


def _settings_value(settings_root: Path) -> str:
    value = os.environ.get("SUPABASE_POSTGRES_URL", "")
    if value:
        return value
    for filename in (".env", ".env.local"):
        path = settings_root / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("SUPABASE_POSTGRES_URL="):
                value = line.partition("=")[2].strip().strip('"').strip("'")
    if not value:
        raise RuntimeError("SUPABASE_POSTGRES_URL is unavailable")
    return value


def _quarter_end(fiscal_end: str, quarter: str) -> date:
    fy = date.fromisoformat(fiscal_end)
    q = 4 if quarter == "FY" else int(quarter[0])
    month_index = fy.year * 12 + fy.month - 1 - (4 - q) * 3
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, calendar.monthrange(year, month)[1])


def _filing_id(source: dict, page: int) -> str:
    return f"official_ir|{source['sha256']}|{source['url']}|p{page}"


def build_rows(manifest: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    financial, segments, wide = [], [], []
    seen = set()
    for item in manifest:
        assert item["ticker"] == "6623"
        assert item["scope"] == "consolidated_actual_ytd"
        assert item["unit"] == "millions_jpy"
        key = (item["ticker"], item["period"], item["quarter"])
        assert key not in seen, key
        seen.add(key)
        assert _quarter_end(item["period"], item["quarter"]) <= date.fromisoformat(item["disclosure_date"])
        pl_source = item["pl_source"]
        disclosure = item["disclosure_date"] + "T00:00:00+09:00"
        # EPS has yen/share units and is on the first page of each filing.
        for metric, value in item["pl"].items():
            if value is None:
                continue  # A dash in the filing is absent, not an invented zero.
            page = pl_source["eps_pdf_page"] if metric == "eps" else pl_source["pdf_page"]
            rows, skipped = expand_financials_rows(
                ticker="6623", period=item["period"], quarter=item["quarter"],
                metrics_dict={metric: value}, source=SOURCE,
                filing_id=_filing_id(pl_source, page),
                disclosure_datetime=disclosure,
                unit="yen_per_share" if metric == "eps" else "millions_jpy",
            )
            assert len(rows) == 1 and skipped == 0, (key, metric)
            row = rows[0]
            row["unit"] = "yen_per_share" if metric == "eps" else "millions_jpy"
            row["document_type"] = "tanshin"
            row["period_start"] = f"{int(item['period'][:4])-1}-04-01"
            row["period_end"] = _quarter_end(item["period"], item["quarter"]).isoformat()
            financial.append(row)

        segment_source = item["segment_source"]
        segment_disclosure = segment_source["disclosure_date"] + "T00:00:00+09:00"
        segment_input = [
            {
                "segment_name": s["name"], "sales": s["external_sales"],
                "sales_with_internal": s["sales_with_internal"], "profit": s["profit"],
                "source_system": "official_ir", "segment_type": "ordinary",
                "derivation_method": "recast_comparative" if segment_source["comparison_recast"] else "reported_ytd",
            }
            for s in item["segments"]
        ]
        rows, skipped = expand_segments_rows(
            ticker="6623", period=item["period"], quarter=item["quarter"],
            segments=segment_input, source=SOURCE,
            filing_id=_filing_id(segment_source, segment_source["pdf_page"]),
            disclosure_datetime=segment_disclosure, unit="millions_jpy",
        )
        assert len(rows) == 9 and skipped == 0, key
        for row in rows:
            row["source_disclosure_period"] = _quarter_end(item["period"], item["quarter"]).isoformat()
            row["source_disclosure_date"] = segment_disclosure
            row["flags"] = json.dumps({
                "source_url": segment_source["url"], "pdf_page": segment_source["pdf_page"],
                "comparison_recast": segment_source["comparison_recast"],
            }, ensure_ascii=False)
        segments.extend(rows)
        wide.append({
            "ticker": "6623", "period": item["period"], "quarter": item["quarter"],
            "sales": item["pl"]["sales"], "gross_profit": item["pl"]["gross_profit"],
            "operating_profit": item["pl"]["operating_profit"], "source": SOURCE,
            "unit": "millions_jpy",
        })
    assert len(financial) == 193 and len(segments) == 126 and len(wide) == 14
    return financial, segments, wide


def _existing(cur, table: str, columns: str) -> list[dict]:
    cur.execute(f"SELECT {columns} FROM {table} WHERE ticker = %s AND period BETWEEN %s AND %s",
                ("6623", "2024-03-31", "2027-03-31"))
    names = [c.name for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def plan(cur, financial: list[dict], segments: list[dict], wide: list[dict]) -> dict:
    old_fin = {r["source_row_key"]: r for r in _existing(cur, "canonical_financials", "source_row_key,value,unit")}
    old_seg = {r["source_row_key"]: r for r in _existing(cur, "canonical_segments", "source_row_key,value,unit")}
    old_wide = {(r["period"], r["quarter"]): r for r in _existing(
        cur, "financials", "period,quarter,sales,gross_profit,operating_profit,source")}

    def changes(rows, old):
        missing = changed = unchanged = 0
        for row in rows:
            prior = old.get(row["source_row_key"])
            if prior is None:
                missing += 1
            elif float(prior["value"]) != float(row["value"]) or prior["unit"] != row["unit"]:
                changed += 1
            else:
                unchanged += 1
        return {"insert": missing, "update": changed, "unchanged": unchanged}

    wide_changes = []
    for row in wide:
        prior = old_wide.get((row["period"], row["quarter"]))
        fields = [field for field in ("sales", "gross_profit", "operating_profit")
                  if prior is None or prior[field] is None or float(prior[field]) != float(row[field])]
        if fields:
            wide_changes.append({"period": row["period"], "quarter": row["quarter"],
                                 "fields": fields, "previous": prior, "expected": row})
    return {
        "ticker": "6623", "periods": len(wide),
        "canonical_financials": changes(financial, old_fin),
        "canonical_segments": changes(segments, old_seg),
        "financials_wide_changed_periods": len(wide_changes),
        "financials_wide_changes": wide_changes,
    }


def _upsert_canonical(cur, table: str, rows: list[dict], columns: list[str]) -> None:
    template = "(" + ",".join(["%s"] * len(columns)) + ")"
    update = ",".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in ("source_row_key", "ticker", "period", "quarter", "metric"))
    sql = (f"INSERT INTO {table} ({','.join(columns)}) VALUES %s "
           f"ON CONFLICT (source_row_key) DO UPDATE SET {update} "
           f"WHERE {table}.value IS DISTINCT FROM EXCLUDED.value "
           f"OR {table}.unit IS DISTINCT FROM EXCLUDED.unit "
           f"OR {table}.source_priority IS DISTINCT FROM EXCLUDED.source_priority")
    execute_values(cur, sql, [[row.get(c) for c in columns] for row in rows], template=template, page_size=100)


def apply(cur, financial: list[dict], segments: list[dict], wide: list[dict],
          wide_change_keys: set[tuple[str, str]]) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("company_viewer_6623_repair",))
    cur.execute(MIGRATION.read_text(encoding="utf-8"))
    _upsert_canonical(cur, "canonical_financials", financial, [
        "ticker", "period", "quarter", "metric", "value", "unit", "source", "source_priority",
        "filing_id", "source_row_key", "disclosure_datetime", "correction_flag", "recency_key",
        "updated_at", "document_type", "period_start", "period_end",
    ])
    _upsert_canonical(cur, "canonical_segments", segments, [
        "ticker", "period", "quarter", "segment_name", "segment_key", "metric", "value", "unit",
        "source", "source_system", "source_priority", "segment_type", "derivation_method",
        "filing_id", "source_row_key", "disclosure_datetime", "correction_flag", "recency_key",
        "updated_at", "source_disclosure_period", "source_disclosure_date", "flags",
    ])
    changed_wide = [r for r in wide if (r["period"], r["quarter"]) in wide_change_keys]
    if not changed_wide:
        return
    execute_values(cur, """
        INSERT INTO financials
        (ticker,period,quarter,sales,gross_profit,operating_profit,source,unit,updated_at,normalized_at)
        VALUES %s ON CONFLICT (ticker,period,quarter) DO UPDATE SET
          sales=EXCLUDED.sales, gross_profit=EXCLUDED.gross_profit,
          operating_profit=EXCLUDED.operating_profit, source=EXCLUDED.source,
          unit=EXCLUDED.unit, updated_at=EXCLUDED.updated_at,
          normalized_at=EXCLUDED.normalized_at
        WHERE financials.sales IS DISTINCT FROM EXCLUDED.sales
           OR financials.gross_profit IS DISTINCT FROM EXCLUDED.gross_profit
           OR financials.operating_profit IS DISTINCT FROM EXCLUDED.operating_profit
           OR financials.source IS DISTINCT FROM EXCLUDED.source
    """, [[r["ticker"], r["period"], r["quarter"], r["sales"], r["gross_profit"],
           r["operating_profit"], SOURCE, "millions_jpy", date.today(), date.today()]
          for r in changed_wide], page_size=100)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-root", type=Path,
                        default=Path(r"C:\Users\takuy\tdnet-production\settings"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-json", type=Path)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    financial, segments, wide = build_rows(manifest)
    con = psycopg2.connect(_settings_value(args.settings_root))
    try:
        con.set_session(readonly=not args.apply)
        with con:
            with con.cursor() as cur:
                before = plan(cur, financial, segments, wide)
                print(json.dumps(before, ensure_ascii=False, indent=2, default=str))
                if args.plan_json:
                    args.plan_json.write_text(json.dumps(before, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
                if args.apply:
                    wide_change_keys = {
                        (r["period"], r["quarter"])
                        for r in before["financials_wide_changes"]
                    }
                    apply(cur, financial, segments, wide, wide_change_keys)
                    after = plan(cur, financial, segments, wide)
                    assert after["canonical_financials"]["insert"] == 0
                    assert after["canonical_segments"]["insert"] == 0
                    assert after["financials_wide_changed_periods"] == 0
                    print("AFTER", json.dumps(after, ensure_ascii=False, default=str))
    finally:
        con.close()


if __name__ == "__main__":
    main()
