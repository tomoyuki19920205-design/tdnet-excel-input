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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .common_models import (
    DocumentMeta,
    EventRecord,
    EventType,
    PipelineResult,
)
from .common_normalizers import compute_fingerprint, compute_text_hash
from .common_storage import ensure_events_table, upsert_event, get_unnotified_events, mark_notified
from .common_notify import send_event_discord
from .notify_rules import should_notify_event

# classifiers
from .buyback_classifier import classify_buyback, classify_buyback_subtype
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
def _get_text(doc: "DocumentMeta") -> str:
    """文書のテキストを取得（互換性維持用）。_get_text_and_pdf() のラッパー。"""
    text, _ = _get_text_and_pdf(doc)
    return text


def _get_text_and_pdf(doc: "DocumentMeta") -> tuple[str, str]:
    """文書のテキストとローカルPDFパスを取得。

    副作用: doc_url から PDF をダウンロードした場合は doc.pdf_path を自動補完する。
    戻り値: (text, local_pdf_path)
    """
    # 既存 text_body
    if doc.text_body:
        return doc.text_body, doc.pdf_path or ""

    # ローカルPDFから抽出
    if doc.pdf_path and os.path.isfile(doc.pdf_path):
        extracted = _extract_text_from_pdf_path(doc.pdf_path)
        if extracted:
            return extracted, doc.pdf_path

    # URL から PDF をダウンロードして保存・抽出
    if doc.doc_url:
        text, local_path = _download_and_save_pdf(doc.doc_url)
        if local_path:
            doc.pdf_path = local_path  # 自動補完
        if text:
            return text, local_path

    return "", ""


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
    """URLからPDFをダウンロードしてテキスト抽出 (旧形式、互换性維持用)"""
    text, _ = _download_and_save_pdf(url)
    return text


def _download_and_save_pdf(url: str, save_dir: str = "") -> tuple[str, str]:
    """URLからPDFをダウンロードしてテキスト抽出し、ディスクに保存する。

    戻り値: (extracted_text, local_pdf_path)
    失敗時: ("", "")
    """
    if not url:
        return "", ""
    try:
        import io
        import pdfplumber
        import requests

        if not any(h in url for h in ["tdnet.info", "disclosure.edinet"]):
            return "", ""

        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TDNETEventBot/1.0)"
        })
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and resp.content[:5] != b"%PDF-":
            return "", ""

        pdf_bytes = resp.content

        # --- data/docs/ にPDFを保存 ---
        local_path = ""
        try:
            if not save_dir:
                save_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                    "data", "docs"
                )
            os.makedirs(save_dir, exist_ok=True)
            # URL末尾のファイル名 or doc_id を使う
            fname = url.rstrip("/").split("/")[-1]
            if not fname.endswith(".pdf"):
                fname = fname + ".pdf"
            local_path = os.path.join(save_dir, fname)
            if not os.path.exists(local_path):
                with open(local_path, "wb") as f:
                    f.write(pdf_bytes)
                logger.info(f"[PDF_SAVED] {local_path}")
            else:
                logger.debug(f"[PDF_CACHED] {local_path}")
        except Exception as save_err:
            logger.warning(f"[PDF_SAVE_FAIL] {url[:60]}: {save_err}")
            local_path = ""

        # --- テキスト抽出 ---
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            texts = [page.extract_text() or "" for page in pdf.pages[:10]]
            text = "\n".join(texts)
            if text.strip():
                logger.debug(f"[EVENT] PDF downloaded and extracted: {url[:60]}... ({len(text)} chars)")
            return text, local_path

    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"[EVENT] PDF download failed (non-fatal): {url[:60]}... {e}")
    return "", ""


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
        doc_url=doc.doc_url or "",
    )


