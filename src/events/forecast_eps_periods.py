"""Extract EPS revisions from each labelled forecast table independently."""
from __future__ import annotations

import calendar
from datetime import date
import re
import unicodedata


_HEADING = re.compile(r"(?m)^\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*期([^\n]*)")
_DATE_RANGE = re.compile(
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s*[～〜~\-]\s*"
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日"
)
_PREVIOUS = re.compile(r"^\s*(?:前回(?:発表|公表|修正)?予想|修正前|従来予想|当初予想)")
_REVISED = re.compile(r"^\s*(?:今回(?:修正|発表|公表)?予想|修正後|新予想)")
_NUMBER = re.compile(r"(?<![\d.])[-−]?\d[\d,]*(?:\.\d+)?(?![\d.])")


def _period(heading: str, fiscal_year: int | None, fiscal_month: int | None) -> str:
    compact = re.sub(r"\s+", "", heading)
    full_year = "通期" in compact
    cumulative = any(term in compact for term in ("中間期", "上期", "上半期", "第2四半期累計"))
    standalone = ("第2四半期" in compact or "2Q" in compact) and ("単独" in compact or "会計期間" in compact)
    explicit = [name for name, present in (("FULL_YEAR", full_year), ("2Q_CUMULATIVE", cumulative), ("2Q_STANDALONE", standalone)) if present]
    if len(explicit) > 1:
        return "UNKNOWN"
    match = _DATE_RANGE.search(compact)
    if match:
        try:
            y1, m1, d1, y2, m2, d2 = map(int, match.groups())
            days = (date(y2, m2, d2) - date(y1, m1, d1)).days + 1
            dated = "UNKNOWN"
            if fiscal_year and fiscal_month:
                fy_end = date(fiscal_year, fiscal_month, calendar.monthrange(fiscal_year, fiscal_month)[1])
                if date(y2, m2, d2) == fy_end and 340 <= days <= 380:
                    dated = "FULL_YEAR"
            if 165 <= days <= 200:
                dated = "2Q_CUMULATIVE"
            if 80 <= days <= 100:
                dated = "2Q_STANDALONE"
            if dated != "UNKNOWN" and explicit and dated != explicit[0]:
                return "UNKNOWN"
            return dated if dated != "UNKNOWN" else (explicit[0] if explicit else "UNKNOWN")
        except ValueError:
            return "UNKNOWN"
    return explicit[0] if explicit else "UNKNOWN"


def _row_values(lines: list[str], start: int) -> list[float]:
    """Read numeric cells of a labelled forecast row, never a reference row."""
    row = lines[start]
    # Some PDF text orders put the label and the numeric cells on adjacent lines.
    if not _NUMBER.search(row) and start + 1 < len(lines):
        row += " " + lines[start + 1]
    values = _NUMBER.findall(row)
    try:
        return [float(value.replace(",", "").replace("−", "-")) for value in values]
    except ValueError:
        return []


def forecast_eps_revisions(text: str) -> list[dict]:
    """Return one EPS pair per table, with its own fiscal year, period and basis.

    A heading without an identifiable duration remains UNKNOWN. A previous-year
    actual is never paired with a forecast row. We require an EPS header in each
    table and both labelled forecast rows before returning an item.
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    headings = list(_HEADING.finditer(normalized))
    sections: list[tuple[int | None, int | None, str, str]] = []
    if headings:
        for index, match in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(normalized)
            sections.append((int(match[1]), int(match[2]), match[3], normalized[match.end():end]))
    else:
        sections.append((None, None, "", normalized))

    revisions: list[dict] = []
    for year, month, heading, body in sections:
        # Stop at prose/dividend; only the local forecast table is relevant.
        body = re.split(r"(?m)^\s*(?:修正の理由|配当予想|配当金予想)", body, maxsplit=1)[0]
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        previous = next((i for i, line in enumerate(lines) if _PREVIOUS.match(line)), None)
        revised = next((i for i, line in enumerate(lines) if _REVISED.match(line)), None)
        if previous is None or revised is None or revised <= previous or revised - previous > 5:
            continue
        header = "".join(lines[max(0, previous - 8):previous]).replace(" ", "")
        has_eps_header = (
            "EPS" in header.upper()
            or ("1株当たり" in header or "1株当り" in header)
            and "純利益" in header
        ) and "1株当たり配当" not in header
        if not has_eps_header:
            continue
        # The EPS cell must occupy the same final column in both rows. A short
        # row means that cell is missing; never shift net income into its place.
        other_header = re.sub(r"1株当たり(?:当期|中間|四半期)?純利益|1株当り(?:当期|中間|四半期)?純利益|EPS", "", header, flags=re.I)
        expected_columns = 1 + sum(
            marker in other_header
            for marker in ("売上高", "営業利益", "経常利益", "純利益")
        )
        prev_values = _row_values(lines, previous)
        revised_values = _row_values(lines, revised)
        if len(prev_values) != len(revised_values) or len(prev_values) < expected_columns:
            continue
        prev_eps, revised_eps = prev_values[-1], revised_values[-1]
        if abs(prev_eps) > 10000 or abs(revised_eps) > 10000 or prev_eps == revised_eps:
            continue
        fiscal = f"{year}年{month}月期" if year and month else None
        context = heading + " " + " ".join(lines[:previous])
        basis = "連結" if "連結" in context else "個別" if "個別" in context else None
        item = {
            "fiscal_period": fiscal,
            "period": _period(heading, year, month),
            "basis": basis,
            "previous_eps": prev_eps,
            "revised_eps": revised_eps,
        }
        if item not in revisions:
            revisions.append(item)
    return revisions
