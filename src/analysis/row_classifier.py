# ============================================================
# row_classifier.py — セグメント候補テーブルの行分類器
# ============================================================
"""
PDF セグメント抽出の候補テーブル内の各行を分類し、
候補テーブル全体の品質を判定する。

行分類 (RowClass):
  1. garbage_fragment_like   - 意味をなさない断片
  2. pl_account_like         - PL 勘定科目
  3. bs_cf_like              - BS/CF 項目・説明
  4. narrative_like          - 本文ナラティブ
  5. detail_breakdown_like   - 内訳・収入明細
  6. total_or_metric_like    - 合計・小計・指標
  7. valid_segment_like      - 有効なセグメント名
  8. unknown                 - 分類不能

候補テーブルガード (CandidateGuardResult):
  行分類を集計し、候補テーブル全体を accept / reject する。
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("tdnet.row_classifier")


# ============================================================
# キーワード辞書
# ============================================================

# --- Narrative (本文テキスト) ---
# Strong: 単独で narrative 判定
NARRATIVE_STRONG_KW = [
    "につきましては", "により", "となりました", "なりました",
    "前期末比", "当第", "当連結",
    "この結果", "実施", "計上",
    "キャッシュ・フロー", "営業活動", "投資活動", "財務活動",
]
# Weak: 単独では narrative にしない。自然文シグナルとの複合のみ
NARRATIVE_WEAK_KW = [
    "増加", "減少", "影響", "改善", "注力",
    "回復", "伸長", "悪化", "推移", "見込", "見込み",
]
# 後方互換用 (既存参照がある場合)
NARRATIVE_KW = NARRATIVE_STRONG_KW + NARRATIVE_WEAK_KW

# --- PL_summary_guard 用パターン ---
# (A) 全社 PL 指標キーワード
_PL_SUMMARY_METRICS = [
    "売上高", "営業利益", "経常利益", "当期純利益", "親会社株主に帰属",
]
# 自然文シグナル: 助詞パターン
_SENTENCE_PARTICLES = ("は", "が", "を", "に", "で", "と", "へ", "から", "まで")
_SENTENCE_ENDINGS = ("により", "となりました", "なりました", "ました", "です", "ある", "おり")


# --- BS/CF 項目 ---
BS_CF_KW = [
    "流動資産", "固定資産", "流動負債", "固定負債", "自己資本",
    "有形固定資産", "無形固定資産", "投資有価証券",
    "その他有価証券評価差額金",
    "営業活動によるキャッシュ・フロー",
    "投資活動によるキャッシュ・フロー",
    "財務活動によるキャッシュ・フロー",
    "現金及び現金同等物",
    "資産の部", "負債の部", "純資産の部",
]

# BS/CF の単体キーワード (短い語は文脈チェック付き)
BS_CF_SHORT_KW = [
    "資産", "負債", "純資産", "有価証券", "配当金",
    "現金", "借入金",
]

# --- Detail Breakdown (内訳・収入明細) ---
DETAIL_BREAKDOWN_KW = [
    "倉庫収入", "港湾運送収入", "国際輸送収入", "陸上運送",
    "ほか収入", "売上収入", "内訳",
]

# 括弧で囲まれた収入明細パターン
_DETAIL_BRACKET_RE = re.compile(r'^[（(].+[）)]$')

# --- Total / Metric (合計・指標) ---
TOTAL_OR_METRIC_KW = [
    "純営業収益", "セグメント間内部", "内部売上高",
    "内部営業収益", "連結",
]

# exact match 用 total
TOTAL_EXACT = {"売上高", "営業利益", "経常利益", "純利益", "利益", "損失",
               "収益", "営業収益", "調整額", "計", "合計", "全社"}

# --- PL 勘定科目 (table_scoring.py と同じ辞書を共有) ---
PL_ACCOUNT_LABELS = [
    "売上原価", "売上総利益", "販売費及び一般管理費", "販売費・一般管理費",
    "営業外収益", "営業外費用", "経常利益", "特別利益", "特別損失",
    "税金等調整前", "法人税等", "法人税、住民税及び事業税",
    "当期純利益", "四半期純利益", "親会社株主に帰属する",
    "受取利息", "支払利息", "受取配当金",
    "減価償却費", "人件費", "賃借料", "租税公課",
    "貸倒引当金繰入", "トレーディング損益", "金融収益", "金融費用",
    "純営業収益", "営業総利益", "資金調達費用",
    "受入手数料", "委託手数料", "固定資産売却益", "固定資産除却損",
    "投資有価証券売却", "為替差損", "減損損失",
]

# --- Valid Segment (事業名パターン) ---
_SEGMENT_PRIMARY_RE = re.compile(
    r'.*(事業|部門|セグメント|ビジネス|カンパニー)$'
)
_SEGMENT_SECONDARY_KW = [
    "国内", "海外", "日本", "北米", "欧州", "アジア", "中国",
    "不動産", "物流", "エネルギー", "生活関連", "環境",
    "金融", "情報", "通信", "建設", "機械", "化学",
]

# --- Garbage fragment シグナル ---
_GARBAGE_ENDS = ("、", "。", "は", "が", "に", "を", "で", "と", "の", "も",
                 "より", "から", "へ", "「", "する", "した", "って")
_GARBAGE_MAX_LEN = 5
_PUNCT_CHARS = set("、。「」（）()【】")


# ============================================================
# 行分類結果
# ============================================================

@dataclass
class RowClassResult:
    """行分類の結果"""
    class_name: str  # e.g. "valid_segment_like", "narrative_like"
    matched_reasons: list[str] = field(default_factory=list)
    label_normalized: str = ""


# ============================================================
# 正規化
# ============================================================

def normalize_label(text: str) -> str:
    """行ラベルの正規化: 先頭の非数値部分を抽出し、空白トリム"""
    stripped = text.strip()
    if not stripped:
        return ""
    # 先頭の非数値部分
    m = re.match(r'^([^\d△▲\-－]*)', stripped)
    if m:
        return m.group(1).strip()
    return stripped


# ============================================================
# 個別判定関数
# ============================================================

def is_garbage_fragment_like(label: str) -> tuple[bool, list[str]]:
    """意味をなさない断片かを判定"""
    reasons = []
    if not label:
        return True, ["empty"]

    # 短すぎる (1~4文字で事業名パターンでない)
    if len(label) <= _GARBAGE_MAX_LEN:
        if not _SEGMENT_PRIMARY_RE.match(label) and label not in ("その他", "合計", "計", "全社", "調整額"):
            reasons.append(f"short_{len(label)}chars")

    # 末尾が助詞・句読点で終わる自然文断片
    for end in _GARBAGE_ENDS:
        if label.endswith(end):
            reasons.append(f"ends_with_{end}")
            break

    # 開き/閉じ括弧の片側のみ
    if label.startswith("（") and "）" not in label:
        reasons.append("open_bracket_only")
    if label.startswith("(") and ")" not in label:
        reasons.append("open_paren_only")

    # 句読点が含まれるのに事業名パターンでない
    punct_count = sum(1 for c in label if c in _PUNCT_CHARS)
    if punct_count >= 2 and not _SEGMENT_PRIMARY_RE.match(label):
        reasons.append(f"punct_{punct_count}")

    # 引用符を含む断片
    if "「" in label and "」" not in label:
        reasons.append("unclosed_quote")
    if "」" in label and "「" not in label:
        reasons.append("unopened_quote")

    return len(reasons) >= 1, reasons


def is_pl_account_like(label: str) -> tuple[bool, list[str]]:
    """PL 勘定科目かを判定"""
    reasons = []
    for kw in PL_ACCOUNT_LABELS:
        if kw in label:
            reasons.append(f"pl:{kw}")
            return True, reasons
    return False, reasons


def is_bs_cf_like(label: str) -> tuple[bool, list[str]]:
    """BS/CF 項目かを判定"""
    reasons = []
    for kw in BS_CF_KW:
        if kw in label:
            reasons.append(f"bs_cf:{kw}")
            return True, reasons
    # 短い BS/CF キーワード: 行全体の文脈チェック
    for kw in BS_CF_SHORT_KW:
        if kw in label:
            # 「不動産事業」のような事業名に「資産」が含まれるケースを除外
            if _SEGMENT_PRIMARY_RE.match(label):
                continue
            # 「○○事業」の中に含まれるケースも除外
            if any(suffix in label for suffix in ("事業", "部門", "セグメント")):
                continue
            reasons.append(f"bs_cf_short:{kw}")
            return True, reasons
    return False, reasons


def _has_sentence_signals(label: str) -> list[str]:
    """自然文シグナルを検出する。"""
    signals = []
    if "。" in label:
        signals.append("period")
    if "、" in label:
        signals.append("comma")
    if "「" in label or "」" in label:
        signals.append("quote")
    # 助詞が 2つ以上
    particle_count = sum(1 for p in _SENTENCE_PARTICLES if p in label)
    if particle_count >= 2:
        signals.append(f"particles_{particle_count}")
    # 文末が自然文っぽい
    for end in _SENTENCE_ENDINGS:
        if label.endswith(end):
            signals.append(f"ending_{end}")
            break
    # 長い (15文字以上で事業/部門末尾でない)
    if len(label) >= 15 and not _SEGMENT_PRIMARY_RE.match(label):
        signals.append("long_label")
    return signals


def is_narrative_like(label: str) -> tuple[bool, list[str]]:
    """本文ナラティブかを判定。

    - strong KW がある → narrative_like
    - weak KW のみ → 自然文シグナルがなければ non-narrative
    - 句読点が多い自然文 → narrative_like
    """
    reasons = []
    strong_hits = []
    weak_hits = []

    for kw in NARRATIVE_STRONG_KW:
        if kw in label:
            strong_hits.append(kw)
    for kw in NARRATIVE_WEAK_KW:
        if kw in label:
            weak_hits.append(kw)

    # strong KW がある → narrative 確定
    if strong_hits:
        reasons.extend(f"strong:{kw}" for kw in strong_hits)
        return True, reasons

    # 自然文シグナル
    sentence_signals = _has_sentence_signals(label)

    # weak KW + 自然文シグナル → narrative
    if weak_hits and sentence_signals:
        reasons.extend(f"weak:{kw}" for kw in weak_hits)
        reasons.extend(f"signal:{s}" for s in sentence_signals)
        return True, reasons

    # 句読点が多い自然文 (KW なしでも)
    if "。" in label:
        reasons.append("period_sentence")
    if label.count("、") >= 2:
        reasons.append("many_commas")
    # 長すぎる (30文字以上の自然文)
    if len(label) >= 30 and ("、" in label or "。" in label):
        reasons.append("long_natural_text")
    elif len(label) >= 40:
        reasons.append("very_long_label")

    return len(reasons) >= 1, reasons


def is_detail_breakdown_like(label: str) -> tuple[bool, list[str]]:
    """内訳・収入明細かを判定"""
    reasons = []
    for kw in DETAIL_BREAKDOWN_KW:
        if kw in label:
            reasons.append(f"detail:{kw}")
            return True, reasons
    # 括弧で囲まれた項目
    if _DETAIL_BRACKET_RE.match(label):
        reasons.append("bracketed_detail")
        return True, reasons
    # 「（○○収入）」パターン
    if re.match(r'^[（(].*収入[）)]', label):
        reasons.append("bracketed_revenue")
        return True, reasons
    return False, reasons


def is_total_or_metric_like(label: str) -> tuple[bool, list[str]]:
    """合計・小計・指標行かを判定"""
    reasons = []
    stripped = label.strip()
    if stripped in TOTAL_EXACT:
        reasons.append(f"total_exact:{stripped}")
        return True, reasons
    for kw in TOTAL_OR_METRIC_KW:
        if kw in label:
            reasons.append(f"metric:{kw}")
            return True, reasons
    # 末尾「計」
    if stripped.endswith("計") and len(stripped) >= 2:
        reasons.append("ends_with_計")
        return True, reasons
    return False, reasons


def is_valid_segment_like(label: str) -> tuple[bool, list[str]]:
    """有効なセグメント名かを判定"""
    reasons = []
    stripped = label.strip()
    # 主軸: .*事業/.*部門/.*セグメント
    if _SEGMENT_PRIMARY_RE.match(stripped):
        reasons.append(f"primary_pattern:{stripped}")
        return True, reasons
    # 既知の特殊セグメント名
    if stripped in ("その他",):
        reasons.append("known_segment:その他")
        return True, reasons
    # 補助: 地域名を含む
    for kw in _SEGMENT_SECONDARY_KW:
        if kw in stripped and len(stripped) <= 20:
            reasons.append(f"secondary:{kw}")
            return True, reasons
    return False, reasons


# ============================================================
# 統合分類関数
# ============================================================

def classify_row_label(label_raw: str) -> RowClassResult:
    """
    行ラベルを分類する。

    優先順位:
    1. narrative_like   (最優先: 「事業につきましては」等を確実に弾く)
    2. pl_account_like  (勘定科目)
    3. bs_cf_like       (BS/CF 項目)
    4. detail_breakdown_like (内訳)
    5. total_or_metric_like  (合計/指標)
    6. valid_segment_like    (有効なセグメント名)
    7. garbage_fragment_like (残余: 他に該当しない断片)
    8. unknown
    """
    label = normalize_label(label_raw)

    if not label:
        return RowClassResult("garbage_fragment_like", ["empty"], label)

    # 1. narrative (最優先 — 事業名を含んでいても narrative 判定が出たら弾く)
    is_narr, narr_reasons = is_narrative_like(label)
    if is_narr:
        return RowClassResult("narrative_like", narr_reasons, label)

    # 2. PL 勘定科目
    is_pl, pl_reasons = is_pl_account_like(label)
    if is_pl:
        return RowClassResult("pl_account_like", pl_reasons, label)

    # 3. BS/CF
    is_bscf, bscf_reasons = is_bs_cf_like(label)
    if is_bscf:
        return RowClassResult("bs_cf_like", bscf_reasons, label)

    # 4. detail breakdown
    is_detail, detail_reasons = is_detail_breakdown_like(label)
    if is_detail:
        return RowClassResult("detail_breakdown_like", detail_reasons, label)

    # 5. total / metric
    is_total, total_reasons = is_total_or_metric_like(label)
    if is_total:
        return RowClassResult("total_or_metric_like", total_reasons, label)

    # 6. valid segment
    is_valid, valid_reasons = is_valid_segment_like(label)
    if is_valid:
        return RowClassResult("valid_segment_like", valid_reasons, label)

    # 7. garbage (他のどの分類にも該当しなかった場合のみ)
    is_garb, garb_reasons = is_garbage_fragment_like(label)
    if is_garb:
        return RowClassResult("garbage_fragment_like", garb_reasons, label)

    # 8. unknown
    return RowClassResult("unknown", [], label)


# ============================================================
# Candidate-level Guard
# ============================================================

@dataclass
class CandidateGuardResult:
    """候補テーブル全体の品質判定結果"""
    accepted: bool = False
    reject_reason: str = ""
    dropped_by: str = ""  # Phase 2: 何で落とされたか (reject_reason より詳細)

    # 行分類集計
    total_rows: int = 0
    valid_segment_like: int = 0
    narrative_like: int = 0
    bs_cf_like: int = 0
    pl_account_like: int = 0
    detail_breakdown_like: int = 0
    total_or_metric_like: int = 0
    garbage_fragment_like: int = 0
    unknown: int = 0
    non_total_segment_rows: int = 0  # Phase 2: total-like を除外したセグメント行数

    # 表シグナル (外部から渡される or 内部計算)
    numeric_density: float = 0.0
    repeated_numeric_rows: int = 0
    header_keyword_hits: int = 0
    segment_name_like_rows: int = 0
    narrative_penalty: float = 0.0
    bs_cf_penalty: float = 0.0
    rescued_by: str = ""
    candidate_score: float = 0.0

    # デバッグ用
    row_classifications: list[RowClassResult] = field(default_factory=list)
    top_samples: list[str] = field(default_factory=list)


# ============================================================
# 候補テーブルの表シグナル算出
# ============================================================

_NUMERIC_TOKEN_RE = re.compile(
    r'[△▲]?\s*[\d,]+(?:\.\d+)?'
)

# Phase 2: total-like ラベル (non_total_segment_rows 算出用)
_TOTAL_LIKE_LABELS = {
    "合計", "計", "小計", "全社", "調整額", "消去", "連結",
    "売上高", "営業利益", "経常利益", "純利益", "利益", "損失",
    "収益", "営業収益", "純営業収益",
    "内部売上高", "内部営業収益",
    "セグメント間内部売上高", "セグメント間内部営業収益",
}
_TOTAL_LIKE_SUFFIXES = ("計", "合計", "小計")
_TOTAL_LIKE_PREFIXES = ("消去又は全社", "消去・全社", "調整額及び全社")


def compute_candidate_table_signals(
    candidate_lines: list[str],
) -> tuple[float, int, int]:
    """
    候補テーブル行テキストから numeric_density, repeated_numeric_rows,
    distinct_numeric_positions を算出する。

    Returns:
        (numeric_density, repeated_numeric_rows, distinct_numeric_positions)
    """
    total_tokens = 0
    numeric_tokens = 0
    repeated_numeric_rows = 0
    # Phase 3: 数値トークンが出現する列位置を集計
    _num_pos_counter: dict[int, int] = {}  # pos -> count

    for line in candidate_lines:
        stripped = line.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        total_tokens += max(len(tokens), 1)
        num_positions = [m.start() for m in _NUMERIC_TOKEN_RE.finditer(stripped)]
        num_in_line = len(num_positions)
        numeric_tokens += num_in_line
        if num_in_line >= 2:
            repeated_numeric_rows += 1
        # 各数値位置を粗いバケット (10文字幅) で丸めて位置カウント
        for pos in num_positions:
            bucket = pos // 10
            _num_pos_counter[bucket] = _num_pos_counter.get(bucket, 0) + 1

    numeric_density = numeric_tokens / max(total_tokens, 1)
    # distinct_numeric_positions: 2行以上で出現する位置バケットの数
    distinct_numeric_positions = sum(
        1 for cnt in _num_pos_counter.values() if cnt >= 2
    )
    return numeric_density, repeated_numeric_rows, distinct_numeric_positions


def evaluate_candidate_guard(
    row_labels: list[str],
    *,
    candidate_lines: list[str] | None = None,
    header_keyword_hits: int = 0,
    anchor_hits: int = 0,
    segment_name_like_rows: int = 0,
    min_valid_segments: int = 2,
    narrative_ratio_threshold: float = 0.40,
    bs_cf_ratio_threshold: float = 0.15,
    debug: bool = False,
) -> CandidateGuardResult:
    """
    候補テーブルの行ラベル群を分類・集計し、候補を accept/reject する。

    表シグナル (header_keyword_hits, numeric_density, repeated_numeric_rows) が
    十分強い場合は narrative_guard / bs_cf_guard の即死を回避し、候補として残す。

    Args:
        row_labels: 行ラベル群
        candidate_lines: 候補テーブルの raw テキスト行 (numeric_density 算出用)
        header_keyword_hits: 売上高/営業利益 等のヘッダーキーワードヒット数 (外部算出)
        anchor_hits: セグメントアンカーキーワードヒット数 (外部算出)
        segment_name_like_rows: セグメント名らしい行数 (外部算出)
    """
    result = CandidateGuardResult()

    # --- 表シグナル算出 ---
    numeric_density = 0.0
    repeated_numeric_rows = 0
    distinct_numeric_positions = 0
    if candidate_lines:
        numeric_density, repeated_numeric_rows, distinct_numeric_positions = (
            compute_candidate_table_signals(candidate_lines)
        )
    result.numeric_density = round(numeric_density, 4)
    result.repeated_numeric_rows = repeated_numeric_rows

    # --- header_keyword_hits 内部補完 ---
    # 外部算出値が 0 のとき、candidate_lines を直接スキャンして補完する。
    # 正規化・拡張キーワード・前後近傍行の3点を強化。
    if header_keyword_hits == 0 and candidate_lines:
        _SALES_KW = [
            "売上高", "外部顧客への売上高", "売上収益", "営業収益",
            "net sales", "revenues",
        ]
        _PROFIT_KW = [
            "セグメント利益", "セグメント損失", "営業利益",
            "operating profit", "operating income",
        ]

        def _norm_hdr(s: str) -> str:
            """文字間空白除去 + 全角スペース統一"""
            s = s.replace("\u3000", " ").replace("\t", " ")
            s = re.sub(r'(?<=[\u3040-\u9fff\uff00-\uffef])\s+(?=[\u3040-\u9fff\uff00-\uffef])', "", s)
            s = re.sub(r'\s+', " ", s)
            return s.lower()

        _lines_normed = [_norm_hdr(ln) for ln in candidate_lines]
        _found_sales = False
        _found_profit = False
        for _i, _ln in enumerate(_lines_normed):
            # 前行・当該行・次行の近傍ウィンドウ
            _window = _ln
            if _i > 0:
                _window = _lines_normed[_i - 1] + " " + _window
            if _i < len(_lines_normed) - 1:
                _window = _window + " " + _lines_normed[_i + 1]
            if not _found_sales and any(kw in _window for kw in _SALES_KW):
                _found_sales = True
            if not _found_profit and any(kw in _window for kw in _PROFIT_KW):
                _found_profit = True
            if _found_sales and _found_profit:
                break
        header_keyword_hits = int(_found_sales) + int(_found_profit)

    result.header_keyword_hits = header_keyword_hits
    seg_header_flag = (header_keyword_hits >= 1)
    result.segment_name_like_rows = segment_name_like_rows

    # 表シグナル強度判定
    _has_table_signal = (
        header_keyword_hits >= 1
        and numeric_density >= 0.10
        and repeated_numeric_rows >= 2
    )
    _has_strong_table_signal = (
        header_keyword_hits >= 2
        and repeated_numeric_rows >= 3
    )

    # 行分類
    classifications: list[RowClassResult] = []
    counts: dict[str, int] = {
        "valid_segment_like": 0,
        "narrative_like": 0,
        "bs_cf_like": 0,
        "pl_account_like": 0,
        "detail_breakdown_like": 0,
        "total_or_metric_like": 0,
        "garbage_fragment_like": 0,
        "unknown": 0,
    }

    for label_raw in row_labels:
        cls = classify_row_label(label_raw)
        classifications.append(cls)
        if cls.class_name in counts:
            counts[cls.class_name] += 1

    total = len(row_labels)
    result.total_rows = total
    result.valid_segment_like = counts["valid_segment_like"]
    result.narrative_like = counts["narrative_like"]
    result.bs_cf_like = counts["bs_cf_like"]
    result.pl_account_like = counts["pl_account_like"]
    result.detail_breakdown_like = counts["detail_breakdown_like"]
    result.total_or_metric_like = counts["total_or_metric_like"]
    result.garbage_fragment_like = counts["garbage_fragment_like"]
    result.unknown = counts["unknown"]
    result.row_classifications = classifications

    # Phase 2: non_total_segment_rows 算出
    # total-like ラベル (合計/全社/調整額/消去等) を除外した行数
    _non_total = 0
    for cls in classifications:
        if cls.class_name == "total_or_metric_like":
            continue
        if cls.class_name in ("narrative_like", "bs_cf_like", "pl_account_like",
                              "garbage_fragment_like"):
            continue
        _lab = cls.label_normalized.strip()
        if _lab in _TOTAL_LIKE_LABELS:
            continue
        if any(_lab.endswith(s) for s in _TOTAL_LIKE_SUFFIXES) and len(_lab) >= 2:
            continue
        if any(_lab.startswith(p) for p in _TOTAL_LIKE_PREFIXES):
            continue
        if _lab:  # 空ラベルは除外
            _non_total += 1
    result.non_total_segment_rows = _non_total

    # デバッグ用サンプル
    result.top_samples = [
        f"{cls.label_normalized}:{cls.class_name}"
        for cls in classifications[:5]
    ]

    if total == 0:
        result.reject_reason = "no_rows"
        return result

    v = result.valid_segment_like
    n = result.narrative_like
    g = result.garbage_fragment_like
    b = result.bs_cf_like
    d = result.detail_breakdown_like
    t = result.total_or_metric_like
    p = result.pl_account_like

    # --- reject checks (表シグナル救済付き) ---

    # 修正4: valid_segment が 1件以上あれば早期通過
    if v >= 1:
        result.accepted = True
        return result

    # 1. narrative 汚染
    # narrative 単独で 3 行以上 かつ valid_segment より多い場合 reject
    # ただし表シグナルが十分強ければ reject しない
    _narrative_triggered = False
    if n >= 3 and n > v:
        _narrative_triggered = True
    if total > 0 and n / total >= narrative_ratio_threshold:
        _narrative_triggered = True

    if _narrative_triggered:
        result.narrative_penalty = float(n)
        if _has_strong_table_signal:
            # 強い表シグナル → narrative_guard を回避
            result.rescued_by = "strong_table_signal:narrative"
            logger.debug(
                f"[candidate_guard] narrative_guard RESCUED: "
                f"hdr={header_keyword_hits} numdens={numeric_density:.3f} "
                f"repnum={repeated_numeric_rows} n={n} v={v}"
            )
        elif _has_table_signal:
            # 表シグナルあり → narrative_guard を回避
            result.rescued_by = "table_signal:narrative"
            logger.debug(
                f"[candidate_guard] narrative_guard RESCUED (weak): "
                f"hdr={header_keyword_hits} numdens={numeric_density:.3f} "
                f"repnum={repeated_numeric_rows} n={n} v={v}"
            )
        else:
            # 修正3: valid_segment が 0件のときだけ reject
            if v == 0:
                result.reject_reason = "narrative_guard"
                return result

    # 2. BS/CF 汚染 (表シグナル救済付き)
    _bscf_triggered = False
    # 修正2: b>=3 かつ valid_segment=0 のときだけ発火
    if b >= 3 and v < 1:
        _bscf_b_limit = 5 if result.header_keyword_hits >= 1 else 3
        _bscf_light_exempt = (
            b <= _bscf_b_limit
            and (
                segment_name_like_rows >= 3
                or result.header_keyword_hits >= 1
            )
        )
        if not _bscf_light_exempt:
            _bscf_triggered = True
    if total > 0 and b / total >= bs_cf_ratio_threshold and v == 0:
        # Phase BでBS判定済みならスキップ
        if not getattr(result, "bs_table_detected", False):
            _bscf_triggered = True

    if _bscf_triggered:
        result.bs_cf_penalty = float(b)
        if _has_table_signal:
            # 表シグナルあり → bs_cf_guard を回避
            if not result.rescued_by:
                result.rescued_by = "table_signal:bs_cf"
            else:
                result.rescued_by += "+table_signal:bs_cf"
            logger.debug(
                f"[candidate_guard] bs_cf_guard RESCUED: "
                f"hdr={header_keyword_hits} numdens={numeric_density:.3f} "
                f"repnum={repeated_numeric_rows} b={b} v={v}"
            )
        else:
            # DISABLED: allow PDF segment tables to pass
            # result.reject_reason = "bs_cf_guard"
            # return result
            pass

    # 3. PL 汚染 (rescue 対象外 — PL は表構造を持つため表シグナルでは区別できない)
    if p >= 3:
        result.reject_reason = "pl_guard"
        return result

    # 4. detail breakdown 混在 (Phase 2: 表シグナル救済付き)
    _detail_triggered = False
    if d > v:
        _detail_triggered = True
    if total > 0 and (d + t) / total > 0.5 and v < min_valid_segments:
        _detail_triggered = True

    if _detail_triggered:
        # Phase 2: header_keyword_hits >= 1 + repeated_numeric_rows >= 3
        #          + segment_name_like_rows >= 2 + numeric_density >= 0.10 なら救済
        _detail_rescue = (
            header_keyword_hits >= 1
            and repeated_numeric_rows >= 3
            and segment_name_like_rows >= 2
            and numeric_density >= 0.10
        )
        # 強救済条件: header >= 2 + repeated >= 4
        _detail_strong_rescue = (
            header_keyword_hits >= 2
            and repeated_numeric_rows >= 4
        )
        if _detail_rescue or _detail_strong_rescue:
            _rescue_tag = "detail_breakdown_table_rescue"
            if _detail_strong_rescue:
                _rescue_tag = "detail_breakdown_strong_rescue"
            if not result.rescued_by:
                result.rescued_by = _rescue_tag
            else:
                result.rescued_by += f"+{_rescue_tag}"
            logger.debug(
                f"[candidate_guard] detail_breakdown_guard RESCUED: "
                f"hdr={header_keyword_hits} numdens={numeric_density:.3f} "
                f"repnum={repeated_numeric_rows} segrows={segment_name_like_rows} "
                f"d={d} v={v}"
            )
        else:
            if d > v:
                result.reject_reason = "detail_breakdown_guard"
                result.dropped_by = f"detail_breakdown_guard:d={d}>v={v}"
            else:
                result.reject_reason = "invalid_structure"
                result.dropped_by = f"invalid_structure:d+t={d+t}/{total}>0.5"
            return result

    # 5. valid segment 不足
    # anchor=0 でも header+numeric 信号が強ければ reject しない
    if v < min_valid_segments:
        if _has_table_signal and segment_name_like_rows >= 2:
            # 表シグナル + セグメント名行あり → reject しない
            if not result.rescued_by:
                result.rescued_by = "header_numeric:no_valid_segment"
            else:
                result.rescued_by += "+header_numeric:no_valid_segment"
            logger.debug(
                f"[candidate_guard] no_valid_segment_rows RESCUED: "
                f"hdr={header_keyword_hits} numdens={numeric_density:.3f} "
                f"repnum={repeated_numeric_rows} segrows={segment_name_like_rows} v={v}"
            )
        else:
            # ================================================================
            # Phase 3: Weak Table Rescue (check 5)
            # ================================================================
            # anchor=0, header=0 でも行構造 + 数値列構造が十分表らしければ rescue
            _weak_rescue_5 = False
            _weak_rescue_5_tag = ""
            _has_col_structure = distinct_numeric_positions >= 2

            # A: segrows>=3 + repnum>=2 + numdens>=0.08 + 列構造あり
            if (segment_name_like_rows >= 3 and repeated_numeric_rows >= 2
                    and numeric_density >= 0.08 and _has_col_structure):
                _weak_rescue_5 = True
                _weak_rescue_5_tag = "weak_table_rescue:A"
            # B: segrows>=2 + repnum>=3 + 列構造あり
            elif (segment_name_like_rows >= 2 and repeated_numeric_rows >= 3
                    and _has_col_structure):
                _weak_rescue_5 = True
                _weak_rescue_5_tag = "weak_table_rescue:B"

            if _weak_rescue_5:
                if not result.rescued_by:
                    result.rescued_by = _weak_rescue_5_tag
                else:
                    result.rescued_by += f"+{_weak_rescue_5_tag}"
                logger.debug(
                    f"[WEAK] segrows={segment_name_like_rows} "
                    f"repnum={repeated_numeric_rows} "
                    f"numdens={numeric_density:.3f} "
                    f"distinct_numpos={distinct_numeric_positions} "
                    f"rescued_by={_weak_rescue_5_tag}"
                )
            else:
                # disabled: downstream rescue / later phases に委ねる
                # result.reject_reason = "no_valid_segment_rows"
                # return result
                pass

    # 6. total/metric 優勢 (Phase 2: 表シグナル + non_total_segment_rows で救済)
    if t >= v and v <= 1:
        _nts = result.non_total_segment_rows
        _total_rescue = False
        if _has_strong_table_signal and _nts >= 2:
            _total_rescue = True
        elif _has_table_signal and _nts >= 2:
            _total_rescue = True

        if _total_rescue:
            _rescue_tag = "table_signal:total_metric_dominant"
            if not result.rescued_by:
                result.rescued_by = _rescue_tag
            else:
                result.rescued_by += f"+{_rescue_tag}"
            logger.debug(
                f"[candidate_guard] total_metric_dominant RESCUED: "
                f"hdr={header_keyword_hits} numdens={numeric_density:.3f} "
                f"repnum={repeated_numeric_rows} non_total={_nts} t={t} v={v}"
            )
        else:
            # ================================================================
            # Phase 3: Weak Table Rescue (check 6)
            # ================================================================
            _weak_rescue_6 = False
            _weak_rescue_6_tag = ""
            _has_col_structure = distinct_numeric_positions >= 2

            # A: segrows>=3 + repnum>=2 + numdens>=0.08 + 列構造あり
            if (segment_name_like_rows >= 3 and repeated_numeric_rows >= 2
                    and numeric_density >= 0.08 and _has_col_structure):
                _weak_rescue_6 = True
                _weak_rescue_6_tag = "weak_table_rescue:A"
            # B: segrows>=2 + repnum>=3 + 列構造あり
            elif (segment_name_like_rows >= 2 and repeated_numeric_rows >= 3
                    and _has_col_structure):
                _weak_rescue_6 = True
                _weak_rescue_6_tag = "weak_table_rescue:B"

            if _weak_rescue_6:
                if not result.rescued_by:
                    result.rescued_by = _weak_rescue_6_tag
                else:
                    result.rescued_by += f"+{_weak_rescue_6_tag}"
                logger.debug(
                    f"[WEAK] segrows={segment_name_like_rows} "
                    f"repnum={repeated_numeric_rows} "
                    f"numdens={numeric_density:.3f} "
                    f"distinct_numpos={distinct_numeric_positions} "
                    f"rescued_by={_weak_rescue_6_tag}"
                )
            else:
                # DISABLED: allow segment tables with strong total rows
                # result.reject_reason = "total_metric_dominant"
                # result.dropped_by = f"total_metric_dominant:t={t}>=v={v},nts={_nts}"
                # return result
                pass

    # --- 候補スコア算出 (デバッグ/ログ用) ---
    score = 0.0
    score += min(anchor_hits, 3) * 2
    score += min(header_keyword_hits, 3) * 3
    if numeric_density >= 0.20:
        score += 3
    elif numeric_density >= 0.10:
        score += 1
    if repeated_numeric_rows >= 3:
        score += 3
    elif repeated_numeric_rows >= 2:
        score += 1
    if segment_name_like_rows >= 2:
        score += 2
    score -= result.narrative_penalty * 0.5
    score -= result.bs_cf_penalty * 0.5
    result.candidate_score = round(score, 1)

    # --- accept ---
    result.accepted = True
    return result


def log_candidate_guard(
    guard_result: CandidateGuardResult,
    *,
    page: int = 0,
    table_index: int = 0,
) -> None:
    """候補テーブルガード結果のデバッグログ出力"""
    status = "ACCEPT" if guard_result.accepted else f"REJECT:{guard_result.reject_reason}"
    logger.debug(
        f"[pdf_segment_guard] page={page} table={table_index} "
        f"rows={guard_result.total_rows} "
        f"valid={guard_result.valid_segment_like} "
        f"narrative={guard_result.narrative_like} "
        f"bs_cf={guard_result.bs_cf_like} "
        f"detail={guard_result.detail_breakdown_like} "
        f"total_metric={guard_result.total_or_metric_like} "
        f"non_total={guard_result.non_total_segment_rows} "
        f"garbage={guard_result.garbage_fragment_like} "
        f"pl={guard_result.pl_account_like} "
        f"rescued_by={guard_result.rescued_by or 'none'} "
        f"dropped_by={guard_result.dropped_by or 'none'} "
        f"result={status}"
    )
    if guard_result.top_samples:
        logger.debug(
            f"[pdf_segment_guard] samples={guard_result.top_samples}"
        )


# ============================================================
# Reason → Review Hint マッピング
# ============================================================

_REJECT_REASON_TO_HINT: dict[str, str] = {
    "narrative_guard": "pdf_narrative_block_selected",
    "bs_cf_guard": "pdf_narrative_block_selected",
    "pl_guard": "pdf_pl_table_selected",
    "detail_breakdown_guard": "pdf_segment_like_but_invalid_structure",
    "invalid_structure": "pdf_segment_like_but_invalid_structure",
    "no_segment_narrative_page": "pdf_no_segment_narrative_page",
    "no_valid_segment_rows": "pdf_no_segment_table_after_guard",
    "total_metric_dominant": "pdf_no_segment_table_after_guard",
    "no_candidate_after_pl_filter": "pdf_no_segment_table_after_guard",
    "no_candidate_after_guard": "pdf_no_segment_table_after_guard",
    "toc_page_guard": "pdf_toc_page_selected",
    "table_parse_failed": "pdf_table_parse_failed",
    "extraction_failed": "pdf_extraction_failed",
    "picked_pl_table": "pdf_pl_table_selected",
}


def map_reject_reason_to_review_hint(reason: str | None) -> str:
    """reject_reason を review_hint にマッピングする一元関数。"""
    if not reason:
        return "pdf_extraction_failed"
    # "candidate_guard:xxx" 形式から reason を抽出
    if reason.startswith("candidate_guard:"):
        reason = reason.split(":", 1)[1]
    # "xxx|hint=yyy" 形式から reason を抽出
    if "|" in reason:
        reason = reason.split("|")[0]
    return _REJECT_REASON_TO_HINT.get(reason, "pdf_extraction_failed")


# Reason 優先順位 (小さいほど情報価値が高い)
_REASON_PRIORITY: dict[str, int] = {
    "pl_guard": 1,
    "narrative_guard": 2,
    "no_segment_narrative_page": 3,
    "bs_cf_guard": 3,
    "detail_breakdown_guard": 4,
    "invalid_structure": 5,
    "no_valid_segment_rows": 6,
    "total_metric_dominant": 7,
    "toc_page_guard": 8,
    "picked_pl_table": 8,
    "no_candidate_after_pl_filter": 9,
    "no_candidate_after_guard": 10,
    "no_rows": 11,
    "table_parse_failed": 20,
    "extraction_failed": 21,
}


def choose_better_reason(reason_a: str | None, reason_b: str | None) -> str | None:
    """より分析価値の高い reason を選択する。"""
    if not reason_a:
        return reason_b
    if not reason_b:
        return reason_a
    # "candidate_guard:xxx" から core を取り出す
    core_a = reason_a.split(":", 1)[-1] if ":" in reason_a else reason_a
    core_b = reason_b.split(":", 1)[-1] if ":" in reason_b else reason_b
    pri_a = _REASON_PRIORITY.get(core_a, 99)
    pri_b = _REASON_PRIORITY.get(core_b, 99)
    return reason_a if pri_a <= pri_b else reason_b


def format_guard_summary(guard: CandidateGuardResult, *, detector: str = "v2") -> str:
    """candidate guard 結果を 1-line summary にフォーマットする。"""
    parts = [
        f"candidate_reject_reason={guard.reject_reason or 'accept'}",
        f"detector={detector}",
        f"stage=candidate_guard",
        f"rows={guard.total_rows}",
        f"valid={guard.valid_segment_like}",
        f"narrative={guard.narrative_like}",
        f"bs_cf={guard.bs_cf_like}",
        f"pl={guard.pl_account_like}",
        f"detail={guard.detail_breakdown_like}",
        f"total_metric={guard.total_or_metric_like}",
        f"garbage={guard.garbage_fragment_like}",
    ]
    if guard.top_samples:
        parts.append(f"samples={guard.top_samples[:3]}")
    return "; ".join(parts)

