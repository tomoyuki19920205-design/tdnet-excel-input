"""lib/backfill/phase2_runner.py — Phase 2: 2段階 executor 実行

Stage B: XBRL-first (高並列) → ok | needs_pdf
Stage C: PDF-only  (低並列) → ok | quarantined

main 側が batch upsert + state 更新を行う。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.backfill.worker import (
    process_one_filing_xbrl_first,
    process_one_filing_pdf_only,
    FilingResult,
)
from lib.backfill.metrics import BackfillMetrics
from lib.backfill.jsonl_logger import RunLogger

logger = logging.getLogger("backfill.phase2")


def run_phase2(
    pending: list[dict],
    filing_map: dict,
    *,
    store,
    metrics: BackfillMetrics,
    run_logger: RunLogger,
    run_id: str,
    cache_root: str = "data/tdnet_cache",
    xbrl_workers: int = 6,
    pdf_workers: int = 3,
    retry_download: int = 3,
    retry_xbrl: int = 2,
    retry_pdf: int = 1,
    timeout_download: int = 30,
    timeout_xbrl: int = 60,
    timeout_pdf: int = 120,
    segment_buffer: list[dict] | None = None,
    fid_buffer: list[str] | None = None,
    db_batch_size: int = 200,
    decision_db_path: str | None = None,
    flush_every_seconds: int = 300,
    flush_callback=None,
) -> list[FilingResult]:
    """Phase 2 を実行する。

    Returns:
        全 FilingResult のリスト (ok/quarantined/failed 含む)
    """
    if segment_buffer is None:
        segment_buffer = []
    if fid_buffer is None:
        fid_buffer = []

    all_results: list[FilingResult] = []
    needs_pdf_queue: list[tuple] = []  # (filing_info, xbrl_result)
    last_flush = time.monotonic()

    # ================================================================
    # Stage B: XBRL-first (高並列)
    # ================================================================
    t_xbrl_stage = time.monotonic()
    logger.info(f"[phase2] Stage B: XBRL-first with {xbrl_workers} workers, {len(pending)} filings")

    def _xbrl_process(fi):
        return process_one_filing_xbrl_first(
            fi,
            cache_root=cache_root,
            state_store=store,
            retry_download=retry_download,
            retry_xbrl=retry_xbrl,
            timeout_download=timeout_download,
            timeout_xbrl=timeout_xbrl,
            run_id=run_id,
        )

    with ThreadPoolExecutor(max_workers=xbrl_workers) as executor:
        futures = {}
        for row in pending:
            fid = row["filing_id"]
            fi = filing_map.get(fid)
            if not fi:
                logger.warning(f"[phase2] skip {fid}: not in listing")
                continue
            futures[executor.submit(_xbrl_process, fi)] = (fid, fi)

        for i, fut in enumerate(as_completed(futures), 1):
            fid, fi = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logger.error(f"[phase2] xbrl {fid} exception: {e}")
                result = FilingResult(filing_id=fid, status="failed", quarantine={"error_message": str(e)})
                try:
                    store.mark_failed(fid, error=str(e), stage="xbrl_worker_exception")
                except Exception:
                    pass

            all_results.append(result)
            metrics.record_xbrl_result(result)
            run_logger.log_filing_result(result, fi)

            if result.status == "ok":
                _handle_ok(result, fid, store, segment_buffer, fid_buffer)
            elif result.status == "needs_pdf":
                needs_pdf_queue.append((fi, result))
                try:
                    store.mark_needs_pdf(fid, review_hint=(result.quarantine or {}).get("review_hint", ""))
                except Exception:
                    pass
            elif result.status == "quarantined":
                _handle_quarantine(result, fid, store)
            elif result.status == "failed":
                _handle_failed(result, fid, store)

            # flush check
            now_t = time.monotonic()
            if flush_callback and (len(segment_buffer) >= db_batch_size or (now_t - last_flush > flush_every_seconds and segment_buffer)):
                flush_callback(segment_buffer, fid_buffer)
                last_flush = time.monotonic()

            if i % 20 == 0 or i == len(futures):
                logger.info(f"[phase2] xbrl progress: {i}/{len(futures)} ok={metrics.ok_xbrl_count} needs_pdf={metrics.needs_pdf_count}")

    metrics.xbrl_stage_elapsed = time.monotonic() - t_xbrl_stage

    # ================================================================
    # Stage C: PDF-only (低並列)
    # ================================================================
    if not needs_pdf_queue:
        logger.info("[phase2] Stage C: no PDF filings needed")
        return all_results

    t_pdf_stage = time.monotonic()
    logger.info(f"[phase2] Stage C: PDF-only with {pdf_workers} workers, {len(needs_pdf_queue)} filings")

    def _pdf_process(fi, xbrl_result):
        fin_data = xbrl_result.financial_records[0] if xbrl_result.financial_records else None
        return process_one_filing_pdf_only(
            fi,
            cache_root=cache_root,
            state_store=store,
            retry_pdf=retry_pdf,
            timeout_pdf=timeout_pdf,
            run_id=run_id,
            financials_data=fin_data,
        )

    with ThreadPoolExecutor(max_workers=pdf_workers) as executor:
        futures = {}
        for fi, xr in needs_pdf_queue:
            futures[executor.submit(_pdf_process, fi, xr)] = (fi.filing_id, fi)

        for i, fut in enumerate(as_completed(futures), 1):
            fid, fi = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logger.error(f"[phase2] pdf {fid} exception: {e}")
                result = FilingResult(filing_id=fid, status="failed", quarantine={"error_message": str(e)})
                try:
                    store.mark_failed(fid, error=str(e), stage="pdf_worker_exception")
                except Exception:
                    pass

            all_results.append(result)
            metrics.record_pdf_result(result)
            run_logger.log_filing_result(result, fi)

            if result.status == "ok":
                _handle_ok(result, fid, store, segment_buffer, fid_buffer)
            elif result.status == "quarantined":
                _handle_quarantine(result, fid, store)
            elif result.status == "failed":
                _handle_failed(result, fid, store)

            # flush check
            now_t = time.monotonic()
            if flush_callback and (len(segment_buffer) >= db_batch_size or (now_t - last_flush > flush_every_seconds and segment_buffer)):
                flush_callback(segment_buffer, fid_buffer)
                last_flush = time.monotonic()

            if i % 10 == 0 or i == len(futures):
                logger.info(f"[phase2] pdf progress: {i}/{len(futures)} ok_pdf={metrics.ok_pdf_count}")

    metrics.pdf_stage_elapsed = time.monotonic() - t_pdf_stage

    return all_results


# ================================================================
# 内部ヘルパー
# ================================================================

def _handle_ok(result, fid, store, seg_buf, fid_buf):
    seg_buf.extend(result.segment_records)
    fid_buf.append(fid)
    try:
        store.mark_done(
            fid, via=result.via,
            segment_count=len(result.segment_records),
            result_fingerprint=result.result_fingerprint,
            duration_ms=result.metrics.get("total_ms", 0),
        )
    except Exception as e:
        logger.warning(f"[phase2] mark_done failed {fid}: {e}")


def _handle_quarantine(result, fid, store):
    try:
        q = result.quarantine or {}
        store.mark_quarantined(
            fid,
            error=q.get("error_message", ""),
            stage=q.get("stage", "unknown"),
            review_hint=q.get("review_hint", ""),
        )
    except Exception as e:
        logger.warning(f"[phase2] mark_quarantined failed {fid}: {e}")


def _handle_failed(result, fid, store):
    try:
        store.mark_failed(
            fid,
            error=(result.quarantine or {}).get("error_message", "unknown"),
            stage="phase2",
        )
    except Exception as e:
        logger.warning(f"[phase2] mark_failed failed {fid}: {e}")


# ================================================================
# V2 Runner
# ================================================================

def run_phase2_v2(
    pending: list[dict],
    filing_map: dict,
    *,
    store,
    metrics,                            # BackfillMetricsV2
    run_logger: RunLogger,
    run_id: str,
    cache_root: str = "data/tdnet_cache",
    workers: int = 4,
    retry_download: int = 3,
    retry_xbrl: int = 2,
    retry_pdf: int = 1,
    timeout_download: int = 30,
    timeout_xbrl: int = 60,
    timeout_pdf: int = 120,
    segment_buffer: list[dict] | None = None,
    fid_buffer: list[str] | None = None,
    db_batch_size: int = 200,
    flush_every_seconds: int = 300,
    flush_callback=None,
) -> list:
    """V2 worker 経路: XBRL → HTML(stub) → PDF を1パスで実行。

    process_one_filing_v2 は内部で全 source を試し、最良を返す。
    2段階 stage 分割は不要。

    Returns:
        FilingResultV2 のリスト
    """
    from lib.backfill.worker_v2 import process_one_filing_v2, FilingResultV2

    if segment_buffer is None:
        segment_buffer = []
    if fid_buffer is None:
        fid_buffer = []

    all_results: list[FilingResultV2] = []
    last_flush = time.monotonic()

    logger.info(f"[phase2_v2] Starting V2 worker with {workers} workers, {len(pending)} filings")

    def _v2_process(fi):
        return process_one_filing_v2(
            fi,
            cache_root=cache_root,
            state_store=store,
            retry_download=retry_download,
            retry_xbrl=retry_xbrl,
            retry_pdf=retry_pdf,
            timeout_download=timeout_download,
            timeout_xbrl=timeout_xbrl,
            timeout_pdf=timeout_pdf,
            run_id=run_id,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for row in pending:
            fid = row["filing_id"]
            fi = filing_map.get(fid)
            if not fi:
                logger.warning(f"[phase2_v2] skip {fid}: not in listing")
                continue
            futures[executor.submit(_v2_process, fi)] = (fid, fi)

        for i, fut in enumerate(as_completed(futures), 1):
            fid, fi = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logger.error(f"[phase2_v2] {fid} exception: {e}")
                result = FilingResultV2(
                    filing_id=fid, status="failed", source="", selected_path="none",
                    confidence=0.0, reason=f"worker_exception: {e}",
                    hard_fail_reason="", quarantine_reason="worker_exception",
                    fallback_used=False, fallback_reason="",
                    raw_segment_count=0, valid_segment_count=0, invalid_segment_count=0,
                    sales_non_null_count=0, profit_non_null_count=0,
                    metrics={"total_ms": 0},
                )
                try:
                    store.mark_failed(fid, error=str(e), stage="v2_worker_exception")
                except Exception:
                    pass

            all_results.append(result)
            metrics.record_v2_result(result)
            run_logger.log_filing_result_v2(result, fi)

            # status routing
            if result.status in ("ok", "partial"):
                _handle_ok_v2(result, fid, store, segment_buffer, fid_buffer)
            elif result.status == "quarantined":
                _handle_quarantine_v2(result, fid, store)
            elif result.status == "failed":
                _handle_failed_v2(result, fid, store)

            # periodic flush
            now_t = time.monotonic()
            if flush_callback and (
                len(segment_buffer) >= db_batch_size
                or (now_t - last_flush > flush_every_seconds and segment_buffer)
            ):
                flush_callback(segment_buffer, fid_buffer)
                last_flush = time.monotonic()

            if i % 20 == 0 or i == len(futures):
                ok_count = sum(1 for r in all_results if r.status == "ok")
                partial_count = sum(1 for r in all_results if r.status == "partial")
                q_count = sum(1 for r in all_results if r.status == "quarantined")
                logger.info(
                    f"[phase2_v2] progress: {i}/{len(futures)} "
                    f"ok={ok_count} partial={partial_count} quarantined={q_count}"
                )

    return all_results


def _handle_ok_v2(result, fid, store, seg_buf, fid_buf):
    seg_buf.extend(result.segment_records)
    fid_buf.append(fid)
    try:
        store.mark_done(
            fid, via=result.selected_path,
            segment_count=len(result.segment_records),
            result_fingerprint=result.result_fingerprint,
            duration_ms=result.metrics.get("total_ms", 0),
        )
    except Exception as e:
        logger.warning(f"[phase2_v2] mark_done failed {fid}: {e}")


def _handle_quarantine_v2(result, fid, store):
    try:
        q = result.quarantine or {}
        store.mark_quarantined(
            fid,
            error=q.get("hard_fail_reason", result.quarantine_reason),
            stage="segment_extraction_v2",
            review_hint=result.quarantine_reason,
        )
    except Exception as e:
        logger.warning(f"[phase2_v2] mark_quarantined failed {fid}: {e}")


def _handle_failed_v2(result, fid, store):
    try:
        store.mark_failed(
            fid,
            error=result.reason[:500],
            stage="phase2_v2",
        )
    except Exception as e:
        logger.warning(f"[phase2_v2] mark_failed failed {fid}: {e}")


# ================================================================
# V4 Runner
# ================================================================

def run_phase2_v4(
    pending: list[dict],
    filing_map: dict,
    *,
    store,
    metrics,                            # BackfillMetricsV2
    run_logger: RunLogger,
    run_id: str,
    cache_root: str = "data/tdnet_cache",
    workers: int = 4,
    retry_download: int = 3,
    retry_xbrl: int = 2,
    retry_pdf: int = 1,
    timeout_download: int = 30,
    timeout_xbrl: int = 60,
    timeout_pdf: int = 120,
    segment_buffer: list[dict] | None = None,
    fid_buffer: list[str] | None = None,
    db_batch_size: int = 200,
    flush_every_seconds: int = 300,
    flush_callback=None,
    dry_run_only: bool = False,
    isolated_worker_dry_run: bool = False,
    skip_pdf: bool = False,
) -> list:
    """V4 worker 経路: XBRL-first → V4 PDF fallback を1パスで実行。

    process_one_filing_v4 は XBRL が取れない場合に
    run_segment_detection_v4 で PDF 抽出を行う。
    V1 fallback は呼ばない。

    Returns:
        FilingResultV2 のリスト
    """
    from lib.backfill.worker_v4 import process_one_filing_v4
    from lib.backfill.worker_v2 import FilingResultV2

    if segment_buffer is None:
        segment_buffer = []
    if fid_buffer is None:
        fid_buffer = []

    all_results: list[FilingResultV2] = []
    last_flush = time.monotonic()

    logger.info(f"[phase2_v4] Starting V4 worker with {workers} workers, {len(pending)} filings")

    def _v4_process(fi):
        return process_one_filing_v4(
            fi,
            cache_root=cache_root,
            state_store=store,
            retry_download=retry_download,
            retry_xbrl=retry_xbrl,
            retry_pdf=retry_pdf,
            timeout_download=timeout_download,
            timeout_xbrl=timeout_xbrl,
            timeout_pdf=timeout_pdf,
            run_id=run_id,
            dry_run_only=dry_run_only,
            isolated_worker_dry_run=isolated_worker_dry_run,
            skip_pdf=skip_pdf,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        # FIXED POPULATION mode 対応:
        # pending を filing_map に含まれる fid のみに絞り込み、
        # filing_map に存在するが pending にない fid（done 済み等）も投入対象に追加する。
        _pending_map = {row["filing_id"]: row for row in pending if row["filing_id"] in filing_map}
        for fid in filing_map:
            if fid not in _pending_map:
                _pending_map[fid] = {"filing_id": fid}
        pending = list(_pending_map.values())

        futures = {}
        for row in pending:
            fid = row["filing_id"]
            fi = filing_map.get(fid)
            if not fi:
                logger.warning(f"[phase2_v4] skip {fid}: not in listing")
                continue
            futures[executor.submit(_v4_process, fi)] = (fid, fi)

        for i, fut in enumerate(as_completed(futures), 1):
            fid, fi = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logger.error(f"[phase2_v4] {fid} exception: {e}")
                result = FilingResultV2(
                    filing_id=fid, status="failed", source="", selected_path="none",
                    confidence=0.0, reason=f"worker_exception: {e}",
                    hard_fail_reason="", quarantine_reason="worker_exception",
                    fallback_used=False, fallback_reason="",
                    raw_segment_count=0, valid_segment_count=0, invalid_segment_count=0,
                    sales_non_null_count=0, profit_non_null_count=0,
                    metrics={"total_ms": 0},
                )
                try:
                    store.mark_failed(fid, error=str(e), stage="v4_worker_exception")
                except Exception:
                    pass

            all_results.append(result)
            metrics.record_v2_result(result)
            run_logger.log_filing_result_v2(result, fi)

            # status routing
            if result.status in ("ok", "partial"):
                _handle_ok_v4(result, fid, store, segment_buffer, fid_buffer)
            elif result.status == "skipped_normal":
                # 正常スキップ: quarantined には入れず done として記録
                try:
                    store.mark_done(fid, via="skipped_normal", segment_count=0,
                                    duration_ms=result.metrics.get("total_ms", 0))
                except Exception:
                    pass
            elif result.status == "quarantined":
                _handle_quarantine_v4(result, fid, store)
            elif result.status == "failed":
                _handle_failed_v4(result, fid, store)

            # periodic flush
            now_t = time.monotonic()
            if flush_callback and (
                len(segment_buffer) >= db_batch_size
                or (now_t - last_flush > flush_every_seconds and segment_buffer)
            ):
                flush_callback(segment_buffer, fid_buffer)
                last_flush = time.monotonic()

            if i % 20 == 0 or i == len(futures):
                ok_count = sum(1 for r in all_results if r.status == "ok")
                partial_count = sum(1 for r in all_results if r.status == "partial")
                q_count = sum(1 for r in all_results if r.status == "quarantined")
                logger.info(
                    f"[phase2_v4] progress: {i}/{len(futures)} "
                    f"ok={ok_count} partial={partial_count} quarantined={q_count}"
                )

    return all_results


def _handle_ok_v4(result, fid, store, seg_buf, fid_buf):
    seg_buf.extend(result.segment_records)
    fid_buf.append(fid)
    try:
        store.mark_done(
            fid, via=result.selected_path,
            segment_count=len(result.segment_records),
            result_fingerprint=result.result_fingerprint,
            duration_ms=result.metrics.get("total_ms", 0),
        )
    except Exception as e:
        logger.warning(f"[phase2_v4] mark_done failed {fid}: {e}")


def _handle_quarantine_v4(result, fid, store):
    try:
        q = result.quarantine or {}
        store.mark_quarantined(
            fid,
            error=q.get("hard_fail_reason", result.quarantine_reason),
            stage="segment_extraction_v4",
            review_hint=result.quarantine_reason,
        )
    except Exception as e:
        logger.warning(f"[phase2_v4] mark_quarantined failed {fid}: {e}")


def _handle_failed_v4(result, fid, store):
    try:
        store.mark_failed(
            fid,
            error=result.reason[:500],
            stage="phase2_v4",
        )
    except Exception as e:
        logger.warning(f"[phase2_v4] mark_failed failed {fid}: {e}")

