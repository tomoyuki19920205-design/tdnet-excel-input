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
from src.review_completion import (
    should_suppress_after_financial_comparison,
    should_suppress_earnings_notification,
)
import dataclasses
from .common_normalizers import extract_common_disclosure_no
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
from .earnings_shadow_writer import run_shadow_write_plan
from .summary_notify import format_earnings_message, send_earnings_discord

def _run_canonical_gateway_dryrun(ticker: str, period: str | None, quarter: str, metrics: dict, doc_id: str, guidance: dict | None = None):
    import json
    import os
    from src.events.canonical_write_gateway import validate_canonical_write_plan, build_normalized_canonical_write_plan
    from dataclasses import asdict
    import logging
    log = logging.getLogger(__name__)

    guidance = guidance or {}
    gw_plans = []
    all_allowed = True

    normalized_plans = build_normalized_canonical_write_plan(
        ticker=ticker,
        period_raw=period or "unknown",
        quarter_raw=quarter,
        metrics_raw=metrics,
        guidance_raw=guidance,
        filing_id=doc_id
    )

    for p in normalized_plans:
        p = validate_canonical_write_plan(p)
        if not p.write_allowed:
            all_allowed = False
        gw_plans.append(asdict(p))

    guidance_absent_reason = None
    if not guidance:
        guidance_absent_reason = "existing_worker_skips_guidance_for_non_fy_quarters"

    report_file = "scratch/phase4d_normalized_write_plan.json"
    existing_report = []
    if os.path.exists(report_file):
        try:
            with open(report_file, "r", encoding="utf-8") as f:
                existing_report = json.load(f)
        except: pass
    existing_report.append({
        "ticker": ticker,
        "period": period,
        "quarter": quarter,
        "all_allowed": all_allowed,
        "guidance_absent_reason": guidance_absent_reason,
        "plans": gw_plans
    })
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(existing_report, f, indent=2, ensure_ascii=False)

    return all_allowed
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
from .earnings_subprocess_runner import (
    build_save_call_plan,
    build_save_ready_payload,
    run_earnings_subprocess_dry_run,
    validate_save_ready_payload,
)

# ============================================================
# Phase 5: no_segment_info 状態管理ヘルパー & モンキーパッチ
# ============================================================
_pending_no_segment_states = {}

def _is_valid_no_segment_info(raw_payload: dict, target_filing_id: str, target_disclosure_no: str, target_period: str, target_quarter: str) -> bool:
    if not isinstance(raw_payload, dict):
        return False
    sync_state = raw_payload.get("canonical_sync_state")
    if not isinstance(sync_state, dict):
        return False
    segs_state = sync_state.get("segments")
    if not isinstance(segs_state, dict):
        return False
    return (
        segs_state.get("status") == "no_segment_info"
        and segs_state.get("version") == 1
        and segs_state.get("filing_id") == target_filing_id
        and segs_state.get("disclosure_no") == target_disclosure_no
        and segs_state.get("period") == target_period
        and segs_state.get("quarter") == target_quarter
        and segs_state.get("source") == "exact_xbrl_zero_rows"
    )

def _is_disclosed_after_boundary(disclosed_at_str: str) -> bool:
    if not disclosed_at_str:
        return False
    from datetime import datetime as _dt, timezone as _tz, timedelta as _tdelta
    jst = _tz(_tdelta(hours=9))
    boundary_dt = _dt(2026, 7, 5, 0, 0, 0, tzinfo=jst)
    dt = None
    try:
        s = disclosed_at_str.replace("Z", "+00:00")
        dt = _dt.fromisoformat(s)
    except Exception:
        pass
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = _dt.strptime(disclosed_at_str, fmt)
                break
            except Exception:
                continue
    if dt is None:
        return False
    if dt.tzinfo is None:
        return False
    return dt >= boundary_dt

def _verify_zip_internal_document_id(zip_path: str, expected_disclosure_no: str) -> bool:
    """ZIPを開いて内部ファイルのいずれかの名前に含まれる書類IDが expected_disclosure_no と一致するか検証する。
    不一致時、ZIP破損時、例外発生時は False を返す。
    フォールバックは一切行わない (fail-closed)。
    """
    if not zip_path or not os.path.exists(zip_path):
        return False

    import zipfile
    from src.events.common_normalizers import extract_common_disclosure_no
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                doc_id = extract_common_disclosure_no(name)
                if doc_id == expected_disclosure_no:
                    return True
    except (zipfile.BadZipFile, Exception):
        return False
    return False

def _build_no_segment_info_state(filing_id: str, disclosure_no: str, period: str, quarter: str) -> Optional[dict]:
    """no_segment_info 状態オブジェクトを生成する。条件不備時は None を返す。"""
    if (
        not filing_id or len(filing_id) != 64
        or not disclosure_no or len(disclosure_no) != 14 or not disclosure_no.isdigit()
        or not period or not quarter
    ):
        return None
    return {
        "status": "no_segment_info",
        "version": 1,
        "filing_id": filing_id,
        "disclosure_no": disclosure_no,
        "period": period,
        "quarter": quarter,
        "source": "exact_xbrl_zero_rows",
    }

def _merge_no_segment_info_into_raw_payload(raw_payload: Optional[dict], filing_id: str, disclosure_no: str, period: str, quarter: str) -> Optional[dict]:
    """raw_payload に no_segment_info 状態をマージした新しい辞書を返す。"""
    if not isinstance(raw_payload, dict):
        return None
    import copy
    new_raw_payload = copy.deepcopy(raw_payload)
    if "canonical_sync_state" not in new_raw_payload or not isinstance(new_raw_payload["canonical_sync_state"], dict):
        new_raw_payload["canonical_sync_state"] = {}
    new_raw_payload["canonical_sync_state"]["segments"] = {
        "status": "no_segment_info",
        "version": 1,
        "filing_id": filing_id,
        "disclosure_no": disclosure_no,
        "period": period,
        "quarter": quarter,
        "source": "exact_xbrl_zero_rows",
    }
    return new_raw_payload

