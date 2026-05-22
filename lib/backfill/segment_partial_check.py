"""lib/backfill/segment_partial_check.py — TDNET XBRL partial-success 判定

XBRL抽出が「成功」判定でも、セグメントが一部しか取れていない疑いがある場合に
suspicious_partial_segments=True を返す。

判定条件:
  1. segment数が少なすぎる (unique segment_name <= 2)
     - Otherのみ、または 主力1件+Other なら強く疑う
  2. Other比率がおかしい (Other売上 / 全売上 >= 30% または Other が2位以上)
  3. segment合計と連結売上の乖離 (差分 >= 5%)
  4. 同一 ticker の過去EDINET由来セグメント数と比較 (TDNET < EDINET - 1)

fallback採用ルール:
  - PDF V4 segment_count > XBRL segment_count → PDF V4優先
  - PDF V4に売上・利益の両方がある行が多ければ PDF V4優先
  - それ以外はXBRL維持
"""
from __future__ import annotations

import logging
import statistics
from typing import Any

logger = logging.getLogger("backfill.segment_partial_check")

# ---------------------------------------------------------------------------
# "Other" セグメント名パターン
# ---------------------------------------------------------------------------
_OTHER_NAMES_LOWER = {
    "other", "others", "その他", "その他事業", "その他セグメント",
    "その他・調整", "other operations", "other businesses",
    "other segments",
}

# 連結売上フィールド候補
_CONSOLIDATED_SALES_KEYS = [
    "net_sales", "sales", "revenue", "operating_revenue",
    "consolidated_sales", "total_sales",
]


def _is_other_segment(name: str) -> bool:
    """セグメント名が "Other" 系かどうかを判定する。"""
    n = (name or "").strip().lower()
    if n in _OTHER_NAMES_LOWER:
        return True
    # 部分一致（"その他" を含む等）
    if "その他" in n:
        return True
    if n.startswith("other") and len(n) <= 20:
        return True
    return False


def _get_consolidated_sales(financials_data: dict | None) -> float | None:
    """financials_data から連結売上を取得する。"""
    if not financials_data:
        return None
    for key in _CONSOLIDATED_SALES_KEYS:
        v = financials_data.get(key)
        if v is not None:
            try:
                fv = float(v)
                if fv > 0:
                    return fv
            except (TypeError, ValueError):
                pass
    return None


