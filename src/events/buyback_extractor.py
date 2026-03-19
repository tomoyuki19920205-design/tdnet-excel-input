#!/usr/bin/env python3
"""buyback_extractor.py — 自社株買い関連テキストからの値抽出

HTML / PDF / テキストから以下を抽出する:
- 株数 (shares_limit / shares_acquired / shares_cancelled)
- 金額 (amount_limit / amount_acquired)  → 百万円単位で保存
- 日付 (start_date / end_date / board_resolution_date / cancel_date)
- 期間 (start_date + end_date)
- 比率 (ratio_to_outstanding)
- 取得方法 (acquisition_method)
- ステータス期間ラベル (status_period_label)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Optional

from .buyback_models import (
    BuybackEvent,
    BUYBACK_DECISION,
    BUYBACK_STATUS,
    BUYBACK_RESULT,
    TREASURY_CANCEL,
    EXTRACTOR_VERSION,
)

logger = logging.getLogger("buyback_extractor")


# ============================================================
# 全角→半角 変換
# ============================================================
_ZEN_TO_HAN = str.maketrans(
    "０１２３４５６７８９．，　",
    "0123456789., ",
)


def normalize_jp_number(text: str) -> str:
    """全角数字→半角、カンマ除去、全角スペース→半角"""
    s = text.translate(_ZEN_TO_HAN)
    s = s.replace(",", "").replace("，", "")
    return s.strip()


# ============================================================
# 株数正規化
# ============================================================
_SHARE_RE = re.compile(
    r"([\d,.]+)\s*(?:(億|万|千))?\s*株",
)

_UNIT_MULTI = {"億": 100_000_000, "万": 10_000, "千": 1_000}


def normalize_share_count(text: str) -> Optional[int]:
    """テキストから株数を抽出して int で返す。

    対応:
    - 3,000,000株
    - 300万株
    - 300.5万株
    - 3,000千株
    """
    s = normalize_jp_number(text)
    m = _SHARE_RE.search(s)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    unit = m.group(2) or ""
    try:
        num = float(num_str)
    except ValueError:
        return None
    multiplier = _UNIT_MULTI.get(unit, 1)
    return int(num * multiplier)


# ============================================================
# 金額正規化 → 百万円単位
# ============================================================
_AMOUNT_RE = re.compile(
    r"([\d,.]+)\s*(?:(億|百万|万|千))?\s*円",
)

_AMOUNT_TO_MILLION = {
    "億":   100,       # 1億 = 100百万
    "百万": 1,         # 百万 = 百万
    "万":   0.01,      # 1万 = 0.01百万
    "千":   0.001,     # 1千 = 0.001百万
}


def normalize_amount_to_million_yen(text: str) -> Optional[float]:
    """テキストから金額を抽出し、百万円単位で返す。

    対応:
    - 50億円 → 5000.0
    - 50.5億円 → 5050.0
    - 1,200百万円 → 1200.0
    - 3,450,000,000円 → 3450.0
    """
    s = normalize_jp_number(text)
    m = _AMOUNT_RE.search(s)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    unit = m.group(2) or ""
    try:
        num = float(num_str)
    except ValueError:
        return None

    if unit:
        multiplier = _AMOUNT_TO_MILLION.get(unit, 1)
        return round(num * multiplier, 2)
    else:
        # 円単位 → 百万円に変換
        return round(num / 1_000_000, 2)


# ============================================================
# 日付正規化
# ============================================================
_JP_DATE_RE = re.compile(
    r"(?:令和|平成|昭和)?\s*(\d{1,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_SLASH_DATE_RE = re.compile(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})")

# 和暦→西暦
_ERA_OFFSET = {"令和": 2018, "平成": 1988, "昭和": 1925}
_ERA_RE = re.compile(r"(令和|平成|昭和)\s*(\d{1,2})")


def normalize_jp_date(text: str) -> Optional[str]:
    """日本語日付 → YYYY-MM-DD。

    対応:
    - 2025年4月1日
    - 令和7年4月1日
    - 2025/04/01
    - 2025-04-01
    """
    s = normalize_jp_number(text)

    # 和暦をチェック
    era_m = _ERA_RE.search(s)
    era_offset = 0
    if era_m:
        era_name = era_m.group(1)
        era_year = int(era_m.group(2))
        era_offset = _ERA_OFFSET.get(era_name, 0)
        # 和暦年を西暦年に変換してテキストを置換
        western = era_offset + era_year
        # era_m.end() 直後に「年」があればスキップ
        rest_start = era_m.end()
        if rest_start < len(s) and s[rest_start] == "年":
            rest_start += 1
        s = s[:era_m.start()] + str(western) + "年" + s[rest_start:]

    # 年月日パターン
    m = _JP_DATE_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            # 2桁年は西暦に変換済みのはず
            pass
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # スラッシュ/ハイフン
    m = _SLASH_DATE_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    return None


# ============================================================
# 期間正規化
# ============================================================
_PERIOD_KARA_RE = re.compile(
    r"(.{8,30}?)(?:から|～|〜|-)(.{8,30}?)(?:まで|$)",
)
_PERIOD_JI_RE = re.compile(
    r"自\s*(.{8,20}?)\s*至\s*(.{8,20})",
)
# 翌営業日パターン
_PERIOD_YOKU_RE = re.compile(
    r"翌営業日.{0,10}?(.{8,30}?)(?:から|～|〜|-)(.{8,30}?)(?:まで|$)",
)


def normalize_period(text: str) -> tuple[Optional[str], Optional[str]]:
    """期間テキスト → (start_date, end_date)。

    対応:
    - 2025年4月1日から2025年9月30日まで
    - 自 2025年4月1日 至 2025年9月30日
    - 翌営業日から2025年9月30日まで

    説明用の日付候補（売出価格等決定日 3/4〜3/9）は優先しない。
    「から〜まで」「自〜至」形式を優先して抽出する。
    """
    # 「自〜至」パターン（最優先）
    m = _PERIOD_JI_RE.search(text)
    if m:
        start = normalize_jp_date(m.group(1))
        end = normalize_jp_date(m.group(2))
        if start and end:
            return start, end

    # 「から〜まで」パターン
    m = _PERIOD_KARA_RE.search(text)
    if m:
        start = normalize_jp_date(m.group(1))
        end = normalize_jp_date(m.group(2))
        if start and end:
            return start, end

    # start のみ / end のみの fallback
    m = _PERIOD_JI_RE.search(text)
    if m:
        start = normalize_jp_date(m.group(1))
        end = normalize_jp_date(m.group(2))
        if start or end:
            return start, end
    m = _PERIOD_KARA_RE.search(text)
    if m:
        start = normalize_jp_date(m.group(1))
        end = normalize_jp_date(m.group(2))
        if start or end:
            return start, end

    return None, None


# ============================================================
# 比率正規化
# ============================================================
_PERCENT_RE = re.compile(r"([\d.]+)\s*[%％]")


def normalize_percent(text: str) -> Optional[float]:
    """パーセント値を抽出。2.35% → 2.35"""
    s = normalize_jp_number(text)
    m = _PERCENT_RE.search(s)
    if m:
        try:
            return round(float(m.group(1)), 4)
        except ValueError:
            pass
    return None


# ============================================================
# 取得方法正規化
# ============================================================
_METHOD_MAP = [
    ("tostnet",           ["ToSTNeT", "tostnet", "立会外買付取引"]),
    ("off_auction",       ["立会外取引"]),
    ("market_purchase",   ["市場買付", "市場買い付け", "東京証券取引所"]),
]


def normalize_method(text: str) -> Optional[str]:
    """取得方法テキスト → 正規化コード"""
    for code, keywords in _METHOD_MAP:
        for kw in keywords:
            if kw.lower() in text.lower():
                return code
    return "other" if text.strip() else None


# ============================================================
# テキストハッシュ
# ============================================================
def compute_text_hash(text: str) -> str:
    """テキストの SHA-256 先頭16文字"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ============================================================
