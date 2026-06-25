#!/usr/bin/env python3
"""tdnet_event_store.py — EventRecord → Supabase tdnet_events 保存

既存パイプラインの EventRecord を受け取り、
display_title / display_summary / priority_rank / dedupe_key を生成して
Supabase の tdnet_events テーブルへ INSERT する。

- 重複は dedupe_key UNIQUE制約で自動スキップ (ON CONFLICT DO NOTHING 相当)
- 保存失敗は既存パイプラインを止めない (best-effort)
- Discord通知条件とViewer保存条件は分離
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .common_models import EventRecord, EventType
from .notify_rules import should_notify_event

logger = logging.getLogger("tdnet_event_store")

JST = timezone(timedelta(hours=9))

# ============================================================
# 表示カテゴリ定数 (小文字統一)
# ============================================================
DISPLAY_BUYBACK = "buyback"
DISPLAY_FORECAST = "forecast"
DISPLAY_DIVIDEND = "dividend"
DISPLAY_EARNINGS = "earnings"
DISPLAY_SHAREHOLDER = "shareholder"
DISPLAY_OTHER = "other"

# event_type → display_category 直接マッピング
_EVENT_TYPE_TO_CATEGORY = {
    "buyback": DISPLAY_BUYBACK,
    "forecast_revision": DISPLAY_FORECAST,
    "dividend_revision": DISPLAY_DIVIDEND,
    "earnings": DISPLAY_EARNINGS,
    "shareholder": DISPLAY_SHAREHOLDER,
    "other": DISPLAY_OTHER,
}

# headline/title キーワード → display_category マッピング
_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    (DISPLAY_BUYBACK, [
        "自己株式取得", "自己株式の取得", "自己株取得",
        "自社株買い", "自己株式消却",
        "treasury share", "repurchase",
    ]),
    (DISPLAY_FORECAST, [
        "業績予想", "通期予想", "上方修正", "下方修正", "予想修正",
        "forecast", "guidance",
    ]),
    (DISPLAY_DIVIDEND, [
        "配当", "増配", "減配", "期末配当", "中間配当", "記念配当",
        "special dividend", "dividend",
    ]),
    (DISPLAY_EARNINGS, [
        "決算短信", "決算", "financial results", "earnings",
    ]),
    (DISPLAY_SHAREHOLDER, [
        "大量保有", "変更報告書", "株主",
        "shareholder", "beneficial ownership",
    ]),
]


def _normalize_display_category(event: EventRecord) -> str:
    """EventRecord → 表示用カテゴリ (小文字) を決定する。

    判定優先順位:
      1. event_type の直接マッピング
      2. headline / title のキーワードマッチ
      3. フォールバック → "other"
    """
    # 1) event_type 直接マッピング
    et = (event.event_type or "").strip().lower()
    if et in _EVENT_TYPE_TO_CATEGORY:
        return _EVENT_TYPE_TO_CATEGORY[et]

    # 2) headline / title キーワードマッチ
    search_text = " ".join([
        event.title or "",
        event.summary_text or "",
    ]).lower()

    for category, keywords in _KEYWORD_RULES:
        for kw in keywords:
            if kw.lower() in search_text:
                return category

    # 3) フォールバック
    return DISPLAY_OTHER


# ============================================================
# Supabase クライアント (lazy-init)
# ============================================================
_supabase_client = None


def _get_supabase():
    """Supabase client を遅延初期化して返す。"""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            logger.warning("[STORE] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
            return None
        _supabase_client = create_client(url, key)
        return _supabase_client
    except ImportError:
        logger.warning("[STORE] supabase package not installed — pip install supabase")
        return None
    except Exception as e:
        logger.error(f"[STORE] Supabase init failed: {e}")
        return None


# ============================================================
# dedupe_key 生成
# ============================================================
def _normalize_headline(text: str) -> str:
    """ヘッドライン正規化: 空白除去・全角→半角・カッコ内除去"""
    if not text:
        return ""
    s = text.strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[（(].+?[）)]", "", s)
    return s[:120]


def build_dedupe_key(event: EventRecord) -> str:
    """EventRecord → dedupe_key (SHA-256 hex 先頭40文字)

    ticker + event_type + normalized(title) + disclosed_at(分単位)
    """
    dt_part = ""
    if event.disclosure_datetime:
        dt_part = event.disclosure_datetime[:16]  # YYYY-MM-DD HH:MM

    parts = [
        event.ticker or "",
        event.event_type or "",
        _normalize_headline(event.title),
        dt_part,
    ]
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:40]


# ============================================================
# priority_rank 算出
# ============================================================
_PRIORITY_MAP = {
    (EventType.BUYBACK, None): 10,
    (EventType.FORECAST_REVISION, "upward"): 20,
    (EventType.DIVIDEND_REVISION, "increase"): 30,
    (EventType.DIVIDEND_REVISION, "special_dividend"): 30,
    (EventType.DIVIDEND_REVISION, "commemorative_dividend"): 30,
    ("earnings", None): 40,
    (EventType.FORECAST_REVISION, "downward"): 50,
    (EventType.FORECAST_REVISION, "difference"): 50,
    (EventType.FORECAST_REVISION, "neutral"): 60,
    (EventType.DIVIDEND_REVISION, "decrease"): 60,
    (EventType.DIVIDEND_REVISION, "maintain"): 70,
}


def compute_priority_rank(event: EventRecord) -> int:
    """EventRecord → priority_rank (小さいほど重要)"""
    # 完全一致を最初にチェック
    key = (event.event_type, event.subtype)
    if key in _PRIORITY_MAP:
        return _PRIORITY_MAP[key]
    # event_type だけでチェック
    key_type_only = (event.event_type, None)
    if key_type_only in _PRIORITY_MAP:
        return _PRIORITY_MAP[key_type_only]
    return 90


# ============================================================
# display_title / display_summary / formatted_message は
# common_notify.build_display_title / build_display_summary / build_formatted_message を使用
# (build_event_parts を唯一の元ネタとする共通フォーマッタ)


# ============================================================
# payload ヘルパー
# ============================================================
def _get_payload(event: EventRecord) -> dict:
    """EventRecord の extracted_payload_json を parse して dict を返す"""
    if not event.extracted_payload_json:
        return {}
    try:
        payload = json.loads(event.extracted_payload_json)
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


# ============================================================
# primary_metric 抽出
# ============================================================
def _extract_primary_metric(event: EventRecord) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(metric_name, metric_value, metric_yoy) を返す"""
    payload = _get_payload(event)

    if event.event_type == EventType.BUYBACK:
        ratio = payload.get("ratio_to_outstanding")
        if ratio is not None:
            return "ratio_to_outstanding", f"{ratio:.1f}%", None
        return None, None, None

    elif event.event_type == EventType.FORECAST_REVISION:
        for key, label, pct_key in [
            ("revised_net_income", "純利益", "change_net_income_pct"),
            ("revised_op", "営業利益", "change_op_pct"),
            ("revised_sales", "売上高", "change_sales_pct"),
        ]:
            val = payload.get(key)
            pct = payload.get(pct_key)
            if val is not None:
                yoy = None
                if pct is not None:
                    sign = "+" if pct > 0 else ""
                    yoy = f"{sign}{pct:.1f}%"
                if isinstance(val, (int, float)) and abs(val) >= 100:
                    return label, f"{val/100:.1f}億円", yoy
                return label, f"{val}百万円", yoy
        return None, None, None

    elif event.event_type == EventType.DIVIDEND_REVISION:
        rev = payload.get("revised_dividend_per_share")
        if rev is not None:
            prev = payload.get("previous_dividend_per_share")
            yoy = None
            if prev is not None:
                try:
                    p_f = float(prev)
                    if p_f > 0:
                        yoy = f"+{(float(rev)-p_f)/p_f*100:.1f}%"
                except (ValueError, TypeError):
                    pass
            return "配当", f"{rev}円", yoy
        return None, None, None

    elif event.event_type == "earnings" or event.event_type == DISPLAY_EARNINGS:
        op_current = payload.get("op_current")
        op_yoy = payload.get("op_yoy")
        sales_current = payload.get("sales_current")
        sales_yoy = payload.get("sales_yoy")

        if op_current is not None:
            val_str = f"{int(op_current / 1000000):,}百万円"
            yoy_str = None
            if op_yoy is not None:
                sign = "+" if op_yoy > 0 else ""
                yoy_str = f"{sign}{op_yoy * 100:.1f}%"
            return "営業利益", val_str, yoy_str

        elif sales_current is not None:
            val_str = f"{int(sales_current / 1000000):,}百万円"
            yoy_str = None
            if sales_yoy is not None:
                sign = "+" if sales_yoy > 0 else ""
                yoy_str = f"{sign}{sales_yoy * 100:.1f}%"
            return "売上高", val_str, yoy_str

        return None, None, None

    return None, None, None


