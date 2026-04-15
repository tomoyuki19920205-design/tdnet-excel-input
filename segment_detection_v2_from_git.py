# ============================================================
# segment_detection_v2.py — v2 統合エンジン (Phase 2)
# ============================================================
"""
Phase A-G を統合した PDF セグメント表自動検出 v2。

Phase 2 追加:
  - unit_detection 統合
  - segment_name_normalizer 統合
  - ColumnRole taxonomy 活用
  - RowRole is_reportable_segment 活用
  - quarantine review jsonl 出力
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pdfplumber

from .page_scoring import score_segment_page, rank_candidate_pages, apply_sequence_boost, PageScore
from .table_scoring import score_segment_table, find_table_regions, TableScore, is_weak_evidence_table, is_heading_like_table
from .header_analysis import (
    detect_header_band,
    reconstruct_header_grid,
    extract_header_units,
    normalize_header,
    HeaderGrid,
    _NUM_PATTERN,
)
from .column_analysis import classify_columns, ColumnAnalysisResult, ColumnRole
from .row_analysis import classify_rows, RowAnalysisResult, RowRole
from .unit_detection import detect_unit_for_table, UnitDetectionResult
from .segment_name_normalizer import normalize_segment_name, SegmentNameNormalizationResult


logger = logging.getLogger("tdnet.v2")

JST = timezone(timedelta(hours=9))


# ============================================================
# V2 結果 (Phase 2 拡張)
# ============================================================

@dataclass
class SegmentRecordV2:
    """v2 で抽出されたセグメント1件"""
    segment_name: str = ""
    segment_order: int = 0
    segment_sales: float | None = None
    segment_profit: float | None = None
    raw_profit_label: str = ""
    raw_text: str = ""
    confidence: float = 0.0
    provenance: dict[str, Any] = field(default_factory=dict)
    # Phase 2 追加
    segment_name_raw: str = ""
    segment_name_normalized: str = ""
    segment_name_normalize_rule: str | None = None
    segment_name_confidence: float = 1.0
    unit_raw: str | None = None
    unit_multiplier: int | None = None
    currency: str | None = None
    unit_source: str | None = None
    unit_confidence: float = 0.0
    row_role: str = ""
    is_reportable_segment: bool = True
    sales_col_role: str = ""
    profit_col_role: str = ""
    extraction_engine: str = "v2"
    parse_quality: str = "full"  # "full" | "partial_sales_only"


@dataclass
class V2DetectionResult:
    """v2 検出の全体結果"""
    segments: list[SegmentRecordV2] = field(default_factory=list)
    quarantine_reason: str = ""
    failed_stage: str = ""
    review_hint: str = ""
    rule_trace: list[str] = field(default_factory=list)
    score_summary: dict[str, Any] = field(default_factory=dict)
    used_v2: bool = False
    # Phase 2 追加
    unit_info: UnitDetectionResult | None = None
    candidate_tables_count: int = 0
    scored_pages_count: int = 0

    @property
    def success(self) -> bool:
        return len(self.segments) > 0 and not self.quarantine_reason


def _extract_numbers_from_line(line: str) -> list[float]:
    """行から数値を抽出する (extractor.py と同等)"""
    results: list[float] = []
    for m in re.finditer(r'[△▲]?\s*[\d,]+(?:\.\d+)?', line):
        raw = m.group()
        is_neg = "△" in raw or "▲" in raw
        num_str = re.sub(r'[△▲\s,]', '', raw)
        try:
            val = float(num_str)
            if is_neg:
                val = -val
            results.append(val)
        except ValueError:
            continue
    return results


def _pdfplumber_table_to_lines(raw_table: list[list[str | None]]) -> list[str]:
    """pdfplumber extract_tables() の結果をテキスト行リストに変換する。

    各行は セル値を空白区切りで結合した文字列。
    None セルは空文字列にする。
    """
    lines: list[str] = []
    for row in raw_table:
        cells = [(cell or "").strip() for cell in row]
        line = "  ".join(cells)
        if line.strip():
            lines.append(line)
    return lines

# ============================================================
# quarantine review jsonl 出力
# ============================================================

_REVIEW_DIR = None  # 初期化時に設定可能


def set_review_output_dir(path: str) -> None:
    """quarantine review jsonl の出力先を設定"""
    global _REVIEW_DIR
    _REVIEW_DIR = path


def write_quarantine_review(
    result: V2DetectionResult,
    *,
    doc_id: str = "",
    ticker: str = "",
    source_file: str = "",
    best_table_lines: list[str] | None = None,
    column_diagnosis: dict | None = None,
    table_index: int | None = None,
) -> None:
    """quarantine 案件を review 用 jsonl に出力 (Phase 2 強化版)"""
    try:
        review_dir = _REVIEW_DIR or os.environ.get("TDNET_REVIEW_DIR", "")
        if not review_dir:
            review_dir = str(Path(__file__).resolve().parents[2] / "review")

        os.makedirs(review_dir, exist_ok=True)
        review_path = os.path.join(review_dir, "quarantine_review_segment.jsonl")

        # header/row snapshot (強化: header 10行, row_labels 20件)
        header_snapshot = []
        row_labels_sample = []
        if best_table_lines:
            header_snapshot = [l.strip() for l in best_table_lines[:10] if l.strip()]
            for l in best_table_lines:
                m = re.match(r'^([^\d\u25B3\u25B2\-\uFF0D]*)', l.strip())
                if m and m.group(1).strip():
                    row_labels_sample.append(m.group(1).strip())
            row_labels_sample = row_labels_sample[:20]

        record = {
            "doc_id": doc_id or "?",
            "ticker": ticker or "?",
            "source_file": source_file or "?",
            "failed_stage": result.failed_stage,
            "quarantine_reason": result.quarantine_reason,
            "review_hint": result.review_hint,
            "table_index": table_index,
            "candidate_tables": result.candidate_tables_count,
            "best_table_score": result.score_summary.get("table_score"),
            "sales_col_role": result.score_summary.get("sales_col_role"),
            "profit_col_role": result.score_summary.get("profit_col_role"),
            "unit_raw": result.unit_info.unit_raw if result.unit_info else None,
            "unit_multiplier": result.unit_info.unit_multiplier if result.unit_info else None,
            "header_snapshot": header_snapshot,
            "row_labels_sample": row_labels_sample,
            "extraction_engine": "v2",
            "rule_trace": result.rule_trace[-5:] if result.rule_trace else [],
            "timestamp": datetime.now(JST).isoformat(),
        }

        # column diagnosis (no_sales_profit_columns 用)
        if column_diagnosis:
            record["candidate_column_labels"] = column_diagnosis.get("column_labels", [])
            record["candidate_column_roles"] = column_diagnosis.get("column_roles", [])
            record["column_taxonomy_scores"] = column_diagnosis.get("taxonomy_scores", [])
            record["reconstructed_headers"] = column_diagnosis.get("reconstructed_headers", [])
            record["header_role_fallback_scores"] = column_diagnosis.get("header_role_scores", {})
        else:
            record["candidate_column_labels"] = []
            record["candidate_column_roles"] = []

        with open(review_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    except Exception as e:
        logger.warning(f"[v2] quarantine review 書き込み失敗: {e}")


def _build_column_diagnosis(col_result, reconstructed, data_rows, fallback_scores=None, raw_headers=None):
    """no_sales_profit_columns quarantine 用の column 診断情報を構築"""
    diag = {}
    # raw header rows
    diag["raw_headers"] = list(raw_headers) if raw_headers else []
    # 再構築ヘッダー
    diag["reconstructed_headers"] = list(reconstructed) if reconstructed else []
    # 列ラベル (data_rows の先頭2行から)
    column_labels = []
    if data_rows:
        for row in data_rows[:2]:
            column_labels.append(row)
    diag["column_labels"] = column_labels
    # 列ロール (classify_columns の結果)
    if hasattr(col_result, "column_roles") and col_result.column_roles:
        diag["column_roles"] = [str(r) for r in col_result.column_roles]
    else:
        diag["column_roles"] = []
    # sales/profit candidates
    diag["best_sales_col"] = col_result.best_sales_col
    diag["best_profit_col"] = col_result.best_profit_col
    diag["profit_role"] = col_result.profit_role if hasattr(col_result, "profit_role") else ""
    # taxonomy スコア (列ごとの全roleスコア — 先頭5列のみ)
    if hasattr(col_result, "role_score_breakdown") and col_result.role_score_breakdown:
        diag["taxonomy_scores"] = col_result.role_score_breakdown[:5]
    else:
        diag["taxonomy_scores"] = []

    # --- profit / sales candidate 詳細 ---
    profit_candidates = []
    sales_candidates = []
    _profit_roles = {
        "operating_profit_like", "segment_profit_like", "ordinary_profit_like",
        "pretax_like", "net_income_like",
    }
    _sales_roles = {"sales", "external_sales", "total_sales_like"}
    breakdown = col_result.role_score_breakdown if hasattr(col_result, "role_score_breakdown") else []
    headers_list = list(reconstructed) if reconstructed else []
    for ci, sc in enumerate(breakdown):
        if not isinstance(sc, dict):
            continue
        header = headers_list[ci] if ci < len(headers_list) else ""
        # profit
        profit_scores = {r: round(sc.get(r, 0), 3) for r in _profit_roles if sc.get(r, 0) > 0}
        if profit_scores:
            best_p_role = max(profit_scores, key=lambda r: profit_scores[r])
            profit_candidates.append({
                "col": ci,
                "header": header[:30],
                "scores": profit_scores,
                "best_role": best_p_role,
                "best_score": profit_scores[best_p_role],
                "selected": ci == col_result.best_profit_col,
            })
        # sales
        sales_scores = {r: round(sc.get(r, 0), 3) for r in _sales_roles if sc.get(r, 0) > 0}
        if sales_scores:
            best_s_role = max(sales_scores, key=lambda r: sales_scores[r])
            sales_candidates.append({
                "col": ci,
                "header": header[:30],
                "scores": sales_scores,
                "best_role": best_s_role,
                "best_score": sales_scores[best_s_role],
                "selected": ci == col_result.best_sales_col,
            })
    diag["profit_candidates"] = profit_candidates
    diag["sales_candidates"] = sales_candidates

    # header fallback スコア
    if fallback_scores:
        diag["header_role_scores"] = {str(k): round(v, 3) if isinstance(v, float) else v
                                       for k, v in fallback_scores.items()}
    else:
        diag["header_role_scores"] = {}
    return diag


# ============================================================
# Phase 5: Multi-page Table Merge
# ============================================================

def _try_merge_adjacent_pages(
    table_candidates: list[tuple[TableScore, list[str], str, float, int]],
    pages_data: list[tuple[str, int]],
    page_candidates: list,
) -> list[tuple[TableScore, list[str], str, float, int]]:
    """
    隣接ページのテーブル候補を結合して再スコアリングする。

    結合条件:
      - ページ番号が連続 (page N と N+1)
      - 数値列数が近い (差2以内)
      - 行構造が似ている

    header 継承:
      - 1ページ目にヘッダー行あり + 2ページ目にヘッダーなし → 継承

    Returns:
        merge 候補のリスト (通常候補と同列比較用)
    """
    if len(table_candidates) < 2:
        return []

    merge_results: list[tuple[TableScore, list[str], str, float, int]] = []

    # ページ番号でグルーピング
    by_page: dict[int, list[tuple[TableScore, list[str], str, float, int]]] = {}
    for tc in table_candidates:
        page_no = tc[4]
        by_page.setdefault(page_no, []).append(tc)

    page_numbers = sorted(by_page.keys())

    for i in range(len(page_numbers) - 1):
        pg_a = page_numbers[i]
        pg_b = page_numbers[i + 1]

        # 連続ページのみ
        if pg_b != pg_a + 1:
            continue

        for tc_a in by_page[pg_a]:
            for tc_b in by_page[pg_b]:
                ts_a, lines_a = tc_a[0], tc_a[1]
                ts_b, lines_b = tc_b[0], tc_b[1]

                # 列数チェック
                if abs(ts_a.numeric_col_count - ts_b.numeric_col_count) > 2:
                    continue

                # merge: 2ページ目のヘッダーが弱い場合は1ページ目のヘッダーを継承
                merged_lines = list(lines_a)

                # 2ページ目からヘッダーらしくない行だけを追加
                # (ヘッダー判定: 数値を含まない短い行が先頭にある場合はスキップ)
                skip_count = 0
                for line in lines_b[:3]:
                    stripped = line.strip()
                    if stripped and not re.search(r'[\d,]{3,}', stripped) and len(stripped) < 50:
                        skip_count += 1
                    else:
                        break

                # 2ページ目のデータ行を追加 (ヘッダー部分は最大2行スキップ)
                if skip_count <= 2 and ts_b.has_sales_header:
                    # 2ページ目もヘッダーあり → そのまま結合しない
                    continue

                data_start = min(skip_count, 2)
                merged_lines.extend(lines_b[data_start:])

                # 再スコアリング
                merged_ts = score_segment_table(
                    merged_lines, "", 0, ts_a.start_line, ts_b.end_line
                )

                # merge 後スコアが両方の単ページより高い場合のみ候補に
                if merged_ts.score > max(ts_a.score, ts_b.score):
                    # page_text は1ページ目を使用、page_score は高い方
                    best_ps = max(tc_a[3], tc_b[3])
                    merge_results.append(
                        (merged_ts, merged_lines, tc_a[2], best_ps, pg_a)
                    )

    return merge_results


# ============================================================
# run_segment_detection_v2 (Phase 2 + Phase 5)
# ============================================================

def run_segment_detection_v2(
    pdf_path: str,
    *,
    top_pages: int = 5,
    min_page_score: float = 0.15,
    min_table_score: float = 0.20,
    min_confidence: float = 0.30,
    doc_id: str = "",
    ticker: str = "",
) -> V2DetectionResult:
    """
    Phase A-G 統合: PDF セグメント表自動検出 v2 (Phase 2)。
    """
    result = V2DetectionResult()
    trace: list[str] = []

    # ================================================================
    # Phase A: ページスコアリング
    # ================================================================
    trace.append("Phase A: ページスコアリング開始")

    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_data: list[tuple[str, int]] = []
            # Phase 6: pdfplumber tables も取得して region 不在時に利用
            pages_tables: dict[int, list[list[list[str | None]]]] = {}
            for i, page in enumerate(pdf.pages[:12]):
                page_text = page.extract_text() or ""
                pages_data.append((page_text, i))
                try:
                    raw_tables = page.extract_tables() or []
                    pages_tables[i] = raw_tables
                except Exception:
                    pages_tables[i] = []
    except Exception as e:
        result.quarantine_reason = f"PDF読み込みエラー: {e}"
        result.failed_stage = "page_scoring"
        result.review_hint = "PDFファイルを開けません。"
        result.rule_trace = trace
        return result

    if not pages_data:
        result.quarantine_reason = "テキスト抽出不可"
        result.failed_stage = "page_scoring"
        result.review_hint = "PDFからテキストが抽出できません。OCR対象候補です。"
        result.rule_trace = trace
        return result

    page_scores: list[PageScore] = []
    for text, page_no in pages_data:
        ps = score_segment_page(text, page_no)
        page_scores.append(ps)

    result.scored_pages_count = len(page_scores)

    # Phase 4: ページ連続性ブースト (前後ページの相互補完)
    apply_sequence_boost(page_scores)

    candidates = rank_candidate_pages(page_scores, top_n=top_pages, min_score=min_page_score)

    if not candidates:
        result.quarantine_reason = "no_segment_page_candidate"
        result.failed_stage = "page_scoring"
        result.review_hint = "pdf_no_segment_page_candidate"
        result.rule_trace = trace
        result.score_summary["page_scores"] = [
            {"page": ps.page_no, "score": ps.score} for ps in page_scores
        ]
        write_quarantine_review(result, doc_id=doc_id, ticker=ticker, source_file=pdf_path)
        return result

    trace.append(f"Phase A: 候補ページ {len(candidates)}件 (top={candidates[0].page_no}, score={candidates[0].score:.2f})")

    # ================================================================
    # Phase B: テーブルスコアリング (Phase 5 強化 + Phase 6 pdfplumber tables)
    # ================================================================
    trace.append("Phase B: テーブルスコアリング開始")

    # Phase 5: 全候補テーブルを集めて同列比較
    all_table_candidates: list[tuple[TableScore, list[str], str, float, int]] = []
    # tuple: (TableScore, table_lines, page_text, page_score, page_no)
    total_candidate_tables = 0

    for page_candidate in candidates:
        page_no = page_candidate.page_no
        page_text = pages_data[page_no][0]
        lines = page_text.split("\n")

        regions = find_table_regions(lines)

        if regions:
            # 通常: region ベースの候補
            for idx, (start, end, nearby) in enumerate(regions):
                table_lines = lines[start:end]
                ts = score_segment_table(table_lines, nearby, idx, start, end)
                total_candidate_tables += 1
                if ts.score >= min_table_score or is_weak_evidence_table(ts):
                    all_table_candidates.append(
                        (ts, table_lines, page_text, page_candidate.score, page_no)
                    )
        else:
            # Phase 6: region なし → pdfplumber tables を先に試す
            pdf_tables = pages_tables.get(page_no, [])
            pdfplumber_used = False
            for tbl_idx, raw_tbl in enumerate(pdf_tables):
                tbl_lines = _pdfplumber_table_to_lines(raw_tbl)
                if len(tbl_lines) < 3:
                    continue  # 3行未満は候補外
                ts = score_segment_table(tbl_lines, "", tbl_idx, 0, len(tbl_lines))
                total_candidate_tables += 1
                if ts.score >= min_table_score or is_weak_evidence_table(ts):
                    all_table_candidates.append(
                        (ts, tbl_lines, page_text, page_candidate.score, page_no)
                    )
                    pdfplumber_used = True

            # pdfplumber tables で候補なし → ページ全体テキストを fallback
            if not pdfplumber_used:
                ts = score_segment_table(lines, "", 0, 0, len(lines))
                total_candidate_tables += 1
                if ts.score >= min_table_score or is_weak_evidence_table(ts):
                    all_table_candidates.append(
                        (ts, lines, page_text, page_candidate.score, page_no)
                    )

    # Phase 5: multi-page table merge
    merge_candidates = _try_merge_adjacent_pages(
        all_table_candidates, pages_data, candidates
    )
    for mc in merge_candidates:
        all_table_candidates.append(mc)
        total_candidate_tables += 1

    result.candidate_tables_count = total_candidate_tables

    # Phase 5: 全候補から最高スコアを選択
    if not all_table_candidates:
        result.quarantine_reason = "no_segment_table_candidate"
        result.failed_stage = "table_scoring"
        result.review_hint = "pdf_no_segment_table_candidate"
        result.rule_trace = trace
        # debug: 全候補スコア情報
        result.score_summary["all_table_scores"] = []
        write_quarantine_review(result, doc_id=doc_id, ticker=ticker,
                                source_file=pdf_path, best_table_lines=[])
        return result

    # スコア降順でソート
    all_table_candidates.sort(key=lambda x: x[0].score, reverse=True)
    best_table, best_table_lines, best_page_text, best_page_score, best_page_no = (
        all_table_candidates[0]
    )

    # --- heading block fallback ---
    # top1 が見出しブロック型なら、次の非 heading 候補にフォールバック
    heading_fallback_used = False
    if is_heading_like_table(best_table) and len(all_table_candidates) > 1:
        for alt_idx in range(1, len(all_table_candidates)):
            alt_ts = all_table_candidates[alt_idx][0]
            if not is_heading_like_table(alt_ts) and alt_ts.score >= 0.15:
                trace.append(
                    f"Phase B: heading_fallback top1=heading_like(score={best_table.score:.2f}) "
                    f"→ top{alt_idx+1}(score={alt_ts.score:.2f})"
                )
                best_table, best_table_lines, best_page_text, best_page_score, best_page_no = (
                    all_table_candidates[alt_idx]
                )
                heading_fallback_used = True
                break

    # debug: 全候補のスコア情報を保持
    result.score_summary["all_table_scores"] = [
        {
            "page": c[4],
            "score": round(c[0].score, 3),
            "categories": c[0].score_categories,
            "reason": c[0].reason[:80],
            "weak_evidence": is_weak_evidence_table(c[0]),
            "heading_like": is_heading_like_table(c[0]),
        }
        for c in all_table_candidates[:5]
    ]

    fb_tag = " (heading_fallback)" if heading_fallback_used else ""
    trace.append(f"Phase B: best_table score={best_table.score:.2f} ({best_table.reason}){fb_tag}")

    # ================================================================
    # Phase B-1.5: TOC Guard (目次ページ / TOC テーブル除外)
    # ================================================================
    from .toc_detection import detect_toc_candidate, detect_toc_page

    # candidate 行での TOC チェック
    _toc_cand = detect_toc_candidate(best_table_lines)

    # ページテキスト全体での TOC チェック (candidate 行が局所的で TOC を捉えられない場合の補完)
    _best_page_text = pages_data[best_page_no][0] if best_page_no < len(pages_data) else ""
    _best_page_lines = _best_page_text.split("\n") if _best_page_text else []
    _toc_page = detect_toc_page(_best_page_lines)

    # 統合判定: candidate OR page で TOC
    _is_toc = _toc_cand.is_toc_candidate or _toc_page.is_toc_page
    _toc_reason = _toc_cand.reject_reason or ("toc_page_detected" if _toc_page.is_toc_page else "")

    result.score_summary["toc_guard"] = {
        "is_toc": _is_toc,
        "candidate_toc_lines": _toc_cand.toc_line_count,
        "candidate_toc_ratio": round(_toc_cand.toc_line_ratio, 3),
        "page_toc_lines": _toc_page.toc_line_count,
        "page_toc_score": round(_toc_page.toc_score, 3),
        "page_is_toc": _toc_page.is_toc_page,
        "dotted_leader_count": max(_toc_cand.dotted_leader_count, _toc_page.dotted_leader_count),
        "page_number_like_count": max(_toc_cand.page_number_like_count, _toc_page.page_number_like_count),
        "reject_reason": _toc_reason,
    }

    if _is_toc:
        trace.append(
            f"Phase B-1.5: TOC detected "
            f"cand_toc={_toc_cand.toc_line_count} page_toc={_toc_page.toc_line_count} "
            f"page_score={_toc_page.toc_score:.2f} reason={_toc_reason}"
        )

        # 代替候補で TOC でないものを探す
        _toc_alt_found = False
        for _toc_alt_idx in range(1, min(len(all_table_candidates), 6)):
            _toc_alt_ts, _toc_alt_lines, _toc_alt_text, _, _toc_alt_page = all_table_candidates[_toc_alt_idx]
            _toc_alt_cand = detect_toc_candidate(_toc_alt_lines)
            _toc_alt_page_lines = _toc_alt_text.split("\n") if _toc_alt_text else []
            _toc_alt_page_result = detect_toc_page(_toc_alt_page_lines)
            _alt_is_toc = _toc_alt_cand.is_toc_candidate or _toc_alt_page_result.is_toc_page
            if not _alt_is_toc:
                # TOC でない候補を採用
                best_table = _toc_alt_ts
                best_table_lines = _toc_alt_lines
                best_page_no = _toc_alt_page
                trace.append(
                    f"Phase B-1.5: ACCEPT alt candidate #{_toc_alt_idx+1} "
                    f"(non-TOC, score={_toc_alt_ts.score:.2f})"
                )
                _toc_alt_found = True
                break
            else:
                trace.append(
                    f"Phase B-1.5: alt #{_toc_alt_idx+1} also TOC "
                    f"(cand={_toc_alt_cand.toc_line_count} page={_toc_alt_page_result.toc_line_count})"
                )

        if not _toc_alt_found:
            # 全候補が TOC → quarantine
            result.quarantine_reason = "toc_page_guard"
            result.failed_stage = "toc_guard"
            result.review_hint = "pdf_toc_page_selected"
            result.rule_trace = trace
            trace.append(
                f"Phase B-1.5: REJECT all candidates are TOC pages "
                f"(checked {min(len(all_table_candidates), 6)} candidates)"
            )
            write_quarantine_review(
                result, doc_id=doc_id, ticker=ticker,
                source_file=pdf_path, best_table_lines=best_table_lines,
            )
            return result

    # ================================================================
    # Phase B-2: Candidate Guard (行分類 + narrative/BS/CF 汚染検出)
    # ================================================================
    from .row_classifier import evaluate_candidate_guard, log_candidate_guard, normalize_label
    import re as _re_cg

    # best_table_lines から行ラベルを抽出
    _cg_labels: list[str] = []
    for _cg_line in best_table_lines:
        _cg_stripped = _cg_line.strip()
        if not _cg_stripped:
            continue
        # 数値がある行のラベル部分を抽出
        _cg_m = _re_cg.match(r'^([^\d△▲\-－]*)', _cg_stripped)
        if _cg_m and _cg_m.group(1).strip():
            _cg_labels.append(_cg_m.group(1).strip())
        elif _cg_stripped:
            _cg_labels.append(_cg_stripped)

    cg_result = evaluate_candidate_guard(_cg_labels)
    log_candidate_guard(cg_result, page=best_page_no, table_index=best_table.table_index)

    result.score_summary["candidate_guard"] = {
        "accepted": cg_result.accepted,
        "reject_reason": cg_result.reject_reason,
        "valid_segment_like": cg_result.valid_segment_like,
        "narrative_like": cg_result.narrative_like,
        "bs_cf_like": cg_result.bs_cf_like,
        "detail_breakdown_like": cg_result.detail_breakdown_like,
        "total_or_metric_like": cg_result.total_or_metric_like,
        "garbage_fragment_like": cg_result.garbage_fragment_like,
        "pl_account_like": cg_result.pl_account_like,
        "top_samples": cg_result.top_samples[:5],
    }

    if not cg_result.accepted:
        # candidate guard で reject → review_hint マッピング
        _hint_map = {
            "narrative_guard": "pdf_narrative_block_selected",
            "bs_cf_guard": "pdf_narrative_block_selected",
            "pl_guard": "pdf_pl_table_selected",
            "detail_breakdown_guard": "pdf_segment_like_but_invalid_structure",
            "invalid_structure": "pdf_segment_like_but_invalid_structure",
            "no_valid_segment_rows": "pdf_no_segment_table_after_guard",
            "total_metric_dominant": "pdf_no_segment_table_after_guard",
        }
        raw_hint = _hint_map.get(cg_result.reject_reason, "pdf_no_segment_table_after_guard")
        raw_reason = cg_result.reject_reason

        # Reclassification layer: detail_breakdown_guard のうち表なし narrative page を再分類
        from .hint_reclassifier import reclassify_candidate_failure
        _reclass = reclassify_candidate_failure(
            raw_reason=raw_reason,
            raw_hint=raw_hint,
            valid_segment=cg_result.valid_segment_like,
            narrative=cg_result.narrative_like,
            garbage=cg_result.garbage_fragment_like,
            detail_breakdown=cg_result.detail_breakdown_like,
            bs_cf=cg_result.bs_cf_like,
            pl_account=cg_result.pl_account_like,
            total_or_metric=cg_result.total_or_metric_like,
            has_sales_header=best_table.has_sales_header,
            has_profit_header=best_table.has_profit_header,
        )

        hint = _reclass.final_hint
        result.score_summary["reclassification"] = {
            "raw_reason": raw_reason,
            "raw_hint": raw_hint,
            "final_reason": _reclass.final_reason,
            "final_hint": _reclass.final_hint,
            "reclassified": _reclass.reclassified,
            "basis": _reclass.basis,
        }

        # ================================================================
        # Invalid Structure Rescue: detail/total 優勢でも valid_segment>=2 なら
        # parent row のみ抽出して rescue を試みる
        # ================================================================
        _rescue_reasons = {"detail_breakdown_guard", "invalid_structure"}
        _rescue_attempted = False
        _rescue_succeeded = False
        v = cg_result.valid_segment_like
        n = cg_result.narrative_like
        g = cg_result.garbage_fragment_like
        d = cg_result.detail_breakdown_like
        t = cg_result.total_or_metric_like
        p = cg_result.pl_account_like
        b = cg_result.bs_cf_like
        total_rows = cg_result.total_rows

        if (raw_reason in _rescue_reasons
                and v >= 2
                and (n + g) <= total_rows * 0.5  # narrative/garbage が過半でない
                and p < 3                          # PL 汚染なし
                and b <= 1                         # BS/CF 汚染なし
                and (best_table.has_sales_header or best_table.has_profit_header
                     or best_table.score >= 0.3)):
            _rescue_attempted = True
            trace.append(
                f"Phase B-2: RESCUE attempt: valid={v} detail={d} total={t} "
                f"narr={n} garbage={g} pl={p} bscf={b} "
                f"has_sales_hdr={best_table.has_sales_header} "
                f"has_profit_hdr={best_table.has_profit_header}"
            )
            # 親行のみを残したラベルリストで再評価 — guard を通過させる
            result.score_summary["invalid_structure_rescue"] = {
                "attempted": True,
                "valid_segment": v,
                "detail_breakdown": d,
                "total_or_metric": t,
                "narrative": n,
                "garbage": g,
            }
            # guard をバイパスして Phase C/D に進む (parent filter は Phase F で適用)
            _rescue_succeeded = True
            result.quarantine_reason = ""
            result.failed_stage = ""
            result.review_hint = ""
            trace.append(
                f"Phase B-2: RESCUE accepted — proceeding to Phase C/D with parent-row filter"
            )
            # cg_result.accepted を True 相当にして下へ fall through
        else:
            # rescue 不適格 → 従来通り reject
            if raw_reason in _rescue_reasons:
                result.score_summary["invalid_structure_rescue"] = {
                    "attempted": False,
                    "reason": (
                        f"ineligible: v={v} n={n} g={g} p={p} b={b} "
                        f"hdr_sales={best_table.has_sales_header} "
                        f"hdr_profit={best_table.has_profit_header}"
                    ),
                }

        if not _rescue_succeeded:
            result.quarantine_reason = f"candidate_guard:{_reclass.final_reason}"
            result.failed_stage = "candidate_guard"
            result.review_hint = hint
            result.rule_trace = trace
            trace.append(
                f"Phase B-2: REJECT candidate_guard={raw_reason} "
                f"valid={cg_result.valid_segment_like} narr={cg_result.narrative_like} "
                f"bscf={cg_result.bs_cf_like} garbage={cg_result.garbage_fragment_like}"
            )
            if _reclass.reclassified:
                trace.append(
                    f"Phase B-2: RECLASSIFIED {raw_reason} → {_reclass.final_reason} "
                    f"({_reclass.basis})"
                )

            # 他の候補も試す
            _alt_accepted = False
            for _alt_idx in range(1, min(len(all_table_candidates), 4)):
                _alt_ts, _alt_lines, _, _, _alt_page = all_table_candidates[_alt_idx]
                _alt_labels = []
                for _al in _alt_lines:
                    _als = _al.strip()
                    if not _als:
                        continue
                    _alm = _re_cg.match(r'^([^\d△▲\-－]*)', _als)
                    if _alm and _alm.group(1).strip():
                        _alt_labels.append(_alm.group(1).strip())
                _alt_cg = evaluate_candidate_guard(_alt_labels)
                if _alt_cg.accepted:
                    # この候補を採用
                    best_table = _alt_ts
                    best_table_lines = _alt_lines
                    best_page_no = _alt_page
                    cg_result = _alt_cg
                    trace.append(
                        f"Phase B-2: ACCEPT alt candidate #{_alt_idx+1} "
                        f"valid={_alt_cg.valid_segment_like} score={_alt_ts.score:.2f}"
                    )
                    result.quarantine_reason = ""
                    result.failed_stage = ""
                    result.review_hint = ""
                    _alt_accepted = True
                    break

            if not _alt_accepted:
                write_quarantine_review(
                    result, doc_id=doc_id, ticker=ticker,
                    source_file=pdf_path, best_table_lines=best_table_lines,
                )
                return result

    trace.append(
        f"Phase B-2: ACCEPT valid={cg_result.valid_segment_like} "
        f"narr={cg_result.narrative_like}"
    )

    # ================================================================
    # Phase C: ヘッダーグリッド再構築 (Phase 7 分割ヘッダー復元)
    # ================================================================
    trace.append("Phase C: ヘッダーグリッド再構築")

    # --- Phase B→C 遷移ログ ---
    from .table_scoring import _count_numeric_columns as _cnt_num_cols
    _phase_b_rows = len(best_table_lines)
    _phase_b_num_cols = _cnt_num_cols(best_table_lines)
    _phase_b_preview = [l.strip()[:60] for l in best_table_lines[:3]]

    # Phase 7: 前置き行を除去 (日付/各位/開示定型文)
    from .header_analysis import trim_non_table_preamble
    trimmed_table_lines, preamble_debug = trim_non_table_preamble(best_table_lines)
    if preamble_debug["skipped_count"] > 0:
        trace.append(
            f"Phase C: preamble_trim skipped={preamble_debug['skipped_count']} "
            f"stop_reason={preamble_debug.get('stop_reason', '?')} "
            f"first={preamble_debug['skipped_lines'][:3]}"
        )

    header_band_h = detect_header_band(trimmed_table_lines)
    header_lines = trimmed_table_lines[:header_band_h]
    data_lines = trimmed_table_lines[header_band_h:]

    # Phase 5 legacy: reconstruct_header_grid (fallback 用に保持)
    legacy_reconstructed = reconstruct_header_grid(header_lines)
    header_units = extract_header_units(header_lines)

    # Phase 7 new: 分割ヘッダー復元
    from .header_reconstruction import reconstruct_from_lines
    recon_result = reconstruct_from_lines(header_lines)

    # raw header rows を debug に残す
    raw_header_texts = [h.strip() for h in header_lines]

    # --- CID 破損検出 ---
    # PDF テキスト抽出で (cid:XXXX) が大量に出る場合はフォント埋め込み不良
    _cid_sample = " ".join(best_table_lines[:min(10, len(best_table_lines))])
    _cid_count = _cid_sample.count("(cid:")
    if _cid_count >= 5:
        result.quarantine_reason = "pdf_text_cid_corrupted"
        result.failed_stage = "header_extraction"
        result.review_hint = "pdf_text_cid_corrupted"
        result.rule_trace = trace
        trace.append(f"Phase C: CID corrupted detected (cid_count={_cid_count} in top 10 lines)")
        return result

    # 新旧ヘッダーを両方保持
    new_reconstructed = recon_result.reconstructed_headers
    reconstruction_steps = recon_result.steps

    header_conf = 0.5
    if len(new_reconstructed) >= 2:
        header_conf += 0.2
    if header_units:
        header_conf += 0.1

    trace.append(f"Phase C: band_h={header_band_h}, raw={raw_header_texts[:3]}, reconstructed={new_reconstructed[:5]}, units={header_units}")
    if reconstruction_steps:
        for step in reconstruction_steps[:3]:
            trace.append(f"Phase C: merge {step.get('type','?')} {step.get('parts',[])} -> {step.get('result','?')} score={step.get('score','?')}")

    # --- Helper: text-only token 抽出 (数値/注記/section除外) ---
    def _extract_text_tokens(line: str) -> list[str]:
        """行から text-only token を抽出し、数値/注記/section語を除外する。"""
        tokens = re.split(r'\s{2,}|\t', line.strip())
        text_tokens = []
        for t in tokens:
            t_s = t.strip()
            if not t_s:
                continue
            _t_c = t_s.replace(",", "").replace("△", "-").replace("▲", "-").replace("－", "-")
            if _NUM_PATTERN.fullmatch(_t_c):
                continue
            if re.fullmatch(r'[△▲\-－]?[\d,]+\.?\d*[%％]?', _t_c):
                continue
            if re.match(r'^[（(]注[）)]', t_s):
                continue
            text_tokens.append(t_s)
        return text_tokens

    # --- Helper: dual-metric header 分割 ---
    _SALES_SPLIT_KW = ["売上高", "売上収益", "営業収益", "経常収益",
                        "外部顧客への売上高", "外部顧客への売上収益"]
    _PROFIT_SPLIT_KW = ["セグメント利益", "セグメント損失", "セグメント利益又は損失",
                         "セグメント損益", "営業利益", "営業損失", "事業利益",
                         "コア営業利益", "経常利益", "利益又は損失", "EBITDA",
                         "セグメント利益(△損失)", "セグメント利益（△損失）"]

    def _split_dual_metric_headers(headers: list[str]) -> list[str]:
        """売上系語+利益系語が同一 header に共存する場合、分割する。"""
        result_hdrs = []
        for h in headers:
            found_sales = None
            found_profit = None
            for kw in sorted(_SALES_SPLIT_KW, key=len, reverse=True):
                if kw in h:
                    found_sales = kw
                    break
            for kw in sorted(_PROFIT_SPLIT_KW, key=len, reverse=True):
                if kw in h:
                    found_profit = kw
                    break
            if found_sales and found_profit and found_sales != found_profit:
                result_hdrs.append(found_sales)
                result_hdrs.append(found_profit)
            else:
                result_hdrs.append(h)
        return result_hdrs

    # --- Phase C-2: descriptive_only_header ガード ---
    from .header_reconstruction import is_descriptive_segment_header, split_header_rows_for_role_detection
    if is_descriptive_segment_header(raw_header_texts) and is_descriptive_segment_header(new_reconstructed):
        trace.append("Phase C-2: descriptive_only_header detected, scanning lower rows")

        _METRIC_A_KW = [
            "売上高", "売上収益", "営業収益", "経常収益",
            "外部顧客への売上高", "外部顧客への売上収益",
        ]
        _METRIC_B_KW = [
            "セグメント利益", "セグメント損失", "セグメント利益又は損失",
            "セグメント損益", "営業利益", "営業損失",
            "事業利益", "事業損失", "コア営業利益", "経常利益",
            "利益又は損失", "EBITDA",
        ]
        _METRIC_C_KW = [
            "その他", "調整額", "合計", "全社", "消去",
            "報告セグメント情報", "報告セグメントに関する情報",
            "セグメント資産", "減価償却費", "設備投資",
        ]

        _scan_limit = min(15, len(data_lines))
        _recovered_a: list[str] = []
        _recovered_b: list[str] = []
        _recovered_c: list[str] = []
        _num_dense_rows = 0

        for _dl in data_lines[:_scan_limit]:
            _dls = _dl.strip()
            if not _dls:
                continue
            _tokens = re.split(r'\s{2,}|\t', _dls)
            _num_t = sum(1 for t in _tokens if t.strip() and _NUM_PATTERN.search(t))
            if len(_tokens) >= 2 and _num_t / len(_tokens) > 0.5:
                _num_dense_rows += 1
                if _num_dense_rows >= 2:
                    trace.append("Phase C-2: stopped at dense numeric block")
                    break
                continue

            has_a = any(kw in _dls for kw in _METRIC_A_KW)
            has_b = any(kw in _dls for kw in _METRIC_B_KW)
            has_c = any(kw in _dls for kw in _METRIC_C_KW)

            if has_a or has_b:
                text_tokens = _extract_text_tokens(_dls)
                if text_tokens:
                    if has_a:
                        _recovered_a.extend(text_tokens)
                        trace.append(f"Phase C-2: recovered A (sales): {text_tokens}")
                    elif has_b:
                        _recovered_b.extend(text_tokens)
                        trace.append(f"Phase C-2: recovered B (profit): {text_tokens}")
            elif has_c:
                _recovered_c.append(_dls)

        # primary header = A + B tokens を直接使用 (reconstruct_from_lines を通さない)
        _primary_tokens = _recovered_a + _recovered_b
        if _primary_tokens:
            new_reconstructed = _split_dual_metric_headers(_primary_tokens)
            trace.append(
                f"Phase C-2: direct tokens A={len(_recovered_a)} B={len(_recovered_b)} "
                f"C(skipped)={len(_recovered_c)} → headers={new_reconstructed[:5]}"
            )
        else:
            trace.append(
                f"Phase C-2: no A/B metric headers found "
                f"(C only={len(_recovered_c)}); skipping helper-only reconstruction"
            )

    # ================================================================
    # Unit Detection (Phase 2)
    # ================================================================
    unit_result = detect_unit_for_table(
        page_text=best_page_text,
        table_headers=header_lines,
        nearby_text="\n".join(best_table_lines[:3]),
    )
    result.unit_info = unit_result
    trace.append(f"Unit: raw={unit_result.unit_raw}, mult={unit_result.unit_multiplier}, src={unit_result.unit_source}")

    # --- Phase C 後遷移ログ ---
    _phase_c_rows = len(trimmed_table_lines)
    _phase_c_num_cols = _cnt_num_cols(trimmed_table_lines)
    _phase_c_preview = [l.strip()[:60] for l in trimmed_table_lines[:3]]
    trace.append(
        f"Phase C: transition B({_phase_b_rows}rows,{_phase_b_num_cols}cols)→C({_phase_c_rows}rows,{_phase_c_num_cols}cols) "
        f"header_band={header_band_h} new_headers={new_reconstructed[:3]}"
    )
    result.score_summary["phase_transition"] = {
        "phase_b_rows": _phase_b_rows,
        "phase_b_num_cols": _phase_b_num_cols,
        "phase_b_preview": _phase_b_preview,
        "phase_c_rows": _phase_c_rows,
        "phase_c_num_cols": _phase_c_num_cols,
        "phase_c_preview": _phase_c_preview,
        "header_band_h": header_band_h,
        "best_table_has_sales": best_table.has_sales_header,
        "best_table_has_profit": best_table.has_profit_header,
    }

    # ================================================================
    # Phase D: 列ロール分類 (Phase 7 二段構え: new → legacy fallback)
    # ================================================================
    trace.append("Phase D: 列ロール分類")

    data_rows: list[list[str]] = []
    for line in data_lines:
        if not line.strip():
            continue
        tokens = re.split(r'\s{2,}|\t', line.strip())
        tokens = [t.strip() for t in tokens]
        data_rows.append(tokens)

    # Phase D-0: header splitting — 期間/比較行を secondary に分離
    _header_split = split_header_rows_for_role_detection(raw_header_texts)
    _primary_header_rows = _header_split["primary"]
    _secondary_header_rows = _header_split["secondary"]
    _primary_reconstructed = None

    trace.append(
        f"Phase D-0: split result primary={len(_primary_header_rows)} secondary={len(_secondary_header_rows)}"
    )

    _D0_METRIC_KW = [
        "売上高", "売上収益", "営業収益", "経常収益",
        "営業利益", "セグメント利益", "事業利益", "コア営業利益",
        "経常利益", "セグメント損益", "利益又は損失", "EBITDA",
        "外部顧客への売上高", "外部顧客への売上収益",
    ]

    if _secondary_header_rows and _primary_header_rows:
        trace.append(f"Phase D-0: secondary_headers={[h.strip()[:60] for h in _secondary_header_rows]}")
        # primary rows から再構築
        recon_primary = reconstruct_from_lines(_primary_header_rows)
        _primary_reconstructed = recon_primary.reconstructed_headers
        if _primary_reconstructed:
            trace.append(f"Phase D-0: primary_reconstructed={_primary_reconstructed[:5]}")
            # メトリクス妥当性検証
            _primary_text = " ".join(_primary_reconstructed)
            _has_metric = any(kw in _primary_text for kw in _D0_METRIC_KW)
            if not _has_metric:
                trace.append("Phase D-0: primary_split_empty_or_non_metric, discarding primary_reconstructed")
                _primary_reconstructed = None
        else:
            trace.append("Phase D-0: primary_reconstructed is empty after reconstruction")
            _primary_reconstructed = None
    elif _secondary_header_rows and not _primary_header_rows:
        # 全行 secondary → primary 空 → split 失敗
        trace.append("Phase D-0: primary_split_empty (all rows are secondary), falling back")

    # D-0 body metric recovery: primary_split 失敗/empty 時に data_lines からメトリクス行を探索
    if _primary_reconstructed is None and _secondary_header_rows:
        trace.append("Phase D-0: body_metric_recovery starting")
        _body_metric_tokens: list[str] = []
        _body_scan = min(15, len(data_lines))
        _body_num_dense = 0
        for _bml in data_lines[:_body_scan]:
            _bmls = _bml.strip()
            if not _bmls:
                continue
            # 数値密度停止
            _bm_tokens = re.split(r'\s{2,}|\t', _bmls)
            _bm_num = sum(1 for t in _bm_tokens if t.strip() and _NUM_PATTERN.search(t))
            if len(_bm_tokens) >= 2 and _bm_num / len(_bm_tokens) > 0.5:
                _body_num_dense += 1
                if _body_num_dense >= 2:
                    break
                continue
            # 注記/section title 除外
            if re.match(r'^[（(]注[）)]', _bmls):
                continue
            if "の内訳" in _bmls or "内訳は" in _bmls:
                continue
            # メトリクス語チェック → text-only tokens
            if any(kw in _bmls for kw in _D0_METRIC_KW):
                _bm_text = _extract_text_tokens(_bmls)
                if _bm_text:
                    _body_metric_tokens.extend(_bm_text)
                    trace.append(f"Phase D-0: body_metric_recovery found: {_bm_text}")

        if _body_metric_tokens:
            # dual-metric split して直接使用 (reconstruct_from_lines を通さない)
            new_reconstructed = _split_dual_metric_headers(_body_metric_tokens)
            trace.append(f"Phase D-0: body_metric_recovery_headers={new_reconstructed[:5]}")
        else:
            trace.append("Phase D-0: body_metric_recovery found nothing")

    # dual-metric split を new_reconstructed にも適用 (C-2 以外の経路用)
    if new_reconstructed:
        _pre_split = new_reconstructed[:]
        new_reconstructed = _split_dual_metric_headers(new_reconstructed)
        if new_reconstructed != _pre_split:
            trace.append(f"Phase D: dual_metric_split: {_pre_split[:3]} → {new_reconstructed[:5]}")

    # 1) new 方式で試行 (primary_reconstructed があればそれを優先)
    _headers_for_classify = _primary_reconstructed if _primary_reconstructed else new_reconstructed
    col_result = classify_columns(data_rows, _headers_for_classify)
    resolution_strategy = "new" if _primary_reconstructed is None else "primary_split"

    # 2) primary_split で不十分なら new_reconstructed で再試行
    if not col_result.has_sales and not col_result.has_profit and _primary_reconstructed:
        col_result_full = classify_columns(data_rows, new_reconstructed)
        if col_result_full.has_sales or col_result_full.has_profit:
            col_result = col_result_full
            resolution_strategy = "new"
            trace.append("Phase D: primary_split unresolved, falling back to full new_reconstructed")

    # 3) new が不十分なら legacy fallback
    if not col_result.has_sales and not col_result.has_profit:
        col_result_legacy = classify_columns(data_rows, legacy_reconstructed)
        if col_result_legacy.has_sales or col_result_legacy.has_profit:
            col_result = col_result_legacy
            resolution_strategy = "legacy_fallback"
            trace.append("Phase D: legacy fallback adopted (new method unresolved)")

    # debug: 使用ヘッダーを記録
    active_headers = _headers_for_classify if resolution_strategy == "primary_split" else (
        new_reconstructed if resolution_strategy == "new" else legacy_reconstructed
    )
    trace.append(f"Phase D: resolution_strategy={resolution_strategy}")

    # --- Phase D-0b: numeric-only header + slide 語 → explanation_slide 再分類 ---
    _explanation_slide_forced = False
    if col_result.best_sales_col is None and col_result.best_profit_col is None:
        _non_empty_headers = [h.strip() for h in (new_reconstructed or []) if h.strip()]
        _all_headers_numeric = (
            len(_non_empty_headers) > 0
            and all(
                _NUM_PATTERN.fullmatch(h.replace(",", "").replace("△", "").replace("▲", ""))
                for h in _non_empty_headers
            )
        )
        if _all_headers_numeric:
            _SLIDE_CHECK_KW = [
                "決算説明資料", "四半期業績推移", "業績推移", "財務データ",
                "見通し", "事業体制", "説明資料",
            ]
            _preamble_text = " ".join(best_table_lines[:5]) if best_table_lines else ""
            _slide_in_context = any(
                (kw in best_page_text) or (kw in _preamble_text) for kw in _SLIDE_CHECK_KW
            )
            if _slide_in_context:
                best_table.non_segment_type = "explanation_slide"
                _explanation_slide_forced = True
                result.quarantine_reason = "non_segment_table_explanation_slide"
                result.review_hint = "pdf_non_segment_table"
                trace.append("Phase D-0b: explanation_slide forced (quarantine_reason + review_hint set)")
            else:
                trace.append("Phase D-0b: numeric_only_header but no slide context")

    # debug 情報 — 列診断を常に保持
    col_diag = _build_column_diagnosis(
        col_result, active_headers, data_rows, raw_headers=raw_header_texts
    )
    col_diag["resolution_strategy"] = resolution_strategy
    col_diag["legacy_headers"] = legacy_reconstructed[:5]
    col_diag["new_headers"] = new_reconstructed[:5]
    col_diag["reconstruction_steps"] = reconstruction_steps[:5]
    result.score_summary["column_diagnosis"] = col_diag

    # ================================================================
    # Phase D-2: strong-table fallback
    # ================================================================
    # Phase B で strong table (numeric_cols>=2 + sales/profit header) なのに
    # Phase C/D で sales/profit が立たない場合、Phase B 元テーブルで再 classify
    _strong_table_fallback_used = False
    if (not col_result.has_sales and not col_result.has_profit
            and _phase_b_num_cols >= 2
            and (best_table.has_sales_header or best_table.has_profit_header)):
        # Phase B 元テーブルから直接 data_rows を作成
        _fb_data_rows: list[list[str]] = []
        for _fbl in best_table_lines:
            _fbl_s = _fbl.strip()
            if not _fbl_s:
                continue
            _fb_tokens = re.split(r'\s{2,}|\t', _fbl_s)
            _fb_tokens = [t.strip() for t in _fb_tokens]
            _fb_data_rows.append(_fb_tokens)
        # Phase B 元行からヘッダーを取得
        _fb_headers = [row[0] if row else "" for row in _fb_data_rows[:3]]
        _fb_col_result = classify_columns(_fb_data_rows, _fb_headers)
        if _fb_col_result.has_sales or _fb_col_result.has_profit:
            col_result = _fb_col_result
            resolution_strategy = "phase_b_fallback"
            _strong_table_fallback_used = True
            trace.append(
                f"Phase D: fallback rerun column diagnosis on Phase B source table "
                f"sales_col={_fb_col_result.best_sales_col} profit_col={_fb_col_result.best_profit_col}"
            )
            # data_rows も Phase B 元に差替
            data_rows = _fb_data_rows
            active_headers = _fb_headers
            # col_diag 再構築
            col_diag = _build_column_diagnosis(
                col_result, active_headers, data_rows, raw_headers=raw_header_texts
            )
            col_diag["resolution_strategy"] = resolution_strategy
            result.score_summary["column_diagnosis"] = col_diag

    _fallback_scores_for_quarantine: dict = {}

    if not col_result.has_sales and not col_result.has_profit:
        full_header_text = "\n".join(best_table_lines[:min(10, len(best_table_lines))])
        from .header_analysis import score_header_role
        fallback_scores = score_header_role(full_header_text)
        has_sales_fb = fallback_scores.get("sales", 0) >= 0.3
        has_profit_fb = any(
            fallback_scores.get(r, 0) >= 0.3
            for r in ["operating_profit", "segment_profit", "ordinary_profit"]
        )

        # 2-3列表の簡易推定: 数値列が2つなら sales + profit とみなす
        if not has_sales_fb and not has_profit_fb:
            # data_rows からまず試す
            sample_rows = data_rows[:5] if data_rows else []
            if not sample_rows:
                # data_rows が空なら best_table_lines 全体から試す
                sample_rows = [
                    re.split(r'\s{2,}|\t', l.strip())
                    for l in best_table_lines[:10]
                    if _NUM_PATTERN.search(l)
                ]
            num_cols = []
            for row in sample_rows:
                nc = sum(1 for cell in row if _NUM_PATTERN.search(cell if isinstance(cell, str) else ""))
                num_cols.append(nc)
            avg_num_cols = sum(num_cols) / max(len(num_cols), 1)
            if avg_num_cols >= 1.5:
                has_sales_fb = True
                if avg_num_cols >= 2.5:
                    has_profit_fb = True
                    trace.append(f"Phase D: 簡易推定 (avg_num_cols={avg_num_cols:.1f}) → sales+profit")
                else:
                    trace.append(f"Phase D: 簡易推定 (avg_num_cols={avg_num_cols:.1f}) → sales only")

        if not has_sales_fb and not has_profit_fb:
            # D-3/D-4/D-5 を経由させるため early return せず flag を立てる
            _fallback_scores_for_quarantine = fallback_scores
            trace.append("Phase D: header/numeric fallback failed, deferring quarantine to post-fallbacks")

        # sales のみでも続行 (partial rescue)
        if has_sales_fb and not has_profit_fb:
            trace.append("Phase D: sales only - partial rescue mode")

        # fallback 結果を col_result に反映 (numeric layout fallback)
        # has_sales / has_profit / profit_role は read-only property なので
        # underlying フィールドのみ更新する
        try:
            if has_sales_fb and col_result.best_sales_col is None:
                col_result.best_sales_col = 0
                trace.append("Phase D: column_fallback_numeric_layout sales_col=0")
            if has_profit_fb and col_result.best_profit_col is None:
                col_result.best_profit_col = 1
                # column_roles を必要サイズまで拡張して role を設定
                while len(col_result.column_roles) <= 1:
                    col_result.column_roles.append("unknown")
                col_result.column_roles[1] = "numeric_layout_fallback"
                trace.append("Phase D: column_fallback_numeric_layout profit_col=1")
        except Exception as e:
            trace.append(f"Phase D: column_fallback_numeric_layout error={e}")

        trace.append("Phase D: ヘッダー全文フォールバックで列推定")

    # ================================================================
    # Phase D-3: profit near-sales recheck (強化版)
    # ================================================================
    # sales_col のみ立って profit_col=None のとき、隣接列を緩和閾値で再探索
    if col_result.best_sales_col is not None and col_result.best_profit_col is None:
        _sc = col_result.best_sales_col
        trace.append(f"Phase D-3: profit near-sales recheck start sales_col={_sc}")
        _recheck_candidates = []
        _recheck_details = []  # debug 用
        for _off in [1, 2, -1, -2, 3, -3]:
            _adj = _sc + _off
            if 0 <= _adj < len(col_result.role_score_breakdown):
                _adj_scores = col_result.role_score_breakdown[_adj]
                _adj_role = col_result.column_roles[_adj] if _adj < len(col_result.column_roles) else "unknown"
                _adj_header = ""
                if active_headers and _adj < len(active_headers):
                    _adj_header = active_headers[_adj]
                # ratio/yoy/margin/assets は除外
                if _adj_role in ("ratio", "yoy", "margin_like", "assets_like",
                                 "depreciation_like", "capex_like", "segment_label"):
                    _recheck_details.append(
                        f"col={_adj} role={_adj_role} header={_adj_header!r} → skip (excluded role)"
                    )
                    continue
                for _pr in ["operating_profit_like", "segment_profit_like",
                            "ordinary_profit_like", "pretax_like", "net_income_like"]:
                    _ps = _adj_scores.get(_pr, 0)
                    if _ps >= 0.03:  # 非常に緩い閾値で候補集め
                        _recheck_candidates.append((_adj, _pr, _ps, _adj_header, _adj_role))
                # 候補なしの場合も記録
                _best_pr_score = max((_adj_scores.get(r, 0) for r in
                    ["operating_profit_like", "segment_profit_like",
                     "ordinary_profit_like", "pretax_like", "net_income_like"]), default=0)
                _recheck_details.append(
                    f"col={_adj} role={_adj_role} header={_adj_header!r} "
                    f"best_profit_score={_best_pr_score:.3f}"
                )

        # 候補を score_summary に記録
        result.score_summary["profit_recheck_candidates"] = [
            {"col": c[0], "role": c[1], "score": round(c[2], 3),
             "header": c[3], "current_role": c[4]}
            for c in _recheck_candidates
        ]

        if _recheck_candidates:
            _recheck_candidates.sort(key=lambda x: x[2], reverse=True)
            _best_rc = _recheck_candidates[0]
            # 採用閾値: 0.05 以上で採用
            if _best_rc[2] >= 0.05:
                col_result.best_profit_col = _best_rc[0]
                while len(col_result.column_roles) <= _best_rc[0]:
                    col_result.column_roles.append("unknown")
                col_result.column_roles[_best_rc[0]] = _best_rc[1]
                trace.append(
                    f"Phase D-3: profit near-sales selected col={_best_rc[0]} "
                    f"role={_best_rc[1]} score={_best_rc[2]:.3f} "
                    f"header={_best_rc[3]!r} candidates={len(_recheck_candidates)}"
                )
            else:
                trace.append(
                    f"Phase D-3: profit near-sales candidates found but below threshold "
                    f"best_score={_best_rc[2]:.3f} candidates={len(_recheck_candidates)}"
                )
        else:
            trace.append("Phase D-3: profit near-sales no candidate")

    # ================================================================
    # Phase D-4: numeric virtual columns reconstruction
    # ================================================================
    # column_roles が segment_label のみだが Phase B で numeric_cols >= 2 なら
    # best_table_lines から直接数値位置を見て仮想列再構成
    _effective_roles = [r for r in col_result.column_roles if r != "unknown"]
    if (set(_effective_roles) <= {"segment_label", ""}
            and _phase_b_num_cols >= 2
            and col_result.best_sales_col is None):
        # 各行の数値セル位置を集計
        _num_positions: list[list[int]] = []
        for _vl in best_table_lines:
            _vtokens = re.split(r'\s{2,}|\t', _vl.strip())
            _npos = [i for i, t in enumerate(_vtokens)
                     if t.strip() and _NUM_PATTERN.search(t)]
            if _npos:
                _num_positions.append(_npos)
        if _num_positions:
            # 最頻出の数値列数を取得
            from collections import Counter
            _col_counts = Counter(len(p) for p in _num_positions)
            _most_common_ncols = _col_counts.most_common(1)[0][0]
            if _most_common_ncols >= 2:
                # 仮想的に sales=最初の数値列, profit=2番目の数値列
                col_result.best_sales_col = 0
                col_result.best_profit_col = 1
                while len(col_result.column_roles) <= 1:
                    col_result.column_roles.append("unknown")
                col_result.column_roles[0] = "sales"
                col_result.column_roles[1] = "virtual_profit"
                trace.append(
                    f"Phase D: virtual columns reconstruction "
                    f"most_common_ncols={_most_common_ncols} "
                    f"num_rows={len(_num_positions)}"
                )
            elif _most_common_ncols == 1:
                col_result.best_sales_col = 0
                while len(col_result.column_roles) <= 0:
                    col_result.column_roles.append("unknown")
                col_result.column_roles[0] = "sales"
                trace.append(
                    f"Phase D: virtual columns reconstruction (sales only) "
                    f"num_rows={len(_num_positions)}"
                )

    # ================================================================
    # Phase D-5: ratio misclassification guard
    # ================================================================
    # ratio 判定のみで sales/profit が全滅 → ratio を抑制して再 classify
    if (col_result.best_sales_col is None
            and col_result.best_profit_col is None
            and any(r in ("ratio", "margin_like") for r in col_result.column_roles)
            and (best_table.has_sales_header or best_table.has_profit_header)):
        # ratio/margin スコアを半減させて再 classify
        _ratio_guard_data = data_rows if data_rows else [
            re.split(r'\s{2,}|\t', l.strip()) for l in best_table_lines
            if l.strip()
        ]
        _ratio_guard_result = classify_columns(_ratio_guard_data, active_headers)
        # ratio/margin を取り除いた後のスコアで再判定
        if _ratio_guard_result.has_sales or _ratio_guard_result.has_profit:
            # ratio が本当に支配的か確認
            _ratio_dominated = all(
                r in ("ratio", "margin_like", "unknown", "segment_label", "")
                for r in col_result.column_roles
            )
            if _ratio_dominated:
                col_result = _ratio_guard_result
                trace.append("Phase D: ratio misclassification guard applied")

    # ================================================================
    # Phase D-final: 最終 quarantine 判定 (全 fallback 後)
    # ================================================================
    if col_result.best_sales_col is None and col_result.best_profit_col is None:
        # true no-sales/no-profit → quarantine
        result.quarantine_reason = "segment_table_found_but_no_sales_profit_columns"
        result.failed_stage = "column_classification"
        # non_segment_type に応じて review_hint を分類
        _nst = best_table.non_segment_type
        if _nst == "company_profile":
            result.quarantine_reason = "non_segment_table_company_profile"
            result.review_hint = "pdf_non_segment_table"
        elif _nst == "correction_or_notice":
            result.quarantine_reason = "non_segment_table_correction_or_notice"
            result.review_hint = "pdf_non_segment_table"
        elif _nst == "narrative_text":
            result.quarantine_reason = "non_segment_table_narrative_text"
            result.review_hint = "pdf_non_segment_table"
        elif _nst == "explanation_slide":
            result.quarantine_reason = "non_segment_table_explanation_slide"
            result.review_hint = "pdf_non_segment_table"
        else:
            result.review_hint = "pdf_no_sales_profit_columns"
        result.rule_trace = trace
        result.score_summary["column_scores"] = col_result.role_score_breakdown
        result.score_summary["profit_col_role"] = col_result.profit_role
        result.score_summary["non_segment_type"] = _nst
        _fb_scores = _fallback_scores_for_quarantine
        col_diag = _build_column_diagnosis(col_result, active_headers, data_rows, _fb_scores, raw_headers=raw_header_texts)
        write_quarantine_review(result, doc_id=doc_id, ticker=ticker,
                                source_file=pdf_path, best_table_lines=best_table_lines,
                                column_diagnosis=col_diag)
        return result

    # ================================================================
    # Phase D-6: profit inference from value distribution
    # ================================================================
    # sales_col はあるが profit_col=None の場合、隣接 numeric 列の
    # 値分布から ratio (小数/百分率) を除外して profit を推定する。
    if col_result.best_sales_col is not None and col_result.best_profit_col is None:
        _sc_idx = col_result.best_sales_col
        trace.append(f"Phase D-6: profit inference start sales_col={_sc_idx} num data_rows={len(data_rows)}")

        # data_rows の各列の値を収集
        _d6_candidates: list[tuple[int, float, str]] = []  # (col_idx, score, reason)

        for _ci in range(len(col_result.column_roles)):
            if _ci == _sc_idx:
                continue
            _role = col_result.column_roles[_ci] if _ci < len(col_result.column_roles) else ""
            # segment_label は除外
            if _role == "segment_label":
                continue

            # 列の値を収集
            _vals: list[float] = []
            _pct_count = 0
            _decimal_count = 0
            _total_cells = 0
            for _dr in data_rows:
                if _ci < len(_dr):
                    _cell = _dr[_ci].strip()
                    if not _cell:
                        continue
                    _total_cells += 1
                    if "%" in _cell or "％" in _cell:
                        _pct_count += 1
                    if "." in _cell:
                        _decimal_count += 1
                    _clean = _cell.replace(",", "").replace("△", "-").replace("▲", "-").replace("－", "-").replace("(", "-").replace(")", "").replace("（", "-").replace("）", "")
                    try:
                        _vals.append(float(_clean))
                    except ValueError:
                        pass

            if not _vals or _total_cells < 2:
                continue

            # ratio 判定: 値が小さい (-200～200) かつ % or 小数が多い
            _abs_vals = [abs(v) for v in _vals]
            _median = sorted(_abs_vals)[len(_abs_vals) // 2]
            _is_ratio_like = False
            if _pct_count / _total_cells >= 0.3:
                _is_ratio_like = True
            elif _decimal_count / _total_cells >= 0.5 and _median < 200:
                _is_ratio_like = True
            elif _median < 10 and max(_abs_vals) < 200:
                _is_ratio_like = True

            # ヘッダーに「率」「%」「比」があれば ratio
            _hdr = ""
            if _ci < len(col_result.column_roles):
                # active_headers から取得
                if active_headers and _ci < len(active_headers):
                    _hdr = active_headers[_ci]
            _hdr_lower = _hdr.replace(" ", "").lower() if _hdr else ""
            if any(rk in _hdr_lower for rk in ["率", "%", "％", "比", "ratio", "margin"]):
                _is_ratio_like = True

            # ヘッダーに profit KW があれば強候補
            _has_profit_kw = False
            if _hdr:
                _profit_kws = ["利益", "損失", "損益", "profit", "income", "loss"]
                _ratio_override_kws = ["率", "margin"]
                _hdr_has_profit = any(pk in _hdr_lower for pk in _profit_kws)
                _hdr_has_ratio_override = any(rk in _hdr_lower for rk in _ratio_override_kws)
                if _hdr_has_profit and not _hdr_has_ratio_override:
                    _has_profit_kw = True
                    _is_ratio_like = False  # profit KW があれば ratio 判定を override

            if _is_ratio_like:
                trace.append(f"Phase D-6: col={_ci} skipped (ratio-like: median={_median:.1f} pct={_pct_count} decimal={_decimal_count})")
                continue

            # profit スコア計算
            _adjacency_score = max(0, 1.0 - abs(_ci - _sc_idx) * 0.3)
            _fill_score = len(_vals) / max(_total_cells, 1)
            _kw_score = 0.5 if _has_profit_kw else 0.0
            _d6_score = _adjacency_score * 0.3 + _fill_score * 0.3 + _kw_score * 0.4

            _d6_candidates.append((_ci, _d6_score, f"adj={_adjacency_score:.2f} fill={_fill_score:.2f} kw={_kw_score:.2f} median={_median:.0f}"))

        if _d6_candidates:
            _d6_candidates.sort(key=lambda x: x[1], reverse=True)
            _best_d6 = _d6_candidates[0]
            if _best_d6[1] >= 0.15:
                col_result.best_profit_col = _best_d6[0]
                while len(col_result.column_roles) <= _best_d6[0]:
                    col_result.column_roles.append("unknown")
                col_result.column_roles[_best_d6[0]] = "segment_profit_like"
                trace.append(
                    f"Phase D-6: ACCEPT profit_col={_best_d6[0]} score={_best_d6[1]:.3f} ({_best_d6[2]}) "
                    f"candidates={len(_d6_candidates)}"
                )
            else:
                trace.append(
                    f"Phase D-6: no candidate above threshold "
                    f"best_score={_best_d6[1]:.3f} candidates={len(_d6_candidates)}"
                )
        else:
            trace.append("Phase D-6: no numeric columns to evaluate")

    # sales-only 正式化
    if col_result.best_sales_col is not None and col_result.best_profit_col is None:
        trace.append("Phase D: accepted sales-only fallback (parse_quality=partial_sales_only)")


    trace.append(f"Phase D: sales_col={col_result.best_sales_col}, profit_col={col_result.best_profit_col}, role={col_result.profit_role}")

    # ================================================================
    # Phase E: 行ロール分類
    # ================================================================
    trace.append("Phase E: 行ロール分類")

    label_col_idx = col_result.label_col_candidates[0] if col_result.label_col_candidates else 0

    row_result = classify_rows(
        best_table_lines,
        label_col_idx=label_col_idx,
        header_band_height=header_band_h,
    )

    if row_result.extractable_count == 0:
        result.quarantine_reason = "segment_table_found_but_no_rows_extracted"
        result.failed_stage = "row_classification"
        result.review_hint = "セグメント名行が検出されません。行ラベルパターンの確認を。"
        result.rule_trace = trace
        result.score_summary["row_roles"] = [
            {"row": r.row_index, "role": r.role, "label": r.label}
            for r in row_result.rows
        ]
        write_quarantine_review(result, doc_id=doc_id, ticker=ticker,
                                source_file=pdf_path, best_table_lines=best_table_lines)
        return result

    trace.append(f"Phase E: 抽出可能行={row_result.extractable_count}, skip行={len(row_result.skip_rows)}, non_reportable={row_result.non_reportable_count}")

    # ================================================================
    # Phase F: セグメントレコード組立 (Phase 2 拡張)
    # ================================================================
    trace.append("Phase F: レコード組立")

    segments: list[SegmentRecordV2] = []
    order = 0

    # 単位: unit_detection 優先、フォールバックに header_units
    unit_multiplier = unit_result.unit_multiplier
    unit_raw = unit_result.unit_raw

    if unit_multiplier is None and header_units:
        # legacy fallback
        from .header_analysis import detect_unit_annotations
        legacy_unit = detect_unit_annotations(best_page_text) or header_units
        unit_raw = legacy_unit

    # sales / profit col role 名
    sales_col_role = ""
    if col_result.best_sales_col is not None and col_result.best_sales_col < len(col_result.column_roles):
        sales_col_role = col_result.column_roles[col_result.best_sales_col]
    profit_col_role = col_result.profit_role

    # ================================================================
    # Phase F pre-scan: profit inference for text-line tables
    # ================================================================
    # sales_only の場合、テキスト行の nums[1+] が ratio か profit かを事前判定
    _pf_profit_idx: int | None = None  # nums 内の profit 位置 (0-indexed)
    if col_result.has_sales and not col_result.has_profit:
        # 全セグメント行の nums を収集
        _pf_all_nums: list[list[float]] = []
        _pf_all_raw: list[list[str]] = []
        for _pf_row in row_result.segment_rows:
            _pf_line = best_table_lines[_pf_row.row_index].strip()
            _pf_nums = _extract_numbers_from_line(_pf_line)
            _pf_all_nums.append(_pf_nums)
            # raw tokens for % check
            _pf_raw_tokens = [m.group() for m in re.finditer(r'[△▲]?\s*[\d,]+(?:\.\d+)?[%％]?', _pf_line)]
            _pf_all_raw.append(_pf_raw_tokens)

        # 各位置 (1, 2, ...) の値を収集して ratio/profit 判定
        max_nums = max((len(n) for n in _pf_all_nums), default=0)
        if max_nums >= 2:
            for _pf_pos in range(1, max_nums):
                _pf_vals = []
                _pf_pct = 0
                _pf_dec = 0
                _pf_total = 0
                for _ri, _pf_n in enumerate(_pf_all_nums):
                    if _pf_pos < len(_pf_n):
                        _pf_vals.append(abs(_pf_n[_pf_pos]))
                        _pf_total += 1
                        # % check from raw line
                        _pf_raw_line = best_table_lines[row_result.segment_rows[_ri].row_index]
                        if "%" in _pf_raw_line or "％" in _pf_raw_line:
                            # check if this specific position has %
                            if _pf_pos < len(_pf_all_raw[_ri]) and ("%" in _pf_all_raw[_ri][_pf_pos] or "％" in _pf_all_raw[_ri][_pf_pos]):
                                _pf_pct += 1
                        if "." in str(_pf_n[_pf_pos]):
                            _pf_dec += 1

                if not _pf_vals or _pf_total < 2:
                    continue

                _pf_vals.sort()
                _pf_median = _pf_vals[len(_pf_vals) // 2]

                # ratio 判定: 小さい値 + 小数/% が多い
                _pf_is_ratio = False
                if _pf_total > 0:
                    if _pf_pct / _pf_total >= 0.3:
                        _pf_is_ratio = True
                    elif _pf_dec / _pf_total >= 0.5 and _pf_median < 200:
                        _pf_is_ratio = True
                    elif _pf_median < 10 and max(_pf_vals) < 200:
                        _pf_is_ratio = True

                if not _pf_is_ratio:
                    _pf_profit_idx = _pf_pos
                    trace.append(f"Phase F: profit inference: pos={_pf_pos} median={_pf_median:.1f} pct={_pf_pct}/{_pf_total} dec={_pf_dec}/{_pf_total} -> PROFIT")
                    break
                else:
                    trace.append(f"Phase F: profit inference: pos={_pf_pos} median={_pf_median:.1f} pct={_pf_pct}/{_pf_total} dec={_pf_dec}/{_pf_total} -> RATIO (skip)")

        # profit_col_role を設定
        if _pf_profit_idx is not None:
            profit_col_role = "segment_profit_like"

    # ================================================================
    # Phase F pre-filter: rescue mode の場合 parent row のみ保持
    # ================================================================
    _rescue_mode = result.score_summary.get("invalid_structure_rescue", {}).get("attempted", False)
    _rescue_dropped_detail = 0
    _rescue_dropped_total = 0
    _rescue_kept_adjustment = 0

    if _rescue_mode and row_result.segment_rows:
        from .row_classifier import classify_row_label as _classify_row_label_rescue
        _TOTAL_DROP_LABELS = {
            "計", "合計", "小計", "収益", "営業収益", "純営業収益",
            "連結", "内部売上高", "セグメント間内部売上高",
            "セグメント間内部営業収益",
        }
        _ADJUSTMENT_KEEP_LABELS = {"調整額", "全社", "その他"}

        _parent_rows = []
        for _rc in row_result.segment_rows:
            _lab = _rc.label.strip()
            # classify this row
            _row_cls = _classify_row_label_rescue(_lab)
            _is_detail = (_row_cls.class_name == "detail_breakdown_like")
            _is_total = (_row_cls.class_name == "total_or_metric_like")
            _is_narrative = (_row_cls.class_name == "narrative_like")
            _is_pl = (_row_cls.class_name == "pl_account_like")
            _is_bscf = (_row_cls.class_name == "bs_cf_like")
            _is_garbage = (_row_cls.class_name == "garbage_fragment_like")

            # 合計/計/純営業収益 は常に drop
            if any(kw == _lab for kw in _TOTAL_DROP_LABELS):
                _rescue_dropped_total += 1
                continue
            if _lab.endswith("計") and len(_lab) >= 2 and _lab not in _ADJUSTMENT_KEEP_LABELS:
                _rescue_dropped_total += 1
                continue

            # detail / bracket detail → drop
            if _is_detail:
                _rescue_dropped_detail += 1
                continue

            # total/metric → drop (ただし 調整額/全社/その他 は keep)
            if _is_total and _lab not in _ADJUSTMENT_KEEP_LABELS:
                _rescue_dropped_total += 1
                continue

            # narrative / PL / BS/CF / garbage → drop
            if _is_narrative or _is_pl or _is_bscf or _is_garbage:
                _rescue_dropped_detail += 1
                continue

            # 調整額/全社/その他 は条件付き保持
            if _lab in _ADJUSTMENT_KEEP_LABELS:
                _rescue_kept_adjustment += 1

            _parent_rows.append(_rc)

        # rescue 成功判定: parent rows が 2件以上残れば rescue 成功
        if len(_parent_rows) >= 2:
            row_result_segment_rows = _parent_rows
            trace.append(
                f"Phase F: RESCUE filter: kept={len(_parent_rows)} "
                f"dropped_detail={_rescue_dropped_detail} "
                f"dropped_total={_rescue_dropped_total} "
                f"kept_adjustment={_rescue_kept_adjustment}"
            )
            result.score_summary["invalid_structure_rescue"]["succeeded"] = True
            result.score_summary["invalid_structure_rescue"]["parent_rows_built"] = len(_parent_rows)
            result.score_summary["invalid_structure_rescue"]["dropped_detail"] = _rescue_dropped_detail
            result.score_summary["invalid_structure_rescue"]["dropped_total"] = _rescue_dropped_total
            result.score_summary["invalid_structure_rescue"]["kept_adjustment"] = _rescue_kept_adjustment
        else:
            # rescue 失敗: parent rows 不足
            trace.append(
                f"Phase F: RESCUE failed: parent_rows={len(_parent_rows)} < 2, "
                f"reverting to quarantine"
            )
            result.score_summary["invalid_structure_rescue"]["succeeded"] = False
            result.score_summary["invalid_structure_rescue"]["parent_rows_built"] = len(_parent_rows)
            result.quarantine_reason = "candidate_guard:detail_breakdown_guard"
            result.failed_stage = "candidate_guard"
            result.review_hint = "pdf_segment_like_but_invalid_structure"
            write_quarantine_review(
                result, doc_id=doc_id, ticker=ticker,
                source_file=pdf_path, best_table_lines=best_table_lines,
            )
            return result
    else:
        row_result_segment_rows = row_result.segment_rows

    for row_cls in row_result_segment_rows:
        line = best_table_lines[row_cls.row_index].strip()
        nums = _extract_numbers_from_line(line)

        if not nums:
            continue

        seg_name_raw = row_cls.label
        seg_sales = None
        seg_profit = None

        # 列ロールベースで割り当て
        if col_result.has_sales and col_result.has_profit:
            if len(nums) >= 2:
                sales_idx = col_result.best_sales_col or 0
                profit_idx = col_result.best_profit_col or 1
                if sales_idx < profit_idx:
                    seg_sales = nums[0]
                    seg_profit = nums[1] if len(nums) > 1 else None
                else:
                    seg_profit = nums[0]
                    seg_sales = nums[1] if len(nums) > 1 else None
            elif len(nums) == 1:
                seg_sales = nums[0]
        elif col_result.has_sales:
            seg_sales = nums[0] if nums else None
            # Phase F profit inference: nums[1] が profit-like なら採用
            if seg_sales is not None and len(nums) >= 2 and _pf_profit_idx is not None:
                seg_profit = nums[_pf_profit_idx]
        elif col_result.has_profit:
            seg_profit = nums[0] if nums else None
        else:
            if len(nums) >= 2:
                seg_sales = nums[0]
                seg_profit = nums[1]
            elif len(nums) == 1:
                seg_sales = nums[0]

        # 単位正規化 (unit_detection ベース)
        if unit_multiplier and unit_multiplier != 1_000_000:
            if seg_sales is not None:
                seg_sales = _apply_unit_multiplier(seg_sales, unit_multiplier)
            if seg_profit is not None:
                seg_profit = _apply_unit_multiplier(seg_profit, unit_multiplier)
        elif unit_raw:
            # legacy string-based
            if seg_sales is not None:
                seg_sales = _normalize_unit_legacy(seg_sales, unit_raw)
            if seg_profit is not None:
                seg_profit = _normalize_unit_legacy(seg_profit, unit_raw)

        order += 1

        # segment name 正規化
        name_norm = normalize_segment_name(seg_name_raw, ticker=ticker)

        # confidence 算出
        conf = _compute_confidence(
            best_page_score, best_table.score, header_conf,
            col_result.confidence, row_cls.score,
        )

        # parse_quality
        pq = "full" if seg_sales is not None and seg_profit is not None else "partial_sales_only"

        segments.append(SegmentRecordV2(
            segment_name=name_norm.normalized_name,
            segment_order=order,
            segment_sales=seg_sales,
            segment_profit=seg_profit,
            raw_profit_label=profit_col_role,
            raw_text=line,
            confidence=conf,
            provenance={
                "page_no": best_table.start_line,
                "table_no": best_table.table_index,
                "row_no": row_cls.row_index,
                "col_sales": col_result.best_sales_col,
                "col_profit": col_result.best_profit_col,
                "page_score": best_page_score,
                "table_score": best_table.score,
                "header_confidence": header_conf,
                "col_confidence": col_result.confidence,
                "row_score": row_cls.score,
            },
            # Phase 2 fields
            segment_name_raw=seg_name_raw,
            segment_name_normalized=name_norm.normalized_name,
            segment_name_normalize_rule=name_norm.normalize_rule,
            segment_name_confidence=name_norm.confidence,
            unit_raw=unit_raw,
            unit_multiplier=unit_multiplier,
            currency=unit_result.currency,
            unit_source=unit_result.unit_source,
            unit_confidence=unit_result.confidence,
            row_role=row_cls.role,
            is_reportable_segment=row_cls.is_reportable_segment,
            sales_col_role=sales_col_role,
            profit_col_role=profit_col_role,
            extraction_engine="v2",
            parse_quality=pq,
        ))

    # ================================================================
    # Phase G: 最終判定
    # ================================================================
    if not segments:
        result.quarantine_reason = "segment_table_found_but_no_rows_extracted"
        result.failed_stage = "record_assembly"
        result.review_hint = "数値割当に失敗。列位置と数値パターンが合っていない可能性。"
        result.rule_trace = trace
        write_quarantine_review(result, doc_id=doc_id, ticker=ticker,
                                source_file=pdf_path, best_table_lines=best_table_lines)
        return result

    trace.append(f"Phase F: {len(segments)}件のセグメント抽出成功")

    result.segments = segments
    result.used_v2 = True
    result.rule_trace = trace

    # parse_quality 集計
    _pq_full = sum(1 for s in segments if s.parse_quality == "full")
    _pq_partial = sum(1 for s in segments if s.parse_quality == "partial_sales_only")
    _all_partial = _pq_full == 0 and _pq_partial > 0

    result.score_summary.update({
        "page_score": best_page_score,
        "table_score": best_table.score,
        "header_confidence": header_conf,
        "col_confidence": col_result.confidence,
        "segment_count": len(segments),
        "non_reportable_count": row_result.non_reportable_count,
        "unit_raw": unit_raw,
        "unit_multiplier": unit_multiplier,
        "sales_col_role": sales_col_role,
        "profit_col_role": profit_col_role,
        "parse_quality_full": _pq_full,
        "parse_quality_partial": _pq_partial,
    })

    # 全セグメントが partial_sales_only なら review_hint を設定
    if _all_partial:
        result.review_hint = "pdf_partial_sales_only"

    return result


def _apply_unit_multiplier(value: float, multiplier: int) -> float:
    """unit_detection の multiplier で百万円ベースへ正規化"""
    if multiplier == 1_000_000:
        return value  # そのまま
    elif multiplier > 1_000_000:
        # 億円 (100_000_000) → ×100
        return value * (multiplier / 1_000_000)
    elif multiplier < 1_000_000 and multiplier > 0:
        # 千円 (1_000) → ÷1000
        return value * (multiplier / 1_000_000)
    return value


def _normalize_unit_legacy(value: float, unit: str) -> float:
    """百万円ベースへ正規化 (legacy string-based)"""
    if unit == "億円":
        return value * 100
    elif unit == "千円":
        return value / 1000 if abs(value) >= 1000 else value
    elif unit == "円":
        return value / 1_000_000 if abs(value) >= 1_000_000 else value
    return value


def _compute_confidence(
    page_score: float,
    table_score: float,
    header_conf: float,
    col_conf: float,
    row_score: float,
) -> float:
    """各段階のスコアを統合してconfidenceを算出"""
    weighted = (
        page_score * 0.10
        + table_score * 0.25
        + header_conf * 0.20
        + col_conf * 0.25
        + row_score * 0.20
    )
    return min(max(weighted, 0.0), 1.0)


# 後方互換: Phase 1 の _normalize_unit
_normalize_unit = _normalize_unit_legacy
