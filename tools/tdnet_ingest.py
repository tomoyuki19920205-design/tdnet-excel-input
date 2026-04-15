#!/usr/bin/env python3
# ============================================================
# tdnet_ingest.py — TDNET開示ワンショット取得→DB反映CLI
# ============================================================
#
# 使い方:
#   .\.venv\Scripts\python.exe tools\tdnet_ingest.py
#   .\.venv\Scripts\python.exe tools\tdnet_ingest.py --company-code 0812
#   .\.venv\Scripts\python.exe tools\tdnet_ingest.py --dry-run
#   .\.venv\Scripts\python.exe tools\tdnet_ingest.py --dump-on-error data\dumps
#   .\.venv\Scripts\python.exe tools\tdnet_ingest.py --replay data\docs\test.zip
#
# ============================================================
from __future__ import annotations
import time

import argparse
import hashlib
import io
import json
import sys
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.config import load_config, Config
from src.db import StateDB
from src.downloader import download_document
from src.extractor import extract_financials, extract_order_metrics, extract_segment_financials
from src.fetcher import fetch_new_disclosures
from src.migration.migration_db import MigrationDB
from src.models import Status, DisclosureType
from src.utils import (
    setup_logger,
    convert_to_excel_unit,
    parse_scale_unit,
    excel_unit_multiplier,
)
from src.year_parser import parse_reiwa, extract_fiscal_info

import calendar
import logging
import shutil

logger = logging.getLogger("tdnet")

JST = timezone(timedelta(hours=9))


# ============================================================
# ユーティリティ
# ============================================================

def _reiwa_to_fiscal_year_end(r_str: str) -> str | None:
    """R表記 → fiscal_year_end (YYYY-MM-DD)"""
    parsed = parse_reiwa(r_str)
    if parsed is None:
        return None
    ad_year, month = parsed
    last_day = calendar.monthrange(ad_year, month)[1]
    return f"{ad_year:04d}-{month:02d}-{last_day:02d}"


