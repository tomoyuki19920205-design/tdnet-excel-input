"""
セグメント名正規化 + 特殊行分類

SEGMENT_EXTRACTION_SPEC §6, §7 に基づく。
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
