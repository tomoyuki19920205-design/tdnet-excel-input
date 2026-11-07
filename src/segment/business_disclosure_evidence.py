"""Explicit business-note omissions, including older untagged attachments."""
import re
import unicodedata
from bs4 import BeautifulSoup, NavigableString, Tag
from src.segment.disclosure_evidence import single_segment_omission, _PERIOD_HEADING

def _flat(value):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value or '')))

def business_segment_omission_html(html, *, period_role='current'):
    if period_role not in ('current','previous'):return None
    soup=BeautifulSoup(str(html or ''),'html.parser')
    for tag in soup.find_all(['table','ix:nonfraction']):tag.insert_before(' SEGMENT_NUMERIC_STRUCTURE ')
    text=_flat(soup.get_text(' ',strip=True))
    text=re.sub(r'[【\[(]セグメント情報(?:等)?[】\])]','',text)
    text=re.split(r'【(?:関連情報|報告セグメントごとの(?:固定資産|のれん|負ののれん))',text,maxsplit=1)[0]
    headings=list(re.finditer(_PERIOD_HEADING,text));heading=None
    if headings:
        matching=[(i,h) for i,h in enumerate(headings) if ('current' if h.group(1)=='当' else 'previous')==period_role]
        if len(matching)!=1:return None
        i,h=matching[0];text=text[h.start():headings[i+1].start() if i+1<len(headings) else len(text)]
        heading=text[:text.find(')')+1] if ')' in text else None
    elif re.search(r'前(?:第|期|年|連結|事業|中間|四半期)',text):return None
    if 'SEGMENT_NUMERIC_STRUCTURE' in text:return None
    proof=single_segment_omission(text,period_role=period_role)
    if not proof:
        for sentence in re.findall(r'[^。]+(?:。|$)',text):
            if any(word in sentence for word in ('地域別','所在地別','製品別','顧客別')):continue
            if re.search(r'セグメント情報(?:等)?(?:について)?(?:の記載)?(?:は|を)省略(?:する|して)',sentence):
                proof={'kind':'business_segment_omission','period_role':period_role,'statement':sentence,'omission_basis':'immateriality' if '重要性' in sentence else 'explicit_business_note'}
                break
    if proof:proof.update(evidence_rule='explicit_business_note_period_section_without_numeric_table',period_heading=heading)
    return proof

def attachment_business_omissions(html):
    """Isolate real segment headings; unrelated financial tables prove nothing."""
    soup=BeautifulSoup(str(html or ''),'html.parser');out=[];seen=set()
    for ordinal,heading in enumerate(soup.find_all(['h1','h2','h3','h4','h5','h6','p'])):
        label=_flat(heading.get_text(' ',strip=True))
        if not re.fullmatch(r'[【\[(]?セグメント情報(?:等)?[】\])]?',label):continue
        parts=[]
        for node in heading.next_elements:
            if isinstance(node,Tag):
                if node is not heading and node.name in ('h1','h2','h3','h4','h5','h6'):break
                text=_flat(node.get_text(' ',strip=True)) if node.name=='p' else ''
                if re.fullmatch(r'【[^】]+】',text) and 'セグメント情報' not in text:break
                if node.name in ('table','ix:nonfraction'):parts.append(' SEGMENT_NUMERIC_STRUCTURE ')
            elif isinstance(node,NavigableString):parts.append(str(node))
        section=' '.join(parts)
        for role in ('current','previous'):
            proof=business_segment_omission_html(section,period_role=role)
            if not proof or (role=='previous' and not proof.get('period_heading')):continue
            key=role,proof['statement']
            if key in seen:continue
            seen.add(key);out.append({'heading':heading.get_text(' ',strip=True),'heading_ordinal':ordinal,**proof})
    return out
