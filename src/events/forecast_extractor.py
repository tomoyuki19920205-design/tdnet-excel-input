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
# 単位正規化
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
# 連結/個別テーブル分離
# ============================================================
def _find_consolidated_section(lines: list[str]) -> tuple[int, int]:
    """連結セクションの開始/終了行を推定。個別もあれば個別セクションは除外。"""
    consolidated_start = None
    standalone_start = None

    for i, line in enumerate(lines):
        clean = _normalize_text(line).strip()
        if "連結" in clean and ("業績予想" in clean or "経営成績" in clean):
            consolidated_start = i
        if "個別" in clean and ("業績予想" in clean or "経営成績" in clean):
            standalone_start = i

    if consolidated_start is not None:
        end = standalone_start if standalone_start and standalone_start > consolidated_start else len(lines)
        return consolidated_start, end

    return 0, len(lines)


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

        # チェック3: 変化率が500%超（利益は変動大きいが売上で500%超は単位ミス）
        if metric == "sales":
            pct = table_result.get(pct_key)
            if pct is not None and abs(pct) > 500:
                anomaly = True
                logger.debug(f"[SANITIZE] sales pct={pct}% too high, likely unit error")

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
# subtype 判定
# ============================================================
def _determine_subtype(event: ForecastRevisionEvent, is_difference: bool = False) -> str:
    """主要利益項目の変化率から subtype を判定"""
    if is_difference or event.is_difference_disclosure:
        return "difference"

    changes = []
    for attr in ["change_op_pct", "change_ordinary_pct", "change_net_income_pct"]:
        val = getattr(event, attr, None)
        if val is not None:
            changes.append(val)

    if not changes:
        # 売上のみの場合
        if event.change_sales_pct is not None:
            if event.change_sales_pct > 5:
                return "upward"
            elif event.change_sales_pct < -5:
                return "downward"
        return "undecided"

    # 黒字転換/赤字転落チェック
    for prev_attr, rev_attr in [
        ("previous_net_income", "revised_net_income"),
        ("previous_op", "revised_op"),
    ]:
        prev = getattr(event, prev_attr, None)
        rev = getattr(event, rev_attr, None)
        if prev is not None and rev is not None:
            if prev < 0 and rev > 0:
                return "upward"
            if prev > 0 and rev < 0:
                return "downward"

    positive = sum(1 for c in changes if c > 5)
    negative = sum(1 for c in changes if c < -5)

    if positive > 0 and negative == 0:
        return "upward"
    if negative > 0 and positive == 0:
        return "downward"
    if positive > 0 and negative > 0:
        return "neutral"

    # 小さな変化
    if all(abs(c) <= 5 for c in changes):
        avg = sum(changes) / len(changes)
        if avg > 0:
            return "upward"
        elif avg < 0:
            return "downward"
        return "neutral"

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
) -> ForecastRevisionEvent:
    """テキストから業績予想修正イベントを抽出する。

    テーブル形式のテキストから前回予想と今回修正予想の行を探し、
    売上高/営業利益/経常利益/純利益の数値を抽出する。
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

    # 連結優先: 連結/個別セクションがあれば連結を使う
    c_start, c_end = _find_consolidated_section(lines)
    target_lines = lines[c_start:c_end] if c_start > 0 else lines

    # テーブル抽出を試行
    table_result = _extract_from_horizontal_table(target_lines, is_difference, unit)

    metrics_count = table_result.get("metrics_count", 0)

    if metrics_count > 0:
        confidence += 0.40
        event.extraction_source = "pdf_text"

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
    event.subtype = _determine_subtype(event, is_difference)
    event.importance = _calc_importance(event)

    return event
