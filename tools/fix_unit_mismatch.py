#!/usr/bin/env python3
"""tools/fix_unit_mismatch.py — Supabase data unit + period fix

Scans financials/canonical_financials by source to avoid offset limits.

CLI:
  python tools/fix_unit_mismatch.py --dry-run
  python tools/fix_unit_mismatch.py --apply
"""
from __future__ import annotations
import argparse, io, logging, os, sys, time
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
for stream in (sys.stdout, sys.stderr):
    if stream and hasattr(stream, "encoding") and stream.encoding and stream.encoding.lower() not in ("utf-8","utf8"):
        setattr(sys, stream.name if hasattr(stream,'name') else 'stdout',
                io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))

logger = logging.getLogger("fix_unit")
M = 1_000_000
THRESH = 100_000_000  # >= this = likely yen, not millions
COLS = ["sales", "gross_profit", "operating_profit"]
SOURCES = ["jquants", "tdnet", "summary_xbrl", "attachment_xbrl", "pdf", "html"]
KEYSET_BATCH_SIZE = 1000


class API:
    def __init__(self, url, key):
        import requests; self.R = requests
        self.base = url.rstrip("/") + "/rest/v1"; self.key = key
    def _h(self, p=""): return {"apikey":self.key,"Authorization":f"Bearer {self.key}","Content-Type":"application/json",**({"Prefer":p} if p else {})}
    def get(self, tbl, p):
        for attempt in range(4):
            r = self.R.get(f"{self.base}/{tbl}", headers=self._h(), params=p, timeout=60)
            if r.status_code in (500, 502, 503, 504) and attempt < 3:
                wait = 2 ** (attempt + 1)
                logger.warning(f"  GET {tbl} returned {r.status_code}, retry {attempt+1}/3 in {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
    def patch(self, tbl, filt, data):
        p = {k:f"eq.{v}" for k,v in filt.items()}
        r=self.R.patch(f"{self.base}/{tbl}",headers=self._h("return=representation"),params=p,json=data,timeout=30); r.raise_for_status(); return r.json()


def _paginate(api, tbl, params, batch=1000):
    """Paginate GET with offset."""
    all_rows = []
    offset = 0
    while True:
        p = {**params, "limit": str(batch), "offset": str(offset)}
        rows = api.get(tbl, p)
        all_rows.extend(rows)
        if len(rows) < batch:
            break
        offset += batch
        if offset > 20000:  # safety cap per source
            logger.warning(f"  offset cap reached at {offset}")
            break
    return all_rows


def fix_financials(api, dry_run=True):
    stats = {"checked":0, "needs_fix":0, "fixed":0, "errors":0, "samples":[]}

    for src in SOURCES:
        rows = _paginate(api, "financials", {
            "select": "ticker,period,quarter,sales,gross_profit,operating_profit,source",
            "source": f"eq.{src}",
            "or": f"(sales.gte.{THRESH},sales.lte.{-THRESH},"
                  f"gross_profit.gte.{THRESH},gross_profit.lte.{-THRESH},"
                  f"operating_profit.gte.{THRESH},operating_profit.lte.{-THRESH})",
            "order": "ticker,period.desc",
        })
        logger.info(f"  [{src}] yen-scale rows: {len(rows)}")
        stats["checked"] += len(rows)

        for row in rows:
            upd = {}
            for c in COLS:
                v = row.get(c)
                if v is not None and abs(v) >= THRESH:
                    upd[c] = int(v / M)
            if not upd:
                continue
            stats["needs_fix"] += 1
            if len(stats["samples"]) < 20:
                s = {"ticker":row["ticker"],"period":row["period"],"quarter":row["quarter"],"source":src}
                for c in upd: s[f"{c}_before"]=row[c]; s[f"{c}_after"]=upd[c]
                stats["samples"].append(s)
            if not dry_run:
                try:
                    api.patch("financials",
                              {"ticker":row["ticker"],"period":row["period"],"quarter":row["quarter"]},
                              upd)
                    stats["fixed"] += 1
                except Exception as e:
                    logger.error(f"  ERR {row['ticker']} {row['period']}: {e}")
                    stats["errors"] += 1
    return stats


def _fix_canonical_jquants(api, stats, dry_run, verbose=False):
    """Keyset pagination for canonical_financials / source=jquants."""
    last_key = None
    batch_no = 0
    total_scanned = 0
    total_candidate = 0
    total_updated = 0
    total_skipped = 0

    while True:
        params = {
            "select": "source_row_key,ticker,period,quarter,metric,value,unit,source",
            "source": "eq.jquants",
            "order": "source_row_key.asc",
            "limit": str(KEYSET_BATCH_SIZE),
        }
        if last_key is not None:
            params["source_row_key"] = f"gt.{last_key}"

        rows = api.get("canonical_financials", params)
        if not rows:
            break

        batch_no += 1
        scanned = len(rows)
        candidate = 0
        updated = 0
        skipped = 0

        new_last_key = rows[-1].get("source_row_key")
        if not new_last_key:
            raise RuntimeError(f"source_row_key missing in keyset batch #{batch_no}")
        if last_key is not None and new_last_key == last_key:
            raise RuntimeError(f"keyset stalled at source_row_key={last_key}")

        for row in rows:
            srk = row.get("source_row_key")
            if not srk:
                logger.warning(f"  skipping row with null source_row_key")
                skipped += 1
                continue

            v = row.get("value")
            if v is None or not isinstance(v, (int, float)):
                skipped += 1
                continue
            if abs(v) < THRESH:
                skipped += 1
                continue

            nv = int(v / M)
            candidate += 1
            stats["needs_fix"] += 1

            if len(stats["samples"]) < 20:
                stats["samples"].append({
                    "ticker": row["ticker"], "period": row["period"],
                    "quarter": row["quarter"], "metric": row["metric"],
                    "source": "jquants", "before": v, "after": nv})

            if not dry_run:
                try:
                    api.patch("canonical_financials",
                              {"source_row_key": srk},
                              {"value": nv, "unit": "millions_jpy"})
                    stats["fixed"] += 1
                    updated += 1
                except Exception as e:
                    logger.error(f"  ERR {row['ticker']} srk={srk}: {e}")
                    stats["errors"] += 1
            else:
                updated += 1

        total_scanned += scanned
        total_candidate += candidate
        total_updated += updated
        total_skipped += scanned - candidate

        if verbose or batch_no % 10 == 0:
            logger.info(f"  [jquants] batch={batch_no} scanned={scanned} "
                        f"candidate={candidate} updated={updated} "
                        f"cum_scanned={total_scanned} last_key={new_last_key}")

        last_key = new_last_key

    stats["checked"] += total_scanned
    logger.info(f"  [jquants] DONE total_scanned={total_scanned} "
                f"total_candidate={total_candidate} "
                f"total_updated={total_updated} "
                f"total_skipped={total_scanned - total_candidate} "
                f"batches={batch_no} final_last_key={last_key}")


def fix_canonical(api, dry_run=True, verbose=False, jquants_only=False):
    stats = {"checked":0, "needs_fix":0, "fixed":0, "errors":0, "samples":[]}

    # 1) jquants: keyset pagination (offset制限回避)
    _fix_canonical_jquants(api, stats, dry_run, verbose)

    if jquants_only:
        logger.info("  [non-jquants] SKIPPED (--jquants-only)")
        return stats

    # 2) 非jquants: 既存の offset pagination (件数少ないため問題なし)
    for src in SOURCES:
        if src == "jquants":
            continue
        rows = _paginate(api, "canonical_financials", {
            "select": "source_row_key,ticker,period,quarter,metric,value,unit,source",
            "source": f"eq.{src}",
            "or": f"(value.gte.{THRESH},value.lte.{-THRESH})",
            "order": "ticker,period.desc",
        })
        logger.info(f"  [{src}] yen-scale rows: {len(rows)}")
        stats["checked"] += len(rows)

        for row in rows:
            v = row.get("value")
            if v is None or abs(v) < THRESH:
                continue
            nv = int(v / M)
            stats["needs_fix"] += 1
            if len(stats["samples"]) < 20:
                stats["samples"].append({
                    "ticker":row["ticker"],"period":row["period"],"quarter":row["quarter"],
                    "metric":row["metric"],"source":src,"before":v,"after":nv})
            if not dry_run:
                try:
                    api.patch("canonical_financials",
                              {"source_row_key":row["source_row_key"]},
                              {"value":nv, "unit":"millions_jpy"})
                    stats["fixed"] += 1
                except Exception as e:
                    logger.error(f"  ERR {row['ticker']}: {e}")
                    stats["errors"] += 1
    return stats


def kosel_report(api):
    """Kosel 6905 / 69050 detail."""
    data = {}
    for t in ["6905", "69050"]:
        rows = api.get("financials", {
            "select":"ticker,period,quarter,sales,gross_profit,operating_profit,source",
            "ticker":f"eq.{t}", "order":"period.desc,quarter.desc"})
        data[t] = rows
    return data


def period_report(api):
    """Period inconsistency detection (first 3000 rows sample)."""
    rows = api.get("financials", {
        "select":"ticker,period,quarter,source",
        "limit":"3000", "order":"ticker,period.desc"})
    tqp = defaultdict(set)
    for r in rows:
        tqp[(r["ticker"],r["quarter"])].add(r["period"])
    cases = []
    for (t,q), ps in sorted(tqp.items()):
        if len(ps) > 1:
            cases.append({"ticker":t,"quarter":q,"periods":sorted(ps)})
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--canonical-only", action="store_true",
                        help="Skip financials table, only process canonical_financials")
    parser.add_argument("--jquants-only", action="store_true",
                        help="In canonical, only process jquants (skip other sources)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    dry_run = not args.apply
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    from lib.pipeline.db import load_env; load_env()
    url = os.environ.get("SUPABASE_URL","")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY","")
    if not url or not key: print("ERROR: need SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY"); sys.exit(1)

    api = API(url, key)
    mode = "DRY-RUN" if dry_run else "APPLY"
    scope = "canonical-only" if args.canonical_only else "all"
    print(f"\n{'='*70}\n  Unit + Period Fix ({mode}, scope={scope})\n{'='*70}")

    fin = {"checked":0, "needs_fix":0, "fixed":0, "errors":0, "samples":[]}

    # Phase 1a: financials (skip if --canonical-only)
    if not args.canonical_only:
        print("\n== Phase 1: financials ==")
        fin = fix_financials(api, dry_run)
        print(f"  total yen-scale: {fin['checked']}  needs_fix: {fin['needs_fix']}  "
              f"fixed: {fin['fixed']}  errors: {fin['errors']}")
        for s in fin["samples"][:10]:
            parts = [f"{c}: {s[f'{c}_before']}->{s[f'{c}_after']}" for c in COLS if f"{c}_before" in s]
            print(f"    {s['ticker']} {s['period']} {s['quarter']} src={s['source']}: {', '.join(parts)}")
    else:
        print("\n== Phase 1: financials == SKIPPED (--canonical-only)")

    # Phase 1b: canonical_financials
    print("\n== Phase 1: canonical_financials ==")
    can = fix_canonical(api, dry_run, verbose=args.verbose, jquants_only=args.jquants_only)
    print(f"  total yen-scale: {can['checked']}  needs_fix: {can['needs_fix']}  "
          f"fixed: {can['fixed']}  errors: {can['errors']}")
    for s in can["samples"][:10]:
        print(f"    {s['ticker']} {s['period']} {s['quarter']} {s['metric']} "
              f"src={s['source']}: {s['before']}->{s['after']}")

    # Phase 2: Kosel detail
    print("\n== Kosel 6905 Detail ==")
    kd = kosel_report(api)
    for t in ["6905","69050"]:
        if kd[t]:
            print(f"\n  ticker={t}:")
            for r in kd[t]:
                print(f"    period={r['period']} q={r['quarter']} sales={r['sales']} "
                      f"gp={r['gross_profit']} op={r['operating_profit']} src={r['source']}")

    # Phase 3: Period inconsistency (skip if --canonical-only)
    if not args.canonical_only:
        print("\n== Period Inconsistencies (sample) ==")
        cases = period_report(api)
        print(f"  cases found: {len(cases)}")
        for c in cases[:10]:
            print(f"    {c['ticker']} {c['quarter']}: {c['periods']}")

    total = fin["needs_fix"] + can["needs_fix"]
    print(f"\n{'='*70}")
    if dry_run:
        print(f"  DRY-RUN: {total} rows need fix")
        if total: print(f"  Run: python tools/fix_unit_mismatch.py --apply --canonical-only")
    else:
        print(f"  DONE: {fin['fixed']+can['fixed']} rows fixed")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
