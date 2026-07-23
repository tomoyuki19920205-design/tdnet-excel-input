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
from lib.pipeline.canonical_writer import (
    expand_segments_rows,
    has_segment_display_aliases,
    normalize_segment_display_key,
)
from lib.pipeline.source_priority import get_priority

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

    seg_key = normalize_segment_display_key(row.normalized_ticker, seg_name)
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
    sales = row.get("segment_sales")
    profit = row.get("segment_profit")
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


def _canonical_readback_matches(config: dict, *, ticker: str, period: str, quarter: str, source: str, segments: list[dict]) -> bool:
    """Confirm the canonical long rows contain each just-written sales/profit pair."""
    from lib.pipeline.db import supabase_select
    rows = supabase_select(
        "canonical_segments",
        params={
            "select": "segment_name,metric,value",
            "ticker": f"eq.{ticker}", "period": f"eq.{period}",
            "quarter": f"eq.{quarter}", "source": f"eq.{source}",
        },
        config=config,
    )
    actual = {(r.get("segment_name"), r.get("metric"), r.get("value")) for r in rows}
    expected = set()
    for segment in segments:
        if segment.get("sales") is not None:
            expected.add((segment["segment_name"], "sales", segment["sales"]))
        if segment.get("profit") is not None:
            expected.add((segment["segment_name"], "profit", segment["profit"]))
    return expected.issubset(actual)


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


def _empty_layered_stats(segment_ids: list[int]) -> dict:
    return {
        "sqlite_total": 0, "sqlite_valid": 0, "sqlite_upserted": 0,
        "sqlite_errors": 0, "requested_segment_ids": sorted(set(segment_ids)),
        "synced_segment_ids": [], "sync_error": "", "payloads": [],
        "wide_inserted": 0, "wide_updated": 0, "wide_unchanged": 0,
        "wide_skipped_alias_equivalent_existing": 0, "wide_conflict": 0,
        "eav_inserted": 0, "eav_updated": 0, "eav_unchanged": 0,
        "eav_skipped_alias_equivalent_existing": 0, "eav_conflict": 0,
        "row_results": [], "conflicts": [],
    }