def _fetch_edinet_hist_segment_count(
    ticker: str,
    fiscal_year_end: str,
    db=None,
    db_path: str | None = None,
    max_prior_periods: int = 3,
) -> int | None:
    """同一 ticker の過去EDINET由来セグメント数の代表値を返す。

    - data_source または extractor_route が edinet / edinet_xbrl 系
    - fiscal_year_end < 対象 fiscal_year_end の直近 max_prior_periods 期を参照
    - 代表値は median（偶数のとき下位値）を返す
    - 取得できなければ None を返す
    """
    if db is None and db_path is None:
        return None

    conn = None
    _should_close = False
    try:
        if db is not None:
            # MigrationDB 互換: .conn or ._conn
            conn = getattr(db, "conn", None) or getattr(db, "_conn", None)
        if conn is None and db_path:
            import sqlite3
            conn = sqlite3.connect(db_path)
            _should_close = True

        if conn is None:
            return None

        # 過去 EDINET 行の fiscal_year_end ごとのセグメント数を集計
        # edinet / edinet_xbrl / backfill_edinet などを包括的に捕捉
        sql = """
            SELECT fiscal_year_end, COUNT(DISTINCT segment_name) AS seg_cnt
            FROM segment_financials
            WHERE company_code = ?
              AND fiscal_year_end < ?
              AND (
                  data_source LIKE '%edinet%'
                  OR extractor_route LIKE '%edinet%'
              )
            GROUP BY fiscal_year_end
            ORDER BY fiscal_year_end DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (ticker, fiscal_year_end, max_prior_periods)).fetchall()
        if not rows:
            return None

        counts = [r[1] for r in rows if r[1] and r[1] > 0]
        if not counts:
            return None

        # median（中央値）: statistics.median は偶数時に平均するため、
        # 保守的に floor を取る
        med = statistics.median(counts)
        return int(med)

    except Exception as e:
        logger.debug("[segment_partial_check] edinet hist fetch error: %s", e)
        return None
    finally:
        if _should_close and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# メイン判定関数
# ---------------------------------------------------------------------------

def check_xbrl_partial_segments(
    xbrl_records: list[dict],
    ticker: str,
    fiscal_year_end: str,
    financials_data: dict | None = None,
    db=None,
    db_path: str | None = None,
) -> tuple[bool, str, dict]:
    """XBRL抽出結果が partial-success の疑いがあるかを判定する。

    Args:
        xbrl_records:    XBRL抽出セグメントレコード一覧
        ticker:          4桁ティッカー
        fiscal_year_end: 対象期末日 (例: "2026-03-31")
        financials_data: PL連結データ (net_salesなど) - 任意
        db:              MigrationDB インスタンス (任意)
        db_path:         SQLite パス (dbがNoneの場合のみ使用)

    Returns:
        (suspicious: bool, reason: str, detail: dict)
        - suspicious=True なら PDF V4 fallback を試行すべき
        - reason: "main_plus_other_only" など
        - detail: ログ用の詳細情報
    """
    detail: dict[str, Any] = {
        "ticker": ticker,
        "fiscal_year_end": fiscal_year_end,
        "xbrl_count": 0,
        "other_ratio": 0.0,
        "edinet_hist_count": None,
    }

    if not xbrl_records:
        detail["reason"] = "no_records"
        return False, "no_records", detail

    # ── current period の行のみ対象 ──
    # (period が fiscal_year_end と一致する行、または period_type="current"、
    #   またはすべて period_type 未設定の場合は全件)
    current_rows = [
        r for r in xbrl_records
        if r.get("period") == fiscal_year_end
        or r.get("period_type") == "current"
    ]
    if not current_rows:
        current_rows = xbrl_records  # period 解決できない場合は全件を対象

    # ── 指標計算 ──
    seg_names = [r.get("segment_name", "") for r in current_rows]
    unique_names = list(dict.fromkeys(seg_names))  # 順序保持で deduplicate
    unique_count = len(unique_names)
    detail["xbrl_count"] = unique_count

    other_segs = [n for n in unique_names if _is_other_segment(n)]
    non_other_segs = [n for n in unique_names if not _is_other_segment(n)]

    # セグメント売上合計 & Other売上
    total_seg_sales = 0.0
    other_seg_sales = 0.0
    other_rank = None
    sales_by_seg: list[tuple[str, float]] = []

    for name in unique_names:
        seg_rows = [r for r in current_rows if r.get("segment_name") == name]
        sales_sum = sum(
            float(r["segment_sales"])
            for r in seg_rows
            if r.get("segment_sales") is not None
        )
        total_seg_sales += sales_sum
        if _is_other_segment(name):
            other_seg_sales += sales_sum
        sales_by_seg.append((name, sales_sum))

    # Other の売上ランク (1-indexed)
    if sales_by_seg and total_seg_sales > 0:
        sales_by_seg_sorted = sorted(sales_by_seg, key=lambda x: -x[1])
        for rank, (name, _) in enumerate(sales_by_seg_sorted, start=1):
            if _is_other_segment(name):
                other_rank = rank
                break

    other_ratio = (other_seg_sales / total_seg_sales) if total_seg_sales > 0 else 0.0
    detail["other_ratio"] = round(other_ratio, 4)
    detail["other_rank"] = other_rank
    detail["unique_count"] = unique_count
    detail["non_other_count"] = len(non_other_segs)

    # ── 条件1: セグメント数が少なすぎる ──
    # 「主力1件+Other」パターン → 強く疑う
    is_main_plus_other = (
        unique_count == 2
        and len(non_other_segs) == 1
        and len(other_segs) == 1
    )
    # Other のみ
    is_other_only = (unique_count == 1 and len(other_segs) == 1)

    # ── 条件2: Other比率がおかしい ──
    other_ratio_suspicious = (other_ratio >= 0.30) or (other_rank is not None and other_rank <= 2)

    # ── 条件3: segment合計と連結売上の乖離 ──
    consolidated_sales_suspicious = False
    consolidated_sales = _get_consolidated_sales(financials_data)
    detail["consolidated_sales"] = consolidated_sales
    if consolidated_sales and consolidated_sales > 0 and total_seg_sales > 0:
        gap_ratio = abs(total_seg_sales - consolidated_sales) / consolidated_sales
        detail["consolidated_gap_ratio"] = round(gap_ratio, 4)
        consolidated_sales_suspicious = gap_ratio >= 0.05
    else:
        detail["consolidated_gap_ratio"] = None

    # ── 条件4: EDINET過去データとの比較 ──
    edinet_hist_count = _fetch_edinet_hist_segment_count(
        ticker, fiscal_year_end, db=db, db_path=db_path,
    )
    detail["edinet_hist_count"] = edinet_hist_count
    edinet_gap_suspicious = False
    edinet_gap = None
    if edinet_hist_count is not None and unique_count is not None:
        edinet_gap = edinet_hist_count - unique_count
        detail["edinet_gap"] = edinet_gap
        edinet_gap_suspicious = edinet_gap >= 2
    else:
        detail["edinet_gap"] = None

    # ── 総合判定 ──
    suspicious = False
    reason = ""

    if is_other_only:
        suspicious = True
        reason = "other_only"
    elif is_main_plus_other:
        suspicious = True
        reason = "main_plus_other_only"
    elif unique_count <= 2 and other_ratio_suspicious:
        suspicious = True
        reason = "too_few_segments_with_high_other"
    elif unique_count <= 2 and consolidated_sales_suspicious:
        suspicious = True
        reason = "too_few_segments_with_sales_gap"
    elif edinet_gap_suspicious:
        suspicious = True
        reason = "edinet_hist_gap"
    elif other_ratio_suspicious and not is_main_plus_other:
        # Other比率が高い（少なくとも警告）→ EDINET との差が1未満でも疑う
        if edinet_gap is not None and edinet_gap >= 1:
            suspicious = True
            reason = "other_ratio_high_with_edinet_gap"
        else:
            # 警告のみ: fallbackはしない
            suspicious = False
            reason = "other_ratio_high_warn_only"

    detail["reason"] = reason
    detail["suspicious"] = suspicious

    return suspicious, reason, detail


# ---------------------------------------------------------------------------
# PDF V4 fallback 採用判定
# ---------------------------------------------------------------------------

def decide_fallback_adoption(
    xbrl_records: list[dict],
    pdf_records: list[dict],
    detail: dict,
) -> tuple[bool, str]:
    """PDF V4 の結果を採用すべきかを判定する。

    Returns:
        (use_pdf: bool, decision: str)
        - use_pdf=True → PDF V4 結果を採用
        - decision: "use_pdf_v4" | "keep_xbrl"
    """
    xbrl_count = len({r.get("segment_name") for r in xbrl_records})
    pdf_count = len({r.get("segment_name") for r in pdf_records})
    detail["pdf_v4_count"] = pdf_count

    if not pdf_records or pdf_count == 0:
        logger.info(
            "[segment_partial_check] pdf_v4_count=0 decision=keep_xbrl ticker=%s fy=%s",
            detail.get("ticker"), detail.get("fiscal_year_end"),
        )
        return False, "keep_xbrl"

    # PDF V4 の方がセグメント数が多い → 採用
    if pdf_count > xbrl_count:
        logger.info(
            "[segment_partial_check] pdf_v4_count=%d decision=use_pdf_v4 ticker=%s fy=%s",
            pdf_count, detail.get("ticker"), detail.get("fiscal_year_end"),
        )
        return True, "use_pdf_v4"

    # 同数でも、PDF V4に売上・利益の両方がある行が多ければ採用
    def _quality_score(records: list[dict]) -> int:
        """売上・利益両方 non-null な行数を返す。"""
        return sum(
            1 for r in records
            if r.get("segment_sales") is not None and r.get("segment_profit") is not None
        )

    pdf_quality = _quality_score(pdf_records)
    xbrl_quality = _quality_score(xbrl_records)
    detail["pdf_v4_quality"] = pdf_quality
    detail["xbrl_quality"] = xbrl_quality

    if pdf_quality > xbrl_quality:
        logger.info(
            "[segment_partial_check] pdf_v4_quality=%d xbrl_quality=%d decision=use_pdf_v4 ticker=%s fy=%s",
            pdf_quality, xbrl_quality, detail.get("ticker"), detail.get("fiscal_year_end"),
        )
        return True, "use_pdf_v4"

    logger.info(
        "[segment_partial_check] pdf_v4_count=%d xbrl_count=%d decision=keep_xbrl ticker=%s fy=%s",
        pdf_count, xbrl_count, detail.get("ticker"), detail.get("fiscal_year_end"),
    )
    return False, "keep_xbrl"


# ---------------------------------------------------------------------------
# 旧 XBRL 行削除（PDF V4 採用確定後）
# ---------------------------------------------------------------------------

#: 削除対象の data_source 値
_XBRL_DATA_SOURCES = ("backfill_xbrl", "tdnet_xbrl")
#: 削除対象の extractor_route 値
_XBRL_EXTRACTOR_ROUTES = ("xbrl",)


def cleanup_old_xbrl_rows(
    conn,
    *,
    ticker: str,
    fiscal_year_end: str,
    quarter: str,
    tdnet_doc_id: str,
    reason: str = "pdf_v4_adopted",
) -> int:
    """PDF V4 採用確定後に、同一キーの旧 XBRL セグメント行を削除する。

    削除条件:
        company_code     = ticker
        fiscal_year_end  = fiscal_year_end
        quarter          = quarter
        tdnet_doc_id     = tdnet_doc_id
        row_type         = 'segment'
        data_source IN ('backfill_xbrl', 'tdnet_xbrl')
        OR extractor_route = 'xbrl'

    Args:
        conn:            sqlite3.Connection（または _conn を持つ MigrationDB）
        ticker:          4桁ティッカー
        fiscal_year_end: 対象期末日
        quarter:         対象四半期
        tdnet_doc_id:    TDNET 開示 ID（filing_id）
        reason:          ログ用理由文字列

    Returns:
        削除件数 (0 の場合も正常)
    """
    # MigrationDB 互換: conn が接続オブジェクトでなければ .conn/_conn を取得
    import sqlite3 as _sqlite3
    _raw_conn = conn
    if not isinstance(conn, _sqlite3.Connection):
        _raw_conn = getattr(conn, "_conn", None) or getattr(conn, "conn", None)
    if _raw_conn is None:
        logger.warning(
            "[segment_partial_check] cleanup_old_xbrl_rows: conn is None, skip ticker=%s fy=%s",
            ticker, fiscal_year_end,
        )
        return 0

    ds_placeholders = ",".join("?" * len(_XBRL_DATA_SOURCES))
    er_placeholders = ",".join("?" * len(_XBRL_EXTRACTOR_ROUTES))

    sql = f"""
        DELETE FROM segment_financials
        WHERE company_code = ?
          AND fiscal_year_end = ?
          AND quarter = ?
          AND tdnet_doc_id = ?
          AND row_type = 'segment'
          AND (
              data_source IN ({ds_placeholders})
              OR extractor_route IN ({er_placeholders})
          )
    """
    params = (
        ticker,
        fiscal_year_end,
        quarter,
        tdnet_doc_id,
        *_XBRL_DATA_SOURCES,
        *_XBRL_EXTRACTOR_ROUTES,
    )

    try:
        cur = _raw_conn.execute(sql, params)
        deleted = cur.rowcount
        logger.info(
            "[segment_partial_check] cleanup_old_xbrl_rows ticker=%s fy=%s quarter=%s "
            "tdnet_doc_id=%s deleted=%d reason=%s",
            ticker, fiscal_year_end, quarter, tdnet_doc_id, deleted, reason,
        )
        return deleted
    except Exception as e:
        logger.error(
            "[segment_partial_check] cleanup_old_xbrl_rows FAILED ticker=%s fy=%s err=%s",
            ticker, fiscal_year_end, e,
        )
        return 0


# ---------------------------------------------------------------------------
# PDF V4 集計行削除（aggregate segment cleanup）
# ---------------------------------------------------------------------------

#: 削除対象の PDF V4 data_source 値
_PDF_V4_DATA_SOURCES = ("backfill_v4_pdf", "v4_pdf")
#: 削除対象の PDF V4 extractor_route 値
_PDF_V4_EXTRACTOR_ROUTES = ("v4_v4", "pdf_v4")

# aggregate 判定: segment_name の NFKC 正規化後がこれらと完全一致
_AGGREGATE_EXACT_NAMES = (
    "連結", "合計", "計", "調整額",
    "consolidated", "total", "adjustment", "eliminations",
)

# aggregate 判定: segment_name がこれらの文字列を含む
_AGGREGATE_CONTAINS_NAMES = (
    "調整額", "セグメント利益調整", "その他調整",
    "adjustment", "eliminations",
    "連結財務諸表",
)

# aggregate 判定: segment_name がこれらで終わる（「その他」系を除く）
_AGGREGATE_ENDSWITH_NAMES = ("合計", "計", "total")

# ホワイトリスト: これで始まる segment_name は除外しない
# ただし「その他調整」のように調整系ワードを含む場合は保護を解除（Python側で判定）
_AGGREGATE_WHITELIST_PREFIX = ("その他", "other")


def _build_aggregate_name_condition() -> tuple[str, list]:
    """segment_name が aggregate に該当する SQL 条件と params を返す。

    「その他」系の誤削除を防ぐため、NOT LIKE ガードを付ける。
    完全一致 + 含む + 末尾一致の OR 結合。
    """
    conditions = []
    params: list = []

    # ── ホワイトリスト除外条件（NOT LIKE） ──
    # 「その他」「other」で始まり、かつ調整系ワードを含まないものは保護
    whitelist_not_conditions = " AND ".join(
        f"segment_name NOT LIKE ?"
        for _ in _AGGREGATE_WHITELIST_PREFIX
    )
    whitelist_params = [f"{p}%" for p in _AGGREGATE_WHITELIST_PREFIX]

    # ── 完全一致（NFKC正規化はSQL側では難しいため OR LIKE で対応）──
    exact_conds = " OR ".join("segment_name = ?" for _ in _AGGREGATE_EXACT_NAMES)
    params.extend(_AGGREGATE_EXACT_NAMES)

    # ── 含む（LIKE %kw%）──
    contains_conds = " OR ".join("segment_name LIKE ?" for _ in _AGGREGATE_CONTAINS_NAMES)
    params.extend(f"%{kw}%" for kw in _AGGREGATE_CONTAINS_NAMES)

    # ── 末尾一致（LIKE %kw）──
    endswith_conds = " OR ".join("segment_name LIKE ?" for _ in _AGGREGATE_ENDSWITH_NAMES)
    params.extend(f"%{kw}" for kw in _AGGREGATE_ENDSWITH_NAMES)

    # 全条件を OR 結合
    name_cond = f"({exact_conds} OR {contains_conds} OR {endswith_conds})"

    # ホワイトリスト: 「その他」「other」で始まるものを保護
    # 「その他調整」は削除したいので、調整系ワードを含む場合はホワイトリストを解除する
    # SQLだけでは難しいため、ホワイトリストをNOT LIKE で付与し、
    # 「その他調整」は _AGGREGATE_CONTAINS_NAMES の「調整」で補足する
    whitelist_guard = (
        "NOT ("
        + " OR ".join(
            f"(segment_name LIKE ? AND segment_name NOT LIKE ? AND segment_name NOT LIKE ?)"
            for _ in _AGGREGATE_WHITELIST_PREFIX
        )
        + ")"
    )
    whitelist_guard_params = []
    for prefix in _AGGREGATE_WHITELIST_PREFIX:
        whitelist_guard_params.extend([
            f"{prefix}%",   # LIKE prefix%
            "%調整%",       # NOT LIKE %調整%
            "%adjustment%", # NOT LIKE %adjustment%
        ])

    final_cond = f"({name_cond} AND {whitelist_guard})"
    final_params = params + whitelist_guard_params
    return final_cond, final_params


def cleanup_aggregate_pdf_rows(
    conn,
    *,
    ticker: str,
    fiscal_year_end: str,
    quarter: str,
    tdnet_doc_id: str,
) -> int:
    """PDF V4 由来の集計行（連結・合計・調整額等）を削除する。

    PDF V4 再処理時に過去の aggregate segment が残存しないよう、
    cleanup_old_xbrl_rows() と同一トランザクション内で呼ぶ。

    削除条件:
        company_code     = ticker
        fiscal_year_end  = fiscal_year_end
        quarter          = quarter
        tdnet_doc_id     = tdnet_doc_id
        row_type         = 'segment'
        data_source IN ('backfill_v4_pdf', 'v4_pdf')
        OR extractor_route IN ('v4_v4', 'pdf_v4')
        AND segment_name が aggregate 判定に該当
        AND segment_name が「その他」系でない

    Returns:
        削除件数 (0 の場合も正常)
    """
    import sqlite3 as _sqlite3
    _raw_conn = conn
    if not isinstance(conn, _sqlite3.Connection):
        _raw_conn = getattr(conn, "_conn", None) or getattr(conn, "conn", None)
    if _raw_conn is None:
        logger.warning(
            "[segment_partial_check] cleanup_aggregate_pdf_rows: conn is None, skip ticker=%s fy=%s",
            ticker, fiscal_year_end,
        )
        return 0

    ds_placeholders = ",".join("?" * len(_PDF_V4_DATA_SOURCES))
    er_placeholders = ",".join("?" * len(_PDF_V4_EXTRACTOR_ROUTES))

    name_cond, name_params = _build_aggregate_name_condition()

    sql = f"""
        DELETE FROM segment_financials
        WHERE company_code = ?
          AND fiscal_year_end = ?
          AND quarter = ?
          AND tdnet_doc_id = ?
          AND row_type = 'segment'
          AND (
              data_source IN ({ds_placeholders})
              OR extractor_route IN ({er_placeholders})
          )
          AND {name_cond}
    """
    params = (
        ticker,
        fiscal_year_end,
        quarter,
        tdnet_doc_id,
        *_PDF_V4_DATA_SOURCES,
        *_PDF_V4_EXTRACTOR_ROUTES,
        *name_params,
    )

    try:
        cur = _raw_conn.execute(sql, params)
        deleted = cur.rowcount
        logger.info(
            "[segment_partial_check] cleanup_aggregate_pdf_rows ticker=%s fy=%s quarter=%s "
            "tdnet_doc_id=%s deleted=%d",
            ticker, fiscal_year_end, quarter, tdnet_doc_id, deleted,
        )
        return deleted
    except Exception as e:
        logger.error(
            "[segment_partial_check] cleanup_aggregate_pdf_rows FAILED ticker=%s fy=%s err=%s",
            ticker, fiscal_year_end, e,
        )
        return 0
