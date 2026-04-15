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
import os
import re
import unicodedata
from typing import Optional

from .forecast_models import ForecastRevisionEvent
from .common_normalizers import normalize_jp_number, normalize_amount_to_million_yen, parse_number

_parse_number = parse_number # Alias for internal use
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
    "修正前",
    "訂正前",
    "訂正前(a)",
    "修正前(a)",
    "修正前（a）",
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
    "訂正後",
    "訂正後(b)",
    "修正後(b)",
    "修正後（b）",
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
    """全角→半角、NFKC正規化、および数値の分断修復"""
    if not text: return ""
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("\u3000", " ")  # 全角スペース
    
    # 1. 数字間の「単一」スペースのみ結合。2つ以上のスペース(タブ等)は区切りとして維持。
    for _ in range(5):
        s = re.sub(r"(\d) (\d)", r"\1\2", s)
    
    # 2. 小数点・カンマ前後のスペースを結合 (分断対応)
    # 複数スペース(2つ以上)は数値の区切りである可能性が高いため、単一スペースのみ結合
    s = re.sub(r"(\d+)\s?\.\s?(\d+)", r"\1.\2", s)
    s = re.sub(r"(\d+)\s?,\s?(\d+)", r"\1,\2", s)
    
    s = s.replace("\r", "")
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
    ("一 株 当 た り", "一株当たり"),
    ("一株 当たり", "一株当たり"),
    ("E P S", "EPS"),
    ("当 期 純 利 益", "当期純利益"),
    ("当期 純利益", "当期純利益"),
    ("当期 純 利益", "当期純利益"),
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
    ("今回 修正 予 想", "今回修正予想"),
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
    # △/▲ + 空白 + 数字もつなぐ (単一スペースのみ)
    s = re.sub(r"([△▲]) ?(\d)", r"\1\2", s)
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
    pattern = r'(?<![.\d])([△▲]?\s*[\d,]+\.?\d*%?|[―\-–—]{1,2})(?![年月日期四株])'
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
                    has_financial_number = bool(re.search(r"[\d,]{2,}", after_label)) # 4桁 -> 2桁に緩和
                    has_triangle_number = bool(re.search(r"[△▲]\s*[\d,]+", after_label))
                    num_tokens = len(re.findall(r"[\d,.]+", after_label))
                    if has_financial_number or has_triangle_number or num_tokens >= 1:
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

    # 数値抽出 (ラベル行に数値がなければ次行も確認)
    def _extract_with_fallback(idx: int | None) -> list[float | None]:
        if idx is None: return []
        nums = _extract_numbers_from_line(lines[idx])
        if not nums and idx + 1 < len(lines):
            # 次行が別のラベル行でなければ、その行から数値を取る
            next_line = _normalize_text(lines[idx + 1]).lower()
            all_labels = _PREVIOUS_LABELS + _REVISED_LABELS + _ACTUAL_LABELS + _DELTA_LABELS + _CHANGE_PCT_LABELS
            if not any(lbl.lower() in next_line for lbl in all_labels):
                nums = _extract_numbers_from_line(lines[idx + 1])
        return nums

    prev_nums = _extract_with_fallback(prev_idx)
    revised_nums = _extract_with_fallback(revised_idx)
    delta_nums = _extract_with_fallback(delta_idx)
    pct_nums = _extract_with_fallback(pct_idx)

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

    result["latest_full_year_eps"] = extract_latest_full_year_eps("\n".join(lines))

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

    # 変化判定ログ
    logger.info(
        f"[forecast] change_check "
        f"ni=({getattr(event, 'previous_net_income', None)},{getattr(event, 'revised_net_income', None)}) "
        f"eps=({event.previous_eps},{event.revised_eps}) "
        f"op_pct={event.change_op_pct} ni_pct={event.change_net_income_pct}"
    )

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
        # EPS フォールバック
        if event.previous_eps is not None and event.revised_eps is not None:
            if event.revised_eps > event.previous_eps:
                return "upward"
            elif event.revised_eps < event.previous_eps:
                return "downward"
            return "neutral"
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
    pdf_path: str = "",
) -> ForecastRevisionEvent:
    """テキストからForecastRevisionEventを抽出するコアパーサ。

    pdf_path が渡された場合、座標情報を用いた高度な EPS フィルタリングが有効になる。
    """
    event = ForecastRevisionEvent()
    event.is_difference_disclosure = is_difference

    full_text = _normalize_text(text)
    full_title = _normalize_text(title)

    event.period_label = _detect_period(full_text) or _detect_period(full_title)
    event.basis = _detect_basis(full_text) or _detect_basis(full_title)

    lines = full_text.split("\n")
    confidence = 0.0
    unit = _detect_unit(full_text) or "百万円"

    c_start, c_end = _find_consolidated_section(lines)
    target_lines = lines[c_start:c_end] if c_start > 0 else lines

    # ---- Phase 1: 旧来の横型テーブル抽出（安全性最優先） ----
    table_result = _extract_from_horizontal_table(target_lines, is_difference, unit)
    metrics_count = table_result.get("metrics_count", 0)

    # フィールド設定（EPS以外を先行設定）
    for metric in ["sales", "op", "ordinary", "net_income", "eps"]:
        prev = table_result.get(f"previous_{metric}")
        rev = table_result.get(f"revised_{metric}")
        delta = table_result.get(f"delta_{metric}")
        pct = table_result.get(f"change_{metric}_pct")

        if metric == "sales":
            event.previous_sales = prev; event.revised_sales = rev
            event.delta_sales = delta; event.change_sales_pct = pct
            if rev is not None: confidence += 0.05
        elif metric == "op":
            event.previous_op = prev; event.revised_op = rev
            event.delta_op = delta; event.change_op_pct = pct
            if rev is not None: confidence += 0.10
        elif metric == "ordinary":
            event.previous_ordinary = prev; event.revised_ordinary = rev
            event.delta_ordinary = delta; event.change_ordinary_pct = pct
            if rev is not None: confidence += 0.05
        elif metric == "net_income":
            event.previous_net_income = prev; event.revised_net_income = rev
            event.delta_net_income = delta; event.change_net_income_pct = pct
            if rev is not None: confidence += 0.10
        elif metric == "eps":
            # EPS は一旦保持し、後続の新ロジックと比較する
            event.previous_eps = prev
            event.revised_eps = rev
            event.delta_eps = delta
            event.change_eps_pct = pct

    if metrics_count > 0:
        confidence += 0.40
        event.extraction_source = source

    logger.debug(f"[FORECAST] Phase 1 metrics_count={metrics_count}, revised_eps={event.revised_eps}")
def _is_reliable_eps(prev: float | None, rev: float | None) -> bool:
    """EPS数値が信頼できるか判定する。
    - prev または rev のどちらか一つでも取得できていれば True とする。
    - ただし、明らかに配当として誤判定されやすい切りの良い数字は除外する。
    - 「弱い整数(<=4)」による制限は撤廃（早期 return 優先）。
    """
    if prev is None and rev is None:
        return False

    # 配当として誤認されやすい「切りの良い整数」をガード
    OBVIOUS_DIVIDENDS = {
        5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 
        60.0, 70.0, 75.0, 80.0, 100.0, 150.0, 200.0
    }
    
    def is_obvious_dividend(v: float | None) -> bool:
        if v is None: return False
        return v == float(int(v)) and v in OBVIOUS_DIVIDENDS

    vals = [v for v in [prev, rev] if v is not None]
    
    # 取得できた候補のいずれかが「配当らしくない」ものであれば、早期 return 用の信頼ありとみなす
    if any(not is_obvious_dividend(v) for v in vals):
        return True
        
    return False


