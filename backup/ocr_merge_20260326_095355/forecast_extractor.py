#!/usr/bin/env python3
"""forecast_extractor.py — 業績予想修正テキストからの値抽出

PDF/HTML/テキストから以下を抽出する:
- 前回予想 / 今回修正予想 の主要財務数値
- 変化率
- subtype (upward / downward / difference / neutral / undecided)
- importance スコア
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Optional

from .forecast_models import ForecastRevisionEvent
from .common_normalizers import normalize_jp_number, normalize_amount_to_million_yen, parse_number

logger = logging.getLogger("forecast_extractor")


# ============================================================
# テーブル行ラベルマッチ
# ============================================================
_PREVIOUS_LABELS = [
    "前回発表予想(a)",
    "前回発表予想（a）",
    "前回発表予想(A)",
    "前回発表予想（A）",
    "前回発表予想",
    "前回予想(a)",
    "前回予想（a）",
    "前回予想(A)",
    "前回予想（A）",
    "前回予想",
    "前回修正予想",
]

_REVISED_LABELS = [
    "今回修正予想(b)",
    "今回修正予想（b）",
    "今回修正予想(B)",
    "今回修正予想（B）",
    "今回修正予想",
    "今回予想(b)",
    "今回予想（b）",
    "今回予想(B)",
    "今回予想（B）",
    "今回予想",
    "修正予想",
    "修正後",
]

_ACTUAL_LABELS = [
    "実績(b)",
    "実績（b）",
    "実績(B)",
    "実績（B）",
    "実績値",
    "実績",
    "実績 (b)",
    "実績 (B)",
]

_DELTA_LABELS = [
    "増減額(b-a)",
    "増減額（b-a）",
    "増減額(B-A)",
    "増減額（B-A）",
    "増減額",
]

_CHANGE_PCT_LABELS = [
    "増減率(%)",
    "増減率（%）",
    "増減率(％)",
    "増減率（％）",
    "増減率",
]

_REFERENCE_LABELS = [
    "（ご参考）",
    "(ご参考)",
    "前期実績",
    "参考",
]


# ============================================================
# 項目名パターン（行ラベル＝縦型テーブル用）
# ============================================================
_ITEM_PATTERNS = {
    "sales": [
        "売上高", "売上収益", "営業収益", "経営収益",
        "事業収益", "医業収益",
    ],
    "op": [
        "営業利益", "事業利益", "営業損益", "営業損失",
        "営業損失（△）",
    ],
    "ordinary": [
        "経常利益", "経常損益", "経常損失",
        "経常損失（△）", "税引前利益",
    ],
    "net_income": [
        "親会社株主に帰属する当期純利益",
        "親会社株主に帰属する四半期純利益",
        "親会社に帰属する当期純利益",
        "親会社株主に帰属する当期純損益",
        "親会社株主に帰属する純利益",
        "当期純利益", "四半期純利益",
        "当期純損益", "当期利益",
        "当期純損失",
    ],
    "eps": [
        "1株当たり当期純利益",
        "１株当たり当期純利益",
        "1株当たり四半期純利益",
        "１株当たり四半期純利益",
        "1株当たり純利益",
        "１株当たり純利益",
        "1株当たり当期純損益",
        "１株当たり当期純損益",
        "eps",
    ],
}

# Phase 2.5: 短縮別名ラベル（CMap文字化け耐性向上）
# 漢字のみ抽出後に部分一致でマッチするための短いキーワード
_SHORT_LABEL_MAP = {
    "売上": "sales",
    "収益": "sales",
    "営利": "op",
    "営業利": "op",
    "経常": "ordinary",
    "経利": "ordinary",
    "税引前": "ordinary",
    "純利": "net_income",
    "純損": "net_income",
    "当期純": "net_income",
    "1株": "eps",
}


# ============================================================
# 期間・基準判定
# ============================================================
_PERIOD_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*期\s*(通期|第[1-4]四半期(?:累計)?|中間期|上半期)"
)
_FY_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*期")
_QUARTER_RE = re.compile(r"第(\d)四半期")
_BASIS_RE = re.compile(r"(連結|個別)")

# 単位検出
_UNIT_RE = re.compile(
    r"(?:単位\s*[：:]\s*|[(（])?(百万円|千円|億円|円(?!\s*[建以]))[)）]?"
)


def _detect_period(text: str) -> str:
    m = _PERIOD_RE.search(text)
    if m:
        return f"{m.group(1)}年{m.group(2)}月期 {m.group(3)}"
    m = _FY_RE.search(text)
    if m:
        return f"{m.group(1)}年{m.group(2)}月期"
    return ""


def _detect_basis(text: str) -> str:
    m = _BASIS_RE.search(text)
    return m.group(1) if m else ""


def _detect_unit(text: str) -> str:
    """テキストから金額単位を検出"""
    m = _UNIT_RE.search(text)
    return m.group(1) if m else ""


# ============================================================
# テキスト正規化
# ============================================================
def _normalize_text(text: str) -> str:
    """全角→半角、NFKC正規化"""
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("\u3000", " ")  # 全角スペース
    return s


def _normalize_label(text: str) -> str:
    """Phase 2.5: ラベル文字列を正規化して CMap文字化け耐性を上げる。

    具体的には:
    - NFKC正規化
    - 制御文字・ゼロ幅文字除去
    - 空白圧縮
    - 漢字・かな・カナ・英数字のみ保持
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    # 制御文字 (Cc, Cf) とゼロ幅文字を除去
    s = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad]', '', s)
    s = ''.join(c for c in s if unicodedata.category(c) not in ('Cc', 'Cf') or c in ('\n', '\t'))
    # 連続空白を単一スペースに
    s = re.sub(r'\s+', ' ', s).strip()
    return s


# ============================================================
# 数値抽出
# ============================================================
_DASH_CHARS = set("―-–—‐−")
_TRIANGLE_CHARS = set("△▲")


def _parse_cell_value(raw: str) -> float | None:
    """セル文字列を数値に変換。

    対応:
    - △1,234 → -1234.0
    - ▲123 → -123.0
    - -123 → -123.0
    - 1,234 → 1234.0
    - 123.4 → 123.4
    - ― / – / - (単独) → None
    - 空欄 → None
    """
    s = _normalize_text(raw).strip()
    if not s:
        return None
    # ―, –, — 単体またはハイフン単体は None
    if len(s) <= 2 and all(c in _DASH_CHARS | {" "} for c in s):
        return None
    # * 注記（先頭のみ）を除去
    s = re.sub(r"^[\*※注]+", "", s).strip()
    # 負号判定
    is_negative = False
    for t in _TRIANGLE_CHARS:
        if t in s:
            is_negative = True
            s = s.replace(t, "")
    if s.startswith("-") or s.startswith("−"):
        is_negative = True
        s = s.lstrip("-−")
    # カンマ除去
    s = s.replace(",", "").replace(" ", "")
    # パーセント除去
    s = s.rstrip("%％")
    if not s:
        return None
    try:
        val = float(s)
        return -val if is_negative else val
    except ValueError:
        return None


def _extract_numbers_from_line(line: str) -> list[float | None]:
    """行からすべての数値を抽出。

    正規表現で数値トークン（カンマ付き数値、△/▲付き負数、小数、ダッシュ）を
    行内から直接見つけて抽出する。スペース区切りに依存しない。
    """
    clean = _normalize_text(line)
    # 数値パターン: △/▲付き、カンマ付き整数、小数、ダッシュ(None値)
    # 年号(2025年, 4月)などを除外するため、数値の後に「年」「月」「日」が
    # 続くものは除外
    pattern = r'(?<![.\d])([△▲]?\s*[\d,]+\.?\d*%?|[―\-–—]{1,2})(?![年月日期四])'
    matches = re.findall(pattern, clean)
    nums = []
    for m in matches:
        m = m.strip()
        if not m:
            continue
        # 年号の一部（4桁のみの数字 + 周囲に年月が近い）をスキップ
        # → 簡易版: 4桁で2000-2099範囲は年号の可能性があるのでスキップ
        try:
            raw_val = m.replace(',', '').replace('△', '').replace('▲', '').rstrip('%')
            if raw_val and raw_val.replace('.', '').replace('-', '').isdigit():
                test_val = float(raw_val)
                if 2000 <= test_val <= 2099 and ',' not in m:
                    continue  # 年号スキップ
        except ValueError:
            pass
        val = _parse_cell_value(m)
        nums.append(val)
    return nums


