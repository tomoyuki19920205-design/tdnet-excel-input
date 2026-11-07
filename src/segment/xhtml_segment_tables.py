"""Literal monetary cells in period-bound segment TextBlock XHTML tables.

No metric names, dimensions or XBRL facts are invented. Each output carries
the real table/row/column coordinates and the exact displayed cell string.
"""
import re
import datetime
import unicodedata
from decimal import Decimal, InvalidOperation
from bs4 import BeautifulSoup, NavigableString
from src.segment.semantic_facts import compact, column_role, metric_from_label, sales_basis
from src.segment.profit_basis import profit_basis

_DATE = r'(?:(令和|平成)(元|\d{1,2})|(\d{4}))年(\d{1,2})月(\d{1,2})日'
_RANGE = re.compile(r'自' + _DATE + r'至' + _DATE)
_NIL = {'', '-', '―', '—', '–', '…', '該当なし'}


def _date(groups):
    era,ey,year,month,day=groups
    y=int(year) if year else (2018 if era=='令和' else 1988)+(1 if ey=='元' else int(ey))
    return datetime.date(y,int(month),int(day)).isoformat()


def _period_ranges(text):
    result=[]
    for m in _RANGE.finditer(compact(text)):
        try:result.append((_date(m.groups()[:5]),_date(m.groups()[5:]),m.group(0)))
        except (ValueError,TypeError):continue
    return result


def _number(text):
    t=compact(text).replace(',','').replace('−','-')
    if t in _NIL:return None
    if not re.fullmatch(r'[△▲+\-]?\(?\d+(?:\.\d+)?\)?',t):return None
    negative=t.startswith(('△','▲','-','('))
    try:return abs(Decimal(t.lstrip('△▲+-').strip('()')))*(-1 if negative else 1)
    except InvalidOperation:return None


def _grid(table):
    occupied={};rows=[]
    for ri,tr in enumerate(t for t in table.find_all('tr') if t.find_parent('table') is table):
        ci=0
        for cell in tr.find_all(['td','th'],recursive=False):
            while (ri,ci) in occupied:ci+=1
            try:rs=max(1,int(cell.get('rowspan') or 1));cs=max(1,int(cell.get('colspan') or 1))
            except ValueError:return []
            if rs>100 or cs>100:return []
            value={'text':cell.get_text(' ',strip=True),'row':ri,'column':ci,'colspan':cs,'node':cell}
            for rr in range(ri,ri+rs):
                for cc in range(ci,ci+cs):occupied[rr,cc]=value
            ci+=cs
        rows.append([occupied.get((ri,c)) for c in range(max((c for rr,c in occupied if rr==ri),default=-1)+1)])
    return rows


def _prefix(soup,table):
    chunks=[]
    for element in soup.descendants:
        if element is table:break
        if isinstance(element,NavigableString):chunks.append(str(element))
    return ' '.join(chunks)


