#!/usr/bin/env python3
# ============================================================
# edinet_shareholder_watch.py
# ============================================================
# EDINET の有価証券報告書・半期報告書から「大株主の状況」テーブルを
# 抽出し、特定株主が新規掲載された場合のみ Discord Webhook で通知する。
#
# 使い方:
#   python scripts/edinet_shareholder_watch.py                  # 1回実行
#   python scripts/edinet_shareholder_watch.py --loop           # 5分ループ
#   python scripts/edinet_shareholder_watch.py --test DOCID     # 単体抽出テスト
#   python scripts/edinet_shareholder_watch.py --test-discord   # テスト通知
#   python scripts/edinet_shareholder_watch.py --dry-run        # 通知せず表示
#   python scripts/edinet_shareholder_watch.py --seed-state     # seen登録のみ
#   python scripts/edinet_shareholder_watch.py --seed-snapshot  # snapshot登録のみ
#   python scripts/edinet_shareholder_watch.py --date 2025-06-27  # 単日検証
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
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

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
_SEEN_FILE = _STATE_DIR / "edinet_shareholder_seen.json"
_SNAPSHOT_FILE = _STATE_DIR / "shareholder_snapshot_cache.json"
_LOG_DIR = _PROJECT_ROOT / "logs"

JST = timezone(timedelta(hours=9))
EDINET_API_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
EDINET_DOC_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
POLL_INTERVAL_SEC = 300
MAX_SEEN_IDS = 10000
MIN_SHAREHOLDERS_FOR_SUCCESS = 3  # テーブル抽出成功の最低株主数

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("edinet_shareholder")

# ============================================================
# 監視対象株主（エイリアス辞書）
# ============================================================
TARGET_SHAREHOLDER_ALIASES: dict[str, list[str]] = {
    "井村俊哉": ["井村俊哉", "井村 俊哉"],
    "片山晃": ["片山晃", "片山 晃"],
}

