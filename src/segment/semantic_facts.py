"""Interpret monetary segment facts from their disclosed table row, not substrings."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation


def monetary_fact(tag, soup):
    """Apply the actual XBRL unit and scale exactly once, preserving sub-millions."""
    ref = str(tag.get('unitref') or '')
    if not ref:
        return None  # Legacy XHTML without facts uses the disclosed table unit.
    units = soup.__dict__.get('_segment_money_units')
    if units is None:
        units = {}
        for unit in soup.find_all(lambda t: t.name and t.name.lower().split(':')[-1] == 'unit'):
            measures = unit.find_all(lambda t: t.name and t.name.lower().split(':')[-1] == 'measure')
            units[str(unit.get('id') or '')] = [m.get_text(strip=True).split(':')[-1].upper() for m in measures]
        soup.__dict__['_segment_money_units'] = units
    measures = units.get(ref, [ref.upper()])
    currency = 'JPY' if measures == ['JPY'] else None
    evidence = {'unit_ref': ref, 'currency': currency, 'scale': tag.get('scale'),
                'context': tag.get('contextref'), 'tag': tag.get('name'),
                'raw_text': tag.get_text(strip=True), 'sign': tag.get('sign')}
    if currency != 'JPY':
        return evidence
    raw = unicodedata.normalize('NFKC', evidence['raw_text']).replace(',', '').replace(' ', '')
    negative = tag.get('sign') == '-' or '△' in raw or raw.startswith('(')
    raw = raw.replace('△', '').strip('()')
    try:
        value = abs(Decimal(raw)) * (-1 if negative or raw.startswith('-') else 1)
        value *= Decimal(10) ** int(tag.get('scale') or 0)
        value /= Decimal(1000000)
        if value.is_finite():
            evidence['million_value'] = int(value) if value == value.to_integral_value() else float(value)
    except (InvalidOperation, ValueError, OverflowError):
        pass
    return evidence

def row_label(tag):
    label=_leading_row_text(tag)
    evidence=column_evidence(tag)
    if evidence.get('orientation')=='business_rows_metric_columns':
        return evidence['metric_column']
    if evidence.get('orientation')=='business_rows_period_columns':
        if compact(label)=='外部顧客に対するもの':
            for prior in tag.find_parent('tr').find_previous_siblings('tr'):
                cells=prior.find_all(['td','th'],recursive=False)
                text=cells[0].get_text(' ',strip=True) if cells else ''
                if metric_from_label(text)=='sales':return text.rstrip(':：')+'（'+label+'）'
        if metric_from_label(label) or re.match(r'^(?:外部顧客|セグメント間)',compact(label)):
            return label
        row=tag.find_parent('tr')
        for prior in row.find_previous_siblings('tr'):
            cells=prior.find_all(['td','th'],recursive=False)
            text=cells[0].get_text(' ',strip=True) if cells else ''
            if metric_from_label(text):return text
    return label


def _leading_row_text(tag):
    row = tag.find_parent('tr')
    if row is None:
        return ''
    parts = []
    for cell in row.find_all(['td', 'th'], recursive=False):
        if cell.find('ix:nonfraction') is not None:
            break
        text = cell.get_text(' ', strip=True)
        if text:
            parts.append(text)
    return ' '.join(parts).strip()[:250]

def compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or '')).lower()


def column_evidence(tag):
    """Resolve the fact's actual XHTML header, including row/column spans.

    Some issuer files attach an Other member to a total column. The displayed
    column role is therefore an independent guard on dimension-based extraction.
    """
    cell = tag.find_parent(['td', 'th'])
    table = tag.find_parent('table')
    if cell is None or table is None:
        return {'label': '', 'role': 'unresolved'}
    cache = table.__dict__.get('_segment_column_evidence')
    if cache is None:
        occupied = {}; headers = {}; cache = {}; body_started = False; spanning_group_headers=set()
        for ri, row in enumerate(table.find_all('tr')):
            if row.find_parent('table') is not table:
                continue
            cells = row.find_all(['td', 'th'], recursive=False)
            has_fact = any(c.find('ix:nonfraction') is not None for c in cells)
            # XHTML can contain untagged numeric rows before the first XBRL fact.
            numeric_cells = sum(bool(re.fullmatch(r'[△▲+\-−]?\(?\d[\d,]*(?:\.\d+)?\)?', compact(c.get_text(' ', strip=True)))) or compact(c.get_text(' ', strip=True)) in ('-','―','—') for c in cells)
            labels=[c.get_text(' ',strip=True) for c in cells]
            wrapped_leaf_header = (
                labels and not compact(labels[0])
                and sum(column_role(label) in ('segment','total','adjustment_or_consolidated') and not metric_from_label(label) for label in labels) >= 2
                and any(column_role(label) in ('total','adjustment_or_consolidated') for label in labels)
            )
            if body_started and not has_fact and not numeric_cells and (wrapped_leaf_header or any(compact(label) in ('報告セグメント','事業セグメント','reportablesegments') for label in labels)):
                # The same HTML table can wrap horizontally and repeat its header.
                headers={};body_started=False
            body_started = body_started or has_fact or numeric_cells >= 2
            ci = 0
            for current in cells:
                while (ri, ci) in occupied:
                    ci += 1
                try:
                    colspan = max(1, int(current.get('colspan') or 1))
                    rowspan = max(1, int(current.get('rowspan') or 1))
                except ValueError:
                    colspan = rowspan = 1
                label = current.get_text(' ', strip=True)
                unit_only = bool(re.fullmatch(r'(?:金額)?\(?(?:百万円|千円|円|millionsofyen|thousandsofyen)\)?',compact(label)))
                note_only = bool(re.fullmatch(r'\(?注\)?[0-9一二三四五六七八九十、,.・]*\)?',compact(label)))
                if not body_started and label and not unit_only and not note_only:
                    if colspan > 1 and column_role(label) in ('total','adjustment_or_consolidated'):
                        spanning_group_headers.add(label)
                    for col in range(ci, ci + colspan):
                        headers.setdefault(col,[]).append(label)
                if has_fact:
                    paths = [headers.get(col,[]) for col in range(ci, ci + colspan)]
                    cache[id(current)] = paths[0] if paths and all(p==paths[0] for p in paths) else []
                for rr in range(ri, ri + rowspan):
                    for cc in range(ci, ci + colspan):
                        occupied[rr, cc] = True
                ci += colspan
        table.__dict__['_segment_column_evidence'] = cache
        table.__dict__['_segment_spanning_group_headers'] = spanning_group_headers
        leaf_paths={}
        for p in cache.values():
            businesses=tuple(compact(x) for x in p if column_role(x)=='segment')
            if businesses:leaf_paths.setdefault(businesses[-1],set()).add(businesses)
        table.__dict__['_segment_leaf_paths']=leaf_paths
    path = cache.get(id(cell), [])
    if not path:
        # Some filings split a single printed table into adjacent header/body
        # tables. Require a header-only sibling and an identical expanded grid.
        previous=table.find_previous_sibling()
        if previous is None and table.parent.name=='div' and len(table.parent.find_all('table'))==1 and compact(table.parent.get_text(' ',strip=True))==compact(table.get_text(' ',strip=True)):
            wrapper=table.parent.find_previous_sibling()
            if wrapper is not None and wrapper.name=='div' and len(wrapper.find_all('table'))==1:
                candidate=wrapper.find('table')
                if compact(wrapper.get_text(' ',strip=True))==compact(candidate.get_text(' ',strip=True)):previous=candidate
        if previous is not None and previous.name=='table' and previous.find('ix:nonfraction') is None and '報告セグメント' in compact(previous.get_text(' ',strip=True)):
            from src.segment.xhtml_segment_tables import _grid
            header_grid=_grid(previous);body_grid=_grid(table)
            widths={len(row) for row in header_grid+body_grid if row}
            if len(widths)==1 and not any(re.fullmatch(r'[△▲+\-−]?\d[\d,]*(?:\.\d+)?',compact(c['text'])) for row in header_grid for c in row if c):
                columns={ci for row in body_grid for ci,c in enumerate(row) if c and c['node'] is cell}
                paths=[]
                for ci in columns:
                    parts=[]
                    for row in header_grid:
                        text=row[ci]['text'] if row[ci] else ''
                        if text and text not in parts:parts.append(text)
                    paths.append(parts)
                if paths and all(p==paths[0] for p in paths):path=paths[0]
    label = path[-1] if path else ''
    period_headers=[x for x in path if re.match(r'^(?:(?:前|当).*(?:会計期間|会計年度|事業年度|累計期間)|\d{4}年度)',compact(x))]
    business_headers=[x for x in path if column_role(x)=='segment' and not metric_from_label(x)]
    if (period_headers and not business_headers) or metric_from_label(label):
        business=_leading_row_text(tag)
        # A standalone intersegment row is a reconciliation, not a nested
        # revenue measure whose business name must be recovered above it.
        if compact(business) in ('セグメント間取引','セグメント間取引消去'):
            return {'label':business,'role':'adjustment_or_consolidated','header_path':path}
        if column_role(business) in ('total','adjustment_or_consolidated'):
            return {'label':business,'role':column_role(business),'header_path':path}
        if re.match(r'^(?:外部顧客|セグメント間)',compact(business)):
            for prior in tag.find_parent('tr').find_previous_siblings('tr'):
                cells=prior.find_all(['td','th'],recursive=False)
                if not cells:continue
                heading=cells[0].get_text(' ',strip=True).strip().rstrip(':：')
                if any(c.get_text(strip=True) for c in cells[1:]):continue
                if heading and not metric_from_label(heading) and column_role(heading)=='segment':
                    business=heading;break
        if business and not metric_from_label(business) and column_role(business)=='segment':
            result={'label':business,'role':'segment','orientation':'business_rows_metric_columns' if metric_from_label(label) else 'business_rows_period_columns','header_path':path}
            if period_headers:result['period_column']=period_headers[-1]
            if metric_from_label(label):result['metric_column']=label
            return result
    # Retain both levels of real business headers (e.g. construction / Japan).
    # Reportable-segment group captions and note-only rows do not rename a cell.
    groups=table.__dict__.get('_segment_spanning_group_headers',set())
    meaningful=[x for i,x in enumerate(path) if column_role(x) not in ('header_group','unresolved') and not (
        x in groups and any(column_role(child)=='segment' and not metric_from_label(child) for child in path[i+1:])
    )]
    if meaningful:
        last=meaningful[-1];role=column_role(last)
        aggregate=next((x for x in meaningful if column_role(x) in ('total','adjustment_or_consolidated')),None)
        if aggregate:label=aggregate;role=column_role(aggregate)
        else:
            ambiguous_leaf=len(table.__dict__['_segment_leaf_paths'].get(compact(last),set()))>1
            label=' / '.join(dict.fromkeys(meaningful)) if role=='segment' and ambiguous_leaf else last
    else:role=column_role(label)
    return {'label': label, 'role': role, 'header_path':path}


def column_role(label):
    text = compact(label)
    if re.fullmatch(r'[△▲+\-−]?\(?\d[\d,]*(?:\.\d+)?\)?', text):
        role = 'unresolved'
    elif re.match(r'^(?:(?:前|当).*(?:会計期間|会計年度|事業年度|累計期間)|\d{4}年度)',text) or text in ('報告セグメント', '事業セグメント', 'セグメント', 'reportablesegments') or '単位:' in text or re.fullmatch(r'(?:金額)?\(?(?:百万円|千円|円)\)?',text):
        role = 'header_group'
    elif re.match(r'^(?:計|合計|小計|総計|連結合計|報告セグメント(?:合)?計|事業セグメント(?:合)?計|事業計|total|subtotal)(?:$|\(|注|※)', text):
        role = 'total'
    elif text in ('連結','consolidated') or re.match(r'^(?:調整額|消去|全社|(?:四半期|中間)?(?:連結)?財務諸表|(?:四半期|中間)?(?:連結)?損益計算書)', text):
        role = 'adjustment_or_consolidated'
    else:
        role = 'segment' if label else 'unresolved'
    return role

def disclosed_metric(tag):
    """Require an unambiguous row label and a monetary unit; caller checks dimension."""
    label = row_label(tag)
    unit = compact(str(tag.get('unitref') or ''))
    if not unit or any(x in unit for x in ('pershare', 'shares', 'pure', 'percent')):
        return None, label
    return metric_from_label(label), label


def metric_from_label(label):
    text = compact(label)
    if re.match(r'^売上高及び金融ビジネス収入(?:$|[:：(])',text):
        return 'sales'
    if re.match(r'^(?:(?:調整後|adjusted)?ebit(?:da)?|財務[・及び]*法人所得税前利益|金利[・及び]*税引前利益)(?:$|\(|※|（)', text):
        return 'profit'
    # Components of profit, rates, forecasts and reconciliation adjustments are not profits.
    if any(x in text for x in ('利益率', '損益率', '前年比', '増減率', '償却費', '減損', '調整額', '配賦', '法人税', '1株', '一株')):
        return None
    # Net income is eligible only as a whole disclosed measure. Callers still
    # require a real business column/dimension and its duration context.
    from src.segment.profit_basis import profit_basis
    if not any(x in text for x in ('非支配','予想','増減額','構成比','1人','一人')) and profit_basis(text) in ('net_profit','profit_attributable_to_owners'):
        return 'profit'
    if re.match(r'^(?:セグメント(?:利益|損益|損失)|(?:調整後営業|コア営業|事業|売上総|営業|経常|税引前|のれん等償却前セグメント)(?:利益|損益|損失)|与信費用控除後業務純益)', text):
        return 'profit'
    if re.match(r'^(?:segment(?:profit|loss|income)|adjustedoperating(?:profit|loss)|businessprofit)', text):
        return 'profit'
    if re.match(r'^(?:外部顧客(?:への|からの|に対する)(?:売上高|売上収益|営業収益|収益)|外部収益|完成工事高|営業収益|営業総収入|売上高|売上収益(?:合計)?|顧客との契約から生じる収益)(?:$|\(|※)', text):
        return 'sales'
    return None

def sales_basis(label, selected_tag, available_tags):
    text = compact(label)
    tag = compact(selected_tag)
    if 'externalcustomer' in tag or '外部顧客' in text:
        return 'external_customers'
    if 'contractswithcustomers' in tag or '顧客との契約' in text:
        return 'customer_contract_revenue'
    if any('externalcustomer' in compact(t) for t in available_tags):
        return 'including_intersegment'
    if '完成工事高' in text:
        return 'construction_revenue'
    return 'disclosed_revenue'
