"""Conservative extraction of the first-page financial results table."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from .utils import normalize_number

_MONEY_TOKEN_RE = re.compile(r"(?:△|▲|-|－)?[0-9０-９][0-9０-９,，]*")

_HEADER_PATTERNS = {
    "sales": re.compile(r"売上高|売上収益|営業収益|経常収益"),
    "operating_profit": re.compile(r"営業利益|営業損失"),
    "ordinary_profit": re.compile(r"経常利益|経常損失"),
    "net_income": re.compile(
        r"親会社株主.*(?:純利益|利益)|当期純利益|四半期純利益|中間純利益|純利益"
    ),
}


def _money_value(cell) -> int | None:
    text = str(cell or "").replace("，", ",")
    for token in _MONEY_TOKEN_RE.findall(text):
        value = normalize_number(token)
        if value is not None:
            return value
    return None


def extract_actual_financial_table(tables: list) -> dict[str, int]:
    """Extract current-period PL values from a compact summary table.

    TDnet summary PDFs often merge current/prior values in one cell, e.g.
    ``百万円\n362\n348``.  The first monetary token is the current period.
    Percentage columns have no metric header and are deliberately ignored.
    """
    for table in tables or []:
        if not table:
            continue
        for header_index, header_row in enumerate(table[:3]):
            column_for: dict[str, int] = {}
            for column_index, cell in enumerate(header_row or []):
                header = re.sub(r"\s+", "", str(cell or ""))
                for metric, pattern in _HEADER_PATTERNS.items():
                    if metric not in column_for and pattern.search(header):
                        column_for[metric] = column_index

            if "sales" not in column_for or len(column_for) < 2:
                continue

            for data_row in table[header_index + 1:]:
                values: dict[str, int] = {}
                for metric, column_index in column_for.items():
                    if column_index >= len(data_row or []):
                        continue
                    value = _money_value(data_row[column_index])
                    if value is not None:
                        values[metric] = value
                if "sales" in values and len(values) >= 2:
                    return values
    return {}


_STATEMENT_ROWS = {
    "sales": re.compile(r"^(?:売上高|売上収益|営業収益)$"),
    "gross_profit": re.compile(r"^(?:売上総利益|売上総損失|売上総損益)$"),
    "selling_general_and_administrative_expenses": re.compile(r"^販売費及び一般管理費$"),
    "operating_profit": re.compile(r"^(?:営業利益|営業損失)$"),
}


def extract_actual_statement_table(tables: list, page_text: str, title: str) -> tuple[dict[str, int], str]:
    """Read the current column of an identified PL statement, never a summary/prior column."""
    compact = re.sub(r"\s+", "", page_text or "")
    if "損益計算書" not in compact or "前" not in compact or "当" not in compact:
        return {}, ""
    if "非連結" in title and re.search(r"(?<!非)連結損益計算書", compact):
        return {}, ""
    if "非連結" not in title and "非連結損益計算書" in compact:
        return {}, ""
    unit_match = re.search(r"単位[：:]?(千円|百万円)", compact)
    if not unit_match:
        return {}, ""
    for table in tables or []:
        values: dict[str, int] = {}
        for row in table or []:
            cells = [str(cell or "") for cell in row or []]
            if len(cells) < 3:
                continue
            label = re.sub(r"\s+", "", "".join(cells[:-2]))
            for metric, pattern in _STATEMENT_ROWS.items():
                if pattern.fullmatch(label):
                    prior, current = _money_value(cells[-2]), _money_value(cells[-1])
                    if prior is not None and current is not None:
                        values[metric] = current
        if {"sales", "gross_profit", "operating_profit"}.issubset(values):
            gp = values["gross_profit"]
            op = values["operating_profit"]
            sga = values.get("selling_general_and_administrative_expenses")
            if sga is not None and abs(gp - sga - op) > 1:
                continue
            return values, unit_match.group(1)
    return {}, ""


def extract_actual_statement_pdf(pdf_path: str, title: str) -> tuple[dict[str, int], str]:
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "損益計算書" not in text:
                continue
            values, unit = extract_actual_statement_table(page.extract_tables() or [], text, title)
            if values:
                return values, unit
    return {}, ""


def extract_fy_actual_period_pdf(pdf_path: str) -> str | None:
    """Return the FY end only when the PDF states the full actual date range."""
    import pdfplumber

    matches = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:3]:
            text = unicodedata.normalize("NFKC", page.extract_text() or "")
            for parts in re.findall(
                r"(\d{4})年\s*(\d{1,2})月期の(?:連結|個別)?業績\s*[（(]\s*"
                r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*[~〜～-]\s*"
                r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*[）)]",
                text,
            ):
                fy, month, sy, sm, sd, ey, em, ed = map(int, parts)
                try:
                    start, end = date(sy, sm, sd), date(ey, em, ed)
                except ValueError:
                    continue
                if (fy, month) == (end.year, end.month) and 330 <= (end - start).days <= 400:
                    matches.append(end.isoformat())
    return matches[0] if len(set(matches)) == 1 and matches else None
