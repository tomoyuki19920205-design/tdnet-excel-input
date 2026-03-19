#!/usr/bin/env python3
# ============================================================
# edinet_large_holder_watch.py
# ============================================================
# EDINET API v2 から「大量保有報告書」「変更報告書（大量保有）」を取得し、
# 特定人物（片山晃・井村俊哉）に該当するものを Discord Webhook で通知する。
#
# 使い方:
#   python scripts/edinet_large_holder_watch.py          # 1回実行
#   python scripts/edinet_large_holder_watch.py --loop   # 5分間隔でループ
#   python scripts/edinet_large_holder_watch.py --test    # Discord接続テスト
#   python scripts/edinet_large_holder_watch.py --test-api  # EDINET API接続テスト
#   python scripts/edinet_large_holder_watch.py --dry-run   # 通知せず検知結果表示
#   python scripts/edinet_large_holder_watch.py --seed-state # 既存分をstateに登録
# ============================================================
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    _HAS_PDFPLUMBER = False

# ============================================================
# パス・定数
# ============================================================
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _PROJECT_ROOT / "state"
_STATE_FILE = _STATE_DIR / "edinet_large_holder.json"
_EDINET_CACHE_FILE = _STATE_DIR / "edinet_code_cache.json"
_LOG_DIR = _PROJECT_ROOT / "logs"
MAX_CACHE_ENTRIES = 10000  # edinetCodeキャッシュの上限件数

JST = timezone(timedelta(hours=9))
EDINET_API_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
POLL_INTERVAL_SEC = 300  # 5分
MAX_SEEN_IDS = 5000  # seen_doc_ids の上限件数

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("edinet_large_holder")

# ============================================================
# 対象人物（エイリアス辞書構造 — 将来拡張容易）
# ============================================================
# キー: 表示名, 値: filerName 部分一致に使う別名リスト
TARGET_ALIASES: dict[str, list[str]] = {
    "片山晃": ["片山晃", "片山 晃"],
    "井村俊哉": ["井村俊哉", "井村 俊哉"],
}

# 大量保有関連の docDescription キーワード（直接マッチ）
DOC_KEYWORDS_DIRECT = ["大量保有報告書", "大量保有変更報告書"]
# docDescription 内に「大量」を含み、かつ報告書系キーワードを含む場合
DOC_KEYWORDS_REPORT = ["報告書", "変更報告書"]
DOC_CONTEXT_LARGE = "大量"
# ordinanceCode による判定（大量保有府令 = "060"）
ORDINANCE_CODE_LARGE_HOLDER = "060"


# ============================================================
# .env 読込
# ============================================================
def _load_dotenv():
    env_path = _PROJECT_ROOT / ".env"
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
# 状態管理
# ============================================================
def load_state() -> dict:
    """状態ファイルを読み込む。存在しなければ初期状態を返す。"""
    if not _STATE_FILE.exists():
        return {"seen_doc_ids": [], "last_checked_at": None}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 後方互換
        if "seen_doc_ids" not in data:
            data["seen_doc_ids"] = []
        if "last_checked_at" not in data:
            data["last_checked_at"] = None
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("状態ファイル読込失敗 (初期状態で継続): %s", e)
        return {"seen_doc_ids": [], "last_checked_at": None}