def _extract_from_text(
    text: str,
    title: str = "",
    is_difference: bool = False,
    source: str = "pdf_text",
    pdf_path: str = "",
) -> ForecastRevisionEvent:
    """テキストからForecastRevisionEventを抽出するコアパーサ。

    pdf_path が渡された場合、座標情報を用いた高度な EPS フィルタリングが有効になる。
    """
    event = ForecastRevisionEvent()
    event.is_difference_disclosure = is_difference
    # 追加属性
    setattr(event, "extraction_stage", "none")
    setattr(event, "fallback_used", False)

    full_text = _normalize_text(text)
    full_title = _normalize_text(title)

    event.period_label = _detect_period(full_text) or _detect_period(full_title)
    event.basis = _detect_basis(full_text) or _detect_basis(full_title)

    lines = full_text.split("\n")
    confidence = 0.0
    unit = _detect_unit(full_text) or "百万円"

    c_start, c_end = _find_consolidated_section(lines)
    target_lines = lines[c_start:c_end] if c_start > 0 else lines

    # ---- Phase 1: Native/Regex (横型テーブル抽出) ----
    table_result = _extract_from_horizontal_table(target_lines, is_difference, unit)
    metrics_count = table_result.get("metrics_count", 0)

    # フィールド設定
    for metric in ["sales", "op", "ordinary", "net_income", "eps"]:
        prev = table_result.get(f"previous_{metric}")
        rev = table_result.get(f"revised_{metric}")
        delta = table_result.get(f"delta_{metric}")
        pct = table_result.get(f"change_{metric}_pct")
        if metric == "sales":
            event.previous_sales = prev; event.revised_sales = rev
            event.delta_sales = delta; event.change_sales_pct = pct
        elif metric == "op":
            event.previous_op = prev; event.revised_op = rev
            event.delta_op = delta; event.change_op_pct = pct
        elif metric == "ordinary":
            event.previous_ordinary = prev; event.revised_ordinary = rev
            event.delta_ordinary = delta; event.change_ordinary_pct = pct
        elif metric == "net_income":
            event.previous_net_income = prev; event.revised_net_income = rev
            event.delta_net_income = delta; event.change_net_income_pct = pct
        elif metric == "eps":
            event.previous_eps = prev; event.revised_eps = rev
            event.delta_eps = delta; event.change_eps_pct = pct

    if metrics_count > 0:
        confidence += 0.40
        event.extraction_source = source

    # ---- Phase 1.2: Native/Regex (EPS専有検索/縦ブロック) ※pdfplumber座標ガード抜き ----
    if not _is_reliable_eps(event.previous_eps, event.revised_eps):
        eps_p_prev, eps_p_rev = _find_eps_from_lines(lines)
        if (eps_p_prev is not None or eps_p_rev is not None):
            # 既存より完備度が高い、または信頼できる場合に採用
            curr_count = sum(1 for x in [event.previous_eps, event.revised_eps] if x is not None)
            new_count = sum(1 for x in [eps_p_prev, eps_p_rev] if x is not None)
            if new_count > curr_count or _is_reliable_eps(eps_p_prev, eps_p_rev):
                event.previous_eps = eps_p_prev
                event.revised_eps = eps_p_rev
                event.delta_eps = _calc_delta(eps_p_prev, eps_p_rev)
                event.change_eps_pct = _calc_pct(eps_p_prev, eps_p_rev)
    
    # 信頼できるEPSが取れたら Native 段階で即 return
    if _is_reliable_eps(event.previous_eps, event.revised_eps):
        event.extraction_stage = "native"
        event.confidence = min(round(confidence + 0.1, 2), 1.0)
        event.extracted_metrics_count = metrics_count
        event.subtype = _determine_subtype(event, is_difference)
        event.importance = _calc_importance(event)
        return event

    # ---- Phase 2: Prose (文章中) 抽出 ----
    # 欠落している項目を文章中から補完する
    eps_prose_prev, eps_prose_rev = _extract_eps_from_prose(full_text)
    if eps_prose_rev is not None:
        # 文章中の方が信頼できる（または補完できる）場合
        if event.revised_eps is None or _is_reliable_eps(eps_prose_prev, eps_prose_rev):
            event.previous_eps = eps_prose_prev
            event.revised_eps = eps_prose_rev
            event.delta_eps = _calc_delta(event.previous_eps, event.revised_eps)
            event.change_eps_pct = _calc_pct(event.previous_eps, event.revised_eps)
            event.extraction_source = "prose" if event.revised_eps == eps_prose_rev else event.extraction_source

    if _is_reliable_eps(event.previous_eps, event.revised_eps):
        event.extraction_stage = "prose"
        event.confidence = min(round(confidence + 0.05, 2), 1.0)
        event.extracted_metrics_count = metrics_count
        event.subtype = _determine_subtype(event, is_difference)
        event.importance = _calc_importance(event)
        return event

    # ---- Phase 3: Note (注記) 抽出 ----
    eps_note_prev, eps_note_rev = _extract_eps_from_notes(full_text)
    if eps_note_rev is not None:
        if event.revised_eps is None or _is_reliable_eps(eps_note_prev, eps_note_rev):
            event.previous_eps = eps_note_prev
            event.revised_eps = eps_note_rev
            event.delta_eps = _calc_delta(event.previous_eps, event.revised_eps)
            event.change_eps_pct = _calc_pct(event.previous_eps, event.revised_eps)
            event.extraction_source = "note" if event.revised_eps == eps_note_rev else event.extraction_source

    if _is_reliable_eps(event.previous_eps, event.revised_eps):
        event.extraction_stage = "note"
        event.confidence = min(round(confidence + 0.05, 2), 1.0)
        event.extracted_metrics_count = metrics_count
        event.subtype = _determine_subtype(event, is_difference)
        event.importance = _calc_importance(event)
        return event

    # ---- Phase 4: pdfplumber (最終 Fallback) ----
    if pdf_path and os.path.exists(pdf_path):
        event.fallback_used = True
        event.extraction_stage = "pdfplumber"
        logger.debug(f"[FORECAST] Final fallback to pdfplumber for {pdf_path}")
        
        # 1. 表抽出のレスキュー (Phase 4.1)
        pdf_table_result = _extract_via_pdfplumber_table(pdf_path)
        if pdf_table_result and pdf_table_result.get("metrics_count", 0) >= metrics_count:
            table_result = pdf_table_result
            metrics_count = table_result.get("metrics_count", 0)
            event.extraction_source = "pdfplumber_table"
            for metric in ["sales", "op", "ordinary", "net_income", "eps"]:
                setattr(event, f"previous_{metric}", table_result.get(f"previous_{metric}"))
                setattr(event, f"revised_{metric}", table_result.get(f"revised_{metric}"))
                setattr(event, f"delta_{metric}", table_result.get(f"delta_{metric}"))
                setattr(event, f"change_{metric}_pct", table_result.get(f"change_{metric}_pct"))
            confidence += 0.20

        # 2. 座標座標ガード付き縦ブロック (Phase 4.2)
        eps_guard_prev, eps_guard_rev = _find_eps_from_lines_with_guard(lines, pdf_path)
        p_count = sum(1 for x in [event.previous_eps, event.revised_eps] if x is not None)
        g_count = sum(1 for x in [eps_guard_prev, eps_guard_rev] if x is not None)
        if g_count > p_count or _is_reliable_eps(eps_guard_prev, eps_guard_rev):
            event.previous_eps = eps_guard_prev
            event.revised_eps = eps_guard_rev
            event.delta_eps = _calc_delta(eps_guard_prev, eps_guard_rev)
            event.change_eps_pct = _calc_pct(eps_guard_prev, eps_guard_rev)
            logger.info(f"[EPS_FINAL_ADOPTED] Adopted via pdfplumber guard: {eps_guard_rev}")

    event.extracted_metrics_count = metrics_count
    event.confidence = min(round(confidence, 2), 1.0)
    event.subtype = _determine_subtype(event, is_difference)
    event.importance = _calc_importance(event)

    return event



