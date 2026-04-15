"""tools/inspect_fallback_cases.py — fallback成功/失敗の差分比較"""
import argparse, json, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    args = ap.parse_args()

    records = []
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("event") != "filing_result":
                continue
            if not r.get("fallback_used"):
                continue
            records.append(r)

    def status_of(r):
        return r.get("worker_status") or r.get("status") or ""

    success = [r for r in records if status_of(r) == "ok"]
    fail = [r for r in records if status_of(r) == "quarantined"]
    total = len(records)
    s_rate = (len(success) / total * 100) if total else 0

    print("=" * 50)
    print("  Fallback Summary")
    print("=" * 50)
    print(f"  total:        {total}")
    print(f"  success:      {len(success)}")
    print(f"  fail:         {len(fail)}")
    print(f"  success_rate: {s_rate:.1f}%")

    # fallback_reason別
    print("\n" + "=" * 50)
    print("  By fallback_reason")
    print("=" * 50)
    reasons = sorted(set(r.get("fallback_reason") or "" for r in records))
    for reason in reasons:
        sub = [r for r in records if (r.get("fallback_reason") or "") == reason]
        ok = sum(1 for r in sub if status_of(r) == "ok")
        q = sum(1 for r in sub if status_of(r) == "quarantined")
        rate = (ok / len(sub) * 100) if sub else 0
        print(f"  {reason or '(empty)'}:")
        print(f"    total={len(sub)}  ok={ok}  quarantined={q}  ok_rate={rate:.1f}%")

    def val(r, k):
        v = r.get(k)
        return v if isinstance(v, (int, float)) else 0

    def print_list(title, items):
        print(f"\n{'=' * 50}")
        print(f"  {title} ({len(items)})")
        print("=" * 50)
        for r in items:
            fid = r.get("filing_id") or ""
            tk = r.get("ticker") or ""
            fr = r.get("fallback_reason") or ""
            rows = val(r, "rows")
            sc = val(r, "segment_count")
            ms = val(r, "duration_ms")
            via = r.get("via") or ""
            print(f"  {fid}  ticker={tk}  reason={fr}  rows={rows}  seg={sc}  ms={ms}  via={via}")

    print_list("Success Cases", success)
    print_list("Fail Cases", fail)

    def avg(items, k):
        vals = [val(r, k) for r in items]
        return sum(vals) / len(vals) if vals else 0

    print(f"\n{'=' * 50}")
    print("  Avg Comparison (success vs fail)")
    print("=" * 50)
    for k in ("rows", "segment_count", "duration_ms"):
        s_avg = avg(success, k)
        f_avg = avg(fail, k)
        print(f"  {k:20s}  success={s_avg:8.1f}  fail={f_avg:8.1f}")

if __name__ == "__main__":
    main()
