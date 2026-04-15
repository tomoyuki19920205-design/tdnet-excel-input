#!/usr/bin/env python3
"""earnings_guidance_extractor.py — 4Q決算短信 来期ガイダンス + 見通し抽出

ゼロから再実装。テスト駆動。
"""
from __future__ import annotations

import io
import logging
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger("guidance_extractor")

# ============================================================
# 定数
# ============================================================

_EPS_ABSURD_THRESHOLD = 10_000

# Forecast 用 XBRL タグマッピング
_FORECAST_TAG_MAP = {
    # Sales
    "NetSales": "sales",
    "Revenue": "sales",
    "OperatingRevenue": "sales",
    "OperatingRevenuesREIT": "sales",
    # Operating profit
    "OperatingIncome": "operating_profit",
    "OperatingProfit": "operating_profit",
    # Ordinary profit
    "OrdinaryIncome": "ordinary_profit",
    # Net income
    "NetIncome": "net_income",
    "ProfitLoss": "net_income",
    "ProfitLossAttributableToOwnersOfParent": "net_income",
}

# 内部メトリック名 → GuidanceData フィールド名
_METRIC_TO_FIELD = {
    "sales": "sales_forecast",
    "operating_profit": "op_forecast",
    "ordinary_profit": "ordinary_forecast",
    "net_income": "net_income_forecast",
    "eps": "eps_forecast",
}

# EPS XBRL タグ
_EPS_BASIC_TAGS = {
    "EarningsPerShare",
    "BasicEarningsPerShare",
    "BasicEarningsLossPerShare",
    "NetIncomePerShare",
}
_EPS_DILUTED_TAGS = {
    "DilutedEarningsPerShare",
    "DilutedEarningsLossPerShare",
}
_EPS_ALL_TAGS = _EPS_BASIC_TAGS | _EPS_DILUTED_TAGS

# 配当金タグ（EPS と誤認しないよう除外）
_DIVIDEND_XBRL_TAGS = {
    "DividendPerShare",
    "DividendsPerShare",
    "InterimDividendPerShare",
    "FinalDividendPerShare",
    "AnnualDividendPerShare",
    "TotalDividendPaidPerShare",
}

# iXBRL / XBRL 拡張子
_IXBRL_EXTS = ("-ixbrl.htm", ".ixbrl.htm", "-ixbrl.html", ".ixbrl.html", ".ixbrl")
_XBRL_EXTS = (".xbrl",)
_ZIP_SIG = b"PK\x03\x04"


# ============================================================
# GuidanceData
# ============================================================
@dataclass
class GuidanceData:
    """来期ガイダンスデータ"""
    sales_forecast: Optional[float] = None
    op_forecast: Optional[float] = None
    ordinary_forecast: Optional[float] = None
    net_income_forecast: Optional[float] = None
    eps_forecast: Optional[float] = None

    sales_actual: Optional[float] = None
    op_actual: Optional[float] = None
    eps_actual: Optional[float] = None

    outlook_text: str = ""
    outlook_summary: str = ""
    outlook_factors: dict = field(default_factory=dict)

    @property
    def has_guidance(self) -> bool:
        return any(v is not None for v in [
            self.sales_forecast, self.op_forecast, self.eps_forecast,
        ])

    @property
    def has_outlook(self) -> bool:
        # Outlook 抽出は無効化済み — 常に False
        return False

    @property
    def sales_yoy(self) -> Optional[float]:
        if self.sales_forecast is None or self.sales_actual is None or self.sales_actual == 0:
            return None
        return (self.sales_forecast / self.sales_actual) - 1.0

    @property
    def op_yoy(self) -> Optional[float]:
        if self.op_forecast is None or self.op_actual is None or self.op_actual == 0:
            return None
        return (self.op_forecast / self.op_actual) - 1.0

    @property
    def eps_yoy(self) -> Optional[float]:
        if self.eps_forecast is None or self.eps_actual is None or self.eps_actual == 0:
            return None
        return (self.eps_forecast / self.eps_actual) - 1.0


# ============================================================
# コンテキスト分類
# ============================================================

def _classify_period_type(ctx: str) -> str:
    """contextRef から期間種別を判定する。

    Returns: "full_year" / "q2_cumulative" / "unknown"
    """
    if "AccumulatedQ2" in ctx or "SecondQuarterMember" in ctx:
        return "q2_cumulative"
    if "YearDuration" in ctx and "QuarterMember" not in ctx:
        return "full_year"
    return "unknown"


def _classify_horizon(ctx: str) -> str:
    """contextRef から来期/当期を判定する。

    Returns: "next_year" / "current_year" / "unknown"
    """
    if "Next" in ctx:
        return "next_year"
    if "Current" in ctx:
        return "current_year"
    return "unknown"


# ============================================================
# 候補選択
# ============================================================