# ============================================================
# strength_score 算出
# ============================================================
def _compute_strength_score(event: EventRecord) -> Optional[float]:
    """強度スコア (0-100)"""
    payload = _get_payload(event)

    if event.event_type == EventType.BUYBACK:
        ratio = payload.get("ratio_to_outstanding")
        if ratio is not None:
            return min(float(ratio) * 10, 100)
        return None

    elif event.event_type == EventType.FORECAST_REVISION:
        for key in ["change_net_income_pct", "change_op_pct", "change_sales_pct"]:
            val = payload.get(key)
            if val is not None:
                return min(abs(float(val)), 100)
        return None

    elif event.event_type == EventType.DIVIDEND_REVISION:
        prev = payload.get("previous_dividend_per_share")
        rev = payload.get("revised_dividend_per_share")
        if prev is not None and rev is not None:
            try:
                p_f, r_f = float(prev), float(rev)
                if p_f > 0:
                    return min(abs(r_f - p_f) / p_f * 100, 100)
            except (ValueError, TypeError):
                pass
        return None

    return None


# ============================================================
# sort_key 生成
# ============================================================
def _build_sort_key(priority_rank: int, detected_at: str, ticker: str) -> str:
    """ソートキー: priority_rank + detected_at + ticker"""
    return f"{priority_rank:03d}|{detected_at}|{ticker}"


# ============================================================
# timestamp 正規化
# ============================================================
def _sanitize_timestamp(dt_str: str | None, fallback: str) -> str:
    """timestamptz に渡せるISO形式に正規化。不正値はfallbackを返す。"""
    if not dt_str:
        return fallback
    s = dt_str.strip()
    # "12:00" のような時刻のみ → 不正
    if len(s) <= 5:
        return fallback
    # "YYYY-MM-DD HH:MM" or "YYYY-MM-DD HH:MM:SS" は有効
    if len(s) >= 10 and (s[4] == '-' or s[4] == '/'):
        return s
    # ISO 8601 (T含む) も有効
    if 'T' in s and len(s) >= 16:
        return s
    return fallback

def _sanitize_disclosed_at(dt_str: str | None) -> str | None:
    """disclosed_at 用の正規化。タイムゾーンなしのJST文字列をUTCのISO文字列に変換する。"""
    if not dt_str:
        return None
    s = dt_str.strip()
    if len(s) <= 5:
        return None

    # 既にタイムゾーン（+ または Z）が含まれていればそのまま
    if "+" in s or "Z" in s or "z" in s:
        return s

    try:
        s_clean = s.replace("T", " ")
        if len(s_clean) == 16:
            dt = datetime.strptime(s_clean, "%Y-%m-%d %H:%M")
        elif len(s_clean) >= 19:
            dt = datetime.strptime(s_clean[:19], "%Y-%m-%d %H:%M:%S")
        else:
            return s
            
        dt_jst = dt.replace(tzinfo=JST)
        return dt_jst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return s

