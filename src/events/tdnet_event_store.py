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

    if quarter == "1Q":
        # 1Q: use current guidance (FY予)
        guidance = extracted.get("guidance", {})
        compare_data = {
            "label": "FY予",
            "sales_yoy": guidance.get("sales_yoy"),
            "op_yoy": guidance.get("op_yoy")
        }
    elif quarter in ("4Q", "FY"):
        # 4Q/FY: use next year forecast
        guidance = extracted.get("guidance", {})
        compare_data = {
            "label": "FY予",
            "sales_yoy": guidance.get("sales_yoy"),
            "op_yoy": guidance.get("op_yoy")
        }
    elif quarter in ("2Q", "3Q"):
        # 2Q/3Q: fetch previous quarter from Supabase
        target_quarter = "1Q" if quarter == "2Q" else "2Q"
        if client is not None:
            try:
                res = client.table('tdnet_events').select('raw_payload').eq('ticker', ticker).eq('event_type', 'earnings').order('disclosed_at', desc=True).limit(20).execute()
                if res.data:
                    for r in res.data:
                        rp = r.get('raw_payload')
                        if isinstance(rp, str):
                            try:
                                rp = json.loads(rp)
                            except:
                                continue
                        if isinstance(rp, dict):
                            ext = rp.get('extracted', {})
                            if isinstance(ext, dict) and ext.get('quarter') == target_quarter:
                                compare_data = {
                                    "label": "前Q",
                                    "sales_yoy": ext.get('sales_yoy'),
                                    "op_yoy": ext.get('op_yoy')
                                }
                                break
            except Exception as e:
                logger.warning(f"[STORE] Failed to fetch previous quarter from Supabase: {e}")

    # nullの場合は "compare": null とする
    return {
        "current": {
            "label": quarter,
            "sales_yoy": extracted.get("sales_yoy"),
            "op_yoy": extracted.get("op_yoy")
        },
        "compare": compare_data
    }


# ============================================================
# メイン: Supabase へ保存
# ============================================================
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
        dedupe_key = build_dedupe_key(event)
        result["dedupe_key"] = dedupe_key

        priority_rank = compute_priority_rank(event)
        # display_title / display_summary / formatted_message:
        # EventRecord のフィールドを直接使う（event_type ごとの整形は呼び出し側の責務）
        display_title = event.title or ""
        display_summary = event.summary_text or ""
        formatted_message = event.summary_text or ""
        metric_name, metric_value, metric_yoy = _extract_primary_metric(event)
        strength = _compute_strength_score(event)
        notify_discord = should_notify_event(event)

        # 表示カテゴリ正規化 (元 event_type は raw_payload に保持)
        display_category = _normalize_display_category(event)
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
            try:
                raw_payload["extracted"] = json.loads(event.extracted_payload_json)
            except (json.JSONDecodeError, TypeError):
                raw_payload["extracted_text"] = event.extracted_payload_json
        # 元の event_type を raw_payload に保存
        raw_payload["original_event_type"] = original_event_type

        # text_extract_status: extracted 全数値が null なら "empty" フラグ付与
        extracted = raw_payload.get("extracted", {})
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
            "pdf_url": event.doc_url if event.event_type in ("earnings", "forecast") else None,
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

        if dry_run:
            logger.info(
                f"[STORE DRY-RUN] would insert: ticker={event.ticker} "
                f"type={original_event_type} -> {display_category} "
                f"title={display_title[:60]}"
            )
            result["action"] = "dry_run"
            result["display_title"] = display_title
            result["display_category"] = display_category
            result["priority_rank"] = priority_rank
            return result

        client = _get_supabase()
        if client is None:
            logger.warning("[STORE] Supabase client not available — skipping save")
            result["action"] = "error"
            result["error"] = "supabase_not_available"
            return result

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

        # INSERT with ON CONFLICT DO NOTHING (dedupe_key unique)
        # supabase-py uses upsert with ignoreDuplicates
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
