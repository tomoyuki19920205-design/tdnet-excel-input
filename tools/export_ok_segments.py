import csv
import json
import sys
from pathlib import Path

def load_records(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    text_strip = text.lstrip()

    # JSON array
    if text_strip.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):
            return data

    # JSONL
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records

def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and k in d:
            return d[k]
    return None

def main():
    if len(sys.argv) < 3:
        print("usage: python tools/export_ok_segments.py <input.json|jsonl> <output.csv>")
        sys.exit(1)

    in_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    records = load_records(in_path)
    rows = []

    for rec in records:
        mode = rec.get("detected_mode", "")
        qr   = rec.get("quarantine_reason", "")
        pq   = rec.get("parse_quality", "")

        # quarantine は除外
        if mode == "quarantine" or qr:
            continue
        # mode が ROW_BASED / COL_AS_SEG 以外は除外
        if mode not in ("ROW_BASED", "COL_AS_SEG"):
            continue

        # ok / partial の判定
        if pq == "full":
            status = "ok"
        elif pq in ("partial", "sales_only"):
            status = "partial"
        else:
            status = "ok"   # parse_quality 未設定でも mode が有効なら ok 扱い

        ticker  = rec.get("ticker") or ""
        company = pick(rec, "company_name", "company", "filer_name") or ""

        segments_raw = rec.get("segments_json", "[]")
        try:
            segments = json.loads(segments_raw)
        except Exception:
            segments = []

        segment_count = len(segments)

        for i, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            seg_name = pick(seg, "segment_name", "name") or ""
            sales    = pick(seg, "sales", "segment_sales")
            profit   = pick(seg, "profit", "segment_profit")
            rows.append({
                "status":          status,
                "ticker":          ticker,
                "company_name":    company,
                "segment_count":   segment_count,
                "segment_index":   i + 1,
                "segment_name":    seg_name if seg_name is not None else "",
                "sales":           "" if sales is None else sales,
                "profit":          "" if profit is None else profit,
            })

    # ticker → segment_index でソート
    rows.sort(key=lambda r: (r["ticker"], r["segment_index"]))

    fieldnames = [
        "status", "ticker", "company_name",
        "segment_count", "segment_index",
        "segment_name", "sales", "profit",
    ]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows to {out_path}")

if __name__ == "__main__":
    main()
