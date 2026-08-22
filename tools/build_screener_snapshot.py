#!/usr/bin/env python3
"""Build and validate the local stock-screener snapshot (read-only by default)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.screener_snapshot import build_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "jquants.db"))
    parser.add_argument("--as-of")
    parser.add_argument("--output", help="Optional JSON output path; omitted means no writes")
    parser.add_argument("--include-rows", action="store_true")
    args = parser.parse_args()

    result = build_snapshot(args.db, as_of=args.as_of)
    payload = {
        "batch_id": result.batch_id,
        "universe_date": result.universe_date,
        "row_count": len(result.rows),
        "revision_event_count": len(result.revision_events),
        "coverage": result.coverage,
        "null_reasons": result.null_reasons,
    }
    if args.include_rows:
        payload["rows"] = result.rows
        payload["revision_events"] = result.revision_events
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(output)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