# アンカーベース値抽出
# ============================================================
def _find_near_anchor(text: str, anchors: list[str], window: int = 200) -> str:
    """アンカーキーワードの近傍テキストを取得"""
    for anchor in anchors:
        idx = text.find(anchor)
        if idx >= 0:
            start = idx
            end = min(len(text), idx + len(anchor) + window)
            return text[start:end]
    return ""


def _extract_shares(text: str, anchors: list[str]) -> tuple[Optional[int], str]:
    """アンカー近傍から株数を抽出"""
    snippet = _find_near_anchor(text, anchors)
    if not snippet:
        return None, ""
    val = normalize_share_count(snippet)
    return val, snippet


def _extract_amount(text: str, anchors: list[str]) -> tuple[Optional[float], str]:
    """アンカー近傍から金額を抽出"""
    snippet = _find_near_anchor(text, anchors)
    if not snippet:
        return None, ""
    val = normalize_amount_to_million_yen(snippet)
    return val, snippet


def _extract_date(text: str, anchors: list[str]) -> tuple[Optional[str], str]:
    """アンカー近傍から日付を抽出"""
    snippet = _find_near_anchor(text, anchors)
    if not snippet:
        return None, ""
    val = normalize_jp_date(snippet)
    return val, snippet


def _extract_ratio(text: str) -> tuple[Optional[float], str]:
    """比率を抽出"""
    anchors = [
        "発行済株式総数",
        "発行済株式",
        "割合",
    ]
    snippet = _find_near_anchor(text, anchors)
    if not snippet:
        return None, ""
    val = normalize_percent(snippet)
    return val, snippet