def save_state(state: dict):
    """状態ファイルを保存する。seen_doc_ids は上限を超えたら古い方から切り捨て。"""
    # 上限超過時は新しい方を残す
    ids = state.get("seen_doc_ids", [])
    if len(ids) > MAX_SEEN_IDS:
        state["seen_doc_ids"] = ids[-MAX_SEEN_IDS:]
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# EDINET API
# ============================================================
def fetch_documents(date: str, api_key: str, max_retries: int = 3) -> list[dict]:
    """指定日の書類一覧を取得する。リトライ付き。"""
    params = {
        "date": date,
        "type": 2,
        "Subscription-Key": api_key,
    }
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(EDINET_API_URL, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            log.info("  %s: %d 件取得", date, len(results))
            return results
        except requests.RequestException as e:
            wait = 2 ** attempt
            log.warning("  API失敗 (%d/%d): %s → %d秒後リトライ", attempt, max_retries, e, wait)
            if attempt < max_retries:
                time.sleep(wait)
        except (json.JSONDecodeError, KeyError) as e:
            log.error("  JSONデコード失敗: %s", e)
            return []
    log.error("  %s: API取得失敗（リトライ上限）", date)
    return []


# ============================================================
# フィルタリング
# ============================================================
def is_target_doc(doc: dict) -> bool:
    """大量保有関連の書類かどうかを判定する。

    判定方法（OR条件）:
    1. ordinanceCode が "060"（大量保有府令） → 無条件で True
    2. docDescription に「大量保有報告書」「大量保有変更報告書」を直接含む → True
    3. docDescription に「大量」+「報告書」を両方含む → True
       （例: 「変更報告書（短期大量譲渡）」もマッチ）
    """
    # 方法1: ordinanceCode ベース（最も信頼性が高い）
    ordinance = str(doc.get("ordinanceCode") or "")
    if ordinance == ORDINANCE_CODE_LARGE_HOLDER:
        return True

    # 方法2-3: docDescription ベース（フォールバック）
    desc = doc.get("docDescription") or ""
    # 直接キーワードにマッチ
    for kw in DOC_KEYWORDS_DIRECT:
        if kw in desc:
            return True
    # 「大量」+「報告書」コンテキスト
    if DOC_CONTEXT_LARGE in desc and any(kw in desc for kw in DOC_KEYWORDS_REPORT):
        return True

    return False


def match_person(filer_name: str) -> list[str]:
    """filerName に対象人物名が含まれるかチェックし、一致した表示名リストを返す。"""
    if not filer_name:
        return []
    matched = []
    for display_name, aliases in TARGET_ALIASES.items():
        if any(alias in filer_name for alias in aliases):
            matched.append(display_name)
    return matched


def should_notify(doc: dict) -> bool:
    """訂正・取下げを除外する。型揺れを str() で吸収。"""
    ws = str(doc.get("withdrawalStatus", ""))
    ds = str(doc.get("disclosureStatus", ""))
    # "0" = 通常、それ以外は訂正・取下げ
    if ws != "0" or ds != "0":
        return False
    return True


# ============================================================
# 保有比率抽出 (PDF)
# ============================================================
def download_pdf(doc_id: str, api_key: str, timeout: int = 20) -> bytes | None:
    """EDINET API type=2 で PDF を取得する。失敗時は None。"""
    url = f"https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
    params = {"type": 2, "Subscription-Key": api_key}
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "")
        if "pdf" in ct.lower() or r.content[:5] == b"%PDF-":
            return r.content
        log.warning("  PDF取得: 予期しないContent-Type=%s (docID=%s)", ct, doc_id)
        return None
    except requests.RequestException as e:
        log.warning("  PDF取得失敗 (docID=%s): %s", doc_id, e)
        return None


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """PDF バイナリからテキストを抽出し、NFKC正規化する。"""
    if not _HAS_PDFPLUMBER:
        log.warning("  pdfplumber 未インストール: pip install pdfplumber")
        return ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages_text = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages_text.append(text)
            raw = "\n".join(pages_text)
        # NFKC正規化 (全角→半角)
        return unicodedata.normalize("NFKC", raw)
    except Exception as e:
        log.warning("  PDFテキスト抽出失敗: %s", e)
        return ""


def extract_holding_ratio(text: str) -> dict:
    """テキストから保有比率を抽出する。

    Returns:
        {"current": float|None, "previous": float|None, "delta": float|None,
         "issuer_name": str, "sec_code": str}
    """
    result: dict = {
        "current": None, "previous": None, "delta": None,
        "issuer_name": "", "sec_code": "",
    }
    if not text:
        return result

    # --- 発行者名・証券コード抽出 ---
    m_issuer = re.search(r'発行者の名称\s*(.+)', text)
    if m_issuer:
        result["issuer_name"] = m_issuer.group(1).strip()
    m_sec = re.search(r'証券コード\s*(\w+)', text)
    if m_sec:
        result["sec_code"] = m_sec.group(1).strip()

    # --- 保有比率抽出（複数パターン fallback）---
    # パターン1: "株券等保有割合(%)" の直後の数値行
    m_current = re.search(
        r'上記提出者の株券等保有割合\s*\(?%?\)?\s*[\n\r]+\s*(\d+\.\d+)',
        text,
    )
    if m_current:
        result["current"] = float(m_current.group(1))

    # パターン1b: "保有割合" + 同行 or 次行の数値%
    if result["current"] is None:
        m_alt = re.search(
            r'(?:株券等)?保有割合\s*\(?%?\)?\s*[:\s]*(\d+\.\d+)\s*%?',
            text,
        )
        if m_alt:
            result["current"] = float(m_alt.group(1))

    # 前回保有割合: "直前の報告書に記載された" の後の数値
    m_prev = re.search(
        r'直前の報告書に記載された\s*[\n\r]+\s*(\d+\.\d+)',
        text,
    )
    if m_prev:
        result["previous"] = float(m_prev.group(1))

    # パターン2b: 前回 fallback
    if result["previous"] is None:
        m_prev2 = re.search(
            r'直前.*?保有割合\s*\(?%?\)?\s*[:\s]*(\d+\.\d+)',
            text, re.DOTALL,
        )
        if m_prev2:
            result["previous"] = float(m_prev2.group(1))

    # 増減計算
    if result["current"] is not None and result["previous"] is not None:
        result["delta"] = round(result["current"] - result["previous"], 2)

    return result