def _extract_via_pdfplumber_table(pdf_path: str) -> dict | None:
    """pdfplumber を用いて表を直接抽出する。
    
    テキスト抽出 (CID欠落など) が困難な場合の最終手段。
    """
    best_res = None
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[:3]:
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2: continue
                    clean_table = []
                    for row in table:
                        clean_table.append([_normalize_label(str(cell)) if cell else "" for cell in row])
                    
                    # ヘッダー行を特定 (キーワードが含まれる行)
                    header_row = -1
                    for i, row in enumerate(clean_table):
                        row_text = "".join([str(c) for c in row if c])
                        if any(lbl in row_text for lbl in ["EPS", "1株当たり", "純利益", "売上高"]):
                            header_row = i
                            break
                    
                    if header_row != -1:
                        # カラムマップの構築
                        col_map = {}
                        for j, cell in enumerate(clean_table[header_row]):
                            m_cells = _match_label_in_line(str(cell))
                            if m_cells:
                                col_map[m_cells[0][0]] = j
                        
                        # 前回/修正行を探す
                        prev_row = None; rev_row = None
                        for row in clean_table[header_row+1:]:
                            row_text = "".join([str(c) for c in row if c is not None])
                            if any(lbl in row_text for lbl in _REVISED_LABELS) and not rev_row:
                                rev_row = row
                            elif any(lbl in row_text for lbl in _PREVIOUS_LABELS) and not prev_row:
                                prev_row = row
                            if prev_row and rev_row: break
                        
                        if not rev_row: continue
                    
                    res = {"metrics_count": 0}
                    for field, c_idx in col_map.items():
                        prev_val = _parse_cell_value(prev_row[c_idx]) if prev_row and c_idx < len(prev_row) else None
                        rev_val = _parse_cell_value(rev_row[c_idx]) if rev_row and c_idx < len(rev_row) else None
                        
                        # ノイズ除去: 1桁の整数などは EPS としても他メトリクスとしても怪しい
                        if rev_val is not None and abs(rev_val) in [0.0, 1.0, 2.0, 3.0, 4.0]:
                            rev_val = None
                        if prev_val is not None and abs(prev_val) in [0.0, 1.0, 2.0, 3.0, 4.0]:
                            prev_val = None
                            
                        if rev_val is not None: res["metrics_count"] += 1
                        res[f"previous_{field}"] = prev_val
                        res[f"revised_{field}"] = rev_val
                    
                    # より多くのメトリクスを持つ結果を優先
                    if best_res is None or res["metrics_count"] > best_res["metrics_count"]:
                        best_res = res
    except Exception as e:
        logger.debug(f"[forecast_ocr] pdfplumber extract failed: {e}")
    
    if best_res:
        # 計算項目の補足
        for metric in ["sales", "op", "ordinary", "net_income", "eps"]:
            p = best_res.get(f"previous_{metric}")
            r = best_res.get(f"revised_{metric}")
            best_res[f"delta_{metric}"] = _calc_delta(p, r)
            best_res[f"change_{metric}_pct"] = _calc_pct(p, r)
        return best_res
    return None


def _extract_eps_from_prose(text: str) -> tuple[float | None, float | None]:
    """文章中から EPS 表現を抽出する。強アンカー制約あり。"""
    anchors = ["1株当たり", "当期純利益", "四半期純利益", "純利益", "EPS"]
    lines = text.split("\n")
    
    for i, line in enumerate(lines):
        if not any(a in line for a in anchors): continue
        
        context = " ".join(lines[max(0, i-1):min(len(lines), i+3)])
        context_no_space = _normalize_text(context).replace(" ", "")
        
        # 1. 矢印を用いた明示的な前回・今回修正パターンを優先
        # 例: 「修正前 100.00 → 修正後 120.00」「100円 → 120円」「EPS 100 → 120」
        # % が含まれるものは除外するガード
        arrow_match = re.search(
            r"(?:修正前|前回予想)?\s*([\d,.]+)\s*円?\s*[→→]\s*(?:修正後|今回修正予想)?\s*([\d,.]+)\s*円?",
            context_no_space
        )
        if arrow_match:
            # 抽出された数値の直後に % がないか、またはコンテキスト全体に % が含まれていないか
            matched_text = arrow_match.group(0)
            if "%" not in matched_text and "％" not in matched_text:
                prev = _parse_cell_value(arrow_match.group(1))
                rev = _parse_cell_value(arrow_match.group(2))
                if prev is not None and rev is not None:
                    return prev, rev

        # 既存の強アンカー個別検索
        prev_match = None; rev_match = None
        # 円銭パターン: 123円45銭 or 123.45円 or 123.45 (強アンカー時のみ)
        pattern_yen_sen = r"(\d[\d,]*\.?\d*)円(\d{1,2})銭"
        pattern_yen_only = r"(\d[\d,]*\.?\d*)円"
        pattern_no_unit = r"(\d[\d,]*\.?\d+)" # 単位なし (強アンカー時のみ限定)
        
        def find_val(anchor_keyword, ctx):
            # パーセンテージ除外
            if re.search(anchor_keyword + r"[^【】]*?(\d[\d,]*\.?\d*)[%％]", ctx):
                return None

            # 1. 円銭
            m_sen = re.search(anchor_keyword + r"[^【】]{0,15}?" + pattern_yen_sen, ctx)
            if m_sen:
                yen = _parse_number(m_sen.group(1)) or 0.0
                sen = _parse_number(m_sen.group(2)) or 0.0
                return round(yen + sen / 100.0, 2)
            
            # 2. 円のみ
            m_yen = re.search(anchor_keyword + r"[^【】]{0,15}?" + pattern_yen_only, ctx)
            if m_yen:
                s_val = m_yen.group(1)
                if re.search(rf"{re.escape(s_val)}[%％百万円千円億円]", ctx): return None
                val = _parse_number(s_val)
                if val is not None:
                    if abs(val) > 4000.0: return None
                    return val
            
            # 3. 単位なし (強アンカー括弧付きなどの場合のみ)
            # 例: 【修正前】136.04
            if "修正" in anchor_keyword or "予想" in anchor_keyword:
                m_no = re.search(anchor_keyword + r"[^【】]{0,5}?" + pattern_no_unit, ctx)
                if m_no:
                    s_val = m_no.group(1)
                    if re.search(rf"{re.escape(s_val)}[%％百万円千円億円]", ctx): return None
                    val = _parse_number(s_val)
                    if val is not None:
                        if abs(val) > 4000.0: return None
                        # 1.0 銭などの端数としての 1.0 ではなく、EPS としての 1.0 (1円) は許容
                        if abs(val) < 0.1: return None
                        return val
            return None

        rev_match = find_val("(?:今回修正予想|修正後|利益は|EPSは|修正予想）)", context_no_space)
        prev_match = find_val("(?:前回発表予想|修正前|当初予想|前回予想）)", context_no_space)
        
        if rev_match is not None:
            if "配当" in context_no_space:
                if not re.search(r"(?:利益|EPS|純利|予想)[^【】]{0,15}" + re.escape(str(rev_match).replace(".0", "")), context_no_space):
                    continue
            return prev_match, rev_match
            
    return None, None


def _extract_eps_from_notes(text: str) -> tuple[float | None, float | None]:
    """注記セクション ((注), 補足, ※) から EPS を抽出する。"""
    note_anchors = ["(注)", "補足", "※", "注記事項"]
    lines = text.split("\n")
    
    note_start = -1
    for i, line in enumerate(lines):
        if any(a in line for a in note_anchors):
            note_start = i
            break
    
    if note_start == -1: return None, None
    
    note_text = "\n".join(lines[note_start:])
    # 注記内の EPS 表現
    # 例: 「1株当たり当期純利益は 156.78円であります」
    # 例: 「修正後 156.78円」
    m = re.search(r"1株当たり[^\d\n]*?(\d[\d,]*\.?\d*)円", note_text)
    if m:
        val = _parse_number(m.group(1))
        if val is not None and abs(val) > 4.0:
            # Note は low priority なのでこれだけ返す
            return None, val
            
    return None, None


def _find_eps_from_lines_with_guard(
    lines: list[str], 
    pdf_path: str = ""
) -> tuple[float | None, float | None]:
    """縦ブロック抽出を行い、高度なガードを適用した結果を返す。"""
    # 既存の _find_eps_from_lines を呼び出し
    prev, rev = _find_eps_from_lines(lines)
    if prev is None and rev is None:
        return None, None

    # ガード適用
    # pdf_path がある場合は座標ベース、なければテキストベースの簡易ガード
    context_data = []
    if pdf_path and os.path.exists(pdf_path):
        context_data = _extract_text_lines_with_positions_prod(pdf_path)
    else:
        # pdf_path がない場合は行テキストから比率を擬似的に計算（中央固定など）
        for idx, l in enumerate(lines):
            context_data.append({'text': l, 'y_ratio': 0.5, 'page': 1})

    prev = _enhanced_guard_prod(prev, None, context_data, is_revised=False)
    rev = _enhanced_guard_prod(rev, None, context_data, is_revised=True)
    
    return prev, rev