def _select_best_candidates(candidates: list[dict]) -> dict:
    """候補リストからメトリックごとに最適候補を選ぶ。

    Returns: {"sales": value_or_None, "op": ..., "eps": ..., ...}
    """
    by_metric: dict[str, list[dict]] = {}
    for c in candidates:
        by_metric.setdefault(c["metric"], []).append(c)

    result: dict[str, float | None] = {}
    for metric, cands in by_metric.items():
        best = _pick_best(cands, is_eps=(metric == "eps"))
        result[metric] = best["value"] if best else None
    return result


def _pick_best(candidates: list[dict], is_eps: bool = False) -> dict | None:
    """単一メトリックの候補から最適を選ぶ。

    ルール:
    - q2_cumulative は常に除外
    - EPS は full_year のみ採用
    - non-EPS は full_year > unknown（q2 除外済み）
    - horizon: next_year > current_year
    - 連結優先
    """
    # q2_cumulative は常に除外
    filtered = [c for c in candidates if c["period_type"] != "q2_cumulative"]
    if not filtered:
        return None

    if is_eps:
        # EPS: full_year のみ採用
        fy = [c for c in filtered if c["period_type"] == "full_year"]
        if not fy:
            return None
        filtered = fy

    def _score(c: dict) -> tuple:
        """スコアをタプルで返す（大きい方が優先）。

        EPS の場合: Basic 優先 + 絶対値が大きい方を優先
        （配当金 2.0 vs 実EPS 59.41 のような誤採用を防止）
        """
        period_s = 100 if c["period_type"] == "full_year" else (10 if c["period_type"] == "unknown" else 0)
        horizon_s = 50 if c["horizon"] == "next_year" else (30 if c["horizon"] == "current_year" else 0)
        consol_s = 5 if c.get("is_consol") else 0
        basic_s = 2 if c.get("is_basic", True) else 0
        # EPS の場合、絶対値が大きい方が真のEPSである可能性が高い
        abs_val = abs(c["value"]) if is_eps else 0
        return (period_s, basic_s, abs_val, horizon_s, consol_s)

    filtered.sort(key=_score, reverse=True)
    return filtered[0]


# ============================================================
# EPS テキスト正規化
# ============================================================

def _normalize_eps_text(text: str) -> float | None:
    """EPS テキストを float に変換する。

    "237.98" → 237.98, "120円50銭" → 120.5, "△10.5円" → -10.5
    """
    if text is None:
        return None
    s = unicodedata.normalize("NFKC", text).strip()
    if not s:
        return None

    is_negative = False
    for c in "△▲":
        if c in s:
            is_negative = True
            s = s.replace(c, "")
    s = s.strip()
    if s.startswith("-") or s.startswith("−"):
        is_negative = True
        s = s.lstrip("-−").strip()

    # 円銭: "120円50銭" → 120.50
    m = re.match(r"([\d,.]+)\s*円\s*(\d+)\s*銭", s)
    if m:
        yen = float(m.group(1).replace(",", ""))
        sen = float(m.group(2))
        val = yen + sen / 100
        return -val if is_negative else val

    # 円のみ: "120円"
    m = re.match(r"([\d,.]+)\s*円", s)
    if m:
        val = float(m.group(1).replace(",", ""))
        return -val if is_negative else val

    # プレーン数値
    s = s.replace(",", "")
    if not s:
        return None
    try:
        val = float(s)
        return -val if is_negative else val
    except ValueError:
        return None


def _classify_eps_text_period(context_text: str) -> str:
    """テキスト行の期間キーワードを判定する。"""
    s = unicodedata.normalize("NFKC", context_text)
    if re.search(r"通期|年間", s):
        return "full_year"
    if re.search(r"第2四半期累計|2Q累計|中間", s):
        return "q2_cumulative"
    return "unknown"


# ============================================================
# EPS テキスト抽出（forecast table fallback）
# ============================================================

_EPS_HEADER_KEYWORDS = [
    "1株当たり当期純利益",
    "1株当たり純利益",
]
_DIVIDEND_KEYWORDS = ["配当", "1株当たり配当金"]
_EPS_SUSPICIOUS_THRESHOLD = 1000  # text fallback で > 1000 は suspicious
_MAX_TABLE_DATA_ROWS = 20  # テーブルデータ行の上限

# 配当文脈を示すキーワード
_DIVIDEND_CONTEXT_RE = re.compile(
    r"配当|期末配当|中間配当|年間配当|円/株|株主還元"
)

# テーブルヘッダーらしい単語（売上/利益等）
_TABLE_HEADER_INDICATORS = re.compile(
    r"売上|営業利益|経常利益|当期純利益|百万円|千円"
)


