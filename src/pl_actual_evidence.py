"""Evidence for statement actuals; never infer a closing day from a month label.

This module has no network, database or model/API calls. Forecast extraction
has a separate entry point and must not use the actual-result gate below.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


def normalized(text: str) -> str:
    text = unicodedata.normalize('NFKC', text or '')
    for era, offset in (('令和', 2018), ('平成', 1988)):
        text = re.sub(era + r'(元|\d+)年',
                      lambda m: str(offset + (1 if m[1] == '元' else int(m[1]))) + '年', text)
    return text


def is_actual_statement(title: str) -> bool:
    title = normalized(title).lower()
    return ('決算短信' in title or 'financial results' in title) and not re.search(
        r'質疑|q\s*&\s*a|説明会|説明資料|補足資料|presentation|スライド', title)


@dataclass(frozen=True)
class ActualPeriod:
    fiscal_end: str
    quarter: str
    start: str
    end: str


_DATE = r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日'
_RANGE = re.compile(_DATE + r'\s*[~～〜－—–-]\s*' + _DATE)


def actual_period(title: str, text: str) -> ActualPeriod | None:
    """Resolve explicit current ranges, with a checked regular-year fallback.

    A forecast's full-year *date range* can identify the fiscal closing day,
    but forecast amounts and quarter labels are never treated as actuals.
    Short fiscal years need an explicit full-year range. A single quarter's
    standalone duration must not be relabelled as cumulative or annual.
    """
    title, text = normalized(title), normalized(text)
    compact = re.sub(r'\s+', '', text)
    ym = re.search(r'(20\d{2})年(\d{1,2})月期', re.sub(r'\s+', '', title))
    if not ym:
        ym = re.search(r'(20\d{2})年(\d{1,2})月期', compact)
    if not ym:
        return None
    expected = (int(ym[1]), int(ym[2]))
    heading = title if '決算短信' in title else compact[ym.start():ym.start()+100]
    qm = re.search(r'第\s*([1-4])\s*四半期', heading)
    q = qm[1] + 'Q' if qm else ('2Q' if '中間' in heading else 'FY')
    if q == '4Q' or ('会計期間' in heading and '累計' not in heading):
        return None
    stop = re.search(r'(?:連結)?経営成績', compact)
    if not stop:
        return None
    ranges = []
    for m in _RANGE.finditer(compact):
        try:
            start = date(*map(int, m.groups()[:3])); end = date(*map(int, m.groups()[3:]))
        except ValueError:
            continue
        if start <= end:
            ranges.append((m.start(), start, end))
    current = [r for r in ranges if r[0] < stop.start()]
    if not current:
        return None
    _, start, end = current[-1]
    if q == 'FY':
        return ActualPeriod(end.isoformat(), 'FY', start.isoformat(), end.isoformat()) if (end.year, end.month) == expected else None
    days = (end - start).days + 1
    lo, hi = {'1Q': (75,105), '2Q': (165,195), '3Q': (255,285)}[q]
    # A disclosed short/changed fiscal year is allowed only with explicit dates;
    # never synthesize its end by adding twelve months.
    candidates = {e for _, s, e in ranges if s == start and (e.year,e.month) == expected and e >= end}
    if len(candidates) == 1:
        fiscal_end = candidates.pop()
    elif not candidates and lo <= days <= hi and not re.search(r'決算期.{0,12}変更|変則決算', compact):
        try:
            fiscal_end = start.replace(year=start.year+1) - timedelta(days=1)
        except ValueError:
            return None
        if (fiscal_end.year,fiscal_end.month) != expected:
            return None
    else:
        return None
    if not lo <= days <= hi and not re.search(r'決算期.{0,12}変更|変則決算', compact):
        return None
    return ActualPeriod(fiscal_end.isoformat(), q, start.isoformat(), end.isoformat())


_TOKEN = re.compile(r'^(?:[△▲−－-]?\d[\d,]*(?:\.\d+)?|[―—–－─-])$')


def money(token: str) -> Decimal | None:
    """Parse the entire cell; never delete a decimal/percentage substring."""
    token = normalized(token).strip()
    if not _TOKEN.fullmatch(token):
        raise ValueError('not a complete monetary cell')
    if token in ('―','—','–','－','─','-'):
        return None
    token = token.translate(str.maketrans({'△':'-','▲':'-','−':'-','－':'-'}))
    if ',' in token and not re.fullmatch(r'-?\d{1,3}(?:,\d{3})+(?:\.\d+)?', token):
        raise ValueError('broken thousands grouping')
    return Decimal(token.replace(',', ''))


def summary_actuals(text: str, fiscal_end: str, quarter: str) -> tuple[dict, str]:
    """Read one labelled current row in the first actual results table.

    Require explicit units, complete cells, distinct metric headings, the
    requested fiscal year and quarter, and the usual amount/YoY column pairs.
    Ambiguous layouts return empty, never a nearby year, profit or fragment.
    """
    era_year = '令和' in text or '平成' in text
    text = normalized(text)
    begin = re.search(r'(?:連結)?経営成績', text)
    if not begin:
        return {}, ''
    block = text[begin.end():]
    end = re.search(r'(?:連結)?財政状態|包括利益|1\s*株当たり|業績予想', block)
    if end:
        block = block[:end.start()]
    year, month = map(int, fiscal_end.split('-')[:2])
    year_pattern = '(?:'+str(year)+'|'+str(year-2018)+')' if era_year else str(year)
    rowlabel = re.compile(r'^\s*'+year_pattern+r'年\s*0?'+str(month)+r'月期(?P<q>第\s*[1-4]\s*四半期(?:\s*\(?累計\)?)?|中間期)?\s+(?P<values>.+)$')
    lines = block.splitlines()
    matches=[]
    for i,line in enumerate(lines):
        m = rowlabel.match(line)
        if not m:
            continue
        label = re.sub(r'\s+', '', m['q'] or '')
        rowq = '2Q' if '中間' in label else (re.search(r'第([1-4])',label)[1]+'Q' if '第' in label else 'FY')
        if rowq != quarter:
            continue
        header = '\n'.join(lines[:i])
        if '営業利益' not in header and '営業損失' not in header:
            continue
        if not re.search(r'売上高|売上収益|営業収益|営業収入|経常収益',header):
            continue
        # IFRS summaries with both total and attributable profit need their
        # structured XBRL/table route; do not assume their column count.
        sales_headers = re.findall(r'売上高|売上収益|営業収益|営業収入|経常収益', header)
        if len(sales_headers)>2 or len(sales_headers)!=len(set(sales_headers)):
            continue
        preferred = '売上高' if '売上高' in sales_headers else sales_headers[0]
        metrics=['sales' if name==preferred else 'operating_revenue' for name in sales_headers]+['operating_profit']
        if re.search(r'経常利益|経常損失',header):metrics.append('ordinary_profit')
        elif '税引前' in header:metrics.append('profit_before_tax')
        if re.search(r'純利益|純損失|帰属',header):metrics.append('net_income')
        units=re.findall(r'百万円|千円|(?<![千万円億])円(?!銭)',header)
        if not units or len(set(units)) != 1:
            continue
        tokens=m['values'].split()
        if len(tokens) != 2*len(metrics):
            continue
        try:
            values={k:money(tokens[2*j]) for j,k in enumerate(metrics)}
            for tok in tokens[1::2]:money(tok.rstrip('%'))
        except (ValueError,InvalidOperation):
            continue
        if values.get('sales') is None or values.get('operating_profit') is None:
            continue
        matches.append((values,units[0]))
    return matches[0] if len(matches)==1 else ({},'')


def reconcile_summary(result, values: dict, unit: str) -> bool:
    """Compare validated cells in yen, including published rounding precision."""
    factors={'円':1,'千円':1000,'百万円':1000000,'億円':100000000}
    source=factors.get(result.source_unit); target=factors.get(unit)
    if not source or not target or not values:
        return False
    for field in ('sales','operating_profit'):
        a=getattr(result,field,None); b=values.get(field)
        if a is None or b is None or abs(Decimal(str(a))*source-b*target)>=max(source,target):
            return False
    return True