def _extract_text_lines_with_positions_prod(pdf_path: str) -> list[dict]:
    """PDF から行ごとのテキストと Y 座標比率を取得。pdfplumber を使用。"""
    results = []
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            # 主要な表は最初の数ページにあることがほとんど
            for p_idx, page in enumerate(pdf.pages[:8]):
                h = page.height
                words = page.extract_words()
                if not words: continue
                words.sort(key=lambda x: (x['top'], x['x0']))
                current_top = words[0]['top']
                line_words = []
                for w in words:
                    if abs(w['top'] - current_top) > 3:
                        line_text = "".join(lw['text'] for lw in line_words)
                        results.append({'text': line_text, 'y_ratio': (current_top / h), 'page': p_idx + 1})
                        line_words = [w]; current_top = w['top']
                    else:
                        line_words.append(w)
                if line_words:
                    line_text = "".join(lw['text'] for lw in line_words)
                    results.append({'text': line_text, 'y_ratio': (current_top/h), 'page': p_idx + 1})
    except Exception as e:
        logger.debug(f"[EPS_GUARD] Coordinate extraction failed: {e}")
    return results


def _enhanced_guard_prod(
    val: float | None,
    old_val: float | None,
    context_lines: list[dict],
    is_revised: bool = False
) -> float | None:
    """位置・ラベル・文脈に基づく高度な数値棄却ロジック（本番統合版）。"""
    if val is None: return None
    val_abs = abs(val)

    # フィルタ設定
    REJECT_WORDS = ["配当", "利益率", "％", "%", "増減", "進捗", "達成", "実績", "前期", "修正率", "騰落"]
    STRONG_EPS_ANCHORS = ["1株当たり", "１株当たり", "一株当たり", "EPS", "eps", "純利益", "当期純利益"]
    COMMON_DIV_VALUES = [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 60.0, 70.0, 75.0, 80.0, 100.0]
    
    found_occurrences = []
    page_has_reject = {line['page']: True for line in context_lines if any(rw in line['text'] for rw in REJECT_WORDS)}

    for i, line in enumerate(context_lines):
        txt_for_match = line['text']
        for anchor in STRONG_EPS_ANCHORS:
            txt_for_match = txt_for_match.replace(anchor, "____")
        
        is_match = False
        tokens = re.findall(r"[\d,.]+", txt_for_match)
        for t in tokens:
            t_clean = t.replace(",", "")
            try:
                if abs(float(t_clean) - val_abs) < 0.01:
                    is_match = True; break
            except: continue

        if is_match:
            has_strong_same_line = any(a in line['text'] for a in STRONG_EPS_ANCHORS)
            start_idx = max(0, i - 2); end_idx = min(len(context_lines), i + 3)
            nearby_text = "".join(l['text'] for l in context_lines[start_idx:end_idx])

            is_hf = (line['y_ratio'] < 0.07 or line['y_ratio'] > 0.93)
            has_reject_word = any(rw in nearby_text for rw in REJECT_WORDS)
            page_wide_reject = page_has_reject.get(line['page'], False) and val_abs in COMMON_DIV_VALUES
            has_strong_nearby = any(a in nearby_text for a in STRONG_EPS_ANCHORS)
            has_weak_nearby = any(a in nearby_text for a in ["円", "銭"])

            if is_hf: 
                found_occurrences.append({'is_bad': True, 'reason': 'header_footer'}); continue
            if (has_reject_word or page_wide_reject) and not has_strong_same_line:
                found_occurrences.append({'is_bad': True, 'reason': 'reject_word_detected'}); continue

            if val_abs < 1.1: # 1.0 等
                bad_anchor = not has_strong_same_line # 1.0は同一行アンカー必須
            else:
                bad_anchor = not (has_strong_nearby or has_weak_nearby)
            
            found_occurrences.append({'is_bad': bad_anchor, 'reason': 'bad_anchor'})

    if any(not occ['is_bad'] for occ in found_occurrences):
        return val

    if found_occurrences:
        logger.info(f"[EPS_GUARD_REJECT] Rejected suspicious EPS: {val} (Reason: {found_occurrences[0]['reason']})")
    return None


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

    # ---- EPS 専用フォールバック検出 ----
    # EPS は別セクション（「(円 銭)」ヘッダー下）に出ることが多い
    # 上記で取れなかった場合、専用走査を行う
    if event.revised_eps is None:
        eps_prev, eps_rev = _find_eps_from_lines(lines)
        if eps_rev is not None:
            event.previous_eps = eps_prev
            event.revised_eps = eps_rev
            if eps_prev is not None and eps_rev is not None:
                event.delta_eps = _calc_delta(eps_prev, eps_rev)
                event.change_eps_pct = _calc_pct(eps_prev, eps_rev)
            metrics_count += 1
            confidence += 0.05
            logger.debug(
                f"[forecast_ocr] eps_fallback: "
                f"previous_eps={eps_prev} revised_eps={eps_rev}"
            )

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

    logger.debug(
        f"[forecast_ocr] ocr_line_parser result: "
        f"previous_eps={event.previous_eps} revised_eps={event.revised_eps} "
        f"metrics_count={metrics_count}"
    )

    return event


# ============================================================
# EPS 専用フォールバック検出
# ============================================================
# EPS ラベルの検出パターン
_EPS_LABELS = [
    "1株当たり当期純利益", "１株当たり当期純利益", "一株当たり当期純利益",
    "1株当たり四半期純利益", "１株当たり四半期純利益", "一株当たり四半期純利益",
    "1株当たり中間純利益", "1株当たり四半期純利益金額", "1株当たり当期純利益金額",
    "潜在株式調整後1株当たり当期純利益", "潜在株式調整後1株当たり四半期純利益",
    "潜在株式調整後１株当たり当期純利益", "潜在株式調整後１株当たり四半期純利益",
    "株当たり当期純利益", "株当たり四半期純利益", "1株当たり純利益",
    "１株当たり純利益", "一株当たり純利益", "EPS", "eps", "E P S", "e p s",
    "1 株 当 た り 当 期 純 利 益", "1 株 当 た り 四 半 期 純 利 益",
    "1株当たり当期純損益", "１株当たり当期純損益", "1株当たり", "１株当たり", "一株当たり",
    "1株", "一株", "当期純利益", "per share",
]

# 縦ブロック抽出で除外するメトリクス名
_EPS_BLOCK_EXCLUDE_KEYWORDS = [
    "売上高", "営業利益", "経常利益", "当期純利益", "四半期純利益",
    "純利益", "利益剰余金", "営業損益", "経常損益", "純損益",
    "増減額", "増減率", "対前期", "対前年",
    "配当", "株主",
    # 経営指標等から追加
    "総資産", "資産合計", "純資産", "自己資本", "持分法",
    "売上益", "売上損", "営業収益", "経常収益",
    "キャッシュ・フロー", "キャッシュフロー",
]


def _score_eps_candidate(value: float | None, line_text: str, is_revised: bool = False, label_pos: int = -1, val_pos: int = -1) -> float:
    """EPS数値候補の『らしさ』をスコアリングしてランク付けする。
    """
    if value is None:
        return -10.0

    score = 0.0
    val_abs = abs(value)
    text_normalized = _normalize_label(line_text)

    # 1. 範囲ボーナス (EPSとして一般的な範囲: 0.1 ~ 5000)
    if 0.1 <= val_abs <= 5000:
        score += 5.0
    if 1.0 <= val_abs <= 500:
        score += 2.0

    # 2. キーワード近接ボーナス (Strong Anchors)
    strong_anchors = ["1株当たり", "一株当たり", "EPS", "純利益", "当期純利益", "per share"]
    if any(a in text_normalized for a in strong_anchors):
        score += 10.0

    # 3. 単位・記号ボーナス
    if "円" in text_normalized:
        score += 3.0
    if "銭" in text_normalized:
        score += 5.0
    
    # 位置的な近接性ボーナス (同一行の右側の数値を優先)
    if label_pos >= 0 and val_pos > label_pos:
        score += 3.0

    # 4. 拒絶キーワードペナルティ (PL指標も含む)
    reject_words = ["配当", "利益率", "%", "％", "株主", "増減", "比"] + _EPS_BLOCK_EXCLUDE_KEYWORDS
    # "当期純利益" 等を含んでいても "1株当たり" があれば EPS なので減点しない
    is_eps_specific = any(el in text_normalized for el in ["1株当たり", "一株当たり", "EPS"])
    if not is_eps_specific:
        if any(rw in text_normalized for rw in reject_words):
            score -= 30.0

    # 5. 特定値ペナルティ (1.0 や 2.0 や 0.0 は極めて怪しい)
    if val_abs in [0.0, 1.0, 2.0, 3.0, 4.0]: # 1桁の整数は基本的にノイズ
        score -= 30.0

    # 6. 改訂/前回ラベルとの一致ボーナス
    if is_revised:
        if any(lbl in text_normalized for lbl in _REVISED_LABELS):
            score += 5.0
    else:
        if any(lbl in text_normalized for lbl in _PREVIOUS_LABELS):
            score += 5.0

    return score


