"""lib/backfill/worker.py — filing 単位の処理ワーカー (Step 4: Phase 2 対応)

3 つのエントリポイント:
  - process_one_filing()          Phase 1 互換 (download→xbrl→pdf→結果)
  - process_one_filing_xbrl_first()  Phase 2 Stage B (download→xbrl→ok or needs_pdf)
  - process_one_filing_pdf_only()    Phase 2 Stage C (cache再利用→pdf→ok or quarantined)

DB 書き込みは **禁止** — main スレッドが batch upsert する。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("backfill.worker")


@dataclass
class FilingResult:
    """process_one_filing の返り値。"""
    filing_id: str
    status: str                     # "ok" | "quarantined" | "failed" | "needs_pdf"
    via: str | None = None          # "xbrl" | "pdf" | None
    segment_records: list = field(default_factory=list)
    financial_records: list = field(default_factory=list)
    quarantine: dict | None = None
    metrics: dict = field(default_factory=dict)
    cache_paths: dict = field(default_factory=dict)
    result_fingerprint: str | None = None
    # Phase 2: Trace & Scores
    rule_trace: list[str] = field(default_factory=list)
    score_summary: dict[str, Any] = field(default_factory=dict)
    quarantine_reason: str = ""
    selected_path: str = "unknown"


def compute_result_fingerprint(segment_records: list[dict]) -> str:
    """segment_records を安定ソートして SHA1[:24] で fingerprint を生成。"""
    if not segment_records:
        return "empty"
    sorted_recs = sorted(
        segment_records,
        key=lambda r: (
            r.get("ticker", ""),
            r.get("period", ""),
            r.get("quarter", ""),
            r.get("segment_name", ""),
        ),
    )
    serialized = json.dumps(sorted_recs, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()[:24]


# ================================================================
# 共通ヘルパー (内部)
# ================================================================

def _ensure_imports():
    """src/ を path に追加して import 可能にする。"""
    import sys
    project_root = str(Path(__file__).resolve().parent.parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


def _download_originals(filing, paths, metrics, *, retry_download, timeout_download, sleep_fn):
    """原本 (PDF/XBRL) をダウンロードする。cache があれば skip。"""
    from lib.backfill.cache import has_pdf, has_xbrl
    from lib.backfill.retry import retry_with_backoff

    doc_path = None
    xbrl_path = None

    # PDF
    t_dl = time.monotonic()
    if has_pdf(paths):
        doc_path = str(paths.source_pdf)
        metrics["pdf_cache_hit"] = True
    elif filing.doc_url:
        def _dl_pdf():
            from src.downloader import download_document
            p = download_document(filing.doc_url, str(paths.cache_dir))
            if not p:
                raise RuntimeError("download returned None")
            dl = Path(p)
            if dl != paths.source_pdf and dl.exists():
                shutil.copy2(dl, paths.source_pdf)
                dl.unlink(missing_ok=True)
            return str(paths.source_pdf)

        r = retry_with_backoff(
            _dl_pdf, stage="download",
            max_attempts=retry_download, timeout_sec=timeout_download,
            sleep_fn=sleep_fn,
        )
        metrics["attempts"]["download"] = r.attempts
        if r.success:
            doc_path = r.value
            metrics["pdf_cache_hit"] = False
        else:
            metrics["download_error"] = r.last_error
            metrics["download_timed_out"] = r.timed_out
    metrics["download_ms"] = int((time.monotonic() - t_dl) * 1000)

    # XBRL — 解決順: cache → archive → download (inferred URL 含む)
    from lib.backfill.cache import resolve_xbrl_from_archive
    xbrl_source = "none"
    xbrl_archive_hit = False
    xbrl_download_error_class = ""
    xbrl_url_inferred = getattr(filing, "xbrl_url_inferred", False)

    # Step 1: cache or archive
    resolved_path, resolved_source = resolve_xbrl_from_archive(
        filing.ticker, paths,
    )
    if resolved_path:
        xbrl_path = resolved_path
        xbrl_source = resolved_source
        xbrl_archive_hit = (resolved_source == "archive")
        if resolved_source == "cache":
            metrics["xbrl_cache_hit"] = True
    # Step 2: download (archive miss 時に xbrl_url があれば試行)
    elif filing.xbrl_url:
        def _dl_xbrl():
            from src.downloader import download_document_ex
            r = download_document_ex(filing.xbrl_url, str(paths.cache_dir))
            if not r.success:
                raise RuntimeError(f"xbrl_download_{r.error_class}")
            dl = Path(r.path)
            if dl != paths.xbrl_zip and dl.exists():
                shutil.copy2(dl, paths.xbrl_zip)
                dl.unlink(missing_ok=True)
            return str(paths.xbrl_zip)

        r = retry_with_backoff(
            _dl_xbrl, stage="download",
            max_attempts=retry_download, timeout_sec=timeout_download,
            sleep_fn=sleep_fn,
        )
        metrics["attempts"]["xbrl_download"] = r.attempts
        if r.success:
            xbrl_path = r.value
            xbrl_source = "download"
            metrics["xbrl_cache_hit"] = False
        else:
            # HTTP エラー分類を抽出
            err_str = str(r.last_error) if r.last_error else ""
            if "xbrl_download_" in err_str:
                xbrl_download_error_class = err_str.replace("xbrl_download_", "")
            else:
                xbrl_download_error_class = "unknown"

    metrics["xbrl_source"] = xbrl_source
    metrics["xbrl_archive_hit"] = xbrl_archive_hit
    metrics["xbrl_resolved"] = xbrl_path is not None
    metrics["xbrl_download_attempted"] = "xbrl_download" in metrics.get("attempts", {})
    metrics["xbrl_url_inferred"] = xbrl_url_inferred
    metrics["xbrl_download_error_class"] = xbrl_download_error_class

    return doc_path, xbrl_path


def _extract_financials_data(doc_path, xbrl_path, filing, metrics, *, retry_xbrl, retry_pdf, timeout_xbrl, timeout_pdf, sleep_fn):
    """PL 抽出。(financials_data dict, via str) を返す。"""
    from lib.backfill.retry import retry_with_backoff

    def _extract():
        from src.extractor import extract_financials
        result, err = extract_financials(doc_path, filing.title, xbrl_path)
        if not result:
            raise RuntimeError(err or "extract_financials returned None")
        return result

    t = time.monotonic()
    r = retry_with_backoff(
        _extract,
        stage="xbrl" if xbrl_path else "pdf",
        max_attempts=retry_xbrl if xbrl_path else retry_pdf,
        timeout_sec=timeout_xbrl if xbrl_path else timeout_pdf,
        sleep_fn=sleep_fn,
    )
    metrics["attempts"]["financials"] = r.attempts
    metrics["extract_ms"] = int((time.monotonic() - t) * 1000)

    if not r.success:
        return None, None

    result = r.value
    # via 判定: xbrl_path 有 → xbrl, result.source に xbrl/ixbrl → xbrl, それ以外 → pdf
    result_source = (getattr(result, "source", "") or "").lower()
    if xbrl_path:
        via = "xbrl"
    elif "xbrl" in result_source or "ixbrl" in result_source:
        via = "xbrl"
    else:
        via = "pdf"
    data = {
        "period": result.fiscal_year,
        "quarter": result.quarter,
        "sales": result.sales,
        "operating_profit": result.operating_profit,
        "gross_profit": getattr(result, "gross_profit", None),
        "cost_of_sales": getattr(result, "cost_of_sales", None),
        "source": getattr(result, "source", "unknown"),
    }
    return data, via


def _normalize_segment_name_conservative(name: str) -> str:
    """segment_name の conservative 正規化 (仕様書 §9).

    - 前後空白 trim
    - 全角空白 → 半角空白
    - 連続空白 → 1つ
    - 改行 → 空白
    - タブ → 空白
    - 空文字なら元名 fallback
    """
    import unicodedata
    s = name.strip()
    s = s.replace("\u3000", " ")
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = unicodedata.normalize("NFKC", s)
    s = " ".join(s.split())  # 連続空白を1つに
    return s if s else name


def _classify_row_type(segment_name: str) -> str | None:
    """segment_name から row_type を簡易判定 (仕様書 §10).

    判定できない場合は None (NULL 許容)。
    """
    n = segment_name.strip()
    # adjustment
    if "調整額" in n or "調整" in n:
        return "adjustment"
    # total
    if n in ("合計", "計") or n.endswith("合計") or n.endswith("計"):
        return "total"
    # company
    if "全社" in n or "消去又は全社" in n:
        return "company"
    # segment (default for non-empty names)
    return "segment"


def _extract_segments(doc_path, filing, financials_data, via, fid, metrics, *, retry_pdf, timeout_pdf, sleep_fn):
    """セグメント抽出。(records list, error str) を返す。"""
    from lib.backfill.retry import retry_with_backoff

    def _extract():
        from src.extractor import extract_segment_financials, get_last_v2_segment_result
        segments, seg_err = extract_segment_financials(
            doc_path, filing.title, doc_id=fid, ticker=filing.ticker,
        )
        # trace / scores capture
        v2_res = get_last_v2_segment_result()
        _extract.rule_trace = v2_res.rule_trace if v2_res else []
        _extract.score_summary = v2_res.score_summary if v2_res else {}

        if not segments:
            raise RuntimeError(seg_err or "no_segments_found")
        return segments

    t = time.monotonic()
    _extract.rule_trace = []
    _extract.score_summary = {}
    r = retry_with_backoff(
        _extract, stage="pdf",
        max_attempts=retry_pdf, timeout_sec=timeout_pdf,
        sleep_fn=sleep_fn,
    )
    metrics["attempts"]["segments"] = r.attempts
    metrics["segment_ms"] = int((time.monotonic() - t) * 1000)

    if not r.success:
        return [], r.last_error or "segment_extraction_failed", getattr(_extract, "rule_trace", []), getattr(_extract, "score_summary", {})

    period = (financials_data or {}).get("period", "")
    quarter = (financials_data or {}).get("quarter", "")

    # --- period / quarter フォールバック: 古い判定は無効化 ---
    # period / quarter 空保存禁止 → quarantine
    if not period or not quarter:
        return [], f"period_quarter_unresolved:period={period!r},quarter={quarter!r}"


    # extractor_route 判定: via を使い、フォールバックで source を確認
    extractor_route = via or "pdf_table"

    records = []
    for seg in r.value:
        seg_name = seg.segment_name
        records.append({
            "ticker": filing.ticker,
            "period": period,
            "quarter": quarter,
            "segment_name": seg_name,
            "segment_order": seg.segment_order,
            "segment_sales": seg.segment_sales,
            "segment_profit": seg.segment_profit,
            "raw_profit_label": getattr(seg, "raw_profit_label", ""),
            "source": f"backfill_{via or 'pdf'}",
            # 新カラム
            "segment_name_norm": _normalize_segment_name_conservative(seg_name),
            "extractor_route": extractor_route,
            "source_doc_type": "earnings_summary",
            "disclosure_date": filing.disclosure_date,
            "tdnet_doc_id": fid,
            "row_type": _classify_row_type(seg_name),
            "rule_trace": getattr(seg, "rule_trace", []),
            "score_summary": getattr(seg, "score_summary", {}),
        })
    return records, "", getattr(_extract, "rule_trace", []), getattr(_extract, "score_summary", {})


# ================================================================
# Phase 1 互換: process_one_filing
# ================================================================

def process_one_filing(
    filing, *,
    cache_root: str = "data/tdnet_cache",
    state_store=None,
    skip_pdf: bool = False,
    only_xbrl: bool = False,
    retry_download: int = 3, retry_xbrl: int = 2, retry_pdf: int = 1,
    timeout_download: int = 30, timeout_xbrl: int = 60, timeout_pdf: int = 120,
    run_id: str | None = None,
    sleep_fn=None,
) -> FilingResult:
    """Phase 1 互換: download → extract → segments → 結果。"""
    _ensure_imports()
    from lib.backfill.cache import (
        ensure_cache_layout, write_metadata,
        save_extract_financials_result, save_extract_segments_result,
        save_quarantine, append_filing_log,
    )
    from lib.backfill.retry import classify_review_hint
    import time as _time
    _sleep = sleep_fn or _time.sleep

    t0 = time.monotonic()
    fid = filing.filing_id
    metrics: dict = {"attempts": {}}
    paths = ensure_cache_layout(cache_root, fid)
    write_metadata(paths, filing)

    _update_state(state_store, fid, "running", stage="downloading")
    append_filing_log(paths, {"event": "start", "ticker": filing.ticker, "run_id": run_id})

    doc_path, xbrl_path = _download_originals(
        filing, paths, metrics,
        retry_download=retry_download, timeout_download=timeout_download, sleep_fn=_sleep,
    )

    if not doc_path and not xbrl_path:
        elapsed = int((time.monotonic() - t0) * 1000)
        hint = classify_review_hint("download", metrics.get("download_error", "no_url"), metrics.get("download_timed_out", False))
        append_filing_log(paths, {"event": "failed", "review_hint": hint})
        return FilingResult(filing_id=fid, status="failed", quarantine={"review_hint": hint, "filing_id": fid}, metrics={**metrics, "total_ms": elapsed}, cache_paths={"cache_dir": str(paths.cache_dir)})

    _update_state(state_store, fid, "running", stage="extracting")
    financials_data, via = _extract_financials_data(
        doc_path, xbrl_path, filing, metrics,
        retry_xbrl=retry_xbrl, retry_pdf=retry_pdf, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf, sleep_fn=_sleep,
    )
    # PL 抽出失敗時でも via を判定 (segment 抽出の extractor_route 用)
    if via is None:
        via = "xbrl" if xbrl_path else "pdf"
    if financials_data:
        save_extract_financials_result(paths, financials_data)

    _update_state(state_store, fid, "running", stage="extracting_segments")
    if doc_path:
        _seg_result = _extract_segments(
            doc_path, filing, financials_data, via, fid, metrics,
            retry_pdf=retry_pdf, timeout_pdf=timeout_pdf, sleep_fn=_sleep,
        )
    else:
        _seg_result = ([], "no_doc_path", [], {})
    segment_records = _seg_result[0] if len(_seg_result) > 0 else []
    seg_err        = _seg_result[1] if len(_seg_result) > 1 else ""
    rule_trace     = _seg_result[2] if len(_seg_result) > 2 else []
    score_summary  = _seg_result[3] if len(_seg_result) > 3 else {}

    if segment_records:
        save_extract_segments_result(paths, segment_records)

    elapsed = int((time.monotonic() - t0) * 1000)
    metrics["total_ms"] = elapsed
    fp = compute_result_fingerprint(segment_records) if segment_records else None

    if segment_records:
        append_filing_log(paths, {"event": "ok", "via": via, "segments": len(segment_records), "fingerprint": fp, "attempts": metrics.get("attempts", {})})
        return FilingResult(filing_id=fid, status="ok", via=via, segment_records=segment_records, financial_records=[financials_data] if financials_data else [], metrics=metrics, cache_paths={"cache_dir": str(paths.cache_dir)}, result_fingerprint=fp, rule_trace=rule_trace, score_summary=score_summary)

    reason = seg_err or "unknown"
    v2_reason = None
    v1_reason = None
    if reason.startswith("v2_reason:"):
        v2_part, _, reason_rest = reason.partition("|")
        v2_reason = v2_part.replace("v2_reason:", "")
        v1_reason = reason_rest or None
        reason = reason_rest or reason
    else:
        v1_reason = reason

    # --- reason 優先順位: v2/v1 のうち分析価値の高い方を採用 ---
    try:
        from src.analysis.row_classifier import choose_better_reason, map_reject_reason_to_review_hint
        best_reason = choose_better_reason(v2_reason, v1_reason)
        # 具体的な reason がある場合はそこから hint を生成
        if best_reason and ("candidate_guard:" in best_reason or "picked_pl_table" in best_reason or "period_quarter" in best_reason):
            hint = map_reject_reason_to_review_hint(best_reason)
        else:
            hint = classify_review_hint("pdf" if doc_path else "download", reason, False, v2_reason=v2_reason)
    except ImportError:
        best_reason = v2_reason or v1_reason
        hint = classify_review_hint("pdf" if doc_path else "download", reason, False, v2_reason=v2_reason)

    # ================================================================
    # XBRL Fallback: PDF で表が無い場合に XBRL からセグメントを救済
    # ================================================================
    _XBRL_FALLBACK_HINTS = {
        "pdf_no_segment_narrative_page",
        "pdf_no_segment_table_after_guard",
        "pdf_no_segment_page_candidate",
        "pdf_no_segment_table_candidate",
    }

    # EDINET は法定開示書類 (有価証券報告書・四半期報告書) の XBRL 取得用。
    # 決算短信は TDnet 固有の適時開示であり EDINET には格納されない。
    # よって financial_statement / forecast_revision では EDINET resolve をスキップする。
    _EDINET_APPLICABLE_DOC_TYPES = {"securities_report", "quarterly_report"}

    xbrl_fallback_attempted = False
    xbrl_fallback_succeeded = False
    rescued_from_hint = None
    xbrl_failure_reason = None
    fallback_source = None

    # EDINET metadata
    edinet_api_key_present = False
    edinet_resolve_attempted = False
    edinet_resolve_succeeded = False
    edinet_resolve_skipped_reason = ""
    edinet_skipped_not_applicable = False
    edinet_skip_reason = ""
    edinet_doc_id = ""
    edinet_match_score = 0.0
    edinet_match_basis = ""
    edinet_candidate_count = 0
    edinet_top1_doc_id = ""
    edinet_top1_score = 0.0
    edinet_top2_score = 0.0
    edinet_selected_reason = ""
    edinet_fail_reason = ""
    edinet_ticker_match_count = 0
    edinet_window_stats = {}
    edinet_download_attempted = False
    edinet_download_succeeded = False
    edinet_cache_hit = False

    if hint in _XBRL_FALLBACK_HINTS:
        rescued_from_hint = hint
        # --- Step 1: TDnet XBRL cache ---
        effective_xbrl_path = xbrl_path  # from TDnet download

        # --- Step 2: EDINET (doc_type 判定でスキップ判断) ---
        if not effective_xbrl_path:
            filing_doc_type = getattr(filing, "doc_type", "financial_statement")

            if filing_doc_type not in _EDINET_APPLICABLE_DOC_TYPES:
                # 決算短信等は EDINET 非対象 — 明示的に skip
                edinet_skipped_not_applicable = True
                if filing_doc_type == "financial_statement":
                    edinet_skip_reason = "edinet_not_applicable_financial_statement"
                elif filing_doc_type == "forecast_revision":
                    edinet_skip_reason = "edinet_not_applicable_forecast_revision"
                else:
                    edinet_skip_reason = f"edinet_not_applicable_other_doc_type"
                xbrl_failure_reason = edinet_skip_reason
                logger.info(
                    f"[worker] EDINET skip: fid={fid} doc_type={filing_doc_type} "
                    f"reason={edinet_skip_reason}"
                )
            else:
                # 法定開示書類 — EDINET resolve を試行
                try:
                    from .edinet_xbrl_cache import EdinetXbrlCache
                    from .edinet_client import EdinetClient
                    _edinet = EdinetClient()
                    edinet_api_key_present = _edinet.has_api_key

                    resolve_result = _edinet.resolve_document(
                        ticker=filing.ticker,
                        disclosure_date=filing.disclosure_date,
                        title=filing.title,
                        doc_type=filing_doc_type,
                    )
                    edinet_resolve_attempted = resolve_result.attempted
                    edinet_resolve_succeeded = resolve_result.succeeded
                    edinet_resolve_skipped_reason = resolve_result.skipped_reason
                    edinet_doc_id = resolve_result.doc_id
                    edinet_match_score = resolve_result.match_score
                    edinet_match_basis = resolve_result.match_basis
                    edinet_candidate_count = resolve_result.candidate_count
                    edinet_top1_doc_id = resolve_result.top1_doc_id
                    edinet_top1_score = resolve_result.top1_score
                    edinet_top2_score = resolve_result.top2_score
                    edinet_selected_reason = resolve_result.selected_reason
                    edinet_fail_reason = getattr(resolve_result, "fail_reason", "")
                    edinet_ticker_match_count = getattr(resolve_result, "ticker_match_count", 0)
                    edinet_window_stats = getattr(resolve_result, "window_stats", {})

                    if resolve_result.succeeded and resolve_result.doc_id:
                        dl_result = _edinet.download_xbrl_zip(resolve_result.doc_id)
                        edinet_download_attempted = dl_result.attempted
                        edinet_download_succeeded = dl_result.succeeded
                        edinet_cache_hit = dl_result.cache_hit

                        if dl_result.succeeded and dl_result.cache_path:
                            effective_xbrl_path = dl_result.cache_path
                            fallback_source = "xbrl_edinet"
                            logger.info(
                                f"[worker] EDINET XBRL source: fid={fid} "
                                f"doc_id={resolve_result.doc_id} "
                                f"cache_hit={dl_result.cache_hit}"
                            )
                        else:
                            xbrl_failure_reason = dl_result.failure_reason or dl_result.skipped_reason or "edinet_download_failed"
                    elif resolve_result.skipped:
                        xbrl_failure_reason = resolve_result.skipped_reason
                    else:
                        xbrl_failure_reason = f"edinet_resolve_failed(score={resolve_result.match_score:.2f},fail={edinet_fail_reason})"

                except Exception as e:
                    logger.warning(f"[worker] EDINET fallback error: fid={fid} {e}")
                    xbrl_failure_reason = f"edinet_error:{str(e)[:150]}"

        if effective_xbrl_path:
            xbrl_fallback_attempted = True
            if not fallback_source:
                fallback_source = "xbrl_tdnet"
            logger.info(f"[worker] XBRL fallback: fid={fid} ticker={filing.ticker} hint={hint} source={fallback_source}")
            try:
                from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
                xbrl_rows = extract_segments_from_xbrl_zip(effective_xbrl_path, title=getattr(filing, "title", None))
                if xbrl_rows and len(xbrl_rows) > 0:
                    # period / quarter 解決
                    period = (financials_data or {}).get("period", "")
                    quarter = (financials_data or {}).get("quarter", "")
                    # period を XBRL row から取得 (fallback)
                    if not period and xbrl_rows[0].period:
                        period = xbrl_rows[0].period
                    if not quarter and xbrl_rows[0].quarter:
                        quarter = xbrl_rows[0].quarter

                    if period and quarter and quarter != "UNKNOWN":
                        xbrl_segment_records = []
                        for idx, row in enumerate(xbrl_rows):
                            seg_name = row.normalized_segment_name or row.raw_segment_name
                            xbrl_segment_records.append({
                                "ticker": filing.ticker,
                                "period": period,
                                "quarter": quarter,
                                "segment_name": seg_name,
                                "segment_order": idx + 1,
                                "segment_sales": row.sales,
                                "segment_profit": row.profit,
                                "raw_profit_label": "",
                                "source": "backfill_xbrl_fallback",
                                "segment_name_norm": _normalize_segment_name_conservative(seg_name),
                                "extractor_route": f"xbrl_fallback_{fallback_source}",
                                "source_doc_type": "earnings_summary",
                                "disclosure_date": filing.disclosure_date,
                                "tdnet_doc_id": fid,
                                "row_type": _classify_row_type(seg_name),
                            })
                        if xbrl_segment_records:
                            xbrl_fallback_succeeded = True
                            save_extract_segments_result(paths, xbrl_segment_records)
                            fp = compute_result_fingerprint(xbrl_segment_records)
                            metrics["xbrl_fallback_attempted"] = True
                            metrics["xbrl_fallback_succeeded"] = True
                            metrics["rescued_from_hint"] = rescued_from_hint
                            metrics["fallback_source"] = fallback_source
                            metrics["edinet_doc_id"] = edinet_doc_id
                            metrics["edinet_api_key_present"] = edinet_api_key_present
                            metrics["total_ms"] = int((time.monotonic() - t0) * 1000)
                            append_filing_log(paths, {
                                "event": "ok", "via": "xbrl_fallback",
                                "segments": len(xbrl_segment_records),
                                "fingerprint": fp,
                                "rescued_from_hint": rescued_from_hint,
                                "fallback_source": fallback_source,
                            })
                            logger.info(f"[worker] XBRL fallback SUCCESS: fid={fid} segments={len(xbrl_segment_records)} source={fallback_source}")
                            return FilingResult(
                                filing_id=fid, status="ok", via="xbrl_fallback",
                                segment_records=xbrl_segment_records,
                                financial_records=[financials_data] if financials_data else [],
                                metrics=metrics,
                                cache_paths={"cache_dir": str(paths.cache_dir)},
                                result_fingerprint=fp,
                            )
                    else:
                        xbrl_failure_reason = f"period_quarter_unresolved:{period!r},{quarter!r}"
                else:
                    xbrl_failure_reason = "xbrl_no_segment_facts"
            except Exception as e:
                xbrl_failure_reason = str(e)[:200]
                logger.info(f"[worker] XBRL fallback failed: fid={fid} err={xbrl_failure_reason}")

    metrics["xbrl_fallback_attempted"] = xbrl_fallback_attempted
    metrics["xbrl_fallback_succeeded"] = xbrl_fallback_succeeded
    metrics["edinet_api_key_present"] = edinet_api_key_present
    metrics["edinet_resolve_attempted"] = edinet_resolve_attempted
    metrics["edinet_resolve_succeeded"] = edinet_resolve_succeeded
    metrics["edinet_resolve_skipped_reason"] = edinet_resolve_skipped_reason
    metrics["edinet_skipped_not_applicable"] = edinet_skipped_not_applicable
    metrics["edinet_skip_reason"] = edinet_skip_reason
    metrics["edinet_doc_id"] = edinet_doc_id
    metrics["edinet_match_score"] = edinet_match_score
    metrics["edinet_match_basis"] = edinet_match_basis
    metrics["edinet_cache_hit"] = edinet_cache_hit
    metrics["edinet_candidate_count"] = edinet_candidate_count
    metrics["edinet_top1_doc_id"] = edinet_top1_doc_id
    metrics["edinet_top1_score"] = edinet_top1_score
    metrics["edinet_top2_score"] = edinet_top2_score
    metrics["edinet_selected_reason"] = edinet_selected_reason
    metrics["edinet_fail_reason"] = edinet_fail_reason
    metrics["edinet_ticker_match_count"] = edinet_ticker_match_count
    metrics["edinet_window_stats"] = edinet_window_stats
    metrics["fallback_source"] = fallback_source or ""
    if xbrl_failure_reason:
        metrics["xbrl_failure_reason"] = xbrl_failure_reason
    if rescued_from_hint:
        metrics["rescued_from_hint"] = rescued_from_hint

    q = {
        "filing_id": fid, "ticker": filing.ticker,
        "stage": "segment_extraction",
        "error_message": reason[:500],
        "review_hint": hint,
        "via": via,
        "candidate_reject_reason": best_reason or "",
        "detector_v2_reason": v2_reason or "",
        "detector_v1_reason": v1_reason or "",
        "xbrl_fallback_attempted": xbrl_fallback_attempted,
        "xbrl_fallback_succeeded": xbrl_fallback_succeeded,
        "xbrl_failure_reason": xbrl_failure_reason or "",
    }
    save_quarantine(paths, q)
    append_filing_log(paths, {
        "event": "quarantined",
        "reason": reason[:200],
        "review_hint": hint,
        "candidate_reject_reason": best_reason or "",
        "xbrl_fallback_attempted": xbrl_fallback_attempted,
    })
    return FilingResult(filing_id=fid, status="quarantined", via=via, quarantine=q, financial_records=[financials_data] if financials_data else [], metrics=metrics, cache_paths={"cache_dir": str(paths.cache_dir)}, rule_trace=rule_trace, score_summary=score_summary)


# ================================================================
# Phase 2 Stage B: XBRL-first
# ================================================================

def process_one_filing_xbrl_first(
    filing, *,
    cache_root: str = "data/tdnet_cache",
    state_store=None,
    retry_download: int = 3, retry_xbrl: int = 2,
    timeout_download: int = 30, timeout_xbrl: int = 60,
    run_id: str | None = None,
    sleep_fn=None,
) -> FilingResult:
    """Phase 2 Stage B: download → XBRL extraction → ok or needs_pdf。

    XBRL で segment が取れない場合は needs_pdf を返す (failed ではない)。
    """
    _ensure_imports()
    from lib.backfill.cache import (
        ensure_cache_layout, write_metadata,
        save_extract_financials_result, save_extract_segments_result,
        append_filing_log,
    )
    from lib.backfill.retry import retry_with_backoff, classify_review_hint
    import time as _time
    _sleep = sleep_fn or _time.sleep

    t0 = time.monotonic()
    fid = filing.filing_id
    metrics: dict = {"attempts": {}, "stage": "xbrl_first"}
    paths = ensure_cache_layout(cache_root, fid)
    write_metadata(paths, filing)

    _update_state(state_store, fid, "running", stage="downloading")
    append_filing_log(paths, {"event": "xbrl_first_start", "ticker": filing.ticker, "run_id": run_id})

    # download
    doc_path, xbrl_path = _download_originals(
        filing, paths, metrics,
        retry_download=retry_download, timeout_download=timeout_download, sleep_fn=_sleep,
    )

    if not doc_path and not xbrl_path:
        elapsed = int((time.monotonic() - t0) * 1000)
        hint = classify_review_hint("download", metrics.get("download_error", "no_url"), metrics.get("download_timed_out", False))
        append_filing_log(paths, {"event": "failed", "review_hint": hint})
        return FilingResult(filing_id=fid, status="failed", quarantine={"review_hint": hint, "filing_id": fid}, metrics={**metrics, "total_ms": elapsed}, cache_paths={"cache_dir": str(paths.cache_dir)})

    # XBRL PL extraction
    _update_state(state_store, fid, "running", stage="extracting_xbrl")
    financials_data, via = _extract_financials_data(
        doc_path, xbrl_path, filing, metrics,
        retry_xbrl=retry_xbrl, retry_pdf=1, timeout_xbrl=timeout_xbrl, timeout_pdf=120, sleep_fn=_sleep,
    )
    if financials_data:
        save_extract_financials_result(paths, financials_data)

    # XBRL segment extraction (XBRL 由来のみ試みる)
    segment_records = []
    xbrl_seg_hint = ""
    if xbrl_path and doc_path:
        # XBRL で segment が取れるか試行
        def _xbrl_segments():
            from src.extractor import extract_segment_financials
            segments, err = extract_segment_financials(
                doc_path, filing.title, doc_id=fid, ticker=filing.ticker,
            )
            if not segments:
                raise RuntimeError(err or "xbrl_no_segments")
            return segments

        t_seg = time.monotonic()
        seg_r = retry_with_backoff(
            _xbrl_segments, stage="xbrl",
            max_attempts=retry_xbrl, timeout_sec=timeout_xbrl,
            sleep_fn=_sleep,
        )
        metrics["attempts"]["xbrl_segments"] = seg_r.attempts
        metrics["xbrl_segment_ms"] = int((time.monotonic() - t_seg) * 1000)

        if seg_r.success:
            period = (financials_data or {}).get("period", "")
            quarter = (financials_data or {}).get("quarter", "")
            # --- period / quarter フォールバック: 古い判定は無効化 ---
            # period / quarter 空またはUNKNOWN保存禁止
            if not period or not quarter or quarter == "UNKNOWN":
                segment_records = []
                xbrl_seg_hint = f"period_quarter_unresolved:period={period!r},quarter={quarter!r}"
            else:
                for seg in seg_r.value:
                    seg_name = seg.segment_name
                    segment_records.append({
                        "ticker": filing.ticker,
                        "period": period,
                        "quarter": quarter,
                        "segment_name": seg_name,
                        "segment_order": seg.segment_order,
                        "segment_sales": seg.segment_sales,
                        "segment_profit": seg.segment_profit,
                        "raw_profit_label": getattr(seg, "raw_profit_label", ""),
                        "source": "backfill_xbrl",
                        # 新カラム
                        "segment_name_norm": _normalize_segment_name_conservative(seg_name),
                        "extractor_route": "xbrl",
                        "source_doc_type": "earnings_summary",
                        "disclosure_date": filing.disclosure_date,
                        "tdnet_doc_id": fid,
                        "row_type": _classify_row_type(seg_name),
                    })
        else:
            # XBRL segment 失敗 — review_hint を細分化
            err_msg = str(seg_r.last_error) if seg_r.last_error else "unknown"
            if seg_r.timed_out:
                xbrl_seg_hint = "xbrl_segment_timeout"
            elif "xbrl_no_segments" in err_msg:
                xbrl_seg_hint = "xbrl_no_segment_facts"
            elif "テキスト抽出不可" in err_msg:
                xbrl_seg_hint = "xbrl_pdf_text_empty"
            else:
                xbrl_seg_hint = "xbrl_segment_parse_failed"
            metrics["xbrl_seg_error"] = err_msg
            logger.debug(f"[worker] xbrl segment failed: fid={fid} hint={xbrl_seg_hint} err={err_msg}")
    elif not xbrl_path:
        xbrl_seg_hint = "xbrl_zip_not_available"
    elif not doc_path:
        xbrl_seg_hint = "xbrl_doc_not_available"

    elapsed = int((time.monotonic() - t0) * 1000)
    metrics["total_ms"] = elapsed

    if segment_records:
        save_extract_segments_result(paths, segment_records)
        fp = compute_result_fingerprint(segment_records)
        append_filing_log(paths, {"event": "ok", "via": "xbrl", "segments": len(segment_records), "fingerprint": fp})
        return FilingResult(
            filing_id=fid, status="ok", via="xbrl",
            segment_records=segment_records,
            financial_records=[financials_data] if financials_data else [],
            metrics=metrics,
            cache_paths={"cache_dir": str(paths.cache_dir)},
            result_fingerprint=fp,
        )

    # → needs_pdf
    hint = xbrl_seg_hint or ("xbrl_missing_segment_data" if xbrl_path else "xbrl_missing")
    append_filing_log(paths, {"event": "needs_pdf", "review_hint": hint})
    return FilingResult(
        filing_id=fid, status="needs_pdf", via=None,
        financial_records=[financials_data] if financials_data else [],
        quarantine={"review_hint": hint, "filing_id": fid, "stage": "needs_pdf"},
        metrics=metrics,
        cache_paths={"cache_dir": str(paths.cache_dir)},
    )


# ================================================================
# Phase 2 Stage C: PDF-only
# ================================================================

def process_one_filing_pdf_only(
    filing, *,
    cache_root: str = "data/tdnet_cache",
    state_store=None,
    retry_pdf: int = 1,
    timeout_pdf: int = 120,
    run_id: str | None = None,
    sleep_fn=None,
    financials_data: dict | None = None,
) -> FilingResult:
    """Phase 2 Stage C: cache 再利用 → PDF segment 抽出。

    xbrl_first で needs_pdf になった filing が対象。
    cache にある原本 PDF を再利用する。
    """
    _ensure_imports()
    from lib.backfill.cache import (
        ensure_cache_layout, has_pdf,
        save_extract_segments_result, save_quarantine, append_filing_log,
    )
    from lib.backfill.retry import classify_review_hint
    import time as _time
    _sleep = sleep_fn or _time.sleep

    t0 = time.monotonic()
    fid = filing.filing_id
    metrics: dict = {"attempts": {}, "stage": "pdf_only"}
    paths = ensure_cache_layout(cache_root, fid)

    _update_state(state_store, fid, "running", stage="extracting_pdf")
    append_filing_log(paths, {"event": "pdf_only_start", "run_id": run_id})

    # cache から PDF を取得
    if not has_pdf(paths):
        elapsed = int((time.monotonic() - t0) * 1000)
        append_filing_log(paths, {"event": "failed", "reason": "no_cached_pdf"})
        return FilingResult(
            filing_id=fid, status="failed",
            quarantine={"review_hint": "no_cached_pdf", "filing_id": fid},
            metrics={**metrics, "total_ms": elapsed},
            cache_paths={"cache_dir": str(paths.cache_dir)},
        )

    doc_path = str(paths.source_pdf)

    # PL データが無い場合は cache から読む
    if not financials_data:
        import json as _json
        if paths.extract_financials_result_json.exists():
            try:
                financials_data = _json.loads(paths.extract_financials_result_json.read_text(encoding="utf-8"))
            except Exception:
                pass

    via = "pdf"
    _seg_result = _extract_segments(
        doc_path, filing, financials_data, via, fid, metrics,
        retry_pdf=retry_pdf, timeout_pdf=timeout_pdf, sleep_fn=_sleep,
    )
    segment_records = _seg_result[0] if len(_seg_result) > 0 else []
    seg_err        = _seg_result[1] if len(_seg_result) > 1 else ""
    rule_trace     = _seg_result[2] if len(_seg_result) > 2 else []
    score_summary  = _seg_result[3] if len(_seg_result) > 3 else {}

    elapsed = int((time.monotonic() - t0) * 1000)
    metrics["total_ms"] = elapsed

    if segment_records:
        save_extract_segments_result(paths, segment_records)
        fp = compute_result_fingerprint(segment_records)
        append_filing_log(paths, {"event": "ok", "via": "pdf", "segments": len(segment_records), "fingerprint": fp})
        return FilingResult(
            filing_id=fid, status="ok", via="pdf",
            segment_records=segment_records,
            financial_records=[financials_data] if financials_data else [],
            metrics=metrics,
            cache_paths={"cache_dir": str(paths.cache_dir)},
            result_fingerprint=fp,
            rule_trace=rule_trace,
            score_summary=score_summary,
        )

    reason = seg_err or "pdf_extraction_failed"
    v2_reason = None
    v1_reason = None
    if reason.startswith("v2_reason:"):
        v2_part, _, reason_rest = reason.partition("|")
        v2_reason = v2_part.replace("v2_reason:", "")
        v1_reason = reason_rest or None
        reason = reason_rest or reason
    else:
        v1_reason = reason

    # --- reason 優先順位 ---
    try:
        from src.analysis.row_classifier import choose_better_reason, map_reject_reason_to_review_hint
        best_reason = choose_better_reason(v2_reason, v1_reason)
        if best_reason and ("candidate_guard:" in best_reason or "picked_pl_table" in best_reason or "period_quarter" in best_reason):
            hint = map_reject_reason_to_review_hint(best_reason)
        else:
            hint = classify_review_hint("pdf", reason, False, v2_reason=v2_reason)
    except ImportError:
        best_reason = v2_reason or v1_reason
        hint = classify_review_hint("pdf", reason, False, v2_reason=v2_reason)

    # ================================================================
    # XBRL Fallback: PDF で表が無い場合に cache 内 XBRL から救済
    # ================================================================
    _XBRL_FALLBACK_HINTS = {
        "pdf_no_segment_narrative_page",
        "pdf_no_segment_table_after_guard",
        "pdf_no_segment_page_candidate",
        "pdf_no_segment_table_candidate",
    }
    xbrl_fallback_attempted = False
    xbrl_fallback_succeeded = False
    rescued_from_hint = None
    xbrl_failure_reason = None

    from lib.backfill.cache import has_xbrl
    xbrl_path_cached = str(paths.xbrl_zip) if has_xbrl(paths) else None

    if hint in _XBRL_FALLBACK_HINTS and xbrl_path_cached:
        xbrl_fallback_attempted = True
        rescued_from_hint = hint
        logger.info(f"[worker/pdf_only] XBRL fallback: fid={fid} ticker={filing.ticker} hint={hint}")
        try:
            from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
            xbrl_rows = extract_segments_from_xbrl_zip(xbrl_path_cached, title=getattr(filing, "title", None))
            if xbrl_rows and len(xbrl_rows) > 0:
                period = (financials_data or {}).get("period", "")
                quarter = (financials_data or {}).get("quarter", "")
                if not period and xbrl_rows[0].period:
                    period = xbrl_rows[0].period
                if not quarter and xbrl_rows[0].quarter:
                    quarter = xbrl_rows[0].quarter

                if period and quarter and quarter != "UNKNOWN":
                    xbrl_segment_records = []
                    for idx, row in enumerate(xbrl_rows):
                        seg_name = row.normalized_segment_name or row.raw_segment_name
                        xbrl_segment_records.append({
                            "ticker": filing.ticker,
                            "period": period,
                            "quarter": quarter,
                            "segment_name": seg_name,
                            "segment_order": idx + 1,
                            "segment_sales": row.sales,
                            "segment_profit": row.profit,
                            "raw_profit_label": "",
                            "source": "backfill_xbrl_fallback",
                            "segment_name_norm": _normalize_segment_name_conservative(seg_name),
                            "extractor_route": "xbrl_fallback",
                            "source_doc_type": "earnings_summary",
                            "disclosure_date": filing.disclosure_date,
                            "tdnet_doc_id": fid,
                            "row_type": _classify_row_type(seg_name),
                        })
                    if xbrl_segment_records:
                        xbrl_fallback_succeeded = True
                        save_extract_segments_result(paths, xbrl_segment_records)
                        fp = compute_result_fingerprint(xbrl_segment_records)
                        metrics["xbrl_fallback_attempted"] = True
                        metrics["xbrl_fallback_succeeded"] = True
                        metrics["rescued_from_hint"] = rescued_from_hint
                        metrics["total_ms"] = int((time.monotonic() - t0) * 1000)
                        append_filing_log(paths, {
                            "event": "ok", "via": "xbrl_fallback",
                            "segments": len(xbrl_segment_records),
                            "fingerprint": fp,
                            "rescued_from_hint": rescued_from_hint,
                        })
                        logger.info(f"[worker/pdf_only] XBRL fallback SUCCESS: fid={fid} segments={len(xbrl_segment_records)}")
                        return FilingResult(
                            filing_id=fid, status="ok", via="xbrl_fallback",
                            segment_records=xbrl_segment_records,
                            financial_records=[financials_data] if financials_data else [],
                            metrics=metrics,
                            cache_paths={"cache_dir": str(paths.cache_dir)},
                            result_fingerprint=fp,
                        )
                else:
                    xbrl_failure_reason = f"period_quarter_unresolved:{period!r},{quarter!r}"
            else:
                xbrl_failure_reason = "xbrl_no_segment_facts"
        except Exception as e:
            xbrl_failure_reason = str(e)[:200]
            logger.info(f"[worker/pdf_only] XBRL fallback failed: fid={fid} err={xbrl_failure_reason}")

    metrics["xbrl_fallback_attempted"] = xbrl_fallback_attempted
    metrics["xbrl_fallback_succeeded"] = xbrl_fallback_succeeded
    if xbrl_failure_reason:
        metrics["xbrl_failure_reason"] = xbrl_failure_reason
    if rescued_from_hint:
        metrics["rescued_from_hint"] = rescued_from_hint

    q = {
        "filing_id": fid, "ticker": filing.ticker,
        "stage": "extracting_pdf",
        "error_message": reason[:500],
        "review_hint": hint,
        "via": "pdf",
        "candidate_reject_reason": best_reason or "",
        "detector_v2_reason": v2_reason or "",
        "detector_v1_reason": v1_reason or "",
        "xbrl_fallback_attempted": xbrl_fallback_attempted,
        "xbrl_fallback_succeeded": xbrl_fallback_succeeded,
        "xbrl_failure_reason": xbrl_failure_reason or "",
    }
    save_quarantine(paths, q)
    append_filing_log(paths, {
        "event": "quarantined",
        "reason": reason[:200],
        "review_hint": hint,
        "candidate_reject_reason": best_reason or "",
        "xbrl_fallback_attempted": xbrl_fallback_attempted,
    })
    return FilingResult(
        filing_id=fid, status="quarantined", via="pdf",
        quarantine=q,
        financial_records=[financials_data] if financials_data else [],
        metrics=metrics,
        cache_paths={"cache_dir": str(paths.cache_dir)},
        rule_trace=rule_trace,
        score_summary=score_summary,
    )


# ================================================================
# Util
# ================================================================

def _update_state(store, fid, status, **kwargs):
    """state_store 更新 (non-fatal)。"""
    if not store:
        return
    try:
        store.update_status(fid, status, **kwargs)
    except Exception as e:
        logger.debug(f"[worker] state update skipped for {fid}: {e}")