def _extract_eps_from_forecast_table(plain_text: str) -> list[dict]:
    """業績予想テーブルのプレーンテキストから EPS 候補を抽出する。

    品質ガード:
    - 配当文脈の見出しは拒否
    - テーブル境界検出（空行連続で停止）
    - データ行は最大 _MAX_TABLE_DATA_ROWS 行
    - suspicious 値（> 1000）にはフラグ付与
    """
    lines = plain_text.strip().split("\n")
    header_idx = None
    has_extra_right = False

    # ヘッダー行検索 —— 配当文脈は拒否
    for i, line in enumerate(lines):
        norm = unicodedata.normalize("NFKC", line)
        for kw in _EPS_HEADER_KEYWORDS:
            nkw = unicodedata.normalize("NFKC", kw)
            if nkw not in norm:
                continue

            # ---- 配当文脈チェック ----
            # 見出しの前方に「配当」があれば配当段落の可能性が高い
            eps_pos = norm.find(nkw)
            before = norm[:eps_pos]
            if _DIVIDEND_CONTEXT_RE.search(before):
                continue  # 配当文脈 → スキップ

            # 「1株当たり配当金」と誤認しない: 前後30文字以内に配当キーワード
            context_window = norm[max(0, eps_pos - 30):eps_pos + len(nkw) + 30]
            if re.search(r"配当", context_window) and "純利益" not in context_window:
                continue

            # ---- 見出し行の妥当性: テーブルヘッダーらしいか ----
            # テーブルヘッダーには「売上」「営業利益」「百万円」等がある
            is_table_header = bool(_TABLE_HEADER_INDICATORS.search(norm))

            # テーブルヘッダーでない場合は本文中の配当言及の可能性
            if not is_table_header:
                # 「利益」も含まれていない → 非テーブル行（本文段落等）
                if "利益" not in norm and "EPS" not in norm.upper():
                    continue

            header_idx = i
            # 右側に配当列があるか確認
            after = norm[eps_pos + len(nkw):]
            for dk in _DIVIDEND_KEYWORDS:
                ndk = unicodedata.normalize("NFKC", dk)
                if ndk in after:
                    has_extra_right = True
            break
        if header_idx is not None:
            break

    if header_idx is None:
        return []

    candidates: list[dict] = []
    consecutive_empty = 0
    data_row_count = 0

    for i in range(header_idx + 1, len(lines)):
        line = lines[i].strip()
        if not line:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break  # テーブル終了
            continue
        consecutive_empty = 0

        # データ行上限
        data_row_count += 1
        if data_row_count > _MAX_TABLE_DATA_ROWS:
            break

        norm = unicodedata.normalize("NFKC", line)

        # 配当文脈の行はスキップ
        if _DIVIDEND_CONTEXT_RE.search(norm) and "純利益" not in norm:
            continue

        period_type = _classify_eps_text_period(norm)

        # 数値トークン抽出
        nums_raw = re.findall(r"[-−△▲]?\s*[\d,]+\.?\d*", norm)
        nums: list[float] = []
        for raw in nums_raw:
            v = _normalize_eps_text(raw)
            if v is not None:
                nums.append(v)
        if not nums:
            continue

        # EPS 列の値を選択
        if has_extra_right and len(nums) >= 2:
            eps_val = nums[-2]
        else:
            eps_val = nums[-1]

        # 異常値チェック
        if abs(eps_val) > _EPS_ABSURD_THRESHOLD:
            continue

        # suspicious フラグ
        is_suspicious = abs(eps_val) > _EPS_SUSPICIOUS_THRESHOLD

        candidates.append({
            "metric": "eps",
            "value": eps_val,
            "period_type": period_type,
            "horizon": "next_year",
            "is_consol": True,
            "is_basic": True,
            "ctx": "text",
            "tag": "text_eps",
            "source": "text",
            "suspicious": is_suspicious,
        })

    return candidates


# ============================================================
# 見通し（Outlook）テキスト抽出
# ============================================================

_OUTLOOK_HEADING_RE = re.compile(
    r"(今後の見通し|業績見通し|次期.*見通し|来期.*見通し|"
    r"業績予想に関する説明|経営成績に関する.*分析|"
    r"当面の見通し|今後の事業環境|通期見通し|次期業績予想|"
    r"今後の.*見通し|業績予想について)",
)
_OUTLOOK_STOP_RE = re.compile(
    r"(継続企業|注記事項|配当の状況|配当予想|配当予想に関する|"
    r"役員異動|役員人事|重要事象|セグメント情報.*の注記|"
    r"株主還元|株主還元方針|利益配分|"
    r"コーポレートガバナンス|その他の経営|会計基準の選択)",
)

# 述語パターン（短文品質判定用）
_PREDICATE_RE = re.compile(
    r"(おります|います|ます|です|ございます|なります|であり|見込|想定|予想|計画|方針)"
)