# ============================================================
# 単位正規化 (Phase 2: table-level unit normalization)
# ============================================================
_UNIT_MULTIPLIER = {
    "百万円": 1.0,      # 内部保存は百万円
    "千円": 0.001,       # 千円 → 百万円
    "億円": 100.0,       # 億円 → 百万円
    "円": 0.000001,      # 円 → 百万円 (EPSには使わない)
}


def _apply_unit(val: float | None, unit: str, is_eps: bool = False) -> float | None:
    """単位を百万円に変換。EPSは円単位のまま返す"""
    if val is None:
        return None
    if is_eps:
        return val  # EPSは円単位のまま
    mult = _UNIT_MULTIPLIER.get(unit, 1.0)
    return val * mult


def _units_compatible(unit_a: str, unit_b: str) -> bool:
    """2つの unit 文字列が互換かどうかを判定する。

    同一 or 片方/両方が空(unknown) → 互換
    異なる既知 unit → 非互換
    """
    if not unit_a or not unit_b:
        return True  # unknown は許容
    return unit_a == unit_b


def _convert_unit_value(
    val: float | None, from_unit: str, to_unit: str, is_eps: bool = False
) -> float | None:
    """from_unit の値を to_unit に変換する。

    両方が既知 unit なら変換可能。片方が unknown なら変換不可(None)。
    """
    if val is None:
        return None
    if is_eps:
        return val
    if not from_unit or not to_unit:
        return None  # unknown → 変換不可
    from_mult = _UNIT_MULTIPLIER.get(from_unit)
    to_mult = _UNIT_MULTIPLIER.get(to_unit)
    if from_mult is None or to_mult is None:
        return None
    if from_mult == to_mult:
        return val
    # 一旦百万円にしてから target unit に変換
    in_million = val * from_mult
    return in_million / to_mult


# ============================================================
# 変化率計算
# ============================================================
def _calc_pct(previous: float | None, revised: float | None) -> float | None:
    if previous is None or revised is None:
        return None
    if previous == 0:
        if revised > 0:
            return 100.0
        elif revised < 0:
            return -100.0
        return 0.0
    return round((revised - previous) / abs(previous) * 100, 1)


def _calc_delta(previous: float | None, revised: float | None) -> float | None:
    if previous is None or revised is None:
        return None
    return round(revised - previous, 2)


# ============================================================
# 表構造の解析
# ============================================================
def _find_labeled_line(lines: list[str], labels: list[str], search_start: int = 0, search_end: int | None = None, require_numbers: bool = False) -> int | None:
    """指定ラベルを含む行インデックスを返す。

    require_numbers=True の場合、行に数値（またはダッシュ= None値）を含むことを要求する。
    これにより「実績値との差異」のようなタイトル行との誤マッチを防ぐ。
    """
    end = search_end or len(lines)
    for i in range(search_start, min(end, len(lines))):
        clean = _normalize_text(lines[i]).lower().strip()
        if not clean:
            continue
        for label in labels:
            if label.lower() in clean:
                if require_numbers:
                    # ラベルの後方に数値トークンが含まれることを要求
                    # 「2026年3月期...実績値との差異」のような行を除外
                    label_pos = clean.find(label.lower())
                    after_label = clean[label_pos + len(label):]
                    has_financial_number = bool(re.search(r"[\d,]{4,}", after_label))
                    has_triangle_number = bool(re.search(r"[△▲]\s*[\d,]+", after_label))
                    num_tokens = len(re.findall(r"[\d,.]+", after_label))
                    if has_financial_number or has_triangle_number or num_tokens >= 2:
                        return i
                else:
                    return i
    return None


def _find_table_region(lines: list[str]) -> tuple[int, int]:
    """表の開始行と終了行を推定（前回予想 or 修正予想が含まれるブロック）"""
    table_start = None
    table_end = None

    for i, line in enumerate(lines):
        clean = _normalize_text(line).lower()
        for label in _PREVIOUS_LABELS + _REVISED_LABELS + _ACTUAL_LABELS:
            if label.lower() in clean:
                if table_start is None:
                    table_start = max(0, i - 5)
                table_end = min(len(lines), i + 10)
                break

    if table_start is not None and table_end is not None:
        return table_start, table_end
    return 0, len(lines)


def _detect_column_order(lines: list[str], ref_idx: int) -> dict[str, int]:
    """ヘッダー行の列名から列順序を特定する。

    ref_idx の前15行を走査してヘッダー候補を探す。
    見つからなければデフォルト順序（売上,営利,経常,純利,EPS）。
    """
    default_map = {"sales": 0, "op": 1, "ordinary": 2, "net_income": 3, "eps": 4}

    search_start = max(0, ref_idx - 20)
    search_end = ref_idx
    if search_start >= search_end:
        return default_map

    for i in range(search_start, search_end):
        line = _normalize_text(lines[i]) if i < len(lines) else ""
        col_map = {}
        col_idx = 0
        for field_key, patterns in _ITEM_PATTERNS.items():
            for pat in patterns:
                if pat in line:
                    col_map[field_key] = col_idx
                    col_idx += 1
                    break

        if len(col_map) >= 3:
            return col_map

    return default_map


# ============================================================
# 横型テーブル抽出（PDF/テキスト共通）
# ============================================================
def _extract_from_horizontal_table(
    lines: list[str],
    is_difference: bool = False,
    unit: str = "百万円",
) -> dict:
    """横型テーブル（行=前回予想/修正予想、列=売上/営利/経常/純利/EPS）から抽出

    ラベルベース→数値パターンフォールバックの2段階で抽出。
    TDNETのPDFはCMap問題で日本語ラベルが文字化けすることがあるため、
    数値パターンだけで連続行を特定するフォールバックが必要。
    """
    result = {}

    table_start, table_end = _find_table_region(lines)
    region_lines = lines[table_start:table_end]

    # 表領域内で単位を再検出
    region_text = "\n".join(region_lines)
    detected_unit = _detect_unit(region_text)
    if detected_unit:
        unit = detected_unit

    # -- Phase 1: ラベルベース行検索 --
    prev_idx = _find_labeled_line(lines, _PREVIOUS_LABELS, table_start, table_end, require_numbers=True)
    revised_idx = _find_labeled_line(lines, _REVISED_LABELS, table_start, table_end, require_numbers=True)
    actual_idx = _find_labeled_line(lines, _ACTUAL_LABELS, table_start, table_end, require_numbers=True)
    delta_idx = _find_labeled_line(lines, _DELTA_LABELS, table_start, table_end, require_numbers=True)
    pct_idx = _find_labeled_line(lines, _CHANGE_PCT_LABELS, table_start, table_end, require_numbers=True)

    # 差異表の場合: 実績をrevised扱い
    if is_difference and actual_idx is not None:
        revised_idx = actual_idx

    # -- Phase 2: 数値パターンフォールバック --
    # ラベルが文字化けしていても、4+数値トークンの連続行を特定
    if prev_idx is None and revised_idx is None:
        numeric_rows = _find_numeric_rows(lines, table_start, table_end)
        if len(numeric_rows) >= 2:
            # トークン数が最も近いペアを選択
            best_pair = _select_best_pair(lines, numeric_rows)
            if best_pair:
                prev_idx, revised_idx = best_pair
                logger.debug(f"[FORECAST] Fallback: numeric rows at {prev_idx}, {revised_idx}")
                # delta/pct行は次の数値行
                rest = [r for r in numeric_rows if r > revised_idx]
                if rest:
                    delta_idx = rest[0]
                if len(rest) >= 2:
                    pct_idx = rest[1]

    # 列順特定
    ref_idx = prev_idx or revised_idx or table_start
    col_map = _detect_column_order(lines, ref_idx)

    # 数値抽出
    prev_nums = _extract_numbers_from_line(lines[prev_idx]) if prev_idx is not None else []
    revised_nums = _extract_numbers_from_line(lines[revised_idx]) if revised_idx is not None else []
    delta_nums = _extract_numbers_from_line(lines[delta_idx]) if delta_idx is not None else []
    pct_nums = _extract_numbers_from_line(lines[pct_idx]) if pct_idx is not None else []

    metrics_count = 0

    for field_key, col_idx in col_map.items():
        is_eps = (field_key == "eps")
        prev_val = prev_nums[col_idx] if col_idx < len(prev_nums) else None
        rev_val = revised_nums[col_idx] if col_idx < len(revised_nums) else None
        delta_val = delta_nums[col_idx] if col_idx < len(delta_nums) else None
        pct_val = pct_nums[col_idx] if col_idx < len(pct_nums) else None

        # 単位適用
        prev_val = _apply_unit(prev_val, unit, is_eps)
        rev_val = _apply_unit(rev_val, unit, is_eps)
        delta_val = _apply_unit(delta_val, unit, is_eps)

        # 変化率は行から取得 or 自前計算
        if pct_val is None and prev_val is not None and rev_val is not None:
            pct_val = _calc_pct(prev_val, rev_val)

        # delta を自前計算
        if delta_val is None and prev_val is not None and rev_val is not None:
            delta_val = _calc_delta(prev_val, rev_val)

        if rev_val is not None:
            metrics_count += 1

        result[f"previous_{field_key}"] = prev_val
        result[f"revised_{field_key}"] = rev_val
        result[f"delta_{field_key}"] = delta_val
        result[f"change_{field_key}_pct"] = pct_val

    result["metrics_count"] = metrics_count
    result["raw_table_text"] = "\n".join(
        lines[max(0, (prev_idx or revised_idx or table_start) - 2):
              min(len(lines), (pct_idx or delta_idx or revised_idx or prev_idx or table_end) + 3)]
    )

    return result


