#!/usr/bin/env python3
"""Resume the remaining J-Quants five-year market backfill in isolated stages.

Each stage owns a non-overlapping date range, SQLite file, and checkpoint.  This
avoids both refetching the already-completed primary range and concurrent writes
to the production SQLite database.  Only after every stage returns zero are the
staged rows atomically merged into the primary database and synced to Supabase.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PRIMARY_DB = ROOT / "data" / "jquants.db"
STAGES = (
    ("01", "2021-08-01", "2022-11-09"),
    ("02", "2022-11-10", "2023-08-31"),
    ("03", "2023-09-01", "2024-12-31"),
    ("04", "2025-01-01", "2026-08-01"),
)
RUN_ID = "repair_v2_code_identity"
MANIFEST = ROOT / "data" / f"jquants_market_backfill_{RUN_ID}_manifest.json"


def stage_paths(name: str) -> tuple[Path, Path, Path, Path, Path]:
    base = ROOT / "data"
    return (
        base / f"jquants_market_{RUN_ID}_stage_{name}.db",
        base / f"jquants_market_{RUN_ID}_stage_{name}.tmp.db",
        base / f"jquants_market_{RUN_ID}_stage_{name}_progress.json",
        ROOT / "logs" / f"jquants_market_{RUN_ID}_stage_{name}.out.log",
        ROOT / "logs" / f"jquants_market_{RUN_ID}_stage_{name}.err.log",
    )


def write_and_validate_manifest() -> None:
    previous_end = None
    entries = []
    for name, start, end in STAGES:
        if start > end or (previous_end is not None and start <= previous_end):
            raise RuntimeError(f"overlapping or invalid stage range: {name} {start}..{end}")
        entries.append({"stage": name, "start": start, "end": end})
        previous_end = end
    payload = {"version": 1, "stages": entries, "ranges_non_overlapping": True}
    temporary = MANIFEST.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(MANIFEST)


def run_stages() -> None:
    write_and_validate_manifest()
    processes: list[tuple[str, subprocess.Popen]] = []
    for name, start, end in STAGES:
        final_db, temporary_db, progress, out, err = stage_paths(name)
        # A published file is the stage's completion marker.  Never recreate its
        # temporary database on a retry: a completed checkpoint would otherwise
        # skip every date and publish an empty replacement.
        if final_db.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(PYTHON), "-X", "utf8", "tools/fetch_jquants_prices.py",
            "--db", str(temporary_db), "--backfill", "--date-mode", "--resume",
            "--since", start, "--until", end, "--progress-file", str(progress),
        ]
        with out.open("ab") as stdout, err.open("ab") as stderr:
            processes.append((name, subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)))
    failures = []
    for name, process in processes:
        rc = process.wait()
        if rc:
            failures.append(f"stage {name}: rc={rc}")
            continue
        final_db, temporary_db, _, _, _ = stage_paths(name)
        if not temporary_db.exists():
            failures.append(f"stage {name}: temporary database missing")
            continue
        temporary_db.replace(final_db)
    if failures:
        raise RuntimeError("; ".join(failures))


def merge_stages() -> dict[str, int]:
    conn = sqlite3.connect(PRIMARY_DB)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        before = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
        stage_rows = 0
        aliases: list[str] = []
        # ATTACH/DETACH are intentionally outside the write transaction.  SQLite
        # can reject DETACH while a transaction is active, which would otherwise
        # turn a fully completed staged run into a failed merge.
        for index, (name, _, _) in enumerate(STAGES):
            db, _, _, _, _ = stage_paths(name)
            if not db.exists():
                raise RuntimeError(f"stage {name} was not atomically published")
            alias = f"stage{index}"
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(db),))
            aliases.append(alias)
        conn.execute("BEGIN IMMEDIATE")
        for alias in aliases:
            stage_rows += conn.execute(f"SELECT COUNT(*) FROM {alias}.market_data").fetchone()[0]
            conn.execute(f"INSERT OR REPLACE INTO market_data SELECT * FROM {alias}.market_data")
            conn.execute(f"INSERT OR REPLACE INTO market_data_universe SELECT * FROM {alias}.market_data_universe")
        conn.commit()
        # Closing the connection releases attached databases.  Explicit DETACH
        # after multi-database writes can remain locked by SQLite's internal
        # statement cache even after COMMIT, so do not risk a false failed merge.
        conn.execute("ANALYZE")
        after = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
        return {"before": before, "stage_rows": stage_rows, "after": after}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_supabase() -> None:
    result = subprocess.run(
        [str(PYTHON), "-X", "utf8", "tools/sync_market_data.py", "--apply", "--full"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Supabase full sync failed rc={result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-sync", action="store_true")
    args = parser.parse_args()
    run_stages()
    stats = merge_stages()
    print(f"MERGE {stats}")
    if not args.skip_sync:
        sync_supabase()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