def _extract_method(text: str) -> tuple[Optional[str], str]:
    """取得方法を抽出"""
    anchors = ["取得方法", "取得の方法", "買付方法"]
    snippet = _find_near_anchor(text, anchors, window=150)
    if not snippet:
        return None, ""
    val = normalize_method(snippet)
    return val, snippet


def _extract_period(text: str) -> tuple[Optional[str], Optional[str], str]:
    """取得期間を抽出"""
    anchors = ["取得期間", "取得の期間"]
    snippet = _find_near_anchor(text, anchors, window=300)
    if not snippet:
        return None, None, ""
    start, end = normalize_period(snippet)
    return start, end, snippet


def _extract_status_period_label(text: str) -> Optional[str]:
    """ステータス期間ラベルを抽出（例: 2025年4月度）"""
    m = re.search(r"(\d{4}年\d{1,2}月[度分]?)", text)
    if m:
        return m.group(1)
    return None


# ============================================================
# メイン抽出関数
# ============================================================
def extract_buyback_event(
    text: str,
    event_type: str,
    ticker: str = "",
    disclosure_date: str = "",
    title: str = "",
    source_type: str = "",
    source_path: str = "",
    source_doc_id: str | None = None,
    source_url: str | None = None,
) -> BuybackEvent:
    """テキストから BuybackEvent を抽出する。

    Parameters
    ----------
    text : str
        本文テキスト
    event_type : str
        検出済みの event_type
    """
    raw_snippets: dict = {}
    confidence = 0.40  # event_type 確定済み

    event = BuybackEvent(
        ticker=ticker,
        disclosure_date=disclosure_date,
        event_type=event_type,
        title=title,
        source_type=source_type,
        source_path=source_path,
        source_doc_id=source_doc_id,
        source_url=source_url,
        raw_text_hash=compute_text_hash(text),
        extractor_version=EXTRACTOR_VERSION,
    )

    # ------ event_type 別の抽出 ------
    if event_type == BUYBACK_DECISION:
        # 株数上限
        shares, snip = _extract_shares(text, [
            "取得し得る株式の総数", "取得株式の種類及び数",
            "取得する株式の総数", "取得する株式の数",
            "株式の総数",
        ])
        if shares:
            event.shares_limit = shares
            raw_snippets["raw_shares_text"] = snip
            confidence += 0.15

        # 金額上限
        amt, snip = _extract_amount(text, [
            "取得価額の総額", "取得し得る株式の総額",
            "取得に要する資金", "取得総額",
        ])
        if amt:
            event.amount_limit_million_yen = amt
            raw_snippets["raw_amount_text"] = snip
            confidence += 0.15

        # 取得期間
        start, end, snip = _extract_period(text)
        if start or end:
            event.start_date = start
            event.end_date = end
            raw_snippets["raw_period_text"] = snip
            confidence += 0.10

        # 取得方法
        method, snip = _extract_method(text)
        if method:
            event.acquisition_method = method
            raw_snippets["raw_method_text"] = snip
            confidence += 0.05

        # 取締役会決議日
        res_date, snip = _extract_date(text, [
            "取締役会決議", "決議日", "取締役会において決議",
        ])
        if res_date:
            event.board_resolution_date = res_date

        # 比率
        ratio, snip = _extract_ratio(text)
        if ratio:
            event.ratio_to_outstanding = ratio
            raw_snippets["raw_ratio_text"] = snip

    elif event_type == BUYBACK_STATUS:
        # 取得株数
        shares, snip = _extract_shares(text, [
            "取得した株式の数", "取得株式数", "買付株式数",
            "当月の取得", "取得した株式",
        ])
        if shares:
            event.shares_acquired = shares
            raw_snippets["raw_shares_text"] = snip
            confidence += 0.15

        # 取得金額
        amt, snip = _extract_amount(text, [
            "取得価額の総額", "取得金額", "買付金額",
            "当月の取得価額",
        ])
        if amt:
            event.amount_acquired_million_yen = amt
            raw_snippets["raw_amount_text"] = snip
            confidence += 0.15

        # 取得方法
        method, snip = _extract_method(text)
        if method:
            event.acquisition_method = method
            raw_snippets["raw_method_text"] = snip
            confidence += 0.05

        # ステータス期間ラベル
        event.status_period_label = _extract_status_period_label(text)

    elif event_type == BUYBACK_RESULT:
        # 取得株数
        shares, snip = _extract_shares(text, [
            "取得した株式の総数", "取得株式数",
            "取得した株式の数",
        ])
        if shares:
            event.shares_acquired = shares
            raw_snippets["raw_shares_text"] = snip
            confidence += 0.15

        # 取得金額
        amt, snip = _extract_amount(text, [
            "取得価額の総額", "取得金額",
        ])
        if amt:
            event.amount_acquired_million_yen = amt
            raw_snippets["raw_amount_text"] = snip
            confidence += 0.15

        # 取得期間
        start, end, snip = _extract_period(text)
        if start or end:
            event.start_date = start
            event.end_date = end
            raw_snippets["raw_period_text"] = snip
            confidence += 0.10

        # 取得方法
        method, snip = _extract_method(text)
        if method:
            event.acquisition_method = method
            raw_snippets["raw_method_text"] = snip
            confidence += 0.05

        # 比率
        ratio, snip = _extract_ratio(text)
        if ratio:
            event.ratio_to_outstanding = ratio
            raw_snippets["raw_ratio_text"] = snip

    elif event_type == TREASURY_CANCEL:
        # 消却株数
        shares, snip = _extract_shares(text, [
            "消却する株式の数", "消却株式数", "消却した株式",
            "消却に係る株式の数",
        ])
        if shares:
            event.shares_cancelled = shares
            raw_snippets["raw_shares_text"] = snip
            confidence += 0.15

        # 消却日
        cancel_dt, snip = _extract_date(text, [
            "消却予定日", "消却日", "消却を行う日",
        ])
        if cancel_dt:
            event.cancel_date = cancel_dt
            event.end_date = cancel_dt  # end_date 互換
            confidence += 0.10

        # 比率
        ratio, snip = _extract_ratio(text)
        if ratio:
            event.ratio_to_outstanding = ratio
            raw_snippets["raw_ratio_text"] = snip

        # treasury_cancel key fields 欠落チェック
        if not event.shares_cancelled and not event.cancel_date:
            confidence -= 0.25  # key fields 両方欠落でペナルティ
            raw_snippets["cancel_penalty"] = "shares_cancelled and cancel_date both missing"

    # + 必須キーワード確認
    required_kw = ["自己株式", "取得", "株式"]
    kw_count = sum(1 for kw in required_kw if kw in text)
    if kw_count >= 2:
        confidence += 0.20

    event.extraction_confidence = min(round(confidence, 2), 1.0)

    # extracted_json 構築
    extracted = {
        "title_used": title,
        "body_head_used": text[:500],
        **raw_snippets,
        "extraction_notes": "",
    }
    event.extracted_json = json.dumps(extracted, ensure_ascii=False)

    if event.extraction_confidence < 0.5:
        logger.warning(
            f"低 confidence ({event.extraction_confidence}): "
            f"ticker={ticker} event_type={event_type} title={title[:50]}"
        )

    return event


