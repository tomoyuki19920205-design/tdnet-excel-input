"""IPO listing-day financial disclosure parsing and persistence.

TDnet's "上場に伴う（当社）決算情報等のお知らせ" is not a normal
earnings-release title and normally has no XBRL attachment.  The PDF nevertheless
contains a summary table plus an appended earnings-statement section.  This module
keeps that special document classification small while reusing the canonical
financial writer and the existing TDnet notification store.
"""
from __future__ import annotations

import calendar
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from src.common_ticker import is_valid_ticker, normalize_ticker

logger = logging.getLogger("ipo_listing_financials")

IPO_LISTING_FINANCIALS = "ipo_listing_financials"


def normalize_disclosure_title(title: str) -> str:
    """Normalize full/half width, whitespace and line breaks for classification."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(title or ""))).lower()


def is_ipo_listing_financial_title(title: str) -> bool:
    """Return true only for listing disclosures that explicitly contain financials."""
    normalized = normalize_disclosure_title(title)
    return bool(
        "上場に伴う" in normalized
        and re.search(r"上場に伴う(?:当社)?決算情報等?のお知らせ", normalized)
    )


def disclosure_identity(source_url: str, source_doc_id: str | None = None) -> str:
    """Use provider ID when present, otherwise the normalized official PDF URL."""
    if source_doc_id and str(source_doc_id).strip():
        return str(source_doc_id).strip()
    raw = unquote(str(source_url or "").strip())
    match = re.search(r"/([0-9A-Za-z_-]+)\.pdf(?:[?#]|$)", raw, re.IGNORECASE)
    if match:
        return match.group(1)
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"
    return raw


def display_company_name(company_name: str) -> str:
    """Apply the card convention: omit a leading corporate designator."""
    name = unicodedata.normalize("NFKC", str(company_name or "")).strip()
    name = re.sub(r"^[PGR]-\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^(?:株式会社|\(株\)|㈱)\s*", "", name)
    return name.strip()


@dataclass(frozen=True)
class IpoFinancialPeriod:
    period: str
    quarter: str
    kind: str  # actual | forecast
    metrics: dict[str, float]
    period_start: str | None = None
    period_end: str | None = None
    source_unit: str = "百万円"
    source_page: int = 1


@dataclass(frozen=True)
class IpoListingExtraction:
    ticker: str
    company_name: str
    periods: tuple[IpoFinancialPeriod, ...]
    source_pages: tuple[int, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SUMMARY_METRICS = {
    "税引前当期(中間)利益": "profit_before_tax",
    "税引前当期利益": "profit_before_tax",
    "税引前中間利益": "profit_before_tax",
    "基本的1株当たり当期(中間)利益": "eps",
    "1株当たり当期(四半期)純利益": "eps",
    "1株当たり当期(中間)純利益": "eps",
    "売上高": "sales",
    "売上収益": "sales",
    "営業利益": "operating_profit",
    "経常利益": "ordinary_profit",
    "当期(四半期)純利益": "net_income",
    "当期(中間)純利益": "net_income",
    "当期(中間)利益": "net_income",
    "当期純利益": "net_income",
    "中間利益": "net_income",
    "四半期純利益": "net_income",
    "1株当たり当期(四半期)純利益": "eps",
    "1株当たり当期純利益": "eps",
    "1株当たり四半期純利益": "eps",
}

_DETAIL_METRICS = {
    "売上高": "sales",
    "売上収益": "sales",
    "売上原価": "cost_of_sales",
    "売上総利益": "gross_profit",
    "販売費及び一般管理費": "sga",
    "営業利益": "operating_profit",
    "営業外収益合計": "non_operating_income",
    "営業外費用合計": "non_operating_expenses",
    "金融収益": "non_operating_income",
    "金融費用": "non_operating_expenses",
    "経常利益": "ordinary_profit",
    "税引前四半期純利益": "profit_before_tax",
    "税引前当期純利益": "profit_before_tax",
    "税引前中間利益": "profit_before_tax",
    "税引前中間純利益": "profit_before_tax",
    "税引前当期利益": "profit_before_tax",
    "税金等調整前中間純利益": "profit_before_tax",
    "税金等調整前四半期純利益": "profit_before_tax",
    "税金等調整前当期純利益": "profit_before_tax",
    "法人税等": "income_taxes",
    "法人税等合計": "income_taxes",
    "法人税、住民税及び事業税": "income_taxes",
    "法人税、住民税及び事業税等": "income_taxes",
    "法人所得税費用": "income_taxes",
    "四半期純利益": "net_income",
    "中間純利益": "net_income",
    "中間利益": "net_income",
    "当期純利益": "net_income",
    # Consolidated statements can show both group-wide profit and the amount
    # attributable to owners.  The summary/forecast and Viewer net_income use
    # the latter; its row follows the group-wide row in the statement.
    "親会社株主に帰属する四半期純利益": "net_income",
    "親会社株主に帰属する中間純利益": "net_income",
    "親会社株主に帰属する当期純利益": "net_income",
    "親会社の所有者に帰属する四半期利益": "net_income",
    "親会社の所有者に帰属する中間利益": "net_income",
    "親会社の所有者に帰属する当期利益": "net_income",
}


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def _number(value: Any) -> float | None:
    text = _compact(value).replace(",", "").replace("△", "-").replace("▲", "-")
    eps = re.fullmatch(r"(-?\d+)円(\d{1,2})銭", text)
    if eps:
        whole = Decimal(eps.group(1))
        fractional = Decimal(eps.group(2)) / Decimal(100)
        return float(whole - fractional if whole < 0 else whole + fractional)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(Decimal(match.group(0)))
    except (InvalidOperation, ValueError):
        return None


def _period_end_from_header(header: str) -> str | None:
    match = re.search(r"(\d{4})年(\d{1,2})月期", _compact(header))
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def _quarter_from_header(header: str) -> str:
    compact = _compact(header)
    match = re.search(r"第([1-4])四半期", compact)
    if not match and "中間" in compact:
        return "2Q"
    return f"{match.group(1)}Q" if match else "FY"


def _metric_for_summary_label(label: str) -> str | None:
    normalized = _compact(label).replace("１", "1")
    if "1株当たり" in normalized and "利益" in normalized:
        return "eps"
    for candidate, metric in _SUMMARY_METRICS.items():
        if candidate in normalized:
            return metric
    return None


def _extract_summary_periods(pdf: Any) -> tuple[list[IpoFinancialPeriod], int]:
    for page_index, page in enumerate(pdf.pages):
        for table in page.extract_tables() or []:
            if not table or not table[0]:
                continue
            headers = [_compact(cell) for cell in table[0]]
            if not any("予想" in value or "予測" in value for value in headers):
                continue
            value_columns: list[tuple[int, str, str, str]] = []
            for column, header in enumerate(headers):
                period = _period_end_from_header(header)
                if not period:
                    continue
                if "予想" in header or "予測" in header:
                    value_columns.append((column, period, "FY", "forecast"))
                else:
                    # Listing forecasts often label the comparative column only
                    # by fiscal period (for example "2025年12月期") without the
                    # word "実績". A dated, non-forecast comparative column is an
                    # independently stated actual period, not an inferred value.
                    value_columns.append((column, period, _quarter_from_header(header), "actual"))

            if len(value_columns) < 2:
                continue

            page_text = _compact(page.extract_text() or "")
            unit = "千円" if "単位:千円" in page_text else "百万円"
            factor = Decimal("0.001") if unit == "千円" else Decimal("1")
            metrics_by_column: dict[int, dict[str, float]] = {
                column: {} for column, *_ in value_columns
            }
            for row in table[1:]:
                if not row:
                    continue
                metric = _metric_for_summary_label(row[0] if row else "")
                if not metric:
                    continue
                for column, *_ in value_columns:
                    if column >= len(row):
                        continue
                    value = _number(row[column])
                    if value is not None:
                        converted = Decimal(str(value)) if metric == "eps" else Decimal(str(value)) * factor
                        metrics_by_column[column][metric] = float(converted)

            periods = [
                IpoFinancialPeriod(
                    period=period,
                    quarter=quarter,
                    kind=kind,
                    metrics=metrics_by_column[column],
                    source_unit=unit,
                    source_page=page_index + 1,
                )
                for column, period, quarter, kind in value_columns
                if metrics_by_column[column]
            ]
            if (
                any(period.kind == "forecast" for period in periods)
                and any(period.kind == "actual" for period in periods)
            ):
                return periods, page_index + 1
    raise ValueError("IPO summary table with independently usable actual/forecast periods was not found")


def _lines_from_words(words: list[dict[str, Any]], tolerance: float = 2.0) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        if not lines or abs(float(word["top"]) - float(lines[-1][0]["top"])) > tolerance:
            lines.append([word])
        else:
            lines[-1].append(word)
    return lines


def _extract_detailed_cumulative(
    pdf: Any,
    *,
    expected_period: str | None = None,
    expected_quarter: str | None = None,
) -> IpoFinancialPeriod | None:
    for page_index, page in enumerate(pdf.pages):
        words = page.extract_words() or []
        page_text = "".join(_compact(word.get("text")) for word in words)
        is_cumulative_statement = (
            ("四半期" in page_text and "損益計算書" in page_text and "累計期間" in page_text)
            or ("中間" in page_text and "損益計算書" in page_text)
        )
        if not is_cumulative_statement:
            continue
        if "売上原価" not in page_text or "販売費及び一般管理費" not in page_text:
            continue

        period = _period_end_from_header(page_text) or expected_period
        quarter = _quarter_from_header(page_text)
        if quarter == "FY" and expected_quarter:
            quarter = expected_quarter
        date_matches = re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", page_text)
        period_start = period_end = None
        if len(date_matches) >= 2:
            # Comparative statements lay out prior/current dates side by side,
            # so word extraction returns both starts followed by both ends.
            start, end = (
                (date_matches[1], date_matches[3])
                if len(date_matches) == 4
                else (date_matches[-2], date_matches[-1])
            )
            period_start = f"{int(start[0]):04d}-{int(start[1]):02d}-{int(start[2]):02d}"
            period_end = f"{int(end[0]):04d}-{int(end[1]):02d}-{int(end[2]):02d}"
        if not period and period_end:
            # Fiscal end is normally present in the page heading.  Refuse to
            # invent it from the quarter end because the fiscal month matters.
            continue

        unit = "千円" if "単位:千円" in page_text or "単位：千円" in page_text else "百万円"
        factor = Decimal("0.001") if unit == "千円" else Decimal("1")
        metrics: dict[str, float] = {}
        for line in _lines_from_words(words):
            label_words = [word for word in line if float(word["x0"]) < 250]
            value_words = [word for word in line if float(word["x0"]) >= 250]
            label = "".join(_compact(word["text"]) for word in label_words)
            metric = _DETAIL_METRICS.get(label)
            if not metric or not value_words:
                continue
            value = _number(value_words[-1]["text"])
            if value is None:
                continue
            metrics[metric] = float(Decimal(str(value)) * factor)

        required = {"sales", "cost_of_sales", "gross_profit", "sga", "operating_profit", "net_income"}
        missing = required - set(metrics)
        if not ({"ordinary_profit", "profit_before_tax"} & set(metrics)):
            missing.add("ordinary_profit_or_profit_before_tax")
        if missing:
            logger.warning(
                "[IPO_FINANCIALS] detailed PL page=%s incomplete missing=%s",
                page_index + 1,
                sorted(missing),
            )
            continue
        return IpoFinancialPeriod(
            period=period or "",
            quarter=quarter,
            kind="actual",
            metrics=metrics,
            period_start=period_start,
            period_end=period_end,
            source_unit=unit,
            source_page=page_index + 1,
        )
    return None


def _extract_detailed_annual_actual(
    pdf: Any,
    *,
    expected_period: str,
) -> IpoFinancialPeriod | None:
    """Extract the current column of a comparative full-year income statement."""
    for page_index, page in enumerate(pdf.pages):
        words = page.extract_words() or []
        page_text = "".join(_compact(word.get("text")) for word in words)
        if "損益計算書" not in page_text or "売上原価" not in page_text:
            continue
        if "四半期損益計算書" in page_text or "中間損益計算書" in page_text:
            continue
        if "販売費及び一般管理費" not in page_text:
            continue

        period = _period_end_from_header(page_text) or expected_period
        if period != expected_period:
            continue
        date_matches = re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", page_text)
        period_start = period_end = None
        if len(date_matches) >= 2:
            start, end = (
                (date_matches[1], date_matches[3])
                if len(date_matches) == 4
                else (date_matches[-2], date_matches[-1])
            )
            period_start = f"{int(start[0]):04d}-{int(start[1]):02d}-{int(start[2]):02d}"
            period_end = f"{int(end[0]):04d}-{int(end[1]):02d}-{int(end[2]):02d}"

        unit = "千円" if "単位:千円" in page_text or "単位：千円" in page_text else "百万円"
        factor = Decimal("0.001") if unit == "千円" else Decimal("1")
        metrics: dict[str, float] = {}
        for line in _lines_from_words(words):
            label_words = [word for word in line if float(word["x0"]) < 250]
            value_words = [word for word in line if float(word["x0"]) >= 250]
            label = "".join(_compact(word["text"]) for word in label_words)
            metric = _DETAIL_METRICS.get(label)
            if not metric or not value_words:
                continue
            value = _number(value_words[-1]["text"])
            if value is not None:
                metrics[metric] = float(Decimal(str(value)) * factor)

        required = {
            "sales", "cost_of_sales", "gross_profit", "sga",
            "operating_profit", "ordinary_profit", "profit_before_tax",
            "income_taxes", "net_income",
        }
        if not required.issubset(metrics):
            logger.warning(
                "[IPO_FINANCIALS] detailed annual PL page=%s incomplete missing=%s",
                page_index + 1,
                sorted(required - set(metrics)),
            )
            continue
        return IpoFinancialPeriod(
            period=period,
            quarter="FY",
            kind="actual",
            metrics=metrics,
            period_start=period_start,
            period_end=period_end,
            source_unit=unit,
            source_page=page_index + 1,
        )
    return None


def _extract_detailed_annual_forecast(
    pdf: Any,
    *,
    expected_period: str,
) -> IpoFinancialPeriod | None:
    """Extract exact full-year forecast PL values stated in Japanese narrative."""
    labels = (
        ("販売費及び一般管理費", "sga"),
        ("売上総利益", "gross_profit"),
        ("売上原価", "cost_of_sales"),
    )
    metrics: dict[str, float] = {}
    source_page = 0
    for page_index, page in enumerate(pdf.pages):
        text = _compact(page.extract_text() or "")
        if "予想" not in text and "予測" not in text and "見込" not in text:
            continue
        if "損益計算書" in text:
            # A statement page can contain a forecast footnote while all table
            # values are actuals; never mine forecast detail from that page.
            continue
        for label, metric in labels:
            oku_match = re.search(
                rf"{re.escape(label)}[^。]{{0,100}}?(\d+)億(\d+)百万円",
                text,
            )
            million_match = re.search(
                rf"{re.escape(label)}[^。]{{0,100}}?([0-9][0-9,]*)百万円",
                text,
            )
            if oku_match:
                metrics.setdefault(
                    metric,
                    float(int(oku_match.group(1)) * 100 + int(oku_match.group(2))),
                )
            elif million_match:
                metrics.setdefault(
                    metric,
                    float(million_match.group(1).replace(",", "")),
                )
            else:
                continue
            source_page = source_page or page_index + 1
    detail_metrics = {"cost_of_sales", "gross_profit", "sga"}
    if not detail_metrics.issubset(metrics):
        return None
    return IpoFinancialPeriod(
        period=expected_period,
        quarter="FY",
        kind="forecast",
        metrics=metrics,
        source_unit="百万円",
        source_page=source_page,
    )


def _extract_stated_cumulative_eps(
    pdf: Any,
    *,
    expected_period: str,
    expected_quarter: str,
) -> tuple[float, int] | None:
    """Read an explicitly stated cumulative EPS from an attached summary table."""
    year, month = expected_period[:4], str(int(expected_period[5:7]))
    period_label = f"{year}年{month}月期"
    quarter_marker = "中間" if expected_quarter == "2Q" else f"第{expected_quarter[:1]}四半期"
    for page_index, page in enumerate(pdf.pages):
        for table in page.extract_tables() or []:
            if len(table) < 2 or not table[0]:
                continue
            headers = [_compact(cell).replace("１", "1") for cell in table[0]]
            eps_columns = [
                index for index, header in enumerate(headers)
                if "1株当たり" in header and "純利益" in header and "潜在" not in header
            ]
            if not eps_columns:
                continue
            for row in table[1:]:
                if not row:
                    continue
                label = _compact(row[0])
                if period_label not in label or quarter_marker not in label:
                    continue
                for column in eps_columns:
                    if column >= len(row):
                        continue
                    value = _number(row[column])
                    if value is not None:
                        return value, page_index + 1
    return None


def _extract_identity(pdf: Any) -> tuple[str, str]:
    text = pdf.pages[0].extract_text() or ""
    compact = _compact(text)
    code_match = re.search(r"コード(?:番号)?[:：]?([0-9]{3}[A-Za-z]|[0-9]{4})", compact)
    company_name = ""
    for line in text.splitlines():
        compact_line = _compact(line)
        if compact_line.startswith("会社名"):
            company_name = display_company_name(compact_line.removeprefix("会社名"))
            break
    return (
        normalize_ticker(code_match.group(1)) if code_match else "",
        company_name,
    )


def _validate_summary_detail(summary: IpoFinancialPeriod, detail: IpoFinancialPeriod) -> None:
    for metric in (
        "sales", "operating_profit", "ordinary_profit", "profit_before_tax", "net_income",
    ):
        summary_value = summary.metrics.get(metric)
        detail_value = detail.metrics.get(metric)
        if summary_value is None or detail_value is None:
            continue
        if int(Decimal(str(detail_value))) != int(Decimal(str(summary_value))):
            raise ValueError(
                f"summary/detail mismatch metric={metric} summary={summary_value} detail={detail_value}"
            )


def extract_ipo_listing_financials(pdf_path: str | Path) -> IpoListingExtraction:
    """Extract every independently supported actual/forecast period in an IPO notice."""
    import pdfplumber

    with pdfplumber.open(str(pdf_path)) as pdf:
        periods, summary_page = _extract_summary_periods(pdf)
        ticker, company_name = _extract_identity(pdf)
        actual_cumulative = next(
            (period for period in periods if period.kind == "actual" and period.quarter != "FY"),
            None,
        )
        detail = _extract_detailed_cumulative(
            pdf,
            expected_period=actual_cumulative.period if actual_cumulative else None,
            expected_quarter=actual_cumulative.quarter if actual_cumulative else None,
        )
        if detail is not None and "eps" not in detail.metrics:
            stated_eps = _extract_stated_cumulative_eps(
                pdf,
                expected_period=detail.period,
                expected_quarter=detail.quarter,
            )
            if stated_eps is not None:
                eps, _eps_page = stated_eps
                detail = IpoFinancialPeriod(
                    period=detail.period,
                    quarter=detail.quarter,
                    kind=detail.kind,
                    metrics={**detail.metrics, "eps": eps},
                    period_start=detail.period_start,
                    period_end=detail.period_end,
                    source_unit=detail.source_unit,
                    source_page=detail.source_page,
                )
        annual_actual = next(
            (period for period in periods if period.kind == "actual" and period.quarter == "FY"),
            None,
        )
        annual_forecast = next(
            (period for period in periods if period.kind == "forecast" and period.quarter == "FY"),
            None,
        )
        annual_actual_detail = (
            _extract_detailed_annual_actual(pdf, expected_period=annual_actual.period)
            if actual_cumulative is None and annual_actual is not None
            else None
        )
        annual_forecast_detail = (
            _extract_detailed_annual_forecast(pdf, expected_period=annual_forecast.period)
            if actual_cumulative is None and annual_forecast is not None
            else None
        )
    if detail is not None and actual_cumulative is not None:
        if detail.period != actual_cumulative.period or detail.quarter != actual_cumulative.quarter:
            raise ValueError(
                "detailed PL period does not match summary cumulative period: "
                f"summary={actual_cumulative.period}/{actual_cumulative.quarter} "
                f"detail={detail.period}/{detail.quarter}"
            )
        _validate_summary_detail(actual_cumulative, detail)
        merged = dict(actual_cumulative.metrics)
        merged.update(detail.metrics)
        replacement = IpoFinancialPeriod(
            period=detail.period,
            quarter=detail.quarter,
            kind="actual",
            metrics=merged,
            period_start=detail.period_start,
            period_end=detail.period_end,
            source_unit=detail.source_unit,
            source_page=detail.source_page,
        )
        periods = [replacement if period is actual_cumulative else period for period in periods]
    elif detail is not None:
        # Some listing notices put only prior-FY actual and current-FY forecast
        # in the cover table, while the attached interim statement independently
        # states the current cumulative period. Preserve that exact period too.
        periods.append(detail)

    for summary, annual_detail in (
        (annual_actual, annual_actual_detail),
        (annual_forecast, annual_forecast_detail),
    ):
        if summary is None or annual_detail is None:
            continue
        _validate_summary_detail(summary, annual_detail)
        merged = dict(summary.metrics)
        merged.update(annual_detail.metrics)
        replacement = IpoFinancialPeriod(
            period=summary.period,
            quarter="FY",
            kind=summary.kind,
            metrics=merged,
            period_start=annual_detail.period_start,
            period_end=annual_detail.period_end,
            source_unit=annual_detail.source_unit,
            source_page=annual_detail.source_page,
        )
        periods = [replacement if period is summary else period for period in periods]

    keys = {(period.period, period.quarter, period.kind) for period in periods}
    if len(keys) != len(periods):
        raise ValueError(f"duplicate IPO financial periods: {sorted(keys)}")
    if not any(period.kind == "actual" for period in periods):
        raise ValueError("IPO disclosure has no independently usable actual period")
    if not any(period.kind == "forecast" for period in periods):
        raise ValueError("IPO disclosure has no independently usable forecast period")

    pages = tuple(sorted({summary_page, *(period.source_page for period in periods)}))
    return IpoListingExtraction(
        ticker=ticker,
        company_name=company_name,
        periods=tuple(periods),
        source_pages=pages,
    )


def build_canonical_rows(
    extraction: IpoListingExtraction,
    *,
    ticker: str,
    filing_id: str,
    disclosed_at: str,
) -> list[dict[str, Any]]:
    """Build idempotent canonical rows using the existing long-table contract."""
    from lib.pipeline.canonical_writer import expand_financials_rows

    normalized_ticker = normalize_ticker(ticker or extraction.ticker)
    if not is_valid_ticker(normalized_ticker):
        raise ValueError(f"invalid ticker: {ticker!r}")
    rows: list[dict[str, Any]] = []
    for period in extraction.periods:
        source = "tdnet_forecast" if period.kind == "forecast" else "official_pdf"
        expanded, skipped = expand_financials_rows(
            ticker=normalized_ticker,
            period=period.period,
            quarter=period.quarter,
            metrics_dict=period.metrics,
            source=source,
            filing_id=filing_id,
            disclosure_datetime=disclosed_at,
            unit="millions_jpy",
        )
        if skipped:
            logger.info(
                "[IPO_FINANCIALS] canonical skipped ticker=%s period=%s quarter=%s count=%s",
                normalized_ticker, period.period, period.quarter, skipped,
            )
        for row in expanded:
            row["document_type"] = IPO_LISTING_FINANCIALS
            row["period_start"] = period.period_start
            row["period_end"] = period.period_end
        rows.extend(expanded)
    return rows


def write_ipo_financials(
    extraction: IpoListingExtraction,
    *,
    ticker: str,
    filing_id: str,
    disclosed_at: str,
    config: dict[str, Any] | None = None,
    upsert: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Upsert every extracted period at the disclosure identity level."""
    from lib.pipeline.db import supabase_upsert

    rows = build_canonical_rows(
        extraction, ticker=ticker, filing_id=filing_id, disclosed_at=disclosed_at,
    )
    writer = upsert or supabase_upsert
    result = writer(
        "canonical_financials",
        rows,
        on_conflict="source_row_key",
        config=config,
    )
    return {**result, "rows": len(rows), "periods": len(extraction.periods)}


