"""
EDINET 書類検索 — ticker + 決算期から有報/半報 doc_id を自動取得

EDINET docTypeCode:
  120 = 有価証券報告書
  130 = 訂正有価証券報告書
  140 = 四半期報告書 (2024年制度改正で廃止中)
  150 = 訂正四半期報告書
  160 = 半期報告書
  170 = 訂正半期報告書
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from lib.backfill.edinet_client import EdinetClient, EdinetDocument

logger = logging.getLogger("edinet_search")

# ============================================================
# docTypeCode → 文書カテゴリ
# ============================================================
# 有報・半報系 (セグメント抽出対象)
_SECURITIES_REPORT_CODES = {"120", "130"}  # 有報 + 訂正有報
_SEMIANNUAL_REPORT_CODES = {"160", "170"}  # 半報 + 訂正半報
_QUARTERLY_REPORT_CODES = {"140", "150"}   # 四半期報 + 訂正 (2024年以降は減少)

# セグメント抽出対象の全 docTypeCode
_SEGMENT_TARGET_CODES = _SECURITIES_REPORT_CODES | _SEMIANNUAL_REPORT_CODES | _QUARTERLY_REPORT_CODES

# doc_type 文字列 → docTypeCode セット
_DOC_TYPE_MAP = {
    "securities_report": _SECURITIES_REPORT_CODES,
    "semiannual_report": _SEMIANNUAL_REPORT_CODES,
    "quarterly_report": _QUARTERLY_REPORT_CODES,
    "all": _SEGMENT_TARGET_CODES,
}


@dataclass
class EdinetSearchResult:
    """EDINET 書類検索の結果。"""
    found: bool = False
    doc_id: str = ""
    doc: Optional[EdinetDocument] = None
    candidates: list[EdinetDocument] = field(default_factory=list)
    search_dates: list[str] = field(default_factory=list)
    reason: str = ""  # "found" / "no_candidates" / "no_api_key" / "no_ticker_match"


def _ticker_to_sec_code(ticker: str) -> str:
    """4桁 ticker → 5桁 secCode (末尾0)。common_ticker に委譲。"""
    from src.common_ticker import ticker_to_sec_code
    return ticker_to_sec_code(ticker)


def _normalize_ticker_for_match(ticker: str) -> str:
    """ticker/secCode を比較用に正規化。common_ticker に委譲。"""
    from src.common_ticker import normalize_ticker
    return normalize_ticker(ticker)


def search_securities_reports(
    client: EdinetClient,
    ticker: str,
    fiscal_end: str,
    *,
    doc_type: str = "securities_report",
    search_days_after: int = 120,
    search_days_before: int = 0,
) -> EdinetSearchResult:
    """EDINET 検索 API で有報/半報の doc_id を自動取得。

    Args:
        client: EdinetClient instance
        ticker: 4桁銘柄コード
        fiscal_end: 決算期末日 (YYYY-MM-DD)
        doc_type: "securities_report" / "semiannual_report" / "quarterly_report" / "all"
        search_days_after: 決算日からの検索日数 (提出は通常60-90日後)
        search_days_before: 決算日前の検索日数

    Returns:
        EdinetSearchResult
    """
    if not client.has_api_key:
        return EdinetSearchResult(reason="no_api_key")

    target_codes = _DOC_TYPE_MAP.get(doc_type, _SEGMENT_TARGET_CODES)
    sec_code = _ticker_to_sec_code(ticker)

    # 検索日付範囲: 決算日 - before ~ + after
    base_date = datetime.strptime(fiscal_end, "%Y-%m-%d")
    start_date = base_date - timedelta(days=search_days_before)
    end_date = base_date + timedelta(days=search_days_after)

    # 効率化: 全日検索は非効率なので、提出が集中する期間を段階的に探す
    # Phase 1: 決算日+60日~+100日 (ほとんどの有報はここ)
    # Phase 2: 決算日+30日~+59日 / +101日~+120日
    # Phase 3: 決算日~+29日 (早期提出)
    _WINDOWS = [
        (60, 100),   # メイン窓
        (30, 59),    # 早期窓
        (101, 120),  # 遅延窓
        (0, 29),     # 超早期窓
    ]

    all_candidates: list[EdinetDocument] = []
    search_dates: list[str] = []
    seen_doc_ids: set[str] = set()
    norm_ticker = _normalize_ticker_for_match(ticker)

    def _search_window(win_start: int, win_end: int, *, use_ticker_filter: bool) -> bool:
        """指定窓を検索し、ticker一致候補が見つかれば True。"""
        for day_offset in range(win_start, win_end + 1):
            d = (base_date + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            search_dates.append(d)

            # Phase A: ticker 付きで高速検索
            # Phase B: ticker なしで全件取得 (secCode 欠落対策)
            docs = client.search_documents(
                d, ticker=ticker if use_ticker_filter else None
            )
            for doc in docs:
                if doc.doc_id in seen_doc_ids:
                    continue
                seen_doc_ids.add(doc.doc_id)

                if doc.doc_type_code not in target_codes:
                    continue
                if not doc.xbrl_available:
                    continue

                all_candidates.append(doc)

        matches = [
            c for c in all_candidates
            if _normalize_ticker_for_match(c.ticker or "") == norm_ticker
            or _normalize_ticker_for_match(c.secCode or "") == norm_ticker
        ]
        return len(matches) > 0

    # Phase A: ticker フィルタ付きで段階的窓検索 (高速)
    found_in_phase_a = False
    for win_start, win_end in _WINDOWS:
        if _search_window(win_start, win_end, use_ticker_filter=True):
            found_in_phase_a = True
            logger.info(
                f"[edinet_search] found in Phase A (ticker filter) "
                f"window [{win_start}d, {win_end}d]"
            )
            break

    # Phase B: Phase A で見つからない場合、ticker フィルタなしで再検索
    # (secCode 欠落・不一致の企業向け fallback)
    if not found_in_phase_a:
        logger.info(
            f"[edinet_search] Phase A failed for ticker={ticker}, "
            f"trying Phase B (no ticker filter, main window only)"
        )
        seen_doc_ids.clear()
        all_candidates.clear()
        # メイン窓のみ (効率化: 全窓は遅すぎる)
        _search_window(60, 100, use_ticker_filter=False)

    if not all_candidates:
        return EdinetSearchResult(
            candidates=[], search_dates=search_dates,
            reason="no_candidates",
        )

    # ticker 一致で絞り込み
    ticker_matches = [
        c for c in all_candidates
        if _normalize_ticker_for_match(c.ticker or "") == norm_ticker
        or _normalize_ticker_for_match(c.secCode or "") == norm_ticker
    ]

    if not ticker_matches:
        return EdinetSearchResult(
            candidates=all_candidates, search_dates=search_dates,
            reason="no_ticker_match",
        )

    # 最新の提出を選択 (訂正があれば訂正を優先)
    ticker_matches.sort(key=lambda d: d.document_date, reverse=True)

    # 訂正版を優先 (130=訂正有報, 170=訂正半報)
    correction_codes = {"130", "170", "150"}
    corrections = [d for d in ticker_matches if d.doc_type_code in correction_codes]
    if corrections:
        best = corrections[0]
    else:
        best = ticker_matches[0]

    logger.info(
        f"[edinet_search] selected: ticker={ticker} doc_id={best.doc_id} "
        f"docType={best.doc_type_code} title={best.title}"
    )

    return EdinetSearchResult(
        found=True,
        doc_id=best.doc_id,
        doc=best,
        candidates=ticker_matches,
        search_dates=search_dates,
        reason="found",
    )


def search_all_report_types(
    client: EdinetClient,
    ticker: str,
    fiscal_end: str,
) -> list[EdinetSearchResult]:
    """有報 + 半報 + 四半期報を全部検索して返す。

    Returns:
        [securities_report_result, semiannual_result, quarterly_result]
    """
    results = []
    for dt in ("securities_report", "semiannual_report", "quarterly_report"):
        r = search_securities_reports(client, ticker, fiscal_end, doc_type=dt)
        results.append(r)
    return results