# 参考行の除外パターン（文字化けしていない場合用）
_REFERENCE_LABELS = ["ご参考", "参考", "前期実績", "前年同期", "前年度"]


def _find_numeric_rows(
    lines: list[str],
    search_start: int = 0,
    search_end: int | None = None,
    min_tokens: int = 4,
) -> list[int]:
    """4+個の数値トークンを持つ行のインデックスリストを返す。

    TDNETのPDFでラベルが文字化けしていても、前回予想/今回修正予想の行は
    「30,000 2,200 2,100 1,500 80.44」のように数値トークンが4個以上並ぶ。
    参考行（前期実績等）はスキップする。
    """
    end = search_end or len(lines)
    result = []
    for i in range(search_start, min(end, len(lines))):
        line = lines[i]
        # 参考行をスキップ
        clean = _normalize_text(line).lower()
        if any(ref in clean for ref in _REFERENCE_LABELS):
            continue
        nums = _extract_numbers_from_line(line)
        if len(nums) >= min_tokens:
            result.append(i)
    return result


def _select_best_pair(
    lines: list[str], numeric_rows: list[int],
) -> tuple[int, int] | None:
    """数値行リストから前回/修正のベストペアを選択する。

    選択基準:
    1. 隣接する行（行番号が近い）を優先
    2. トークン数が同じペアを優先
    3. 最初の2行をデフォルトとする
    """
    if len(numeric_rows) < 2:
        return None

    best = None
    best_score = -1

    for i in range(len(numeric_rows) - 1):
        r1 = numeric_rows[i]
        r2 = numeric_rows[i + 1]
        n1 = len(_extract_numbers_from_line(lines[r1]))
        n2 = len(_extract_numbers_from_line(lines[r2]))
        gap = r2 - r1

        # スコア: トークン数一致ボーナス + 近接ボーナス
        score = 0
        if n1 == n2:
            score += 10
        if gap <= 2:
            score += 5
        elif gap <= 4:
            score += 2

        if score > best_score:
            best_score = score
            best = (r1, r2)

    return best or (numeric_rows[0], numeric_rows[1])


# ============================================================
# 連結/個別テキストセクション分離 (Phase 1.5)
# ============================================================

# セクション境界キーワード
_TEXT_SECTION_CONSOLIDATED = ["連結業績予想", "連結予想", "連結業績", "連結経営成績"]
_TEXT_SECTION_NON_CONSOLIDATED = ["個別業績予想", "個別予想", "個別業績", "個別経営成績", "単体業績予想", "単体予想", "単体業績"]
_TEXT_SECTION_BOUNDARY = _TEXT_SECTION_CONSOLIDATED + _TEXT_SECTION_NON_CONSOLIDATED


def _classify_text_line(line: str) -> str | None:
    """テキスト行がセクション境界かどうかを判定する。

    Returns: "consolidated" | "non_consolidated" | None (境界でない)
    """
    clean = _normalize_text(line).strip()
    if not clean:
        return None

    # 個別を先に判定（「連結」が「個別」の後に来ることがあるため）
    for kw in _TEXT_SECTION_NON_CONSOLIDATED:
        if kw in clean:
            return "non_consolidated"
    for kw in _TEXT_SECTION_CONSOLIDATED:
        if kw in clean:
            return "consolidated"

    # 単純キーワード（見出し行っぽい場合のみ）
    # 行が短い（見出し的）場合のみ簡易判定
    if len(clean) < 30:
        if "個別" in clean and ("業績" in clean or "予想" in clean or "成績" in clean):
            return "non_consolidated"
        if "単体" in clean and ("業績" in clean or "予想" in clean or "成績" in clean):
            return "non_consolidated"
        if "連結" in clean and ("業績" in clean or "予想" in clean or "成績" in clean):
            return "consolidated"

    return None


def _find_text_sections(lines: list[str]) -> list[dict]:
    """テキスト行を連結/個別/不明セクションに分割する。

    Returns: list of {"type": str, "start": int, "end": int}
        type: "consolidated" | "non_consolidated" | "unknown"
    """
    boundaries = []  # list of (line_idx, type)

    for i, line in enumerate(lines):
        section_type = _classify_text_line(line)
        if section_type is not None:
            boundaries.append((i, section_type))

    if not boundaries:
        return [{"type": "unknown", "start": 0, "end": len(lines)}]

    sections = []

    # 最初のセクション境界の前がある場合は unknown
    if boundaries[0][0] > 0:
        sections.append({
            "type": "unknown",
            "start": 0,
            "end": boundaries[0][0],
        })

    # 各境界からのセクション
    for idx, (line_idx, section_type) in enumerate(boundaries):
        if idx + 1 < len(boundaries):
            end = boundaries[idx + 1][0]
        else:
            end = len(lines)
        sections.append({
            "type": section_type,
            "start": line_idx,
            "end": end,
        })

    return sections


def _select_target_sections(
    sections: list[dict],
    target_type: str,
) -> list[dict]:
    """target_type に一致するセクションを選択する。

    Returns: 使用すべきセクションのリスト
    """
    if target_type != "unknown":
        primary = [s for s in sections if s["type"] == target_type]
        if primary:
            return primary
        # 一致なし → unknown のみ
        fallback = [s for s in sections if s["type"] == "unknown"]
        return fallback if fallback else sections[:1]

    # target_type unknown: unknown セクション優先
    unknown = [s for s in sections if s["type"] == "unknown"]
    if unknown:
        return unknown

    # unknown なし → 全セクション中最も多い type
    from collections import Counter
    type_counts = Counter(s["type"] for s in sections)
    if type_counts:
        most_common = type_counts.most_common(1)[0][0]
        return [s for s in sections if s["type"] == most_common]

    return sections


