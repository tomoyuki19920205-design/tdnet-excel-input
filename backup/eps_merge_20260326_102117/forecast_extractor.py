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
        "一株当たり当期純利益",
        "1株当たり四半期純利益",
        "１株当たり四半期純利益",
        "一株当たり四半期純利益",
        "1株当たり純利益",
        "１株当たり純利益",
        "一株当たり純利益",
        "1株当たり当期純損益",
        "１株当たり当期純損益",
        "EPS",
        "eps",
    ],
}

# ============================================================
# ラベル辞書（Phase 2.5: 文字化け対策）
# 正規化後のラベルで照合する。_ITEM_PATTERNS と内容は同一だが
# 正規化後の値で事前構築しておくことで高速照合を実現。
# ============================================================
_METRIC_LABEL_ALIASES: dict[str, list[str]] = {}


def _build_metric_label_aliases() -> dict[str, list[str]]:
    """_ITEM_PATTERNS から NFKC正規化済み辞書を構築"""
    aliases: dict[str, list[str]] = {}
    for key, patterns in _ITEM_PATTERNS.items():
        normalized = []
        for pat in patterns:
            n = unicodedata.normalize("NFKC", pat)
            if n not in normalized:
                normalized.append(n)
        aliases[key] = normalized
    return aliases


_METRIC_LABEL_ALIASES = _build_metric_label_aliases()


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
# OCR テキスト専用正規化
# ============================================================
# OCR で頻出する1文字ずつ分断されたラベルの結合パターン
_OCR_LABEL_JOINS = [
    # 利益系ラベル
    ("売 上 高", "売上高"),
    ("売上 高", "売上高"),
    ("営 業 利 益", "営業利益"),
    ("営業 利益", "営業利益"),
    ("営業 利 益", "営業利益"),
    ("経 常 利 益", "経常利益"),
    ("経常 利益", "経常利益"),
    ("経常 利 益", "経常利益"),
    ("当 期 純 利 益", "当期純利益"),
    ("当期 純利益", "当期純利益"),
    ("当期 純 利益", "当期純利益"),
    ("当期純 利益", "当期純利益"),
    ("四 半 期 純 利 益", "四半期純利益"),
    ("四半期 純利益", "四半期純利益"),
    ("四半期 純 利益", "四半期純利益"),
    ("営 業 損 益", "営業損益"),
    ("経 常 損 益", "経常損益"),
    ("当 期 純 損 益", "当期純損益"),
    # 親会社株主系
    ("親 会 社 株 主", "親会社株主"),
    ("親会社 株主", "親会社株主"),
    ("帰 属 す る", "帰属する"),
    ("帰属 する", "帰属する"),
    # EPS
    ("1 株 当 た り", "1株当たり"),
    ("1 株 当たり", "1株当たり"),
    ("1株 当たり", "1株当たり"),
    ("1 株当たり", "1株当たり"),
    # 単位
    ("百 万 円", "百万円"),
    ("百万 円", "百万円"),
    ("千 円", "千円"),
    ("億 円", "億円"),
    ("円 銭", "円銭"),
    # 行ラベル
    ("前 回 発 表 予 想", "前回発表予想"),
    ("前回 発表 予想", "前回発表予想"),
    ("前回 発表予想", "前回発表予想"),
    ("前回発表 予想", "前回発表予想"),
    ("今 回 修 正 予 想", "今回修正予想"),
    ("今回 修正 予想", "今回修正予想"),
    ("今回 修正予想", "今回修正予想"),
    ("今回修正 予想", "今回修正予想"),
    ("増 減 額", "増減額"),
    ("増 減 率", "増減率"),
    ("増減 額", "増減額"),
    ("増減 率", "増減率"),
    # 期間
    ("通 期", "通期"),
    ("連 結", "連結"),
    ("個 別", "個別"),
    ("業 績 予 想", "業績予想"),
    ("業績 予想", "業績予想"),
]