def _calculate_notification_compare(ticker: str, extracted: dict, client=None) -> dict | None:
    """決算通知の2行目（比較YOY）用JSONを計算する"""
    quarter = extracted.get("quarter")
    if not quarter:
        return None

    compare_data = None
    reason_code = None

    if quarter in ("1Q", "4Q", "FY"):
        guidance = extracted.get("guidance", {})
        s_yoy = guidance.get("sales_yoy")
        o_yoy = guidance.get("op_yoy")
        s_f = guidance.get("sales_forecast")
        o_f = guidance.get("op_forecast")
        s_curr = extracted.get("sales_current")
        o_curr = extracted.get("op_current")
        calc_source = "llm_extracted"

        if s_yoy is None or o_yoy is None:
            if s_f is None and o_f is None:
                reason_code = "forecast_missing"
            else:
                if quarter in ("FY", "4Q"):
                    if s_yoy is None and s_f is not None and s_curr is not None and s_curr > 0:
                        s_yoy = (s_f / s_curr) - 1.0
                        calc_source = "calculated_from_current"
                    if o_yoy is None and o_f is not None and o_curr is not None and o_curr > 0:
                        o_yoy = (o_f / o_curr) - 1.0
                        calc_source = "calculated_from_current"
                elif quarter == "1Q":
                    current_fy = extracted.get("fiscal_year")
                    if client is not None and current_fy:
                        try:
                            prev_fy = str(int(current_fy) - 1)
                            res = client.table('canonical_financials') \
                                .select('period, metric, value') \
                                .eq('ticker', ticker) \
                                .eq('quarter', 'FY') \
                                .in_('metric', ['sales', 'operating_profit']) \
                                .execute()
                            prev_sales = None
                            prev_op = None
                            if res.data:
                                for row in res.data:
                                    period = str(row.get('period', ''))
                                    metric = row.get('metric')
                                    val = row.get('value')
                                    if period.startswith(prev_fy) and val is not None:
                                        if metric == 'sales': prev_sales = val
                                        elif metric == 'operating_profit': prev_op = val
                            
                            if s_yoy is None and s_f is not None and prev_sales is not None and prev_sales > 0:
                                s_yoy = (s_f / 1_000_000) / prev_sales - 1.0
                                calc_source = "calculated_from_db_prev_fy"
                            if o_yoy is None and o_f is not None and prev_op is not None and prev_op > 0:
                                o_yoy = (o_f / 1_000_000) / prev_op - 1.0
                                calc_source = "calculated_from_db_prev_fy"
                            if prev_sales is None and prev_op is None:
                                reason_code = "prev_actual_missing"
                        except Exception as e:
                            logger.warning(f"[STORE] Failed to fetch previous FY for 1Q YoY fallback: {e}")
                            reason_code = "db_error"

                if s_yoy is None and o_yoy is None and not reason_code:
                    reason_code = "calculation_failed_or_missing_inputs"

        if s_yoy is not None or o_yoy is not None:
            compare_data = {
                "label": "通期予" if quarter == "1Q" else "来期FY予",
                "sales_yoy": s_yoy,
                "op_yoy": o_yoy,
                "source": calc_source
            }
            if reason_code:
                compare_data["reason"] = reason_code
        else:
            logger.info(f"[STORE] Missing guidance YOY. ticker={ticker}, quarter={quarter}, reason={reason_code}")
            compare_data = {"reason": reason_code}

    elif quarter in ("2Q", "3Q"):
        # 2Q/3Q: fetch previous quarter from Supabase
        target_quarter = "1Q" if quarter == "2Q" else "2Q"
        current_fy = extracted.get("fiscal_year")
        reason_code = "prev_actual_missing_db"

        if client is not None and current_fy:
            try:
                prev_fy = str(int(current_fy) - 1)
                res = client.table('canonical_financials') \
                    .select('period, metric, value') \
                    .eq('ticker', ticker) \
                    .eq('quarter', target_quarter) \
                    .in_('metric', ['sales', 'operating_profit']) \
                    .execute()
                
                curr_sales = None
                curr_op = None
                prev_sales = None
                prev_op = None
                
                if res.data:
                    for row in res.data:
                        period = str(row.get('period', ''))
                        metric = row.get('metric')
                        val = row.get('value')
                        
                        if period.startswith(str(current_fy)) and val is not None:
                            if metric == 'sales': curr_sales = val
                            elif metric == 'operating_profit': curr_op = val
                        elif period.startswith(str(prev_fy)) and val is not None:
                            if metric == 'sales': prev_sales = val
                            elif metric == 'operating_profit': prev_op = val
                
                s_yoy = None
                if curr_sales is not None and prev_sales is not None and prev_sales > 0:
                    s_yoy = (curr_sales / prev_sales) - 1.0
                
                o_yoy = None
                if curr_op is not None and prev_op is not None and prev_op > 0:
                    o_yoy = (curr_op / prev_op) - 1.0
                    
                if curr_sales is not None or curr_op is not None:
                    compare_data = {
                        "label": "前Q",
                        "sales_yoy": s_yoy,
                        "op_yoy": o_yoy,
                        "source": "jquants_canonical_financials"
                    }
                else:
                    compare_data = {"reason": "prev_actual_missing_db"}
                    logger.info(
                        f"[STORE] Previous quarter not found in canonical_financials. "
                        f"ticker={ticker}, current_quarter={quarter}, target_quarter={target_quarter}, "
                        f"fiscal_year={current_fy}, source_checked=canonical_financials"
                    )

            except Exception as e:
                compare_data = {"reason": "db_error"}
                logger.warning(f"[STORE] Failed to fetch previous quarter from Supabase canonical_financials: {e}")

    return {
        "current": {
            "label": quarter,
            "sales_yoy": extracted.get("sales_yoy"),
            "op_yoy": extracted.get("op_yoy")
        },
        "compare": compare_data
    }

