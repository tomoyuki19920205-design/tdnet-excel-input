#!/usr/bin/env python3
"""dividend_extractor.py — 配当予想修正テキストからの値抽出"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from .dividend_models import DividendRevisionEvent
from .common_normalizers import normalize_jp_number, parse_number

logger = logging.getLogger("dividend_extractor")


# ============================================================
# 期間・基準判定
# ============================================================
_FY_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*期")
_BASIS_KEYWORDS = {
    "期末": "期末",
    "中間": "中間",
    "年間": "年間",
    "第2四半期末": "中間",
    "第3四半期末": "第3四半期末",
}


def _detect_fiscal_period(text: str) -> str:
    m = _FY_RE.search(text)
    return f"{m.group(1)}年{m.group(2)}月期" if m else ""


def _detect_dividend_basis(text: str) -> str:
    for kw, basis in _BASIS_KEYWORDS.items():
        if kw in text:
            return basis
    return ""


# ============================================================
# 配当額抽出
# ============================================================
_DIVIDEND_AMOUNT_RE = re.compile(r"([\d,.]+)\s*円")
_NEGATIVE_MARK = re.compile(r"[△▲\-]")


def _extract_dividend_per_share(text: str, anchors: list[str], window: int = 200) -> Optional[float]:
    """アンカー近傍から1株当たり配当額を抽出"""
    for anchor in anchors:
        idx = text.find(anchor)
        if idx >= 0:
            # Never cross into the next table row or a prior-year actual.
            snippet = text[idx:idx + len(anchor) + window].splitlines()[0]
            s = normalize_jp_number(snippet)
            s = re.sub(r"[（(][^）)]*(?:普通配当|特別配当|記念配当|増配)[^）)]*[）)]", "", s)
            if "未定" in s or "未決定" in s:
                continue
            m = _DIVIDEND_AMOUNT_RE.search(s)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
    return None


def _find_dividend_table_values(text: str) -> dict:
    """テーブル形式の配当情報から前回/修正後の値を抽出する。

    一般的な配当予想修正PDFの表形式:
                      中間  期末  年間
    前回予想(A)        XX円  XX円  XX円
    今回修正予想(B)    XX円  XX円  XX円
    """
    result = {}
    lines = text.split("\n")

    prev_keywords = ("前回発表予想", "前回公表予想", "前回予想", "修正前")
    rev_keywords = ("今回修正予想", "今回公表予想", "今回予想", "公表予想", "修正予想", "修正後")

    def amounts(line: str) -> list[float]:
        # 内訳は総額の代用にならない。括弧内の普通/特別/記念/増配を除く。
        s = normalize_jp_number(line)
        s = re.sub(r"[（(][^）)]*(?:普通配当|特別配当|記念配当|増配)[^）)]*[）)]", "", s)
        return [float(m.group(1).replace(",", "")) for m in
                re.finditer(r"([\d,]+(?:\.\d+)?)\s*円(?:\s*\d{1,2}\s*銭)?", s)]

    for i, line in enumerate(lines):
        compact = normalize_jp_number(line).replace(" ", "").replace("\u3000", "")
        kind = ("previous_values" if any(k in compact for k in prev_keywords) else
                "revised_values" if any(k in compact for k in rev_keywords) else None)
        if kind is None or kind in result or "前期実績" in compact:
            continue
        vals = amounts(line)
        if not vals and i > 0:
            # PDFで対象期の総額行の直下に「（修正前/後）」がある形式。
            prior = normalize_jp_number(lines[i - 1])
            if "期" in prior and "予想" in prior and "実績" not in prior:
                vals = amounts(prior)
        if vals:
            result[kind] = vals

    return result


def _revised_component_amount(text: str, label: str) -> Optional[float]:
    """Find a component only in the revised forecast, excluding prior-year actuals."""
    lines = [normalize_jp_number(line).replace(" ", "").replace("\u3000", "")
             for line in text.splitlines()]
    for i, line in enumerate(lines):
        m = re.search(re.escape(label) + r"([\d,]+(?:\.\d+)?)円", line)
        if not m:
            continue
        revision_rows = [j for j in range(i + 1)
                         if any(k in lines[j] for k in ("修正後", "今回修正予想", "今回予想"))]
        start = revision_rows[-1] if revision_rows else 0
        if any("前期実績" in row or "期実績" in row for row in lines[start:i + 1]):
            continue
        return float(m.group(1).replace(",", ""))
    return None


# ============================================================
# subtype 判定
# ============================================================
def _determine_subtype(
    event: DividendRevisionEvent,
    title: str = "",
) -> str:
    """配当修正の subtype を決定"""
    # 優先順: commemorative > special > increase > decrease > maintain > undecided
    if event.commemorative_dividend_per_share and event.commemorative_dividend_per_share > 0:
        return "commemorative_dividend"
    if event.special_dividend_per_share and event.special_dividend_per_share > 0:
        return "special_dividend"

    if event.revised_dividend_per_share is not None and event.previous_dividend_per_share is not None:
        if event.revised_dividend_per_share > event.previous_dividend_per_share:
            return "increase"
        elif event.revised_dividend_per_share < event.previous_dividend_per_share:
            return "decrease"
        else:
            return "maintain"

    # タイトルからのヒント
    if "増配" in title:
        return "increase"
    if "減配" in title:
        return "decrease"
    if "記念配当" in title:
        return "commemorative_dividend"
    if "特別配当" in title:
        return "special_dividend"

    return "undecided"


# ============================================================
# importance 算出
# ============================================================
def _calc_importance(event: DividendRevisionEvent) -> int:
    score = 50

    if event.subtype in ("commemorative_dividend", "special_dividend"):
        score = 75

    if event.previous_dividend_per_share is not None and event.revised_dividend_per_share is not None:
        prev = event.previous_dividend_per_share
        rev = event.revised_dividend_per_share
        if prev > 0:
            change_pct = (rev - prev) / prev * 100
            if change_pct >= 50:
                score = 85
            elif change_pct >= 20:
                score = 75
            elif change_pct > 0:
                score = 70
            elif change_pct <= -50:
                score = 80
            elif change_pct < 0:
                score = 70
        elif prev == 0 and rev > 0:
            score = 80  # 無配→復配

    return score


# ============================================================
# メイン抽出関数
# ============================================================
def extract_dividend_revision(
    text: str,
    title: str = "",
    pdf_path: str = "",
) -> DividendRevisionEvent:
    """テキストから配当予想修正イベントを抽出する。"""
    event = DividendRevisionEvent()
    event.fiscal_period = _detect_fiscal_period(title) or _detect_fiscal_period(text)
    # The title defines a specific comparison basis when stated. Otherwise
    # the annual total is the default; narrative mentions of 期末/中間 are not a basis.
    event.dividend_basis = _detect_dividend_basis(title) or "年間"

    confidence = 0.0

    # テーブル値抽出
    table_vals = _find_dividend_table_values(text)
    prev_vals = table_vals.get("previous_values", [])
    revised_vals = table_vals.get("revised_values", [])

    # 期末/年間を優先（配列の後ろの方）
    if prev_vals and revised_vals:
        confidence += 0.40

        if event.dividend_basis == "中間":
            # 中間配当 = 最初の値
            event.previous_dividend_per_share = prev_vals[0] if prev_vals else None
            event.revised_dividend_per_share = revised_vals[0] if revised_vals else None
        elif len(prev_vals) >= 2 and len(revised_vals) >= 2:
            # 期末 = 2番目, 年間 = 3番目（あれば）
            chosen = -1 if event.dividend_basis == "年間" else -2
            event.previous_dividend_per_share = prev_vals[chosen]
            event.revised_dividend_per_share = revised_vals[chosen]
            if len(prev_vals) >= 3:
                event.annual_total_previous = prev_vals[-1]
            if len(revised_vals) >= 3:
                event.annual_total_revised = revised_vals[-1]
        else:
            event.previous_dividend_per_share = prev_vals[-1]
            event.revised_dividend_per_share = revised_vals[-1]
    elif revised_vals:
        confidence += 0.20
        event.revised_dividend_per_share = revised_vals[-1] if revised_vals else None

    # delta 計算
    if event.previous_dividend_per_share is not None and event.revised_dividend_per_share is not None:
        event.delta_dividend_per_share = round(
            event.revised_dividend_per_share - event.previous_dividend_per_share, 2
        )

    # アンカーベースのフォールバック
    if event.revised_dividend_per_share is None:
        val = _extract_dividend_per_share(text, ["修正後", "今回修正予想", "今回予想"])
        if val is not None:
            event.revised_dividend_per_share = val
            confidence += 0.15

    if event.previous_dividend_per_share is None:
        val = _extract_dividend_per_share(text, ["前回予想", "前回発表予想"])
        if val is not None:
            event.previous_dividend_per_share = val
            confidence += 0.10

    # 特別配当/記念配当
    special = _revised_component_amount(text, "特別配当")
    if special:
        event.special_dividend_per_share = special
        confidence += 0.10

    commemorative = _revised_component_amount(text, "記念配当")
    if commemorative:
        event.commemorative_dividend_per_share = commemorative
        confidence += 0.10

    # 配当性向
    payout_match = re.search(r"配当性向\s*[:：]?\s*([\d.]+)\s*[%％]", normalize_jp_number(text))
    if payout_match:
        try:
            event.payout_ratio = float(payout_match.group(1))
        except ValueError:
            pass

    event.confidence = min(round(confidence, 2), 1.0)
    event.subtype = _determine_subtype(event, title)
    event.importance = _calc_importance(event)

    # ---- FITZ 年間合計抽出 (pdf_path がある場合) ----
    if pdf_path and os.path.exists(pdf_path):
        fitz_div = _extract_dividend_annual_total_via_fitz(
            pdf_path, requested_basis=event.dividend_basis or "年間")
        if fitz_div:
            rev_a = fitz_div.get("annual_total_revised")
            prev_a = fitz_div.get("annual_total_previous")
            if rev_a is not None:
                event.annual_total_revised = rev_a
            if prev_a is not None:
                event.annual_total_previous = prev_a
            comp_rev = fitz_div.get("comparison_revised")
            comp_prev = fitz_div.get("comparison_previous")
            if comp_rev is not None:
                event.revised_dividend_per_share = comp_rev
            if comp_prev is not None:
                event.previous_dividend_per_share = comp_prev
            event.dividend_basis = fitz_div.get("comparison_basis") or event.dividend_basis
            event.confidence = max(event.confidence, 0.8 if prev_a is not None else 0.6)
            # delta / subtype / importance を年間合計ベースで再計算
            if event.previous_dividend_per_share is not None and event.revised_dividend_per_share is not None:
                event.delta_dividend_per_share = round(
                    event.revised_dividend_per_share - event.previous_dividend_per_share, 2
                )
            event.subtype = _determine_subtype(event, title)
            event.importance = _calc_importance(event)
            logger.info(
                f"[dividend_fitz] annual_prev={event.annual_total_previous} "
                f"annual_rev={event.annual_total_revised} "
                f"subtype={event.subtype}"
            )

    return event


# ============================================================
# FITZ 年間合計抽出 — ローカルヘルパー
# ============================================================

def _div_group_rows(words: list[dict], y_tol: float = 5.0) -> list[list[dict]]:
    """Y座標で words を行グループ化し、各行を X 昇順に並び替える。"""
    if not words:
        return []
    rows: list[list[dict]] = []
    current: list[dict] = [words[0]]
    for w in words[1:]:
        if abs(w["top"] - current[0]["top"]) <= y_tol:
            current.append(w)
        else:
            rows.append(sorted(current, key=lambda x: x["x0"]))
            current = [w]
    rows.append(sorted(current, key=lambda x: x["x0"]))
    return rows


def _div_row_to_tokens(row: list[dict]) -> list[dict]:
    """pdfplumber word リストから {text, cx} トークンリストを作る。"""
    return [
        {"text": w["text"], "cx": (w["x0"] + w["x1"]) / 2}
        for w in row
    ]


def _div_fitz_compact(s: str) -> str:
    """NFKC正規化して小文字化・空白除去。"""
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s)).lower()


# ダッシュ系文字（無配・未定を表す）
_DIV_DASH_CHARS = frozenset("－―\u2014\u2013-―")
# 円銭形式: 「105円00銭」→ 105.00
_DIV_YEN_SEN_RE = re.compile(r"([\d,，]+)\s*円\s*(\d{1,2})\s*銭")
_DIV_YEN_RE     = re.compile(r"([\d,，]+(?:\.\d+)?)\s*円")


def _div_parse_cell_value(text: str) -> float | None:
    """配当額文字列を float に変換する。

    対応形式:
        105円00銭 → 105.00
        105円50銭 → 105.50
        105.00円  → 105.00
        105円     → 105.00
        1,234.56  → 1234.56
        － / - / ― → None
    """
    s = text.strip()
    # ダッシュ系は None
    if s and all(c in _DIV_DASH_CHARS for c in s):
        return None
    # 「105円00銭」形式
    m = _DIV_YEN_SEN_RE.search(s)
    if m:
        yen  = float(m.group(1).replace(",", "").replace("，", ""))
        sen  = float(m.group(2)) / 100
        return round(yen + sen, 2)
    # 「105.50円」「105円」形式
    m = _DIV_YEN_RE.search(s)
    if m:
        return float(m.group(1).replace(",", "").replace("，", ""))
    # 通常の数値文字列
    plain = s.replace(",", "").replace("，", "")
    try:
        return float(plain)
    except ValueError:
        return None


def _div_parse_total_cell(text: str) -> float | None:
    """Read a whole table cell, never a yen amount embedded in a component note."""
    s = _div_fitz_compact(text).replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:円(?:\d{1,2}銭)?)?", s):
        return None
    return _div_parse_cell_value(s)


# ============================================================
# FITZ 年間合計抽出
# ============================================================

def _extract_dividend_annual_total_via_fitz(
    pdf_path: str, requested_basis: str = "年間"
) -> dict | None:
    """pdfplumber 座標ベースで年間合計配当額（前回/今回）を抽出する。

    業績予想修正PDFに同居する配当予想修正テーブルも対象。
    抽出対象は annual_total_previous / annual_total_revised のみ。

    改善点:
      - 表タイプ判定（配当予想修正型/剰余金配当決定型）でラベル切替
      - ヘッダー行スコアに本文ペナルティ・列token数ボーナスを追加
      - >= 比較で後出し優先（テーブル直前ヘッダーが勝つ）
      - 合計 > 年間 > 期末 の列優先順位
      - rightmost fallback でゼロ値(abs<0.5)を除外
    """
    # 配当キーワード事前チェック用
    _DIV_TRIGGER = [
        "配当予想", "1株当たり配当金", "年間配当金", "期末配当", "配当金", "配当の修正",
    ]
    # ── 表タイプ別ラベル定義 ──────────────────────────────────
    # A. 配当予想修正型
    _PREV_LABELS_A = [
        "前回発表予想", "前回予想", "前回公表予想", "修正前",
        "前回(a)", "前回（a）", "(a)", "（a）",
    ]
    _REV_LABELS_A = [
        "今回修正予想", "今回予想", "修正後", "今回公表予想",
        "公表予想", "修正予想", "今回(b)", "今回（b）", "(b)", "（b）",
    ]
    # B. 剰余金配当決定型
    _PREV_LABELS_B = ["直近の配当予想", "直近配当予想"]
    _REV_LABELS_B  = ["決定額", "今回決定額"]

    # ── 年間/合計/期末列ラベル（優先順位順）──────────────────
    _DIV_TOTAL_LABELS    = ["合計", "年間合計", "合計配当金", "1株当たり年間配当金"]
    _DIV_ANNUAL_LABELS   = ["年間", "年間配当金"]
    _DIV_TERM_END_LABELS = ["期末", "期末配当"]

    # ── ヘッダー行スコアリング ───────────────────────────────
    # 加点: 配当テーブルのヘッダーらしいKW
    _HDR_POSITIVE = [
        "第1四半期末", "第2四半期末", "第3四半期末",
        "中間", "期末", "合計", "年間", "円", "銭",
    ]
    # 減点: 本文説明文らしいKW
    _HDR_NEGATIVE = [
        "ため", "いたし", "見込み", "上方修正", "下方修正",
        "株主還元", "剰余金", "につき", "ことにより", "となり",
        "お知らせ", "します", "まし",
    ]

    # forecast_extractor に依存せずローカルヘルパーを使用
    _group_rows       = _div_group_rows
    _row_to_tokens    = _div_row_to_tokens
    _fitz_compact     = _div_fitz_compact
    _parse_cell_value = _div_parse_cell_value

    best_prev: float | None = None
    best_rev:  float | None = None
    best_basis = ""
    best_comp_prev: float | None = None
    best_comp_rev: float | None = None
    best_comp_basis = ""

    try:
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            # Step 0: 配当キーワード事前チェック
            full_text = "".join(p.extract_text() or "" for p in pdf.pages[:5])
            if not any(kw in full_text for kw in _DIV_TRIGGER):
                logger.debug("[dividend_fitz] no dividend keyword found, skip")
                return None

            # ── Step 1: 表タイプ判定（全文から）──────────────
            ft_compact = _fitz_compact(full_text)
            if any(_fitz_compact(lbl) in ft_compact for lbl in _REV_LABELS_A + _PREV_LABELS_A):
                _table_type      = "A"  # 配当予想修正型
                _DIV_PREV_LABELS = _PREV_LABELS_A
                _DIV_REV_LABELS  = _REV_LABELS_A
            elif any(_fitz_compact(lbl) in ft_compact for lbl in _REV_LABELS_B + _PREV_LABELS_B):
                _table_type      = "B"  # 剰余金配当決定型
                _DIV_PREV_LABELS = _PREV_LABELS_B
                _DIV_REV_LABELS  = _REV_LABELS_B
            else:
                _table_type      = "A"  # デフォルト
                _DIV_PREV_LABELS = _PREV_LABELS_A
                _DIV_REV_LABELS  = _REV_LABELS_A
            logger.debug(f"[dividend_fitz] table_type={_table_type}")

            for page in pdf.pages[:5]:
                raw_words = page.extract_words(x_tolerance=3, y_tolerance=3)
                if not raw_words:
                    continue

                grouped_rows = _group_rows(raw_words)

                # ── Step 2: スコアベースでヘッダー行を検出 ────
                # スコア = 正KW数 + token数ボーナス - 負KW数×2
                # >= 比較で同スコア時は後出し優先（テーブル直前ヘッダーが勝つ）
                def _score_div_row(rw: list) -> float:
                    rc = _fitz_compact("".join(w["text"] for w in rw))
                    pos = sum(1 for kw in _HDR_POSITIVE if _fitz_compact(kw) in rc)
                    neg = sum(1 for kw in _HDR_NEGATIVE if _fitz_compact(kw) in rc)
                    tok_bonus = min(len(rw) / 10.0, 1.0)
                    return pos - neg * 2 + tok_bonus

                hdr_idx    = -1
                best_score = -999.0
                hdr_tokens: list = []
                for ri, rw in enumerate(grouped_rows[:35]):
                    s = _score_div_row(rw)
                    if s >= best_score:  # >= で後出し優先
                        best_score = s
                        hdr_idx    = ri
                        hdr_tokens = _row_to_tokens(rw)

                if hdr_idx == -1 or best_score <= 0:
                    continue
                print(f"[dividend_fitz_sort] header_row_idx={hdr_idx} score={best_score:.2f}")

                # ── Step 3: 列X座標取得（合計 > 年間 > 期末 優先）──
                total_x:    float | None = None  # 合計列
                annual_x:   float | None = None  # 年間列
                term_end_x: float | None = None  # 期末列
                interim_x:  float | None = None  # 中間列

                below_tokens: list = []
                if hdr_idx + 1 < len(grouped_rows):
                    nxt_compact = _fitz_compact(
                        "".join(w["text"] for w in grouped_rows[hdr_idx + 1])
                    )
                    is_data = any(
                        _fitz_compact(lbl) in nxt_compact
                        for lbl in _DIV_PREV_LABELS + _DIV_REV_LABELS
                    )
                    if not is_data:
                        below_tokens = _row_to_tokens(grouped_rows[hdr_idx + 1])

                for tok in hdr_tokens:
                    tok_cx = tok["cx"]
                    below_text = ""
                    for bt in below_tokens:
                        if abs(bt["cx"] - tok_cx) < 25:
                            below_text = bt["text"]
                            break
                    combined = _fitz_compact(tok["text"] + below_text)
                    # 優先順位1: 合計列
                    if total_x is None and any(_fitz_compact(lbl) in combined for lbl in _DIV_TOTAL_LABELS):
                        total_x = tok_cx
                        print(f"[dividend_fitz_colmap] total_x={tok_cx:.1f} header_text={tok['text']!r}")
                    # 優先順位2: 年間列
                    elif annual_x is None and any(_fitz_compact(lbl) in combined for lbl in _DIV_ANNUAL_LABELS):
                        annual_x = tok_cx
                        print(f"[dividend_fitz_colmap] annual_x={tok_cx:.1f} header_text={tok['text']!r}")
                    elif interim_x is None and ("中間" in combined or "第2四半期末" in combined):
                        interim_x = tok_cx
                    # 優先順位3: 期末列
                    elif (term_end_x is None and "四半期" not in combined
                          and any(_fitz_compact(lbl) in combined for lbl in _DIV_TERM_END_LABELS)):
                        term_end_x = tok_cx
                        print(f"[dividend_fitz_colmap] term_end_x={tok_cx:.1f} header_text={tok['text']!r}")

                # Some PDFs split 合計 and 期末 into individual glyph tokens.
                for left, right in zip(hdr_tokens, hdr_tokens[1:]):
                    if right["cx"] - left["cx"] > 35:
                        continue
                    pair = _fitz_compact(left["text"] + right["text"])
                    center = (left["cx"] + right["cx"]) / 2
                    if total_x is None and any(_fitz_compact(lbl) in pair for lbl in _DIV_TOTAL_LABELS):
                        total_x = center
                    if term_end_x is None and any(_fitz_compact(lbl) in pair for lbl in _DIV_TERM_END_LABELS):
                        term_end_x = center

                # 列もなければ次ページへ（合計 > 年間 > 期末 優先）
                target_x: float | None = total_x or annual_x or term_end_x
                if target_x is None:
                    continue
                page_basis = "年間" if total_x or annual_x else "期末"
                comparison_x = (term_end_x if requested_basis == "期末" else
                                interim_x if requested_basis == "中間" else target_x)
                comparison_basis = (page_basis if requested_basis == "年間" and page_basis != "年間"
                                    else requested_basis if comparison_x is not None else page_basis)
                if comparison_x is None:
                    comparison_x = target_x

                # Step 4: 前回行 / 今回行を検出。修正ラベルが総額行の直下に
                # 単独で置かれる場合、括弧内訳ではなく直上の対象期行に結ぶ。
                prev_tokens: list | None = None
                rev_tokens:  list | None = None

                def _has_total_cell(rw: list) -> bool:
                    return any(
                        _div_parse_total_cell(t["text"]) is not None
                        and abs((t["x0"] + t["x1"]) / 2 - target_x) <= 65
                        for t in rw
                    )

                def _data_row(label_idx: int) -> list | None:
                    row = grouped_rows[label_idx]
                    if _has_total_cell(row):
                        return row
                    for candidate_idx in (label_idx - 1, label_idx + 1):
                        if candidate_idx <= hdr_idx or candidate_idx >= len(grouped_rows):
                            continue
                        candidate = grouped_rows[candidate_idx]
                        cmp_candidate = _fitz_compact("".join(t["text"] for t in candidate))
                        is_other_role = any(_fitz_compact(lbl) in cmp_candidate
                                            for lbl in _DIV_PREV_LABELS + _DIV_REV_LABELS)
                        if ("実績" not in cmp_candidate and not is_other_role
                                and _has_total_cell(candidate)):
                            return candidate
                    return None

                for ri in range(hdr_idx + 1, len(grouped_rows)):
                    rw       = grouped_rows[ri]
                    row_text = "".join(w["text"] for w in rw)
                    cmp      = _fitz_compact(row_text)
                    if rev_tokens is None and any(_fitz_compact(lbl) in cmp for lbl in _DIV_REV_LABELS):
                        data = _data_row(ri)
                        if data is not None:
                            rev_tokens = _row_to_tokens(data)
                    elif prev_tokens is None and any(_fitz_compact(lbl) in cmp for lbl in _DIV_PREV_LABELS):
                        data = _data_row(ri)
                        if data is not None:
                            prev_tokens = _row_to_tokens(data)
                    if prev_tokens is not None and rev_tokens is not None:
                        break
                if rev_tokens is None:
                    continue

                # Step 5: 明示された合計列のみを採用。内訳の加算・右端値の
                # 推測採用は行わず、未定・抽出不能は None のまま残す。
                def _pick_annual(tokens: list, kind: str, column_x: float) -> float | None:
                    if not tokens:
                        return None
                    nums_cx = sorted(
                        [
                            (t["cx"], v)
                            for t in tokens
                            if (v := _div_parse_total_cell(t["text"])) is not None
                        ],
                        key=lambda x: x[0],
                    )
                    # 行数値一覧ログ
                    print(
                        f"[dividend_fitz_row_numbers] kind={kind} "
                        f"count={len(nums_cx)} nums={[v for _, v in nums_cx]}"
                    )
                    if not nums_cx:
                        return None
                    col_tol = 65.0
                    if column_x is not None:
                        closest = min(nums_cx, key=lambda x: abs(x[0] - column_x))
                        dist    = abs(closest[0] - column_x)
                        print(
                            f"[dividend_fitz_assign] kind={kind} "
                            f"value={closest[1]} num_x={closest[0]:.1f} "
                            f"column_x={column_x:.1f} dist={dist:.1f} "
                            f"accepted={dist <= col_tol} "
                            f"reason={'within_tolerance' if dist <= col_tol else 'out_of_tolerance'}"
                        )
                        if dist <= col_tol:
                            return closest[1]
                    return None

                rev_val  = _pick_annual(rev_tokens,  "rev", target_x)
                prev_val = _pick_annual(prev_tokens, "prev", target_x) if prev_tokens else None
                comp_rev = _pick_annual(rev_tokens, "comp_rev", comparison_x)
                comp_prev = (_pick_annual(prev_tokens, "comp_prev", comparison_x)
                             if prev_tokens else None)

                if rev_val is not None:
                    best_rev  = rev_val if page_basis == "年間" else None
                    best_prev = prev_val if page_basis == "年間" else None
                    best_basis = page_basis
                    best_comp_rev = comp_rev
                    best_comp_prev = comp_prev
                    best_comp_basis = comparison_basis
                    break  # 最初に取れたページで確定

    except Exception as e:
        import traceback
        logger.debug(f"[dividend_fitz] pdfplumber extract failed: {e}")
        print(f"[DEBUG_FITZ_ERROR] pdfplumber extract failed: {traceback.format_exc()}")

    if best_comp_rev is None:
        return None

    return {
        "annual_total_previous": best_prev,
        "annual_total_revised":  best_rev,
        "dividend_basis":       best_basis,
        "comparison_previous": best_comp_prev,
        "comparison_revised":  best_comp_rev,
        "comparison_basis":    best_comp_basis,
        "extraction_source":     "fitz_annual_total",
    }
