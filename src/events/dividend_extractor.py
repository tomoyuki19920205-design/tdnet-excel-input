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
            snippet = text[idx:idx + len(anchor) + window]
            s = normalize_jp_number(snippet)
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

    prev_line = None
    revised_line = None

    for i, line in enumerate(lines):
        clean = normalize_jp_number(line).strip()
        if not clean:
            continue

        # スペース（半角・全角）を除去した比較用文字列でキーワード判定
        # → 「前　回　予　想」「今　回　修　正　予　想」等に対応
        compact = clean.replace(" ", "").replace("\u3000", "")

        _PREV_KEYWORDS = ["前回発表予想", "前回公表予想", "前回予想", "修正前"]
        _REV_KEYWORDS  = ["今回修正予想", "今回公表予想", "今回予想", "公表予想", "修正予想", "修正後"]

        if any(kw in compact for kw in _PREV_KEYWORDS):
            prev_line = clean
        elif any(kw in compact for kw in _REV_KEYWORDS):
            revised_line = clean

    # 数値抽出
    def _extract_yen_values(line: str) -> list[float]:
        s = normalize_jp_number(line)
        vals = []
        for m in re.finditer(r"([\d,.]+)\s*(?:円|\.)", s):
            try:
                vals.append(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
        return vals

    if prev_line:
        prev_vals = _extract_yen_values(prev_line)
        if prev_vals:
            result["previous_values"] = prev_vals

    if revised_line:
        revised_vals = _extract_yen_values(revised_line)
        if revised_vals:
            result["revised_values"] = revised_vals

    return result


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
    event.fiscal_period = _detect_fiscal_period(text) or _detect_fiscal_period(title)
    event.dividend_basis = _detect_dividend_basis(text) or _detect_dividend_basis(title)

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
            event.previous_dividend_per_share = prev_vals[-2] if len(prev_vals) >= 2 else prev_vals[-1]
            event.revised_dividend_per_share = revised_vals[-2] if len(revised_vals) >= 2 else revised_vals[-1]
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
    special = _extract_dividend_per_share(text, ["特別配当"])
    if special:
        event.special_dividend_per_share = special
        confidence += 0.10

    commemorative = _extract_dividend_per_share(text, ["記念配当"])
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
        fitz_div = _extract_dividend_annual_total_via_fitz(pdf_path)
        if fitz_div:
            rev_a = fitz_div.get("annual_total_revised")
            prev_a = fitz_div.get("annual_total_previous")
            if rev_a is not None:
                event.annual_total_revised = rev_a
                event.revised_dividend_per_share = rev_a
            if prev_a is not None:
                event.annual_total_previous = prev_a
                event.previous_dividend_per_share = prev_a
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


# ============================================================
# FITZ 年間合計抽出
# ============================================================

def _extract_dividend_annual_total_via_fitz(pdf_path: str) -> dict | None:
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
                    # 優先順位3: 期末列
                    elif term_end_x is None and any(_fitz_compact(lbl) in combined for lbl in _DIV_TERM_END_LABELS):
                        term_end_x = tok_cx
                        print(f"[dividend_fitz_colmap] term_end_x={tok_cx:.1f} header_text={tok['text']!r}")

                # 列もなければ次ページへ（合計 > 年間 > 期末 優先）
                target_x: float | None = total_x or annual_x or term_end_x
                if target_x is None:
                    continue

                # Step 4: 前回行 / 今回行を検出
                prev_tokens: list | None = None
                rev_tokens:  list | None = None
                prev_row_idx = -1
                rev_row_idx  = -1

                for ri in range(hdr_idx + 1, len(grouped_rows)):
                    rw       = grouped_rows[ri]
                    row_text = "".join(w["text"] for w in rw)
                    cmp      = _fitz_compact(row_text)
                    if rev_tokens is None and any(_fitz_compact(lbl) in cmp for lbl in _DIV_REV_LABELS):
                        rev_tokens  = _row_to_tokens(rw)
                        rev_row_idx = ri
                        print(f"[dividend_fitz_row] rev_row_found=True  rev_row_text={row_text[:60]!r}")
                    elif prev_tokens is None and any(_fitz_compact(lbl) in cmp for lbl in _DIV_PREV_LABELS):
                        prev_tokens  = _row_to_tokens(rw)
                        prev_row_idx = ri
                        print(f"[dividend_fitz_row] prev_row_found=True prev_row_text={row_text[:60]!r}")
                    if prev_tokens is not None and rev_tokens is not None:
                        break

                print(
                    f"[dividend_fitz_row] "
                    f"prev_row_found={prev_tokens is not None} "
                    f"rev_row_found={rev_tokens is not None}"
                )
                if rev_tokens is None:
                    continue

                # Step 5: continuation フォールバック（有効数値が少ない行は次行を探す）
                def _div_count_nums(tokens: list) -> int:
                    return sum(
                        1 for t in tokens
                        if (v := _parse_cell_value(t["text"])) is not None
                        and abs(v) not in {0.0, 1.0, 2.0}
                    )

                def _div_continuation(tokens: list, row_idx: int, kind: str) -> list:
                    if row_idx < 0 or _div_count_nums(tokens) >= 1:
                        return tokens
                    for offset in range(1, 4):
                        nri = row_idx + offset
                        if nri >= len(grouped_rows):
                            break
                        cand = _row_to_tokens(grouped_rows[nri])
                        if _div_count_nums(cand) >= 1:
                            nums = [
                                v for t in cand
                                if (v := _parse_cell_value(t["text"])) is not None
                                and abs(v) not in {0.0, 1.0, 2.0}
                            ]
                            print(f"[dividend_fitz_row_continuation] kind={kind} adopted_row={nri} nums={nums}")
                            return cand
                    return tokens

                if prev_tokens is not None:
                    prev_tokens = _div_continuation(prev_tokens, prev_row_idx, "prev")
                rev_tokens = _div_continuation(rev_tokens, rev_row_idx, "rev")

                # Step 6: 年間合計額の割り当て
                def _pick_annual(tokens: list, kind: str) -> float | None:
                    if not tokens:
                        return None
                    nums_cx = sorted(
                        [
                            (t["cx"], v)
                            for t in tokens
                            if (v := _parse_cell_value(t["text"])) is not None
                            and abs(v) not in {0.0, 1.0, 2.0}
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
                    col_tol = 40.0
                    if target_x is not None:
                        closest = min(nums_cx, key=lambda x: abs(x[0] - target_x))
                        dist    = abs(closest[0] - target_x)
                        print(
                            f"[dividend_fitz_assign] kind={kind} "
                            f"value={closest[1]} num_x={closest[0]:.1f} "
                            f"annual_x={target_x:.1f} dist={dist:.1f} "
                            f"accepted={dist <= col_tol} "
                            f"reason={'within_tolerance' if dist <= col_tol else 'out_of_tolerance'}"
                        )
                        if dist <= col_tol:
                            return closest[1]
                        print(f"[dividend_fitz_assign] kind={kind} → rightmost_fallback")
                    # フォールバック: ゼロ値(abs<0.5)を除外した最右値
                    # （「00銭」が最右になるのを防ぐ）
                    non_zero = [(cx, v) for cx, v in nums_cx if abs(v) > 0.5]
                    if not non_zero:
                        return None
                    rightmost = non_zero[-1]
                    print(
                        f"[dividend_fitz_assign] kind={kind} "
                        f"value={rightmost[1]} num_x={rightmost[0]:.1f} "
                        f"target_x={target_x:.1f if target_x is not None else 'N/A'} "
                        f"reason=rightmost_fallback"
                    )
                    return rightmost[1]

                rev_val  = _pick_annual(rev_tokens,  "rev")
                prev_val = _pick_annual(prev_tokens, "prev") if prev_tokens else None

                if rev_val is not None:
                    best_rev  = rev_val
                    best_prev = prev_val
                    break  # 最初に取れたページで確定

    except Exception as e:
        logger.debug(f"[dividend_fitz] pdfplumber extract failed: {e}")

    if best_rev is None:
        return None

    return {
        "annual_total_previous": best_prev,
        "annual_total_revised":  best_rev,
        "extraction_source":     "fitz_annual_total",
    }