def _get_numbers_with_positions(line: str) -> list[tuple[float, int]]:
    """行内の数値とその文字開始位置を取得する"""
    clean = _normalize_text(line)
    # 数値パターン: △/▲付き、カンマ付き整数、小数、ダッシュ(None値)は除外
    pattern = r'(?<![.\d])([△▲]?\s*[\d,]+\.?\d*%?)(?![年月日期四株])'
    matches = re.finditer(pattern, clean)
    results = []
    for m in matches:
        val = _parse_cell_value(m.group(0))
        if val is not None:
            results.append((val, m.start()))
    return results


def _find_eps_from_lines(
    lines: list[str],
) -> tuple[float | None, float | None]:
    """テキスト行から EPS の前回/修正値を専用走査で抽出する。

    走査ロジック:
    1. EPS ラベルを含むヘッダー行を探す
    2. まず縦ブロック抽出を試す（ラベル直下 1〜3行）
    3. 取れなければ周辺の前回/修正ラベル行から取得
    4. それでも取れなければヘッダー直後の数値行2行を使う

    Returns: (previous_eps, revised_eps)
    """
    reject_reason = "label_not_found"

    # Step 1: EPS ラベルを含むヘッダー行を「すべて」探す
    header_indices = []
    for i, line in enumerate(lines):
        clean = _normalize_text(line).lower().strip()
        if not clean:
            continue
        # 部分一致でOK
        if any(lbl.lower() in clean for lbl in _EPS_LABELS):
            header_indices.append(i)
            continue

    # ラベルなし救済: 「前回」「今回」等のキーワードがある場所を仮のヘッダーとする
    if not header_indices:
        for i, line in enumerate(lines):
            clean = line.lower()
            if any(lbl in clean for lbl in _REVISED_LABELS + _PREVIOUS_LABELS):
                # 数値が含まれているか確認し、あれば候補行とする
                if re.search(r'\d', clean):
                    header_indices.append(i)
                    # 救済モードではあまり広げすぎない
                    if len(header_indices) > 3: break

    if not header_indices:
        return None, None

    logger.info(f"[forecast_ocr] eps_labels_detected count={len(header_indices)} indices={header_indices}")

    all_prev_candidates: list[tuple[float, float, int]] = []
    all_rev_candidates: list[tuple[float, float, int]] = []

    for eps_header_idx in header_indices:
        # Step 2: 縦位置優先探索 (同一行、真下1行、真下2行)
        # これを Phase 1: 優先レスキューとして扱う
        candidate_lines = []
        for offset in range(3): # 0, 1, 2
            idx = eps_header_idx + offset
            if idx < len(lines):
                raw = lines[idx]
                # PL指標ラベル（売上・利益等）が含まれる行は EPS 候補数値として除外
                # ただし、「1株当たり」等の EPS 限定ラベルが共存していれば通過させる
                is_eps_specific = any(kw in raw for kw in ["1株当たり", "一株当たり", "EPS"])
                if not is_eps_specific:
                    if any(kw in raw for kw in _EPS_BLOCK_EXCLUDE_KEYWORDS):
                        continue
                nums = _get_numbers_with_positions(raw)
                if nums:
                    candidate_lines.append((idx, nums, raw))

        if len(candidate_lines) >= 1:
            # 同一行に2つある場合
            if len(candidate_lines[0][1]) >= 2:
                all_prev_candidates.append((candidate_lines[0][1][0][0], 25.0, eps_header_idx))
                all_rev_candidates.append((candidate_lines[0][1][1][0], 25.0, eps_header_idx))
            # 真下1行、真下2行に1つずつある場合
            elif len(candidate_lines) >= 3 and len(candidate_lines[1][1]) >= 1 and len(candidate_lines[2][1]) >= 1:
                all_prev_candidates.append((candidate_lines[1][1][0][0], 20.0, eps_header_idx))
                all_rev_candidates.append((candidate_lines[2][1][0][0], 20.0, eps_header_idx))
            # 真下1行のみに数値があるケース（単独改訂など）
            elif len(candidate_lines) >= 2 and len(candidate_lines[1][1]) >= 1:
                all_rev_candidates.append((candidate_lines[1][1][0][0], 15.0, eps_header_idx))

        # 汎用縦ブロック抽出
        vb_prev, vb_rev = _find_eps_vertical_block(lines, eps_header_idx)
        if vb_prev is not None:
            all_prev_candidates.append((vb_prev, 20.0, eps_header_idx))
        if vb_rev is not None:
            all_rev_candidates.append((vb_rev, 20.0, eps_header_idx))

        # Step 3: 周辺の前回/修正ラベル行から取得
        search_start = max(0, eps_header_idx - 3)
        search_end = min(len(lines), eps_header_idx + 15)

        eps_prev_idx = None
        eps_rev_idx = None
        for i in range(search_start, search_end):
            clean = _normalize_text(lines[i]).lower().strip()
            if not clean:
                continue
            for lbl in _PREVIOUS_LABELS:
                if lbl.lower() in clean:
                    eps_prev_idx = i
                    break
            for lbl in _REVISED_LABELS + _ACTUAL_LABELS:
                if lbl.lower() in clean:
                    eps_rev_idx = i
                    break

        if eps_prev_idx is not None:
            raw_line = lines[eps_prev_idx]
            label_pos = -1
            for lbl in _PREVIOUS_LABELS:
                if lbl.lower() in raw_line.lower():
                    label_pos = raw_line.lower().find(lbl.lower())
                    break
            nums_with_pos = _get_numbers_with_positions(raw_line)
            for val, v_pos in nums_with_pos:
                all_prev_candidates.append((val, _score_eps_candidate(val, raw_line, is_revised=False, label_pos=label_pos, val_pos=v_pos), eps_header_idx))

        if eps_rev_idx is not None:
            raw_line = lines[eps_rev_idx]
            label_pos = -1
            for lbl in _REVISED_LABELS + _ACTUAL_LABELS:
                if lbl.lower() in raw_line.lower():
                    label_pos = raw_line.lower().find(lbl.lower())
                    break
            nums_with_pos = _get_numbers_with_positions(raw_line)
            for val, v_pos in nums_with_pos:
                all_rev_candidates.append((val, _score_eps_candidate(val, raw_line, is_revised=True, label_pos=label_pos, val_pos=v_pos), eps_header_idx))

    # スコア順にソートしてトップを採用
    all_prev_candidates.sort(key=lambda x: x[1], reverse=True)
    all_rev_candidates.sort(key=lambda x: x[1], reverse=True)

    prev_eps = all_prev_candidates[0][0] if all_prev_candidates else None
    rev_eps = all_rev_candidates[0][0] if all_rev_candidates else None

    if rev_eps is not None:
        reject_reason = "none"
    elif not all_prev_candidates and not all_rev_candidates:
        reject_reason = "number_not_found_near_label"
    else:
        reject_reason = "candidate_rejected_by_score"

    if reject_reason != "none":
        logger.info(f"[forecast_ocr] EPS extraction fallthrough: reason={reject_reason}")

    return prev_eps, rev_eps

    return prev_eps, rev_eps