# ============================================================
# PDF 本文先頭から metadata 補完
# ============================================================
_CODE_RE = re.compile(r"(?:コード[番号：:\s]*|証券[コード：:\s]*)([\d０-９]{4})")
_DERIVED_DATE_RE = re.compile(
    r"((?:令和|平成|昭和)?\s*[\d０-９]{1,4}\s*年\s*[\d０-９]{1,2}\s*月\s*[\d０-９]{1,2}\s*日)"
)


def derive_metadata_from_text(text: str) -> dict:
    """PDF 本文先頭からメタデータを補完する。

    Returns
    -------
    dict with keys:
        derived_ticker, derived_disclosure_date, derived_title
    """
    head = text[:800]
    result: dict = {
        "derived_ticker": None,
        "derived_disclosure_date": None,
        "derived_title": None,
    }

    # ticker (コード番号)
    head_norm = head.translate(_ZEN_TO_HAN)
    m = _CODE_RE.search(head_norm)
    if m:
        result["derived_ticker"] = m.group(1).strip()

    # disclosure_date (先頭の日付)
    m = _DERIVED_DATE_RE.search(head)
    if m:
        result["derived_disclosure_date"] = normalize_jp_date(m.group(1))

    # title (最初の意味のある見出し)
    lines = head.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 日付行・宛名行・会社名行をスキップ
        if re.match(r"^[\d０-９年月日\s]+$", line):
            continue
        if line.startswith("各") and "位" in line:
            continue
        if any(skip in line for skip in ["会 社 名", "会社名", "代表者名", "問合せ先",
                                          "TEL", "コード", "証券コード"]):
            continue
        # 意味のある行を見出し候補とする
        if len(line) >= 8 and ("お知らせ" in line or "に関する" in line
                               or "について" in line or "決算" in line
                               or "取得" in line or "消却" in line
                               or "配当" in line or "株式" in line
                               or "開示" in line):
            result["derived_title"] = line[:200]
            break

    return result
