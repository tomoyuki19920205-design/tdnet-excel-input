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

from lib.runtime_paths import runtime_path
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
def _run_ingest(dry_run: bool, yanoshin_timeout_sec: float | None = None) -> StepResult:
    step = StepResult("ingest")
    st = time.monotonic()
    try:
        from tools.filings_ingest import run as ingest_run
        result = ingest_run(dry_run=dry_run, yanoshin_timeout_sec=yanoshin_timeout_sec)
        # run_ingest は個別開示の失敗を例外にせず、summary.errors で返す。
        # これを無視すると scheduler まで exit 0 となり、実データの欠損を
        # 正常実行として監視してしまう。
        summary = result.get("summary") or {}
        old_parser_errors = int(summary.get("errors", 0) or 0)
        errors = int(
            summary.get(
                "fatal_errors",
                summary.get("unresolved_financial_errors", old_parser_errors),
            ) or 0
        )
        normalized = dict(result)
        normalized.setdefault("success", summary.get("success", summary.get("succeeded", 0)))
        normalized["failed"] = errors
        normalized["old_parser_errors"] = old_parser_errors
        normalized.setdefault("skipped", summary.get("skipped", 0))
        normalized.setdefault("quarantined", summary.get("quarantined", errors))
        step.detail = normalized
        step.status = "success" if errors == 0 else "failed"
        if errors:
            logger.error(
                "[PIPELINE] ingest completed with item failures: "
                "total=%s success=%s fatal_errors=%s old_parser_errors=%s skipped=%s",
                result.get("total", 0),
                normalized["success"],
                errors,
                old_parser_errors,
                normalized["skipped"],
            )
    except Exception as e:
        step.status = "failed"
        step.detail = {"error": str(e)}
        logger.error(f"[PIPELINE] ingest FAILED: {e}")
    step.elapsed = time.monotonic() - st
    return step


def _run_process(dry_run: bool, skip_jquants: bool, mode: str = "nightly",
                 phase: str | None = None, limit_filings: int = 0,
                 target_tickers: list[str] | None = None,
                 canonical_lookback_days: int = 7,
                 enable_prior_comparative: bool = False,
                 prior_comparative_canary_tickers: list[str] | None = None,
                 canonical_target: str = "all",
                 skip_jquants_sync: bool = False) -> StepResult:
    step = StepResult("process")
    st = time.monotonic()
    try:
        from tools.filings_process import run_batch
        result = run_batch(
            dry_run=dry_run, skip_jquants=skip_jquants,
            skip_jquants_sync=skip_jquants_sync,
            mode=mode,
            phase=phase, limit_filings=limit_filings,
            target_tickers=target_tickers,
            canonical_lookback_days=canonical_lookback_days,
            enable_prior_comparative=enable_prior_comparative,
            prior_comparative_canary_tickers=prior_comparative_canary_tickers,
            canonical_target=canonical_target,
        )
        step.detail = result
        push_errors = result.get("push", {}).get("errors", 0)
        canonical_status = (result.get("canonical") or {}).get("status")
        step.status = "success" if push_errors == 0 and canonical_status == "ok" else "failed"
    except Exception as e:
        step.status = "failed"
        step.detail = {"error": str(e)}
        logger.error(f"[PIPELINE] process FAILED: {e}")
    step.elapsed = time.monotonic() - st
    return step