def _find_eps_vertical_block(
    lines: list[str],
    eps_header_idx: int,
) -> tuple[float | None, float | None]:
    """EPSラベル直下の縦ブロックから前回/修正EPSを抽出する。

    緩和条件:
    1. 同一行に数値が2つ以上ある場合は [前回, 修正済] のペアとして即採用
    2. 数値行が連続している場合はラベルがなくても順に拾う
    3. 「修正」「予想」「今回」「前回」などのキーワードがある行を優先
    """
    search_end = min(len(lines), eps_header_idx + 5)
    candidates = []

    for i in range(eps_header_idx + 1, search_end):
        raw = lines[i]
        clean = _normalize_text(raw).strip()
        if not clean:
            continue
        # 別メトリクス名を含む行は除外（当期純利益はEPS近傍なので許可）
        clean_lower = clean.lower()
        if any(kw in clean_lower for kw in _EPS_BLOCK_EXCLUDE_KEYWORDS if kw != "当期純利益"):
            break
        
        nums = _extract_numbers_from_line(clean)
        if nums:
            candidates.append({
                "nums": nums,
                "clean": clean,
                "has_kw": any(kw in clean for kw in ["修正", "予想", "今回", "前回"])
            })

    if not candidates:
        return None, None

    # 戦略1: 同一行に数値が2回以上出現する場合 (横型の埋め込み)
    for c in candidates:
        if len(c["nums"]) >= 2:
            return c["nums"][0], c["nums"][1]

    # 戦略2: ラベル一致を優先
    prev_eps = None
    rev_eps = None
    for c in candidates:
        is_prev = any(lbl in c["clean"] for lbl in _PREVIOUS_LABELS)
        is_rev = any(lbl in c["clean"] for lbl in _REVISED_LABELS + _ACTUAL_LABELS)
        if is_rev and rev_eps is None:
            rev_eps = c["nums"][0]
        elif is_prev and prev_eps is None:
            prev_eps = c["nums"][0]

    if prev_eps is not None or rev_eps is not None:
        return prev_eps, rev_eps

    # 戦略3: ラベルがなくても数値構造だけで通す (縦の並び)
    num_list = []
    for c in candidates:
        # %値などは除外
        if "%" in c["clean"] or "％" in c["clean"]:
            continue
        num_list.append(c["nums"][0])
    
    if len(num_list) >= 2:
        return num_list[0], num_list[1]
    elif len(num_list) == 1:
        return None, num_list[0]

    return None, None


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


# 補助経路呼び出しのトリガーキーワード（本文に含まれる場合のみ専用抽出器を起動）
_FY_TRIGGER_KEYWORDS = [
    "業績予想", "修正後", "通期予想", "決算短信",
    "上期業績", "展期予測", "予想修正",
]
# 補助経路を呼ばない除外キーワード
_FY_SKIP_KEYWORDS = [
    "配当おおび", "月次", "KPI", "決議通知",
]


def _return_with_eps_log(
    event: ForecastRevisionEvent,
    text: str = "",
) -> ForecastRevisionEvent:
    """EPS を INFO ログに出力し、補助経路による latest_full_year_eps を設定してから event を返す。"""
    if event.previous_eps is not None or event.revised_eps is not None:
        logger.info(
            f"[forecast_ocr] EPS prev={event.previous_eps} rev={event.revised_eps}"
        )

    # --- 補助経路: extract_latest_full_year_eps ---
    if text and event.latest_full_year_eps is None:
        text_n = text[:2000]  # 最先頭 2000 文字で卷屋判定
        has_trigger = any(kw in text_n for kw in _FY_TRIGGER_KEYWORDS)
        has_skip = any(kw in text_n for kw in _FY_SKIP_KEYWORDS)
        if has_trigger and not has_skip:
            try:
                val = extract_latest_full_year_eps(text)
                event.latest_full_year_eps = val
                if val is not None:
                    logger.info(
                        f"[latest_full_year_eps] extracted={val}"
                    )
                else:
                    logger.debug(
                        "[latest_full_year_eps] result=None (attempted, not found)"
                    )
            except Exception as _e:
                logger.debug(f"[latest_full_year_eps] exception={_e}")
        else:
            logger.debug(
                f"[latest_full_year_eps] skipped "
                f"(has_trigger={has_trigger} has_skip={has_skip})"
            )

    return event


def extract_forecast_revision(
    text: str,
    title: str = "",
    is_difference: bool = False,
    pdf_path: str = "",
    doc_url: str = "",
    doc_id: str = "",
) -> ForecastRevisionEvent:
    """テキストから業績予想修正イベントを抽出する。

    Phase 1: Native/Prose/Note (および pdfplumber) による軽量・高速抽出
    Phase 2: OCR Fallback (最終手段)
    """
    _doc_label = doc_id[:16] if doc_id else "?"

    # ---- Phase 1: 既存テキストおよび pdfplumber から抽出 ----
    # ここに Native/Prose/Note/pdfplumber 全ロジックが含まれる
    base = _extract_from_text(text, title, is_difference, source="pdf_text", pdf_path=pdf_path)

    # ---- Phase 2: OCR フォールバック判定 (最終 Fallback) ----
    # EPS が未取得かつ、OCR 実行条件を満たす場合のみ実行
    logger.debug(f"[forecast_ocr] Entering OCR fallback logic for doc_id={_doc_label}")
    if not _HAS_OCR_MODULE:
        return _return_with_eps_log(base, text)

    try:
        diagnostics: dict = {}
        should_run = _should_run_ocr(text, base, diagnostics)
        logger.info(f"[forecast_ocr] should_run_ocr result: {should_run} (diagnostics: {diagnostics})")

        if not should_run:
            return _return_with_eps_log(base, text)

        # ==== should_run=True → 必ず start ログ ====
        logger.info(f"[forecast_ocr] Starting OCR execution for doc_id={_doc_label}")

        # OCR 環境チェック
        if not _ocr_enabled_check():
            logger.info(f"[forecast_ocr] disabled (ENABLE_GOOGLE_OCR not set)")
            return _return_with_eps_log(base, text)

        # ---- PDF 取得 ----
        ocr_pdf_path = pdf_path
        if not ocr_pdf_path and doc_url:
            ocr_pdf_path = _download_pdf_for_ocr(doc_url)

        if not ocr_pdf_path:
            logger.info(
                f"[forecast_ocr] skipped reason=no_pdf_source "
                f"doc_id={_doc_label}"
            )
            return _return_with_eps_log(base, text)

        # ---- Ghostscript ラスタライズ ----
        logger.info(f"[forecast_ocr] Rasterizing PDF: {ocr_pdf_path}")
        images = _rasterize(ocr_pdf_path)
        if not images:
            logger.info(f"[forecast_ocr] skipped reason=rasterize_failed")
            return base

        try:
            # ---- Google OCR テキスト抽出 ----
            logger.info(f"[forecast_ocr] Calling Google OCR API...")
            ocr_text = _google_ocr(images)
            if not ocr_text.strip():
                logger.info(f"[forecast_ocr] OCR text extraction failed (empty result)")
                return base
            logger.info(f"[forecast_ocr] OCR text extraction succeeded (len={len(ocr_text)})")

            # ---- normalize（OCR 専用、native には絶対かけない）----
            normalized_ocr = _normalize_ocr_text(ocr_text)
            logger.info(
                f"[forecast_ocr] normalized_text_len={len(normalized_ocr)}"
            )

            # ---- 既存パーサに通す ----
            logger.info(f"[forecast_ocr] Re-running core parser (_extract_from_text) on OCR text")
            ocr_event = _extract_from_text(
                normalized_ocr, title, is_difference, source="ocr_text", pdf_path=pdf_path
            )

            # ---- score==0 の時だけ簡易行パーサ ----
            ocr_score_val = _score_result(ocr_event)
            if ocr_score_val == 0:
                logger.info(f"[forecast_ocr] core parser failed on OCR text (score=0), attempting fallback row parser")
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
                    return _return_with_eps_log(base, text)
                ocr_event.extraction_source = "ocr_fallback"
                logger.info(
                    f"[forecast_ocr] selected=ocr "
                    f"reason=ocr_fallback_used"
                )
                return _return_with_eps_log(ocr_event)
            elif final_ocr_score == 0:
                logger.info(
                    f"[forecast_ocr] selected=base "
                    f"reason=ocr_no_signal"
                )
                return _return_with_eps_log(base, text)
            else:
                logger.info(
                    f"[forecast_ocr] selected=base "
                    f"reason=native_keep"
                )
                return _return_with_eps_log(base, text)

        finally:
            _cleanup_images(images)

    except Exception as e:
        logger.warning(
            f"[forecast_ocr] OCR fallback failed (non-fatal): {e}"
        )
        return _return_with_eps_log(base)


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


# ============================================================
# 今回の通期EPS予想 専用抽出器
# ============================================================
# 採用してよいEPSラベル
_FY_EPS_LABELS = [
    "1株当たり当期純利益",
    "１株当たり当期純利益",
    "一株当たり当期純利益",
    "1株当たり純利益",
    "１株当たり純利益",
    "EPS",
    "1株当たり利益",
    "１株当たり利益",
]

# 候補ブロック検出語（いずれかを含む行の周辺をブロック候補にする）
_FY_EPS_BLOCK_KEYWORDS = [
    "業績予想", "通期予想", "修正後", "今回予想", "売上高",
    "営業利益", "経常利益", "当期純利益", "1株当たり当期純利益",
]