# ============================================================
# forecast_revision → EventRecord 変換
# ============================================================
def _has_forecast_change(forecast_event) -> bool:
    """抽出データに実際の変化があるか判定（ゆるい検知）。

    prev と rev が両方あって異なる、または片方だけある場合は変化とみなす。
    """
    has_change = False
    for prev_attr, rev_attr in [
        ("previous_net_income", "revised_net_income"),
        ("previous_op", "revised_op"),
        ("previous_ordinary", "revised_ordinary"),
        ("previous_sales", "revised_sales"),
        ("previous_eps", "revised_eps"),
    ]:
        prev = getattr(forecast_event, prev_attr, None)
        rev = getattr(forecast_event, rev_attr, None)
        # 両方あって異なる → 変化
        if prev is not None and rev is not None:
            if prev != rev:
                has_change = True
        # 片方だけある → 変化（OCRで片側だけ取れたケースを救う）
        elif prev is not None or rev is not None:
            has_change = True

    logger.info(
        f"[forecast] has_change={has_change} "
        f"ni=({getattr(forecast_event, 'previous_net_income', None)},{getattr(forecast_event, 'revised_net_income', None)}) "
        f"eps=({forecast_event.previous_eps},{forecast_event.revised_eps}) "
        f"subtype={forecast_event.subtype}"
    )
    return has_change