def _run_process_realtime(dry_run: bool, max_jobs: int = 50,
                          enable_prior_comparative: bool = False,
                          prior_comparative_canary_tickers: list[str] | None = None) -> StepResult:
    """queue 駆動の realtime process ステップ。"""
    step = StepResult("process-realtime")
    st = time.monotonic()
    try:
        try:
            from tools.financial_recovery_retry import run as recovery_run
            recovery = recovery_run(dry_run=dry_run, max_jobs=max_jobs)
        except Exception as recovery_error:
            logger.error(
                "[PIPELINE] financial-recovery FAILED: %s", recovery_error
            )
            recovery = {"failed": 1, "error": str(recovery_error)}

        from tools.filings_process import run_realtime
        result = run_realtime(
            dry_run=dry_run, 
            max_jobs=max_jobs,
            enable_prior_comparative=enable_prior_comparative,
            prior_comparative_canary_tickers=prior_comparative_canary_tickers,
        )
        result["financial_recovery"] = recovery
        result["errors"] = int(result.get("errors", 0) or 0) + int(
            recovery.get("failed", 0) or 0
        )
        step.detail = result
        step.status = "success" if result.get("errors", 0) == 0 else "warning"
    except Exception as e:
        step.status = "failed"
        step.detail = {"error": str(e)}
        logger.error(f"[PIPELINE] process-realtime FAILED: {e}")
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


def _reconcile_exit_code(step: StepResult) -> int:
    """Return the reconcile CLI exit code from its validated summary."""
    if step.status == "failed":
        return EXIT_RECONCILE_CRITICAL

    summary = step.detail
    if not isinstance(summary, dict):
        return EXIT_RECONCILE_CRITICAL

    status = summary.get("status")
    new_critical = summary.get("new_critical")
    issues_total = summary.get("issues_total")
    if (
        not isinstance(status, str)
        or not isinstance(new_critical, int)
        or isinstance(new_critical, bool)
        or not isinstance(issues_total, int)
        or isinstance(issues_total, bool)
        or new_critical < 0
        or issues_total < 0
    ):
        return EXIT_RECONCILE_CRITICAL

    if status != "success" or new_critical >= 1 or issues_total >= 10:
        return EXIT_RECONCILE_CRITICAL
    return EXIT_OK


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


