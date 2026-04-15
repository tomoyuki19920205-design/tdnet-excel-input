#!/usr/bin/env python3
"""tools/backfill_segments_tdnet.py — TDNET 並列バックフィル CLI (Step 5: Benchmark)

Usage:
    # Phase 1 互換
    python tools/backfill_segments_tdnet.py --limit 100 --workers 8

    # Phase 2 (XBRL/PDF 分離)
    python tools/backfill_segments_tdnet.py --phase2 --xbrl-workers 6 --pdf-workers 3

    # Resume
    python tools/backfill_segments_tdnet.py --resume --phase2 --retry-quarantine
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.listing_provider import CompositeListingProvider
from lib.backfill.listing_sources.tdnet_html import TdnetHtmlListingProvider
from lib.backfill.state_store import BackfillStateStore
from lib.backfill.worker import process_one_filing
from lib.backfill.batch_upsert import batch_upsert_segments
from lib.backfill.metrics import BackfillMetrics, BackfillMetricsV2
from lib.backfill.jsonl_logger import RunLogger, generate_run_id
from lib.backfill.filing_selector import should_process_for_segment_backfill

logger = logging.getLogger("backfill")


# ============================================================
# Filing list / manifest helpers
# ============================================================

def _load_filing_list(path: str) -> list:
    """JSON / JSONL / CSV の manifest ファイルを読み込み、FilingInfo リストを返す。

    サポート形式:
      - .json: [{"filing_id": ..., "ticker": ..., ...}, ...]
      - .jsonl: 1行1 JSON オブジェクト
      - .csv: ヘッダ付き CSV

    必須フィールド: filing_id, ticker, title, disclosure_date, doc_url
    """
    from lib.backfill.listing_sources.base import FilingInfo

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"filing-list not found: {path}")

    raw_records: list[dict] = []

    if p.suffix == ".json":
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                raw_records = data
            elif isinstance(data, dict) and "filings" in data:
                raw_records = data["filings"]
            else:
                raise ValueError(f"Unsupported JSON structure in {path}")
    elif p.suffix == ".jsonl":
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw_records.append(json.loads(line))
    elif p.suffix == ".csv":
        with open(p, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_records.append(row)
    else:
        raise ValueError(f"Unsupported file type: {p.suffix} (use .json/.jsonl/.csv)")

    filings = []
    for rec in raw_records:
        fi = FilingInfo(
            filing_id=rec["filing_id"],
            ticker=rec.get("ticker", ""),
            title=rec.get("title", ""),
            disclosure_date=rec.get("disclosure_date", ""),
            doc_url=rec.get("doc_url", ""),
            xbrl_url=rec.get("xbrl_url") or None,
            doc_type=rec.get("doc_type", "financial_statement"),
            company_name=rec.get("company_name", ""),
            published_at=rec.get("published_at", ""),
            listing_source=rec.get("listing_source", "manifest"),
            has_xbrl=bool(rec.get("has_xbrl", False)),
        )
        filings.append(fi)

    logger.info(f"[backfill] loaded {len(filings)} filings from manifest: {path}")
    print(f"[backfill] loaded {len(filings)} filings from manifest: {path}")
    return filings


def _save_manifest(filings: list, run_id: str, log_dir: str = "logs") -> str:
    """対象 filing の manifest を JSON で保存する。

    Returns:
        保存先パス
    """
    Path(log_dir).mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = f"{log_dir}/filing_manifest_{run_id}_{ts}.json"

    records = []
    for fi in filings:
        records.append({
            "filing_id": fi.filing_id,
            "ticker": fi.ticker,
            "disclosure_date": fi.disclosure_date,
            "title": fi.title,
            "doc_url": fi.doc_url,
            "xbrl_url": fi.xbrl_url or "",
            "doc_type": fi.doc_type,
            "company_name": fi.company_name,
            "published_at": fi.published_at,
            "listing_source": fi.listing_source,
            "has_xbrl": fi.has_xbrl,
        })

    manifest = {
        "run_id": run_id,
        "timestamp": ts,
        "filing_count": len(records),
        "filings": records,
    }
    Path(manifest_path).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"[backfill] manifest saved: {manifest_path} ({len(records)} filings)")
    print(f"[backfill] manifest saved: {manifest_path}")
    return manifest_path


def _build_provider(name: str, listing_log_dir: str | None = None):
    if name == "tdnet_html":
        return TdnetHtmlListingProvider(listing_log_dir=listing_log_dir)
    elif name == "auto":
        return CompositeListingProvider([
            TdnetHtmlListingProvider(listing_log_dir=listing_log_dir),
        ])
    else:
        raise ValueError(f"unknown listing provider: {name}")


def _compute_date_range(args) -> tuple[str, str]:
    if args.date_from and args.date_to:
        return args.date_from, args.date_to
    years = args.years or 1
    end = datetime.now()
    start = end - timedelta(days=365 * years)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _make_result(metrics, run_id, start_date, end_date, phase2, xbrl_workers, pdf_workers, workers):
    """統一戻り値を構築。early return でも benchmark でも同じ形式。"""
    return {
        "summary": metrics.summary_dict(),
        "metrics": metrics,
        "run_id": run_id,
        "date_range": f"{start_date}~{end_date}",
        "phase2": phase2,
        "xbrl_workers": xbrl_workers,
        "pdf_workers": pdf_workers,
        "workers": workers,
    }


def run_backfill(
    *,
    start_date: str,
    end_date: str,
    tickers: list[str] | None = None,
    limit: int | None = None,
    workers: int = 4,
    cache_root: str = "data/tdnet_cache",
    state_db: str = "data/backfill_state.db",
    db_batch_size: int = 200,
    listing_provider_name: str = "tdnet_html",
    skip_pdf: bool = False,
    only_xbrl: bool = False,
    listing_log_dir: str | None = "data/backfill_listing_logs",
    decision_db_path: str | None = None,
    resume: bool = False,
    retry_quarantine: bool = False,
    retry_failed: bool = False,
    retry_download: int = 3,
    retry_xbrl: int = 2,
    retry_pdf: int = 1,
    timeout_download: int = 30,
    timeout_xbrl: int = 60,
    timeout_pdf: int = 120,
    log_jsonl_path: str | None = None,
    flush_every_seconds: int = 300,
    phase2: bool = False,
    xbrl_workers: int = 6,
    pdf_workers: int = 3,
    repair_extracted: bool = False,
    only_earnings_summary: bool = True,
    exclude_corrections: bool = True,
    worker_version: str = "v2",
    filing_list_path: str | None = None,
    reset_target: bool = False,
) -> dict:
    """バックフィルを実行する (Phase 1 / Phase 2 自動選択)。"""
    run_id = generate_run_id()
    use_v2 = worker_version == "v2"
    metrics = BackfillMetricsV2() if use_v2 else BackfillMetrics()

    if log_jsonl_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_jsonl_path = f"logs/backfill_segments_tdnet_{worker_version}_{ts}.jsonl"
    run_logger = RunLogger(log_jsonl_path, run_id=run_id)

    mode = "phase2" if phase2 else "phase1"
    logger.info(
        f"[backfill] ===== RUN START ====="
    )
    applied_limit = limit if limit and limit > 0 else None
    logger.info(
        f"[backfill] run_id={run_id} mode={mode} range={start_date}~{end_date} "
        f"limit={applied_limit or 'unlimited'} resume={resume} state_db={state_db} "
        f"only_earnings_summary={only_earnings_summary} exclude_corrections={exclude_corrections}"
    )
    print(f"[backfill] run_id={run_id} mode={mode} range={start_date}~{end_date} limit={applied_limit or 'unlimited'}")

    def _result():
        return _make_result(metrics, run_id, start_date, end_date, phase2, xbrl_workers, pdf_workers, workers)

    # ── 1. Listing ──
    if filing_list_path:
        # 固定母集団モード: manifest から直接読み込み、listing provider skip
        filings = _load_filing_list(filing_list_path)
        logger.info(f"[backfill] FIXED POPULATION mode: {len(filings)} filings from manifest")
        print(f"[backfill] FIXED POPULATION mode: {len(filings)} filings")
    else:
        logger.info(f"[backfill] listing provider start: {listing_provider_name}")
        print(f"[backfill] listing provider start: {listing_provider_name}")
        provider = _build_provider(listing_provider_name, listing_log_dir)
        filings = provider.list_filings(
            start_date, end_date, tickers=tickers, doc_types=["financial_statement"],
        )
        logger.info(f"[backfill] listing done (pre-selector): total={len(filings)}")
        print(f"[backfill] listing done (pre-selector): total={len(filings)}")

    # ── 1b. filing_selector による最終判定 ──
    # 固定母集団モードでは selector をスキップ (manifest は既にフィルタ済み)
    if filing_list_path:
        accepted = filings
    else:
        accepted = []
        excluded_reasons: dict[str, int] = {}
        excluded_samples: dict[str, list[str]] = {}
        for fi in filings:
            ok, reason = should_process_for_segment_backfill(
                fi.title,
                exclude_corrections=exclude_corrections,
                only_earnings_summary=only_earnings_summary,
            )
            if ok:
                accepted.append(fi)
            else:
                excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
                if reason not in excluded_samples:
                    excluded_samples[reason] = []
                if len(excluded_samples[reason]) < 10:
                    excluded_samples[reason].append(f"[{fi.ticker}] {fi.title}")

        logger.info(
            f"[backfill] selector done: accepted={len(accepted)} excluded={len(filings) - len(accepted)} "
            f"reasons={excluded_reasons}"
        )
        print(f"[backfill] selector done: accepted={len(accepted)} excluded={len(filings) - len(accepted)}")
        for reason, count in sorted(excluded_reasons.items()):
            print(f"  {reason}: {count}")

    filings = accepted

    # ── 1c. Manifest 保存 ──
    _save_manifest(filings, run_id)

    if not filings:
        logger.warning("[backfill] listing returned 0 filings — nothing to do")
        print("[backfill] WARNING: listing returned 0 filings")
        metrics.finalize()
        run_logger.log_summary(metrics.summary_dict())
        run_logger.close()
        return _result()

    # ── 2. State Store ──
    logger.info(f"[backfill] register_filings start: input_count={len(filings)}")
    print(f"[backfill] register_filings start: input_count={len(filings)}")
    store = BackfillStateStore(state_db)
    reg = store.register_filings(filings)
    logger.info(
        f"[backfill] register_filings done: new={reg['new']} existing={reg['existing']} "
        f"total={reg['new'] + reg['existing']}"
    )
    print(
        f"[backfill] register_filings done: new={reg['new']} existing={reg['existing']} "
        f"total={reg['new'] + reg['existing']}"
    )

    # Invariant: listing kept>0 なのに register total==0 はおかしい
    reg_total = reg['new'] + reg['existing']
    if len(filings) > 0 and reg_total == 0:
        msg = f"INVARIANT VIOLATION: listing kept={len(filings)} but register total=0"
        logger.error(f"[backfill] {msg}")
        print(f"[backfill] ERROR: {msg}")
        run_logger.log_fatal(msg)
        metrics.finalize()
        run_logger.log_summary(metrics.summary_dict())
        run_logger.close()
        store.close()
        raise RuntimeError(msg)

    stale = store.reset_stale_running(max_age_hours=2)
    if stale > 0:
        logger.info(f"[backfill] reset {stale} stale running entries")

    if resume:
        if retry_quarantine:
            store.reset_for_retry(statuses=["quarantined"])
        if retry_failed:
            store.reset_for_retry(statuses=["failed"])

    # done/extracted repair: resume または --repair-extracted 時に自動リセット
    if resume or repair_extracted:
        done_count = store.reset_done_to_queued()
        if done_count > 0:
            logger.info(f"[backfill] repaired {done_count} done/extracted filings -> queued")
            print(f"[backfill] repaired {done_count} done/extracted -> queued")

    # --reset-target: 対象 filing だけ強制リセット (固定母集団テスト用)
    if reset_target:
        target_fids = [f.filing_id for f in filings]
        reset_count = 0
        for fid in target_fids:
            try:
                store.reset_filing(fid)
                reset_count += 1
            except Exception:
                pass
        if reset_count > 0:
            logger.info(f"[backfill] reset-target: {reset_count} filings -> queued")
            print(f"[backfill] reset-target: {reset_count} filings -> queued")

    # ── 3. Pending ──
    _limit_for_query = applied_limit or 0  # 0 = unlimited in state store
    logger.info(f"[backfill] get_candidates start: resume={resume} applied_limit={applied_limit or 'unlimited'}")
    if resume:
        pending = store.get_resume_candidates(
            limit=_limit_for_query, tickers=tickers,
            include_quarantined=retry_quarantine,
            include_failed=retry_failed,
        )
    else:
        pending = store.get_pending(limit=_limit_for_query, tickers=tickers)

    metrics.total_filings = len(pending)
    logger.info(
        f"[backfill] get_candidates done: candidate_count={len(pending)} "
        f"applied_limit={applied_limit or 'unlimited'}"
    )
    print(f"[backfill] pending candidates: {len(pending)} (limit={applied_limit or 'unlimited'})")

    if not pending:
        logger.info("[backfill] no pending filings, run complete (all previously processed)")
        print("[backfill] no pending filings (all done or already processed)")
        store_stats = store.stats()
        logger.info(f"[backfill] state_store stats: {store_stats}")
        metrics.finalize()
        run_logger.log_summary(metrics.summary_dict())
        run_logger.close()
        store.close()
        return _result()

    filing_map = {f.filing_id: f for f in filings}

    # ── 4. 実行 (Phase 1 or Phase 2) ──
    segment_buffer: list[dict] = []
    fid_buffer: list[str] = []

    def _flush(buf, fid_buf):
        _flush_buffer(buf, fid_buf, decision_db_path, db_batch_size, metrics, store, run_logger)

    logger.info(f"[backfill] phase={mode} stage start: input_count={len(pending)}")
    print(f"[backfill] {mode} start: {len(pending)} filings")

    if use_v2:
        from lib.backfill.phase2_runner import run_phase2_v2
        run_phase2_v2(
            pending, filing_map,
            store=store, metrics=metrics, run_logger=run_logger, run_id=run_id,
            cache_root=cache_root,
            workers=workers,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            segment_buffer=segment_buffer, fid_buffer=fid_buffer,
            db_batch_size=db_batch_size,
            flush_every_seconds=flush_every_seconds,
            flush_callback=_flush if decision_db_path else None,
        )
    elif phase2:
        from lib.backfill.phase2_runner import run_phase2
        run_phase2(
            pending, filing_map,
            store=store, metrics=metrics, run_logger=run_logger, run_id=run_id,
            cache_root=cache_root,
            xbrl_workers=xbrl_workers, pdf_workers=pdf_workers,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            segment_buffer=segment_buffer, fid_buffer=fid_buffer,
            db_batch_size=db_batch_size, decision_db_path=decision_db_path,
            flush_every_seconds=flush_every_seconds,
            flush_callback=_flush if decision_db_path else None,
        )
    else:
        _run_phase1(
            pending, filing_map,
            store=store, metrics=metrics, run_logger=run_logger, run_id=run_id,
            cache_root=cache_root, workers=workers,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            skip_pdf=skip_pdf, only_xbrl=only_xbrl,
            segment_buffer=segment_buffer, fid_buffer=fid_buffer,
            db_batch_size=db_batch_size, decision_db_path=decision_db_path,
            flush_every_seconds=flush_every_seconds,
        )

    logger.info(f"[backfill] {mode} done")
    print(f"[backfill] {mode} done")

    # ── 5. 残りバッファ flush ──
    if segment_buffer and decision_db_path:
        logger.info(f"[backfill] flush start: record_count={len(segment_buffer)}")
        _flush(segment_buffer, fid_buffer)
        logger.info("[backfill] flush done")

    # ── 6. サマリ ──
    logger.info("[backfill] summary start")
    metrics.finalize()
    store_stats = store.stats()
    store.close()

    logger.info(f"[backfill] state_store stats: {store_stats}")
    run_logger.log_summary(metrics.summary_dict())
    run_logger.close()
    metrics.print_summary()
    logger.info(f"[backfill] summary done, report={log_jsonl_path}")
    print(f"[backfill] summary done, JSONL={log_jsonl_path}")
    return _result()


def _run_phase1(
    pending, filing_map, *, store, metrics, run_logger, run_id, cache_root,
    workers, retry_download, retry_xbrl, retry_pdf,
    timeout_download, timeout_xbrl, timeout_pdf,
    skip_pdf, only_xbrl,
    segment_buffer, fid_buffer, db_batch_size, decision_db_path,
    flush_every_seconds,
):
    """Phase 1: 従来の ThreadPoolExecutor。"""
    last_flush = time.monotonic()

    def _process(fi):
        return process_one_filing(
            fi, cache_root=cache_root, state_store=store,
            skip_pdf=skip_pdf, only_xbrl=only_xbrl,
            retry_download=retry_download, retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
            timeout_download=timeout_download, timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf,
            run_id=run_id,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for row in pending:
            fid = row["filing_id"]
            fi = filing_map.get(fid)
            if not fi:
                continue
            futures[executor.submit(_process, fi)] = (fid, fi)

        for i, fut in enumerate(as_completed(futures), 1):
            fid, fi = futures[fut]
            try:
                result = fut.result()

                import json
                
                import os
                base_dir = os.path.dirname(os.path.dirname(__file__))
                output_path = os.path.join(base_dir, "data", "backfill_results.jsonl")
                with open(output_path, "a", encoding="utf-8") as f:
                    try:
                        f.write(json.dumps(result, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"[backfill] {fid} exception: {e}")
                result = None
                try:
                    store.mark_failed(fid, error=str(e), stage="worker_exception")
                except Exception:
                    pass

            if result is None:
                metrics.failed_count += 1
                metrics.completed_filings += 1
                continue

            metrics.record_result(result)
            run_logger.log_filing_result(result, fi)

            try:
                if result.status == "ok":
                    store.mark_done(fid, via=result.via, segment_count=len(result.segment_records), result_fingerprint=result.result_fingerprint, duration_ms=result.metrics.get("total_ms", 0))
                elif result.status == "quarantined":
                    store.mark_quarantined(fid, error=(result.quarantine or {}).get("error_message", ""), stage=(result.quarantine or {}).get("stage", "unknown"), review_hint=(result.quarantine or {}).get("review_hint", ""))
                elif result.status == "failed":
                    store.mark_failed(fid, error=(result.quarantine or {}).get("error_message", "unknown"), stage="worker")
            except Exception:
                pass

            if result.segment_records:
                segment_buffer.extend(result.segment_records)
                fid_buffer.append(fid)

            now_t = time.monotonic()
            if decision_db_path and (len(segment_buffer) >= db_batch_size or (now_t - last_flush > flush_every_seconds and segment_buffer)):
                _flush_buffer(segment_buffer, fid_buffer, decision_db_path, db_batch_size, metrics, store, run_logger)
                last_flush = time.monotonic()

            if i % 10 == 0 or i == len(futures):
                logger.info(f"[backfill] progress: {i}/{len(futures)} ok={metrics.ok_count} q={metrics.quarantined_count} f={metrics.failed_count}")


def _flush_buffer(buffer, fid_buffer, decision_db_path, batch_size, metrics, store, run_logger):
    """segment バッファを DB に flush し、state を mark_upserted する。"""
    if not decision_db_path:
        buffer.clear()
        fid_buffer.clear()
        return
    try:
        from src.migration.migration_db import MigrationDB
        db = MigrationDB(decision_db_path)
        stats = batch_upsert_segments(buffer, db, batch_size=batch_size)
        metrics.record_upsert(stats)
        run_logger.log_upsert("batch", {"records": len(buffer), "inserted": stats.inserted, "updated": stats.updated, "failed_batches": stats.failed_batches})
        if stats.failed_batches == 0:
            for fid in fid_buffer:
                try:
                    store.mark_upserted(fid)
                    metrics.upserted_count += 1
                except Exception:
                    pass
        logger.info(f"[backfill] flushed {len(buffer)} records: inserted={stats.inserted} updated={stats.updated}")
        db.close()
    except Exception as e:
        logger.error(f"[backfill] flush failed: {e}")
    buffer.clear()
    fid_buffer.clear()


def _run_benchmark_report(result: dict, args) -> None:
    """ベンチマーク後処理: estimator + reporting。"""
    from lib.backfill.estimator import estimate_full_backfill, compute_retry_factor
    from lib.backfill.reporting import (
        generate_notes, compute_percentiles, build_report,
        save_json_report, save_markdown_report,
    )

    metrics_obj = result["metrics"]
    summary = result["summary"]

    # percentiles
    pct = compute_percentiles(metrics_obj.filing_durations_ms)

    # estimate
    estimate = None
    est_total = getattr(args, "estimated_total_filings", 0) or 0
    if est_total > 0:
        # filing-based metrics を優先
        sample = summary.get("filing_completed", summary.get("completed", 0))
        avg_xbrl = summary.get("avg_xbrl_sec", 0) or (metrics_obj.avg_xbrl_sec if hasattr(metrics_obj, 'avg_xbrl_sec') else 0)
        avg_pdf = summary.get("avg_pdf_sec", 0) or (metrics_obj.avg_pdf_sec if hasattr(metrics_obj, 'avg_pdf_sec') else 0)
        # fallback: avg_pdf が 0 なら avg_sec_per_filing * 3
        if avg_pdf <= 0:
            avg_pdf = summary.get("avg_sec_per_filing", 1.0) * 3
        # fallback: avg_xbrl が 0 なら avg_sec_per_filing
        if avg_xbrl <= 0:
            avg_xbrl = summary.get("avg_sec_per_filing", 1.0)

        xbrl_rate_str = summary.get("xbrl_success_rate", "0%")
        pdf_fb_str = summary.get("pdf_fallback_rate", "0%")
        q_rate_str = summary.get("quarantine_rate", "0%")
        # parse '%' strings to float
        def _pct(s):
            if isinstance(s, (int, float)):
                return float(s)
            try:
                return float(str(s).rstrip("%")) / 100
            except (ValueError, TypeError):
                return 0.0

        est = estimate_full_backfill(
            estimated_total_filings=est_total,
            sample_filings=sample,
            avg_xbrl_sec=avg_xbrl,
            avg_pdf_sec=avg_pdf,
            xbrl_success_rate=_pct(xbrl_rate_str),
            pdf_fallback_rate=_pct(pdf_fb_str),
            quarantine_rate=_pct(q_rate_str),
            xbrl_workers=result.get("xbrl_workers", 6),
            pdf_workers=result.get("pdf_workers", 3),
            retry_factor=compute_retry_factor(
                summary.get("retried", 0),
                summary.get("filing_completed", summary.get("completed", 0)),
            ),
        )
        estimate = est.to_dict()

        # invariant check
        if sample > 0 and est.base_case_sec <= 0:
            logger.warning(
                f"[report] invariant violation: sample_filings={sample}, "
                f"avg_pdf_sec={avg_pdf}, avg_xbrl_sec={avg_xbrl}, "
                f"but base_case_sec=0"
            )

        print("\n" + "=" * 60)
        print("  3-Year Full Backfill Estimate")
        print("=" * 60)
        for k, v in estimate.items():
            print(f"  {k:30s} {v}")
        print("=" * 60)

    # notes
    notes = generate_notes(summary, estimate)
    if notes:
        print("\n  Observations:")
        for n in notes:
            print(f"    \u2022 {n}")

    # build report
    report = build_report(
        benchmark_name=getattr(args, "benchmark_name", "unnamed") or "unnamed",
        phase2=result.get("phase2", False),
        xbrl_workers=result.get("xbrl_workers", 6),
        pdf_workers=result.get("pdf_workers", 3),
        workers=result.get("workers", 4),
        metrics=summary,
        estimate=estimate,
        notes=notes,
        percentiles=pct,
        run_id=result.get("run_id", ""),
        date_range=result.get("date_range", ""),
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bn = getattr(args, "benchmark_name", "bench") or "bench"

    # JSON
    json_path = getattr(args, "report_json", None)
    if json_path is None:
        json_path = f"reports/backfill_benchmark_{bn}_{ts}.json"
    save_json_report(report, json_path)
    print(f"\n  JSON report: {json_path}")

    # Markdown
    md_path = getattr(args, "report_md", None)
    if md_path is None:
        md_path = f"reports/backfill_benchmark_{bn}_{ts}.md"
    save_markdown_report(report, md_path)
    print(f"  Markdown report: {md_path}")


def _run_dry_run(
    *,
    start_date: str,
    end_date: str,
    tickers: list[str] | None = None,
    listing_provider_name: str = "tdnet_html",
    only_earnings_summary: bool = True,
    exclude_corrections: bool = True,
) -> None:
    """dry-run: 対象母集団の集計のみ。download/extract/upsert しない。"""
    import csv
    import json as json_mod

    print("=" * 60)
    print("  SEGMENT BACKFILL — DRY RUN")
    print("=" * 60)
    print(f"  range: {start_date} ~ {end_date}")
    print(f"  only_earnings_summary: {only_earnings_summary}")
    print(f"  exclude_corrections: {exclude_corrections}")
    print()

    # 1. Listing 取得
    print("[dry-run] listing provider start ...")
    provider = _build_provider(listing_provider_name)
    filings = provider.list_filings(
        start_date, end_date, tickers=tickers, doc_types=["financial_statement"],
    )
    print(f"[dry-run] listing done: pre-selector total = {len(filings)}")

    # 2. selector 判定
    accepted: list = []
    excluded: list = []
    excluded_reasons: dict[str, int] = {}
    excluded_samples: dict[str, list[dict]] = {}
    accepted_samples: list[dict] = []

    for fi in filings:
        ok, reason = should_process_for_segment_backfill(
            fi.title,
            exclude_corrections=exclude_corrections,
            only_earnings_summary=only_earnings_summary,
        )
        entry = {
            "ticker": fi.ticker,
            "disclosure_date": fi.disclosure_date,
            "title": fi.title,
            "reason": reason,
        }
        if ok:
            accepted.append(entry)
            if len(accepted_samples) < 10:
                accepted_samples.append(entry)
        else:
            excluded.append(entry)
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
            if reason not in excluded_samples:
                excluded_samples[reason] = []
            if len(excluded_samples[reason]) < 10:
                excluded_samples[reason].append(entry)

    # 3. 出力
    print()
    print(f"  総件数 (pre-selector):  {len(filings)}")
    print(f"  採用件数:                {len(accepted)}")
    print(f"  除外件数:                {len(excluded)}")
    print()
    print("  除外理由別件数:")
    for reason, count in sorted(excluded_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {count}")
    print()

    print("  採用タイトルサンプル (最大10件):")
    for s in accepted_samples:
        print(f"    [{s['ticker']}] {s['title']}")
    print()

    for reason, samples in excluded_samples.items():
        print(f"  除外サンプル [{reason}] (最大10件):")
        for s in samples:
            print(f"    [{s['ticker']}] {s['title']}")
        print()

    # 4. ファイル保存
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    Path("logs").mkdir(exist_ok=True)

    # JSON
    report_data = {
        "timestamp": ts,
        "date_range": f"{start_date}~{end_date}",
        "only_earnings_summary": only_earnings_summary,
        "exclude_corrections": exclude_corrections,
        "total_pre_selector": len(filings),
        "accepted_count": len(accepted),
        "excluded_count": len(excluded),
        "excluded_reasons": excluded_reasons,
        "accepted_samples": accepted_samples[:10],
        "excluded_samples": {k: v[:10] for k, v in excluded_samples.items()},
    }
    json_path = f"logs/segment_backfill_dryrun_{ts}.json"
    Path(json_path).write_text(
        json_mod.dumps(report_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  JSON saved: {json_path}")

    # TXT
    txt_path = f"logs/segment_backfill_dryrun_{ts}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"SEGMENT BACKFILL DRY RUN - {ts}\n")
        f.write(f"Range: {start_date} ~ {end_date}\n")
        f.write(f"Total (pre-selector): {len(filings)}\n")
        f.write(f"Accepted: {len(accepted)}\n")
        f.write(f"Excluded: {len(excluded)}\n\n")
        for reason, count in sorted(excluded_reasons.items(), key=lambda x: -x[1]):
            f.write(f"  {reason}: {count}\n")
    print(f"  TXT saved: {txt_path}")

    # CSV (accepted)
    csv_accepted_path = f"logs/segment_backfill_dryrun_{ts}_accepted.csv"
    with open(csv_accepted_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "disclosure_date", "title", "reason"])
        writer.writeheader()
        writer.writerows(accepted)
    print(f"  CSV (accepted) saved: {csv_accepted_path}")

    # CSV (excluded)
    csv_excluded_path = f"logs/segment_backfill_dryrun_{ts}_excluded.csv"
    with open(csv_excluded_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "disclosure_date", "title", "reason"])
        writer.writeheader()
        writer.writerows(excluded)
    print(f"  CSV (excluded) saved: {csv_excluded_path}")

    print()
    print("=" * 60)
    print("  DRY RUN COMPLETE — no downloads, no extractions, no upserts")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="TDNET 並列バックフィル — セグメント業績抽出")
    parser.add_argument("--years", type=int, default=None)
    parser.add_argument("--date-from", type=str, default=None)
    parser.add_argument("--date-to", type=str, default=None)
    parser.add_argument("--tickers", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None, help="Max filings to process (default: unlimited)")
    parser.add_argument("--workers", type=int, default=4, help="Phase 1 並列数")
    parser.add_argument("--listing-provider", type=str, default="tdnet_html")
    parser.add_argument("--cache-root", type=str, default="data/tdnet_cache")
    parser.add_argument("--state-db", type=str, default="data/backfill_state.db")
    parser.add_argument("--decision-db", type=str, default=None)
    parser.add_argument("--db-batch-size", type=int, default=200)
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--only-xbrl", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-quarantine", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-download", type=int, default=3)
    parser.add_argument("--retry-xbrl", type=int, default=2)
    parser.add_argument("--retry-pdf", type=int, default=1)
    parser.add_argument("--timeout-download", type=int, default=30)
    parser.add_argument("--timeout-xbrl", type=int, default=60)
    parser.add_argument("--timeout-pdf", type=int, default=120)
    parser.add_argument("--repair-extracted", action="store_true",
                        help="done/extracted の残骸 filing を queued に戻して再抽出")
    parser.add_argument("--log-jsonl", type=str, default=None)
    parser.add_argument("--flush-every-seconds", type=int, default=300)
    # 固定母集団
    parser.add_argument("--filing-list", type=str, default=None,
                        help="固定母集団 manifest (JSON/JSONL/CSV)。listing provider をスキップ")
    parser.add_argument("--reset-target", action="store_true",
                        help="対象 filing を強制的に queued にリセットして再処理")
    # Step 4
    parser.add_argument("--phase2", action="store_true", help="Phase 2: XBRL/PDF 分離実行")
    parser.add_argument("--xbrl-workers", type=int, default=6, help="Phase 2 XBRL 並列数")
    parser.add_argument("--pdf-workers", type=int, default=3, help="Phase 2 PDF 並列数")
    # Step 5: ベンチマーク
    parser.add_argument("--benchmark", action="store_true", help="ベンチマークモード")
    parser.add_argument("--benchmark-name", type=str, default=None, help="ベンチ名")
    parser.add_argument("--estimated-total-filings", type=int, default=0, help="3年フル推定用の総 filing 数")
    parser.add_argument("--report-json", type=str, default=None, help="JSON レポート出力先")
    parser.add_argument("--report-md", type=str, default=None, help="Markdown レポート出力先")
    # 決算短信フィルタ
    parser.add_argument("--only-earnings-summary", action="store_true", default=True,
                        help="決算短信のみ対象 (デフォルト ON)")
    parser.add_argument("--no-only-earnings-summary", dest="only_earnings_summary", action="store_false",
                        help="決算短信以外も対象にする")
    parser.add_argument("--exclude-corrections", action="store_true", default=True,
                        help="訂正資料を除外 (デフォルト ON)")
    parser.add_argument("--no-exclude-corrections", dest="exclude_corrections", action="store_false",
                        help="訂正資料も対象にする")
    parser.add_argument("--worker-version", type=str, default="v2", choices=["v1", "v2"],
                        help="Worker version: v1 (legacy PDF-only) or v2 (XBRL-first source-aware, default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="集計のみ。download・extract・upsert しない")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    start_date, end_date = _compute_date_range(args)
    tickers = args.tickers.split(",") if args.tickers else None

    # ── dry-run モード ──
    if args.dry_run:
        _run_dry_run(
            start_date=start_date, end_date=end_date, tickers=tickers,
            listing_provider_name=args.listing_provider,
            only_earnings_summary=args.only_earnings_summary,
            exclude_corrections=args.exclude_corrections,
        )
        return

    # JSONL logger は main 側でも保持 — fatal ログ用
    run_logger_for_fatal: RunLogger | None = None

    try:
        result = run_backfill(
            start_date=start_date, end_date=end_date, tickers=tickers, limit=args.limit,
            workers=args.workers, cache_root=args.cache_root, state_db=args.state_db,
            db_batch_size=args.db_batch_size, listing_provider_name=args.listing_provider,
            skip_pdf=args.skip_pdf, only_xbrl=args.only_xbrl, decision_db_path=args.decision_db,
            resume=args.resume, retry_quarantine=args.retry_quarantine, retry_failed=args.retry_failed,
            retry_download=args.retry_download, retry_xbrl=args.retry_xbrl, retry_pdf=args.retry_pdf,
            timeout_download=args.timeout_download, timeout_xbrl=args.timeout_xbrl, timeout_pdf=args.timeout_pdf,
            log_jsonl_path=args.log_jsonl, flush_every_seconds=args.flush_every_seconds,
            phase2=args.phase2, xbrl_workers=args.xbrl_workers, pdf_workers=args.pdf_workers,
            repair_extracted=args.repair_extracted,
            only_earnings_summary=args.only_earnings_summary,
            exclude_corrections=args.exclude_corrections,
            worker_version=args.worker_version,
            filing_list_path=args.filing_list,
            reset_target=args.reset_target,
        )
    except Exception:
        import traceback
        tb = traceback.format_exc()
        logger.exception("[backfill] FATAL: unhandled exception in run_backfill")
        print(f"[backfill] FATAL:\n{tb}", file=sys.stderr)
        # JSONL fatal ログ — run_backfill 内の RunLogger は既に close 済みかもしれないが、
        # main 側で別途 fatal を書く
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fatal_path = args.log_jsonl or f"logs/backfill_fatal_{ts}.jsonl"
        try:
            run_logger_for_fatal = RunLogger(fatal_path)
            run_logger_for_fatal.log_fatal(tb[:2000])
            run_logger_for_fatal.close()
        except Exception:
            pass
        sys.exit(1)

    if args.benchmark:
        try:
            _run_benchmark_report(result, args)
        except Exception:
            logger.exception("[backfill] benchmark report failed")
            print("[backfill] WARNING: benchmark report failed", file=sys.stderr)

    summary = result.get("summary", result) if isinstance(result, dict) else result
    if summary.get("failed", 0) > 0 or summary.get("upsert_failed_batches", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