# alias の最小長 — false positive 防止
MIN_ALIAS_LENGTH = 3


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
# 状態管理: seen_doc_ids
# ============================================================
def load_seen_state() -> dict:
    if _SEEN_FILE.exists():
        with open(_SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_doc_ids": [], "last_checked_at": None}


def save_seen_state(state: dict):
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    ids = state.get("seen_doc_ids", [])
    if len(ids) > MAX_SEEN_IDS:
        state["seen_doc_ids"] = ids[-MAX_SEEN_IDS:]
    with open(_SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# 状態管理: shareholder_snapshot_cache
# ============================================================
def load_snapshot_cache() -> dict:
    if _SNAPSHOT_FILE.exists():
        with open(_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_snapshot_cache(cache: dict):
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ============================================================
# EDINET API: documents.json
# ============================================================
def fetch_documents(date_str: str, api_key: str) -> list[dict]:
    """指定日の書類一覧を取得する。"""
    params = {"date": date_str, "type": 2, "Subscription-Key": api_key}
    for attempt in range(1, 4):
        try:
            r = requests.get(EDINET_API_URL, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            results = data.get("results") or []
            log.info("  %s: %d 件取得", date_str, len(results))
            return results
        except requests.RequestException as e:
            log.warning("  documents.json 取得失敗 (%d/3): %s", attempt, e)
            if attempt < 3:
                time.sleep(2 ** attempt)
    return []


# ============================================================
# 書類フィルタリング
# ============================================================
def is_target_report_doc(doc: dict) -> bool:
    """有価証券報告書 or 半期報告書を判定（訂正は除外）。"""
    desc = doc.get("docDescription") or ""
    if "訂正" in desc:
        return False
    if "有価証券報告書" in desc:
        return True
    if "半期報告書" in desc:
        return True
    return False


def should_process_doc(doc: dict) -> bool:
    """処理対象の条件チェック。"""
    ws = str(doc.get("withdrawalStatus", "0"))
    ds = str(doc.get("disclosureStatus", "0"))
    if ws != "0" or ds != "0":
        return False
    # xbrlFlag or pdfFlag が必要
    xbrl = str(doc.get("xbrlFlag", "0"))
    pdf = str(doc.get("pdfFlag", "0"))
    if xbrl != "1" and pdf != "1":
        return False
    return True


def get_report_type(doc: dict) -> str:
    """annual / semiannual を判定。"""
    desc = doc.get("docDescription") or ""
    if "半期報告書" in desc:
        return "semiannual"
    return "annual"


# ============================================================
# 書類取得: ZIP (type=1)
# ============================================================
def download_document_zip(doc_id: str, api_key: str, timeout: int = 30) -> bytes | None:
    url = EDINET_DOC_URL.format(doc_id=doc_id)
    params = {"type": 1, "Subscription-Key": api_key}
    for attempt in range(1, 4):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            if r.content[:2] == b'PK':
                return r.content
            log.warning("  ZIP取得: 非ZIP (%d bytes, docID=%s)", len(r.content), doc_id)
            return None
        except requests.RequestException as e:
            log.warning("  ZIP取得失敗 (%d/3, docID=%s): %s", attempt, doc_id, e)
            if attempt < 3:
                time.sleep(2 ** attempt)
    return None


# ============================================================
# 書類取得: PDF (type=2)
# ============================================================
def download_document_pdf(doc_id: str, api_key: str, timeout: int = 30) -> bytes | None:
    url = EDINET_DOC_URL.format(doc_id=doc_id)
    params = {"type": 2, "Subscription-Key": api_key}
    for attempt in range(1, 4):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            if "pdf" in ct.lower() or r.content[:5] == b"%PDF-":
                return r.content
            log.warning("  PDF取得: 非PDF Content-Type=%s (docID=%s)", ct, doc_id)
            return None
        except requests.RequestException as e:
            log.warning("  PDF取得失敗 (%d/3, docID=%s): %s", attempt, doc_id, e)
            if attempt < 3:
                time.sleep(2 ** attempt)
    return None


# ============================================================
# HTML解析: ZIP内から大株主テーブルを抽出
# ============================================================

# 見出しスコア
_HEADING_KEYWORDS = {
    "大株主の状況": 10,
    "主要株主の状況": 5,
}

# 列名スコア
_COLUMN_KEYWORDS = {
    "氏名又は名称": 10,
    "氏名": 8,
    "名称": 6,
    "所有株式数": 8,
    "所有株数": 7,
    "発行済株式": 6,
    "所有株式数の割合": 8,
    "持株比率": 7,
    "所有割合": 6,
    "順位": 3,
}


def extract_html_from_zip(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    """ZIP内の全HTMLファイルを返す: [(filename, content), ...]"""
    result = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                lower = name.lower()
                if any(lower.endswith(ext) for ext in (".htm", ".html", ".xhtml")):
                    try:
                        content = zf.read(name)
                        result.append((name, content))
                    except Exception:
                        pass
    except zipfile.BadZipFile as e:
        log.warning("  ZIP解凍失敗: %s", e)
    return result


def _score_html_for_shareholders(html_bytes: bytes) -> tuple[int, str]:
    """HTMLに大株主テーブルが含まれるかスコアリング。
    Returns: (score, html_text_decoded)
    """
    try:
        # エンコーディング推定
        for enc in ("utf-8", "shift_jis", "euc-jp", "cp932"):
            try:
                text = html_bytes.decode(enc)
                break
            except (UnicodeDecodeError, ValueError):
                continue
        else:
            text = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return (0, "")

    score = 0
    # 見出しスコア
    for kw, pts in _HEADING_KEYWORDS.items():
        if kw in text:
            score += pts

    # 列名スコア（テーブル内に列キーワードがあるか）
    for kw, pts in _COLUMN_KEYWORDS.items():
        if kw in text:
            score += pts

    return (score, text)


def _find_shareholder_table(soup: BeautifulSoup) -> list[dict] | None:
    """見出し「大株主の状況」近傍のテーブルを探して株主リストを返す。"""
    # 見出し要素を探す
    heading_el = None
    for kw in ["大株主の状況", "主要株主の状況"]:
        candidates = soup.find_all(string=re.compile(re.escape(kw)))
        if candidates:
            heading_el = candidates[0]
            break

    if not heading_el:
        return None

    # 見出し後のテーブルを候補として取得
    parent = heading_el.parent if heading_el else None
    if parent is None:
        return None

    tables = parent.find_all_next("table", limit=5)
    if not tables:
        return None

    # 各テーブルをスコアリング
    best_table = None
    best_score = 0

    for tbl in tables:
        tbl_text = tbl.get_text()
        score = 0
        for kw, pts in _COLUMN_KEYWORDS.items():
            if kw in tbl_text:
                score += pts
        rows = tbl.find_all("tr")
        if len(rows) >= 3:  # ヘッダ + 最低2行
            score += 5
        if score > best_score:
            best_score = score
            best_table = tbl

    if best_table is None or best_score < 10:
        return None

    return _parse_table_to_shareholders(best_table)


def _parse_table_to_shareholders(table_el) -> list[dict]:
    """BeautifulSoup table → 株主リスト。"""
    rows = table_el.find_all("tr")
    if len(rows) < 2:
        return []

    # ヘッダ行を探す
    header_cells = rows[0].find_all(["th", "td"])
    headers = [c.get_text(strip=True) for c in header_cells]
    headers_norm = [unicodedata.normalize("NFKC", h) for h in headers]

    # 列インデックスマッピング
    name_col = _find_col(headers_norm, ["氏名又は名称", "氏名", "名称"])
    shares_col = _find_col(headers_norm, ["所有株式数", "所有株数"])
    ratio_col = _find_col(headers_norm, ["発行済株式総数に対する所有株式数の割合", "所有割合", "持株比率", "割合"])
    rank_col = _find_col(headers_norm, ["順位"])

    if name_col is None:
        # ヘッダが取れなかった場合、2行目も試す
        if len(rows) > 1:
            header_cells = rows[1].find_all(["th", "td"])
            headers = [c.get_text(strip=True) for c in header_cells]
            headers_norm = [unicodedata.normalize("NFKC", h) for h in headers]
            name_col = _find_col(headers_norm, ["氏名又は名称", "氏名", "名称"])
            shares_col = _find_col(headers_norm, ["所有株式数", "所有株数"])
            ratio_col = _find_col(headers_norm, ["発行済株式総数に対する所有株式数の割合", "所有割合", "持株比率", "割合"])
            rank_col = _find_col(headers_norm, ["順位"])
            rows = rows[1:]  # ヘッダを再設定

    if name_col is None:
        return []

    result = []
    for row in rows[1:]:
        cells = row.find_all(["th", "td"])
        if len(cells) <= name_col:
            continue
        raw_name = cells[name_col].get_text(strip=True)
        if not raw_name or raw_name in headers:
            continue
        # 合計行をスキップ
        name_norm = normalize_shareholder_name(raw_name)
        if name_norm in ("計", "合計", ""):
            continue

        entry: dict[str, Any] = {
            "name_raw": raw_name,
            "name_norm": name_norm,
            "shares": _safe_int(cells, shares_col),
            "ratio": _safe_float(cells, ratio_col),
            "rank": _safe_int(cells, rank_col),
        }
        result.append(entry)

    return result


def _find_col(headers: list[str], keywords: list[str]) -> int | None:
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i
    return None


def _safe_int(cells: list, col: int | None) -> int | None:
    if col is None or col >= len(cells):
        return None
    txt = cells[col].get_text(strip=True)
    txt = unicodedata.normalize("NFKC", txt)
    txt = re.sub(r"[,、 株口千百万]", "", txt)
    try:
        return int(txt)
    except (ValueError, TypeError):
        return None


def _safe_float(cells: list, col: int | None) -> float | None:
    if col is None or col >= len(cells):
        return None
    txt = cells[col].get_text(strip=True)
    txt = unicodedata.normalize("NFKC", txt)
    txt = re.sub(r"[%％ ]", "", txt)
    try:
        return float(txt)
    except (ValueError, TypeError):
        return None


def parse_shareholders_from_html(html_content: bytes | str) -> list[dict]:
    """HTMLから大株主テーブルを抽出し、株主リストを返す。"""
    if isinstance(html_content, bytes):
        for enc in ("utf-8", "shift_jis", "euc-jp", "cp932"):
            try:
                html_str = html_content.decode(enc)
                break
            except (UnicodeDecodeError, ValueError):
                continue
        else:
            html_str = html_content.decode("utf-8", errors="replace")
    else:
        html_str = html_content

    try:
        soup = BeautifulSoup(html_str, "lxml")
    except Exception:
        soup = BeautifulSoup(html_str, "html.parser")

    result = _find_shareholder_table(soup)
    return result if result else []


# ============================================================
# PDF fallback 解析
# ============================================================
def parse_shareholders_from_pdf(pdf_bytes: bytes) -> list[dict]:
    """PDF fallback: pdfplumber で大株主テーブルを抽出。"""
    if not _HAS_PDFPLUMBER:
        log.warning("  pdfplumber 未インストール")
        return []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # 「大株主の状況」を含むページを探す
            target_pages = []
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                if "大株主の状況" in text:
                    target_pages.append(i)

            if not target_pages:
                return []

            # 該当ページ付近のテーブルを抽出
            shareholders = []
            for pi in target_pages:
                for offset in range(3):  # 当該ページ + 次2ページ
                    idx = pi + offset
                    if idx >= len(pdf.pages):
                        break
                    page = pdf.pages[idx]
                    tables = page.extract_tables() or []
                    for tbl in tables:
                        parsed = _parse_pdf_table(tbl)
                        if parsed:
                            shareholders.extend(parsed)

            return shareholders
    except Exception as e:
        log.warning("  PDF解析失敗: %s", e)
        return []


def _parse_pdf_table(table: list[list[str]]) -> list[dict]:
    """pdfplumber table → 株主リスト。"""
    if len(table) < 2:
        return []

    # ヘッダ行
    headers = [unicodedata.normalize("NFKC", c or "") for c in table[0]]
    name_col = _find_col_str(headers, ["氏名又は名称", "氏名", "名称"])
    if name_col is None:
        return []

    shares_col = _find_col_str(headers, ["所有株式数", "所有株数"])
    ratio_col = _find_col_str(headers, ["所有割合", "持株比率", "割合"])
    rank_col = _find_col_str(headers, ["順位"])

    result = []
    for row in table[1:]:
        if len(row) <= name_col:
            continue
        raw = (row[name_col] or "").strip()
        if not raw:
            continue
        name_norm = normalize_shareholder_name(raw)
        if name_norm in ("計", "合計", ""):
            continue

        shares_val = None
        if shares_col is not None and shares_col < len(row):
            txt = unicodedata.normalize("NFKC", row[shares_col] or "")
            txt = re.sub(r"[,、 株口千百万]", "", txt)
            try:
                shares_val = int(txt)
            except (ValueError, TypeError):
                pass

        ratio_val = None
        if ratio_col is not None and ratio_col < len(row):
            txt = unicodedata.normalize("NFKC", row[ratio_col] or "")
            txt = re.sub(r"[%％ ]", "", txt)
            try:
                ratio_val = float(txt)
            except (ValueError, TypeError):
                pass

        rank_val = None
        if rank_col is not None and rank_col < len(row):
            txt = unicodedata.normalize("NFKC", row[rank_col] or "")
            txt = re.sub(r"[位 ]", "", txt)
            try:
                rank_val = int(txt)
            except (ValueError, TypeError):
                pass

        result.append({
            "name_raw": raw,
            "name_norm": name_norm,
            "shares": shares_val,
            "ratio": ratio_val,
            "rank": rank_val,
        })

    return result


def _find_col_str(headers: list[str], keywords: list[str]) -> int | None:
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i
    return None


# ============================================================
# 株主名正規化
# ============================================================
def normalize_shareholder_name(name: str) -> str:
    """株主名を正規化する。"""
    name = unicodedata.normalize("NFKC", name)
    name = name.replace("\u3000", " ")  # 全角スペース → 半角
    name = re.sub(r"\s+", "", name)     # 空白除去
    name = name.replace("\n", "").replace("\r", "")
    return name.strip()


# ============================================================
# 株主マッチング
# ============================================================
def match_target_shareholders(shareholders: list[dict]) -> dict[str, dict]:
    """株主リストに対して TARGET_SHAREHOLDER_ALIASES をマッチし、
    canonical_name -> match_info の辞書を返す。
    """
    targets: dict[str, dict] = {}
    for canonical, aliases in TARGET_SHAREHOLDER_ALIASES.items():
        match_info: dict[str, Any] = {"matched": False}
        for sh in shareholders:
            name_norm = sh.get("name_norm", "")
            for alias in aliases:
                if len(alias) < MIN_ALIAS_LENGTH:
                    continue
                if alias in name_norm:
                    match_info = {
                        "matched": True,
                        "matched_raw_name": sh.get("name_raw", ""),
                        "rank": sh.get("rank"),
                        "shares": sh.get("shares"),
                        "ratio": sh.get("ratio"),
                    }
                    break
            if match_info.get("matched"):
                break
        targets[canonical] = match_info
    return targets


# ============================================================
# スナップショット比較 — 新規掲載判定
# ============================================================
def compare_with_previous_snapshot(
    issuer_key: str,
    current_targets: dict[str, dict],
    snapshot_cache: dict,
) -> list[str]:
    """新規掲載された target の canonical name リストを返す。"""
    new_targets = []
    prev = snapshot_cache.get(issuer_key, {}).get("targets_present", {})

    for canonical, info in current_targets.items():
        if not info.get("matched"):
            continue
        # 前回のスナップショットに存在しない場合 → 新規掲載
        prev_info = prev.get(canonical, {})
        if not prev_info.get("matched"):
            new_targets.append(canonical)

    return new_targets


# ============================================================
# 抽出成功判定
# ============================================================
def is_extraction_successful(shareholders: list[dict]) -> bool:
    """抽出が成功したかを判定する。
    成功条件:
      - 株主リストが MIN_SHAREHOLDERS_FOR_SUCCESS 件以上
      - 少なくとも1件は name_norm が空でない
    """
    if len(shareholders) < MIN_SHAREHOLDERS_FOR_SUCCESS:
        return False
    valid = [s for s in shareholders if s.get("name_norm")]
    return len(valid) >= MIN_SHAREHOLDERS_FOR_SUCCESS


# ============================================================
# 統合抽出: HTML優先 → PDF fallback
# ============================================================
def extract_shareholders(
    doc_id: str, api_key: str,
    no_html: bool = False, no_pdf: bool = False,
) -> tuple[list[dict], str]:
    """大株主リストを抽出する。
    Returns: (shareholders, extraction_path)
      extraction_path: "html", "pdf", "failed"
    """
    shareholders: list[dict] = []
    path = "failed"

    # === HTML (type=1 ZIP) ===
    if not no_html:
        zip_bytes = download_document_zip(doc_id, api_key)
        if zip_bytes:
            html_files = extract_html_from_zip(zip_bytes)
            log.debug("  ZIP内HTML: %d files", len(html_files))

            # 全HTMLをスコアリング
            scored: list[tuple[int, str, bytes]] = []
            for fname, content in html_files:
                sc, _ = _score_html_for_shareholders(content)
                if sc > 0:
                    scored.append((sc, fname, content))

            # スコア降順でトライ
            scored.sort(key=lambda x: x[0], reverse=True)

            for sc, fname, content in scored[:3]:  # 上位3ファイルまで
                log.debug("  HTML候補: %s (score=%d)", fname, sc)
                result = parse_shareholders_from_html(content)
                if is_extraction_successful(result):
                    shareholders = result
                    path = "html"
                    log.debug("  HTML抽出成功: %s (%d名)", fname, len(result))
                    break

    # === PDF fallback ===
    if path == "failed" and not no_pdf:
        pdf_bytes = download_document_pdf(doc_id, api_key)
        if pdf_bytes:
            result = parse_shareholders_from_pdf(pdf_bytes)
            if is_extraction_successful(result):
                shareholders = result
                path = "pdf"

    return shareholders, path


# ============================================================
# Discord通知
# ============================================================
def send_discord(webhook_url: str, content: str, max_retries: int = 3) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(webhook_url, json={"content": content}, timeout=20)
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            wait = 2 ** attempt
            log.warning("  Discord送信失敗 (%d/%d): %s → %d秒後リトライ",
                        attempt, max_retries, e, wait)
            if attempt < max_retries:
                time.sleep(wait)
    log.error("  Discord送信失敗（リトライ上限）")
    return False


def build_discord_message(
    doc: dict,
    canonical_name: str,
    match_info: dict,
    prev_snapshot: dict | None,
) -> str:
    """新規掲載通知メッセージを構築する。"""
    issuer_name = doc.get("filerName") or "不明"
    sec_code = doc.get("secCode") or "-"
    desc = doc.get("docDescription") or ""
    submit_dt = doc.get("submitDateTime") or ""
    doc_id = doc.get("docID") or ""

    rank = match_info.get("rank")
    shares = match_info.get("shares")
    ratio = match_info.get("ratio")

    lines = [
        "🔔 **【EDINET 株主掲載検知】**",
        "",
        f"一致: {canonical_name}",
        f"発行者: {issuer_name}（{sec_code}）",
        f"書類: {desc}",
        f"時刻: {submit_dt}",
        "種別: 新規掲載",
    ]

    if rank is not None:
        lines.append(f"順位: {rank}位")
    if shares is not None:
        lines.append(f"持株数: {shares:,}株")
    if ratio is not None:
        lines.append(f"持株比率: {ratio:.2f}%")

    # 前回情報
    if prev_snapshot:
        prev_desc = prev_snapshot.get("doc_description") or "不明"
        prev_dt = prev_snapshot.get("last_submit_datetime") or "不明"
        lines.append(f"前回書類: {prev_desc}")
        lines.append(f"前回時刻: {prev_dt}")
    else:
        lines.append("前回書類: 初回スナップショット比較不能")

    lines.append(f"docID: {doc_id}")
    return "\n".join(lines)


# ============================================================
# メイン処理
# ============================================================
def run_once(
    api_key: str,
    webhook_url: str,
    dry_run: bool = False,
    seed_state: bool = False,
    seed_snapshot: bool = False,
    no_html: bool = False,
    no_pdf: bool = False,
    target_date: str | None = None,
    limit: int | None = None,
) -> dict:
    """1回分のチェックを実行する。"""
    now = datetime.now(JST)

    if target_date:
        dates = [target_date]
    else:
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        dates = [today, yesterday]

    state = load_seen_state()
    seen_ids: list[str] = state.get("seen_doc_ids", [])
    seen_set = set(seen_ids)

    snapshot_cache = load_snapshot_cache()

    stats = {
        "fetched_docs": 0, "candidate_docs": 0,
        "parsed_html_ok": 0, "parsed_pdf_ok": 0, "parsed_failed": 0,
        "target_hits": 0, "new_target_hits": 0,
        "notified_count": 0, "skipped_seen": 0, "skipped_status": 0,
        "no_prev_snapshot": 0, "errors": 0,
    }
    processed_count = 0

    for date_str in dates:
        docs = fetch_documents(date_str, api_key)
        stats["fetched_docs"] += len(docs)

        for doc in docs:
            doc_id = doc.get("docID", "")

            # フィルタ: 有報/半期報
            if not is_target_report_doc(doc):
                continue

            stats["candidate_docs"] += 1

            # ステータス除外
            if not should_process_doc(doc):
                stats["skipped_status"] += 1
                continue

            # 重複排除（seed-snapshot時はスキップしない）
            if doc_id in seen_set and not seed_snapshot:
                stats["skipped_seen"] += 1
                continue

            # --- limit チェック ---
            if limit is not None and processed_count >= limit:
                log.info("  LIMIT到達 (%d件): 残りスキップ", limit)
                break

            # --- 対象書類 ---
            issuer_name = doc.get("filerName") or ""
            issuer_edinet = doc.get("edinetCode") or doc.get("issuerEdinetCode") or ""
            sec_code = doc.get("secCode") or ""
            report_type = get_report_type(doc)
            issuer_key = issuer_edinet or doc_id  # fallback
            processed_count += 1

            try:
                # 抽出
                if seed_state:
                    # --seed-state: 抽出しない
                    log.info("  SEED-STATE: docID=%s issuer=%s", doc_id, issuer_name)
                    seen_ids.append(doc_id)
                    seen_set.add(doc_id)
                    continue

                shareholders, extraction_path = extract_shareholders(
                    doc_id, api_key, no_html=no_html, no_pdf=no_pdf,
                )

                # 統計更新
                if extraction_path == "html":
                    stats["parsed_html_ok"] += 1
                elif extraction_path == "pdf":
                    stats["parsed_pdf_ok"] += 1
                else:
                    stats["parsed_failed"] += 1

                # マッチング
                current_targets = match_target_shareholders(shareholders)
                matched_names = [c for c, i in current_targets.items() if i.get("matched")]

                # docごとのログ（notify判定も含める）
                has_prev = issuer_key in snapshot_cache
                log.info(
                    "  DOC docID=%s issuer=%s extraction=%s shareholders=%d "
                    "matched=%s prev_snapshot=%s",
                    doc_id, issuer_name, extraction_path,
                    len(shareholders), ",".join(matched_names) or "none",
                    "yes" if has_prev else "no",
                )

                if matched_names:
                    stats["target_hits"] += 1

                # 抽出成功判定
                extraction_ok = is_extraction_successful(shareholders)

                if extraction_ok:
                    # 比較元 snapshot 有無チェック
                    has_prev_snapshot = issuer_key in snapshot_cache
                    if not has_prev_snapshot:
                        stats["no_prev_snapshot"] += 1
                        log.info("  NO-PREV-SNAPSHOT: issuer=%s key=%s → 初回snapshot作成のみ",
                                 issuer_name, issuer_key)

                    # 新規掲載判定
                    new_targets = compare_with_previous_snapshot(
                        issuer_key, current_targets, snapshot_cache,
                    )

                    if new_targets:
                        stats["new_target_hits"] += len(new_targets)

                    # seed-snapshot モード
                    if seed_snapshot:
                        log.info("  SEED-SNAPSHOT: docID=%s targets=%s prev_snapshot=%s",
                                 doc_id, ",".join(matched_names) or "none",
                                 "yes" if has_prev_snapshot else "NO(initial)")
                    elif dry_run:
                        for nt in new_targets:
                            mi = current_targets[nt]
                            log.info("  DRY-RUN NOTIFY: %s @ %s (rank=%s ratio=%s)",
                                     nt, issuer_name, mi.get("rank"), mi.get("ratio"))
                    else:
                        # 通知送信
                        for nt in new_targets:
                            mi = current_targets[nt]
                            prev = snapshot_cache.get(issuer_key)
                            msg = build_discord_message(doc, nt, mi, prev)
                            log.info("  NOTIFY: %s @ %s → Discord",
                                     nt, issuer_name)
                            if send_discord(webhook_url, msg):
                                stats["notified_count"] += 1
                            else:
                                stats["errors"] += 1

                    # snapshot 更新（抽出成功時のみ）
                    snapshot_cache[issuer_key] = {
                        "issuer_name": issuer_name,
                        "sec_code": sec_code,
                        "last_doc_id": doc_id,
                        "last_submit_datetime": doc.get("submitDateTime", ""),
                        "report_type": report_type,
                        "targets_present": current_targets,
                        "all_shareholders": [
                            {"rank": s.get("rank"), "name_norm": s.get("name_norm"),
                             "shares": s.get("shares"), "ratio": s.get("ratio")}
                            for s in shareholders
                        ],
                    }
                else:
                    log.info("  抽出不十分 (shareholders=%d, extraction=%s): snapshot未更新",
                             len(shareholders), extraction_path)

                # docID は抽出成否に関わらず seen に追加
                seen_ids.append(doc_id)
                seen_set.add(doc_id)

            except Exception as e:
                log.exception("  処理エラー (docID=%s): %s", doc_id, e)
                stats["errors"] += 1
                # エラーでも seen に追加（無限再処理防止）
                seen_ids.append(doc_id)
                seen_set.add(doc_id)

    # 状態保存
    state["seen_doc_ids"] = seen_ids
    state["last_checked_at"] = now.isoformat()
    save_seen_state(state)
    save_snapshot_cache(snapshot_cache)

    # サマリログ
    log.info(
        "SUMMARY fetched=%d candidate=%d processed=%d html_ok=%d pdf_ok=%d "
        "failed=%d target_hits=%d new_hits=%d notified=%d "
        "no_prev_snapshot=%d skipped_seen=%d skipped_status=%d errors=%d",
        stats["fetched_docs"], stats["candidate_docs"], processed_count,
        stats["parsed_html_ok"], stats["parsed_pdf_ok"], stats["parsed_failed"],
        stats["target_hits"], stats["new_target_hits"],
        stats["notified_count"], stats["no_prev_snapshot"],
        stats["skipped_seen"], stats["skipped_status"], stats["errors"],
    )
    return stats


# ============================================================
# テスト: 単体docID
# ============================================================
def test_extract(doc_id: str, api_key: str, no_html: bool = False, no_pdf: bool = False):
    """指定 docID で大株主抽出テストを実行する。"""
    log.info("=== 大株主抽出テスト: %s ===", doc_id)
    shareholders, path = extract_shareholders(
        doc_id, api_key, no_html=no_html, no_pdf=no_pdf,
    )
    log.info("抽出パス: %s", path)
    log.info("株主数: %d", len(shareholders))
    log.info("抽出成功: %s", is_extraction_successful(shareholders))

    for i, sh in enumerate(shareholders, 1):
        log.info("  %2d. %s  shares=%s  ratio=%s",
                 i, sh.get("name_norm", "?"),
                 sh.get("shares"), sh.get("ratio"))

    targets = match_target_shareholders(shareholders)
    matched = [c for c, i in targets.items() if i.get("matched")]
    log.info("マッチ: %s", ", ".join(matched) if matched else "なし")
    for c, i in targets.items():
        log.info("  %s: %s", c, json.dumps(i, ensure_ascii=False))


# ============================================================
# CLI
# ============================================================
def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="EDINET 有報・半期報 大株主監視 → Discord通知",
    )
    parser.add_argument("--loop", action="store_true", help="5分間隔でループ実行")
    parser.add_argument("--once", action="store_true", help="1回だけ実行（デフォルト）")
    parser.add_argument("--test", metavar="DOCID", help="指定docIDで大株主抽出テスト")
    parser.add_argument("--test-discord", action="store_true", help="テスト通知送信")
    parser.add_argument("--dry-run", action="store_true", help="通知せず結果のみ表示")
    parser.add_argument("--seed-state", action="store_true",
                        help="通知せず既存docIDをseenに登録（初回導入用）")
    parser.add_argument("--seed-snapshot", action="store_true",
                        help="通知せずsnapshot登録のみ（初回導入用）")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="単日検証用")
    parser.add_argument("--no-html", action="store_true", help="HTML解析を無効化しPDF fallback強制")
    parser.add_argument("--no-pdf", action="store_true", help="PDF fallbackを無効化")
    parser.add_argument("--limit", type=int, metavar="N", help="処理する書類数の上限")
    args = parser.parse_args()

    api_key = os.environ.get("EDINET_API_KEY", "")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    # --- テストモード ---
    if args.test_discord:
        if not webhook_url:
            log.error("DISCORD_WEBHOOK_URL が .env に未設定")
            sys.exit(1)
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        ok = send_discord(webhook_url,
                          f"🔔 EDINET 株主欄監視 - テスト通知 OK ({now_str})")
        log.info("TEST %s", "OK" if ok else "FAILED")
        sys.exit(0 if ok else 1)

    if args.test:
        if not api_key:
            log.error("EDINET_API_KEY が .env に未設定")
            sys.exit(1)
        test_extract(args.test, api_key, no_html=args.no_html, no_pdf=args.no_pdf)
        sys.exit(0)

    # --- バリデーション ---
    if not api_key:
        log.error("EDINET_API_KEY が .env に未設定")
        sys.exit(1)
    if not webhook_url and not args.dry_run and not args.seed_state and not args.seed_snapshot:
        log.error("DISCORD_WEBHOOK_URL が .env に未設定")
        sys.exit(1)

    log.info("=== EDINET 株主欄監視 開始 ===")
    log.info("対象株主: %s", ", ".join(TARGET_SHAREHOLDER_ALIASES.keys()))
    mode_str = ("seed-state" if args.seed_state
                else "seed-snapshot" if args.seed_snapshot
                else "dry-run" if args.dry_run
                else "loop" if args.loop else "once")
    log.info("モード: %s", mode_str)

    if args.loop:
        while True:
            try:
                run_once(api_key, webhook_url,
                         dry_run=args.dry_run,
                         seed_state=args.seed_state,
                         seed_snapshot=args.seed_snapshot,
                         no_html=args.no_html, no_pdf=args.no_pdf,
                         target_date=args.date, limit=args.limit)
            except Exception:
                log.exception("実行エラー（次回リトライ）")
            log.info("次回チェック: %d秒後", POLL_INTERVAL_SEC)
            time.sleep(POLL_INTERVAL_SEC)
    else:
        run_once(api_key, webhook_url,
                 dry_run=args.dry_run,
                 seed_state=args.seed_state,
                 seed_snapshot=args.seed_snapshot,
                 no_html=args.no_html, no_pdf=args.no_pdf,
                 target_date=args.date, limit=args.limit)


if __name__ == "__main__":
    main()