# 今回/修正後 側の列ラベル（優先順、加点用）
_FY_EPS_REVISED_COL_KEYWORDS = [
    "修正後", "今回修正予想", "今回修正", "今回予想", "今回",
    "通期予想", "当期予想", "会社予想", "今期予想",
]

# 前回/修正前 側の列ラベル（これに合致する列値は読まない）
_FY_EPS_PREVIOUS_COL_KEYWORDS = [
    "前回予想", "修正前", "従来予想", "前回発表予想", "前回修正予想",
]

# 増減列・比較列の列ラベル（計算差分列を EPS 候補から除外する）
_FY_EPS_DIFF_COL_KEYWORDS = [
    "増減額", "増減率", "増減", "差額", "差分", "修正率", "変化率",
]

# 強除外語（近傍にあれば候補を強制除外）
_FY_EPS_HARD_EXCLUDE = [
    "第1四半期", "第２四半期", "第2四半期",
    "第3四半期", "第４四半期", "第4四半期",
    "四半期", "累計", "上期", "中間",
    "1株当たり四半期純利益", "１株当たり四半期純利益",
]

# 減点語（近傍にあると採用優先度を下げる）
_FY_EPS_SOFT_PENALTY = [
    "実績", "前回予想", "修正前", "従来予想",
]

# BPS/配当 行文脈語（候補値のある行/列見出しに含まれていれば除外）
_FY_EPS_DIV_BPS_WORDS = [
    "配当", "BPS", "純資産", "発行済株式数",
]

_logger_fy = logging.getLogger("forecast_extractor.full_year_eps")


def _fy_normalize(text: str) -> str:
    """専用抽出器内部用 軽量正規化（NFKC + 全角スペース）"""
    s = unicodedata.normalize("NFKC", text)
    return s.replace("\u3000", " ")


def _fy_context_window(lines: list[str], center: int, radius: int = 4) -> str:
    """center 行を中心に radius 行の文脈テキストを返す"""
    lo = max(0, center - radius)
    hi = min(len(lines), center + radius + 1)
    return " ".join(lines[lo:hi])


def _fy_is_hard_excluded(context: str) -> bool:
    """強除外語が文脈に含まれているか判定"""
    n = _fy_normalize(context)
    return any(kw in n for kw in _FY_EPS_HARD_EXCLUDE)


def _fy_is_div_bps_context(line_text: str) -> bool:
    """BPS/配当文脈か判定"""
    n = _fy_normalize(line_text)
    return any(kw in n for kw in _FY_EPS_DIV_BPS_WORDS)


# 金額文脈語（EPS ラベルを持たない行にこれらが含まれていれば「金額行」とみなす）
_FY_AMOUNT_CONTEXT_WORDS = [
    "売上高", "営業利益", "経常利益", "当期純利益",
    "親会社株主に帰属", "百万円", "千円", "金額",
]


def _fy_is_amount_row(line_text: str) -> bool:
    """金額行判定（EPS ラベルを持たない行で金額文脈語あり）。

    offset != 0 の候補行に適用し、純粋な金額行（売上高・当期純利益等の
    金額列）を EPS として誤採用することを防ぐ。
    EPS ラベル（株当たり等）が同行に存在する場合は金額行扱いにしない。
    """
    n = _fy_normalize(line_text)
    # EPS ラベルがあれば金額行扱いにしない
    if any(lbl in n for lbl in _FY_EPS_LABELS):
        return False
    if "株当たり" in n or "EPS" in n or "eps" in n:
        return False
    # 金額文脈語を含む場合は金額行とみなす
    return any(kw in n for kw in _FY_AMOUNT_CONTEXT_WORDS)


def _fy_score_eps_label(line_text: str) -> float:
    """
    EPSラベル行のスコアを計算する。
    返り値が高いほど「今回の通期EPS予想」らしい行。
    """
    n = _fy_normalize(line_text)
    score = 0.0

    # 1. EPSラベル加点
    for lbl in _FY_EPS_LABELS:
        if lbl in n:
            score += 10.0
            break

    # 2. 今回/通期/修正後文脈で加点
    for kw in _FY_EPS_REVISED_COL_KEYWORDS:
        if kw in n:
            score += 5.0
            break

    # 3. 強除外語で大幅減点
    for kw in _FY_EPS_HARD_EXCLUDE:
        if kw in n:
            score -= 30.0
            break

    # 4. 前回/修正前文脈で減点
    for kw in _FY_EPS_PREVIOUS_COL_KEYWORDS:
        if kw in n:
            score -= 8.0
            break

    # 5. 実績で軽減点
    if "実績" in n:
        score -= 4.0

    # 6. BPS/配当文脈は大幅減点
    if _fy_is_div_bps_context(n):
        score -= 20.0

    return score


def _fy_find_revised_column_index(header_lines: list[str]) -> int | None:
    """
    ヘッダー行群から「今回/修正後」に対応するデータ列インデックスを返す。
    ヘッダー行の先頭が空文字やラベル列（数値なし）の場合は除いてから
    データ列オフセットとして返す。見つからなければ None。
    """
    for line in reversed(header_lines):  # 直前のヘッダー行を優先
        n = _fy_normalize(line)
        parts = re.split(r"\s{2,}|\t", n)
        # 先頭の空文字パーツを除去（strip 前の行頭スペースが残っていた場合）
        while parts and not parts[0].strip():
            parts = parts[1:]
        if not parts:
            continue

        previous_data_indices: set[int] = set()
        diff_data_indices: set[int] = set()
        revised_data_idx: int | None = None
        for data_col_idx, part in enumerate(parts):
            part_n = _fy_normalize(part)
            if any(kw in part_n for kw in _FY_EPS_PREVIOUS_COL_KEYWORDS):
                previous_data_indices.add(data_col_idx)
            if any(kw in part_n for kw in _FY_EPS_DIFF_COL_KEYWORDS):
                diff_data_indices.add(data_col_idx)
            if revised_data_idx is None and any(
                kw in part_n for kw in _FY_EPS_REVISED_COL_KEYWORDS
            ):
                if data_col_idx not in previous_data_indices and data_col_idx not in diff_data_indices:
                    revised_data_idx = data_col_idx
        if revised_data_idx is not None:
            return revised_data_idx
    return None


def _fy_pick_value_from_row(line: str, col_idx: int | None) -> float | None:
    """
    行から col_idx 列の値を取得。
    先頭のラベル列（純粋テキスト）を除いたデータ列インデックスとして col_idx を使う。
    col_idx=None の場合: 小数点付き候補が1つだけなら採用、複数は None（安全側）。
    """
    n = _fy_normalize(line)
    parts = re.split(r"\s{2,}|\t", n)
    parts = [p for p in parts if "%" not in p and "％" not in p]

    if col_idx is not None:
        # 先頭のラベル列（純粋テキスト or EPSラベル）を除外してデータ列に合わせる
        # 「1株当たり当期純利益」のような先頭に数字を含むラベルも正しくスキップする
        _LABEL_MARKERS = ["株当たり", "売上", "営業", "経常", "当期純", "純利益", "EPS", "eps"]
        data_parts = []
        label_consumed = False
        for p in parts:
            if not label_consumed:
                pn = _fy_normalize(p)
                # EPSラベルや行ラベルキーワードを含む → 無条件にラベル列扱い
                if any(mk in pn for mk in _LABEL_MARKERS):
                    continue
                v = _parse_cell_value(p)
                if v is None and not re.search(r"\d", p):
                    # 数値もなし → ラベル列として読み飛ばす
                    continue
                label_consumed = True
            data_parts.append(p)

        if col_idx < len(data_parts):
            val = _parse_cell_value(data_parts[col_idx])
            return _fy_guard_value(val)
        return None

    # col_idx が None: 数値候補を収集してフィルタリング
    nums = _extract_numbers_from_line(line)
    decimal_candidates = []
    whole_candidates = []   # 整数フロート（例: 156.00 → 156.0）
    for v in nums:
        if v is None:
            continue
        g = _fy_guard_value(v)
        if g is None:
            continue
        if g != int(g):
            decimal_candidates.append(g)
        else:
            whole_candidates.append(g)

    # 小数点付き候補が 1 件のみ → 採用（従来通り）
    if len(decimal_candidates) == 1:
        return decimal_candidates[0]

    # 整数値候補が 1 件のみ（小数なし）かつ EPS ラベルが同一行 → 採用
    # 例: 「1株当たり当期純利益  156.00」のような整数値 EPS
    if len(whole_candidates) == 1 and not decimal_candidates:
        n_check = _fy_normalize(line)
        if any(lbl in n_check for lbl in _FY_EPS_LABELS):
            return whole_candidates[0]
    return None


