#!/usr/bin/env python3
"""review_buyback_extraction.py — 自社株買い抽出エンジンの実データ一括検証ツール

実データを一括走査し、classifier / extractor の結果を
CSV / JSONL / Markdown サマリで出力する。

Usage:
  cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
  python tools/review_buyback_extraction.py --input-dir data/docs --recursive \\
      --output-dir artifacts/buyback_review --min-confidence 0.60
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.events.buyback_classifier import classify_buyback
from src.events.buyback_extractor import extract_buyback_event, derive_metadata_from_text
from src.events.buyback_models import (
    BUYBACK_DECISION, BUYBACK_STATUS, BUYBACK_RESULT, TREASURY_CANCEL,
)

logger = logging.getLogger("buyback_review")
JST = timezone(timedelta(hours=9))

# ============================================================
# 定数
# ============================================================
SUPPORTED_EXTS = {".html", ".htm", ".pdf", ".txt"}

# event_type ごとの必須フィールド
_REQUIRED_FIELDS = {
    BUYBACK_DECISION: ["shares_limit", "amount_limit_million_yen", "start_date", "end_date"],
    BUYBACK_STATUS: ["shares_acquired", "amount_acquired_million_yen"],
    BUYBACK_RESULT: ["shares_acquired", "amount_acquired_million_yen"],
    TREASURY_CANCEL: ["shares_cancelled", "cancel_date"],
}

# 抽出可能フィールド一覧
_ALL_EXTRACTED_FIELDS = [
    "shares_limit", "shares_acquired", "shares_cancelled",
    "amount_limit_million_yen", "amount_acquired_million_yen",
    "ratio_to_outstanding",
    "start_date", "end_date", "cancel_date",
    "acquisition_method", "board_resolution_date", "status_period_label",
]

# bucket 判定用の主要フィールド（補助フィールドだけでは high_confidence にしない）
_CORE_EXTRACTED_FIELDS = [
    "shares_limit", "shares_acquired", "shares_cancelled",
    "amount_limit_million_yen", "amount_acquired_million_yen",
    "start_date", "end_date", "cancel_date",
    "ratio_to_outstanding",
]

# manifest 由来列
_MANIFEST_COLUMNS = [
    "manifest_candidate_score", "manifest_review_priority",
    "manifest_matched_keywords", "manifest_matched_keyword_count",
    "manifest_ticker", "manifest_title", "manifest_disclosure_date",
]

# CSV 出力カラム
_CSV_COLUMNS = [
    "file_path", "file_type", "file_name",
    "ticker", "disclosure_date", "title", "source_doc_id", "source_url",
    "text_extract_ok", "text_length", "title_detected",
    "is_buyback_related", "event_type_candidate",
    "classification_confidence", "extraction_confidence", "confidence_final",
    "matched_keywords", "exclusion_reason",
    "event_type",
    "shares_limit", "shares_acquired", "shares_cancelled",
    "amount_limit_million_yen", "amount_acquired_million_yen",
    "ratio_to_outstanding",
    "start_date", "end_date", "cancel_date",
    "acquisition_method", "board_resolution_date", "status_period_label",
    "extracted_fields_count", "missing_key_fields",
    "raw_method_text", "raw_amount_text", "raw_shares_text", "raw_period_text",
    "review_bucket", "review_notes",
] + _MANIFEST_COLUMNS

_LOW_CONF_COLUMNS = [
    "file_path", "ticker", "title", "event_type_candidate",
    "confidence_final", "missing_key_fields", "matched_keywords", "review_notes",
    "manifest_candidate_score", "manifest_review_priority",
]

_FAILURE_COLUMNS = [
    "file_path", "file_type", "ticker", "title",
    "stage", "error_type", "error_message",
    "text_extract_ok", "text_length",
]

_MANIFEST_RESOLUTION_FAILURE_COLUMNS = [
    "path_raw", "resolved_path", "failure_reason", "stage", "manifest_row_number",
]


# ============================================================
# テキスト読み込み
# ============================================================
def load_text_from_file(path: str, file_type: str) -> tuple[bool, str, str]:
    """ファイルからテキストを抽出する。

    Returns: (success, text, error_message)
    """
    try:
        if file_type == "pdf":
            return _load_pdf(path)
        elif file_type in ("html", "htm"):
            return _load_html(path)
        else:
            return _load_text(path)
    except Exception as e:
        return False, "", str(e)


def _load_html(path: str) -> tuple[bool, str, str]:
    from bs4 import BeautifulSoup
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    soup = BeautifulSoup(html, "html.parser")
    # script/style 除去
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    if not text.strip():
        return False, "", "HTML text extraction yielded empty text"
    return True, text, ""


def _load_pdf(path: str) -> tuple[bool, str, str]:
    try:
        import pdfplumber
    except ImportError:
        return False, "", "pdfplumber not installed"
    try:
        pages = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
        text = "\n".join(pages)
        if not text.strip():
            return False, "", "PDF text extraction yielded empty text"
        return True, text, ""
    except Exception as e:
        return False, "", f"PDF open failed: {e}"


def _load_text(path: str) -> tuple[bool, str, str]:
    for enc in ("utf-8", "cp932", "shift_jis"):
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read()
            if text.strip():
                return True, text, ""
        except (UnicodeDecodeError, LookupError):
            continue
    return False, "", "Text file read failed (encoding)"


# ============================================================
# タイトル推定
# ============================================================
def detect_title_from_html(path: str) -> str:
    """HTML の <title> や <h1> からタイトルを推定"""
    try:
        from bs4 import BeautifulSoup
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
        soup = BeautifulSoup(html, "html.parser")
        # <title>
        if soup.title and soup.title.string:
            return soup.title.string.strip()[:200]
        # <h1>
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)[:200]
        # 最初の見出し
        for tag in ("h2", "h3", "h4"):
            el = soup.find(tag)
            if el:
                return el.get_text(strip=True)[:200]
    except Exception:
        pass
    return ""


# ============================================================
# ファイル名からのメタ推定
# ============================================================
_TICKER_RE = re.compile(r"(\d{4})")
_DATE_RE = re.compile(r"((?:19|20)\d{2})[_\-]?(\d{2})[_\-]?(\d{2})")


def guess_metadata_from_filename(filename: str) -> dict:
    """ファイル名から ticker / disclosure_date を緩く推定"""
    result: dict = {"ticker": "", "disclosure_date": "", "title_guess": ""}
    stem = Path(filename).stem

    # date を先に抽出（日付部分を除いてから ticker を探す）
    date_match = _DATE_RE.search(stem)
    date_span = ""
    if date_match:
        result["disclosure_date"] = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        date_span = date_match.group(0)

    # ticker: 日付部分を除いた残りから4桁数字を探す
    stem_without_date = stem.replace(date_span, "") if date_span else stem
    ticker_match = _TICKER_RE.search(stem_without_date)
    if ticker_match:
        result["ticker"] = ticker_match.group(1)

    # title-like (underscore/hyphen の残り部分)
    cleaned = re.sub(r"\d{4,8}", "", stem)
    cleaned = re.sub(r"[_\-]+", " ", cleaned).strip()
    if cleaned:
        result["title_guess"] = cleaned

    return result


# ============================================================
# manifest 読み込み
# ============================================================
def load_manifest(manifest_path: str) -> tuple[dict[str, dict], list[dict]]:
    """manifest CSV を読み込み、(path→row辞書, 全行リスト) を返す。

    候補 manifest の全列を保持する。
    """
    index: dict[str, dict] = {}
    all_rows: list[dict] = []
    if not manifest_path or not os.path.exists(manifest_path):
        return index, all_rows
    with open(manifest_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            row["_manifest_row_number"] = i
            all_rows.append(row)
            p = row.get("path", "").strip()
            if p:
                index[p] = row
    return index, all_rows


# ============================================================
# manifest path 解決
# ============================================================
def resolve_manifest_path(
    path_value: str,
    input_dir: str | None = None,
    manifest_path: str | None = None,
) -> str | None:
    """manifest の path 値を実ファイルパスに解決する。

    解決順:
        1. 絶対パスならそのまま
        2. input_dir / path
        3. manifest ファイルの親 / path
        4. project root / path

    Returns: 解決済みパス or None
    """
    if not path_value:
        return None

    # 1. 絶対パス
    if os.path.isabs(path_value):
        if os.path.exists(path_value):
            return path_value
        return None

    # 2. input_dir 基準
    if input_dir:
        candidate = os.path.join(input_dir, path_value)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
        # basename match
        candidate_base = os.path.join(input_dir, os.path.basename(path_value))
        if os.path.exists(candidate_base):
            return os.path.abspath(candidate_base)

    # 3. manifest ファイルの親
    if manifest_path:
        manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
        candidate = os.path.join(manifest_dir, path_value)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    # 4. project root
    candidate = os.path.join(_PROJECT_ROOT, path_value)
    if os.path.exists(candidate):
        return os.path.abspath(candidate)

    return None


# ============================================================
# manifest から対象ファイルリストを構築
# ============================================================
def build_files_from_manifest(
    manifest_rows: list[dict],
    input_dir: str | None = None,
    manifest_path: str | None = None,
    limit: int = 0,
) -> tuple[list[str], list[dict], dict[str, dict]]:
    """manifest の path を解決し、(files, failures, path_to_mrow) を返す。"""
    files: list[str] = []
    failures: list[dict] = []
    path_map: dict[str, dict] = {}  # resolved_path -> manifest row

    for mrow in manifest_rows:
        if limit > 0 and len(files) >= limit:
            break
        path_raw = mrow.get("path", "").strip()
        row_num = mrow.get("_manifest_row_number", 0)
        resolved = resolve_manifest_path(path_raw, input_dir, manifest_path)
        if resolved:
            files.append(resolved)
            path_map[resolved] = mrow
        else:
            failures.append({
                "path_raw": path_raw,
                "resolved_path": "",
                "failure_reason": "file_not_found",
                "stage": "manifest_path_resolve",
                "manifest_row_number": row_num,
            })

    return files, failures, path_map


# ============================================================
# メタデータ解決
# ============================================================
def _find_manifest_row(
    file_path: str,
    manifest_index: dict[str, dict],
) -> dict | None:
    """manifest dict から file_path に対応する行を見つける。"""
    # 直接一致
    mrow = manifest_index.get(file_path)
    if mrow:
        return mrow
    # basename 一致
    basename = os.path.basename(file_path)
    mrow = manifest_index.get(basename)
    if mrow:
        return mrow
    # 部分一致
    for k, v in manifest_index.items():
        if k in file_path or file_path.endswith(k):
            return v
    return None


def resolve_metadata(
    file_path: str,
    manifest: dict[str, dict],
    defaults: dict,
) -> dict:
    """メタデータを解決する。

    優先順位:
        1. manifest の ticker / title / disclosure_date
        2. manifest の derived_* 列
        3. ファイル名推定
        4. default オプション
    """
    meta = {
        "ticker": defaults.get("ticker", ""),
        "disclosure_date": defaults.get("disclosure_date", ""),
        "title": defaults.get("title", ""),
        "source_doc_id": None,
        "source_url": None,
        # manifest 由来列（raw）
        "manifest_candidate_score": "",
        "manifest_review_priority": "",
        "manifest_matched_keywords": "",
        "manifest_matched_keyword_count": "",
        "manifest_ticker": "",
        "manifest_title": "",
        "manifest_disclosure_date": "",
    }

    # ファイル名推定
    fn_meta = guess_metadata_from_filename(os.path.basename(file_path))
    if fn_meta.get("ticker"):
        meta["ticker"] = fn_meta["ticker"]
    if fn_meta.get("disclosure_date"):
        meta["disclosure_date"] = fn_meta["disclosure_date"]

    # manifest 検索
    mrow = _find_manifest_row(file_path, manifest)

    if mrow:
        # manifest 由来列を raw 保持
        meta["manifest_candidate_score"] = mrow.get("candidate_score", "")
        meta["manifest_review_priority"] = mrow.get("review_priority", "")
        meta["manifest_matched_keywords"] = mrow.get("matched_keywords", "")
        meta["manifest_matched_keyword_count"] = mrow.get("matched_keyword_count", "")
        meta["manifest_ticker"] = mrow.get("ticker", "")
        meta["manifest_title"] = mrow.get("title", "")
        meta["manifest_disclosure_date"] = mrow.get("disclosure_date", "")

        # 優先順位 1: manifest の直接列
        if mrow.get("ticker"):
            meta["ticker"] = mrow["ticker"]
        if mrow.get("disclosure_date"):
            meta["disclosure_date"] = mrow["disclosure_date"]
        if mrow.get("title"):
            meta["title"] = mrow["title"]
        if mrow.get("source_doc_id"):
            meta["source_doc_id"] = mrow["source_doc_id"]
        if mrow.get("source_url"):
            meta["source_url"] = mrow["source_url"]

        # 優先順位 2: derived_* フォールバック
        if not meta["ticker"] and mrow.get("derived_ticker"):
            meta["ticker"] = mrow["derived_ticker"]
        if not meta["disclosure_date"] and mrow.get("derived_disclosure_date"):
            meta["disclosure_date"] = mrow["derived_disclosure_date"]
        if not meta["title"] and mrow.get("derived_title"):
            meta["title"] = mrow["derived_title"]

    return meta


# ============================================================
# 入力ファイル列挙
# ============================================================
def iter_input_files(
    input_dir: str,
    globs: list[str] | None = None,
    recursive: bool = False,
    limit: int = 0,
) -> list[str]:
    """指定ディレクトリからファイルを列挙"""
    if globs is None:
        globs = [f"*{ext}" for ext in SUPPORTED_EXTS]

    files: list[str] = []
    input_path = Path(input_dir)
    if not input_path.exists():
        logger.warning(f"入力ディレクトリが見つかりません: {input_dir}")
        return files

    for pattern in globs:
        if recursive:
            found = list(input_path.rglob(pattern))
        else:
            found = list(input_path.glob(pattern))
        files.extend(str(f) for f in found if f.is_file())

    # 重複除去・ソート
    files = sorted(set(files))
    if limit > 0:
        files = files[:limit]
    return files


# ============================================================
# review_bucket / missing_key_fields / extracted_fields_count
# ============================================================
def compute_extracted_fields_count(event_dict: dict) -> int:
    """抽出済みフィールド数（全フィールド）"""
    return sum(1 for f in _ALL_EXTRACTED_FIELDS if event_dict.get(f) is not None)


def compute_core_fields_count(event_dict: dict) -> int:
    """主要抽出フィールド数（bucket判定用）"""
    return sum(1 for f in _CORE_EXTRACTED_FIELDS if event_dict.get(f) is not None)


def compute_missing_key_fields(event_type: str, event_dict: dict) -> list[str]:
    """event_type ごとの必須フィールドのうち欠けているもの"""
    required = _REQUIRED_FIELDS.get(event_type, [])
    return [f for f in required if event_dict.get(f) is None]


def classify_review_bucket(
    text_ok: bool,
    is_buyback: bool,
    exclusion_reason: str,
    confidence_final: float,
    min_confidence: float,
    core_fields_count: int,
    extractor_error: bool,
) -> str:
    """review_bucket を分類

    core_fields_count は shares/amount/period/cancel_date 等の
    主要フィールド数。board_resolution_date 等の補助フィールドだけでは
    high_confidence_extracted にしない。
    """
    if not text_ok:
        return "text_extract_failed"
    if exclusion_reason:
        return "excluded"
    if not is_buyback:
        return "non_buyback"
    if extractor_error:
        return "extraction_failed"
    if core_fields_count == 0:
        return "classifier_only"
    if confidence_final < min_confidence:
        return "low_confidence"
    return "high_confidence_extracted"


# ============================================================
# 単一ファイル処理
# ============================================================
def run_buyback_review_for_file(
    file_path: str,
    meta: dict,
    min_confidence: float = 0.60,
) -> tuple[dict, dict | None]:
    """1ファイルの buyback レビューを実行。

    Returns: (review_row, failure_row or None)
    """
    file_type = Path(file_path).suffix.lstrip(".").lower()
    file_name = os.path.basename(file_path)

    row: dict = {
        "file_path": file_path,
        "file_type": file_type,
        "file_name": file_name,
        "ticker": meta.get("ticker", ""),
        "disclosure_date": meta.get("disclosure_date", ""),
        "title": meta.get("title", ""),
        "source_doc_id": meta.get("source_doc_id"),
        "source_url": meta.get("source_url"),
        "text_extract_ok": False,
        "text_length": 0,
        "title_detected": "",
        "is_buyback_related": False,
        "event_type_candidate": "",
        "classification_confidence": 0.0,
        "extraction_confidence": 0.0,
        "confidence_final": 0.0,
        "matched_keywords": "",
        "exclusion_reason": "",
        "event_type": "",
        "extracted_fields_count": 0,
        "missing_key_fields": "",
        "review_bucket": "",
        "review_notes": "",
    }
    for f in _ALL_EXTRACTED_FIELDS:
        row[f] = None
    row["raw_method_text"] = ""
    row["raw_amount_text"] = ""
    row["raw_shares_text"] = ""
    row["raw_period_text"] = ""

    failure: dict | None = None

    # 1. テキスト取得
    text_ok, text, err_msg = load_text_from_file(file_path, file_type)
    row["text_extract_ok"] = text_ok
    row["text_length"] = len(text) if text else 0

    if not text_ok:
        row["review_bucket"] = "text_extract_failed"
        row["review_notes"] = err_msg
        failure = {
            "file_path": file_path, "file_type": file_type,
            "ticker": row["ticker"], "title": row["title"],
            "stage": "load_text", "error_type": "text_extract_failed",
            "error_message": err_msg,
            "text_extract_ok": False, "text_length": 0,
        }
        return row, failure

    # 2. タイトル推定
    if not row["title"] and file_type in ("html", "htm"):
        detected = detect_title_from_html(file_path)
        if detected:
            row["title_detected"] = detected
            row["title"] = detected

    # 2b. PDF 本文先頭から metadata 補完
    derived = derive_metadata_from_text(text)
    if derived.get("derived_ticker") and not row["ticker"]:
        row["ticker"] = derived["derived_ticker"]
    if derived.get("derived_disclosure_date") and not row["disclosure_date"]:
        row["disclosure_date"] = derived["derived_disclosure_date"]
    if derived.get("derived_title") and not row["title"]:
        row["title"] = derived["derived_title"]
        row["title_detected"] = derived["derived_title"]

    # 3. 分類
    try:
        cls_result = classify_buyback(row["title"], text[:2000])
        row["is_buyback_related"] = cls_result.is_buyback_related
        row["event_type_candidate"] = cls_result.event_type_candidate
        row["classification_confidence"] = cls_result.confidence
        row["matched_keywords"] = "|".join(cls_result.matched_keywords)

        # 除外理由
        for kw in cls_result.matched_keywords:
            if kw.startswith("EXCLUDED:") or kw.startswith("COND_EXCLUDED:"):
                row["exclusion_reason"] = kw
                break
    except Exception as e:
        row["review_bucket"] = "extraction_failed"
        row["review_notes"] = f"Classify error: {e}"
        failure = {
            "file_path": file_path, "file_type": file_type,
            "ticker": row["ticker"], "title": row["title"],
            "stage": "classify", "error_type": type(e).__name__,
            "error_message": str(e),
            "text_extract_ok": True, "text_length": len(text),
        }
        return row, failure

    # 4. 抽出
    extractor_error = False
    if cls_result.is_buyback_related and cls_result.event_type_candidate:
        try:
            event = extract_buyback_event(
                text=text,
                event_type=cls_result.event_type_candidate,
                ticker=row["ticker"],
                disclosure_date=row["disclosure_date"],
                title=row["title"],
                source_type=file_type,
                source_path=file_path,
                source_doc_id=row.get("source_doc_id"),
                source_url=row.get("source_url"),
            )
            row["event_type"] = event.event_type
            row["extraction_confidence"] = event.extraction_confidence

            for f in _ALL_EXTRACTED_FIELDS:
                row[f] = getattr(event, f, None)

            # extracted_json から raw snippets 取得
            try:
                ej = json.loads(event.extracted_json) if event.extracted_json else {}
                row["raw_method_text"] = ej.get("raw_method_text", "")
                row["raw_amount_text"] = ej.get("raw_amount_text", "")
                row["raw_shares_text"] = ej.get("raw_shares_text", "")
                row["raw_period_text"] = ej.get("raw_period_text", "")
            except (json.JSONDecodeError, TypeError):
                pass

        except Exception as e:
            extractor_error = True
            row["review_notes"] = f"Extract error: {e}"
            failure = {
                "file_path": file_path, "file_type": file_type,
                "ticker": row["ticker"], "title": row["title"],
                "stage": "extract", "error_type": type(e).__name__,
                "error_message": str(e),
                "text_extract_ok": True, "text_length": len(text),
            }

    # 5. confidence_final
    if not row["is_buyback_related"]:
        row["confidence_final"] = row["classification_confidence"]
    else:
        row["confidence_final"] = max(
            row["classification_confidence"],
            row["extraction_confidence"],
        )

    # 6. extracted_fields_count / missing_key_fields
    row["extracted_fields_count"] = compute_extracted_fields_count(row)
    if row["event_type"]:
        missing = compute_missing_key_fields(row["event_type"], row)
        row["missing_key_fields"] = "|".join(missing)

    # 7. review_bucket
    core_count = compute_core_fields_count(row)
    row["review_bucket"] = classify_review_bucket(
        text_ok=text_ok,
        is_buyback=row["is_buyback_related"],
        exclusion_reason=row["exclusion_reason"],
        confidence_final=row["confidence_final"],
        min_confidence=min_confidence,
        core_fields_count=core_count,
        extractor_error=extractor_error,
    )

    return row, failure


# ============================================================
# 出力
# ============================================================
def write_csv(path: str, rows: list[dict], columns: list[str]) -> None:
    """CSV 出力"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    logger.info(f"CSV 出力: {path} ({len(rows)} rows)")


