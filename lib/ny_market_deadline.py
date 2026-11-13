"""Finite candidate review and honest publication at the daily deadline.

Publication is independent of exhausting a discovery queue. Pending candidates
remain pending and are never evidence for a quiet market or an exclusion.
"""
from datetime import datetime
import hashlib
from pathlib import Path
import re

CONTRACT = 'ny_market_deadline_v1'
REVIEW_CONTRACT = 'ny_market_after_hours_v3'
MAX_CANDIDATES = 5000


def counts(candidates):
    if not isinstance(candidates,list) or len(candidates)>MAX_CANDIDATES:
        raise ValueError('candidate list must be bounded')
    result=dict(discovered=len(candidates),confirmed=0,adopted=0,excluded=0,pending=0,important_pending=0)
    seen=set()
    for index,c in enumerate(candidates):
        ticker=c.get('ticker')
        if not isinstance(ticker,str) or not ticker or ticker in seen:
            raise ValueError('candidate ticker missing or duplicated')
        seen.add(ticker)
        if c.get('importance_rank')!=index+1:
            raise ValueError('candidate ranks must be contiguous')
        if c.get('priority') not in {'high','normal'} or (index<10 and c['priority']!='high'):
            raise ValueError('first ten importance ranks must be high priority')
        if not isinstance(c.get('reason'),str) or not c['reason'].strip():
            raise ValueError('candidate decision or pending reason required')
        status=c.get('status')
        if status=='pending':
            result['pending']+=1
            result['important_pending']+=c['priority']=='high'
            if c.get('reason_code')!='awaiting_verification':
                raise ValueError('pending is not an exclusion')
        elif status in {'included','excluded'}:
            if not c.get('decision_evidence'):
                raise ValueError('resolved candidate requires decision evidence')
            result['confirmed']+=1
            result['adopted' if status=='included' else 'excluded']+=1
        else:raise ValueError('unsupported candidate status')
    return result


def notice(value):
    return (f"候補{value['discovered']}件：確認済み{value['confirmed']}件／採用{value['adopted']}件／"
            f"未確認{value['pending']}件（重要候補{value['important_pending']}件）。")


def validate_progress(progress):
    if not isinstance(progress,dict) or progress.get('contract_version')!=CONTRACT:
        raise ValueError('deadline review contract required')
    expected=counts(progress.get('candidates'))
    if progress.get('counts')!=expected:
        raise ValueError('review counts differ from candidate decisions')
    cutoff=datetime.fromisoformat(progress['as_of_utc'].replace('Z','+00:00'))
    if cutoff.tzinfo is None:raise ValueError('evidence cutoff needs timezone')
    return expected,cutoff


def validate_updates(updates,cutoff):
    if not isinstance(updates,list) or len(updates)>40:
        raise ValueError('verified updates must be bounded')
    ids=set()
    for update in updates:
        for key in ('id','title','summary','section'):
            if not isinstance(update.get(key),str) or not update[key].strip():
                raise ValueError('verified update needs id, title, section and summary')
        if update['id'] in ids:raise ValueError('duplicate verified update')
        ids.add(update['id'])
        if not update.get('evidence'):raise ValueError('verified update needs primary evidence')
        for source in update['evidence']:
            if not re.match(r'https?://[^/]+',str(source.get('url',''))):
                raise ValueError('source URL required')
            published=datetime.fromisoformat(source['published_at'].replace('Z','+00:00'))
            if published.tzinfo is None or published>cutoff:
                raise ValueError('future source must not enter an earlier report')
            for path_key,hash_key in (('path','sha256'),('text_path','text_sha256')):
                raw=Path(source[path_key]).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=source[hash_key]:
                    raise ValueError('archived source integrity failure')
            text=Path(source['text_path']).read_text(encoding='utf8')
            if not source.get('quote') or source['quote'] not in text:
                raise ValueError('evidence quote must occur in verified archived text')


def render_updates(payload):
    progress=payload['review_progress']
    value,cutoff=validate_progress(progress)
    updates=payload.get('verified_updates',[])
    validate_updates(updates,cutoff)
    lines=['','## 確認状況',notice(value),
           '未確認候補は除外していません。確認できた情報を先に掲載し、確認が進んだ場合に更新します。']
    if value['important_pending']:
        tickers=[c['ticker'] for c in progress['candidates'] if c['status']=='pending' and c['priority']=='high']
        lines.append('重要候補の確認が残っています：'+', '.join(tickers)+'。このレポートは完全版ではありません。')
    previous=None
    for update in updates:
        if update['section']!=previous:
            lines.extend(['','## '+update['section']]);previous=update['section']
        links='、'.join(f"[出典{i+1}]({s['url']})" for i,s in enumerate(update['evidence']))
        lines.extend(['','### '+update['title'],update['summary'],links])
    return '\n'.join(lines)
