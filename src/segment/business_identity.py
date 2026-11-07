"""Exact printed business identities, independent of changing taxonomy members."""
import re
import unicodedata

METHOD = 'original_business_header_v1'

def normalize_business_header(label):
    """Remove typography and explicit trailing notes, retaining business wording."""
    parts = []
    for part in unicodedata.normalize('NFKC', str(label or '')).split(' / '):
        part = re.sub(r'\s*\(注[^)]*\)\s*[0-9一二三四五六七八九十、,・.]*\s*$', '', part)
        part = re.sub(r'\s*\(?注\)?\s*[0-9一二三四五六七八九十、,・.]*\s*$', '', part)
        part = re.sub(r'\s+', '', part).lower()
        if part:
            parts.append(part)
    return '/'.join(parts)

def business_identity_flags(columns):
    """Every selected metric must identify the same real printed business."""
    from src.segment.semantic_facts import column_role, metric_from_label
    if not columns:
        return {}
    keys = set()
    for column in columns:
        if not column or column.get('role') != 'segment':
            return {}
        label = column.get('label')
        if not label or column_role(label) != 'segment' or metric_from_label(label):
            return {}
        key = normalize_business_header(label)
        if not key:
            return {}
        keys.add(key)
    if len(keys) != 1:
        return {}
    return {'business_key': keys.pop(), 'business_key_method': METHOD,
            'business_key_evidence': columns}

def raw_business_identity_flags(raw):
    """Use selected fact columns or literal table coordinates, never a guessed alias."""
    columns = []
    for metric in ('sales', 'profit'):
        literal = (raw.get('table_facts') or {}).get(metric)
        fact = raw.get(metric + '_fact')
        if literal and literal.get('status') == 'disclosed_numeric':
            columns.append({'label': literal.get('segment_label'), 'role': 'segment',
                'textblock_context': literal.get('textblock_context'),
                'table_index': literal.get('table_index'), 'column_index': literal.get('column_index')})
        elif fact:
            column = fact.get('column') or (fact.get('inline_evidence') or {}).get('column')
            if raw.get('hierarchical_business_identity') and column:
                column = {**column, 'label': raw.get('source_column_label')}
            columns.append(column)
    return business_identity_flags(columns)