# 段落 fallback 用: 見通し語（任意語）
_OUTLOOK_KEYWORD_RE = re.compile(
    r"(見込[むみ]|想定|予想|継続|回復|拡大|需要|価格|原材料|為替|"
    r"影響|進捗|伸長|改善|増収|増益|減収|減益|見通し|計画|方針)"
)
# 段落 fallback 用: 強語（1つでも採用候補）
_OUTLOOK_STRONG_KEYWORD_RE = re.compile(
    r"(影響|見込[むみ]|想定|予想|回復|拡大|改善|為替|原材料)"
)
# 「経営成績に関する分析」見出し用: 未来表現フィルタ
_FUTURE_EXPRESSION_RE = re.compile(
    r"(来期|次期|今後|見込[むみ]|想定|予想|継続|進める|期待|影響|見通し|計画)"
)
# 「経営成績に関する分析」見出し判定
_KEIEI_SEISEKI_HEADING_RE = re.compile(
    r"経営成績に関する.*分析"
)

# 目次・概況見出し断片の reject フィルタ
_TOC_LINE_RE = re.compile(
    r"(…{2,}|・・・|·{3,}|\.\.\.)"  # ドットリーダー
)
_PAGE_NUM_LINE_RE = re.compile(
    r"[\s　]+\d{1,3}\s*$"  # 行末の単独ページ番号
)
_TOC_JUNK_RE = re.compile(
    r"^\s*("
    r"qualitative|定性的情報|添付資料の目次|添付資料|目次"
    r"|&(#160|#xa0|nbsp);?"   # HTML entities
    r"|\d{1,3}"               # ページ番号 (1-3桁の数字のみ)
    r"|○.*目次"              # ○添付資料の目次 etc.
    r")\s*$",
    re.IGNORECASE,
)
# 概況見出し（outlook 見出しとして認めない）
_GAIKYO_HEADING_RE = re.compile(
    r"(経営成績等の概況|当期の経営成績の概況|財政状態の概況|キャッシュ・フローの概況"
    r"|経営成績に関する説明|当四半期.*概況|当中間期.*概況)"
)
# 列挙見出し（目次の項目見出し）
_ENUM_HEADING_RE = re.compile(
    r"^\s*([（(]\s*[0-9０-９]\s*[）)]|[0-9０-９]+[.．])"  # （1）, (1), 1., １．
)


def _strip_html_tags(text: str) -> str:
    """簡易 HTML タグ除去。style/script ブロックも除去。"""
    # style/script ブロック除去
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", text)


def _is_toc_junk(text: str) -> bool:
    """結果全体が目次/概況断片かどうかを複合スコアリングで判定。

    4 種の構造シグナルと未来表現の有無で判定:
      構造シグナル:
        1. ドットリーダー行が content の 30%以上
        2. ページ番号行が content の 30%以上
        3. 短い列挙見出し行（60文字以下）が content の 40%以上
        4. 概況見出し行が content に 1 つ以上

    reject 条件:
      - 構造シグナル 2 つ以上 → 即 reject（TOC 確定）
      - 構造シグナル 1 つ + 非 TOC 行に未来表現なし → reject
      - 構造シグナル 0 → 通過

    概況見出し単独 + 未来表現あり → 通過（シグナル 1 + 未来あり）。
    注意: 未来表現は TOC 行以外（本文行）でのみ判定する。
    """
    if not text:
        return True
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return True

    # qualitative/目次/定性的情報/ページ番号/HTMLエンティティ を除外
    content_lines = [l for l in lines if not _TOC_JUNK_RE.match(l)]
    if not content_lines:
        return True

    n = len(content_lines)
    structural_signals = 0

    # Signal 1: ドットリーダー行が 30%以上
    dot_count = sum(1 for l in content_lines if _TOC_LINE_RE.search(l))
    if dot_count >= n * 0.3:
        structural_signals += 1

    # Signal 2: ページ番号行が 30%以上
    page_count = sum(1 for l in content_lines if _PAGE_NUM_LINE_RE.search(l))
    if page_count >= n * 0.3:
        structural_signals += 1

    # Signal 3: 短い列挙見出し行（60文字以下）が 40%以上
    enum_count = sum(
        1 for l in content_lines
        if len(l) <= 60 and _ENUM_HEADING_RE.match(l)
    )
    if enum_count >= n * 0.4:
        structural_signals += 1

    # Signal 4: 概況見出し行が content の 50%以上
    gaikyo_count = sum(1 for l in content_lines if _GAIKYO_HEADING_RE.search(l))
    if gaikyo_count >= n * 0.5:
        structural_signals += 1

    # 構造シグナル 2 つ以上 → reject（TOC 確定）
    return structural_signals >= 2


