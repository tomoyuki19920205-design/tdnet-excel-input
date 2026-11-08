"""Bind PDF segment amounts to their own page's printed period and unit."""
import re,calendar,datetime,unicodedata
from decimal import Decimal
from dataclasses import asdict,is_dataclass
from src.segment.xhtml_segment_tables import _RANGE,_date
from src.segment.semantic_facts import compact,sales_basis,column_role
from src.segment.profit_basis import profit_basis

_HEADING=re.compile(r'(前|当)(?:第[123一二三])?(?:四半期連結累計期間|四半期累計期間|中間連結会計期間|中間会計期間|連結会計年度|事業年度|連結累計期間|四半期連結会計期間)')

def pdf_period_section(page_text,period_type):
    """Keep notes within the identified current/prior section on this page."""
    text=compact(page_text);headings=list(_HEADING.finditer(text));sections=[]
    for i,h in enumerate(headings):
        role='current' if h.group(1)=='当' else 'previous'
        section=text[h.start():headings[i+1].start() if i+1<len(headings) else len(text)]
        match=_RANGE.search(section[:160])
        if role==period_type and match:
            try:start,end=_date(match.groups()[:5]),_date(match.groups()[5:])
            except (ValueError,TypeError):continue
            if start<=end:sections.append(section)
    return sections[0] if len(sections)==1 else None

def bind_pdf_period_evidence(page_text,period_type):
    if period_type not in ('current','previous'):return None
    text=compact(page_text);matches=[]
    for m in _RANGE.finditer(text):
        headings=list(_HEADING.finditer(text[max(0,m.start()-100):m.start()]))
        if not headings:continue
        heading=headings[-1];role='current' if heading.group(1)=='当' else 'previous'
        if role!=period_type:continue
        try:start=_date(m.groups()[:5]);end=_date(m.groups()[5:])
        except (ValueError,TypeError):continue
        if start>end:continue
        scope='連結' in heading.group(0)
        matches.append((start,end,scope,heading.group(0),m.group(0)))
    identities={(m[0],m[1],m[2]) for m in matches}
    if len(identities)!=1:return None
    units=set(re.findall(r'単位[:：]?(百万円|千円|円)',text))
    if len(units)!=1:return None
    start,end,scope,heading,printed=matches[0];unit=next(iter(units))
    return {'period_start':start,'period_end':end,'period_type':'ytd','source_period_role':period_type,'is_consolidated':scope,'currency':'JPY','display_unit':unit,'unit_multiplier':{'百万円':1000000,'千円':1000,'円':1}[unit],'period_heading':heading,'printed_period':printed,'evidence_rule':'same_pdf_page_explicit_period_and_unit'}

def pdf_fiscal_period(base_period,quarter,period_type,printed_end=None):
    """Canonical period remains the fiscal year-end, including quarterly PDFs."""
    if period_type not in ('current','previous'):return None
    if quarter not in ('FY','1Q','2Q','3Q'):return None
    try:
        y,m,d=map(int,base_period.split('-'))
        if period_type=='previous':y-=1;d=min(d,calendar.monthrange(y,m)[1])
        fiscal=datetime.date(y,m,d).isoformat()
        from src.segment.xbrl_segment_extractor import _calculate_expected_context_end
        expected=_calculate_expected_context_end(fiscal,quarter)
        if expected and d==calendar.monthrange(y,m)[1]:expected=expected.replace(day=calendar.monthrange(expected.year,expected.month)[1])
        if printed_end and (expected is None or str(expected)!=printed_end):return None
        return fiscal
    except (ValueError,TypeError):return None

def pdf_measure_metadata(proof,sales_label,profit_label,page_number):
    return {**proof,'sales_basis':sales_basis(sales_label,'',[]),'sales_label':sales_label,'profit_basis':profit_basis(profit_label) or 'company_defined_unknown','profit_label':profit_label,'source_page':page_number,'extraction_method':'pdf_table'}

def verified_pdf_records(result,base_period,quarter,*,current_only=False):
    """Convert once to million JPY only after each table's identity is bound."""
    rows=[]
    for period in getattr(result,'extracted_periods',[]) or []:
        role=period.period_type
        if current_only and role!='current':continue
        for segment in period.segments:
            record=asdict(segment) if is_dataclass(segment) else dict(segment)
            flags=record.get('segment_metadata') or {}
            if column_role((flags.get('source_table_cells') or {}).get('header',''))!='segment':continue
            if flags.get('evidence_rule')!='same_pdf_page_explicit_period_and_unit':continue
            normalized_quarter=quarter
            if quarter in ('',None):
                # A missing legacy quarter is annual only when the original
                # table explicitly says so. Quarterly headings remain unresolved.
                if flags.get('period_heading') not in ('当連結会計年度','前連結会計年度','当事業年度','前事業年度'):continue
                normalized_quarter='FY'
            fiscal=pdf_fiscal_period(base_period,normalized_quarter,role,flags.get('period_end'))
            if not fiscal or not flags.get('source_table_cells'):continue
            y,m,d=map(int,fiscal.split('-'))
            previous_end=datetime.date(y-1,m,min(d,calendar.monthrange(y-1,m)[1]))
            expected_start=(previous_end+datetime.timedelta(days=1)).isoformat()
            if flags.get('period_start')!=expected_start:continue
            scale=flags.get('unit_multiplier')
            if scale not in (1,1000,1000000):continue
            for metric in ('segment_sales','segment_profit'):
                if record.get(metric) is not None:
                    raw=(flags['source_table_cells'].get(metric.replace('segment_','')+'_raw_cell') or '')
                    normalized=compact(str(raw)).replace(',','')
                    negative=normalized.startswith(('△','▲','−','-')) or (normalized.startswith('(') and normalized.endswith(')'))
                    normalized=normalized.lstrip('△▲−-+').strip('()')
                    try:amount=Decimal(normalized)*(-1 if negative else 1)
                    except Exception:amount=None
                    if amount is None or amount!=Decimal(str(record[metric])):
                        record[metric]=None
                        record['segment_metadata'][metric.replace('segment_','')+'_evidence_status']='ambiguous_pdf_numeric_cell'
                    else:record[metric]=float(amount*Decimal(scale)/Decimal(1000000))
            if record.get('segment_sales') is None and record.get('segment_profit') is None:continue
            record.update(period=fiscal,quarter=normalized_quarter,unit_raw='百万円',unit_multiplier=1000000)
            record['segment_metadata']={**flags,'normalized_unit':'millions_jpy','normalization_count':1}
            if normalized_quarter!=quarter:
                record['segment_metadata']['quarter_identity_evidence']='original_annual_heading_and_exact_full_fiscal_duration'
            rows.append(record)
    return rows