def extract_textblock_tables(html, *, period_start, period_end, is_consolidated, textblock_tag='', context_ref=''):
    """Return only explicit business tables whose printed date range matches.

    The caller binds company, fiscal year, quarter and consolidation using DEI.
    TextBlock context alone is insufficient because it often wraps two years.
    """
    if not period_start or not period_end or not isinstance(is_consolidated,bool):return []
    if 'segment' not in textblock_tag.lower() or 'textblock' not in textblock_tag.lower():return []
    soup=BeautifulSoup(html or '','html.parser');whole=soup.get_text(' ',strip=True)
    outputs=[]
    for ti,table in enumerate(soup.find_all('table')):
        if table.find_parent('table') is not None:continue
        grid=_grid(table)
        if not grid:continue
        prefix=_prefix(soup,table);ranges=_period_ranges(prefix)
        if not ranges or ranges[-1][:2]!=(period_start,period_end):continue
        # Do not reuse a business-period heading after entering related
        # geographic/customer/product disclosures.
        flat=compact(prefix)
        boundary=max(flat.rfind('【関連情報】'),flat.rfind('[関連情報]'))
        if boundary>=0 and 'セグメント情報' not in flat[boundary+len('【関連情報】'):]:continue
        unit_matches=list(re.finditer(r'(?:単位[:：]?)?(百万円|千円|円)',compact(table.get_text(' ',strip=True))[:180]))
        if not unit_matches:
            unit_matches=list(re.finditer(r'単位[:：]?(百万円|千円|円)',compact(prefix)[-500:]))
        units={m.group(1) for m in unit_matches}
        if len(units)!=1:continue
        unit=next(iter(units));factor={'百万円':Decimal(1),'千円':Decimal('0.001'),'円':Decimal('0.000001')}[unit]
        metric_rows=[];body=None
        for ri,row in enumerate(grid):
            if not row or not row[0]:continue
            label=row[0]['text'];metric=metric_from_label(label)
            numeric=sum(c is not None and _number(c['text']) is not None for c in row[1:])
            if body is None and numeric>=2:body=ri
            has_dash=any(c is not None and compact(c['text']) in _NIL and compact(c['text']) for c in row[1:])
            if metric and (numeric or has_dash):metric_rows.append((ri,metric,label,row))
        if body is None or not any(m=='profit' for _,m,_,_ in metric_rows):continue
        # Presence of a segment-profit row distinguishes business performance
        # from regional revenue disaggregation without a corresponding profit.
        if not any('セグメント' in compact(label) or 'segment' in compact(label) for _,m,label,_ in metric_rows if m=='profit') and '報告セグメント' not in compact(table.get_text(' ',strip=True)):continue
        headers={}
        for row in grid[:body]:
            for ci,cell in enumerate(row):
                if not cell:continue
                label=cell['text'];text=compact(label)
                if not text or '単位' in text or text.strip('()') in ('千円','百万円','円'):continue
                headers[ci]=label
        selected={};ambiguous=False
        for ri,metric,label,row in metric_rows:
            basis=sales_basis(label,'',[]) if metric=='sales' else profit_basis(label)
            priority=(100 if basis=='external_customers' else 60 if basis=='disclosed_revenue' else 40) if metric=='sales' else 100
            if metric in selected and priority==selected[metric][0]:
                if [c['text'] if c else None for c in row]!=[c['text'] if c else None for c in selected[metric][4]]:ambiguous=True
            if metric not in selected or priority>selected[metric][0]:selected[metric]=(priority,ri,label,basis,row)
        if ambiguous:continue
        period_rows=[]
        for ci,label in sorted(headers.items()):
            if ci==0 or column_role(label)!='segment':continue
            # Remove only an explicit note marker, retaining the literal source label.
            name=re.sub(r'[（(]注[）)]\s*\d*.*$','',label).strip()
            if not name:continue
            values={};facts={};labels={};bases={}
            for metric,(_,ri,rowlabel,basis,row) in selected.items():
                if ci>=len(row) or row[ci] is None:continue
                cell=row[ci]
                if cell['colspan']!=1 or cell['column']!=ci:continue
                amount=_number(cell['text']);status='disclosed_numeric' if amount is not None else 'explicit_dash' if compact(cell['text']) in _NIL and compact(cell['text']) else 'unresolved_cell'
                value=amount*factor if amount is not None else None
                values[metric]=int(value) if value is not None and value==value.to_integral_value() else float(value) if value is not None else None
                facts[metric]={'textblock_tag':textblock_tag,'textblock_context':context_ref,'table_index':ti,'row_index':ri,'column_index':ci,'segment_label':label,'row_label':rowlabel,'raw_text':cell['text'],'display_unit':unit,'currency':'JPY','million_value':values[metric],'status':status,'period_start':period_start,'period_end':period_end,'printed_period':ranges[-1][2],'evidence_type':'xhtml_table_cell'}
                labels[metric]=rowlabel;bases[metric]=basis
            if not any(v is not None for v in values.values()):continue
            if not bases.get('profit'):
                explicit=[]
                for sentence in re.split(r'[。\n]',whole):
                    if re.search(r'(?:セグメント|報告セグメント|事業セグメント).{0,8}利益.{0,8}(?:は|を).{0,35}ベース',compact(sentence)):
                        b=profit_basis(sentence)
                        if b:explicit.append(b)
                bases['profit']=next(iter(set(explicit))) if len(set(explicit))==1 else 'company_defined_unknown'
            period_rows.append({'segment_name':name,'raw_segment_label':label,'sales':values.get('sales'),'profit':values.get('profit'),'sales_label':labels.get('sales'),'profit_label':labels.get('profit'),'sales_basis':bases.get('sales'),'profit_basis':bases.get('profit'),'period_start':period_start,'period_end':period_end,'is_consolidated':is_consolidated,'currency':'JPY','facts':facts})
        if period_rows:outputs.extend(period_rows)
    # Repeated presentation of the same period must agree. Never choose a
    # conflicting table by position or overwrite a valid individual fact.
    byname={}
    for row in outputs:
        key=compact(row['segment_name'])
        if key in byname and any(row.get(m)!=byname[key].get(m) for m in ('sales','profit')):return []
        byname.setdefault(key,row)
    return list(byname.values())
