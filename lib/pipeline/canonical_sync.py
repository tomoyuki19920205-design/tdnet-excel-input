"""lib/pipeline/canonical_sync.py — 日次差分 canonical 同期

process ステップの後に呼ばれ、最新の抽出結果を
canonical_financials / canonical_segments に反映する。

差分判定:
  第一優先: process 対象の filing_id / ticker / period / quarter
  第二優先: lookback_days fallback (updated_at >= now - N日)

既存 canonical_writer.py の write_financials_canonical /
write_segments_canonical を再利用。source_row_key ベースで
idempotent upsert → correction / recency を壊さない。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

from .canonical_writer import write_financials_canonical, write_segments_canonical
from .db import load_env

logger = logging.getLogger("pipeline.canonical_sync")
JST = timezone(timedelta(hours=9))

# ================================================================
# 定数
# ================================================================
DEFAULT_LOOKBACK_DAYS = 7

# financials 対象 metric マッピング
_FINANCIALS_METRIC_COLS = {
    "sales": "sales",
    "gross_profit": "gross_profit",
    "sga": "sga",
    "operating_profit": "operating_profit",
}


# ================================================================
# helper: stats 初期化
# ================================================================

def _init_sub_stats() -> dict:
    """financials / segments 個別の stats dict を初期化。"""
    return {
        "targets": 0,
        "rows_selected": 0,
        "attempted": 0,
        "written": 0,
        "skipped": 0,
        "errors": 0,
    }


def _empty_result(*, lookback_days: int, target_keys_count: int) -> dict:
    """canonical 未実行/空時のデフォルト result dict。"""
    return {
        "status": "ok",
        "mode": "empty",
        "fallback_used": False,
        "target_keys_count": target_keys_count,
        "resolved_target_count": 0,
        "lookback_days": lookback_days,
        "financials": _init_sub_stats(),
        "segments": _init_sub_stats(),
        "summary": "",
    }


# ================================================================
# helper: target_keys 正規化
# ================================================================

def _normalize_target_keys(
    target_keys: list[tuple[str, str, str]] | None,
) -> list[tuple[str, str, str]] | None:
    """target_keys の重複排除 + tuple 化。"""
    if target_keys is None:
        return None
    seen: set[tuple[str, str, str]] = set()
    result: list[tuple[str, str, str]] = []
    for key in target_keys:
        t = tuple(key)
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result if result else None


# ================================================================
# helper: status 判定
# ================================================================

def _determine_status(
    *,
    fin_stats: dict,
    seg_stats: dict,
    mode: str,
    target_keys_count: int,
) -> str:
    """最終 status を判定する。

    Rules:
      error:
        - financials も segments も全件失敗 (written=0 & errors>0)
      warning:
        - どちらかで errors > 0
        - target_keys ありなのに rows_selected=0 & fallback も 0
        - 片系統成功・片系統全失敗
      ok:
        - errors = 0、または empty 正常終了
    """
    fin_err = fin_stats["errors"]
    seg_err = seg_stats["errors"]
    fin_written = fin_stats["written"]
    seg_written = seg_stats["written"]
    total_errors = fin_err + seg_err
    total_written = fin_written + seg_written

    # target_keys あり → 0 件抽出 → fallback も 0 → warning
    if mode == "lookback_fallback" and fin_stats["rows_selected"] == 0 and seg_stats["rows_selected"] == 0:
        return "warning"

    # 全件失敗
    if total_errors > 0 and total_written == 0:
        return "error"

    # 一部失敗
    if total_errors > 0:
        return "warning"

    return "ok"


# ================================================================
# helper: summary 生成
# ================================================================

def _build_summary(result: dict) -> str:
    """notify / log にそのまま流せる1行 summary を生成。"""
    fin = result["financials"]
    seg = result["segments"]
    parts = [
        f"canonical_sync {result['status']}",
        f"mode={result['mode']}",
        f"fallback_used={result['fallback_used']}",
        f"financials(written={fin['written']} errors={fin['errors']})",
        f"segments(written={seg['written']} errors={seg['errors']})",
    ]
    if result["mode"] == "lookback_fallback" and result["fallback_used"]:
        if fin["rows_selected"] == 0 and seg["rows_selected"] == 0:
            parts.append("no rows resolved from target_keys or fallback")
    return " ".join(parts)


# ================================================================
# helper: source 推定
# ================================================================

def _detect_source(row: dict) -> str:
    """quarterly_results 行から source を推定。"""
    fs = row.get("field_sources") or ""
    if "attachment_xbrl" in fs:
        return "attachment_xbrl"
    if "summary_xbrl" in fs or "xbrl" in fs.lower():
        return "summary_xbrl"
    if "pdf" in fs.lower():
        return "pdf"
    return "tdnet"


# ================================================================
# 差分対象の取得 (返り値に fallback_used を含む)
# ================================================================

def _select_financials_rows(
    db_path: str,
    *,
    target_keys: list[tuple[str, str, str]] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[list[dict], bool]:
    """financials 行を取得。Returns (rows, fallback_used)."""
    if not os.path.exists(db_path):
        return [], target_keys is not None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows: list[dict] = []
    fallback_used = False

    if target_keys:
        for ticker, period, quarter in target_keys:
            cursor = conn.execute(
                "SELECT * FROM quarterly_results "
                "WHERE company_code = ? AND period = ? AND quarter = ?",
                (ticker, period, quarter),
            )
            for r in cursor:
                rows.append(dict(r))

    if not rows:
        # fallback
        if target_keys:
            fallback_used = True
        cutoff = (datetime.now(JST) - timedelta(days=lookback_days)).isoformat()
        cursor = conn.execute(
            "SELECT * FROM quarterly_results WHERE updated_at >= ? "
            "ORDER BY id DESC",
            (cutoff,),
        )
        rows = [dict(r) for r in cursor]

    conn.close()
    return rows, fallback_used


def _select_segments_rows(
    db_path: str,
    *,
    target_keys: list[tuple[str, str, str]] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[list[dict], bool]:
    """segment 行を取得。Returns (rows, fallback_used)."""
    if not os.path.exists(db_path):
        return [], target_keys is not None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    if "segment_financials" not in tables:
        conn.close()
        return [], target_keys is not None

    rows: list[dict] = []
    fallback_used = False

    if target_keys:
        for ticker, period, quarter in target_keys:
            cursor = conn.execute(
                "SELECT * FROM segment_financials "
                "WHERE ticker = ? AND period = ? AND quarter = ?",
                (ticker, period, quarter),
            )
            for r in cursor:
                rows.append(dict(r))

    if not rows:
        if target_keys:
            fallback_used = True
        cols = [c[1] for c in conn.execute(
            "PRAGMA table_info(segment_financials)"
        ).fetchall()]
        if "updated_at" in cols:
            cutoff = (datetime.now(JST) - timedelta(days=lookback_days)).isoformat()
            cursor = conn.execute(
                "SELECT * FROM segment_financials WHERE updated_at >= ? "
                "ORDER BY rowid DESC",
                (cutoff,),
            )
            rows = [dict(r) for r in cursor]
        else:
            cursor = conn.execute(
                "SELECT * FROM segment_financials ORDER BY rowid DESC LIMIT 200"
            )
            rows = [dict(r) for r in cursor]

    conn.close()
    return rows, fallback_used


# ================================================================
# sync_canonical — メイン関数
# ================================================================

def sync_canonical(
    *,
    db_path: str,
    dry_run: bool = False,
    target_keys: list[tuple[str, str, str]] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    strict: bool = False,
) -> dict:
    """日次差分で canonical_financials / canonical_segments を更新する。

    Args:
        db_path: SQLite DB path (decision_db.db)
        dry_run: True なら Supabase 書き込みをスキップ
        target_keys: [(ticker, period, quarter), ...] — process 対象
        lookback_days: fallback lookback 日数
        strict: True なら canonical 失敗で例外を raise

    Returns:
        {
            "status": "ok" | "warning" | "error",
            "mode": "target_keys" | "lookback_fallback" | "lookback_only" | "empty",
            "fallback_used": bool,
            "target_keys_count": int,
            "resolved_target_count": int,
            "lookback_days": int,
            "financials": {targets, rows_selected, attempted, written, skipped, errors},
            "segments": {targets, rows_selected, attempted, written, skipped, errors},
            "summary": str,
        }
    """
    # ── 1. target_keys 正規化 ──
    target_keys = _normalize_target_keys(target_keys)
    target_keys_count = len(target_keys) if target_keys else 0

    # ── 2. Supabase config ──
    load_env()  # .env を環境変数にロード (戻り値は None)
    from .db import get_supabase_write_config
    config = get_supabase_write_config()
    if not config:
        logger.error("[canonical] no write config available (SUPABASE_SERVICE_ROLE_KEY missing)")
        return {"financials": _init_sub_stats(), "segments": _init_sub_stats()}

    fin_stats = _init_sub_stats()
    seg_stats = _init_sub_stats()

    # ── 3/4. primary + fallback selection ──
    logger.info("[FINANCIALS] select start")
    _t0 = time.monotonic()
    try:
        fin_rows, fin_fb = _select_financials_rows(
            db_path, target_keys=target_keys, lookback_days=lookback_days,
        )
        logger.info(f"[FINANCIALS] select done rows={len(fin_rows)} fallback_used={fin_fb} elapsed={time.monotonic()-_t0:.1f}s")
    except Exception as e:
        logger.error(f"[canonical_sync] financials selection FAILED: {e}")
        fin_rows, fin_fb = [], target_keys is not None
        fin_stats["errors"] += 1
        if strict:
            raise

    logger.info("[SEGMENTS] select start")
    _t0 = time.monotonic()
    try:
        seg_rows, seg_fb = _select_segments_rows(
            db_path, target_keys=target_keys, lookback_days=lookback_days,
        )
        logger.info(f"[SEGMENTS] select done rows={len(seg_rows)} fallback_used={seg_fb} elapsed={time.monotonic()-_t0:.1f}s")
    except Exception as e:
        logger.error(f"[canonical_sync] segments selection FAILED: {e}")
        seg_rows, seg_fb = [], target_keys is not None
        seg_stats["errors"] += 1
        if strict:
            raise

    # ── 5. mode / fallback_used 確定 ──
    fallback_used = fin_fb or seg_fb

    if target_keys and not fallback_used:
        mode = "target_keys"
    elif target_keys and fallback_used:
        mode = "lookback_fallback"
    elif not target_keys and (fin_rows or seg_rows):
        mode = "lookback_only"
    else:
        mode = "empty"

    # ── 6. rows_selected 算出 ──
    fin_stats["rows_selected"] = len(fin_rows)
    fin_stats["targets"] = len(fin_rows)
    seg_stats["rows_selected"] = len(seg_rows)
    seg_stats["targets"] = len(seg_rows)

    # resolved_target_count: target_keys から実際に取得できた unique target 数
    resolved_target_count = 0
    if target_keys and not fallback_used:
        fin_keys = set()
        for r in fin_rows:
            fin_keys.add((r.get("company_code", ""), r.get("period", ""), r.get("quarter", "")))
        seg_keys = set()
        for r in seg_rows:
            seg_keys.add((r.get("ticker", ""), r.get("period", ""), r.get("quarter", "")))
        resolved_target_count = len(fin_keys | seg_keys)
    elif target_keys and fallback_used:
        resolved_target_count = 0

    # ── 7. writer 呼び出し ──

    # -- Financials --
    try:
        if fin_rows and not dry_run:
            logger.info(f"[FINANCIALS] write start rows={len(fin_rows)}")
            _t0 = time.monotonic()
            for row in fin_rows:
                ticker = row.get("company_code", "")
                period = row.get("period", "")
                quarter = row.get("quarter", "")
                source = _detect_source(row)

                metrics = {}
                for db_col, metric_name in _FINANCIALS_METRIC_COLS.items():
                    val = row.get(db_col)
                    if val is not None:
                        metrics[metric_name] = val

                if not metrics:
                    fin_stats["skipped"] += 1
                    continue

                fin_stats["attempted"] += 1
                try:
                    wr = write_financials_canonical(
                        ticker=ticker,
                        period=period,
                        quarter=quarter,
                        metrics_dict=metrics,
                        source=source,
                        filing_id=row.get("disclosure_id"),
                        disclosure_datetime=row.get("disclosure_datetime"),
                        correction_flag=bool(row.get("revision_flag")),
                        config=config,
                    )
                    fin_stats["written"] += wr.get("written", 0)
                    fin_stats["skipped"] += wr.get("skipped", 0)
                    fin_stats["errors"] += wr.get("errors", 0)
                except Exception as e:
                    logger.warning(f"[canonical_sync] financials write error: {ticker} {period} {quarter}: {e}")
                    fin_stats["errors"] += 1
                    if strict:
                        raise
            logger.info(
                f"[FINANCIALS] write done written={fin_stats['written']} "
                f"skipped={fin_stats['skipped']} errors={fin_stats['errors']} "
                f"elapsed={time.monotonic()-_t0:.1f}s"
            )
        elif fin_rows and dry_run:
            logger.info(f"[FINANCIALS] DRY-RUN: {len(fin_rows)} targets")
            fin_stats["skipped"] = len(fin_rows)
    except Exception as e:
        if "financials write error" not in str(e):
            logger.error(f"[canonical_sync] financials FAILED: {e}")
            fin_stats["errors"] += 1
        if strict:
            raise

    # -- Segments --
    try:
        if seg_rows and not dry_run:
            logger.info(f"[SEGMENTS] write start rows={len(seg_rows)}")
            _t0 = time.monotonic()
            groups: dict[tuple, list] = {}
            for row in seg_rows:
                ticker = row.get("ticker", "")
                period = row.get("period", "")
                quarter = row.get("quarter", "")
                key = (ticker, period, quarter)
                groups.setdefault(key, []).append(row)

            logger.info(f"[SEGMENTS] grouped into {len(groups)} ticker/period/quarter keys")

            for grp_idx, ((ticker, period, quarter), segs) in enumerate(groups.items(), 1):
                seg_dicts = []
                for s in segs:
                    seg_name = s.get("segment_name", "")
                    if not seg_name or seg_name.strip() == "":
                        seg_stats["skipped"] += 1
                        continue
                    seg_dicts.append({
                        "segment_name": seg_name,
                        "sales": s.get("segment_sales"),
                        "profit": s.get("segment_profit"),
                    })

                if not seg_dicts:
                    continue

                seg_stats["attempted"] += len(seg_dicts)
                source = segs[-1].get("source", "tdnet")
                filing_id = segs[-1].get("filing_id")

                try:
                    wr = write_segments_canonical(
                        ticker=ticker,
                        period=period,
                        quarter=quarter,
                        segments=seg_dicts,
                        source=source,
                        filing_id=filing_id,
                        config=config,
                    )
                    seg_stats["written"] += wr.get("written", 0)
                    seg_stats["skipped"] += wr.get("skipped", 0)
                    seg_stats["errors"] += wr.get("errors", 0)
                except Exception as e:
                    logger.warning(f"[canonical_sync] segments write error: {ticker} {period} {quarter}: {e}")
                    seg_stats["errors"] += 1
                    if strict:
                        raise

                # 50グループごとに進捗ログ
                if grp_idx % 50 == 0 or grp_idx == len(groups):
                    logger.info(
                        f"[SEGMENTS] progress {grp_idx}/{len(groups)} "
                        f"written={seg_stats['written']} errors={seg_stats['errors']}"
                    )

            logger.info(
                f"[SEGMENTS] write done written={seg_stats['written']} "
                f"skipped={seg_stats['skipped']} errors={seg_stats['errors']} "
                f"elapsed={time.monotonic()-_t0:.1f}s"
            )
        elif seg_rows and dry_run:
            logger.info(f"[SEGMENTS] DRY-RUN: {len(seg_rows)} targets")
            seg_stats["skipped"] = len(seg_rows)
    except Exception as e:
        if "segments write error" not in str(e):
            logger.error(f"[canonical_sync] segments FAILED: {e}")
            seg_stats["errors"] += 1
        if strict:
            raise

    # ── 8. final status 判定 ──
    status = _determine_status(
        fin_stats=fin_stats,
        seg_stats=seg_stats,
        mode=mode,
        target_keys_count=target_keys_count,
    )

    # ── 9. result 組立 + summary 生成 ──
    result = {
        "status": status,
        "mode": mode,
        "fallback_used": fallback_used,
        "target_keys_count": target_keys_count,
        "resolved_target_count": resolved_target_count,
        "lookback_days": lookback_days,
        "financials": fin_stats,
        "segments": seg_stats,
        "summary": "",
    }
    result["summary"] = _build_summary(result)

    # ── 10. final aggregate log ──
    logger.info(
        f"[canonical_sync] done status={status} mode={mode} "
        f"fallback_used={fallback_used} "
        f"target_keys_count={target_keys_count} "
        f"resolved_target_count={resolved_target_count} "
        f"lookback_days={lookback_days} "
        f"financials(targets={fin_stats['targets']} rows={fin_stats['rows_selected']} "
        f"attempted={fin_stats['attempted']} written={fin_stats['written']} "
        f"skipped={fin_stats['skipped']} errors={fin_stats['errors']}) "
        f"segments(targets={seg_stats['targets']} rows={seg_stats['rows_selected']} "
        f"attempted={seg_stats['attempted']} written={seg_stats['written']} "
        f"skipped={seg_stats['skipped']} errors={seg_stats['errors']})"
    )

    # ── 11. return ──
    return result
