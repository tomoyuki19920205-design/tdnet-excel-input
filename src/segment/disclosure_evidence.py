"""Period-bound evidence of an omitted business-segment disclosure."""
import re
import unicodedata

_PERIOD_HEADING = r'(前|当)(?:第[123一二三])?(?:四半期連結累計期間|四半期累計期間|中間連結会計期間|中間会計期間|連結会計年度|事業年度|連結累計期間|四半期連結会計期間)\(自'

def single_segment_omission_html(html, *, period_role='current'):
    """Bind the actual omission section without discarding another year's table.

    Table/fact markers retain their document positions. Related geographical,
    product and goodwill sections cannot establish business-table omission.
    """
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(str(html or ''),'html.parser')
    for tag in soup.find_all(['table','ix:nonfraction']):
        tag.insert_before(' SEGMENT_NUMERIC_STRUCTURE ')
    flat=re.sub(r'\s+','',unicodedata.normalize('NFKC',soup.get_text(' ',strip=True)))
    business=re.split(r'【(?:関連情報|報告セグメントごとの(?:固定資産|のれん|負ののれん))',flat,maxsplit=1)[0]
    headings=list(re.finditer(_PERIOD_HEADING,business))
    section=business;heading=None
    if headings:
        matching=[(i,h) for i,h in enumerate(headings) if ('current' if h.group(1)=='当' else 'previous')==period_role]
        if len(matching)!=1:return None
        i,h=matching[0]
        section=business[h.start():headings[i+1].start() if i+1<len(headings) else len(business)]
        heading=section[:section.find(')')+1] if ')' in section else None
    elif 'SEGMENT_NUMERIC_STRUCTURE' in business:
        return None
    if 'SEGMENT_NUMERIC_STRUCTURE' in section:return None
    proof=single_segment_omission(section,period_role=period_role)
    if proof:
        proof.update(evidence_rule='original_html_period_section_without_numeric_table',period_heading=heading)
    return proof

def single_segment_omission(text, *, period_role='current', table_count=0, numeric_facts=0):
    flat=re.sub(r'\s+','',unicodedata.normalize('NFKC',str(text or '')))
    if period_role not in ('current','previous') or table_count or numeric_facts:
        return None
    headings=list(re.finditer(_PERIOD_HEADING,flat))
    section=flat
    if headings:
        matching=[(i,h) for i,h in enumerate(headings) if ('current' if h.group(1)=='当' else 'previous')==period_role]
        if len(matching)!=1:return None
        i,h=matching[0];section=flat[h.start():headings[i+1].start() if i+1<len(headings) else len(flat)]
    elif re.search(r'前(?:第|期|年|連結|事業|中間|四半期)',flat):
        return None
    # A geographical or product-sales 90% exemption is not evidence that
    # business segment profit is undisclosed.
    for single in re.finditer(r'単一(?:の)?(?:事業)?セグメント',section):
        left=section.rfind('。',0,single.start())+1
        right=section.find('。',single.end())
        sentence=section[left:right+1 if right>=0 else len(section)]
        if period_role=='current' and re.search(r'前(?:第|期|年|連結|事業|中間|四半期)',sentence):continue
        omitted=any(x in sentence for x in ('省略','記載しておりません','開示しておりません'))
        if omitted:
            return {'kind':'single_segment_omission','period_role':period_role,'statement':sentence,'evidence_rule':'explicit_period_section_and_sentence'}
    return None
