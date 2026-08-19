#!/usr/bin/env python3
# ============================================================
# scheduler_nightly.py — Nightly オーケストレータ
# ============================================================
"""
夜間の重処理統合実行。

スケジュール: 毎日 19:00
処理フロー:
    1. nightly.lock + tdnet_pipeline.lock 取得
    2. ingest 補完（取りこぼし取得）
    3. process（フルモード: PDF/XBRL/iXBRL解析）
    4. reconcile 完全（DB vs TDNET差分チェック）
    5. rebuild（summary/cache/view再生成）
    6. ログ出力 → lock解放

Usage:
    python tools/scheduler_nightly.py
    python tools/scheduler_nightly.py --dry-run
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

logger = logging.getLogger("scheduler.nightly")

JST = timezone(timedelta(hours=9))
PYTHON = os.path.join(_PROJECT_ROOT, ".venv", "Scripts", "python.exe")

TASK_NAME = "TDNET_Nightly"
GLOBAL_LOCK_MAX_AGE = 120  # 分（nightly は長時間なので余裕を持つ）
JOB_LOCK_MAX_AGE = 120     # 分


# ============================================================
# subprocess ステップ実行（scheduler_realtime.py と同一パターン）
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
    timeout_sec: int | None = None,
    cwd: str = _PROJECT_ROOT,
) -> StepResult:
    """subprocess でパイプラインステップを実行。timeout_sec=None で無制限。"""
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
            for line in result.stdout.strip().split("\n")[-10:]:
                logger.info(f"  [{name}] {line.strip()}")
        if result.returncode != 0 and result.stderr:
            for line in result.stderr.strip().split("\n")[-5:]:
                logger.warning(f"  [{name}] STDERR: {line.strip()}")

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

    parser = argparse.ArgumentParser(description="TDNET Nightly Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="書き込みをスキップ")
    args = parser.parse_args()

    # ── ログ設定 ──
    now = datetime.now(JST)
    log_dir = os.path.join(_PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"nightly_{now.strftime('%Y%m%d')}.log")

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
        "nightly",
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
        # ── Step 1: ingest 補完 ──
        step = run_step("ingest", [
            PYTHON, "tools/pipeline_run.py", "ingest",
            "--trigger", "scheduler", *dry_flag,
        ], timeout_sec=300)
        steps.append(step)

        # ── Step 2: process-batch（フルモード） ──
        step = run_step("process-batch", [
            PYTHON, "tools/pipeline_run.py", "process-batch",
            "--trigger", "scheduler",
            "--skip-jquants-sync",
            *dry_flag,
        ], timeout_sec=1800)  # 30分
        steps.append(step)

        # ── Step 3: reconcile 完全 ──
        step = run_step("reconcile", [
            PYTHON, "tools/pipeline_run.py", "reconcile",
            "--trigger", "scheduler", *dry_flag,
        ], timeout_sec=600)
        steps.append(step)

        # ── Step 4: J-Quants 財務取得（直近30日分）+ details gross_profit 補完 ──
        step = run_step("fetch-jquants-fin", [
            PYTHON, "-X", "utf8",
            "tools/fetch_jquants_financials.py",
            "--recent-days", "30",
            *([] if args.dry_run else ["--apply"]),
            "--enable-details-gp",  # /v2/fins/details から gross_profit を自動補完
        ], timeout_sec=900)         # details 補完分で処理時間が延びるため 600→900
        steps.append(step)

        # ── Step 5: sync_financials（J-Quants DB → Supabase financials） ──
        step = run_step("sync-jquants-fin", [
            PYTHON, "-X", "utf8",
            "tools/sync_financials.py",
            "--recent", "30",
            *([] if args.dry_run else ["--apply"]),
        ], timeout_sec=300)
        steps.append(step)

        # Pending financial gaps are rechecked only after J-Quants sync.  An
        # existing canonical key resolves the job without another write.
        step = run_step("financial-recovery", [
            PYTHON, "-X", "utf8",
            "tools/financial_recovery_retry.py",
            *dry_flag,
        ], timeout_sec=300)
        steps.append(step)

        # ── Step 5a: extract_per_share_from_raw（jquants.db raw_json → per_share_data） ──
        logger.info("[PER_SHARE] extract start")
        step = run_step("per-share-extract", [
            PYTHON, "-X", "utf8",
            "tools/extract_per_share_from_raw.py",
            "--db", "data/jquants.db",
            *([] if args.dry_run else ["--apply"]),
        ], timeout_sec=300)
        steps.append(step)
        if step.rc == 0:
            logger.info(f"[PER_SHARE] extract done rc={step.rc}")
        else:
            logger.warning(f"[PER_SHARE] extract done rc={step.rc} status={step.status}")

        # ── Step 5b: fetch_jquants_prices（J-Quants → jquants.db prices） ──
        logger.info("[MARKET_DATA] step=market-price-fetch START")
        market_fetch_step = run_step("market-price-fetch", [
            PYTHON, "-X", "utf8",
            "tools/fetch_jquants_prices.py",
            "--recent",
        ], timeout_sec=900)
        steps.append(market_fetch_step)
        if market_fetch_step.rc == 0:
            logger.info(
                f"[MARKET_DATA] step=market-price-fetch OK rc={market_fetch_step.rc}"
            )
        else:
            logger.error(
                f"[MARKET_DATA] step=market-price-fetch FAIL rc={market_fetch_step.rc} "
                f"status={market_fetch_step.status}"
            )

        # ── Step 5c: sync_market_data（jquants.db prices → Supabase market_data） ──
        if market_fetch_step.rc != 0:
            logger.warning(
                "[MARKET_DATA] step=market-data-sync SKIP "
                f"reason=market-price-fetch-failed rc={market_fetch_step.rc}"
            )
            market_sync_step = StepResult("market-data-sync")
            market_sync_step.rc = -1
            market_sync_step.status = "warning"
            market_sync_step.duration = 0.0
            market_sync_step.stdout_tail = (
                f"SKIPPED: market-price-fetch failed rc={market_fetch_step.rc}"
            )
            steps.append(market_sync_step)
        else:
            logger.info("[MARKET_DATA] step=market-data-sync START")
            market_sync_step = run_step("market-data-sync", [
                PYTHON, "-X", "utf8",
                "tools/sync_market_data.py",
                *([] if args.dry_run else ["--apply"]),
            ], timeout_sec=300)
            steps.append(market_sync_step)
            if market_sync_step.rc == 0:
                logger.info(
                    f"[MARKET_DATA] step=market-data-sync OK rc={market_sync_step.rc}"
                )
            else:
                logger.error(
                    f"[MARKET_DATA] step=market-data-sync FAIL rc={market_sync_step.rc} "
                    f"status={market_sync_step.status} (later steps unaffected)"
                )

        # ── Step 5d: sync_per_share_data（per_share_data → Supabase） ──
        logger.info("[PER_SHARE] sync start")
        step = run_step("per-share-sync", [
            PYTHON, "-X", "utf8",
            "tools/sync_per_share_data.py",
            *([] if args.dry_run else ["--apply"]),
        ], timeout_sec=300)
        steps.append(step)
        if step.rc == 0:
            logger.info(f"[PER_SHARE] sync done rc={step.rc}")
        else:
            logger.error(f"[PER_SHARE] sync done rc={step.rc} status={step.status} (Supabase sync FAILED)")

        # ── Step 7a: EDINET受注抽出・edinet_order_data更新（前段） ──
        # 当日提出有報のXBRL/HTMLを取得し、受注高・受注残高を抽出してDBに保存する。
        # dry-run時はDB書き込みなし。apply時のみ edinet_order_data へ保存。
        # この前段なしに Step 7b を実行すると、古いDBを見るだけになり SKIP_PERIOD_MISMATCH が多発する。
        logger.info(f"[EDINET_ORDER] step=edinet-order-extract-nightly START")
        jst_today = datetime.now(JST).strftime("%Y-%m-%d")
        edinet_apply_flag_7a = [] if args.dry_run else ["--apply"]
        step_7a = run_step("edinet-order-extract-nightly", [
            PYTHON, "-X", "utf8",
            "run_edinet_orders.py",
            "--date", jst_today,
            *edinet_apply_flag_7a,
            "--no-notify",
            "--max-docs", "1000",  # デフォルト50件制限を回避（有報+XBRL最大1000件まで処理）
        ], timeout_sec=1800)
        steps.append(step_7a)

        if step_7a.rc == 0:
            logger.info(
                f"[EDINET_ORDER] extract done rc={step_7a.rc} status={step_7a.status}"
            )
        else:
            logger.error(
                f"[EDINET_ORDER] extract FAILED rc={step_7a.rc} status={step_7a.status} "
                f"(edinet-order-extract-nightly) \u2014 SKIP Step 7b"
            )

        # ── Step 7b: EDINET受注イベント生成・tdnet_events保存（後段） ──
        # edinet_order_data に保存済みの受注データを読み、通知イベントを生成する。
        # Step 7a が rc != 0 の場合は DBが更新されていないため Step 7b をスキップする。
        # notify_to_discord=false は generate_edinet_order_events.py 側で強制されている。
        if step_7a.rc != 0:
            logger.warning(
                "[EDINET_ORDER] step=edinet-order-event-nightly SKIPPED "
                f"(reason: extract_step_failed rc={step_7a.rc})"
            )
            # skippedとして記録するダミーStepResultを追加
            skip_step = StepResult("edinet-order-event-nightly")
            skip_step.rc = -1
            skip_step.status = "warning"
            skip_step.duration = 0.0
            skip_step.stdout_tail = "SKIPPED: extract step failed"
            steps.append(skip_step)
        else:
            logger.info(f"[EDINET_ORDER] step=edinet-order-event-nightly START")
            edinet_apply_flag_7b = [] if args.dry_run else ["--apply"]
            step_7b = run_step("edinet-order-event-nightly", [
                PYTHON, "-X", "utf8",
                "tools/generate_edinet_order_events.py",
                "--date", jst_today,
                *edinet_apply_flag_7b,
            ], timeout_sec=600)
            steps.append(step_7b)
            if step_7b.rc == 0:
                logger.info(
                    f"[EDINET_ORDER] event done rc={step_7b.rc} status={step_7b.status}"
                )
            else:
                logger.warning(
                    f"[EDINET_ORDER] event FAILED rc={step_7b.rc} status={step_7b.status} "
                    f"(edinet-order-event-nightly, other steps unaffected)"
                )

        # Incrementally build/repair the TSE-wide source registry.  This is a
        # bounded same-domain crawl and is resumable across Nightly runs.
        if not args.dry_run:
            step = run_step("company-ir-source-discovery", [
                PYTHON, "-X", "utf8", "tools/company_ir_source_discovery.py",
                "--batch-size", "250", "--repair",
            ], timeout_sec=1800)
            steps.append(step)

        # Nightly-only: company official IR materials/videos.  The global DB
        # gate remains fail-closed until all-company baseline validation is
        # explicitly completed.
        step = run_step("company-ir-monitor", [
            PYTHON, "-X", "utf8", "tools/company_ir_nightly.py",
            "--require-discovery-complete", *dry_flag,
        ], timeout_sec=1800)
        steps.append(step)


        elapsed = time.monotonic() - t_start
        _print_summary(steps, elapsed)

        failed = [s for s in steps if s.status in ("warning", "error", "timeout")]
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