def ensure_ipo_company(
    ticker: str,
    company_name: str,
    *,
    config: dict[str, Any] | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Add a listing-day issuer to Viewer search without changing existing rows."""
    import requests
    from lib.pipeline.db import get_supabase_write_config

    normalized = normalize_ticker(ticker)
    name = display_company_name(company_name)
    if not is_valid_ticker(normalized) or not name:
        raise ValueError("IPO company identity is incomplete")
    cfg = config or get_supabase_write_config()
    if not cfg:
        raise RuntimeError("SUPABASE service-role write configuration is missing")
    client = session or requests
    endpoint = f"{cfg['rest_url']}/companies"
    existing = client.get(
        endpoint, params={"select": "ticker_code", "ticker_code": f"eq.{normalized}"},
        headers=cfg["headers"], timeout=30,
    )
    existing.raise_for_status()
    if existing.json():
        return {"action": "existing", "ticker": normalized}
    # An on-conflict no-op also preserves a row inserted by a concurrent sync.
    created = client.post(
        endpoint, params={"on_conflict": "ticker_code"},
        json={"ticker_code": normalized, "name_ja": name, "is_active": True},
        headers={**cfg["headers"], "Prefer": "resolution=ignore-duplicates,return=representation"},
        timeout=30,
    )
    created.raise_for_status()
    return {"action": "inserted" if created.json() else "existing", "ticker": normalized}


def build_ipo_notification_event(item: Any):
    """Create the existing EventRecord shape without depending on PL parsing."""
    from src.events.common_models import EventRecord, EventType

    ticker = normalize_ticker(getattr(item, "ticker", ""))
    company_name = display_company_name(getattr(item, "company_name", ""))
    source_url = str(getattr(item, "doc_url", "") or "")
    source_doc_id = disclosure_identity(source_url, getattr(item, "source_doc_id", None))
    raw_payload = {
        "source_title": getattr(item, "title", ""),
        "source_doc_id": source_doc_id,
        "document_type": IPO_LISTING_FINANCIALS,
    }
    return EventRecord(
        source_doc_id=source_doc_id,
        ticker=ticker,
        company_name=company_name,
        disclosure_datetime=str(getattr(item, "published_at", "") or ""),
        title=f"新規上場 {ticker} {company_name}".strip(),
        doc_url=source_url,
        event_type=EventType.CAPITAL_ACTION,
        subtype="announced",
        importance=85,
        summary_text="",
        raw_payload_json=json.dumps(raw_payload, ensure_ascii=False),
        extracted_payload_json="{}",
    )


def save_ipo_notification(item: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Idempotently save the listing card; intentionally independent of parsing."""
    event = build_ipo_notification_event(item)
    if dry_run:
        return {"action": "dry_run", "display_title": event.title, "source_url": event.doc_url}
    from src.events.tdnet_event_store import save_event_to_supabase

    return save_event_to_supabase(event)