def write_jsonl(path: str, rows: list[dict]) -> None:
    """JSONL 出力"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    logger.info(f"JSONL 出力: {path} ({len(rows)} rows)")


def generate_review_summary(
    rows: list[dict],
    failures: list[dict],
    input_dir: str,
    recursive: bool,
    min_confidence: float,
    *,
    manifest_path: str = "",
    manifest_total: int = 0,
    manifest_resolved: int = 0,
    manifest_resolve_failures: int = 0,
) -> str:
    """Markdown サマリ生成"""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    total = len(rows)

    # ファイルタイプ別件数
    type_counter = Counter(r["file_type"] for r in rows)
    text_ok = sum(1 for r in rows if r["text_extract_ok"])
    text_fail = total - text_ok

    # buyback 分類
    buyback_count = sum(1 for r in rows if r["is_buyback_related"])
    non_buyback = sum(1 for r in rows if not r["is_buyback_related"] and not r["exclusion_reason"])
    excluded = sum(1 for r in rows if r["exclusion_reason"])

    # event_type
    etype_counter = Counter(r["event_type"] for r in rows if r["event_type"])

    # confidence
    high_conf = sum(1 for r in rows if r["confidence_final"] >= min_confidence and r["is_buyback_related"])
    low_conf = sum(1 for r in rows if r["confidence_final"] < min_confidence and r["is_buyback_related"])

    # review_bucket
    bucket_counter = Counter(r["review_bucket"] for r in rows)

    # 抽出率
    field_stats: dict[str, int] = {}
    buyback_rows = [r for r in rows if r["is_buyback_related"] and r["event_type"]]
    for f in _ALL_EXTRACTED_FIELDS:
        field_stats[f] = sum(1 for r in buyback_rows if r.get(f) is not None)

    # missing_key_fields
    all_missing: list[str] = []
    for r in buyback_rows:
        if r["missing_key_fields"]:
            all_missing.extend(r["missing_key_fields"].split("|"))
    missing_counter = Counter(all_missing)

    lines = [
        "# 自社株買い抽出エンジン — 実データ検証サマリ",
        "",
        "## 実行情報",
        "| 項目 | 値 |",
        "|:---|:---|",
        f"| 実行時刻 | {now} |",
        f"| input_dir | {input_dir or '(manifest のみ)'} |",
        f"| recursive | {recursive} |",
        f"| min_confidence | {min_confidence} |",
    ]

    # manifest 情報
    if manifest_path:
        lines.extend([
            f"| manifest | `{manifest_path}` |",
            f"| manifest 行数 | {manifest_total:,} |",
            f"| manifest 解決成功 | {manifest_resolved:,} |",
            f"| manifest 解決失敗 | {manifest_resolve_failures:,} |",
        ])

    lines.extend([
        "",
        "## ファイル集計",
        "| 指標 | 件数 |",
        "|:---|---:|",
        f"| 対象ファイル数 | {total:,} |",
    ])
    for ft, cnt in sorted(type_counter.items()):
        lines.append(f"| └ {ft} | {cnt:,} |")
    lines.extend([
        f"| テキスト取得成功 | {text_ok:,} |",
        f"| テキスト取得失敗 | {text_fail:,} |",
        "",
        "## 分類集計",
        "| 指標 | 件数 |",
        "|:---|---:|",
        f"| buyback_related | {buyback_count:,} |",
        f"| non_buyback | {non_buyback:,} |",
        f"| excluded | {excluded:,} |",
        "",
    ])

    if etype_counter:
        lines.append("## event_type 別件数")
        lines.append("| event_type | 件数 |")
        lines.append("|:---|---:|")
        for et, cnt in etype_counter.most_common():
            lines.append(f"| {et} | {cnt:,} |")
        lines.append("")

    lines.extend([
        "## confidence 分布",
        "| 区分 | 件数 |",
        "|:---|---:|",
        f"| high (>= {min_confidence}) | {high_conf:,} |",
        f"| low (< {min_confidence}) | {low_conf:,} |",
        "",
        "## review_bucket 別件数",
        "| bucket | 件数 |",
        "|:---|---:|",
    ])
    for bkt, cnt in bucket_counter.most_common():
        lines.append(f"| {bkt} | {cnt:,} |")
    lines.append("")

    # manifest review_priority vs review_bucket 集計
    if manifest_path:
        lines.append("## manifest 連携集計")
        m_pri_counter = Counter(
            r.get("manifest_review_priority", "") for r in rows
            if r.get("manifest_review_priority")
        )
        if m_pri_counter:
            lines.append("### manifest_review_priority 別件数")
            lines.append("| priority | 件数 |")
            lines.append("|:---|---:|")
            for p in ["high", "medium", "low"]:
                lines.append(f"| {p} | {m_pri_counter.get(p, 0):,} |")
            lines.append("")

        # medium/high のうち実際に buyback_related になった件数
        m_high_med = [r for r in rows if r.get("manifest_review_priority") in ("high", "medium")]
        if m_high_med:
            bb_in_hm = sum(1 for r in m_high_med if r["is_buyback_related"])
            hce_in_hm = sum(1 for r in m_high_med if r["review_bucket"] == "high_confidence_extracted")
            lines.append("### manifest high/medium → review 結果")
            lines.append("| 指標 | 件数 |")
            lines.append("|:---|---:|")
            lines.append(f"| manifest high/medium 合計 | {len(m_high_med):,} |")
            lines.append(f"| → buyback_related | {bb_in_hm:,} |")
            lines.append(f"| → high_confidence_extracted | {hce_in_hm:,} |")
            lines.append("")

        # candidate_score 簡易分布
        scores = []
        for r in rows:
            s = r.get("manifest_candidate_score", "")
            try:
                scores.append(int(s))
            except (ValueError, TypeError):
                pass
        if scores:
            lines.append("### candidate_score 分布")
            lines.append("| 区分 | 件数 |")
            lines.append("|:---|---:|")
            lines.append(f"| score >= 6 | {sum(1 for s in scores if s >= 6):,} |")
            lines.append(f"| score 3-5 | {sum(1 for s in scores if 3 <= s < 6):,} |")
            lines.append(f"| score < 3 | {sum(1 for s in scores if s < 3):,} |")
            lines.append("")

    if buyback_rows:
        n = len(buyback_rows)
        lines.append("## 主要抽出項目の抽出率")
        lines.append("| フィールド | 抽出件数 | 抽出率 |")
        lines.append("|:---|---:|---:|")
        for f in _ALL_EXTRACTED_FIELDS:
            cnt = field_stats.get(f, 0)
            rate = cnt / n * 100 if n else 0
            lines.append(f"| {f} | {cnt:,} | {rate:.1f}% |")
        lines.append("")

    if missing_counter:
        lines.append("## 頻出 missing_key_fields")
        lines.append("| フィールド | 欠損件数 |")
        lines.append("|:---|---:|")
        for f, cnt in missing_counter.most_common(10):
            lines.append(f"| {f} | {cnt:,} |")
        lines.append("")

    if failures:
        lines.append("## extraction_failures")
        lines.append(f"合計 {len(failures):,} 件のエラーが発生しました。")
        stage_counter = Counter(f["stage"] for f in failures)
        lines.append("| stage | 件数 |")
        lines.append("|:---|---:|")
        for s, cnt in stage_counter.most_common():
            lines.append(f"| {s} | {cnt:,} |")
        lines.append("")

    # 所見
    lines.append("## 所見")
    findings = []
    if manifest_path:
        findings.append(f"- manifest 由来の候補 {manifest_resolved:,} 件を review")
    if text_fail > 0:
        findings.append(f"- テキスト取得失敗が {text_fail:,} 件あります（PDF画像系の可能性）")
    if low_conf > 0:
        findings.append(f"- 低 confidence の buyback 文書が {low_conf:,} 件あります（手レビュー推奨）")
    if missing_counter:
        top_missing = missing_counter.most_common(1)[0]
        findings.append(f"- 最頻出の欠損フィールドは `{top_missing[0]}` ({top_missing[1]:,} 件)")
    if buyback_count == 0 and total > 0:
        findings.append("- buyback 関連文書が検出されませんでした")
    if not findings:
        findings.append("- 特記事項なし")
    lines.extend(findings)
    lines.append("")

    return "\n".join(lines)


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="自社株買い抽出エンジン — 実データ一括検証ツール"
    )
    parser.add_argument("--input-dir", default="", help="入力ディレクトリ")
    parser.add_argument("--glob", action="append", dest="globs",
                        help="ファイル検索パターン (複数指定可)")
    parser.add_argument("--recursive", action="store_true", help="サブディレクトリも検索")
    parser.add_argument("--limit", type=int, default=0, help="処理上限")
    parser.add_argument("--output-dir", default="artifacts/buyback_review",
                        help="出力ディレクトリ")
    parser.add_argument("--manifest", default="", help="manifest CSV パス")
    parser.add_argument("--only-manifest-files", action="store_true",
                        help="manifest にあるファイルだけ処理する")
    parser.add_argument("--min-confidence", type=float, default=0.60,
                        help="low confidence 閾値")
    parser.add_argument("--only-buyback-related", action="store_true",
                        help="buyback 関連のみ出力")
    parser.add_argument("--include-non-buyback", action="store_true",
                        help="non-buyback も全出力 (default)")
    parser.add_argument("--default-ticker", default="", help="デフォルト ticker")
    parser.add_argument("--default-disclosure-date", default="",
                        help="デフォルト開示日")
    parser.add_argument("--default-title", default="", help="デフォルトタイトル")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("pdfminer", "pdfplumber", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # manifest 読み込み
    manifest_index: dict[str, dict] = {}
    manifest_all_rows: list[dict] = []
    manifest_resolve_failures: list[dict] = []
    manifest_total = 0
    manifest_resolved = 0

    if args.manifest:
        manifest_index, manifest_all_rows = load_manifest(args.manifest)
        manifest_total = len(manifest_all_rows)
        logger.info(f"manifest: {manifest_total:,} エントリ")

    # 対象ファイル決定
    if args.only_manifest_files and args.manifest:
        # manifest のみ
        files, manifest_resolve_failures, path_to_mrow = build_files_from_manifest(
            manifest_all_rows,
            input_dir=args.input_dir or None,
            manifest_path=args.manifest,
            limit=args.limit,
        )
        manifest_resolved = len(files)
        # path_to_mrow の情報を manifest_index にマージ
        for rpath, mrow in path_to_mrow.items():
            manifest_index[rpath] = mrow
        if manifest_resolve_failures:
            logger.warning(
                f"manifest path 解決失敗: {len(manifest_resolve_failures):,} 件"
            )
    elif args.input_dir:
        # input-dir 走査
        files = iter_input_files(
            args.input_dir,
            globs=args.globs,
            recursive=args.recursive,
            limit=args.limit,
        )
        manifest_resolved = len(manifest_index)
    else:
        print("--input-dir または --manifest --only-manifest-files を指定してください。")
        sys.exit(1)

    logger.info(f"対象ファイル: {len(files):,} 件")

    if not files:
        print("対象ファイルが見つかりませんでした。")
        sys.exit(0)

    defaults = {
        "ticker": args.default_ticker,
        "disclosure_date": args.default_disclosure_date,
        "title": args.default_title,
    }

    # 処理
    all_rows: list[dict] = []
    failures: list[dict] = []

    for i, fpath in enumerate(files, 1):
        if i % 50 == 0 or i == len(files):
            logger.info(f"[{i}/{len(files)}] 処理中...")

        meta = resolve_metadata(fpath, manifest_index, defaults)
        row, fail = run_buyback_review_for_file(
            fpath, meta, min_confidence=args.min_confidence,
        )
        # manifest 由来列を row に反映
        for mc in _MANIFEST_COLUMNS:
            row[mc] = meta.get(mc, "")
        all_rows.append(row)
        if fail:
            failures.append(fail)

    # フィルタ
    output_rows = all_rows
    if args.only_buyback_related:
        output_rows = [r for r in all_rows if r["is_buyback_related"]]

    # 出力
    out_dir = args.output_dir
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(_PROJECT_ROOT, out_dir)
    os.makedirs(out_dir, exist_ok=True)

    write_csv(os.path.join(out_dir, "review_buyback_results.csv"),
              output_rows, _CSV_COLUMNS)
    write_jsonl(os.path.join(out_dir, "review_buyback_results.jsonl"),
                output_rows)

    # low confidence
    low_conf_rows = [
        r for r in all_rows
        if r["is_buyback_related"] and (
            r["confidence_final"] < args.min_confidence
            or (r["missing_key_fields"] and r["event_type"])
        )
    ]
    write_csv(os.path.join(out_dir, "review_low_confidence.csv"),
              low_conf_rows, _LOW_CONF_COLUMNS)

    # failures (extraction)
    write_csv(os.path.join(out_dir, "review_extraction_failures.csv"),
              failures, _FAILURE_COLUMNS)

    # manifest path 解決失敗
    if manifest_resolve_failures:
        write_csv(
            os.path.join(out_dir, "review_manifest_resolution_failures.csv"),
            manifest_resolve_failures,
            _MANIFEST_RESOLUTION_FAILURE_COLUMNS,
        )

    # summary
    summary = generate_review_summary(
        all_rows, failures,
        args.input_dir, args.recursive, args.min_confidence,
        manifest_path=args.manifest,
        manifest_total=manifest_total,
        manifest_resolved=manifest_resolved,
        manifest_resolve_failures=len(manifest_resolve_failures),
    )
    summary_path = os.path.join(out_dir, "review_summary.md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)
    logger.info(f"サマリ出力: {summary_path}")

    # コンソール出力
    total = len(all_rows)
    buyback = sum(1 for r in all_rows if r["is_buyback_related"])
    print(f"\n検証完了: {total:,} ファイル")
    print(f"  buyback_related: {buyback:,}")
    print(f"  non_buyback: {total - buyback:,}")
    print(f"  failures: {len(failures):,}")
    if manifest_resolve_failures:
        print(f"  manifest path 解決失敗: {len(manifest_resolve_failures):,}")
    print(f"  出力先: {out_dir}")


if __name__ == "__main__":
    main()