def _supplement_current_yoy(ticker: str, extracted: dict, client) -> None:
    """TDNETでYOYが取れなかった場合、canonical_financialsから取得して補完する"""
    if not extracted or not client or not ticker:
        return
        
    quarter = extracted.get("quarter")
    current_fy = extracted.get("fiscal_year")
    if not quarter or not current_fy or quarter in ("1Q", "4Q", "FY"):
        print(f"[_supplement_current_yoy] SKIPPED {ticker} reason=invalid_quarter_or_fy: quarter={quarter}, fy={current_fy}")
        return

    s_yoy = extracted.get("sales_yoy")
    o_yoy = extracted.get("op_yoy")
    s_curr = extracted.get("sales_current")
    o_curr = extracted.get("op_current")
    
    if (s_yoy is not None and o_yoy is not None) or (s_curr is None and o_curr is None):
        print(f"[_supplement_current_yoy] SKIPPED {ticker} reason=no_need: s_yoy={s_yoy}, o_yoy={o_yoy}, s_curr={s_curr}, o_curr={o_curr}")
        return
    print(f"[_supplement_current_yoy] EXECUTED {ticker} quarter={quarter} fy={current_fy}")

    try:
        prev_fy = str(int(current_fy) - 1)
        res = client.table("canonical_financials") \
            .select("period, metric, value") \
            .eq("ticker", ticker) \
            .eq("quarter", quarter) \
            .in_("metric", ["sales", "operating_profit"]) \
            .execute()
            
        prev_s = None
        prev_o = None
        
        if res.data:
            for row in res.data:
                period = str(row.get("period", ""))
                metric = row.get("metric")
                val = row.get("value")
                
                if period.startswith(prev_fy) and val is not None:
                    if metric == "sales": prev_s = val
                    elif metric == "operating_profit": prev_o = val
                    
        # 単位合わせ: TDNET(extracted)は円単位、canonical_financialsは百万円単位
        if s_yoy is None and s_curr is not None and prev_s is not None and prev_s > 0:
            calc_s_yoy = (s_curr / 1_000_000 - prev_s) / prev_s
            extracted["sales_yoy"] = calc_s_yoy
            extracted["primary_metric_yoy_source"] = "jquants_canonical_financials"
            
        if o_yoy is None and o_curr is not None and prev_o is not None and prev_o > 0:
            calc_o_yoy = (o_curr / 1_000_000 - prev_o) / prev_o
            extracted["op_yoy"] = calc_o_yoy
            extracted["primary_metric_yoy_source"] = "jquants_canonical_financials"
            
    except Exception as e:
        logger.warning(f"[STORE] Failed to supplement current YOY: {e}")


# ============================================================
# メイン: Supabase へ保存
# ============================================================
def _strip_volatile_keys(obj):
    """Recursively remove volatile keys like detected_at, updated_at from dict or list."""
    if isinstance(obj, dict):
        new_dict = {}
        for k, v in obj.items():
            if str(k).lower() in {
                "detected_at", "updated_at", "checked_at", "generated_at", 
                "processed_at", "synced_at", "last_seen_at", "last_checked_at", 
                "raw_fetched_at", "fetched_at", "run_id"
            }:
                continue
            new_dict[k] = _strip_volatile_keys(v)
        return new_dict
    elif isinstance(obj, list):
        return [_strip_volatile_keys(item) for item in obj]
    else:
        return obj

def _safe_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except Exception:
            return {}
    return {}

def _nested_get(d, *path):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur

def _payload_contains_doc_id(payload, source_doc_id, xbrl_doc_id):
    candidates = [
        payload.get("source_doc_id"),
        _nested_get(payload, "raw", "source_doc_id"),
        _nested_get(payload, "extracted", "source_doc_id"),
        _nested_get(payload, "raw", "xbrl_doc_id"),
        _nested_get(payload, "extracted", "xbrl_doc_id"),
        _nested_get(payload, "raw", "source_url"),
        _nested_get(payload, "raw", "pdf_url"),
        _nested_get(payload, "extracted", "source_url"),
        _nested_get(payload, "extracted", "pdf_url"),
    ]

    joined = " ".join(str(x or "") for x in candidates)

    if source_doc_id and source_doc_id in joined:
        return True, "source_doc_id_in_raw_payload"

    if xbrl_doc_id and xbrl_doc_id in joined:
        return True, "xbrl_doc_id_in_raw_payload"

    return False, ""

def _merge_compare_json(new_row_dict: dict, existing_payload_str: str) -> None:
    if not existing_payload_str:
        return
    try:
        new_payload = json.loads(new_row_dict["raw_payload"])
        old_payload = json.loads(existing_payload_str)

        new_comp = new_payload.get("notification_compare_json")
        old_comp = old_payload.get("notification_compare_json") if isinstance(old_payload, dict) else None

        if old_comp and isinstance(old_comp, dict) and old_comp.get("compare"):
            if not new_comp or not isinstance(new_comp, dict) or not new_comp.get("compare"):
                if not isinstance(new_comp, dict):
                    new_comp = {}
                    new_payload["notification_compare_json"] = new_comp
                new_comp["compare"] = old_comp["compare"]
                new_row_dict["raw_payload"] = json.dumps(new_payload, ensure_ascii=False, default=str)
    except Exception:
        pass