def _read_segment_id_rows(db_path: str, segment_ids: list[int]) -> tuple[list[dict], str]:
    requested_ids = sorted({int(value) for value in segment_ids})
    if not os.path.isfile(db_path):
        return [], "segment_sync_db_missing"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in requested_ids)
        segment_columns = {
            str(column["name"]) for column in conn.execute(
                "PRAGMA table_info(segment_financials)"
            ).fetchall()
        }
        optional_segment_columns = ", ".join(
            column if column in segment_columns else f"NULL AS {column}"
            for column in ("tdnet_doc_id", "disclosure_date")
        )
        rows = [dict(row) for row in conn.execute(
            "SELECT id, company_code, fiscal_year_end, quarter, segment_name, "
            "segment_sales, segment_profit, data_source, " + optional_segment_columns + " "
            f"FROM segment_financials WHERE id IN ({placeholders})",
            requested_ids,
        ).fetchall()]
        lineage_present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='filing_segment_lineage'"
        ).fetchone() is not None
        if lineage_present:
            lineage_columns = {
                str(column["name"]) for column in conn.execute(
                    "PRAGMA table_info(filing_segment_lineage)"
                ).fetchall()
            }
            lineage_tdnet = (
                "tdnet_doc_id" if "tdnet_doc_id" in lineage_columns
                else "NULL AS tdnet_doc_id"
            )
            lineage_rows = conn.execute(
                "SELECT canonical_segment_financial_id, filing_id, relation_role, "
                + lineage_tdnet + " "
                "FROM filing_segment_lineage "
                f"WHERE canonical_segment_financial_id IN ({placeholders}) "
                "AND relation_role IN "
                "('CANONICAL_SOURCE','EQUIVALENT_REFERENCE','NONCANONICAL_OBSERVATION') "
                "ORDER BY canonical_segment_financial_id, filing_id",
                requested_ids,
            ).fetchall()
            observation_dates: dict[tuple[int, str], str] = {}
            observation_business: dict[tuple[int, str], tuple] = {}
            observations_present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='filing_segment_observations'"
            ).fetchone() is not None
            if observations_present:
                for observation in conn.execute(
                    "SELECT canonical_segment_financial_id, filing_id, disclosure_date, "
                    "company_code, fiscal_year_end, quarter, segment_name, "
                    "observed_sales, observed_profit, row_semantic_digest "
                    "FROM filing_segment_observations "
                    f"WHERE canonical_segment_financial_id IN ({placeholders})",
                    requested_ids,
                ).fetchall():
                    observation_dates[
                        (int(observation["canonical_segment_financial_id"]),
                         str(observation["filing_id"]))
                    ] = str(observation["disclosure_date"] or "").strip()
                    observation_business[
                        (int(observation["canonical_segment_financial_id"]),
                         str(observation["filing_id"]))
                    ] = (
                        str(observation["company_code"]),
                        str(observation["fiscal_year_end"]),
                        str(observation["quarter"]),
                        str(observation["segment_name"]),
                        observation["observed_sales"],
                        observation["observed_profit"],
                        str(observation["row_semantic_digest"]),
                    )
            lineage_by_id: dict[int, dict[str, set[str]]] = {}
            equivalent_provenance: dict[int, dict[str, tuple[str, str]]] = {}
            for lineage in lineage_rows:
                segment_id = int(lineage["canonical_segment_financial_id"])
                role_map = lineage_by_id.setdefault(segment_id, {})
                filing_id = str(lineage["filing_id"] or "").strip()
                role = str(lineage["relation_role"])
                role_map.setdefault(role, set()).add(filing_id)
                if role == "EQUIVALENT_REFERENCE" and filing_id:
                    equivalent_provenance.setdefault(segment_id, {})[filing_id] = (
                        str(lineage["tdnet_doc_id"] or "").strip(),
                        observation_dates.get((segment_id, filing_id), ""),
                    )
            for row in rows:
                segment_id = int(row["id"])
                roles = lineage_by_id.get(segment_id, {})
                canonical = sorted(value for value in roles.get("CANONICAL_SOURCE", set()) if value)
                equivalent = sorted(value for value in roles.get("EQUIVALENT_REFERENCE", set()) if value)
                noncanonical = sorted(value for value in roles.get("NONCANONICAL_OBSERVATION", set()) if value)
                row["equivalent_reference_filing_ids"] = equivalent
                if len(canonical) == 1:
                    row["canonical_filing_id"] = canonical[0]
                    row["canonical_route"] = "DIRECT_CANONICAL_REFERENCE"
                elif not canonical and len(equivalent) == 1:
                    row["canonical_filing_id"] = equivalent[0]
                    row["canonical_route"] = "ALIAS_CANONICAL_REFERENCE"
                elif not canonical and len(equivalent) > 1:
                    business_rows = [
                        observation_business.get((segment_id, filing_id))
                        for filing_id in equivalent
                    ]
                    expected_business = (
                        str(row["company_code"]), str(row["fiscal_year_end"]),
                        str(row["quarter"]), str(row["segment_name"]),
                        row["segment_sales"], row["segment_profit"],
                    )
                    business_equivalent = (
                        all(value is not None for value in business_rows)
                        and len({value for value in business_rows if value is not None}) == 1
                        and business_rows[0][:-1] == expected_business
                    )
                    baseline_tdnet = str(row.get("tdnet_doc_id") or "").strip()
                    baseline_date = str(row.get("disclosure_date") or "").strip()
                    baseline_matches = [
                        filing_id for filing_id in equivalent
                        if baseline_tdnet and baseline_date
                        and equivalent_provenance.get(segment_id, {}).get(filing_id)
                        == (baseline_tdnet, baseline_date)
                    ]
                    if not business_equivalent:
                        row["canonical_filing_id"] = None
                        row["canonical_route"] = "IDENTITY_UNRESOLVED"
                        row["route_evidence_class"] = "MULTI_REFERENCE_BUSINESS_CONFLICT"
                    elif len(baseline_matches) == 1:
                        row["canonical_filing_id"] = baseline_matches[0]
                        row["canonical_route"] = "UNIQUE_BASELINE_PROVENANCE"
                        row["route_evidence_class"] = "UNIQUE_BASELINE_PROVENANCE"
                    else:
                        row["canonical_filing_id"] = None
                        row["canonical_route"] = "MULTI_EQUIVALENT_REFERENCE"
                        row["route_evidence_class"] = (
                            "MULTI_EQUIVALENT_REFERENCE_SAME_DESTINATION"
                        )
                elif not canonical and not equivalent and noncanonical:
                    row["canonical_filing_id"] = None
                    row["canonical_route"] = "OBSERVATION_ONLY_NO_CANONICAL_MUTATION"
                else:
                    row["canonical_filing_id"] = None
                    row["canonical_route"] = "IDENTITY_UNRESOLVED"
                row["lineage_contract_present"] = True
        else:
            for row in rows:
                row["canonical_filing_id"] = None
                row["canonical_route"] = "LEGACY_LINEAGE_UNAVAILABLE"
                row["lineage_contract_present"] = False
    finally:
        conn.close()
    if {int(row["id"]) for row in rows} != set(requested_ids):
        return rows, "segment_sync_requested_ids_missing"
    return rows, ""