def _find_consolidated_section(lines: list[str]) -> tuple[int, int]:
    """連結セクションの開始/終了行を推定。個別もあれば個別セクションは除外。

    後方互換ラッパー: _find_text_sections() を内部で呼び出す。
    """
    sections = _find_text_sections(lines)

    # 連結セクションがあればそれを返す
    for s in sections:
        if s["type"] == "consolidated":
            return s["start"], s["end"]

    # 連結セクションなし → 全体
    return 0, len(lines)


# ============================================================
# Phase 0: pdfplumber extract_tables() からの構造化抽出
# ============================================================

# テーブル選定用: 除外テーブルキーワード
_TABLE_EXCLUDE_KEYWORDS = ["株主", "配当", "議決権", "持株", "役員", "取締役", "監査"]

# previous/revised ラベル判定
_TBL_PREVIOUS_KEYWORDS = ["前回", "従来", "修正前"]
_TBL_REVISED_KEYWORDS = ["今回", "修正", "最新", "修正後"]
_TBL_ACTUAL_KEYWORDS = ["実績"]
_TBL_DELTA_KEYWORDS = ["増減額", "増減"]
_TBL_PCT_KEYWORDS = ["増減率", "%", "％"]
_TBL_REFERENCE_KEYWORDS = ["参考", "前期", "前年"]


def _normalize_cell(val) -> str | None:
    """Phase 2.5: テーブルセル値を正規化する。

    - None / 空文字 → None
    - 改行除去、空白トリム
    - NFKC正規化
    - 制御文字・ゼロ幅文字除去
    """
    if val is None:
        return None
    s = str(val)
    s = s.replace("\n", "").replace("\r", "")
    s = _normalize_label(s)
    if not s:
        return None
    return s


def _match_metric_label(text: str, already_matched: set[str] | None = None) -> str | None:
    """Phase 2.5: ラベル文字列を指標名にマッチする（多段階）。

    Tier 1: _ITEM_PATTERNS の完全/部分一致
    Tier 2: _normalize_label 後に _SHORT_LABEL_MAP で短縮マッチ

    Returns: 指標キー ("sales"/"op"/"ordinary"/"net_income"/"eps") or None
    """
    if not text:
        return None
    skip = already_matched or set()

    # Tier 1: 正規パターン一致
    for field_key, patterns in _ITEM_PATTERNS.items():
        if field_key in skip:
            continue
        if any(p in text for p in patterns):
            return field_key

    # Tier 2: 正規化後の短縮マッチ
    norm = _normalize_label(text)
    for short_kw, field_key in _SHORT_LABEL_MAP.items():
        if field_key in skip:
            continue
        if short_kw in norm:
            logger.debug(f"[LABEL-P2.5] short match: '{text[:30]}' -> {field_key} via '{short_kw}'")
            return field_key

    return None


def _is_forecast_table(table: list[list]) -> bool:
    """テーブルが業績予想修正表かどうかを判定する。

    判定基準:
    - 数値セルが5個以上
    - 指標ヘッダーが2個以上（_ITEM_PATTERNS ベース）
    - 除外キーワードを含まない
    """
    if not table or len(table) < 2:
        return False

    all_text = ""
    num_count = 0
    for row in table:
        if not row:
            continue
        for cell in row:
            s = _normalize_cell(cell)
            if s is None:
                continue
            all_text += s + " "
            if _parse_cell_value(s) is not None:
                num_count += 1

    for kw in _TABLE_EXCLUDE_KEYWORDS:
        if kw in all_text:
            return False

    if num_count < 5:
        return False

    # _ITEM_PATTERNS ベースで指標ヘッダーをカウント
    metric_hit = 0
    for patterns in _ITEM_PATTERNS.values():
        for p in patterns:
            if p in all_text:
                metric_hit += 1
                break
    if metric_hit < 2:
        return False

    return True


def _detect_table_orientation(table: list[list]) -> str:
    """テーブルが行方向(horizontal)か列方向(vertical)かを判定する。

    horizontal: ヘッダーが横一列（列=指標）、行=previous/revised
    vertical:   ヘッダーが縦一列（行=指標）、列=previous/revised
    """
    if not table or len(table) < 2:
        return "horizontal"

    header_hits_row = 0
    for row in table[:3]:
        hits = 0
        if not row:
            continue
        for cell in row:
            s = _normalize_cell(cell)
            if s is None:
                continue
            for patterns in _ITEM_PATTERNS.values():
                if any(p in s for p in patterns):
                    hits += 1
                    break
        header_hits_row = max(header_hits_row, hits)

    header_hits_col = 0
    for row in table:
        if not row:
            continue
        for cell in row[:2]:
            s = _normalize_cell(cell)
            if s is None:
                continue
            for patterns in _ITEM_PATTERNS.values():
                if any(p in s for p in patterns):
                    header_hits_col += 1
                    break
            break

    if header_hits_col >= 2 and header_hits_col > header_hits_row:
        return "vertical"
    return "horizontal"


def _classify_row_label(text: str) -> str | None:
    """行/列ラベルを分類する。"""
    if not text:
        return None
    s = _normalize_text(text).strip()
    for kw in _TBL_REFERENCE_KEYWORDS:
        if kw in s:
            return "reference"
    for kw in _TBL_PREVIOUS_KEYWORDS:
        if kw in s:
            return "previous"
    for kw in _TBL_REVISED_KEYWORDS:
        if kw in s:
            return "revised"
    for kw in _TBL_ACTUAL_KEYWORDS:
        if kw in s:
            return "actual"
    for kw in _TBL_DELTA_KEYWORDS:
        if kw in s:
            return "delta"
    for kw in _TBL_PCT_KEYWORDS:
        if kw in s:
            return "pct"
    return None


def _find_metric_columns_horizontal(table: list[list]) -> dict[str, int]:
    """Phase 2.5: 横型テーブルのヘッダー行から指標の列インデックスを特定する。

    多段階マッチ: _match_metric_label() で Tier1/Tier2 を実行。
    """
    for row_idx in range(min(3, len(table))):
        row = table[row_idx]
        if not row:
            continue
        col_map = {}
        for col_idx, cell in enumerate(row):
            s = _normalize_cell(cell)
            if s is None:
                continue
            matched = _match_metric_label(s, already_matched=set(col_map.keys()))
            if matched:
                col_map[matched] = col_idx
        if len(col_map) >= 2:
            return col_map
    return {}


def _find_metric_rows_vertical(table: list[list]) -> dict[str, int]:
    """Phase 2.5: 縦型テーブルの左列から指標の行インデックスを特定する。

    多段階マッチ: _match_metric_label() で Tier1/Tier2 を実行。
    """
    row_map = {}
    for row_idx, row in enumerate(table):
        if not row:
            continue
        label = None
        for cell in row[:2]:
            s = _normalize_cell(cell)
            if s is not None:
                label = s
                break
        if label is None:
            continue
        matched = _match_metric_label(label, already_matched=set(row_map.keys()))
        if matched:
            row_map[matched] = row_idx
    return row_map


