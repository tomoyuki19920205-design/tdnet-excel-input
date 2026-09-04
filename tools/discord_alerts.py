#!/usr/bin/env python3
# ============================================================
# discord_alerts.py
# ============================================================
# TDNET取込済み銘柄の YOY/QoQ をチェックし、
# 閾値超の銘柄だけ Discord Webhook で通知する。
#
# ルール設計:
#   ALERT_RULES にカテゴリ別ルールを定義。
#   現在は "tanshin" のみ。将来 "revision", "briefing" 等を追加可能。
#
# 重複防止:
#   logs/alert_sent_log.json に (ticker, period, quarter, doc_category) を記録。
#   同じ組み合わせは2度送信しない。
#
# 使い方:
#   python tools\discord_alerts.py
#   python tools\discord_alerts.py --tickers 7203,6758
#   python tools\discord_alerts.py --test
#   python tools\discord_alerts.py --clear-log   (送信ログをクリア)
# ============================================================
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_DEFAULT_ITEMS_FILE = os.path.join(_PROJECT_ROOT, "logs", "last_ingested_items.json")
_DEFAULT_TICKERS_FILE = os.path.join(_PROJECT_ROOT, "logs", "last_ingested_tickers.json")
_SENT_LOG_FILE = os.path.join(_PROJECT_ROOT, "logs", "alert_sent_log.json")
_DEFAULT_DB_PATH = os.path.join(_PROJECT_ROOT, "decision_db.db")
JST = timezone(timedelta(hours=9))


# ticker 正規化は共通モジュールを使用
from lib.runtime_paths import runtime_path
from src.common_ticker import normalize_ticker as _normalize_ticker, ticker_to_sec_code as _ticker_to_sec_code

# 決算スコア
from src.alerts.earnings_score import (
    calculate_earnings_score,
    build_score_reason,
    ScoreResult,
)


def _safe_print(msg: str):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ============================================================
# Alert Rules (extensible)
# ============================================================
# Each rule: { "doc_category": str, "enabled": bool, "label": str }
# Future: add "revision", "briefing", etc.
ALERT_RULES = [
    {"doc_category": "tanshin", "enabled": False, "label": "決算短信"},  # 一時無効化: あとで正式版を設定する
    {"doc_category": "revision", "enabled": False, "label": "業績修正"},
    {"doc_category": "briefing", "enabled": False, "label": "説明資料"},
]

def _get_enabled_categories() -> set[str]:
    return {r["doc_category"] for r in ALERT_RULES if r["enabled"]}


# ============================================================
# Sent log (dedup)
# ============================================================
def _load_sent_log() -> set[tuple]:
    if not os.path.exists(str(runtime_path(_SENT_LOG_FILE))):
        return set()
    try:
        with open(str(runtime_path(_SENT_LOG_FILE)), "r", encoding="utf-8-sig") as f:
            entries = json.load(f)
        return {tuple(e) for e in entries}
    except Exception:
        return set()