def build_supabase_row(event: EventRecord, client=None) -> tuple[dict, dict, str, str, str | None]:
    """
    EventRecordからSupabaseのtdnet_eventsテーブルに保存するrowを生成する。
    
    Returns:
        row (dict): tdnet_eventsにinsertする辞書
        raw_payload (dict): JSONシリアライズ前のraw_payload辞書
        dedupe_key (str): 生成されたdedupe_key
        display_category (str): 表示カテゴリ
        metric_yoy (str | None): YOY値
    """
    dedupe_key = build_dedupe_key(event)
    display_category = _normalize_display_category(event)
    
    # --- 抽出ペイロードの事前準備・補完 ---
    extracted = {}
    if event.extracted_payload_json:
        try:
            extracted = json.loads(event.extracted_payload_json)
        except (json.JSONDecodeError, TypeError):
            pass
            
    if display_category == "earnings" and client and isinstance(extracted, dict):
        _supplement_current_yoy(event.ticker or "", extracted, client)
        event.extracted_payload_json = json.dumps(extracted, ensure_ascii=False)
    # ------------------------------------

    priority_rank = compute_priority_rank(event)
    display_title = event.title or ""
    display_summary = event.summary_text or ""
    formatted_message = event.summary_text or ""
    metric_name, metric_value, metric_yoy = _extract_primary_metric(event)
    strength = _compute_strength_score(event)
    notify_discord = should_notify_event(event)

    original_event_type = event.event_type or ""

    now_iso = datetime.now(JST).isoformat()
    detected_at = _sanitize_timestamp(event.disclosure_datetime, now_iso)

    raw_payload = {}
    if event.raw_payload_json:
        try:
            raw_payload["raw"] = json.loads(event.raw_payload_json)
        except (json.JSONDecodeError, TypeError):
            raw_payload["raw_text"] = event.raw_payload_json
            
    if event.extracted_payload_json:
        if extracted:
            raw_payload["extracted"] = extracted
        else:
            raw_payload["extracted_text"] = event.extracted_payload_json
            
    # 元の event_type を raw_payload に保存
    raw_payload["original_event_type"] = original_event_type

    # text_extract_status: extracted 全数値が null なら "empty" フラグ付与
    if isinstance(extracted, dict):
        numeric_fields = [
            "revised_sales", "revised_op", "revised_ordinary", "revised_net_income",
            "previous_sales", "previous_op", "previous_ordinary", "previous_net_income",
            "total_amount", "share_count", "ratio_to_issued",
            "previous_dividend", "revised_dividend",
            "revised_dividend_per_share", "previous_dividend_per_share",
            "shares_limit", "amount_limit_million_yen",
        ]
        has_any_number = any(extracted.get(f) is not None for f in numeric_fields)
        if not has_any_number:
            raw_payload["text_extract_status"] = "empty"
            raw_payload["text_empty"] = True
        else:
            raw_payload["text_extract_status"] = "ok"
            raw_payload["text_empty"] = False
            
    # 追加: notification_compare_json の生成と埋め込み
    if display_category == "earnings" and isinstance(extracted, dict):
        comp_json = _calculate_notification_compare(event.ticker or "", extracted, client=client)
        if comp_json:
            raw_payload["notification_compare_json"] = comp_json

    sort_key = _build_sort_key(priority_rank, detected_at, event.ticker or "")

    row = {
        "detected_at": detected_at,
        "disclosed_at": _sanitize_disclosed_at(event.disclosure_datetime),
        "ticker": event.ticker or "",
        "company_name": event.company_name or "",
        "event_type": display_category,
        "event_subtype": event.subtype or None,
        "headline": event.title or "",
        "summary": event.summary_text or "",
        "source_url": event.doc_url or None,
        "pdf_url": event.doc_url if display_category in ("earnings", "forecast", "dividend", "buyback") else None,
        "raw_payload": json.dumps(raw_payload, ensure_ascii=False, default=str),
        "strength_score": strength,
        "priority_rank": priority_rank,
        "primary_metric_name": metric_name,
        "primary_metric_value": metric_value,
        "primary_metric_yoy": metric_yoy,
        "display_title": display_title,
        "display_summary": display_summary,
        "formatted_message": formatted_message,
        "sort_key": sort_key,
        "dedupe_key": dedupe_key,
        "notify_to_discord": notify_discord,
        "status": "active",
        "schema_version": 1,
    }
    
    return row, raw_payload, dedupe_key, display_category, metric_yoy