def fetch_holding_ratio(doc_id: str, api_key: str) -> dict:
    """docID から PDF を取得し、保有比率を抽出する。"""
    pdf_bytes = download_pdf(doc_id, api_key)
    if not pdf_bytes:
        return {"current": None, "previous": None, "delta": None,
                "issuer_name": "", "sec_code": ""}
    text = extract_text_from_pdf(pdf_bytes)
    return extract_holding_ratio(text)


def _fmt_ratio(ratio_info: dict) -> str:
    """保有比率情報を通知用文字列にフォーマットする。"""
    current = ratio_info.get("current")
    previous = ratio_info.get("previous")
    delta = ratio_info.get("delta")

    if current is None:
        return "保有比率: 取得失敗"

    parts = [f"{current:.2f}%"]
    if previous is not None:
        delta_str = f"{delta:+.2f}%" if delta is not None else "?"
        parts.append(f"前回 {previous:.2f}% / {delta_str}")
    return f"保有比率: {parts[0]}" + (f"（{parts[1]}）" if len(parts) > 1 else "")


# ============================================================
# Discord Webhook
# ============================================================
def send_discord(webhook_url: str, content: str, max_retries: int = 3) -> bool:
    """Discord Webhook にメッセージを送信する。リトライ付き。"""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(
                webhook_url,
                json={"content": content},
                timeout=10,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            wait = 2 ** attempt
            log.warning("  Discord送信失敗 (%d/%d): %s → %d秒後リトライ", attempt, max_retries, e, wait)
            if attempt < max_retries:
                time.sleep(wait)
    log.error("  Discord送信失敗（リトライ上限）")
    return False


# ============================================================
# edinetCode キャッシュ（永続化）
# ============================================================
def _load_edinet_cache() -> dict[str, dict]:
    """ローカルキャッシュを読み込む。存在しなければ空辞書を返す。"""
    if not _EDINET_CACHE_FILE.exists():
        return {}
    try:
        with open(_EDINET_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("キャッシュ読込失敗: %s", e)
        return {}


def _save_edinet_cache(cache: dict[str, dict]):
    """ローカルキャッシュを保存する。上限超過時は古いエントリを削除。"""
    if len(cache) > MAX_CACHE_ENTRIES:
        # 古い方（先頭側）を削除して上限に収める
        keys = list(cache.keys())
        for k in keys[:len(cache) - MAX_CACHE_ENTRIES]:
            del cache[k]
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_EDINET_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _merge_docs_into_cache(cache: dict[str, dict], docs: list[dict]) -> dict[str, dict]:
    """ドキュメント一覧から edinetCode → {filerName, secCode} を抽出しキャッシュにマージする。

    secCode 付きのエントリを優先。既存の secCode 付きは上書きしない。
    """
    for doc in docs:
        ec = doc.get("edinetCode") or ""
        if not ec:
            continue
        existing = cache.get(ec)
        new_sec = doc.get("secCode") or ""
        new_name = doc.get("filerName") or ""
        if existing and existing.get("secCode") and not new_sec:
            continue  # secCode 付きが既にあれば上書きしない
        cache[ec] = {"filerName": new_name, "secCode": new_sec}
    return cache


def _resolve_issuer(doc: dict, edinet_cache: dict[str, dict]) -> tuple[str, str]:
    """発行者情報を解決する。

    フォールバック順:
    1. issuerEdinetCode → キャッシュから filerName/secCode を取得
    2. 解決できなければ ("", "")

    Returns:
        (issuer_name, issuer_sec_code)
    """
    issuer_ec = doc.get("issuerEdinetCode") or ""
    if not issuer_ec:
        return ("", "")
    info = edinet_cache.get(issuer_ec, {})
    return (info.get("filerName", ""), info.get("secCode", ""))


def build_message(doc: dict, matched_names: list[str],
                  edinet_cache: dict[str, dict] | None = None,
                  ratio_info: dict | None = None) -> str:
    """Discord通知メッセージを構築する。"""
    filer = doc.get("filerName", "不明")
    sec_code = doc.get("secCode") or ""
    desc = doc.get("docDescription", "")
    submit_dt = doc.get("submitDateTime", "")
    doc_id = doc.get("docID", "")
    names_str = "・".join(matched_names)

    # issuerEdinetCode 経由で発行者情報を補完
    issuer_name = ""
    issuer_sec = ""
    if edinet_cache:
        issuer_name, issuer_sec = _resolve_issuer(doc, edinet_cache)

    # PDF抽出の発行者名・証券コードで追加補完
    if ratio_info:
        if not issuer_name and ratio_info.get("issuer_name"):
            issuer_name = ratio_info["issuer_name"]
        if not sec_code and not issuer_sec and ratio_info.get("sec_code"):
            issuer_sec = ratio_info["sec_code"]

    # 銘柄表示: secCode → issuer_sec → "-"
    display_code = sec_code or issuer_sec or "-"
    # 発行者表示
    issuer_line = ""
    if issuer_name:
        if display_code and display_code != "-":
            issuer_line = f"発行者: {issuer_name}（{display_code}）"
        else:
            issuer_line = f"発行者: {issuer_name}"

    lines = [
        "🔔 **【EDINET 大量保有】**",
        "",
        f"一致: {names_str}",
        f"提出者: {filer}",
    ]
    if issuer_line:
        lines.append(issuer_line)
    lines.append(f"銘柄: {display_code}")

    # 保有比率
    if ratio_info:
        lines.append("")
        lines.append(_fmt_ratio(ratio_info))
        lines.append("")

    lines += [
        f"書類: {desc}",
        f"時刻: {submit_dt}",
        f"docID: {doc_id}",
    ]
    return "\n".join(lines)


# ============================================================
# メインロジック
# ============================================================
def run_once(
    api_key: str,
    webhook_url: str,
    dry_run: bool = False,
    seed_state: bool = False,
    no_ratio: bool = False,
) -> dict:
    """1回分のチェックを実行する。"""
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    state = load_state()
    seen_ids: list[str] = state.get("seen_doc_ids", [])
    seen_set = set(seen_ids)

    stats = {"fetched": 0, "matched": 0, "notified": 0, "skipped": 0}

    # edinetCode キャッシュ読込（過去の蓄積分）
    edinet_cache = _load_edinet_cache()

    for target_date in [today, yesterday]:
        docs = fetch_documents(target_date, api_key)
        stats["fetched"] += len(docs)

        # 全ドキュメントから edinetCode マップをキャッシュにマージ
        _merge_docs_into_cache(edinet_cache, docs)

        for doc in docs:
            doc_id = doc.get("docID", "")

            # Step 1: 大量保有判定
            if not is_target_doc(doc):
                continue

            # Step 2: 人物マッチ
            filer_name = doc.get("filerName", "")
            matched_names = match_person(filer_name)
            if not matched_names:
                continue

            stats["matched"] += 1

            # Step 3: 除外条件
            if not should_notify(doc):
                log.info("  SKIP (訂正/取下げ): docID=%s", doc_id)
                stats["skipped"] += 1
                continue

            # Step 4: 重複排除
            if doc_id in seen_set:
                log.info("  SKIP (既知): docID=%s", doc_id)
                stats["skipped"] += 1
                continue

            # --- ここに到達 = 通知対象 ---
            # 保有比率抽出（PDF取得）
            ratio_info = None
            if not no_ratio and not seed_state:
                log.info("  PDF取得中: docID=%s", doc_id)
                ratio_info = fetch_holding_ratio(doc_id, api_key)
                if ratio_info.get("current") is not None:
                    log.info("  比率: current=%.2f%% previous=%s delta=%s",
                             ratio_info["current"],
                             f"{ratio_info['previous']:.2f}%" if ratio_info.get('previous') is not None else 'N/A',
                             f"{ratio_info['delta']:+.2f}%" if ratio_info.get('delta') is not None else 'N/A')
                else:
                    log.warning("  比率抽出失敗: docID=%s", doc_id)

            msg = build_message(doc, matched_names,
                                edinet_cache=edinet_cache, ratio_info=ratio_info)

            if seed_state:
                # --seed-state: 通知せず state に登録のみ
                log.info("  SEED: docID=%s filer=%s", doc_id, filer_name)
                seen_ids.append(doc_id)
                seen_set.add(doc_id)
                stats["skipped"] += 1
                continue

            if dry_run:
                # --dry-run: 通知せず結果表示のみ
                log.info("  DRY-RUN: docID=%s filer=%s", doc_id, filer_name)
                log.info("  MSG:\n%s", msg)
                stats["matched"] += 0  # already counted
                continue

            # 通知送信
            log.info("  NOTIFY: docID=%s filer=%s → %s", doc_id, filer_name, "・".join(matched_names))
            if send_discord(webhook_url, msg):
                stats["notified"] += 1
                seen_ids.append(doc_id)
                seen_set.add(doc_id)
            else:
                log.error("  通知失敗: docID=%s", doc_id)

    # 状態保存
    state["seen_doc_ids"] = seen_ids
    state["last_checked_at"] = now.isoformat()
    save_state(state)

    # edinetCode キャッシュ保存
    _save_edinet_cache(edinet_cache)

    # サマリログ
    log.info(
        "SUMMARY fetched=%d matched=%d notified=%d skipped=%d",
        stats["fetched"], stats["matched"], stats["notified"], stats["skipped"],
    )
    return stats


# ============================================================
# CLI
# ============================================================
def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="EDINET 大量保有報告書 特定人物監視 → Discord通知",
    )
    parser.add_argument("--loop", action="store_true", help="5分間隔でループ実行")
    parser.add_argument("--once", action="store_true", help="1回だけ実行（デフォルト）")
    parser.add_argument("--test", action="store_true", help="Discordにテスト通知を送信")
    parser.add_argument("--test-api", action="store_true", help="EDINET API接続テスト")
    parser.add_argument("--dry-run", action="store_true", help="通知せず検知結果のみ表示")
    parser.add_argument("--seed-state", action="store_true",
                        help="通知せず既存分をstateに登録（初回導入時用）")
    parser.add_argument("--test-ratio", metavar="DOCID",
                        help="指定docIDのPDFから保有比率を抽出テスト")
    parser.add_argument("--no-ratio", action="store_true",
                        help="保有比率抽出を無効化")
    args = parser.parse_args()

    api_key = os.environ.get("EDINET_API_KEY", "")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    # --- テストモード ---
    if args.test:
        if not webhook_url:
            log.error("DISCORD_WEBHOOK_URL が .env に未設定")
            sys.exit(1)
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        ok = send_discord(webhook_url, f"🔔 EDINET 大量保有監視 - テスト通知 OK ({now})")
        log.info("TEST %s", "OK" if ok else "FAILED")
        sys.exit(0 if ok else 1)

    if args.test_ratio:
        if not api_key:
            log.error("EDINET_API_KEY が .env に未設定")
            sys.exit(1)
        log.info("=== 保有比率抽出テスト: %s ===", args.test_ratio)
        ratio = fetch_holding_ratio(args.test_ratio, api_key)
        log.info("結果: %s", json.dumps(ratio, ensure_ascii=False))
        log.info("表示: %s", _fmt_ratio(ratio))
        sys.exit(0)

    if args.test_api:
        if not api_key:
            log.error("EDINET_API_KEY が .env に未設定")
            sys.exit(1)
        today = datetime.now(JST).strftime("%Y-%m-%d")
        docs = fetch_documents(today, api_key)
        log.info("API TEST: %d 件取得 (date=%s)", len(docs), today)
        holder_docs = [d for d in docs if is_target_doc(d)]
        log.info("  うち大量保有関連: %d 件", len(holder_docs))
        for d in holder_docs[:5]:
            log.info("  docID=%s filer=%s desc=%s",
                     d.get("docID"), d.get("filerName"), d.get("docDescription"))
        sys.exit(0)

    # --- バリデーション ---
    if not api_key:
        log.error("EDINET_API_KEY が .env に未設定")
        sys.exit(1)
    if not webhook_url and not args.dry_run and not args.seed_state:
        log.error("DISCORD_WEBHOOK_URL が .env に未設定")
        sys.exit(1)

    log.info("=== EDINET 大量保有監視 開始 ===")
    log.info("対象人物: %s", ", ".join(TARGET_ALIASES.keys()))
    log.info("モード: %s", "seed-state" if args.seed_state else "dry-run" if args.dry_run else "loop" if args.loop else "once")

    if args.loop:
        while True:
            try:
                run_once(api_key, webhook_url, dry_run=args.dry_run,
                         seed_state=args.seed_state, no_ratio=args.no_ratio)
            except Exception:
                log.exception("実行エラー（次回リトライ）")
            log.info("次回チェック: %d秒後", POLL_INTERVAL_SEC)
            time.sleep(POLL_INTERVAL_SEC)
    else:
        run_once(api_key, webhook_url, dry_run=args.dry_run,
                 seed_state=args.seed_state, no_ratio=args.no_ratio)


if __name__ == "__main__":
    main()
