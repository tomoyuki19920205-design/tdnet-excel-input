#!/usr/bin/env python3
# ============================================================
# filings_process.py — SQLite→Supabase push + J-Quants sync
# ============================================================
"""
core process: sqlite_to_supabase.push_sqlite_to_supabase()
optional:     sync_financials.sync() (J-Quants)
Step 3:       canonical_sync.sync_canonical()
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.runtime_paths import runtime_path
from tools.sqlite_to_supabase import push_sqlite_to_supabase

logger = logging.getLogger("pipeline.process")

# canonical sync のデフォルト lookback
_DEFAULT_CANONICAL_LOOKBACK_DAYS = 7

JST = timezone(timedelta(hours=9))


def _load_env_value(key: str) -> str:
    """環境変数 or .env から value を取得"""
    val = os.environ.get(key, "")
    if val:
        return val
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return ""


def _canonical_default_result() -> dict:
    """canonical 未実行時のデフォルト result dict。"""
    return {
        "status": "ok",
        "mode": "empty",
        "fallback_used": False,
        "target_keys_count": 0,
        "resolved_target_count": 0,
        "lookback_days": _DEFAULT_CANONICAL_LOOKBACK_DAYS,
        "financials": {"targets": 0, "rows_selected": 0, "attempted": 0,
                       "written": 0, "skipped": 0, "errors": 0},
        "segments": {"targets": 0, "rows_selected": 0, "attempted": 0,
                     "written": 0, "skipped": 0, "errors": 0},
        "summary": "",
    }


def run(
    *,
    dry_run: bool = False,
    skip_jquants: bool = False,
    db_path: str | None = None,
    strict_canonical: bool = False,
    canonical_lookback_days: int = _DEFAULT_CANONICAL_LOOKBACK_DAYS,
    mode: str = "nightly",
) -> dict:
    """filings process を実行。後方互換ラッパー。

    mode="realtime" の場合は run_realtime() を使うことを推奨。
    この関数は実質 run_batch() と同義。
    """
    return run_batch(
        dry_run=dry_run,
        skip_jquants=skip_jquants,
        db_path=db_path,
        strict_canonical=strict_canonical,
        canonical_lookback_days=canonical_lookback_days,
        mode=mode,
    )


def _setup_batch_log_handler() -> logging.FileHandler | None:
    """batch 専用ログファイルの handler を作成する。

    既に同一パスの handler がある場合は追加しない。
    """
    now = datetime.now(JST)
    log_dir = str(runtime_path(os.path.join(_PROJECT_ROOT, "logs"), code_root=_PROJECT_ROOT))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(
        log_dir, f"process_batch_{now.strftime('%Y%m%d')}.log"
    )

    # handler 重複チェック
    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        if isinstance(h, logging.FileHandler):
            try:
                if os.path.abspath(h.baseFilename) == os.path.abspath(log_file):
                    return None  # 既に存在
            except AttributeError:
                pass

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S")
    )
    root_logger.addHandler(handler)
    logger.info(f"[process_batch] batch log → {log_file}")
    return handler


def _remove_batch_log_handler(handler: logging.FileHandler | None) -> None:
    """batch 専用ログの handler を安全に除去する。"""
    if handler is None:
        return
    try:
        logging.getLogger().removeHandler(handler)
        handler.close()
    except Exception:
        pass


def _count_batch_targets(db_path: str) -> dict:
    """SQLite から batch 処理対象の件数を取得する。"""
    counts: dict = {
        "disclosures": 0,
        "facts": 0,
        "financials": 0,
        "canonical_keys": 0,
    }
    if not os.path.exists(db_path):
        return counts
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # quarterly_results 行数 = facts/financials の元データ
        row = conn.execute(
            "SELECT COUNT(*) as c FROM quarterly_results"
        ).fetchone()
        counts["facts"] = row["c"] if row else 0
        counts["financials"] = counts["facts"]

        # unique disclosure keys
        row = conn.execute(
            "SELECT COUNT(DISTINCT company_code || '|' || fiscal_year_end || '|' || quarter) as c "
            "FROM quarterly_results"
        ).fetchone()
        counts["disclosures"] = row["c"] if row else 0

        # canonical target keys
        row = conn.execute(
            "SELECT COUNT(DISTINCT company_code || '|' || fiscal_year_end || '|' || quarter) as c "
            "FROM quarterly_results"
        ).fetchone()
        counts["canonical_keys"] = row["c"] if row else 0

        conn.close()
    except Exception as e:
        logger.warning(f"[process_batch] target count query failed: {e}")
    return counts


def run_batch(
    *,
    dry_run: bool = False,
    skip_jquants: bool = False,
    skip_jquants_sync: bool = False,
    db_path: str | None = None,
    strict_canonical: bool = False,
    canonical_lookback_days: int = _DEFAULT_CANONICAL_LOOKBACK_DAYS,
    mode: str = "nightly",
    phase: str | None = None,
    limit_filings: int = 0,
    target_tickers: list[str] | None = None,
    enable_prior_comparative: bool = False,
    prior_comparative_canary_tickers: list[str] | None = None,
    canonical_target: str = "all",
) -> dict:
    """
    filings process (batch) を実行。

    nightly / 手動実行用。全体 push + lookback 許可 canonical sync。

    Args:
        mode: 実行モード。
            - "realtime": 軽量モード。PDF重解析スキップ、J-Quantsスキップ、
                          canonical lookback 短縮。
            - "nightly": フルモード（デフォルト）。全指標抽出、全解析。
        phase: 特定 phase のみ実行。
            - "push": push のみ、canonical スキップ
            - "canonical": push スキップ、canonical のみ
            - None: 全 phase 実行（デフォルト）
        limit_filings: filing 件数制限（0=無制限）
        target_tickers: 特定 ticker のみ処理（canonical 用）
        canonical_target: canonical同期の対象 ("all" | "financials" | "segments")

    Returns:
        {"push": dict, "jquants": dict | None, "canonical": dict, "mode": str}
    """
    t0_batch = time.monotonic()
    run_id = f"{datetime.now(JST).strftime('%H%M%S')}_{os.getpid()}"

    # ── 改修③: batch 専用ログファイル ──
    batch_handler = _setup_batch_log_handler()

    try:
        return _run_batch_inner(
            dry_run=dry_run,
            skip_jquants=skip_jquants,
            skip_jquants_sync=skip_jquants_sync,
            db_path=db_path,
            strict_canonical=strict_canonical,
            canonical_lookback_days=canonical_lookback_days,
            mode=mode,
            phase=phase,
            limit_filings=limit_filings,
            target_tickers=target_tickers,
            run_id=run_id,
            t0_batch=t0_batch,
            enable_prior_comparative=enable_prior_comparative,
            prior_comparative_canary_tickers=prior_comparative_canary_tickers,
            canonical_target=canonical_target,
        )
    finally:
        _remove_batch_log_handler(batch_handler)


def _run_batch_inner(
    *,
    dry_run: bool,
    skip_jquants: bool,
    skip_jquants_sync: bool,
    db_path: str | None,
    strict_canonical: bool,
    canonical_lookback_days: int,
    mode: str,
    phase: str | None,
    limit_filings: int,
    target_tickers: list[str] | None,
    run_id: str,
    t0_batch: float,
    enable_prior_comparative: bool,
    prior_comparative_canary_tickers: list[str] | None,
    canonical_target: str,
) -> dict:
    """run_batch の内部実装。"""
    _is_realtime = mode == "realtime"
    _db = db_path or str(runtime_path(os.path.join(_PROJECT_ROOT, "decision_db.db"), code_root=_PROJECT_ROOT))

    # realtime モードの自動調整
    if _is_realtime:
        skip_jquants = True
        canonical_lookback_days = min(canonical_lookback_days, 1)
        logger.info("[filings_process] mode=realtime: skip_jquants=True, lookback=1d")

    # ── 改修①: process_batch START ──
    logger.info(
        f"[process_batch] START run_id={run_id} mode={mode} "
        f"phase={phase or 'all'} dry_run={dry_run} "
        f"limit_filings={limit_filings} target_tickers={target_tickers}"
    )

    # ── 改修⑤: 件数サマリ ──
    logger.info("[process_batch] phase=precheck START")
    t_pre = time.monotonic()
    targets = _count_batch_targets(_db)
    logger.info(
        f"[process_batch] targets: "
        f"disclosures={targets['disclosures']} "
        f"facts={targets['facts']} "
        f"financials={targets['financials']} "
        f"canonical_keys={targets['canonical_keys']} "
        f"mode={'target_only' if target_tickers else 'lookback'}"
    )
    logger.info(
        f"[process_batch] phase=precheck END "
        f"elapsed={time.monotonic() - t_pre:.1f}s"
    )

    result: dict = {
        "push": {},
        "jquants": None,
        "canonical": _canonical_default_result(),
        "mode": mode,
    }

    push_stats: dict = {}
    batch_status = "ok"

    # ── push phase ──
    if phase is None or phase == "push":
        logger.info("[process_batch] phase=push START")
        t_push = time.monotonic()
        try:
            push_kwargs: dict = {
                "db_path": _db,
                "dry_run": dry_run,
                "target_tickers": target_tickers,
            }
            if limit_filings > 0:
                push_kwargs["limit"] = limit_filings

            push_stats = push_sqlite_to_supabase(**push_kwargs)
            result["push"] = push_stats
            logger.info(
                f"[process_batch] phase=push END "
                f"elapsed={time.monotonic() - t_push:.1f}s "
                f"facts={push_stats.get('facts_pushed', 0)} "
                f"financials={push_stats.get('financials_inserted', 0)} "
                f"errors={push_stats.get('errors', 0)}"
            )
        except Exception as e:
            elapsed_push = time.monotonic() - t_push
            logger.error(
                f"[process_batch] phase=push FAILED "
                f"elapsed={elapsed_push:.1f}s "
                f"error={e}\n{traceback.format_exc()}"
            )
            batch_status = "failed"
            raise
    else:
        logger.info("[process_batch] phase=push SKIPPED (--phase canonical)")

    # ── J-Quants sync (optional) ──
    if phase is None or phase == "push":
        if skip_jquants:
            logger.info("[filings_process] Step 2: J-Quants sync SKIPPED")
        elif skip_jquants_sync:
            logger.info("[filings_process] Step 2: J-Quants sync SKIPPED (skipped by skip_jquants_sync)")
            result["jquants"] = {"status": "skipped", "reason": "skipped_by_skip_jquants_sync"}
        else:
            logger.info("[filings_process] Step 2: J-Quants financial sync")
            try:
                from tools.sync_financials import sync as jquants_sync

                supabase_url = _load_env_value("SUPABASE_URL")
                supabase_key = _load_env_value("SUPABASE_ANON_KEY")
                jquants_db = str(runtime_path(os.path.join(_PROJECT_ROOT, "data", "jquants.db"), code_root=_PROJECT_ROOT))

                if not os.path.exists(jquants_db):
                    logger.warning(
                        f"[filings_process] J-Quants DB not found: {jquants_db}"
                    )
                    result["jquants"] = {"status": "skipped", "reason": "db_not_found"}
                elif not supabase_url or not supabase_key:
                    logger.warning("[filings_process] Supabase credentials missing")
                    result["jquants"] = {"status": "skipped", "reason": "no_credentials"}
                else:
                    jq_stats = jquants_sync(
                        db_path=jquants_db,
                        supabase_url=supabase_url,
                        supabase_key=supabase_key,
                        dry_run=dry_run,
                    )
                    result["jquants"] = jq_stats
                    logger.info(f"[filings_process] J-Quants sync done: {jq_stats}")
            except Exception as e:
                logger.warning(f"[filings_process] J-Quants sync failed: {e}")
                result["jquants"] = {"status": "error", "error": str(e)}

    # ── canonical phase ──
    if phase is None or phase == "canonical":
        # canonical 単独実行時の入力検証
        if phase == "canonical" and not target_tickers and canonical_lookback_days <= 0:
            logger.error(
                "[process_batch] phase=canonical requires "
                "--target-tickers or --lookback-days > 0"
            )
            result["canonical"] = _canonical_default_result()
            result["canonical"]["status"] = "error"
            result["canonical"]["summary"] = "canonical standalone requires target specification"
            batch_status = "failed"
            logger.info(
                f"[process_batch] END status={batch_status} "
                f"elapsed={time.monotonic() - t0_batch:.1f}s"
            )
            return result

        logger.info("[process_batch] phase=canonical START")
        t_canonical = time.monotonic()
        try:
            from lib.pipeline.canonical_sync import sync_canonical

            # target_keys の決定
            if target_tickers:
                # ticker 指定 → lookback で該当 ticker の行を取得
                target_keys = None  # sync_canonical が lookback で取得
            elif push_stats:
                target_keys = _extract_target_keys(push_stats)
            else:
                target_keys = None

            canonical_stats = sync_canonical(
                db_path=_db,
                dry_run=dry_run,
                target_keys=target_keys,
                lookback_days=canonical_lookback_days,
                strict=strict_canonical,
                sync_financials=canonical_target in ("all", "financials"),
                sync_segments=canonical_target in ("all", "segments"),
            )
            result["canonical"] = canonical_stats
            logger.info(
                f"[process_batch] phase=canonical END "
                f"elapsed={time.monotonic() - t_canonical:.1f}s "
                f"financials_written={canonical_stats.get('financials', {}).get('written', 0)} "
                f"segments_written={canonical_stats.get('segments', {}).get('written', 0)}"
            )
            
            # ── 改修: prior_comparative save ──
            if enable_prior_comparative:
                try:
                    from lib.pipeline.prior_comparative_saver import save_prior_comparative_from_event
                    
                    target_doc_ids = []
                    if push_stats and "results" in push_stats:
                        for row in push_stats["results"]:
                            if row.get("status") == "success":
                                did = str(row["doc_id"])
                                if did and did not in target_doc_ids:
                                    target_doc_ids.append(did)
                    
                    logger.info(f"[PRIOR_COMP_SAVER] ENABLED dry_run=True targets={len(target_doc_ids)} canary_tickers={prior_comparative_canary_tickers}")
                    if target_doc_ids:
                        prior_stats = save_prior_comparative_from_event(
                            target_doc_ids,
                            dry_run=True, # STRICTLY ENFORCED FOR DRY RUN
                            canary_tickers=prior_comparative_canary_tickers,
                        )
                        result["prior_comparative"] = prior_stats
                        logger.info(f"[PRIOR_COMP_SAVER] DONE stats={prior_stats}")
                except Exception as e:
                    logger.error(f"[PRIOR_COMP_SAVER] FAILED: {e}")
            else:
                logger.info("[PRIOR_COMP_SAVER] SKIPPED: not enabled")

        except Exception as e:
            elapsed_can = time.monotonic() - t_canonical
            logger.error(
                f"[process_batch] phase=canonical FAILED "
                f"elapsed={elapsed_can:.1f}s "
                f"error={e}\n{traceback.format_exc()}"
            )
            if strict_canonical:
                batch_status = "failed"
                raise
            err_result = _canonical_default_result()
            err_result["status"] = "error"
            err_result["summary"] = f"canonical_sync error: {e}"
            result["canonical"] = err_result
    else:
        logger.info("[process_batch] phase=canonical SKIPPED (--phase push)")

    # ── 改修① postcheck + END ──
    logger.info("[process_batch] phase=postcheck START")
    t_post = time.monotonic()
    # postcheck: push エラー数チェック
    push_errors = result.get("push", {}).get("errors", 0)
    canonical_status = result.get("canonical", {}).get("status", "ok")
    if push_errors > 0 or canonical_status == "error":
        batch_status = "warning" if batch_status == "ok" else batch_status
    logger.info(
        f"[process_batch] phase=postcheck END "
        f"elapsed={time.monotonic() - t_post:.1f}s "
        f"push_errors={push_errors} canonical_status={canonical_status}"
    )

    elapsed_total = time.monotonic() - t0_batch
    logger.info(
        f"[process_batch] END status={batch_status} "
        f"elapsed={elapsed_total:.1f}s"
    )

    return result


# ============================================================
# run_realtime — queue 駆動の軽量差分処理
# ============================================================

def run_realtime(
    *,
    dry_run: bool = False,
    db_path: str | None = None,
    max_jobs: int = 50,
    enable_prior_comparative: bool = False,
    prior_comparative_canary_tickers: list[str] | None = None,
) -> dict:
    """queue 駆動の realtime process。

    job_queue から pending jobs を取得し、対象 disclosure_ids のみ
    targeted push + target-only canonical sync を実行する。

    disclosure_ids は外部から受け取らない。source of truth は job_queue のみ。

    Args:
        dry_run: True なら書き込みスキップ
        db_path: SQLite DB パス（省略時はデフォルト）
        max_jobs: 1回で処理する最大 job 数

    Returns:
        {
            "mode": "realtime",
            "taken_job_count": int,
            "target_disclosure_ids": list[str],
            "push": dict,
            "canonical": dict,
            "queue_success": int,
            "queue_failed": int,
            "queue_partial": int,
            "errors": int,
            "elapsed_sec": float,
        }
    """
    import time
    t0 = time.monotonic()
    _db = db_path or str(runtime_path(os.path.join(_PROJECT_ROOT, "decision_db.db"), code_root=_PROJECT_ROOT))

    result: dict = {
        "mode": "realtime",
        "taken_job_count": 0,
        "target_disclosure_ids": [],
        "push": {},
        "canonical": _canonical_default_result(),
        "queue_success": 0,
        "queue_failed": 0,
        "queue_partial": 0,
        "errors": 0,
        "elapsed_sec": 0.0,
    }

    # ── Step 1: queue から pending jobs を取得 ──
    t_take = time.monotonic()
    try:
        from lib.pipeline.queue import take_pending_jobs, complete_job
        from lib.pipeline.db import load_env
        load_env(_PROJECT_ROOT)

        jobs = take_pending_jobs(
            "tdnet_realtime_process",
            limit=max_jobs,
        )
    except Exception as e:
        logger.error(f"[process_realtime] phase=take_pending_jobs FAILED: {e}")
        result["errors"] += 1
        result["elapsed_sec"] = time.monotonic() - t0
        return result

    logger.info(
        f"[process_realtime] phase=take_pending_jobs "
        f"elapsed={time.monotonic()-t_take:.1f}s "
        f"actual_jobs={len(jobs)}"
    )
    result["taken_job_count"] = len(jobs)

    if not jobs:
        logger.info("[process_realtime] no pending jobs in queue, nothing to do")
        result["elapsed_sec"] = time.monotonic() - t0
        return result

    # ── Step 2: disclosure_ids 収集 ──
    disclosure_ids: list[str] = []
    job_id_map: dict[str, int] = {}  # disclosure_id → job_id
    for job in jobs:
        target_id = job.get("target_id", "")
        if target_id:
            disclosure_ids.append(target_id)
            job_id_map[target_id] = job["id"]

    result["target_disclosure_ids"] = list(disclosure_ids)
    logger.info(
        f"[process_realtime] target_disclosure_ids={disclosure_ids}"
    )

    invalid_jobs = [job for job in jobs if not job.get("target_id")]
    if invalid_jobs:
        logger.warning("[process_realtime] jobs found but no disclosure_ids extracted")
        for job in invalid_jobs:
            try:
                complete_job(job["id"], status="failed",
                             error_message="no target_id in job")
            except Exception:
                pass
            result["queue_failed"] += 1
    if not disclosure_ids:
        result["elapsed_sec"] = time.monotonic() - t0
        return result

    # ── Step 3: targeted push ──
    t_push = time.monotonic()
    push_ok = True
    push_stats = {}
    if disclosure_ids:
        try:
            from tools.sqlite_to_supabase import push_sqlite_to_supabase_targeted
            push_stats = push_sqlite_to_supabase_targeted(
                db_path=_db,
                disclosure_ids=disclosure_ids,
                dry_run=dry_run,
            )
            result["push"] = push_stats
            if push_stats.get("errors", 0) > 0:
                push_ok = False
        except Exception as e:
            logger.error(f"[process_realtime] phase=targeted_push FAILED: {e}")
            push_ok = False
            result["push"] = {"error": str(e)}
            result["errors"] += 1
    else:
        result["push"] = {"status": "skipped", "reason": "no actual jobs"}

    logger.info(
        f"[process_realtime] phase=targeted_push "
        f"elapsed={time.monotonic()-t_push:.1f}s "
        f"ok={push_ok} "
        f"target_rows_count={push_stats.get('target_rows_count', 0)} "
        f"facts={push_stats.get('push_rows', {}).get('facts', 0)} "
        f"financials={push_stats.get('push_rows', {}).get('financials', 0)}"
    )

    # ── Step 4: canonical sync (target-only, no fallback) ──
    t_canonical = time.monotonic()
    canonical_ok = True
    target_keys = push_stats.get("target_keys", [])
    # target_keys を tuple リストに変換
    target_keys_tuples = [
        (tk[0], tk[1], tk[2]) if isinstance(tk, (list, tuple)) else tk
        for tk in target_keys
    ]

    if disclosure_ids:
        try:
            from lib.pipeline.canonical_sync import sync_canonical
            canonical_stats = sync_canonical(
                db_path=_db,
                dry_run=dry_run,
                target_keys=target_keys_tuples if target_keys_tuples else None,
                lookback_days=0,
                allow_fallback=False,
            )
            result["canonical"] = canonical_stats
            if canonical_stats.get("status") == "error":
                canonical_ok = False
        except Exception as e:
            logger.error(f"[process_realtime] phase=canonical_sync FAILED: {e}")
            canonical_ok = False
            result["errors"] += 1

    logger.info(
        f"[process_realtime] phase=canonical_sync "
        f"elapsed={time.monotonic()-t_canonical:.1f}s "
        f"ok={canonical_ok} "
        f"target_keys_count={len(target_keys_tuples)} "
        f"canonical_financials_written={result.get('canonical', {}).get('financials', {}).get('written', 0)} "
        f"canonical_segments_written={result.get('canonical', {}).get('segments', {}).get('written', 0)}"
    )

    # ── 改修: prior_comparative save ──
    if enable_prior_comparative and canonical_ok:
        try:
            from lib.pipeline.prior_comparative_saver import save_prior_comparative_from_event
            target_doc_ids = []
            for did in job_id_map.keys():
                did_str = str(did)
                if did_str and did_str not in target_doc_ids:
                    target_doc_ids.append(did_str)
            
            logger.info(f"[PRIOR_COMP_SAVER] ENABLED dry_run=True targets={len(target_doc_ids)} canary_tickers={prior_comparative_canary_tickers}")
            if target_doc_ids:
                prior_stats = save_prior_comparative_from_event(
                    target_doc_ids,
                    dry_run=True, # STRICTLY ENFORCED FOR DRY RUN
                    canary_tickers=prior_comparative_canary_tickers,
                )
                result["prior_comparative"] = prior_stats
                logger.info(f"[PRIOR_COMP_SAVER] DONE stats={prior_stats}")
        except Exception as e:
            logger.error(f"[PRIOR_COMP_SAVER] FAILED: {e}")
    else:
        if enable_prior_comparative:
            logger.info("[PRIOR_COMP_SAVER] SKIPPED: canonical phase was not ok")
        else:
            logger.info("[PRIOR_COMP_SAVER] SKIPPED: not enabled")

    # ── Step 5: queue 完了更新 ──
    t_complete = time.monotonic()
    for did, job_id in job_id_map.items():
        try:
            if push_ok and canonical_ok:
                complete_job(job_id, status="done")
                result["queue_success"] += 1
            elif push_ok or canonical_ok:
                complete_job(
                    job_id, status="failed",
                    error_message="partial: push_ok={} canonical_ok={}".format(
                        push_ok, canonical_ok),
                )
                result["queue_partial"] += 1
            else:
                complete_job(
                    job_id, status="failed",
                    error_message="both push and canonical failed",
                )
                result["queue_failed"] += 1
        except Exception as e:
            logger.error(
                f"[process_realtime] phase=queue_complete FAILED "
                f"job_id={job_id}: {e}"
            )
            result["errors"] += 1

    logger.info(
        f"[process_realtime] phase=queue_complete "
        f"elapsed={time.monotonic()-t_complete:.1f}s "
        f"queue_success={result['queue_success']} "
        f"queue_failed={result['queue_failed']} "
        f"queue_partial={result['queue_partial']}"
    )

    result["elapsed_sec"] = time.monotonic() - t0
    logger.info(
        f"[process_realtime] DONE "
        f"elapsed={result['elapsed_sec']:.1f}s "
        f"taken_job_count={result['taken_job_count']} "
        f"target_disclosure_ids={result['target_disclosure_ids']} "
        f"queue_success={result['queue_success']} "
        f"queue_failed={result['queue_failed']} "
        f"queue_partial={result['queue_partial']} "
        f"errors={result['errors']}"
    )
    return result


def _extract_target_keys(push_stats: dict) -> list[tuple[str, str, str]] | None:
    """push_stats から process 対象の (ticker, period, quarter) リストを抽出。

    push_stats に processed_rows があればそこから取得。
    なければ None (lookback fallback を使う)。
    """
    processed = push_stats.get("processed_rows", [])
    if not processed:
        return None

    keys = set()
    for row in processed:
        ticker = row.get("company_code") or row.get("ticker", "")
        period = row.get("period", "")
        quarter = row.get("quarter", "")
        if ticker and period and quarter:
            keys.add((ticker, period, quarter))

    return list(keys) if keys else None


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-jquants", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict-canonical", action="store_true",
                        help="canonical sync 失敗時にエラーを raise する")
    parser.add_argument("--canonical-lookback-days", type=int,
                        default=_DEFAULT_CANONICAL_LOOKBACK_DAYS,
                        help="target_keys が取れない/0件の場合の fallback 抽出日数 (default: 7)")
    parser.add_argument("--mode", type=str, default="nightly",
                        choices=["realtime", "nightly"],
                        help="実行モード (default: nightly)")
    parser.add_argument("--phase", type=str, default=None,
                        choices=["push", "canonical"],
                        help="特定 phase のみ実行")
    parser.add_argument("--limit-filings", type=int, default=0,
                        help="filing 件数制限 (0=無制限)")
    parser.add_argument("--target-tickers", type=str, nargs="*",
                        help="特定 ticker のみ処理")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG レベルログ有効化")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    run_batch(
        dry_run=args.dry_run,
        skip_jquants=args.skip_jquants,
        strict_canonical=args.strict_canonical,
        canonical_lookback_days=args.canonical_lookback_days,
        mode=args.mode,
        phase=args.phase,
        limit_filings=args.limit_filings,
        target_tickers=args.target_tickers,
    )

