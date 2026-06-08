"""
セグメント名正規化 + 特殊行分類

SEGMENT_EXTRACTION_SPEC §6, §7, §6b, §6c に基づく。
"""
from __future__ import annotations

import re
import unicodedata


# ============================================================
# §6 セグメント名正規化
# ============================================================
# 末尾語の削除候補（ただし保守的: セグメント名の一部として意味がある場合あり）
_SUFFIX_STRIP = re.compile(r"セグメント$")

# 連続空白
_MULTI_SPACE = re.compile(r"\s+")


def normalize_segment_name(name: str) -> str:
    """セグメント名を正規化。

    ルール:
    - NFKC 正規化 (全角英数→半角, etc)
    - 前後空白除去
    - 改行除去
    - 連続空白を1つのスペースに圧縮
    - 不要記号除去 (※, *, 注 etc)
    - 「セグメント」末尾語を削除 (例: "電子事業セグメント" → "電子事業")
    """
    if not name:
        return ""
    # NFKC
    s = unicodedata.normalize("NFKC", name)
    # 改行→スペース
    s = s.replace("\n", " ").replace("\r", " ")
    # 不要記号除去
    s = re.sub(r"[※＊*（）()]", "", s)
    s = re.sub(r"^[\s　]+|[\s　]+$", "", s)
    # 連続空白圧縮
    s = _MULTI_SPACE.sub(" ", s)
    # 末尾「セグメント」除去
    s = _SUFFIX_STRIP.sub("", s)
    s = s.strip()
    return s


# ============================================================
# §6b segment_key 生成 — 統合判定用キー
# ============================================================
# Step A: 除去する記号群（中黒・ハイフン・括弧・スラッシュ・& 等）
_KEY_SYMBOLS = re.compile(
    r"[・\-_\u30FB\u2010-\u2015\u2212&＆/／\\\(\)（）\[\]【】「」『』"
    r"\u3014\u3015\u2018\u2019\u201C\u201D<>｛｝{}｢｣<>★☆●○◆◇■□▲▼△▽→←↑↓♦•·，,。、．!！?？]+"
)

# Step B: 汎用語（除去対象）— 日英両方
_GENERIC_JA = re.compile(
    r"事業|分野|部門|セグメント|カンパニー|グループ|関連|ビジネス|ホールディングス"
)
_GENERIC_EN = re.compile(
    r"\b(?:sector|business|division|solutions?|services?|segment|group|"
    r"holdings?|company|companies|operations?|unit|and)\b",
    re.IGNORECASE,
)

# Step C: Semantic key マッピング
# (検索パターン, key) — 先頭から順に照合し最初にマッチしたものを使用
_SEMANTIC_RULES: list[tuple[re.Pattern, str]] = [
    # エンタテインメント（digital より先に判定）
    (re.compile(r"entertainment|エンタテインメント|entertain", re.IGNORECASE), "entertainment"),
    # モビリティ / 自動車
    (re.compile(
        r"mobility|モビリティ|自動車|automotive|vehicle|telematics|テレマティクス",
        re.IGNORECASE,
    ), "mobility"),
    # 食品
    (re.compile(r"food|食品|食料|食肉|飲料|beverage|grocery", re.IGNORECASE), "food"),
    # 化学
    (re.compile(r"chemical|化学", re.IGNORECASE), "chemicals"),
    # 鉄鋼
    (re.compile(r"steel|鉄鋼|iron|製鉄", re.IGNORECASE), "steel"),
    # 金属 / 資源 / ミネラル
    (re.compile(r"metal|minerals?|resources?|金属|資源|非鉄|鉱業", re.IGNORECASE), "metals_minerals"),
    # 機械 / インフラ / プラント
    (re.compile(r"machin|infrastructure|インフラ|plant|プラント|工業|機械", re.IGNORECASE), "machinery_infrastructure"),
    # 情報 / デジタル / ICT / メディア
    (re.compile(r"ict|digital|デジタル|media|メディア|情報|テクノロジ|technology|telecom", re.IGNORECASE), "digital_media"),
    # 金融
    (re.compile(r"financ|金融|banking|保険|insurance|証券", re.IGNORECASE), "finance"),
    # 生活 / 不動産 / ライフスタイル
    (re.compile(r"lifestyle|ライフスタイル|realty|realestate|不動産|住生活|住宅|生活", re.IGNORECASE), "lifestyle_realty"),
    # エネルギー
    (re.compile(r"energy|エネルギー|transformation|電力|ガス", re.IGNORECASE), "energy"),
    # 物流 / サプライチェーン
    (re.compile(r"logistics?|supplychain|サプライチェーン|物流|流通", re.IGNORECASE), "logistics"),
    # 採用 / リクルーティング
    (re.compile(r"recruit|リクルーティング|採用", re.IGNORECASE), "recruiting"),
    # 人材 / HR
    (re.compile(r"humanresource|staffing|人材", re.IGNORECASE), "human_resources"),
    # その他（完全一致）
    (re.compile(r"^(?:other|その他)$", re.IGNORECASE), "other"),
]