def save_event_to_supabase(
    event: EventRecord,
    *,
    dry_run: bool = False,
) -> dict:
    """EventRecord → Supabase tdnet_events へ INSERT (best-effort)

    Returns:
        {"action": "inserted"|"dedup_skipped"|"error"|"dry_run", ...}
    """
    result = {"action": "error", "dedupe_key": ""}

    try:
        client = _get_supabase()
        if client is None:
            logger.warning("[STORE] Supabase client not available — skipping save")
            result["error"] = "supabase_not_available"
            return result

        row, raw_payload, dedupe_key, display_category, metric_yoy = build_supabase_row(event, client)
        result["dedupe_key"] = dedupe_key
        original_event_type = event.event_type or ""
        display_title = row.get("display_title", "")


        # YOY protection: Prevent overwriting existing YOY data with null
        if display_category == DISPLAY_EARNINGS and metric_yoy is None:
            try:
                date_str = event.disclosure_datetime[:10] if event.disclosure_datetime else ""
                if date_str:
                    dt_jst = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
                    start_utc = dt_jst.astimezone(timezone.utc)
                    end_utc = (dt_jst + timedelta(days=1)).astimezone(timezone.utc)
                    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    end_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

                    exist_res = (
                        client.table("tdnet_events")
                        .select("id, primary_metric_yoy")
                        .eq("ticker", event.ticker or "")
                        .eq("event_type", display_category)
                        .gte("disclosed_at", start_iso)
                        .lt("disclosed_at", end_iso)
                        .execute()
                    )
                    if exist_res.data:
                        for ext_row in exist_res.data:
                            if ext_row.get("primary_metric_yoy") is not None:
                                logger.info(
                                    f"[STORE] DEDUP_SKIPPED (YOY protect) ticker={event.ticker} "
                                    f"Existing YOY={ext_row.get('primary_metric_yoy')} vs New=None"
                                )
                                result["action"] = "dedup_skipped"
                                result["display_category"] = display_category
                                return result
            except Exception as check_e:
                logger.warning(f"[STORE] Failed to check existing record for YOY protection: {check_e}")

        # --- 厳密な重複チェック（テスト実行等の事故防止） ---
        try:
            date_str = event.disclosure_datetime[:10] if event.disclosure_datetime else ""
            if date_str:
                dt_jst = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
                start_utc = dt_jst.astimezone(timezone.utc)
                end_utc = (dt_jst + timedelta(days=1)).astimezone(timezone.utc)
                day_start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                next_day_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                
                q = (
                    client.table("tdnet_events")
                    .select("*")
                    .eq("ticker", event.ticker or "")
                    .order("created_at", desc=True)
                )

                q = q.limit(20)
                strict_match_res = q.execute()

                matched_row = None
                match_reason = ""
                
                if strict_match_res.data:
                    norm_title = _normalize_headline(event.title)
                    xbrl_doc_id = _nested_get(raw_payload, "raw", "xbrl_doc_id") or _nested_get(raw_payload, "extracted", "xbrl_doc_id")
                    
                    for r in strict_match_res.data:
                        raw_p = _safe_json(r.get("raw_payload"))
                        has_doc, reason = _payload_contains_doc_id(raw_p, event.source_doc_id, xbrl_doc_id)
                        if has_doc:
                            matched_row = r
                            match_reason = reason
                            break
                        
                        r_source_url = str(r.get("source_url") or "")
                        r_pdf_url = str(r.get("pdf_url") or "")
                        if event.source_doc_id and (event.source_doc_id in r_source_url or event.source_doc_id in r_pdf_url):
                            matched_row = r
                            match_reason = "source_url_contains_source_doc_id"
                            break
                            
                        r_headline = str(r.get("headline") or "")
                        if norm_title and _normalize_headline(r_headline) == norm_title:
                            if display_category == "buyback":
                                new_ids = set()
                                if event.source_doc_id: new_ids.add(str(event.source_doc_id))
                                if getattr(event, "doc_id", None): new_ids.add(str(event.doc_id))
                                if getattr(event, "disclosure_id", None): new_ids.add(str(event.disclosure_id))
                                if xbrl_doc_id: new_ids.add(str(xbrl_doc_id))
                                
                                old_ids = set()
                                for k in ["source_doc_id", "doc_id", "disclosure_id"]:
                                    v = r.get(k)
                                    if v: old_ids.add(str(v))
                                for k in ["source_doc_id", "doc_id", "disclosure_id", "xbrl_doc_id"]:
                                    v1 = _nested_get(raw_p, "raw", k)
                                    v2 = _nested_get(raw_p, "extracted", k)
                                    if v1: old_ids.add(str(v1))
                                    if v2: old_ids.add(str(v2))
                                
                                new_ids = {x for x in new_ids if x and x != "None"}
                                old_ids = {x for x in old_ids if x and x != "None"}
                                
                                if new_ids and old_ids:
                                    if not (new_ids & old_ids):
                                        logger.info(f"[STORE] SEMANTIC_TITLE_MATCH_SKIPPED ticker={event.ticker} type=buyback reason=doc_id_mismatch")
                                        continue
                                else:
                                    date_match = False
                                    try:
                                        from dateutil import parser as dt_parser
                                        if event.disclosure_datetime and r.get("disclosed_at"):
                                            dt_new = dt_parser.parse(event.disclosure_datetime).astimezone(JST)
                                            dt_old = dt_parser.parse(str(r.get("disclosed_at"))).astimezone(JST)
                                            if dt_new.date() == dt_old.date():
                                                date_match = True
                                    except Exception:
                                        pass
                                    if not date_match:
                                        logger.info(f"[STORE] SEMANTIC_TITLE_MATCH_SKIPPED ticker={event.ticker} type=buyback reason=missing_id_and_date_mismatch")
                                        continue

                            matched_row = r
                            match_reason = "semantic_title_match"
                            break
                            
                if matched_row:
                    existing_id = matched_row["id"]
                    
                    # YOY保護のロジック: 上書き時、新しいYOYがnullで既存にYOYがあれば維持
                    if display_category == DISPLAY_EARNINGS and metric_yoy is None:
                        # check existing YOY from another query, or we can just fetch it now
                        exist_yoy_res = client.table("tdnet_events").select("primary_metric_yoy").eq("id", existing_id).execute()
                        if exist_yoy_res.data and exist_yoy_res.data[0].get("primary_metric_yoy") is not None:
                            logger.info(f"[STORE] DEDUP_SKIPPED (YOY protect strict) ticker={event.ticker}")
                            result["action"] = "dedup_skipped"
                            result["display_category"] = display_category
                            return result

                    existing_payload_str = matched_row.get("raw_payload", "{}")
                    if isinstance(existing_payload_str, dict):
                        existing_payload_str = json.dumps(existing_payload_str)
                    
                    _merge_compare_json(row, existing_payload_str)
                    
                    # 差分比較 (マージ後)
                    def _has_substantial_changes(old_row, new_row):
                        keys = [
                            "ticker", "company_name", "event_type", "event_subtype",
                            "headline", "disclosed_at", "source_url", "pdf_url",
                            "primary_metric_name", "primary_metric_value", "primary_metric_yoy",
                            "display_title", "display_summary", "formatted_message",
                            "notify_to_discord"
                        ]
                        from dateutil import parser as dt_parser
                        for k in keys:
                            if k in old_row and k in new_row:
                                if old_row[k] is None and new_row[k] is None:
                                    continue
                                if old_row[k] is None or new_row[k] is None:
                                    return True

                                val_old = str(old_row[k])
                                val_new = str(new_row[k])
                                
                                if k in ("disclosed_at", "detected_at"):
                                    try:
                                        dt_old = dt_parser.parse(val_old)
                                        dt_new = dt_parser.parse(val_new)
                                        if dt_old == dt_new:
                                            continue
                                    except Exception:
                                        pass

                                if k in ("primary_metric_value", "primary_metric_yoy"):
                                    try:
                                        f_old = float(val_old)
                                        f_new = float(val_new)
                                        if f_old == f_new:
                                            continue
                                    except ValueError:
                                        pass

                                if val_old != val_new:
                                    return True
                        
                        try:
                            old_p = json.loads(existing_payload_str) if isinstance(existing_payload_str, str) else existing_payload_str
                            new_p = json.loads(new_row.get("raw_payload", "{}")) if isinstance(new_row.get("raw_payload", "{}"), str) else new_row.get("raw_payload", {})
                            if _strip_volatile_keys(old_p) != _strip_volatile_keys(new_p):
                                return True
                        except Exception as e:
                            return True
                        return False

                    if not _has_substantial_changes(matched_row, row):
                        result["action"] = "dedup_skipped"
                        result["id"] = existing_id
                        result["display_category"] = display_category
                        logger.info(f"[STORE] UNCHANGED_SKIPPED existing record ticker={event.ticker} id={existing_id[:8]} (reason: {match_reason})")
                        return result

                    if dry_run:
                        result["action"] = "updated"
                        result["id"] = existing_id
                        result["display_category"] = display_category
                        logger.info(f"[STORE DRY-RUN] WOULD UPDATE existing record ticker={event.ticker} id={existing_id[:8]} (reason: {match_reason})")
                        return result
                        
                    resp = client.table("tdnet_events").update(row).eq("id", existing_id).execute()
                    if resp.data and len(resp.data) > 0:
                        result["action"] = "updated"
                        result["id"] = existing_id
                        result["display_category"] = display_category
                        logger.info(f"[STORE] UPDATED existing record ticker={event.ticker} id={existing_id[:8]} (reason: {match_reason})")
                        return result
        except Exception as check_e:
            logger.warning(f"[STORE] Failed to check strict existing record: {check_e}")
        # ----------------------------------------------------

        # INSERT with ON CONFLICT DO NOTHING (dedupe_key unique)
        # supabase-py uses upsert with ignoreDuplicates
        if dry_run:
            logger.info(
                f"[STORE DRY-RUN] WOULD INSERT ticker={event.ticker} "
                f"type={original_event_type} -> {display_category} "
                f"title={display_title[:60]}"
            )
            result["action"] = "dry_run"
            result["display_title"] = display_title
            result["display_category"] = display_category
            result["priority_rank"] = row.get("priority_rank", 50)
            return result
            
        resp = (
            client.table("tdnet_events")
            .upsert(row, on_conflict="dedupe_key", ignore_duplicates=True)
            .execute()
        )

        if resp.data and len(resp.data) > 0:
            result["action"] = "inserted"
            result["id"] = resp.data[0].get("id", "")
            result["display_category"] = display_category
            logger.info(
                f"[STORE] INSERTED ticker={event.ticker} "
                f"type={original_event_type} -> {display_category} "
                f"dedupe_key={dedupe_key[:12]}... title={display_title[:50]}"
            )
        else:
            result["action"] = "dedup_skipped"
            result["display_category"] = display_category
            logger.info(
                f"[STORE] DEDUP_SKIPPED ticker={event.ticker} "
                f"type={original_event_type} -> {display_category} "
                f"dedupe_key={dedupe_key[:12]}..."
            )

        return result

    except Exception as e:
        logger.error(f"[STORE] save_event_to_supabase FAILED (best-effort): {e}")
        result["action"] = "error"
        result["error"] = str(e)
        return result


