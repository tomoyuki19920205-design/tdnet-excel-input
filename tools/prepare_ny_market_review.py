#!/usr/bin/env python3
"""Checkpoint a finite review queue; no inference, retrieval or paid API calls."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lib.ny_market_deadline import CONTRACT,counts
from tools.company_news_atomic import atomic_write_json

def prepare(candidates,decisions,as_of,previous=None):
    if isinstance(candidates,dict):candidates=candidates['candidates']
    old={c['ticker']:c for c in (previous or {}).get('candidates',[])}
    updates={d['ticker']:d for d in decisions}
    if len(updates)!=len(decisions):raise ValueError('duplicate candidate decisions')
    names={c['ticker'] for c in candidates}
    if set(updates)-names or set(old)-names:raise ValueError('cannot discard an existing candidate')
    output=[]
    for i,c in enumerate(candidates):
        ticker=c['ticker']
        row=old.get(ticker,dict(ticker=ticker,importance_rank=i+1,priority='high' if i<10 else 'normal',
            status='pending',reason_code='awaiting_verification',reason='一次資料・発表時刻・価格反応等の確認が未完了。',
            discovery_routes=c.get('discovery_routes',[])))
        # Preserve discovery provenance for the complete-review validator too.
        routes=c.get('discovery_routes') or []
        discovery=c.get('discovery_source_url') or next((u for u in routes if isinstance(u,str) and u.startswith(('http://','https://'))),None)
        if discovery and not row.get('discovery_source_url'):
            row={**row,'discovery_source_url':discovery}
        if ticker in updates:
            if old.get(ticker,{}).get('status') in {'included','excluded'} and updates[ticker].get('status')=='pending':
                raise ValueError('resolved candidate cannot silently regress to pending')
            row={**row,**updates[ticker]}
        output.append(row)
    return dict(contract_version=CONTRACT,as_of_utc=as_of,
                priority_policy='market-cap discovery order; first ten high; explicit material-event overrides retained',
                candidates=output,counts=counts(output))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidates',type=Path,required=True)
    p.add_argument('--decisions',type=Path)
    p.add_argument('--previous',type=Path)
    p.add_argument('--as-of-utc',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
    if a.output.exists():raise ValueError('save each review revision to a new path')
    result=prepare(read(a.candidates),read(a.decisions) if a.decisions else [],a.as_of_utc,read(a.previous) if a.previous else None)
    atomic_write_json(a.output,result)
    print(json.dumps(result['counts']))
if __name__=='__main__':main()
