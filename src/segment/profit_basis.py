"""Preserve the disclosed segment profit measure independently of its value."""
import re

LABELS = {
    'gross_profit': 'セグメント利益（売上総利益ベース）',
    'operating_profit': 'セグメント利益（営業利益ベース）',
    'ordinary_profit': 'セグメント利益（経常利益ベース）',
    'profit_before_tax': 'セグメント利益（税引前利益ベース）',
}


def profit_basis(value):
    text = re.sub(r'\s+', '', str(value or '')).lower()
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
    return None


def profit_label(value):
    return LABELS.get(profit_basis(value), str(value or 'セグメント利益'))


def segment_flags(raw):
    raw = raw or {}
    evidence = raw.get('_context_evidence') or {}
    return {k: v for k, v in {
        'profit_basis': raw.get('profit_basis') or profit_basis(raw.get('profit_tag')),
        'profit_tag': raw.get('profit_tag'),
        'period_start': evidence.get('context_start'),
        'period_end': evidence.get('context_end'),
        'is_consolidated': True,
        'period_type': 'ytd',
    }.items() if v is not None and v != '?'}