def _save_sent_log(log: set[tuple]):
    os.makedirs(os.path.dirname(str(runtime_path(_SENT_LOG_FILE))), exist_ok=True)
    entries = sorted(log)
    with open(str(runtime_path(_SENT_LOG_FILE)), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _make_key(ticker: str, period: str, quarter: str, doc_category: str) -> tuple:
    return (ticker, period, quarter, doc_category)


# ============================================================
# .env
# ============================================================
def _load_dotenv():
    env_path = Path(_PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


# ============================================================
# Supabase fetch
# ============================================================
def _fetch_company_names(rest_url: str, headers: dict) -> dict[str, str]:
    """Supabase companies テーブルから ticker_code → name_ja のマップを一括取得する。

    Supabase REST API はデフォルト1000行制限のため、Range ヘッダーで
    ページネーションして全件取得する。

    Returns:
        { "7203": "トヨタ自動車", ... }  (4桁ticker → name_ja)
        取得失敗時は {} を返し、通知全体は止めない。
    """
    PAGE_SIZE = 1000
    company_map: dict[str, str] = {}
    try:
        offset = 0
        while True:
            page_headers = {
                **headers,
                "Range": f"{offset}-{offset + PAGE_SIZE - 1}",
            }
            r = requests.get(
                f"{rest_url}/companies", headers=page_headers,
                params={"select": "ticker_code,name_ja"},
                timeout=15,
            )
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for row in rows:
                tc = row.get("ticker_code", "")
                name = (row.get("name_ja") or "").strip()
                if not tc or not name:
                    continue
                # ticker_code → 4桁に正規化してキーにする
                ticker4 = _normalize_ticker(tc)
                if ticker4:
                    company_map[ticker4] = name
            if len(rows) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        _safe_print(f"  [INFO] companies map loaded: {len(company_map)} entries")
        return company_map
    except Exception as e:
        _safe_print(f"  [WARN] companies 取得失敗 (ticker のみで継続): {e}")
        return {}


def _display_name(ticker: str, company_name: str = "") -> str:
    """通知用の表示名を生成する。

    企業名があれば  "{company_name}（{ticker}）"
    なければ        "{ticker}"
    """
    if company_name:
        return f"{company_name}（{ticker}）"
    return ticker


def _fetch_financials(rest_url: str, headers: dict, ticker: str) -> list[dict]:
    # Supabase financials は5桁コードなので4桁→5桁に変換して検索
    t = _normalize_ticker(ticker)
    ticker5 = t + "0" if len(t) == 4 else t
    params = {
        "select": "ticker,period,quarter,sales,operating_profit",
        "ticker": f"eq.{ticker5}",
        "order": "period.desc,quarter.desc",
    }
    try:
        r = requests.get(f"{rest_url}/financials", headers=headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _safe_print(f"  [WARN] {ticker}: fetch failed - {e}")
        return []


# ============================================================
# YOY / QoQ calculation
# ============================================================
def _pct(current, previous) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def _find(rows: list[dict], period: str, quarter: str) -> dict | None:
    for r in rows:
        if r.get("period") == period and r.get("quarter") == quarter:
            return r
    return None


def _prev_year(period: str) -> str | None:
    try:
        parts = period.split("-")
        return f"{int(parts[0]) - 1}-{parts[1]}-{parts[2]}"
    except Exception:
        return None


_PREV_Q = {"2Q": "1Q", "3Q": "2Q", "4Q": "3Q", "FY": "4Q"}


def compute_alert(rows: list[dict], yoy_th: float, qoq_th: float) -> dict:
    if not rows:
        return {"should_alert": False, "reason": "no data"}

    latest = rows[0]
    period = latest.get("period", "")
    quarter = latest.get("quarter", "")

    result = {
        "ticker": latest.get("ticker", ""),
        "period": period,
        "quarter": quarter,
        "sales_yoy": None, "op_yoy": None,
        "sales_qoq": None, "op_qoq": None,
        "prev_q_sales_yoy": None, "prev_q_op_yoy": None,
        "should_alert": False,
    }

    # YOY
    py = _prev_year(period)
    if py:
        prev = _find(rows, py, quarter)
        if prev:
            result["sales_yoy"] = _pct(latest.get("sales"), prev.get("sales"))
            result["op_yoy"] = _pct(latest.get("operating_profit"), prev.get("operating_profit"))

    # QoQ
    pq = _PREV_Q.get(quarter)
    if pq:
        prev_q = _find(rows, period, pq)
        if prev_q:
            result["sales_qoq"] = _pct(latest.get("sales"), prev_q.get("sales"))
            result["op_qoq"] = _pct(latest.get("operating_profit"), prev_q.get("operating_profit"))

    # Prev Q YOY
    if pq and py:
        pq_row = _find(rows, period, pq)
        py_pq = _find(rows, py, pq)
        if pq_row and py_pq:
            result["prev_q_sales_yoy"] = _pct(pq_row.get("sales"), py_pq.get("sales"))
            result["prev_q_op_yoy"] = _pct(pq_row.get("operating_profit"), py_pq.get("operating_profit"))

    # Threshold check
    for k in ("sales_yoy", "op_yoy"):
        if result[k] is not None and abs(result[k]) >= yoy_th:
            result["should_alert"] = True
    for k in ("sales_qoq", "op_qoq"):
        if result[k] is not None and abs(result[k]) >= qoq_th:
            result["should_alert"] = True

    return result


# ============================================================
# Discord message
# ============================================================
def _fmt(val: float | None) -> str:
    if val is None:
        return "---"
    return f"{'+' if val > 0 else ''}{val:.1f}%"


def _build_msg(alert: dict, doc_category: str,
               score_result: ScoreResult | None = None,
               ai_comment: str = "",
               company_name: str = "") -> str:
    t = alert["ticker"]
    disp = _display_name(t, company_name)
    p = alert.get("period", "?")
    q = alert.get("quarter", "?")
    pq = _PREV_Q.get(q, "?")
    cat_label = next((r["label"] for r in ALERT_RULES if r["doc_category"] == doc_category), doc_category)

    lines = []

    # スコアヘッダー
    if score_result:
        sr = score_result
        reason = build_score_reason(sr)
        lines.append(f"{sr.emoji}【{sr.rank}ランク決算】{disp} | {p} {q} [{cat_label}]")
        lines.append(f"Score: {sr.total_score}")
        lines.append(f"・スコア理由: {reason}")
    else:
        lines.append(f"**{disp}** | {p} {q} [{cat_label}]")

    lines.append("")
    lines.append(f"**Latest Q ({q})**")
    lines.append(f"  Sales YOY: {_fmt(alert.get('sales_yoy'))}  |  OP YOY: {_fmt(alert.get('op_yoy'))}")
    lines.append(f"  Sales QoQ: {_fmt(alert.get('sales_qoq'))}  |  OP QoQ: {_fmt(alert.get('op_qoq'))}")
    lines.append("")
    lines.append(f"**Prev Q ({pq})**")
    lines.append(f"  Sales YOY: {_fmt(alert.get('prev_q_sales_yoy'))}  |  OP YOY: {_fmt(alert.get('prev_q_op_yoy'))}")

    msg = "\n".join(lines)
    if ai_comment:
        msg += "\n\n" + ai_comment
    return msg


# ============================================================
# AI 差分要約コメント
# ============================================================

# tone_change 日本語マッピング
_TONE_JA = {
    "stronger_positive": "強気",
    "slightly_positive": "やや強気",
    "neutral": "中立",
    "slightly_negative": "やや慎重",
    "stronger_negative": "慎重",
    "mixed": "強弱混在",
}

# キーワード分類
_POSITIVE_KEYWORDS = {"需要", "受注", "価格改定", "為替", "稼働率", "増収", "回復",
                      "好調", "拡大", "成長", "改善", "貢献"}
_NEGATIVE_KEYWORDS = {"原材料", "減損", "在庫調整", "不透明感", "低調", "為替",
                      "リスク", "悪化", "減少", "減収", "高騰", "圧迫"}


def _truncate(text: str, max_len: int = 80) -> str:
    """テキストを max_len 文字に制限"""
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def _parse_keywords_json(kw_json: str | None) -> list[str]:
    """new_keywords_json をパースし、重複除去後のリストを返す"""
    if not kw_json:
        return []
    try:
        kws = json.loads(kw_json)
        if isinstance(kws, list):
            seen = set()
            result = []
            for k in kws:
                k_str = str(k).strip()
                if k_str and k_str not in seen:
                    seen.add(k_str)
                    result.append(k_str)
            return result
        return []
    except (json.JSONDecodeError, TypeError):
        return []


def extract_positive_reason(row: dict) -> str:
    """
    好調理由を組み立てる。
    優先: profit_factor_change -> demand_change -> positive keywords
    """
    parts = []
    pfc = (row.get("profit_factor_change") or "").strip()
    if pfc:
        parts.append(_truncate(pfc, 60))
    dc = (row.get("demand_change") or "").strip()
    if dc:
        parts.append(_truncate(dc, 60))

    if not parts:
        # キーワードから補完
        kws = _parse_keywords_json(row.get("new_keywords_json"))
        pos = [k for k in kws if k in _POSITIVE_KEYWORDS]
        if pos:
            parts.append(", ".join(pos[:3]))

    return " / ".join(parts) if parts else ""


def extract_risk_reason(row: dict) -> str:
    """
    注意点を組み立てる。
    優先: risk_change -> negative keywords
    """
    parts = []
    rc = (row.get("risk_change") or "").strip()
    if rc:
        parts.append(_truncate(rc, 60))

    if not parts:
        kws = _parse_keywords_json(row.get("new_keywords_json"))
        neg = [k for k in kws if k in _NEGATIVE_KEYWORDS]
        if neg:
            parts.append(", ".join(neg[:3]))

    return " / ".join(parts) if parts else ""


def get_latest_diff_summary(
    conn: sqlite3.Connection,
    ticker: str,
    period: str,
    quarter: str,
) -> dict | None:
    """
    filing_diff_summaries から AI 差分要約を取得する。

    優先順位:
    1. 同一 ticker/period/quarter で ai_status='completed'（最新）
    2. 同一 ticker で period/quarter が近い completed レコード
       （period DESC, quarter のソート順で近いものを優先）
    3. completed がなければ None
    """
    # 1. 完全一致
    row = conn.execute(
        "SELECT * FROM filing_diff_summaries "
        "WHERE ticker=? AND period=? AND quarter=? AND ai_status='completed' "
        "ORDER BY created_at DESC LIMIT 1",
        (ticker, period, quarter),
    ).fetchone()
    if row:
        return dict(row)

    # 2. フォールバック: 同一 ticker で近い period/quarter
    #    ABS(julianday(period) - julianday(target)) でソート
    row = conn.execute(
        "SELECT * FROM filing_diff_summaries "
        "WHERE ticker=? AND ai_status='completed' "
        "ORDER BY ABS(julianday(period) - julianday(?)) ASC, "
        "  CASE quarter "
        "    WHEN ? THEN 0 "
        "    ELSE 1 "
        "  END ASC, "
        "  created_at DESC "
        "LIMIT 1",
        (ticker, period, quarter),
    ).fetchone()
    if row:
        return dict(row)

    return None


def build_ai_comment_block(row: dict | None) -> str:
    """
    AI 差分要約から Discord コメントブロックを構築する。
    すべて空なら空文字列を返す。
    """
    if row is None:
        return ""

    items: list[str] = []

    # 総評
    so = _truncate(row.get("summary_overall") or "", 80)
    if so:
        items.append(f"・総評: {so}")

    # 好調理由
    pos = extract_positive_reason(row)
    if pos:
        items.append(f"・好調理由: {_truncate(pos, 80)}")

    # 見通し
    gc = _truncate(row.get("guidance_change") or "", 80)
    if gc:
        items.append(f"・見通し: {gc}")

    # 注意点
    risk = extract_risk_reason(row)
    if risk:
        items.append(f"・注意点: {_truncate(risk, 80)}")

    # トーン
    tone_raw = (row.get("tone_change") or "").strip()
    if tone_raw:
        tone_ja = _TONE_JA.get(tone_raw, tone_raw)
        items.append(f"・トーン: {tone_ja}")

    # キーワード
    kws = _parse_keywords_json(row.get("new_keywords_json"))
    if kws:
        items.append(f"・キーワード: {', '.join(kws[:5])}")

    if not items:
        return ""

    return "【AI差分要約】\n" + "\n".join(items)


def _send_discord(webhook_url: str, content: str) -> bool:
    try:
        r = requests.post(webhook_url, json={"content": content}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        _safe_print(f"  [ERROR] Discord send failed: {e}")
        return False


# ============================================================
# Main logic
# ============================================================
def run_alerts(
    items: list[dict],
    webhook_url: str,
    supabase_url: str,
    supabase_key: str,
    yoy_th: float = 20.0,
    qoq_th: float = 30.0,
    db_path: str = "",
) -> dict:
    rest_url = supabase_url.rstrip("/") + "/rest/v1"
    api_headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}

    # 企業名マップ一括取得（失敗時は {} で ticker のみ表示）
    company_map = _fetch_company_names(rest_url, api_headers)

    enabled = _get_enabled_categories()
    sent_log = _load_sent_log()

    # SQLite 接続（AI差分要約取得用）
    _db = db_path or str(runtime_path(_DEFAULT_DB_PATH))
    diff_conn: sqlite3.Connection | None = None
    if os.path.exists(_db):
        try:
            diff_conn = sqlite3.connect(_db)
            diff_conn.row_factory = sqlite3.Row
        except Exception as e:
            _safe_print(f"  [WARN] SQLite open failed: {e}")
            diff_conn = None

    checked = 0
    sent = 0
    skipped = 0
    deduped = 0

    for item in items:
        # -- item format (new): {"ticker","period","quarter","doc_category","status"}
        # -- item format (legacy): plain string ticker
        if isinstance(item, str):
            ticker = item
            doc_category = "tanshin"
            period_hint = ""
            quarter_hint = ""
        else:
            ticker = item.get("ticker", "")
            doc_category = item.get("doc_category", "tanshin")
            period_hint = item.get("period", "")
            quarter_hint = item.get("quarter", "")

        # Category filter
        if doc_category not in enabled:
            _safe_print(f"  {ticker}: SKIP (category={doc_category} disabled)")
            skipped += 1
            continue

        # Dedup check
        key = _make_key(ticker, period_hint, quarter_hint, doc_category)
        if key in sent_log:
            _safe_print(f"  {ticker}: SKIP (already sent {period_hint} {quarter_hint})")
            deduped += 1
            continue

        # Fetch data
        rows = _fetch_financials(rest_url, api_headers, ticker)
        if not rows:
            _safe_print(f"  {ticker}: SKIP (no data)")
            skipped += 1
            continue

        alert = compute_alert(rows, yoy_th, qoq_th)
        alert["ticker"] = ticker  # 4桁正規化済みtickerを優先
        checked += 1

        if alert["should_alert"]:
            # AI 差分要約の取得
            ai_comment = ""
            summary_row = None
            if diff_conn:
                try:
                    a_period = alert.get("period", period_hint)
                    a_quarter = alert.get("quarter", quarter_hint)
                    summary_row = get_latest_diff_summary(
                        diff_conn, ticker, a_period, a_quarter
                    )
                    ai_comment = build_ai_comment_block(summary_row)
                except Exception as e:
                    _safe_print(f"  {ticker}: [WARN] AI comment failed: {e}")

            # 決算スコア算出
            score_result = calculate_earnings_score(alert, summary_row)

            msg = _build_msg(alert, doc_category,
                             score_result=score_result, ai_comment=ai_comment,
                             company_name=company_map.get(ticker, ""))
            _safe_print(f"  {ticker}: ALERT -> sending... (Score:{score_result.total_score} Rank:{score_result.rank})")
            _safe_print(f"[MSG_PREVIEW]\n{msg}")
            if _send_discord(webhook_url, msg):
                sent += 1
                sent_log.add(key)
            else:
                _safe_print(f"  {ticker}: send failed")
        else:
            _safe_print(f"  {ticker}: below threshold (YOY<{yoy_th}% QoQ<{qoq_th}%)")

    # Cleanup
    if diff_conn:
        diff_conn.close()

    # Save log
    _save_sent_log(sent_log)

    _safe_print(f"ALERT_DONE sent={sent} checked={checked} skipped={skipped} deduped={deduped}")
    return {"sent": sent, "checked": checked, "skipped": skipped, "deduped": deduped}


def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Discord alert for TDNET ingested tickers (YOY/QoQ threshold)",
    )
    parser.add_argument("--tickers_file", default=None,
                        help="items JSON file (default: auto-detect items.json or tickers.json)")
    parser.add_argument("--tickers", default=None, help="Comma-separated tickers (e.g. 7203,6758)")
    parser.add_argument("--test", action="store_true", help="Send test notification")
    parser.add_argument("--clear-log", action="store_true", help="Clear sent log")
    args = parser.parse_args()

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")
    yoy_th = float(os.environ.get("ALERT_YOY_PCT", "20"))
    qoq_th = float(os.environ.get("ALERT_QOQ_PCT", "30"))

    if not webhook_url:
        _safe_print("[ERROR] DISCORD_WEBHOOK_URL not set in .env")
        _safe_print("  Add to .env: DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...")
        sys.exit(1)

    if not supabase_url or not supabase_key:
        _safe_print("[ERROR] SUPABASE_URL / SUPABASE_ANON_KEY not set in .env")
        sys.exit(1)

    # Clear log
    if args.clear_log:
        if os.path.exists(str(runtime_path(_SENT_LOG_FILE))):
            os.remove(str(runtime_path(_SENT_LOG_FILE)))
            _safe_print(f"[OK] Sent log cleared: {str(runtime_path(_SENT_LOG_FILE))}")
        else:
            _safe_print("[OK] No sent log to clear")
        sys.exit(0)

    # Test mode
    if args.test:
        _safe_print("[TEST] Sending test notification...")
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        ok = _send_discord(webhook_url, f"TDNET Alert System - test OK ({now})")
        _safe_print("[TEST] OK!" if ok else "[TEST] FAILED")
        sys.exit(0 if ok else 1)

    # Load items
    if args.tickers:
        items = [{"ticker": _normalize_ticker(t.strip()), "doc_category": "tanshin", "period": "", "quarter": ""}
                 for t in args.tickers.split(",") if t.strip()]
    else:
        # items.json 優先、なければ tickers.json
        input_file = args.tickers_file
        if input_file is None:
            if os.path.exists(str(runtime_path(_DEFAULT_ITEMS_FILE))):
                input_file = str(runtime_path(_DEFAULT_ITEMS_FILE))
            elif os.path.exists(str(runtime_path(_DEFAULT_TICKERS_FILE))):
                input_file = str(runtime_path(_DEFAULT_TICKERS_FILE))
            else:
                _safe_print("[INFO] No items/tickers file found")
                _safe_print("ALERT_DONE sent=0 checked=0 skipped=0 deduped=0")
                sys.exit(0)
        if not os.path.exists(input_file):
            _safe_print(f"[INFO] File not found: {input_file}")
            _safe_print("ALERT_DONE sent=0 checked=0 skipped=0 deduped=0")
            sys.exit(0)
        with open(input_file, "r", encoding="utf-8-sig") as f:
            items = json.load(f)
        # 後方互換: tickerを正規化
        for item in items:
            if isinstance(item, dict) and "ticker" in item:
                item["ticker"] = _normalize_ticker(item["ticker"])

    if not items:
        _safe_print("[INFO] 0 items. No alerts.")
        _safe_print("ALERT_DONE sent=0 checked=0 skipped=0 deduped=0")
        sys.exit(0)

    _safe_print(f"[ALERT] {len(items)} item(s)")
    _safe_print(f"[ALERT] thresholds: YOY>={yoy_th}% QoQ>={qoq_th}%")
    _safe_print(f"[ALERT] enabled categories: {sorted(_get_enabled_categories())}")

    result = run_alerts(
        items=items,
        webhook_url=webhook_url,
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        yoy_th=yoy_th,
        qoq_th=qoq_th,
        db_path=str(runtime_path(os.path.join(_PROJECT_ROOT, "decision_db.db"))),
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
