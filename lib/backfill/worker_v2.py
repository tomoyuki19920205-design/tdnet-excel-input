"""lib/backfill/worker_v2.py — 新パイプライン worker (XBRL only)

TDNET セグメント抽出は XBRL only。PDF path は disabled。
速報レイヤーでは precision を最優先し、XBRL source のみを使用する。
XBRL source がない場合は no_xbrl_segment_source として quarantine する
(「セグメントなし」ではなく「速報時点では未取得」)。

エントリポイント:
  - process_one_filing_v2()   XBRL only 抽出

PIPELINE_SPEC §11
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from lib.backfill.worker import (
    _ensure_imports,
    _download_originals,
    _extract_financials_data,
    _extract_segments,
    _normalize_segment_name_conservative,
    _classify_row_type,
    _update_state,
    compute_result_fingerprint,
)

logger = logging.getLogger("backfill.worker_v2")

# ============================================================
# [TEST ONLY] PDF強制ルート — テスト後は必ず False に戻すこと
# ============================================================
TEST_FORCE_PDF_ONLY: bool = False


# ============================================================
# validator status → worker status 変換
# ============================================================

_VALIDATOR_TO_WORKER_STATUS = {
    "success": "ok",
    "partial": "partial",
    "quarantine": "quarantined",
}


def validator_status_to_worker(validator_status: str) -> str:
    """ExtractionStatus.value → worker result status に変換。"""
    return _VALIDATOR_TO_WORKER_STATUS.get(validator_status, "quarantined")


# ============================================================
# Source priority (小さい方が高優先)
# ============================================================

_SOURCE_PRIORITY = {"xbrl": 0, "html": 1}
_STATUS_PRIORITY = {"success": 0, "partial": 1, "quarantine": 2}


# ============================================================
# データクラス
# ============================================================

@dataclass
class SourceCandidate:
    """各 source の抽出・検証結果。"""
    source: str                                   # "xbrl" | "html" | "pdf"
    attempted: bool                               # 抽出を実行したか
    available: bool                               # source ファイルが存在するか
    skip_reason: str = ""                         # not_available | not_implemented | disabled | ""
    segment_records: list = field(default_factory=list)
    validation: object = None                     # ExtractionValidationResult | None
    error: str = ""                               # 抽出エラー (あれば)
    # Phase 2: Trace & Scores
    rule_trace: list[str] = field(default_factory=list)
    score_summary: dict = field(default_factory=dict)


@dataclass
class FilingResultV2:
    """process_one_filing_v2 の返り値。"""
    filing_id: str
    status: str                                   # "ok" | "partial" | "quarantined" | "failed"
    source: str                                   # "xbrl" | "html" | "pdf" | ""
    selected_path: str                            # "xbrl" | "html" | "pdf" | "none"
    confidence: float                             # 0.0-1.0
    reason: str                                   # 人間可読な判定理由
    hard_fail_reason: str                         # HardFailReason.value (stable string)
    quarantine_reason: str                        # 安定名 (集計用)
    fallback_used: bool
    fallback_reason: str                          # primary_unavailable | primary_partial | primary_quarantine | ""
    raw_segment_count: int
    valid_segment_count: int
    invalid_segment_count: int
    sales_non_null_count: int
    profit_non_null_count: int
    invalid_names: list = field(default_factory=list)
    account_like_ratio: float = 0.0
    narrative_contamination: bool = False
    # 互換フィールド
    segment_records: list = field(default_factory=list)
    financial_records: list = field(default_factory=list)
    via: str | None = None                        # 互換: selected_path alias
    metrics: dict = field(default_factory=dict)
    cache_paths: dict = field(default_factory=dict)
    quarantine: dict | None = None
    result_fingerprint: str | None = None
    # Phase 2 trace
    rule_trace: list[str] = field(default_factory=list)
    score_summary: dict = field(default_factory=dict)
    # debug
    candidates: list = field(default_factory=list, repr=False)
    candidate_summary: str = ""
    route_mode: str = ""                          # "xbrl_v2" | "pdf_v1_compat" | ""


# ============================================================
# メインエントリポイント
# ============================================================

def process_one_filing_v2(
    filing, *,
    cache_root: str = "data/tdnet_cache",
    state_store=None,
    retry_download: int = 3, retry_xbrl: int = 2, retry_pdf: int = 1,
    timeout_download: int = 30, timeout_xbrl: int = 60, timeout_pdf: int = 120,
    run_id: str | None = None,
    sleep_fn=None,
) -> FilingResultV2:
    """新パイプライン: XBRL → HTML(stub) → PDF 順序で抽出し validator で判定。"""
    _ensure_imports()
    from lib.backfill.cache import (
        ensure_cache_layout, write_metadata, has_pdf, has_xbrl,
        save_extract_financials_result, save_extract_segments_result,
        save_quarantine, append_filing_log,
    )
    import time as _time
    _sleep = sleep_fn or _time.sleep

    t0 = time.monotonic()
    fid = filing.filing_id
    metrics: dict = {"attempts": {}, "pipeline": "v2"}
    paths = ensure_cache_layout(cache_root, fid)
    write_metadata(paths, filing)

    _update_state(state_store, fid, "running", stage="downloading_v2")
    append_filing_log(paths, {"event": "v2_start", "ticker": filing.ticker, "run_id": run_id})

    # ========================================
    # Step 1: Download
    # ========================================
    doc_path, xbrl_path = _download_originals(
        filing, paths, metrics,
        retry_download=retry_download, timeout_download=timeout_download, sleep_fn=_sleep,
    )

    if not doc_path and not xbrl_path:
        elapsed = int((time.monotonic() - t0) * 1000)
        return FilingResultV2(
            filing_id=fid, status="failed", source="", selected_path="none",
            confidence=0.0, reason="ダウンロード失敗", hard_fail_reason="",
            quarantine_reason="download_failed", fallback_used=False, fallback_reason="",
            raw_segment_count=0, valid_segment_count=0, invalid_segment_count=0,
            sales_non_null_count=0, profit_non_null_count=0,
            metrics={**metrics, "total_ms": elapsed},
            cache_paths={"cache_dir": str(paths.cache_dir)},
        )

    # PL 抽出 (全 source 共通)
    _update_state(state_store, fid, "running", stage="extracting_v2")
    financials_data, fin_via = _extract_financials_data(
        doc_path, xbrl_path, filing, metrics,
        retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
        timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf, sleep_fn=_sleep,
    )
    if financials_data:
        save_extract_financials_result(paths, financials_data)

    # ========================================
    # [TEST] XBRL 無効化 → PDF 強制ルート
    # ========================================
    if TEST_FORCE_PDF_ONLY and doc_path:
        logger.info(f"[v2][TEST] TEST_FORCE_PDF_ONLY=True: fid={fid} → PDF only")
        from lib.backfill.worker import process_one_filing_pdf_only
        _pdf_result = process_one_filing_pdf_only(
            filing,
            cache_root=cache_root,
            state_store=state_store,
            retry_pdf=retry_pdf,
            timeout_pdf=timeout_pdf,
            sleep_fn=_sleep,
            financials_data=financials_data,
        )
        if not isinstance(_pdf_result, dict):
            setattr(_pdf_result, "route_mode", "test_pdf_forced")
        return _pdf_result

    # ========================================
    # Step 2: 各 source で抽出 → validator
    # ========================================
    from src.segment.extraction_result_validator import validate_extraction_result

    candidates: list[SourceCandidate] = []

    # --- XBRL ---
    xbrl_candidate = _try_xbrl_source(
        xbrl_path, doc_path, filing, financials_data, fid, paths, metrics,
        retry_xbrl=retry_xbrl, timeout_xbrl=timeout_xbrl, sleep_fn=_sleep,
    )
    candidates.append(xbrl_candidate)

    # --- HTML (Phase 3 stub) ---
    html_candidate = SourceCandidate(
        source="html", attempted=False, available=False,
        skip_reason="not_implemented",
    )
    candidates.append(html_candidate)

    # --- PDF (disabled: TDNET XBRL only policy) ---
    pdf_candidate = SourceCandidate(
        source="pdf", attempted=False, available=bool(doc_path),
        skip_reason="disabled",
    )
    candidates.append(pdf_candidate)

    # ========================================
    # Step 3: XBRL candidate 評価 (XBRL only)
    # ========================================
    best = xbrl_candidate  # XBRL only: 常に XBRL を選択
    fallback_used = False
    fallback_reason = ""

    # ========================================
    # Step 4: FilingResultV2 構築
    # ========================================
    validation = best.validation
    xbrl_resolved = best.available and best.attempted

    if validation:
        worker_status = validator_status_to_worker(validation.status.value)
        confidence = validation.confidence
        reason = validation.reason
        hard_fail_reason = validation.hard_fail_reason.value
        raw_seg_count = validation.raw_segment_count
        valid_seg_count = validation.valid_segment_count
        invalid_seg_count = validation.invalid_segment_count
        sales_nn = validation.sales_non_null_count
        profit_nn = validation.profit_non_null_count
        invalid_names = validation.invalid_names
        account_like_ratio = validation.account_like_ratio
        narrative_contamination = validation.narrative_contamination
    elif not best.available:
        # XBRL source 自体なし → no_xbrl_segment_source
        fallback_used = True
        fallback_reason = "no_xbrl_segment_source"
        from lib.backfill.worker import process_one_filing_pdf_only
        result = process_one_filing_pdf_only(filing)
        # 属性引き継ぎ (FilingResult -> FilingResultV2 / dict 互換)
        if isinstance(result, dict):
            result["fallback_used"] = True
            result["fallback_reason"] = "no_xbrl_segment_source"
            if not result.get("rule_trace"):
                from src.extractor import get_last_v2_segment_result as _get_v2
                _v2 = _get_v2()
                result["rule_trace"] = _v2.rule_trace if _v2 else []
                result["score_summary"] = _v2.score_summary if _v2 else {}
            # PDF成功: valid_segment_count >= 1 なら status="ok"
            if result.get("valid_segment_count", 0) >= 1:
                result["status"] = "ok"
                result["selected_path"] = "pdf"
                result["via"] = "pdf"
        else:
            setattr(result, "fallback_used", True)
            setattr(result, "fallback_reason", "no_xbrl_segment_source")
            if not getattr(result, "rule_trace", None):
                from src.extractor import get_last_v2_segment_result as _get_v2
                _v2 = _get_v2()
                setattr(result, "rule_trace", _v2.rule_trace if _v2 else [])
                setattr(result, "score_summary", _v2.score_summary if _v2 else {})
            # PDF成功: valid_segment_count >= 1 なら status="ok"
            if getattr(result, "valid_segment_count", 0) >= 1:
                result.status = "ok"
                result.selected_path = "pdf"
                result.via = "pdf"
                if getattr(result, "valid_segment_count", 0) >= 2:
                    result.confidence = max(getattr(result, "confidence", 0.0), 0.7)
        return result
        raw_seg_count = 0
        valid_seg_count = 0
        invalid_seg_count = 0
        sales_nn = 0
        profit_nn = 0
        invalid_names = []
        account_like_ratio = 0.0
        narrative_contamination = False
    elif best.error:
        # XBRL ZIP はあるが抽出エラー or facts なし
        worker_status = "quarantined"
        confidence = 0.0
        reason = f"[tdnet_xbrl] {best.error}"
        # xbrl_no_segment_facts → no_records, それ以外→ xbrl_extraction_error
        if best.error == "xbrl_no_segment_facts":
            fallback_used = True
            fallback_reason = "no_records"
            from lib.backfill.worker import process_one_filing_pdf_only
            result = process_one_filing_pdf_only(filing)
            if isinstance(result, dict):
                result["fallback_used"] = True
                result["fallback_reason"] = "no_records"
                if not result.get("rule_trace"):
                    from src.extractor import get_last_v2_segment_result as _get_v2
                    _v2 = _get_v2()
                    result["rule_trace"] = _v2.rule_trace if _v2 else []
                    result["score_summary"] = _v2.score_summary if _v2 else {}
                # PDF成功: valid_segment_count >= 1 なら status="ok"
                if result.get("valid_segment_count", 0) >= 1:
                    result["status"] = "ok"
                    result["selected_path"] = "pdf"
                    result["via"] = "pdf"
            else:
                setattr(result, "fallback_used", True)
                setattr(result, "fallback_reason", "no_records")
                if not getattr(result, "rule_trace", None):
                    from src.extractor import get_last_v2_segment_result as _get_v2
                    _v2 = _get_v2()
                    setattr(result, "rule_trace", _v2.rule_trace if _v2 else [])
                    setattr(result, "score_summary", _v2.score_summary if _v2 else {})
                # PDF成功: valid_segment_count >= 1 なら status="ok"
                if getattr(result, "valid_segment_count", 0) >= 1:
                    result.status = "ok"
                    result.selected_path = "pdf"
                    result.via = "pdf"
                    if getattr(result, "valid_segment_count", 0) >= 2:
                        result.confidence = max(getattr(result, "confidence", 0.0), 0.7)
            return result
        elif best.error.startswith("period_quarter_unresolved"):
            hard_fail_reason = "xbrl_extraction_error"
        else:
            hard_fail_reason = "xbrl_extraction_error"
        raw_seg_count = 0
        valid_seg_count = 0
        invalid_seg_count = 0
        sales_nn = 0
        profit_nn = 0
        invalid_names = []
        account_like_ratio = 0.0
        narrative_contamination = False
    else:
        # 想定外 fallthrough
        worker_status = "quarantined"
        confidence = 0.0
        reason = "no_extraction_attempted"
        hard_fail_reason = "no_records"
        raw_seg_count = 0
        valid_seg_count = 0
        invalid_seg_count = 0
        sales_nn = 0
        profit_nn = 0
        invalid_names = []
        account_like_ratio = 0.0
        narrative_contamination = False

    quarantine_reason = hard_fail_reason if worker_status == "quarantined" else ""

    # segment_records, fingerprint
    segment_records = best.segment_records
    fp = compute_result_fingerprint(segment_records) if segment_records else None

    if segment_records:
        save_extract_segments_result(paths, segment_records)

    # candidate_summary
    summary_parts = []
    for c in candidates:
        if not c.attempted and not c.available:
            summary_parts.append(f"{c.source}:skip({c.skip_reason or 'not_available'})")
        elif c.validation:
            vs = c.validation.status.value
            vr = c.validation.hard_fail_reason.value
            summary_parts.append(f"{c.source}:{vs}" + (f"({vr})" if vr else ""))
        elif c.error:
            summary_parts.append(f"{c.source}:error({c.error[:50]})")
        else:
            summary_parts.append(f"{c.source}:not_attempted")
    candidate_summary = " → ".join(summary_parts)

    elapsed = int((time.monotonic() - t0) * 1000)
    metrics["total_ms"] = elapsed

    # quarantine dict (互換)
    quarantine_dict = None
    if worker_status == "quarantined":
        quarantine_dict = {
            "filing_id": fid, "ticker": filing.ticker,
            "stage": "segment_extraction_v2",
            "review_hint": quarantine_reason,
            "hard_fail_reason": hard_fail_reason,
            "selected_source": best.source,
            "candidate_summary": candidate_summary,
        }
        save_quarantine(paths, quarantine_dict)

    # route_mode 決定
    if xbrl_resolved and validation:
        route_mode = "xbrl_v2"
    else:
        route_mode = "xbrl_only_no_source"

    # ========================================
    # Step 5: Debug ログ
    # ========================================
    debug_entry = _build_debug_log(
        fid, candidates, best, worker_status, confidence,
        hard_fail_reason, quarantine_reason, fallback_used, fallback_reason,
        valid_seg_count, sales_nn, profit_nn, candidate_summary, route_mode,
    )
    logger.debug(json.dumps(debug_entry, ensure_ascii=False, default=str))

    # filing log
    log_event = "ok" if worker_status in ("ok", "partial") else "quarantined"
    append_filing_log(paths, {
        "event": log_event,
        "via": best.source,
        "status": worker_status,
        "segments": len(segment_records),
        "fingerprint": fp,
        "hard_fail_reason": hard_fail_reason,
        "fallback_used": fallback_used,
        "candidate_summary": candidate_summary,
    })

    # selected_path: XBRL 成功時は "xbrl"、未解決時は "none"
    selected_path = "xbrl" if (xbrl_resolved and validation) else "none"

    result = FilingResultV2(
        filing_id=fid,
        status=worker_status,
        source=best.source if (xbrl_resolved and validation) else "",
        selected_path=selected_path,
        confidence=confidence,
        reason=reason,
        hard_fail_reason=hard_fail_reason,
        quarantine_reason=quarantine_reason,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        raw_segment_count=raw_seg_count,
        valid_segment_count=valid_seg_count,
        invalid_segment_count=invalid_seg_count,
        sales_non_null_count=sales_nn,
        profit_non_null_count=profit_nn,
        invalid_names=invalid_names,
        account_like_ratio=account_like_ratio,
        narrative_contamination=narrative_contamination,
        segment_records=segment_records,
        financial_records=[financials_data] if financials_data else [],
        via=selected_path,
        metrics=metrics,
        cache_paths={"cache_dir": str(paths.cache_dir)},
        quarantine=quarantine_dict,
        result_fingerprint=fp,
        rule_trace=getattr(best, "rule_trace", []),
        score_summary=getattr(best, "score_summary", {}),
        candidates=candidates,
        candidate_summary=candidate_summary,
        route_mode=route_mode,
    )
    return result


# ============================================================
# 候補選択ロジック
# ============================================================

def _select_best_candidate(candidates: list[SourceCandidate]) -> SourceCandidate:
    """STATUS 優先 (SUCCESS > PARTIAL > QUARANTINE)、同格なら confidence > source priority。

    全 QUARANTINE でも常に代表候補を返す (None を返さない)。
    """
    evaluated = [c for c in candidates if c.attempted and c.validation]

    if not evaluated:
        # 全未試行 → 最初に利用可能だった candidate、なければ先頭
        for c in candidates:
            if c.available:
                return c
        return candidates[0]

    def sort_key(c: SourceCandidate):
        v = c.validation
        status_pri = _STATUS_PRIORITY.get(v.status.value, 99)
        source_pri = _SOURCE_PRIORITY.get(c.source, 99)
        return (status_pri, -v.confidence, source_pri)

    evaluated.sort(key=sort_key)
    return evaluated[0]


# ============================================================
# Source 抽出ヘルパー
# ============================================================

def _try_xbrl_source(
    xbrl_path, doc_path, filing, financials_data, fid, paths, metrics, *,
    retry_xbrl, timeout_xbrl, sleep_fn,
) -> SourceCandidate:
    """XBRL セグメント抽出を試行し SourceCandidate を返す。"""
    if not xbrl_path:
        return SourceCandidate(
            source="xbrl", attempted=False, available=False,
            skip_reason="not_available",
        )

    from src.segment.extraction_result_validator import validate_extraction_result

    try:
        from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
        xbrl_rows = extract_segments_from_xbrl_zip(xbrl_path)
    except Exception as e:
        logger.debug(f"[v2] XBRL extraction error: fid={fid} err={e}")
        return SourceCandidate(
            source="xbrl", attempted=True, available=True,
            error=f"xbrl_extraction_error:{str(e)[:150]}",
        )

    if not xbrl_rows:
        return SourceCandidate(
            source="xbrl", attempted=True, available=True,
            error="xbrl_no_segment_facts",
        )

    # period / quarter 解決
    period = (financials_data or {}).get("period", "")
    quarter = (financials_data or {}).get("quarter", "")
    if not period or not quarter:
        from src.year_parser import extract_fiscal_info
        title_fy, title_q = extract_fiscal_info(filing.title)
        if not period and title_fy:
            period = title_fy
        if not quarter and title_q:
            quarter = title_q
    if not period and xbrl_rows[0].period:
        period = xbrl_rows[0].period
    if not quarter and xbrl_rows[0].quarter:
        quarter = xbrl_rows[0].quarter

    if not period or not quarter:
        return SourceCandidate(
            source="xbrl", attempted=True, available=True,
            error=f"period_quarter_unresolved:period={period!r},quarter={quarter!r}",
        )

    # レコード構築
    records = []
    for idx, row in enumerate(xbrl_rows):
        seg_name = row.normalized_segment_name or row.raw_segment_name
        records.append({
            "ticker": filing.ticker,
            "period": row.period or period,   # prior rows は xbrl_rows 側の前期 period を優先
            "quarter": row.quarter or quarter,
            "segment_name": seg_name,
            "segment_order": idx + 1,
            "segment_sales": row.sales,
            "segment_profit": row.profit,
            "raw_profit_label": "",
            "source": "backfill_xbrl",
            "segment_name_norm": _normalize_segment_name_conservative(seg_name),
            "extractor_route": "xbrl",
            "source_doc_type": "earnings_summary",
            "disclosure_date": filing.disclosure_date,
            "tdnet_doc_id": fid,
            "row_type": _classify_row_type(seg_name),
        })

    validation = validate_extraction_result(records, source="xbrl")
    return SourceCandidate(
        source="xbrl", attempted=True, available=True,
        segment_records=records, validation=validation,
    )


def _try_pdf_source(
    doc_path, filing, financials_data, fid, metrics, *,
    retry_pdf, timeout_pdf, sleep_fn,
) -> SourceCandidate:
    """PDF セグメント抽出を試行し SourceCandidate を返す。"""
    if not doc_path:
        return SourceCandidate(
            source="pdf", attempted=False, available=False,
            skip_reason="not_available",
        )

    from src.segment.extraction_result_validator import validate_extraction_result

    _seg_result = _extract_segments(
        doc_path, filing, financials_data, "pdf", fid, metrics,
        retry_pdf=retry_pdf, timeout_pdf=timeout_pdf, sleep_fn=sleep_fn,
    )
    segment_records = _seg_result[0] if len(_seg_result) > 0 else []
    seg_err        = _seg_result[1] if len(_seg_result) > 1 else ""
    rule_trace     = _seg_result[2] if len(_seg_result) > 2 else []
    score_summary  = _seg_result[3] if len(_seg_result) > 3 else {}

    if not segment_records:
        return SourceCandidate(
            source="pdf", attempted=True, available=True,
            error=seg_err or "pdf_no_segments",
            rule_trace=rule_trace,
            score_summary=score_summary,
        )

    validation = validate_extraction_result(segment_records, source="pdf_compat")
    return SourceCandidate(
        source="pdf", attempted=True, available=True,
        segment_records=segment_records, validation=validation,
        rule_trace=rule_trace,
        score_summary=score_summary,
    )


# ============================================================
# Debug ログビルダー
# ============================================================

def _build_debug_log(
    fid, candidates, best, worker_status, confidence,
    hard_fail_reason, quarantine_reason, fallback_used, fallback_reason,
    valid_seg_count, sales_nn, profit_nn, candidate_summary, route_mode="",
) -> dict:
    """比較しやすいフラットなキーの debug ログ dict を構築。"""
    entry = {
        "event": "filing_v2_result",
        "filing_id": fid,
    }
    for c in candidates:
        prefix = c.source
        entry[f"{prefix}_attempted"] = c.attempted
        entry[f"{prefix}_available"] = c.available
        entry[f"{prefix}_skip_reason"] = c.skip_reason
        if c.validation:
            entry[f"{prefix}_validator_status"] = c.validation.status.value
            entry[f"{prefix}_validator_reason"] = c.validation.hard_fail_reason.value
            entry[f"{prefix}_confidence"] = c.validation.confidence
        elif c.error:
            entry[f"{prefix}_validator_status"] = "error"
            entry[f"{prefix}_validator_reason"] = c.error[:100]
        else:
            entry[f"{prefix}_validator_status"] = "not_attempted"
            entry[f"{prefix}_validator_reason"] = c.skip_reason

    entry.update({
        "selected_source": best.source,
        "selected_status": worker_status,
        "selected_confidence": confidence,
        "hard_fail_reason": hard_fail_reason,
        "valid_segment_count": valid_seg_count,
        "sales_non_null_count": sales_nn,
        "profit_non_null_count": profit_nn,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "quarantine_reason": quarantine_reason,
        "candidate_summary": candidate_summary,
        "route_mode": route_mode,
    })
    return entry
