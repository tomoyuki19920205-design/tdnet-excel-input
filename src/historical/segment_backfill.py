# ============================================================
# segment_backfill.py — セグメント業績 historical backfill
# ============================================================
"""
セグメント業績表の比較列・比較行から historical_records を生成。

対象メトリクス:
  - segment_sales  (売上高)
  - segment_profit (営業利益, セグメント利益等)

抽出パターン:
  A. 横持ち比較列 — 表ヘッダーに「前年同四半期累計期間」等
  B. 年度明示比較列 — 「2025年3月期第3四半期」等
  C. 縦持ち比較行 — 「前年同期 建築 売上高」等

絶対条件:
  - expression_type == "absolute" のみ
  - 増減率/増減額/構成比は skip
  - basis 不明なら skip
  - 既存値は上書きしない
  - 逆算しない
  - 調整額/全社/消去/合計/その他は skip
  - セグメント名は正規化を通す
"""
from __future__ import annotations

import logging
import re

from .schemas import ComparisonColumn, HistoricalRecord, ExtractResult, resolve_period_type
from .period_mapper import map_comparison_to_target
from .comparison_classifier import (
    detect_basis_from_label,
    detect_basis_from_header,
    detect_expression_type,
    is_current_column_header,
    is_change_column_header,
)

logger = logging.getLogger("historical.segment_backfill")

# ============================================================
# セグメントスキップ対象ラベル
# ============================================================
_SEG_SKIP_LABELS = {
    "合計", "総計", "計", "調整額", "消去", "消去又は全社",
    "全社", "配賦不能", "セグメント間", "内部取引",
    "全社・消去", "その他", "報告セグメント", "連結",
    "事業セグメント", "セグメント情報",
}

# 売上列キーワード
_SALES_KEYWORDS = [
    "売上高", "売上収益", "営業収益", "収益",
    "外部顧客への売上高",
]

# 利益列キーワード
_PROFIT_KEYWORDS = [
    "セグメント利益", "セグメント損益",
    "営業利益", "事業利益",
    "利益（損失）", "利益(損失)",
]

# セグメントヘッダーキーワード
_SEG_HEADER_KW = [
    "事業セグメント", "報告セグメント", "セグメント情報",
    "セグメント別", "事業別", "部門別",
]

# 数値抽出パターン
_NUM_PATTERN = re.compile(r"[△▲\-－]?\d[\d,]*(?:\.\d+)?")


def _extract_numbers(text: str) -> list[float]:
    """テキストから数値を抽出する（100未満はYoY%等として除外）"""
    # YoY%表記を先に除去
    cleaned = re.sub(r"[+\-－]?\d{1,3}\.\d%?", "", text)
    matches = _NUM_PATTERN.findall(cleaned)

    results: list[float] = []
    for raw in matches:
        val = _normalize_number(raw)
        if val is not None and abs(val) >= 100:
            results.append(val)
    return results


def _normalize_number(raw: str) -> float | None:
    """数値文字列を正規化する"""
    if not raw:
        return None
    negative = False
    s = raw.strip()
    if s.startswith(("△", "▲", "－")):
        negative = True
        s = s[1:]
    elif s.startswith("-"):
        negative = True
        s = s[1:]
    s = s.replace(",", "")
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return None


def _unit_normalize(value: float, scale_str: str) -> float:
    """百万円に正規化"""
    if scale_str == "億円":
        return value * 100
    elif scale_str == "千円":
        return value / 1000 if abs(value) >= 1000 else value
    elif scale_str == "円":
        return value / 1_000_000 if abs(value) >= 1_000_000 else value
    return value


def _is_skip_segment(name: str) -> bool:
    """スキップ対象のセグメント名かを判定する。"""
    from ..segment.normalize import normalize_segment_name, classify_special_row
    normalized = normalize_segment_name(name)
    if not normalized:
        return True
    if normalized in _SEG_SKIP_LABELS:
        return True
    # classify_special_row で ordinary_segment 以外は skip
    special = classify_special_row(normalized)
    if special != "ordinary_segment":
        return True
    return False


def _normalize_seg_name(name: str) -> str:
    """セグメント名を正規化する"""
    from ..segment.normalize import normalize_segment_name
    return normalize_segment_name(name)


