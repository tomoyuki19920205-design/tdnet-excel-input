#!/usr/bin/env python3
"""find_buyback_candidate_docs.py — buyback 候補 PDF 粗選別ツール

data/docs 配下の PDF を走査し、先頭 1〜2 ページの本文キーワード検索で
自社株買い候補を高速に抽出する。

Usage:
  python tools/find_buyback_candidate_docs.py --input-dir data/docs --output-dir artifacts/buyback_candidates
  python tools/find_buyback_candidate_docs.py --input-dir data/docs --limit 200 --verbose
"""
from __future__ import annotations

import argparse
import csv
import json
import copy
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator, Optional

# project root
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("buyback_candidate_scan")

JST = timezone(timedelta(hours=9))

# ============================================================
# キーワード定義
# ============================================================

STRONG_KEYWORDS: list[str] = [
    "自己株式取得",
    "自己株式の取得",
    "自社株買い",
    "取得状況",
    "自己株式取得状況",
    "自己株式の取得状況",
    "取得結果",
    "自己株式取得結果",
    "自己株式の取得結果",
    "取得終了",
    "自己株式の消却",
    "消却予定日",
    "取得する株式の総数",
    "取得価額の総額",
    "取得期間",
    "取得方法",
    "ToSTNeT",
    "自己株式立会外買付取引",
]

WEAK_KEYWORDS: list[str] = [
    "発行済株式総数",
    "自己株式",
    "市場買付",
    "立会外買付",
    "上限",
    "株式の取得",
]

EXCLUDE_HINTS: list[str] = [
    "新株予約権",
    "転換社債",
    "新株予約権付社債",
    "買入消却",
    "消却見合わせ",
    "自己株式処分",
    "ストックオプション",
]

# スコア定数
STRONG_SCORE = 3
WEAK_SCORE = 1
DECISION_PAIR_BONUS = 4    # 取得する株式の総数 + 取得価額の総額
TOSTNET_BONUS = 2
CANCEL_SHASAI_PENALTY = -4
METADATA_TICKER_BONUS = 1
METADATA_TITLE_BONUS = 1

# priority 閾値
HIGH_PRIORITY_THRESHOLD = 6
MEDIUM_PRIORITY_THRESHOLD = 3


# ============================================================
# ScoringRules: 外部設定ファイル対応
# ============================================================

def build_default_rules() -> dict:
    """デフォルトのスコアリングルールを dict で返す。"""
    return {
        "strong_keywords": {kw: STRONG_SCORE for kw in STRONG_KEYWORDS},
        "weak_keywords": {kw: WEAK_SCORE for kw in WEAK_KEYWORDS},
        "penalty_keywords": {
            "新株予約権": -3,
            "転換社債": -4,
            "新株予約権付社債": -4,
            "買入消却": -4,
            "消却見合わせ": -3,
            "自己株式処分": -4,
            "ストックオプション": -4,
        },
        "pair_bonus": {
            "shares_and_amount": DECISION_PAIR_BONUS,
            "tostnet_bonus": TOSTNET_BONUS,
            "derived_ticker_bonus": METADATA_TICKER_BONUS,
            "derived_title_bonus": METADATA_TITLE_BONUS,
        },
        "priority_thresholds": {
            "high": HIGH_PRIORITY_THRESHOLD,
            "medium": MEDIUM_PRIORITY_THRESHOLD,
        },
    }


def load_scoring_rules(path: str | None = None) -> dict:
    """スコアリングルールを JSON ファイルから読み込む。

    path が None またはファイルが存在しない場合はデフォルトルールを返す。
    JSON にないキーはデフォルト値で補完する。
    """
    defaults = build_default_rules()
    if not path or not os.path.isfile(path):
        return defaults
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        # JSON にあるセクションは完全置換（default の余剰キーを残さない）
        for key in defaults:
            if key in loaded:
                defaults[key] = loaded[key]
        return defaults
    except Exception as e:
        logger.warning(f"ルール読み込み失敗 ({path}): {e} → デフォルト使用")
        return build_default_rules()

# ============================================================
# データモデル
# ============================================================


@dataclass
class KeywordHit:
    keyword: str
    position: int
    strength: str  # "strong" / "weak" / "exclude"