def _extract_outlook_text(html_text: str) -> str:
    """見通しセクションのテキストを抽出する。

    継続企業・配当区画で打ち切り、品質ガード適用。
    目次・概況見出し断片・未来表現なしの文は reject。
    """
    text = _strip_html_tags(html_text)
    lines = text.split("\n")

    start_idx = None
    is_keiei_seiseki = False
    for i, line in enumerate(lines):
        if _OUTLOOK_HEADING_RE.search(line):
            if _KEIEI_SEISEKI_HEADING_RE.search(line):
                is_keiei_seiseki = True
            start_idx = i
            break
    if start_idx is None:
        return ""

    extracted: list[str] = []
    for i in range(start_idx + 1, min(start_idx + 60, len(lines))):
        line = lines[i].strip()
        if _is_stop_heading(line):
            break
        extracted.append(line)

    result = "\n".join(extracted).strip()

    # 結果全体が目次/概況断片なら reject
    if _is_toc_junk(result):
        return ""

    # 「経営成績に関する分析」: 未来表現を含む段落のみ残す
    if is_keiei_seiseki:
        result = _filter_future_paragraphs(result)

    if not _is_outlook_quality_ok(result):
        return ""
    return result


def _filter_future_paragraphs(text: str) -> str:
    """未来表現を含む段落のみ残す。経営成績分析の過去実績除外用。"""
    paragraphs = re.split(r"\n\s*\n", text)
    future_paras = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # 段落内の各行にも未来表現チェック
        lines = para.split("\n")
        has_future = any(_FUTURE_EXPRESSION_RE.search(line) for line in lines)
        if has_future:
            future_paras.append(para)
    return "\n".join(future_paras).strip()


def _is_stop_heading(line: str) -> bool:
    """打ち切り語の文脈化: 見出し行・節タイトルならTrue、本文中なら継続。

    見出し行の特徴:
    - 短い行（60文字以下）
    - 数字接頭辞付き（「３．」「(5)」等）
    - 句点なし
    """
    if not _OUTLOOK_STOP_RE.search(line):
        return False
    # 短い行 = 見出し行として扱う
    if len(line) <= 60 and "。" not in line:
        return True
    # 数字接頭辞付き = 節タイトル
    if re.match(r'^[\d０-９（(]+[．.）)\s]', line):
        return True
    # 長い行で句点あり = 本文の一部 → 継続
    return False


def _is_outlook_quality_ok(text: str) -> bool:
    """見通しテキストの品質ガード。

    見通し語が複数 or future強語+述語あれば句点なし単行でも採用可。
    名詞断片だけの短文は不採用。
    """
    if len(text) < 15:
        return False
    # 意味のある日本語文字数
    jp_count = sum(1 for c in text if '\u3040' <= c <= '\u9fff' or '\u30a0' <= c <= '\u30ff')
    if jp_count < 8:
        return False
    has_period = "。" in text
    meaningful_lines = [l.strip() for l in text.split("\n") if l.strip()]
    has_multi_lines = len(meaningful_lines) >= 2
    outlook_kw_count = len(_OUTLOOK_KEYWORD_RE.findall(text))
    # 見通し語2つ以上 → OK
    if outlook_kw_count >= 2:
        return True
    # 句点あり or 複数行 → OK
    if has_period or has_multi_lines:
        return True
    # future強語+述語ありの短文 → OK
    has_strong = bool(_OUTLOOK_STRONG_KEYWORD_RE.search(text))
    has_predicate = bool(_PREDICATE_RE.search(text))
    if has_strong and has_predicate:
        return True
    return False


# CSS/HTMLゴミ段落・財務諸表フラグメントの除外パターン
_CSS_NOISE_RE = re.compile(
    r"(font-family|font-size|text-align|margin|padding|float|width|div#|"
    r"\.style_|page-break|div\{|&amp;#160|&#160|"
    r"貸借対照表|損益計算書|包括利益計算書|株主資本等変動計算書|"
    r"キャッシュ・フロー計算書|四半期連結財務諸表)"
)


def _extract_outlook_paragraphs_fallback(text: str) -> str:
    """見出し不在時に見通し語を含む段落を収集する。

    2段階採用:
    - 見通し語2つ以上 → 強採用
    - 見通し語1つでも強語なら候補採用
    """
    plain = _strip_html_tags(text)
    paragraphs = re.split(r"\n\s*\n", plain)
    candidates: list[str] = []
    for para in paragraphs:
        para = para.strip()
        if not para or len(para) < 30:
            continue
        # 段落全体が打ち切り見出し → 除外（本文中なら継続）
        first_line = para.split('\n')[0].strip()
        if _is_stop_heading(first_line):
            continue
        # 段落に打ち切り語があり未来表現がない → 除外（配当/株主還元段落等）
        if _OUTLOOK_STOP_RE.search(para) and not _FUTURE_EXPRESSION_RE.search(para):
            continue
        # CSS/HTMLゴミ段落を除外
        if _CSS_NOISE_RE.search(para):
            continue
        keywords = _OUTLOOK_KEYWORD_RE.findall(para)
        strong_keywords = _OUTLOOK_STRONG_KEYWORD_RE.findall(para)
        # 2段階採用
        if len(keywords) >= 2:
            candidates.append(para)
        elif len(keywords) >= 1 and len(strong_keywords) >= 1:
            candidates.append(para)
    if not candidates:
        return ""
    result = "\n".join(candidates[:3])  # 最大3段落
    if not _is_outlook_quality_ok(result):
        return ""
    return result