def _detect_metric_from_label(label: str) -> str | None:
    """ラベルからセグメント指標名を判定する。"""
    for kw in _PROFIT_KEYWORDS:
        if kw in label:
            return "segment_profit"
    for kw in _SALES_KEYWORDS:
        if kw in label:
            return "segment_sales"
    return None


# ============================================================
# A. 横持ち比較列の検出
# ============================================================

def _detect_segment_table_columns(
    lines: list[str], table_start: int, table_end: int,
) -> list[dict]:
    """セグメント表のヘッダー行を解析して列情報を返す。

    Returns:
        [{"header": str, "basis": str|None, "is_current": bool, "is_change": bool}, ...]
    """
    # ヘッダー領域: tableのstart〜start+10
    header_end = min(table_start + 10, table_end)

    # 2パス: 2+スペース区切り → 1+スペース区切り
    for split_pattern in [r"\s{2,}|\t", r"\s+"]:
        for i in range(table_start, header_end):
            line = lines[i].strip()
            if not line:
                continue

            parts = re.split(split_pattern, line)
            if len(parts) < 2:
                continue

            line_cols: list[dict] = []
            for part in parts:
                part = part.strip()
                if not part or len(part) <= 1:
                    continue
                # 括弧ラベルスキップ
                if part.startswith(("＜", "【", "（")) and part.endswith(("＞", "】", "）")):
                    continue
                # 単位スキップ
                stripped_part = part.strip("＜＞<>()（）[]「」【】 ")
                if stripped_part in ("百万円", "千円", "億円", "単位", "%", "％"):
                    continue

                basis = detect_basis_from_header(part)
                is_current = is_current_column_header(part)
                is_change = is_change_column_header(part)

                # 構成比 は skip
                if "構成比" in part:
                    is_change = True

                # 認識できないトークンはスキップ
                if basis is None and not is_current and not is_change:
                    continue

                line_cols.append({
                    "header": part,
                    "basis": basis,
                    "is_current": is_current,
                    "is_change": is_change,
                })

            if line_cols and any(c["is_current"] or c["basis"] is not None for c in line_cols):
                return line_cols

    return []


def extract_segment_horizontal_comparisons(
    lines: list[str],
    table_start: int,
    table_end: int,
    scale_str: str,
) -> tuple[list[ComparisonColumn], list[HistoricalRecord]]:
    """セグメント表の横持ち比較列から ComparisonColumn を抽出する。

    Args:
        lines: PDF テキスト行
        table_start: セグメント表の開始行
        table_end: セグメント表の終了行
        scale_str: 単位文字列

    Returns:
        (comparison_columns, current_records_placeholder)
    """
    comparisons: list[ComparisonColumn] = []

    # ヘッダー解析
    columns = _detect_segment_table_columns(lines, table_start, table_end)
    if not columns:
        return comparisons, []

    # 比較列があるか確認
    comparison_cols = [c for c in columns if c["basis"] is not None and not c["is_change"]]
    if not comparison_cols:
        return comparisons, []

    non_change_cols = [c for c in columns if not c["is_change"]]
    if not non_change_cols:
        return comparisons, []

    # 現在のセグメント名トラッキング用
    # セグメント表は通常セグメント名行の後に売上高/利益行が続くか、
    # セグメント名+数値が横一列に並ぶ

    # ヘッダー後のデータ行を走査
    data_start = table_start + 1
    for i in range(data_start, table_end):
        line = lines[i].strip()
        if not line:
            continue

        # 行の先頭部分をセグメント名/メトリクスラベルとして取得
        name_match = re.match(r'^([^\d△▲\-－]+)', line)
        if not name_match:
            continue

        label = name_match.group(1).strip()
        if not label:
            continue

        # 数値抽出
        nums = _extract_numbers(line)
        if len(nums) < 2:
            continue

        # セグメント名か指標名かを判定
        # もしラベルが指標だけなら、直前のセグメント名と組み合わせる
        # ただし、典型的な横持ちパターンでは各行が
        # 「建築  10,000  9,500  ...」のように並ぶ

        # スキップ対象セグメント
        if _is_skip_segment(label):
            continue

        # 指標判定（ラベルに売上高/利益が含まれるか）
        metric_name = _detect_metric_from_label(label)

        # 指標名が特定できない場合、行はセグメント名+値の可能性
        # → metric_name はヘッダーに売上高/利益があるかで推定
        # 簡略化: 指標名がないセグメント名行は skip (安全側)
        if metric_name is None:
            # このラベルがセグメント名なら、次の指標行を待つ
            continue

        # 比較列の値を取得
        for comp_col in comparison_cols:
            col_idx = -1
            for j, nc in enumerate(non_change_cols):
                if nc is comp_col:
                    col_idx = j
                    break

            if col_idx < 0 or col_idx >= len(nums):
                continue

            value = nums[col_idx]
            expr_type = detect_expression_type(comp_col["header"], str(value))
            normalized_value = _unit_normalize(value, scale_str) if expr_type == "absolute" else value

            # セグメント名を抽出（指標名を除去した残り）
            seg_label = label
            for kw in _SALES_KEYWORDS + _PROFIT_KEYWORDS:
                seg_label = seg_label.replace(kw, "").strip()

            if not seg_label:
                continue

            # narrative 文章断片チェック
            if len(seg_label) > 30:
                continue
            if any(ch in seg_label for ch in "、。・はがでをにもとの"):
                continue
            if any(seg_label.startswith(p) for p in _NARRATIVE_PREFIX):
                continue

            # 正規化
            seg_name_normalized = _normalize_seg_name(seg_label)
            if not seg_name_normalized:
                continue

            # スキップ対象チェック
            if _is_skip_segment(seg_label):
                continue

            comparisons.append(ComparisonColumn(
                basis=comp_col["basis"],
                expression_type=expr_type,
                metric_name=metric_name,
                value=normalized_value,
                raw_text=line,
                segment_name=seg_name_normalized,
            ))

    return comparisons, []


