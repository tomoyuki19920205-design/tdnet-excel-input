"""Semantic EDINET order-table extraction.

The legacy extractor flattened multi-level headers and then selected the last
matching column.  This module keeps the DOM header path for every leaf column,
scores every table/row in the filing, and separates explicit backlog from
construction carryover.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from itertools import product
import json
import re
import unicodedata
import zipfile
from typing import Any, Iterable

from bs4 import BeautifulSoup


ORDER_KEYWORDS = (
    "当期受注工事高", "当期受注契約高", "期中受注工事高",
    "当期受注高", "受注工事高", "受注契約高", "受注金額", "受注額", "受注高",
)
EXPLICIT_BACKLOG_KEYWORDS = (
    "当期末受注残高", "当期末受注残", "期末受注残高", "期末受注残",
    "受注残高", "受注残", "オーダーバックログ", "バックログ",
)
BEGIN_CARRYOVER_KEYWORDS = (
    "前期繰越工事高", "期首繰越工事高", "前期繰越高", "期首繰越高",
)
END_CARRYOVER_KEYWORDS = (
    "次期繰越工事高", "期末繰越工事高", "当期末繰越工事高", "次期繰越高", "期末繰越高",
)
COMPLETED_CONSTRUCTION_KEYWORDS = (
    "当期完成工事高", "期中完成工事高", "完成工事高", "当期施工高",
)
RPO_KEYWORDS = ("残存履行義務", "未充足の履行義務")

CURRENT_PERIOD_KEYWORDS = ("当事業年度", "当連結会計年度", "当期", "当年度")
PREVIOUS_PERIOD_KEYWORDS = ("前事業年度", "前連結会計年度", "前期", "前年度")
RATE_KEYWORDS = (
    "%", "％", "比率", "増減率", "前期比", "前年比", "前年同期比",
    "構成比", "前年度比", "前年度末比",
)
CHANGE_KEYWORDS = ("増減額", "増減", "前年差")
SUBCOMPONENT_KEYWORDS = ("うち", "内訳", "施工高", "件数")
TOTAL_LABELS = ("全社計", "合計", "総計", "計", "工事合計", "報告セグメント計")
TOTAL_PRIORITY = {
    "全社計": 6,
    "合計": 5,
    "総計": 5,
    "計": 4,
    "工事合計": 3,
    "報告セグメント計": 2,
}
MIN_HEADER_SCORE_MARGIN = 30


def norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace("\u3000", " ").strip()


def compact(text: str) -> str:
    return re.sub(r"\s+", "", norm(text))


def parse_number(text: str | None) -> int | None:
    if not text:
        return None
    value = compact(text).replace(",", "").replace("△", "-").replace("▲", "-").replace("－", "-")
    match = re.fullmatch(r"\(?([+-]?\d+)\)?", value)
    return int(match.group(1)) if match else None


def parse_number_candidates(text: str | None) -> list[int]:
    """Return DOM-ordered reported integers from one cell.

    Some EDINET construction tables vertically stack two independently
    reported amounts inside one ``td``.  Flattening the cell concatenates the
    values and makes ``parse_number`` reject the actual amount column.  Keep
    every reported value so header and arithmetic evidence can resolve it.
    """
    direct = parse_number(text)
    if direct is not None:
        return [direct]
    value = norm(text or "").replace("△", "-").replace("▲", "-").replace("－", "-")
    matches = re.findall(r"\(?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\)?", value)
    output: list[int] = []
    for match in matches:
        parsed = parse_number(match)
        if parsed is not None:
            output.append(parsed)
    return output


def detect_unit(texts: Iterable[str]) -> str | None:
    value = compact(" ".join(texts))
    if "百万円" in value:
        return "百万円"
    if "千円" in value:
        return "千円"
    if "億円" in value:
        return "億円"
    if re.search(r"(?:単位[:：]?|金額[:：]?|[（(])円[）)]", value):
        return "円"
    return None


def _unambiguous_unit(text: str) -> str | None:
    value = compact(text)
    units: set[str] = set()
    for token in ("百万円", "千円", "億円"):
        if token in value:
            units.add(token)
    without_scaled = value.replace("百万円", "").replace("千円", "").replace("億円", "")
    if re.search(r"(?:単位[:：]?|金額[:：]?|[（(])円[）)]", without_scaled):
        units.add("円")
    return next(iter(units)) if len(units) == 1 else None


@dataclass(frozen=True)
class Origin:
    row: int
    column: int
    text: str
    rowspan: int
    colspan: int


@dataclass
class TableGrid:
    values: list[list[str]]
    origins: list[list[Origin | None]]


@dataclass
class LeafColumn:
    index: int
    header_path: list[str]
    parent_header: str
    leaf_header: str
    unit: str | None
    unit_source: str | None
    unit_evidence: str | None
    is_percentage: bool
    is_change: bool
    is_subcomponent: bool
    leaf_rowspan: int
    leaf_colspan: int
    header_spans: list[dict[str, int | str]]

    @property
    def text(self) -> str:
        return "|".join(self.header_path)


@dataclass
class MetricSelection:
    metric: str
    column: LeafColumn
    value: int
    score: int
    reason: list[str] = field(default_factory=list)
    value_index: int = 0
    value_count: int = 1


@dataclass
class RowCandidate:
    html_name: str
    table_index: int
    row_index: int
    row: list[str]
    row_label: str
    period_label: str
    selected_table_period: str | None
    unit: str | None
    unit_source: str | None
    metrics: dict[str, MetricSelection]
    score: float
    is_total: bool
    row_kind: str
    consolidation_scope: str
    segment_name: str | None
    arithmetic_status: str
    arithmetic_delta: int | None
    ambiguous_header: bool
    leaf_candidates: dict[str, list[dict[str, Any]]]
    selection_margins: dict[str, int | None]
    multi_value_resolution: str
    hierarchy_evidence: dict[str, Any] | None = None


def expand_table(table) -> TableGrid:
    rows = table.find_all("tr")
    values: list[list[str | None]] = []
    origins: list[list[Origin | None]] = []
    for r_idx, row in enumerate(rows):
        while len(values) <= r_idx:
            values.append([]); origins.append([])
        c_idx = 0
        for cell in row.find_all(["th", "td"], recursive=False):
            while c_idx < len(values[r_idx]) and values[r_idx][c_idx] is not None:
                c_idx += 1
            # Preserve separators between independently rendered values in a
            # data cell.  Header paths are compacted when they are built, but
            # collapsing ``※△558 269,399`` here would create the false amount
            # ``-558269`` before multi-value resolution can inspect it.
            text = norm(cell.get_text(" ", strip=True))
            colspan = int(cell.get("colspan", 1)); rowspan = int(cell.get("rowspan", 1))
            origin = Origin(r_idx, c_idx, text, rowspan, colspan)
            for dr in range(rowspan):
                for dc in range(colspan):
                    rr, cc = r_idx + dr, c_idx + dc
                    while len(values) <= rr:
                        values.append([]); origins.append([])
                    while len(values[rr]) <= cc:
                        values[rr].append(None); origins[rr].append(None)
                    values[rr][cc] = text; origins[rr][cc] = origin
            c_idx += colspan
    width = max((len(row) for row in values), default=0)
    clean_values: list[list[str]] = []
    clean_origins: list[list[Origin | None]] = []
    for row, source_row in zip(values, origins):
        clean_values.append([x or "" for x in row] + [""] * (width - len(row)))
        clean_origins.append(source_row + [None] * (width - len(source_row)))
    return TableGrid(clean_values, clean_origins)


def _is_financial_cell(text: str) -> bool:
    value = compact(text)
    if re.fullmatch(r"[△▲+\-－]?\(?\d{1,3}(?:,\d{3})+\)?", value):
        return True
    return bool(re.fullmatch(r"[△▲+\-－]?\(?\d{3,}\)?", value))


def header_row_count(grid: TableGrid) -> int:
    count = 0
    for row in grid.values:
        if any(_is_financial_cell(cell) for cell in row[1:]):
            break
        count += 1
    if not grid.values:
        return 0
    # A compact table can legitimately have two header rows and only one data
    # row (for example, parent ``受注高`` with previous/current child leaves).
    # Capping headers at half the table incorrectly turns the second header
    # into data and discards its current-period semantics.
    return min(max(count, 1), max(1, len(grid.values) - 1))


def _column_unit(path: list[str], unit_context: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    if path:
        unit = _unambiguous_unit(path[-1])
        if unit:
            return unit, "selected_leaf_header", path[-1]
        for parent in reversed(path[:-1]):
            unit = _unambiguous_unit(parent)
            if unit:
                return unit, "parent_header", parent
    for source in ("table_caption", "nearby_text", "source_block_heading"):
        evidence = unit_context.get(source, "")
        unit = _unambiguous_unit(evidence)
        if unit:
            return unit, source, evidence[:500]
    return None, None, None


def build_leaf_columns(grid: TableGrid, n_header: int, unit_context: dict[str, str] | str | None) -> list[LeafColumn]:
    # Backward-compatible support for direct unit strings used by callers.
    if not isinstance(unit_context, dict):
        unit_context = {"nearby_text": unit_context or ""}
    columns: list[LeafColumn] = []
    width = max((len(row) for row in grid.values), default=0)
    for col in range(width):
        path: list[str] = []
        header_spans: list[dict[str, int | str]] = []
        seen: set[tuple[int, int]] = set()
        for row in range(n_header):
            origin = grid.origins[row][col] if col < len(grid.origins[row]) else None
            if not origin or not origin.text or (origin.row, origin.column) in seen:
                continue
            seen.add((origin.row, origin.column)); path.append(compact(origin.text))
            header_spans.append({
                "text": origin.text,
                "rowspan": origin.rowspan,
                "colspan": origin.colspan,
            })
        leaf = path[-1] if path else ""
        leaf_origin = next(
            (grid.origins[row][col] for row in range(n_header - 1, -1, -1)
             if col < len(grid.origins[row]) and grid.origins[row][col] is not None),
            None,
        )
        text = "|".join(path)
        unit, unit_source, unit_evidence = _column_unit(path, unit_context)
        columns.append(LeafColumn(
            index=col,
            header_path=path,
            parent_header=path[0] if path else "",
            leaf_header=leaf,
            unit=unit,
            unit_source=unit_source,
            unit_evidence=unit_evidence,
            is_percentage=any(key in text for key in RATE_KEYWORDS),
            is_change=any(key in text for key in CHANGE_KEYWORDS),
            is_subcomponent=any(key in text for key in SUBCOMPONENT_KEYWORDS),
            leaf_rowspan=leaf_origin.rowspan if leaf_origin else 1,
            leaf_colspan=leaf_origin.colspan if leaf_origin else 1,
            header_spans=header_spans,
        ))
    return columns


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _metric_for_column(column: LeafColumn) -> list[str]:
    text = column.text
    metrics: list[str] = []
    if _contains_any(text, EXPLICIT_BACKLOG_KEYWORDS):
        metrics.append("order_backlog")
    if _contains_any(text, BEGIN_CARRYOVER_KEYWORDS):
        metrics.append("beginning_carryover")
    if _contains_any(text, END_CARRYOVER_KEYWORDS):
        metrics.append("construction_carryover")
    if _contains_any(text, COMPLETED_CONSTRUCTION_KEYWORDS):
        metrics.append("completed_construction")
    if _contains_any(text, RPO_KEYWORDS):
        metrics.append("rpo")
    if _contains_any(text, ORDER_KEYWORDS) and not _contains_any(
        text, EXPLICIT_BACKLOG_KEYWORDS + COMPLETED_CONSTRUCTION_KEYWORDS
    ):
        metrics.append("orders_received")
    return metrics


def _column_score(metric: str, column: LeafColumn, value: int, expected_period: str | None) -> tuple[int, list[str]]:
    text = column.text; leaf = column.leaf_header
    score = 100; reason = ["semantic_keyword"]
    if column.is_percentage:
        return -10_000, ["percentage_leaf_rejected"]
    if column.is_change:
        score -= 100; reason.append("change_leaf_penalty")
    if column.unit:
        score += 20; reason.append("leaf_or_table_unit")
    if column.is_subcomponent:
        score -= 80; reason.append("subcomponent_penalty")
    if metric == "construction_carryover":
        if "手持工事高" in leaf:
            score += 50; reason.append("ending_amount_leaf")
        if "うち" in leaf or "施工高" in leaf:
            score -= 100; reason.append("ending_subcomponent_rejected")
        if len(column.header_path) == 1:
            score += 35; reason.append("direct_ending_header")
    if metric == "orders_received":
        if _contains_any(leaf, ("当期受注工事高", "当期受注高", "期中受注工事高")):
            score += 45; reason.append("explicit_current_orders_leaf")
    if metric == "order_backlog" and _contains_any(leaf, EXPLICIT_BACKLOG_KEYWORDS):
        score += 50; reason.append("explicit_backlog_leaf")
    # Current/prior headers qualify every period-sensitive amount metric, not
    # only orders received.  Without this, an explicit backlog pair ties and
    # DOM order silently promotes the previous-period leaf.
    if metric in {
        "orders_received", "order_backlog", "construction_carryover",
        "completed_construction", "rpo",
    }:
        if _contains_any(text, PREVIOUS_PERIOD_KEYWORDS):
            score -= 80; reason.append("previous_column_penalty")
        if _contains_any(text, CURRENT_PERIOD_KEYWORDS):
            score += 45; reason.append("current_column")
    if expected_period:
        year = expected_period[:4]
        jp_date = expected_period.replace("-", "年", 1).replace("-", "月", 1) + "日"
        if year in text or jp_date in text:
            score += 15; reason.append("expected_period_header")
    # A rate-looking value without an explicit rate header remains suspicious.
    if abs(value) < 100 and metric in {"orders_received", "order_backlog", "construction_carryover"}:
        score -= 25; reason.append("small_value_penalty")
    return score, reason


def _row_label(row: list[str], metric_columns: Iterable[int]) -> str:
    first_metric = min(metric_columns, default=len(row))
    labels = [cell for cell in row[:first_metric] if cell]
    return "|".join(labels)


def _total_priority(label: str) -> int:
    cells = [compact(cell) for cell in label.split("|") if compact(cell)]
    matches = [
        priority for keyword, priority in TOTAL_PRIORITY.items()
        if any(cell == keyword for cell in cells)
    ]
    return max(matches, default=0)


def _row_kind(label: str) -> str:
    cells = [compact(cell) for cell in label.split("|") if compact(cell)]
    if any(cell in TOTAL_PRIORITY for cell in cells):
        return "grand_total"
    if any(cell == "小計" or cell.endswith("小計") for cell in cells):
        return "subtotal"
    return "detail"


def _consolidation_scope(label: str) -> str:
    value = compact(label)
    if "連結" in value:
        return "consolidated"
    if "事業年度" in value or "単体" in value or "個別" in value:
        return "standalone"
    return "unknown"


def _extract_date_from_label(label: str) -> str | None:
    matches = re.findall(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", label)
    if matches:
        year, month, day = matches[-1]
        return f"{year}-{int(month):02d}-{int(day):02d}"
    # EDINET construction tables often express row periods in Japanese eras.
    era_matches = re.findall(r"令和(元|\d+)年(\d{1,2})月(\d{1,2})日", label)
    if not era_matches:
        return None
    era_year, month, day = era_matches[-1]
    year = 2019 if era_year == "元" else 2018 + int(era_year)
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _period_score(label: str, expected_period: str | None) -> tuple[int, str]:
    row_period = _extract_date_from_label(label)
    if expected_period and row_period == expected_period:
        return 180, "exact_current_period"
    if _contains_any(label, CURRENT_PERIOD_KEYWORDS):
        return 120, "current_period_label"
    if _contains_any(label, PREVIOUS_PERIOD_KEYWORDS):
        return -120, "previous_period_label"
    if expected_period and row_period:
        try:
            delta = abs((date.fromisoformat(expected_period) - date.fromisoformat(row_period)).days)
            if 330 <= delta <= 370:
                return -90, "previous_year_date"
        except ValueError:
            pass
    return 0, "period_unlabelled"


def _arithmetic(metrics: dict[str, MetricSelection]) -> tuple[str, int | None]:
    keys = ("beginning_carryover", "orders_received", "completed_construction", "construction_carryover")
    if not all(key in metrics for key in keys):
        return "NOT_APPLICABLE", None
    begin, orders, completed, ending = (metrics[key].value for key in keys)
    delta = (begin + orders) - (completed + ending)
    tolerance = max(2, round(max(abs(begin + orders), abs(completed + ending), 1) * 0.0001))
    return ("PASS" if abs(delta) <= tolerance else "ARITHMETIC_REVIEW"), delta


def _table_unit_context(table) -> dict[str, str]:
    """Collect local unit evidence without leaking page-global units."""
    caption = table.find("caption")
    caption_text = compact(caption.get_text(" ", strip=True)) if caption else ""

    nearby: list[str] = []
    found_local_annotation = False
    # EDINET commonly emits ``(単位：千円)`` as a short paragraph immediately
    # before the table inside a wrapper div.  DOM sibling traversal alone
    # misses it when the table is nested.  Stop at the previous table so this
    # remains local and can never become a page-global first-unit fallback.
    for node in table.find_all_previous(["p", "div", "table"], limit=30):
        if getattr(node, "name", None) == "table":
            break
        if node.find_parent("table") is not None:
            continue
        value = compact(node.get_text(" ", strip=True))
        if value and len(value) <= 160 and _unambiguous_unit(value):
            nearby.append(value)
            found_local_annotation = True
            break
    if not found_local_annotation:
        anchor = table
        for _depth in range(3):
            node = anchor.find_previous_sibling()
            while node is not None and len(nearby) < 8:
                if getattr(node, "name", None) == "table":
                    break
                value = compact(node.get_text(" ", strip=True))
                # Unit annotations are short.  Excluding large blocks prevents a
                # unit from an unrelated earlier table on the same page leaking in.
                if value and len(value) <= 300 and getattr(node, "name", None) not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    nearby.append(value)
                node = node.find_previous_sibling()
            anchor = anchor.parent
            if anchor is None or getattr(anchor, "name", None) in {"body", "html"}:
                break

    heading = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    heading_text = compact(heading.get_text(" ", strip=True)) if heading else ""
    return {
        "table_caption": caption_text,
        "nearby_text": "|".join(nearby),
        "source_block_heading": heading_text,
    }


def _resolve_arithmetic_options(
    options: dict[str, list[MetricSelection]],
) -> tuple[dict[str, MetricSelection], str, int | None, str]:
    keys = ("beginning_carryover", "orders_received", "completed_construction", "construction_carryover")
    selected = {metric: max(items, key=lambda item: (item.score, -item.value_index)) for metric, items in options.items() if items}
    if not all(key in options for key in keys):
        status, delta = _arithmetic(selected)
        return selected, status, delta, "not_applicable"

    pools: list[list[MetricSelection]] = []
    for key in keys:
        best_score = max(item.score for item in options[key])
        unique: dict[tuple[int, int], MetricSelection] = {}
        for item in options[key]:
            if item.score < best_score - 100:
                continue
            unique.setdefault((item.column.index, item.value), item)
        pools.append(sorted(unique.values(), key=lambda item: (-item.score, item.value_index))[:6])

    passing: list[tuple[int, tuple[MetricSelection, ...], int]] = []
    for combo in product(*pools):
        begin, orders, completed, ending = (item.value for item in combo)
        delta = (begin + orders) - (completed + ending)
        tolerance = max(2, round(max(abs(begin + orders), abs(completed + ending), 1) * 0.0001))
        if abs(delta) <= tolerance:
            rank = sum(item.score for item in combo) - sum(item.value_index for item in combo) * 2
            passing.append((rank, combo, delta))
    passing.sort(key=lambda item: item[0], reverse=True)
    if passing and (len(passing) == 1 or passing[0][0] > passing[1][0]):
        _, combo, delta = passing[0]
        selected.update(dict(zip(keys, combo)))
        return selected, "PASS", delta, "unique_arithmetic_match"

    # EDINET sometimes renders a small footnote adjustment and the reported
    # total in the same td (for example ``※2,242 301,713``).  If arithmetic
    # cannot disambiguate, accept only a uniquely dominant magnitude.  Close
    # alternatives remain ambiguous, preserving the 1960-style safety gate.
    dominant = dict(selected)
    dominant_used = False
    dominant_unresolved = False
    for metric, items in options.items():
        best_score = max(item.score for item in items)
        best_column = min(item.column.index for item in items if item.score == best_score)
        peers = [
            item for item in items
            if item.score == best_score and item.column.index == best_column
        ]
        if len(peers) < 2:
            continue
        ranked = sorted(peers, key=lambda item: abs(item.value), reverse=True)
        largest = abs(ranked[0].value)
        second = abs(ranked[1].value)
        if largest >= max(1, second) * 5:
            choice = ranked[0]
            choice.reason = [*choice.reason, "dominant_magnitude_in_multi_value_cell"]
            dominant[metric] = choice
            dominant_used = True
        else:
            dominant_unresolved = True
    if dominant_used and not dominant_unresolved:
        status, delta = _arithmetic(dominant)
        return dominant, status, delta, "dominant_magnitude_fallback"

    status, delta = _arithmetic(selected)
    resolution = "ambiguous_arithmetic_matches" if passing else "no_arithmetic_match"
    return selected, status, delta, resolution


def _candidate_rows(
    html_name: str,
    table_index: int,
    table,
    expected_period: str | None,
) -> list[RowCandidate]:
    grid = expand_table(table)
    if not grid.values:
        return []
    n_header = header_row_count(grid)
    unit_context = _table_unit_context(table)
    columns = build_leaf_columns(grid, n_header, unit_context)
    if not any(_metric_for_column(column) for column in columns):
        return []
    parent_counts = {
        parent: sum(column.parent_header == parent for column in columns)
        for parent in {column.parent_header for column in columns if column.parent_header}
    }
    repeated_parent = any(count > 1 for count in parent_counts.values())
    output: list[RowCandidate] = []
    for row_index, row in enumerate(grid.values[n_header:], n_header):
        metric_options: dict[str, list[MetricSelection]] = {}
        for column in columns:
            if column.index >= len(row):
                continue
            values = parse_number_candidates(row[column.index])
            if not values:
                continue
            for value_index, value in enumerate(values):
                for metric in _metric_for_column(column):
                    score, reason = _column_score(metric, column, value, expected_period)
                    if len(values) > 1:
                        reason = [*reason, "multi_value_cell_candidate"]
                    metric_options.setdefault(metric, []).append(MetricSelection(
                        metric, column, value, score, reason, value_index=value_index, value_count=len(values)
                    ))
        metrics, arithmetic_status, arithmetic_delta, multi_value_resolution = _resolve_arithmetic_options(metric_options)
        if not metrics or not any(k in metrics for k in ("orders_received", "order_backlog", "construction_carryover", "rpo")):
            continue
        label = _row_label(row, (selection.column.index for selection in metrics.values()))
        total_priority = _total_priority(label)
        row_kind = _row_kind(label)
        consolidation_scope = _consolidation_scope(label)
        p_score, p_reason = _period_score(label, expected_period)
        coverage = sum(key in metrics for key in (
            "orders_received", "order_backlog", "construction_carryover",
            "completed_construction", "beginning_carryover", "rpo",
        ))
        score = coverage * 30 + p_score + total_priority * 25 + table_index / 1000
        if consolidation_scope == "consolidated":
            score += 120
        if arithmetic_status == "PASS":
            score += 70
        elif arithmetic_status == "SOURCE_TABLE_EXCEPTION":
            score -= 10
        is_total = row_kind == "grand_total"
        if arithmetic_status == "ARITHMETIC_REVIEW" and is_total and all(
            len(metrics[key].column.header_path) == 1
            and not metrics[key].column.is_percentage
            and not metrics[key].column.is_subcomponent
            for key in ("beginning_carryover", "orders_received", "completed_construction", "construction_carryover")
        ):
            # A simple, direct four-column total row is strong evidence that
            # the discrepancy is present in the source table itself.  Complex
            # or ambiguous header trees remain review-only and are not saved.
            arithmetic_status = "SOURCE_TABLE_EXCEPTION"
        if not is_total:
            score -= 80
        leaf_candidates: dict[str, list[dict[str, Any]]] = {}
        selection_margins: dict[str, int | None] = {}
        for metric, items in metric_options.items():
            ranked = sorted(items, key=lambda item: (-item.score, item.column.index, item.value_index))
            leaf_candidates[metric] = [
                {
                    "column_index": item.column.index,
                    "header_path": item.column.header_path,
                    "value": item.value,
                    "value_index": item.value_index,
                    "value_count": item.value_count,
                    "amount": not item.column.is_percentage,
                    "percentage": item.column.is_percentage,
                    "current": _contains_any(item.column.text, CURRENT_PERIOD_KEYWORDS),
                    "previous": _contains_any(item.column.text, PREVIOUS_PERIOD_KEYWORDS),
                    "row_period_kind": p_reason,
                    "row_label": label,
                    "row_is_total": is_total,
                    "row_is_detail": not is_total,
                    "unit": item.column.unit,
                    "unit_source": item.column.unit_source,
                    "arithmetic_status": arithmetic_status,
                    "score": item.score,
                    "reason": item.reason,
                }
                for item in ranked[:12]
            ]
            column_scores: dict[int, int] = {}
            for item in ranked:
                column_scores[item.column.index] = max(column_scores.get(item.column.index, -100_000), item.score)
            scores = sorted(column_scores.values(), reverse=True)
            selection_margins[metric] = scores[0] - scores[1] if len(scores) > 1 else None
        low_margin = any(
            margin is not None and margin < MIN_HEADER_SCORE_MARGIN
            for metric, margin in selection_margins.items()
            if metric in {"orders_received", "order_backlog", "construction_carryover", "rpo"}
        )
        selected_subcomponent = any(selection.column.is_subcomponent for selection in metrics.values())
        unresolved_multi_value = any(
            selection.value_count > 1 for metric, selection in metrics.items()
            if metric in {"orders_received", "order_backlog", "construction_carryover", "rpo"}
        ) and multi_value_resolution not in {"unique_arithmetic_match", "dominant_magnitude_fallback"}
        output.append(RowCandidate(
            html_name=html_name, table_index=table_index, row_index=row_index, row=row,
            row_label=label, period_label=p_reason,
            selected_table_period=_extract_date_from_label(label),
            unit=next((selection.column.unit for selection in metrics.values() if selection.column.unit), None),
            unit_source=next((selection.column.unit_source for selection in metrics.values() if selection.column.unit), None),
            metrics=metrics, score=score, is_total=is_total,
            row_kind=row_kind, consolidation_scope=consolidation_scope,
            segment_name=None if is_total else (label.split("|")[-1] if label else None),
            arithmetic_status=arithmetic_status, arithmetic_delta=arithmetic_delta,
            ambiguous_header=selected_subcomponent or unresolved_multi_value or (repeated_parent and low_margin),
            leaf_candidates=leaf_candidates,
            selection_margins=selection_margins,
            multi_value_resolution=multi_value_resolution,
        ))
    _promote_hierarchical_parent_total(output, grid, columns)
    return output


def _promote_hierarchical_parent_total(
    candidates: list[RowCandidate],
    grid: TableGrid,
    columns: list[LeafColumn],
) -> None:
    """Promote a structurally proven parent row to the company total.

    Some EDINET tables omit an explicit ``合計`` row.  Instead they report one
    top-level amount row followed by its child breakdown; the first label cell
    of that breakdown is an empty ``rowspan`` group marker.  Treating every row
    as a peer and summing it double-counts the parent.  Promotion is deliberately
    strict: an explicit total always wins, exactly one full-width parent must
    exist, the child block must be DOM-proven, and every reported parent metric
    must reconcile to the child sum.
    """
    if not candidates or any(candidate.row_kind == "grand_total" for candidate in candidates):
        return

    metric_columns = [
        column.index for column in columns
        if _metric_for_column(column) and not column.is_percentage and not column.is_subcomponent
    ]
    first_metric = min(metric_columns, default=0)
    if first_metric < 2:
        return

    by_row = {candidate.row_index: candidate for candidate in candidates}
    top_level: list[RowCandidate] = []
    for candidate in candidates:
        if candidate.row_index >= len(grid.origins):
            continue
        origin = grid.origins[candidate.row_index][0] if grid.origins[candidate.row_index] else None
        if (
            origin is not None
            and origin.row == candidate.row_index
            and bool(compact(origin.text))
            and origin.colspan >= first_metric
        ):
            top_level.append(candidate)
    if len(top_level) != 1:
        return

    parent = top_level[0]
    child_start = parent.row_index + 1
    if child_start >= len(grid.origins) or not grid.origins[child_start]:
        return
    group_origin = grid.origins[child_start][0]
    if (
        group_origin is None
        or group_origin.row != child_start
        or compact(group_origin.text)
        or group_origin.rowspan < 2
    ):
        return

    child_rows = list(range(child_start, child_start + group_origin.rowspan))
    if child_rows[-1] >= len(grid.values):
        return
    children = [by_row.get(row_index) for row_index in child_rows]
    if any(child is None for child in children):
        return
    child_candidates = [child for child in children if child is not None]
    if any(child.row_kind != "detail" for child in child_candidates):
        return
    if any(not child.row_label for child in child_candidates):
        return

    material_metrics = (
        "orders_received", "order_backlog", "construction_carryover",
        "completed_construction", "rpo",
    )
    reconciliations: dict[str, dict[str, Any]] = {}
    for metric in material_metrics:
        parent_selection = parent.metrics.get(metric)
        if parent_selection is None:
            continue
        child_selections = [child.metrics.get(metric) for child in child_candidates]
        if any(selection is None for selection in child_selections):
            return
        concrete = [selection for selection in child_selections if selection is not None]
        if any(selection.column.index != parent_selection.column.index for selection in concrete):
            return
        if any(selection.column.unit != parent_selection.column.unit for selection in concrete):
            return
        child_sum = sum(selection.value for selection in concrete)
        delta = parent_selection.value - child_sum
        # Parent and children are independently rounded display amounts.  The
        # maximum legitimate reconciliation noise depends on the number of
        # displayed children, not the magnitude of the business itself.
        tolerance = max(2, (len(concrete) + 1) // 2)
        if abs(delta) > tolerance:
            return
        reconciliations[metric] = {
            "parent_value": parent_selection.value,
            "child_sum": child_sum,
            "delta": delta,
            "tolerance": tolerance,
        }
    if "orders_received" not in reconciliations:
        return

    parent.row_kind = "hierarchical_parent_total"
    parent.is_total = True
    parent.segment_name = None
    # Remove the detail penalty (-80) and give the same priority as an
    # explicit 合計 row (5 * 25).  Explicit totals never enter this branch.
    parent.score += 205
    parent.hierarchy_evidence = {
        "rule": "single_full_width_parent_with_rowspan_child_breakdown",
        "parent_row_index": parent.row_index,
        "child_row_indices": child_rows,
        "child_row_labels": [child.row_label for child in child_candidates],
        "label_column_count": first_metric,
        "parent_label_colspan": grid.origins[parent.row_index][0].colspan,
        "child_group_rowspan": group_origin.rowspan,
        "reconciliations": reconciliations,
    }
    for items in parent.leaf_candidates.values():
        for item in items:
            item["row_is_total"] = True
            item["row_is_detail"] = False


def _dei_value(soup: BeautifulSoup, suffix: str) -> str | None:
    for tag in soup.find_all(attrs={"name": True}):
        if str(tag.get("name", "")).lower().endswith(suffix.lower()):
            value = compact(tag.get_text(" ", strip=True))
            if value:
                return value
    return None


def _report_type(target: dict[str, Any], dei: dict[str, str | None]) -> str:
    code = str(target.get("doc_type_code") or target.get("document_type") or "")
    if not code and target.get("docs"):
        doc = target["docs"][0] or {}
        code = str(doc.get("docTypeCode") or doc.get("document_type") or "")
    if code in {"120", "130"}:
        return "FY"
    if code in {"160", "170"}:
        return "HY"
    if code in {"140", "150"}:
        return "Q"
    document_type = dei.get("document_type") or ""
    period_type = dei.get("period_type") or ""
    if "第四号の三" in document_type or "半期" in period_type:
        return "HY"
    if "四半期" in document_type or "四半期" in period_type:
        return "Q"
    if "第三号" in document_type or period_type in {"通期", "FY", "年度"}:
        return "FY"
    return "UNKNOWN"


def extract_semantic_tables(zip_path: str, target: dict[str, Any]) -> dict[str, Any] | None:
    """Return the best semantic table result from one EDINET filing."""
    candidates: list[RowCandidate] = []
    soups: list[BeautifulSoup] = []
    dei: dict[str, str | None] = {}
    with zipfile.ZipFile(zip_path) as archive:
        html_names = [name for name in archive.namelist() if "PublicDoc" in name and name.lower().endswith(".htm")]
        for html_name in html_names:
            raw = archive.read(html_name)
            try:
                html = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                html = raw.decode("cp932", errors="replace")
            soup = BeautifulSoup(html, "html.parser"); soups.append(soup)
            if not dei:
                dei = {
                    "current_period_end": _dei_value(soup, "CurrentPeriodEndDateDEI"),
                    "fiscal_year_end": _dei_value(soup, "CurrentFiscalYearEndDateDEI"),
                    "document_type": _dei_value(soup, "DocumentTypeDEI"),
                    "period_type": _dei_value(soup, "TypeOfCurrentPeriodDEI"),
                }
            expected_period = (
                target.get("period_end") or target.get("fiscal_end") or target.get("api_period_end")
                or dei.get("fiscal_year_end") or dei.get("current_period_end")
            )
            for table_index, table in enumerate(soup.find_all("table")):
                candidates.extend(_candidate_rows(html_name, table_index, table, expected_period))
    if not candidates:
        return None
    expected_period = (
        target.get("period_end") or target.get("fiscal_end") or target.get("api_period_end")
        or dei.get("fiscal_year_end") or dei.get("current_period_end")
    )
    candidates.sort(key=lambda item: item.score, reverse=True)
    filing_fye = dei.get("fiscal_year_end")
    target_is_previous = False
    if expected_period and filing_fye and expected_period != filing_fye:
        try:
            days = (date.fromisoformat(filing_fye) - date.fromisoformat(expected_period)).days
            target_is_previous = 330 <= days <= 370
        except ValueError:
            target_is_previous = False
    if target_is_previous:
        # Separate previous/current construction tables frequently have no
        # period label inside either table.  Their document order is previous
        # then current, so reverse only the tiny ordinal tie-breaker when the
        # caller explicitly requests the comparative fiscal year.
        for candidate in candidates:
            if candidate.period_label == "period_unlabelled":
                candidate.score -= candidate.table_index * 100
        candidates.sort(key=lambda item: item.score, reverse=True)
    best = candidates[0]

    # Supplement a missing explicit metric only from another non-previous total
    # candidate in the same filing.  Construction metrics are never duplicated
    # into explicit backlog.
    metrics = dict(best.metrics)
    for metric in ("orders_received", "order_backlog", "construction_carryover", "completed_construction", "rpo"):
        if metric in metrics:
            continue
        alternatives = [
            item for item in candidates
            if metric in item.metrics and item.is_total and item.period_label != "previous_period_label"
        ]
        if alternatives:
            metrics[metric] = max(alternatives, key=lambda item: item.score).metrics[metric]

    report_type = _report_type(target, dei)
    mismatch = report_type in {"HY", "Q"}
    provenance_metrics = {
        metric: {
            "column_index": selection.column.index,
            "header_path": selection.column.header_path,
            "header_spans": selection.column.header_spans,
            "leaf_rowspan": selection.column.leaf_rowspan,
            "leaf_colspan": selection.column.leaf_colspan,
            "unit": selection.column.unit,
            "unit_source": selection.column.unit_source,
            "unit_evidence": selection.column.unit_evidence,
            "score": selection.score,
            "reason": selection.reason,
        }
        for metric, selection in metrics.items()
    }
    provenance = {
        "version": "semantic_table_v2",
        "source_doc_id": target.get("doc_id"),
        "source_html": best.html_name,
        "source_table_index": best.table_index,
        "source_row_index": best.row_index,
        "source_row_label": best.row_label,
        "selected_row_kind": best.row_kind,
        "consolidation_scope": best.consolidation_scope,
        "consolidated": best.consolidation_scope == "consolidated",
        "selected_table_period": best.selected_table_period,
        "period_selection": best.period_label,
        "report_type": report_type,
        "document_type": dei.get("document_type"),
        "current_period_end": dei.get("current_period_end"),
        "fiscal_year_end": dei.get("fiscal_year_end"),
        "source_unit": best.unit,
        "source_unit_source": best.unit_source,
        "selection_reason": "highest_score_after_all_table_comparison",
        "selection_score": best.score,
        "candidate_count": len(candidates),
        "arithmetic_status": best.arithmetic_status,
        "arithmetic_delta": best.arithmetic_delta,
        "multi_value_resolution": best.multi_value_resolution,
        "hierarchy_evidence": best.hierarchy_evidence,
        "selection_margin_threshold": MIN_HEADER_SCORE_MARGIN,
        "selection_margins": best.selection_margins,
        "leaf_candidates": best.leaf_candidates,
        "metrics": provenance_metrics,
    }
    flat_header = [" > ".join(selection.column.header_path) for selection in metrics.values()]
    snippet = (
        f"HeaderPath: {' | '.join(flat_header)}\n"
        f"Row: {' | '.join(best.row[:12])}\n"
        f"Provenance: {json.dumps(provenance, ensure_ascii=False, separators=(',', ':'))}"
    )
    candidate_evidence = [
        {
            "source_table_index": candidate.table_index,
            "source_row_index": candidate.row_index,
            "row_label": candidate.row_label,
            "period_selection": candidate.period_label,
            "selected_table_period": candidate.selected_table_period,
            "score": candidate.score,
            "is_total": candidate.is_total,
            "row_kind": candidate.row_kind,
            "consolidation_scope": candidate.consolidation_scope,
            "orders_received": candidate.metrics.get("orders_received").value
            if candidate.metrics.get("orders_received") else None,
            "order_backlog": candidate.metrics.get("order_backlog").value
            if candidate.metrics.get("order_backlog") else None,
            "construction_carryover": candidate.metrics.get("construction_carryover").value
            if candidate.metrics.get("construction_carryover") else None,
            "arithmetic_status": candidate.arithmetic_status,
            "ambiguous_header": candidate.ambiguous_header,
            "selection_margins": candidate.selection_margins,
            "hierarchy_evidence": candidate.hierarchy_evidence,
        }
        for candidate in candidates[:20]
    ]
    result: dict[str, Any] = {
        "unit": best.unit,
        "orders_received": metrics.get("orders_received").value if metrics.get("orders_received") else None,
        "order_backlog": metrics.get("order_backlog").value if metrics.get("order_backlog") else None,
        "construction_carryover": metrics.get("construction_carryover").value if metrics.get("construction_carryover") else None,
        "completed_construction": metrics.get("completed_construction").value if metrics.get("completed_construction") else None,
        "beginning_carryover": metrics.get("beginning_carryover").value if metrics.get("beginning_carryover") else None,
        "rpo": metrics.get("rpo").value if metrics.get("rpo") else None,
        "segment_name": best.segment_name,
        "source_type": "table",
        "source_tag": "semantic_table_v2",
        "snippet": snippet[:12000],
        "provenance": provenance,
        # Dry-run/audit evidence.  This is deliberately not part of the DB
        # insert contract; persisted provenance stays compact.
        "candidate_evidence": candidate_evidence,
        "report_type": report_type,
        "document_period_mismatch": mismatch,
        "previous_period_selected_while_current_exists": (
            not target_is_previous and best.period_label in {"previous_period_label", "previous_year_date"}
        ),
        "arithmetic_status": best.arithmetic_status,
        "arithmetic_delta": best.arithmetic_delta,
        "selected_percentage_leaf": any(selection.column.is_percentage for selection in metrics.values()),
        "selected_subcomponent_leaf": any(selection.column.is_subcomponent for selection in metrics.values()),
        "ambiguous_header": best.ambiguous_header,
        "source_unit_consistent": all(
            selection.column.unit in {None, best.unit} for selection in metrics.values()
        ),
        "has_total_row": best.is_total,
        "selected_period_kind": best.period_label,
        "confidence": "high" if best.is_total and best.unit and metrics.get("orders_received") else "medium",
        "notes": "",
    }
    return result
