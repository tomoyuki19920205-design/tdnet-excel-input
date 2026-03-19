"""lib/backfill/filing_selector.py — 決算短信判定 / 説明資料除外 / 訂正除外

バックフィル対象の最終判定を一箇所に集中させるモジュール。
tdnet_html.py 等の listing source は一覧取得に徹し、
最終判定は必ず本モジュールを通す。

Usage::

    from lib.backfill.filing_selector import should_process_for_segment_backfill

    ok, reason = should_process_for_segment_backfill(
        title=filing.title,
        exclude_corrections=True,
    )
"""
from __future__ import annotations

import re
import unicodedata


# ============================================================
# 正規化
# ============================================================

def normalize_title(title: str) -> str:
    """タイトル正規化 (判定前の共通前処理).

    - 前後空白除去
    - 改行 → 空白
    - 全角空白 → 半角空白
    - 連続空白を 1 つに圧縮
    - Unicode NFKC 正規化
    - 英字を小文字化
    """
    s = title.strip()
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("\u3000", " ")  # 全角空白
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s)
    return s.lower()


# ============================================================
# 訂正判定
# ============================================================

_CORRECTION_KEYWORDS = [
    "訂正",
    "数値データ訂正",
    "一部訂正",
    "correction",
    "amendment",
]


def is_correction_title(title: str) -> bool:
    """訂正資料かどうか判定."""
    n = normalize_title(title)
    return any(kw in n for kw in _CORRECTION_KEYWORDS)


# ============================================================
# 説明資料 / 補足資料 / プレゼン除外
# ============================================================

_EXCLUDED_MATERIAL_KEYWORDS = [
    "説明資料",
    "決算説明資料",
    "補足説明資料",
    "補足資料",
    "プレゼン",
    "presentation",
    "briefing",
    "factbook",
    "fact book",
    "supplementary",
    "supplemental",
    "月次",
    "参考資料",
    "データ集",
    "業績予想修正",
]


def is_excluded_material_title(title: str) -> bool:
    """説明資料 / 補足資料 / プレゼン等の除外対象かどうか判定."""
    n = normalize_title(title)
    return any(kw in n for kw in _EXCLUDED_MATERIAL_KEYWORDS)


# ============================================================
# 決算短信判定
# ============================================================

_EARNINGS_SUMMARY_KEYWORDS = [
    "決算短信",
    "四半期決算短信",
]
# Note: "四半期決算短信" は "決算短信" を含むため、
# "決算短信" の 1 キーワードで FY/Q 両方をカバーする。
# ただし明示性のために両方残す。


def is_earnings_summary_title(title: str) -> bool:
    """決算短信かどうか判定.

    「第1四半期決算短信」「通期決算短信」「決算短信〔IFRS〕」等を含む。
    """
    n = normalize_title(title)
    return any(kw in n for kw in _EARNINGS_SUMMARY_KEYWORDS)


# ============================================================
# 総合判定
# ============================================================

def should_process_for_segment_backfill(
    title: str,
    *,
    exclude_corrections: bool = True,
    only_earnings_summary: bool = True,
) -> tuple[bool, str]:
    """バックフィル対象かどうかの最終判定.

    Returns:
        (accepted: bool, reason: str)
        reason は dry-run 集計やログ出力で使用する。

    判定順 (仕様書 §4):
        1. normalize_title
        2. correction 判定
        3. 説明資料 / 補足資料 / presentation 判定
        4. 決算短信判定
        5. 不明なら除外
    """
    # 1. 正規化 (normalize_title は各判定関数で呼ばれるが明示)
    # 2. 訂正判定
    if exclude_corrections and is_correction_title(title):
        return False, "excluded_correction"

    # 3. 説明資料 / 補足資料 / presentation 判定
    if is_excluded_material_title(title):
        return False, "excluded_presentation"

    # 4. 決算短信判定
    if only_earnings_summary:
        if is_earnings_summary_title(title):
            return True, "included_earnings_summary"
        else:
            return False, "excluded_non_earnings_summary"

    # only_earnings_summary=False → financial_statement 全般を受け入れ
    # ただし上記で除外されなかったもの
    return True, "included_financial_statement"
