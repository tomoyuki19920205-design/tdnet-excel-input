#!/usr/bin/env python3
"""Publish source-verified progress without discarding pending candidates."""
from pathlib import Path
from datetime import date
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lib.ny_market import render_provisional_markdown,report_markdown_sha256,validate_payload
from lib.ny_market_deadline import CONTRACT,validate_progress,validate_updates,validate_morning_cutoff
from tools.build_ny_market_provisional_payload import build
from tools.company_news_atomic import atomic_write_json

def build_deadline(packet,report_date,market_status,progress,updates,unresolved):
    value,cutoff=validate_progress(progress)
    validate_morning_cutoff(cutoff,report_date)
    validate_updates(updates,cutoff)
    payload=build(packet,report_date,market_status)
    payload.update(publication_contract_version=CONTRACT,review_progress=progress,
                   verified_updates=updates,
                   unresolved_items=list(packet.get('unresolved_items') or [])+unresolved)
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
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.output.exists():raise ValueError('use a new version; never overwrite a published payload')
    load=lambda path:json.loads(path.read_text(encoding='utf-8-sig'))
    result=build_deadline(load(a.packet),a.report_date_jst,a.market_status,load(a.progress),load(a.verified_updates),load(a.unresolved))
    atomic_write_json(a.output,result)
    print(json.dumps({'status':result['quality_status'],'counts':result['review_progress']['counts'],'output':str(a.output)},ensure_ascii=True))
if __name__=='__main__':main()
