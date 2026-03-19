# ============================================================
# header_analysis.py — ヘッダー解析モジュール
# ============================================================
"""
セグメント表 / 受注表などのヘッダー行を解析して、
各列のロール (売上/利益/比率 etc.) をスコアベースで判定する。

設計思想:
  - normalize_header でまず揺れを吸収
  - merge_multiline_headers で複数行/結合セルを統合
  - score_header_role で role ごとの score dict を返す
  - infer_header_roles で表全体のヘッダー構造を一括判定
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


# ============================================================
# ヘッダーロール定義
# ============================================================

class HeaderRole:
    """ヘッダー列のロール定数"""
    SEGMENT_LABEL = "segment_label"
    SALES = "sales"
    OPERATING_PROFIT = "operating_profit"
    ORDINARY_PROFIT = "ordinary_profit"
    SEGMENT_PROFIT = "segment_profit"
    ASSETS = "assets"
    RATIO = "ratio"
    NOTES = "notes"
    UNKNOWN = "unknown"


# ============================================================
# キーワード辞書 (ロール別 + スコア重み)
# ============================================================

# (keyword, score) のペア。keyword は正規化後テキストでマッチ。
_SALES_KEYWORDS: list[tuple[str, float]] = [
    ("売上高", 1.0),
    ("売上収益", 1.0),
    ("営業収益", 0.9),
    ("経常収益", 0.8),
    ("事業収益", 0.8),
    ("営業収入", 0.8),
    ("純売上高", 1.0),
    ("売上合計", 0.9),
    ("収益", 0.6),
    ("売上", 0.7),
    ("外部顧客への売上高", 1.0),
    ("外部顧客への売上収益", 1.0),
    ("外部顧客売上高", 1.0),
    ("外部売上高", 0.9),
    ("Netsales", 1.0),
    ("NetSales", 1.0),
    ("Revenue", 0.9),
    ("Sales", 0.8),
    # Phase 3 追加
    ("Operatingrevenue", 0.9),
    ("Netrevenue", 0.9),
    ("Totalrevenue", 0.85),
    ("Totalsales", 0.85),
    # Phase 5 追加 (context-sensitive: 単独では sales 採用しない)
    ("金額", 0.25),      # 汎用語 — 単独不採用
    ("Amount", 0.2),     # 汎用語 — 単独不採用
    ("総売上高", 0.9),
    ("純収益", 0.8),
    ("Grosspremiums", 0.5),  # 保険業 — 中スコア (segment文脈で昇格)
]

_OPERATING_PROFIT_KEYWORDS: list[tuple[str, float]] = [
    ("営業利益", 1.0),
    ("営業損失", 0.9),
    ("事業利益", 0.9),
    ("コア営業利益", 0.95),
    ("Operatingprofit", 1.0),
    ("Operatingincome", 1.0),
    ("Operatingloss", 0.9),
    # Phase 3 追加
    ("CoreOperatingProfit", 0.95),
    ("AdjustedOperatingIncome", 0.9),
]

_SEGMENT_PROFIT_KEYWORDS: list[tuple[str, float]] = [
    ("セグメント利益", 1.0),
    ("セグメント損益", 1.0),
    ("セグメント利益又は損失", 1.0),
    ("セグメント利益(損失)", 1.0),
    ("セグメント利益（損失）", 1.0),
    ("利益又は損失", 0.9),
    ("利益(損失)", 0.9),
    ("利益（損失）", 0.9),
    ("利益又は損失(△は損失)", 0.9),
    ("利益", 0.4),
    ("損益", 0.7),
    ("損失", 0.5),
    ("Segmentprofit", 1.0),
    ("Segmentincome", 1.0),
    ("Profit", 0.6),
    # Phase 3 追加
    ("ProfitorLoss", 0.9),
    ("SegmentProfitorLoss", 1.0),
    ("Income", 0.4),
    # Phase 5 追加
    ("CoreOperatingIncome", 0.95),
    ("AdjustedOperatingIncome", 0.9),
    ("Adjustedprofit", 0.85),
]

_ORDINARY_PROFIT_KEYWORDS: list[tuple[str, float]] = [
    ("経常利益", 1.0),
    ("経常損失", 0.9),
    ("Ordinaryincome", 1.0),
    ("Ordinaryprofit", 1.0),
]

_RATIO_KEYWORDS: list[tuple[str, float]] = [
    ("前年比", 1.0),
    ("前年同期比", 1.0),
    ("増減率", 1.0),
    ("増減額", 0.8),
    ("構成比", 1.0),
    ("利益率", 1.0),
    ("売上高比", 0.9),
    ("%", 0.7),
    ("％", 0.7),
    ("YoY", 0.9),
    ("Change", 0.6),
]

_ASSETS_KEYWORDS: list[tuple[str, float]] = [
    ("資産", 0.9),
    ("総資産", 1.0),
    ("セグメント資産", 1.0),
    ("Assets", 0.9),
    ("Totalassets", 1.0),
]


# ============================================================
# normalize_header
# ============================================================

def normalize_header(text: str) -> str:
    """
    ヘッダーテキストの正規化。

    処理:
      1. NFKC正規化 (全角→半角、合字分解 etc.)
      2. 改行→空文字
      3. 連続空白→空文字
      4. 括弧内の単位注記を保持しつつ空白除去

    例:
      "売 上 高" → "売上高"
      "売上高\\n（百万円）" → "売上高(百万円)"
      "Operating   profit" → "Operatingprofit"
      "セグメント 利益" → "セグメント利益"
    """
    # Step 1: NFKC 正規化
    text = unicodedata.normalize("NFKC", text)

    # Step 2: 改行除去
    text = text.replace("\n", "").replace("\r", "")

    # Step 3: 全空白除去
    text = re.sub(r'\s+', '', text)

    return text


def normalize_header_for_role(text: str) -> str:
    """
    Phase 5: role 判定用のヘッダー正規化。
    単位注記・注記を除去して、role 判定に必要な部分のみ残す。

    例:
      "売上高(百万円)" → "売上高"
      "セグメント利益（注）" → "セグメント利益"
      "Amount(Millions of yen)" → "Amount"
    """
    text = normalize_header(text)
    # 括弧内の単位注記を除去
    text = re.sub(r'[（(][^）)]*(?:百万円|億円|千円|円|Millionsofyen|Billionsofyen|Thousandsofyen|単位|注|Note)[^）)]*[）)]', '', text)
    return text.strip()


# ============================================================
# Phase 5: 単位行/注記行判定
# ============================================================

_UNIT_NOTE_PATTERNS = [
    re.compile(r'^\s*[（(](?:百万円|億円|千円|円|単位|注|Notes?)[）)]\s*$'),
    re.compile(r'^\s*(?:百万円|億円|千円|Millionsofyen|Billionsofyen)\s*$', re.IGNORECASE),
    re.compile(r'^\s*[（(]?単位[：:]', re.IGNORECASE),
    re.compile(r'^\s*[（(]?(?:注|Note)', re.IGNORECASE),
]


def _is_unit_or_note_token(text: str) -> bool:
    """トークンが単位/注記のみかを判定。"""
    normalized = normalize_header(text)
    if not normalized:
        return False
    for pat in _UNIT_NOTE_PATTERNS:
        if pat.search(normalized):
            return True
    # 括弧書き単位のみ
    if re.fullmatch(r'[（(][^）)]{1,20}[）)]', normalized):
        inner = normalized[1:-1]
        if any(u in inner for u in ['百万円', '億円', '千円', '円', '単位', '注', 'Million', 'Billion']):
            return True
    return False


def _is_unit_or_note_line(line: str) -> bool:
    """行全体が単位/注記のみで構成されているかを判定。"""
    stripped = line.strip()
    if not stripped:
        return False
    return _is_unit_or_note_token(stripped)


# ============================================================
# merge_multiline_headers
# ============================================================

def merge_multiline_headers(
    rows: list[str],
    max_header_lines: int = 3,
) -> list[str]:
    """
    複数行に分断されたヘッダーを結合する。

    結合セル由来のヘッダー分断に対応:
      行1: "売上高    セグメント"
      行2: "（百万円）  利益"
      → ["売上高(百万円)", "セグメント利益"]

    簡易実装: 先頭 max_header_lines 行を連結して1行にする。
    本格的な列位置ベースの結合は Sprint 2 で実装。

    Args:
        rows: ヘッダー候補行のリスト (最大 max_header_lines 行)
        max_header_lines: 結合する最大行数

    Returns:
        結合済みヘッダーテキストのリスト (現実装では1要素)
    """
    if not rows:
        return []

    target = rows[:max_header_lines]
    merged = " ".join(line.strip() for line in target if line.strip())
    return [merged]


# ============================================================
# score_header_role
# ============================================================

def score_header_role(text: str) -> dict[str, float]:
    """
    ヘッダーテキストのロールをスコアリング。

    ロールごとの score dict を返す。
    最も高いスコアのロールが推定ロール。

    Args:
        text: ヘッダーテキスト (正規化前でも可)

    Returns:
        {
            "sales": 0.9,
            "operating_profit": 0.2,
            "segment_profit": 0.0,
            "ordinary_profit": 0.0,
            "ratio": 0.0,
            "assets": 0.0,
            "unknown": 0.0,
        }

    Example:
        >>> score_header_role("売上高")
        {"sales": 1.0, ...}

        >>> score_header_role("前年比")
        {"ratio": 1.0, ...}
    """
    normalized = normalize_header(text)

    scores: dict[str, float] = {
        HeaderRole.SALES: 0.0,
        HeaderRole.OPERATING_PROFIT: 0.0,
        HeaderRole.SEGMENT_PROFIT: 0.0,
        HeaderRole.ORDINARY_PROFIT: 0.0,
        HeaderRole.RATIO: 0.0,
        HeaderRole.ASSETS: 0.0,
        HeaderRole.UNKNOWN: 0.0,
    }

    def _match_best(keywords: list[tuple[str, float]]) -> float:
        best = 0.0
        for kw, s in keywords:
            nkw = normalize_header(kw)
            if nkw in normalized:
                best = max(best, s)
        return best

    scores[HeaderRole.SALES] = _match_best(_SALES_KEYWORDS)
    scores[HeaderRole.OPERATING_PROFIT] = _match_best(_OPERATING_PROFIT_KEYWORDS)
    scores[HeaderRole.SEGMENT_PROFIT] = _match_best(_SEGMENT_PROFIT_KEYWORDS)
    scores[HeaderRole.ORDINARY_PROFIT] = _match_best(_ORDINARY_PROFIT_KEYWORDS)
    scores[HeaderRole.RATIO] = _match_best(_RATIO_KEYWORDS)
    scores[HeaderRole.ASSETS] = _match_best(_ASSETS_KEYWORDS)

    # --- 競合解消 ---
    # "利益率" は ratio であって profit ではない
    if scores[HeaderRole.RATIO] > 0 and "率" in normalized:
        scores[HeaderRole.OPERATING_PROFIT] = min(
            scores[HeaderRole.OPERATING_PROFIT], 0.1
        )
        scores[HeaderRole.SEGMENT_PROFIT] = min(
            scores[HeaderRole.SEGMENT_PROFIT], 0.1
        )
        scores[HeaderRole.ORDINARY_PROFIT] = min(
            scores[HeaderRole.ORDINARY_PROFIT], 0.1
        )

    # "セグメント利益" > "営業利益" (セグメント表コンテキストでは)
    if scores[HeaderRole.SEGMENT_PROFIT] > 0 and scores[HeaderRole.OPERATING_PROFIT] > 0:
        if "セグメント" in normalized:
            scores[HeaderRole.OPERATING_PROFIT] = min(
                scores[HeaderRole.OPERATING_PROFIT], 0.3
            )

    # 全てゼロなら unknown
    if max(scores.values()) == 0:
        scores[HeaderRole.UNKNOWN] = 1.0

    return scores


# ============================================================
# infer_header_roles
# ============================================================

@dataclass
class HeaderAnalysis:
    """ヘッダー解析の結果"""
    headers: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    scores: list[dict[str, float]] = field(default_factory=list)

    @property
    def has_sales(self) -> bool:
        return HeaderRole.SALES in self.roles

    @property
    def has_profit(self) -> bool:
        return any(r in self.roles for r in [
            HeaderRole.OPERATING_PROFIT,
            HeaderRole.SEGMENT_PROFIT,
            HeaderRole.ORDINARY_PROFIT,
        ])

    @property
    def profit_label(self) -> str:
        """利益列のラベルを返す (見つからなければ空文字)"""
        for hdr, role in zip(self.headers, self.roles):
            if role in [
                HeaderRole.OPERATING_PROFIT,
                HeaderRole.SEGMENT_PROFIT,
                HeaderRole.ORDINARY_PROFIT,
            ]:
                return hdr
        return ""


def infer_header_roles(
    header_texts: list[str],
) -> HeaderAnalysis:
    """
    ヘッダーテキストのリストから各列のロールを推定する。

    Args:
        header_texts: 列ヘッダーテキストのリスト

    Returns:
        HeaderAnalysis (headers, roles, scores)
    """
    roles: list[str] = []
    scores_list: list[dict[str, float]] = []

    for text in header_texts:
        scores = score_header_role(text)
        scores_list.append(scores)

        # 最高スコアのロールを採用
        best_role = max(scores, key=lambda r: scores[r])
        best_score = scores[best_role]

        if best_score >= 0.5:
            roles.append(best_role)
        else:
            roles.append(HeaderRole.UNKNOWN)

    return HeaderAnalysis(
        headers=header_texts,
        roles=roles,
        scores=scores_list,
    )


# ============================================================
# detect_numeric_columns
# ============================================================

_NUM_PATTERN = re.compile(r'[△▲\-－]?[\d,]+(?:\.\d+)?')


def detect_numeric_columns(
    rows: list[list[str]],
    min_ratio: float = 0.5,
) -> list[bool]:
    """
    行データから数値列を判定する。

    Args:
        rows: 行のリスト (各行はセル文字列のリスト)
        min_ratio: 数値セルの割合がこの閾値以上なら数値列

    Returns:
        列ごとの bool リスト (True = 数値列)
    """
    if not rows or not rows[0]:
        return []

    n_cols = max(len(row) for row in rows)
    results: list[bool] = []

    for col_idx in range(n_cols):
        total = 0
        numeric = 0
        for row in rows:
            if col_idx < len(row):
                cell = row[col_idx].strip()
                if cell:
                    total += 1
                    if _NUM_PATTERN.fullmatch(cell):
                        numeric += 1

        is_num = (numeric / total >= min_ratio) if total > 0 else False
        results.append(is_num)

    return results


# ============================================================
# detect_unit_annotations
# ============================================================

_UNIT_PATTERNS = [
    re.compile(r'[（(]単位[：:]?\s*(百万円|億円|千円|円)[）)]'),
    re.compile(r'単位[：:]\s*(百万円|億円|千円|円)'),
    re.compile(r'[（(](百万円|億円|千円)[）)]'),
]


def detect_unit_annotations(text: str) -> str | None:
    """
    テキストから単位アノテーションを検出する。

    Returns:
        "百万円" / "億円" / "千円" / "円" / None
    """
    for pat in _UNIT_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


# ============================================================
# Phase C pre-step: Preamble Trim (Phase 7)
# ============================================================

_PREAMBLE_DATE_RE = re.compile(
    r'^\d{4}年\d{1,2}月\d{1,2}日$'
)
_PREAMBLE_GREETING_RE = re.compile(
    r'^各\s*位$'
)
# 章見出し: "1.", "１．", "（１）", "一、" 等で始まる行
_CHAPTER_HEADING_RE = re.compile(
    r'^[0-9０-９一二三四五六七八九十]+[\\.．、)\）]\s*'
    r'|^[(（][0-9０-９一二三四五六七八九十]+[)）]\s*'
)
# 開示冒頭の定型語 — 行内のどこかにこれがあれば preamble 候補
_PREAMBLE_DISCLOSURE_TERMS = [
    # 会社情報
    "会社名", "代表者名", "代表者", "問合せ先", "問い合わせ先",
    "証券コード", "コード番号", "上場会社名",
    "所在地", "上場取引所", "上場市場",
    # 役職名
    "代表取締役", "取締役社長", "社長執行役員", "執行役員",
    "常務取締役", "専務取締役",
    # 連絡先
    "TEL", "FAX", "URL", "E-mail", "電話",
    "広報", "経理部", "経営企画",
    # 短信定型文
    "決算短信", "決算説明", "補足説明",
    "添付資料", "IR", "お知らせ",
    "東証", "有無", "作成の有無", "開催の有無",
    "配当予想の修正", "業績予想の修正",
    # 訂正案内
    "訂正いたします", "訂正後", "訂正前", "訂正箇所",
    "数値データ", "送信します",
    # セクション見出し (表ヘッダーではない)
    "セグメント別の概況", "セグメントの概要", "報告セグメントの概要",
    "セグメント情報に記載", "区分ごとの状況",
    "セグメント情報等", "セグメント情報",
    "関連情報", "地域別",
    "お詫び", "お詫び申し上げ",
    "ご迷惑", "ご心配",
    "深くお詫び",
]
# 説明文の短い見出し (exact or prefix)
_PREAMBLE_SHORT_HEADINGS: set[str] = {
    "記", "以上", "概要", "サマリー情報",
    "訂正の内容", "訂正理由", "訂正についてのお知らせ",
    "なお", "下記のとおり",
    # 訂正マーカー
    "【訂正前】", "【訂正後】", "[訂正前]", "[訂正後]",
    "訂正前", "訂正後",
}
_TABLE_HEADER_TERMS = [
    "売上", "利益", "収益", "営業", "セグメント", "経常",
    "損益", "資産", "前年", "予想", "増減", "計",
    "外部", "内部", "合計", "調整", "通期",
    "第1四半期", "第2四半期", "第3四半期", "第4四半期",
    "Revenue", "Sales", "Profit", "Income", "Segment",
]
# 文書タイトル判定語 — これを含む行は表ヘッダーではなく文書タイトル
_DOCUMENT_TITLE_MARKERS = [
    "決算短信", "短信", "決算説明",
    "お知らせ", "一部訂正", "訂正に関する",
    "数値データ訂正", "数値データ",
    "〔日本基準〕", "(日本基準)", "（日本基準）",
    "〔IFRS〕", "(IFRS)", "（IFRS）",
    "〔米国基準〕", "(米国基準)", "（米国基準）",
    "(非連結)", "（非連結）", "(連結)", "（連結）",
    "年3月期", "年12月期", "年9月期", "年6月期",
    "年2月期", "年1月期", "年8月期", "年7月期",
    "年11月期", "年10月期", "年5月期", "年4月期",
    "サマリー情報",
    "業績予想", "配当予想",
]


def _is_document_title(no_space: str) -> bool:
    """文書タイトル行かどうかを判定する。

    「第3四半期決算短信〔日本基準〕（非連結）」のような行は
    表ヘッダー語 (第3四半期) を含むが、文書タイトルである。
    """
    return any(marker in no_space for marker in _DOCUMENT_TITLE_MARKERS)


# セクション見出しパターン — 表ヘッダー語を含んでいても skip すべき
_SECTION_HEADING_RE = re.compile(
    r'^[\[\]【】（()）\(\)]+.*[\[\]【】（()）\(\)]+$'   # 括弧で囲まれた見出し
    r'|^[a-zA-Zａ-ｚＡ-Ｚ]\.\s*'                    # c.xxx, a.xxx
    r'|^[①②③④⑤⑥⑦⑧⑨⑩]'                         # ①②③...
    r'|^[\(（]?セグメント情報'                     # (セグメント情報...
    r'|^[\(（]?報告セグメント'                     # (報告セグメント...
    r'|^[\(（]?セグメント別'                       # (セグメント別...
    r'|^[\(（]?事業セグメント'                     # (事業セグメント...
)
_SECTION_HEADING_TERMS = [
    "の注記", "等の注記", "の概要", "の概況",
    "区分ごとの状況", "に記載された",
    "の状況",
]
# 末尾一致で判定する概況/概要見出し
_SECTION_HEADING_SUFFIX_RE = re.compile(r'概況$|の概要$|の状況$')


def _is_section_heading(normalized: str, no_space: str) -> bool:
    """セクション見出しかどうかを判定する。

    以下は表ヘッダー語 (セグメント等) を含んでいても skip すべき:
    - （セグメント情報等の注記）
    - 【セグメント情報】
    - c.セグメント情報に記載された区分ごとの状況
    - ２）セグメント別の概況
    - ① [SyB V-1901...]
    """
    # 括弧囲み: (セグメント情報等) 【セグメント情報】
    if (no_space.startswith(('【', '(', '（', '['))
            and no_space.endswith(('】', ')', '）', ']'))
            and len(no_space) <= 30):
        return True
    # セクション特有語を含む
    if any(term in no_space for term in _SECTION_HEADING_TERMS):
        return True
    # 末尾が概況/概要
    if _SECTION_HEADING_SUFFIX_RE.search(no_space):
        return True
    # 箇条書き / 英字 / セグメント見出しパターン
    if _SECTION_HEADING_RE.match(normalized):
        return True
    return False


# 強い表ヘッダー語 — 文章中に出ても表の可能性が高い
_STRONG_TABLE_TERMS = {
    "売上高", "営業利益", "経常利益", "セグメント利益", "セグメント",
    "収益", "増減", "前年同期", "合計", "調整額",
    "外部顧客", "セグメント間",
    "Revenue", "Sales", "Operating", "Segment",
}

# 文章 (narrative) 判定用語
_NARRATIVE_MARKERS = [
    "ため", "ので", "こと", "もの", "ところ",
    "関する", "判明", "修正", "あります", "ございます",
    "いたします", "おります", "つきまして", "ついて",
    "における", "しました", "なります", "おける",
    "誤りが", "変更", "見直し",
    # Phase C 強化: 注記文・お知らせ文の検出用
    "こうした中", "当社グループ", "詳細については",
    "ご覧ください", "ページをご覧", "をベースとした",
    "以下のとおり", "セグメント間の内部",
    "なお,", "また,", "ただし,",
    "報告セグメントの利益は", "当期純利益は",
]


def _is_sentence_like(normalized: str, no_space: str) -> bool:
    """行が説明文/文章っぽいかを判定する。

    以下のいずれかを満たせば文章:
    - 句読点 (。、) を含む
    - narrative marker を含む
    - 25文字超の1列テキスト
    """
    # 句読点
    if "。" in normalized or "、" in normalized:
        return True
    # narrative marker
    if any(m in no_space for m in _NARRATIVE_MARKERS):
        return True
    # 長文 (25文字超) かつ1列
    if len(no_space) > 25:
        tokens = re.split(r'\s{2,}|\t', normalized)
        tokens = [t for t in tokens if t.strip()]
        if len(tokens) <= 1:
            return True
    return False


def trim_non_table_preamble(
    lines: list[str],
    *,
    max_skip: int = 20,
) -> tuple[list[str], dict]:
    """表データの前に混入した開示文書前置き・説明文をスキップする。

    判定順序 (重要):
      0. 文書タイトル (決算短信/お知らせ/訂正等) → skip (表ヘッダー語があっても)
      1. 表ヘッダー用語があり文書タイトルではない → stop
      2. 開示定型語があれば → skip
      3. 章見出し (「1.訂正の内容」等) → skip
      4. 句点「。」を含む説明文 (1列) → skip
      5. 2列以上で定型語なし → stop (表形式)
      6. 数値があり日付でない → stop
      7. 短い行 (15文字以下) or 短い見出し → skip

    Args:
        lines: best_table_lines
        max_skip: 最大スキップ行数

    Returns:
        (trimmed_lines, debug_info)
    """
    if not lines:
        return lines, {"skipped_count": 0, "skipped_lines": [], "stop_reason": "empty"}

    skip_count = 0
    skipped_lines: list[str] = []
    stop_reason = "max_skip"

    for i, line in enumerate(lines):
        if i >= max_skip:
            stop_reason = "max_skip"
            break

        stripped = line.strip()
        if not stripped:
            skip_count += 1
            skipped_lines.append("")
            continue

        normalized = unicodedata.normalize("NFKC", stripped)
        no_space = re.sub(r'\s+', '', normalized)

        # === 0. 文書タイトル判定 (最優先 skip) ===
        if _is_document_title(no_space):
            skip_count += 1
            skipped_lines.append(stripped[:60])
            continue

        # === 0.5 セクション見出し判定 (表ヘッダー語より優先して skip) ===
        if _is_section_heading(normalized, no_space):
            skip_count += 1
            skipped_lines.append(stripped[:60])
            continue

        # === 1. 表ヘッダー用語チェック (sentence_like ガード付き) ===
        matched_terms = [t for t in _TABLE_HEADER_TERMS if t in no_space]
        if matched_terms:
            # 文章判定: 句読点/接続詞/長文 → 説明文なので skip
            is_sentence = _is_sentence_like(normalized, no_space)
            if is_sentence:
                # 強い表語が含まれるか
                has_strong = any(t for t in matched_terms if t in _STRONG_TABLE_TERMS)
                if not has_strong:
                    # 弱い表語のみ + 文章 → skip
                    skip_count += 1
                    skipped_lines.append(stripped[:60])
                    continue
                # 強い表語あり + 文章 → さらに厳密チェック
                # 1列で句読点ありなら文章として skip
                tokens_check = re.split(r'\s{2,}|\t', stripped)
                tokens_check = [t for t in tokens_check if t.strip()]
                if len(tokens_check) <= 1 and ("。" in normalized or "、" in normalized):
                    skip_count += 1
                    skipped_lines.append(stripped[:60])
                    continue
            # 1列の長文 (≥ 40文字 + 句読点) → お知らせ文/説明文として skip
            tokens_check2 = re.split(r'\s{2,}|\t', stripped)
            tokens_check2 = [t for t in tokens_check2 if t.strip()]
            if len(tokens_check2) <= 1 and len(no_space) >= 40:
                skip_count += 1
                skipped_lines.append(stripped[:60])
                continue
            # 複数列 (tokens ≥ 2) なら表ヘッダーとして stop
            tokens_stop = re.split(r'\s{2,}|\t', stripped)
            tokens_stop = [t for t in tokens_stop if t.strip()]
            if len(tokens_stop) >= 2:
                stop_reason = "table_header_term"
                break
            # 1列だが短い表語行 (表ヘッダーの一部) → stop
            stop_reason = "table_header_term"
            break

        # === 2. 開示定型語チェック (空白除去版でマッチ → skip) ===
        if any(term in no_space for term in _PREAMBLE_DISCLOSURE_TERMS):
            skip_count += 1
            skipped_lines.append(stripped[:60])
            continue

        # === 3. 章見出し (「1.訂正の内容」等) → skip ===
        if _CHAPTER_HEADING_RE.match(normalized):
            skip_count += 1
            skipped_lines.append(stripped[:60])
            continue

        # === 4. 句点「。」を含む説明文 (1列・表語なし) → skip ===
        tokens = re.split(r'\s{2,}|\t', stripped)
        tokens = [t for t in tokens if t.strip()]
        if len(tokens) <= 1 and "。" in normalized:
            skip_count += 1
            skipped_lines.append(stripped[:60])
            continue

        # === 5. 表形式チェック: 2列以上で定型語なし → stop ===
        if len(tokens) >= 2:
            stop_reason = "multi_column"
            break

        # === 6. 数値含有チェック ===
        if _NUM_PATTERN.search(normalized):
            if not _PREAMBLE_DATE_RE.fullmatch(no_space):
                stop_reason = "numeric"
                break

        # === 7. 前置き判定 (残ったシンプル行) ===
        is_preamble = False

        if _PREAMBLE_DATE_RE.fullmatch(no_space):
            is_preamble = True
        elif _PREAMBLE_GREETING_RE.fullmatch(normalized):
            is_preamble = True
        elif no_space in _PREAMBLE_SHORT_HEADINGS:
            is_preamble = True
        elif len(no_space) <= 15 and not _NUM_PATTERN.search(normalized):
            is_preamble = True
        # 長い1列テキスト (sentence_like) も preamble
        elif len(tokens) <= 1 and len(no_space) > 15 and _is_sentence_like(normalized, no_space):
            is_preamble = True

        if is_preamble:
            skip_count += 1
            skipped_lines.append(stripped[:60])
        else:
            stop_reason = "unknown_content"
            break

    debug_info = {
        "skipped_count": skip_count,
        "skipped_lines": skipped_lines,
        "stop_reason": stop_reason,
    }

    return lines[skip_count:], debug_info


# ============================================================
# Phase C: Header Grid Reconstruction (v2)
# ============================================================

@dataclass
class HeaderGrid:
    """ヘッダーグリッド再構築の結果"""
    reconstructed_headers: list[str] = field(default_factory=list)
    header_units: str | None = None
    header_band_height: int = 0
    header_confidence: float = 0.0
    raw_header_rows: list[str] = field(default_factory=list)


def detect_header_band(lines: list[str], max_scan: int = 10) -> int:
    """
    テーブル行リストの先頭からヘッダーバンド（数値データが始まる前の行数）を推定する。

    ヘッダー行 = 数値がほとんどない行 (数値含有率 < 0.3)
    データ行  = 数値が多い行

    Args:
        lines: テーブル行リスト
        max_scan: 最大スキャン行数

    Returns:
        ヘッダーバンドの高さ (0 = ヘッダーなし)
    """
    if not lines:
        return 0

    scan_end = min(len(lines), max_scan)
    data_start = 0

    for i in range(scan_end):
        line = lines[i].strip()
        if not line:
            continue
        # 数値トークン数をカウント
        tokens = re.split(r'\s{2,}|\t', line)
        num_count = sum(1 for t in tokens if _NUM_PATTERN.fullmatch(t.strip()))
        total_tokens = len([t for t in tokens if t.strip()])

        # 数値トークンが半数以上 → データ行と判定
        if total_tokens > 0 and num_count / total_tokens >= 0.3:
            data_start = i
            break
    else:
        # 全行がヘッダー的 → 保守的に2行
        data_start = min(2, len(lines))

    return max(data_start, 1)  # 最低1行はヘッダー


def reconstruct_header_grid(
    header_lines: list[str],
) -> list[str]:
    """
    Phase 5 改良: 複数行ヘッダーを再構築して列ごとのヘッダーテキストを復元する。

    改良点:
      - 単位行/注記行を検出して除去 (role 判定に影響しない)
      - 上段「営業」+ 下段「利益」→「営業利益」のような分割ヘッダーを復元
      - 空セルをまたいだ結合セル補完

    Args:
        header_lines: ヘッダー行のテキストリスト

    Returns:
        再構築された列ヘッダーのリスト
    """
    if not header_lines:
        return []

    # Phase 5: 単位行/注記行を除外
    effective_lines = []
    for line in header_lines:
        if _is_unit_or_note_line(line):
            continue
        effective_lines.append(line)

    if not effective_lines:
        # 全行が単位/注記 → 元のヘッダーで続行
        effective_lines = header_lines

    # 各行をトークンに分割 (2スペース以上またはタブで区切り)
    tokenized_rows: list[list[str]] = []
    for line in effective_lines:
        tokens = re.split(r'\s{2,}|\t', line.strip())
        tokens = [t.strip() for t in tokens if t.strip()]
        # 単位/注記トークンを除去
        tokens = [t for t in tokens if not _is_unit_or_note_token(t)]
        tokenized_rows.append(tokens)

    if not tokenized_rows:
        return []

    # 最大列数を決定
    max_cols = max(len(row) for row in tokenized_rows)
    if max_cols == 0:
        return []

    # 各列を縦結合 (分割ヘッダー復元: 上段「営業」+下段「利益」→「営業利益」)
    merged: list[str] = []
    for col_idx in range(max_cols):
        parts: list[str] = []
        for row in tokenized_rows:
            if col_idx < len(row):
                text = row[col_idx]
                if text:
                    parts.append(text)
        combined = " ".join(parts)
        # role 判定用に単位注記を除去した正規化
        merged.append(normalize_header_for_role(combined))

    return merged


def extract_header_units(header_lines: list[str]) -> str | None:
    """
    ヘッダー行群から単位情報を抽出する。
    単位はヘッダーテキストから分離して返す。

    Returns:
        "百万円" / "億円" / "千円" / "円" / None
    """
    full_text = " ".join(header_lines)
    return detect_unit_annotations(full_text)
