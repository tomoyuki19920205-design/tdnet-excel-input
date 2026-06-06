#!/usr/bin/env python3
"""earnings_production_pipeline.py — 決算短信V2 本番パイプライン

sample_test とは完全分離。本番用の全件保存・条件付き通知を行う。

処理フロー:
  Phase 0-1. 開示取得 → 決算短信フィルタ → 事前検証
  Phase 0-2. 全件: XBRL→数値抽出→AI整形→DB保存
  Phase 0-3. 通知条件判定 → 条件一致のみ Discord 送信

保存ルール:
  - 全件保存（fingerprintで重複防止）
  - 通知のみ条件付き (sales_yoy >= 25% or op_yoy >= 25%)
  - 判定は内部実値（表示クリップ後ではない）
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .summary_financials import (
    extract_earnings_data,
    extract_narrative_from_xbrl_zip,
    extract_company_info_from_zip,
    EarningsSummaryData,
)
from .summary_narrative_extractor import extract_narrative, NarrativeData
from .summary_notify import format_earnings_message, send_earnings_discord
from .earnings_summary_storage import (
    ensure_earnings_summary_table,
    save_earnings_summary,
    should_notify_earnings,
    mark_earnings_notified,
)
from .earnings_guidance_extractor import (
    extract_guidance_from_zip,
    format_guidance_section,
    GuidanceData,
)
from .common_models import EventRecord
from .tdnet_event_store import save_event_to_supabase

logger = logging.getLogger("earnings_production")


# ============================================================
# 結果データ
# ============================================================
@dataclass
class EarningsProductionResult:
    """本番パイプライン実行結果"""
    total_disclosures: int = 0
    tanshin_count: int = 0          # 決算短信数
    validated_count: int = 0        # 事前検証通過数
    generated_count: int = 0        # 要約生成数
    saved_count: int = 0            # DB保存数
    already_exists_count: int = 0   # fingerprint重複スキップ
    notified_count: int = 0         # Discord通知数
    filtered_count: int = 0         # 通知条件非該当
    no_yoy_count: int = 0           # YOYなしスキップ
    errors: list[str] = field(default_factory=list)
    saved_tickers: list[str] = field(default_factory=list)  # DB新規保存できたticker一覧


# ============================================================
# タイトルフィルタ（sample_test と共通ロジック）
# ============================================================
_TANSHIN_RE = re.compile(r"決算短信")
_EXCLUDE_TITLE_RE = re.compile(r"決算説明|説明会資料|補足資料|プレゼンテーション|参考資料|業績予想")


def _is_tanshin_title(title: str) -> bool:
    if not _TANSHIN_RE.search(title):
        return False
    if _EXCLUDE_TITLE_RE.search(title):
        return False
    return True


# ============================================================
# fiscal_year / quarter 解析
# ============================================================

# 除外パターン: 四半期レポート / 中間期は False を優先
_QUARTER_EXCLUDE_RE = re.compile(
    r"第[1-3]四半期|中間"
)

# タイトルからFY判定: 「○年○月期 決算短信」（スペース揺れ対応）
_FY_TANSHIN_TITLE_RE = re.compile(
    r"\d{4}年\s*\d{1,2}月期\s*決算短信"
)


def _normalize_title(title: str) -> str:
    """タイトルをNFKC正規化（全角→半角数字/英字/スペース統一）"""
    return unicodedata.normalize("NFKC", title)


def _parse_fiscal_info(title: str, earnings: EarningsSummaryData) -> tuple[str, str]:
    """タイトルとEarnings情報からfiscal_year, quarterを推定。

    Returns: (fiscal_year, quarter)
        fiscal_year: "2026-03-31" 形式
        quarter: "1Q"/"2Q"/"3Q"/"4Q"/"FY"
    """
    # EarningsSummaryData に period/quarter がある場合はそれを使う
    if earnings.period and earnings.quarter:
        return earnings.period, earnings.quarter

    # タイトルから推定
    quarter = ""
    fiscal_year = earnings.period or ""
    normalized = _normalize_title(title)

    # "第3四半期" → "3Q"
    m = re.search(r"第(\d)四半期", normalized)
    if m:
        quarter = f"{m.group(1)}Q"
    elif "通期" in title or "本決算" in title:
        quarter = "FY"
    elif earnings.quarter:
        quarter = earnings.quarter
    else:
        # FY fallback: 「○年○月期 決算短信」で四半期キーワードなし → FY
        if (_FY_TANSHIN_TITLE_RE.search(normalized)
                and not _QUARTER_EXCLUDE_RE.search(normalized)):
            quarter = "FY"

    return fiscal_year, quarter


# ============================================================
# 4Q判定
# ============================================================
def _is_fy_or_4q(earnings: EarningsSummaryData, title: str) -> tuple[bool, str]:
    """通期決算（FY/4Q）かを判定する。

    主判定: quarter / metadata
    補助判定: title（主判定で取れない場合のみ）

    Returns:
        (is_fy_or_4q, reason)
    """
    # 主判定: EarningsSummaryData.quarter
    if earnings.quarter in ("FY", "4Q"):
        return True, f"quarter={earnings.quarter}"
    # 明示的に1Q-3Qなら False
    if earnings.quarter in ("1Q", "2Q", "3Q"):
        return False, f"quarter={earnings.quarter}"
    # --- フォールバック: quarter が空の場合 ---
    normalized = _normalize_title(title)
    # 除外チェック: 四半期・中間キーワード
    if _QUARTER_EXCLUDE_RE.search(normalized):
        return False, "title_contains_quarter_keyword"
    # 「通期」「本決算」
    if re.search(r"通期|本決算", title):
        return True, "title_contains_tsuuki"
    # 「○年○月期 決算短信」（四半期キーワードなし）
    if _FY_TANSHIN_TITLE_RE.search(normalized):
        return True, "title_fy_tanshin_pattern"
    return False, "no_fy_indicator"



# ============================================================
# fingerprint 生成
# ============================================================
def _compute_earnings_fingerprint(ticker: str, title: str, doc_id: str = "") -> str:
    """決算短信要約用の fingerprint"""
    import hashlib
    raw = f"earnings_v2:{ticker}:{title}:{doc_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ============================================================
# メイン関数
# ============================================================
def run_earnings_production(
    docs: list,
    conn: sqlite3.Connection,
    webhook_url: str = "",
    model: str = "",
    dry_run: bool = False,
) -> EarningsProductionResult:
    """決算短信V2 本番パイプラインを実行する。

    全件保存、通知のみ条件付き。

    Parameters
    ----------
    docs : DisclosureItem のリスト
    conn : SQLite コネクション（earnings_summaries テーブルを使用）
    webhook_url : Discord Webhook URL
    model : AIモデル名
    dry_run : dry-runモード（DB書き込みなし、API呼び出しなし）
    """
    from src.downloader import download_document
    from src.events.env_loader import get_project_root

    result = EarningsProductionResult()
    result.total_disclosures = len(docs)

    ensure_earnings_summary_table(conn)

    xbrl_dir = str(get_project_root() / "data" / "xbrl_archive")

    # ---- Phase 0-1: 決算短信フィルタ ----
    tanshin_docs = []
    seen_tickers: set[str] = set()
    for doc in docs:
        if not _is_tanshin_title(doc.title):
            continue
        if doc.ticker in seen_tickers:
            continue
        seen_tickers.add(doc.ticker)
        tanshin_docs.append(doc)

    result.tanshin_count = len(tanshin_docs)
    logger.info(
        f"[EARNINGS] total_disclosures={len(docs)} tanshin_candidates={len(tanshin_docs)}"
    )

    if not tanshin_docs:
        return result

    parse_success = 0
    parse_failed = 0

    # ---- Phase 0-2: 全件処理 → DB保存 ----
    for doc in tanshin_docs:
        ticker = doc.ticker

        try:
            # ---- XBRL取得 ----
            xbrl_path = None
            if getattr(doc, 'xbrl_url', None):
                xbrl_path = download_document(doc.xbrl_url, xbrl_dir)
                if xbrl_path:
                    logger.info(f"[EARNINGS] {ticker} ZIP downloaded: {Path(xbrl_path).name}")
                else:
                    logger.info(f"[EARNINGS] {ticker} ZIP download failed: {doc.xbrl_url}")
            else:
                logger.info(f"[EARNINGS] {ticker} no xbrl_url, trying cache")

            if not xbrl_path:
                xbrl_path = _find_cached_xbrl(xbrl_dir, ticker)
                if xbrl_path:
                    logger.info(f"[EARNINGS] {ticker} found cached ZIP: {Path(xbrl_path).name}")
            if not xbrl_path:
                result.errors.append(f"{ticker}: XBRL ZIP not found")
                parse_failed += 1
                continue

            # ---- 数値抽出 ----
            try:
                earnings = extract_earnings_data(
                    xbrl_path=xbrl_path, title=doc.title, ticker=ticker,
                )
            except Exception as e:
                logger.error(f"[EARNINGS] {ticker} parse error: {e}")
                logger.error(f"[EARNINGS] {ticker} xbrl_path={xbrl_path}")
                # ZIP内ファイル一覧を出力
                try:
                    import zipfile
                    with zipfile.ZipFile(xbrl_path) as zf:
                        logger.error(f"[EARNINGS] {ticker} ZIP contents: {zf.namelist()[:10]}")
                except Exception:
                    logger.error(f"[EARNINGS] {ticker} not a valid ZIP file")
                result.errors.append(f"{ticker}: parse error: {str(e)[:80]}")
                parse_failed += 1
                continue

            if earnings is None or not earnings.has_yoy:
                result.no_yoy_count += 1
                continue

            parse_success += 1
            result.validated_count += 1

            # ---- 企業名フォールバック ----
            company_name = doc.company_name
            if not company_name:
                extracted_name, _ = extract_company_info_from_zip(xbrl_path)
                if extracted_name:
                    company_name = extracted_name

            # ---- 数値フォーマット ----
            summary_line = earnings.format_summary_line(clip=2.0)
            segment_lines = earnings.format_segment_lines()

            # ---- テキスト抽出・理由抽出 ----
            company_reasons: list[str] = []
            segment_reasons: list[dict] = []

            if not dry_run:
                narrative_text = extract_narrative_from_xbrl_zip(xbrl_path)
                if narrative_text:
                    narrative = extract_narrative(narrative_text, title=doc.title)
                    if narrative.has_reason:
                        try:
                            ai_result = _format_reasons_with_ai(narrative, model=model)
                            company_reasons = ai_result.get("company_reasons", [])
                            segment_reasons = ai_result.get("segment_reasons", [])
                        except Exception as e:
                            logger.warning(f"[EARNINGS] {ticker} AI formatting failed: {e}")
                            if narrative.company_reason:
                                company_reasons = [
                                    s.strip() for s in narrative.company_reason.split("。")
                                    if s.strip()
                                ][:3]

            # ---- 通知メッセージ生成 ----
            full_message = format_earnings_message(
                ticker=ticker,
                company_name=company_name,
                summary_line=summary_line,
                segment_lines=segment_lines,
                company_reasons=company_reasons,
                segment_reasons=segment_reasons,
                title=doc.title,
            )

            # ---- 4Q専用: 来期ガイダンス + 見通し ----
            guidance: GuidanceData | None = None
            is_4q, fy_reason = _is_fy_or_4q(earnings, doc.title)
            # quarter を is_4q と同タイミングで確定（ログ・DB保存で一致させる）
            fiscal_year, quarter = _parse_fiscal_info(doc.title, earnings)
            if is_4q and quarter not in ("FY", "4Q", "1Q", "2Q", "3Q"):
                quarter = "FY"
            logger.info(
                f"[EARNINGS] {ticker} is_fy_or_4q={is_4q} "
                f"reason={fy_reason} "
                f"quarter={quarter!r} title={doc.title[:40]!r}"
            )

            if is_4q:
                try:
                    guidance = extract_guidance_from_zip(
                        xbrl_path=xbrl_path,
                        actual_sales=earnings.sales_current,
                        actual_op=earnings.op_current,
                    )

                    guidance_extracted = guidance is not None and guidance.has_guidance
                    logger.info(
                        f"[EARNINGS] {ticker} guidance_extracted={guidance_extracted}"
                    )

                    if guidance:
                        logger.info(
                            f"[EARNINGS] {ticker} guidance_fields: "
                            f"sales={guidance.sales_forecast} "
                            f"op={guidance.op_forecast} "
                            f"eps={guidance.eps_forecast} "
                            f"sales_yoy={guidance.sales_yoy} "
                            f"op_yoy={guidance.op_yoy} "
                            f"eps_yoy={guidance.eps_yoy}"
                        )

                    # ---- 通知にガイダンスセクションを追加 ----
                    if guidance:
                        guidance_section = format_guidance_section(guidance)
                        if guidance_section:
                            full_message += "\n\n" + guidance_section
                            logger.info(
                                f"[EARNINGS] {ticker} notification_sections_added=True "
                                f"section_len={len(guidance_section)}"
                            )
                        else:
                            logger.info(
                                f"[EARNINGS] {ticker} notification_sections_added=False "
                                f"(no guidance to display)"
                            )

                except Exception as e:
                    logger.warning(f"[EARNINGS] {ticker} guidance extraction failed: {e}")
                    # ガイダンス失敗でも本体の通知は続行
            else:
                # is_4q=False: ガイダンス対象外
                logger.info(
                    f"[EARNINGS] {ticker} guidance_extracted=N/A "
                    f"(not FY/4Q, reason={fy_reason})"
                )

            # ---- summary_short 生成 ----
            summary_short = summary_line

            # ---- セグメントJSON ----
            seg_json = ""
            if earnings.segments:
                seg_json = json.dumps(
                    [{"name": s.name, "sales": s.sales_current, "profit": s.profit_current}
                     for s in earnings.segments],
                    ensure_ascii=False,
                )

            # ---- fingerprint ----
            fp = _compute_earnings_fingerprint(
                ticker, doc.title,
                getattr(doc, "disclosure_id", "") or getattr(doc, "doc_id", ""),
            )

            # ---- DB保存（全件） ----
            result.generated_count += 1

            if not dry_run:
                save_data = {
                    "ticker": ticker,
                    "company_name": company_name,
                    "fiscal_year": fiscal_year,
                    "quarter": quarter,
                    "title": doc.title,
                    "disclosure_date": getattr(doc, "published_at", "")[:10] if getattr(doc, "published_at", "") else "",
                    "sales_value": earnings.sales_current,
                    "sales_yoy": earnings.sales_yoy,
                    "op_value": earnings.op_current,
                    "op_yoy": earnings.op_yoy,
                    "segment_summary_json": seg_json,
                    "overall_reason_summary": "\n".join(company_reasons),
                    "segment_reason_summary": json.dumps(segment_reasons, ensure_ascii=False) if segment_reasons else "",
                    "summary_short": summary_short,
                    "summary_full": full_message,
                    "fingerprint": fp,
                    "source_url": getattr(doc, "doc_url", "") or "",
                    "archive_path": xbrl_path,
                }
                # 4Qガイダンスカラム
                if guidance and guidance.has_guidance:
                    save_data["guidance_sales"] = guidance.sales_forecast
                    save_data["guidance_op"] = guidance.op_forecast
                    save_data["guidance_eps"] = guidance.eps_forecast
                    save_data["guidance_sales_yoy"] = guidance.sales_yoy
                    save_data["guidance_op_yoy"] = guidance.op_yoy
                    save_data["guidance_eps_yoy"] = guidance.eps_yoy


                action = save_earnings_summary(conn, save_data)
                if action == "inserted":
                    result.saved_count += 1
                    result.saved_tickers.append(ticker)
                    # ---- tdnet_events へ earnings イベントを best-effort 保存 ----
                    try:
                        _save_earnings_to_tdnet_events(
                            doc=doc,
                            earnings=earnings,
                            company_name=company_name,
                            full_message=full_message,
                            guidance=guidance,
                            fiscal_year=fiscal_year,
                            quarter=quarter,
                            xbrl_path=xbrl_path,
                            dry_run=dry_run,
                        )
                    except Exception as _e:
                        logger.warning(f"[EARNINGS_STORE] {ticker} tdnet_events save failed (non-fatal): {_e}")
                else:
                    result.already_exists_count += 1
                    continue  # 既存の場合は通知もスキップ
            else:
                logger.info(f"[DRY-RUN] would save: {ticker} {company_name}")
                result.saved_count += 1

            # ---- Phase 0-3: 通知条件判定 ----
            if should_notify_earnings(earnings.sales_yoy, earnings.op_yoy):
                if not dry_run and webhook_url:
                    sent = send_earnings_discord(webhook_url, full_message)
                    if sent:
                        mark_earnings_notified(conn, fp)
                        result.notified_count += 1
                        logger.info(f"[EARNINGS] ✅ 通知送信: {ticker} {company_name}")
                    else:
                        result.errors.append(f"{ticker}: Discord送信失敗")
                    time.sleep(1.5)  # ratelimit対策
                else:
                    if dry_run:
                        logger.info(
                            f"[DRY-RUN] would notify: {ticker} "
                            f"sales_yoy={earnings.sales_yoy} op_yoy={earnings.op_yoy}"
                        )
                    result.notified_count += 1
            else:
                result.filtered_count += 1

        except Exception as e:
            result.errors.append(f"{ticker}: {str(e)[:100]}")
            logger.error(f"[EARNINGS] {ticker} error: {e}")

    logger.info(
        f"[EARNINGS] 完了: tanshin={result.tanshin_count} "
        f"parse_success={parse_success} parse_failed={parse_failed} "
        f"generated={result.generated_count} "
        f"saved={result.saved_count} notified={result.notified_count} "
        f"filtered={result.filtered_count} no_yoy={result.no_yoy_count}"
    )
    return result


# ============================================================
# ユーティリティ
# ============================================================
def _find_cached_xbrl(xbrl_dir: str, ticker: str) -> str | None:
    d = Path(xbrl_dir)
    if not d.is_dir():
        return None
    candidates = sorted(d.glob(f"{ticker}_*.zip"), reverse=True)
    if candidates:
        return str(candidates[0])
    return None


def _format_reasons_with_ai(narrative: NarrativeData, model: str = "") -> dict:
    from .summary_ai_client import call_reason_format_api
    result, usage = call_reason_format_api(
        reason_text=narrative.company_reason,
        segment_texts=narrative.segment_reasons if narrative.segment_reasons else None,
        model=model,
    )
    logger.info(
        f"[EARNINGS] AI format OK: tokens={usage.get('input_tokens', 0)}+{usage.get('output_tokens', 0)}"
    )
    return result


# ============================================================
# tdnet_events 保存ヘルパー
# ============================================================
def _build_earnings_event_record(
    doc,
    earnings: EarningsSummaryData,
    company_name: str,
    full_message: str,
    guidance,
    fiscal_year: str,
    quarter: str,
    xbrl_path: str,
) -> EventRecord:
    """EarningsSummaryData → EventRecord（tdnet_events 保存用）"""
    # extracted payload: PL + セグメント + ガイダンス
    extracted: dict = {
        "ticker": doc.ticker,
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "sales_current": earnings.sales_current,
        "sales_yoy": earnings.sales_yoy,
        "op_current": earnings.op_current,
        "op_yoy": earnings.op_yoy,
        "has_yoy": earnings.has_yoy,
        "segments": [
            {"name": s.name, "sales": s.sales_current, "profit": s.profit_current}
            for s in (earnings.segments or [])
        ],
        "source_url": getattr(doc, "doc_url", "") or "",
        "xbrl_path": xbrl_path,
    }
    if guidance and guidance.has_guidance:
        extracted["guidance"] = {
            "sales_forecast": guidance.sales_forecast,
            "op_forecast": guidance.op_forecast,
            "eps_forecast": guidance.eps_forecast,
            "sales_yoy": guidance.sales_yoy,
            "op_yoy": guidance.op_yoy,
            "eps_yoy": guidance.eps_yoy,
        }

    raw_payload = {"title": getattr(doc, "title", "")}

    # disclosure_datetime: published_at → disclosure_datetime 優先
    disclosure_dt = (
        getattr(doc, "disclosure_datetime", "")
        or getattr(doc, "published_at", "")
        or ""
    )

    return EventRecord(
        source_doc_id=(
            getattr(doc, "disclosure_id", "")
            or getattr(doc, "doc_id", "")
            or ""
        ),
        ticker=doc.ticker,
        company_name=company_name,
        disclosure_datetime=disclosure_dt,
        title=getattr(doc, "title", ""),
        doc_url=getattr(doc, "doc_url", "") or "",
        event_type="earnings",
        subtype=quarter,                    # "FY" / "1Q" / "2Q" / "3Q"
        importance=60,
        summary_text=earnings.format_summary_line(clip=2.0),
        raw_payload_json=json.dumps(
            {"raw": raw_payload}, ensure_ascii=False
        ),
        extracted_payload_json=json.dumps(
            extracted, ensure_ascii=False, default=str
        ),
        fingerprint=_compute_earnings_fingerprint(
            doc.ticker,
            getattr(doc, "title", ""),
            getattr(doc, "disclosure_id", "") or getattr(doc, "doc_id", ""),
        ),
    )


def _save_earnings_to_tdnet_events(
    doc,
    earnings: EarningsSummaryData,
    company_name: str,
    full_message: str,
    guidance,
    fiscal_year: str,
    quarter: str,
    xbrl_path: str,
    dry_run: bool = False,
) -> dict:
    """earnings イベントを Supabase tdnet_events へ best-effort 保存。

    formatted_message には format_earnings_message() の出力をそのまま使う。
    失敗しても呼び出し元の処理は継続する。

    Returns: {"action": "inserted"|"dedup_skipped"|"error"|"dry_run", ...}
    """
    record = _build_earnings_event_record(
        doc=doc,
        earnings=earnings,
        company_name=company_name,
        full_message=full_message,
        guidance=guidance,
        fiscal_year=fiscal_year,
        quarter=quarter,
        xbrl_path=xbrl_path,
    )
    # summary_text に full_message を直接セット
    # → tdnet_event_store.build_formatted_message() の代わりに
    #   format_earnings_message() の出力を Viewer の formatted_message として使う
    record.summary_text = full_message

    result = save_event_to_supabase(record, dry_run=dry_run)
    logger.info(
        f"[EARNINGS_STORE] {doc.ticker} tdnet_events: action={result.get('action')} "
        f"dedupe_key={result.get('dedupe_key', '')[:12]}..."
    )
    return result
