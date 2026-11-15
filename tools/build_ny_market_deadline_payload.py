#!/usr/bin/env python3
"""Publish source-verified progress without discarding pending candidates."""
from pathlib import Path
from datetime import date
import argparse,hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lib.ny_market import render_provisional_markdown,report_markdown_sha256,validate_payload
from lib.ny_market_deadline import CONTRACT,validate_progress,validate_updates,validate_morning_cutoff
from lib.ny_market_research import market_data_packet_sha256
from tools.build_ny_market_provisional_payload import build
from tools.company_news_atomic import atomic_write_json

def _current_market_gaps(packet, resolution):
    gaps=list(packet.get('unresolved_items') or [])
    if resolution is None:
        return gaps
    if packet.get('quality_status')!='provisional' or packet.get('top_gainers_20'):
        raise ValueError('only a provisional market packet can supersede an old blocker')
    if resolution.get('packet_sha256')!=market_data_packet_sha256(packet):
        raise ValueError('superseded market packet hash mismatch')
    old=resolution.get('superseded_items')
    new=resolution.get('replacement_items')
    if not isinstance(old,list) or not old or not isinstance(new,list) or not new:
        raise ValueError('market blocker replacement requires old and current unresolved items')
    if any(item not in gaps for item in old):
        raise ValueError('superseded blocker differs from the archived market packet')
    if not any(item.get('field') in {prior.get('field') for prior in old} for item in new):
        raise ValueError('replacement must retain the unresolved market field')
    evidence=resolution.get('evidence')
    if not isinstance(evidence,list) or not evidence:
        raise ValueError('market blocker resolution needs archived evidence')
    for item in evidence:
        path=Path(item['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item.get('sha256'):
            raise ValueError('market blocker resolution evidence hash mismatch')
    candidate=json.loads(Path(resolution['candidate_path']).read_text(encoding='utf-8-sig'))
    if (hashlib.sha256(Path(resolution['candidate_path']).read_bytes()).hexdigest()!=resolution.get('candidate_sha256')
            or candidate.get('market_session_date')!=packet.get('market_session_date')
            or [row.get('rank') for row in candidate.get('top_gainers_20') or []]!=list(range(1,21))):
        raise ValueError('verified price candidate does not match the market session')
    for item in new:
        if not isinstance(item,dict) or not isinstance(item.get('field'),str) or not item['field'].strip() or not isinstance(item.get('reason'),str) or not item['reason'].strip():
            raise ValueError('replacement market blocker needs a field and reason')
    return [item for item in gaps if item not in old]+new

def build_deadline(packet,report_date,market_status,progress,updates,unresolved,market_resolution=None):
    value,cutoff=validate_progress(progress)
    validate_morning_cutoff(cutoff,report_date)
    validate_updates(updates,cutoff)
    payload=build(packet,report_date,market_status)
    payload.update(publication_contract_version=CONTRACT,review_progress=progress,
                   verified_updates=updates,
                   unresolved_items=_current_market_gaps(packet,market_resolution)+unresolved)
    if not unresolved:raise ValueError('deadline edition must disclose incomplete sections')
    payload['report_markdown']=render_provisional_markdown(payload)
    payload['report_delivery']={'source_field':'report_markdown','sha256':report_markdown_sha256(payload['report_markdown'])}
    return validate_payload(payload).payload

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--packet',type=Path,required=True)
    p.add_argument('--report-date-jst',type=date.fromisoformat,required=True)
    p.add_argument('--market-status',choices=('open','holiday_or_weekend'),required=True)
    p.add_argument('--progress',type=Path,required=True)
    p.add_argument('--verified-updates',type=Path,required=True)
    p.add_argument('--unresolved',type=Path,required=True)
    p.add_argument('--market-resolution-evidence',type=Path,
                   help='hash-checked evidence when an archived provisional packet blocker was resolved')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.output.exists():raise ValueError('use a new version; never overwrite a published payload')
    load=lambda path:json.loads(path.read_text(encoding='utf-8-sig'))
    result=build_deadline(load(a.packet),a.report_date_jst,a.market_status,load(a.progress),load(a.verified_updates),load(a.unresolved),
                          load(a.market_resolution_evidence) if a.market_resolution_evidence else None)
    atomic_write_json(a.output,result)
    print(json.dumps({'status':result['quality_status'],'counts':result['review_progress']['counts'],'output':str(a.output)},ensure_ascii=True))
if __name__=='__main__':main()
