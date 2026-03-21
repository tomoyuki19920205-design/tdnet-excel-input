#!/usr/bin/env python3
"""event_pipeline.py — TDNET文書からのイベント検知パイプライン

DocumentMeta のリストを受け取り、各文書に対して
buyback / forecast_revision / dividend_revision の分類・抽出・保存を行う。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from .common_models import (
    DocumentMeta,
    EventRecord,
    EventType,
    PipelineResult,
)
from .common_normalizers import compute_fingerprint, compute_text_hash
from .common_storage import ensure_events_table, upsert_event, get_unnotified_events, mark_notified, mark_filtered
from .common_notify import send_event_discord
from .notify_rules import filter_and_sort_events, should_notify_event
from .tdnet_event_store import save_event_to_supabase

# classifiers
from .buyback_classifier import classify_buyback
from .forecast_classifier import classify_forecast
from .dividend_classifier import classify_dividend

# extractors
from .buyback_extractor import extract_buyback_event
from .forecast_extractor import extract_forecast_revision
from .dividend_extractor import extract_dividend_revision
from .price_service import get_last_close

logger = logging.getLogger("event_pipeline")

# buyback event_type → subtype マッピング
_BUYBACK_SUBTYPE_MAP = {
    "buyback_decision": "resolution",
    "buyback_status": "status",
    "buyback_result": "result",
    "treasury_cancel": "cancellation",
}


# ============================================================
# テキスト取得ヘルパー
# ============================================================
def _get_text(doc: DocumentMeta) -> str:
    """文書のテキストを取得。text_body があればそのまま、なければ pdf_path / doc_url から抽出。"""
    if doc.text_body:
        return doc.text_body

    # ローカルPDFから抽出
    if doc.pdf_path and os.path.isfile(doc.pdf_path):
        extracted = _extract_text_from_pdf_path(doc.pdf_path)
        if extracted:
            return extracted

    # URL から PDF をダウンロードして抽出
    if doc.doc_url:
        extracted = _extract_text_from_pdf_url(doc.doc_url)
        if extracted:
            return extracted

    return ""


def _extract_text_from_pdf_path(pdf_path: str) -> str:
    """ローカルPDFファイルからテキスト抽出"""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            texts = [page.extract_text() or "" for page in pdf.pages[:10]]
            return "\n".join(texts)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"[EVENT] PDF text extraction failed: {pdf_path}: {e}")
    return ""


def _extract_text_from_pdf_url(url: str) -> str:
    """URLからPDFをダウンロードしてテキスト抽出"""
    if not url:
        return ""
    try:
        import io
        import pdfplumber
        import requests

        # TDNET PDFのURLかチェック
        if not any(h in url for h in ["tdnet.info", "disclosure.edinet"]):
            return ""

        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TDNETEventBot/1.0)"
        })
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not resp.content[:5] == b"%PDF-":
            return ""

        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            texts = [page.extract_text() or "" for page in pdf.pages[:10]]
            text = "\n".join(texts)
            if text.strip():
                logger.debug(f"[EVENT] PDF downloaded and extracted: {url[:60]}... ({len(text)} chars)")
            return text

    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"[EVENT] PDF download failed (non-fatal): {url[:60]}... {e}")
    return ""


# ============================================================
# buyback → EventRecord 変換
# ============================================================
def _buyback_to_event_record(
    doc: DocumentMeta,
    buyback_event,
    subtype: str,
) -> EventRecord:
    """BuybackEvent → EventRecord"""
    payload = buyback_event.to_dict()

    # fingerprint: event_type + ticker + 正規化主要値
    fp_parts = [
        "buyback",
        doc.ticker,
        subtype,
        str(buyback_event.shares_limit or ""),
        str(buyback_event.amount_limit_million_yen or ""),
        str(buyback_event.shares_acquired or ""),
        str(buyback_event.shares_cancelled or ""),
        buyback_event.board_resolution_date or "",
        buyback_event.start_date or "",
    ]
    fingerprint = compute_fingerprint(*fp_parts)

    # summary
    summary_parts = [f"自社株買い({subtype})"]
    if buyback_event.shares_limit:
        summary_parts.append(f"上限{buyback_event.shares_limit:,}株")
    if buyback_event.amount_limit_million_yen:
        summary_parts.append(f"上限{buyback_event.amount_limit_million_yen:.0f}百万円")
    summary = " ".join(summary_parts)

    importance = 70 if subtype == "resolution" else 50

    return EventRecord(
        source_doc_id=doc.doc_id,
        ticker=doc.ticker,
        company_name=doc.company_name,
        disclosure_datetime=doc.disclosure_datetime,
        title=doc.title,
        event_type=EventType.BUYBACK,
        subtype=subtype,
        importance=importance,
        summary_text=summary,
        raw_payload_json=json.dumps({"title": doc.title}, ensure_ascii=False),
        extracted_payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        fingerprint=fingerprint,
    )


# ============================================================
# forecast_revision → EventRecord 変換
# ============================================================
def _forecast_to_event_record(
    doc: DocumentMeta,
    forecast_event,
) -> EventRecord:
    payload = forecast_event.to_dict()

    fp_parts = [
        "forecast_revision",
        doc.ticker,
        forecast_event.period_label,
        str(forecast_event.revised_sales or ""),
        str(forecast_event.revised_op or ""),
        str(forecast_event.revised_net_income or ""),
        forecast_event.subtype,
    ]
    fingerprint = compute_fingerprint(*fp_parts)

    summary_parts = [forecast_event.subtype]
    if forecast_event.period_label:
        summary_parts.append(forecast_event.period_label)
    summary = " ".join(summary_parts)

    return EventRecord(
        source_doc_id=doc.doc_id,
        ticker=doc.ticker,
        company_name=doc.company_name,
        disclosure_datetime=doc.disclosure_datetime,
        title=doc.title,
        event_type=EventType.FORECAST_REVISION,
        subtype=forecast_event.subtype,
        importance=forecast_event.importance,
        summary_text=summary,
        raw_payload_json=json.dumps({"title": doc.title}, ensure_ascii=False),
        extracted_payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        fingerprint=fingerprint,
    )


# ============================================================
# dividend_revision → EventRecord 変換
# ============================================================
def _dividend_to_event_record(
    doc: DocumentMeta,
    dividend_event,
    db_path: str = "",
) -> EventRecord:
    payload = dividend_event.to_dict()

    # 前日終値を取得して payload に追加
    if doc.ticker and db_path:
        closing_price = get_last_close(doc.ticker, db_path=db_path)
        if closing_price is not None:
            payload["closing_price"] = closing_price
    # annual_total_revised をペイロードに含める（利回り計算用）
    if hasattr(dividend_event, 'annual_total_revised') and dividend_event.annual_total_revised is not None:
        payload["annual_total_revised"] = dividend_event.annual_total_revised

    fp_parts = [
        "dividend_revision",
        doc.ticker,
        dividend_event.fiscal_period,
        dividend_event.dividend_basis,
        str(dividend_event.revised_dividend_per_share or ""),
        str(dividend_event.special_dividend_per_share or ""),
        str(dividend_event.commemorative_dividend_per_share or ""),
        dividend_event.subtype,
    ]
    fingerprint = compute_fingerprint(*fp_parts)

    summary_parts = [dividend_event.subtype]
    if dividend_event.fiscal_period:
        summary_parts.append(dividend_event.fiscal_period)
    summary = " ".join(summary_parts)

    return EventRecord(
        source_doc_id=doc.doc_id,
        ticker=doc.ticker,
        company_name=doc.company_name,
        disclosure_datetime=doc.disclosure_datetime,
        title=doc.title,
        event_type=EventType.DIVIDEND_REVISION,
        subtype=dividend_event.subtype,
        importance=dividend_event.importance,
        summary_text=summary,
        raw_payload_json=json.dumps({"title": doc.title}, ensure_ascii=False),
        extracted_payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        fingerprint=fingerprint,
    )


# ============================================================
# 単一文書処理
# ============================================================
def _process_single_document(
    doc: DocumentMeta,
    conn: sqlite3.Connection,
    event_types: set[str] | None = None,
    dry_run: bool = False,
    db_path: str = "",
) -> list[dict]:
    """1文書を分類・抽出・保存する。

    Returns: [{event_type, subtype, action, event_id}, ...]
    """
    results = []
    text = _get_text(doc)
    title = doc.title

    allowed = event_types or {EventType.BUYBACK, EventType.FORECAST_REVISION, EventType.DIVIDEND_REVISION}

    # ---- buyback ----
    if EventType.BUYBACK in allowed:
        try:
            cls_result = classify_buyback(title, text[:2000])
            if cls_result.is_buyback_related and cls_result.event_type_candidate:
                event_type_raw = cls_result.event_type_candidate
                subtype = _BUYBACK_SUBTYPE_MAP.get(event_type_raw, event_type_raw)
                buyback_ev = extract_buyback_event(
                    text=text,
                    event_type=event_type_raw,
                    ticker=doc.ticker,
                    disclosure_date=doc.disclosure_datetime,
                    title=title,
                    source_doc_id=doc.doc_id,
                    source_url=doc.doc_url,
                )
                record = _buyback_to_event_record(doc, buyback_ev, subtype)
                if dry_run:
                    results.append({
                        "event_type": EventType.BUYBACK, "subtype": subtype,
                        "action": "dry_run", "event_id": record.event_id,
                        "summary": record.summary_text,
                    })
                else:
                    action, eid = upsert_event(conn, record)
                    results.append({
                        "event_type": EventType.BUYBACK, "subtype": subtype,
                        "action": action, "event_id": eid,
                    })
                    # Supabase保存 (best-effort, Viewer用に全イベント保存)
                    if action == "inserted":
                        sb_result = save_event_to_supabase(record, dry_run=dry_run)
                        results[-1]["supabase"] = sb_result.get("action", "error")
        except Exception as e:
            logger.warning(f"[EVENT] buyback failed doc_id={doc.doc_id[:16]} ticker={doc.ticker}: {e}")
            results.append({"event_type": EventType.BUYBACK, "action": "error", "error": str(e)})

    # ---- forecast_revision ----
    if EventType.FORECAST_REVISION in allowed:
        try:
            cls_result = classify_forecast(title, text[:2000])
            if cls_result.is_target:
                is_diff = cls_result.subtype_hint == "difference"
                forecast_ev = extract_forecast_revision(text, title, is_difference=is_diff)
                record = _forecast_to_event_record(doc, forecast_ev)
                if dry_run:
                    results.append({
                        "event_type": EventType.FORECAST_REVISION, "subtype": forecast_ev.subtype,
                        "action": "dry_run", "event_id": record.event_id,
                        "summary": record.summary_text,
                    })
                else:
                    action, eid = upsert_event(conn, record)
                    results.append({
                        "event_type": EventType.FORECAST_REVISION, "subtype": forecast_ev.subtype,
                        "action": action, "event_id": eid,
                    })
                    # Supabase保存 (best-effort)
                    if action == "inserted":
                        sb_result = save_event_to_supabase(record, dry_run=dry_run)
                        results[-1]["supabase"] = sb_result.get("action", "error")
        except Exception as e:
            logger.warning(f"[EVENT] forecast failed doc_id={doc.doc_id[:16]} ticker={doc.ticker}: {e}")
            results.append({"event_type": EventType.FORECAST_REVISION, "action": "error", "error": str(e)})

    # ---- dividend_revision ----
    if EventType.DIVIDEND_REVISION in allowed:
        try:
            cls_result = classify_dividend(title, text[:2000])
            if cls_result.is_target:
                dividend_ev = extract_dividend_revision(text, title)
                record = _dividend_to_event_record(doc, dividend_ev, db_path=db_path)
                if dry_run:
                    results.append({
                        "event_type": EventType.DIVIDEND_REVISION, "subtype": dividend_ev.subtype,
                        "action": "dry_run", "event_id": record.event_id,
                        "summary": record.summary_text,
                    })
                else:
                    action, eid = upsert_event(conn, record)
                    results.append({
                        "event_type": EventType.DIVIDEND_REVISION, "subtype": dividend_ev.subtype,
                        "action": action, "event_id": eid,
                    })
                    # Supabase保存 (best-effort)
                    if action == "inserted":
                        sb_result = save_event_to_supabase(record, dry_run=dry_run)
                        results[-1]["supabase"] = sb_result.get("action", "error")
        except Exception as e:
            logger.warning(f"[EVENT] dividend failed doc_id={doc.doc_id[:16]} ticker={doc.ticker}: {e}")
            results.append({"event_type": EventType.DIVIDEND_REVISION, "action": "error", "error": str(e)})

    return results


# ============================================================
# メイン: 複数文書を処理
# ============================================================
def process_documents(
    docs: list[DocumentMeta],
    db_path: str,
    dry_run: bool = False,
    event_types: set[str] | None = None,
    webhook_url: str = "",
) -> PipelineResult:
    """文書リストを処理し、イベントを検知・保存・通知する。

    Parameters
    ----------
    docs : list[DocumentMeta]
    db_path : str
        SQLite DB パス (decision_db.db と同じか別ファイル)
    dry_run : bool
    event_types : set[str] | None
        処理するイベント種別の制限。None=全種別
    webhook_url : str
        Discord Webhook URL。空なら通知しない

    Returns
    -------
    PipelineResult
    """
    result = PipelineResult()

    conn: sqlite3.Connection | None = None
    try:
        if not dry_run:
            conn = sqlite3.connect(db_path)
            ensure_events_table(conn)
        else:
            # dry-run でも分類結果を見るためにメモリDBを使う
            conn = sqlite3.connect(":memory:")
            ensure_events_table(conn)

        for doc in docs:
            result.processed += 1
            try:
                doc_results = _process_single_document(doc, conn, event_types, dry_run, db_path=db_path)
                for dr in doc_results:
                    if dr.get("action") == "error":
                        result.errors += 1
                    elif dr.get("action") == "inserted":
                        result.detected += 1
                        result.saved += 1
                    elif dr.get("action") == "dry_run":
                        result.detected += 1
                    elif dr.get("action") == "no_change":
                        result.skipped += 1
                    # Supabase カウンタ集計
                    sb_action = dr.get("supabase", "")
                    if sb_action == "inserted":
                        result.supabase_saved += 1
                    elif sb_action == "dedup_skipped":
                        result.supabase_dedup_skipped += 1
                    elif sb_action == "error":
                        result.supabase_errors += 1
                    result.details.append(dr)
            except Exception as e:
                result.errors += 1
                logger.error(f"[EVENT] processing failed doc_id={doc.doc_id[:16]}: {e}")
                result.details.append({
                    "doc_id": doc.doc_id, "ticker": doc.ticker, "action": "error", "error": str(e),
                })

        # 通知: フィルタ + ソート適用
        if webhook_url and not dry_run and conn:
            try:
                unnotified = get_unnotified_events(conn)
                notifiable, filtered_events = filter_and_sort_events(unnotified)
                # 非通知対象を filtered ステータスに更新
                for ev in filtered_events:
                    mark_filtered(conn, ev.event_id)
                    result.filtered += 1
                # 通知対象をソート順で送信
                for ev in notifiable:
                    if send_event_discord(webhook_url, ev, dry_run=False):
                        mark_notified(conn, ev.event_id)
                        result.notified += 1
            except Exception as e:
                logger.error(f"[EVENT] notification failed: {e}")
        elif dry_run and conn:
            # dry-run: フィルタ + ソートを適用してプレビュー
            try:
                unnotified = get_unnotified_events(conn)
                notifiable, filtered_events = filter_and_sort_events(unnotified)
                result.filtered = len(filtered_events)
                for ev in notifiable:
                    send_event_discord("", ev, dry_run=True)
                    result.notified += 1
            except Exception as e:
                logger.warning(f"[EVENT] dry-run notification preview failed: {e}")

    finally:
        if conn:
            conn.close()

    logger.info(
        f"[EVENT] pipeline done: "
        f"processed={result.processed} detected={result.detected} "
        f"saved={result.saved} filtered={result.filtered} "
        f"notified={result.notified} errors={result.errors} "
        f"supabase_saved={result.supabase_saved} "
        f"supabase_dedup={result.supabase_dedup_skipped} "
        f"supabase_errors={result.supabase_errors}"
    )
    return result
