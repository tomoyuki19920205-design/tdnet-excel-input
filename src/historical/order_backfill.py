# ============================================================
# order_backfill.py — 受注系 extractor の historical backfill 統合
# ============================================================
"""
既存の extract_order_metrics を拡張し、比較列・比較行から
historical_records を生成する。

対象メトリクス:
  - orders_total (受注高)
  - backlog_total (受注残高)
  - carryover_construction_total (繰越工事高)

抽出パターン:
  A. 横持ち比較列 — 表のヘッダーに「前年同四半期累計期間」等がある
  B. 縦持ち比較行 — 「前年同期受注高」等のラベルがある
  C. 前期末パターン — 「前期末受注残高」等

絶対条件:
  - expression_type == "absolute" のみ historical_records 化
  - basis 不明なら skip
  - 既存値は上書きしない
  - 逆算しない
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
    is_comparison_column_header,
    is_current_column_header,
    is_change_column_header,
)

logger = logging.getLogger("historical.order_backfill")

# 受注系キーワード（extractor.py と一致）
_ORDERS_KEYWORDS = ["受注高", "受注額", "新規受注", "受注工事高"]
_BACKLOG_KEYWORDS = [
    "受注残高", "受注残", "手持工事高", "手持ち工事高",
    "繰越工事高", "繰越高", "次期繰越工事高", "繰り越し工事高",
]
_CARRYOVER_KEYWORDS = [
    "繰越工事高", "繰越高", "次期繰越工事高", "繰り越し工事高",
]

_ORDER_METRIC_MAP = [
    ("orders_total", _ORDERS_KEYWORDS),
    ("backlog_total", _BACKLOG_KEYWORDS),
    ("carryover_construction_total", _CARRYOVER_KEYWORDS),
]

# 合計行キーワード
_TOTAL_KEYWORDS = ["合計", "総計", "計"]

# 数値抽出パターン (extractor.py 互換)
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
    # △▲は負数
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
    return value  # 百万円 or unknown → そのまま


def _detect_metric_name(label: str) -> str | None:
    """ラベルから metric_name を判定する（比較ラベル含む）"""
    # 比較プレフィックスを除去してマッチ
    cleaned = label
    for prefix in ["前年同期末", "前年同期", "前年同四半期末", "前年同四半期",
                    "前期末", "前連結会計年度末", "前連結会計年度",
                    "前年度末", "前年度同期"]:
        cleaned = cleaned.replace(prefix, "")

    for metric_name, keywords in _ORDER_METRIC_MAP:
        for kw in keywords:
            if kw in cleaned or kw in label:
                return metric_name
    return None


# ============================================================
# B. 縦持ち比較行の検出
# ============================================================

def extract_vertical_comparisons(
    lines: list[str],
    scale_str: str,
) -> list[ComparisonColumn]:
    """縦持ち比較行から ComparisonColumn を抽出する。

    例:
      "前年同期受注高  18,000" → ComparisonColumn(basis="yoy", ...)
      "前年同期末受注残高  25,000" → ComparisonColumn(basis="yoy_end", ...)
      "前期末受注残高  30,000" → ComparisonColumn(basis="prev_period_end", ...)
    """
    results: list[ComparisonColumn] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # basis 検出
        basis = detect_basis_from_label(stripped)
        if basis is None:
            continue

        # metric_name 検出
        metric_name = _detect_metric_name(stripped)
        if metric_name is None:
            continue

        # 数値抽出
        nums = _extract_numbers(stripped)
        if not nums:
            continue

        # expression_type 判定
        expr_type = detect_expression_type(stripped, str(nums[0]))
        value = _unit_normalize(nums[0], scale_str) if expr_type == "absolute" else nums[0]

        results.append(ComparisonColumn(
            basis=basis,
            expression_type=expr_type,
            metric_name=metric_name,
            value=value,
            raw_text=stripped,
        ))

    return results


# ============================================================
# A. 横持ち比較列の検出
# ============================================================

def _detect_table_columns(lines: list[str], keyword_line_idx: int) -> list[dict]:
    """テーブルのヘッダー行を解析して列情報を返す。

    Returns:
        [{"header": str, "basis": str|None, "is_current": bool, "is_change": bool}, ...]
    """
    # keyword行の直前数行からヘッダーを探す
    header_search_start = max(0, keyword_line_idx - 15)

    # 2パス: まず2+スペース区切り → なければ1+スペース区切り
    for split_pattern in [r"\s{2,}|\t", r"\s+"]:
        columns: list[dict] = []
        for i in range(header_search_start, keyword_line_idx):
            line = lines[i].strip()
            if not line:
                continue

            parts = re.split(split_pattern, line)
            if len(parts) < 2:
                continue

            # 各パートを判定
            line_cols: list[dict] = []
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                # ノイズ除去: 短すぎるトークン
                if len(part) <= 1:
                    continue
                # 括弧で囲まれたカテゴリラベルをスキップ
                # ＜連結＞ ＜個別＞ （百万円） [連結] 等
                stripped_part = part.strip("＜＞<>()（）[]「」【】 ")
                if not stripped_part or all(c in "＜＞<>()（）[]「」【】 " for c in part):
                    continue
                # 単位表記をスキップ
                if stripped_part in ("百万円", "千円", "億円", "単位", "%", "％"):
                    continue
                # カテゴリラベルをスキップ: ＜連結＞
                if part.startswith(("＜", "【", "（")) and part.endswith(("＞", "】", "）")):
                    continue

                basis = detect_basis_from_header(part)
                is_current = is_current_column_header(part)
                is_change = is_change_column_header(part)

                # 列として認識できないトークンはスキップ
                # (basis, current, change いずれにも該当しない → データ列の位置合わせに影響)
                if basis is None and not is_current and not is_change:
                    continue

                line_cols.append({
                    "header": part,
                    "basis": basis,
                    "is_current": is_current,
                    "is_change": is_change,
                })

            # ヘッダー行として有効: 少なくとも1つ is_current or basis ありの列がある
            if line_cols and any(c["is_current"] or c["basis"] is not None for c in line_cols):
                columns = line_cols
                break  # 最初に見つかったヘッダー行を使用

        if columns:
            return columns

    return []


def extract_horizontal_comparisons(
    lines: list[str],
    keyword_line_idx: int,
    keyword: str,
    metric_name: str,
    scale_str: str,
) -> list[ComparisonColumn]:
    """横持ち比較列から ComparisonColumn を抽出する。

    合計行 or キーワード行の複数数値から、
    ヘッダーで特定した比較列の値を取得。
    """
    results: list[ComparisonColumn] = []

    # ヘッダー解析
    columns = _detect_table_columns(lines, keyword_line_idx)
    if not columns:
        return results

    # 比較列があるか確認
    comparison_cols = [c for c in columns if c["basis"] is not None and not c["is_change"]]
    if not comparison_cols:
        return results

    # データ行を探す: 合計行 → キーワード行自身（fallback）
    data_line = None
    data_line_idx = None

    # (1) 合計行を探す
    search_end = min(keyword_line_idx + 30, len(lines))
    for i in range(keyword_line_idx + 1, search_end):
        line = lines[i].strip()
        for tw in _TOTAL_KEYWORDS:
            if tw in line:
                stripped = line.strip()
                if stripped.startswith(tw) or stripped == tw:
                    data_line = line
                    data_line_idx = i
                    break
                if keyword in line and tw in line:
                    data_line = line
                    data_line_idx = i
                    break
        if data_line:
            break

    # (2) 合計行がない → キーワード行自身を使う
    if data_line is None:
        kw_line = lines[keyword_line_idx].strip()
        kw_nums = _extract_numbers(kw_line)
        if len(kw_nums) >= 2:
            # キーワード行に2つ以上の数値がある → データ行として使用
            data_line = kw_line
            data_line_idx = keyword_line_idx

    if data_line is None:
        return results

    # データ行から全数値を抽出
    all_nums = _extract_numbers(data_line)
    if len(all_nums) < 2:
        # 次の行も試す
        if data_line_idx is not None and data_line_idx + 1 < len(lines):
            next_nums = _extract_numbers(lines[data_line_idx + 1])
            all_nums.extend(next_nums)
        if len(all_nums) < 2:
            return results

    # 非変動列（absolute値を持つ列）のみ抽出
    non_change_cols = [c for c in columns if not c["is_change"]]

    if not non_change_cols:
        return results

    # 比較列のインデックスを特定してデータ取得
    for comp_col in comparison_cols:
        col_idx_in_non_change = -1
        for j, nc in enumerate(non_change_cols):
            if nc is comp_col:
                col_idx_in_non_change = j
                break

        if col_idx_in_non_change < 0 or col_idx_in_non_change >= len(all_nums):
            continue

        value = all_nums[col_idx_in_non_change]
        expr_type = detect_expression_type(comp_col["header"], str(value))

        normalized_value = _unit_normalize(value, scale_str) if expr_type == "absolute" else value

        results.append(ComparisonColumn(
            basis=comp_col["basis"],
            expression_type=expr_type,
            metric_name=metric_name,
            value=normalized_value,
            raw_text=data_line.strip(),
        ))

    return results


# ============================================================
# ComparisonColumn → HistoricalRecord 変換
# ============================================================

def convert_comparisons_to_historical(
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

        # period mapping
        # 残高系は current_period_type ではなく point_in_time を使う
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
        ))
        stats["extracted"] += 1

    return records, stats


# ============================================================
# メインエントリ: extract_order_metrics_with_historical
# ============================================================

def extract_order_metrics_with_historical(
    pdf_path: str,
    title: str,
    *,
    company_code: str = "",
    fiscal_year_end: str = "",
    quarter: str = "",
    period_type: str = "cumulative",
    source_doc_id: str = "",
) -> ExtractResult:
    """受注系メトリクスを抽出し、historical_records も生成する。

    既存の extract_order_metrics の出力に加え、
    比較列・比較行から historical backfill 用レコードを生成する。

    Args:
        pdf_path: PDFファイルパス
        title: 開示タイトル
        company_code: 企業コード "1801"
        fiscal_year_end: 当期の期末日 "2025-03-31"
        quarter: 当期の四半期 "3Q"
        period_type: "cumulative" | "quarterly" | "point_in_time"
        source_doc_id: 開示ID

    Returns:
        ExtractResult with current_records, historical_records, stats
    """
    import pdfplumber
    from ..extractor import (
        _extract_numbers_from_line,
        _extract_total_from_table,
        _detect_scale,
        parse_scale_unit,
        ORDERS_KEYWORDS,
        BACKLOG_KEYWORDS,
        CARRYOVER_KEYWORDS,
        TOTAL_ROW_KEYWORDS,
    )
    from ..models import OrderMetric

    result = ExtractResult()

    # PDF読み込み:
    # 受注系キーワードで候補ページを抽出してから処理
    all_kws = _ORDERS_KEYWORDS + _BACKLOG_KEYWORDS + _CARRYOVER_KEYWORDS
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Pass 1: 先頭5ページ + キーワード含有ページを特定
            candidate_pages: list[int] = []
            for page_idx, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                if page_idx < 5:
                    # 先頭5ページは常に候補
                    candidate_pages.append(page_idx)
                elif any(kw in page_text for kw in all_kws):
                    # 6ページ目以降はキーワード含有ページのみ
                    # ただし目次行（…を含む行のみ）の場合は除外
                    kw_lines = [
                        l for l in page_text.split("\n")
                        if any(kw in l for kw in all_kws)
                    ]
                    has_data_line = any("…" not in l and "・・" not in l for l in kw_lines)
                    if has_data_line:
                        candidate_pages.append(page_idx)

            # Pass 2: 候補ページからテキスト抽出
            text = ""
            for page_idx in candidate_pages:
                page_text = pdf.pages[page_idx].extract_text() or ""
                text += page_text + "\n"
    except Exception as e:
        logger.warning("PDF読み込みエラー: %s", e)
        return result

    if not text.strip():
        return result

    lines = text.split("\n")

    # 受注系キーワード存在チェック
    if not any(kw in text for kw in all_kws):
        return result

    # 単位検出
    scale_str = _detect_scale(text)

    # ---- 従来 extraction (current_records) ----
    # まず各メトリクスのキーワード行を特定
    metric_keyword_lines: dict[str, tuple[int, str]] = {}  # metric -> (line_idx, kw)
    for metric_name, keywords in _ORDER_METRIC_MAP:
        for i, line in enumerate(lines):
            for kw in keywords:
                if kw in line:
                    basis = detect_basis_from_label(line.strip())
                    if basis is not None:
                        continue
                    metric_keyword_lines[metric_name] = (i, kw)
                    break
            if metric_name in metric_keyword_lines:
                break

    # 共有ヘッダー検出: 受注高と受注残高が同一行にある場合
    shared_header_metrics = _detect_shared_header(metric_keyword_lines)
    if shared_header_metrics:
        # 共有ヘッダーの場合: 列位置で振り分け
        _extract_from_shared_header(
            lines, shared_header_metrics, scale_str,
            result, company_code, fiscal_year_end, quarter,
            period_type, source_doc_id,
            _extract_total_from_table, _extract_numbers_from_line,
            TOTAL_ROW_KEYWORDS,
        )
        # 共有ヘッダーで処理済みのメトリクスを除外
        for mn in shared_header_metrics:
            metric_keyword_lines.pop(mn, None)

    # 残りのメトリクスは従来ロジック
    for metric_name, (kw_line_idx, matched_kw) in metric_keyword_lines.items():
        value, confidence, raw_text = _extract_total_from_table(
            lines, kw_line_idx, matched_kw
        )

        if value is not None:
            normalized = _unit_normalize(float(value), scale_str)
            effective_pt = resolve_period_type(metric_name, period_type)
            result.current_records.append(HistoricalRecord(
                company_code=company_code,
                target_fiscal_year_end=fiscal_year_end,
                target_quarter=quarter,
                target_period_type=effective_pt,
                metric_name=metric_name,
                value=normalized,
                unit="百万円",
                source_basis="",
                source_doc_id=source_doc_id,
                source_expression_type="absolute",
                confidence=confidence,
            ))

    # ---- 比較抽出 ----
    all_comparisons: list[ComparisonColumn] = []

    # A. 横持ち比較列
    for metric_name, keywords in _ORDER_METRIC_MAP:
        for i, line in enumerate(lines):
            for kw in keywords:
                if kw in line:
                    basis = detect_basis_from_label(line.strip())
                    if basis is not None:
                        continue
                    h_comps = extract_horizontal_comparisons(
                        lines, i, kw, metric_name, scale_str,
                    )
                    all_comparisons.extend(h_comps)
                    break

    # B. 縦持ち比較行
    v_comps = extract_vertical_comparisons(lines, scale_str)
    all_comparisons.extend(v_comps)

    # 重複排除 (同じ metric_name + basis で先勝ち)
    seen: set[tuple[str, str]] = set()
    deduped: list[ComparisonColumn] = []
    for comp in all_comparisons:
        key = (comp.metric_name, comp.basis)
        if key not in seen:
            seen.add(key)
            deduped.append(comp)
    all_comparisons = deduped

    # ComparisonColumn → HistoricalRecord 変換
    historical, conv_stats = convert_comparisons_to_historical(
        all_comparisons,
        company_code=company_code,
        current_fiscal_year_end=fiscal_year_end,
        current_quarter=quarter,
        current_period_type=period_type,
        source_doc_id=source_doc_id,
    )

    # ---- Same-value guard ----
    # orders_total と backlog_total が同値の場合は historical を skip
    # (backlog_total == carryover_construction_total は建設業で正常なので許可)
    historical, n_same_value_skipped = _apply_same_value_guard(historical)

    result.historical_records = historical
    result.stats = {
        "extracted": conv_stats["extracted"],
        "skipped_ratio_only": conv_stats["skipped_ratio_only"],
        "skipped_unknown_basis": conv_stats["skipped_unknown_basis"],
        "skipped_existing": 0,  # filter_skip_existing は呼び出し側で適用
        "skipped_same_value_guard": n_same_value_skipped,
        "comparison_columns_total": len(all_comparisons),
    }

    return result


# ============================================================
# 共有ヘッダー検出 + 列位置ベース抽出
# ============================================================

def _detect_shared_header(
    metric_keyword_lines: dict[str, tuple[int, str]],
) -> dict[str, tuple[int, str]]:
    """受注高と受注残高が同一行にある場合を検出する。

    Returns:
        同一行にあるメトリクス名→(line_idx, kw) の dict (空なら共有なし)
    """
    orders_info = metric_keyword_lines.get("orders_total")
    backlog_info = metric_keyword_lines.get("backlog_total")

    if orders_info is None or backlog_info is None:
        return {}

    # 同一行にあるか?
    if orders_info[0] != backlog_info[0]:
        return {}

    # 同一行 → 共有ヘッダー
    shared = {"orders_total": orders_info, "backlog_total": backlog_info}

    # carryover も同じ行にあるかチェック
    carryover_info = metric_keyword_lines.get("carryover_construction_total")
    if carryover_info and carryover_info[0] == orders_info[0]:
        shared["carryover_construction_total"] = carryover_info

    logger.debug("shared header detected at L%d: %s", orders_info[0],
                 list(shared.keys()))
    return shared


def _extract_from_shared_header(
    lines: list[str],
    shared_metrics: dict[str, tuple[int, str]],
    scale_str: str,
    result: ExtractResult,
    company_code: str,
    fiscal_year_end: str,
    quarter: str,
    period_type: str,
    source_doc_id: str,
    _extract_total_from_table_fn,
    _extract_numbers_from_line_fn,
    total_row_keywords: list[str],
) -> None:
    """共有ヘッダー行からメトリクスごとに正しい列の値を抽出する。

    ヘッダー行例:
        受注高 受注残高 受注高 受注残高 受注高 受注残高
    合計行例:
        合計 15,529,548 5,460,502 15,863,514 5,051,671 333,966 △408,831

    受注高の列 = 偶数位置 (0, 2, 4...)
    受注残高の列 = 奇数位置 (1, 3, 5...)
    """
    # 共有ヘッダー行のインデックス
    header_line_idx = next(iter(shared_metrics.values()))[0]
    header_line = lines[header_line_idx].strip()

    # ヘッダー行のキーワード出現順序からメトリクスの列位置を特定
    # 例: "受注高 受注残高 受注高 受注残高" → [("受注高", orders_total), ("受注残高", backlog_total), ...]
    header_kw_positions: list[tuple[int, str, str]] = []  # (text_pos, metric_name, kw)
    for metric_name, (_, kw) in shared_metrics.items():
        pos = header_line.find(kw)
        if pos >= 0:
            header_kw_positions.append((pos, metric_name, kw))

    # ヘッダーのキーワード位置でソート
    header_kw_positions.sort(key=lambda x: x[0])

    if not header_kw_positions:
        return

    # 列の数 = ヘッダー行のユニークなキーワード種類数
    # 受注高/受注残高の繰り返しパターンを検出
    unique_metrics_in_header = []
    seen_metrics = set()
    for _, mn, _ in header_kw_positions:
        if mn not in seen_metrics:
            unique_metrics_in_header.append(mn)
            seen_metrics.add(mn)

    n_metric_types = len(unique_metrics_in_header)
    if n_metric_types < 2:
        return  # 1種類だけなら共有ヘッダーではない

    # 合計行を探す
    search_end = min(header_line_idx + 30, len(lines))
    total_line = None
    total_line_idx = None
    for i in range(header_line_idx + 1, search_end):
        line = lines[i].strip()
        for tw in total_row_keywords:
            if tw in line:
                stripped = line.strip()
                if stripped.startswith(tw) or stripped == tw:
                    total_line = line
                    total_line_idx = i
                    break
        if total_line:
            break

    if total_line is None:
        return

    # 合計行から数値を抽出
    all_nums = _extract_numbers(total_line)
    if len(all_nums) < n_metric_types:
        return

    # 列位置マッピング: ヘッダーの繰り返しパターンに応じて
    # 典型パターン: "前期受注高 前期受注残高 当期受注高 当期受注残高 増減受注高 増減受注残高"
    # → nums[0]=前期受注高, nums[1]=前期受注残高, nums[2]=当期受注高, ...
    # 当期値は n_metric_types 番目から始まる (前期の次)
    #
    # ただし、合計行の数値数から列構造を推定:
    # - 6値 = 前期(2) + 当期(2) + 増減(2) → 当期は [2], [3]
    # - 4値 = 前期(2) + 当期(2) → 当期は [2], [3]
    # - 3値以上 = 前期(n) + 当期(n) + ... → 当期は [n_metric_types]~

    n_groups = len(all_nums) // n_metric_types if n_metric_types > 0 else 0
    if n_groups < 2:
        # 列数が足りない → 安全にスキップ
        logger.debug("shared header: not enough number groups (%d nums, %d types)",
                      len(all_nums), n_metric_types)
        return

    # 当期値の列インデックス (2番目のグループ)
    current_start_idx = n_metric_types  # 前期の後

    for col_offset, metric_name in enumerate(unique_metrics_in_header):
        num_idx = current_start_idx + col_offset
        if num_idx >= len(all_nums):
            continue

        value = all_nums[num_idx]
        if value is None:
            continue

        normalized = _unit_normalize(float(value), scale_str)
        effective_pt = resolve_period_type(metric_name, period_type)
        result.current_records.append(HistoricalRecord(
            company_code=company_code,
            target_fiscal_year_end=fiscal_year_end,
            target_quarter=quarter,
            target_period_type=effective_pt,
            metric_name=metric_name,
            value=normalized,
            unit="百万円",
            source_basis="",
            source_doc_id=source_doc_id,
            source_expression_type="absolute",
            confidence="high",
        ))

    logger.debug("shared header extraction: %d metrics from L%d total at L%d",
                 len(unique_metrics_in_header), header_line_idx, total_line_idx)


# ============================================================
# Same-value guard
# ============================================================

def _apply_same_value_guard(
    records: list[HistoricalRecord],
) -> tuple[list[HistoricalRecord], int]:
    """orders_total と backlog_total が同値の historical records を除去する。

    backlog_total == carryover_construction_total は建設業で正常なので許可。

    Note: orders_total は cumulative, backlog_total は point_in_time と
    period_type が異なるため、グループキーに period_type を含めない。

    Returns:
        (filtered_records, n_skipped)
    """
    # 同一 (company, fye, quarter) でグループ化（period_type は含めない）
    from collections import defaultdict
    groups: dict[tuple, dict[str, float]] = defaultdict(dict)
    for rec in records:
        key = (rec.company_code, rec.target_fiscal_year_end,
               rec.target_quarter)
        groups[key][rec.metric_name] = rec.value

    # 除去対象のキーを特定
    skip_keys: set[tuple] = set()
    for key, metrics in groups.items():
        orders_val = metrics.get("orders_total")
        backlog_val = metrics.get("backlog_total")

        if orders_val is not None and backlog_val is not None:
            if abs(orders_val - backlog_val) < 0.01:  # float 比較
                skip_keys.add(key)
                logger.info(
                    "same-value guard: skip %s %s/%s orders=backlog=%.1f",
                    key[0], key[1], key[2], orders_val,
                )

    if not skip_keys:
        return records, 0

    # skip_keys に属するレコードを除外 (orders と backlog 両方)
    filtered = []
    n_skipped = 0
    for rec in records:
        key = (rec.company_code, rec.target_fiscal_year_end,
               rec.target_quarter)
        if key in skip_keys and rec.metric_name in ("orders_total", "backlog_total"):
            n_skipped += 1
            continue
        filtered.append(rec)

    return filtered, n_skipped
