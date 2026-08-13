"""lib/pipeline/source_priority.py -- source 優先順位マッピング"""
from __future__ import annotations

# ============================================================
# Source Priority Map
# ============================================================
# 小さいほど高優先。勝者判定で source_priority ASC を使う。
#
# jquants 優先度について:
#   J-Quants は TSE 公式 PL 集計データ（証券取引所由来）。
#   sales / operating_profit 等の PL 主要値は高信頼性。
#   gross_profit / sga 等の補完値は jquants に存在しないことが多く
#   その場合は tdnet 等が自動採用される（競合なし）。
#
# priority 一覧 (昇順 = 高優先):
#   0: backfill_v4_pdf / v4_pdf  ← XBRL partial 判定で PDF V4 採用済み (XBRL より信頼)
#   1: summary_xbrl / xbrl
#   2: attachment_xbrl / backfill_xbrl / jquants / edinet_xbrl / edinet
#   3: html / html_table / tdnet
#   4: pdf / pdf_table
#   5: excel_legacy / legacy_excel

# Manually verified official-PDF repairs retain explicit provenance. A later
# official correction outranks the superseded J-Quants row; an uncorrected
# official PDF stays at normal TDnet priority.

SOURCE_PRIORITY: dict[str, int] = {
    # ── financials source ──
    "summary_xbrl": 1,
    "tdnet_xbrl": 1,
    "attachment_xbrl": 2,
    "jquants": 2,        # J-Quants: TSE公式集計データ → attachment_xbrl と同格
    "official_pdf_correction": 1,
    "official_pdf": 3,
    "html_table": 3,
    "pdf_table": 4,
    "legacy_excel": 5,
    # Forecast sources intentionally share one priority. Forecast winner
    # selection is disclosure-time first; source is only a deterministic tie.
    "jquants_nxf": 10,
    "jquants_forecast_fy": 10,
    "jquants_forecast_next_fy": 10,
    "jquants_forecast": 10,
    # ── segment source (実データ値) ──
    # SegmentRawRow.source: 'xbrl' | 'html' | 'pdf' | 'tdnet'
    # xbrl は summary_xbrl 相当、html/pdf はそれぞれ html_table/pdf_table 相当
    # aliases / backward compat
    #
    # ── XBRL partial fallback 採用済み PDF V4 (priority=0) ──
    # XBRL が partial success と判定され、PDF V4 が xbrl より多くのセグメントを
    # 抽出した場合に採用される。「XBRL より信頼できる」と自動判定済みなので
    # xbrl(1) より高優先の 0 とする。
    # 通常の PDF 抽出一般 ("pdf": 4) は変更しない。
    "backfill_v4_pdf": 0,
    "v4_pdf": 0,
    "xbrl": 1,
    # ── Backfill / historical XBRL source ──
    # backfill_xbrl: XBRL ZIP から後処理抽出したデータ (attachment_xbrl 相当)
    # excel_legacy(5) より高優先、summary_xbrl(1) より低優先
    "backfill_xbrl": 2,
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
