#!/usr/bin/env python3
# ============================================================
# scheduler_reconcile.py — Reconcile オーケストレータ
# ============================================================
"""
取りこぼし修復専用ジョブ。

スケジュール: 毎日 18:35（Realtime非衝突時刻）
処理フロー:
    1. reconcile.lock + tdnet_pipeline.lock 取得
    2. ingest済み未process検出 → 再投入
    3. process済み未notify検出 → 再投入
    4. ログ出力 → lock解放

Usage:
    python tools/scheduler_reconcile.py
    python tools/scheduler_reconcile.py --dry-run
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.file_lock import acquire_dual_lock, release_dual_lock

logger = logging.getLogger("scheduler.reconcile")

JST = timezone(timedelta(hours=9))
PYTHON = os.path.join(_PROJECT_ROOT, ".venv", "Scripts", "python.exe")

TASK_NAME = "TDNET_Reconcile"
GLOBAL_LOCK_MAX_AGE = 60  # 分
JOB_LOCK_MAX_AGE = 15     # 分


# ============================================================
# subprocess ステップ実行
# ============================================================

class StepResult:
    __slots__ = ("name", "rc", "duration", "status", "stdout_tail", "stderr_tail")

    def __init__(self, name: str) -> None:
        self.name = name
        self.rc = -1
        self.duration = 0.0
        self.status = "pending"
        self.stdout_tail = ""
        self.stderr_tail = ""

    def __repr__(self) -> str:
        return f"Step({self.name}: {self.status}, rc={self.rc}, {self.duration:.1f}s)"


def run_step(
    name: str,
    cmd: list[str],
    *,
    timeout_sec: int = 120,
    cwd: str = _PROJECT_ROOT,
) -> StepResult:
    step = StepResult(name)
    logger.info(f"[{TASK_NAME}] step={name} START cmd={' '.join(cmd)}")
    t0 = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        step.rc = result.returncode
        step.duration = time.monotonic() - t0
        step.status = "success" if result.returncode == 0 else "warning"
        step.stdout_tail = (result.stdout or "")[-500:].strip()
        step.stderr_tail = (result.stderr or "")[-300:].strip()

        if result.stdout:
            for line in result.stdout.strip().split("\n")[-5:]:
                logger.info(f"  [{name}] {line.strip()}")

    except subprocess.TimeoutExpired:
        step.duration = time.monotonic() - t0
        step.status = "timeout"
        step.rc = -1
        logger.error(
            f"[{TASK_NAME}] step={name} TIMEOUT after {step.duration:.1f}s"
        )

    except Exception as e:
        step.duration = time.monotonic() - t0
        step.status = "error"
        step.rc = -1
        logger.error(f"[{TASK_NAME}] step={name} ERROR: {e}")

    logger.info(
        f"[{TASK_NAME}] step={name} {step.status.upper()} "
        f"rc={step.rc} duration={step.duration:.1f}s"
    )
    return step


# ============================================================
# メインフロー
# ============================================================

def main() -> int:
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    parser = argparse.ArgumentParser(description="TDNET Reconcile Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="書き込みをスキップ")
    args = parser.parse_args()

    # ── ログ設定 ──
    now = datetime.now(JST)
    log_dir = os.path.join(_PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"reconcile_scheduled_{now.strftime('%Y%m%d')}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

    run_id = f"{now.strftime('%H%M%S')}_{os.getpid()}"
    logger.info(f"{'=' * 55}")
    logger.info(f"  {TASK_NAME} START run_id={run_id}")
    logger.info(f"  dry_run={args.dry_run}")
    logger.info(f"{'=' * 55}")

    t_start = time.monotonic()

    # ── Lock 取得 ──
    locks = acquire_dual_lock(
        "reconcile",
        global_max_age=GLOBAL_LOCK_MAX_AGE,
        job_max_age=JOB_LOCK_MAX_AGE,
    )
    if locks is None:
        logger.info(f"[{TASK_NAME}] skip_reason=lock_held")
        _print_summary([], time.monotonic() - t_start, skip_reason="lock_held")
        return 0

    global_lock, job_lock = locks
    steps: list[StepResult] = []
    dry_flag = ["--dry-run"] if args.dry_run else []

    try:
        # ── Step 1: reconcile（未処理検出 + 再投入） ──
        step = run_step("reconcile", [
            PYTHON, "tools/pipeline_run.py", "reconcile",
            "--trigger", "reconcile", *dry_flag,
        ], timeout_sec=180)
        steps.append(step)

        # ── Step 2: retry-failed（失敗リトライ） ──
        step = run_step("retry-failed", [
            PYTHON, "tools/pipeline_run.py", "retry-failed",
            "--trigger", "reconcile", *dry_flag,
        ], timeout_sec=180)
        steps.append(step)

        elapsed = time.monotonic() - t_start
        _print_summary(steps, elapsed)

        failed = [s for s in steps if s.status in ("error", "timeout")]
        return 1 if failed else 0

    finally:
        release_dual_lock(global_lock, job_lock)


def _print_summary(
    steps: list[StepResult],
    elapsed: float,
    *,
    skip_reason: str | None = None,
) -> None:
    print()
    print("=" * 55)
    print(f"  {TASK_NAME} SUMMARY")
    print("=" * 55)

    if skip_reason:
        print(f"  skip_reason         : {skip_reason}")
    else:
        for s in steps:
            icon = {
                "success": "[OK]", "warning": "[WARN]", "error": "[FAIL]",
                "timeout": "[TOUT]", "pending": "[...]",
            }.get(s.status, "[?]")
            print(f"  {s.name:20s}: {icon} rc={s.rc} ({s.duration:.1f}s)")

    print("-" * 55)
    print(f"  elapsed             : {elapsed:.1f}s")
    print("=" * 55)
    print()

    step_info = " ".join(
        f"{s.name}={s.status}({s.duration:.1f}s)" for s in steps
    )
    logger.info(
        f"task={TASK_NAME} run_id=summary "
        f"elapsed={elapsed:.1f}s "
        f"skip_reason={skip_reason or 'none'} "
        f"{step_info}"
    )


if __name__ == "__main__":
    sys.exit(main())
