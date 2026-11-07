"""Preserve the disclosed segment profit measure independently of its value."""
import re

LABELS = {
    'gross_profit': 'セグメント利益（売上総利益ベース）',
    'operating_profit': 'セグメント利益（営業利益ベース）',
    'ordinary_profit': 'セグメント利益（経常利益ベース）',
    'profit_before_tax': 'セグメント利益（税引前利益ベース）',
    'adjusted_operating_profit': 'セグメント利益（調整後営業利益ベース）',
    'core_operating_profit': 'セグメント利益（コア営業利益ベース）',
    'business_profit': 'セグメント利益（事業利益ベース）',
    'ebitda': 'セグメント利益（EBITDAベース）',
    'adjusted_ebitda': 'セグメント利益（調整後EBITDAベース）',
    'ebit': 'セグメント利益（EBITベース）',
    'adjusted_ebit': 'セグメント利益（調整後EBITベース）',
    'before_goodwill_amortization': 'セグメント利益（のれん等償却前）',
    'company_defined_unknown': 'セグメント利益（基準未確定）',
}


def profit_basis(value):
    if value in LABELS:
        return value
    text = re.sub(r'\s+', '', str(value or '')).lower()
    if re.search(r'all[-－‐‑–—]?in営業利益', text):
        # The issuer's ALL-IN result includes extraordinary property gains.
        # Retain its original label; it is not ordinary operating profit.
        return 'company_defined_unknown'
    if 'ebitda' in text:
        return 'adjusted_ebitda' if 'adjusted' in text or '調整後' in text else 'ebitda'
    if re.fullmatch(r'(?:adjusted|調整後)?ebit(?:ifrs)?', text) or 'profitbeforefinancingandincometax' in text or '財務・法人所得税前利益' in text or '金利税引前利益' in text:
        return 'adjusted_ebit' if 'adjusted' in text or '調整後' in text else 'ebit'
    if 'adjustedoperating' in text or '調整後営業' in text:
        return 'adjusted_operating_profit'
    if 'coreoperating' in text or 'コア営業' in text:
        return 'core_operating_profit'
    if 'beforeamortizationofgoodwill' in text or 'のれん等償却前' in text:
        return 'before_goodwill_amortization'
    if 'businessprofit' in text or '事業利益' in text or '事業損益' in text:
        return 'business_profit'
    if 'grossprofit' in text or '売上総利益' in text:
        return 'gross_profit'
    if 'extraordinary' in text:
        return None
    if 'ordinaryincome' in text or '経常利益' in text:
        return 'ordinary_profit'
    if 'beforetax' in text or 'incomebefore' in text or '税引前' in text:
        return 'profit_before_tax'
    if 'operatingincome' in text or 'operatingprofit' in text or '営業利益' in text:
        return 'operating_profit'
    if '基準未確定' in text:
        return 'company_defined_unknown'
    return None


def profit_label(value):
    return LABELS.get(profit_basis(value), str(value or 'セグメント利益'))


def segment_flags(raw):
    raw = raw or {}
    evidence = raw.get('_context_evidence') or {}
    flags = {k: v for k, v in {
        'profit_basis': raw.get('profit_basis') or profit_basis(raw.get('profit_tag')),
        'profit_tag': raw.get('profit_tag'),
        'profit_label': raw.get('raw_profit_label'),
        'segment_label': raw.get('raw_segment_label'),
        'sales_basis': raw.get('sales_basis'),
        'sales_label': raw.get('raw_sales_label'),
        'sales_tag': raw.get('_sales_fact_selected_name'),
        'period_start': evidence.get('context_start'),
        'period_end': evidence.get('context_end'),
        'is_consolidated': raw.get('is_consolidated', True),
        'currency': raw.get('currency'),
        'table_facts': raw.get('table_facts'),
        'extraction_method': raw.get('extraction_method'),
        'period_type': 'ytd',
    }.items() if v is not None and v != '?'}
    from src.segment.business_identity import raw_business_identity_flags
    flags.update(raw_business_identity_flags(raw))
    return flags


def persisted_flags(value):
    """Read metadata from the existing SQLite segment record, including old rows."""
    import json
    raw = value.get('segment_metadata') or value.get('flags') or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    flags = dict(raw)
    if not flags.get('profit_basis') and value.get('raw_profit_label'):
        flags['profit_basis'] = profit_basis(value['raw_profit_label']) or 'company_defined_unknown'
    if value.get('raw_profit_label') and not flags.get('profit_label'):
        flags['profit_label'] = value['raw_profit_label']
    return {k: v for k, v in flags.items() if v is not None}


def raw_profit_label(raw):
    raw = raw or {}
    return raw.get('raw_profit_label') or profit_label(raw.get('profit_basis') or raw.get('profit_tag'))
