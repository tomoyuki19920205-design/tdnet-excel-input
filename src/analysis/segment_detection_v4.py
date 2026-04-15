"""
segment_detection_v4.py — シンプル決め打ち横型セグメント抽出器 v4
=====================================================================

設計方針:
  - v2/v3 の構造的問題（text優先・左端依存・多段fallback・bypass暴発）を完全排除
  - 横型セグメント表を前提とした「決め打ち抽出」
  - 目次＋ワードベースによるページ特定
  - pdfplumber.extract_tables() のみ使用（textは補助的にのみ利用）
  - 前期・当期の2期間ブロックを独立抽出

フロー:
  Phase 0: 目次探索
  Phase 1: 候補ページ探索（後方 +1〜+4）
  Phase 2: ワードベースフィルタ
  Phase 3: table抽出
  Phase 3b: 期間ブロック検出（表内・ページテキスト）
  Phase 4: 横型表判定
  Phase 5: segment name取得（2行スパンヘッダー対応）
  Phase 6: 売上（計優先）・利益抽出
  Phase 7: record組立
  Phase 8: 最小検証

CLI:
  python src/analysis/segment_detection_v4.py --sample-dir <dir>
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

logger = logging.getLogger("tdnet.v4")


# ==============================================================
# 定数
# ==============================================================

# Phase 0: 目次ページ検出キーワード
_TOC_PAGE_KEYWORDS: list[str] = ["目次", "Contents"]

# Phase 0: 目次内セグメント行キーワード
_TOC_SEGMENT_KEYWORDS: list[str] = [
    "セグメント",
    "事業別",
    "報告セグメント",
    "Segment",
]

# Phase 2: セグメントページ判定語彙 A（segmentish）
# 「セグメント」1本に依存せず、報告セグメント関連の汎用語を OR 判定する
_SEGMENT_PAGE_KEYWORDS: tuple[str, ...] = (
    "セグメント",
    "報告セグメント",
    "セグメント情報",
    "セグメント利益",
    "セグメント損失",
    "セグメント利益又は損失",
    "Segment",
    "事業別",
)

# Phase 2: セグメントページ判定語彙 B（metricish）
# A とのAND条件で「表ページであること」を担保する
_SEGMENT_METRIC_KEYWORDS: tuple[str, ...] = (
    "売上高",
    "売上収益",
    "利益",
    "又は損失",
    "合計",
    "調整額",
    "外部顧客への売上高",
    "報告セグメント",
)

# Phase 2: セグメントらしいワード（スコア加点）
_WORD_SEGMENT_LIKE: list[str] = [
    "セグメント", "報告セグメント", "Segment", "事業別",
    "売上", "売上高", "利益", "営業利益", "セグメント利益", "事業利益",
    "外部顧客", "全社", "消去", "調整額", "その他", "事業",
]

# Phase 2: 除外ワード（BS/PL系）
_WORD_EXCLUDE: list[str] = [
    "貸借対照表", "Balance Sheet", "資産", "負債", "純資産",
    "損益計算書", "PL", "包括利益",
]

# Phase 4: 横型表の左端ヘッダー検出ワード
_HORIZ_LEFT_KEYWORDS: list[str] = ["売上", "利益", "計", "合計"]

# Phase 5: segment name 除外語（部分一致）
# 注: 「その他」は正規の報告セグメント名として頻出するため除外しない
_SEG_NAME_EXCLUDE: frozenset[str] = frozenset([
    "売上", "売上高", "売上収益", "利益", "営業利益", "セグメント利益", "事業利益",
    "計", "合計", "報告セグメント計", "全社", "消去", "調整", "調整額",
    "合計・消去", "合計/消去",
    "外部顧客", "内部", "交点",
])

# Phase 5: 追加カテゴリ除外語
_SEG_HEADER_CATEGORY_EXCLUDE: frozenset[str] = frozenset([
    "報告セグメント",
    "連結財務諸表計上額",
    "中間連結損益計算書計上額",
    "計算書計上額",
    "財務諸表計上額",
    "セグメント間",
])

# Phase 6: 売上行 優先キーワード（最高優先）
_SALES_PRIMARY_KEYWORDS: list[str] = ["計", "合計"]

# Phase 6: 売上行 fallback キーワード
_SALES_FALLBACK_KEYWORDS: list[str] = [
    "外部顧客への売上高",
    "外部顧客",
    "外部収益",
    "外部売上",
    "売上収益",   # IFRS採用企業（9983ユニクロ等）: "売上収益" 形式
]

# Phase 6: 売上行 除外キーワード（これらは売上行に採用禁止）
_SALES_ROW_EXCLUDE: list[str] = [
    "一時点で移転される財",
    "一定の期間にわたり移転される財",
    "顧客との契約から生じる収益",
    "その他の収益",
]

# Phase 6: 利益行キーワード
_PROFIT_ROW_KEYWORDS: list[str] = [
    "セグメント利益",
    "セグメント損益",   # 6701 日本電気: "セグメント損益" 形式
    "利益",
    "営業利益",
    "事業利益",
]

# Phase 3b: 期間ブロック検出パターン
_PERIOD_PREV_RE = re.compile(
    r"前(第[1-4１-４]四半期|中間|連結会計年度|事業年度|年度)"
)
_PERIOD_CURR_RE = re.compile(
    r"当(第[1-4１-４]四半期|中間|連結会計年度|事業年度|年度)"
)
_PERIOD_ANY_RE = re.compile(
    r"(前|当)(第[1-4１-４]四半期|中間|連結会計年度|事業年度|年度)"
)

# Phase 5: セグメント名が数値っぽいかを判定するパターン
# 数字・カンマ・符号・括弧のみで構成されるもの、または数字比率が高いものを検出
_NUMERIC_LIKE_HEADER_RE = re.compile(
    r"^[\d,△▲▽\-\u2212\.\(\)（）\+０-９＋％×÷±\s]+$"
)
# 数値比率の閾値（この割合以上の行はヘッダーとして不採用）
_HEADER_NUMERIC_RATIO_THRESHOLD: float = 0.4

# 数値パターン
_NUM_RE = re.compile(r"^[\s\d,△▲\-\u2212\.\(\)（）▽]+$")

# 目次行末ページ番号抽出パターン
_TOC_PAGE_NUM_RE = re.compile(r"(\d+)\s*$")


# ==============================================================
# データクラス
# ==============================================================

@dataclass
class SegmentRecordV4:
    """v4で抽出されたセグメント1件"""
    segment_name: str = ""
    segment_order: int = 0
    segment_sales: float | None = None
    segment_profit: float | None = None
    raw_profit_label: str = ""
    extraction_engine: str = "v4"


@dataclass
class PeriodResultV4:
    """前期/当期 1ブロック分の抽出結果"""
    period_type: str = ""        # "previous" | "current" | "unknown"
    period_label: str = ""       # 例: 当第３四半期連結累計期間
    page_index_0based: int = -1
    page_index_1based: int = -1
    sales_row_label: str = ""
    profit_row_label: str = ""
    segments: list[SegmentRecordV4] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.segments) >= 2


@dataclass
class V4DetectionResult:
    """v4 検出の全体結果"""
    # 後方互換: current period のセグメント一覧（または最良候補）
    segments: list[SegmentRecordV4] = field(default_factory=list)
    # 全期間の結果
    extracted_periods: list[PeriodResultV4] = field(default_factory=list)
    quarantine_reason: str = ""
    failed_stage: str = ""
    rule_trace: list[str] = field(default_factory=list)
    log: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return (
            (len(self.segments) >= 2 or len(self.extracted_periods) >= 1)
            and not self.quarantine_reason
        )


# ==============================================================
# ユーティリティ
# ==============================================================

def _cell_str(cell: Any) -> str:
    return (cell or "").strip()


def _is_numeric_cell(cell: Any) -> bool:
    s = _cell_str(cell).replace(" ", "").replace("\u3000", "")
    if not s:
        return False
    return bool(_NUM_RE.match(s))


def _parse_num(cell: Any) -> float | None:
    s = _cell_str(cell)
    # 改行区切り複合セル（例: '357,700\n△\n△'）対応
    # → 改行で分割して先頭から順に解析し、最初に成功した値を返す
    raw_str = (cell or "").strip()
    if "\n" in raw_str:
        for part in raw_str.split("\n"):
            v = _parse_num(part.strip())
            if v is not None:
                return v
        return None

    s = raw_str
    s = s.replace(",", "").replace(" ", "").replace("\u3000", "")
    s = s.replace("△", "-").replace("▲", "-").replace("▽", "-")
    s = s.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    s = s.replace("\u2212", "-")
    if not s or s in ("-", "—", "－", "―"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_seg_name(name: str) -> str:
    return re.sub(r"[\s\u3000]+", "", name).strip()


def _is_category_cell(name: str) -> bool:
    if any(exc in name for exc in _SEG_NAME_EXCLUDE):
        return True
    if any(exc in name for exc in _SEG_HEADER_CATEGORY_EXCLUDE):
        return True
    return False


def _is_numeric_like(text: str) -> bool:
    """
    セグメント名候補として不適切な「数値っぽい」文字列かどうかを判定。

    検出対象:
      - 数字・カンマ・符号・括弧のみで構成されるもの: '47,840' '△123' '(123)'
      - 数字と記号が結合したもの: '47,8401,187' '130,041203'
      - 数字比率が 70% 以上のもの
    """
    t = text.strip()
    if not t:
        return False
    # パターン完全一致
    if _NUMERIC_LIKE_HEADER_RE.match(t):
        return True
    # 文字に占める数字・カンマ・符号の比率
    numeric_chars = sum(
        1 for c in t
        if c.isdigit() or c in ',.\u25b3\u25b2\u25bd\uff08\uff09()\u2212+-\uff0b\uff0d'
    )
    if len(t) > 0 and numeric_chars / len(t) >= 0.7:
        return True
    return False


def _row_numeric_ratio(row: list[Any], skip_col0: bool = True) -> float:
    """
    行内の非空セルのうち numeric-like セルの割合を返す。
    skip_col0=True のとき col 0（左端ラベル列）は除外する。
    """
    cells = [_cell_str(c) for c in (row[1:] if skip_col0 else row)]
    non_empty = [c for c in cells if c]
    if not non_empty:
        return 0.0
    numeric_count = sum(1 for c in non_empty if _is_numeric_like(_normalize_seg_name(c)))
    return numeric_count / len(non_empty)


# ==============================================================
# Phase 0: 目次探索
# ==============================================================

def _phase0_find_toc_page_number(
    pdf: pdfplumber.PDF,
    log: dict[str, Any],
) -> tuple[int | None, int | None]:
    """
    目次ページを検出し、セグメント関連行のページ番号 (冊子番号) を返す。

    ページ番号の基準（v4 固定定義）:
      phys_idx         = pdfplumber が返す 0始まりの物理インデックス
      toc_booklet_page = 目次テキスト内に印刷されている冊子ページ番号（1始まり）

    Returns:
        (toc_booklet_page, toc_physical_idx)
        検出失敗時は (None, None)
    """
    toc_physical_idx: int | None = None
    toc_booklet_page: int | None = None
    toc_detected = False
    matched_line: str = ""

    for phys_idx, page in enumerate(pdf.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            continue

        is_toc = any(kw in text for kw in _TOC_PAGE_KEYWORDS)
        if not is_toc:
            continue

        lines = text.split("\n")
        for line in lines:
            if not any(kw in line for kw in _TOC_SEGMENT_KEYWORDS):
                continue
            m = _TOC_PAGE_NUM_RE.search(line.strip())
            if m:
                toc_booklet_page = int(m.group(1))
                toc_physical_idx = phys_idx
                toc_detected = True
                matched_line = line.strip()[:80]
                logger.debug(
                    "[v4] Phase0: toc_detected phys_0based=%d phys_1based=%d "
                    "toc_printed_page=%d line=%r",
                    phys_idx, phys_idx + 1, toc_booklet_page, matched_line,
                )
                break

        if toc_detected:
            break

    log["toc_detected"] = toc_detected
    log["toc_printed_page_number"] = toc_booklet_page
    log["toc_booklet_page"] = toc_booklet_page
    log["toc_page_index_0based"] = toc_physical_idx
    log["toc_page_index_1based"] = (
        toc_physical_idx + 1 if toc_physical_idx is not None else None
    )
    log["toc_physical_page_idx"] = toc_physical_idx
    log["toc_matched_line"] = matched_line

    return toc_booklet_page, toc_physical_idx


# ==============================================================
# Phase 1: 候補ページ探索
# ==============================================================

def _phase1_candidate_pages(
    pdf: pdfplumber.PDF,
    toc_booklet_page: int | None,
    toc_physical_idx: int | None,
    log: dict[str, Any],
) -> list[int]:
    """
    目次記載ページ番号から物理ページ候補を生成する。

    ページ番号の基準（v4 固定定義）:
      candidate_pages の各要素 = 0始まりの物理インデックス
      toc_booklet_page          = 目次記載の冊子番号（1始まり）
      computed_base_0based      = toc_booklet_page - 1（単純変換）

    探索対象: base-1 〜 base+3（TOC→物理ページの ±1 ずれを安全吸収）
    """
    total_pages = len(pdf.pages)

    if toc_booklet_page is None or toc_physical_idx is None:
        candidate_pages = list(range(total_pages))
        log["candidate_pages"] = candidate_pages
        log["candidate_pages_1based"] = [p + 1 for p in candidate_pages]
        log["candidate_source"] = "all_pages_fallback"
        return candidate_pages

    # 冊子番号 → 0始まり物理インデックス（単純変換）
    computed_base_0based = toc_booklet_page - 1

    candidate_pages = []
    candidate_detail: list[dict] = []
    # delta -1〜+3: 前方1ページを追加して ±1 ずれを吸収（back1_forward3）
    for delta in range(-1, 4):
        p_0based = computed_base_0based + delta
        if 0 <= p_0based < total_pages:
            candidate_pages.append(p_0based)
            candidate_detail.append({
                "delta": delta,
                "page_index_0based": p_0based,
                "page_index_1based": p_0based + 1,
            })

    if not candidate_pages:
        for p_0based in range(
            max(0, toc_physical_idx + 1),
            min(total_pages, toc_physical_idx + 5),
        ):
            candidate_pages.append(p_0based)
            candidate_detail.append({
                "delta": p_0based - toc_physical_idx,
                "page_index_0based": p_0based,
                "page_index_1based": p_0based + 1,
            })

    log["candidate_pages"] = candidate_pages
    log["candidate_pages_1based"] = [p + 1 for p in candidate_pages]
    log["candidate_source"] = "toc_derived"
    log["candidate_window"] = "back1_forward3"
    log["toc_printed_page_number"] = toc_booklet_page
    log["computed_pdf_page_0based"] = computed_base_0based
    log["computed_pdf_page_1based"] = computed_base_0based + 1
    log["candidate_detail"] = candidate_detail

    logger.debug(
        "[v4] Phase1: toc_printed=%d computed_base_0based=%d "
        "candidates_0based=%s candidates_1based=%s window=back1_forward3",
        toc_booklet_page, computed_base_0based,
        candidate_pages, [p + 1 for p in candidate_pages],
    )
    return candidate_pages


# ==============================================================
# Phase 2: ワードベースフィルタ
# ==============================================================

def _phase2_word_filter(
    page_text: str,
    page_idx: int,
    log_pages: list[dict],
) -> bool:
    page_log: dict[str, Any] = {"page_idx": page_idx}

    # ── 入口判定: segmentish AND metricish の両方が必要 ──────────────────
    # 従来は "セグメント" 1語だけを必須としていたが、
    # pdfplumber のテキスト抽出により "報告セグメント" 等しか取れない場合でも
    # 通過できるよう、汎用語彙の OR → AND に変更する。
    segment_hits = [k for k in _SEGMENT_PAGE_KEYWORDS if k in page_text]
    metric_hits  = [k for k in _SEGMENT_METRIC_KEYWORDS if k in page_text]
    has_segmentish = len(segment_hits) >= 1
    has_metricish  = len(metric_hits)  >= 1

    page_log["segment_hits"] = segment_hits
    page_log["metric_hits"]  = metric_hits

    if not (has_segmentish and has_metricish):
        page_log["result"] = "reject_no_segment_keyword"
        log_pages.append(page_log)
        return False

    # ── スコア評価: 既存ロジックをそのまま活かす ─────────────────────────
    seg_word_hits = [w for w in _WORD_SEGMENT_LIKE if w in page_text]
    seg_word_score = len(seg_word_hits)
    exclude_hits = [w for w in _WORD_EXCLUDE if w in page_text]
    exclude_score = len(exclude_hits)

    page_log["word_filter_score"] = {
        "seg_word_hits": seg_word_hits,
        "seg_word_score": seg_word_score,
        "exclude_hits": exclude_hits,
        "exclude_score": exclude_score,
    }

    if seg_word_score < 2:
        page_log["result"] = "reject_seg_word_count_lt2"
        log_pages.append(page_log)
        return False

    if exclude_score > seg_word_score:
        page_log["result"] = "reject_exclude_dominant"
        log_pages.append(page_log)
        return False

    page_log["result"] = "pass"
    log_pages.append(page_log)
    return True


# ==============================================================
# Phase 3b: 期間ブロック検出
# ==============================================================

def _detect_period_label_in_row(row: list[Any]) -> str | None:
    """
    テーブル1行から期間ラベルを検出する。
    行内のいずれかのセルが期間パターンにマッチすれば返す。
    """
    for cell in row:
        text = _cell_str(cell)
        if _PERIOD_ANY_RE.search(text):
            return text
    return None


def _classify_period_type(label: str) -> str:
    """期間ラベルを "previous" / "current" / "unknown" に分類する。"""
    if _PERIOD_PREV_RE.search(label):
        return "previous"
    if _PERIOD_CURR_RE.search(label):
        return "current"
    return "unknown"


def _split_table_by_period(
    raw_table: list[list[Any]],
    header_rows: int,
) -> list[tuple[str, str, list[list[Any]]]]:
    """
    テーブルを期間ブロックで分割する。

    テーブル内の行を走査し、期間ラベル行（セルに「前/当...」を含む行）を
    区切りとしてサブテーブルに split する。

    期間ラベル行の判定:
      - 行内のいずれかのセルが _PERIOD_ANY_RE にマッチする
      - かつ数値セルが少ない（ラベル行）

    Returns:
        [(period_type, period_label, sub_table), ...]
        分割できない場合は [("unknown", "", raw_table)]
    """
    # まずヘッダー行を分離
    header = raw_table[:header_rows]
    data_rows = raw_table[header_rows:]

    blocks: list[tuple[str, str, list[list[Any]]]] = []
    current_label = ""
    current_type = "unknown"
    current_rows: list[list[Any]] = []

    for row in data_rows:
        label = _detect_period_label_in_row(row)
        # 数値セル数が少ない = ラベル行とみなす
        num_cells = sum(1 for c in row if _is_numeric_cell(c))
        total_cells = sum(1 for c in row if _cell_str(c))
        is_label_row = label is not None and num_cells <= 1

        if is_label_row:
            # 前のブロックを確定
            if current_rows:
                blocks.append((current_type, current_label, header + current_rows))
            current_label = label or ""
            current_type = _classify_period_type(current_label)
            current_rows = []
        else:
            current_rows.append(row)

    # 最後のブロック
    if current_rows:
        blocks.append((current_type, current_label, header + current_rows))

    # 分割なし → 元のまま返す
    if not blocks:
        return [("unknown", "", raw_table)]

    # 1ブロックのみで type=unknown なら分割なし扱い
    if len(blocks) == 1 and blocks[0][0] == "unknown":
        return [("unknown", "", raw_table)]

    return blocks


def _detect_period_label_in_text(page_text: str) -> list[tuple[str, str]]:
    """
    ページテキストから期間ラベルを全て抽出する。

    Returns:
        [(period_type, period_label), ...]
    """
    results = []
    for m in _PERIOD_ANY_RE.finditer(page_text):
        # より長いラベルのために周辺テキストを取る
        start = max(0, m.start() - 2)
        end = min(len(page_text), m.end() + 20)
        label_candidate = page_text[start:end].split("\n")[0].strip()
        p_type = _classify_period_type(label_candidate)
        results.append((p_type, label_candidate))
    return results


# ==============================================================
# Phase 4: 横型表判定
# ==============================================================

def _phase4_is_horizontal_table(
    raw_table: list[list[Any]],
    log_tables: list[dict],
    table_idx: int,
    page_idx: int,
) -> bool:
    tlog: dict[str, Any] = {
        "table_idx": table_idx,
        "page_idx": page_idx,
        "rows": len(raw_table),
        "cols": max((len(r) for r in raw_table), default=0),
    }

    if not raw_table or len(raw_table) < 2:
        tlog["reject"] = "too_few_rows"
        log_tables.append(tlog)
        return False

    max_cols = max(len(r) for r in raw_table)
    if max_cols < 3:
        tlog["reject"] = "too_few_cols"
        log_tables.append(tlog)
        return False

    header_row = raw_table[0]
    header_cells = [_cell_str(c) for c in header_row]
    non_empty_header_cols = sum(1 for c in header_cells if c)
    if non_empty_header_cols < 2:
        tlog["reject"] = "header_too_sparse"
        log_tables.append(tlog)
        return False

    has_left_keyword = False
    for row in raw_table:
        if not row:
            continue
        left_cell = _cell_str(row[0])
        if any(kw in left_cell for kw in _HORIZ_LEFT_KEYWORDS):
            has_left_keyword = True
            break

    if not has_left_keyword:
        tlog["reject"] = "no_left_keyword"
        log_tables.append(tlog)
        return False

    numeric_col_count = 0
    for col_idx in range(1, max_cols):
        col_values = []
        for row in raw_table[1:]:
            if col_idx < len(row):
                col_values.append(row[col_idx])
        numeric_count = sum(1 for v in col_values if _is_numeric_cell(v))
        if numeric_count >= 2:
            numeric_col_count += 1

    if numeric_col_count < 2:
        tlog["reject"] = f"not_enough_numeric_cols ({numeric_col_count})"
        log_tables.append(tlog)
        return False

    tlog["reject"] = None
    tlog["non_empty_header_cols"] = non_empty_header_cols
    tlog["numeric_col_count"] = numeric_col_count
    log_tables.append(tlog)
    return True


# ==============================================================
# Phase 5: segment name取得（2行スパンヘッダー対応）
# ==============================================================

def _phase5_get_segment_names(
    raw_table: list[list[Any]],
    log: dict[str, Any],
) -> tuple[list[tuple[int, str]], int]:
    """
    ヘッダー行（row[0]/row[1]）からセグメント名を取得。

    強化ポイント:
      1. ヘッダー行レベルの numeric_ratio >= 0.4 → その行は不採用
      2. セルレベル: numeric-like なセルは候補から除外
      3. 最終バリデーション: 1件でも numeric-like が残ったら全件 reject

    Returns:
        ([(col_idx, segment_name), ...], header_rows_count)
    """
    if not raw_table:
        log["segment_names"] = []
        log["chosen_header_rows"] = 1
        return [], 1

    row0 = raw_table[0] if len(raw_table) > 0 else []
    row1 = raw_table[1] if len(raw_table) > 1 else []
    max_cols = max(len(row0), len(row1)) if (row0 or row1) else 0

    # ヘッダー行レベルの数値汚染チェック
    row0_numeric_ratio = _row_numeric_ratio(row0)
    row1_numeric_ratio = _row_numeric_ratio(row1) if row1 else 0.0
    row0_contaminated = row0_numeric_ratio >= _HEADER_NUMERIC_RATIO_THRESHOLD
    row1_contaminated = row1_numeric_ratio >= _HEADER_NUMERIC_RATIO_THRESHOLD

    log["row0_numeric_ratio"] = round(row0_numeric_ratio, 3)
    log["row1_numeric_ratio"] = round(row1_numeric_ratio, 3)
    log["row0_contaminated"] = row0_contaminated
    log["row1_contaminated"] = row1_contaminated

    # row0 がセグメント名を持つか（カテゴリ語でなく、数値でもない）
    row0_has_seg_name = False
    if not row0_contaminated:
        for ci in range(1, len(row0)):
            c = _normalize_seg_name(_cell_str(row0[ci]))
            if c and not _is_category_cell(c) and not _is_numeric_like(c):
                row0_has_seg_name = True
                break

    # row1 がセグメント名を持つか
    row1_has_seg_name = False
    if not row1_contaminated:
        for ci in range(1, len(row1)):
            c = _normalize_seg_name(_cell_str(row1[ci]))
            if c and not _is_category_cell(c) and not _is_numeric_like(c):
                row1_has_seg_name = True
                break

    use_two_row_header = (not row0_has_seg_name) and row1_has_seg_name
    header_rows = 2 if use_two_row_header else 1

    segments: list[tuple[int, str]] = []
    rejected_cells: list[str] = []

    for col_idx in range(1, max_cols):
        # row1 優先（汚染されていない場合）
        name1 = ""
        if not row1_contaminated and col_idx < len(row1):
            name1 = _normalize_seg_name(_cell_str(row1[col_idx]))

        # row0 補完（汚染されていない場合）
        name0 = ""
        if not row0_contaminated and col_idx < len(row0):
            name0 = _normalize_seg_name(_cell_str(row0[col_idx]))

        # 候補選択
        chosen_name = ""
        if name1 and not _is_category_cell(name1) and not _is_numeric_like(name1):
            chosen_name = name1
        elif name0 and not _is_category_cell(name0) and not _is_numeric_like(name0):
            chosen_name = name0
        else:
            # 数値として弾いた場合はログへ
            discarded = name1 or name0
            if discarded:
                rejected_cells.append(f"col{col_idx}:{discarded}")
            continue

        segments.append((col_idx, chosen_name))

    # ---- 最終バリデーション ----
    # 1件でも numeric-like が残っていたら全件 reject
    final_rejected = [name for _, name in segments if _is_numeric_like(name)]
    if final_rejected:
        log["segment_names"] = []
        log["chosen_header_rows"] = header_rows
        log["use_two_row_header"] = use_two_row_header
        log["rejected_header_cells"] = rejected_cells
        log["final_numeric_reject"] = final_rejected
        log["header_numeric_reject_reason"] = "numeric_name_in_final_segments"
        logger.debug(
            "[v4] Phase5: REJECT numeric contamination final=%s",
            final_rejected,
        )
        return [], header_rows

    log["segment_names"] = [s[1] for s in segments]
    log["chosen_header_rows"] = header_rows
    log["use_two_row_header"] = use_two_row_header
    log["rejected_header_cells"] = rejected_cells
    log["header_numeric_ratio"] = {
        "row0": round(row0_numeric_ratio, 3),
        "row1": round(row1_numeric_ratio, 3),
    }
    logger.debug(
        "[v4] Phase5: segment_names=%s header_rows=%d two_row=%s "
        "row0_nr=%.2f row1_nr=%.2f rejected=%s",
        [s[1] for s in segments], header_rows, use_two_row_header,
        row0_numeric_ratio, row1_numeric_ratio, rejected_cells,
    )
    return segments, header_rows


# ==============================================================
# Phase 6: 売上・利益抽出（計優先ロジック）
# ==============================================================

def _phase6_find_sales_row(
    raw_table: list[list[Any]],
    log: dict[str, Any],
    header_rows: int = 1,
) -> list[Any] | None:
    """
    左端列から売上行を選択する。優先順位:
      1. 「計」または「合計」を含む行（除外語に該当しないもの）
      2. 「外部顧客への売上高」「外部収益」等 (fallback)

    除外（絶対に採用しない）:
      - 収益認識分解行（一時点で移転される財 等）
    """
    primary: list[Any] | None = None    # 最優先候補
    fallback: list[Any] | None = None   # fallback 候補

    for row in raw_table[header_rows:]:
        if not row:
            continue
        left_cell = _cell_str(row[0])

        # 除外行
        if any(exc in left_cell for exc in _SALES_ROW_EXCLUDE):
            continue

        # Priority 1: 「計」「合計」
        if primary is None and any(kw in left_cell for kw in _SALES_PRIMARY_KEYWORDS):
            primary = row
            log["chosen_sales_row"] = left_cell
            log["sales_row_priority"] = "primary"

        # Priority 2: fallback （計が見つかった後でも念のためスキャン継続しない）
        if fallback is None and any(kw in left_cell for kw in _SALES_FALLBACK_KEYWORDS):
            fallback = row
            if primary is None:
                log["sales_fallback_label"] = left_cell

        # 最高優先が見つかったら即リターン
        if primary is not None:
            break

    if primary is not None:
        logger.debug("[v4] Phase6: sales_row(primary)=%r", log.get("chosen_sales_row"))
        return primary

    if fallback is not None:
        log["chosen_sales_row"] = log.get("sales_fallback_label", "")
        log["sales_row_priority"] = "fallback"
        logger.debug("[v4] Phase6: sales_row(fallback)=%r", log["chosen_sales_row"])
        return fallback

    log["chosen_sales_row"] = None
    log["sales_row_priority"] = None
    return None


def _phase6_find_profit_row(
    raw_table: list[list[Any]],
    log: dict[str, Any],
    header_rows: int = 1,
) -> tuple[list[Any] | None, str]:
    # 別指標行 (continuation 判定で除外するキーワード)
    _CONTINUATION_EXCLUDE_LABELS: tuple[str, ...] = (
        "売上高", "外部顧客", "外部売上高", "売上収益",
        "合計", "資産", "減価", "設備",
    )
    # continuation と見なせる左端の断片
    _CONTINUATION_FRAGMENTS: tuple[str, ...] = (
        "又は損失", "損失（△）", "（注）", "注）", "（△）", "△",
    )

    data_rows_indexed = [
        (i, row) for i, row in enumerate(raw_table[header_rows:], start=header_rows)
        if row
    ]

    for idx, (row_i, row) in enumerate(data_rows_indexed):
        left_cell = _cell_str(row[0])
        for kw in _PROFIT_ROW_KEYWORDS:
            if kw not in left_cell:
                continue

            # 利益ラベル行が見つかった
            log["chosen_profit_row"] = left_cell

            # 行全体に数値セルがあるか（col[1:] を確認）
            has_numeric = any(
                _parse_num(cell) is not None for cell in row[1:]
            )

            if has_numeric:
                logger.debug("[v4] Phase6: chosen_profit_row=%r", left_cell)
                return row, left_cell

            # 数値セルがない → 直下行を continuation 候補として確認
            if idx + 1 < len(data_rows_indexed):
                next_row_i, next_row = data_rows_indexed[idx + 1]
                next_left = _cell_str(next_row[0]) if next_row else ""

                # 別指標行でないことを確認
                is_new_metric = any(kw in next_left for kw in _CONTINUATION_EXCLUDE_LABELS)
                # 左端が空または継続断片であること
                is_continuation = (
                    not next_left
                    or any(frag in next_left for frag in _CONTINUATION_FRAGMENTS)
                )
                next_has_numeric = any(
                    _parse_num(cell) is not None for cell in next_row[1:]
                )

                if not is_new_metric and is_continuation and next_has_numeric:
                    log["chosen_profit_row"] = left_cell + "(continuation)"
                    log["profit_continuation_rescue"] = True
                    logger.debug(
                        "[v4] Phase6: profit_continuation_rescue label=%r next_left=%r",
                        left_cell, next_left,
                    )
                    return next_row, left_cell

            # 直下行でも救済できない場合はこの行を返す（数値は None になる）
            logger.debug("[v4] Phase6: chosen_profit_row=%r (no numeric)", left_cell)
            return row, left_cell

    log["chosen_profit_row"] = None
    return None, ""


# ==============================================================
# Phase 7: record組立
# ==============================================================

def _phase7_build_records(
    seg_cols: list[tuple[int, str]],
    sales_row: list[Any] | None,
    profit_row: list[Any] | None,
    profit_label: str,
    log: dict[str, Any],
) -> list[SegmentRecordV4]:
    records: list[SegmentRecordV4] = []

    for order, (col_idx, seg_name) in enumerate(seg_cols, start=1):
        sales_val: float | None = None
        profit_val: float | None = None

        if sales_row is not None and col_idx < len(sales_row):
            sales_val = _parse_num(sales_row[col_idx])

        if profit_row is not None and col_idx < len(profit_row):
            profit_val = _parse_num(profit_row[col_idx])

        if sales_val is None and profit_val is None:
            continue

        records.append(SegmentRecordV4(
            segment_name=seg_name,
            segment_order=order,
            segment_sales=sales_val,
            segment_profit=profit_val,
            raw_profit_label=profit_label,
            extraction_engine="v4",
        ))

    return records


# ==============================================================
# Phase 8: 最小検証
# ==============================================================

def _phase8_validate(
    records: list[SegmentRecordV4],
    log: dict[str, Any],
) -> tuple[bool, str]:
    n_seg = len(records)
    n_sales = sum(1 for r in records if r.segment_sales is not None)
    n_profit = sum(1 for r in records if r.segment_profit is not None)

    reject_reason = ""
    if n_seg < 2:
        reject_reason = f"segment_count_lt2 (n={n_seg})"
    elif n_sales < 2:
        reject_reason = f"sales_count_lt2 (n={n_sales})"
    elif n_profit < 1:
        reject_reason = f"profit_count_lt1 (n={n_profit})"

    log["validation"] = {
        "n_seg": n_seg,
        "n_sales": n_sales,
        "n_profit": n_profit,
        "reject_reason": reject_reason,
    }

    if reject_reason:
        log["reject_reason"] = reject_reason
        return False, reject_reason

    return True, ""


# ==============================================================
# 1ブロック（サブテーブル）を Phase4〜8 で処理する
# ==============================================================

def _process_one_block(
    sub_table: list[list[Any]],
    period_type: str,
    period_label: str,
    page_idx: int,
    tbl_idx: int,
    block_idx: int,
    log: dict[str, Any],
    trace: list[str],
    ticker: str = "",          # [v3-period] ログ用
) -> PeriodResultV4 | None:
    """
    1期間ブロック（sub_table）を Phase4〜8 で処理する。
    成功すれば PeriodResultV4 を返す。失敗すれば None。
    """
    _debug_blk_5713 = (ticker == "5713")
    prefix = f"page={page_idx} tbl={tbl_idx} blk={block_idx} [{period_type}]"

    # Phase 4
    p4_log: list[dict] = []
    if not _phase4_is_horizontal_table(sub_table, p4_log, tbl_idx, page_idx):
        reject = p4_log[-1].get("reject", "?") if p4_log else "?"
        trace.append(f"Phase4: {prefix} NOT horizontal reason={reject}")
        # ── [v4-5713-candidate] Phase4 落ち ──────────────────────────────────
        if _debug_blk_5713:
            logger.info(
                "[v4-5713-candidate] ticker=5713 page_1based=%d tbl=%d blk=%d "
                "accepted=no reason=phase4_%s",
                page_idx + 1, tbl_idx, block_idx, reject,
            )
        # ── end ──────────────────────────────────────────────────────────────
        return None
    trace.append(f"Phase4: {prefix} IS horizontal")

    # Phase 5
    p5_log: dict[str, Any] = {}
    seg_cols, header_rows = _phase5_get_segment_names(sub_table, p5_log)
    log.setdefault("phase5_logs", []).append(p5_log)

    # ── [v4-5713-phase5] Phase5 結果 ────────────────────────────────────────
    if _debug_blk_5713:
        _chosen_names = [s[1] for s in seg_cols]
        _rejected_hdr = p5_log.get("rejected_header_cells", [])
        _final_rej    = p5_log.get("final_numeric_reject", [])
        _row0_nr      = p5_log.get("row0_numeric_ratio", "?")
        _row1_nr      = p5_log.get("row1_numeric_ratio", "?")
        _two_row      = p5_log.get("use_two_row_header", "?")
        logger.info(
            "[v4-5713-phase5] ticker=5713 page_1based=%d tbl=%d blk=%d "
            "seg_count=%d chosen=%s rejected_cells=%s final_numeric_rej=%s "
            "row0_nr=%s row1_nr=%s two_row_header=%s",
            page_idx + 1, tbl_idx, block_idx,
            len(seg_cols), _chosen_names, _rejected_hdr, _final_rej,
            _row0_nr, _row1_nr, _two_row,
        )
    # ── end [v4-5713-phase5] ────────────────────────────────────────────────

    if len(seg_cols) < 2:
        trace.append(
            f"Phase5: {prefix} seg_cols={len(seg_cols)} < 2 "
            f"header_rows={header_rows} → skip"
        )
        # ── [v4-5713-candidate] Phase5 落ち ──────────────────────────────────
        if _debug_blk_5713:
            logger.info(
                "[v4-5713-candidate] ticker=5713 page_1based=%d tbl=%d blk=%d "
                "accepted=no reason=phase5_seg_count_lt2 seg_count=%d "
                "row0_contaminated=%s row1_contaminated=%s",
                page_idx + 1, tbl_idx, block_idx, len(seg_cols),
                p5_log.get("row0_contaminated"), p5_log.get("row1_contaminated"),
            )
        # ── end ──────────────────────────────────────────────────────────────
        return None
    trace.append(f"Phase5: {prefix} seg_cols={len(seg_cols)} names={[s[1] for s in seg_cols]}")

    # Phase 6
    p6_log: dict[str, Any] = {}
    sales_row = _phase6_find_sales_row(sub_table, p6_log, header_rows=header_rows)
    profit_row, profit_label = _phase6_find_profit_row(sub_table, p6_log, header_rows=header_rows)
    log.setdefault("phase6_logs", []).append(p6_log)
    trace.append(
        f"Phase6: {prefix} "
        f"sales={p6_log.get('chosen_sales_row')!r}({p6_log.get('sales_row_priority')}) "
        f"profit={p6_log.get('chosen_profit_row')!r}"
    )

    if sales_row is None and profit_row is None:
        trace.append(f"Phase6: {prefix} both missing → skip")
        # ── [v4-5713-candidate] Phase6 落ち ──────────────────────────────────
        if _debug_blk_5713:
            logger.info(
                "[v4-5713-candidate] ticker=5713 page_1based=%d tbl=%d blk=%d "
                "accepted=no reason=phase6_no_sales_and_profit",
                page_idx + 1, tbl_idx, block_idx,
            )
        # ── end ──────────────────────────────────────────────────────────────
        return None

    # Phase 7
    p7_log: dict[str, Any] = {}
    records = _phase7_build_records(seg_cols, sales_row, profit_row, profit_label, p7_log)
    trace.append(f"Phase7: {prefix} records={len(records)}")

    # Phase 8
    p8_log: dict[str, Any] = {}
    ok, reason = _phase8_validate(records, p8_log)
    log.setdefault("phase8_logs", []).append(p8_log)

    if not ok:
        trace.append(f"Phase8: {prefix} REJECT reason={reason}")
        # ── [v4-5713-candidate] Phase8 落ち ──────────────────────────────────
        if _debug_blk_5713:
            _v8 = p8_log.get("validation", {})
            logger.info(
                "[v4-5713-candidate] ticker=5713 page_1based=%d tbl=%d blk=%d "
                "accepted=no reason=phase8_%s n_seg=%s n_sales=%s n_profit=%s",
                page_idx + 1, tbl_idx, block_idx, reason,
                _v8.get("n_seg"), _v8.get("n_sales"), _v8.get("n_profit"),
            )
        # ── end ──────────────────────────────────────────────────────────────
        return None

    trace.append(f"Phase8: {prefix} ACCEPTED n={len(records)}")
    # ── [v4-5713-candidate] 採用確定 ─────────────────────────────────────────
    if _debug_blk_5713:
        logger.info(
            "[v4-5713-candidate] ticker=5713 page_1based=%d tbl=%d blk=%d "
            "accepted=yes seg_count=%d period_type=%s",
            page_idx + 1, tbl_idx, block_idx, len(records), period_type,
        )
    # ── end [v4-5713-candidate] ──────────────────────────────────────────────

    # ── [v3-period] 期間確定ログ (1セグメント1行) ──────────────────────────
    ev = f"header:{period_label[:40]}" if period_label else "(no_label)"
    reason = "header_label" if period_label else "no_label"
    for rec in records:
        logger.info(
            "[v3-period] ticker=%s page=%s seg=%r final=%s "
            "reason=%s evidence=%s",
            ticker, page_idx + 1, rec.segment_name,
            period_type, reason, ev,
        )
    # ── end [v3-period] ────────────────────────────────────────────────────

    return PeriodResultV4(
        period_type=period_type,
        period_label=period_label,
        page_index_0based=page_idx,
        page_index_1based=page_idx + 1,
        sales_row_label=p6_log.get("chosen_sales_row") or "",
        profit_row_label=profit_label,
        segments=records,
    )


# ==============================================================
# Phase 3: table extraction 再試行関数
# ==============================================================

# 再試行戦略定義: (strategy_name, table_settings_dict)
# None の設定は pdfplumber のデフォルト（置なし与えより少し弱い）
_TABLE_EXTRACTION_STRATEGIES: list[tuple[str, dict]] = [
    # A: default — 置なしベース（現行動作と同等）
    ("default", {}),
    # B: text・テキスト対視— 置なしが弱いPDF向け
    ("text_text", {
        "vertical_strategy":   "text",
        "horizontal_strategy": "text",
        "snap_tolerance":      3,
        "join_tolerance":      3,
        "intersection_tolerance": 3,
    }),
    # C: mixed — 横は置なし・縦はテキスト（2段ヘッダー対応）
    ("lines_text", {
        "vertical_strategy":   "lines_strict",
        "horizontal_strategy": "text",
        "snap_tolerance":      5,
        "join_tolerance":      3,
        "intersection_tolerance": 5,
    }),
]

# テーブル条件の最小基準（雑音回避）
_TABLE_RETRY_MIN_ROWS: int = 3
_TABLE_RETRY_MIN_COLS: int = 3


def _extract_tables_with_retry(
    page: Any,
) -> tuple[list[list[list[Any]]], str, list[str]]:
    """
    pdfplumber の extract_tables() を最大5方式で再試行する。

    流れ:
      1. default 設定で試妙。raw_tables > 0 かつ最小条件を満たせば即採用。
      2. 驾めなければ次の設定を試妙。
      3. 全設定失敗なら ([],"none", tried) を返す。

    最小条件:
      - 少なくとも 1 table が rows >= _TABLE_RETRY_MIN_ROWS
        AND cols >= _TABLE_RETRY_MIN_COLS

    Returns:
      (tables, used_strategy_name, tried_strategy_names)
    """
    tried: list[str] = []

    for strategy_name, settings in _TABLE_EXTRACTION_STRATEGIES:
        tried.append(strategy_name)
        try:
            if settings:
                tables = page.extract_tables(table_settings=settings) or []
            else:
                tables = page.extract_tables() or []
        except Exception:
            continue

        if not tables:
            continue

        # 最小条件: 少なくとも 1 表が rows>=3 かつ cols>=3
        has_valid = any(
            len(t) >= _TABLE_RETRY_MIN_ROWS
            and max((len(r) for r in t), default=0) >= _TABLE_RETRY_MIN_COLS
            for t in tables
        )
        if has_valid:
            return tables, strategy_name, tried

        # 過小な表だけの場合は次の設定へ

    return [], "none", tried


# ==============================================================
# Phase 3: text fallback — 文字位置ベースの簡易表復元
# ==============================================================

# fallback 採用の最小基準
_FALLBACK_MIN_ROWS: int  = 3
_FALLBACK_MIN_COLS: int  = 4
_FALLBACK_MIN_WORDS: int = 6      # ページ全体の最低文字数（実質チェックのみ）
_FALLBACK_BAND_MIN_WORDS: int = 5 # 帯内の最低文字数（帯で絞った後の条件）
_FALLBACK_ROW_TOL: float = 4.0   # 同一行とみなす y 許容（pt）
_FALLBACK_COL_TOL: float = 15.0  # 同一列とみなす x 許容（pt）: 8→15 で数値列ずれを吸収
_FALLBACK_MIN_COL_FREQ: int = 2  # 列候補となるために必要な出現行数

# fallback 「帯検出」の強シグナル — セグメント表固有性が高い語
# 期間語・銘柄固有語は使わない
_FALLBACK_BAND_STRONG: tuple[str, ...] = (
    "報告セグメント",
    "セグメント利益",
    "セグメント利益又は損失",
    "セグメント損失",
    "セグメント情報",
    "調整額",
    "連結",
    "全社",
    "消去",
)

# 補助シグナル — 単独では弱いが優先度付けに使う
_FALLBACK_BAND_SUPPORT: tuple[str, ...] = (
    "売上高",
    "売上収益",
    "合計",
    "その他",
)

# band 採用の最低条件
_FALLBACK_BAND_MIN_STRONG: int  = 2   # 強シグナル >= 2  OR
_FALLBACK_BAND_MIN_STRONG1: int = 1   #   強シグナル >= 1 AND
_FALLBACK_BAND_MIN_SUPPORT: int = 2   #   補助シグナル >= 2

# band のマージン（表本体を含むための上下余白 pt）
_FALLBACK_BAND_MARGIN_TOP: float    = 25.0
_FALLBACK_BAND_MARGIN_BOTTOM: float = 150.0  # 55→150: ヘッダー下の数値行を取り込むため拡大

# band クラスタリングの y 許容
_FALLBACK_BAND_CLUSTER_TOL: float = 30.0

# fallback 最終確認用シグナル（グリッドに含まれるべき語）
_FALLBACK_SEGMENT_SIGNALS: tuple[str, ...] = (
    "報告セグメント",
    "セグメント",
    "売上高",
    "売上収益",
    "セグメント利益",
    "調整額",
    "連結",
    "合計",
)


def _detect_segment_band(
    words: list[dict],
    debug_5713: bool = False,
    page_1based: int = 0,
) -> tuple[float | None, float | None, dict]:
    """
    words リストからセグメント固有語の密集帯を検出し、
    (band_top, band_bottom, debug_info) を返す。

    強シグナル >= 2 条件、または 強1 + 補助2 条件を満たす
    最強クラスタを採用する。

    検出失敗時は (None, None, info) を返す。
    """
    # 各 word の text を結合して語検索
    # （pdfplumber は 1語=1 word とは限らないので結合しながら判定）
    # ここでは word.text を直接使う
    strong_hits: list[tuple[float, str]] = []  # (y, keyword)
    support_hits: list[tuple[float, str]] = []

    # 全 words の連結テキスト（ページ単位でのシグナル確認）
    page_text_all = " ".join(w.get("text", "") for w in words)

    for kw in _FALLBACK_BAND_STRONG:
        # kw がページに存在するか確認してから word-level で位置を求める
        if kw not in page_text_all:
            continue
        # kw の一部を含む word を探す（先頭一致 or 部分一致）
        for w in words:
            if w.get("text", "") in kw or kw in w.get("text", ""):
                strong_hits.append((w["top"], kw))
    for kw in _FALLBACK_BAND_SUPPORT:
        if kw not in page_text_all:
            continue
        for w in words:
            if w.get("text", "") in kw or kw in w.get("text", ""):
                support_hits.append((w["top"], kw))

    # 重複除去（同一 y + 同一 kw）
    strong_hits = list(dict.fromkeys(strong_hits))
    support_hits = list(dict.fromkeys(support_hits))

    if not strong_hits and not support_hits:
        return None, None, {"reason": "no_signal_hits"}

    # y 座標でクラスタリング
    all_hits = sorted(set(y for y, _ in strong_hits) | set(y for y, _ in support_hits))
    clusters: list[list[float]] = []
    for y in all_hits:
        placed = False
        for cl in clusters:
            if abs(y - cl[0]) <= _FALLBACK_BAND_CLUSTER_TOL:
                cl.append(y)
                placed = True
                break
        if not placed:
            clusters.append([y])

    # 各クラスタのスコア = (strong_count, support_count)
    best_cluster: list[float] = []
    best_strong = 0
    best_support = 0
    for cl in clusters:
        cl_min = min(cl)
        cl_max = max(cl) + _FALLBACK_BAND_CLUSTER_TOL
        sc = sum(1 for y, _ in strong_hits if cl_min <= y <= cl_max)
        sp = sum(1 for y, _ in support_hits if cl_min <= y <= cl_max)
        if (sc > best_strong) or (sc == best_strong and sp > best_support):
            best_strong = sc
            best_support = sp
            best_cluster = cl

    # 採用基準チェック
    ok = (
        best_strong >= _FALLBACK_BAND_MIN_STRONG
        or (best_strong >= _FALLBACK_BAND_MIN_STRONG1 and best_support >= _FALLBACK_BAND_MIN_SUPPORT)
    )

    info = {
        "strong_count": best_strong,
        "support_count": best_support,
        "cluster_count": len(clusters),
        "strong_kws": list({kw for y, kw in strong_hits}),
        "support_kws": list({kw for y, kw in support_hits}),
    }

    if not ok or not best_cluster:
        info["reason"] = f"band_too_weak (strong={best_strong} support={best_support})"
        return None, None, info

    band_top    = min(best_cluster) - _FALLBACK_BAND_MARGIN_TOP
    band_bottom = max(best_cluster) + _FALLBACK_BAND_MARGIN_BOTTOM
    info["band_top"]    = band_top
    info["band_bottom"] = band_bottom

    return band_top, band_bottom, info


# ==============================================================
# Phase 3: text fallback — 構造ベース帯検出ヘルパー
# ==============================================================

# 上ヘッダーブロック検出: セグメント表ヘッダー固有語（右寄り配置を期待）
_STRUCT_HEADER_STRONG: tuple[str, ...] = (
    "その他", "調整額", "連結", "消去", "全社", "計上額",
)
_STRUCT_HEADER_SUPPORT: tuple[str, ...] = (
    "報告セグメント", "合計", "計",
)

# 左ラベル帯検出: 行ラベル（左寄り）でセグメント表の縦範囲を確定
_STRUCT_ROW_START_SIGNALS: tuple[str, ...] = (
    "売上高", "外部顧客", "外部売上高", "売上収益",
)
_STRUCT_ROW_END_SIGNALS: tuple[str, ...] = (
    "セグメント利益又は損失", "セグメント利益", "セグメント損失", "利益", "損失",
)

# 数値判定パターン
_NUMERIC_RE: re.Pattern = re.compile(
    r'^[\d,△▲\-\−\.（）()\s]+$'
)


def _words_to_row_buckets(words: list[dict]) -> list[list[dict]]:
    """words リストを y 座標でグルーピングして row_buckets に変換する。"""
    sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    row_buckets: list[list[dict]] = []
    for w in sorted_words:
        placed = False
        for bucket in row_buckets:
            if abs(w["top"] - bucket[0]["top"]) <= _FALLBACK_ROW_TOL:
                bucket.append(w)
                placed = True
                break
        if not placed:
            row_buckets.append([w])
    for bucket in row_buckets:
        bucket.sort(key=lambda w: w["x0"])
    return row_buckets


def _detect_segment_header_block(
    row_buckets: list[list[dict]],
    page_width: float,
) -> tuple[int, int, dict]:
    """
    row_buckets の中から「セグメント表上ヘッダー」に相当する
    1〜3 行の連続ブロックを検出する。

    判定: 右半分 (x0 > page_width * 0.45) に
          強シグナルが 2 個以上、
          または強 1 + サポート 1 以上。

    Returns: (block_start_idx, block_end_idx, info_dict)
             見つからなければ (-1, -1, info)
    """
    right_x = page_width * 0.45
    mid_x   = page_width * 0.30

    best_score  = 0
    best_start  = -1
    best_end    = -1
    best_info: dict = {}

    for start in range(len(row_buckets)):
        for width in range(1, 4):          # 1〜3 行ブロック
            end = start + width - 1
            if end >= len(row_buckets):
                break

            block_words: list[dict] = []
            for bi in range(start, end + 1):
                block_words.extend(row_buckets[bi])

            right_texts = [
                w.get("text", "") for w in block_words if w["x0"] > right_x
            ]
            mid_texts = [
                w.get("text", "") for w in block_words if w["x0"] > mid_x
            ]

            strong_hits = [
                kw for kw in _STRUCT_HEADER_STRONG
                if any(kw in t or t in kw for t in right_texts)
            ]
            support_hits = [
                kw for kw in _STRUCT_HEADER_SUPPORT
                if any(kw in t or t in kw for t in mid_texts)
            ]

            sc = len(strong_hits)
            sp = len(support_hits)
            ok = (sc >= 2) or (sc >= 1 and sp >= 1)
            score = sc * 3 + sp

            if ok and score > best_score:
                best_score  = score
                best_start  = start
                best_end    = end
                best_info   = {
                    "strong_hits":  strong_hits,
                    "support_hits": support_hits,
                    "score":        score,
                    "block_width":  width,
                }

    if best_start == -1:
        return -1, -1, {"reason": "no_header_block_found"}

    best_info["start_idx"] = best_start
    best_info["end_idx"]   = best_end
    return best_start, best_end, best_info


def _detect_segment_row_band(
    row_buckets: list[list[dict]],
    page_width: float,
    header_end_idx: int,
) -> tuple[int, int, dict]:
    """
    row_buckets の中から「行ラベル帯」を検出する。

    【start 検出】構造ベース優先:
      header_end_idx の直後から各行を走査し、
      - 左 1/3 に非数値テキスト（ラベル）が存在
      - 右 2/3 に数値セルが 2 個以上存在
      を満たす最初の行を start とする。
      キーワード（売上高等）は不使用。
      構造検出失敗時のみ従来キーワード判定にフォールバック。

    【end 検出】現行維持:
      左 1/3 に利益/損失系語が現れた行を end とする。

    Returns: (start_idx, end_idx, info_dict)
             見つからなければ (-1, -1, info)
    """
    left_x  = page_width * 0.35
    right_x = page_width * 0.35  # 右側判定の境界（left_x と同値で左/右を二分）
    search_from = header_end_idx + 1 if header_end_idx >= 0 else 0

    start_idx: int   = -1
    end_idx:   int   = -1
    start_labels: list[str] = []
    end_labels:   list[str] = []
    start_detected_by: str  = "none"

    # ── 構造ベース start 検出 ────────────────────────────────────────
    _STRUCT_NUM_MIN_RIGHT = 2   # 右側の数値セルの最低数
    _STRUCT_NUMERIC_RE_LOCAL = re.compile(r'^[\d,△▲\-\−\.（）()\s]+$')

    for i in range(search_from, len(row_buckets)):
        row = row_buckets[i]

        # 左1/3 の非数値ラベルを確認
        left_words = [w for w in row if w["x0"] < left_x]
        left_texts = [w.get("text", "").strip() for w in left_words]
        left_has_label = any(
            t and not (
                _STRUCT_NUMERIC_RE_LOCAL.match(t) or
                t.lstrip("△▲−-").replace(",", "").replace(".", "").isdigit()
            )
            for t in left_texts
        )

        # 右2/3 の数値セルを確認
        right_words = [w for w in row if w["x0"] >= right_x]
        right_num_count = sum(
            1 for w in right_words
            if (
                _STRUCT_NUMERIC_RE_LOCAL.match(w.get("text", "").strip()) or
                w.get("text", "").strip().lstrip("△▲−-").replace(",", "").replace(".", "").isdigit()
            )
        )

        if left_has_label and right_num_count >= _STRUCT_NUM_MIN_RIGHT:
            start_idx = i
            start_labels = [left_texts[0]] if left_texts else []
            start_detected_by = "structure"
            break

    # ── キーワードフォールバック（構造検出失敗時のみ）────────────────
    if start_idx == -1:
        for i in range(search_from, len(row_buckets)):
            left_joined = " ".join(
                w.get("text", "")
                for w in row_buckets[i]
                if w["x0"] < left_x
            )
            for sig in _STRUCT_ROW_START_SIGNALS:
                if sig in left_joined:
                    start_idx = i
                    start_labels.append(sig)
                    start_detected_by = "keyword"
                    break
            if start_idx != -1:
                break

    if start_idx == -1:
        return -1, -1, {"reason": "no_row_band_start_found"}

    # ── end 検出（現行キーワードベース維持）──────────────────────────
    for i in range(start_idx, len(row_buckets)):
        left_joined = " ".join(
            w.get("text", "")
            for w in row_buckets[i]
            if w["x0"] < left_x
        )
        for sig in _STRUCT_ROW_END_SIGNALS:
            if sig in left_joined:
                if end_idx == -1:
                    end_idx = i
                    end_labels.append(sig)
                    # ラベル行に数値がなく（折り返しの場合）、直下行があればそこまで含める
                    end_row_words = row_buckets[i]
                    end_row_has_num = any(
                        w.get("text", "").strip().lstrip("△▲−-").replace(",", "").replace(".", "").isdigit()
                        for w in end_row_words
                        if w["x0"] >= left_x
                    )
                    if not end_row_has_num and i + 1 < len(row_buckets):
                        end_idx = i + 1  # 直下行（数値継続行）を含める

    # 終端が見つからなければ開始から最大 8 行分
    if end_idx == -1:
        end_idx = min(start_idx + 8, len(row_buckets) - 1)


    return start_idx, end_idx, {
        "start_idx":        start_idx,
        "end_idx":          end_idx,
        "start_labels":     start_labels,
        "end_labels":       end_labels,
        "start_detected_by": start_detected_by,
    }


def _check_numeric_ratio(
    row_buckets: list[list[dict]],
    band_start: int,
    band_end: int,
    page_width: float,
) -> tuple[float, int]:
    """
    行ラベル帯 [band_start, band_end] の右 2/3 セルについて
    数値らしさを評価する。

    Returns: (numeric_ratio, numeric_col_count)
    """
    right_x = page_width * 0.35
    total   = 0
    numeric = 0
    x0_num_cnt: dict[int, int] = {}
    x0_tot_cnt: dict[int, int] = {}

    for i in range(band_start, band_end + 1):
        for w in row_buckets[i]:
            if w["x0"] < right_x:
                continue
            text = w.get("text", "").strip()
            if not text:
                continue
            col_key = int(w["x0"] / 20) * 20
            total += 1
            x0_tot_cnt[col_key] = x0_tot_cnt.get(col_key, 0) + 1
            stripped = text.lstrip("△▲−-").replace(",", "").replace(".", "")
            is_num = (
                _NUMERIC_RE.match(text) is not None
                or stripped.isdigit()
            )
            if is_num:
                numeric += 1
                x0_num_cnt[col_key] = x0_num_cnt.get(col_key, 0) + 1

    ratio = numeric / total if total > 0 else 0.0
    num_cols = sum(
        1 for k in x0_tot_cnt
        if x0_num_cnt.get(k, 0) / x0_tot_cnt[k] >= 0.4
    )
    return ratio, num_cols


def _build_text_table_fallback(
    page: Any,
    debug_5713: bool = False,
    page_1based: int = 0,
) -> tuple[list[list[list[str]]], dict]:
    """
    pdfplumber の extract_tables() が全設定失敗した場合の text fallback。

    優先順位:
      第1候補: 構造ベース検出
        1. page 全体で row_buckets を作成
        2. 上ヘッダーブロック (右寄りシグナル語) を検出
        3. 左ラベル帯 (売上高→利益) を検出
        4. 交差領域の数値比率を確認
        5. 採用条件を満たせばその範囲の words でグリッド生成
      第2候補: 既存シグナル密集帯 (_detect_segment_band)

    返り値:
      (fallback_tables, band_info_dict)
    """
    # ── 文字層チェック ────────────────────────────────────────────────
    try:
        words = page.extract_words(x_tolerance=3, y_tolerance=3) or []
    except Exception:
        return [], {"reason": "extract_words_error"}

    if len(words) < _FALLBACK_MIN_WORDS:
        return [], {"reason": f"too_few_words ({len(words)})"}

    page_width: float = getattr(page, "width", 600.0)

    # ── ページ全体の row_buckets 生成 ─────────────────────────────────
    all_row_buckets = _words_to_row_buckets(words)

    # ══════════════════════════════════════════════════════════════════
    # 第1候補: 構造ベース検出
    # ══════════════════════════════════════════════════════════════════
    _hdr_start, _hdr_end, _hdr_info = _detect_segment_header_block(
        all_row_buckets, page_width,
    )
    _struct_accepted = False
    _struct_info: dict = {}
    _row_info: dict = {}   # _hdr_start<0 の場合に未定義にならないよう初期化

    if _hdr_start >= 0:
        _row_start, _row_end, _row_info = _detect_segment_row_band(
            all_row_buckets, page_width, _hdr_end,
        )
        if _row_start >= 0:
            _num_ratio, _num_cols = _check_numeric_ratio(
                all_row_buckets, _row_start, _row_end, page_width,
            )
            _struct_info = {
                "header_start":    _hdr_start,
                "header_end":      _hdr_end,
                "header_hits":     _hdr_info.get("strong_hits", []) + _hdr_info.get("support_hits", []),
                "row_band_start":  _row_start,
                "row_band_end":    _row_end,
                "row_label_hits":  _row_info.get("start_labels", []) + _row_info.get("end_labels", []),
                "numeric_ratio":   _num_ratio,
                "numeric_cols":    _num_cols,
            }
            row_span = _row_end - _row_start + 1
            if _num_ratio >= 0.35 and _num_cols >= 2 and row_span >= 1:
                _struct_accepted = True

    # ── [v4-5713-struct-band] ログ ────────────────────────────────────
    if debug_5713:
        _hdr_y_tops = [
            round(all_row_buckets[i][0]["top"], 2)
            for i in range(_hdr_start, _hdr_end + 1)
            if 0 <= i < len(all_row_buckets)
        ] if _hdr_start >= 0 else []
        _rb_top = round(all_row_buckets[_struct_info.get("row_band_start", -1)][0]["top"], 2) \
                  if _struct_info.get("row_band_start", -1) >= 0 else None
        _rb_bot = round(all_row_buckets[_struct_info.get("row_band_end", -1)][0]["top"], 2) \
                  if _struct_info.get("row_band_end", -1) >= 0 else None
        logger.info(
            "[v4-5713-struct-band] page=%d "
            "header_block_y=%s header_hits=%s "
            "row_band_start_y=%s row_band_end_y=%s "
            "row_label_hits=%s "
            "numeric_ratio=%s numeric_cols=%s "
            "accepted=%s reject_reason=%s",
            page_1based,
            _hdr_y_tops,
            _struct_info.get("header_hits"),
            _rb_top, _rb_bot,
            _struct_info.get("row_label_hits"),
            round(_struct_info.get("numeric_ratio", 0.0), 3),
            _struct_info.get("numeric_cols"),
            "yes" if _struct_accepted else "no",
            _hdr_info.get("reason") or _row_info.get("reason", "") if not _struct_accepted else "",
        )
    # ── end [v4-5713-struct-band] ─────────────────────────────────────

    # ── [v4-struct-rowband] ログ（start 検出方法の記録）──────────────
    if debug_5713 and _struct_info.get("row_band_start", -1) >= 0:
        _rbi_start = _struct_info.get("row_band_start", -1)
        _rb_start_y = round(all_row_buckets[_rbi_start][0]["top"], 2) \
                      if 0 <= _rbi_start < len(all_row_buckets) else None
        logger.info(
            "[v4-struct-rowband] page=%d "
            "start_y=%s detected_by=%s "
            "left_label_sample=%s numeric_cols=%s",
            page_1based,
            _rb_start_y,
            _row_info.get("start_detected_by", "unknown"),
            _row_info.get("start_labels", [])[:1],
            _struct_info.get("numeric_cols"),
        )
    # ── end [v4-struct-rowband] ────────────────────────────────────────

    if _struct_accepted:
        # 構造採用: hdr / dat words を分けて保存し、col clustering は両方から
        _hdr_words_s = [
            w for i in range(_hdr_start, _hdr_end + 1) for w in all_row_buckets[i]
        ]
        _dat_words_s = [
            w
            for i in range(_struct_info["row_band_start"], _struct_info["row_band_end"] + 1)
            for w in all_row_buckets[i]
        ]
        target_words = _hdr_words_s + _dat_words_s
        band_info = {"structural": True, **_struct_info}
    else:
        # ══════════════════════════════════════════════════════════════
        # 第2候補: シグナル密集帯ベース
        # ══════════════════════════════════════════════════════════════
        band_top, band_bottom, band_info = _detect_segment_band(
            words, debug_5713=debug_5713, page_1based=page_1based,
        )
        if band_top is None:
            return [], band_info

        band_words = [w for w in words if band_top <= w["top"] <= band_bottom]
        if len(band_words) < _FALLBACK_BAND_MIN_WORDS:
            band_info["reason"] = f"band_words_too_few ({len(band_words)})"
            return [], band_info

        target_words = band_words
        band_info["structural"] = False
        _hdr_words_s = None
        _dat_words_s = None

    # ── B: x0 クラスターから列候補識別 ───────────────────────────────
    # (col clustering は hdr+dat 両方の target_words から行う)
    all_x0 = sorted(w["x0"] for w in target_words)
    col_centers: list[float] = []
    if all_x0:
        cur = all_x0[0]
        cnt = 1
        for x in all_x0[1:]:
            if x - cur <= _FALLBACK_COL_TOL:
                cur = (cur * cnt + x) / (cnt + 1)
                cnt += 1
            else:
                if cnt >= _FALLBACK_MIN_COL_FREQ:
                    col_centers.append(cur)
                cur = x
                cnt = 1
        if cnt >= _FALLBACK_MIN_COL_FREQ:
            col_centers.append(cur)

    if len(col_centers) < _FALLBACK_MIN_COLS:
        band_info["reason"] = f"col_centers_too_few ({len(col_centers)})"
        return [], band_info

    # ── C: 行 × 列 グリッドへ word を配置 ──────────────────────────
    n_cols = len(col_centers)
    _skip_e_trim = False  # struct 採用時は疎行刈りをスキップするフラグ

    def _wlist_to_grid(wlist: list[dict]) -> list[list[str]]:
        """words リストを y grouping → col 配置 → grid に変換する。"""
        sw2 = sorted(wlist, key=lambda w: (w["top"], w["x0"]))
        rbs2: list[list[dict]] = []
        for w2 in sw2:
            placed2 = False
            for bkt2 in rbs2:
                if abs(w2["top"] - bkt2[0]["top"]) <= _FALLBACK_ROW_TOL:
                    bkt2.append(w2)
                    placed2 = True
                    break
            if not placed2:
                rbs2.append([w2])
        for bkt2 in rbs2:
            bkt2.sort(key=lambda w: w["x0"])
        g2: list[list[str]] = []
        for bkt2 in rbs2:
            rc2 = [""] * n_cols
            for w2 in bkt2:
                ci2 = min(range(n_cols), key=lambda ii, ww=w2: abs(col_centers[ii] - ww["x0"]))
                txt2 = w2.get("text", "").strip()
                if rc2[ci2]:
                    rc2[ci2] += " " + txt2
                else:
                    rc2[ci2] = txt2
            if any(c for c in rc2):
                g2.append(rc2)
        return g2

    if _struct_accepted and _hdr_words_s is not None and _dat_words_s is not None:
        # ── 構造採用時: header words を y 分割せず 1 行に圧縮 ────────────
        # ヘッダーブロック全体の words を列方向に配置し、単一の header row を作る。
        # y 単位で分割すると 1 行あたり非空セル=1 になりやすく Phase4 sparse 判定に
        # 落ちるため、ブロック全体を 1 行として扱う。
        _hdr_single_row: list[str] = [""] * n_cols
        for _hw in sorted(_hdr_words_s, key=lambda w: w["x0"]):
            _hci = min(range(n_cols), key=lambda ii, ww=_hw: abs(col_centers[ii] - ww["x0"]))
            _ht = _hw.get("text", "").strip()
            if _hdr_single_row[_hci]:
                _hdr_single_row[_hci] += " " + _ht
            else:
                _hdr_single_row[_hci] = _ht
        header_rows = [_hdr_single_row] if any(c for c in _hdr_single_row) else []
        data_rows   = _wlist_to_grid(_dat_words_s)


        # Sanity check: header が本当に非数値のセグメント語を含むか
        _hdr_non_num = sum(
            1 for row in header_rows
            for c in row[1:]
            if c.strip() and not c.strip().lstrip("△▲−-").replace(",", "").replace(".", "").isdigit()
        )
        _dat_label_hits = [
            c
            for row in data_rows[:3]
            for c in row[:1]
            if any(sig in c for sig in _STRUCT_ROW_START_SIGNALS + _STRUCT_ROW_END_SIGNALS)
        ]
        _struct_split_ok = (
            len(header_rows) >= 1
            and _hdr_non_num >= 2
            and len(_dat_label_hits) >= 1
        )

        # ── [v4-5713-struct-grid] ログ ──────────────────────────────────
        if debug_5713:
            logger.info(
                "[v4-5713-struct-grid] page=%d "
                "header_rows=%d data_rows=%d "
                "row0=%s row1=%s first_data_row=%s "
                "header_non_numeric_count=%d data_label_hits=%s "
                "struct_split_ok=%s",
                page_1based,
                len(header_rows), len(data_rows),
                header_rows[0][:6] if header_rows else [],
                header_rows[1][:6] if len(header_rows) > 1 else [],
                data_rows[0][:6] if data_rows else [],
                _hdr_non_num,
                _dat_label_hits,
                _struct_split_ok,
            )
        # ── end [v4-5713-struct-grid] ───────────────────────────────────

        if _struct_split_ok:
            grid = header_rows + data_rows
            _skip_e_trim = True
        else:
            # sanity 失敗 → hdr+dat 一括 grid 化（generic 経路と同じ）
            grid = _wlist_to_grid(target_words)
    else:
        # ── generic 経路 ─────────────────────────────────────────────────
        grid = _wlist_to_grid(target_words)

    if len(grid) < _FALLBACK_MIN_ROWS:
        band_info["reason"] = f"grid_rows_too_few ({len(grid)})"
        return [], band_info

    # ── D: セグメントシグナル最終確認 ──────────────────────────────
    flat_text = " ".join(c for row in grid for c in row)
    has_signal = any(sig in flat_text for sig in _FALLBACK_SEGMENT_SIGNALS)
    if not has_signal:
        band_info["reason"] = "no_segment_signal_in_grid"
        return [], band_info

    # ── E: 先頭疎行の刈り取り（generic 経路のみ）────────────────────
    _FALLBACK_TRIM_MIN_NONEMPTY = 3
    _FALLBACK_TRIM_MAX          = 4
    _trimmed = 0
    if not _skip_e_trim:
        while (
            grid
            and _trimmed < _FALLBACK_TRIM_MAX
            and sum(1 for c in grid[0] if c.strip()) < _FALLBACK_TRIM_MIN_NONEMPTY
        ):
            grid.pop(0)
            _trimmed += 1
        if len(grid) < _FALLBACK_MIN_ROWS:
            band_info["reason"] = f"grid_rows_too_few_after_trim ({len(grid)} trimmed={_trimmed})"
            return [], band_info

    band_info["grid_rows"]    = len(grid)
    band_info["grid_cols"]    = n_cols
    band_info["trimmed_rows"] = _trimmed
    return [grid], band_info


def _try_merge_side_by_side_segment_tables(
    raw_tables: list[list[list[Any]]],
    ticker: str,
    page_idx: int,
    trace: list[str],
) -> list[list[list[Any]]]:
    """
    同一ページ内で左右に分割された同一セグメント表を横結合する。

    検出条件:
      A. page 内のテーブル数がちょうど 2
      B. 行数を概ぽ一致（±1）
      C. 行ラベル（col 0）の一致率 >= 60%
      D. セグメント名の重複率 < 50%
      E. 右テーブルの上位2行に continuation シグナル（計/調整額/四半期連結…）

    結合方法:
      各行について row_a + row_b[1:] として横連結する
    """
    if len(raw_tables) != 2:
        return raw_tables

    tbl_a, tbl_b = raw_tables[0], raw_tables[1]

    # 条件B: 行数一致（±1）
    if abs(len(tbl_a) - len(tbl_b)) > 1:
        logger.info(
            "[v4-side-merge-skip] ticker=%s page=%s reason=row_count_mismatch a=%d b=%d",
            ticker, page_idx + 1, len(tbl_a), len(tbl_b),
        )
        return raw_tables

    n_rows = min(len(tbl_a), len(tbl_b))

    # 条件C: 行ラベル（col 0）の一致率
    def _lbl(tbl: list, i: int) -> str:
        if i >= len(tbl) or not tbl[i]:
            return ""
        return str(tbl[i][0] or "").strip()

    matched = sum(
        1 for i in range(n_rows)
        if _lbl(tbl_a, i) == _lbl(tbl_b, i)
    )
    ratio = matched / max(n_rows, 1)
    if ratio < 0.6:
        logger.info(
            "[v4-side-merge-skip] ticker=%s page=%s reason=row_label_mismatch ratio=%.2f",
            ticker, page_idx + 1, ratio,
        )
        return raw_tables

    # 条件D: row1 のセグメント名非重複
    def _seg_set(tbl: list) -> set[str]:
        if len(tbl) < 2:
            return set()
        return {
            str(c or "").strip()
            for c in tbl[1][1:]
            if c and str(c or "").strip()
            and not _is_numeric_like(str(c or "").strip())
        }

    segs_a = _seg_set(tbl_a)
    segs_b = _seg_set(tbl_b)

    if not segs_a or not segs_b:
        logger.info(
            "[v4-side-merge-skip] ticker=%s page=%s reason=no_segment_names",
            ticker, page_idx + 1,
        )
        return raw_tables

    overlap_n = len(segs_a & segs_b)
    if overlap_n / max(len(segs_a), len(segs_b)) >= 0.5:
        logger.info(
            "[v4-side-merge-skip] ticker=%s page=%s reason=segment_overlap overlap=%d",
            ticker, page_idx + 1, overlap_n,
        )
        return raw_tables

    # 条件E: 右テーブルの上位2行に continuation シグナルがある
    _CONT = {'計', '調整額', '四半期連結損益計算書計上額', '四半期連結', '連結計算書'}
    b_top = " ".join(str(c or "") for row in tbl_b[:2] for c in row)
    if not any(sig in b_top for sig in _CONT):
        logger.info(
            "[v4-side-merge-skip] ticker=%s page=%s reason=no_continuation_signal",
            ticker, page_idx + 1,
        )
        return raw_tables

    # 横結合: row_a + row_b[1:]
    n_cols_a = max((len(r) for r in tbl_a), default=0)
    merged: list[list[Any]] = []
    for i in range(n_rows):
        row_a  = list(tbl_a[i]) if i < len(tbl_a) else [''] * n_cols_a
        extra  = list(tbl_b[i][1:]) if i < len(tbl_b) and len(tbl_b[i]) > 1 else []
        merged.append(row_a + extra)

    n_segs = len(segs_a) + len(segs_b)
    logger.info(
        "[v4-side-merge] ticker=%s page=%s left_tbl=0 right_tbl=1 "
        "reason=segment_continuation merged_segments=%d row_match_ratio=%.2f",
        ticker, page_idx + 1, n_segs, ratio,
    )
    trace.append(
        f"[v4-side-merge] page={page_idx} segs=({len(segs_a)}+{len(segs_b)})={n_segs} "
        f"ratio={ratio:.2f}"
    )
    return [merged]


# ==============================================================
# 同一ページ内テーブル結合
# ==============================================================

def _try_merge_page_tables(
    raw_tables: list[list[list[Any]]],
    ticker: str,
    page_idx: int,
    trace: list[str],
) -> list[list[list[Any]]]:
    """
    同一ページ内で同構造の複数テーブルを1テーブルに結合する。
    安全条件: テーブル数 2≤3 / 列数一致 / ヘッダー一致 / セグメント名非重複
    """
    if len(raw_tables) < 2 or len(raw_tables) > 3:
        return raw_tables

    def _n_cols(tbl: list[list[Any]]) -> int:
        return max((len(row) for row in tbl), default=0)

    def _header_key(tbl: list[list[Any]]) -> tuple:
        if not tbl:
            return ()
        return tuple(str(c or "").strip() for c in tbl[0])

    def _left_col_texts(tbl: list[list[Any]]) -> set[str]:
        """ヘッダー除く左端列の非空・非数値テキスト集合"""
        names: set[str] = set()
        for row in tbl[1:]:
            if row and row[0]:
                v = str(row[0]).strip()
                if v and not _is_numeric_like(v):
                    names.add(v)
        return names

    base      = raw_tables[0]
    base_cols = _n_cols(base)
    base_hdr  = _header_key(base)
    base_segs = _left_col_texts(base)

    merged: list[list[Any]] = list(base)
    merged_indices: list[int] = [0]

    for i, tbl in enumerate(raw_tables[1:], start=1):
        t_cols = _n_cols(tbl)
        t_hdr  = _header_key(tbl)
        t_segs = _left_col_texts(tbl)

        # 条件A: 列数一致（±1許容）
        if abs(t_cols - base_cols) > 1:
            logger.info(
                "[v4-table-merge-skip] ticker=%s page=%s tbl=%d "
                "reason=col_count_mismatch base=%d this=%d",
                ticker, page_idx + 1, i, base_cols, t_cols,
            )
            continue

        # 条件B: ヘッダー一致（もしくは2テーブル目ヘッダーが全空）
        all_empty_hdr = all(not str(c or "").strip() for c in (tbl[0] if tbl else []))
        if t_hdr != base_hdr and not all_empty_hdr:
            logger.info(
                "[v4-table-merge-skip] ticker=%s page=%s tbl=%d "
                "reason=header_mismatch",
                ticker, page_idx + 1, i,
            )
            continue

        # 条件C: セグメント名の重複が50%未満
        if base_segs and t_segs:
            overlap = len(base_segs & t_segs)
            if base_segs and overlap / max(len(base_segs), len(t_segs)) >= 0.5:
                logger.info(
                    "[v4-table-merge-skip] ticker=%s page=%s tbl=%d "
                    "reason=segment_overlap overlap=%d",
                    ticker, page_idx + 1, i, overlap,
                )
                continue

        # 条件D: 行数が最低3行以上（ヘッダー1 + セグ2）
        if len(tbl) < 3:
            logger.info(
                "[v4-table-merge-skip] ticker=%s page=%s tbl=%d "
                "reason=too_few_rows rows=%d",
                ticker, page_idx + 1, i, len(tbl),
            )
            continue

        # 結合: ヘッダーが同じなら2テーブル目のヘッダー行をスキップ
        if t_hdr == base_hdr:
            merged.extend(tbl[1:])
        else:
            merged.extend(tbl)
        merged_indices.append(i)
        base_segs |= t_segs

    if len(merged_indices) > 1:
        logger.info(
            "[v4-table-merge] ticker=%s page=%s tables=%d "
            "merged_rows=%d reason=same_structure_same_page merged_indices=%s",
            ticker, page_idx + 1, len(merged_indices), len(merged), merged_indices,
        )
        trace.append(
            f"[v4-table-merge] page={page_idx} merged={merged_indices} "
            f"rows={len(merged)}"
        )
        remaining = [
            tbl for i, tbl in enumerate(raw_tables)
            if i not in set(merged_indices)
        ]
        return [merged] + remaining

    return raw_tables


# ==============================================================
# メインエントリーポイント
# ==============================================================

def run_segment_detection_v4(
    pdf_path: str,
    *,
    doc_id: str = "",
    ticker: str = "",
) -> V4DetectionResult:
    result = V4DetectionResult()
    log = result.log
    trace = result.rule_trace

    log["doc_id"] = doc_id
    log["ticker"] = ticker
    log["pdf_path"] = pdf_path

    logger.info("[v4] START pdf=%s ticker=%s", pdf_path, ticker)

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as e:
        result.quarantine_reason = f"pdf_open_error: {e}"
        result.failed_stage = "open"
        log["reject_reason"] = result.quarantine_reason
        trace.append(f"FAIL open: {e}")
        return result

    try:
        with pdf:
            return _run_v4_inner(pdf, result, log, trace)
    except Exception as e:
        result.quarantine_reason = f"unexpected_error: {e}"
        result.failed_stage = "inner"
        log["reject_reason"] = result.quarantine_reason
        trace.append(f"FAIL unexpected: {e}")
        logger.exception("[v4] unexpected_error: %s", e)
        return result


def _run_v4_inner(
    pdf: pdfplumber.PDF,
    result: V4DetectionResult,
    log: dict[str, Any],
    trace: list[str],
) -> V4DetectionResult:

    # ── 5713 診断フラグ ──────────────────────────────────────────────────────
    _ticker_raw = log.get("ticker", "")
    _debug_5713 = (_ticker_raw == "5713")
    # ── end 5713 診断フラグ ──────────────────────────────────────────────────

    # Phase 0
    trace.append("Phase0: TOC探索")
    toc_booklet_page, toc_physical_idx = _phase0_find_toc_page_number(pdf, log)
    if log.get("toc_detected"):
        trace.append(
            f"Phase0: toc_detected=True booklet_page={toc_booklet_page} "
            f"phys_idx={toc_physical_idx}"
        )
    else:
        trace.append("Phase0: toc_detected=False → 全ページ対象")

    # Phase 1
    trace.append("Phase1: 候補ページ生成")
    candidate_pages = _phase1_candidate_pages(
        pdf, toc_booklet_page, toc_physical_idx, log
    )
    trace.append(f"Phase1: candidate_pages(0based)={candidate_pages}")

    # ── [v4-5713-pages] Phase1 直後: 候補ページ一覧 ─────────────────────────
    if _debug_5713:
        logger.info(
            "[v4-5713-pages] ticker=5713 toc_booklet=%s toc_phys=%s "
            "candidate_pages_0based=%s candidate_pages_1based=%s source=%s",
            toc_booklet_page, toc_physical_idx,
            candidate_pages, [p + 1 for p in candidate_pages],
            log.get("candidate_source"),
        )
    # ── end [v4-5713-pages] ──────────────────────────────────────────────────

    if not candidate_pages:
        result.quarantine_reason = "no_candidate_pages"
        result.failed_stage = "phase1"
        log["reject_reason"] = result.quarantine_reason
        trace.append("FAIL Phase1: no_candidate_pages")
        return result

    log["page_word_filter"] = []
    log["table_horizontal_filter"] = []

    # period_type → PeriodResultV4 のベスト候補（多セグ優先）
    period_best: dict[str, PeriodResultV4] = {}
    ticker = log.get("ticker", "")
    # 期間別かつ成功ブロック数（複数 unknown の判別用）
    period_counts: dict[str, int] = {}   # {"previous": 1, "current": 0, "unknown": 1, ...}
    # unknown ブロック全件を記録 (block_id, PeriodResultV4) — fill2 用
    unknown_blocks_list: list[tuple[str, PeriodResultV4]] = []

    for page_idx in candidate_pages:
        if page_idx >= len(pdf.pages):
            continue

        page = pdf.pages[page_idx]

        # Phase 2
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""

        if not _phase2_word_filter(page_text, page_idx, log["page_word_filter"]):
            trace.append(
                f"Phase2: page_index_0based={page_idx} "
                f"page_index_1based={page_idx + 1} REJECTED"
            )
            # ── [v4-5713-pages] word_filter REJECTED 詳細 ─────────────────────
            if _debug_5713:
                _wf_log    = log["page_word_filter"][-1] if log["page_word_filter"] else {}
                _wf_reason = _wf_log.get("result", "?")
                logger.info(
                    "[v4-5713-pages] ticker=5713 page_0based=%d page_1based=%d "
                    "passed_word_filter=False reason=%s "
                    "segment_hits=%s metric_hits=%s",
                    page_idx, page_idx + 1, _wf_reason,
                    _wf_log.get("segment_hits", []),
                    _wf_log.get("metric_hits", []),
                )
            # ── end ─────────────────────────────────────────────────────────────
            continue

        trace.append(
            f"Phase2: page_index_0based={page_idx} "
            f"page_index_1based={page_idx + 1} PASSED"
        )
        # ── [v4-5713-pages] word_filter PASSED 詳細 ───────────────────────────
        if _debug_5713:
            _wf_log_p = log["page_word_filter"][-1] if log["page_word_filter"] else {}
            logger.info(
                "[v4-5713-pages] ticker=5713 page_0based=%d page_1based=%d "
                "passed_word_filter=True segment_hits=%s metric_hits=%s",
                page_idx, page_idx + 1,
                _wf_log_p.get("segment_hits", []),
                _wf_log_p.get("metric_hits", []),
            )
        # ── end ────────────────────────────────────────────────────────────────

        # ページテキストから期間ラベルを検出（補助情報）
        page_period_labels = _detect_period_label_in_text(page_text)
        if page_period_labels:
            trace.append(f"Phase3b: page={page_idx} period_labels_in_text={page_period_labels[:4]}")

        # Phase 3: テーブル抽出（複数設定再試行）
        raw_tables, _used_strategy, _tried_strategies = _extract_tables_with_retry(page)
        if raw_tables == [] and _used_strategy == "none":
            # エラーなし・全設定失敗
            trace.append(
                f"Phase3: page_index_0based={page_idx} "
                f"page_index_1based={page_idx + 1} extract_tables all_failed tried={_tried_strategies}"
            )
        elif not raw_tables:
            trace.append(
                f"Phase3: page_index_0based={page_idx} "
                f"page_index_1based={page_idx + 1} extract_tables all_empty tried={_tried_strategies}"
            )

        log.setdefault("tables_found", {})[page_idx] = len(raw_tables)
        trace.append(
            f"Phase3: page_index_0based={page_idx} "
            f"page_index_1based={page_idx + 1} "
            f"tables_found={len(raw_tables)} strategy={_used_strategy}"
        )

        # ── [v4-5713-pages] ページごとの raw_tables 件数＋strategy ─────────────
        if _debug_5713:
            logger.info(
                "[v4-5713-pages] ticker=5713 page_0based=%d page_1based=%d "
                "passed_word_filter=True raw_tables=%d used_strategy=%s tried=%s",
                page_idx, page_idx + 1, len(raw_tables),
                _used_strategy, _tried_strategies,
            )
        # ── end [v4-5713-pages] ──────────────────────────────────────────────

        # raw_tables=0 の場合は text fallback を試みる
        if not raw_tables:
            _fb_tables, _fb_band_info = _build_text_table_fallback(
                page, debug_5713=_debug_5713, page_1based=page_idx + 1,
            )
            _fb_word_count = 0
            try:
                _fb_word_count = len(page.extract_words() or [])
            except Exception:
                pass
            # ── [v4-5713-fallback-band] band 情報ログ ──────────────────────────────
            if _debug_5713:
                logger.info(
                    "[v4-5713-fallback-band] ticker=5713 page_1based=%d "
                    "strong_hits=%s support_hits=%s strong_count=%s support_count=%s "
                    "cluster_count=%s band_top=%s band_bottom=%s reason=%s",
                    page_idx + 1,
                    _fb_band_info.get("strong_kws"),
                    _fb_band_info.get("support_kws"),
                    _fb_band_info.get("strong_count"),
                    _fb_band_info.get("support_count"),
                    _fb_band_info.get("cluster_count"),
                    _fb_band_info.get("band_top"),
                    _fb_band_info.get("band_bottom"),
                    _fb_band_info.get("reason", "ok"),
                )
            # ── end [v4-5713-fallback-band] ──────────────────────────────────────
            # ── [v4-5713-fallback] 結果ログ ─────────────────────────────────────────
            if _debug_5713:
                if _fb_tables:
                    _fb_r = _fb_band_info.get("grid_rows", 0)
                    _fb_c = _fb_band_info.get("grid_cols", 0)
                    logger.info(
                        "[v4-5713-fallback] ticker=5713 page_1based=%d "
                        "triggered=yes words=%d rows=%d cols=%d tables=%d used_strategy=fallback_text",
                        page_idx + 1, _fb_word_count, _fb_r, _fb_c, len(_fb_tables),
                    )
                else:
                    logger.info(
                        "[v4-5713-fallback] ticker=5713 page_1based=%d "
                        "triggered=yes words=%d tables=0 reason=%s",
                        page_idx + 1, _fb_word_count,
                        _fb_band_info.get("reason", "no_segment_grid"),
                    )
            # ── end [v4-5713-fallback] ────────────────────────────────────────────
            if _fb_tables:
                raw_tables = _fb_tables
                _used_strategy = "fallback_text"
                log.setdefault("tables_found", {})[page_idx] = len(raw_tables)
                trace.append(
                    f"Phase3-fallback: page_index_0based={page_idx} "
                    f"page_index_1based={page_idx + 1} "
                    f"fallback_tables={len(raw_tables)}"
                )
                # ── [v4-5713-raw] strategy 更新 ────────────────────────────────────────
                if _debug_5713:
                    logger.info(
                        "[v4-5713-raw] ticker=5713 page_1based=%d "
                        "used_strategy=fallback_text raw_tables=%d",
                        page_idx + 1, len(raw_tables),
                    )
                # ── end [v4-5713-raw] ────────────────────────────────────────────────
            else:
                continue


        # 左右分割セグメント表を先に横結合（side-by-side）
        raw_tables = _try_merge_side_by_side_segment_tables(
            raw_tables, ticker, page_idx, trace
        )
        # 上下分割テーブルを縦結合（page merge）
        raw_tables = _try_merge_page_tables(raw_tables, ticker, page_idx, trace)
        log.setdefault("tables_found_after_merge", {})[page_idx] = len(raw_tables)

        # ── [v4-5713-raw] merge 後の各テーブル概要 ──────────────────────────
        if _debug_5713:
            _KEY_WORDS_5713 = [
                "報告セグメント", "資源", "製錬", "材料", "その他",
                "合計", "調整額", "連結",
            ]
            for _ti, _rt in enumerate(raw_tables):
                _r_cnt = len(_rt)
                _c_cnt = max((len(r) for r in _rt), default=0)
                _labels = [
                    str(_rt[ri][0] or "").strip()
                    for ri in range(min(6, _r_cnt))
                    if _rt[ri] and str(_rt[ri][0] or "").strip()
                ][:5]
                _hdr_row = _rt[0] if _rt else []
                _hdr_cells = [
                    str(c or "").strip()
                    for c in _hdr_row
                    if str(c or "").strip()
                ][:10]
                _page_text_flat = " ".join(
                    str(c or "") for row in _rt[:3] for c in row
                )
                _kw_found = [kw for kw in _KEY_WORDS_5713 if kw in _page_text_flat]
                logger.info(
                    "[v4-5713-raw] ticker=5713 page_0based=%d page_1based=%d "
                    "tbl=%d rows=%d cols=%d labels=%s headers=%s keywords=%s",
                    page_idx, page_idx + 1,
                    _ti, _r_cnt, _c_cnt,
                    _labels, _hdr_cells, _kw_found,
                )
        # ── end [v4-5713-raw] ────────────────────────────────────────────────

        for tbl_idx, raw_table in enumerate(raw_tables):

            # Phase 5 で header_rows を先に確認（ブロック分割に使う）
            # 分割前に暫定的なheader_rowsを取得
            p5_probe: dict[str, Any] = {}
            _, probe_header_rows = _phase5_get_segment_names(raw_table, p5_probe)

            # Phase 3b: 期間ブロック分割
            blocks = _split_table_by_period(raw_table, probe_header_rows)
            has_multiple_periods = len(blocks) > 1
            trace.append(
                f"Phase3b: page={page_idx} tbl={tbl_idx} "
                f"blocks={len(blocks)} multi_period={has_multiple_periods}"
            )

            # ── [v4-5713-candidate] candidate 化前チェック ───────────────────
            if _debug_5713:
                logger.info(
                    "[v4-5713-candidate] ticker=5713 page_0based=%d page_1based=%d "
                    "tbl=%d blocks=%d probe_header_rows=%d",
                    page_idx, page_idx + 1, tbl_idx, len(blocks), probe_header_rows,
                )
            # ── end [v4-5713-candidate] ──────────────────────────────────────

            # ブロックが1つだけで type=unknown かつ
            # ページテキストに期間ラベルが両方あるときは、
            # ページテキストの情報で period_type を補完
            if len(blocks) == 1 and blocks[0][0] == "unknown":
                # ページに both prev と current があれば current と推定
                # ページに1つだけあればそちらを適用
                page_types = {pt for pt, _ in page_period_labels}
                if "current" in page_types and "previous" not in page_types:
                    blocks = [("current", blocks[0][1], blocks[0][2])]
                elif "previous" in page_types and "current" not in page_types:
                    blocks = [("previous", blocks[0][1], blocks[0][2])]
                # 両方あるか不明 → unknown のまま（後で best として使う）

            for block_idx, (period_type, period_label, sub_table) in enumerate(blocks):

                # ── [v3-period] ブロック root 根拠ログ ───────────────────────
                _blk_ev_raw = period_label or ""
                if period_label:
                    _blk_reason = "header_label"
                    _blk_ev = f"header:{_blk_ev_raw[:50]}"
                elif _blk_reason_from_text := (
                    # page_text 補完が効いた場合を判別
                    "page_text_label"
                    if (len(blocks) == 1 and period_type != "unknown")
                    else ""
                ):
                    _blk_reason = _blk_reason_from_text
                    ptypes_str = "+".join(sorted({pt for pt, _ in page_period_labels}))
                    _blk_ev = f"page_text:{ptypes_str}"
                else:
                    _blk_reason = "no_label"
                    _blk_ev = f"no period label in table or page_text"
                logger.info(
                    "[v3-period] ticker=%s page=%s tbl=%s blk=%s "
                    "final=%s reason=%s evidence=%s",
                    ticker, page_idx + 1, tbl_idx, block_idx,
                    period_type, _blk_reason, _blk_ev,
                )
                # ── end [v3-period] ─────────────────────────────────────────

                pr = _process_one_block(
                    sub_table, period_type, period_label,
                    page_idx, tbl_idx, block_idx, log, trace,
                    ticker=ticker,
                )
                if pr is None:
                    continue

                # period_best に登録（セグメント数多い方を優先）
                existing = period_best.get(period_type)
                if existing is None or len(pr.segments) > len(existing.segments):
                    period_best[period_type] = pr
                    trace.append(
                        f"BEST_UPDATED: period_type={period_type} "
                        f"n_seg={len(pr.segments)} page={page_idx}"
                    )
                # 期間別カウントを欠算（最初の登録だけカウントする）
                period_counts[period_type] = period_counts.get(period_type, 0) + 1
                if period_type == "unknown":
                    _uid = f"pg{page_idx + 1}_tbl{tbl_idx}_blk{block_idx}"
                    unknown_blocks_list.append((_uid, pr))

    # ------------------------------------------------------------------
    # 単独 unknown 補完後処理（最小安全正規化）
    # ------------------------------------------------------------------
    _n_prev   = period_counts.get("previous", 0)
    _n_curr   = period_counts.get("current",  0)
    _n_unk    = period_counts.get("unknown",   0)

    if _n_unk == 1 and "unknown" in period_best:
        _unk_blk = period_best["unknown"]
        if _n_prev >= 1 and _n_curr == 0:
            # パターA: previousあり / currentなし / unknown 1件 → current に補完
            _unk_blk.period_type = "current"
            period_best.pop("unknown")
            period_best["current"] = _unk_blk
            _uid1 = unknown_blocks_list[0][0] if unknown_blocks_list else "?"
            logger.info(
                "[v4-period-fill] ticker=%s fill=current "
                "reason=single_unknown_with_previous "
                "unknown_block=%s previous_blocks=%d current_blocks=%d unknown_blocks=%d",
                ticker, _uid1, _n_prev, _n_curr, _n_unk,
            )
            trace.append(
                f"[v4-period-fill] fill=current unknown_block={_uid1} "
                f"prev={_n_prev} curr={_n_curr} unk={_n_unk}"
            )
        elif _n_curr >= 1 and _n_prev == 0:
            # パターB: currentあり / previousなし / unknown 1件 → previous に補完
            _unk_blk.period_type = "previous"
            period_best.pop("unknown")
            period_best["previous"] = _unk_blk
            _uid1 = unknown_blocks_list[0][0] if unknown_blocks_list else "?"
            logger.info(
                "[v4-period-fill] ticker=%s fill=previous "
                "reason=single_unknown_with_current "
                "unknown_block=%s previous_blocks=%d current_blocks=%d unknown_blocks=%d",
                ticker, _uid1, _n_prev, _n_curr, _n_unk,
            )
            trace.append(
                f"[v4-period-fill] fill=previous unknown_block={_uid1} "
                f"prev={_n_prev} curr={_n_curr} unk={_n_unk}"
            )
        # else: prev/curr 両方ある or 両方なし → 補完しない
    elif _n_unk > 1:
        logger.info(
            "[v4-period-fill-skip] ticker=%s reason=unknown_blocks_multiple "
            "previous_blocks=%d current_blocks=%d unknown_blocks=%d",
            ticker, _n_prev, _n_curr, _n_unk,
        )

    # ------------------------------------------------------------------
    # 2件 unknown 補完後処理 (fill2) — prev=0, curr=0, unk=2 の典型ケース
    # ------------------------------------------------------------------
    _n_prev2 = period_counts.get("previous", 0)
    _n_curr2 = period_counts.get("current",  0)
    _n_unk2  = period_counts.get("unknown",   0)

    if _n_prev2 == 0 and _n_curr2 == 0 and _n_unk2 == 2 and len(unknown_blocks_list) == 2:

        def _blk_sort_key(uid: str) -> tuple:
            # "pg{p}_tbl{t}_blk{b}" → (p, t, b)
            import re as _re
            nums = _re.findall(r'\d+', uid)
            return tuple(int(n) for n in nums) if len(nums) >= 3 else (0, 0, 0)

        (uid_a, blk_a), (uid_b, blk_b) = sorted(
            unknown_blocks_list, key=lambda x: _blk_sort_key(x[0])
        )
        segs_a = {s.segment_name for s in blk_a.segments}
        segs_b = {s.segment_name for s in blk_b.segments}
        n_a, n_b = len(blk_a.segments), len(blk_b.segments)
        overlap = len(segs_a & segs_b)
        count_gap = abs(n_a - n_b)

        # 安全条件チェック
        skip_reason = ""
        if n_a < 2 or n_b < 2:
            skip_reason = f"segment_count_too_low (n={n_a},{n_b})"
        elif count_gap > 2:
            skip_reason = f"segment_count_gap_too_large (gap={count_gap})"
        elif overlap < 1:
            skip_reason = f"segment_overlap_too_low (overlap={overlap})"

        if skip_reason:
            logger.info(
                "[v4-period-fill2-skip] ticker=%s reason=%s "
                "prev=%d curr=%d unk=%d seg_counts=%d,%d overlap=%d",
                ticker, skip_reason, _n_prev2, _n_curr2, _n_unk2, n_a, n_b, overlap,
            )
        else:
            # 先ブロック→previous、後ブロック→current
            blk_a.period_type = "previous"
            blk_b.period_type = "current"
            period_best.pop("unknown", None)
            period_best["previous"] = blk_a
            period_best["current"]  = blk_b
            logger.info(
                "[v4-period-fill2] ticker=%s fill=paired_unknown_two_blocks "
                "reason=ordered_two_unknown_blocks "
                "prev_block=%s curr_block=%s "
                "prev=%d curr=%d unk=%d seg_counts=%d,%d overlap=%d",
                ticker, uid_a, uid_b,
                _n_prev2, _n_curr2, _n_unk2, n_a, n_b, overlap,
            )
            trace.append(
                f"[v4-period-fill2] fill=paired prev={uid_a} curr={uid_b} "
                f"seg_counts={n_a},{n_b} overlap={overlap}"
            )

    # ------------------------------------------------------------------
    # fill3: 完全単独 unknown (prev=0, curr=0, unk=1) → current に補完
    #        「どちらの期間か不明だが抽出は正常」なブロックを当期として扱う
    #        安全条件: seg >= 2 のみ
    # ------------------------------------------------------------------
    _n_prev3 = period_counts.get("previous", 0)
    _n_curr3 = period_counts.get("current",  0)
    _n_unk3  = period_counts.get("unknown",   0)

    if _n_prev3 == 0 and _n_curr3 == 0 and _n_unk3 == 1 and unknown_blocks_list:
        _uid3, _blk3 = unknown_blocks_list[0]
        _seg3 = len(_blk3.segments)
        if _seg3 >= 2:
            _blk3.period_type = "current"
            period_best.pop("unknown", None)
            period_best["current"] = _blk3
            logger.info(
                "[v4-period-fill3] ticker=%s fill=current "
                "reason=single_unknown_only_block "
                "prev=%d curr=%d unk=%d seg_count=%d block=%s",
                ticker, _n_prev3, _n_curr3, _n_unk3, _seg3, _uid3,
            )
            trace.append(
                f"[v4-period-fill3] fill=current block={_uid3} seg={_seg3}"
            )
        else:
            logger.info(
                "[v4-period-fill3-skip] ticker=%s reason=segment_count_too_small "
                "prev=%d curr=%d unk=%d seg_count=%d",
                ticker, _n_prev3, _n_curr3, _n_unk3, _seg3,
            )

    # ------------------------------------------------------------------
    # 最終結果組立
    # ------------------------------------------------------------------
    result.extracted_periods = list(period_best.values())

    if not result.extracted_periods:
        result.quarantine_reason = "no_valid_horizontal_segment_table"
        result.failed_stage = "extraction"
        log["reject_reason"] = result.quarantine_reason
        trace.append("FAIL: no_valid_horizontal_segment_table")
        logger.info(
            "[v4] FAILED pdf=%s reason=%s",
            log.get("pdf_path"), result.quarantine_reason,
        )
        # ------------------------------------------------------------------
        # Vision fallback 試行 (feature flag OFF のときは何もしない)
        # ------------------------------------------------------------------
        fallback_result = _maybe_try_vision_fallback(
            log.get("pdf_path", ""), result, log, trace,
            reason=result.quarantine_reason,
        )
        if fallback_result is not None:
            return fallback_result
        # ── [v4-5713-final] 最終FAIL ────────────────────────────────────────
        if _debug_5713:
            _p5s = log.get("phase5_logs", [])
            _p8s = log.get("phase8_logs", [])
            _last_p5 = _p5s[-1] if _p5s else {}
            _last_p8 = _p8s[-1] if _p8s else {}
            _v8 = _last_p8.get("validation", {})
            logger.info(
                "[v4-5713-final] ticker=5713 failed_stage=%s "
                "quarantine_reason=%s "
                "candidate_pages_1based=%s "
                "last_phase5_seg_names=%s last_phase5_rejected=%s "
                "last_phase8_n_seg=%s n_sales=%s n_profit=%s reject=%s",
                result.failed_stage,
                result.quarantine_reason,
                log.get("candidate_pages_1based"),
                _last_p5.get("segment_names"),
                _last_p5.get("rejected_header_cells"),
                _v8.get("n_seg"), _v8.get("n_sales"), _v8.get("n_profit"),
                _v8.get("reject_reason"),
            )
        # ── end [v4-5713-final] ──────────────────────────────────────────────
        return result

    # 後方互換: segments = current を優先、なければ unknown/previous
    for pt in ("current", "unknown", "previous"):
        if pt in period_best:
            result.segments = period_best[pt].segments
            log["best_page"] = period_best[pt].page_index_0based
            log["n_segments"] = len(result.segments)
            break

    log["extracted_period_types"] = [pr.period_type for pr in result.extracted_periods]
    trace.append(
        f"SUCCESS: periods={[pr.period_type for pr in result.extracted_periods]} "
        f"n_seg={len(result.segments)}"
    )
    logger.info(
        "[v4] SUCCESS pdf=%s periods=%s n_segments=%d",
        log.get("pdf_path"),
        [pr.period_type for pr in result.extracted_periods],
        len(result.segments),
    )
    # ── [v4-5713-success] 成功確定 ──────────────────────────────────────────
    if _debug_5713:
        for _pr in result.extracted_periods:
            logger.info(
                "[v4-5713-success] ticker=5713 page_1based=%d "
                "period_type=%s period_label=%r "
                "segments=%s sales_label=%r profit_label=%r",
                _pr.page_index_1based,
                _pr.period_type, _pr.period_label,
                [s.segment_name for s in _pr.segments],
                _pr.sales_row_label, _pr.profit_row_label,
            )
    # ── end [v4-5713-success] ────────────────────────────────────────────────

    # ------------------------------------------------------------------
    # v4 quality gate (minimal)
    # Phase 0-8 を通過したが内容不正なマッチに vision fallback を発動。
    # 既存の FAIL 分岐および Phase 0-8 のロジックには一切触れない。
    # ------------------------------------------------------------------
    if result.segments:
        seg_names = [s.segment_name for s in result.segments]

        # 条件1: セグメント数が少なすぎる
        if len(seg_names) < 3:
            _qg_result = _maybe_try_vision_fallback(
                log.get("pdf_path", ""), result, log, trace,
                reason="low_segment_count",
            )
            if _qg_result is not None:
                return _qg_result

        # 条件2: 補助列名のみ（実質セグメントなし）
        _QUALITY_GATE_EXCLUDE = frozenset([
            "その他", "連結", "合計", "調整額", "調整", "消去", "全社",
        ])
        core_names = [n for n in seg_names if n not in _QUALITY_GATE_EXCLUDE]
        if len(core_names) == 0:
            _qg_result = _maybe_try_vision_fallback(
                log.get("pdf_path", ""), result, log, trace,
                reason="only_other_consolidated",
            )
            if _qg_result is not None:
                return _qg_result
    return result



# ==============================================================
# CLI エントリーポイント
# ==============================================================

# ==============================================================
# Vision fallback 定数・ヘルパー関数
# (既存 Phase 0-8 には一切触れない)
# ==============================================================

# feature flag 環境変数名
_VISION_FLAG_ENV: str = "ENABLE_VISION_SEGMENT_FALLBACK"

# vision fallback を発動する reject_reason のホワイトリスト
VISION_FALLBACK_REASONS: frozenset = frozenset([
    "no_valid_horizontal_segment_table",
])


def _determine_reject_reason(
    log: dict,
    result,
) -> str:
    """log / result から reject_reason を導出する。"""
    reason = log.get("reject_reason", "")
    if reason:
        return reason
    if result.quarantine_reason:
        return result.quarantine_reason
    return ""


def _get_vision_candidate_pages(log: dict) -> list:
    """vision fallback 候補ページリストを log から取得する。"""
    pages = log.get("candidate_pages", [])
    if pages:
        return [int(p) for p in pages]
    pages_1 = log.get("candidate_pages_1based", [])
    if pages_1:
        return [int(p) for p in pages_1]
    best = log.get("best_page")
    if best is not None:
        return [int(best)]
    return []


def _maybe_try_vision_fallback(
    pdf_path: str,
    result,
    log: dict,
    trace: list,
    reason=None,
):
    """
    条件を満たす場合のみ vision fallback を試みる。

    以下のいずれかなら None を返し呼び出し側が既存 fail を返す:
    - feature flag OFF
    - reject_reason が対象外（FAIL 経路）または quality gate トリガーを含む
    - candidate_pages が空
    - vision モジュール import 失敗
    - API key 未設定
    - pymupdf 未インストール
    - vision fallback 結果の success=False

    Args:
        pdf_path: PDF ファイルパス
        result: 現在の V4DetectionResult
        log: 実行ログ dict
        trace: ルールトレースリスト
        reason: トリガー理由（FAIL の quarantine_reason または quality gate 名）

    Returns:
        成功した V4DetectionResult (segments 上書き済み)、
        またはスキップ/失敗時は None
    """
    import os
    import logging
    _logger = logging.getLogger("segment_detection_v4")

    # feature flag チェック
    if os.environ.get(_VISION_FLAG_ENV, "0") != "1":
        return None

    # トリガー理由をログ出力
    ticker = str(log.get("ticker", ""))
    _logger.info(
        "[v4-vision-trigger] reason=%s ticker=%s",
        reason or "(unknown)", ticker,
    )

    # reject_reason チェック（FAIL 経路）または quality gate トリガーか判定
    _QUALITY_GATE_REASONS: frozenset = frozenset([
        "low_segment_count", "only_other_consolidated",
    ])
    reject_reason = _determine_reject_reason(log, result)
    if reason not in _QUALITY_GATE_REASONS:
        # FAIL 経路: 既存の VISION_FALLBACK_REASONS チェック
        if reject_reason not in VISION_FALLBACK_REASONS:
            _logger.debug(
                "[v4-vision] skip: reject_reason=%r not in VISION_FALLBACK_REASONS",
                reject_reason,
            )
            return None

    # 候補ページ取得
    candidate_pages = _get_vision_candidate_pages(log)
    if not candidate_pages:
        _logger.warning(
            "[v4-vision-reject] reason=no_candidate_pages pdf=%s reject_reason=%s",
            pdf_path, reject_reason,
        )
        log.setdefault("vision_fallback", {})["reject"] = "no_candidate_pages"
        return None

    # vision モジュール import チェック
    try:
        from src.segment.vision_fallback import extract_segments_with_vision
    except ImportError:
        _logger.warning("[v4-vision-reject] reason=import_error (vision_fallback not installed)")
        log.setdefault("vision_fallback", {})["reject"] = "import_error"
        return None

    # API key チェック
    if not os.environ.get("OPENAI_API_KEY", ""):
        _logger.warning("[v4-vision-reject] reason=no_api_key ticker=%s", ticker)
        log.setdefault("vision_fallback", {})["reject"] = "no_api_key"
        return None

    _logger.info(
        "[v4-vision-fallback] ticker=%s candidates=%s pages_to_try=%s provider=openai model=gpt-4o",
        ticker, candidate_pages, candidate_pages[:2],
    )
    trace.append(
        f"VisionFallback: START ticker={ticker} "
        f"pages={candidate_pages} reason={reject_reason}"
    )

    # vision fallback 実行
    try:
        vfr = extract_segments_with_vision(
            pdf_path=pdf_path,
            candidate_pages=candidate_pages,
            ticker=ticker,
            provider="openai",
        )
    except Exception as e:
        _logger.exception(
            "[v4-vision-reject] ticker=%s reason=vision_exception detail=%s",
            ticker, e,
        )
        log.setdefault("vision_fallback", {})["reject"] = f"vision_exception: {e}"
        trace.append(f"VisionFallback: EXCEPTION {e}")
        return None

    # 結果メタデータを log に記録
    log["vision_fallback"] = {
        "attempted": True,
        "success": vfr.success,
        "selected_page": vfr.selected_page,
        "confidence": vfr.confidence,
        "provider": vfr.provider,
        "model": vfr.model,
        "n_records": len(vfr.segment_records),
        "validation_errors": vfr.validation_errors,
        "reject_reason_trigger": reason,
    }

    if not vfr.success:
        trace.append(f"VisionFallback: FAILED errors={vfr.validation_errors}")
        return None

    # 採用: 既存 result に vision 結果を上書き
    from src.analysis.segment_detection_v4 import V4DetectionResult
    new_result = V4DetectionResult()
    new_result.segments = vfr.segment_records
    new_result.quarantine_reason = ""
    new_result.failed_stage = ""
    new_result.rule_trace = trace + [
        f"VisionFallback: ACCEPTED n={len(vfr.segment_records)} "
        f"page={vfr.selected_page} confidence={vfr.confidence:.2f}"
    ]
    new_result.log = log
    new_result.log["best_page"] = vfr.selected_page
    new_result.log["n_segments"] = len(vfr.segment_records)

    _logger.info(
        "[v4] VisionFallback ACCEPTED ticker=%s n=%d page=%d confidence=%.2f",
        ticker, len(vfr.segment_records), vfr.selected_page or -1, vfr.confidence,
    )
    return new_result


def _cli_main() -> None:
    parser = argparse.ArgumentParser(
        description="segment_detection_v4 — 横型セグメント抽出器 v4 CLI",
    )
    parser.add_argument("--sample-dir", required=True, metavar="DIR")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        stream=sys.stderr,
        level=log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )

    sample_dir = Path(args.sample_dir)
    if not sample_dir.is_dir():
        print(f"[ERROR] not a directory: {sample_dir}", file=sys.stderr)
        sys.exit(1)

    pdf_files = sorted(sample_dir.glob("**/*.pdf"))
    if not pdf_files:
        print(f"[WARN] no PDF found in {sample_dir}", file=sys.stderr)
        sys.exit(0)

    print(f"{'='*70}")
    print(f"segment_detection_v4  ({len(pdf_files)} PDFs)")
    print(f"{'='*70}")

    ok_count = 0
    fail_count = 0

    for pdf_path in pdf_files:
        stem = pdf_path.stem
        ticker_match = re.match(r"^(\d{4})", stem)
        ticker = ticker_match.group(1) if ticker_match else stem[:10]

        result = run_segment_detection_v4(str(pdf_path), ticker=ticker)
        log = result.log

        print(f"\n{'─'*60}")
        print(f"  FILE   : {pdf_path.name}")
        print(f"  TICKER : {ticker}")
        print(f"  STATUS : {'OK' if result.success else 'FAIL'}")

        # PAGE 診断
        print(f"  [PAGE] toc_printed_page_number  : {log.get('toc_printed_page_number')}")
        print(f"  [PAGE] candidate_pages(0based)  : {log.get('candidate_pages')}")
        print(f"  [PAGE] candidate_pages(1based)  : {log.get('candidate_pages_1based')}")

        if result.success:
            ok_count += 1
            periods = result.extracted_periods
            print(f"  periods: {[pr.period_type for pr in periods]}")
            for pr in periods:
                tag = f"[{pr.period_type.upper():<10}]"
                print(f"\n  {tag} period_label   = {pr.period_label!r}")
                print(f"  {tag} page(1based)   = {pr.page_index_1based}")
                print(f"  {tag} sales_row      = {pr.sales_row_label!r}")
                print(f"  {tag} profit_row     = {pr.profit_row_label!r}")
                print(f"  {tag} segment_names  = {[s.segment_name for s in pr.segments]}")
                for r in pr.segments:
                    print(
                        f"  {tag}   [{r.segment_order:02d}] {r.segment_name:<20} "
                        f"sales={r.segment_sales}  profit={r.segment_profit}"
                    )
        else:
            fail_count += 1
            reject = log.get("reject_reason") or result.quarantine_reason
            print(f"  reject_reason : {reject}")
            tail_trace = result.rule_trace[-6:]
            for t in tail_trace:
                print(f"    trace: {t}")

    print(f"\n{'='*70}")
    print(f"RESULT: OK={ok_count}  FAIL={fail_count}  TOTAL={len(pdf_files)}")
    print(f"{'='*70}")


if __name__ == "__main__":
    _cli_main()
