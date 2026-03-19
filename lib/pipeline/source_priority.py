"""lib/pipeline/source_priority.py -- source 優先順位マッピング"""
from __future__ import annotations

# ============================================================
# Source Priority Map
# ============================================================
# 小さいほど高優先。勝者判定で source_priority ASC を使う。

SOURCE_PRIORITY: dict[str, int] = {
    # ── financials source ──
    "summary_xbrl": 1,
    "attachment_xbrl": 2,
    "html_table": 3,
    "pdf_table": 4,
    "legacy_excel": 5,
    "jquants": 6,
    # ── segment source (実データ値) ──
    # SegmentRawRow.source: 'xbrl' | 'html' | 'pdf' | 'tdnet'
    # xbrl は summary_xbrl 相当、html/pdf はそれぞれ html_table/pdf_table 相当
    # aliases / backward compat
    "xbrl": 1,
    # ── EDINET source ──
    # EDINET XBRL は TDnet XBRL (priority=1) の次、HTML/PDF より上
    "edinet_xbrl": 2,
    "edinet": 2,
    # ── TDnet 他 ──
    "html": 3,
    "pdf": 4,
    "tdnet": 3,
    "excel_legacy": 5,
}

# デフォルト (未知 source)
DEFAULT_PRIORITY = 99


def get_priority(source: str) -> int:
    """source 文字列から優先順位を返す。"""
    return SOURCE_PRIORITY.get(source, DEFAULT_PRIORITY)


def is_higher_priority(source_a: str, source_b: str) -> bool:
    """source_a が source_b より高優先 (小さい) か。"""
    return get_priority(source_a) < get_priority(source_b)


def all_sources_ordered() -> list[tuple[str, int]]:
    """(source, priority) を priority 昇順で返す (重複排除)。"""
    seen: set[int] = set()
    result = []
    for src, pri in sorted(SOURCE_PRIORITY.items(), key=lambda x: x[1]):
        if pri not in seen:
            result.append((src, pri))
            seen.add(pri)
    return result