def _normalize_ocr_text(text: str) -> str:
    """OCR テキスト専用の軽い正規化。

    source="ocr_text" の時だけ適用する。native PDF text には絶対にかけない。
    方針: 強すぎない正規化 → 既存パーサ再利用。
    """
    # Step 1: NFKC 正規化 + 全角スペース統一
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("\u3000", " ")

    # Step 2: 制御文字・ゼロ幅文字の除去
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200c\u200d\ufeff]", "", s)

    # Step 3: 行単位で処理
    lines = s.split("\n")
    normalized_lines = []
    for line in lines:
        # 行頭/行末空白除去
        line = line.strip()
        # 列区切り（2+スペース）をタブに変換して保持
        line = re.sub(r"[ ]{2,}", "\t", line)
        # タブ以外の連続空白を1個に圧縮
        line = re.sub(r" +", " ", line)
        if line:
            normalized_lines.append(line)

    # Step 4: 異常に細かい改行の抑制
    # 5文字以下の行で、前後が数値でもラベルでもない場合は前行に結合
    merged_lines: list[str] = []
    for line in normalized_lines:
        if (merged_lines
            and len(line) <= 5
            and not re.match(r"^[\d△▲,.\.\-−]+$", line)
            and not any(kw in line for kw in ["円", "期", "予想", "実績", "増減"])):
            merged_lines[-1] = merged_lines[-1] + " " + line
        else:
            merged_lines.append(line)

    s = "\n".join(merged_lines)

    # Step 5: OCR 分断ラベルの結合
    for broken, fixed in _OCR_LABEL_JOINS:
        s = s.replace(broken, fixed)

    # Step 6: 数字列途中の空白を縮約（タブ区切り = 列境界は超えない）
    # "1 0 , 0 0 0" → "10,000" / "1 0 0 . 5 0" → "100.50"
    # △/▲ + 空白 + 数字もつなぐ
    s = re.sub(r"([△▲])\s+(\d)", r"\1\2", s)
    # 数字・カンマ・ピリオドが空白で分断されているもの（タブは含まない）
    def _collapse_numeric_spaces(m: re.Match) -> str:
        return m.group(0).replace(" ", "")
    s = re.sub(
        r"[△▲]?\d(?: [\d,.]){2,}",
        _collapse_numeric_spaces,
        s,
    )
    # 残りのカンマ前後空白: "1 ,234" → "1,234"
    s = re.sub(r"(\d) ?, ?(\d)", r"\1,\2", s)
    # 残りのピリオド前後空白: "12 .34" → "12.34"
    s = re.sub(r"(\d) ?\. ?(\d)", r"\1.\2", s)

    # Step 7: タブをスペースに戻す（既存パーサとの互換）
    s = s.replace("\t", " ")

    return s


