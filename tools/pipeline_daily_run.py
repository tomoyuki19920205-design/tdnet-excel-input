#!/usr/bin/env python3
"""
pipeline_daily_run.py — TDnet セグメントパイプライン日次ランナー

日次の自動更新フロー:
  1. TDnet 新着取得 (ingest)
  2. PDF/XBRL 抽出 + DB保存 (process)
  3. Quarantine retry (retry_quarantine_segments)
  4. Summary report 出力 (pipeline_summary_report)

Usage:
  .\\.venv\\Scripts\\python.exe tools\\pipeline_daily_run.py
  .\\.venv\\Scripts\\python.exe tools\\pipeline_daily_run.py --skip-retry
  .\\.venv\\Scripts\\python.exe tools\\pipeline_daily_run.py --dry-run --days 3
"""
from __future__ import annotations

import argparse
import io
import logging
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("daily_pipeline")

PYTHON = sys.executable


def _run_step(name: str, cmd: list[str], *, dry_run: bool = False) -> dict:
    """サブプロセスでステップを実行し、結果を返す。"""
    logger.info(f"{'=' * 55}")
    logger.info(f"  [{name}] START")
    logger.info(f"{'=' * 55}")
    t0 = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,  # 10分タイムアウト
        )
        elapsed = time.monotonic() - t0
        status = "success" if result.returncode == 0 else "warning"

        # 出力を表示
        if result.stdout:
            for line in result.stdout.strip().split("\n")[-20:]:
                print(f"  {line}")
        if result.returncode != 0 and result.stderr:
            for line in result.stderr.strip().split("\n")[-5:]:
                logger.warning(f"  STDERR: {line}")

        logger.info(f"  [{name}] {status.upper()} ({elapsed:.1f}s)")
        return {
            "name": name,
            "status": status,
            "elapsed": round(elapsed, 1),
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        logger.error(f"  [{name}] TIMEOUT ({elapsed:.1f}s)")
        return {"name": name, "status": "timeout", "elapsed": round(elapsed, 1)}
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error(f"  [{name}] ERROR: {e}")
        return {"name": name, "status": "failed", "elapsed": round(elapsed, 1), "error": str(e)}


def main(args: list[str] | None = None) -> int:
    # UTF-8 出力確保
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    parser = argparse.ArgumentParser(description="TDnet セグメントパイプライン日次ランナー")
    parser.add_argument("--dry-run", action="store_true", help="DB更新なし")
    parser.add_argument("--skip-retry", action="store_true", help="quarantine retry をスキップ")
    parser.add_argument("--skip-ingest", action="store_true", help="ingest をスキップ")
    parser.add_argument("--days", type=int, default=1, help="取得日数 (default: 1)")
    parser.add_argument("--retry-limit", type=int, default=50, help="retry 対象件数上限 (default: 50)")
    opts = parser.parse_args(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    t0 = time.monotonic()
    steps: list[dict] = []
    dry_flag = ["--dry-run"] if opts.dry_run else []

    # Step 1: Ingest
    if not opts.skip_ingest:
        step = _run_step("ingest", [
            PYTHON, "tools/pipeline_run.py", "ingest", *dry_flag,
        ], dry_run=opts.dry_run)
        steps.append(step)

    # Step 2: Process
    step = _run_step("process", [
        PYTHON, "tools/pipeline_run.py", "process", "--skip-jquants", *dry_flag,
    ], dry_run=opts.dry_run)
    steps.append(step)

    # Step 3: Quarantine Retry
    if not opts.skip_retry:
        retry_mode = "--dry-run" if opts.dry_run else "--apply"
        step = _run_step("quarantine_retry", [
            PYTHON, "tools/retry_quarantine_segments.py",
            retry_mode,
            "--limit", str(opts.retry_limit),
        ], dry_run=opts.dry_run)
        steps.append(step)

    # Step 4: Summary Report
    step = _run_step("summary_report", [
        PYTHON, "tools/pipeline_summary_report.py",
    ], dry_run=opts.dry_run)
    steps.append(step)

    total_elapsed = time.monotonic() - t0

    # Summary
    print()
    print("=" * 55)
    print("  DAILY PIPELINE SUMMARY")
    print("=" * 55)
    for s in steps:
        icon = {"success": "[OK]", "failed": "[FAIL]", "warning": "[WARN]",
                "timeout": "[TIMEOUT]"}.get(s["status"], "[?]")
        print(f"  {s['name']:25s}: {icon} {s['status']} ({s['elapsed']}s)")
    print("-" * 55)
    mode = "DRY-RUN" if opts.dry_run else "APPLY"
    print(f"  mode                     : {mode}")
    print(f"  total_elapsed            : {total_elapsed:.1f}s")
    print("=" * 55)
    print()

    # 全ステップ成功判定
    failed = [s for s in steps if s["status"] in ("failed", "timeout")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