def _forecast_to_event_record(
    doc: DocumentMeta,
    forecast_event,
) -> EventRecord:
    payload = forecast_event.to_dict()

    # 配当年間合計が取れている場合は payload に付加（通知文生成で利用）
    for _div_key in (
        "dividend_annual_total_previous",
        "dividend_annual_total_revised",
        "dividend_delta",
        "dividend_change_pct",
    ):
        _div_val = getattr(forecast_event, _div_key, None)
        if _div_val is not None:
            payload[_div_key] = _div_val

    if payload.get("dividend_annual_total_revised") is not None:
        logger.info(
            f"[forecast_dividend_payload] "
            f"prev={payload.get('dividend_annual_total_previous')} "
            f"rev={payload.get('dividend_annual_total_revised')}"
        )

    def _fp_val(v) -> str:
        return str(v) if v is not None else ""

    fp_parts = [
        "forecast_revision",
        doc.ticker,
        forecast_event.period_label,
        _fp_val(forecast_event.revised_sales),
        _fp_val(forecast_event.revised_op),
        _fp_val(forecast_event.revised_net_income),
        _fp_val(forecast_event.revised_eps),
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
        doc_url=doc.doc_url or "",
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
        doc_url=doc.doc_url or "",
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
    text, _pdf_path = _get_text_and_pdf(doc)  # PDFをダウンロード保存しdoc.pdf_pathを補完
    title = doc.title

    logger.info(
        f"[FORECAST_CALL] doc_id={doc.doc_id[:16] if doc.doc_id else '?'} "
        f"ticker={doc.ticker} "
        f"pdf_path={doc.pdf_path!r} "
        f"doc_url={doc.doc_url[:60] if doc.doc_url else ''!r}"
    )

    allowed = event_types or {EventType.BUYBACK, EventType.FORECAST_REVISION, EventType.DIVIDEND_REVISION}

    # ---- buyback ----
    if EventType.BUYBACK in allowed:
        try:
            # [A] 入口ガード: タイトルから buyback_event_subtype を判定
            #     new_program / tostnet のみ処理。ignore は完全スキップ。
            buyback_event_subtype = classify_buyback_subtype(title)
            logger.info(
                f"[EVENT] buyback_subtype={buyback_event_subtype!r} "
                f"ticker={doc.ticker} title={title[:60]!r}"
            )
            if buyback_event_subtype == "ignore":
                logger.debug(
                    f"[EVENT] buyback subtype-guard skip (ignore): "
                    f"ticker={doc.ticker} title={title[:60]!r}"
                )
            else:
                # [B] 既存の classify_buyback で event_type を推定
                cls_result = classify_buyback(title, text[:2000])
                # [C] 中間ガード: confidence < 0.50 は除外
                _cls_conf = getattr(cls_result, "confidence", 0.0) or 0.0
                if cls_result.is_buyback_related and cls_result.event_type_candidate \
                        and _cls_conf >= 0.50:
                    event_type_raw = cls_result.event_type_candidate
                    # サブタイプは buyback_event_subtype (new_program/tostnet) を優先
                    subtype = buyback_event_subtype
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
                            "buyback_event_subtype": buyback_event_subtype,
                        })
                    else:
                        action, eid = upsert_event(conn, record)
                        results.append({
                            "event_type": EventType.BUYBACK, "subtype": subtype,
                            "action": action, "event_id": eid,
                            "buyback_event_subtype": buyback_event_subtype,
                            "_event_record": record if action in ("inserted", "updated") else None,
                        })
                else:
                    logger.debug(
                        f"[EVENT] buyback conf-guard skip: ticker={doc.ticker} "
                        f"conf={_cls_conf:.2f} related={cls_result.is_buyback_related}"
                    )
        except Exception as e:
            logger.warning(f"[EVENT] buyback failed doc_id={doc.doc_id[:16]} ticker={doc.ticker}: {e}")
            results.append({"event_type": EventType.BUYBACK, "action": "error", "error": str(e)})


    # ---- forecast_revision ----
    if EventType.FORECAST_REVISION in allowed:
        try:
            cls_result = classify_forecast(title, text[:2000])
            logger.info(
                f"[EVENT] forecast_classify ticker={doc.ticker} "
                f"is_target={cls_result.is_target} "
                f"subtype_hint={getattr(cls_result, 'subtype_hint', '?')!r} "
                f"title={title[:50]!r}"
            )
            if cls_result.is_target:
                is_diff = cls_result.subtype_hint == "difference"
                forecast_ev = extract_forecast_revision(
                    text, title, is_difference=is_diff,
                    pdf_path=doc.pdf_path, doc_url=doc.doc_url, doc_id=doc.doc_id,
                )
                # 変化判定: 実際の数値変化がなければスキップ
                if not _has_forecast_change(forecast_ev):
                    logger.info(
                        f"[EVENT] forecast skipped (no_change_detected) "
                        f"ticker={doc.ticker} subtype={forecast_ev.subtype} "
                        f"prev_sales={getattr(forecast_ev,'previous_sales',None)} "
                        f"rev_sales={getattr(forecast_ev,'revised_sales',None)} "
                        f"prev_op={getattr(forecast_ev,'previous_op',None)} "
                        f"rev_op={getattr(forecast_ev,'revised_op',None)} "
                        f"prev_ni={getattr(forecast_ev,'previous_net_income',None)} "
                        f"rev_ni={getattr(forecast_ev,'revised_net_income',None)} "
                        f"prev_eps={getattr(forecast_ev,'previous_eps',None)} "
                        f"rev_eps={getattr(forecast_ev,'revised_eps',None)}"
                    )
                    results.append({
                        "event_type": EventType.FORECAST_REVISION, "subtype": forecast_ev.subtype,
                        "action": "no_change_detected", "event_id": "",
                    })
                else:
                    # ---- 配当年間合計を同一PDFから抽出して付加 ----
                    _fcast_pdf = doc.pdf_path or _pdf_path or ""
                    if _fcast_pdf:
                        try:
                            from .dividend_extractor import _extract_dividend_annual_total_via_fitz
                            _div_result = _extract_dividend_annual_total_via_fitz(_fcast_pdf)
                            if _div_result and _div_result.get("annual_total_revised") is not None:
                                _d_prev = _div_result.get("annual_total_previous")
                                _d_rev  = _div_result["annual_total_revised"]
                                forecast_ev.dividend_annual_total_previous = _d_prev
                                forecast_ev.dividend_annual_total_revised  = _d_rev
                                if _d_prev is not None:
                                    forecast_ev.dividend_delta = round(_d_rev - _d_prev, 2)
                                    forecast_ev.dividend_change_pct = (
                                        round((_d_rev - _d_prev) / abs(_d_prev) * 100, 1)
                                        if _d_prev != 0 else None
                                    )
                        except Exception as _div_e:
                            logger.debug(f"[forecast_dividend_fitz] skip: {_div_e}")
                    record = _forecast_to_event_record(doc, forecast_ev)
                    if dry_run:
                        results.append({
                            "event_type": EventType.FORECAST_REVISION, "subtype": forecast_ev.subtype,
                            "action": "dry_run", "event_id": record.event_id,
                            "summary": record.summary_text,
                            "_event_record": record,
                        })
                    else:
                        action, eid = upsert_event(conn, record)
                        results.append({
                            "event_type": EventType.FORECAST_REVISION, "subtype": forecast_ev.subtype,
                            "action": action, "event_id": eid,
                            "_event_record": record if action in ("inserted", "updated") else None,
                        })
        except Exception as e:
            logger.warning(f"[EVENT] forecast failed doc_id={doc.doc_id[:16]} ticker={doc.ticker}: {e}")
            results.append({"event_type": EventType.FORECAST_REVISION, "action": "error", "error": str(e)})

    # ---- dividend_revision ----
    if EventType.DIVIDEND_REVISION in allowed:
        try:
            cls_result = classify_dividend(title, text[:2000])
            logger.info(
                f"[EVENT] dividend_classify ticker={doc.ticker} "
                f"is_target={cls_result.is_target} "
                f"title={title[:50]!r}"
            )
            if cls_result.is_target:
                _div_pdf = doc.pdf_path or _pdf_path or ""
                logger.info(
                    f"[DIVIDEND_CALL] ticker={doc.ticker} pdf_path={_div_pdf!r}"
                )
                dividend_ev = extract_dividend_revision(
                    text,
                    title,
                    pdf_path=_div_pdf,
                )
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
                        "_event_record": record if action in ("inserted", "updated") else None,
                    })
        except Exception as e:
            logger.warning(f"[EVENT] dividend failed doc_id={doc.doc_id[:16]} ticker={doc.ticker}: {e}")
            results.append({"event_type": EventType.DIVIDEND_REVISION, "action": "error", "error": str(e)})

    return results