def _normalize_label(raw: str) -> str:
    """PDFラベルの正規化。文字化け耐性を高める。

    - NFKC正規化（全角英数→半角、㈱→(株) 等）
    - 改行・制御文字・ゼロ幅文字の除去
    - 連続空白の圧縮
    """
    s = unicodedata.normalize("NFKC", raw)
    s = s.replace("\u3000", " ")
    s = re.sub(r"[\r\n]+", "", s)
    # 制御文字・ゼロ幅文字の除去
    s = re.sub(r"[\x00-\x1f\x7f\u200b\u200c\u200d\ufeff]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _match_label_in_line(
    line: str,
) -> list[tuple[str, int]]:
    """行からメトリクスラベルを検出し、(field_key, position) のリストを返す。

    3段階マッチ（厳格）:
    1. 完全一致: 正規化前の _ITEM_PATTERNS
    2. 正規化後一致: _normalize_label 適用後の _ITEM_PATTERNS
    3. 辞書一致: _METRIC_LABEL_ALIASES で完全一致/前方一致/後方一致
       （中間部分一致は誤抽出リスクがあるため禁止）

    Returns: [(field_key, char_position), ...] 位置順ソート済み
    """
    matches: list[tuple[str, int]] = []
    matched_keys: set[str] = set()

    normalized_line = _normalize_label(line)

    # --- Phase 1: 正規化前の完全一致 ---
    for field_key, patterns in _ITEM_PATTERNS.items():
        if field_key in matched_keys:
            continue
        for pat in patterns:
            escaped = re.escape(pat)
            for m in re.finditer(escaped, line):
                matches.append((field_key, m.start()))
                matched_keys.add(field_key)
                break  # このフィールドの最初のマッチのみ
            if field_key in matched_keys:
                break

    # --- Phase 2: 正規化後一致 ---
    for field_key, patterns in _ITEM_PATTERNS.items():
        if field_key in matched_keys:
            continue
        for pat in patterns:
            n_pat = unicodedata.normalize("NFKC", pat)
            escaped = re.escape(n_pat)
            for m in re.finditer(escaped, normalized_line):
                matches.append((field_key, m.start()))
                matched_keys.add(field_key)
                break
            if field_key in matched_keys:
                break

    # --- Phase 3: 辞書一致 (完全/前方/後方一致のみ, 中間部分一致禁止) ---
    for field_key, aliases in _METRIC_LABEL_ALIASES.items():
        if field_key in matched_keys:
            continue
        for alias in aliases:
            escaped = re.escape(alias)
            # 完全一致 or 前方一致 or 後方一致
            # 前方一致: 行頭 or 空白の直後にラベルが出現
            # 後方一致: ラベルの直後が行末 or 空白
            boundary_pat = rf"(?:^|\s){escaped}(?:\s|$)"
            for m in re.finditer(boundary_pat, normalized_line):
                # m.start() は先行空白を含む場合があるので調整
                pos = m.start()
                if normalized_line[pos:pos+1] in (" ", "\t"):
                    pos += 1
                matches.append((field_key, pos))
                matched_keys.add(field_key)
                break
            if field_key in matched_keys:
                break

    # 位置順にソート
    matches.sort(key=lambda x: x[1])
    return matches


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

    ref_idx の前20行を走査してヘッダー候補を探す。
    3段階マッチ（完全一致→正規化後→辞書）で検出し、
    re.finditer() による出現位置でソートして列インデックスを割り当てる。
    見つからなければデフォルト順序（売上,営利,経常,純利,EPS）。
    """
    default_map = {"sales": 0, "op": 1, "ordinary": 2, "net_income": 3, "eps": 4}

    search_start = max(0, ref_idx - 20)
    search_end = ref_idx
    if search_start >= search_end:
        return default_map

    for i in range(search_start, search_end):
        line = lines[i] if i < len(lines) else ""
        label_matches = _match_label_in_line(line)

        if len(label_matches) >= 3:
            # 位置順のインデックスを割り当て
            col_map = {}
            for col_idx, (field_key, _pos) in enumerate(label_matches):
                col_map[field_key] = col_idx
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
# コアテキスト抽出（共通化）
# ============================================================
def _extract_from_text(
    text: str,
    title: str = "",
    is_difference: bool = False,
    source: str = "pdf_text",
) -> ForecastRevisionEvent:
    """テキストからForecastRevisionEventを抽出するコアパーサ。

    pdf_text / ocr_text 問わず同一ロジックで抽出する。
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
        event.extraction_source = source

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


