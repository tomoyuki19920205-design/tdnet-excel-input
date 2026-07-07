#!/usr/bin/env python3
r"""
sync_segments.py -- XBRL ZIP + SQLite(excel_legacy) -> segment_canonical sync

Standard operation syncs BOTH sources. Use --xbrl-only for XBRL-only mode.

Usage:
  .\.venv\Scripts\python.exe tools\sync_segments.py --dry-run
  .\.venv\Scripts\python.exe tools\sync_segments.py --apply
  .\.venv\Scripts\python.exe tools\sync_segments.py --apply --xbrl-only
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import requests
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
from src.segment.normalize import (
    classify_special_row,
    normalize_segment_key,
    resolve_segment_key_with_jp,
    is_english_dominant,
)

logger = logging.getLogger("sync_seg")
JST = timezone(timedelta(hours=9))


# ============================================================
# 過去EDINET日本語候補取得
# ============================================================
def get_jp_segment_names(
    rest_url: str,
    headers: dict,
    ticker: str,
    *,
    target_period: str | None = None,
    max_periods: int = 5,
    limit: int = 200,
) -> list[str]:
    """canonical_segments から同一 ticker の過去EDINET日本語セグメント名を取得する。

    設計方针:
      - period 完全一致は必須にしない。
        最新TDNET FYに対する同期 EDINETは未存在のため。
      - target_period があれば period <= target_period の過去候補を取得。
      - source = 'edinet_xbrl' を優先、なければ全 source から取得。
      - period desc で新しい順、最大 max_periods 期にわたる重複排除済み日本語名を返す。
      - ASCII比率 > 0.5 の行（英語優勢）は除外。

    Args:
        rest_url: Supabase REST URL
        headers: 認証ヘッダー
        ticker: 4桁銘柄コード
        target_period: TDNET同期対象 YYYY-MM-DD。None の場合は制限なし。
        max_periods: 参照する最大期数（デフォルト 5）
        limit: Supabase リクエスト最大件数（デフォルト 200）

    Returns:
        日本語セグメント名のリスト（重複排除済み）
    """
    import requests as _req
    try:
        # source='edinet_xbrl' 先行、なければ全 source
        for source_filter in ["edinet_xbrl", None]:
            params: dict = {
                "ticker": f"eq.{ticker}",
                "select": "segment_name,period,quarter,source",
                "order": "period.desc,quarter.desc",
                "limit": str(limit),
            }
            if target_period:
                params["period"] = f"lte.{target_period}"
            if source_filter:
                params["source"] = f"eq.{source_filter}"

            r = _req.get(
                f"{rest_url}/canonical_segments",
                headers=headers,
                params=params,
                timeout=15,
            )
            rows = r.json() if r.status_code == 200 else []

            # 日本語優勢（ASCII比率 <= 0.5）のみ重複排除して収集
            seen_names: set[str] = set()
            period_count: dict[str, int] = {}
            result_names: list[str] = []
            for row in rows:
                seg = (row.get("segment_name") or "").strip()
                period_val = row.get("period") or ""
                if not seg or seg in seen_names:
                    continue
                alpha = sum(1 for c in seg if c.isascii() and c.isalpha())
                if alpha / max(len(seg), 1) > 0.5:
                    continue  # 英語優勢は除外
                seen_names.add(seg)
                cnt = period_count.get(period_val, 0)
                if cnt == 0 and len(period_count) >= max_periods:
                    continue  # max_periods 期履歴を超えたら打ち切り
                period_count[period_val] = cnt + 1
                result_names.append(seg)

            if not result_names:
                continue  # edinet_xbrl なし → 全 source で再試行

            logger.debug(
                f"[sync_seg] jp_candidates ticker={ticker}"
                f" target_period={target_period} source={source_filter or 'any'}"
                f" found={len(result_names)} names={result_names[:4]}"
            )
            return result_names

        return []

    except Exception as e:
        logger.debug(f"[sync_seg] get_jp_segment_names error ticker={ticker}: {e}")
        return []


# ============================================================
# canonical 条件 (conservative)
# ============================================================
_VALID_QUARTERS = {"1Q", "2Q", "3Q", "FY"}


def _is_canonical_candidate(row) -> tuple[bool, str]:
    """canonical に採用できるかチェック。

    Returns:
        (ok, reason)
    """
    if row.quarter not in _VALID_QUARTERS:
        return False, f"invalid quarter: {row.quarter}"
    if row.special_row_type != "ordinary_segment":
        return False, f"special_row_type: {row.special_row_type}"
    if not row.normalized_segment_name:
        return False, "no normalized_segment_name"
    if row.sales is None and row.profit is None:
        return False, "no sales or profit"
    return True, ""


# ============================================================
# Supabase upsert
# ============================================================
def _upsert_segment_raw(row, rest_url: str, headers: dict, dry_run: bool) -> str:
    """segment_raw に insert。"""
    import requests
    # デプロイ仕様 DDL に準拠したカラムのみ
    payload = {
        "source": row.source,
        "source_doc_type": row.source_doc_type,
        "raw_ticker": row.raw_ticker,
        "normalized_ticker": row.normalized_ticker,
        "period": row.period,
        "quarter": row.quarter,
        "raw_segment_name": row.raw_segment_name,
        "normalized_segment_name": row.normalized_segment_name,
        "special_row_type": row.special_row_type,
        "sales": row.sales,
        "profit": row.profit,
        "confidence_score": float(row.confidence_score),
        "extraction_method": row.extraction_method,
        "is_consolidated": row.is_consolidated,
        "accounting_standard": row.accounting_standard,
    }
    if dry_run:
        return "dry_run"
    
    r = requests.post(
        f"{rest_url}/segment_raw",
        json=payload,
        headers={**headers, "Prefer": "return=minimal"},
        timeout=30,
    )
    if r.status_code in (200, 201):
        return "upserted"
    else:
        logger.warning(f"[RAW] insert failed: {r.status_code} {r.text[:200]}")
        return "error"


def _upsert_segment_canonical(
    row,
    rest_url: str,
    headers: dict,
    dry_run: bool,
    *,
    jp_segment_names: list[str] | None = None,
    match_stats: dict | None = None,
) -> str:
    """segment_canonical に upsert (PK: ticker, period, quarter, segment_name)。

    segment_key 決定ロジック:
      - 日英統合なし方針（2026-05以降）:
        英語名・日本語名ともに normalize_segment_key(segment_name) のみで segment_key を生成する。
        過去 EDINET 日本語名への寄せ処理（resolve_segment_key_with_jp）は無効。
        英語名は英語セグメントとして、日本語名は日本語セグメントとして独立して格納する。

    NOTE: jp_segment_names / match_stats 引数は互換性のため残しているが使用しない。
    """
    import requests
    seg_name = row.normalized_segment_name or ""

    # segment_key: 日英統合なし — segment_name を直接 normalize するのみ
    seg_key = normalize_segment_key(seg_name)
    logger.debug(
        f"[sync_seg] seg_key ticker={row.normalized_ticker}"
        f" period={row.period} name={seg_name!r} key={seg_key!r}"
    )

    # デプロイ仕様 DDL に準拠したカラムのみ
    payload = {
        "ticker": row.normalized_ticker,
        "period": row.period,
        "quarter": row.quarter,
        "segment_name": seg_name,
        "segment_key": seg_key,
        "sales": row.sales,
        "profit": row.profit,
        "source": row.source,
        "confidence_score": float(row.confidence_score),
        "updated_at": datetime.now(JST).isoformat(),
    }
    if dry_run:
        return "dry_run"

    r = requests.post(
        f"{rest_url}/segment_canonical",
        json=payload,
        headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
        timeout=30,
    )
    if r.status_code in (200, 201):
        return "upserted"
    else:
        logger.warning(f"[CANONICAL] upsert failed: {r.status_code} {r.text[:200]}")
        return "error"


# ============================================================
# Supabase canonical_segments 集計行クリーンアップ
# ============================================================

# aggregate 判定: Supabase 側で削除する segment_name パターン
_SUPABASE_AGG_EXACT: tuple[str, ...] = (
    "連結", "合計", "計", "調整額",
    "consolidated", "total", "adjustment", "eliminations",
)
_SUPABASE_AGG_CONTAINS: tuple[str, ...] = (
    "調整額", "セグメント利益調整", "その他調整",
    "adjustment", "eliminations",
)
_SUPABASE_AGG_ENDSWITH: tuple[str, ...] = ("合計", "計", "total")
# ホワイトリスト: これで始まる名前は前宛条件を除いて保護
_SUPABASE_AGG_WHITELIST: tuple[str, ...] = ("その他", "other")


def _is_aggregate_name(name: str) -> bool:
    """segment_name が集計行属性か判定。「その他」系は調整系ワードを含む場合のみ True。"""
    if not name:
        return False
    import unicodedata
    n = unicodedata.normalize("NFKC", name.strip()).lower()

    # ホワイトリスト優先: 「その他」「other」で始まる場合は保護
    for prefix in _SUPABASE_AGG_WHITELIST:
        if n.startswith(prefix.lower()):
            # 調整系ワードを含む場合は保護解除
            if any(kw.lower() in n for kw in ("調整", "adjustment", "eliminations")):
                break
            return False

    if n in {k.lower() for k in _SUPABASE_AGG_EXACT}:
        return True
    for kw in _SUPABASE_AGG_CONTAINS:
        if kw.lower() in n:
            return True
    for kw in _SUPABASE_AGG_ENDSWITH:
        kw_l = kw.lower()
        if n.endswith(kw_l) and len(n) > len(kw_l):
            is_whitelisted = any(
                n.startswith(p.lower()) and
                not any(adj.lower() in n for adj in ("調整", "adjustment", "eliminations"))
                for p in _SUPABASE_AGG_WHITELIST
            )
            if not is_whitelisted:
                return True
    return False


def cleanup_supabase_aggregate_orphans(
    rest_url: str,
    headers: dict,
    *,
    sources: tuple[str, ...] = (
        "backfill_v4_pdf", "v4_pdf",
        "excel_legacy", "legacy_excel",
    ),
    page_size: int = 1000,
    dry_run: bool = False,
) -> int:
    """Supabase canonical_segments から aggregate 属性の orphan 行を一括削除する。

    起点: Supabase 側の canonical_segments を直接検索（SQLite 不要）。
    これにより、ローカル側から削除済みの excel_legacy 等の連結行も正しく捕捉できる。

    フロー:
      1. canonical_segments から source 別に全件をページング GET
      2. Python 側で aggregate 判定して対象行 ID を収集
      3. ID をまとめて DELETE

    Returns:
        削除件数
    """
    import requests

    deleted_total = 0

    for source in sources:
        offset = 0
        source_targets: list[dict] = []  # {"id": ..., "label": "ticker|period|quarter|name"}

        # ── Step 1: 全件をページング取得 ──
        while True:
            params = {
                "source": f"eq.{source}",
                "select": "id,ticker,period,quarter,segment_name",
                "limit": str(page_size),
                "offset": str(offset),
                "order": "id.asc",
            }
            r = requests.get(
                f"{rest_url}/canonical_segments",
                headers=headers, params=params, timeout=30,
            )
            if r.status_code != 200:
                logger.warning(
                    "[sync_segments] orphan GET failed source=%s status=%d body=%s",
                    source, r.status_code, r.text[:200],
                )
                break
            page = r.json()
            if not isinstance(page, list) or not page:
                break  # 空 や 最後ページ

            # ── Step 2: aggregate 判定 ──
            for row in page:
                name = row.get("segment_name") or ""
                if _is_aggregate_name(name):
                    label = "|".join([
                        str(row.get("ticker") or ""),
                        str(row.get("period") or ""),
                        str(row.get("quarter") or ""),
                        name,
                    ])
                    source_targets.append({"id": row["id"], "label": label})

            if len(page) < page_size:
                break  # 最後ページ
            offset += page_size

        if not source_targets:
            logger.debug(
                "[sync_segments] orphan cleanup: no aggregates found source=%s", source
            )
            continue

        target_ids   = [t["id"]    for t in source_targets]
        target_labels = [t["label"] for t in source_targets]

        if dry_run:
            logger.info(
                "[sync_segments] [DRY-RUN] cleanup_supabase_aggregate_orphans "
                "source=%s would_delete=%d names=%s",
                source, len(target_ids), target_labels,
            )
            deleted_total += len(target_ids)
            continue

        # ── Step 3: まとめて DELETE ──
        del_params = {"id": "in.(" + ",".join(str(i) for i in target_ids) + ")"}
        dr = requests.delete(
            f"{rest_url}/canonical_segments",
            headers={**headers, "Prefer": "return=minimal"},
            params=del_params,
            timeout=30,
        )
        if dr.status_code in (200, 204):
            deleted_total += len(target_ids)
            logger.info(
                "[sync_segments] cleanup_supabase_aggregate_orphans "
                "source=%s deleted=%d names=%s",
                source, len(target_ids), target_labels,
            )
        else:
            logger.warning(
                "[sync_segments] cleanup_supabase_aggregate_orphans DELETE failed "
                "source=%s status=%d body=%s",
                source, dr.status_code, dr.text[:200],
            )

    return deleted_total



# segment_name としてスキップする値（ヘッダー行や無効行）
_SKIP_SEGMENT_NAMES = {
    "売上", "利益", "月次売上", "累計", "0", "#VALUE!", "",
}

_QUARTER_MAP = {"4Q": "FY"}


def _classify_skip_reason(row: dict) -> str:
    """スキップ理由を分類して返す。valid なら空文字列。"""
    name = (row.get("segment_name") or "").strip()
    if not name:
        return "empty_name"
    if name in _SKIP_SEGMENT_NAMES:
        return "header"
    if name.startswith("UNKNOWN_"):
        return "unknown"
    sales = row.get("segment_sales") or 0
    profit = row.get("segment_profit") or 0
    if sales == 0 and profit == 0:
        return "zero_value"
    quarter = (row.get("quarter") or "")
    if quarter == "?Q":
        return "invalid_quarter"
    # ratio check
    if sales is not None and abs(sales) > 0 and abs(sales) < 1:
        return "ratio"
    if profit is not None and abs(profit) > 0 and abs(profit) < 1:
        return "ratio"
    return ""


def _is_valid_sqlite_segment(row: dict) -> bool:
    """SQLite segment_financials 行が canonical 対象か判定。"""
    return _classify_skip_reason(row) == ""


def count_sqlite_valid_rows(db_path: str) -> int:
    """SQLite に有効セグメント行が何件あるか返す (guard 用)。"""
    if not os.path.isfile(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT segment_name, segment_sales, segment_profit, quarter "
        "FROM segment_financials"
    ).fetchall()
    conn.close()
    return sum(1 for r in rows if _is_valid_sqlite_segment(dict(r)))


def sync_sqlite_segments(
    db_path: str, rest_url: str, headers: dict, dry_run: bool,
) -> dict:
    """SQLite segment_financials -> segment_canonical / canonical_segments に push.

    segment_financials.data_source の値をそのまま source として使う。
    data_source が None の場合は 'excel_legacy' にフォールバック。
    backfill_v4_pdf 等の高優先 source は source_priority=0 で canonical_segments に登録される。
    """
    stats = {
        "sqlite_total": 0,
        "sqlite_valid": 0,
        "sqlite_upserted": 0,
        "sqlite_errors": 0,
        "sqlite_skip_header": 0,
        "sqlite_skip_zero": 0,
        "sqlite_skip_unknown": 0,
        "sqlite_skip_quarter": 0,
        "sqlite_skip_ratio": 0,
        "sqlite_skip_empty": 0,
    }

    if not os.path.isfile(db_path):
        logger.warning(f"[SQLITE] DB not found: {db_path}")
        return stats

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT company_code, fiscal_year_end, quarter, segment_name, "
        "segment_sales, segment_profit, data_source "
        "FROM segment_financials"
    ).fetchall()

    stats["sqlite_total"] = len(rows)
    logger.info(f"[SQLITE] segment_financials: {len(rows):,} rows")

    for row in rows:
        rdict = dict(row)
        reason = _classify_skip_reason(rdict)
        if reason:
            skip_key = f"sqlite_skip_{reason}"
            if skip_key in stats:
                stats[skip_key] += 1
            continue

        stats["sqlite_valid"] += 1
        quarter = _QUARTER_MAP.get(rdict["quarter"], rdict["quarter"])
        seg_name = rdict["segment_name"].strip()

        # SQLite REAL -> Supabase bigint: int() で変換
        raw_sales = rdict["segment_sales"]
        raw_profit = rdict["segment_profit"]
        sales = int(raw_sales) if raw_sales is not None else None
        profit = int(raw_profit) if raw_profit is not None else None

        payload = {
            "ticker": rdict["company_code"],
            "period": rdict["fiscal_year_end"],
            "quarter": quarter,
            "segment_name": seg_name,
            "sales": sales,
            "profit": profit,
            "source": rdict.get("data_source") or "excel_legacy",
            "updated_at": datetime.now(JST).isoformat(),
        }
        if dry_run:
            stats["sqlite_upserted"] += 1
            continue

        r = requests.post(
            f"{rest_url}/segment_canonical",
            json=payload,
            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=30,
        )
        if r.status_code in (200, 201):
            stats["sqlite_upserted"] += 1
        else:
            stats["sqlite_errors"] += 1
            logger.warning(f"[SQLITE] upsert failed: {r.status_code} {r.text[:200]}")

    conn.close()

    # ── Phase 2-A: canonical dual-write (best-effort) ──
    if not dry_run and stats["sqlite_valid"] > 0:
        try:
            from lib.pipeline.canonical_writer import write_segments_canonical
            from lib.pipeline.db import load_env, get_supabase_write_config
            load_env()
            canonical_config = get_supabase_write_config()
            if canonical_config:
                # per-(ticker, period, quarter, data_source) batch に再構成
                # data_source ごとに分けることで source が正しく引き継がれる
                ticker_batches: dict[tuple, list[dict]] = {}
                conn2 = sqlite3.connect(db_path)
                conn2.row_factory = sqlite3.Row
                for row in conn2.execute(
                    "SELECT company_code, fiscal_year_end, quarter, segment_name, "
                    "segment_sales, segment_profit, data_source "
                    "FROM segment_financials"
                ).fetchall():
                    rdict = dict(row)
                    if _classify_skip_reason(rdict):
                        continue
                    quarter = _QUARTER_MAP.get(rdict["quarter"], rdict["quarter"])
                    # data_source を引き継ぐ (なければ excel_legacy)
                    ds = rdict.get("data_source") or "excel_legacy"
                    key = (rdict["company_code"], rdict["fiscal_year_end"], quarter, ds)
                    if key not in ticker_batches:
                        ticker_batches[key] = []
                    raw_sales = rdict["segment_sales"]
                    raw_profit = rdict["segment_profit"]
                    ticker_batches[key].append({
                        "segment_name": rdict["segment_name"].strip(),
                        "sales": int(raw_sales) if raw_sales is not None else None,
                        "profit": int(raw_profit) if raw_profit is not None else None,
                    })
                conn2.close()

                canonical_total = 0
                canonical_errors = 0
                # backfill_v4_pdf を含む (ticker, period, quarter) を記録
                # → 後で xbrl:Other only 行を削除するために使用
                _v4pdf_keys: set[tuple] = set()
                for (ticker, period, quarter, ds), segs in ticker_batches.items():
                    cw_result = write_segments_canonical(
                        ticker=ticker,
                        period=period,
                        quarter=quarter,
                        segments=segs,
                        source=ds,  # SQLite の data_source をそのまま使う
                        config=canonical_config,
                    )
                    canonical_total += cw_result["written"]
                    canonical_errors += cw_result["errors"]
                    if ds == "backfill_v4_pdf" and len(segs) >= 2:
                        _v4pdf_keys.add((ticker, period, quarter))
                logger.info(
                    f"[CANONICAL] segments dual-write: "
                    f"written={canonical_total} errors={canonical_errors}"
                )

                # ── backfill_v4_pdf 採用済みキーの xbrl:Other only 行を削除 ──
                # canonical_segments に xbrl source で segment_name = 'Other'/'other' のみの
                # 行が残存している場合、backfill_v4_pdf の方が信頼性が高いため削除する。
                if _v4pdf_keys:
                    _rest = canonical_config.get("rest_url", "")
                    _key = canonical_config.get("anon_key") or canonical_config.get("service_role_key", "")
                    _ch = {
                        "apikey": _key,
                        "Authorization": f"Bearer {_key}",
                        "Content-Type": "application/json",
                    }
                    _other_names = ("Other", "other", "その他", "others")
                    for (t, p, q) in _v4pdf_keys:
                        # xbrl / backfill_xbrl source の segment_name が Other 系のみか確認してから削除
                        # （仕様: source IN ('xbrl', 'backfill_xbrl') かつ Other only）
                        for _xbrl_src in ("xbrl", "backfill_xbrl"):
                            _r = requests.get(
                                f"{_rest}/canonical_segments",
                                headers=_ch,
                                params={
                                    "ticker": f"eq.{t}", "period": f"eq.{p}",
                                    "quarter": f"eq.{q}", "source": f"eq.{_xbrl_src}",
                                    "select": "segment_name",
                                },
                                timeout=15,
                            )
                            if not _r.ok:
                                continue
                            xbrl_rows = _r.json()
                            xbrl_names = {row["segment_name"] for row in xbrl_rows if row.get("segment_name")}
                            # Other 系のみの場合に削除
                            if xbrl_names and all(
                                n.strip().lower() in {x.lower() for x in _other_names}
                                for n in xbrl_names
                            ):
                                _dr = requests.delete(
                                    f"{_rest}/canonical_segments",
                                    headers={**_ch, "Prefer": "return=minimal"},
                                    params={
                                        "ticker": f"eq.{t}", "period": f"eq.{p}",
                                        "quarter": f"eq.{q}", "source": f"eq.{_xbrl_src}",
                                    },
                                    timeout=15,
                                )
                                if _dr.ok:
                                    logger.info(
                                        "[CANONICAL] deleted %s:Other-only rows for "
                                        "backfill_v4_pdf ticker=%s period=%s quarter=%s",
                                        _xbrl_src, t, p, q,
                                    )
                                else:
                                    logger.warning(
                                        "[CANONICAL] xbrl delete failed src=%s ticker=%s p=%s q=%s: %s",
                                        _xbrl_src, t, p, q, _dr.text[:200],
                                    )
            else:
                logger.warning(
                    "[CANONICAL] segments dual-write skipped: no write config"
                )
        except Exception as _cw_err:
            logger.warning(
                f"[CANONICAL] segments dual-write failed "
                f"(best-effort, legacy unaffected): {_cw_err}"
            )

    return stats


# ============================================================
# メイン処理
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="segment sync: XBRL + SQLite(excel_legacy) -> Supabase segment_canonical",
    )
    parser.add_argument("--apply", action="store_true", help="Supabase に書き込み")
    parser.add_argument("--dry-run", action="store_true", help="書き込みなし")
    parser.add_argument("--source-dir", default="data/docs", help="XBRL ZIP ディレクトリ")
    parser.add_argument("--xbrl-only", action="store_true",
                        help="XBRL のみ sync (SQLite excel_legacy を除外)")
    # 後方互換: --include-sqlite は受け付けるが無視 (デフォルトで含まれるため)
    parser.add_argument("--include-sqlite", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--db", default="decision_db.db",
                        help="SQLite DB パス")
    return parser


def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    
    dry_run = not opts.apply or opts.dry_run
    mode = "DRY-RUN" if dry_run else "APPLY"
    include_sqlite = not opts.xbrl_only
    sync_mode = "XBRL + SQLite" if include_sqlite else "XBRL ONLY"
    
    # Supabase 接続
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, "r", encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")
    rest_url = f"{supabase_url}/rest/v1"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }
    
    # テーブル存在チェック (apply モードのみ)
    if not dry_run:
        import requests as _req
        for tbl in ["segment_raw", "segment_canonical"]:
            r = _req.get(
                f"{rest_url}/{tbl}?select=*&limit=0",
                headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
                timeout=15,
            )
            if r.status_code != 200:
                logger.error(
                    f"[SYNC] テーブル '{tbl}' が存在しません。\n"
                    f"  Supabase SQL Editor で docs/segment_ddl.sql を実行してください。"
                )
                return 1
        logger.info("[SYNC] テーブル存在確認 OK")

    # ============================
    # Guard: --xbrl-only 時に SQLite valid rows があれば警告
    # ============================
    db_path = os.path.join(_PROJECT_ROOT, opts.db)
    if opts.xbrl_only:
        valid_count = count_sqlite_valid_rows(db_path)
        if valid_count > 0:
            msg = (
                f"[GUARD] SQLite に有効セグメント行が {valid_count:,} 件ありますが "
                f"--xbrl-only のため未同期です。\n"
                f"  excel_legacy 由来セグメントが COMPANYVIEW に表示されません。\n"
                f"  通常運用では --xbrl-only を外して実行してください。"
            )
            logger.warning(msg)
            if not dry_run:
                print()
                print("!" * 60)
                print(f"  WARNING: {valid_count:,} excel_legacy rows NOT synced")
                print("  Remove --xbrl-only for standard operation")
                print("!" * 60)
                print()

    logger.info(f"[SYNC] mode={mode} sync={sync_mode}")
    
    # ZIP 収集
    source_dir = os.path.join(_PROJECT_ROOT, opts.source_dir)
    zip_files = sorted(glob.glob(os.path.join(source_dir, "*.zip")))
    
    logger.info(f"[SYNC] source={source_dir} zips={len(zip_files)}")
    
    stats = {
        "sync_mode": sync_mode,
        "zips_total": len(zip_files),
        "zips_with_segments": 0,
        "xbrl_raw_total": 0,
        "xbrl_raw_upserted": 0,
        "xbrl_canonical_total": 0,
        "xbrl_canonical_upserted": 0,
        "xbrl_skipped_special": 0,
        "xbrl_skipped_no_data": 0,
        "xbrl_skipped_quarter": 0,
        "xbrl_skipped_fy_end_unresolved": 0,
        "xbrl_skipped_disc_no_unresolved": 0,
        "xbrl_errors": 0,
        # Phase 2-A: xbrl → canonical_segments (EAV) dual-write
        "xbrl_cs_batches": 0,
        "xbrl_cs_rows": 0,
        "xbrl_cs_written": 0,
        "xbrl_cs_errors": 0,
    }
    # JPマッチング統計
    match_stats: dict = {}
    # ticker 単位の日本語候補キャッシュ {ticker: list[str]}
    jp_cache: dict[str, list[str]] = {}

    # canonical 通過済み xbrl 行を蓄積 (dual-write 用)
    _xbrl_canonical_rows: list = []
    
    for zpath in zip_files:
        basename = os.path.basename(zpath)
        
        # disc_no をファイルパスから推測して jquants から period (FY End) を取得
        disc_no = None
        fy_end = None
        from pathlib import Path
        parts = Path(zpath).parts
        for p in parts:
            if p.isdigit() and len(p) >= 10:
                disc_no = p
                break
        
        if disc_no:
            try:
                jquants_conn = sqlite3.connect(os.path.join(_PROJECT_ROOT, "data", "jquants.db"))
                c = jquants_conn.cursor()
                c.execute('''
                    SELECT current_fiscal_year_end_date 
                    FROM jquants_financials_normalized 
                    WHERE raw_json LIKE ?
                    LIMIT 1
                ''', (f'%"{disc_no}"%',))
                row_fy = c.fetchone()
                if row_fy:
                    fy_end = row_fy[0]
                jquants_conn.close()
                logger.info(f"  [DRY-RUN] disc_no={disc_no}, fy_end={fy_end}")
            except Exception as e:
                logger.warning(f"Error fetching fy_end for disc_no {disc_no}: {e}")
        else:
            if not dry_run:
                stats["xbrl_skipped_disc_no_unresolved"] += 1
                logger.warning(f"  [SKIP] disc_no unresolved from {zpath}")
                continue
            logger.warning(f"  [DRY-RUN] could not extract disc_no from {zpath}")
            
        if disc_no and not fy_end:
            if not dry_run:
                stats["xbrl_skipped_fy_end_unresolved"] += 1
                logger.warning(f"  [SKIP] fy_end unresolved for disc_no {disc_no}")
                continue
            logger.warning(f"  [DRY-RUN] fallback for unresolved fy_end (disc_no: {disc_no})")

        rows = extract_segments_from_xbrl_zip(zpath, period=fy_end)
        if not rows:
            continue
        
        stats["zips_with_segments"] += 1
        ticker = rows[0].normalized_ticker if rows else "?"
        periods_found = set(r.period for r in rows)
        if dry_run:
            logger.info(f"  [DRY-RUN] Extracted periods for {ticker}: {periods_found}")
        period = max((r.period for r in rows), default="") if rows else ""  # prior rows が先頭でも current period を使う

        # JP 候補取得 — 日英統合なし方針により無効化（2026-05以降）
        # get_jp_segment_names() は互換性のため残すが呼び出さない。
        # 英語セグメントは英語名のまま segment_key を生成する。
        jp_names: list[str] = []  # 常に空 → jp マッチングは _upsert_segment_canonical で行われない

        for row in rows:
            stats["xbrl_raw_total"] += 1

            # raw upsert
            result = _upsert_segment_raw(row, rest_url, headers, dry_run)
            if result == "upserted":
                stats["xbrl_raw_upserted"] += 1
            elif result == "error":
                stats["xbrl_errors"] += 1

            # canonical 判定
            ok, reason = _is_canonical_candidate(row)
            if not ok:
                if "quarter" in reason:
                    stats["xbrl_skipped_quarter"] += 1
                elif "special" in reason:
                    stats["xbrl_skipped_special"] += 1
                else:
                    stats["xbrl_skipped_no_data"] += 1
                continue

            stats["xbrl_canonical_total"] += 1
            result = _upsert_segment_canonical(
                row, rest_url, headers, dry_run,
                jp_segment_names=jp_names,
                match_stats=match_stats,
            )
            if result == "upserted":
                stats["xbrl_canonical_upserted"] += 1
            elif result == "error":
                stats["xbrl_errors"] += 1

            # dual-write 用に蓄積
            _xbrl_canonical_rows.append(row)
        
        seg_names = [r.normalized_segment_name or r.raw_segment_name for r in rows 
                     if r.special_row_type == "ordinary_segment"]
        logger.info(f"  [{mode}] {ticker} {basename[:20]}: {len(rows)} raw, segments: {seg_names}")

    # ── Phase 2-A: xbrl → canonical_segments (EAV) dual-write ──
    if not dry_run and _xbrl_canonical_rows:
        try:
            from lib.pipeline.canonical_writer import write_segments_canonical
            from lib.pipeline.db import load_env, get_supabase_write_config
            load_env()
            canonical_config = get_supabase_write_config()
            if canonical_config:
                # ticker/period/quarter ごとにバッチ集約
                xbrl_batches: dict[tuple, list[dict]] = {}
                for row in _xbrl_canonical_rows:
                    key = (row.normalized_ticker, row.period, row.quarter)
                    if key not in xbrl_batches:
                        xbrl_batches[key] = []
                    xbrl_batches[key].append({
                        "segment_name": row.normalized_segment_name,
                        "sales": row.sales,
                        "profit": row.profit,
                    })

                stats["xbrl_cs_batches"] = len(xbrl_batches)
                stats["xbrl_cs_rows"] = len(_xbrl_canonical_rows)

                for (t, p, q), segs in xbrl_batches.items():
                    cw_result = write_segments_canonical(
                        ticker=t, period=p, quarter=q,
                        segments=segs, source="xbrl", config=canonical_config,
                    )
                    stats["xbrl_cs_written"] += cw_result["written"]
                    stats["xbrl_cs_errors"] += cw_result["errors"]

                logger.info(
                    f"[CANONICAL] xbrl dual-write: "
                    f"batches={stats['xbrl_cs_batches']} "
                    f"rows={stats['xbrl_cs_rows']} "
                    f"written={stats['xbrl_cs_written']} "
                    f"errors={stats['xbrl_cs_errors']}"
                )
            else:
                logger.warning("[CANONICAL] xbrl dual-write skipped: no write config")
        except Exception as _cw_err:
            logger.warning(
                f"[CANONICAL] xbrl dual-write failed (best-effort): {_cw_err}"
            )
    
    # SQLite 連携 (デフォルトで有効)
    if include_sqlite:
        logger.info(f"[SYNC] SQLite sync: {db_path}")
        sq_stats = sync_sqlite_segments(db_path, rest_url, headers, dry_run)
        stats.update(sq_stats)

        # ── Supabase aggregate orphan クリーンアップ ──
        # Supabase 側を直接検索して、ローカル側から削除済みの集計行を削除する。
        # excel_legacy / backfill_v4_pdf 等 あらゆる source に対応。
        try:
            agg_deleted_total = cleanup_supabase_aggregate_orphans(
                rest_url, headers, dry_run=dry_run,
            )
            stats["supabase_aggregate_deleted"] = agg_deleted_total
            if agg_deleted_total > 0 or dry_run:
                logger.info(
                    "[sync_segments] supabase aggregate orphan cleanup total deleted=%d",
                    agg_deleted_total,
                )
        except Exception as _e:
            logger.warning(
                "[sync_segments] supabase aggregate orphan cleanup failed (best-effort): %s", _e
            )

    # サマリ
    print()
    print("=" * 60)
    print(f"  Segment Sync - {mode} ({sync_mode})")
    print("=" * 60)

    # XBRL
    print("  [XBRL]")
    print(f"    zips_total               : {stats['zips_total']}")
    print(f"    zips_with_segments       : {stats['zips_with_segments']}")
    print(f"    raw_upserted             : {stats['xbrl_raw_upserted']}")
    print(f"    canonical_upserted       : {stats['xbrl_canonical_upserted']}")
    if stats["xbrl_cs_batches"] > 0:
        print(f"    cs_dual_write_batches    : {stats['xbrl_cs_batches']}")
        print(f"    cs_dual_write_written    : {stats['xbrl_cs_written']}")
        if stats["xbrl_cs_errors"]:
            print(f"    cs_dual_write_errors     : {stats['xbrl_cs_errors']}")
    if stats["xbrl_skipped_fy_end_unresolved"] > 0:
        print(f"    skipped_fy_unresolved    : {stats['xbrl_skipped_fy_end_unresolved']}")
    if stats["xbrl_skipped_disc_no_unresolved"] > 0:
        print(f"    skipped_disc_unresolved  : {stats['xbrl_skipped_disc_no_unresolved']}")
    if stats["xbrl_errors"]:
        print(f"    errors                   : {stats['xbrl_errors']}")

    # SQLite
    if include_sqlite:
        print("  [SQLite excel_legacy]")
        print(f"    total_rows               : {stats.get('sqlite_total', 0)}")
        print(f"    valid                    : {stats.get('sqlite_valid', 0)}")
        print(f"    upserted                 : {stats.get('sqlite_upserted', 0)}")
        print(f"    errors                   : {stats.get('sqlite_errors', 0)}")
        print(f"    skip_header              : {stats.get('sqlite_skip_header', 0)}")
        print(f"    skip_zero                : {stats.get('sqlite_skip_zero', 0)}")
        print(f"    skip_unknown             : {stats.get('sqlite_skip_unknown', 0)}")
        print(f"    skip_quarter             : {stats.get('sqlite_skip_quarter', 0)}")
        print(f"    skip_ratio               : {stats.get('sqlite_skip_ratio', 0)}")
        print(f"    skip_empty               : {stats.get('sqlite_skip_empty', 0)}")
    else:
        print("  [SQLite excel_legacy]")
        print("    ** NOT SYNCED (--xbrl-only) **")

    # JP マッチング統計
    if match_stats:
        print("  [JP Segment Matching]")
        hit     = match_stats.get("jp_candidate_hit", 0)
        empty   = match_stats.get("jp_candidate_empty", 0)
        success = match_stats.get("jp_match_success", 0)
        low     = match_stats.get("jp_match_low_score", 0)
        fall    = match_stats.get("jp_match_fallback", 0)
        total_en = success + low + fall
        print(f"    jp_candidate_hit         : {hit}")
        print(f"    jp_candidate_empty       : {empty}")
        print(f"    jp_match_success         : {success}")
        print(f"    jp_match_low_score       : {low}")
        print(f"    jp_match_fallback        : {fall}")
        if total_en > 0:
            jp_match_rate = success / total_en * 100
            print(f"    jp_match_rate            : {jp_match_rate:.1f}%")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
