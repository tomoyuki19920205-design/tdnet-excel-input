import argparse
import json
import sys
from collections import defaultdict

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to JSONL output")
    args = parser.parse_args()

    stats = defaultdict(lambda: {"total": 0, "ok": 0, "partial": 0, "quarantined": 0})

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                worker_status = data.get("worker_status")
                if not worker_status:
                    continue
                if data.get("fallback_used") is not True:
                    continue
                if worker_status not in ["ok", "partial", "quarantined"]:
                    continue
                reason = data.get("fallback_reason")
                if not reason:
                    reason = "unknown"
                stats[reason]["total"] += 1
                stats[reason][worker_status] += 1

    except FileNotFoundError:
        print(f"Error: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    print("=== Fallback Analysis ===")
    for reason, counts in stats.items():
        total = counts["total"]
        if total == 0:
            continue
            
        ok_rate = (counts["ok"] / total) * 100
        
        print(f"\n[{reason}]")
        print(f"total: {total}")
        print(f"ok: {counts['ok']}")
        print(f"partial: {counts['partial']}")
        print(f"quarantined: {counts['quarantined']}")
        print(f"ok_rate: {ok_rate:.1f}%")

if __name__ == "__main__":
    main()