# ============================================================
# OCR 簡易行パーサ（フォールバック）
# ============================================================
def _extract_from_ocr_lines(
    text: str,
    title: str = "",
    is_difference: bool = False,
) -> ForecastRevisionEvent:
    """OCR テキスト専用の簡易行パーサ。

    既存 _extract_from_text() で 0件だった場合のみ呼ばれる。
    ラベルを含む行を走査し、同一行 or 次行から数値を拾う。
    """
    event = ForecastRevisionEvent()
    event.is_difference_disclosure = is_difference
    event.extraction_source = "ocr_line_parser"

    full_text = _normalize_text(text)
    full_title = _normalize_text(title)

    event.period_label = _detect_period(full_text) or _detect_period(full_title)
    event.basis = _detect_basis(full_text) or _detect_basis(full_title)

    unit = _detect_unit(full_text) or "百万円"
    lines = full_text.split("\n")

    # 前回/修正行のインデックスを探す
    prev_line_idx = None
    rev_line_idx = None
    for i, line in enumerate(lines):
        clean = _normalize_text(line).lower().strip()
        if not clean:
            continue
        for lbl in _PREVIOUS_LABELS:
            if lbl.lower() in clean:
                prev_line_idx = i
                break
        for lbl in _REVISED_LABELS + _ACTUAL_LABELS:
            if lbl.lower() in clean:
                rev_line_idx = i
                break

    # 前回/修正行から数値を抽出
    prev_nums: list[float | None] = []
    rev_nums: list[float | None] = []

    if prev_line_idx is not None:
        prev_nums = _extract_numbers_from_line(lines[prev_line_idx])
        # 同一行で数値が足りなければ次行も合体
        if len(prev_nums) < 3 and prev_line_idx + 1 < len(lines):
            prev_nums.extend(_extract_numbers_from_line(lines[prev_line_idx + 1]))

    if rev_line_idx is not None:
        rev_nums = _extract_numbers_from_line(lines[rev_line_idx])
        if len(rev_nums) < 3 and rev_line_idx + 1 < len(lines):
            rev_nums.extend(_extract_numbers_from_line(lines[rev_line_idx + 1]))

    # ラベル行でヘッダー列順を検出
    col_map = {"sales": 0, "op": 1, "ordinary": 2, "net_income": 3, "eps": 4}
    # ヘッダー行を前回行より前で探す
    ref_idx = prev_line_idx or rev_line_idx
    if ref_idx is not None:
        detected_map = _detect_column_order(lines, ref_idx)
        if detected_map:
            col_map = detected_map

    metrics_count = 0
    confidence = 0.0

    for field_key, col_idx in col_map.items():
        is_eps = (field_key == "eps")
        prev_val = prev_nums[col_idx] if col_idx < len(prev_nums) else None
        rev_val = rev_nums[col_idx] if col_idx < len(rev_nums) else None

        prev_val = _apply_unit(prev_val, unit, is_eps)
        rev_val = _apply_unit(rev_val, unit, is_eps)

        pct_val = None
        delta_val = None
        if prev_val is not None and rev_val is not None:
            pct_val = _calc_pct(prev_val, rev_val)
            delta_val = _calc_delta(prev_val, rev_val)

        if rev_val is not None:
            metrics_count += 1
            confidence += 0.05

        if field_key == "sales":
            event.previous_sales = prev_val
            event.revised_sales = rev_val
            event.delta_sales = delta_val
            event.change_sales_pct = pct_val
        elif field_key == "op":
            event.previous_op = prev_val
            event.revised_op = rev_val
            event.delta_op = delta_val
            event.change_op_pct = pct_val
        elif field_key == "ordinary":
            event.previous_ordinary = prev_val
            event.revised_ordinary = rev_val
            event.delta_ordinary = delta_val
            event.change_ordinary_pct = pct_val
        elif field_key == "net_income":
            event.previous_net_income = prev_val
            event.revised_net_income = rev_val
            event.delta_net_income = delta_val
            event.change_net_income_pct = pct_val
        elif field_key == "eps":
            event.previous_eps = prev_val
            event.revised_eps = rev_val
            event.delta_eps = delta_val
            event.change_eps_pct = pct_val

    if metrics_count > 0:
        # 異常値検出
        table_result = {}
        for m in ["sales", "op", "ordinary", "net_income", "eps"]:
            table_result[f"previous_{m}"] = getattr(event, f"previous_{m}", None)
            table_result[f"revised_{m}"] = getattr(event, f"revised_{m}", None)
            table_result[f"delta_{m}"] = getattr(event, f"delta_{m}", None)
            table_result[f"change_{m}_pct"] = getattr(event, f"change_{m}_pct", None)
        table_result["metrics_count"] = metrics_count
        table_result = _sanitize_metrics(table_result)
        metrics_count = table_result.get("metrics_count", 0)

        # sanitize後の値を反映
        for m in ["sales", "op", "ordinary", "net_income", "eps"]:
            setattr(event, f"previous_{m}", table_result.get(f"previous_{m}"))
            setattr(event, f"revised_{m}", table_result.get(f"revised_{m}"))
            setattr(event, f"delta_{m}", table_result.get(f"delta_{m}"))
            setattr(event, f"change_{m}_pct", table_result.get(f"change_{m}_pct"))

    event.extracted_metrics_count = metrics_count
    event.confidence = min(round(confidence, 2), 1.0)
    event.subtype = _determine_subtype(event, is_difference)
    event.importance = _calc_importance(event)

    return event


# ============================================================
# メイン抽出関数（OCRフォールバック統合）
# ============================================================
# OCR モジュールの事前 import（なければフラグで管理）
try:
    from .pdf_ocr import (
        _is_ocr_enabled as _ocr_enabled_check,
        should_run_ocr_fallback as _should_run_ocr,
        rasterize_pdf_with_ghostscript as _rasterize,
        extract_text_via_google_ocr as _google_ocr,
        score_forecast_result as _score_result,
        cleanup_temp_images as _cleanup_images,
    )
    _HAS_OCR_MODULE = True
except ImportError:
    _HAS_OCR_MODULE = False

    # ダミー定義（テスト環境対応）
    def _score_result(ev) -> int:  # noqa: E302
        return 0


