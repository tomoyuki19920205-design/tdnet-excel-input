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

import re

def _reiwa_to_fiscal_year_end(r_str: str) -> str | None:
    """R表記 -> fiscal_year_end (YYYY-MM-DD), または既に YYYY-MM-DD ならそのまま返す"""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", r_str):
        return r_str
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
    session=None,
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
    doc_path = download_document(item.doc_url, docs_dir, session=session)

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
        xbrl_path = download_document(item.xbrl_url, docs_dir, session=session)
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

    # ── 受注メトリクス抽出 ── [一時停止中]
    # 停止理由:
    #   受注系PDF抽出は、現状では以下の精度問題があるため本番ingestから一時停止。
    #   - 単位誤認: _detect_scale が全文先頭マッチのみのため、受注ページの千円が
    #     表紙の百万円に上書きされ、17,455,472千円→17,455,472百万円として保存される。
    #   - backlog/carryover 重複: BACKLOG_KEYWORDS と CARRYOVER_KEYWORDS が完全重複しており
    #     「次期繰越工事高」から backlog_total と carryover_construction_total が
    #     同じ値で二重生成される。
    #   - 本文誤爆: 同一行に複数KPI（「受注高A及び受注残高B」）がある場合、
    #     backlog_total が受注高側の値を拾う。
    #
    # 再開条件:
    #   受注系V2抽出ロジック（ページ別単位検出・KPI分離・合計行必須化）の実装後に
    #   このブロックのコメントアウトを解除して再開する。
    #
    # 実装・DBは保持: src/extractor.py の extract_order_metrics()、
    #   MigrationDB.upsert_order_metric()、order_metrics テーブルは削除しない。
    #
    # [DISABLED ORDER EXTRACTION BEGIN]
    # try:
    #     order_result, order_reason = extract_order_metrics(
    #         pdf_path=doc_path,
    #         title=item.title,
    #     )
    #     if order_result and order_result.metrics:
    #         for m in order_result.metrics:
    #             om_result = decision_db.upsert_order_metric(
    #                 company_code=code,
    #                 fiscal_year_end=fiscal_year_end,
    #                 quarter=financials.quarter,
    #                 metric_name=m.metric_name,
    #                 value=m.value,
    #                 raw_value=m.raw_value,
    #                 unit=m.unit,
    #                 confidence=m.confidence,
    #                 raw_text=m.raw_text,
    #                 source_doc_id=disclosure_id,
    #             )
    #             logger.info(
    #                 f"[ORDER] {code} {fiscal_year_end} {financials.quarter} "
    #                 f"{m.metric_name}={m.value} ({om_result})"
    #             )
    #         decision_db.commit()
    #     elif order_reason and order_reason != "no_order_keywords":
    #         # 受注キーワードはあるが合計行なし等 → quarantine
    #         decision_db.quarantine_record(
    #             company_code=code,
    #             reason=order_reason,
    #             fiscal_year_end=fiscal_year_end,
    #             quarter=financials.quarter,
    #             metric_type="order_metrics",
    #             detail=f"title={item.title[:60]}",
    #             source_doc_id=disclosure_id,
    #         )
    #         decision_db.commit()
    #         logger.info(f"[ORDER] {code} quarantine: {order_reason[:80]}")
    # except Exception as e:
    #     logger.warning(f"[ORDER] {code} extraction error (non-fatal): {e}")
    #     try:
    #         decision_db.quarantine_record(
    #             company_code=code,
    #             reason=f"extraction_error: {e}",
    #             fiscal_year_end=fiscal_year_end,
    #             quarter=financials.quarter,
    #             metric_type="order_metrics",
    #             source_doc_id=disclosure_id,
    #         )
    #         decision_db.commit()
    #     except Exception:
    #         pass
    # [DISABLED ORDER EXTRACTION END]
    logger.debug(f"[ORDER] {code} skipped (order extraction disabled)")

    # ── セグメント別売上・利益抽出（XBRL優先 + V4 PDF fallback） ──
    # extract_segment_financials (旧V1/V2/V3) は停止済み。
    seg_metrics: dict = {"v4_route": True}
    _V4_NORMAL_SKIP = {"single_segment_omitted", "no_segment_page", "no_segment_table", "skipped_normal"}
    try:
        _v4_segs_list = []
        _v4_ok = False
        _v4_reason = "none"

        # 1. XBRL抽出を試行
        if xbrl_path and os.path.isfile(xbrl_path):
            try:
                from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
                xbrl_raw_rows = extract_segments_from_xbrl_zip(
                    zip_path=xbrl_path,
                    period=fiscal_year_end,
                    quarter=financials.quarter
                )
                if xbrl_raw_rows:
                    # 当期分のみフィルタ
                    current_xbrl = [r for r in xbrl_raw_rows if r.period == fiscal_year_end]
                    for i, r in enumerate(current_xbrl):
                        _v4_segs_list.append({
                            "segment_name": r.raw_segment_name or r.normalized_segment_name,
                            "segment_order": i + 1,
                            "segment_sales": r.sales,
                            "segment_profit": r.profit,
                            "unit_raw": None,       # 既に百万円単位化済み
                            "unit_multiplier": None,
                            "raw_profit_label": "xbrl",
                        })
                    if _v4_segs_list:
                        _v4_ok = True
                        seg_metrics["xbrl_used"] = True
                        logger.info(f"[SEGMENT_XBRL] {code} XBRL extraction success: {len(_v4_segs_list)} segments")
            except Exception as e:
                logger.warning(f"[SEGMENT_XBRL] {code} exception: {e}")

        # 2. XBRL抽出が0件または失敗した場合はPDFフォールバック
        if not _v4_segs_list:
            from src.analysis.segment_detection_v4 import run_segment_detection_v4
            _v4r = run_segment_detection_v4(doc_path, ticker=code)
            _v4_segs_list = getattr(_v4r, "segments", []) or []
            _v4_ok = bool(_v4r.success or _v4_segs_list)
            _v4_reason = getattr(_v4r, "quarantine_reason", None) or "none"
            seg_metrics["xbrl_used"] = False

        if _v4_ok and _v4_segs_list:
            # ── V4成功：保存 ──
            seg_metrics["segment_records"] = len(_v4_segs_list)
            seg_metrics["segment_detected"] = True
            seg_metrics["v4_success"] = True
            for seg in _v4_segs_list:
                seg_result = decision_db.upsert_segment(
                    company_code=code,
                    fiscal_year_end=fiscal_year_end,
                    quarter=financials.quarter,
                    segment_name=seg.get("segment_name", "") if isinstance(seg, dict) else getattr(seg, "segment_name", ""),
                    segment_order=seg.get("segment_order", 0) if isinstance(seg, dict) else getattr(seg, "segment_order", 0),
                    segment_sales=seg.get("segment_sales") if isinstance(seg, dict) else getattr(seg, "segment_sales", None),
                    segment_profit=seg.get("segment_profit") if isinstance(seg, dict) else getattr(seg, "segment_profit", None),
                    unit_raw=seg.get("unit_raw") if isinstance(seg, dict) else getattr(seg, "unit_raw", None),
                    unit_multiplier=seg.get("unit_multiplier") if isinstance(seg, dict) else getattr(seg, "unit_multiplier", None),
                    raw_profit_label=seg.get("raw_profit_label", "") if isinstance(seg, dict) else getattr(seg, "raw_profit_label", ""),
                    data_source="tdnet",
                    actor="tdnet_ingest_v4",
                    source="v4",
                    tdnet_disclosure_id=disclosure_id,
                    run_id=run_id,
                )
                _sname = seg.get("segment_name", "") if isinstance(seg, dict) else getattr(seg, "segment_name", "")
                _ssales = seg.get("segment_sales") if isinstance(seg, dict) else getattr(seg, "segment_sales", None)
                _sprofit = seg.get("segment_profit") if isinstance(seg, dict) else getattr(seg, "segment_profit", None)
                logger.info(
                    f"[SEGMENT_V4] {code} {financials.quarter} "
                    f"{_sname}: sales={_ssales} profit={_sprofit} ({seg_result})"
                )
            decision_db.commit()
            logger.info(f"[SEGMENT_V4] {code} ok segs={len(_v4_segs_list)} reason={_v4_reason}")

        elif _v4_reason in _V4_NORMAL_SKIP:
            # ── 正常skip（単一セグメント省略等）：quarantine不要 ──
            seg_metrics["segment_detected"] = False
            seg_metrics["v4_skipped_normal"] = True
            seg_metrics["v4_skip_reason"] = _v4_reason
            logger.info(f"[SEGMENT_V4] {code} normal_skip reason={_v4_reason}")

        else:
            # ── V4失敗・低信頼・想定外 ──
            seg_metrics["segment_detected"] = False
            seg_metrics["v4_quarantined"] = True
            seg_metrics["quarantine_reason"] = _v4_reason
            if _v4_reason not in ("none",):
                decision_db.quarantine_record(
                    company_code=code,
                    reason=f"v4:{_v4_reason}",
                    fiscal_year_end=fiscal_year_end,
                    quarter=financials.quarter,
                    metric_type="segment",
                    detail=f"title={item.title[:60]}",
                    source_doc_id=disclosure_id,
                )
                decision_db.commit()
            logger.info(f"[SEGMENT_V4] {code} quarantine reason={_v4_reason}")

    except Exception as _v4_ex:
        logger.warning(f"[SEGMENT_V4] {code} exception (non-fatal): {_v4_ex}")
        seg_metrics["segment_detected"] = False
        seg_metrics["v4_exception"] = True
        seg_metrics["quarantine_reason"] = f"v4_exception:{_v4_ex!s:.80}"
        try:
            decision_db.quarantine_record(
                company_code=code,
                reason=f"v4_exception:{_v4_ex!s:.120}",
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
    v4_success = 0
    v4_skipped_normal = 0
    v4_quarantined = 0
    v4_exception = 0
    quarantine_reasons: dict[str, int] = {}
    skip_reason_counts: dict[str, int] = {}
    for r in results:
        sm = r.get("seg_metrics", {})
        if not sm:
            continue
        seg_records_total += sm.get("segment_records", 0)
        if sm.get("segment_detected"):
            seg_detected_docs += 1
        if sm.get("v4_success"):
            v4_success += 1
        if sm.get("v4_skipped_normal"):
            v4_skipped_normal += 1
            sr = sm.get("v4_skip_reason", "unknown")
            skip_reason_counts[sr] = skip_reason_counts.get(sr, 0) + 1
        if sm.get("v4_quarantined"):
            v4_quarantined += 1
        if sm.get("v4_exception"):
            v4_exception += 1
        qr = sm.get("quarantine_reason", "")
        if qr:
            quarantine_reasons[qr] = quarantine_reasons.get(qr, 0) + 1
    summary["segment_records"] = seg_records_total
    summary["segment_detected_docs"] = seg_detected_docs
    summary["v4_success"] = v4_success
    summary["v4_skipped_normal"] = v4_skipped_normal
    summary["v4_quarantined"] = v4_quarantined
    summary["v4_exception"] = v4_exception
    summary["v4_segments_total"] = seg_records_total
    summary["avg_segments_per_doc"] = (
        round(seg_records_total / seg_detected_docs, 1) if seg_detected_docs else 0
    )
    if quarantine_reasons:
        top = sorted(quarantine_reasons.items(), key=lambda x: -x[1])[:5]
        summary["quarantine_reason_top"] = dict(top)
    else:
        summary["quarantine_reason_top"] = {}
    if skip_reason_counts:
        summary["v4_skip_reason_top"] = dict(
            sorted(skip_reason_counts.items(), key=lambda x: -x[1])[:5]
        )
    summary["quarantined"] = summary["errors"] + v4_quarantined + v4_exception
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
    print("  [Segment V4 メトリクス]")
    for label, key in [
        ("segment_records",      "segment_records"),
        ("segment_detected_docs", "segment_detected_docs"),
        ("v4_success",           "v4_success"),
        ("v4_skipped_normal",    "v4_skipped_normal"),
        ("v4_quarantined",       "v4_quarantined"),
        ("v4_exception",         "v4_exception"),
        ("avg_segments_per_doc", "avg_segments_per_doc"),
        ("quarantined",          "quarantined"),
    ]:
        print(f"  {label:24s}: {summary.get(key, 0)}")
    print(f"  {'elapsed':24s}: {summary.get('elapsed', 0):.1f}s")
    qr_top = summary.get("quarantine_reason_top", {})
    if qr_top:
        print("  [quarantine_reason_top]")
        for reason, count in qr_top.items():
            print(f"    {reason}: {count}")
    skip_top = summary.get("v4_skip_reason_top", {})
    if skip_top:
        print("  [v4_skip_reason_top]")
        for reason, count in skip_top.items():
            print(f"    {reason}: {count}")
    print()
    kv_keys = [
        "files_total", "files_pdf", "files_zip", "success", "errors",
        "skipped", "quarantined", "segment_records", "segment_detected_docs",
        "v4_success", "v4_skipped_normal", "v4_quarantined", "v4_exception",
        "avg_segments_per_doc", "elapsed",
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
    earnings_db_path: str | None = None,
    dump_dir: str | None = None,
    skip_notify: bool = False,
    yanoshin_timeout_sec: float | None = None,
) -> dict:
    """
    ワンショットingest実行。

    Returns:
        {"total": int, "results": [...], "summary": {...}}
    """
    t0 = time.monotonic()
    run_id = f"ingest-{uuid.uuid4().hex[:8]}"
    start_iso = datetime.now(JST).isoformat(timespec='seconds')

    # 進捗カウンタ初期化
    total_candidates = 0
    processed_count = 0
    success_count = 0
    skipped_count = 0
    failed_count = 0
    last_ticker = ""
    last_step = "init"


    # DB 初期化
    state_db_path = config.state_db_path
    decision_db_path = db_path or config.decision_db_path

    state_db = StateDB(state_db_path)
    decision_db = MigrationDB(decision_db_path)

    # === Phase 4-1B: Lock management ===
    process_name = "TDNET_Realtime"
    stale_after_sec = 180
    lock_id = uuid.uuid4().hex

    active_lock = state_db.get_active_process_lock(process_name)
    if active_lock:
        hb_str = active_lock.get("heartbeat_at", "")
        if hb_str:
            try:
                from src.utils import now_jst_str
                from datetime import datetime as dt
                hb_time = dt.strptime(hb_str, "%Y-%m-%d %H:%M:%S")
                now_time = dt.strptime(now_jst_str(), "%Y-%m-%d %H:%M:%S")
                if (now_time - hb_time).total_seconds() <= stale_after_sec:
                    logger.info("[LOCK] already running, skip this scheduler tick")
                    return {"total": 0, "results": [], "summary": {"status": "skipped_by_lock"}}
                else:
                    logger.warning("[LOCK] stale lock detected")
            except Exception as e:
                logger.warning(f"Failed to parse heartbeat_at: {e}")

    lock_acquired = state_db.acquire_process_lock(lock_id, process_name, os.getpid(), stale_after_sec)
    if not lock_acquired:
        logger.info("[LOCK] already running, skip this scheduler tick")
        return {"total": 0, "results": [], "summary": {"status": "skipped_by_lock"}}

    logger.info("[LOCK] acquire success")
    # ===================================

    session = None
    try:
        import requests
        session = requests.Session()

        # ウォッチリスト設定
        watch_tickers = [company_code] if company_code else config.watch_tickers or None

        # 開示取得
        items = fetch_new_disclosures(
            watch_tickers=watch_tickers,
            is_processed_fn=state_db.is_processed if not dry_run else None,
            target_date=getattr(config, "start_date", None),
            session=session,
            yanoshin_timeout_sec=yanoshin_timeout_sec,
        )

        # [JQUANTS_SHADOW] Phase 2: Shadow Run — JQUANTS_SHADOW_ENABLED=1 の場合のみ実行
        # DB保存なし・Discord通知なし・本番フロー影響なし
        # 例外時も本番処理を継続（try/except は _run_jquants_shadow 内部で担保）
        _run_jquants_shadow(
            items,
            date_str=getattr(config, "start_date", None),
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
        total_candidates = len(target_items)
        
        logger.info(
            f"[RUN] run_id={run_id} target_date={getattr(config, 'start_date', 'today') or 'today'} "
            f"mode={'dry_run' if dry_run else 'realtime'} "
            f"total_disclosures={len(items)} tanshin_candidates={total_candidates} "
            f"already_success=unknown pending={total_candidates} "
            f"started_at={start_iso}"
        )


        # V2 takeover 対象の事前計算
        enable_v2 = os.environ.get("ENABLE_EARNINGS_V2_PIPELINE", "0") == "1"
        use_subprocess = os.environ.get("USE_SUBPROCESS_WORKER", "0") == "1"
        real_save = os.environ.get("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "0") == "1"
        allowlist_str = os.environ.get("EARNINGS_SUBPROCESS_ALLOWLIST", "")
        v2_allowlist = [t.strip() for t in allowlist_str.split(",") if t.strip()] if allowlist_str else []
        v2_takeover_active = enable_v2 and use_subprocess and real_save

        results = []
        last_heartbeat_time = time.monotonic()
        last_progress_time = time.monotonic()

        for i, item in enumerate(target_items):
            current_time = time.monotonic()
            last_ticker = item.ticker
            last_step = "ingest"

            if current_time - last_heartbeat_time >= 30:
                try:
                    state_db.update_process_lock_heartbeat(process_name, processed_count=processed_count, current_step=last_step, total_candidates=total_candidates)
                except Exception as e:
                    logger.warning(f"[LOCK] heartbeat update failed (non-fatal): {e}")
                last_heartbeat_time = current_time
                logger.info("[LOCK] heartbeat updated")

            if processed_count > 0 and (processed_count % 10 == 0 or current_time - last_progress_time >= 30):
                elapsed_sec = current_time - t0
                avg_sec = elapsed_sec / max(processed_count, 1)
                rem_count = total_candidates - processed_count
                eta_sec = avg_sec * rem_count
                logger.info(
                    f"[PROGRESS] run_id={run_id} processed={processed_count}/{total_candidates} "
                    f"success={success_count} skipped={skipped_count} failed={failed_count} "
                    f"remaining={rem_count} elapsed_sec={int(elapsed_sec)} avg_sec_per_item={avg_sec:.2f} "
                    f"eta_sec={int(eta_sec)} current_ticker={last_ticker} current_step={last_step}"
                )
                last_progress_time = current_time

            # ── 旧ルートからの V2 takeover 対象除外 ──
            if v2_takeover_active and item.ticker in v2_allowlist and item.disclosure_id:
                results.append({
                    "status": "skipped",
                    "detail": "V2_TAKEOVER_ACTIVE",
                    "code": item.ticker,
                    "source_type": "pdf",
                    "disclosure_id": item.disclosure_id,
                })
                skipped_count += 1
                processed_count += 1
                continue

            try:
                result = _process_single(
                    item, config, state_db, decision_db, run_id,
                    dry_run=dry_run, dump_dir=dump_dir, session=session,
                )
                results.append(result)
                if result.get("status") in ("inserted", "updated", "no_change", "dry_run"):
                    success_count += 1
                elif result.get("status") == "skipped":
                    skipped_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                results.append({
                    "status": "error",
                    "detail": f"予期しないエラー: {e}",
                    "code": item.ticker,
                })
                failed_count += 1
            
            processed_count += 1

        elapsed = time.monotonic() - t0
        avg_sec = elapsed / max(processed_count, 1)
        rem_count = total_candidates - processed_count
        logger.info(
            f"[SUMMARY] run_id={run_id} final_status=completed total={total_candidates} "
            f"processed={processed_count} success={success_count} skipped={skipped_count} "
            f"failed={failed_count} remaining={rem_count} elapsed_sec={int(elapsed)} "
            f"avg_sec_per_item={avg_sec:.2f} completed_at={datetime.now(JST).isoformat(timespec='seconds')}"
        )

        # サマリ
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
        event_result = None
        try:
            # フールプルーフ: run_ingest() を直接 import 経由で呼んだ場合も .env を確実に読む
            try:
                from lib.pipeline.db import load_env as _load_env
                _load_env(_PROJECT_ROOT)
            except Exception:
                pass

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

            webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "") if not (dry_run or skip_notify) else ""

            # 診断ログ（webhook URL の中身は出力しない）
            logger.info(
                f"[INGEST] event_pipeline START "
                f"event_docs={len(event_docs)} "
                f"webhook_url_present={bool(webhook_url)} "
                f"dry_run={dry_run} "
                f"db_path={decision_db_path}"
            )

            event_result = process_documents(
                docs=event_docs,
                db_path=decision_db_path,
                dry_run=dry_run,
                webhook_url=webhook_url,
            )

            _ep_skipped = (
                getattr(event_result, "skipped", None)
                or (event_result.processed - event_result.detected)
            )
            summary["event_pipeline"] = {
                "processed": event_result.processed,
                "detected": event_result.detected,
                "saved": event_result.saved,
                "notified": event_result.notified,
                "skipped": _ep_skipped,
                "errors": event_result.errors,
            }
            logger.info(
                f"[INGEST] event_pipeline DONE "
                f"processed={event_result.processed} "
                f"detected={event_result.detected} "
                f"saved={event_result.saved} "
                f"notified={event_result.notified} "
                f"skipped={_ep_skipped} "
                f"errors={event_result.errors}"
            )
        except Exception as e:
            logger.error(f"[INGEST] event_pipeline failed (non-fatal): {e}", exc_info=True)
            summary["event_pipeline"] = {"error": str(e)}

        # ── 非短信イベントの処理済記録（無駄な再評価を抑制） ──
        if not dry_run and event_result is not None and hasattr(event_result, "skipped_all_doc_ids"):
            skipped_all_set = set(event_result.skipped_all_doc_ids)
            target_ids = {i.disclosure_id for i in target_items}
            
            logger.info(f"[TDNET_Realtime] non_target_skip_record START count={len(skipped_all_set)}")
            
            recorded_count = 0
            already_skipped_count = 0
            errors = 0
            for item in items:
                if item.disclosure_id in skipped_all_set and item.disclosure_id not in target_ids:
                    try:
                        if state_db.is_processed(item.disclosure_id):
                            already_skipped_count += 1
                        else:
                            state_db.record(
                                disclosure_id=item.disclosure_id,
                                code=item.ticker,
                                year="", quarter="",
                                status="skipped_non_target",
                            )
                            recorded_count += 1
                    except Exception:
                        errors += 1
            
            logger.info(
                f"[TDNET_Realtime] non_target_skip_record DONE "
                f"recorded={recorded_count} "
                f"skipped_already={already_skipped_count} "
                f"errors={errors}"
            )
            if recorded_count > 0 and "event_pipeline" in summary:
                summary["event_pipeline"]["recorded_skipped_non_target"] = recorded_count

        # ── 決算短信V2詳細解析（feature flag: ENABLE_EARNINGS_V2_PIPELINE=1） ──
        # earnings_summaries 保存 + Supabase tdnet_events 反映。
        # webhook_url="" 固定: Discord通知・time.sleep(1.5) を完全回避。
        # 失敗してもingest全体は成功扱い。
        if os.environ.get("ENABLE_EARNINGS_V2_PIPELINE", "0") == "1":
            import sqlite3 as _sqlite3
            from src.events.earnings_production_pipeline import run_earnings_production
            logger.info("[EARNINGS_V2] enabled: running earnings_production_pipeline")
            
            earnings_db_path = earnings_db_path if earnings_db_path else decision_db_path
            logger.info(f"[EARNINGS_V2] resolved_db_path={earnings_db_path}")
            
            _ev2_conn = _sqlite3.connect(earnings_db_path)
            try:
                _ev2_result = run_earnings_production(
                    docs=items,          # 全取得文書（内部で決算短信のみフィルタ）
                    conn=_ev2_conn,
                    webhook_url="",      # Discord通知・sleep を回避
                    dry_run=dry_run,
                    state_db=state_db,
                    session=session,
                )
                summary["earnings_v2"] = {
                    "tanshin": _ev2_result.tanshin_count,
                    "saved": _ev2_result.saved_count,
                    "skipped": _ev2_result.already_exists_count,
                    "notified": _ev2_result.notified_count,
                    "errors": len(_ev2_result.errors),
                }
                logger.info(
                    f"[EARNINGS_V2] completed: "
                    f"total={_ev2_result.tanshin_count} "
                    f"saved={_ev2_result.saved_count} "
                    f"skipped={_ev2_result.already_exists_count} "
                    f"notified={_ev2_result.notified_count} "
                    f"tickers={','.join(_ev2_result.saved_tickers) or '-'}"
                )
            except Exception as _ev2_e:
                logger.warning(f"[EARNINGS_V2] failed non-fatal: {_ev2_e}")
                summary["earnings_v2"] = {"error": str(_ev2_e)}
            finally:
                if _ev2_conn is not None:
                    _ev2_conn.close()

        # ── prior_comparative realtime hook ──
        if os.environ.get("PRIOR_COMPARATIVE_REALTIME_ENABLED", "0") == "1":
            try:
                from lib.pipeline.prior_comparative_realtime import run_prior_comparative_realtime_hook
                logger.info("[PRIOR_COMPARATIVE_REALTIME] enabled, calling hook")
                max_docs = int(os.environ.get("PRIOR_COMPARATIVE_REALTIME_MAX_DOCS_PER_RUN", "10"))
                pc_summary = run_prior_comparative_realtime_hook(target_items, max_docs)
                summary["prior_comparative"] = pc_summary
            except Exception as e:
                logger.error(f"[PRIOR_COMPARATIVE_REALTIME] hook outer exception: {e}", exc_info=True)
                summary["prior_comparative"] = {"error": str(e)}

        return {"total": len(results), "results": results, "summary": summary}
    finally:
        if session:
            try:
                session.close()
            except Exception:
                pass
        
        import sys
        exc_type, exc_value, _ = sys.exc_info()
        status = "failed" if exc_type is not None else "completed"

        if status == "failed":
            elapsed = time.monotonic() - t0
            rem_count = total_candidates - processed_count
            logger.error(
                f"[SUMMARY] run_id={run_id} final_status=failed total={total_candidates} "
                f"processed={processed_count} success={success_count} skipped={skipped_count} "
                f"failed={failed_count} remaining={rem_count} elapsed_sec={int(elapsed)} "
                f"last_ticker={last_ticker} last_step={last_step} last_error=\"{exc_type.__name__}: {exc_value}\""
            )

        try:
            state_db.release_process_lock(process_name, status=status)
            if status == "completed":
                logger.info("[LOCK] release success")
            else:
                logger.info("[LOCK] release success (failed status)")
        except Exception as e:
            logger.warning(f"[LOCK] release failed: {e}")
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
        "--earnings-db", type=str, default=None,
        help="earnings_summaries保存先（省略時はdecision_db_pathを使用）",
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
            earnings_db_path=args.earnings_db,
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


# ============================================================
# J-Quants Shadow Run ヘルパー (Phase 2)
# ============================================================

def _run_jquants_shadow(
    legacy_items: list,
    *,
    date_str=None,
) -> None:
    """
    J-Quants TDnet Shadow Run を実行する。

    既存 YANOSHIN/HTML 取得結果 (legacy_items) と J-Quants 取得結果を並走比較し、
    差分をログのみで記録する。DB保存・Discord通知・本番フロー変更は一切行わない。

    安全制約:
      - JQUANTS_SHADOW_ENABLED=1 の場合のみ実行 (デフォルト OFF)
      - 全体を try/except で囲む (例外でも本番処理は継続)
      - DB保存なし / Discord通知なし / 本番フロー変更なし
      - APIキー・token・認証ヘッダー・.env値は出力しない

    Args:
        legacy_items: fetch_new_disclosures() の返り値 (list[DisclosureItem])
        date_str:     対象日 (YYYY-MM-DD or YYYYMMDD or None=当日)
    """
    if os.environ.get("JQUANTS_SHADOW_ENABLED", "0") != "1":
        return  # デフォルト OFF — 本番フローへの影響ゼロ

    _shadow_logger = logging.getLogger("jquants.shadow")

    try:
        from src.utils import today_yyyymmdd
        from src.jquants.shadow_runner import run_shadow_comparison

        # date_str を YYYYMMDD 形式に正規化
        if date_str and isinstance(date_str, str) and "-" in date_str:
            target = date_str.replace("-", "")
        elif date_str:
            target = str(date_str)
        else:
            target = today_yyyymmdd()

        _shadow_logger.info(
            f"[JQUANTS_SHADOW_TRIGGER] "
            f"date={target!r} "
            f"legacy_count={len(legacy_items)}"
        )

        result = run_shadow_comparison(
            target,
            legacy_items=legacy_items,  # list[DisclosureItem] をそのまま渡す
        )

        _shadow_logger.info(
            f"[JQUANTS_SHADOW_SUMMARY] "
            f"date={target!r} "
            f"jq_total={result.jquants_total} "
            f"legacy_total={result.legacy_total} "
            f"truncation_gap={result.truncation_gap} "
            f"missing_in_legacy={len(result.missing_in_legacy)} "
            f"matched={result.matched_count} "
            f"fetch_error={result.fetch_error!r}"
        )

    except Exception as e:
        # Shadow Run の失敗は非致命的 — ログのみ出力し本番処理を継続する
        _shadow_logger.error(
            f"[JQUANTS_SHADOW_ERROR] "
            f"shadow run failed (non-fatal): {e}"
        )
        # raise しない — 本番フローを止めない


if __name__ == "__main__":
    main()
