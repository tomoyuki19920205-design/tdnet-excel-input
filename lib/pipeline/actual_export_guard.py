"""Read-only evidence check before old local actuals are exported again."""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse, unquote

from src.pl_actual_evidence import actual_period, is_actual_statement, normalized, summary_actuals, read_pdf_actual_evidence
from src.common_ticker import normalize_ticker

log = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def _read_evidence(filename: str, modified_ns: int, size: int):
    import pdfplumber
    with pdfplumber.open(filename) as pdf:
        text = pdf.pages[0].dedupe_chars().extract_text() or ''
    lines=text.splitlines()
    cover_title='\n'.join(lines[:3])
    title=next((s for s in lines[:40] if '決算短信' in normalized(s) and
                re.match(r'^\s*[（(「【]?(?:訂正|再訂正|20\d{2}年|令和|平成)',normalized(s))),cover_title)
    if re.search(r'質疑|Q\s*&\s*A|説明会|説明資料',normalized(cover_title),re.I):title=cover_title
    compact=re.sub(r'\s+', '', normalized(text))
    issuer=re.search(r'(?:コード番号|証券コード|コード)[:：)）]*([0-9A-Z]{4})', compact)
    period,actual_text,_=read_pdf_actual_evidence(filename,title)
    values,unit=summary_actuals(actual_text,period.fiscal_end,period.quarter) if period else ({},'')
    return title, issuer[1] if issuer else None, period, values,unit


def assess_local_actual(row: dict, docs_dir: str | Path) -> tuple[bool,str]:
    """Reject contradictions; retain unverified legacy rows without guessing.

    Only retained official PDFs are read. There is no network fetch or local
    database mutation; rejected source rows remain available for recovery.
    """
    source = str(row.get('source_url') or '')
    parsed = urlparse(source)
    # Old downloader rows retain the redirect URL, whose query contains the
    # original official URL. Resolve the reference locally, without HTTP.
    if parsed.hostname == 'webapi.yanoshin.jp' and parsed.path == '/rd.php':
        source = unquote(parsed.query)
        parsed = urlparse(source)
    name = Path(parsed.path).name
    if not re.fullmatch(r'\d{18}\.pdf',name):
        return True,'unverified_no_official_pdf_reference'
    path = Path(docs_dir)/name
    if not path.is_file():
        return True,'unverified_original_not_retained'
    try:
        stat=path.stat()
        title,issuer,period,values,unit=_read_evidence(str(path),stat.st_mtime_ns,stat.st_size)
    except Exception:
        return False,'original_unreadable'
    if not is_actual_statement(title):
        return False,'not_actual_statement'
    heading = normalized(title)
    if re.search(r'第\s*4\s*四半期',heading) or ('会計期間' in heading and '累計' not in heading):
        return False,'standalone_quarter_not_cumulative'
    if issuer and issuer != normalize_ticker(str(row.get('company_code') or row.get('ticker') or '')):
        return False,'issuer_conflict'
    if period:
        q=row.get('quarter')
        if (row.get('fiscal_year_end') or row.get('period')) != period.fiscal_end:
            return False,'fiscal_end_conflict'
        if q != period.quarter and not(q=='4Q' and period.quarter=='FY'):
            return False,'quarter_conflict'
    factors={'円':Decimal('0.000001'),'千円':Decimal('.001'),'百万円':Decimal(1),
             'million_yen':Decimal(1),'millions_jpy':Decimal(1),'JPY':Decimal('0.000001')}
    factor=factors.get(unit); stored_factor=factors.get(row.get('unit') or '百万円')
    if values and factor and stored_factor:
        for key in ('sales','operating_profit'):
            if row.get(key) is not None and values.get(key) is not None:
                if abs(Decimal(str(row[key]))*stored_factor-values[key]*factor)>=max(factor,stored_factor):
                    return False,'actual_amount_conflict_'+key
    return True,'verified' if issuer and period and values else 'unverified_incomplete_evidence'


def filter_local_actuals(rows: list[dict], db_path: str) -> list[dict]:
    docs=Path(db_path).resolve().parent/'data'/'docs'
    accepted=[]
    for row in rows:
        ok,reason=assess_local_actual(row,docs)
        if ok:
            accepted.append(row)
        else:
            log.warning('PL_EXPORT_QUARANTINE local_id=%s issuer=%s period=%s quarter=%s reason=%s original_retained=true',
                        row.get('id'),row.get('company_code'),row.get('fiscal_year_end'),row.get('quarter'),reason)
    return accepted