def _extract_horizontal_table_data(
    table: list[list], col_map: dict[str, int], unit: str, is_difference: bool = False,
) -> dict:
    """横型テーブル（行=previous/revised, 列=指標）からデータ抽出。"""
    result = {}
    prev_row_idx = None
    revised_row_idx = None
    delta_row_idx = None
    pct_row_idx = None

    numeric_rows = []
    for row_idx, row in enumerate(table):
        if not row:
            continue
        label_text = ""
        for cell in row[:2]:
            s = _normalize_cell(cell)
            if s is not None:
                label_text += s + " "

        label_type = _classify_row_label(label_text)

        num_count = 0
        for col_idx in col_map.values():
            if col_idx < len(row):
                s = _normalize_cell(row[col_idx])
                if s is not None and _parse_cell_value(s) is not None:
                    num_count += 1

        if label_type == "previous":
            prev_row_idx = row_idx
        elif label_type == "revised":
            revised_row_idx = row_idx
        elif label_type == "actual":
            if is_difference:
                revised_row_idx = row_idx
        elif label_type == "delta":
            delta_row_idx = row_idx
        elif label_type == "pct":
            pct_row_idx = row_idx
        elif label_type == "reference":
            continue

        if num_count >= 2 and label_type not in ("delta", "pct", "reference"):
            numeric_rows.append((row_idx, num_count, label_type))

    # ラベルで見つからない場合、数値パターンで推定
    if prev_row_idx is None and revised_row_idx is None:
        data_rows = [
            (idx, cnt, lt) for idx, cnt, lt in numeric_rows
            if lt not in ("delta", "pct", "reference")
        ]
        if len(data_rows) >= 2:
            prev_row_idx = data_rows[0][0]
            revised_row_idx = data_rows[1][0]
            logger.debug(f"[TABLE-P0] numeric pattern: prev={prev_row_idx} rev={revised_row_idx}")
        elif len(data_rows) == 1:
            revised_row_idx = data_rows[0][0]

    if prev_row_idx is None and revised_row_idx is not None:
        for idx, cnt, lt in numeric_rows:
            if idx < revised_row_idx and lt not in ("delta", "pct", "reference"):
                prev_row_idx = idx
                break

    metrics_count = 0
    has_pair = False

    for field_key, col_idx in col_map.items():
        is_eps = (field_key == "eps")
        prev_val = None
        rev_val = None

        if prev_row_idx is not None and prev_row_idx < len(table):
            row = table[prev_row_idx]
            if col_idx < len(row):
                s = _normalize_cell(row[col_idx])
                if s is not None:
                    prev_val = _parse_cell_value(s)

        if revised_row_idx is not None and revised_row_idx < len(table):
            row = table[revised_row_idx]
            if col_idx < len(row):
                s = _normalize_cell(row[col_idx])
                if s is not None:
                    rev_val = _parse_cell_value(s)

        prev_val = _apply_unit(prev_val, unit, is_eps)
        rev_val = _apply_unit(rev_val, unit, is_eps)

        delta_val = None
        pct_val = None
        if delta_row_idx is not None and delta_row_idx < len(table):
            row = table[delta_row_idx]
            if col_idx < len(row):
                s = _normalize_cell(row[col_idx])
                if s is not None:
                    delta_val = _parse_cell_value(s)
                    delta_val = _apply_unit(delta_val, unit, is_eps)

        if pct_row_idx is not None and pct_row_idx < len(table):
            row = table[pct_row_idx]
            if col_idx < len(row):
                s = _normalize_cell(row[col_idx])
                if s is not None:
                    pct_val = _parse_cell_value(s)

        if pct_val is None and prev_val is not None and rev_val is not None:
            pct_val = _calc_pct(prev_val, rev_val)
        if delta_val is None and prev_val is not None and rev_val is not None:
            delta_val = _calc_delta(prev_val, rev_val)

        if rev_val is not None:
            metrics_count += 1
        if prev_val is not None and rev_val is not None:
            has_pair = True

        result[f"previous_{field_key}"] = prev_val
        result[f"revised_{field_key}"] = rev_val
        result[f"delta_{field_key}"] = delta_val
        result[f"change_{field_key}_pct"] = pct_val

    result["metrics_count"] = metrics_count
    result["has_pair"] = has_pair
    return result


def _extract_vertical_table_data(
    table: list[list], row_map: dict[str, int], unit: str, is_difference: bool = False,
) -> dict:
    """縦型テーブル（行=指標, 列=previous/revised）からデータ抽出。"""
    result = {}

    prev_col_idx = None
    revised_col_idx = None
    delta_col_idx = None
    pct_col_idx = None

    for row in table[:3]:
        if not row:
            continue
        for col_idx, cell in enumerate(row):
            s = _normalize_cell(cell)
            if s is None:
                continue
            label_type = _classify_row_label(s)
            if label_type == "previous":
                prev_col_idx = col_idx
            elif label_type == "revised":
                revised_col_idx = col_idx
            elif label_type == "actual" and is_difference:
                revised_col_idx = col_idx
            elif label_type == "delta":
                delta_col_idx = col_idx
            elif label_type == "pct":
                pct_col_idx = col_idx

    if prev_col_idx is None and revised_col_idx is None:
        col_num_counts = {}
        for field_key, row_idx in row_map.items():
            if row_idx >= len(table):
                continue
            row = table[row_idx]
            for col_idx, cell in enumerate(row):
                if col_idx < 1:
                    continue
                s = _normalize_cell(cell)
                if s is not None and _parse_cell_value(s) is not None:
                    col_num_counts[col_idx] = col_num_counts.get(col_idx, 0) + 1

        data_cols = sorted(col_num_counts.items(), key=lambda x: (-x[1], x[0]))
        if len(data_cols) >= 2:
            cols_sorted = sorted([data_cols[0][0], data_cols[1][0]])
            prev_col_idx = cols_sorted[0]
            revised_col_idx = cols_sorted[1]
        elif len(data_cols) == 1:
            revised_col_idx = data_cols[0][0]

    metrics_count = 0
    has_pair = False

    for field_key, row_idx in row_map.items():
        is_eps = (field_key == "eps")
        if row_idx >= len(table):
            continue
        row = table[row_idx]

        prev_val = None
        rev_val = None
        delta_val = None
        pct_val = None

        if prev_col_idx is not None and prev_col_idx < len(row):
            s = _normalize_cell(row[prev_col_idx])
            if s is not None:
                prev_val = _parse_cell_value(s)

        if revised_col_idx is not None and revised_col_idx < len(row):
            s = _normalize_cell(row[revised_col_idx])
            if s is not None:
                rev_val = _parse_cell_value(s)

        if delta_col_idx is not None and delta_col_idx < len(row):
            s = _normalize_cell(row[delta_col_idx])
            if s is not None:
                delta_val = _parse_cell_value(s)

        if pct_col_idx is not None and pct_col_idx < len(row):
            s = _normalize_cell(row[pct_col_idx])
            if s is not None:
                pct_val = _parse_cell_value(s)

        prev_val = _apply_unit(prev_val, unit, is_eps)
        rev_val = _apply_unit(rev_val, unit, is_eps)
        delta_val = _apply_unit(delta_val, unit, is_eps)

        if pct_val is None and prev_val is not None and rev_val is not None:
            pct_val = _calc_pct(prev_val, rev_val)
        if delta_val is None and prev_val is not None and rev_val is not None:
            delta_val = _calc_delta(prev_val, rev_val)

        if rev_val is not None:
            metrics_count += 1
        if prev_val is not None and rev_val is not None:
            has_pair = True

        result[f"previous_{field_key}"] = prev_val
        result[f"revised_{field_key}"] = rev_val
        result[f"delta_{field_key}"] = delta_val
        result[f"change_{field_key}_pct"] = pct_val

    result["metrics_count"] = metrics_count
    result["has_pair"] = has_pair
    return result

# ============================================================
# Phase 1: テーブル連結/個別分類 & ガード
# ============================================================

# 連結/個別キーワード
_CONSOLIDATED_KEYWORDS = ["連結", "連結業績", "連結予想", "連結業績予想"]
_NON_CONSOLIDATED_KEYWORDS = ["個別", "非連結", "単体", "個別業績", "個別予想", "個別業績予想"]


def _classify_table_context(table: list[list]) -> str:
    """テーブルの連結/個別種別を分類する。

    Returns: "consolidated" | "non_consolidated" | "unknown"
    """
    # テーブル全テキストを結合（上位3行を重み付けで見る）
    header_text = ""
    all_text = ""
    for row_idx, row in enumerate(table):
        if not row:
            continue
        for cell in row:
            s = _normalize_cell(cell)
            if s is None:
                continue
            all_text += s + " "
            if row_idx < 3:
                header_text += s + " "

    has_consolidated = any(kw in header_text for kw in _CONSOLIDATED_KEYWORDS)
    has_non_consolidated = any(kw in header_text for kw in _NON_CONSOLIDATED_KEYWORDS)

    # ヘッダーで判定できない場合は全テキスト
    if not has_consolidated and not has_non_consolidated:
        has_consolidated = any(kw in all_text for kw in _CONSOLIDATED_KEYWORDS)
        has_non_consolidated = any(kw in all_text for kw in _NON_CONSOLIDATED_KEYWORDS)

    if has_consolidated and not has_non_consolidated:
        return "consolidated"
    if has_non_consolidated and not has_consolidated:
        return "non_consolidated"
    # 両方あり or 両方なし → unknown
    return "unknown"