# ============================================================
# C. 縦持ち比較行の検出
# ============================================================

# narrative 文の残骸を排除する文章プレフィックス
_NARRATIVE_PREFIX = [
    "以上の結果", "当社", "当連結", "当第", "前第",
    "なお", "また", "この", "その", "それ",
    "上記", "下記", "注記", "注）", "※",
]

def extract_segment_vertical_comparisons(
    lines: list[str],
    scale_str: str,
) -> list[ComparisonColumn]:
    """縦持ち比較行 (「前年同期 建築 売上高 10,000」) から抽出。

    安全フィルター:
      - セグメント名が30文字以上 → skip (文章断片の可能性)
      - 行が100文字超 → skip (narrative)
      - 句読点・助詞を含む → skip (文章断片)
      - 文章プレフィックスで始まる → skip
      - segment_name_validator で PL/BS/CF → skip
      - 調整額/全社/消去/合計/その他 → skip
    """
    results: list[ComparisonColumn] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 長すぎる行は narrative の可能性が高い → skip
        if len(stripped) > 100:
            continue

        # basis 検出
        basis = detect_basis_from_label(stripped)
        if basis is None:
            continue

        # 指標判定
        metric_name = _detect_metric_from_label(stripped)
        if metric_name is None:
            continue

        # 数値抽出
        nums = _extract_numbers(stripped)
        if not nums:
            continue

        # セグメント名の抽出（比較プレフィックス + 指標名を除去）
        seg_label = stripped
        for prefix in ["前年同期末", "前年同期", "前年同四半期末", "前年同四半期",
                        "前期末", "前期実績", "前連結会計年度末", "前連結会計年度"]:
            seg_label = seg_label.replace(prefix, "")
        for kw in _SALES_KEYWORDS + _PROFIT_KEYWORDS:
            seg_label = seg_label.replace(kw, "")
        # 数値部分を除去
        seg_label = re.sub(r"[\d,△▲\-－.%％]+", "", seg_label).strip()

        if not seg_label:
            continue

        # --- 安全フィルター ---
        # 30文字超の場合はセグメント名ではなく文章断片
        if len(seg_label) > 30:
            continue

        # narrative 文章断片チェック: 句読点・助詞が含まれる場合は文章の一部
        if any(ch in seg_label for ch in "、。・はがでをにもとの"):
            continue

        # 文章プレフィックスが残っている場合は skip
        if any(seg_label.startswith(p) for p in _NARRATIVE_PREFIX):
            continue

        seg_name_normalized = _normalize_seg_name(seg_label)
        if not seg_name_normalized or _is_skip_segment(seg_label):
            continue

        # segment_name_validator でチェック (PL/BS/CF勘定科目排除)
        try:
            from ..segment.segment_name_validator import validate_segment_name, RowType, InvalidReason
            validation = validate_segment_name(seg_name_normalized)
            # deny list (PL/BS/CF/header) に引っかかったら skip
            if validation.invalid_reason in (
                InvalidReason.PL_ACCOUNT,
                InvalidReason.BS_ITEM,
                InvalidReason.CF_ITEM,
                InvalidReason.HEADER_LABEL,
                InvalidReason.UNIT_ROW,
                InvalidReason.NUMERIC_ONLY,
                InvalidReason.PARENTHESIS_ONLY,
                InvalidReason.PUNCTUATION,
            ):
                continue
            # FRAGMENT は short segment name (建築/土木) を誤排除するため除外
            # 30-char max + _is_skip_segment で文章断片をガード
        except ImportError:
            pass  # validator なければスキップ

        expr_type = detect_expression_type(stripped, str(nums[0]))
        value = _unit_normalize(nums[0], scale_str) if expr_type == "absolute" else nums[0]

        results.append(ComparisonColumn(
            basis=basis,
            expression_type=expr_type,
            metric_name=metric_name,
            value=value,
            raw_text=stripped,
            segment_name=seg_name_normalized,
        ))

    return results