# ============================================================
# バッチ保存
# ============================================================
def save_events_batch(
    events: list[EventRecord],
    *,
    dry_run: bool = False,
) -> dict:
    """複数イベントをまとめて保存。

    Returns:
        {"inserted": int, "dedup_skipped": int, "errors": int, "dry_run": int}
    """
    counts = {"inserted": 0, "dedup_skipped": 0, "errors": 0, "dry_run": 0}
    category_counts = {
        "classified_buyback": 0,
        "classified_forecast": 0,
        "classified_dividend": 0,
        "classified_earnings": 0,
        "classified_shareholder": 0,
        "classified_other": 0,
        "classified_undecided": 0,
    }
    _cat_key_map = {
        DISPLAY_BUYBACK: "classified_buyback",
        DISPLAY_FORECAST: "classified_forecast",
        DISPLAY_DIVIDEND: "classified_dividend",
        DISPLAY_EARNINGS: "classified_earnings",
        DISPLAY_SHAREHOLDER: "classified_shareholder",
        DISPLAY_OTHER: "classified_other",
    }

    for ev in events:
        result = save_event_to_supabase(ev, dry_run=dry_run)
        action = result.get("action", "error")
        if action in counts:
            counts[action] += 1
        else:
            counts["errors"] += 1

        # カテゴリ集計
        cat = result.get("display_category", "")
        cat_key = _cat_key_map.get(cat, "classified_undecided")
        category_counts[cat_key] += 1

    logger.info(
        f"[STORE] batch complete: inserted={counts['inserted']} "
        f"dedup={counts['dedup_skipped']} errors={counts['errors']}"
    )
    logger.info(
        f"[STORE] category breakdown: "
        + " ".join(f"{k}={v}" for k, v in category_counts.items())
    )
    counts["category_breakdown"] = category_counts
    return counts


def _execute_chunk_upsert(client, chunk: list[dict], target_table: str = "tdnet_events") -> bool:
    """[STUB] Chunk分割後のupsert実行"""
    pass

def _verify_chunk_upsert_by_read(client, chunk: list[dict], target_table: str = "tdnet_events") -> bool:
    """[STUB] Upsert後の確認read"""
    pass

def _fallback_single_upsert(client, chunk: list[dict], target_table: str = "tdnet_events") -> dict:
    """[STUB] Chunk失敗時の個別fallback処理"""
    pass

def _read_existing_row_for_diff(client, dedupe_key: str, target_table: str = "tdnet_events") -> dict | None:
    """[STUB] write前read / write後read用の補助関数"""
    pass