def _infer_target_type(title: str, full_text: str, table_types: list[str]) -> str:
    """ドキュメント全体から target table type を推定する。

    優先順位:
    1. タイトル
    2. 本文冒頭
    3. テーブル群の多数決
    4. unknown

    Returns: "consolidated" | "non_consolidated" | "unknown"
    """
    norm_title = _normalize_text(title).strip() if title else ""
    norm_text = _normalize_text(full_text[:2000]).strip() if full_text else ""

    # 1. タイトル判定
    title_has_cons = any(kw in norm_title for kw in _CONSOLIDATED_KEYWORDS)
    title_has_nonc = any(kw in norm_title for kw in _NON_CONSOLIDATED_KEYWORDS)
    if title_has_cons and not title_has_nonc:
        return "consolidated"
    if title_has_nonc and not title_has_cons:
        return "non_consolidated"

    # 2. 本文冒頭判定
    text_has_cons = any(kw in norm_text for kw in _CONSOLIDATED_KEYWORDS)
    text_has_nonc = any(kw in norm_text for kw in _NON_CONSOLIDATED_KEYWORDS)
    if text_has_cons and not text_has_nonc:
        return "consolidated"
    if text_has_nonc and not text_has_cons:
        return "non_consolidated"

    # 3. テーブル多数決
    cons_count = sum(1 for t in table_types if t == "consolidated")
    nonc_count = sum(1 for t in table_types if t == "non_consolidated")
    if cons_count > 0 and nonc_count == 0:
        return "consolidated"
    if nonc_count > 0 and cons_count == 0:
        return "non_consolidated"

    return "unknown"


def _extract_from_pdf_tables(
    tables: list[list[list]] | None,
    unit: str = "百万円",
    is_difference: bool = False,
    title: str = "",
    full_text: str = "",
) -> dict:
    """pdfplumber extract_tables() の結果から業績予想修正データを抽出する。

    Phase 0: テーブル構造データを最優先で処理する。
    Phase 1: 連結/個別フィルタ + pair guard でprecision向上。
    """
    if not tables:
        return {"metrics_count": 0, "has_pair": False}

    # ---- Step 1: テーブル分類 & フィルタ ----
    classified = []  # list of (table, table_type, detected_unit, table_idx)
    for idx, table in enumerate(tables):
        if not _is_forecast_table(table):
            continue
        tbl_type = _classify_table_context(table)
        table_text = " ".join(
            _normalize_cell(cell) or ""
            for row in table for cell in (row or [])
        )
        tbl_detected_unit = _detect_unit(table_text)
        classified.append((table, tbl_type, tbl_detected_unit, idx))

    if not classified:
        return {"metrics_count": 0, "has_pair": False}

    # ---- Step 2: target type 推定 ----
    table_types = [c[1] for c in classified]
    target_type = _infer_target_type(title, full_text, table_types)

    logger.debug(
        f"[TABLE-P1] target_type={target_type} "
        f"table_types={table_types} title={title[:40]!r}"
    )

    # ---- Step 3: テーブル選定 ----
    if target_type != "unknown":
        primary = [c for c in classified if c[1] == target_type]
        fallback = [c for c in classified if c[1] == "unknown"]
        use_tables = primary if primary else fallback
    else:
        # target_type unknown: unknown テーブルを優先、なければ全テーブル
        unknown_tables = [c for c in classified if c[1] == "unknown"]
        if unknown_tables:
            use_tables = unknown_tables
        elif len(set(table_types)) == 1:
            # 全テーブルが同じtype → 全部使う
            use_tables = classified
        else:
            # 混在している場合は最も多いtypeを使う
            from collections import Counter
            type_counts = Counter(table_types)
            most_common = type_counts.most_common(1)[0][0]
            use_tables = [c for c in classified if c[1] == most_common]

    logger.debug(
        f"[TABLE-P1] using {len(use_tables)}/{len(classified)} tables"
    )

    # ---- Step 4: 各テーブルから抽出 + pair guard ----
    best_result = {"metrics_count": 0, "has_pair": False}

    for table, tbl_type, tbl_detected_unit, tbl_idx in use_tables:
        tbl_unit = tbl_detected_unit or unit

        orientation = _detect_table_orientation(table)

        if orientation == "horizontal":
            col_map = _find_metric_columns_horizontal(table)
            if len(col_map) < 2:
                continue
            result = _extract_horizontal_table_data(table, col_map, tbl_unit, is_difference)
        else:
            row_map = _find_metric_rows_vertical(table)
            if len(row_map) < 2:
                continue
            result = _extract_vertical_table_data(table, row_map, tbl_unit, is_difference)

        # 結果にメタデータを付与
        result["_table_type"] = tbl_type
        result["_detected_unit"] = tbl_detected_unit
        result["_table_idx"] = tbl_idx

        if result.get("has_pair") and not best_result.get("has_pair"):
            best_result = result
        elif result.get("has_pair") == best_result.get("has_pair"):
            if result.get("metrics_count", 0) > best_result.get("metrics_count", 0):
                best_result = result

    # ---- Step 5: 複数テーブル間 pair guard ----
    # best_result に prev/rev が揃っている場合、同一テーブルから来たことを確認
    # (Phase 0は単一テーブル内で prev/rev を組むので、このガードは
    #  将来複数テーブル間の補完を入れた際のセーフティネット)
    if best_result.get("has_pair"):
        # 同一テーブル内で完結しているので OK
        det_unit = best_result.get('_detected_unit', '')
        doc_unit = unit
        if det_unit and doc_unit and det_unit != doc_unit:
            logger.info(
                f"[TABLE-P2] unit mismatch: table_unit={det_unit} "
                f"doc_unit={doc_unit} table_idx={best_result.get('_table_idx')}"
            )
            # テーブル独自unit で抽出済みなので、doc_unit との不一致はログのみ
            # (テーブル内 prev/rev は同一unit なので pair 自体は安全)
        logger.debug(
            f"[TABLE-P1] pair accepted: table_idx={best_result.get('_table_idx')} "
            f"type={best_result.get('_table_type')} "
            f"unit={det_unit}"
        )

    return best_result