@dataclass
class CandidateRow:
    file_path: str = ""
    file_name: str = ""
    file_size: int = 0
    page_scan_count: int = 0
    text_extract_ok: bool = False
    text_length: int = 0
    matched_keywords: str = ""
    matched_keyword_count: int = 0
    keyword_hit_positions: str = ""
    candidate_score: int = 0
    derived_ticker: str = ""
    derived_disclosure_date: str = ""
    derived_title: str = ""
    text_head_200: str = ""
    review_hint: str = ""
    review_priority: str = "low"
    score_contributions: str = ""


@dataclass
class FailureRow:
    file_path: str = ""
    file_name: str = ""
    stage: str = ""
    error_type: str = ""
    error_message: str = ""
    text_extract_ok: bool = False
    file_size: int = 0


# ============================================================
# PDF ファイル列挙
# ============================================================


def iter_pdf_files(
    input_dir: str,
    recursive: bool = False,
    limit: int | None = None,
    extensions: tuple[str, ...] = (".pdf",),
) -> Iterator[str]:
    """input_dir 配下の PDF ファイルパスを yield する。"""
    count = 0
    if recursive:
        for root, _dirs, files in os.walk(input_dir):
            for f in sorted(files):
                if any(f.lower().endswith(ext) for ext in extensions):
                    if limit and count >= limit:
                        return
                    yield os.path.join(root, f)
                    count += 1
    else:
        for f in sorted(os.listdir(input_dir)):
            if any(f.lower().endswith(ext) for ext in extensions):
                fp = os.path.join(input_dir, f)
                if os.path.isfile(fp):
                    if limit and count >= limit:
                        return
                    yield fp
                    count += 1


# ============================================================
# PDF テキスト抽出
# ============================================================


def extract_pdf_head_text(path: str, max_pages: int = 2) -> tuple[str, int, str]:
    """PDF 先頭 N ページからテキストを抽出する。

    Returns:
        (text, page_count, error_message)
    """
    try:
        import pdfplumber
    except ImportError:
        return "", 0, "pdfplumber not installed"

    try:
        with pdfplumber.open(path) as pdf:
            pages_to_scan = min(len(pdf.pages), max_pages)
            texts = []
            for i in range(pages_to_scan):
                page_text = pdf.pages[i].extract_text() or ""
                texts.append(page_text)
            return "\n".join(texts), pages_to_scan, ""
    except Exception as e:
        return "", 0, f"{type(e).__name__}: {e}"


# ============================================================
# キーワード検索
# ============================================================