# ============================================================
# ComparisonColumn → HistoricalRecord 変換
# ============================================================

def convert_segment_comparisons_to_historical(
    comparison_columns: list[ComparisonColumn],
    company_code: str,
    current_fiscal_year_end: str,
    current_quarter: str,
    current_period_type: str,
    source_doc_id: str = "",
) -> tuple[list[HistoricalRecord], dict]:
    """ComparisonColumn から HistoricalRecord を生成する。

    絶対条件:
      - expression_type == "absolute" のみ
      - basis 不明は skip
      - 逆算しない
      - 調整額/全社/消去/合計/その他は skip

    Returns:
        (historical_records, stats)
    """
    records: list[HistoricalRecord] = []
    stats = {
        "extracted": 0,
        "skipped_ratio_only": 0,
        "skipped_unknown_basis": 0,
    }

    for col in comparison_columns:
        # basis 不明 → skip
        if not col.basis or col.basis in ("", "unknown", "不明"):
            stats["skipped_unknown_basis"] += 1
            logger.debug("skip unknown basis: %s", col.raw_text)
            continue

        # 比率/増減 → skip
        if col.expression_type != "absolute":
            stats["skipped_ratio_only"] += 1
            logger.debug("skip non-absolute: type=%s, %s", col.expression_type, col.raw_text)
            continue

        # value がないなら skip
        if col.value is None:
            stats["skipped_ratio_only"] += 1
            continue

        # period mapping (セグメント業績は基本 cumulative)
        effective_period_type = resolve_period_type(col.metric_name, current_period_type)
        target = map_comparison_to_target(
            col.basis,
            current_fiscal_year_end,
            current_quarter,
            effective_period_type,
        )
        if target is None:
            stats["skipped_unknown_basis"] += 1
            continue

        records.append(HistoricalRecord(
            company_code=company_code,
            target_fiscal_year_end=target.fiscal_year_end,
            target_quarter=target.quarter,
            target_period_type=target.period_type,
            metric_name=col.metric_name,
            value=col.value,
            unit="百万円",
            source_basis=col.basis,
            source_doc_id=source_doc_id,
            source_expression_type="absolute",
            confidence="medium",
            segment_name=col.segment_name,
        ))
        stats["extracted"] += 1

    return records, stats


# ============================================================
# ============================================================
# D. JGAAP Paired-Table 抽出 (Ⅰ前期 + Ⅱ当期)
# ============================================================

def _parse_pdfplumber_value(cell: str | None) -> float | None:
    """pdfplumber セルの値を解析する。改行を含む場合は最初の数値行を使用。"""
    if not cell:
        return None
    # 複数行セルの場合、改行で分割して最初の数値行を取得
    for part in cell.split("\n"):
        part = part.strip()
        if not part or part == "－" or part == "-":
            continue
        val = _normalize_number(part)
        if val is not None:
            return val
    return None


def _detect_metric_from_cell(cell: str | None) -> str | None:
    """pdfplumber テーブルのセル（改行含む）から指標を判定する。"""
    if not cell:
        return None
    text = cell.replace("\n", "")
    return _detect_metric_from_label(text)


