#!/usr/bin/env python3
"""Validate and atomically ingest company_news_v1 files from the local inbox."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.news_monitor import NewsValidationError, connect_news_db, record_failed_run, upsert_run, validate_payload


def ingest_file(path: Path, db_path: Path, processed: Path, quarantine: Path) -> bool:
    payload = None
    conn = connect_news_db(db_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        run = validate_payload(payload)  # validate every item before opening the write transaction
        upsert_run(conn, run)
        processed.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(processed / path.name))
        return True
    except (OSError, json.JSONDecodeError, NewsValidationError, ValueError) as exc:
        record_failed_run(conn, payload, exc)
        quarantine.mkdir(parents=True, exist_ok=True)
        target = quarantine / path.name
        shutil.move(str(path), str(target))
        target.with_suffix(target.suffix + ".error.txt").write_text(str(exc), encoding="utf-8")
        print(f"QUARANTINE {path.name}: {exc}", file=sys.stderr)
        return False
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--db", type=Path, default=ROOT / "decision_db.db")
    parser.add_argument("--inbox", type=Path, default=ROOT / "data" / "news_inbox")
    args = parser.parse_args()
    paths = args.paths or sorted(args.inbox.glob("*.json"))
    processed, quarantine = args.inbox / "processed", args.inbox / "quarantine"
    failed = sum(not ingest_file(path, args.db, processed, quarantine) for path in paths)
    print(f"processed={len(paths) - failed} quarantined={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
