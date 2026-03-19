#!/usr/bin/env python3
# ============================================================
# pipeline_run.py — TDnet Pipeline メインエントリポイント
# ============================================================
"""
全パイプライン実行またはサブコマンド単体実行:

    python tools/pipeline_run.py                  # 全ステップ (legacy)
    python tools/pipeline_run.py ingest
    python tools/pipeline_run.py process
    python tools/pipeline_run.py rebuild
    python tools/pipeline_run.py notify
    python tools/pipeline_run.py reconcile
    python tools/pipeline_run.py retry-failed
    python tools/pipeline_run.py backfill --from 2025-01-01 --to 2025-12-31

共通オプション:
    --dry-run         書き込みをスキップ
    --skip-notify     Discord通知をスキップ
    --skip-jquants    J-Quants syncをスキップ
    --trigger         トリガー種別 (scheduler|manual|retry|reconcile)
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env
from lib.pipeline.logging_utils import PipelineRun, check_concurrent_run

logger = logging.getLogger("pipeline")

EXIT_OK = 0
EXIT_INGEST_FAIL = 1
EXIT_REBUILD_FAIL = 2
EXIT_RECONCILE_CRITICAL = 3


# ============================================================
# ステップ結果
# ============================================================
class StepResult:
    """各ステップの実行結果"""
    __slots__ = ("name", "status", "detail", "elapsed")

    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "pending"   # pending | success | failed | skipped | warning
        self.detail: dict = {}
        self.elapsed = 0.0

    def __repr__(self) -> str:
        return f"StepResult({self.name}: {self.status}, {self.elapsed:.1f}s)"


# ============================================================
# 個別ステップ実行
# ============================================================
def _run_ingest(dry_run: bool) -> StepResult:
    step = StepResult("ingest")
    st = time.monotonic()
    try:
        from tools.filings_ingest import run as ingest_run
        result = ingest_run(dry_run=dry_run)
        step.detail = result
        step.status = "success"
    except Exception as e:
        step.status = "failed"
        step.detail = {"error": str(e)}
        logger.error(f"[PIPELINE] ingest FAILED: {e}")
    step.elapsed = time.monotonic() - st
    return step


def _run_process(dry_run: bool, skip_jquants: bool) -> StepResult:
    step = StepResult("process")
    st = time.monotonic()
    try:
        from tools.filings_process import run as process_run
        result = process_run(dry_run=dry_run, skip_jquants=skip_jquants)
        step.detail = result
        push_errors = result.get("push", {}).get("errors", 0)
        step.status = "success" if push_errors == 0 else "warning"
    except Exception as e:
        step.status = "failed"
        step.detail = {"error": str(e)}
        logger.error(f"[PIPELINE] process FAILED: {e}")
    step.elapsed = time.monotonic() - st
    return step


def _run_rebuild(dry_run: bool, ticker: str | None = None) -> StepResult:
    step = StepResult("rebuild")
    st = time.monotonic()
    try:
        from tools.rebuild_serving_views import run as rebuild_run
        result = rebuild_run(dry_run=dry_run, ticker=ticker)
        step.detail = result
        step.status = result.get("status", "success")
    except Exception as e:
        step.status = "warning"
        step.detail = {"error": str(e)}
        logger.warning(f"[PIPELINE] rebuild WARNING: {e}")
    step.elapsed = time.monotonic() - st
    return step


def _run_notify(dry_run: bool) -> StepResult:
    step = StepResult("notify")
    st = time.monotonic()
    try:
        from tools.notify_updates import run as notify_run
        result = notify_run(dry_run=dry_run)
        step.detail = result
        step.status = result.get("status", "success")
    except Exception as e:
        step.status = "warning"
        step.detail = {"error": str(e)}
        logger.warning(f"[PIPELINE] notify WARNING: {e}")
    step.elapsed = time.monotonic() - st
    return step


def _run_reconcile(dry_run: bool) -> StepResult:
    step = StepResult("reconcile")
    st = time.monotonic()
    try:
        from tools.daily_reconcile import run as reconcile_run
        result = reconcile_run(dry_run=dry_run)
        step.detail = result
        step.status = "success"
    except Exception as e:
        step.status = "failed"
        step.detail = {"error": str(e)}
        logger.error(f"[PIPELINE] reconcile FAILED: {e}")
    step.elapsed = time.monotonic() - st
    return step


def _run_retry(dry_run: bool, target_id: str | None = None) -> StepResult:
    step = StepResult("retry")
    st = time.monotonic()
    try:
        from tools.retry_failed_jobs import run as retry_run
        result = retry_run(dry_run=dry_run, target_id=target_id)
        step.detail = result
        step.status = "success"
    except Exception as e:
        step.status = "failed"
        step.detail = {"error": str(e)}
        logger.error(f"[PIPELINE] retry FAILED: {e}")
    step.elapsed = time.monotonic() - st
    return step


# ============================================================
# フルパイプライン実行
# ============================================================
def run_pipeline(
    *,
    dry_run: bool = False,
    skip_notify: bool = False,
    skip_jquants: bool = False,
    trigger_type: str = "manual",
) -> dict:
    """全パイプラインを実行。pipeline_runs にログを記録する。"""
    t0 = time.monotonic()
    steps: list[StepResult] = []

    with PipelineRun("full_pipeline", trigger_type=trigger_type) as pl:
        try:
            # Step 1: ingest
            logger.info("=" * 55)
            logger.info("  [1/4] filings_ingest")
            logger.info("=" * 55)
            step = _run_ingest(dry_run)
            steps.append(step)

            if step.status == "failed":
                pl.update(processed=0, failed=1)
                raise RuntimeError("ingest failed")

            # Step 2: process
            logger.info("=" * 55)
            logger.info("  [2/4] filings_process")
            logger.info("=" * 55)
            step = _run_process(dry_run, skip_jquants)
            steps.append(step)

            if step.status == "failed":
                pl.update(processed=1, success=1, failed=1)
                raise RuntimeError("process failed")

            # Step 3: rebuild
            logger.info("=" * 55)
            logger.info("  [3/4] rebuild_serving_views")
            logger.info("=" * 55)
            step = _run_rebuild(dry_run)
            steps.append(step)

            # Step 4: notify
            logger.info("=" * 55)
            logger.info("  [4/4] notify_updates")
            logger.info("=" * 55)
            if skip_notify:
                step = StepResult("notify")
                step.status = "skipped"
                step.detail = {"reason": "--skip-notify"}
            else:
                step = _run_notify(dry_run)
            steps.append(step)

            # counts 集計
            n_success = sum(1 for s in steps if s.status in ("success", "skipped"))
            n_failed = sum(1 for s in steps if s.status == "failed")
            pl.update(
                processed=len(steps),
                success=n_success,
                failed=n_failed,
            )
        except RuntimeError:
            # PipelineRun.__exit__ が status=failed を記録する
            pass

    elapsed = time.monotonic() - t0
    _print_summary(steps, elapsed)
    return _build_result(steps, elapsed)


def _build_result(steps: list[StepResult], elapsed: float) -> dict:
    step_dict = {s.name: s.status for s in steps}
    overall = _determine_overall(step_dict)
    return {"steps": step_dict, "overall": overall, "elapsed": elapsed}


def _determine_overall(step_dict: dict[str, str]) -> str:
    statuses = set(step_dict.values())
    if "failed" in statuses:
        return "failed"
    if statuses <= {"success", "skipped"}:
        return "success"
    if "warning" in statuses or "error" in statuses:
        return "partial_success"
    return "success"


def _print_summary(steps: list[StepResult], elapsed: float) -> None:
    step_dict = {s.name: s.status for s in steps}
    overall = _determine_overall(step_dict)
    icon = {"success": "[OK]", "failed": "[FAIL]", "partial_success": "[WARN]"}

    print()
    print("=" * 55)
    print("  PIPELINE SUMMARY")
    print("=" * 55)
    for s in steps:
        si = {"success": "[OK]", "failed": "[FAIL]", "skipped": "[SKIP]",
              "warning": "[WARN]", "pending": "[...]"}.get(s.status, "[?]")
        print(f"  {s.name:20s}: {si} {s.status} ({s.elapsed:.1f}s)")
    print("-" * 55)
    print(f"  overall             : {icon.get(overall, '[?]')} {overall}")
    print(f"  elapsed             : {elapsed:.1f}s")
    print("=" * 55)
    print()

    logger.info(
        f"[PIPELINE] overall={overall} "
        + " ".join(f"{s.name}={s.status}" for s in steps)
        + f" elapsed={elapsed:.1f}s"
    )


# ============================================================
# CLI — サブコマンド対応
# ============================================================
def main():
    # UTF-8 出力確保
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    parser = argparse.ArgumentParser(
        description="TDnet Pipeline -- サブコマンドまたは全ステップ実行",
    )
    parser.add_argument(
        "command", nargs="?", default=None,
        choices=["ingest", "process", "rebuild", "notify", "reconcile", "retry-failed", "backfill"],
        help="サブコマンド (省略時は全ステップ実行)",
    )
    parser.add_argument("--dry-run", action="store_true", help="書き込みをスキップ")
    parser.add_argument("--skip-notify", action="store_true", help="Discord通知をスキップ")
    parser.add_argument("--skip-jquants", action="store_true", help="J-Quants syncをスキップ")
    parser.add_argument("--ticker", type=str, help="特定 ticker (rebuild/process)")
    parser.add_argument("--trigger", type=str, default="manual",
                        choices=["scheduler", "manual", "retry", "reconcile"],
                        help="トリガー種別")
    parser.add_argument("--from", dest="from_date", type=str, help="開始日 (backfill)")
    parser.add_argument("--to", dest="to_date", type=str, help="終了日 (backfill)")
    parser.add_argument("--target-id", type=str, help="retry対象 target_id")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    load_env(_PROJECT_ROOT)

    # サブコマンド
    cmd = args.command

    if cmd is None:
        # 全ステップ (legacy 互換)
        if args.trigger == "scheduler":
            if check_concurrent_run("full_pipeline"):
                logger.info("[PIPELINE] concurrent run detected, skipping")
                sys.exit(EXIT_OK)

        result = run_pipeline(
            dry_run=args.dry_run,
            skip_notify=args.skip_notify,
            skip_jquants=args.skip_jquants,
            trigger_type=args.trigger,
        )
        overall = result["overall"]
        if overall == "failed":
            sys.exit(EXIT_INGEST_FAIL)
        elif overall == "partial_success":
            sys.exit(EXIT_REBUILD_FAIL)
        else:
            sys.exit(EXIT_OK)

    # サブコマンド個別実行 — 必ず PipelineRun で logging
    if args.trigger == "scheduler":
        if check_concurrent_run(cmd):
            logger.info(f"[PIPELINE] concurrent run detected for {cmd}, skipping")
            sys.exit(EXIT_OK)

    def _run_subcommand_with_logging(
        job_type: str,
        run_fn,
        *,
        trigger_type: str = "manual",
    ) -> StepResult:
        """サブコマンドを PipelineRun context で包んで実行。
        pipeline_runs に running → done/failed を必ず記録する。
        """
        try:
            with PipelineRun(job_type, trigger_type=trigger_type) as pl:
                step = run_fn()
                # counts を PipelineRun に反映
                detail = step.detail or {}
                pl.update(
                    processed=detail.get("total", detail.get("processed_count", 0)),
                    success=detail.get("success", detail.get("success_count", 0)),
                    failed=detail.get("failed", detail.get("failed_count", 0)),
                    quarantined=detail.get("quarantined", detail.get("quarantined_count", 0)),
                    skipped=detail.get("skipped", detail.get("skipped_count", 0)),
                )
                if step.status == "failed":
                    raise RuntimeError(
                        step.detail.get("error", "step failed")
                    )
                return step
        except RuntimeError:
            # PipelineRun.__exit__ が status=failed を書く
            return step
        except Exception as e:
            logger.error(f"[PIPELINE] {job_type} unexpected error: {e}")
            step = StepResult(job_type)
            step.status = "failed"
            step.detail = {"error": str(e)}
            return step

    trigger = args.trigger

    if cmd == "ingest":
        step = _run_subcommand_with_logging(
            "ingest", lambda: _run_ingest(args.dry_run), trigger_type=trigger,
        )
        sys.exit(EXIT_OK if step.status == "success" else EXIT_INGEST_FAIL)

    elif cmd == "process":
        step = _run_subcommand_with_logging(
            "process", lambda: _run_process(args.dry_run, args.skip_jquants),
            trigger_type=trigger,
        )
        sys.exit(EXIT_OK if step.status in ("success", "warning") else EXIT_INGEST_FAIL)

    elif cmd == "rebuild":
        step = _run_subcommand_with_logging(
            "rebuild", lambda: _run_rebuild(args.dry_run, ticker=args.ticker),
            trigger_type=trigger,
        )
        sys.exit(EXIT_OK if step.status != "failed" else EXIT_REBUILD_FAIL)

    elif cmd == "notify":
        step = _run_subcommand_with_logging(
            "notify", lambda: _run_notify(args.dry_run), trigger_type=trigger,
        )
        sys.exit(EXIT_OK)  # notify failure は非致命的

    elif cmd == "reconcile":
        step = _run_subcommand_with_logging(
            "reconcile", lambda: _run_reconcile(args.dry_run), trigger_type=trigger,
        )
        issues = step.detail.get("issues_total", 0)
        if step.status == "failed" or issues >= 10:
            sys.exit(EXIT_RECONCILE_CRITICAL)
        sys.exit(EXIT_OK)

    elif cmd == "retry-failed":
        step = _run_subcommand_with_logging(
            "retry", lambda: _run_retry(args.dry_run, target_id=args.target_id),
            trigger_type=trigger,
        )
        sys.exit(EXIT_OK if step.status == "success" else EXIT_INGEST_FAIL)

    elif cmd == "backfill":
        with PipelineRun("backfill", trigger_type=trigger) as pl:
            from tools.backfill_filings import main as backfill_main
            sys.argv = ["backfill_filings.py"]
            if args.from_date:
                sys.argv += ["--from", args.from_date]
            if args.to_date:
                sys.argv += ["--to", args.to_date]
            if args.dry_run:
                sys.argv += ["--dry-run"]
            backfill_main()

    else:
        parser.print_help()
        sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
