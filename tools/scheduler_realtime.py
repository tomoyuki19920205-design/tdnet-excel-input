#!/usr/bin/env python3
# ============================================================
# scheduler_realtime.py — Realtime オーケストレータ
# ============================================================
"""
営業日中の軽量パイプライン統合実行。

スケジュール: 平日 08:30-18:00, 10分間隔
処理フロー:
    1. 15秒待機（TDNET反映遅延対策）
    2. realtime.lock + tdnet_pipeline.lock 取得
    3. ingest（増分のみ）
    4. process（realtimeモード）
    5. notify
    6. light reconcile（当日分のみ）
    7. ログ出力 → lock解放

Usage:
    python tools/scheduler_realtime.py
    python tools/scheduler_realtime.py --dry-run
    python tools/scheduler_realtime.py --skip-delay
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

logger = logging.getLogger("scheduler.realtime")

JST = timezone(timedelta(hours=9))
PYTHON = os.path.join(_PROJECT_ROOT, ".venv", "Scripts", "python.exe")

# ── step 定義 ──────────────────────────────────────────────
TASK_NAME = "TDNET_Realtime"
DEADLINE_MINUTES = 8
STARTUP_DELAY_SEC = 15
GLOBAL_LOCK_MAX_AGE = 60  # 分
JOB_LOCK_MAX_AGE = 15     # 分


# ============================================================
# subprocess ステップ実行ヘルパー
# ============================================================

class StepResult:
    """各 step の実行結果"""
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
    """subprocess でパイプラインステップを実行。"""
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
        step.stdout_tail = _tail(result.stdout, 500)
        step.stderr_tail = _tail(result.stderr, 300)

        if result.stdout:
            for line in result.stdout.strip().split("\n")[-5:]:
                logger.info(f"  [{name}] {line.strip()}")

    except subprocess.TimeoutExpired:
        step.duration = time.monotonic() - t0
        step.status = "timeout"
        step.rc = -1
        logger.error(
            f"[{TASK_NAME}] step={name} TIMEOUT after {step.duration:.1f}s "
            f"(limit={timeout_sec}s)"
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


def _tail(text: str, max_chars: int) -> str:
    """テキストの末尾を取得"""
    if not text:
        return ""
    return text[-max_chars:].strip()


# ============================================================
# メインフロー
# ============================================================

def main() -> int:
    # UTF-8 出力確保
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    parser = argparse.ArgumentParser(description="TDNET Realtime Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="書き込みをスキップ")
    parser.add_argument("--skip-delay", action="store_true", help="起動遅延をスキップ")
    args = parser.parse_args()

    # ── ログ設定 ──
    now = datetime.now(JST)
    log_dir = os.path.join(_PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"realtime_{now.strftime('%Y%m%d')}.log")

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
    logger.info(f"  dry_run={args.dry_run} skip_delay={args.skip_delay}")
    logger.info(f"{'=' * 55}")

    t_start = time.monotonic()
    _now = datetime.now(JST)
    logger.info(f"[TIMING] task_started_at={_now.isoformat(timespec='seconds')}")

    # ── Step 0: 起動遅延 ──
    if not args.skip_delay:
        logger.info(f"[TIMING] startup_delay_start_at={datetime.now(JST).isoformat(timespec='seconds')}")
        logger.info(f"[{TASK_NAME}] startup delay: {STARTUP_DELAY_SEC}s")
        time.sleep(STARTUP_DELAY_SEC)
        logger.info(f"[TIMING] startup_delay_end_at={datetime.now(JST).isoformat(timespec='seconds')}")

    # ── Lock 取得 ──
    locks = acquire_dual_lock(
        "realtime",
        global_max_age=GLOBAL_LOCK_MAX_AGE,
        job_max_age=JOB_LOCK_MAX_AGE,
    )
    if locks is None:
        logger.info(
            f"[{TASK_NAME}] skip_reason=lock_held "
            f"duration={time.monotonic() - t_start:.1f}s"
        )
        _print_summary([], time.monotonic() - t_start, skip_reason="lock_held")
        return 0

    global_lock, job_lock = locks

    try:
        deadline = time.monotonic() + (DEADLINE_MINUTES * 60)
        steps: list[StepResult] = []
        dry_flag = ["--dry-run"] if args.dry_run else []

        # ── Step 1: ingest ──
        if time.monotonic() < deadline:
            logger.info(f"[TIMING] ingest_start_at={datetime.now(JST).isoformat(timespec='seconds')}")
            step = run_step("ingest", [
                PYTHON, "tools/pipeline_run.py", "ingest",
                "--trigger", "scheduler", *dry_flag,
            ], timeout_sec=90)
            steps.append(step)
            logger.info(f"[TIMING] ingest_end_at={datetime.now(JST).isoformat(timespec='seconds')} ingest_sec={step.duration:.1f}")
        else:
            logger.info(f"[{TASK_NAME}] deadline exceeded, skipping ingest")

        # ── Step 2: process-realtime (queue 駆動の軽量差分処理) ──
        if time.monotonic() < deadline:
            logger.info(f"[TIMING] process_start_at={datetime.now(JST).isoformat(timespec='seconds')}")
            step = run_step("process-realtime", [
                PYTHON, "tools/pipeline_run.py", "process-realtime",
                "--trigger", "scheduler",
                *dry_flag,
            ], timeout_sec=180)
            steps.append(step)
            logger.info(f"[TIMING] process_end_at={datetime.now(JST).isoformat(timespec='seconds')} process_sec={step.duration:.1f}")
        else:
            logger.info(f"[{TASK_NAME}] deadline exceeded, skipping process-realtime")

        # ── Step 3: notify ──
        if time.monotonic() < deadline:
            logger.info(f"[TIMING] notify_start_at={datetime.now(JST).isoformat(timespec='seconds')}")
            step = run_step("notify", [
                PYTHON, "tools/pipeline_run.py", "notify",
                "--trigger", "scheduler", *dry_flag,
            ], timeout_sec=60)
            steps.append(step)
            logger.info(f"[TIMING] notify_end_at={datetime.now(JST).isoformat(timespec='seconds')} notify_sec={step.duration:.1f}")
        else:
            logger.info(f"[{TASK_NAME}] deadline exceeded, skipping notify")

        # ── Step 4: light reconcile (当日分のみ) ──
        if time.monotonic() < deadline:
            logger.info(f"[TIMING] reconcile_start_at={datetime.now(JST).isoformat(timespec='seconds')}")
            step = run_step("reconcile", [
                PYTHON, "tools/pipeline_run.py", "reconcile",
                "--trigger", "scheduler", *dry_flag,
            ], timeout_sec=60)
            steps.append(step)
            logger.info(f"[TIMING] reconcile_end_at={datetime.now(JST).isoformat(timespec='seconds')} reconcile_sec={step.duration:.1f}")
        else:
            logger.info(f"[{TASK_NAME}] deadline exceeded, skipping reconcile")

        elapsed = time.monotonic() - t_start
        deadline_exceeded = elapsed > (DEADLINE_MINUTES * 60)
        logger.info(f"[TIMING] total_elapsed_sec={elapsed:.1f}")
        _print_summary(steps, elapsed, deadline_exceeded=deadline_exceeded)

        # 失敗判定
        failed = [s for s in steps if s.status in ("error", "timeout")]
        return 1 if failed else 0

    finally:
        release_dual_lock(global_lock, job_lock)


def _print_summary(
    steps: list[StepResult],
    elapsed: float,
    *,
    skip_reason: str | None = None,
    deadline_exceeded: bool = False,
) -> None:
    """実行結果サマリーを出力。"""
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
    if deadline_exceeded:
        print(f"  deadline_exceeded   : YES (>{DEADLINE_MINUTES}min)")
    print("=" * 55)
    print()

    # key=value ログ
    step_info = " ".join(
        f"{s.name}={s.status}({s.duration:.1f}s)" for s in steps
    )
    logger.info(
        f"task={TASK_NAME} run_id=summary "
        f"elapsed={elapsed:.1f}s "
        f"deadline_exceeded={deadline_exceeded} "
        f"skip_reason={skip_reason or 'none'} "
        f"{step_info}"
    )


if __name__ == "__main__":
    sys.exit(main())