def _sha256_file(path: str) -> str:
    """ファイルのSHA256ハッシュを計算"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_jst_iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


# ============================================================
# ダンプ機能
# ============================================================

def _dump_error(
    dump_dir: str,
    source_doc_id: str,
    source_url: str,
    zip_hash: str | None,
    error_type: str,
    error_message: str,
    doc_path: str | None = None,
    xbrl_path: str | None = None,
) -> None:
    """抽出失敗時にZIPとメタ情報JSONをダンプする"""
    Path(dump_dir).mkdir(parents=True, exist_ok=True)
    safe_id = source_doc_id[:24]
    safe_hash = (zip_hash or "nohash")[:12]
    prefix = f"{safe_id}_{safe_hash}"

    # メタ情報JSON
    meta = {
        "source_doc_id": source_doc_id,
        "source_url": source_url,
        "zip_hash": zip_hash,
        "parser_version": "v2",
        "timestamp": _now_jst_iso(),
        "error_type": error_type,
        "error_message": error_message,
    }
    json_path = os.path.join(dump_dir, f"{prefix}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # ソースファイルコピー（XBRL ZIP優先、なければdoc_path）
    src_file = xbrl_path or doc_path
    if src_file and os.path.isfile(src_file):
        ext = os.path.splitext(src_file)[1] or ".bin"
        dump_file = os.path.join(dump_dir, f"{prefix}{ext}")
        if not os.path.exists(dump_file):
            shutil.copy2(src_file, dump_file)

    logger.info(f"[DUMP] エラーダンプ保存: {json_path}")


# ============================================================
# 個別開示の処理
# ============================================================

def _process_single(
    item,
    config: Config,
    state_db: StateDB,
    decision_db: MigrationDB,
    run_id: str,
    dry_run: bool = False,
    dump_dir: str | None = None,
) -> dict:
    """
    単一開示を処理する。

    Returns:
        結果辞書 {"status": str, "detail": str, "code": str, ...}
    """
    disclosure_id = item.disclosure_id
    code = item.ticker

    # 冪等性チェック
    if state_db.is_processed(disclosure_id):
        return {"status": "skipped", "detail": "処理済み", "code": code}

    # ダウンロード
    docs_dir = str(Path(config.state_db_path).parent / "docs")
    doc_path = download_document(item.doc_url, docs_dir)

    if doc_path is None:
        if not dry_run:
            state_db.record(
                disclosure_id=disclosure_id, code=code,
                year="", quarter="",
                status=Status.DOWNLOAD_FAILED,
                error_detail="ダウンロード失敗",
            )
        logger.warning(
            f"[INGEST] doc_id={disclosure_id[:16]} "
            f"source_url={item.doc_url} status=download_failed"
        )
        return {"status": "error", "detail": "ダウンロード失敗", "code": code}

    # ZIPハッシュ
    zip_hash = _sha256_file(doc_path) if os.path.isfile(doc_path) else None

    # XBRL取得 + ZIP永続化
    xbrl_path = None
    if item.xbrl_url:
        xbrl_path = download_document(item.xbrl_url, docs_dir)
        # XBRL ZIP を永続化（再調査用）
        if xbrl_path and os.path.isfile(xbrl_path):
            try:
                archive_dir = str(Path(config.state_db_path).parent / "xbrl_archive")
                os.makedirs(archive_dir, exist_ok=True)
                from datetime import datetime
                date_str = datetime.now().strftime("%Y%m%d")
                archive_name = f"{code}_{date_str}_{os.path.basename(xbrl_path)}"
                archive_path = os.path.join(archive_dir, archive_name)
                if not os.path.exists(archive_path):
                    import shutil
                    shutil.copy2(xbrl_path, archive_path)
                    logger.debug(f"[INGEST] XBRL ZIP archived: {archive_path}")
            except Exception as e:
                logger.warning(f"[INGEST] XBRL ZIP archive failed: {e}")

    # 抽出
    financials, extract_error = extract_financials(
        doc_path=doc_path,
        title=item.title,
        xbrl_path=xbrl_path,
    )

    if financials is None:
        error_msg = extract_error or "抽出失敗"

        # non-tanshin PDFスキップは retryable skip として記録
        # （コード修正後の再 ingest で自動再処理される）
        if "SKIP_PDF_NOT_TANSHIN" in error_msg:
            logger.info(
                f"[INGEST] doc_id={disclosure_id[:16]} "
                f"status=skipped_not_tanshin title={item.title[:40]}"
            )
            if not dry_run:
                state_db.record(
                    disclosure_id=disclosure_id, code=code,
                    year="", quarter="",
                    status=Status.SKIPPED_NOT_TANSHIN,
                    error_detail=error_msg,
                )
            return {"status": "skipped", "detail": error_msg, "code": code}

        logger.warning(
            f"[INGEST] doc_id={disclosure_id[:16]} "
            f"zip_hash={zip_hash or 'N/A'} "
            f"source_url={item.doc_url} "
            f"status=extract_failed reason={error_msg}"
        )
        if dump_dir:
            _dump_error(
                dump_dir, disclosure_id, item.doc_url, zip_hash,
                "extract_failed", error_msg, doc_path, xbrl_path,
            )
        if not dry_run:
            state_db.record(
                disclosure_id=disclosure_id, code=code,
                year="", quarter="",
                status=Status.PARSE_FAILED,
                error_detail=error_msg,
            )
        return {"status": "error", "detail": f"抽出失敗: {error_msg}", "code": code}

    # 年度・四半期
    if not financials.fiscal_year or not financials.quarter:
        detail = f"年度/Q不明: year={financials.fiscal_year}, q={financials.quarter}"
        logger.warning(
            f"[INGEST] doc_id={disclosure_id[:16]} "
            f"zip_hash={zip_hash or 'N/A'} status=parse_failed reason={detail}"
        )
        if dump_dir:
            _dump_error(
                dump_dir, disclosure_id, item.doc_url, zip_hash,
                "fiscal_info_missing", detail, doc_path, xbrl_path,
            )
        if not dry_run:
            state_db.record(
                disclosure_id=disclosure_id, code=code,
                year=financials.fiscal_year or "", quarter=financials.quarter or "",
                status=Status.PARSE_FAILED, error_detail=detail,
            )
        return {"status": "error", "detail": detail, "code": code}

    fiscal_year_end = _reiwa_to_fiscal_year_end(financials.fiscal_year)
    if fiscal_year_end is None:
        detail = f"年度変換失敗: {financials.fiscal_year}"
        if not dry_run:
            state_db.record(
                disclosure_id=disclosure_id, code=code,
                year=financials.fiscal_year, quarter=financials.quarter,
                status=Status.PARSE_FAILED, error_detail=detail,
            )
        return {"status": "error", "detail": detail, "code": code}

    # 単位変換
    source_mult = parse_scale_unit(financials.source_unit) if financials.source_unit else 1
    excel_mult = excel_unit_multiplier(config.excel_unit)

    sales = convert_to_excel_unit(financials.sales, source_mult, excel_mult) if financials.sales is not None else None
    gross_profit = convert_to_excel_unit(financials.gross_profit, source_mult, excel_mult) if financials.gross_profit is not None else None
    operating_profit = convert_to_excel_unit(financials.operating_profit, source_mult, excel_mult) if financials.operating_profit is not None else None

    result_detail = (
        f"{code} {fiscal_year_end} {financials.quarter} "
        f"sales={sales} gp={gross_profit} op={operating_profit}"
    )

    logger.info(
        f"[INGEST] doc_id={disclosure_id[:16]} "
        f"zip_hash={zip_hash or 'N/A'} status=success {result_detail}"
    )

    if dry_run:
        return {"status": "dry_run", "detail": result_detail, "code": code}

    # DB upsert
    db_result = decision_db.upsert_quarterly_result(
        company_code=code,
        fiscal_year_end=fiscal_year_end,
        quarter=financials.quarter,
        sales=sales,
        gross_profit=gross_profit,
        operating_profit=operating_profit,
        actor="tdnet_ingest",
        source="tdnet",
        tdnet_disclosure_id=disclosure_id,
        run_id=run_id,
        source_doc_id=disclosure_id,
        source_url=item.doc_url,
        zip_hash=zip_hash,
        field_sources=financials.field_sources or None,
    )
    decision_db.commit()

    # ── 受注メトリクス抽出（失敗しても全体を止めない） ──
    try:
        order_result, order_reason = extract_order_metrics(
            pdf_path=doc_path,
            title=item.title,
        )
        if order_result and order_result.metrics:
            for m in order_result.metrics:
                om_result = decision_db.upsert_order_metric(
                    company_code=code,
                    fiscal_year_end=fiscal_year_end,
                    quarter=financials.quarter,
                    metric_name=m.metric_name,
                    value=m.value,
                    raw_value=m.raw_value,
                    unit=m.unit,
                    confidence=m.confidence,
                    raw_text=m.raw_text,
                    source_doc_id=disclosure_id,
                )
                logger.info(
                    f"[ORDER] {code} {fiscal_year_end} {financials.quarter} "
                    f"{m.metric_name}={m.value} ({om_result})"
                )
            decision_db.commit()
        elif order_reason and order_reason != "no_order_keywords":
            # 受注キーワードはあるが合計行なし等 → quarantine
            decision_db.quarantine_record(
                company_code=code,
                reason=order_reason,
                fiscal_year_end=fiscal_year_end,
                quarter=financials.quarter,
                metric_type="order_metrics",
                detail=f"title={item.title[:60]}",
                source_doc_id=disclosure_id,
            )
            decision_db.commit()
            logger.info(f"[ORDER] {code} quarantine: {order_reason[:80]}")
    except Exception as e:
        logger.warning(f"[ORDER] {code} extraction error (non-fatal): {e}")
        try:
            decision_db.quarantine_record(
                company_code=code,
                reason=f"extraction_error: {e}",
                fiscal_year_end=fiscal_year_end,
                quarter=financials.quarter,
                metric_type="order_metrics",
                source_doc_id=disclosure_id,
            )
            decision_db.commit()
        except Exception:
            pass

    # ── セグメント別売上・利益抽出（失敗しても全体を止めない） ──
    seg_metrics = {}  # v2 Phase 2 メトリクス
    try:
        seg_list, seg_reason = extract_segment_financials(
            pdf_path=doc_path,
            title=item.title,
            doc_id=disclosure_id,
            ticker=code,
        )
        if seg_list:
            seg_metrics["segment_records"] = len(seg_list)
            seg_metrics["segment_detected"] = True
            for seg in seg_list:
                seg_result = decision_db.upsert_segment(
                    company_code=code,
                    fiscal_year_end=fiscal_year_end,
                    quarter=financials.quarter,
                    segment_name=seg.segment_name,
                    segment_order=seg.segment_order,
                    segment_sales=seg.segment_sales,
                    segment_profit=seg.segment_profit,
                    raw_profit_label=seg.raw_profit_label,
                    data_source="tdnet",
                    actor="tdnet_ingest",
                    source="tdnet",
                    tdnet_disclosure_id=disclosure_id,
                    run_id=run_id,
                )
                logger.info(
                    f"[SEGMENT] {code} {financials.quarter} "
                    f"{seg.segment_name}: sales={seg.segment_sales} profit={seg.segment_profit} ({seg_result})"
                )
            decision_db.commit()

            # v2 メトリクス収集 (v2 が使われた場合)
            try:
                from src.analysis.segment_detection_v2 import run_segment_detection_v2
                v2r = run_segment_detection_v2(doc_path, doc_id=disclosure_id, ticker=code)
                seg_metrics["v2_adopted"] = v2r.used_v2
                seg_metrics["v1_fallback"] = not v2r.used_v2
                seg_metrics["candidate_tables"] = v2r.candidate_tables_count
                seg_metrics["scored_pages"] = v2r.scored_pages_count
                seg_metrics["unit_unknown"] = v2r.unit_info is None or v2r.unit_info.unit_multiplier is None if v2r.unit_info is not None else True
                seg_metrics["profit_col_role"] = v2r.score_summary.get("profit_col_role", "")
                seg_metrics["non_reportable_rows"] = v2r.score_summary.get("non_reportable_count", 0)
            except Exception:
                seg_metrics["v2_adopted"] = False
                seg_metrics["v1_fallback"] = True
        elif seg_reason:
            seg_metrics["segment_detected"] = False
            seg_metrics["quarantine_reason"] = seg_reason
            if seg_reason not in ("no_segment_table", "テキスト抽出不可"):
                decision_db.quarantine_record(
                    company_code=code,
                    reason=seg_reason,
                    fiscal_year_end=fiscal_year_end,
                    quarter=financials.quarter,
                    metric_type="segment",
                    detail=f"title={item.title[:60]}",
                    source_doc_id=disclosure_id,
                )
                decision_db.commit()
                logger.info(f"[SEGMENT] {code} quarantine: {seg_reason[:80]}")
    except Exception as e:
        logger.warning(f"[SEGMENT] {code} extraction error (non-fatal): {e}")
        seg_metrics["segment_detected"] = False
        try:
            decision_db.quarantine_record(
                company_code=code,
                reason=f"segment_extraction_error: {e}",
                fiscal_year_end=fiscal_year_end,
                quarter=financials.quarter,
                metric_type="segment",
                source_doc_id=disclosure_id,
            )
            decision_db.commit()
        except Exception:
            pass
    state_db.record(
        disclosure_id=disclosure_id, code=code,
        year=financials.fiscal_year, quarter=financials.quarter,
        status=Status.SUCCESS,
        new_values={
            "sales": sales,
            "gross_profit": gross_profit,
            "operating_profit": operating_profit,
        },
    )

    return {
        "status": db_result, "detail": result_detail, "code": code,
        "seg_metrics": seg_metrics,
        "source_type": "pdf" if doc_path and doc_path.lower().endswith(".pdf") else "zip",
        "disclosure_id": disclosure_id,
    }

# ============================================================
# サマリ集計 (Phase 2 メトリクス対応)
# ============================================================

def build_ingest_summary(results, all_items, target_items, success_count, run_id, elapsed):
    """ingest 結果からサマリ dict を構築する。"""
    summary = {
        "run_id": run_id,
        "total_fetched": len(all_items),
        "target_statements": len(target_items),
        "processed": len(results),
        "success": success_count,
        "inserted": sum(1 for r in results if r["status"] == "inserted"),
        "updated": sum(1 for r in results if r["status"] == "updated"),
        "no_change": sum(1 for r in results if r["status"] == "no_change"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "dry_run": sum(1 for r in results if r["status"] == "dry_run"),
    }
    summary["files_total"] = len(results)
    summary["files_pdf"] = sum(1 for r in results if r.get("source_type") == "pdf")
    summary["files_zip"] = sum(1 for r in results if r.get("source_type") == "zip")
    seg_records_total = 0
    seg_detected_docs = 0
    v2_adopted = 0
    v1_fallback = 0
    unit_unknown = 0
    profit_col_unknown = 0
    non_reportable_total = 0
    candidate_tables_sum = 0
    scored_pages_sum = 0
    docs_with_v2 = 0
    quarantine_reasons = {}
    for r in results:
        sm = r.get("seg_metrics", {})
        if not sm:
            continue
        seg_records_total += sm.get("segment_records", 0)
        if sm.get("segment_detected"):
            seg_detected_docs += 1
        if sm.get("v2_adopted"):
            v2_adopted += 1
            docs_with_v2 += 1
            candidate_tables_sum += sm.get("candidate_tables", 0)
            scored_pages_sum += sm.get("scored_pages", 0)
            non_reportable_total += sm.get("non_reportable_rows", 0)
            if sm.get("unit_unknown"):
                unit_unknown += 1
            if not sm.get("profit_col_role"):
                profit_col_unknown += 1
        if sm.get("v1_fallback"):
            v1_fallback += 1
        qr = sm.get("quarantine_reason", "")
        if qr:
            quarantine_reasons[qr] = quarantine_reasons.get(qr, 0) + 1
    summary["segment_records"] = seg_records_total
    summary["segment_detected_docs"] = seg_detected_docs
    summary["v2_adopted"] = v2_adopted
    summary["v1_fallback"] = v1_fallback
    summary["unit_unknown_count"] = unit_unknown
    summary["profit_col_unknown_count"] = profit_col_unknown
    summary["non_reportable_rows_count"] = non_reportable_total
    summary["avg_candidate_tables_per_doc"] = (
        round(candidate_tables_sum / docs_with_v2, 1) if docs_with_v2 else 0
    )
    summary["avg_scored_pages_per_doc"] = (
        round(scored_pages_sum / docs_with_v2, 1) if docs_with_v2 else 0
    )
    if quarantine_reasons:
        top = sorted(quarantine_reasons.items(), key=lambda x: -x[1])[:5]
        summary["quarantine_reason_top"] = dict(top)
    else:
        summary["quarantine_reason_top"] = {}
    summary["quarantined"] = summary["errors"] + sum(quarantine_reasons.values())
    summary["elapsed"] = round(elapsed, 2)
    return summary


def print_ingest_summary(summary):
    """人間向け表 + grep 用 key=value サマリを出力"""
    print("=" * 60)
    print("  結果サマリ")
    print("=" * 60)
    for label, key in [
        ("run_id", "run_id"), ("取得開示数", "total_fetched"),
        ("対象(決算短信)", "target_statements"), ("処理件数", "processed"),
        ("成功", "success"), ("INSERT", "inserted"), ("UPDATE", "updated"),
        ("変更なし", "no_change"), ("スキップ", "skipped"), ("エラー", "errors"),
    ]:
        print(f"  {label:16s}: {summary.get(key, 0)}")
    if summary.get("dry_run", 0) > 0:
        print(f"  {'dry-run':16s}: {summary['dry_run']}")
    if summary.get("skip_reasons"):
        print("  [skip内訳]")
        for reason, count in summary["skip_reasons"].items():
            print(f"    {reason}: {count}")
    if summary.get("failed_codes"):
        print(f"  失敗コード      : {', '.join(summary['failed_codes'])}")
    print()
    print("  [Segment v2 メトリクス]")
    for label, key in [
        ("segment_records", "segment_records"),
        ("segment_detected_docs", "segment_detected_docs"),
        ("v2_adopted", "v2_adopted"),
        ("v1_fallback", "v1_fallback"),
        ("unit_unknown", "unit_unknown_count"),
        ("profit_col_unknown", "profit_col_unknown_count"),
        ("non_reportable_rows", "non_reportable_rows_count"),
        ("avg_candidate_tables", "avg_candidate_tables_per_doc"),
        ("avg_scored_pages", "avg_scored_pages_per_doc"),
        ("quarantined", "quarantined"),
    ]:
        print(f"  {label:24s}: {summary.get(key, 0)}")
    print(f"  {'elapsed':24s}: {summary.get('elapsed', 0):.1f}s")
    qr_top = summary.get("quarantine_reason_top", {})
    if qr_top:
        print("  [quarantine_reason_top]")
        for reason, count in qr_top.items():
            print(f"    {reason}: {count}")
    print()
    kv_keys = [
        "files_total", "files_pdf", "files_zip", "success", "errors",
        "skipped", "quarantined", "segment_records", "segment_detected_docs",
        "v2_adopted", "v1_fallback", "avg_candidate_tables_per_doc",
        "avg_scored_pages_per_doc", "unit_unknown_count",
        "profit_col_unknown_count", "non_reportable_rows_count", "elapsed",
    ]
    kv_pairs = " ".join(f"{k}={summary.get(k, 0)}" for k in kv_keys)
    qr_str = ",".join(f"{k}={v}" for k, v in qr_top.items()) if qr_top else "none"
    summary_line = f"[SUMMARY] {kv_pairs} quarantine_reason_top={qr_str}"
    print(summary_line)
    logger.info(summary_line)
    print()


# ============================================================
# リプレイ（ローカル再現モード）
# ============================================================

def run_replay(zip_path: str, title: str = "リプレイ決算短信") -> dict:
    """
    ローカルZIPからの抽出のみ実行。ネットワーク不使用。

    Returns:
        {"status": str, "detail": str}
    """
    if not os.path.isfile(zip_path):
        return {"status": "error", "detail": f"ファイルが見つかりません: {zip_path}"}

    zip_hash = _sha256_file(zip_path)
    print(f"[REPLAY] ファイル: {zip_path}")
    print(f"[REPLAY] hash: {zip_hash}")

    financials, error = extract_financials(
        doc_path=zip_path,
        title=title,
        xbrl_path=zip_path,
    )

    if financials is None:
        return {"status": "error", "detail": f"抽出失敗: {error}"}

    result_detail = (
        f"sales={financials.sales} gp={financials.gross_profit} "
        f"op={financials.operating_profit} unit={financials.source_unit} "
        f"fy={financials.fiscal_year} q={financials.quarter}"
    )
    return {"status": "success", "detail": result_detail}


# ============================================================
# メイン処理
# ============================================================

def run_ingest(
    config: Config,
    company_code: str | None = None,
    dry_run: bool = False,
    db_path: str | None = None,
    dump_dir: str | None = None,
) -> dict:
    """
    ワンショットingest実行。

    Returns:
        {"total": int, "results": [...], "summary": {...}}
    """
    t0 = time.monotonic()
    run_id = f"ingest-{uuid.uuid4().hex[:8]}"

    # DB 初期化
    state_db_path = config.state_db_path
    decision_db_path = db_path or config.decision_db_path

    state_db = StateDB(state_db_path)
    decision_db = MigrationDB(decision_db_path)

    try:
        # ウォッチリスト設定
        watch_tickers = [company_code] if company_code else config.watch_tickers or None

        # 開示取得
        items = fetch_new_disclosures(
            watch_tickers=watch_tickers,
            is_processed_fn=state_db.is_processed if not dry_run else None,
        )

        # 決算短信のみフィルタ（予想修正は別処理）
        target_items = [
            item for item in items
            if item.disclosure_type == DisclosureType.FINANCIAL_STATEMENT
        ]
        non_target = len(items) - len(target_items)
        forecast_in_new = sum(
            1 for i in items
            if i.disclosure_type == DisclosureType.FORECAST_REVISION
        )
        logger.info(
            f"[INGEST] run={run_id} new_items={len(items)} "
            f"target_statements={len(target_items)} "
            f"non_target={non_target} "
            f"(forecast_revision={forecast_in_new}, other={non_target - forecast_in_new})"
        )

        results = []
        for item in target_items:
            try:
                result = _process_single(
                    item, config, state_db, decision_db, run_id,
                    dry_run=dry_run, dump_dir=dump_dir,
                )
                results.append(result)
            except Exception as e:
                results.append({
                    "status": "error",
                    "detail": f"予期しないエラー: {e}",
                    "code": item.ticker,
                })

        elapsed = time.monotonic() - t0

        # サマリ
        success_count = sum(1 for r in results if r["status"] in ("inserted", "updated", "no_change", "dry_run"))
        summary = build_ingest_summary(results, items, target_items, success_count, run_id, elapsed)

        # skip理由カウント
        skip_reasons: dict[str, int] = {}
        for r in results:
            if r["status"] == "skipped":
                detail = r.get("detail", "")
                if "SKIP_PDF_NOT_TANSHIN" in detail:
                    reason_key = "SKIP_NOT_TANSHIN_TITLE"
                elif "処理済み" in detail:
                    reason_key = "SKIP_ALREADY_PROCESSED"
                else:
                    reason_key = "SKIP_OTHER"
                skip_reasons[reason_key] = skip_reasons.get(reason_key, 0) + 1
        if skip_reasons:
            summary["skip_reasons"] = skip_reasons

        # 失敗doc_id一覧
        failed_ids = [r.get("code", "?") for r in results if r["status"] == "error"]
        if failed_ids:
            summary["failed_codes"] = failed_ids[:20]

        # 1行サマリログ
        skip_str = " ".join(f"{k}={v}" for k, v in skip_reasons.items()) if skip_reasons else ""
        logger.info(
            f"[INGEST] run={run_id} "
            f"processed={len(results)} success={success_count} "
            f"skipped={summary['skipped']} error={summary['errors']} "
            f"(tanshin={len(target_items)}) {skip_str}".rstrip()
        )

        # ── イベント検知パイプライン統合 ──
        # items(全取得文書)をevent_pipelineに渡す。
        # 失敗してもingest全体は成功扱い。
        try:
            from src.events.common_models import DocumentMeta
            from src.events.event_pipeline import process_documents

            event_docs = [
                DocumentMeta(
                    doc_id=item.disclosure_id,
                    ticker=item.ticker,
                    company_name=item.company_name,
                    title=item.title,
                    disclosure_datetime=item.published_at,
                    doc_url=item.doc_url,
                )
                for item in items  # 全文書（決算短信+予想修正+その他）
            ]

            webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "") if not dry_run else ""
            event_result = process_documents(
                docs=event_docs,
                db_path=decision_db_path,
                dry_run=dry_run,
                webhook_url=webhook_url,
            )
            summary["event_pipeline"] = {
                "processed": event_result.processed,
                "detected": event_result.detected,
                "saved": event_result.saved,
                "notified": event_result.notified,
                "errors": event_result.errors,
            }
            logger.info(
                f"[INGEST] event_pipeline: detected={event_result.detected} "
                f"saved={event_result.saved} notified={event_result.notified} "
                f"errors={event_result.errors}"
            )
        except Exception as e:
            logger.error(f"[INGEST] event_pipeline failed (non-fatal): {e}", exc_info=True)
            summary["event_pipeline"] = {"error": str(e)}

        return {"total": len(results), "results": results, "summary": summary}
    finally:
        decision_db.close()
        state_db.close()


# ============================================================
# CLI エントリポイント
# ============================================================

def main():
    # Windows cp932 対策
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    parser = argparse.ArgumentParser(
        description="TDNET開示→iXBRL抽出→DB反映（ワンショット）"
    )
    parser.add_argument(
        "--company-code", type=str, default=None,
        help="対象企業コード（例: 0812）。省略時はconfig.yamlのwatch_tickersを使用",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="DBファイルパス（省略時はconfig.yamlのdecision_db_pathを使用）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="DBに書き込まず結果表示のみ",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="config.yamlパス（省略時はプロジェクトルートから探索）",
    )
    parser.add_argument(
        "--dump-on-error", type=str, default=None, metavar="DIR",
        help="抽出失敗時にZIPとメタ情報をダンプするディレクトリ",
    )
    parser.add_argument(
        "--replay", type=str, default=None, metavar="ZIP_PATH",
        help="ローカルZIPから抽出のみ実行（ネットワーク不使用）",
    )
    args = parser.parse_args()

    # --- リプレイモード ---
    if args.replay:
        print("=" * 60)
        print("  TDNET Ingest - リプレイモード")
        print("=" * 60)
        print()
        result = run_replay(args.replay)
        status_icon = "✅" if result["status"] == "success" else "❌"
        print(f"  {status_icon} {result['status']}: {result['detail']}")
        print()
        sys.exit(0 if result["status"] == "success" else 1)

    # --- 通常モード ---
    # 設定読み込み
    config = load_config(args.config)

    # ロガーセットアップ
    global logger
    logger = setup_logger(config.log_path)

    print("=" * 60)
    print("  TDNET Ingest - ワンショット実行")
    print("=" * 60)
    print()

    if args.dry_run:
        print("[MODE] dry-run (DBに書き込みません)")
    if args.company_code:
        print(f"[対象] 企業コード: {args.company_code}")
    if args.dump_on_error:
        print(f"[DUMP] エラーダンプ先: {args.dump_on_error}")
    print(f"[DB]   {args.db or config.decision_db_path}")
    print()

    try:
        result = run_ingest(
            config=config,
            company_code=args.company_code,
            dry_run=args.dry_run,
            db_path=args.db,
            dump_dir=args.dump_on_error,
        )
    except Exception as e:
        print(f"[ERROR] 実行失敗: {e}")
        logger.error(f"[INGEST] 実行失敗: {e}", exc_info=True)
        sys.exit(1)

    # 結果表示
    summary = result["summary"]
    print_ingest_summary(summary)

    # 個別結果
    if result["results"]:
        print("[個別結果]")
        for r in result["results"]:
            status_icon = {
                "inserted": "+",
                "updated": "~",
                "no_change": "=",
                "skipped": "-",
                "error": "!",
                "dry_run": "?",
            }.get(r["status"], "?")
            print(f"  [{status_icon}] {r['status']:10s} | {r['detail']}")
        print()

    # --- 取り込み結果をJSONに保存（discord_alerts.py 用） ---
    from src.common_ticker import normalize_ticker as _to_ticker4

    ingested = []
    seen = set()
    for r in result["results"]:
        if r["status"] not in ("inserted", "updated"):
            continue
        code = _to_ticker4(r.get("code", ""))
        detail = r.get("detail", "")
        # detail例: "7203 2025-03-31 2Q sales=... gp=... op=..."
        parts = detail.split()
        # parts[0]も5桁の可能性があるのでperiod/quarterは[1],[2]から取る
        period = parts[1] if len(parts) > 1 else ""
        quarter = parts[2] if len(parts) > 2 else ""
        key = (code, period, quarter)
        if key in seen:
            continue
        seen.add(key)
        ingested.append({
            "ticker": code,
            "period": period,
            "quarter": quarter,
            "doc_category": "tanshin",
            "status": r["status"],
        })
    items_file = os.path.join(_PROJECT_ROOT, "logs", "last_ingested_items.json")
    os.makedirs(os.path.dirname(items_file), exist_ok=True)
    with open(items_file, "w", encoding="utf-8") as f:
        json.dump(ingested, f, ensure_ascii=False, indent=2)
    # 後方互換: 旧ファイル名にも書き出す
    tickers_file = os.path.join(_PROJECT_ROOT, "logs", "last_ingested_tickers.json")
    with open(tickers_file, "w", encoding="utf-8") as f:
        json.dump(ingested, f, ensure_ascii=False, indent=2)
    print(f"[TICKERS] {len(ingested)} items -> {items_file}")

    # 終了コード
    if summary["errors"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