# ============================================================
# 異常値検出 — 指標別無効化
# ============================================================
def _sanitize_metrics(table_result: dict) -> dict:
    """抽出された数値の妥当性チェック。

    異常値が検出された指標のみNone化し、イベント全体は保持する。
    チェック対象:
    - 前回→修正で100倍以上の変化 → 単位ミスの疑い
    - 売上高/利益が0.01未満（百万円単位で）→ 千円値を百万円変換した疑い
    - prev/revの絶対値比率が100倍超 → 行ずれ・単位ずれ
    """
    FINANCIAL_METRICS = ["sales", "op", "ordinary", "net_income"]
    valid_count = 0

    for metric in FINANCIAL_METRICS:
        prev_key = f"previous_{metric}"
        rev_key = f"revised_{metric}"
        delta_key = f"delta_{metric}"
        pct_key = f"change_{metric}_pct"

        prev = table_result.get(prev_key)
        rev = table_result.get(rev_key)

        if rev is None:
            continue

        anomaly = False

        # チェック1: 極小値（千円が百万円変換されたら 0.001 等になる）
        if rev is not None and 0 < abs(rev) < 0.01:
            anomaly = True
            logger.debug(f"[SANITIZE] {metric}: rev={rev} too small, likely unit error")

        # チェック2: 前回→修正で100倍以上の比率変化
        if prev is not None and rev is not None and prev != 0:
            ratio = abs(rev / prev)
            if ratio > 100 or ratio < 0.01:
                anomaly = True
                logger.debug(f"[SANITIZE] {metric}: prev={prev} rev={rev} ratio={ratio:.1f} anomaly")

        # チェック3: 変化率が異常に大きい（単位ミスの疑い）
        pct = table_result.get(pct_key)
        if metric == "sales":
            # 売上で300%超は単位ミス
            if pct is not None and abs(pct) > 300:
                anomaly = True
                logger.debug(f"[SANITIZE] sales pct={pct}% too high, likely unit error")
        else:
            # 利益系で1000%超は単位ミスの疑い
            if pct is not None and abs(pct) > 1000:
                anomaly = True
                logger.debug(f"[SANITIZE] {metric} pct={pct}% too high, likely unit error")

        # チェック4: prev/rev の絶対値比率が50倍超（Phase 2: 閾値引き下げ）
        if not anomaly and prev is not None and rev is not None:
            if prev != 0 and rev != 0:
                val_ratio = abs(rev / prev)
                if val_ratio > 50 or val_ratio < 0.02:
                    anomaly = True
                    logger.debug(
                        f"[SANITIZE-P2] {metric}: prev={prev} rev={rev} "
                        f"ratio={val_ratio:.1f} > 50x, likely unit error"
                    )

        if anomaly:
            table_result[prev_key] = None
            table_result[rev_key] = None
            table_result[delta_key] = None
            table_result[pct_key] = None
        else:
            valid_count += 1

    # EPSは別チェック: 極端に小さい値（百万円変換された疑い）
    eps_rev = table_result.get("revised_eps")
    if eps_rev is not None and 0 < abs(eps_rev) < 0.001:
        table_result["previous_eps"] = None
        table_result["revised_eps"] = None
        table_result["delta_eps"] = None
        table_result["change_eps_pct"] = None
    elif eps_rev is not None:
        valid_count += 1

    table_result["metrics_count"] = valid_count
    return table_result


# ============================================================
# subtype 判定 (Phase 1.6: weighted scoring)
# ============================================================

# 各指標の重み
_METRIC_WEIGHTS = {
    "net_income": 4,
    "op": 3,
    "ordinary": 2,
    "sales": 1,
}

# 方向判定の閾値 (変化率 %)
_DIRECTION_THRESHOLD = 5.0
_TURNAROUND_BONUS = 2  # 黒字転換/赤字転落のボーナスweight
_TITLE_PRIOR_WEIGHT = 1  # タイトルキーワードの弱いprior


def _metric_direction(prev: float | None, rev: float | None, pct: float | None) -> str:
    """単一指標の方向を判定する。

    Returns: "upward" | "downward" | "flat" | "unknown"
    """
    if prev is None or rev is None:
        if pct is not None:
            if pct > _DIRECTION_THRESHOLD:
                return "upward"
            elif pct < -_DIRECTION_THRESHOLD:
                return "downward"
            else:
                return "flat"
        return "unknown"

    if prev < 0 and rev > 0:
        return "upward"  # 黒字転換
    if prev > 0 and rev < 0:
        return "downward"  # 赤字転落

    if pct is not None:
        if pct > _DIRECTION_THRESHOLD:
            return "upward"
        elif pct < -_DIRECTION_THRESHOLD:
            return "downward"
        else:
            return "flat"

    # pct なし → prev/rev 比較
    if rev > prev:
        return "upward"
    elif rev < prev:
        return "downward"
    return "flat"


def _is_turnaround(prev: float | None, rev: float | None) -> bool:
    """黒字転換 or 赤字転落かどうか"""
    if prev is None or rev is None:
        return False
    return (prev < 0 and rev > 0) or (prev > 0 and rev < 0)


def _determine_subtype(
    event: ForecastRevisionEvent,
    is_difference: bool = False,
    title: str = "",
) -> str:
    """複数指標の weighted scoring で subtype を判定する。

    Phase 1.6: 単一指標依存から複数指標整合ベースへ改善。

    判定フロー:
      1. is_difference フラグ → "difference"
      2. 各指標の方向を算出し、weight × direction でスコア集計
      3. 黒字転換/赤字転落にはボーナスweight
      4. タイトルキーワードを弱いpriorとして加点
      5. スコア差で判定: 明確優勢 → upward/downward, 拮抗 → neutral
      6. スコア不足 → タイトルフォールバック → undecided
    """
    if is_difference or event.is_difference_disclosure:
        return "difference"

    up_score = 0.0
    down_score = 0.0
    known_count = 0

    # ---- 各指標の方向スコア ----
    metric_attrs = {
        "sales": ("previous_sales", "revised_sales", "change_sales_pct"),
        "op": ("previous_op", "revised_op", "change_op_pct"),
        "ordinary": ("previous_ordinary", "revised_ordinary", "change_ordinary_pct"),
        "net_income": ("previous_net_income", "revised_net_income", "change_net_income_pct"),
    }

    directions = {}
    for metric, (prev_attr, rev_attr, pct_attr) in metric_attrs.items():
        prev = getattr(event, prev_attr, None)
        rev = getattr(event, rev_attr, None)
        pct = getattr(event, pct_attr, None)

        direction = _metric_direction(prev, rev, pct)
        directions[metric] = direction
        weight = _METRIC_WEIGHTS[metric]

        if direction == "upward":
            bonus = _TURNAROUND_BONUS if _is_turnaround(prev, rev) else 0
            up_score += weight + bonus
            known_count += 1
        elif direction == "downward":
            bonus = _TURNAROUND_BONUS if _is_turnaround(prev, rev) else 0
            down_score += weight + bonus
            known_count += 1
        elif direction == "flat":
            known_count += 1
        # unknown は集計しない

    # ---- タイトル prior ----
    title_direction = _subtype_from_title(title)
    if title_direction == "upward":
        up_score += _TITLE_PRIOR_WEIGHT
    elif title_direction == "downward":
        down_score += _TITLE_PRIOR_WEIGHT

    logger.debug(
        f"[SUBTYPE-P1.6] directions={directions} "
        f"up={up_score} down={down_score} known={known_count} "
        f"title_prior={title_direction}"
    )

    # ---- 判定不能: 指標がゼロ ----
    if known_count == 0:
        # タイトルのみの場合
        if title_direction in ("upward", "downward", "difference"):
            return title_direction
        return "undecided"

    # ---- Final decision ----
    total_score = up_score + down_score
    if total_score == 0:
        # 全指標flat
        return "neutral"

    # スコア差で判定
    if up_score > 0 and down_score == 0:
        return "upward"
    if down_score > 0 and up_score == 0:
        return "downward"

    # 両方にスコアがある場合: 2倍以上の差があれば優勢側
    if up_score >= down_score * 2:
        return "upward"
    if down_score >= up_score * 2:
        return "downward"

    # 拮抗 → neutral
    return "neutral"


def _subtype_from_title(title: str) -> str:
    """タイトルキーワードから subtype を推定する（数値未取得時のフォールバック）。

    Returns:
        推定された subtype。判定不能時は "undecided"。
    """
    t = _normalize_text(title).lower() if title else ""
    if not t:
        logger.info("[FORECAST] subtype=undecided: no numeric data and no title")
        return "undecided"

    if "上方修正" in t:
        logger.debug(
            "[FORECAST] subtype fallback: title keyword '上方修正' -> upward "
            f"title='{title[:60]}'"
        )
        return "upward"
    if "下方修正" in t:
        logger.debug(
            "[FORECAST] subtype fallback: title keyword '下方修正' -> downward "
            f"title='{title[:60]}'"
        )
        return "downward"
    if "差異" in t:
        logger.debug(
            "[FORECAST] subtype fallback: title keyword '差異' -> difference "
            f"title='{title[:60]}'"
        )
        return "difference"
    if "増配" in t or "増額" in t:
        logger.debug(
            "[FORECAST] subtype fallback: title keyword '増配/増額' -> upward "
            f"title='{title[:60]}'"
        )
        return "upward"
    if "減配" in t or "減額" in t:
        logger.debug(
            "[FORECAST] subtype fallback: title keyword '減配/減額' -> downward "
            f"title='{title[:60]}'"
        )
        return "downward"

    # タイトルに方向キーワードなし → undecided
    logger.info(
        "[FORECAST] subtype=undecided: no numeric data, no directional keyword "
        f"title='{title[:80]}'"
    )
    return "undecided"