def extract_forecast_revision(
    text: str,
    title: str = "",
    is_difference: bool = False,
    pdf_path: str = "",
    doc_url: str = "",
    doc_id: str = "",
) -> ForecastRevisionEvent:
    """テキストから業績予想修正イベントを抽出する。

    フロー:
    1. base = _extract_from_text(raw_text)
    2. should_run = should_run_ocr_fallback(raw_text, base)
    3. should_run=True → OCR実行 → normalize → 既存パーサ → フォールバック
    4. base vs OCR 比較 → 良い方を返す
    """
    _doc_label = doc_id[:16] if doc_id else "?"

    # ---- Phase 1: 既存テキストから抽出（これは常に実行）----
    base = _extract_from_text(text, title, is_difference, source="pdf_text")

    # ---- Phase 2: OCR フォールバック判定 ----
    if not _HAS_OCR_MODULE:
        return base

    try:
        diagnostics: dict = {}
        should_run = _should_run_ocr(text, base, diagnostics)

        if not should_run:
            return base

        # ==== should_run=True → 必ず start ログ ====
        logger.info(f"[forecast_ocr] start doc_id={_doc_label}")

        # OCR 環境チェック
        if not _ocr_enabled_check():
            logger.info(f"[forecast_ocr] disabled (ENABLE_GOOGLE_OCR not set)")
            return base

        # ---- PDF 取得 ----
        ocr_pdf_path = pdf_path
        if not ocr_pdf_path and doc_url:
            ocr_pdf_path = _download_pdf_for_ocr(doc_url)

        if not ocr_pdf_path:
            logger.info(
                f"[forecast_ocr] skipped reason=no_pdf_source "
                f"doc_id={_doc_label}"
            )
            return base

        # ---- Ghostscript ラスタライズ ----
        images = _rasterize(ocr_pdf_path)
        if not images:
            logger.info(f"[forecast_ocr] skipped reason=rasterize_failed")
            return base

        try:
            # ---- Google OCR テキスト抽出 ----
            ocr_text = _google_ocr(images)
            if not ocr_text.strip():
                logger.info(f"[forecast_ocr] skipped reason=ocr_text_empty")
                return base

            # ---- normalize（OCR 専用、native には絶対かけない）----
            normalized_ocr = _normalize_ocr_text(ocr_text)
            logger.info(
                f"[forecast_ocr] normalized_text_len={len(normalized_ocr)}"
            )

            # ---- 既存パーサに通す ----
            ocr_event = _extract_from_text(
                normalized_ocr, title, is_difference, source="ocr_text"
            )

            # ---- score==0 の時だけ簡易行パーサ ----
            ocr_score_val = _score_result(ocr_event)
            if ocr_score_val == 0:
                ocr_event = _extract_from_ocr_lines(
                    normalized_ocr, title, is_difference
                )
                logger.info(
                    f"[forecast_ocr] parsed_metrics_from_ocr="
                    f"{ocr_event.extracted_metrics_count}"
                )

            # ---- base vs OCR 比較 ----
            base_score = _score_result(base)
            final_ocr_score = _score_result(ocr_event)

            logger.info(
                f"[forecast_ocr] base_score={base_score} "
                f"ocr_score={final_ocr_score}"
            )

            if final_ocr_score > base_score:
                if base_score >= 10:
                    logger.info(
                        f"[forecast_ocr] selected=base "
                        f"reason=ocr_regression_blocked"
                    )
                    return base
                ocr_event.extraction_source = "ocr_fallback"
                logger.info(
                    f"[forecast_ocr] selected=ocr "
                    f"reason=ocr_fallback_used"
                )
                return ocr_event
            elif final_ocr_score == 0:
                logger.info(
                    f"[forecast_ocr] selected=base "
                    f"reason=ocr_no_signal"
                )
                return base
            else:
                logger.info(
                    f"[forecast_ocr] selected=base "
                    f"reason=native_keep"
                )
                return base

        finally:
            _cleanup_images(images)

    except Exception as e:
        logger.warning(
            f"[forecast_ocr] OCR fallback failed (non-fatal): {e}"
        )
        return base


def _download_pdf_for_ocr(url: str) -> str:
    """URL から PDF をダウンロードして一時ファイルに保存する。"""
    if not url:
        return ""
    try:
        import tempfile
        import requests

        if not any(h in url for h in ["tdnet.info", "disclosure.edinet"]):
            return ""

        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TDNETEventBot/1.0)"
        })
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not resp.content[:5] == b"%PDF-":
            return ""

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(resp.content)
            return f.name

    except Exception as e:
        logger.debug(f"[forecast_ocr] PDF download for OCR failed: {e}")
        return ""