# ============================================================
# make_fallback_summary
# ============================================================

_NOISE_LINE_RE = re.compile(r"^\s*-\s*\d+\s*-\s*$")
_RULED_LINE_RE = re.compile(r"^[━─═＝\-=]{3,}")
_HEADING_LINE_RE = re.compile(
    r"^[\d０-９]*[．.\s]*(?:今後の見通し|見通し|業績予想)"
)
# summary 除外: 過去実績文・財務諸表名・HTMLエンティティ
_SUMMARY_SKIP_RE = re.compile(
    r"(&#\d+;|&amp;|&nbsp;|&lt;|&gt;"
    r"|貸借対照表|損益計算書|包括利益計算書|株主資本等変動計算書"
    r"|キャッシュ・フロー計算書|四半期連結財務諸表"
    r"|添付資料の目次|連結注記表|個別注記表"
    r"|font-family|font-size|text-align|div#|page-break"
    r"|\.root\s*\{|width:\s*\d)"
)
_PAST_TENSE_RE = re.compile(
    r"(となりました|でありました|いたしました|推移しました"
    r"|結果となりました|計上しました|増加しました|減少しました"
    r"|に至りました|記録しました|実施しました)"
)


def make_fallback_summary(outlook_text: str, max_len: int = 200) -> str:
    """見通しテキストをノイズ除去して短縮する。

    過去実績文・財務諸表名・HTMLエンティティを除外し、
    未来表現を含む文を優先して要約する。
    """
    lines = outlook_text.strip().split("\n")
    clean: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if _HEADING_LINE_RE.match(line):
            continue
        if _NOISE_LINE_RE.match(line):
            continue
        if _RULED_LINE_RE.match(line):
            continue
        # HTMLエンティティ・財務諸表名・CSSを含む行は除外
        if _SUMMARY_SKIP_RE.search(line):
            continue
        clean.append(line)

    if not clean:
        return ""

    # 文単位で分割して未来表現を含む文を優先
    text = "\n".join(clean).strip()
    text = re.sub(r"[ \u3000]{2,}", " ", text)

    # 句点で分割して文ごとに判定
    sentences = re.split(r"(?<=。)", text)
    future_sents: list[str] = []
    other_sents: list[str] = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if _PAST_TENSE_RE.search(s) and not _FUTURE_EXPRESSION_RE.search(s):
            continue  # 過去実績のみの文はスキップ
        if _FUTURE_EXPRESSION_RE.search(s):
            future_sents.append(s)
        else:
            other_sents.append(s)

    # 未来文を優先、足りなければ他の文を追加
    result_sents = future_sents[:3]
    remaining = max_len - sum(len(s) for s in result_sents)
    for s in other_sents:
        if remaining <= 0:
            break
        if len(s) <= remaining:
            result_sents.append(s)
            remaining -= len(s)

    result = "".join(result_sents).strip()
    if len(result) > max_len:
        result = result[:max_len - 1] + "…"
    return result


# ============================================================
# format_guidance_section
# ============================================================

def _fmt_oku_yen(val: float) -> str:
    """円単位の値を億円表記にフォーマット。"""
    oku = val / 1e8
    if abs(oku) >= 10:
        return f"{oku:,.0f}億円"
    return f"{oku:,.1f}億円"


def _fmt_yoy(ratio: float | None) -> str:
    if ratio is None:
        return ""
    sign = "+" if ratio >= 0 else ""
    return f"{sign}{ratio * 100:.1f}%"


def format_guidance_section(guidance: GuidanceData, clip: float | None = None) -> str:
    """通知用ガイダンスセクションを生成する。

    ガイダンスも見通しもなければ空文字を返す。
    """
    if not guidance.has_guidance:
        return ""

    lines = ["■ 来期ガイダンス"]
    if guidance.sales_forecast is not None:
        line = f"売上: {_fmt_oku_yen(guidance.sales_forecast)}"
        if guidance.sales_yoy is not None:
            line += f"（YOY {_fmt_yoy(guidance.sales_yoy)}）"
        lines.append(line)
    if guidance.op_forecast is not None:
        line = f"OP: {_fmt_oku_yen(guidance.op_forecast)}"
        if guidance.op_yoy is not None:
            line += f"（YOY {_fmt_yoy(guidance.op_yoy)}）"
        lines.append(line)
    if guidance.eps_forecast is not None:
        line = f"EPS: {guidance.eps_forecast}円"
        if guidance.eps_yoy is not None:
            line += f"（YOY {_fmt_yoy(guidance.eps_yoy)}）"
        lines.append(line)

    return "\n".join(lines)


# ============================================================
# HTML バイト列のエンコーディング自動検出
# ============================================================