def _fy_guard_value(val: float | None) -> float | None:
    """
    EPS候補値の数値ガード。
    10000 以上の整数は除外（発行株式数・売上高誤認防止）。
    """
    if val is None:
        return None
    val_abs = abs(val)
    if val_abs >= 10000 and val_abs == int(val_abs):
        _logger_fy.debug(
            f"[reject] value={val} reject_reason=unrealistic_large_integer"
        )
        return None
    return val


def extract_latest_full_year_eps(text: str) -> float | None:
    """
    今回の通期EPS予想 1値だけを返す専用抽出器。

    抽出対象: 「今回の通期（フルイヤー）1株当たり利益予想」のみ。
    取らないもの: 四半期EPS・累計EPS・中間EPS・前回予想・配当・BPS 等。

    Args:
        text: PDF テキスト or OCR 結果（改行区切りの生テキスト）

    Returns:
        float | None: 今回の通期EPS予想値（円単位）。取得できない場合は None。
    """
    full = _fy_normalize(text)
    lines = [l.strip() for l in full.split("\n")]

    # ------------------------------------------------------------------
    # Step 1: 候補ブロック抽出
    # _FY_EPS_BLOCK_KEYWORDS にヒットした行ごとに ±15行のブロックを作る。
    # ヒット 0件でも後段の EPS ラベル検出で全行を対象に絞り込む。
    # ------------------------------------------------------------------
    block_center_indices: list[int] = []
    for i, line in enumerate(lines):
        if any(kw in line for kw in _FY_EPS_BLOCK_KEYWORDS):
            block_center_indices.append(i)

    if block_center_indices:
        block_indices: set[int] = set()
        for center in block_center_indices:
            lo = max(0, center - 15)
            hi = min(len(lines), center + 16)
            block_indices.update(range(lo, hi))
        block_line_map = sorted(block_indices)
        _logger_fy.debug(
            f"[candidate_block_found] centers={block_center_indices[:5]} "
            f"block_lines={len(block_line_map)}"
        )
    else:
        block_line_map = list(range(len(lines)))
        _logger_fy.debug("[candidate_block_found] no_block_keywords_hit -> full_text")

    # ------------------------------------------------------------------
    # Step 2: EPSラベル検出（複数候補収集→スコアリングで最良候補を選ぶ）
    # ------------------------------------------------------------------
    eps_label_candidates: list[tuple[int, float]] = []  # (行インデックス, スコア)

    for orig_idx in block_line_map:
        line = lines[orig_idx]
        if not any(lbl in line for lbl in _FY_EPS_LABELS):
            continue
        # 近傍文脈（±1行）で強除外チェック
        # radius=1 に紞って個別どの四半期語が近位にない㑪期 EPS 候補わで象随除しない
        ctx = _fy_context_window(lines, orig_idx, radius=1)
        if _fy_is_hard_excluded(ctx):
            _logger_fy.debug(
                f"[eps_label_found] idx={orig_idx} -> hard_excluded "
                f"text={line[:40]!r}"
            )
            continue
        score = _fy_score_eps_label(line)
        _logger_fy.debug(
            f"[eps_label_found] idx={orig_idx} score={score:.1f} "
            f"text={line[:50]!r}"
        )
        eps_label_candidates.append((orig_idx, score))

    if not eps_label_candidates:
        _logger_fy.debug("[reject] reject_reason=no_eps_label")
        return None

    # スコア降順ソート
    eps_label_candidates.sort(key=lambda x: x[1], reverse=True)
    top_score = eps_label_candidates[0][1]
    # top - 5 以内の候補を全評価（近似スコアも見る）
    eval_candidates = [
        (idx, sc) for idx, sc in eps_label_candidates if sc >= top_score - 5
    ]

    # ------------------------------------------------------------------
    # Step 3〜4: 今回/通期列の特定と値の読み取り
    # ------------------------------------------------------------------
    value_candidates: list[tuple[float, float, str]] = []  # (値, スコア, 理由)

    for eps_idx, label_score in eval_candidates:
        # ヘッダー行（eps_idx の前 5行以内）から列インデックスを探す
        header_lines = [
            lines[h] for h in range(max(0, eps_idx - 5), eps_idx)
            if lines[h].strip()
        ]
        col_idx = _fy_find_revised_column_index(header_lines)
        _logger_fy.debug(
            f"[selected_column_label] eps_idx={eps_idx} col_idx={col_idx}"
        )

        # Step 4: 同一行 → 真下1行 → 真下2行 の順に値を探す
        # また、EPSラベルより前の行（値行が先に来るパターン）も探す
        # 前行は優先度を下げて探索（priority_score = 0.5）
        # ただし、ヘッダー行やEPSラベル行は backward の候補にしない
        forward_offsets = [(0, 3.0), (1, 2.0), (2, 1.0)]
        backward_offsets: list[tuple[int, float]] = []
        for back in range(1, min(4, eps_idx + 1)):
            bk_idx = eps_idx - back
            bk_line = lines[bk_idx]
            # EPSラベル行、列ヘッダー行（今回/前回キーワード行）は backward 候補外
            if any(lbl in bk_line for lbl in _FY_EPS_LABELS):
                break
            if any(kw in bk_line for kw in _FY_EPS_REVISED_COL_KEYWORDS + _FY_EPS_PREVIOUS_COL_KEYWORDS):
                break
            backward_offsets.append((-back, 0.5))

        for offset, priority_score in forward_offsets + backward_offsets:
            target_idx = eps_idx + offset
            if target_idx < 0 or target_idx >= len(lines):
                continue
            target_line = lines[target_idx]

            # BPS/配当 行文脈チェック（行レベル）
            if _fy_is_div_bps_context(target_line):
                _logger_fy.debug(
                    f"[reject] idx={target_idx} "
                    f"reject_reason=dividend_or_bps_context"
                )
                continue

            # offset != 0 の行（EPS ラベル行自体でない）が金額行なら除外
            if offset != 0 and _fy_is_amount_row(target_line):
                _logger_fy.debug(
                    f"[reject] idx={target_idx} offset={offset} "
                    f"reject_reason=amount_row_not_eps"
                )
                continue

            # 強除外チェック（近傍 ±1行）
            ctx = _fy_context_window(lines, target_idx, radius=1)
            if _fy_is_hard_excluded(ctx):
                _logger_fy.debug(
                    f"[reject] idx={target_idx} "
                    f"reject_reason=quarterly_context"
                )
                continue

            val = _fy_pick_value_from_row(target_line, col_idx)
            if val is None:
                continue

            # 実績語が近傍にあれば軽減点
            soft_penalty = 0.0
            for kw in _FY_EPS_SOFT_PENALTY:
                if kw in ctx:
                    soft_penalty -= 2.0
                    break

            total_score = label_score + priority_score + soft_penalty
            _logger_fy.debug(
                f"[selected_value] val={val} score={total_score:.1f} "
                f"eps_idx={eps_idx} offset={offset}"
            )
            value_candidates.append((val, total_score, f"eps_idx={eps_idx}+{offset}"))
            break  # この eps_idx から値が1つ取れたら次の eps_idx へ

    if not value_candidates:
        _logger_fy.debug("[reject] reject_reason=no_current_full_year_column")
        return None

    # ------------------------------------------------------------------
    # Step 5後半: 複数候補の絞り込み
    # ------------------------------------------------------------------
    value_candidates.sort(key=lambda x: x[1], reverse=True)
    best_val, best_score, best_reason = value_candidates[0]

    # スコア差が 3 未満かつ値が大きく異なる場合は曖昧 → None
    if len(value_candidates) >= 2:
        second_val, second_score, _ = value_candidates[1]
        score_gap = best_score - second_score
        val_differs = abs(best_val - second_val) > max(1.0, abs(best_val) * 0.2)
        if score_gap < 3.0 and val_differs:
            _logger_fy.debug(
                f"[reject] best={best_val} second={second_val} gap={score_gap:.1f} "
                f"reject_reason=ambiguous_multiple_candidates"
            )
            return None

    _logger_fy.info(
        f"[selected_value] latest_full_year_eps={best_val} "
        f"score={best_score:.1f} reason={best_reason}"
    )
    return best_val