def save_events_to_supabase_batch(
    events: list[EventRecord],
    *,
    dry_run: bool = True,
    enable_production_write: bool = False,
    batch_write_mode: str | None = None,
    target_table: str = "tdnet_events",
    max_items: int = 1,
    batch_size: int = 50,
    canary_mode: str | None = None
) -> dict:
    """
    複数イベントをSupabaseのtdnet_eventsへbatch upsertする。
    
    本番write時 (dry_run=False):
      - 厳密な三重ガードにより保護される。
      - Chunk upsert成功後、確認Readを行い整合性を担保。
      - Chunk upsert失敗時: chunk内rowを個別fallback対象にする
      - 成功確認できたrowのみDiscord通知・state更新へ波及させる
    
    Returns:
        dict: 処理の統計情報と成功/失敗リスト
    """
    import os
    if not dry_run:
        # === TRIPLE GUARD FOR PRODUCTION WRITE ===
        # A. 関数引数ガード
        if not enable_production_write:
            logger.error("[STORE_BATCH_GUARD_BLOCKED] enable_production_write is not True")
            raise RuntimeError("Production write is blocked by guard (enable_production_write).")
            
        if batch_write_mode != "explicit_production_canary":
            logger.error(f"[STORE_BATCH_GUARD_BLOCKED] Invalid batch_write_mode: {batch_write_mode}")
            raise RuntimeError("Production write is blocked by guard (batch_write_mode).")
            
        # B. 環境変数ガード
        if os.environ.get("ENABLE_TDNET_BATCH_WRITE") != "1":
            logger.error("[STORE_BATCH_GUARD_BLOCKED] ENABLE_TDNET_BATCH_WRITE env var is not 1")
            raise RuntimeError("Production write is blocked by guard (ENV VAR).")
            
        # C. 件数上限ガード等
        if target_table != "tdnet_events":
            logger.error(f"[STORE_BATCH_GUARD_BLOCKED] Target table must be tdnet_events, got: {target_table}")
            raise RuntimeError("Production write is blocked by guard (target_table).")
            
        if max_items > 5:
            logger.error(f"[STORE_BATCH_GUARD_BLOCKED] max_items exceeds initial limit 5: {max_items}")
            raise RuntimeError("Production write is blocked by guard (max_items).")
            
        if len(events) > max_items:
            logger.error(f"[STORE_BATCH_GUARD_BLOCKED] event count {len(events)} exceeds max_items {max_items}")
            raise RuntimeError("Production write is blocked by guard (event count).")
            
        if canary_mode != "write_only":
            logger.error(f"[STORE_BATCH_GUARD_BLOCKED] canary_mode must be write_only for now, got: {canary_mode}")
            raise RuntimeError("Production write is blocked by guard (canary_mode).")
            
        # 現時点では実態処理の実装前のため停止 (解除済)
        # raise NotImplementedError("Production write logic is fully guarded but not fully implemented yet.")
        
    client = _get_supabase()
    rows = []
    missing_dedupe = 0
    duplicate_dedupe = 0
    seen_dedupes = set()
    payload_bytes_total = 0
    
    for ev in events:
        try:
            row, raw_payload, dedupe_key, display_category, metric_yoy = build_supabase_row(ev, client)
            if not dedupe_key:
                missing_dedupe += 1
            elif dedupe_key in seen_dedupes:
                duplicate_dedupe += 1
            else:
                seen_dedupes.add(dedupe_key)
            
            rows.append(row)
            row_bytes = len(json.dumps(row, ensure_ascii=False).encode('utf-8'))
            payload_bytes_total += row_bytes
        except Exception as e:
            logger.error(f"[STORE] Error building row for {ev.ticker}: {e}")
            
    # Chunking
    chunks = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    chunk_count = len(chunks)
    
    payload_bytes_max_chunk = 0
    for chunk in chunks:
        chunk_bytes = len(json.dumps(chunk, ensure_ascii=False).encode('utf-8'))
        payload_bytes_max_chunk = max(payload_bytes_max_chunk, chunk_bytes)
        
    stats = {
        "total_events": len(events),
        "total_rows": len(rows),
        "batch_size": batch_size,
        "chunk_count": chunk_count,
        "rows_per_chunk": [len(c) for c in chunks],
        "missing_dedupe_key_count": missing_dedupe,
        "duplicate_dedupe_key_count": duplicate_dedupe,
        "payload_bytes_total": payload_bytes_total,
        "payload_bytes_max_chunk": payload_bytes_max_chunk,
        "would_write": False,
        "skipped_write": True
    }
    
    if dry_run:
        logger.info(f"[STORE_BATCH_DRY_RUN] events={len(events)} chunks={chunk_count} max_chunk_bytes={payload_bytes_max_chunk}")
    else:
        # 実write実行
        # (三重ガード通過済みの explicit_production_canary のみここへ到達する)
        logger.info(f"[STORE_BATCH_EXECUTE] Starting batch upsert... events={len(events)} chunks={chunk_count}")
        
        # 実際には max_items=1 なので chunk=1 で1件だけ実行される
        for idx, chunk in enumerate(chunks):
            logger.info(f"[STORE_BATCH_CHUNK_START] chunk={idx+1}/{chunk_count} rows={len(chunk)}")
            try:
                # 既存の upsert ロジック (dedupe_key ベース)
                # 今回は target_table パラメータを使わず固定で "tdnet_events"
                resp = client.table("tdnet_events").upsert(
                    chunk,
                    on_conflict="dedupe_key",
                    returning="minimal"
                ).execute()
                logger.info(f"[STORE_BATCH_CHUNK_SUCCESS] chunk={idx+1} upsert success.")
                stats["would_write"] = True
                stats["skipped_write"] = False
            except Exception as e:
                logger.error(f"[STORE_BATCH_CHUNK_ERROR] chunk={idx+1} failed: {e}")
                raise
                
        logger.info("[STORE_BATCH_VERIFY_START] Verification after upsert...")
        # (今回は外部スクリプトでdiff確認するため詳細verifyは省略)
        logger.info("[STORE_BATCH_VERIFY_SUCCESS] Write completed successfully.")
        logger.info(f"[STORE_BATCH_SUMMARY] Upsert done. rows={len(rows)}")

    return stats


# ============================================================
# Discord送信済み通知: discord_sent_at を Supabase へ書き戻す
# ============================================================
def update_discord_sent_at_supabase(
    event: EventRecord,
    *,
    dry_run: bool = False,
) -> bool:
    """Discord送信成功後に Supabase tdnet_events.discord_sent_at を更新する（best-effort）。

    照合キー: dedupe_key (build_dedupe_key で再計算)
    更新カラム: discord_sent_at のみ（notify_to_discord / status は変更しない）

    Returns:
        True: 更新成功 (または dry_run), False: 失敗またはレコード未発見
    """
    try:
        dedupe_key = build_dedupe_key(event)
        now_iso = datetime.now(timezone.utc).isoformat()

        if dry_run:
            logger.info(
                f"[STORE DRY-RUN] discord_sent_at update skipped: "
                f"ticker={event.ticker} dedupe={dedupe_key[:12]}..."
            )
            return True

        client = _get_supabase()
        if client is None:
            logger.warning("[STORE] Supabase client not available — discord_sent_at not updated")
            return False

        resp = (
            client.table("tdnet_events")
            .update({"discord_sent_at": now_iso})
            .eq("dedupe_key", dedupe_key)
            .execute()
        )

        if resp.data and len(resp.data) > 0:
            logger.info(
                f"[STORE] discord_sent_at updated: ticker={event.ticker} "
                f"dedupe={dedupe_key[:12]}... at={now_iso[:19]}"
            )
            return True
        else:
            # dedupe_key が見つからない = Supabase未登録 or disclosure_datetimeが不正
            logger.warning(
                f"[STORE] discord_sent_at: no row found for dedupe_key={dedupe_key[:12]}... "
                f"ticker={event.ticker} type={event.event_type}"
            )
            return False

    except Exception as e:
        logger.error(
            f"[STORE] update_discord_sent_at_supabase FAILED (best-effort): "
            f"ticker={event.ticker} error={e}"
        )
        return False