def find_keyword_hits(
    text: str,
    strong: list[str] | None = None,
    weak: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[KeywordHit]:
    """テキスト中のキーワードヒットを検出する。"""
    strong = strong or STRONG_KEYWORDS
    weak = weak or WEAK_KEYWORDS
    exclude = exclude or EXCLUDE_HINTS
    hits: list[KeywordHit] = []
    text_lower = text.lower()

    for kw in strong:
        pos = text_lower.find(kw.lower())
        if pos >= 0:
            hits.append(KeywordHit(keyword=kw, position=pos, strength="strong"))

    for kw in weak:
        pos = text_lower.find(kw.lower())
        if pos >= 0:
            # 強キーワードの部分一致で既に含まれていないかチェック
            already = any(h.keyword.lower() in kw.lower() or kw.lower() in h.keyword.lower()
                          for h in hits if h.strength == "strong")
            if not already:
                hits.append(KeywordHit(keyword=kw, position=pos, strength="weak"))

    for kw in exclude:
        pos = text_lower.find(kw.lower())
        if pos >= 0:
            hits.append(KeywordHit(keyword=kw, position=pos, strength="exclude"))

    return hits


# ============================================================
# スコア計算
# ============================================================


def score_candidate(hits: list[KeywordHit], metadata: dict, rules: dict | None = None) -> int:
    """候補スコアを計算する。rules が None ならデフォルト定数を使用 (後方互換)。"""
    total, _ = score_candidate_with_details(hits, metadata, rules)
    return total


def score_candidate_with_details(
    hits: list[KeywordHit],
    metadata: dict,
    rules: dict | None = None,
) -> tuple[int, list[str]]:
    """候補スコアを計算し、各寄与の内訳文字列リストも返す。

    Returns:
        (total_score, contributions)
        contributions: ["自己株式取得:+3", "取得価額の総額:+3", "pair_bonus:+4", ...]
    """
    r = rules or build_default_rules()
    strong_map: dict[str, int] = r.get("strong_keywords", {})
    weak_map: dict[str, int] = r.get("weak_keywords", {})
    penalty_map: dict[str, int] = r.get("penalty_keywords", {})
    pair_bonus = r.get("pair_bonus", {})

    score = 0
    contributions: list[str] = []
    strong_kws: set[str] = set()
    penalty_kws: set[str] = set()

    for h in hits:
        if h.strength == "strong":
            w = strong_map.get(h.keyword, STRONG_SCORE)
            score += w
            strong_kws.add(h.keyword)
            contributions.append(f"{h.keyword}:+{w}")
        elif h.strength == "weak":
            w = weak_map.get(h.keyword, WEAK_SCORE)
            score += w
            contributions.append(f"{h.keyword}:+{w}")
        elif h.strength == "exclude":
            w = penalty_map.get(h.keyword, 0)
            if w != 0:
                score += w
                contributions.append(f"{h.keyword}:{w:+d}")
            penalty_kws.add(h.keyword)

    # decision pair bonus
    has_shares = "取得する株式の総数" in strong_kws
    has_amount = "取得価額の総額" in strong_kws
    sa_bonus = pair_bonus.get("shares_and_amount", DECISION_PAIR_BONUS)
    if has_shares and has_amount:
        score += sa_bonus
        contributions.append(f"pair_bonus:+{sa_bonus}")

    # ToSTNeT bonus
    tostnet_b = pair_bonus.get("tostnet_bonus", TOSTNET_BONUS)
    if any("tostnet" in kw.lower() for kw in strong_kws):
        score += tostnet_b
        contributions.append(f"tostnet_bonus:+{tostnet_b}")

    # cancel + shasai penalty (legacy: penalty_map is more general now)
    has_exclude_shasai = any("社債" in k or "新株予約権" in k for k in penalty_kws)
    if any("消却" in kw for kw in strong_kws) and has_exclude_shasai:
        # Only apply legacy CANCEL_SHASAI_PENALTY if penalty_map didn't already penalize
        if not any(k in penalty_kws for k in penalty_map):
            score += CANCEL_SHASAI_PENALTY
            contributions.append(f"cancel_shasai_penalty:{CANCEL_SHASAI_PENALTY:+d}")

    # metadata bonuses
    tk_bonus = pair_bonus.get("derived_ticker_bonus", METADATA_TICKER_BONUS)
    ti_bonus = pair_bonus.get("derived_title_bonus", METADATA_TITLE_BONUS)
    if metadata.get("derived_ticker"):
        score += tk_bonus
        contributions.append(f"derived_ticker:+{tk_bonus}")
    if metadata.get("derived_title"):
        score += ti_bonus
        contributions.append(f"derived_title:+{ti_bonus}")

    return max(score, 0), contributions


def classify_review_priority(
    score: int,
    thresholds: dict | None = None,
) -> str:
    """review_priority を分類する。thresholds=None ならデフォルト閾値 (後方互換)。"""
    high_th = (thresholds or {}).get("high", HIGH_PRIORITY_THRESHOLD)
    med_th = (thresholds or {}).get("medium", MEDIUM_PRIORITY_THRESHOLD)
    if score >= high_th:
        return "high"
    if score >= med_th:
        return "medium"
    return "low"


# ============================================================
# metadata 補完
# ============================================================


def derive_metadata(text: str) -> dict:
    """PDF 本文先頭からメタデータを補完する。"""
    try:
        from src.events.buyback_extractor import derive_metadata_from_text
        return derive_metadata_from_text(text)
    except ImportError:
        return {"derived_ticker": None, "derived_disclosure_date": None, "derived_title": None}


# ============================================================
# review_hint 生成
# ============================================================


def build_review_hint(hits: list[KeywordHit], metadata: dict) -> str:
    """review_hint を生成する。"""
    hints = []
    strong_kws = {h.keyword for h in hits if h.strength == "strong"}

    if any("取得する株式の総数" in kw or "取得価額の総額" in kw for kw in strong_kws):
        hints.append("decision候補")
    if any("取得状況" in kw for kw in strong_kws):
        hints.append("status候補")
    if any("取得結果" in kw or "取得終了" in kw for kw in strong_kws):
        hints.append("result候補")
    if any("消却" in kw for kw in strong_kws):
        hints.append("cancel候補")
    if any("ToSTNeT" in kw or "tostnet" in kw.lower() for kw in strong_kws):
        hints.append("ToSTNeT")

    if not metadata.get("derived_title"):
        hints.append("title空")
    if not metadata.get("derived_ticker"):
        hints.append("ticker未取得")

    exclude_kws = {h.keyword for h in hits if h.strength == "exclude"}
    if exclude_kws:
        hints.append(f"除外候補:{','.join(exclude_kws)}")

    return "; ".join(hints) if hints else "候補"


# ============================================================
# 候補行構築
# ============================================================


def build_candidate_row(
    path: str,
    text: str,
    page_count: int,
    hits: list[KeywordHit],
    metadata: dict,
    include_head_text: bool = True,
    head_chars: int = 200,
    rules: dict | None = None,
) -> CandidateRow:
    """CandidateRow を構築する。"""
    non_exclude = [h for h in hits if h.strength != "exclude"]
    matched_kws = "|".join(h.keyword for h in non_exclude)
    hit_positions = ", ".join(f"{h.keyword}:{h.position}" for h in hits[:10])

    score, contribs = score_candidate_with_details(hits, metadata, rules)
    thresholds = (rules or {}).get("priority_thresholds")
    priority = classify_review_priority(score, thresholds)

    head_text = ""
    if include_head_text and text:
        head_text = text[:head_chars].replace("\n", " ").replace("\r", " ")

    return CandidateRow(
        file_path=path,
        file_name=os.path.basename(path),
        file_size=os.path.getsize(path) if os.path.exists(path) else 0,
        page_scan_count=page_count,
        text_extract_ok=bool(text),
        text_length=len(text),
        matched_keywords=matched_kws,
        matched_keyword_count=len(non_exclude),
        keyword_hit_positions=hit_positions,
        candidate_score=score,
        derived_ticker=metadata.get("derived_ticker") or "",
        derived_disclosure_date=metadata.get("derived_disclosure_date") or "",
        derived_title=metadata.get("derived_title") or "",
        text_head_200=head_text,
        review_hint=build_review_hint(hits, metadata),
        review_priority=priority,
        score_contributions="|".join(contribs),
    )


# ============================================================
# 出力
# ============================================================

_CSV_COLUMNS = [
    "file_path", "file_name", "file_size", "page_scan_count",
    "text_extract_ok", "text_length", "matched_keywords",
    "matched_keyword_count", "keyword_hit_positions", "candidate_score",
    "derived_ticker", "derived_disclosure_date", "derived_title",
    "text_head_200", "review_hint", "review_priority",
    "score_contributions",
]

_FAILURE_COLUMNS = [
    "file_path", "file_name", "stage", "error_type",
    "error_message", "text_extract_ok", "file_size",
]

_MANIFEST_COLUMNS = [
    "path", "ticker", "title", "disclosure_date",
    "source_doc_id", "source_url",
]


def write_csv_file(path: str, rows: list[dict], columns: list[str]) -> None:
    """CSV ファイルを書き出す。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def write_jsonl_file(path: str, rows: list[dict]) -> None:
    """JSONL ファイルを書き出す。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_manifest(path: str, candidates: list[CandidateRow]) -> None:
    """review ツール用 manifest CSV を出力する。"""
    rows = []
    for c in candidates:
        rows.append({
            "path": c.file_path,
            "ticker": c.derived_ticker,
            "title": c.derived_title,
            "disclosure_date": c.derived_disclosure_date,
            "source_doc_id": "",
            "source_url": "",
        })
    write_csv_file(path, rows, _MANIFEST_COLUMNS)


def write_summary(
    path: str,
    *,
    input_dir: str,
    total_files: int,
    success_count: int,
    failure_count: int,
    candidate_count: int,
    candidates: list[CandidateRow],
    elapsed_sec: float,
) -> None:
    """Markdown サマリを出力する。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    rate = (candidate_count / total_files * 100) if total_files > 0 else 0

    # keyword 集計
    kw_counts: dict[str, int] = {}
    for c in candidates:
        for kw in c.matched_keywords.split("|"):
            kw = kw.strip()
            if kw:
                kw_counts[kw] = kw_counts.get(kw, 0) + 1
    top_kws = sorted(kw_counts.items(), key=lambda x: -x[1])[:10]

    # priority 集計
    priority_counts = {"high": 0, "medium": 0, "low": 0}
    ticker_count = 0
    date_count = 0
    title_count = 0
    for c in candidates:
        priority_counts[c.review_priority] = priority_counts.get(c.review_priority, 0) + 1
        if c.derived_ticker:
            ticker_count += 1
        if c.derived_disclosure_date:
            date_count += 1
        if c.derived_title:
            title_count += 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Buyback Candidate Scan Summary\n\n")
        f.write(f"- **実行時刻**: {now}\n")
        f.write(f"- **input_dir**: `{input_dir}`\n")
        f.write(f"- **所要時間**: {elapsed_sec:.1f}秒\n\n")
        f.write(f"## 集計\n\n")
        f.write(f"| 項目 | 件数 |\n|:---|---:|\n")
        f.write(f"| 対象 PDF | {total_files:,} |\n")
        f.write(f"| テキスト抽出成功 | {success_count:,} |\n")
        f.write(f"| テキスト抽出失敗 | {failure_count:,} |\n")
        f.write(f"| **候補** | **{candidate_count:,}** |\n")
        f.write(f"| 候補率 | {rate:.1f}% |\n\n")
        f.write(f"## review_priority 別\n\n")
        f.write(f"| priority | 件数 |\n|:---|---:|\n")
        for p in ["high", "medium", "low"]:
            f.write(f"| {p} | {priority_counts.get(p, 0):,} |\n")
        f.write(f"\n## metadata 補完\n\n")
        f.write(f"| 項目 | 件数 |\n|:---|---:|\n")
        f.write(f"| derived_ticker | {ticker_count:,} |\n")
        f.write(f"| derived_disclosure_date | {date_count:,} |\n")
        f.write(f"| derived_title | {title_count:,} |\n")
        f.write(f"\n## 上位キーワード\n\n")
        f.write(f"| キーワード | 件数 |\n|:---|---:|\n")
        for kw, cnt in top_kws:
            f.write(f"| {kw} | {cnt:,} |\n")
        f.write(f"\n## 所見\n\n")
        if candidate_count == 0:
            f.write("候補なし。キーワード追加または対象ディレクトリを確認してください。\n")
        else:
            f.write(f"{total_files:,}件中{candidate_count:,}件({rate:.1f}%)が候補。")
            if priority_counts.get("high", 0) > 0:
                f.write(f" うち high priority が {priority_counts['high']:,}件。")
            f.write(" candidate_manifest.csv を review ツールに渡して詳細検証を推奨。\n")


# ============================================================
# メイン処理
# ============================================================


def scan_single_file(
    path: str,
    max_pages: int = 2,
    include_head_text: bool = True,
    head_chars: int = 200,
    min_keyword_hits: int = 1,
    rules: dict | None = None,
) -> tuple[CandidateRow | None, FailureRow | None]:
    """単一ファイルを処理して候補 or 失敗を返す。"""
    file_name = os.path.basename(path)
    file_size = os.path.getsize(path) if os.path.exists(path) else 0

    # 1. テキスト抽出
    text, page_count, err_msg = extract_pdf_head_text(path, max_pages)
    if err_msg:
        return None, FailureRow(
            file_path=path, file_name=file_name,
            stage="extract_text", error_type="pdf_extract_error",
            error_message=err_msg, text_extract_ok=False,
            file_size=file_size,
        )

    if not text.strip():
        return None, FailureRow(
            file_path=path, file_name=file_name,
            stage="extract_text", error_type="empty_text",
            error_message="No text extracted from PDF",
            text_extract_ok=False, file_size=file_size,
        )

    # 2. キーワード検索 (rules から keyword list を導出)
    r = rules or build_default_rules()
    strong_list = list(r.get("strong_keywords", {}).keys())
    weak_list = list(r.get("weak_keywords", {}).keys())
    exclude_list = list(r.get("penalty_keywords", {}).keys())
    hits = find_keyword_hits(text, strong=strong_list, weak=weak_list, exclude=exclude_list)
    non_exclude = [h for h in hits if h.strength != "exclude"]

    if len(non_exclude) < min_keyword_hits:
        return None, None  # 候補でも失敗でもない

    # 3. metadata 補完
    try:
        metadata = derive_metadata(text)
    except Exception:
        metadata = {"derived_ticker": None, "derived_disclosure_date": None, "derived_title": None}

    # 4. 候補行構築
    row = build_candidate_row(
        path, text, page_count, hits, metadata,
        include_head_text=include_head_text,
        head_chars=head_chars,
        rules=rules,
    )

    return row, None


def main(args: list[str] | None = None) -> int:
    """メインエントリポイント。"""
    parser = argparse.ArgumentParser(
        description="buyback 候補 PDF 粗選別ツール",
    )
    parser.add_argument("--input-dir", default="data/docs", help="入力ディレクトリ")
    parser.add_argument("--recursive", action="store_true", help="再帰走査")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限")
    parser.add_argument("--output-dir", default="artifacts/buyback_candidates",
                        help="出力ディレクトリ")
    parser.add_argument("--pages", type=int, default=2, help="スキャンするページ数")
    parser.add_argument("--min-keyword-hits", type=int, default=1, help="最低キーワードヒット数")
    parser.add_argument("--verbose", action="store_true", help="詳細ログ")
    parser.add_argument("--include-head-text", action="store_true", default=True,
                        help="text_head_200 を含める")
    parser.add_argument("--sample-head-chars", type=int, default=200,
                        help="text_head のサンプル文字数")
    parser.add_argument("--manifest-out", default=None,
                        help="manifest CSV の出力パス")
    parser.add_argument("--extensions", default=".pdf", help="対象拡張子（カンマ区切り）")
    parser.add_argument("--rules", default=None,
                        help="スコアリングルール JSON パス (例: configs/buyback_scanner_rules.json)")

    opts = parser.parse_args(args)

    # logging
    level = logging.DEBUG if opts.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # pdfplumber / urllib3 の DEBUG を抑制
    for noisy in ("pdfminer", "pdfplumber", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    extensions = tuple(ext.strip() for ext in opts.extensions.split(","))

    # ファイル列挙
    pdf_files = list(iter_pdf_files(
        opts.input_dir, recursive=opts.recursive,
        limit=opts.limit, extensions=extensions,
    ))
    total = len(pdf_files)
    logger.info(f"対象 PDF: {total:,}件 (input_dir={opts.input_dir})")

    if total == 0:
        logger.warning("対象 PDF がありません。")
        return 1

    # 処理
    candidates: list[CandidateRow] = []
    failures: list[FailureRow] = []
    success_count = 0
    t0 = time.time()

    for i, path in enumerate(pdf_files):
        if (i + 1) % 100 == 0 or (opts.verbose and (i + 1) % 10 == 0):
            logger.info(f"進捗: {i + 1:,}/{total:,}")

        # ルール読み込み (ループ外で1回読めば十分だが、scan_single_fileの引数として渡す)
        if not hasattr(opts, '_loaded_rules'):
            opts._loaded_rules = load_scoring_rules(opts.rules)
        row, failure = scan_single_file(
            path,
            max_pages=opts.pages,
            include_head_text=opts.include_head_text,
            head_chars=opts.sample_head_chars,
            min_keyword_hits=opts.min_keyword_hits,
            rules=opts._loaded_rules,
        )

        if failure:
            failures.append(failure)
            if opts.verbose:
                logger.warning(
                    f"  FAIL: {failure.file_name} [{failure.stage}] {failure.error_message[:80]}"
                )
        elif row:
            candidates.append(row)
            success_count += 1
            if opts.verbose:
                logger.info(
                    f"  HIT: {row.file_name} score={row.candidate_score} "
                    f"priority={row.review_priority} kw={row.matched_keyword_count}"
                )
        else:
            success_count += 1  # テキスト抽出成功だがヒットなし

    elapsed = time.time() - t0

    # 出力
    out_dir = opts.output_dir
    os.makedirs(out_dir, exist_ok=True)

    # 1. CSV
    csv_path = os.path.join(out_dir, "buyback_candidates.csv")
    write_csv_file(csv_path, [asdict(c) for c in candidates], _CSV_COLUMNS)
    logger.info(f"候補 CSV: {csv_path} ({len(candidates):,}件)")

    # 2. JSONL
    jsonl_path = os.path.join(out_dir, "buyback_candidates.jsonl")
    write_jsonl_file(jsonl_path, [asdict(c) for c in candidates])

    # 3. Failures CSV
    fail_path = os.path.join(out_dir, "candidate_failures.csv")
    write_csv_file(fail_path, [asdict(f) for f in failures], _FAILURE_COLUMNS)

    # 4. Summary
    summary_path = os.path.join(out_dir, "candidate_summary.md")
    write_summary(
        summary_path,
        input_dir=opts.input_dir,
        total_files=total,
        success_count=success_count,
        failure_count=len(failures),
        candidate_count=len(candidates),
        candidates=candidates,
        elapsed_sec=elapsed,
    )
    logger.info(f"サマリ: {summary_path}")

    # 5. Manifest
    manifest_path = opts.manifest_out or os.path.join(out_dir, "candidate_manifest.csv")
    write_manifest(manifest_path, candidates)
    logger.info(f"マニフェスト: {manifest_path} ({len(candidates):,}件)")

    # 最終サマリ
    logger.info(
        f"完了: {total:,}件走査, {len(candidates):,}件候補, "
        f"{len(failures):,}件失敗, {elapsed:.1f}秒"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
