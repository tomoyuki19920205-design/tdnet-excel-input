#!/usr/bin/env python3
"""summary_earnings_pipeline.py — 決算短信V2 サンプル通知テスト パイプライン

本番DB (summary_jobs / ai_summaries) には一切書き込まない。
各銘柄を単発で処理し、毎回フレッシュに通知を生成する。

処理フロー:
  Phase 1. 開示取得 → 決算短信フィルタ
  Phase 2. 事前検証 → 通知可能候補の母集団を構築
  Phase 3. ランダムサンプリング (seed再現性)
  Phase 4. 各銘柄の通知生成・送信
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
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

logger = logging.getLogger("earnings_sample")


# ============================================================
# 失敗理由分類
# ============================================================
class SkipReason:
    NON_FINANCIAL_STATEMENT = "non_financial_statement"
    MISSING_ZIP = "missing_zip"
    XBRL_PARSE_FAILED = "xbrl_parse_failed"
    PRIOR_PERIOD_MISSING = "prior_period_missing"
    SALES_MISSING = "sales_missing"
    OP_MISSING = "op_missing"
    NO_YOY = "no_yoy"
    DOWNLOAD_FAILED = "download_failed"
    AI_ERROR = "ai_error"


# ============================================================
# 結果データ
# ============================================================
@dataclass
class EarningsSampleResult:
    """サンプルテスト実行結果"""
    total_disclosures: int = 0
    total_financial_statements: int = 0
    validated_candidates: int = 0     # 事前検証通過数
    sampled: int = 0
    succeeded: int = 0
    failed: int = 0
    notifications_sent: int = 0
    seed_used: int | None = None
    skip_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    processed: list[dict] = field(default_factory=list)


# ============================================================
# タイトルフィルタ
# ============================================================
_TANSHIN_RE = re.compile(r"決算短信")
_EXCLUDE_TITLE_RE = re.compile(r"決算説明|説明会資料|補足資料|プレゼンテーション|参考資料|業績予想")


def _is_tanshin_title(title: str) -> bool:
    """タイトルに「決算短信」を含み、除外パターンに該当しないか"""
    if not _TANSHIN_RE.search(title):
        return False
    if _EXCLUDE_TITLE_RE.search(title):
        return False
    return True


# ============================================================
# 事前検証
# ============================================================
@dataclass
class ValidatedCandidate:
    """事前検証済み候補"""
    doc: object  # DisclosureItem
    xbrl_path: str
    earnings: EarningsSummaryData
    company_name: str


def _validate_candidate(
    doc, xbrl_dir: str,
) -> tuple[ValidatedCandidate | None, str]:
    """1銘柄の事前検証。通知可能かチェックする。

    Returns: (ValidatedCandidate, "") on success, (None, skip_reason) on failure
    """
    ticker = doc.ticker

    # ---- タイトルチェック ----
    if not _is_tanshin_title(doc.title):
        return None, SkipReason.NON_FINANCIAL_STATEMENT

    # ---- XBRL ZIP取得 ----
    xbrl_path = None
    if doc.xbrl_url:
        from src.downloader import download_document
        xbrl_path = download_document(doc.xbrl_url, xbrl_dir)

    if not xbrl_path:
        doc_id = str(getattr(doc, "doc_id", "")) or str(getattr(doc, "tdnet_id", ""))
        xbrl_path = _find_cached_xbrl(xbrl_dir, ticker, doc_id=doc_id)

    if not xbrl_path:
        return None, SkipReason.MISSING_ZIP

    # ---- 数値抽出 ----
    try:
        earnings = extract_earnings_data(xbrl_path=xbrl_path, title=doc.title, ticker=ticker)
    except Exception as e:
        logger.debug(f"[VALIDATE] {ticker} XBRL parse error: {e}")
        return None, SkipReason.XBRL_PARSE_FAILED

    if earnings is None:
        return None, SkipReason.NO_YOY

    # ---- 必要最低限の数値チェック ----
    if earnings.sales_current is None:
        return None, SkipReason.SALES_MISSING
    if earnings.sales_prior is None:
        return None, SkipReason.PRIOR_PERIOD_MISSING
    if earnings.op_current is None and earnings.op_prior is None:
        return None, SkipReason.OP_MISSING

    # ---- 企業名フォールバック ----
    company_name = doc.company_name
    if not company_name:
        extracted_name, _ = extract_company_info_from_zip(xbrl_path)
        if extracted_name:
            company_name = extracted_name

    return ValidatedCandidate(
        doc=doc,
        xbrl_path=xbrl_path,
        earnings=earnings,
        company_name=company_name,
    ), ""


# ============================================================
# メイン関数
# ============================================================
def run_earnings_sample_test(
    target_date: str | None = None,
    sample_size: int = 20,
    sample_seed: int | None = None,
    send_discord: bool = False,
    webhook_url: str = "",
    model: str = "",
) -> EarningsSampleResult:
    """決算短信のランダムサンプル通知テストを実行する。

    本番DBには一切書き込まない。fingerprintも管理しない。

    Parameters
    ----------
    target_date : 対象日付 (YYYY-MM-DD 形式、Noneで当日)
    sample_size : サンプル数 (デフォルト20)
    sample_seed : ランダムシード (再現性用、Noneでランダム)
    send_discord : True=Discord実送信、False=コンソール出力のみ
    webhook_url : Discord Webhook URL
    model : AIモデル名
    """
    from src.fetcher import fetch_new_disclosures

    result = EarningsSampleResult()

    # ---- Phase 1: 開示取得 ----
    print(f"[SAMPLE] Fetching disclosures for date={target_date or 'today'}...")
    all_docs = fetch_new_disclosures(target_date=target_date)
    result.total_disclosures = len(all_docs)

    # ticker重複除外
    seen_tickers: set[str] = set()
    unique_docs = []
    for doc in all_docs:
        if doc.ticker not in seen_tickers:
            seen_tickers.add(doc.ticker)
            unique_docs.append(doc)

    print(f"[SAMPLE] Total disclosures={len(all_docs)}, unique tickers={len(unique_docs)}")

    # ---- Phase 2: 事前検証 → 通知可能候補の母集団構築 ----
    from src.events.env_loader import get_project_root
    xbrl_dir = str(get_project_root() / "data" / "xbrl_archive")

    print(f"[SAMPLE] Validating candidates...")
    candidates: list[ValidatedCandidate] = []
    for doc in unique_docs:
        candidate, skip_reason = _validate_candidate(doc, xbrl_dir)
        if candidate:
            candidates.append(candidate)
        else:
            result.skip_reasons[skip_reason] = result.skip_reasons.get(skip_reason, 0) + 1

    result.total_financial_statements = len(candidates) + result.skip_reasons.get(SkipReason.PRIOR_PERIOD_MISSING, 0) + result.skip_reasons.get(SkipReason.SALES_MISSING, 0) + result.skip_reasons.get(SkipReason.NO_YOY, 0)
    result.validated_candidates = len(candidates)

    print(f"[SAMPLE] Validated candidates={len(candidates)}")
    if result.skip_reasons:
        print(f"[SAMPLE] Skip reasons:")
        for reason, count in sorted(result.skip_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")

    if not candidates:
        print("[SAMPLE] No valid candidates found")
        return result

    # ---- Phase 3: ランダムサンプリング (seed再現性) ----
    if sample_seed is None:
        sample_seed = random.randint(0, 999999)
    result.seed_used = sample_seed

    rng = random.Random(sample_seed)
    actual_size = min(sample_size, len(candidates))
    sampled = rng.sample(candidates, actual_size)
    result.sampled = actual_size

    print(f"\n[SAMPLE] Sampled {actual_size} companies (seed={sample_seed})")
    for i, c in enumerate(sampled, 1):
        print(f"  {i:2d}. {c.doc.ticker} {c.company_name[:20]} — {c.doc.title[:40]}")

    # ---- Phase 4: 各銘柄の通知生成・送信 ----
    for i, c in enumerate(sampled, 1):
        ticker = c.doc.ticker
        print(f"\n{'='*50}")
        print(f"[{i}/{actual_size}] {ticker} {c.company_name}")
        print(f"{'='*50}")

        try:
            item_result = _process_validated_item(
                candidate=c,
                send_discord=send_discord,
                webhook_url=webhook_url,
                model=model,
            )
            result.processed.append(item_result)

            if item_result["status"] == "success":
                result.succeeded += 1
                if item_result.get("sent"):
                    result.notifications_sent += 1
            else:
                result.failed += 1
                reason = item_result.get("skip_reason", "unknown")
                result.skip_reasons[reason] = result.skip_reasons.get(reason, 0) + 1
                if item_result.get("error"):
                    result.errors.append(f"{ticker}: {item_result['error'][:100]}")

        except Exception as e:
            result.failed += 1
            result.errors.append(f"{ticker}: {str(e)[:100]}")
            logger.error(f"[SAMPLE] {ticker} unexpected error: {e}")

        # Discord ratelimit 対策
        if send_discord and i < actual_size:
            time.sleep(1.5)

    return result


def _process_validated_item(
    candidate: ValidatedCandidate,
    send_discord: bool = False,
    webhook_url: str = "",
    model: str = "",
) -> dict:
    """事前検証済みの1銘柄を処理。DBには何も書き込まない。"""
    ticker = candidate.doc.ticker
    company_name = candidate.company_name
    earnings = candidate.earnings
    xbrl_path = candidate.xbrl_path

    # ---- 数値フォーマット ----
    summary_line = earnings.format_summary_line(clip=2.0)
    segment_lines = earnings.format_segment_lines()

    # ---- テキスト抽出 + 理由抽出 ----
    company_reasons: list[str] = []
    segment_reasons: list[dict] = []

    narrative_text = extract_narrative_from_xbrl_zip(xbrl_path)
    if narrative_text:
        narrative = extract_narrative(narrative_text, title=candidate.doc.title)

        if narrative.has_reason:
            try:
                ai_result = _format_reasons_with_ai(narrative, model=model)
                company_reasons = ai_result.get("company_reasons", [])
                segment_reasons = ai_result.get("segment_reasons", [])
            except Exception as e:
                logger.warning(f"[SAMPLE] {ticker} AI formatting failed, using raw: {e}")
                if narrative.company_reason:
                    company_reasons = [s.strip() for s in narrative.company_reason.split("。") if s.strip()][:3]
                for seg_name, seg_reason in narrative.segment_reasons.items():
                    segment_reasons.append({"segment_name": seg_name, "reason": seg_reason[:80]})

    # ---- 通知メッセージ生成 ----
    message = format_earnings_message(
        ticker=ticker,
        company_name=company_name,
        summary_line=summary_line,
        segment_lines=segment_lines,
        company_reasons=company_reasons,
        segment_reasons=segment_reasons,
        title=candidate.doc.title,
    )

    # ---- 送信 or コンソール出力 ----
    sent = False
    if send_discord and webhook_url:
        sent = send_earnings_discord(webhook_url, message)
        if sent:
            print(f"[SAMPLE] ✅ Discord sent: {ticker} {company_name}")
        else:
            print(f"[SAMPLE] ❌ Discord failed: {ticker}")
    else:
        print(f"\n--- Discord Preview ({ticker} {company_name}) ---")
        print(message)
        print("--- End Preview ---\n")

    return {
        "ticker": ticker,
        "company_name": company_name,
        "status": "success",
        "sent": sent,
        "has_segments": bool(segment_lines),
        "has_reasons": bool(company_reasons),
        "summary_line": summary_line,
    }


def _find_cached_xbrl(xbrl_dir: str, ticker: str, doc_id: str = "") -> str | None:
    from .common_normalizers import extract_common_disclosure_no
    d = Path(xbrl_dir)
    if not d.is_dir():
        return None
        
    if not doc_id:
        return None

    common_id = extract_common_disclosure_no(doc_id)
    if not common_id:
        return None

    candidates = sorted(d.glob(f"{ticker}_*.zip"), reverse=True)
    matches = []
    for c in candidates:
        zip_id = extract_common_disclosure_no(c.name)
        if zip_id and zip_id == common_id:
            matches.append(c)

    if len(matches) == 1:
        return str(matches[0])
            
    return None


def _format_reasons_with_ai(
    narrative: NarrativeData,
    model: str = "",
) -> dict:
    """AI で増減理由を箇条書き整形する。"""
    from .summary_ai_client import call_reason_format_api

    result, usage = call_reason_format_api(
        reason_text=narrative.company_reason,
        segment_texts=narrative.segment_reasons if narrative.segment_reasons else None,
        model=model,
    )
    logger.info(
        f"[SAMPLE] AI format OK: tokens={usage.get('input_tokens', 0)}+{usage.get('output_tokens', 0)}"
    )
    return result
