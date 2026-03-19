"""lib/pipeline/canonical_writer.py — canonical テーブルへの書き込み (Phase 2-A)

wide → long 展開 + recency_key 自動生成 + Supabase upsert。
既存 financials / segment_canonical への書き込みは壊さない。
canonical write 失敗時は warning を出して続行 (best-effort)。
"""
from __future__ import annotations

import hashlib
import logging
import unicodedata
import re
from datetime import datetime, timezone, timedelta

from .source_priority import get_priority
from .recency import make_recency_key
from .db import supabase_upsert

logger = logging.getLogger("pipeline.canonical")
JST = timezone(timedelta(hours=9))

# ================================================================
# source_row_key 生成
# ================================================================

def _make_financials_row_key(
    ticker: str, period: str, quarter: str,
    metric: str, source: str, filing_id: str | None,
) -> str:
    """canonical_financials 用の deterministic source_row_key。

    format: cf|ticker|period|quarter|metric|source|filing_id_or_empty
    """
    fid = filing_id or ""
    raw = f"cf|{ticker}|{period}|{quarter}|{metric}|{source}|{fid}"
    return raw


def _make_segments_row_key(
    ticker: str, period: str, quarter: str,
    segment_key: str, metric: str, source: str, filing_id: str | None,
) -> str:
    """canonical_segments 用の deterministic source_row_key。"""
    fid = filing_id or ""
    raw = f"cs|{ticker}|{period}|{quarter}|{segment_key}|{metric}|{source}|{fid}"
    return raw


# ================================================================
# segment_name 正規化 — 指標語除去 + segment_key 生成
# ================================================================

# 末尾指標語を除去するパターン (長い順にマッチ)
_METRIC_SUFFIXES = [
    "セグメント利益(円)",
    "セグメント利益（円）",
    "営業利益(円)",
    "営業利益（円）",
    "売上高(円)",
    "売上高（円）",
    "売上(円)",
    "売上（円）",
    "利益(円)",
    "利益（円）",
    "セグメント利益",
    "営業利益",
    "売上高",
    "売上",
    "利益",
]


def strip_metric_suffix(name: str) -> str:
    """segment_name から末尾の指標語を除去して事業名だけ返す。

    例:
        環境システム売上(円) -> 環境システム
        管工機材利益(円)     -> 管工機材
        GlampingTourism売上(円) -> GlampingTourism
        プラント事業         -> プラント事業  (変更なし)
    """
    s = unicodedata.normalize("NFKC", name).strip()
    for suffix in _METRIC_SUFFIXES:
        if s.endswith(suffix):
            candidate = s[: -len(suffix)].strip()
            # 除去後が空になる場合は除去しない (e.g. "売上" だけの行)
            if candidate:
                return candidate
            break
    return s


def normalize_segment_name(name: str) -> str:
    """segment_name を事業名ベースに正規化する。

    1. NFKC 正規化
    2. 末尾指標語除去
    3. strip + 連続空白圧縮
    """
    s = strip_metric_suffix(name)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_segment_key(name: str) -> str:
    """segment_name -> segment_key に正規化。

    - 事業名ベースに正規化 (指標語除去)
    - NFKC
    - strip
    - 連続空白 -> 1スペース
    - lower
    """
    s = normalize_segment_name(name)
    s = s.lower()
    return s


# ================================================================
# Financials canonical write
# ================================================================

def write_financials_canonical(
    *,
    ticker: str,
    period: str,
    quarter: str,
    metrics_dict: dict[str, int | float | None],
    source: str,
    filing_id: str | None = None,
    disclosure_datetime: str | None = None,
    correction_flag: bool = False,
    unit: str = "JPY",
    config: dict,
) -> dict:
    """financials wide dict → canonical_financials (long) に upsert。

    Args:
        metrics_dict: {"sales": 123, "gross_profit": 45, ...} (値は int | None)
        source: "tdnet" | "jquants" etc.
        config: supabase_upsert 用の config dict

    Returns:
        {"written": int, "skipped": int, "errors": int}
    """
    now_iso = datetime.now(JST).isoformat()
    priority = get_priority(source)
    recency = make_recency_key(
        source,
        correction_flag=correction_flag,
        disclosure_datetime=disclosure_datetime,
        updated_at=now_iso,
    )

    rows: list[dict] = []
    skipped = 0

    for metric, value in metrics_dict.items():
        if value is None:
            skipped += 1
            continue
        row_key = _make_financials_row_key(ticker, period, quarter, metric, source, filing_id)
        rows.append({
            "ticker": ticker,
            "period": period,
            "quarter": quarter,
            "metric": metric,
            "value": value,
            "unit": unit,
            "source": source,
            "source_priority": priority,
            "filing_id": filing_id,
            "source_row_key": row_key,
            "disclosure_datetime": disclosure_datetime,
            "correction_flag": correction_flag,
            "recency_key": recency,
            "updated_at": now_iso,
        })

    if not rows:
        return {"written": 0, "skipped": skipped, "errors": 0}

    try:
        result = supabase_upsert(
            "canonical_financials",
            rows,
            on_conflict="source_row_key",
            config=config,
        )
        if result.get("ok"):
            logger.info(
                f"[canonical] financials written: ticker={ticker} period={period} "
                f"quarter={quarter} rows={len(rows)}"
            )
            return {"written": len(rows), "skipped": skipped, "errors": 0}
        else:
            logger.warning(
                f"[canonical] financials upsert failed (best-effort): "
                f"ticker={ticker} period={period} quarter={quarter} "
                f"error={result.get('error', 'unknown')}"
            )
            return {"written": 0, "skipped": skipped, "errors": len(rows)}
    except Exception as e:
        logger.warning(
            f"[canonical] financials write EXCEPTION (best-effort): "
            f"ticker={ticker} period={period} quarter={quarter} error={e}"
        )
        return {"written": 0, "skipped": skipped, "errors": len(rows)}


