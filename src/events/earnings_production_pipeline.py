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

import os
import json
import logging
import re
import sqlite3
import time
from src.cache.cache_manager import make_cache_key, load_json, save_json
import dataclasses
from .summary_financials import EarningsSummaryData
from .earnings_guidance_extractor import GuidanceData
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


def _derive_fiscal_year_end_period(title: str) -> str | None:
    """タイトル「YYYY年M月期」からその月の末日をYYYY-MM-DD形式で算出する。"""
    if not title:
        return None
    import unicodedata
    import re
    import calendar
    # 全角数字を半角にする
    norm_title = unicodedata.normalize("NFKC", title)
    m = re.search(r"(\d{4})年(\d{1,2})月期", norm_title)
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2))
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


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


def _parse_fiscal_info(title: str, earnings: EarningsSummaryData, pdf_text: str = "", disclosed_at: str = "") -> tuple[str, str]:
    """タイトルとEarnings情報などからfiscal_year, quarterを推定。

    Returns: (fiscal_year, quarter)
        fiscal_year: "2026" (YYYY) のような年文字列
        quarter: "1Q"/"2Q"/"3Q"/"4Q"/"FY"
    """
    # 1. XBRL/抽出済み
    fiscal_year_raw = earnings.period or ""

    # 2. PDF/本文
    if not fiscal_year_raw and pdf_text:
        from src.year_parser import extract_fiscal_year_from_text
        fy = extract_fiscal_year_from_text(pdf_text)
        if fy:
            fiscal_year_raw = fy

    # 3. title
    if not fiscal_year_raw and title:
        from src.year_parser import extract_fiscal_year_from_title
        fy = extract_fiscal_year_from_title(title)
        if fy:
            fiscal_year_raw = fy

    # fiscal_year を YYYY に整形
    fiscal_year = ""
    if fiscal_year_raw:
        from src.year_parser import _era_period_to_iso
        iso = _era_period_to_iso(fiscal_year_raw)
        if len(iso) >= 4 and iso[:4].isdigit():
            fiscal_year = iso[:4]

    # 4. disclosed_at からの推定
    if not fiscal_year and disclosed_at:
        m = re.match(r"^(\d{4})", disclosed_at)
        if m:
            fiscal_year = m.group(1)

    # ---- quarter 推定 ----
    quarter = ""
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
    state_db=None,
    session=None,
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

    if not webhook_url:
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")

    result = EarningsProductionResult()
    result.total_disclosures = len(docs)

    ensure_earnings_summary_table(conn)

    xbrl_dir = str(get_project_root() / "data" / "xbrl_archive")
    docs_dir = str(get_project_root() / "data" / "docs")

    # ---- Phase 0-1: 決算短信フィルタ ----
    tanshin_docs = []
    for doc in docs:
        if not _is_tanshin_title(doc.title):
            continue
        tanshin_docs.append(doc)

    result.tanshin_count = len(tanshin_docs)
    logger.info(
        f"[EARNINGS] total_disclosures={len(docs)} tanshin_candidates={len(tanshin_docs)}"
    )

    if not tanshin_docs:
        return result

    # ---- Phase 0-1.5: Feature Flag による新方式ルーティング ----
    use_subprocess = os.getenv("USE_SUBPROCESS_WORKER", "0")
    if use_subprocess == "1":
        # ── Phase 3-2j: 多重ゲート付き実保存ルート ────────────────────────────────
        # ゲート: EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE=1
        enable_real_save = os.getenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "0") == "1"
        # ゲート: allowlist が設定されており空でないこと
        allowlist_env_j = os.getenv("EARNINGS_SUBPROCESS_ALLOWLIST", "")
        allowlist_j = [t.strip() for t in allowlist_env_j.split(",") if t.strip()] if allowlist_env_j else []
        # ゲート: Discord / state_db 無効フラグ
        enable_discord = os.getenv("EARNINGS_SUBPROCESS_ENABLE_DISCORD", "0") == "1"
        enable_state_update = os.getenv("EARNINGS_SUBPROCESS_ENABLE_STATE_UPDATE", "0") == "1"

        if (enable_real_save or dry_run) and allowlist_j:
            logger.info(
                "[EARNINGS] USE_SUBPROCESS_WORKER=1 dry_run=%s ENABLE_REAL_SAVE=%s "
                "allowlist=%s discord=%s state_update=%s → subprocess route",
                dry_run, enable_real_save, allowlist_j, enable_discord, enable_state_update,
            )
            from src.events.earnings_subprocess_runner import (
                run_earnings_subprocess_dry_run,
                build_save_ready_payload,
                validate_save_ready_payload,
                build_save_call_plan,
                build_discord_call_plan,
                validate_save_call_plan,
                find_semantic_duplicate,
            )
            from src.events.common_models import EventRecord
            from src.events.tdnet_event_store import save_event_to_supabase
            import uuid as _uuid
            from datetime import datetime as _dt, timezone as _tz, timedelta as _tdelta
            _JST = _tz(_tdelta(hours=9))

            # ── allowlist と state_db でフィルタ ──────────────────────────────
            target_docs_j = []
            for d in tanshin_docs:
                _t = getattr(d, "ticker", "")
                _did = getattr(d, "disclosure_id", "") or getattr(d, "doc_id", "") or getattr(d, "source_doc_id", "") or ""
                
                print(f"[DEBUG] _t={_t}, allowlist_j={allowlist_j}, _did={_did}, is_processed={state_db.is_processed(_did) if state_db else 'No state_db'}")
                
                if _t not in allowlist_j:
                    logger.debug("[EARNINGS] ticker=%s not in allowlist. skip.", _t)
                    continue
                if state_db:
                    _log = state_db.get_log(_did)
                    _status = _log.get("status", "") if _log else ""
                    if _status in ("extract_failed", "parse_failed"):
                        logger.info("[EARNINGS] ticker=%s disclosure_id=%s has status=%s. Proceeding to earnings extraction.", _t, _did, _status)
                    elif state_db.is_processed(_did):
                        logger.info("[EARNINGS] ticker=%s disclosure_id=%s is already processed in state_db. skip.", _t, _did)
                        continue
                
                # ZIPダウンロード追加
                _xbrl_url = getattr(d, "xbrl_url", "") or ""
                _zip_path = ""
                
                # J-Quants オンデマンドXBRL取得
                source_doc_id = getattr(d, "source_doc_id", "") or ""
                if not _xbrl_url and source_doc_id and os.environ.get("JQUANTS_PRIMARY_ENABLED", "0") == "1":
                    try:
                        from src.jquants.adapter import get_file_url
                        logger.info(f"[EARNINGS][JQUANTS] fetching on-demand XBRL URL for {_t} (disc_no={source_doc_id})")
                        f_urls = get_file_url(source_doc_id, "x")
                        if f_urls and "xbrl" in f_urls:
                            _xbrl_url = f_urls["xbrl"]
                            logger.info(f"[EARNINGS][JQUANTS] on-demand fetch success")
                        else:
                            logger.warning(f"[EARNINGS][JQUANTS] on-demand fetch failed (no xbrl key in response)")
                    except Exception as e:
                        logger.warning(f"[EARNINGS][JQUANTS] on-demand fetch error: {e}")

                if _xbrl_url:
                    _zip_path = download_document(_xbrl_url, xbrl_dir, session=session, alternate_paths=[docs_dir]) or ""

                target_docs_j.append({
                    "ticker":        _t,
                    "company_name":  getattr(d, "company_name", ""),
                    "title":         getattr(d, "title", ""),
                    "source_title":  getattr(d, "title", ""),
                    "disclosed_at":  getattr(d, "disclosure_datetime", "") or getattr(d, "published_at", ""),
                    "source_url":    getattr(d, "doc_url", "") or "",
                    "pdf_url":       getattr(d, "doc_url", "") or "",
                    "doc_url":       getattr(d, "doc_url", "") or "",
                    "xbrl_url":      _xbrl_url,
                    "zip_path":      _zip_path,
                    "source_doc_id": getattr(d, "disclosure_id", "") or getattr(d, "doc_id", "") or "",
                })

            if not target_docs_j:
                logger.info("[EARNINGS] target_docs_j is empty after allowlist filter. returning early.")
                return result

            # ── worker 実行 ────────────────────────────────────────────────
            runner_summary_j = run_earnings_subprocess_dry_run(target_docs_j, worker_count=4, timeout_sec=60)
            results_by_ticker_j = {r.get("ticker"): r for r in runner_summary_j.get("results", [])}

            for doc_j in target_docs_j:
                _ticker = doc_j.get("ticker")
                _wr = results_by_ticker_j.get(_ticker)
                if not _wr or _wr.get("status") != "ok":
                    logger.warning("[EARNINGS][REAL] %s worker failed (status=%s). skipping save route.", _ticker, _wr.get("error_type") if _wr else "missing")
                    result.errors.append(f"{_ticker}: worker_failed={_wr.get('error_type') if _wr else 'missing'}")
                    continue

                try:
                    # ── payload 生成 / validation ───────────────────────────────
                    _payload = build_save_ready_payload(_wr, doc_j)
                    _valid_p, _reason_p = validate_save_ready_payload(_payload)
                    if not _valid_p:
                        logger.error("[EARNINGS][REAL] %s payload invalid: %s. skip this item.", _ticker, _reason_p)
                        result.errors.append(f"{_ticker}: payload_invalid={_reason_p}")
                        continue

                    # ── call plan 生成 / validation ──────────────────────────────
                    _save_plan = build_save_call_plan(_payload)
                    _discord_plan = build_discord_call_plan(_payload)
                    # save_plan に discord_plan をマージして渡す（discord_ready 診断のため）
                    _merged_plan = {**_save_plan, "discord_plan": _discord_plan}
                    _cp_valid, _cp_reason = validate_save_call_plan(
                        _merged_plan,
                        require_discord=enable_discord,  # Discord 無効時は discord_ready を必須にしない
                    )
                    if not _cp_valid:
                        logger.error("[EARNINGS][REAL] %s call_plan invalid: %s. STOP.", _ticker, _cp_reason)
                        result.errors.append(f"{_ticker}: call_plan_invalid={_cp_reason}")
                        return result

                    if dry_run:
                        logger.info("[EARNINGS][REAL] [DRY_RUN] would save ticker=%s. skipping save route.", _ticker)
                        result.validated_count += 1
                        continue

                    # ── SQLite 保存前: semantic duplicate ガード ───────────────────
                    # fingerprint の一致有無に関わらず、意味的な重複を検知してスキップ
                    _args = _merged_plan["earnings_summary_args"]
                    _dup_rec = find_semantic_duplicate(
                        conn=conn,
                        ticker=_args.get("ticker", ""),
                        fiscal_year=_args.get("fiscal_year", ""),
                        quarter=_args.get("quarter", ""),
                        disclosure_date=_args.get("disclosure_date", ""),
                        title=_args.get("title", ""),
                    )
                    if _dup_rec:
                        logger.info("[EARNINGS][REAL] %s semantic duplicate. skip.", _ticker)
                        result.already_exists_count += 1
                        continue

                    # ── SQLite 保存実行 ──────────────────────────────────────────
                    logger.info("[EARNINGS][REAL] ✁ SQLite保存: %s", _ticker)
                    save_earnings_summary(conn, _merged_plan["earnings_summary_args"])
                    result.saved_count += 1

                    # ── Supabase 保存実行 ────────────────────────────────────────
                    logger.info("[EARNINGS][REAL] ✁ Supabase保存: %s", _ticker)
                    _ev_dict = _merged_plan["tdnet_event_payload"]
                    
                    # ── Supabase ID の復元 ──
                    if state_db:
                        # state_db から元の Supabase ID があれば拾う (既存レコードの UPDATE 防止)
                        # 今回は新規レコードとして挿入するため、UUIDは新規生成する。
                        pass

                    _ev_rec = EventRecord(**_ev_dict)
                    _sup_res = save_event_to_supabase(_ev_rec)
                    _sup_ok = _sup_res.get("action") != "error"
                    _sup_err = _sup_res.get("error", "")
                    if not _sup_ok:
                        logger.error("[EARNINGS][REAL] %s Supabase 保存失敗: %s", _ticker, _sup_err)
                        result.errors.append(f"{_ticker}: supabase_error={_sup_err}")
                        return result

                    # ── canonical_financials 同期 ──────────────────────────────
                    if _sup_ok:
                        try:
                            _args = _merged_plan["earnings_summary_args"]
                            _title = _args.get("title", "")
                            _period = None
                            
                            # payload 内に period / fiscal_year_end があれば優先
                            _payload_ext = _payload.get("extracted", {})
                            _period_cand = _payload_ext.get("period") or _payload_ext.get("fiscal_year_end")
                            if _period_cand:
                                _period = _period_cand
                            else:
                                _period = _derive_fiscal_year_end_period(_title)
                                
                            if not _period:
                                logger.warning("[EARNINGS][REAL] %s period不明のためcanonical同期をスキップ (title=%r)", _ticker, _title)
                            else:
                                _q = _args.get("quarter", "")
                                _sales = _args.get("sales_value")
                                _op = _args.get("op_value")
                                _gross = _args.get("gross_profit_value")
                                _doc_id = _ev_dict.get("source_doc_id", "")
                                
                                logger.info("[EARNINGS][REAL] %s canonical同期開始 (period=%s, quarter=%s)", _ticker, _period, _q)
                                from lib.pipeline.canonical_writer import write_financials_canonical
                                
                                _metrics = {}
                                if _sales is not None: _metrics["sales"] = _sales / 1_000_000
                                if _op is not None: _metrics["operating_profit"] = _op / 1_000_000
                                if _gross is not None: _metrics["gross_profit"] = _gross / 1_000_000
                                
                                _url = os.getenv("SUPABASE_URL", "").rstrip("/")
                                _config = {
                                    "rest_url": f"{_url}/rest/v1" if _url else "",
                                    "key": os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
                                }
                                write_financials_canonical(
                                    ticker=_ticker,
                                    period=_period,
                                    quarter=_q,
                                    metrics_dict=_metrics,
                                    source="jquants_earnings_summary",
                                    filing_id=_doc_id,
                                    unit="millions_jpy",
                                    config=_config
                                )
                                logger.info("[EARNINGS][REAL] %s canonical同期完了", _ticker)
                        except Exception as e:
                            logger.error("[EARNINGS][REAL] %s canonical同期中にエラー: %s", _ticker, e)

                    # ── Discord 送信 ────────────────────────────────────────────
                    _discord_sent = False
                    if enable_discord:
                        logger.info("[EARNINGS][REAL] ✁ 通知送信: %s", _ticker)
                        _discord_msg = _merged_plan["discord_plan"]["discord_message"]
                        _d_ok = send_earnings_discord(webhook_url, _discord_msg)
                        if _d_ok:
                            result.notified_count += 1
                            _discord_sent = True
                        else:
                            logger.error("[EARNINGS][REAL] %s Discord 送信失敗.", _ticker)
                            result.errors.append(f"{_ticker}: discord_failed")
                            return result
                    else:
                        logger.info("[EARNINGS][REAL] ✁ 通知送信スキップ(ENABLE_DISCORD=0): %s", _ticker)

                    # ── state_db success 記録 ────────────────────────────────────
                    if enable_state_update:
                        if enable_discord and not _discord_sent:
                            logger.warning("[EARNINGS][REAL] %s Discord未送信のためstate_db更新をスキップ", _ticker)
                        elif not enable_discord:
                            logger.warning("[EARNINGS][REAL] %s ENABLE_DISCORD=0 のためstate_db更新をスキップ", _ticker)
                        else:
                            if state_db:
                                _did2 = doc_j.get("source_doc_id", "")
                                _fy = _merged_plan["earnings_summary_args"].get("fiscal_year", "")
                                _q = _merged_plan["earnings_summary_args"].get("quarter", "")
                                logger.info("[EARNINGS][REAL] ✁ state_db success 記録: %s", _ticker)
                                state_db.record(_did2, code=_ticker, year=_fy, quarter=_q, status='success')
                    else:
                        logger.info("[EARNINGS][REAL] ✁ state_db 更新スキップ(ENABLE_STATE_UPDATE=0): %s", _ticker)

                except Exception as e:
                    logger.exception("[EARNINGS][REAL] %s 予期せぬエラー: %s", _ticker, e)
                    result.errors.append(f"{_ticker}: runtime_error={str(e)[:80]}")
                    return result
            
            return result
        else:
            # ENABLE_REAL_SAVE=0 または allowlist 未設定 ➡️ 旧方式 fallback
            logger.warning(
                "[EARNINGS] USE_SUBPROCESS_WORKER=1 dry_run=%s but ENABLE_REAL_SAVE=%s allowlist=%s. "
                "Falling back to sequential mode for safety.",
                dry_run, enable_real_save, allowlist_j,
            )
    else:
        pass # sequential

    parse_success = 0
    parse_failed = 0

    # ---- Phase 0-2: 全件処理 → DB保存 ----
    for doc in tanshin_docs:
        ticker = doc.ticker

        try:
            # ---- XBRL取得 ----
            xbrl_path = None
            _xbrl_url = getattr(doc, 'xbrl_url', None)

            # J-Quants オンデマンドXBRL取得
            source_doc_id = getattr(doc, "source_doc_id", "") or ""
            if not _xbrl_url and source_doc_id and os.environ.get("JQUANTS_PRIMARY_ENABLED", "0") == "1":
                try:
                    from src.jquants.adapter import get_file_url
                    logger.info(f"[EARNINGS][JQUANTS] fetching on-demand XBRL URL for {ticker} (disc_no={source_doc_id})")
                    f_urls = get_file_url(source_doc_id, "x")
                    if f_urls and "xbrl" in f_urls:
                        _xbrl_url = f_urls["xbrl"]
                        logger.info(f"[EARNINGS][JQUANTS] on-demand fetch success")
                        if isinstance(doc, dict):
                            doc["xbrl_url"] = _xbrl_url
                        else:
                            setattr(doc, "xbrl_url", _xbrl_url)
                    else:
                        logger.warning(f"[EARNINGS][JQUANTS] on-demand fetch failed (no xbrl key in response)")
                except Exception as e:
                    logger.warning(f"[EARNINGS][JQUANTS] on-demand fetch error: {e}")

            if _xbrl_url:
                xbrl_path = download_document(_xbrl_url, xbrl_dir, session=session, alternate_paths=[docs_dir])
                if xbrl_path:
                    logger.info(f"[EARNINGS] {ticker} ZIP downloaded: {Path(xbrl_path).name}")
                else:
                    logger.info(f"[EARNINGS] {ticker} ZIP download failed: {_xbrl_url}")
            else:
                logger.info(f"[EARNINGS] {ticker} no xbrl_url, trying cache")

            if not xbrl_path:
                doc_id = str(getattr(doc, "doc_id", "")) or str(getattr(doc, "tdnet_id", ""))
                xbrl_path = _find_cached_xbrl(xbrl_dir, ticker, doc_id=doc_id)
                if xbrl_path:
                    logger.info(f"[EARNINGS] {ticker} found cached ZIP: {Path(xbrl_path).name}")
            if not xbrl_path:
                result.errors.append(f"{ticker}: XBRL ZIP not found")
                parse_failed += 1
                continue

            # ---- キャッシュ確認 (parsed) ----
            parser_version = "v4_2c_001"
            tdnet_id_str = str(getattr(doc, "tdnet_id", "")) or str(getattr(doc, "doc_id", ""))
            doc_url_str = str(getattr(doc, "doc_url", "")) or str(getattr(doc, "xbrl_url", ""))
            
            parsed_key = make_cache_key(f"tdnet_parsed:{parser_version}", doc_id=tdnet_id_str, url=doc_url_str)
            cached_parsed = load_json(parsed_key, conn) if conn else load_json(parsed_key)
            
            if cached_parsed:
                earnings_dict = cached_parsed.get("earnings")
                if earnings_dict:
                    # Restore nested dataclass for segments if present
                    if "segments" in earnings_dict and earnings_dict["segments"]:
                        from .summary_financials import SegmentFinancials
                        segs = []
                        for s in earnings_dict["segments"]:
                            segs.append(SegmentFinancials(**s))
                        earnings_dict["segments"] = segs
                    earnings = EarningsSummaryData(**earnings_dict)
                else:
                    earnings = None
                    
                company_name = cached_parsed.get("company_name", "")
                fiscal_year = cached_parsed.get("fiscal_year", "")
                quarter = cached_parsed.get("quarter", "")
                summary_line = cached_parsed.get("summary_line", "")
                segment_lines = cached_parsed.get("segment_lines", [])
                company_reasons = cached_parsed.get("company_reasons", [])
                segment_reasons = cached_parsed.get("segment_reasons", [])
                full_message = cached_parsed.get("full_message", "")
                
                guidance_dict = cached_parsed.get("guidance")
                guidance = GuidanceData(**guidance_dict) if guidance_dict else None
                
                is_4q = cached_parsed.get("is_4q", False)
                fy_reason = cached_parsed.get("fy_reason", "")
                
                if earnings is None:
                    continue
                    
                parse_success += 1
                result.validated_count += 1
                logger.info(f"[EARNINGS] {ticker} parsed cache hit! Bypassing heavy extraction.")
                
            else:
                if not xbrl_path:
                    logger.warning(f"[EARNINGS] {ticker} No XBRL found. skip.")
                    continue
                else:
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

                    if earnings is None:
                        continue

                    parse_success += 1
                    result.validated_count += 1

                # ---- 企業名フォールバック ----
                company_name = doc.company_name
                if not company_name:
                    extracted_name, _ = extract_company_info_from_zip(xbrl_path)
                    if extracted_name:
                        company_name = extracted_name

                # ---- fiscal_year / quarter 推定 (早期に行う) ----
                # doc 側を最優先
                fiscal_year = getattr(doc, "fiscal_year", "") or ""
                quarter = getattr(doc, "quarter", "") or ""
                # 欠損時のみ _parse_fiscal_info で補完
                if not fiscal_year or not quarter:
                    fy_p, q_p = _parse_fiscal_info(
                        doc.title, earnings, pdf_text="",
                        disclosed_at=getattr(doc, "disclosure_datetime", getattr(doc, "published_at", ""))
                    )
                    if not fiscal_year and fy_p:
                        fiscal_year = fy_p
                    if not quarter and q_p:
                        quarter = q_p

                # ---- 前期実績が欠損している場合の DB 補完 ----
                if (earnings.sales_prior is None or earnings.op_prior is None) and fiscal_year and quarter:
                    try:
                        from .tdnet_event_store import _get_supabase
                        client = _get_supabase()
                        if client:
                            prev_fy = str(int(fiscal_year) - 1)
                            # event_type='earnings', subtype=quarter, ticker=ticker
                            res = client.table("tdnet_events").select("raw_payload").eq("ticker", ticker).eq("event_type", "earnings").eq("event_subtype", quarter).execute()
                            if res.data:
                                for row in res.data:
                                    rp = row.get("raw_payload") or {}
                                    if isinstance(rp, str):
                                        try: rp = json.loads(rp)
                                        except: rp = {}
                                    prev_ext = rp.get("extracted") or {}
                                    if prev_ext.get("fiscal_year") == prev_fy:
                                        if earnings.sales_prior is None and prev_ext.get("sales_current"):
                                            earnings.sales_prior = prev_ext.get("sales_current")
                                        if earnings.op_prior is None and prev_ext.get("op_current"):
                                            earnings.op_prior = prev_ext.get("op_current")
                                
                                # YOY再計算
                                # setterがない場合は property を上書きできないので直接プロパティは更新できないが...
                                # EarningsSummaryData は dataclass なので直接再計算してセットしてもよいが、プロパティなので注意。
                                # wait, sales_yoy は property。なので内部状態を更新して参照時に計算させる。
                                pass
                            logger.info(f"[EARNINGS] {ticker} missing prior check done. prev_fy={prev_fy} quarter={quarter}")
                    except Exception as e:
                        logger.warning(f"[EARNINGS] {ticker} missing prior fallback failed: {e}")

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
                from src.period_normalizer import format_fiscal_period
                disp_label = format_fiscal_period(fiscal_year, quarter)
                disp_title = f"[{disp_label}] {doc.title}" if disp_label else doc.title

                full_message = format_earnings_message(
                    ticker=ticker,
                    company_name=company_name,
                    summary_line=summary_line,
                    segment_lines=segment_lines,
                    company_reasons=company_reasons,
                    segment_reasons=segment_reasons,
                    title=disp_title,
                )

                # ---- PDF取得・テキスト抽出 (フォールバック用) ----
                pdf_text = ""
                pdf_path_downloaded = None
                if getattr(doc, 'doc_url', None):
                    pdf_path_downloaded = download_document(doc.doc_url, xbrl_dir, session=session, alternate_paths=[docs_dir])
                    if pdf_path_downloaded and Path(pdf_path_downloaded).exists():
                        try:
                            import pdfplumber
                            with pdfplumber.open(pdf_path_downloaded) as pdf:
                                for page in pdf.pages[:3]:
                                    text = page.extract_text()
                                    if text:
                                        pdf_text += text + "\n"
                        except Exception as e:
                            logger.warning(f"[EARNINGS] PDF read failed {ticker}: {e}")

                # ---- 4Q専用: 来期ガイダンス + 見通し ----
                guidance: GuidanceData | None = None
                is_4q, fy_reason = _is_fy_or_4q(earnings, doc.title)
                # quarter を is_4q と同タイミングで確定（ログ・DB保存で一致させる）
                # fiscal_year, quarterは早期に抽出済み

                if is_4q and quarter not in ("FY", "4Q", "1Q", "2Q", "3Q"):
                    quarter = "FY"
                logger.info(
                    f"[EARNINGS] {ticker} is_fy_or_4q={is_4q} "
                    f"reason={fy_reason} "
                    f"quarter={quarter!r} title={doc.title[:40]!r} fiscal_year={fiscal_year!r}"
                )

                if is_4q or quarter == "1Q":
                    try:
                        actual_sales = earnings.sales_current if is_4q else None
                        actual_op = earnings.op_current if is_4q else None

                        # 1Q等の場合、前年FYの実績をDBから取得してactual_sales/opにセットする（YOY計算用）
                        if not is_4q and quarter == "1Q" and fiscal_year:
                            try:
                                from .tdnet_event_store import _get_supabase
                                client = _get_supabase()
                                if client:
                                    prev_fy = str(int(fiscal_year) - 1)
                                    res = client.table("tdnet_events").select("raw_payload").eq("ticker", ticker).eq("event_type", "earnings").eq("event_subtype", "FY").execute()
                                    if res.data:
                                        for row in res.data:
                                            rp = row.get("raw_payload") or {}
                                            if isinstance(rp, str):
                                                try: rp = json.loads(rp)
                                                except: rp = {}
                                            prev_ext = rp.get("extracted") or {}
                                            if prev_ext.get("fiscal_year") == prev_fy:
                                                actual_sales = prev_ext.get("sales_current")
                                                actual_op = prev_ext.get("op_current")
                                                logger.info(f"[EARNINGS] {ticker} 1Q guidance YOY fallback: loaded prev_fy={prev_fy} FY actuals from DB.")
                            except Exception as e:
                                logger.warning(f"[EARNINGS] {ticker} 1Q guidance YOY fallback failed: {e}")

                        guidance = extract_guidance_from_zip(
                            xbrl_path=xbrl_path,
                            actual_sales=actual_sales,
                            actual_op=actual_op,
                            pdf_path=pdf_path_downloaded,
                            pdf_text=pdf_text,
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


                # ---- キャッシュ保存 (parsed) ----
                if earnings is not None:
                    cache_payload = {
                        "earnings": dataclasses.asdict(earnings) if earnings else None,
                        "company_name": company_name,
                        "fiscal_year": fiscal_year,
                        "quarter": quarter,
                        "summary_line": summary_line,
                        "segment_lines": segment_lines,
                        "company_reasons": company_reasons,
                        "segment_reasons": segment_reasons,
                        "full_message": full_message,
                        "guidance": dataclasses.asdict(guidance) if guidance else None,
                        "is_4q": is_4q,
                        "fy_reason": fy_reason,
                    }
                    if conn:
                        save_json(parsed_key, cache_payload, conn)
                    else:
                        save_json(parsed_key, cache_payload)

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
                    "gross_profit_value": getattr(earnings, "gross_profit_current", None),
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

                # ---- 保存直前の期間ガード ----
                xbrl_fy = getattr(earnings, "fiscal_year", "") or ""
                xbrl_q = getattr(earnings, "quarter", "") or ""
                
                period_mismatch = False
                if xbrl_q and quarter and xbrl_q != quarter:
                    period_mismatch = True
                if xbrl_fy and fiscal_year and xbrl_fy != fiscal_year:
                    period_mismatch = True
                    
                if not xbrl_path:
                    period_mismatch = True # Force mismatch treatment for missing XBRL
                
                if period_mismatch:
                    logger.warning(f"[EARNINGS] {ticker} Period mismatch guard triggered! event={fiscal_year}/{quarter} xbrl={xbrl_fy}/{xbrl_q}")
                    action = "skipped_period_mismatch"
                    earnings.sales_current = None
                    earnings.op_current = None
                    earnings.gross_profit_current = None
                    earnings.sales_yoy = None
                    earnings.op_yoy = None
                    if hasattr(earnings, "has_yoy"):
                        earnings.has_yoy = False
                    # Update save_data with None for financials
                    save_data["sales_value"] = None
                    save_data["op_value"] = None
                    save_data["gross_profit_value"] = None
                else:
                    action = save_earnings_summary(conn, save_data)
                    
                if action == "inserted" or action == "skipped_period_mismatch":
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
def _find_cached_xbrl(xbrl_dir: str, ticker: str, doc_id: str = "") -> str | None:
    from .common_normalizers import extract_common_disclosure_no
    d = Path(xbrl_dir)
    if not d.is_dir():
        return None
        
    if not doc_id:
        return None

    common_id = extract_common_disclosure_no(doc_id)
    if not common_id:
        return None # hash or invalid

    candidates = sorted(d.glob(f"{ticker}_*.zip"), reverse=True)
    matches = []
    for c in candidates:
        zip_id = extract_common_disclosure_no(c.name)
        if zip_id and zip_id == common_id:
            matches.append(c)

    if len(matches) == 1:
        return str(matches[0])
    
    return None # Ambiguous or not found


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
        "source_doc_id": getattr(doc, "disclosure_id", "") or getattr(doc, "doc_id", "") or "",
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "sales_current": earnings.sales_current,
        "sales_label": getattr(earnings, "sales_label", ""),
        "sales_yoy": earnings.sales_yoy,
        "op_current": earnings.op_current,
        "op_label": getattr(earnings, "op_label", ""),
        "op_source": getattr(earnings, "op_source", ""),
        "op_yoy": earnings.op_yoy,
        "gross_profit_value": getattr(earnings, "gross_profit_current", None),
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
            "sales_forecast_low": guidance.sales_forecast_low,
            "sales_forecast_high": guidance.sales_forecast_high,
            "op_forecast": guidance.op_forecast,
            "op_forecast_low": guidance.op_forecast_low,
            "op_forecast_high": guidance.op_forecast_high,
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