def extract_segment_paired_tables(
    pdf_path: str,
    scale_str: str,
) -> list[ComparisonColumn]:
    """JGAAP paired-table 形式からComparisonColumnsを抽出する。

    JGAAP決算短信のセグメント注記は:
      Ⅰ 前第N四半期連結累計期間 → Table 0 (前期データ)
      Ⅱ 当第N四半期連結累計期間 → Table 1 (当期データ)

    のように2つの表が順に配置される。Table 0 の値を前年同期(yoy)
    のhistorical recordとして抽出する。

    安全ガード:
      - 当期テーブル (Ⅱ) の存在確認が必須
      - 同一ページ or 隣接ページで当期マーカーを検出できない場合は skip
      - _is_skip_segment で調整額/全社/合計を排除

    Returns:
        list[ComparisonColumn] — 前期テーブルから得た比較列
    """
    import pdfplumber

    results: list[ComparisonColumn] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            for pi in range(min(8, max(0, n-1)), min(25, n)):
                page = pdf.pages[pi]
                page_text = page.extract_text() or ""

                # セグメント注記セクションか判定
                if not any(kw in page_text for kw in [
                    "セグメント情報", "セグメント情報等",
                    "報告セグメント", "外部顧客への売上高"
                ]):
                    continue

                # 前期・当期セクションマーカー検出
                has_prev = "Ⅰ" in page_text and any(
                    kw in page_text for kw in ["前第", "前連結"]
                )

                # 前期のテーブルが当ページにあることを確認
                if not has_prev:
                    continue

                # === 当期テーブル存在ガード ===
                # 当期マーカーを同一ページ or 隣接ページで確認
                has_curr = "Ⅱ" in page_text and any(
                    kw in page_text for kw in ["当第", "当連結"]
                )
                if not has_curr:
                    # 次ページで当期マーカーを確認
                    if pi + 1 < min(25, n):
                        next_text = pdf.pages[pi + 1].extract_text() or ""
                        has_curr = "Ⅱ" in next_text and any(
                            kw in next_text for kw in ["当第", "当連結"]
                        )
                if not has_curr:
                    logger.debug("Page %d: prev found but no curr marker → skip", pi)
                    continue

                tables = page.extract_tables()
                if not tables:
                    continue

                logger.debug("Page %d: %d tables, prev=%s curr=%s",
                             pi, len(tables), has_prev, has_curr)

                # Table 0 を前期データとして解析
                prev_table = tables[0]
                if not prev_table or len(prev_table) < 3:
                    continue

                # セグメント名をRow 1から取得
                seg_row = prev_table[1] if len(prev_table) > 1 else []
                segment_names: list[str | None] = []
                for col_i, cell in enumerate(seg_row):
                    if col_i == 0:
                        segment_names.append(None)  # 最左列は指標
                        continue
                    if not cell or not cell.strip():
                        segment_names.append(None)
                        continue
                    # 改行除去して正規化
                    raw_name = cell.replace("\n", "").strip()
                    # スキップ対象チェック
                    if _is_skip_segment(raw_name):
                        segment_names.append(None)
                        continue
                    normalized = _normalize_seg_name(raw_name)
                    if not normalized:
                        segment_names.append(None)
                        continue
                    segment_names.append(normalized)

                if not any(segment_names):
                    continue

                # 指標行を走査 (Row 2+)
                for row in prev_table[2:]:
                    if not row or len(row) < 2:
                        continue

                    # 最左セルで指標判定
                    metric_name = _detect_metric_from_cell(row[0])
                    if metric_name is None:
                        continue

                    # 各セグメント列の値を取得
                    for col_i in range(1, min(len(row), len(segment_names))):
                        seg_name = segment_names[col_i]
                        if seg_name is None:
                            continue

                        value = _parse_pdfplumber_value(row[col_i])
                        if value is None:
                            continue

                        # 単位正規化
                        normalized_value = _unit_normalize(value, scale_str)

                        results.append(ComparisonColumn(
                            basis="yoy",
                            expression_type="absolute",
                            metric_name=metric_name,
                            value=normalized_value,
                            raw_text=f"[paired-table p{pi}] {seg_name} {metric_name}={value}",
                            segment_name=seg_name,
                        ))

                # 一つのページで見つかれば十分
                if results:
                    break

    except Exception as e:
        logger.warning("paired-table extraction error: %s", e)

    return results


