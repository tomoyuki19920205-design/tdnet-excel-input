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

def disclosed_metric(tag):
    """Require an unambiguous row label and a monetary unit; caller checks dimension."""
    label = row_label(tag)
    text = compact(label)
    unit = compact(str(tag.get('unitref') or ''))
    if not unit or any(x in unit for x in ('pershare', 'shares', 'pure', 'percent')):
        return None, label
    # Components of profit, rates, forecasts and reconciliation adjustments are not profits.
    if any(x in text for x in ('利益率', '損益率', '前年比', '増減率', '償却費', '減損', '調整額', '配賦', '法人税', '1株', '一株')):
        return None, label
    if re.match(r'^(?:セグメント(?:利益|損益|損失)|(?:調整後営業|コア営業|事業|売上総|営業|経常|税引前|のれん等償却前セグメント)(?:利益|損益|損失)|与信費用控除後業務純益)', text):
        return 'profit', label
    if re.match(r'^(?:segment(?:profit|loss|income)|adjustedoperating(?:profit|loss)|businessprofit)', text):
        return 'profit', label
    if re.match(r'^(?:外部顧客(?:への|からの|に対する)(?:売上|収益)|完成工事高|営業収益|売上高|顧客との契約から生じる収益)(?:$|\(|※)', text):
        return 'sales', label
    return None, label

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