# ================================================================
# Segments canonical write
# ================================================================

def write_segments_canonical(
    *,
    ticker: str,
    period: str,
    quarter: str,
    segments: list[dict],
    source: str,
    filing_id: str | None = None,
    disclosure_datetime: str | None = None,
    correction_flag: bool = False,
    unit: str = "JPY",
    config: dict,
) -> dict:
    """segment list → canonical_segments (long) に upsert。

    Args:
        segments: [{"segment_name": "...", "sales": 123, "profit": 45}, ...]
        source: "xbrl" | "html" | "pdf" etc.

    Returns:
        {"written": int, "skipped": int, "errors": int}
    """
    now_iso = datetime.now(JST).isoformat()
    priority = get_priority(source)
    recency = make_recency_key(
        source,
        correction_flag=correction_flag,
        disclosure_datetime=disclosure_datetime,
        updated_at=now_iso,
    )

    rows: list[dict] = []
    skipped = 0

    for seg in segments:
        raw_name = seg.get("segment_name", "")
        if not raw_name:
            skipped += 1
            continue

        seg_name = normalize_segment_name(raw_name)
        seg_key = normalize_segment_key(raw_name)

        # metric ごとに 1行
        for metric in ("sales", "profit"):
            value = seg.get(metric)
            if value is None:
                skipped += 1
                continue
            row_key = _make_segments_row_key(
                ticker, period, quarter, seg_key, metric, source, filing_id
            )
            rows.append({
                "ticker": ticker,
                "period": period,
                "quarter": quarter,
                "segment_name": seg_name,
                "segment_key": seg_key,
                "metric": metric,
                "value": value,
                "unit": unit,
                "source": source,
                "source_system": seg.get("source_system", "tdnet"),
                "source_priority": priority,
                "segment_type": seg.get("segment_type", "ordinary"),
                "derivation_method": seg.get("derivation_method", ""),
                "filing_id": filing_id,
                "source_row_key": row_key,
                "disclosure_datetime": disclosure_datetime,
                "correction_flag": correction_flag,
                "recency_key": recency,
                "updated_at": now_iso,
            })

    if not rows:
        return {"written": 0, "skipped": skipped, "errors": 0}

    # --- source_row_key 重複検知 + dedupe (後勝ち) ---
    original_count = len(rows)
    deduped: dict[str, dict] = {}
    dup_keys: dict[str, int] = {}
    for row in rows:
        key = row["source_row_key"]
        if key in deduped:
            dup_keys[key] = dup_keys.get(key, 1) + 1
        deduped[key] = row
    rows = list(deduped.values())

    if dup_keys:
        extra_rows = original_count - len(rows)
        top_dups = sorted(dup_keys.items(), key=lambda x: x[1], reverse=True)[:5]
        top_dups_str = ", ".join(f"{k} (x{v})" for k, v in top_dups)
        logger.warning(
            f"[canonical] segments dedupe: ticker={ticker} period={period} "
            f"quarter={quarter} dup_keys={len(dup_keys)} extra_rows={extra_rows} "
            f"top_dups=[{top_dups_str}]"
        )

    try:
        result = supabase_upsert(
            "canonical_segments",
            rows,
            on_conflict="source_row_key",
            config=config,
        )
        if result.get("ok"):
            logger.info(
                f"[canonical] segments written: ticker={ticker} period={period} "
                f"quarter={quarter} rows={len(rows)}"
            )
            return {"written": len(rows), "skipped": skipped, "errors": 0}
        else:
            logger.warning(
                f"[canonical] segments upsert failed (best-effort): "
                f"ticker={ticker} period={period} quarter={quarter} "
                f"error={result.get('error', 'unknown')}"
            )
            return {"written": 0, "skipped": skipped, "errors": len(rows)}
    except Exception as e:
        logger.warning(
            f"[canonical] segments write EXCEPTION (best-effort): "
            f"ticker={ticker} period={period} quarter={quarter} error={e}"
        )
        return {"written": 0, "skipped": skipped, "errors": len(rows)}
