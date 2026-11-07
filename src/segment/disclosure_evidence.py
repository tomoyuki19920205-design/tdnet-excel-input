"""Period-bound evidence of an omitted business-segment disclosure."""
import re
import unicodedata

def single_segment_omission(text, *, period_role='current', table_count=0, numeric_facts=0):
    flat=re.sub(r'\s+','',unicodedata.normalize('NFKC',str(text or '')))
    if period_role not in ('current','previous') or table_count or numeric_facts:
        return None
    headings=list(re.finditer(r'(前|当)(?:第[123一二三])?(?:四半期連結累計期間|四半期累計期間|中間連結会計期間|中間会計期間|連結会計年度|事業年度|連結累計期間|四半期連結会計期間)\(自',flat))
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