def _get_layer_rows(rest_url: str, headers: dict, table: str, key: tuple[str, str, str]) -> list[dict]:
    ticker, period, quarter = key
    response = requests.get(
        f"{rest_url}/{table}", headers=headers,
        params={
            "select": "*", "ticker": f"eq.{ticker}",
            "period": f"eq.{period}", "quarter": f"eq.{quarter}",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"segment_alias_plan_read_failed:{table}:{response.status_code}")
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"segment_alias_plan_read_invalid:{table}")
    return payload


def _null_safe_value_equal(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return float(left) == float(right)


def _existing_eav_alias_key(ticker: str, row: dict) -> str:
    source_name = str(row.get("segment_name") or "").strip()
    if source_name:
        return normalize_segment_display_key(ticker, source_name)
    return str(row.get("segment_key") or "")


def plan_alias_aware_segment_ids(
    db_path: str, segment_ids: list[int], rest_url: str, headers: dict,
    *, live_read: bool = True,
) -> dict:
    """Plan alias-aware wide/EAV actions without issuing any write request."""
    requested_ids = sorted({int(value) for value in segment_ids})
    plan = _empty_layered_stats(requested_ids)
    rows, error = _read_segment_id_rows(db_path, requested_ids)
    plan["sqlite_total"] = len(rows)
    if error:
        plan["sync_error"] = error
        return plan

    lineage_contract_present = bool(rows and rows[0].get("lineage_contract_present"))
    if lineage_contract_present:
        invalid_identity = [
            row for row in rows
            if row.get("canonical_route") not in {
                "DIRECT_CANONICAL_REFERENCE", "ALIAS_CANONICAL_REFERENCE",
                "MULTI_EQUIVALENT_REFERENCE", "UNIQUE_BASELINE_PROVENANCE",
                "OBSERVATION_ONLY_NO_CANONICAL_MUTATION",
            } or (
                row.get("canonical_route") not in {
                    "MULTI_EQUIVALENT_REFERENCE",
                    "OBSERVATION_ONLY_NO_CANONICAL_MUTATION",
                }
                and not str(row.get("canonical_filing_id") or "").strip()
            ) or (
                row.get("canonical_route") == "MULTI_EQUIVALENT_REFERENCE"
                and len(row.get("equivalent_reference_filing_ids") or []) < 2
            )
        ]
        if invalid_identity:
            plan["sync_error"] = "segment_sync_filing_identity_unresolved"
            plan["conflicts"] = [{
                "sqlite_row_id": int(row["id"]),
                "reason": str(
                    row.get("route_evidence_class")
                    or row.get("canonical_route")
                    or "IDENTITY_UNRESOLVED"
                ),
            } for row in invalid_identity]
            return plan

    observation_only_rows = [
        row for row in rows
        if row.get("canonical_route") == "OBSERVATION_ONLY_NO_CANONICAL_MUTATION"
    ]
    plan["observation_only_no_canonical_mutation"] = len(observation_only_rows)
    plan["observation_only_segment_ids"] = sorted(
        int(row["id"]) for row in observation_only_rows
    )
    valid_rows = [
        row for row in rows
        if not _classify_skip_reason(row)
        and row.get("canonical_route") != "OBSERVATION_ONLY_NO_CANONICAL_MUTATION"
    ]
    plan["sqlite_valid"] = len(valid_rows)
    plan["payloads"] = []
    groups = {
        (str(row["company_code"]), str(row["fiscal_year_end"]),
         _QUARTER_MAP.get(str(row["quarter"]), str(row["quarter"])))
        for row in valid_rows
    }
    wide_existing: dict[tuple[str, str, str], list[dict]] = {key: [] for key in groups}
    eav_existing: dict[tuple[str, str, str], list[dict]] = {key: [] for key in groups}
    if live_read:
        try:
            for key in sorted(groups):
                wide_existing[key] = _get_layer_rows(rest_url, headers, "segment_canonical", key)
                eav_existing[key] = _get_layer_rows(rest_url, headers, "canonical_segments", key)
        except RuntimeError as exc:
            plan["sync_error"] = str(exc)
            return plan

    for row in valid_rows:
        row_id = int(row["id"])
        ticker = str(row["company_code"])
        period = str(row["fiscal_year_end"])
        quarter = _QUARTER_MAP.get(str(row["quarter"]), str(row["quarter"]))
        source = str(row.get("data_source") or "excel_legacy")
        filing_id = str(row.get("canonical_filing_id") or "").strip() or None
        equivalent_reference_filing_ids = [
            value for value in list(row.get("equivalent_reference_filing_ids") or [])
            if value != filing_id
        ]
        reference_filing_ids = (
            ([filing_id] if filing_id else []) + equivalent_reference_filing_ids
            if row.get("route_evidence_class") == "UNIQUE_BASELINE_PROVENANCE"
            else list(row.get("equivalent_reference_filing_ids") or [])
            if row.get("canonical_route") == "MULTI_EQUIVALENT_REFERENCE"
            else [filing_id]
        )
        segment_name = str(row["segment_name"]).strip()
        segment_key = normalize_segment_display_key(ticker, segment_name)
        sales = int(row["segment_sales"]) if row["segment_sales"] is not None else None
        profit = int(row["segment_profit"]) if row["segment_profit"] is not None else None
        group = (ticker, period, quarter)
        wide_payload = {
            "ticker": ticker, "period": period, "quarter": quarter,
            "segment_name": segment_name, "segment_key": segment_key,
            "sales": sales, "profit": profit, "source": source,
            "updated_at": datetime.now(JST).isoformat(),
        }
        plan["payloads"].append(wide_payload)
        result = {
            "sqlite_row_id": row_id, "alias_key": segment_key,
            "wide_action": "", "wide_reason": "", "wide_existing": [],
            "eav_actions": [], "source_priority": get_priority(source),
            "filing_id": filing_id,
            "equivalent_reference_filing_ids": equivalent_reference_filing_ids,
            "evaluated_filing_ids": reference_filing_ids,
            "canonical_route": row.get("canonical_route"),
            "route_evidence_class": row.get("route_evidence_class"),
        }

        wide_matches = [
            existing for existing in wide_existing[group]
            if normalize_segment_display_key(ticker, str(existing.get("segment_name") or "")) == segment_key
        ]
        result["wide_existing"] = wide_matches
        if not wide_matches:
            result["wide_action"] = "wide_upsert"
            result["wide_reason"] = "alias_equivalent_existing_not_found"
            plan["wide_inserted"] += 1
        elif any(
            not _null_safe_value_equal(existing.get("sales"), sales)
            or not _null_safe_value_equal(existing.get("profit"), profit)
            for existing in wide_matches
        ):
            result["wide_action"] = "wide_conflict"
            result["wide_reason"] = "segment_wide_alias_value_conflict"
            plan["wide_conflict"] += 1
            plan["conflicts"].append({"sqlite_row_id": row_id, "reason": result["wide_reason"]})
        else:
            best_existing_priority = min(get_priority(str(existing.get("source") or "")) for existing in wide_matches)
            if best_existing_priority > get_priority(source):
                result["wide_action"] = "wide_conflict"
                result["wide_reason"] = "segment_wide_alias_priority_upgrade_requires_review"
                plan["wide_conflict"] += 1
                plan["conflicts"].append({"sqlite_row_id": row_id, "reason": result["wide_reason"]})
            elif any(str(existing.get("segment_name") or "") == segment_name for existing in wide_matches):
                result["wide_action"] = "wide_unchanged"
                result["wide_reason"] = "exact_existing_value_match"
                plan["wide_unchanged"] += 1
            else:
                result["wide_action"] = "wide_skipped_alias_equivalent_existing"
                result["wide_reason"] = "alias_equivalent_existing_value_match"
                plan["wide_skipped_alias_equivalent_existing"] += 1

        eav_payloads = []
        for reference_filing_id in reference_filing_ids:
            reference_payloads, _ = expand_segments_rows(
                ticker=ticker, period=period, quarter=quarter,
                segments=[{"segment_name": segment_name, "sales": sales, "profit": profit}],
                source=source, filing_id=reference_filing_id, unit="millions_jpy",
            )
            eav_payloads.extend(reference_payloads)
        for eav_payload in eav_payloads:
            logical_matches = [
                existing for existing in eav_existing[group]
                if _existing_eav_alias_key(ticker, existing) == segment_key
                and str(existing.get("metric") or "") == str(eav_payload["metric"])
            ]
            action = {
                "metric": eav_payload["metric"], "value": eav_payload["value"],
                "action": "", "reason": "", "payload": eav_payload,
                "existing": logical_matches,
            }
            value_matches = [
                existing for existing in logical_matches
                if _null_safe_value_equal(existing.get("value"), eav_payload["value"])
            ]
            unit_matches = [
                existing for existing in value_matches
                if str(existing.get("unit") or "") == str(eav_payload["unit"])
            ]
            if not logical_matches:
                action["action"] = "eav_upsert"
                action["reason"] = "alias_equivalent_existing_not_found"
                plan["eav_inserted"] += 1
            elif not value_matches:
                action["action"] = "eav_conflict"
                action["reason"] = "segment_eav_alias_value_conflict"
                plan["eav_conflict"] += 1
                plan["conflicts"].append({
                    "sqlite_row_id": row_id, "metric": eav_payload["metric"],
                    "reason": action["reason"],
                })
            elif lineage_contract_present and not unit_matches:
                action["action"] = "eav_conflict"
                action["reason"] = "segment_eav_unit_conflict"
                plan["eav_conflict"] += 1
                plan["conflicts"].append({
                    "sqlite_row_id": row_id, "metric": eav_payload["metric"],
                    "reason": action["reason"],
                })
            else:
                best_existing_priority = min(
                    int(existing.get("source_priority"))
                    if existing.get("source_priority") is not None
                    else get_priority(str(existing.get("source") or ""))
                    for existing in (unit_matches if lineage_contract_present else value_matches)
                )
                if best_existing_priority > get_priority(source):
                    action["action"] = "eav_conflict"
                    action["reason"] = "segment_eav_alias_priority_upgrade_requires_review"
                    plan["eav_conflict"] += 1
                    plan["conflicts"].append({
                        "sqlite_row_id": row_id, "metric": eav_payload["metric"],
                        "reason": action["reason"],
                    })
                else:
                    action["action"] = "eav_skipped_alias_equivalent_existing"
                    action["reason"] = "alias_equivalent_existing_value_match"
                    plan["eav_skipped_alias_equivalent_existing"] += 1
            result["eav_actions"].append(action)
        if row.get("canonical_route") == "MULTI_EQUIVALENT_REFERENCE" and any(
            action["action"] == "eav_upsert" for action in result["eav_actions"]
        ):
            result["eav_actions"] = [
                {**action, "action": "eav_conflict",
                 "reason": "segment_multi_reference_versioned_insert_required"}
                if action["action"] == "eav_upsert" else action
                for action in result["eav_actions"]
            ]
            inserted = sum(
                action["reason"] == "segment_multi_reference_versioned_insert_required"
                for action in result["eav_actions"]
            )
            plan["eav_inserted"] -= inserted
            plan["eav_conflict"] += inserted
            plan["conflicts"].append({
                "sqlite_row_id": row_id,
                "reason": "segment_multi_reference_versioned_insert_required",
            })
        plan["row_results"].append(result)

    if plan["conflicts"]:
        plan["sync_error"] = plan["conflicts"][0]["reason"]
    return plan


def _alias_plan_readback_matches(plan: dict, rest_url: str, headers: dict) -> bool:
    groups = {
        (payload["ticker"], payload["period"], payload["quarter"])
        for payload in plan["payloads"]
    }
    try:
        wide = {key: _get_layer_rows(rest_url, headers, "segment_canonical", key) for key in groups}
        eav = {key: _get_layer_rows(rest_url, headers, "canonical_segments", key) for key in groups}
    except RuntimeError:
        return False
    for row_result, payload in zip(plan["row_results"], plan["payloads"]):
        key = (payload["ticker"], payload["period"], payload["quarter"])
        alias_key = row_result["alias_key"]
        if not any(
            normalize_segment_display_key(payload["ticker"], str(existing.get("segment_name") or "")) == alias_key
            and _null_safe_value_equal(existing.get("sales"), payload["sales"])
            and _null_safe_value_equal(existing.get("profit"), payload["profit"])
            for existing in wide[key]
        ):
            return False
        for action in row_result["eav_actions"]:
            if not any(
                _existing_eav_alias_key(str(payload["ticker"]), existing) == alias_key
                and str(existing.get("metric") or "") == action["metric"]
                and _null_safe_value_equal(existing.get("value"), action["value"])
                for existing in eav[key]
            ):
                return False
    return True


def _execute_alias_aware_plan(plan: dict, rest_url: str, headers: dict, *, dry_run: bool) -> dict:
    if plan["sync_error"]:
        return plan
    if dry_run:
        plan["sqlite_upserted"] = plan["sqlite_valid"]
        plan["synced_segment_ids"] = sorted(row["sqlite_row_id"] for row in plan["row_results"])
        return plan

    plan["planned_wide_inserted"] = plan["wide_inserted"]
    plan["planned_eav_inserted"] = plan["eav_inserted"]
    plan["wide_inserted"] = 0
    plan["eav_inserted"] = 0
    failed_ids: set[int] = set()
    for row_result, payload in zip(plan["row_results"], plan["payloads"]):
        if row_result["wide_action"] == "wide_upsert":
            response = requests.post(
                f"{rest_url}/segment_canonical", json=payload,
                headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                timeout=30,
            )
            if response.status_code not in (200, 201):
                failed_ids.add(row_result["sqlite_row_id"])
            else:
                plan["wide_inserted"] += 1
                row_result["wide_action"] = "wide_inserted"

    eav_payloads = [
        action["payload"]
        for row in plan["row_results"] for action in row["eav_actions"]
        if action["action"] == "eav_upsert"
    ]
    if eav_payloads:
        response = requests.post(
            f"{rest_url}/canonical_segments",
            params={"on_conflict": "source_row_key"}, json=eav_payloads,
            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=30,
        )
        if response.status_code not in (200, 201):
            failed_ids.update(row["sqlite_row_id"] for row in plan["row_results"])
        else:
            plan["eav_inserted"] += len(eav_payloads)
            for row in plan["row_results"]:
                for action in row["eav_actions"]:
                    if action["action"] == "eav_upsert":
                        action["action"] = "eav_inserted"

    if failed_ids:
        plan["sqlite_errors"] = len(failed_ids)
        plan["sync_error"] = "segment_canonical_sync_failed_after_sqlite_commit"
        return plan
    if not _alias_plan_readback_matches(plan, rest_url, headers):
        plan["sync_error"] = "segment_canonical_sync_readback_mismatch"
        return plan
    plan["sqlite_upserted"] = plan["sqlite_valid"]
    plan["synced_segment_ids"] = sorted(row["sqlite_row_id"] for row in plan["row_results"])
    return plan


def sync_sqlite_segments(
    db_path: str, rest_url: str, headers: dict, dry_run: bool,
    *, segment_ids: list[int] | None = None,
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
        "requested_segment_ids": [],
        "synced_segment_ids": [],
        "sync_error": "",
        "payloads": [],
    }

    if not os.path.isfile(db_path):
        logger.warning(f"[SQLITE] DB not found: {db_path}")
        return stats

    requested_ids = sorted({int(value) for value in (segment_ids or [])})
    stats["requested_segment_ids"] = requested_ids
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if requested_ids:
        placeholders = ",".join("?" for _ in requested_ids)
        rows = conn.execute(
            "SELECT id, company_code, fiscal_year_end, quarter, segment_name, "
            "segment_sales, segment_profit, data_source "
            f"FROM segment_financials WHERE id IN ({placeholders})",
            requested_ids,
        ).fetchall()
        actual_ids = {int(row["id"]) for row in rows}
        if actual_ids != set(requested_ids):
            conn.close()
            stats["sync_error"] = "segment_sync_requested_ids_missing"
            return stats
    else:
        rows = conn.execute(
            "SELECT id, company_code, fiscal_year_end, quarter, segment_name, "
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
            "segment_key": normalize_segment_display_key(rdict["company_code"], seg_name),
            "sales": sales,
            "profit": profit,
            "source": rdict.get("data_source") or "excel_legacy",
            "updated_at": datetime.now(JST).isoformat(),
        }
        if requested_ids:
            stats["payloads"].append(payload)
        if dry_run:
            stats["sqlite_upserted"] += 1
            if requested_ids:
                stats["synced_segment_ids"].append(int(rdict["id"]))
            continue

        r = requests.post(
            f"{rest_url}/segment_canonical",
            json=payload,
            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=30,
        )
        if r.status_code in (200, 201):
            stats["sqlite_upserted"] += 1
            if requested_ids:
                stats["synced_segment_ids"].append(int(rdict["id"]))
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
                for row in rows:
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
                    if requested_ids and not cw_result["errors"] and not _canonical_readback_matches(
                        canonical_config,
                        ticker=ticker, period=period, quarter=quarter, source=ds, segments=segs,
                    ):
                        canonical_errors += 1
                    if ds == "backfill_v4_pdf" and len(segs) >= 2:
                        _v4pdf_keys.add((ticker, period, quarter))
                logger.info(
                    f"[CANONICAL] segments dual-write: "
                    f"written={canonical_total} errors={canonical_errors}"
                )
                if requested_ids and canonical_errors:
                    stats["sync_error"] = "segment_canonical_sync_readback_mismatch"

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
                if requested_ids:
                    stats["sync_error"] = "segment_canonical_sync_failed_after_sqlite_commit"
        except Exception as _cw_err:
            logger.warning(
                f"[CANONICAL] segments dual-write failed "
                f"(best-effort, legacy unaffected): {_cw_err}"
            )
            if requested_ids:
                stats["sync_error"] = "segment_canonical_sync_failed_after_sqlite_commit"

    stats["synced_segment_ids"] = sorted(set(stats["synced_segment_ids"]))
    if requested_ids and stats["sqlite_errors"]:
        stats["sync_error"] = "segment_canonical_sync_failed_after_sqlite_commit"
    return stats


def sync_sqlite_segment_ids(
    db_path: str, segment_ids: list[int], rest_url: str, headers: dict, dry_run: bool,
) -> dict:
    """Explicit SQLite row-ID scoped canonical sync; never broadens to ticker/date scope."""
    rows, error = _read_segment_id_rows(db_path, segment_ids)
    if error:
        stats = _empty_layered_stats(segment_ids)
        stats["sqlite_total"] = len(rows)
        stats["sync_error"] = error
        return stats
    if rows and bool(rows[0].get("lineage_contract_present")):
        plan = plan_alias_aware_segment_ids(
            db_path, segment_ids, rest_url, headers, live_read=not dry_run,
        )
        return _execute_alias_aware_plan(plan, rest_url, headers, dry_run=dry_run)

    alias_flags = {has_segment_display_aliases(str(row["company_code"])) for row in rows}
    if True in alias_flags:
        if alias_flags != {True}:
            stats = _empty_layered_stats(segment_ids)
            stats["sqlite_total"] = len(rows)
            stats["sync_error"] = "segment_alias_mixed_scope_requires_review"
            return stats
        plan = plan_alias_aware_segment_ids(
            db_path, segment_ids, rest_url, headers, live_read=not dry_run,
        )
        return _execute_alias_aware_plan(plan, rest_url, headers, dry_run=dry_run)
    return sync_sqlite_segments(
        db_path, rest_url, headers, dry_run, segment_ids=segment_ids,
    )


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
    parser.add_argument("--segment-ids", default="",
                        help="segment_financials.id のCSV。指定時はID限定同期（dry-run既定）")
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

    if opts.segment_ids:
        if opts.xbrl_only:
            parser.error("--segment-ids cannot be combined with --xbrl-only")
        try:
            segment_ids = [int(value) for value in opts.segment_ids.split(",") if value.strip()]
        except ValueError:
            parser.error("--segment-ids must be a comma-separated integer list")
        if not segment_ids:
            parser.error("--segment-ids must not be empty")
        if opts.apply and os.environ.get("ALLOW_SEGMENT_CANONICAL_SYNC") != "1":
            parser.error("--segment-ids --apply requires ALLOW_SEGMENT_CANONICAL_SYNC=1")
        result = sync_sqlite_segment_ids(
            os.path.join(_PROJECT_ROOT, opts.db), segment_ids, rest_url, headers, dry_run,
        )
        print(result)
        return 0 if not result["sync_error"] else 1
    
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
                    SELECT current_fiscal_year_end_date, type_of_current_period 
                    FROM jquants_financials_normalized 
                    WHERE raw_json LIKE ?
                    LIMIT 1
                ''', (f'%"{disc_no}"%',))
                row_fy = c.fetchone()
                if row_fy:
                    fy_end = row_fy[0]
                    quarter = row_fy[1]
                jquants_conn.close()
                logger.info(f"  [DRY-RUN] disc_no={disc_no}, fy_end={fy_end}, quarter={quarter}")
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

        rows = extract_segments_from_xbrl_zip(zpath, period=fy_end, quarter=quarter)
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