# ============================================================
# should_notify_event reject理由診断ヘルパー
# ============================================================
def _classify_reject_reason(ev: "EventRecord") -> str:
    """should_notify_event が False を返した理由を簡潔な文字列で返す（ログ用）。"""
    import json as _json
    try:
        payload = _json.loads(ev.extracted_payload_json or "{}")
    except Exception:
        payload = {}

    if ev.event_type == "buyback":
        subtype = ev.subtype or ""
        if subtype not in ("new_program", "tostnet"):
            return f"buyback_subtype_not_allowed({subtype!r})"
        conf = payload.get("extraction_confidence")
        try:
            if conf is None or float(conf) < 0.50:
                return f"buyback_low_conf({conf})"
        except Exception:
            return f"buyback_conf_parse_error({conf})"
        ratio = payload.get("ratio_to_outstanding")
        try:
            ratio_f = float(ratio) if ratio is not None else None
        except Exception:
            ratio_f = None
        if ratio_f is None or ratio_f < 4.0:
            return f"buyback_ratio_too_low({ratio_f})"
        return "buyback_field_insufficient"

    elif ev.event_type == "forecast_revision":
        subtype = ev.subtype or ""
        if subtype != "upward":
            return f"forecast_not_upward(subtype={subtype!r})"
        return "forecast_other"

    elif ev.event_type == "dividend_revision":
        prev = payload.get("previous_dividend_per_share")
        rev = payload.get("revised_dividend_per_share")
        if prev in (None, "", "---"):
            return f"dividend_prev_null({prev!r})"
        if rev in (None, "", "---"):
            return f"dividend_rev_null({rev!r})"
        try:
            prev_f, rev_f = float(prev), float(rev)
        except Exception:
            return "dividend_parse_error"
        if prev_f <= 0:
            return f"dividend_prev_zero({prev_f})"
        if rev_f <= 0:
            return f"dividend_rev_zero({rev_f})"
        if rev_f <= prev_f:
            return f"dividend_no_increase(prev={prev_f},rev={rev_f})"
        pct = (rev_f - prev_f) / prev_f * 100
        if pct < 20.0:
            return f"dividend_pct_too_low({pct:.1f}%)"
        return "dividend_other"

    return f"unknown_event_type({ev.event_type!r})"


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
                    elif dr.get("action") == "updated":
                        result.detected += 1
                        result.saved += 1
                    elif dr.get("action") == "dry_run":
                        result.detected += 1
                    elif dr.get("action") in ("no_change", "no_change_detected"):
                        result.skipped += 1
                    result.details.append(dr)
            except Exception as e:
                result.errors += 1
                logger.error(f"[EVENT] processing failed doc_id={doc.doc_id[:16]}: {e}")
                result.details.append({
                    "doc_id": doc.doc_id, "ticker": doc.ticker, "action": "error", "error": str(e),
                })

        # 通知: new のみ
        if webhook_url and not dry_run and conn:
            try:
                unnotified = get_unnotified_events(conn)
                logger.info(f"[EVENT_NOTIFY] unnotified_events={len(unnotified)}")
                for ev in unnotified:
                    _notify_ok = should_notify_event(ev)
                    if not _notify_ok:
                        # reject 理由を INFO で出す
                        _rej = _classify_reject_reason(ev)
                        logger.info(
                            "[EVENT_NOTIFY] SKIP event_id=%s event_type=%s subtype=%s "
                            "ticker=%s reject=%s",
                            ev.event_id[:12], ev.event_type, ev.subtype, ev.ticker, _rej,
                        )
                        continue
                    logger.info(
                        "[EVENT_NOTIFY] SEND event_id=%s event_type=%s subtype=%s ticker=%s",
                        ev.event_id[:12], ev.event_type, ev.subtype, ev.ticker,
                    )
                    logger.info(f"notify_start_at: {datetime.now(timezone(timedelta(hours=9))).isoformat()}")
                    if send_event_discord(webhook_url, ev, dry_run=False):
                        mark_notified(conn, ev.event_id)
                        result.notified += 1
            except Exception as e:
                logger.error(f"[EVENT] notification failed: {e}")
        elif dry_run and conn:
            # dry-run: 検知されたイベントをログ出力 (should_notify_event=False は表示のみスキップ)
            try:
                unnotified = get_unnotified_events(conn)
                logger.info(f"[EVENT_NOTIFY] dry-run unnotified_events={len(unnotified)}")
                for ev in unnotified:
                    _notify_ok = should_notify_event(ev)
                    if not _notify_ok:
                        _rej = _classify_reject_reason(ev)
                        logger.info(
                            "[EVENT_NOTIFY] dry-run SKIP event_id=%s event_type=%s subtype=%s "
                            "ticker=%s reject=%s",
                            ev.event_id[:12], ev.event_type, ev.subtype, ev.ticker, _rej,
                        )
                        continue
                    send_event_discord("", ev, dry_run=True)
                    result.notified += 1
            except Exception as e:
                logger.warning(f"[EVENT] dry-run notification preview failed: {e}")

        # ── Supabase 同期 (best-effort) ──
        # dry_run時はSkip。エラーはログのみ、メイン処理は止めない。
        if not dry_run:
            try:
                from .tdnet_event_store import save_event_to_supabase as _save_to_sb
                # 今回処理で inserted / updated になった EventRecord を収集
                _records_to_sync: list[EventRecord] = []
                for dr in result.details:
                    rec = dr.get("_event_record")
                    if rec is not None:
                        _records_to_sync.append(rec)

                if _records_to_sync:
                    logger.info(
                        f"[EVENT_SUPABASE] syncing {len(_records_to_sync)} new/updated events "
                        f"to Supabase tdnet_events ..."
                    )
                    for _rec in _records_to_sync:
                        try:
                            _sb_result = _save_to_sb(_rec, dry_run=False)
                            _action = _sb_result.get("action", "error")
                            if _action == "inserted":
                                result.supabase_saved += 1
                                logger.info(
                                    f"[EVENT_SUPABASE] INSERTED ticker={_rec.ticker} "
                                    f"type={_rec.event_type} "
                                    f"-> {_sb_result.get('display_category')}"
                                )
                            elif _action == "dedup_skipped":
                                result.supabase_dedup_skipped += 1
                                logger.debug(
                                    f"[EVENT_SUPABASE] DEDUP_SKIP ticker={_rec.ticker} "
                                    f"type={_rec.event_type}"
                                )
                            else:
                                result.supabase_errors += 1
                                logger.warning(
                                    f"[EVENT_SUPABASE] ERROR ticker={_rec.ticker} "
                                    f"type={_rec.event_type} "
                                    f"error={_sb_result.get('error', 'unknown')}"
                                )
                        except Exception as _sb_ev_e:
                            result.supabase_errors += 1
                            logger.warning(
                                f"[EVENT_SUPABASE] EXCEPTION ticker={_rec.ticker} "
                                f"type={_rec.event_type}: {_sb_ev_e}"
                            )

                    logger.info(
                        f"[EVENT_SUPABASE] sync done: "
                        f"inserted={result.supabase_saved} "
                        f"dedup={result.supabase_dedup_skipped} "
                        f"errors={result.supabase_errors}"
                    )
            except Exception as _sb_e:
                logger.warning(f"[EVENT_SUPABASE] sync block failed (non-fatal): {_sb_e}")

    finally:
        if conn:
            conn.close()

    logger.info(
        f"[EVENT] pipeline done: "
        f"processed={result.processed} detected={result.detected} "
        f"saved={result.saved} notified={result.notified} errors={result.errors} "
        f"supabase_saved={result.supabase_saved} supabase_dedup={result.supabase_dedup_skipped}"
    )
    return result