# ============================================================
# importance 算出
# ============================================================
def _calc_importance(event: ForecastRevisionEvent) -> int:
    """重要度スコア算出"""
    score = 50  # ベース

    # 黒字転換/赤字転落
    for prev_attr, rev_attr in [
        ("previous_net_income", "revised_net_income"),
        ("previous_op", "revised_op"),
    ]:
        prev = getattr(event, prev_attr, None)
        rev = getattr(event, rev_attr, None)
        if prev is not None and rev is not None:
            if prev < 0 and rev > 0:
                return 95  # 黒字転換
            if prev > 0 and rev < 0:
                return 85  # 赤字転落

    # 変化率ベース
    max_pct = 0
    for attr in ["change_op_pct", "change_net_income_pct"]:
        val = getattr(event, attr, None)
        if val is not None:
            max_pct = max(max_pct, abs(val))

    if max_pct >= 50:
        score = 90
    elif max_pct >= 30:
        score = 80
    elif max_pct >= 10:
        score = 70
    elif event.change_sales_pct is not None and abs(event.change_sales_pct) >= 10:
        score = 70

    # difference は上限60
    if event.subtype == "difference":
        score = min(score, 70)

    # neutral はやや下げ
    if event.subtype == "neutral":
        score = min(score, 55)

    return score


# ============================================================
# メイン抽出関数
# ============================================================
def extract_forecast_revision(
    text: str,
    title: str = "",
    is_difference: bool = False,
    tables: list[list[list]] | None = None,
) -> ForecastRevisionEvent:
    """テキスト（およびPDFテーブル構造）から業績予想修正イベントを抽出する。

    Phase 0: pdfplumber extract_tables() の構造化データから抽出（最優先）
    Phase 1: テキスト行ベースのテーブル解析
    Phase 2: 数値行フォールバック（Phase 1 内部）

    previous+revised ペア≥1 で Phase 0 を採用。
    欠損があれば Phase 1 で補完を試みる。
    """
    event = ForecastRevisionEvent()
    event.is_difference_disclosure = is_difference

    full_text = _normalize_text(text)
    full_title = _normalize_text(title)

    # 期間・基準
    event.period_label = _detect_period(full_text) or _detect_period(full_title)
    event.basis = _detect_basis(full_text) or _detect_basis(full_title)

    lines = full_text.split("\n")
    confidence = 0.0

    # 単位検出（テキスト全体から）
    unit = _detect_unit(full_text) or "百万円"

    # ---- Phase 1.5: テキストセクション分割 ----
    text_sections = _find_text_sections(lines)
    text_section_types = [s["type"] for s in text_sections]

    # target type 推定: Phase 1 の _infer_target_type を再利用
    # テーブル経由の table_types は空 — テキスト経路のみの推定
    text_target_type = _infer_target_type(full_title, full_text, text_section_types)

    # target type に一致するセクションを選択
    target_sections = _select_target_sections(text_sections, text_target_type)
    target_lines = []
    for sec in target_sections:
        target_lines.extend(lines[sec["start"]:sec["end"]])

    # フォールバック: target_lines が空なら全行を使用
    if not target_lines:
        target_lines = lines

    logger.debug(
        f"[TEXT-P1.5] text_target_type={text_target_type} "
        f"sections={text_section_types} "
        f"using_sections={[s['type'] for s in target_sections]} "
        f"target_lines_count={len(target_lines)} "
        f"title={full_title[:40]!r}"
    )

    # ---- Phase 0: pdfplumber テーブル構造から抽出 ----
    phase0_result = _extract_from_pdf_tables(tables, unit, is_difference, title=full_title, full_text=full_text)
    phase0_has_pair = phase0_result.get("has_pair", False)
    phase0_metrics = phase0_result.get("metrics_count", 0)

    # ---- Phase 1: テキスト行ベースのテーブル解析 ----
    phase1_result = _extract_from_horizontal_table(target_lines, is_difference, unit)
    phase1_metrics = phase1_result.get("metrics_count", 0)

    # ---- 結果選択 & 補完 ----
    if phase0_has_pair and phase0_metrics > 0:
        # Phase 0 採用（ペアあり）
        table_result = phase0_result
        event.extraction_source = "pdf_table"
        confidence += 0.50

        # Phase 2: Phase 1 で欠損補完（unit guard 付き）
        if phase1_metrics > 0:
            phase0_unit = table_result.get('_detected_unit', '') or unit
            phase1_unit = unit  # text fallback は doc-level unit を使用
            units_ok = _units_compatible(phase0_unit, phase1_unit)
            if units_ok:
                for metric in ["sales", "op", "ordinary", "net_income", "eps"]:
                    for prefix in ["previous_", "revised_"]:
                        key = f"{prefix}{metric}"
                        if table_result.get(key) is None and phase1_result.get(key) is not None:
                            table_result[key] = phase1_result[key]
                            logger.debug(f"[TABLE-P0] supplemented {key} from Phase 1")
            else:
                logger.info(
                    f"[TABLE-P2] skipped Phase 1 supplement: "
                    f"unit mismatch phase0={phase0_unit} phase1={phase1_unit}"
                )

    elif phase1_metrics > 0:
        # Phase 1 を採用
        table_result = phase1_result
        event.extraction_source = "pdf_text"
        confidence += 0.40
    elif phase0_metrics > 0:
        # Phase 0 に revised のみある場合も採用
        table_result = phase0_result
        event.extraction_source = "pdf_table"
        confidence += 0.30
    else:
        table_result = None

    if table_result is not None:
        metrics_count = table_result.get("metrics_count", 0)

        # 異常値検出 → 指標単位で無効化
        table_result = _sanitize_metrics(table_result)
        metrics_count = table_result.get("metrics_count", 0)

        # フィールド設定
        for metric in ["sales", "op", "ordinary", "net_income", "eps"]:
            prev = table_result.get(f"previous_{metric}")
            rev = table_result.get(f"revised_{metric}")
            delta = table_result.get(f"delta_{metric}")
            pct = table_result.get(f"change_{metric}_pct")

            if metric == "sales":
                event.previous_sales = prev
                event.revised_sales = rev
                event.delta_sales = delta
                event.change_sales_pct = pct
                if rev is not None:
                    confidence += 0.05
            elif metric == "op":
                event.previous_op = prev
                event.revised_op = rev
                event.delta_op = delta
                event.change_op_pct = pct
                if rev is not None:
                    confidence += 0.10
            elif metric == "ordinary":
                event.previous_ordinary = prev
                event.revised_ordinary = rev
                event.delta_ordinary = delta
                event.change_ordinary_pct = pct
                if rev is not None:
                    confidence += 0.05
            elif metric == "net_income":
                event.previous_net_income = prev
                event.revised_net_income = rev
                event.delta_net_income = delta
                event.change_net_income_pct = pct
                if rev is not None:
                    confidence += 0.10
            elif metric == "eps":
                event.previous_eps = prev
                event.revised_eps = rev
                event.delta_eps = delta
                event.change_eps_pct = pct

        event.extracted_metrics_count = metrics_count
        event.raw_table_text = table_result.get("raw_table_text", "")
    else:
        event.extraction_source = "fallback"

    event.confidence = min(round(confidence, 2), 1.0)
    event.subtype = _determine_subtype(event, is_difference, title=title)
    event.importance = _calc_importance(event)

    return event

