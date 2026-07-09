"""lib/pipeline/canonical_sync.py — 日次差分 canonical 同期

process ステップの後に呼ばれ、最新の抽出結果を
canonical_financials / canonical_segments に反映する。

差分判定:
  第一優先: process 対象の filing_id / ticker / period / quarter
  第二優先: lookback_days fallback (updated_at >= now - N日)

**バッチ化済み** — 全行を先に long row に展開してからまとめて
supabase_upsert() に渡す。個別行ごとの HTTP POST は発生しない。

NOTE: 将来、対象件数がさらに増える場合は
「全展開 → 全upsert」ではなく
「一定件数ごとに展開 + flush」のストリーミング方式へ移行可能。
現在はメモリ使用量が問題にならない規模のため全展開方式を採用。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

from .canonical_writer import expand_financials_rows, expand_segments_rows
from .db import load_env, supabase_upsert, get_supabase_write_config

logger = logging.getLogger("pipeline.canonical_sync")
JST = timezone(timedelta(hours=9))

# ================================================================
# helper: segment row から ticker/period を本番スキーマ優先で取得
# ================================================================

def _seg_ticker(row: dict) -> str:
    """segment row から ticker を取得。本番スキーマ (company_code) 優先。

    旧テスト/旧データ互換のため ticker へのフォールバックを残す。
    TODO: 旧スキーマデータが完全に無くなったらフォールバック削除。
    """
    v = row.get("company_code")
    if v is None:
        v = row.get("ticker", "")
    return v or ""


def _seg_period(row: dict) -> str:
    """segment row から period を取得。本番スキーマ (fiscal_year_end) 優先。

    旧テスト/旧データ互換のため period へのフォールバックを残す。
    TODO: 旧スキーマデータが完全に無くなったらフォールバック削除。
    """
    v = row.get("fiscal_year_end")
    if v is None:
        v = row.get("period", "")
    return v or ""


# ================================================================
# 定数
# ================================================================
DEFAULT_LOOKBACK_DAYS = 7

# canonical 書き込みの batch size (supabase_upsert に渡す)
# 展開ロジック = canonical_writer.py / 書き込み粒度 = ここ
CANONICAL_BATCH_SIZE = 200

# セグメント同期用のホワイトリストと除外条件 (excel_legacy, historical_backfill を除外)
_NON_BACKFILL_CLAUSE = "data_source IN ('backfill_xbrl', 'backfill_v4_pdf', 'backfill_v4_ai', 'tdnet', 'xbrl')"
_IS_BACKFILL_CLAUSE = "COALESCE(data_source, '') NOT IN ('backfill_xbrl', 'backfill_v4_pdf', 'backfill_v4_ai', 'tdnet', 'xbrl')"


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
        "batches_attempted": 0,
        "batches_succeeded": 0,
        "batches_failed": 0,
        "excluded_historical_backfill_sql": 0,
        "excluded_historical_backfill_safety": 0,
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
    excluded_hb = (
        seg.get("excluded_historical_backfill_sql", 0)
        + seg.get("excluded_historical_backfill_safety", 0)
    )
    parts = [
        f"canonical_sync {result['status']}",
        f"mode={result['mode']}",
        f"fallback_used={result['fallback_used']}",
        f"financials(written={fin['written']} errors={fin['errors']} "
        f"batches={fin['batches_succeeded']}/{fin['batches_attempted']})",
        f"segments(written={seg['written']} errors={seg['errors']} "
        f"excluded_hb={excluded_hb} "
        f"batches={seg['batches_succeeded']}/{seg['batches_attempted']})",
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
    allow_fallback: bool = True,
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
                "WHERE company_code = ? AND fiscal_year_end = ? AND quarter = ?",
                (ticker, period, quarter),
            )
            for r in cursor:
                rows.append(dict(r))

    if not rows and allow_fallback:
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
    elif not rows and not allow_fallback and target_keys:
        logger.warning(
            f"[canonical_sync] financials: target_keys={len(target_keys)} "
            f"resolved 0 rows, fallback disabled"
        )

    conn.close()
    return rows, fallback_used


def _select_segments_rows(
    db_path: str,
    *,
    target_keys: list[tuple[str, str, str]] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    allow_fallback: bool = True,
) -> tuple[list[dict], bool, int]:
    """segment 行を取得。Returns (rows, fallback_used, excluded_historical_backfill_count).

    excluded count は今回の SELECT スコープ内で historical_backfill
    として除外された件数 (DB 全体件数ではない)。
    """
    if not os.path.exists(db_path):
        return [], target_keys is not None, 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    if "segment_financials" not in tables:
        conn.close()
        return [], target_keys is not None, 0

    rows: list[dict] = []
    fallback_used = False
    excluded_count = 0

    # カラム存在チェック (target_keys / fallback 両方で使う)
    seg_cols = {c[1] for c in conn.execute(
        "PRAGMA table_info(segment_financials)"
    ).fetchall()}
    if "company_code" in seg_cols:
        tk_col, pd_col = "company_code", "fiscal_year_end"
    else:
        tk_col, pd_col = "ticker", "period"

    # data_source カラムが存在するかチェック
    has_data_source = "data_source" in seg_cols

    if target_keys:
        logger.debug(
            f"[canonical] segments WHERE using columns: "
            f"ticker_col={tk_col} period_col={pd_col}"
        )
        for ticker, period, quarter in target_keys:
            if has_data_source:
                cursor = conn.execute(
                    f"SELECT * FROM segment_financials "
                    f"WHERE {tk_col} = ? AND {pd_col} = ? AND quarter = ? "
                    f"AND {_NON_BACKFILL_CLAUSE}",
                    (ticker, period, quarter),
                )
                # スコープ内の除外件数カウント
                exc = conn.execute(
                    f"SELECT COUNT(*) FROM segment_financials "
                    f"WHERE {tk_col} = ? AND {pd_col} = ? AND quarter = ? "
                    f"AND {_IS_BACKFILL_CLAUSE}",
                    (ticker, period, quarter),
                ).fetchone()[0]
                excluded_count += exc
            else:
                cursor = conn.execute(
                    f"SELECT * FROM segment_financials "
                    f"WHERE {tk_col} = ? AND {pd_col} = ? AND quarter = ?",
                    (ticker, period, quarter),
                )
            for r in cursor:
                rows.append(dict(r))

    if not rows and allow_fallback:
        if target_keys:
            fallback_used = True
            logger.info(
                f"[canonical] segments target_keys={len(target_keys)} "
                f"resolved 0 rows from WHERE → entering fallback "
                f"(lookback_days={lookback_days})"
            )
        cols = seg_cols
        if "updated_at" in cols:
            cutoff = (datetime.now(JST) - timedelta(days=lookback_days)).isoformat()
            if has_data_source:
                cursor = conn.execute(
                    f"SELECT * FROM segment_financials "
                    f"WHERE updated_at >= ? AND {_NON_BACKFILL_CLAUSE} "
                    f"ORDER BY rowid DESC",
                    (cutoff,),
                )
                exc = conn.execute(
                    f"SELECT COUNT(*) FROM segment_financials "
                    f"WHERE updated_at >= ? AND {_IS_BACKFILL_CLAUSE}",
                    (cutoff,),
                ).fetchone()[0]
                excluded_count += exc
            else:
                cursor = conn.execute(
                    "SELECT * FROM segment_financials WHERE updated_at >= ? "
                    "ORDER BY rowid DESC",
                    (cutoff,),
                )
            rows = [dict(r) for r in cursor]
        else:
            if has_data_source:
                cursor = conn.execute(
                    f"SELECT * FROM segment_financials "
                    f"WHERE {_NON_BACKFILL_CLAUSE} "
                    f"ORDER BY rowid DESC LIMIT 200"
                )
                exc = conn.execute(
                    f"SELECT COUNT(*) FROM segment_financials "
                    f"WHERE {_IS_BACKFILL_CLAUSE}"
                ).fetchone()[0]
                excluded_count += exc
            else:
                cursor = conn.execute(
                    "SELECT * FROM segment_financials ORDER BY rowid DESC LIMIT 200"
                )
            rows = [dict(r) for r in cursor]
        if target_keys:
            logger.info(
                f"[canonical] segments fallback retrieved {len(rows)} rows"
            )
    elif not rows and not allow_fallback and target_keys:
        logger.warning(
            f"[canonical_sync] segments: target_keys={len(target_keys)} "
            f"resolved 0 rows, fallback disabled"
        )

    conn.close()
    return rows, fallback_used, excluded_count


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
    allow_fallback: bool = True,
    sync_financials: bool = True,
    sync_segments: bool = True,
) -> dict:
    """日次差分で canonical_financials / canonical_segments を更新する。

    Args:
        db_path: SQLite DB path (decision_db.db)
        dry_run: True なら Supabase 書き込みをスキップ
        target_keys: [(ticker, period, quarter), ...] — process 対象
        lookback_days: fallback lookback 日数
        strict: True なら canonical 失敗で例外を raise
        allow_fallback: False なら target_keys で 0 件でも lookback fallback しない
                         (realtime 用)
        sync_financials: False なら financials の書き込みを行わない
        sync_segments: False なら segments の書き込みを行わない

    Returns:
        {
            "status": "ok" | "warning" | "error",
            "mode": "target_keys" | "lookback_fallback" | "lookback_only" | "empty",
            "fallback_used": bool,
            "target_keys_count": int,
            "resolved_target_count": int,
            "lookback_days": int,
            "financials": {targets, rows_selected, attempted, written, skipped, errors,
                           batches_attempted, batches_succeeded, batches_failed},
            "segments": {targets, rows_selected, attempted, written, skipped, errors,
                         batches_attempted, batches_succeeded, batches_failed},
            "summary": str,
        }
    """
    # ── 1. target_keys 正規化 ──
    logger.info("[canonical] phase=extract_target_keys START")
    t_extract = time.monotonic()
    target_keys = _normalize_target_keys(target_keys)
    target_keys_count = len(target_keys) if target_keys else 0
    logger.info(
        f"[canonical] phase=extract_target_keys END "
        f"elapsed={time.monotonic()-t_extract:.1f}s "
        f"count={target_keys_count}"
    )

    # ── 2. Supabase config ──
    load_env()  # .env を環境変数にロード (戻り値は None)
    config = get_supabase_write_config()
    if not config:
        logger.error("[canonical] no write config available (SUPABASE_SERVICE_ROLE_KEY missing)")
        return {"financials": _init_sub_stats(), "segments": _init_sub_stats()}

    fin_stats = _init_sub_stats()
    seg_stats = _init_sub_stats()

    # ── 3/4. primary + fallback selection ──
    logger.info("[canonical] phase=select_financials_rows START")
    _t0 = time.monotonic()
    try:
        if sync_financials:
            fin_rows, fin_fb = _select_financials_rows(
                db_path, target_keys=target_keys, lookback_days=lookback_days,
                allow_fallback=allow_fallback,
            )
        else:
            fin_rows, fin_fb = [], False
        logger.info(
            f"[canonical] phase=select_financials_rows END "
            f"elapsed={time.monotonic()-_t0:.1f}s "
            f"rows={len(fin_rows)} fallback_used={fin_fb}"
        )
    except Exception as e:
        logger.error(f"[canonical_sync] financials selection FAILED: {e}")
        fin_rows, fin_fb = [], target_keys is not None
        fin_stats["errors"] += 1
        if strict:
            raise

    logger.info("[canonical] phase=select_segments_rows START")
    _t0 = time.monotonic()
    seg_excluded_sql = 0
    try:
        if sync_segments:
            seg_rows, seg_fb, seg_excluded_sql = _select_segments_rows(
                db_path, target_keys=target_keys, lookback_days=lookback_days,
                allow_fallback=allow_fallback,
            )
        else:
            seg_rows, seg_fb, seg_excluded_sql = [], False, 0
        logger.info(
            f"[canonical] phase=select_segments_rows END "
            f"elapsed={time.monotonic()-_t0:.1f}s "
            f"rows={len(seg_rows)} fallback_used={seg_fb} "
            f"excluded_historical_backfill_sql={seg_excluded_sql}"
        )

        # ── 必須改修 1: row サンプルログ ──
        if seg_rows:
            sample_rows = seg_rows[:5]
            for i, sr in enumerate(sample_rows):
                logger.debug(
                    f"[canonical][debug] sample_segment_row_{i} "
                    f"keys={list(sr.keys())} "
                    f"ticker='{_seg_ticker(sr)}' "
                    f"period='{_seg_period(sr)}' "
                    f"quarter='{sr.get('quarter', '')}' "
                    f"source='{sr.get('data_source', sr.get('source', ''))}' "
                    f"segment_name='{sr.get('segment_name', '')}'"
                )

        # ── 必須改修 5: field 名の明示確認 (1件分の key 一覧) ──
        if seg_rows:
            logger.debug(
                f"[canonical][debug] segment_row_keys={list(seg_rows[0].keys())}"
            )

        # ── 必須改修 6: source別の分布確認 ──
        if seg_rows:
            from collections import Counter
            src_counter = Counter(
                r.get("data_source", r.get("source", "unknown")) for r in seg_rows
            )
            empty_tk_by_src: dict[str, int] = {}
            for r in seg_rows:
                src = r.get("data_source", r.get("source", "unknown"))
                if not _seg_ticker(r):
                    empty_tk_by_src[src] = empty_tk_by_src.get(src, 0) + 1
            logger.debug(
                f"[canonical][debug] segments source distribution: "
                f"{dict(src_counter)}"
            )
            if empty_tk_by_src:
                logger.debug(
                    f"[canonical][debug] segments empty_ticker by source: "
                    f"{empty_tk_by_src}"
                )
    except Exception as e:
        logger.error(f"[canonical_sync] segments selection FAILED: {e}")
        seg_rows, seg_fb, seg_excluded_sql = [], target_keys is not None, 0
        seg_stats["errors"] += 1
        if strict:
            raise

    seg_stats["excluded_historical_backfill_sql"] = seg_excluded_sql

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
            seg_keys.add((_seg_ticker(r), _seg_period(r), r.get("quarter", "")))
        resolved_target_count = len(fin_keys | seg_keys)
    elif target_keys and fallback_used:
        resolved_target_count = 0

    # ── 7. バッチ展開 + 一括 upsert ──
    # requests.Session を共有して HTTP 接続を再利用する
    import requests as _requests_mod

    with _requests_mod.Session() as session:

        # -- Financials --
        # Note: quarterly_results のデータは百万円単位 (unit 列 = "百万円")。
        # canonical_financials には unit="millions_jpy" で書き込む。
        try:
            if sync_financials and fin_rows and not dry_run:
                logger.info(
                    f"[canonical] write_canonical_financials START "
                    f"rows={len(fin_rows)}"
                )
                _t0 = time.monotonic()

                # ── 全行を long row に展開 ──
                all_fin_long_rows: list[dict] = []
                for row in fin_rows:
                    ticker = row.get("company_code", "")
                    period = row.get("period") or row.get("fiscal_year_end", "")
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

                    expanded, skipped = expand_financials_rows(
                        ticker=ticker,
                        period=period,
                        quarter=quarter,
                        metrics_dict=metrics,
                        source=source,
                        filing_id=row.get("disclosure_id"),
                        disclosure_datetime=row.get("disclosure_datetime"),
                        correction_flag=bool(row.get("revision_flag")),
                        unit="millions_jpy",
                    )
                    all_fin_long_rows.extend(expanded)
                    fin_stats["skipped"] += skipped

                fin_stats["attempted"] = len(all_fin_long_rows)

                # ── 一括 upsert (supabase_upsert 内部で batch_size ごとに分割) ──
                if all_fin_long_rows:
                    try:
                        upsert_result = supabase_upsert(
                            "canonical_financials",
                            all_fin_long_rows,
                            on_conflict="source_row_key",
                            config=config,
                            batch_size=CANONICAL_BATCH_SIZE,
                            session=session,
                        )
                        if upsert_result.get("ok"):
                            fin_stats["written"] = upsert_result.get("count", 0)
                        else:
                            fin_stats["written"] = upsert_result.get("count", 0)
                            fin_stats["errors"] += len(all_fin_long_rows) - upsert_result.get("count", 0)
                            logger.warning(
                                f"[canonical] financials upsert partial/failed: "
                                f"error={upsert_result.get('error', 'unknown')}"
                            )
                        fin_stats["batches_attempted"] = upsert_result.get("batches_attempted", 0)
                        fin_stats["batches_succeeded"] = upsert_result.get("batches_succeeded", 0)
                        fin_stats["batches_failed"] = upsert_result.get("batches_failed", 0)
                    except Exception as e:
                        logger.warning(
                            f"[canonical_sync] financials upsert EXCEPTION: {e} "
                            f"rows={len(all_fin_long_rows)} "
                            f"first_keys={[r.get('source_row_key','?') for r in all_fin_long_rows[:3]]}"
                        )
                        fin_stats["errors"] += len(all_fin_long_rows)
                        if strict:
                            raise

                logger.info(
                    f"[canonical] write_canonical_financials END "
                    f"elapsed={time.monotonic()-_t0:.1f}s "
                    f"rows_total={len(all_fin_long_rows)} "
                    f"written={fin_stats['written']} "
                    f"skipped={fin_stats['skipped']} errors={fin_stats['errors']} "
                    f"batches={fin_stats['batches_succeeded']}/{fin_stats['batches_attempted']}"
                )
            elif sync_financials and fin_rows and dry_run:
                logger.info(f"[canonical] write_canonical_financials DRY-RUN targets={len(fin_rows)}")
                fin_stats["skipped"] = len(fin_rows)
        except Exception as e:
            if "financials upsert EXCEPTION" not in str(e):
                logger.error(f"[canonical_sync] financials FAILED: {e}")
                fin_stats["errors"] += 1
            if strict:
                raise

        # -- Segments --
        try:
            if sync_segments and seg_rows and not dry_run:
                logger.info(
                    f"[canonical] write_canonical_segments START "
                    f"rows={len(seg_rows)}"
                )
                _t0 = time.monotonic()

                # ── 必須改修 2: grouping 前ログ ──
                empty_ticker_count = sum(1 for r in seg_rows if not _seg_ticker(r))
                empty_period_count = sum(1 for r in seg_rows if not _seg_period(r))
                empty_quarter_count = sum(1 for r in seg_rows if not r.get("quarter"))
                logger.debug(
                    f"[canonical][debug] segments before grouping "
                    f"rows={len(seg_rows)} "
                    f"empty_ticker={empty_ticker_count} "
                    f"empty_period={empty_period_count} "
                    f"empty_quarter={empty_quarter_count}"
                )

                # ── 必須改修 7: fail-fast 検証モード ──
                if len(seg_rows) > 0:
                    et_pct = empty_ticker_count / len(seg_rows)
                    ep_pct = empty_period_count / len(seg_rows)
                    if et_pct >= 0.9:
                        logger.warning(
                            f"[canonical][warn] segment rows have abnormal "
                            f"empty fields: empty_ticker="
                            f"{empty_ticker_count}/{len(seg_rows)}"
                        )
                    if ep_pct >= 0.9:
                        logger.warning(
                            f"[canonical][warn] segment rows have abnormal "
                            f"empty fields: empty_period="
                            f"{empty_period_count}/{len(seg_rows)}"
                        )

                # ── グループ化 + 全行を long row に展開 ──
                # safety skip: SQL 除外漏れの保険
                seg_excluded_safety = 0
                groups: dict[tuple, list] = {}
                for row in seg_rows:
                    # 保険フィルタ: historical_backfill が漏れていたらスキップ
                    row_source = row.get("data_source", row.get("source", ""))
                    if row_source == "historical_backfill":
                        seg_excluded_safety += 1
                        continue
                    # 本番スキーマ (company_code/fiscal_year_end) 優先
                    ticker = _seg_ticker(row)
                    period = _seg_period(row)
                    quarter = row.get("quarter", "")
                    key = (ticker, period, quarter)
                    groups.setdefault(key, []).append(row)
                seg_stats["excluded_historical_backfill_safety"] = seg_excluded_safety
                if seg_excluded_safety > 0:
                    logger.warning(
                        f"[canonical][warn] safety skip caught {seg_excluded_safety} "
                        f"historical_backfill rows that passed SQL filter"
                    )

                logger.info(f"[canonical] grouped into {len(groups)} ticker/period/quarter keys")

                # ── 必須改修 2: grouping 後ログ ──
                group_keys = list(groups.keys())
                logger.debug(
                    f"[canonical][debug] grouped_keys_count={len(group_keys)}"
                )
                for gi, gk in enumerate(group_keys[:3]):
                    logger.debug(
                        f"[canonical][debug] grouped_key_{gi} "
                        f"ticker='{gk[0]}' period='{gk[1]}' quarter='{gk[2]}'"
                    )

                all_seg_long_rows: list[dict] = []
                for (ticker, period, quarter), segs in groups.items():
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

                    source = segs[-1].get("data_source", segs[-1].get("source", "tdnet"))
                    filing_id = segs[-1].get("tdnet_doc_id", segs[-1].get("filing_id"))

                    # ── 必須改修 3: expand_segments_rows 入力ログ ──
                    logger.debug(
                        f"[canonical][debug] expand_segments_rows call "
                        f"ticker='{ticker}' period='{period}' "
                        f"quarter='{quarter}' source='{source}' "
                        f"len(segments)={len(seg_dicts)} "
                        f"sample_seg_0={seg_dicts[0] if seg_dicts else '{}'}"
                    )

                    expanded, skipped = expand_segments_rows(
                        ticker=ticker,
                        period=period,
                        quarter=quarter,
                        segments=seg_dicts,
                        source=source,
                        filing_id=filing_id,
                        unit="millions_jpy",
                    )
                    all_seg_long_rows.extend(expanded)
                    seg_stats["skipped"] += skipped

                seg_stats["attempted"] = len(all_seg_long_rows)

                # ── 一括 upsert ──
                if all_seg_long_rows:
                    try:
                        upsert_result = supabase_upsert(
                            "canonical_segments",
                            all_seg_long_rows,
                            on_conflict="source_row_key",
                            config=config,
                            batch_size=CANONICAL_BATCH_SIZE,
                            session=session,
                        )
                        if upsert_result.get("ok"):
                            seg_stats["written"] = upsert_result.get("count", 0)
                        else:
                            seg_stats["written"] = upsert_result.get("count", 0)
                            seg_stats["errors"] += len(all_seg_long_rows) - upsert_result.get("count", 0)
                            logger.warning(
                                f"[canonical] segments upsert partial/failed: "
                                f"error={upsert_result.get('error', 'unknown')}"
                            )
                        seg_stats["batches_attempted"] = upsert_result.get("batches_attempted", 0)
                        seg_stats["batches_succeeded"] = upsert_result.get("batches_succeeded", 0)
                        seg_stats["batches_failed"] = upsert_result.get("batches_failed", 0)
                    except Exception as e:
                        logger.warning(
                            f"[canonical_sync] segments upsert EXCEPTION: {e} "
                            f"rows={len(all_seg_long_rows)} "
                            f"first_keys={[r.get('source_row_key','?') for r in all_seg_long_rows[:3]]}"
                        )
                        seg_stats["errors"] += len(all_seg_long_rows)
                        if strict:
                            raise

                logger.info(
                    f"[canonical] write_canonical_segments END "
                    f"elapsed={time.monotonic()-_t0:.1f}s "
                    f"rows_total={len(all_seg_long_rows)} "
                    f"written={seg_stats['written']} "
                    f"skipped={seg_stats['skipped']} errors={seg_stats['errors']} "
                    f"batches={seg_stats['batches_succeeded']}/{seg_stats['batches_attempted']}"
                )
            elif sync_segments and seg_rows and dry_run:
                logger.info(f"[canonical] write_canonical_segments DRY-RUN targets={len(seg_rows)}")
                seg_stats["skipped"] = len(seg_rows)
        except Exception as e:
            if "segments upsert EXCEPTION" not in str(e):
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
        f"skipped={fin_stats['skipped']} errors={fin_stats['errors']} "
        f"batches={fin_stats['batches_succeeded']}/{fin_stats['batches_attempted']}) "
        f"segments(targets={seg_stats['targets']} rows={seg_stats['rows_selected']} "
        f"attempted={seg_stats['attempted']} written={seg_stats['written']} "
        f"skipped={seg_stats['skipped']} errors={seg_stats['errors']} "
        f"excluded_hb_sql={seg_stats['excluded_historical_backfill_sql']} "
        f"excluded_hb_safety={seg_stats['excluded_historical_backfill_safety']} "
        f"batches={seg_stats['batches_succeeded']}/{seg_stats['batches_attempted']})"
    )

    # ── 11. return ──
    return result