def normalize_segment_key(name: str) -> str:
    """セグメント名から統合判定用の canonical segment key を生成する。

    処理ステップ:
      1. None / 空文字 → 空文字返却
      2. NFKC 正規化（全角英数を半角化）
      3. 空白・全角空白・タブ・改行をすべて除去（スペース差を吸収）
      4. 中黒・ハイフン・記号を除去
      5. 汎用語（事業/部門/division 等）を除去
      6. semantic key への集約（mobility / food / steel 等）
      7. マッチしない場合は lower 圧縮済み文字列を返す

    注意:
      - 表示名 segment_name は変更しない。このキーは統合判定専用。
      - 大文字小文字は最終的に lower に統一。

    例:
      "Mobility And Telematics Sector"    → "mobility"
      "モビリティ&テレマティクスサービス分野"   → "mobility"
      "Steel"                             → "steel"
      "鉄鋼"                              → "steel"
      "エンタテインメントソリューションズ分野" → "entertainment"
      "Supply Chain"                      → "logistics"
      "サプライチェーン"                    → "logistics"
    """
    if not name:
        return ""

    # Step 1: NFKC（全角英数→半角）
    s = unicodedata.normalize("NFKC", name)

    # Step 2: 空白・改行・タブを完全除去（スペース差を吸収）
    s = re.sub(r"[\s\u3000\t\n\r]+", "", s)

    # Step 3: 記号を除去
    s = _KEY_SYMBOLS.sub("", s)

    # Step 4: 汎用語を除去（日本語・英語）
    s = _GENERIC_JA.sub("", s)
    s = _GENERIC_EN.sub("", s)

    # Step 4b: 再度空白除去（汎用語除去後の残留スペース）
    s = re.sub(r"\s+", "", s).strip().lower()

    if not s:
        return ""

    # Step 5: semantic key マッピング
    for pattern, key in _SEMANTIC_RULES:
        if pattern.search(s):
            return key

    # Step 6: マッチなし → 正規化済み文字列をそのまま返す
    return s


# ============================================================
# §6c 英語セグメント名 → 日本語候補展開 + マッチング
# ============================================================

# 英語単語 → 日本語候補の辞書
# キー: 英小文字単語, 値: 日本語候補リスト（包含判定に使う）
EN_TO_JP_DICT: dict[str, list[str]] = {
    "mobility":       ["モビリティ", "自動車", "車両"],
    "automotive":     ["自動車"],
    "vehicle":        ["車両", "自動車"],
    "telematics":     ["テレマティクス", "通信", "車載通信"],
    "entertainment":  ["エンタテインメント", "娯楽"],
    "solutions":      ["ソリューション", "サービス"],
    "safety":         ["セーフティ", "安全"],
    "security":       ["セキュリティ"],
    "steel":          ["鉄鋼"],
    "chemical":       ["化学"],
    "chemicals":      ["化学"],
    "energy":         ["エネルギー"],
    "media":          ["メディア"],
    "digital":        ["デジタル"],
    "food":           ["食品", "食料"],
    "logistics":      ["物流"],
    "supply":         ["供給", "サプライ"],
    "chain":          ["チェーン"],
    "other":          ["その他"],
    # 追加語彙
    "infrastructure": ["インフラ"],
    "real":           ["不動産"],
    "estate":         ["不動産"],
    "finance":        ["金融"],
    "financial":      ["金融"],
    "recruiting":     ["採用", "リクルーティング"],
    "recruitment":    ["採用"],
    "human":          ["人材"],
    "resource":       ["資源"],
    "resources":      ["資源"],
    "mineral":        ["ミネラル", "資源"],
    "minerals":       ["ミネラル", "資源"],
    "metal":          ["金属"],
    "metals":         ["金属"],
    "machinery":      ["機械"],
    "plant":          ["プラント"],
    "iron":           ["鉄鋼", "鉄"],
    "ict":            ["情報"],
    "technology":     ["テクノロジ"],
    "telecom":        ["通信"],
    "lifestyle":      ["ライフスタイル", "生活"],
    "realty":         ["不動産"],
    "beverage":       ["飲料"],
    "grocery":        ["食品"],
    "staffing":       ["人材"],
    "transformation": ["変革", "トランスフォーメーション"],
}

# ストップワード（トークン分解時に除外する英小文字語）
_EN_STOP_WORDS: frozenset[str] = frozenset([
    "and", "the", "of", "sector", "business", "division",
    "services", "service", "segment", "group", "holdings",
    "company", "companies", "operations", "unit",
])

# 英語判定: ASCII アルファベット比率 > 0.5 で英語判定
_ALPHA_RE = re.compile(r"[A-Za-z]")

# トークン分解用区切り文字（スペース / & / - / _ / /）
_TOKEN_SPLIT = re.compile(r"[\s&\-_/]+")