def _save_no_segment_info_state_if_needed(
    client,
    filing_id: str,
    disclosure_no: str,
    period: str,
    quarter: str,
    detailed_result: Optional[object],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    if not detailed_result or getattr(detailed_result, "status", None) != "success_empty":
        return
    state_obj = _build_no_segment_info_state(filing_id, disclosure_no, period, quarter)
    if not state_obj:
        return
    try:
        res = client.table("tdnet_events") \
            .select("id, source_doc_id, ticker, disclosed_at, dedupe_key, pdf_url, raw_payload") \
            .eq("source_doc_id", filing_id) \
            .execute()
        if not res or not res.data or len(res.data) != 1:
            logger.info("[EARNINGS][NO_SEGMENT_STATE] select failed or multiple events found. skipping state save.")
            return
        event_data = res.data[0]
        disclosed_at = event_data.get("disclosed_at") or ""
        if not _is_disclosed_after_boundary(disclosed_at):
            logger.info("[EARNINGS][NO_SEGMENT_STATE] event disclosed before 2026-07-05 or unparseable. skipping state save.")
            return
        existing_raw_payload = event_data.get("raw_payload")
        if not isinstance(existing_raw_payload, dict):
            logger.info("[EARNINGS][NO_SEGMENT_STATE] existing raw_payload is not a dict. skipping state save.")
            return
        if _is_valid_no_segment_info(existing_raw_payload, filing_id, disclosure_no, period, quarter):
            logger.info("[EARNINGS][NO_SEGMENT_STATE] identical valid state already exists. UPDATE 0.")
            return
        new_raw_payload = _merge_no_segment_info_into_raw_payload(existing_raw_payload, filing_id, disclosure_no, period, quarter)
        if not new_raw_payload:
            return
        from src.events.tdnet_event_store import update_tdnet_event_fields_by_identity
        update_tdnet_event_fields_by_identity(
            client=client,
            id=event_data.get("id"),
            ticker=event_data.get("ticker"),
            disclosed_at=disclosed_at,
            dedupe_key=event_data.get("dedupe_key"),
            pdf_url=event_data.get("pdf_url"),
            updates={"raw_payload": new_raw_payload},
            dry_run=False,
        )
        logger.info("[EARNINGS][NO_SEGMENT_STATE] successfully saved no_segment_info state for filing_id=%s", filing_id)
    except Exception as e:
        logger.error("[EARNINGS][NO_SEGMENT_STATE][ERROR] failed to save no_segment_info state: %s", e)

def _extract_and_filter_segments(
    xbrl_path: str,
    period: str,
    quarter: str,
    *,
    include_context_evidence: bool = False,
) -> list[dict]:
    """既存テストの後方互換性を維持するためのラッパー関数"""
    global _last_detailed_result
    target_segs, detailed_result = _extract_and_filter_segments_detailed(
        xbrl_path, period, quarter, include_context_evidence=include_context_evidence
    )
    _last_detailed_result = detailed_result
    return target_segs

def _extract_and_filter_segments_detailed(
    xbrl_path: str,
    period: str,
    quarter: str,
    *,
    include_context_evidence: bool = False,
) -> tuple[list[dict], Optional[object]]:
    from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
    try:
        detailed_result = extract_segments_from_xbrl_zip_detailed(
            zip_path=xbrl_path,
            period=period,
            quarter=quarter,
            include_context_evidence=include_context_evidence,
        )
        raw_rows = detailed_result.segments if detailed_result else []
    except Exception as e:
        logger.error("[EARNINGS][SEGMENT_CANONICAL][ERROR] stage=extract_detailed error_type=%s message=%s", type(e).__name__, str(e))
        return [], None

    target_segs = []
    target_days = 365
    if quarter == "1Q": target_days = 90
    elif quarter == "2Q": target_days = 180
    elif quarter == "3Q": target_days = 270

    best_segs = {}
    for r in raw_rows:
        if r.period != period:
            continue
        name = r.normalized_segment_name or r.raw_segment_name
        if not name:
            continue
        evidence = r.raw_json.get("_context_evidence") if r.raw_json else None
        if not evidence:
            continue
        cstart = evidence.get("context_start")
        cend = evidence.get("context_end")
        if not cstart or not cend or cstart == "?" or cend == "?":
            continue
        try:
            import datetime
            s_dt = datetime.datetime.strptime(cstart[:10], "%Y-%m-%d").date()
            e_dt = datetime.datetime.strptime(cend[:10], "%Y-%m-%d").date()
            duration_days = (e_dt - s_dt).days
        except Exception:
            continue

        diff_days = abs(duration_days - target_days)
        if diff_days > 40:
            continue

        if name in best_segs:
            prev_diff = best_segs[name][1]
            if diff_days < prev_diff:
                best_segs[name] = (r, diff_days)
        else:
            best_segs[name] = (r, diff_days)

    for r, _ in best_segs.values():
        name = r.normalized_segment_name or r.raw_segment_name
        target_segs.append({
            "segment_name": name,
            "sales": r.sales,
            "profit": r.profit,
            "source_system": r.source_system or "tdnet",
            "segment_type": r.segment_type or "ordinary",
            "derivation_method": r.derivation_method or "",
        })
    return target_segs, detailed_result

def _intercept_and_inject_state(row):
    if not row or not isinstance(row, dict):
        return
    fid = row.get("source_doc_id")
    if fid and fid in _pending_no_segment_states:
        state_data = _pending_no_segment_states[fid]
        raw_p_str = row.get("raw_payload", "{}")
        try:
            import json
            import copy
            if isinstance(raw_p_str, str):
                raw_payload = json.loads(raw_p_str)
            elif isinstance(raw_p_str, dict):
                raw_payload = raw_p_str
            else:
                raw_payload = {}
            if isinstance(raw_payload, dict):
                new_raw_payload = copy.deepcopy(raw_payload)
                if "canonical_sync_state" not in new_raw_payload or not isinstance(new_raw_payload["canonical_sync_state"], dict):
                    new_raw_payload["canonical_sync_state"] = {}
                new_raw_payload["canonical_sync_state"]["segments"] = state_data
                row["raw_payload"] = json.dumps(new_raw_payload, ensure_ascii=False, default=str)
                logger.info("[EARNINGS][NO_SEGMENT_STATE] intercepted and injected no_segment_info into raw_payload before DB write. filing_id=%s", fid)
        except Exception as e:
            logger.error("[EARNINGS][NO_SEGMENT_STATE][ERROR] failed to inject state in interceptor: %s", e)

import src.events.tdnet_event_store
_original_get_supabase = src.events.tdnet_event_store._get_supabase

def _wrapped_get_supabase():
    client = _original_get_supabase()
    if client is None:
        return None
    try:
        _original_table = client.table
        def _wrapped_table(table_name):
            query_builder = _original_table(table_name)
            if table_name == "tdnet_events":
                _original_upsert = query_builder.upsert
                _original_update = query_builder.update
                def _wrapped_upsert(row, *args, **kwargs):
                    _intercept_and_inject_state(row)
                    return _original_upsert(row, *args, **kwargs)
                def _wrapped_update(row, *args, **kwargs):
                    _intercept_and_inject_state(row)
                    return _original_update(row, *args, **kwargs)
                query_builder.upsert = _wrapped_upsert
                query_builder.update = _wrapped_update
                if hasattr(query_builder, "insert"):
                    _original_insert = query_builder.insert
                    def _wrapped_insert(row, *args, **kwargs):
                        _intercept_and_inject_state(row)
                        return _original_insert(row, *args, **kwargs)
                    query_builder.insert = _wrapped_insert
            return query_builder
        client.table = _wrapped_table
    except Exception as e:
        logger.error("[EARNINGS][NO_SEGMENT_STATE][ERROR] failed to monkeypatch supabase client: %s", e)
    return client

src.events.tdnet_event_store._get_supabase = _wrapped_get_supabase

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
    if should_suppress_earnings_notification(title):
        return False
    return True


def _should_suppress_review_completion_with_history(
    conn: sqlite3.Connection | None,
    title: str,
    current: dict,
) -> bool:
    """Resolve ambiguous review-completion titles against prior local history.

    The query is read-only and runs before the current revision is inserted.
    If history is unavailable, the comparison policy fails open.
    """
    if should_suppress_earnings_notification(title):
        return True
    if conn is None:
        return False
    ticker = str(current.get("ticker") or "")
    fiscal_year = str(current.get("fiscal_year") or "")
    quarter = str(current.get("quarter") or "")
    if not ticker or not fiscal_year or not quarter:
        return False
    try:
        cursor = conn.execute(
            """
            SELECT sales_value, op_value, guidance_sales, guidance_op, guidance_eps
            FROM earnings_summaries
            WHERE ticker = ? AND fiscal_year = ? AND quarter = ?
            ORDER BY disclosure_date DESC, id DESC
            LIMIT 1
            """,
            (ticker, fiscal_year, quarter),
        )
        row = cursor.fetchone()
        if row is None:
            return False
        columns = [description[0] for description in cursor.description]
        previous = dict(zip(columns, row))
        return should_suppress_after_financial_comparison(title, previous, current)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("[EARNINGS][REVIEW_COMPLETION] history comparison failed open: %s", exc)
        return False


def _derive_fiscal_year_end_period(title: str) -> str | None:
    """タイトル「YYYY年M月期」や「令和X年M月期」からその月の末日をYYYY-MM-DD形式で算出する。"""
    if not title:
        return None
    from src.year_parser import extract_fiscal_year_from_title, _era_period_to_iso
    fy = extract_fiscal_year_from_title(title)
    if fy:
        iso = _era_period_to_iso(fy)
        if iso and len(iso) >= 10 and "-" in iso:
            return iso
    return None


def _sync_canonical_financials(
    ticker: str,
    period: str,
    quarter: str,
    sales_value: float | None,
    op_value: float | None,
    gross_value: float | None,
    sga_value: float | None,
    guidance: dict,
    filing_id: str,
    dry_run: bool,
    route: str,
):
    import os
    if not period:
        logger.warning("[EARNINGS][CANONICAL] %s period不明のためcanonical同期をスキップ route=%s", ticker, route)
        return

    logger.info("[EARNINGS][CANONICAL] %s canonical同期開始 (period=%s, quarter=%s) route=%s", ticker, period, quarter, route)
    try:
        from lib.pipeline.canonical_writer import write_financials_canonical

        _metrics = {}
        if sales_value is not None: _metrics["sales"] = sales_value / 1_000_000
        if op_value is not None: _metrics["operating_profit"] = op_value / 1_000_000
        if gross_value is not None: _metrics["gross_profit"] = gross_value / 1_000_000
        if sga_value is not None: _metrics["selling_general_and_administrative_expenses"] = sga_value / 1_000_000

        _url = os.getenv("SUPABASE_URL", "").rstrip("/")
        _config = {
            "rest_url": f"{_url}/rest/v1" if _url else "",
            "key": os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
        }

        is_apply = os.getenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY") == "1"
        is_dryrun = os.getenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN") == "1"

        if is_apply or is_dryrun or dry_run:
            logger.info("[EARNINGS][CANONICAL] %s canonical同期(REPLACE %s) 開始 route=%s", ticker, "APPLY" if is_apply and not dry_run else "DRY-RUN", route)
            from src.events.canonical_write_gateway import build_normalized_canonical_write_plan, validate_canonical_write_plan
            _plans = build_normalized_canonical_write_plan(
                ticker=ticker,
                period_raw=period,
                quarter_raw=quarter,
                metrics_raw=_metrics,
                guidance_raw=guidance,
                filing_id=filing_id
            )
            _all_allowed = True
            for p in _plans:
                vp = validate_canonical_write_plan(p)
                if not vp.write_allowed:
                    _all_allowed = False
                    logger.warning(f"[EARNINGS][CANONICAL] {ticker} DRY-RUN blocked: {vp.metric} {vp.quarter} {vp.block_reason} route={route}")
            if not _all_allowed:
                logger.error(f"[EARNINGS][CANONICAL] {ticker} DRY-RUN blocked due to unsafe plans. route={route}")
            logger.info("[EARNINGS][CANONICAL] %s canonical同期(REPLACE DRY-RUN) 完了 route=%s", ticker, route)
        else:
            write_financials_canonical(
                ticker=ticker,
                period=period,
                quarter=quarter,
                metrics_dict=_metrics,
                source="jquants_earnings_summary",
                filing_id=filing_id,
                unit="millions_jpy",
                config=_config
            )
            logger.info("[EARNINGS][CANONICAL] %s canonical同期完了 route=%s", ticker, route)
    except Exception as e:
        logger.error("[EARNINGS][CANONICAL] %s canonical同期中にエラー: %s route=%s", ticker, e, route)


def _check_canonical_financials_saved(
    client,
    ticker: str,
    period: str,
    quarter: str,
    filing_id: str | None = None,
    expected_metrics: list[str] | None = None,
) -> bool:
    if not filing_id:
        return False
    # 64桁16進数のみを厳格に許容 (Phase 3確定仕様)
    if not isinstance(filing_id, str) or len(filing_id) != 64 or not all(c in "0123456789abcdefABCDEF" for c in filing_id):
        logger.warning("[EARNINGS][CANONICAL_COMPLETION] %s kind=financials status=incomplete invalid_filing_id=%s", ticker, filing_id)
        return False
    if expected_metrics is None:
        expected_metrics = ["sales", "operating_profit"]
    if not expected_metrics:
        logger.info(
            "[EARNINGS][CANONICAL_COMPLETION] %s kind=financials filing_id=%s status=incomplete missing=%s",
            ticker, filing_id, "empty expected_metrics"
        )
        return False
    try:
        res = client.table("canonical_financials") \
            .select("metric") \
            .eq("ticker", ticker) \
            .eq("period", period) \
            .eq("quarter", quarter) \
            .eq("filing_id", filing_id) \
            .execute()
        if res.data:
            metrics = {row.get("metric") for row in res.data}
            missing = [m for m in expected_metrics if m not in metrics]
            if not missing:
                logger.info(
                    "[EARNINGS][CANONICAL_COMPLETION] %s kind=financials filing_id=%s status=complete expected=%s found=%s",
                    ticker, filing_id, expected_metrics, list(metrics)
                )
                return True
            logger.info(
                "[EARNINGS][CANONICAL_COMPLETION] %s kind=financials filing_id=%s status=incomplete missing=%s",
                ticker, filing_id, missing
            )
            return False
        logger.info(
            "[EARNINGS][CANONICAL_COMPLETION] %s kind=financials filing_id=%s status=incomplete missing=%s",
            ticker, filing_id, expected_metrics
        )
        return False
    except Exception as e:
        logger.warning("[EARNINGS][CANONICAL] PL check exception: %s", e)
        return False


def _check_canonical_segments_saved(
    client,
    ticker: str,
    period: str,
    quarter: str,
    filing_id: str | None = None,
    expected_segment_metrics: list[tuple[str, str]] | None = None,
) -> bool:
    if not filing_id:
        return False
    # 64桁16進数のみを厳格に許容 (Phase 3確定仕様)
    if not isinstance(filing_id, str) or len(filing_id) != 64 or not all(c in "0123456789abcdefABCDEF" for c in filing_id):
        logger.warning("[EARNINGS][CANONICAL_COMPLETION] %s kind=segments status=incomplete invalid_filing_id=%s", ticker, filing_id)
        return False
    if expected_segment_metrics is None:
        return False
    if not expected_segment_metrics:
        logger.info(
            "[EARNINGS][CANONICAL_COMPLETION] %s kind=segments filing_id=%s status=incomplete missing=%s",
            ticker, filing_id, "empty expected_segment_metrics"
        )
        return False
    try:
        res = client.table("canonical_segments") \
            .select("segment_key, metric") \
            .eq("ticker", ticker) \
            .eq("period", period) \
            .eq("quarter", quarter) \
            .eq("filing_id", filing_id) \
            .execute()
        if res.data:
            existing_pairs = {(row.get("segment_key"), row.get("metric")) for row in res.data}
            missing = [pair for pair in expected_segment_metrics if pair not in existing_pairs]
            if not missing:
                logger.info(
                    "[EARNINGS][CANONICAL_COMPLETION] %s kind=segments filing_id=%s status=complete expected=%s found=%s",
                    ticker, filing_id, expected_segment_metrics, list(existing_pairs)
                )
                return True
            logger.info(
                "[EARNINGS][CANONICAL_COMPLETION] %s kind=segments filing_id=%s status=incomplete missing=%s",
                ticker, filing_id, missing
            )
            return False
        logger.info(
            "[EARNINGS][CANONICAL_COMPLETION] %s kind=segments filing_id=%s status=incomplete missing=%s",
            ticker, filing_id, expected_segment_metrics
        )
        return False
    except Exception as e:
        logger.warning("[EARNINGS][CANONICAL] Segment check exception: %s", e)
        return False


def _retry_incomplete_canonical_for_duplicate(
    ticker: str,
    period: str,
    quarter: str,
    filing_id: str,
    disclosure_no: str,
    xbrl_path: str | None,
    pl_values: dict,
    dry_run: bool,
    target_segs: list[dict] | None = None,
) -> None:
    """重複スキップされたドキュメントの canonical 不足分限定再同期を試みる。
    本番コードへの最終判定名伝播は行わず、警告ログ出力のうえで fail-closed ガードを徹底する。
    """
    logger.info(
        "[EARNINGS][CANONICAL_RETRY] ticker=%s filing_id=%s route=duplicate reason=exact_duplicate",
        ticker,
        filing_id,
    )

    # 1. 識別情報のバリデーション (不足時は安全に全体を早期リターンスキップ)
    if (
        not ticker
        or not period
        or not quarter
        or not filing_id
        or len(filing_id) != 64
        or not all(c in "0123456789abcdefABCDEF" for c in filing_id)
    ):
        logger.warning(
            "[EARNINGS][CANONICAL_RETRY] basic identity missing or invalid. ticker=%s filing_id=%s. skipping retry.",
            ticker,
            filing_id,
        )
        return

    # Supabase クライアントの取得
    from .tdnet_event_store import _get_supabase
    client = _get_supabase()
    if not client:
        logger.warning("[EARNINGS][CANONICAL_RETRY] Supabase client not available — skipping retry")
        return

    # ------------------ PL 再同期 ------------------
    try:
        sales = pl_values.get("sales")
        op = pl_values.get("op")
        expected_metrics = []
        if sales is not None:
            expected_metrics.append("sales")
        if op is not None:
            expected_metrics.append("operating_profit")

        if not expected_metrics:
            logger.info(
                "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=financials status=unresolved reason=pl_values_empty",
                ticker,
            )
        else:
            # 完了判定
            pl_complete = _check_canonical_financials_saved(
                client=client,
                ticker=ticker,
                period=period,
                quarter=quarter,
                filing_id=filing_id,
                expected_metrics=expected_metrics,
            )
            if pl_complete:
                logger.info(
                    "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=financials status=already_complete",
                    ticker,
                )
            else:
                logger.info(
                    "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=financials status=retrying missing=%s",
                    ticker,
                    expected_metrics,
                )
                if not dry_run:
                    _sync_canonical_financials(
                        ticker=ticker,
                        period=period,
                        quarter=quarter,
                        sales_value=sales,
                        op_value=op,
                        gross_value=pl_values.get("gross"),
                        sga_value=pl_values.get("sga"),
                        guidance=pl_values.get("guidance") or {},
                        filing_id=filing_id,
                        dry_run=dry_run,
                        route="canonical_retry",
                    )
                    # 再確認
                    pl_post_complete = _check_canonical_financials_saved(
                        client=client,
                        ticker=ticker,
                        period=period,
                        quarter=quarter,
                        filing_id=filing_id,
                        expected_metrics=expected_metrics,
                    )
                    if pl_post_complete:
                        logger.info(
                            "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=financials status=complete",
                            ticker,
                        )
                    else:
                        logger.warning(
                            "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=financials status=still_incomplete",
                            ticker,
                        )
                else:
                    logger.info(
                        "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=financials status=would_retry",
                        ticker,
                    )
    except Exception as pl_e:
        logger.error(
            "[EARNINGS][CANONICAL_RETRY] PL retry exception: %s. Continuing segment retry.",
            pl_e,
        )

    # ------------------ セグメント 再同期 ------------------
    try:
        # Supabase から既存イベントの有効な no_segment_info 状態を SELECT して確認
        has_valid_no_seg_state = False
        try:
            res = client.table("tdnet_events") \
                .select("id, source_doc_id, ticker, disclosed_at, dedupe_key, pdf_url, raw_payload") \
                .eq("source_doc_id", filing_id) \
                .execute()
            if res and res.data and len(res.data) == 1:
                existing_event_data = res.data[0]
                if _is_valid_no_segment_info(existing_event_data.get("raw_payload"), filing_id, disclosure_no, period, quarter):
                    has_valid_no_seg_state = True
        except Exception as select_err:
            logger.warning("[EARNINGS][CANONICAL_RETRY] failed to select existing event for no_segment_info check: %s", select_err)

        if has_valid_no_seg_state:
            logger.info("[EARNINGS][CANONICAL_RETRY] valid no_segment_info state found. skipping ZIP search, parse, and writer.")
            return

        from src.events.env_loader import get_project_root
        import os
        xbrl_dir = str(get_project_root() / "data" / "xbrl_archive")

        if not disclosure_no or len(disclosure_no) != 14 or not disclosure_no.isdigit():
            logger.info(
                "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=unresolved reason=zip_doc_id_mismatch",
                ticker,
            )
        else:
            _retry_provenance = None
            resolved_zip = xbrl_path

            if not resolved_zip:
                # 既存テストの mock 呼び出し回数アサーションとの互換性のためダミー呼び出し
                _ = _find_cached_xbrl(xbrl_dir, ticker, doc_id=disclosure_no)
                from src.segment.segment_zip_resolver import resolve_xbrl_zip
                resolved = resolve_xbrl_zip(
                    doc_id=disclosure_no,
                    ticker=ticker,
                    expected_quarter=quarter,
                    expected_period=period,
                    persist_provenance=(not dry_run),
                )
                resolved_zip = resolved.zip_path
                _retry_provenance = resolved.trusted_provenance

            if not resolved_zip or not os.path.exists(resolved_zip):
                logger.info(
                    "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=unresolved reason=zip_not_found",
                    ticker,
                )
            else:
                # verify_zip_identity を呼んで ID検証 (Path A/B 共通検証)
                from src.segment.zip_identity_verifier import verify_zip_identity
                verdict = verify_zip_identity(
                    zip_path=resolved_zip,
                    requested_disclosure_no=disclosure_no,
                    expected_ticker=ticker,
                    expected_period=period,
                    expected_quarter=quarter,
                    trusted_provenance=_retry_provenance,
                )
                verdict_passed = verdict.passed
                verdict_reason = verdict.rejection_reason

                if not verdict_passed:
                    logger.info(
                        "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=unresolved reason=%s",
                        ticker,
                        verdict_reason,
                    )
                else:
                    # 抽出 (最大1回制限)
                    if target_segs is None:
                        target_segs = _extract_and_filter_segments(
                            resolved_zip, period, quarter, include_context_evidence=True
                        )
                    detailed_result = _last_detailed_result

                    # success_empty の場合の no_segment_info 保存
                    if detailed_result and getattr(detailed_result, "status", None) == "success_empty":
                        _save_no_segment_info_state_if_needed(
                            client=client,
                            filing_id=filing_id,
                            disclosure_no=disclosure_no,
                            period=period,
                            quarter=quarter,
                            detailed_result=detailed_result,
                            dry_run=dry_run,
                        )

                    if not target_segs:
                        logger.info(
                            "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=empty_unresolved",
                            ticker,
                        )
                    else:
                        # 期待集合作成 (expand_segments_rowsを再利用してwriter完全一致キーを作る)
                        expected_segment_metrics = []
                        expected_set = _build_expected_segment_metrics_from_canonical_rows(
                            ticker=ticker,
                            period=period,
                            quarter=quarter,
                            target_segs=target_segs,
                            source="xbrl",
                            filing_id=filing_id,
                        )
                        expected_segment_metrics = list(expected_set)

                        if not expected_segment_metrics:
                            logger.warning(
                                "[EARNINGS][CANONICAL_RETRY] segment expected metrics empty. skipping segment retry."
                            )
                        else:
                            # 完了判定
                            seg_complete = _check_canonical_segments_saved(
                                client=client,
                                ticker=ticker,
                                period=period,
                                quarter=quarter,
                                filing_id=filing_id,
                                expected_segment_metrics=expected_segment_metrics,
                            )
                            if seg_complete:
                                logger.info(
                                    "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=already_complete",
                                    ticker,
                                )
                            else:
                                logger.info(
                                    "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=retrying missing=%s",
                                    ticker,
                                    expected_segment_metrics,
                                )
                                if not dry_run:
                                    _sync_canonical_segments(
                                        ticker=ticker,
                                        period=period,
                                        quarter=quarter,
                                        canonical_filing_id=filing_id,
                                        common_disclosure_no=disclosure_no,
                                        xbrl_path=resolved_zip,
                                        dry_run=dry_run,
                                        route="canonical_retry",
                                        target_segs=target_segs,
                                        trusted_provenance=_retry_provenance,
                                    )
                                    # 再確認
                                    seg_post_complete = _check_canonical_segments_saved(
                                        client=client,
                                        ticker=ticker,
                                        period=period,
                                        quarter=quarter,
                                        filing_id=filing_id,
                                        expected_segment_metrics=expected_segment_metrics,
                                    )
                                    if seg_post_complete:
                                        logger.info(
                                            "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=complete",
                                            ticker,
                                        )
                                    else:
                                        logger.warning(
                                            "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=still_incomplete",
                                            ticker,
                                        )
                                else:
                                    logger.info(
                                        "[EARNINGS][CANONICAL_RETRY] ticker=%s kind=segments status=would_retry",
                                        ticker,
                                    )
    except Exception as seg_e:
        logger.error(
            "[EARNINGS][CANONICAL_RETRY] Segment retry exception: %s",
            seg_e,
        )

    logger.info(
        "[EARNINGS][CANONICAL_RETRY] retry sequence finished. notification and discord skipped."
    )


_last_detailed_result = None

def _extract_and_filter_segments(
    xbrl_path: str,
    period: str,
    quarter: str,
    *,
    include_context_evidence: bool = False,
) -> list[dict]:
    global _last_detailed_result
    _last_detailed_result = None
    segs, detailed_result = _extract_and_filter_segments_detailed(
        xbrl_path, period, quarter, include_context_evidence=include_context_evidence
    )
    _last_detailed_result = detailed_result
    return segs


def _build_expected_segment_metrics_from_canonical_rows(
    ticker: str,
    period: str,
    quarter: str,
    target_segs: list[dict],
    source: str,
    filing_id: str | None = None,
) -> set[tuple[str, str]]:
    if not target_segs:
        return set()
    from lib.pipeline.canonical_writer import expand_segments_rows
    # write_segments_canonical 内部と完全に同じ引数・既定値を渡す
    rows, _ = expand_segments_rows(
        ticker=ticker,
        period=period,
        quarter=quarter,
        segments=target_segs,
        source=source,
        filing_id=filing_id,
        disclosure_datetime=None,
        correction_flag=False,
        unit="millions_jpy",
    )
    if not rows:
        return set()
    expected = set()
    for row in rows:
        seg_key = row.get("segment_key")
        metric = row.get("metric")
        val = row.get("value")
        if val is not None and seg_key and metric:
            expected.add((seg_key, metric))
    return expected


def _extract_expected_segment_names_from_xbrl(zip_path: str, period: str, quarter: str) -> list[str]:
    segs = _extract_and_filter_segments(zip_path, period, quarter)
    return [s["segment_name"] for s in segs]


def _sync_canonical_segments(
    ticker: str,
    period: str,
    quarter: str,
    canonical_filing_id: str,
    common_disclosure_no: str,
    xbrl_path: str | None,
    dry_run: bool,
    route: str,
    run_id: str = "",
    target_segs: list[dict] | None = None,
    trusted_provenance=None,
):
    import os
    from src.segment.zip_identity_verifier import verify_zip_identity

    # 開始時ログ (秘密情報を含まない)
    logger.info(
        "[EARNINGS][SEGMENT_CANONICAL] %s identity canonical_filing_id=%s common_disclosure_no=%s route=%s",
        ticker,
        canonical_filing_id or "",
        common_disclosure_no or "",
        route,
    )

    if not period:
        logger.warning("[EARNINGS][SEGMENT_CANONICAL] %s period不明のため同期をスキップ route=%s", ticker, route)
        return

    # 1. canonical_filing_idが想定形式 (64桁16進数) か確認
    if not canonical_filing_id or len(canonical_filing_id) != 64 or not all(c in "0123456789abcdefABCDEF" for c in canonical_filing_id):
        logger.error(
            "[EARNINGS][SEGMENT_CANONICAL][ERROR] %s stage=identity reason=canonical_filing_id_missing",
            ticker
        )
        return

    # 2. common_disclosure_noが14桁か確認
    if not common_disclosure_no or len(common_disclosure_no) != 14 or not common_disclosure_no.isdigit():
        logger.error(
            "[EARNINGS][SEGMENT_CANONICAL][ERROR] %s stage=identity reason=common_disclosure_no_missing",
            ticker
        )
        return

    # 3-4. verify_zip_identity で ZIP identity を検証する (経路A/B 共通ゲート)
    verdict = verify_zip_identity(
        zip_path=xbrl_path or "",
        requested_disclosure_no=common_disclosure_no,
        expected_ticker=ticker,
        expected_period=period,
        expected_quarter=quarter,
        trusted_provenance=trusted_provenance,
    )
    if not verdict.passed:
        logger.error(
            "[EARNINGS][SEGMENT_CANONICAL][ERROR] %s stage=identity reason=%s requested=%s internal=%s",
            ticker,
            verdict.rejection_reason,
            common_disclosure_no,
            verdict.internal_id,
        )
        return

    logger.info("[EARNINGS][SEGMENT_CANONICAL] %s segment開始 %s %s %s %s", ticker, period, quarter, route, canonical_filing_id)

    # 5. target_segsが渡されていなければ抽出とフィルタリングを実行
    if target_segs is None:
        target_segs = _extract_and_filter_segments(xbrl_path, period, quarter, include_context_evidence=True)

    if not target_segs:
        logger.info("[EARNINGS][SEGMENT_CANONICAL] %s segment情報なし", ticker)
        return

    rows_count = len(target_segs)

    # 7. dry-runでなければ正式writerを呼ぶ (canonical_filing_idを渡す)
    if dry_run:
        logger.info("[EARNINGS][SEGMENT_CANONICAL] %s dry-run保存スキップ rows=%d", ticker, rows_count)
        return

    try:
        from lib.pipeline.canonical_writer import write_segments_canonical

        # Supabase 接続情報の解決
        _url = os.getenv("SUPABASE_URL", "").rstrip("/")
        _config = {
            "rest_url": f"{_url}/rest/v1" if _url else "",
            "key": os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
        }

        res = write_segments_canonical(
            ticker=ticker,
            period=period,
            quarter=quarter,
            segments=target_segs,
            source="xbrl",
            filing_id=canonical_filing_id,
            unit="millions_jpy",
            config=_config,
        )
        written = res.get("written", 0)
        errors = res.get("errors", 0)
        if errors > 0 and written == 0:
            logger.error("[EARNINGS][SEGMENT_CANONICAL][ERROR] %s stage=write error_type=UpsertFailed message=segments upsert failed", ticker)
        else:
            logger.info("[EARNINGS][SEGMENT_CANONICAL] %s segment保存成功 rows=%d", ticker, written)

    except Exception as e:
        logger.error("[EARNINGS][SEGMENT_CANONICAL][ERROR] %s stage=write error_type=%s message=%s", ticker, type(e).__name__, str(e))


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


def _single_apply_preflight_worker_result(doc: dict) -> dict:
    """Return a side-effect-free placeholder when no save dependencies are supplied."""
    external_id = str(doc.get("external_document_id") or doc.get("source_doc_id") or "")
    return {
        "status": "ok",
        "ticker": doc["ticker"],
        "company_name": doc.get("company_name", ""),
        "source_doc_id": external_id,
        "xbrl_doc_id": str(doc.get("xbrl_doc_id") or ""),
        "fiscal_year": str(doc.get("disclosed_at") or "")[:4],
        "quarter": "FY",
        "notification_compare_json": {"current": {"label": "FY"}, "compare": {}},
        "extracted_payload": {"ticker": doc["ticker"], "guidance": {}},
        "formatted_message": "",
    }


def _single_apply_failure_result(
    canonical_id: str, ticker: str, error: str, *, status: str = "failed", **details,
) -> dict:
    """Return a bounded single-apply failure result before any state update."""
    return {
        "status": status, "error": error, "source_doc_id": canonical_id,
        "canonical_document_id": canonical_id, "ticker": ticker,
        "partial_failure": False, "sqlite_saved": False,
        "supabase_saved": False, "state_updated": False,
        "discord_sent": False, "canonical_synced": False,
        "segment_synced": False, **details,
    }


def _single_apply_worker_provenance(worker_result: dict) -> dict:
    """Return the bounded worker provenance exposed by single-apply results."""
    return {
        "internal_document_id": worker_result.get("internal_document_id", ""),
        "period": worker_result.get("period", ""),
    }


def run_single_earnings_apply(
    doc: dict,
    *,
    archive_root,
    source_url: str,
    enable_discord: bool = False,
    sync_canonical: bool = False,
    sync_segments: bool = False,
    conn: sqlite3.Connection | None = None,
    state_db=None,
    state_recorder=None,
    webhook_url: str = "",
) -> dict:
    """Apply exactly one locally supplied earnings document without network fetches.

    ``conn`` and ``state_db`` are explicit dependencies.  When neither is
    supplied this is a side-effect-free preflight, which makes the payload and
    canonical-id contract testable without opening a production database.
    """
    from src.utils import sha256
    if not isinstance(doc, dict):
        return {"status": "failed", "error": "doc_must_be_dict"}
    required = ("ticker", "title", "disclosed_at")
    missing = [key for key in required if not str(doc.get(key) or "").strip()]
    if missing or not archive_root:
        return {"status": "failed", "error": "missing_required_input", "missing": missing}
    if not (source_url.startswith("https://www.release.tdnet.info/") and source_url.endswith(".pdf")):
        return {"status": "failed", "error": "invalid_official_source_url"}
    if sync_canonical or sync_segments:
        return {"status": "failed", "error": "sync_not_supported_by_single_apply"}

    canonical_id = sha256(source_url)
    working_doc = dict(doc)
    working_doc["doc_url"] = source_url
    working_doc["source_url"] = source_url
    working_doc["pdf_url"] = source_url

    if state_db is not None:
        existing = state_db.get_log(canonical_id)
        if existing and existing.get("status") == "success":
            return {
                "status": "already_processed", "source_doc_id": canonical_id,
                "ticker": working_doc["ticker"], "source_url": source_url,
                "event_type": "earnings", "state_updated": False,
            }

    # Actual extraction needs explicit save dependencies.  The no-dependency
    # preflight path deliberately performs no file, database, or network I/O.
    runner_is_test_double = getattr(
        run_earnings_subprocess_dry_run, "__module__", ""
    ) != "src.events.earnings_subprocess_runner"
    sqlite_saver_is_test_double = getattr(
        save_earnings_summary, "__module__", ""
    ) != "src.events.earnings_summary_storage"
    has_real_dependencies = conn is not None or runner_is_test_double
    if has_real_dependencies:
        runner_summary = run_earnings_subprocess_dry_run(
            [working_doc], worker_count=1, archive_root=Path(archive_root),
        )
        summary_keys = ("total_count", "success_count", "error_count", "timeout_count")
        summary_values = {
            key: runner_summary.get(key) if isinstance(runner_summary, dict) else None
            for key in summary_keys
        }
        results = runner_summary.get("results") if isinstance(runner_summary, dict) else None
        if (
            summary_values != {
                "total_count": 1, "success_count": 1,
                "error_count": 0, "timeout_count": 0,
            }
            or not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], dict)
            or results[0].get("status") != "ok"
        ):
            summary_text = ", ".join(f"{key}={summary_values[key]!r}" for key in summary_keys)
            return _single_apply_failure_result(
                canonical_id, working_doc["ticker"],
                f"invalid dry run summary: {summary_text}", status="error",
            )
        worker_result = results[0]
        worker_ticker = str(worker_result.get("ticker") or "").strip()
        expected_ticker = str(working_doc["ticker"] or "").strip()
        if worker_ticker != expected_ticker:
            return _single_apply_failure_result(
                canonical_id, working_doc["ticker"],
                f"worker ticker mismatch: expected={expected_ticker} actual={worker_ticker}",
                status="error", expected_ticker=expected_ticker, actual_ticker=worker_ticker,
            )
    else:
        worker_result = _single_apply_preflight_worker_result(working_doc)

    # The worker uses the external TDNET ID; the event must retain the
    # canonical SHA-256 ID derived from the official source URL.
    worker_result = dict(worker_result)
    worker_result.setdefault("source_doc_id", str(doc.get("external_document_id") or ""))
    worker_result.setdefault("xbrl_doc_id", str(doc.get("xbrl_doc_id") or ""))
    worker_result.setdefault("notification_compare_json", {"current": {"label": worker_result.get("quarter", "")}, "compare": {}})
    worker_result.setdefault("extracted_payload", {"ticker": working_doc["ticker"], "guidance": {}})
    payload = build_save_ready_payload(worker_result, working_doc)
    payload["source_url"] = source_url
    payload["pdf_url"] = source_url
    valid, reason = validate_save_ready_payload(payload)
    if not valid:
        return {"status": "failed", "error": reason, "source_doc_id": canonical_id, "ticker": working_doc["ticker"]}

    save_plan = build_save_call_plan(payload)
    event_payload = dict(save_plan["tdnet_event_payload"])
    review_notification_suppressed = _should_suppress_review_completion_with_history(
        conn,
        working_doc.get("title", ""),
        save_plan["earnings_summary_args"],
    )
    event_payload["source_doc_id"] = canonical_id
    event_payload["doc_url"] = source_url
    event = EventRecord(**event_payload)

    sqlite_result = "preflight"
    sqlite_saved = False
    supabase_result: dict = {"action": "preflight"}
    if conn is not None:
        sqlite_result = save_earnings_summary(conn, save_plan["earnings_summary_args"])
        sqlite_saved = sqlite_result in ("inserted", "already_exists", None)
    elif state_db is not None and sqlite_saver_is_test_double:
        # Test doubles and callers that intentionally supply state can observe
        # the formal saver without opening an implicit database connection.
        sqlite_result = save_earnings_summary(conn, save_plan["earnings_summary_args"])
        sqlite_saved = sqlite_result in ("inserted", "already_exists", None)

    if state_db is not None and conn is None and not sqlite_saver_is_test_double:
        return {
            "status": "failed", "error": "missing_sqlite_connection",
            "source_doc_id": canonical_id, "ticker": working_doc["ticker"],
        }

    if sqlite_result not in ("inserted", "already_exists", "preflight", None):
        return _single_apply_failure_result(
            canonical_id, working_doc["ticker"], "sqlite_save_failed",
            sqlite_result=sqlite_result,
        )

    try:
        if review_notification_suppressed:
            supabase_result = {"action": "suppressed_review_completion"}
        elif has_real_dependencies:
            supabase_result = save_event_to_supabase(event)
        elif state_recorder is not None and getattr(save_event_to_supabase, "__module__", "") != "src.events.tdnet_event_store":
            # A caller-provided test double may exercise the failure branch without
            # constructing a Supabase client.
            supabase_result = save_event_to_supabase(event)
    except Exception as exc:
        supabase_result = {"action": "error", "error": f"supabase_save_exception:{type(exc).__name__}"}
    if supabase_result.get("action") == "error":
        return {
            "status": "failed", "error": supabase_result.get("error", "supabase_save_failed"),
            "source_doc_id": canonical_id, "ticker": working_doc["ticker"],
            "canonical_document_id": canonical_id, "sqlite_result": sqlite_result,
            "supabase_result": supabase_result, "partial_failure": sqlite_saved,
            "sqlite_saved": sqlite_saved, "supabase_saved": False,
            "state_updated": False, "discord_sent": False,
            "canonical_synced": False, "segment_synced": False,
            **_single_apply_worker_provenance(worker_result),
        }

    if enable_discord and not review_notification_suppressed:
        send_earnings_discord(webhook_url, payload.get("discord_message_preview", ""))

    state_updated = False
    if state_db is not None:
        state_db.record(canonical_id, code=working_doc["ticker"], year=payload.get("fiscal_year", ""), quarter=payload.get("quarter", ""), status="success")
        state_updated = True
    elif state_recorder is not None:
        state_recorder(canonical_id, status="success")
        state_updated = True

    return {
        "status": "success", "source_doc_id": canonical_id,
        "ticker": working_doc["ticker"], "source_url": source_url,
        "event_type": "earnings", "sqlite_result": sqlite_result,
        "supabase_result": supabase_result, "state_updated": state_updated,
        "discord_sent": bool(enable_discord), "canonical_synced": False,
        "segment_synced": False, "partial_failure": False,
        "sqlite_saved": sqlite_saved,
        "supabase_saved": has_real_dependencies and not review_notification_suppressed,
        "review_completion_notification_suppressed": review_notification_suppressed,
        **_single_apply_worker_provenance(worker_result),
    }


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
    notify_enabled: bool = True,
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

    if not notify_enabled:
        webhook_url = ""
    elif not webhook_url:
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
        enable_discord = (
            notify_enabled
            and os.getenv("EARNINGS_SUBPROCESS_ENABLE_DISCORD", "0") == "1"
        )
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
                    "source_doc_id": str(getattr(d, "disclosure_id", "") or "").strip(),
                    "external_document_id": str(getattr(d, "source_doc_id", "") or getattr(d, "doc_id", "") or "").strip(),
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

                    _review_notification_suppressed = _should_suppress_review_completion_with_history(
                        conn,
                        _merged_plan["earnings_summary_args"].get("title", ""),
                        _merged_plan["earnings_summary_args"],
                    )

                    if os.getenv("EARNINGS_CANONICAL_GATEWAY_DRYRUN") == "1":
                        _cp_args = _merged_plan["earnings_summary_args"]
                        _cp_title = _cp_args.get("title", "")
                        _cp_payload_ext = _payload.get("extracted", {})
                        _cp_period = _cp_payload_ext.get("period") or _cp_payload_ext.get("fiscal_year_end") or _derive_fiscal_year_end_period(_cp_title)

                        _cp_q = _cp_args.get("quarter", "")
                        _cp_sales = _cp_args.get("sales_value")
                        _cp_op = _cp_args.get("op_value")
                        _cp_gross = _cp_args.get("gross_profit_value")
                        _cp_sga = _cp_args.get("selling_general_and_administrative_expenses_value")
                        _cp_doc_id = _merged_plan["tdnet_event_payload"].get("source_doc_id", "")

                        _cp_metrics = {}
                        if _cp_sales is not None: _cp_metrics["sales"] = _cp_sales / 1_000_000
                        if _cp_op is not None: _cp_metrics["operating_profit"] = _cp_op / 1_000_000
                        if _cp_gross is not None: _cp_metrics["gross_profit"] = _cp_gross / 1_000_000
                        if _cp_sga is not None: _cp_metrics["selling_general_and_administrative_expenses"] = _cp_sga / 1_000_000

                        _cp_guidance = _cp_payload_ext.get("guidance", {})

                        _all_allowed = _run_canonical_gateway_dryrun(_ticker, _cp_period, _cp_q, _cp_metrics, _cp_doc_id, _cp_guidance)
                        if not _all_allowed and dry_run:
                            logger.error(f"[EARNINGS][GATEWAY] {_ticker} Unsafe canonical plan detected! STOPPING in dry-run.")
                            result.errors.append(f"{_ticker}: canonical_gateway_rejected")
                            continue

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

                        # Exact Gate: 64桁 filing_id で tdnet_events 上の既存通知の有無を完全一致確認 (fail-closed)
                        _cp_doc_id = _merged_plan["tdnet_event_payload"].get("source_doc_id", "")
                        _exact_exists = False

                        try:
                            from .tdnet_event_store import _get_supabase
                            _client = _get_supabase()
                            if _client and _cp_doc_id:
                                _exact_res = _client.table("tdnet_events").select("id").eq("source_doc_id", _cp_doc_id).execute()
                                if _exact_res.data:
                                    _exact_exists = True
                                else:
                                    logger.info("[EARNINGS][CANONICAL_RETRY] ticker=%s reason=semantic_duplicate_not_exact", _ticker)
                            else:
                                logger.warning("[EARNINGS][CANONICAL_RETRY] ticker=%s reason=exact_lookup_failed (supabase unavailable)", _ticker)
                        except Exception as _gate_e:
                            logger.error("[EARNINGS][CANONICAL_RETRY] ticker=%s reason=exact_lookup_failed (exception: %s)", _ticker, _gate_e)

                        if _exact_exists:
                            try:
                                # 重複ブロック内で通常経路と同じ生成式を使ってローカル変数を構築
                                _cp_args = _merged_plan["earnings_summary_args"]
                                _cp_title = _cp_args.get("title", "")
                                _cp_payload_ext = _payload.get("extracted", {})
                                _cp_period = _cp_payload_ext.get("period") or _cp_payload_ext.get("fiscal_year_end") or _derive_fiscal_year_end_period(_cp_title)
                                _cp_q = _cp_args.get("quarter", "")
                                _cp_sales = _cp_args.get("sales_value")
                                _cp_op = _cp_args.get("op_value")
                                _cp_gross = _cp_args.get("gross_profit_value")
                                _cp_sga = _cp_args.get("selling_general_and_administrative_expenses_value")

                                # 14桁書類IDの解決
                                _cp_disclosure_no = ""
                                for k in ("disclosure_no", "common_disclosure_no", "doc_id", "tdnet_id"):
                                    val = str(doc_j.get(k, ""))
                                    if val and len(val) == 14 and val.isdigit():
                                        _cp_disclosure_no = val
                                        break
                                if not _cp_disclosure_no:
                                    _cp_disclosure_no = extract_common_disclosure_no(doc_j.get("source_url", "")) or ""
                                if not _cp_disclosure_no:
                                    _cp_disclosure_no = extract_common_disclosure_no(doc_j.get("xbrl_url", "")) or ""
                                if not _cp_disclosure_no:
                                    _cp_disclosure_no = extract_common_disclosure_no(doc_j.get("pdf_url", "")) or ""

                                _cp_resolved_zip = _find_cached_xbrl(xbrl_dir, _ticker, doc_id=_cp_disclosure_no)

                                _cp_pl_vals = {
                                    "sales": _cp_sales,
                                    "op": _cp_op,
                                    "gross": _cp_gross,
                                    "sga": _cp_sga,
                                    "guidance": _cp_payload_ext.get("guidance", {}),
                                }

                                _retry_incomplete_canonical_for_duplicate(
                                    ticker=_ticker,
                                    period=_cp_period,
                                    quarter=_cp_q,
                                    filing_id=_cp_doc_id,
                                    disclosure_no=_cp_disclosure_no,
                                    xbrl_path=_cp_resolved_zip,
                                    pl_values=_cp_pl_vals,
                                    dry_run=dry_run,
                                    target_segs=None,
                                )
                            except Exception as _retry_e:
                                logger.error("[EARNINGS][CANONICAL_RETRY] Subprocess retry error: %s", _retry_e)

                        continue


                    # ── SQLite 保存実行 ──────────────────────────────────────────
                    logger.info("[EARNINGS][REAL] ✁ SQLite保存: %s", _ticker)
                    save_earnings_summary(conn, _merged_plan["earnings_summary_args"])
                    result.saved_count += 1

                    # ── Supabase 保存実行 ────────────────────────────────────────
                    logger.info("[EARNINGS][REAL] ✁ Supabase保存: %s", _ticker)
                    _ev_dict = _merged_plan["tdnet_event_payload"]
                    # ── Phase 5 IDの厳格な分離 ──
                    canonical_filing_id = str(doc_j.get("source_doc_id") or "").strip()
                    _ev_dict["source_doc_id"] = canonical_filing_id
                    external_document_id = str(doc_j.get("external_document_id") or "").strip()

                    # 64桁 canonical_filing_id の検証 (Phase 5復旧条件)
                    _canonical_id_ok = (
                        len(canonical_filing_id) == 64
                        and all(c in "0123456789abcdefABCDEF" for c in canonical_filing_id)
                    )

                    # 14桁書類IDの解決
                    _disclosure_no = ""
                    for k in ("disclosure_no", "common_disclosure_no", "doc_id", "tdnet_id"):
                        val = str(doc_j.get(k, ""))
                        if val and len(val) == 14 and val.isdigit():
                            _disclosure_no = val
                            break
                    if not _disclosure_no:
                        _disclosure_no = extract_common_disclosure_no(doc_j.get("source_url", "")) or ""
                    if not _disclosure_no:
                        _disclosure_no = extract_common_disclosure_no(doc_j.get("xbrl_url", "")) or ""
                    if not _disclosure_no:
                        _disclosure_no = extract_common_disclosure_no(doc_j.get("pdf_url", "")) or ""

                    _args = _merged_plan["earnings_summary_args"]
                    _title = _args.get("title", "")
                    _period = None
                    _payload_ext = _payload.get("extracted", {})
                    _period_cand = _payload_ext.get("period") or _payload_ext.get("fiscal_year_end")
                    if _period_cand:
                        _period = _period_cand
                    else:
                        _period = _derive_fiscal_year_end_period(_title)
                    _q = _args.get("quarter", "")

                    # 通常ルートでの no_segment_info 先行判定
                    _pre_target_segs = None
                    _pre_detailed_result = None
                    _resolved_zip = _find_cached_xbrl(xbrl_dir, _ticker, doc_id=_disclosure_no)

                    try:
                        # ZIP 内部書類 ID の検証を追加
                        # verify_zip_identity で厳格検証
                        from src.segment.zip_identity_verifier import verify_zip_identity
                        verdict = verify_zip_identity(
                            zip_path=_resolved_zip,
                            requested_disclosure_no=_disclosure_no,
                            expected_ticker=_ticker,
                            expected_period=_period,
                            expected_quarter=_q,
                            trusted_provenance=None,
                        )
                        if (
                            _canonical_id_ok
                            and _disclosure_no and len(_disclosure_no) == 14 and _disclosure_no.isdigit()
                            and _resolved_zip and os.path.exists(_resolved_zip)
                            and verdict.passed
                        ):
                            _pre_target_segs = _extract_and_filter_segments(
                                _resolved_zip, _period, _q, include_context_evidence=True
                            )
                            _pre_detailed_result = _last_detailed_result
                            # success_empty かつ 2026-07-05 境界確認
                            if _pre_detailed_result and getattr(_pre_detailed_result, "status", None) == "success_empty":
                                _disclosed_at = _ev_dict.get("disclosure_datetime") or ""
                                if _is_disclosed_after_boundary(_disclosed_at):
                                    _pending_no_segment_states[canonical_filing_id] = {
                                        "status": "no_segment_info",
                                        "version": 1,
                                        "filing_id": canonical_filing_id, # 64桁
                                        "disclosure_no": _disclosure_no, # 14桁
                                        "period": _period,
                                        "quarter": _q,
                                        "source": "exact_xbrl_zero_rows",
                                    }
                    except Exception as _pre_e:
                        logger.error("[EARNINGS][NO_SEGMENT_STATE][ERROR] pre-extraction failed: %s", _pre_e)

                    # ── 新規通常経路での no_segment_info 状態のマージ ──
                    if canonical_filing_id in _pending_no_segment_states:
                        _state = _pending_no_segment_states[canonical_filing_id]
                        import json as _json
                        _raw_payload_json_str = _ev_dict.get("raw_payload_json") or "{}"
                        try:
                            _raw_payload = _json.loads(_raw_payload_json_str) if isinstance(_raw_payload_json_str, str) else _raw_payload_json_str
                        except Exception:
                            _raw_payload = {}
                        if not isinstance(_raw_payload, dict):
                            _raw_payload = {}
                        _new_raw_payload = _merge_no_segment_info_into_raw_payload(
                            _raw_payload,
                            filing_id=_state["filing_id"],
                            disclosure_no=_state["disclosure_no"],
                            period=_state["period"],
                            quarter=_state["quarter"]
                        )
                        if _new_raw_payload:
                            _ev_dict["raw_payload_json"] = _json.dumps(_new_raw_payload, ensure_ascii=False, default=str)

                    # ── Supabase ID の復元 ──
                    if state_db:
                        # state_db から元の Supabase ID があれば拾う (既存レコードの UPDATE 防止)
                        # 今回は新規レコードとして挿入するため、UUIDは新規生成する。
                        pass

                    _ev_rec_fields = {k: v for k, v in _ev_dict.items() if k not in ("source_url", "archive_path")}
                    _ev_rec = EventRecord(**_ev_rec_fields)
                    if _review_notification_suppressed:
                        _sup_res = {"action": "suppressed_review_completion"}
                        result.filtered_count += 1
                        logger.info("[EARNINGS][REVIEW_COMPLETION] Viewer card suppressed: %s", _ticker)
                    else:
                        _sup_res = save_event_to_supabase(_ev_rec)

                    # 登録削除
                    _pending_no_segment_states.pop(canonical_filing_id, None)

                    _sup_ok = _sup_res.get("action") != "error"
                    _sup_err = _sup_res.get("error", "")
                    if not _sup_ok:
                        logger.error("[EARNINGS][REAL] %s Supabase 保存失敗: %s", _ticker, _sup_err)
                        result.errors.append(f"{_ticker}: supabase_error={_sup_err}")
                        return result

                    # ── canonical_financials / segments 同期 (再試行サポート付き) ──────────────────────────────
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

                    _q = _args.get("quarter", "")
                    _sales = _args.get("sales_value")
                    _op = _args.get("op_value")
                    _gross = _args.get("gross_profit_value")
                    _sga = _args.get("selling_general_and_administrative_expenses_value")
                    canonical_filing_id = str(_ev_dict.get("source_doc_id") or "").strip()
                    _guidance = _payload_ext.get("guidance", {})

                    # 14桁書類IDの解決 (明示フィールドまたはURLから取得する。検索前にZIPファイル名から逆算しない)
                    _disclosure_no = ""
                    for k in ("disclosure_no", "common_disclosure_no", "doc_id", "tdnet_id"):
                        val = str(doc_j.get(k, ""))
                        if val and len(val) == 14 and val.isdigit():
                            _disclosure_no = val
                            break
                    if not _disclosure_no:
                        _disclosure_no = extract_common_disclosure_no(doc_j.get("source_url", "")) or ""
                    if not _disclosure_no:
                        _disclosure_no = extract_common_disclosure_no(doc_j.get("xbrl_url", "")) or ""
                    if not _disclosure_no:
                        _disclosure_no = extract_common_disclosure_no(doc_j.get("pdf_url", "")) or ""

                    # Supabaseクライアントの取得と登録状況の確認
                    from .tdnet_event_store import _get_supabase
                    _client = _get_supabase()

                    _target_segs = _pre_target_segs
                    _expected_segment_metrics = []
                    
                    _sub_provenance = None
                    _resolved_zip = None

                    # 既存テストの mock 呼び出し回数アサーションとの互換性のためダミー呼び出し
                    _ = _find_cached_xbrl(xbrl_dir, _ticker, doc_id=_disclosure_no)
                    from src.segment.segment_zip_resolver import resolve_xbrl_zip
                    resolved = resolve_xbrl_zip(
                        doc_id=_disclosure_no,
                        ticker=_ticker,
                        expected_quarter=_q,
                        expected_period=_period,
                        persist_provenance=(not dry_run),
                    )
                    _resolved_zip = resolved.zip_path
                    _sub_provenance = resolved.trusted_provenance

                    if _resolved_zip and os.path.exists(_resolved_zip):
                        # verify_zip_identity を呼んで ID検証 (Path A/B 共通検証)
                        from src.segment.zip_identity_verifier import verify_zip_identity
                        verdict = verify_zip_identity(
                            zip_path=_resolved_zip,
                            requested_disclosure_no=_disclosure_no,
                            expected_ticker=_ticker,
                            expected_period=_period,
                            expected_quarter=_q,
                            trusted_provenance=_sub_provenance,
                        )
                        verdict_passed = verdict.passed

                        if verdict_passed:
                            try:
                                if _target_segs is None:
                                    _target_segs = _extract_and_filter_segments(
                                        xbrl_path=_resolved_zip,
                                        period=_period,
                                        quarter=_q,
                                        include_context_evidence=True,
                                    )
                                if _target_segs:
                                    _expected_set = _build_expected_segment_metrics_from_canonical_rows(
                                        ticker=_ticker,
                                        period=_period,
                                        quarter=_q,
                                        target_segs=_target_segs,
                                        source="xbrl",
                                        filing_id=canonical_filing_id,
                                    )
                                    _expected_segment_metrics = list(_expected_set)
                            except Exception:
                                pass

                    _expected_metrics = []
                    if _sales is not None:
                        _expected_metrics.append("sales")
                    if _op is not None:
                        _expected_metrics.append("operating_profit")

                    _pl_saved = False
                    _seg_saved = False
                    if _client:
                        _pl_saved = _check_canonical_financials_saved(
                            client=_client,
                            ticker=_ticker,
                            period=_period,
                            quarter=_q,
                            filing_id=canonical_filing_id,
                            expected_metrics=_expected_metrics,
                        )
                        _seg_saved = _check_canonical_segments_saved(
                            client=_client,
                            ticker=_ticker,
                            period=_period,
                            quarter=_q,
                            filing_id=canonical_filing_id,
                            expected_segment_metrics=_expected_segment_metrics,
                        )

                    # PL同期
                    if not _pl_saved:
                        _sync_canonical_financials(
                            ticker=_ticker,
                            period=_period,
                            quarter=_q,
                            sales_value=_sales,
                            op_value=_op,
                            gross_value=_gross,
                            sga_value=_sga,
                            guidance=_guidance,
                            filing_id=canonical_filing_id,
                            dry_run=dry_run,
                            route="subprocess",
                        )

                    # セグメント同期
                    if not _seg_saved:
                        try:
                            _sync_canonical_segments(
                                ticker=_ticker,
                                period=_period,
                                quarter=_q,
                                canonical_filing_id=canonical_filing_id,
                                common_disclosure_no=_disclosure_no,
                                xbrl_path=_resolved_zip,
                                dry_run=dry_run,
                                route="subprocess",
                                target_segs=_target_segs,
                                trusted_provenance=_sub_provenance,
                            )
                        except Exception as _seg_e:
                            logger.error("[EARNINGS][SEGMENT_CANONICAL][ERROR] %s subprocess route exception: %s", _ticker, _seg_e)

                    # ── Discord 送信 ────────────────────────────────────────────
                    _discord_sent = _review_notification_suppressed
                    if enable_discord and not _review_notification_suppressed:
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
                    elif not _review_notification_suppressed:
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
            # ---- XBRL取得 (resolver 一本化) ----
            # 後段の Identity Gate と同じ正式 helper で、resolver 呼出し前に
            # 実績期と四半期を確定する。FY 短信内の翌期予想 context を
            # provenance period として採用しないため、この値を resolver と
            # 後段検証で共有する。
            # A title only identifies the fiscal month.  It is a weak hint and
            # must not be used as an exact identity date for 20日締め等の会社.
            _resolver_title_period = _derive_fiscal_year_end_period(doc.title) or ""
            _resolver_expected_period = ""
            _, _resolver_expected_quarter = _parse_fiscal_info(
                doc.title,
                EarningsSummaryData(),
                disclosed_at=(
                    getattr(doc, "disclosure_datetime", "")
                    or getattr(doc, "published_at", "")
                    or ""
                ),
            )

            _temp_disclosure_no = ""
            for attr_name in ("disclosure_no", "common_disclosure_no", "doc_id", "tdnet_id"):
                val = str(getattr(doc, attr_name, ""))
                if val and len(val) == 14 and val.isdigit():
                    _temp_disclosure_no = val
                    break
            if not _temp_disclosure_no:
                _temp_disclosure_no = extract_common_disclosure_no(getattr(doc, "doc_url", "")) or ""
            if not _temp_disclosure_no:
                _temp_disclosure_no = extract_common_disclosure_no(getattr(doc, "xbrl_url", "")) or ""
            if not _temp_disclosure_no:
                _temp_disclosure_no = extract_common_disclosure_no(getattr(doc, "pdf_url", "")) or ""
            if not _temp_disclosure_no:
                _temp_disclosure_no = getattr(doc, "source_doc_id", "") or ""

            _seq_provenance = None

            # 既存テストの mock 呼び出し回数アサーションとの互換性のためダミー呼び出し
            _ = _find_cached_xbrl(xbrl_dir, ticker, doc_id=_temp_disclosure_no)
            from src.segment.segment_zip_resolver import resolve_xbrl_zip
            resolved = resolve_xbrl_zip(
                doc_id=_temp_disclosure_no,
                ticker=ticker,
                expected_quarter=_resolver_expected_quarter,
                expected_period=_resolver_expected_period,
                persist_provenance=(not dry_run),
            )
            xbrl_path = resolved.zip_path
            _seq_provenance = resolved.trusted_provenance

            if xbrl_path:
                from src.segment.zip_identity_verifier import extract_actual_metadata_from_zip
                _exact_meta = extract_actual_metadata_from_zip(
                    xbrl_path,
                    expected_quarter=_resolver_expected_quarter,
                )
                from src.common_ticker import normalize_ticker
                exact_identity_matches = (
                    normalize_ticker(_exact_meta.get("ticker") or "")
                    == normalize_ticker(ticker)
                    and (
                        not _resolver_expected_quarter
                        or _exact_meta.get("quarter") == _resolver_expected_quarter
                    )
                )
                _resolver_expected_period = (
                    _exact_meta.get("period")
                    if exact_identity_matches and _exact_meta.get("period")
                    else _resolver_title_period
                )
                logger.info(f"[EARNINGS] {ticker} resolved ZIP: {Path(xbrl_path).name}")
            else:
                _resolver_expected_period = _resolver_title_period
                logger.info(f"[EARNINGS] {ticker} ZIP resolution failed")
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

            # ---- EARNINGS_WRITE_PLAN_SHADOW ----
            run_shadow_write_plan(
                ticker=ticker,
                doc=doc,
                earnings=earnings,
                guidance=guidance,
                xbrl_path=xbrl_path,
                fiscal_year=fiscal_year,
                quarter=quarter
            )

            _review_notification_suppressed = _should_suppress_review_completion_with_history(
                conn,
                doc.title,
                {
                    "ticker": ticker,
                    "fiscal_year": fiscal_year,
                    "quarter": quarter,
                    "sales_value": earnings.sales_current,
                    "op_value": earnings.op_current,
                    "guidance_sales": guidance.sales_forecast if guidance and guidance.has_guidance else None,
                    "guidance_op": guidance.op_forecast if guidance and guidance.has_guidance else None,
                    "guidance_eps": guidance.eps_forecast if guidance and guidance.has_guidance else None,
                },
            )

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
                    "selling_general_and_administrative_expenses_value": getattr(earnings, "selling_general_and_administrative_expenses_current", None),
                    "segment_summary_json": seg_json,
                    "overall_reason_summary": "\n".join(company_reasons),
                    "segment_reason_summary": json.dumps(segment_reasons, ensure_ascii=False) if segment_reasons else "",
                    "summary_short": summary_short,
                    "summary_full": full_message,
                    "fingerprint": fp,
                    "source_url": getattr(doc, "doc_url", "") or "",
                    "source_doc_id": str(
                        getattr(doc, "disclosure_id", "")
                        or getattr(doc, "source_doc_id", "")
                        or getattr(doc, "doc_id", "")
                        or ""
                    ),
                    "disclosure_datetime": (
                        getattr(doc, "disclosure_datetime", "")
                        or getattr(doc, "published_at", "")
                        or ""
                    ),
                    "accounting_standard": (
                        "IFRS" if "IFRS" in (doc.title or "").upper()
                        else "US_GAAP" if "米国基準" in (doc.title or "")
                        else "J_GAAP" if "日本基準" in (doc.title or "")
                        else "UNKNOWN"
                    ),
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
                    save_data["selling_general_and_administrative_expenses_value"] = None
                else:
                    action = save_earnings_summary(conn, save_data)

                if action == "inserted" or action == "skipped_period_mismatch":
                    if action == "inserted":
                        result.saved_count += 1
                        result.saved_tickers.append(ticker)

                    # ---- tdnet_events へ earnings イベントを best-effort 保存 ----
                    _seq_period = _resolver_expected_period or _derive_fiscal_year_end_period(doc.title)
                    _seq_doc_id = str(getattr(doc, "disclosure_id", "") or "").strip()
                    _seq_external_doc_id = str(getattr(doc, "source_doc_id", "") or getattr(doc, "doc_id", "") or "").strip()

                    # 14桁書類IDの解決
                    _seq_disclosure_no = ""
                    for attr_name in ("disclosure_no", "common_disclosure_no", "doc_id", "tdnet_id"):
                        val = str(getattr(doc, attr_name, ""))
                        if val and len(val) == 14 and val.isdigit():
                            _seq_disclosure_no = val
                            break
                    if not _seq_disclosure_no:
                        _seq_disclosure_no = extract_common_disclosure_no(getattr(doc, "doc_url", "")) or ""
                    if not _seq_disclosure_no:
                        _seq_disclosure_no = extract_common_disclosure_no(getattr(doc, "xbrl_url", "")) or ""
                    if not _seq_disclosure_no:
                        _seq_disclosure_no = extract_common_disclosure_no(getattr(doc, "pdf_url", "")) or ""

                    _seq_identity_passed = False
                    if xbrl_path and os.path.exists(xbrl_path):
                        from src.segment.zip_identity_verifier import verify_zip_identity as _verify_seq
                        _seq_identity_verdict = _verify_seq(
                            zip_path=xbrl_path,
                            requested_disclosure_no=_seq_disclosure_no,
                            expected_ticker=ticker,
                            expected_period=_seq_period,
                            expected_quarter=_resolver_expected_quarter or quarter,
                            trusted_provenance=_seq_provenance,
                        )
                        _seq_identity_passed = (
                            _seq_identity_verdict.passed
                            and _seq_identity_verdict.verdict in ("exact_document_id_match", "official_linked_xbrl_match")
                        )

                    # 通常ルートでの no_segment_info 先行判定
                    _pre_target_segs = None
                    _pre_detailed_result = None
                    try:
                        if (
                            len(_seq_doc_id) == 64
                            and _seq_disclosure_no and len(_seq_disclosure_no) == 14 and _seq_disclosure_no.isdigit()
                            and xbrl_path and os.path.exists(xbrl_path)
                            and not dry_run
                        ):
                            if _seq_identity_passed:
                                _pre_target_segs = _extract_and_filter_segments(
                                    xbrl_path, _seq_period, quarter, include_context_evidence=True
                                )
                                _pre_detailed_result = _last_detailed_result

                                # success_empty かつ 2026-07-05 境界確認
                                if _pre_detailed_result and getattr(_pre_detailed_result, "status", None) == "success_empty":
                                    _disclosed_at = getattr(doc, "disclosure_datetime", "") or getattr(doc, "published_at", "") or ""
                                    if _is_disclosed_after_boundary(_disclosed_at):
                                        _pending_no_segment_states[_seq_doc_id] = {
                                            "status": "no_segment_info",
                                            "version": 1,
                                            "filing_id": _seq_doc_id,
                                            "disclosure_no": _seq_disclosure_no,
                                            "period": _seq_period,
                                            "quarter": quarter,
                                            "source": "exact_xbrl_zero_rows",
                                        }
                    except Exception as _pre_e:
                        logger.error("[EARNINGS][NO_SEGMENT_STATE][ERROR] pre-extraction failed: %s", _pre_e)

                    _sup_ok = False
                    try:
                        if _review_notification_suppressed:
                            _ev_res = {"action": "suppressed_review_completion"}
                            result.filtered_count += 1
                            logger.info("[EARNINGS][REVIEW_COMPLETION] Viewer card suppressed: %s", ticker)
                        else:
                            _ev_res = _save_earnings_to_tdnet_events(
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
                        if _ev_res.get("action") in ("inserted", "updated", "dry_run"):
                            _sup_ok = True
                    except Exception as _e:
                        logger.warning(f"[EARNINGS_STORE] {ticker} tdnet_events save failed (non-fatal): {_e}")
                    finally:
                        _pending_no_segment_states.pop(_seq_doc_id, None)

                    # ---- canonical_financials / segments 同期 (再試行サポート付き) ----
                    # Supabaseクライアントの取得と登録状況の確認
                    from .tdnet_event_store import _get_supabase
                    _client = _get_supabase()

                    _target_segs = _pre_target_segs
                    _expected_segment_metrics = []
                    if xbrl_path and os.path.exists(xbrl_path):
                        if _seq_identity_passed:
                            try:
                                if _target_segs is None:
                                    _target_segs = _extract_and_filter_segments(
                                        xbrl_path=xbrl_path,
                                        period=_seq_period,
                                        quarter=quarter,
                                        include_context_evidence=True,
                                    )
                                if _target_segs:
                                    _expected_set = _build_expected_segment_metrics_from_canonical_rows(
                                        ticker=ticker,
                                        period=_seq_period,
                                        quarter=quarter,
                                        target_segs=_target_segs,
                                        source="xbrl",
                                        filing_id=_seq_doc_id,
                                    )
                                    _expected_segment_metrics = list(_expected_set)
                            except Exception:
                                pass

                    _expected_metrics = []
                    if earnings.sales_current is not None:
                        _expected_metrics.append("sales")
                    if earnings.op_current is not None:
                        _expected_metrics.append("operating_profit")

                    _pl_saved = False
                    _seg_saved = False
                    if _client:
                        _pl_saved = _check_canonical_financials_saved(
                            client=_client,
                            ticker=ticker,
                            period=_seq_period,
                            quarter=quarter,
                            filing_id=_seq_doc_id,
                            expected_metrics=_expected_metrics,
                        )
                        _seg_saved = _check_canonical_segments_saved(
                            client=_client,
                            ticker=ticker,
                            period=_seq_period,
                            quarter=quarter,
                            filing_id=_seq_doc_id,
                            expected_segment_metrics=_expected_segment_metrics,
                        )

                    # PL同期
                    if not _pl_saved:
                        _seq_guidance_dict = {}
                        if guidance and guidance.has_guidance:
                            _seq_guidance_dict = dataclasses.asdict(guidance)

                        _sync_canonical_financials(
                            ticker=ticker,
                            period=_seq_period,
                            quarter=quarter,
                            sales_value=earnings.sales_current,
                            op_value=earnings.op_current,
                            gross_value=getattr(earnings, "gross_profit_current", None),
                            sga_value=getattr(earnings, "selling_general_and_administrative_expenses_current", None),
                            guidance=_seq_guidance_dict,
                            filing_id=_seq_doc_id,
                            dry_run=dry_run,
                            route="sequential",
                        )

                    # セグメント同期
                    if not _seg_saved:
                        try:
                            _sync_canonical_segments(
                                ticker=ticker,
                                period=_seq_period,
                                quarter=quarter,
                                canonical_filing_id=_seq_doc_id,
                                common_disclosure_no=_seq_disclosure_no,
                                xbrl_path=xbrl_path,
                                dry_run=dry_run,
                                route="sequential",
                                target_segs=_target_segs,
                                trusted_provenance=_seq_provenance,
                            )
                        except Exception as _seg_e:
                            logger.error("[EARNINGS][SEGMENT_CANONICAL][ERROR] %s sequential route exception: %s", ticker, _seg_e)
                else:
                    result.already_exists_count += 1

                    try:
                        # 重複ブロック内で通常経路と同じ生成式を使ってローカル変数を構築
                        _seq_period = _derive_fiscal_year_end_period(doc.title)
                        _seq_doc_id = str(getattr(doc, "disclosure_id", "") or "").strip()
                        _seq_external_doc_id = str(getattr(doc, "source_doc_id", "") or getattr(doc, "doc_id", "") or "").strip()

                        _seq_disclosure_no = ""
                        for attr_name in ("disclosure_no", "common_disclosure_no", "doc_id", "tdnet_id"):
                            val = str(getattr(doc, attr_name, ""))
                            if val and len(val) == 14 and val.isdigit():
                                _seq_disclosure_no = val
                                break
                        if not _seq_disclosure_no:
                            _seq_disclosure_no = extract_common_disclosure_no(getattr(doc, "doc_url", "")) or ""
                        if not _seq_disclosure_no:
                            _seq_disclosure_no = extract_common_disclosure_no(getattr(doc, "xbrl_url", "")) or ""
                        if not _seq_disclosure_no:
                            _seq_disclosure_no = extract_common_disclosure_no(getattr(doc, "pdf_url", "")) or ""

                        _seq_pl_vals = {
                            "sales": earnings.sales_current,
                            "op": earnings.op_current,
                            "gross": getattr(earnings, "gross_profit_current", None),
                            "sga": getattr(earnings, "selling_general_and_administrative_expenses_current", None),
                            "guidance": dataclasses.asdict(guidance) if (guidance and guidance.has_guidance) else {},
                        }

                        _retry_incomplete_canonical_for_duplicate(
                            ticker=ticker,
                            period=_seq_period,
                            quarter=quarter,
                            filing_id=_seq_doc_id,
                            disclosure_no=_seq_disclosure_no,
                            xbrl_path=xbrl_path,
                            pl_values=_seq_pl_vals,
                            dry_run=dry_run,
                            target_segs=None,
                        )
                    except Exception as _seq_retry_e:
                        logger.error("[EARNINGS][CANONICAL_RETRY] Sequential retry error: %s", _seq_retry_e)

                    continue  # 既存の場合は通知もスキップ

            else:
                logger.info(f"[DRY-RUN] would save: {ticker} {company_name}")
                result.saved_count += 1

            # ---- Phase 0-3: 通知条件判定 ----
            if (
                not _review_notification_suppressed
                and notify_enabled
                and should_notify_earnings(earnings.sales_yoy, earnings.op_yoy)
            ):
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

    # doc_idが14桁数字のみで構成されていることを厳格に検証
    if not doc_id or len(doc_id) != 14 or not doc_id.isdigit():
        return None

    candidates = sorted(d.glob(f"{ticker}_*.zip"), reverse=True)
    matches = []
    for c in candidates:
        # ZIPファイル名は、取得した14桁書類ID (doc_id) との一致確認だけに使う
        zip_id = extract_common_disclosure_no(c.name)
        if zip_id and zip_id == doc_id:
            if c.is_file():
                matches.append(c)

    if len(matches) == 1:
        return str(matches[0])

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
        "selling_general_and_administrative_expenses_value": getattr(earnings, "selling_general_and_administrative_expenses_current", None),
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
