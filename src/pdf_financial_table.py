"""Conservative extraction of the first-page financial results table."""
from __future__ import annotations

import re

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
