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
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sqlite_to_supabase import push_sqlite_to_supabase

logger = logging.getLogger("pipeline.process")

# canonical sync のデフォルト lookback
_DEFAULT_CANONICAL_LOOKBACK_DAYS = 7


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
) -> dict:
    """
    filings process を実行。

    Returns:
        {"push": dict, "jquants": dict | None, "canonical": dict}
    """
    _db = db_path or os.path.join(_PROJECT_ROOT, "decision_db.db")
    result: dict = {
        "push": {},
        "jquants": None,
        "canonical": _canonical_default_result(),
    }

    # ── Step 1: SQLite → Supabase push ──
    logger.info("[filings_process] Step 1: SQLite → Supabase push")
    try:
        push_stats = push_sqlite_to_supabase(
            db_path=_db,
            dry_run=dry_run,
        )
        result["push"] = push_stats
        logger.info(
            f"[filings_process] push done "
            f"facts={push_stats.get('facts_pushed', 0)} "
            f"financials={push_stats.get('financials_inserted', 0)} "
            f"errors={push_stats.get('errors', 0)}"
        )
    except Exception as e:
        logger.error(f"[filings_process] push failed: {e}")
        raise

    # ── Step 2: J-Quants sync (optional) ──
    if skip_jquants:
        logger.info("[filings_process] Step 2: J-Quants sync SKIPPED")
    else:
        logger.info("[filings_process] Step 2: J-Quants financial sync")
        try:
            from tools.sync_financials import sync as jquants_sync

            supabase_url = _load_env_value("SUPABASE_URL")
            supabase_key = _load_env_value("SUPABASE_ANON_KEY")
            jquants_db = os.path.join(_PROJECT_ROOT, "data", "jquants.db")

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

    # ── Step 3: canonical sync (financials + segments) ──
    logger.info("[filings_process] Step 3: canonical sync")
    try:
        from lib.pipeline.canonical_sync import sync_canonical

        # process 対象の target_keys を push_stats から抽出
        target_keys = _extract_target_keys(push_stats)

        canonical_stats = sync_canonical(
            db_path=_db,
            dry_run=dry_run,
            target_keys=target_keys,
            lookback_days=canonical_lookback_days,
            strict=strict_canonical,
        )
        result["canonical"] = canonical_stats
        logger.info(
            f"[filings_process] canonical sync: "
            f"{canonical_stats.get('summary', '')}"
        )
    except Exception as e:
        if strict_canonical:
            raise
        logger.warning(f"[filings_process] canonical sync failed (best-effort): {e}")
        err_result = _canonical_default_result()
        err_result["status"] = "error"
        err_result["summary"] = f"canonical_sync error: {e}"
        result["canonical"] = err_result

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
    args = parser.parse_args()
    run(dry_run=args.dry_run, skip_jquants=args.skip_jquants,
        strict_canonical=args.strict_canonical,
        canonical_lookback_days=args.canonical_lookback_days)