def _run_event_notify(dry_run: bool) -> StepResult:
    step = StepResult("events")
    st = time.monotonic()
    try:
        from src.events.common_storage import ensure_events_table, get_unnotified_events, mark_notified
        from src.events.common_notify import send_event_discord, SendResult
        from src.events.notify_rules import should_notify_event  # [C] 出口ガード
        import sqlite3

        db_path = str(runtime_path(os.path.join(_PROJECT_ROOT, "decision_db.db"), code_root=_PROJECT_ROOT))
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            ensure_events_table(conn)
            events = get_unnotified_events(conn)
            sent = 0
            skipped = 0
            for ev in events:
                # [C] 出口ガード: notify_rules で通知対象外は送信しない
                if not should_notify_event(ev):
                    skipped += 1
                    logger.debug(
                        f"[EVENT] notify-guard skip: event_type={ev.event_type} "
                        f"ticker={ev.ticker} event_id={ev.event_id[:12]}"
                    )
                    continue
                send_result = send_event_discord(webhook_url, ev, dry_run=dry_run)
                if send_result == SendResult.SUCCESS:
                    mark_notified(conn, ev.event_id)
                    sent += 1
                elif send_result == SendResult.SKIPPED:
                    # dry_run時
                    sent += 1
            step.detail = {"total_unnotified": len(events), "sent": sent, "skipped_by_rules": skipped}
            step.status = "success"
        finally:
            conn.close()
    except Exception as e:
        step.status = "warning"
        step.detail = {"error": str(e)}
        logger.warning(f"[PIPELINE] events WARNING: {e}")
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
        choices=["ingest", "process", "process-realtime", "process-batch",
                 "rebuild", "notify", "reconcile", "retry-failed", "backfill"],
        help="サブコマンド (省略時は全ステップ実行)",
    )
    parser.add_argument("--dry-run", action="store_true", help="書き込みをスキップ")
    parser.add_argument("--skip-notify", action="store_true", help="Discord通知をスキップ")
    parser.add_argument("--skip-jquants", action="store_true", help="J-Quants syncをスキップ")
    parser.add_argument("--skip-jquants-sync", action="store_true", help="J-Quants canonical同期をスキップ")
    parser.add_argument("--ticker", type=str, help="特定 ticker (rebuild/process)")
    parser.add_argument("--trigger", type=str, default="manual",
                        choices=["scheduler", "manual", "retry", "reconcile"],
                        help="トリガー種別")
    parser.add_argument("--mode", type=str, default="nightly",
                        choices=["realtime", "nightly"],
                        help="process 実行モード (default: nightly)")
    parser.add_argument("--ingest-mode", type=str, default="default",
                        choices=["realtime", "default", "standard"],
                        help="ingest 実行モード (default: default)")
    parser.add_argument("--from", dest="from_date", type=str, help="開始日 (backfill)")
    parser.add_argument("--to", dest="to_date", type=str, help="終了日 (backfill)")
    parser.add_argument("--target-id", type=str, help="retry対象 target_id")
    # ── process-batch 再現コマンド用オプション ──
    parser.add_argument("--phase", type=str, default=None,
                        choices=["push", "canonical"],
                        help="process-batch の特定 phase のみ実行")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG レベルログ有効化")
    parser.add_argument("--limit-filings", type=int, default=0,
                        help="filing 件数制限 (0=無制限)")
    parser.add_argument("--target-tickers", type=str, nargs="*",
                        help="特定 ticker のみ処理")
    parser.add_argument("--lookback-days", type=int, default=7,
                        help="canonical lookback 日数 (default: 7)")
    # ── prior_comparative 関連 ──
    parser.add_argument("--enable-prior-comparative", action="store_true",
                        help="prior_comparative の保存処理を有効化する (default: False)")
    parser.add_argument("--prior-comparative-canary-tickers", type=str, nargs="*",
                        help="特定の ticker のみ prior_comparative を保存する (カンマ区切りも可)")
    parser.add_argument("--canonical-target", type=str, default="all",
                        choices=["all", "financials", "segments"],
                        help="canonical sync target (default: all)")
    args = parser.parse_args()

    # --verbose が指定されたら DEBUG レベルに
    if getattr(args, 'verbose', False):
        logging.getLogger().setLevel(logging.DEBUG)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, 'verbose', False) else logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── playground ガード ──
    import os
    _cwd = os.getcwd()
    if "playground" in _cwd.lower():
        print(
            "[FATAL] playground から実行されました。"
            "OneDrive 側から実行してください。",
            file=sys.stderr,
        )
        sys.exit(99)

    load_env(_PROJECT_ROOT)

    # ── 実行時パスログ ──
    _onedrive_marker = r"C:\Users\takuy\OneDrive\tdnet-excel-input"
    _env_path = os.path.join(_PROJECT_ROOT, ".env")
    _db_path = os.environ.get(
        "DECISION_DB_PATH",
        str(runtime_path(os.path.join(_PROJECT_ROOT, "decision_db.db"), code_root=_PROJECT_ROOT)),
    )

    def _tag(path: str) -> str:
        return "" if _onedrive_marker.lower() in path.lower() else " [WARNING: NOT OneDrive]"

    logger.info("=" * 55)
    logger.info("  RUNTIME PATH CHECK")
    logger.info("=" * 55)
    logger.info(f"  cwd         : {_cwd}{_tag(_cwd)}")
    logger.info(f"  python      : {sys.executable}{_tag(sys.executable)}")
    logger.info(f"  .env        : {_env_path}{_tag(_env_path)}")
    logger.info(f"  DB          : {_db_path}{_tag(_db_path)}")
    logger.info("=" * 55)

    # サブコマンド
    cmd = args.command

    # ── phase marker: PYTHON_START ──
    logger.info(
        f"===PYTHON_START=== pid={os.getpid()} cmd={cmd} trigger={args.trigger}"
    )

    # ── stale running cleanup (衛生管理) ──
    from lib.pipeline.logging_utils import cleanup_stale_runs
    cleanup_stale_runs(max_age_hours=2)

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
            logger.info(f"===PYTHON_END=== pid={os.getpid()} cmd={cmd} exit=0 reason=concurrent")
            sys.exit(EXIT_OK)

    # ── process: ingest running なら skip ──
    if cmd == "process" and args.trigger == "scheduler":
        from lib.pipeline.logging_utils import check_ingest_running
        ingest_run_id = check_ingest_running()
        if ingest_run_id:
            logger.info(
                f"===PROCESS_SKIP=== reason=ingest_running ingest_run_id={ingest_run_id}"
            )
            # pipeline_runs に skipped を記録
            try:
                with PipelineRun("process", trigger_type=args.trigger) as pl:
                    pl.update(skipped=1)
                    pl._stats["message_override"] = f"ingest running (id={ingest_run_id})"
            except Exception:
                pass
            logger.info(f"===PYTHON_END=== pid={os.getpid()} cmd={cmd} exit=0 reason=ingest_running")
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
        yanoshin_timeout = 5 if args.ingest_mode == "realtime" else 15
        step = _run_subcommand_with_logging(
            "ingest", lambda: _run_ingest(args.dry_run, yanoshin_timeout_sec=yanoshin_timeout), trigger_type=trigger,
        )
        exit_code = EXIT_OK if step.status == "success" else EXIT_INGEST_FAIL
        logger.info(f"===PYTHON_END=== pid={os.getpid()} cmd={cmd} exit={exit_code}")
        sys.exit(exit_code)

    elif cmd == "process" or cmd == "process-batch":
        canary_tickers = None
        if args.prior_comparative_canary_tickers:
            canary_tickers = []
            for t in args.prior_comparative_canary_tickers:
                canary_tickers.extend([x.strip() for x in t.split(",") if x.strip()])
                
        step = _run_subcommand_with_logging(
            "process", lambda: _run_process(
                args.dry_run, args.skip_jquants, mode=args.mode,
                phase=getattr(args, 'phase', None),
                limit_filings=getattr(args, 'limit_filings', 0),
                target_tickers=getattr(args, 'target_tickers', None),
                canonical_lookback_days=getattr(args, 'lookback_days', 7),
                enable_prior_comparative=args.enable_prior_comparative,
                prior_comparative_canary_tickers=canary_tickers,
                canonical_target=getattr(args, 'canonical_target', 'all'),
                skip_jquants_sync=getattr(args, 'skip_jquants_sync', False),
            ),
            trigger_type=trigger,
        )
        exit_code = EXIT_OK if step.status in ("success", "warning") else EXIT_INGEST_FAIL
        logger.info(f"===PYTHON_END=== pid={os.getpid()} cmd={cmd} exit={exit_code}")
        sys.exit(exit_code)

    elif cmd == "process-realtime":
        canary_tickers = None
        if getattr(args, 'prior_comparative_canary_tickers', None):
            canary_tickers = []
            for t in args.prior_comparative_canary_tickers:
                canary_tickers.extend([x.strip() for x in t.split(",") if x.strip()])
                
        step = _run_subcommand_with_logging(
            "process-realtime", lambda: _run_process_realtime(
                args.dry_run,
                enable_prior_comparative=args.enable_prior_comparative,
                prior_comparative_canary_tickers=canary_tickers,
            ),
            trigger_type=trigger,
        )
        exit_code = EXIT_OK if step.status in ("success", "warning") else EXIT_INGEST_FAIL
        logger.info(f"===PYTHON_END=== pid={os.getpid()} cmd={cmd} exit={exit_code}")
        sys.exit(exit_code)

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
        sys.exit(_reconcile_exit_code(step))

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
            if args.skip_notify:
                sys.argv += ["--skip-notify"]
            backfill_main()

    elif cmd == "events":
        step = _run_subcommand_with_logging(
            "events", lambda: _run_event_notify(args.dry_run), trigger_type=trigger,
        )
        sys.exit(EXIT_OK)  # events failure は非致命的

    else:
        parser.print_help()
        sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