def is_english_dominant(name: str) -> bool:
    """英語値加假定判定。ASCII アルファベット比率 > 0.5 で True。"""
    if not name:
        return False
    alpha_count = len(_ALPHA_RE.findall(name))
    return alpha_count / max(len(name), 1) > 0.5


def tokenize_en_segment(name: str) -> list[str]:
    """英語セグメント名をトークン分解し、ストップワードを除外したリストを返す。

    手順:
      1. NFKC（全角英数→半角）
      2. lower
      3. 区切り文字（スペース/&/-/_/）で分解
      4. ストップワード除外
      5. 1文字以下は除外
    """
    s = unicodedata.normalize("NFKC", name or "").lower()
    tokens = [t for t in _TOKEN_SPLIT.split(s) if t]
    return [t for t in tokens if t not in _EN_STOP_WORDS and len(t) >= 2]


def expand_to_jp_candidates(tokens: list[str]) -> list[str]:
    """トークンリストから日本語候補文字列のフラットリストを返す。

    未知語（辞書にない単語）はスキップ（カタカナ変換は行わない）。
    """
    candidates: list[str] = []
    for token in tokens:
        jp_list = EN_TO_JP_DICT.get(token, [])
        candidates.extend(jp_list)
    return candidates


def score_jp_segment(jp_name: str, candidates: list[str]) -> int:
    """日本語セグメント名に対して候補文字列の包含判定スコアを返す。

    score = 各候補文字列が jp_name に含まれる数の和。
    """
    if not jp_name or not candidates:
        return 0
    return sum(1 for c in candidates if c in jp_name)


def resolve_segment_key_with_jp(
    en_name: str,
    jp_segment_names: list[str],
    *,
    min_score: int = 2,
) -> tuple[str, str, int]:
    """英語セグメント名を過去EDINET日本語候補でマッチングし、segment_key を返す。

    Args:
        en_name: 英語セグメント名（XBRL/TDNET由来）
        jp_segment_names: 同一 ticker の canonical_segments の過去EDINET日本語セグメント名リスト
        min_score: 最低採用スコア（default: 2）

    Returns:
        (segment_key, matched_jp_name, best_score)
        - マッチ成功: (normalize_segment_key(マッチ日本語名), マッチした名前, score)
        - マッチ失敗: (normalize_segment_key(en_name), "", score)
    """
    tokens = tokenize_en_segment(en_name)
    candidates = expand_to_jp_candidates(tokens)

    if not candidates or not jp_segment_names:
        return normalize_segment_key(en_name), "", 0

    # 各日本語セグメントをスコアリング
    scored: list[tuple[int, str]] = []
    for jp_name in jp_segment_names:
        s = score_jp_segment(jp_name, candidates)
        if s > 0:
            scored.append((s, jp_name))

    if not scored:
        return normalize_segment_key(en_name), "", 0

    # 最大スコアの日本語セグメントを採用
    scored.sort(key=lambda x: -x[0])
    best_score, best_jp = scored[0]

    if best_score >= min_score:
        return normalize_segment_key(best_jp), best_jp, best_score

    # スコア不足 → フォールバック
    return normalize_segment_key(en_name), "", best_score


# ============================================================
# §7 特殊行分類
# ============================================================
_SPECIAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("adjustment", re.compile(r"調整(額|金)|セグメント間|消去")),
    ("corporate", re.compile(r"全社|本社|共通|管理本部")),
    ("subtotal", re.compile(r"小計$|部門計$|セグメント計$|報告セグメント計$")),
    ("total", re.compile(r"^合計$|^計$|^連結$|^総計")),
    ("other", re.compile(r"^その他$|^その他の?事業$|^報告セグメント$")),
]

# 「報告セグメント」はメタラベルなのでセグメント名とは別
_META_LABELS = {"報告セグメント", "事業セグメント", "セグメント情報"}


def classify_special_row(name: str) -> str:
    """セグメント行名を分類。

    Returns:
        - 'ordinary_segment': 通常のセグメント (canonical 対象)
        - 'adjustment': 調整額
        - 'corporate': 全社/共通
        - 'total': 合計
        - 'other': その他/メタラベル
    """
    if not name:
        return "other"

    normalized = normalize_segment_name(name)
    if not normalized:
        return "other"

    # メタラベルチェック
    if normalized in _META_LABELS or name in _META_LABELS:
        return "other"

    # パターンマッチ
    for label, pattern in _SPECIAL_PATTERNS:
        if pattern.search(normalized):
            return label

    return "ordinary_segment"


def is_single_segment_company(segment_names: list[str]) -> bool:
    """単一セグメント企業かどうか判定。

    「単一セグメント」「該当事項はありません」等で判定。
    """
    if not segment_names:
        return False
    for name in segment_names:
        n = normalize_segment_name(name)
        if "単一" in n or "該当事項" in n or "なし" in n:
            return True
    return False