def _decode_html_bytes(raw: bytes) -> str:
    """HTMLバイト列をデコードする。"""
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    head = raw[:2000].lower()
    m = re.search(rb'charset[="\s]+([a-z0-9_-]+)', head)
    if m:
        cs = m.group(1).decode("ascii", errors="ignore")
        cs_map = {"shift_jis": "cp932", "sjis": "cp932", "x-sjis": "cp932",
                   "euc-jp": "euc_jp", "iso-2022-jp": "iso2022_jp"}
        cs = cs_map.get(cs, cs)
        try:
            return raw.decode(cs, errors="replace")
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return raw.decode("cp932")
    except UnicodeDecodeError:
        pass
    return raw.decode("utf-8", errors="replace")


# ============================================================
# XBRL Forecast 候補収集
# ============================================================

def _apply_scale(text: str, scale: str, sign: str) -> float | None:
    """iXBRL scale/sign を適用して float を返す。"""
    s = unicodedata.normalize("NFKC", text).strip().replace(",", "")
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    if scale:
        try:
            val = val * (10 ** int(scale))
        except (ValueError, OverflowError):
            pass
    if sign == "-" and val > 0:
        val = -val
    return val


def _collect_forecast_candidates_from_bytes(raw: bytes) -> list[dict]:
    """XBRL/iXBRL バイト列から Forecast 候補を収集する。"""
    try:
        # XBRL クリーン処理
        try:
            from src.xbrl_clean import read_xbrl_bytes
            xml_str = read_xbrl_bytes(raw)
        except ImportError:
            xml_str = raw.decode("utf-8", errors="replace")
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []

    candidates: list[dict] = []

    # ---- Pass 1: 従来 XBRL ----
    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        tag_local = tag.split("}")[-1] if "}" in tag else tag
        ctx = elem.get("contextRef", "")
        if "ForecastMember" not in ctx:
            continue

        # 財務指標
        if tag_local in _FORECAST_TAG_MAP:
            field_name = _FORECAST_TAG_MAP[tag_local]
            val_text = (elem.text or "").strip()
            if not val_text:
                continue
            s = unicodedata.normalize("NFKC", val_text).replace(",", "")
            try:
                val = float(s)
            except ValueError:
                continue
            is_consol = "Consolidated" in ctx and "NonConsolidated" not in ctx
            candidates.append({
                "metric": field_name,
                "value": val,
                "period_type": _classify_period_type(ctx),
                "horizon": _classify_horizon(ctx),
                "is_consol": is_consol,
                "is_basic": True,
                "ctx": ctx,
                "tag": tag_local,
                "source": "xbrl",
            })

        # EPS
        if tag_local in _EPS_ALL_TAGS:
            val_text = (elem.text or "").strip()
            if not val_text:
                continue
            s = unicodedata.normalize("NFKC", val_text).replace(",", "")
            try:
                val = float(s)
            except ValueError:
                continue
            if abs(val) > _EPS_ABSURD_THRESHOLD:
                continue
            is_consol = "Consolidated" in ctx and "NonConsolidated" not in ctx
            is_basic = tag_local in _EPS_BASIC_TAGS
            candidates.append({
                "metric": "eps",
                "value": val,
                "period_type": _classify_period_type(ctx),
                "horizon": _classify_horizon(ctx),
                "is_consol": is_consol,
                "is_basic": is_basic,
                "ctx": ctx,
                "tag": tag_local,
                "source": "xbrl",
            })

    if candidates:
        return candidates

    # ---- Pass 2: iXBRL ----
    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        tag_local = tag.split("}")[-1] if "}" in tag else tag
        if tag_local != "nonFraction":
            continue

        concept = elem.get("name", "")
        ctx = elem.get("contextRef", "")
        scale = elem.get("scale", "")
        sign = elem.get("sign", "")
        if not concept or "ForecastMember" not in ctx:
            continue

        concept_local = concept.split(":")[-1] if ":" in concept else concept

        text = (elem.text or "").strip()
        if not text:
            text = "".join(elem.itertext()).strip()
        if not text:
            continue

        # 財務指標
        if concept_local in _FORECAST_TAG_MAP:
            field_name = _FORECAST_TAG_MAP[concept_local]
            val = _apply_scale(text, scale, sign)
            if val is None:
                continue
            is_consol = "Consolidated" in ctx and "NonConsolidated" not in ctx
            candidates.append({
                "metric": field_name,
                "value": val,
                "period_type": _classify_period_type(ctx),
                "horizon": _classify_horizon(ctx),
                "is_consol": is_consol,
                "is_basic": True,
                "ctx": ctx,
                "tag": concept_local,
                "source": "ixbrl",
            })

        # EPS
        if concept_local in _EPS_ALL_TAGS:
            val = _apply_scale(text, scale, sign)
            if val is None or abs(val) > _EPS_ABSURD_THRESHOLD:
                continue
            is_consol = "Consolidated" in ctx and "NonConsolidated" not in ctx
            is_basic = concept_local in _EPS_BASIC_TAGS
            candidates.append({
                "metric": "eps",
                "value": val,
                "period_type": _classify_period_type(ctx),
                "horizon": _classify_horizon(ctx),
                "is_consol": is_consol,
                "is_basic": is_basic,
                "ctx": ctx,
                "tag": concept_local,
                "source": "ixbrl",
            })

    return candidates