# ============================================================
# メインエントリ: extract_segment_with_historical
# ============================================================

def extract_segment_with_historical(
    pdf_path: str,
    title: str,
    *,
    company_code: str = "",
    fiscal_year_end: str = "",
    quarter: str = "",
    period_type: str = "cumulative",
    source_doc_id: str = "",
    ticker: str = "",
) -> ExtractResult:
    """セグメント業績を抽出し、historical_records も生成する。

    既存の extract_segment_financials の出力は変更せず、
    比較列から historical_records を追加で生成する。

    Returns:
        ExtractResult with current_records, historical_records, stats
    """
    import pdfplumber
    from ..extractor import (
        extract_segment_financials,
        _find_segment_table_region,
        _detect_scale,
        _extract_numbers_from_line,
        SegmentExtracted,
    )

    result = ExtractResult()

    # ---- 既存 extraction (current_records) ----
    segments, quarantine_reason = extract_segment_financials(
        pdf_path, title, doc_id=source_doc_id, ticker=ticker,
    )

    # segments → current_records に変換
    for seg in segments:
        if seg.segment_sales is not None:
            seg_name_norm = _normalize_seg_name(seg.segment_name)
            result.current_records.append(HistoricalRecord(
                company_code=company_code,
                target_fiscal_year_end=fiscal_year_end,
                target_quarter=quarter,
                target_period_type=period_type,
                metric_name="segment_sales",
                value=float(seg.segment_sales),
                unit="百万円",
                source_basis="",
                source_doc_id=source_doc_id,
                source_expression_type="absolute",
                confidence="high",
                segment_name=seg_name_norm or seg.segment_name,
            ))
        if seg.segment_profit is not None:
            seg_name_norm = _normalize_seg_name(seg.segment_name)
            result.current_records.append(HistoricalRecord(
                company_code=company_code,
                target_fiscal_year_end=fiscal_year_end,
                target_quarter=quarter,
                target_period_type=period_type,
                metric_name="segment_profit",
                value=float(seg.segment_profit),
                unit="百万円",
                source_basis="",
                source_doc_id=source_doc_id,
                source_expression_type="absolute",
                confidence="high",
                segment_name=seg_name_norm or seg.segment_name,
            ))

    # ---- 比較抽出 ----
    # PDF再読み込みしてテキスト取得 (pages[:25]に拡張)
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages[:25]:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        logger.warning("PDF読み込みエラー: %s", e)
        return result

    if not text.strip():
        return result

    lines = text.split("\n")

    # セグメント表領域の検出
    region = _find_segment_table_region(lines)

    all_comparisons: list[ComparisonColumn] = []

    # 単位検出
    scale_str = _detect_scale(text)

    if region:
        start, end = region
        # A. 横持ち比較列
        h_comps, _ = extract_segment_horizontal_comparisons(
            lines, start, end, scale_str,
        )
        all_comparisons.extend(h_comps)

    # C. 縦持ち比較行 (全ページ走査)
    v_comps = extract_segment_vertical_comparisons(lines, scale_str)
    all_comparisons.extend(v_comps)

    # D. JGAAP Paired-Table (pdfplumber ベース)
    p_comps = extract_segment_paired_tables(pdf_path, scale_str)
    all_comparisons.extend(p_comps)

    # 重複排除 (同じ segment_name + metric_name + basis で先勝ち)
    seen: set[tuple[str, str, str]] = set()
    deduped: list[ComparisonColumn] = []
    for comp in all_comparisons:
        key = (comp.segment_name or "", comp.metric_name, comp.basis)
        if key not in seen:
            seen.add(key)
            deduped.append(comp)
    all_comparisons = deduped

    # ComparisonColumn → HistoricalRecord 変換
    historical, conv_stats = convert_segment_comparisons_to_historical(
        all_comparisons,
        company_code=company_code,
        current_fiscal_year_end=fiscal_year_end,
        current_quarter=quarter,
        current_period_type=period_type,
        source_doc_id=source_doc_id,
    )

    result.historical_records = historical
    result.stats = {
        "extracted": conv_stats["extracted"],
        "skipped_ratio_only": conv_stats["skipped_ratio_only"],
        "skipped_unknown_basis": conv_stats["skipped_unknown_basis"],
        "skipped_existing": 0,
        "comparison_columns_total": len(all_comparisons),
    }

    return result