# ============================================================
# プレーンテキスト抽出（ZIP内 HTML → テキスト）
# ============================================================

def _extract_plain_text_from_zip(zf: zipfile.ZipFile) -> str:
    """ZIP 内の HTML/iXBRL からプレーンテキストを抽出する。"""
    texts: list[str] = []
    for name in zf.namelist():
        bn = os.path.basename(name).lower()
        if not bn.endswith((".htm", ".html")):
            continue
        try:
            raw = zf.read(name)
            html = _decode_html_bytes(raw)
            plain = _strip_html_tags(html)
            if plain.strip():
                texts.append(plain)
        except Exception:
            continue
    return "\n".join(texts)


# ============================================================
# extract_guidance_from_zip
# ============================================================

def extract_guidance_from_zip(
    xbrl_path: str,
    actual_sales: int | None = None,
    actual_op: int | None = None,
) -> GuidanceData:
    """XBRL ZIP から来期ガイダンスデータを抽出する。"""
    guidance = GuidanceData(
        sales_actual=actual_sales,
        op_actual=actual_op,
    )

    try:
        raw = Path(xbrl_path).read_bytes()
    except Exception as e:
        logger.warning(f"[GUIDANCE] file read failed: {e}")
        return guidance

    if raw[:4] != _ZIP_SIG:
        logger.warning("[GUIDANCE] not a ZIP file")
        return guidance

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except zipfile.BadZipFile:
        logger.warning("[GUIDANCE] bad ZIP file")
        return guidance

    # ---- XBRL/iXBRL 候補収集 ----
    all_candidates: list[dict] = []
    plain_text = ""
    outlook_html = ""

    for name in zf.namelist():
        bn = os.path.basename(name).lower()
        is_ixbrl = any(bn.endswith(ext) for ext in _IXBRL_EXTS)
        is_xbrl = any(bn.endswith(ext) for ext in _XBRL_EXTS)
        if not is_ixbrl and not is_xbrl:
            continue
        try:
            entry_bytes = zf.read(name)
            cands = _collect_forecast_candidates_from_bytes(entry_bytes)
            all_candidates.extend(cands)
        except Exception as e:
            logger.debug(f"[GUIDANCE] parse failed: {name}: {e}")

    # ---- プレーンテキスト + Outlook HTML ----
    for name in zf.namelist():
        bn = os.path.basename(name).lower()
        if not bn.endswith((".htm", ".html")):
            continue
        try:
            entry_bytes = zf.read(name)
            html = _decode_html_bytes(entry_bytes)
            pt = _strip_html_tags(html)
            if pt.strip():
                plain_text += "\n" + pt
            if not outlook_html and _OUTLOOK_HEADING_RE.search(html):
                outlook_html = html
        except Exception:
            continue

    zf.close()

    # ---- EPS テキスト fallback ----
    # XBRL に EPS 候補がある場合はテキストfallback不要
    has_xbrl_eps = any(
        c["metric"] == "eps" and c.get("source") in ("xbrl", "ixbrl")
        for c in all_candidates
    )
    if plain_text and not has_xbrl_eps:
        text_eps = _extract_eps_from_forecast_table(plain_text)
        # suspicious 値（> 1000）は除外
        clean_text_eps = [c for c in text_eps if not c.get("suspicious")]
        if clean_text_eps:
            all_candidates.extend(clean_text_eps)
            logger.info(
                f"[GUIDANCE] text fallback EPS: {len(clean_text_eps)} candidates "
                f"(filtered {len(text_eps) - len(clean_text_eps)} suspicious)"
            )
        elif text_eps:
            logger.warning(
                f"[GUIDANCE] text fallback EPS: all {len(text_eps)} candidates "
                f"were suspicious (> {_EPS_SUSPICIOUS_THRESHOLD}), dropped"
            )

    # ---- ベスト候補選択 ----
    if all_candidates:
        best = _select_best_candidates(all_candidates)
        guidance.sales_forecast = best.get("sales")
        guidance.op_forecast = best.get("operating_profit")
        guidance.ordinary_forecast = best.get("ordinary_profit")
        guidance.net_income_forecast = best.get("net_income")
        guidance.eps_forecast = best.get("eps")

    # ---- Outlook 抽出は無効化済み ----
    # outlook_text / outlook_summary / outlook_factors は互換用に空値を維持
    # (抽出ロジック自体は関数として残存しているが呼び出さない)

    logger.info(
        f"[GUIDANCE] extracted: sales={guidance.sales_forecast} "
        f"op={guidance.op_forecast} eps={guidance.eps_forecast} "
        f"outlook_len={len(guidance.outlook_text)}"
    )
    return guidance
