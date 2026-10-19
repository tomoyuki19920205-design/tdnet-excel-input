"""Guarded, explicit notification-ID reason repair. No event or Discord replay."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def build_patch(before: dict, plan: dict) -> dict:
    assert before['id']==plan['id'] and before['dedupe_key']==plan['dedupe_key']
    assert before['event_type'] in ('forecast','forecast_revision')
    assert before['source_url']==plan['source_url']==plan['reason']['source_url']
    assert digest(before['raw_payload'])==plan['raw_payload_before_sha256'], 'payload changed since review'
    raw=json.loads(before['raw_payload']) if isinstance(before['raw_payload'],str) else copy.deepcopy(before['raw_payload'])
    assert isinstance(raw.get('extracted'),dict)
    reason=plan['reason']
    existing=raw['extracted'].get('revision_reason')
    if existing:
        assert digest(existing)==plan.get('expected_existing_reason_sha256'), 'existing reason must not be overwritten without an exact reviewed hash'
        assert (existing.get('status')=='unavailable'
                or (existing.get('status')=='available' and plan.get('allow_replace_available') is True)), \
            'a usable or verified absent reason must not be replaced without reviewed correction'
        assert existing.get('source_url')==reason['source_url'], 'disclosure source changed'
        if existing.get('pdf_sha256'):
            assert existing['pdf_sha256']==reason['pdf_sha256'], 'original PDF changed'
    assert reason['status']=='available' and reason['version']==1 and reason['sections']
    assert len(reason['pdf_sha256'])==64
    for section in reason['sections']:
        assert section['text'].strip() and section['page_start']>=1 and section['page_end']>=section['page_start']
    raw['extracted']['revision_reason']=reason
    return {'raw_payload': json.dumps(raw,ensure_ascii=False) if isinstance(before['raw_payload'],str) else raw}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--plan',required=True);parser.add_argument('--env-file',required=True)
    parser.add_argument('--audit-dir',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    plans=json.loads(Path(args.plan).read_text(encoding='utf-8'))
    assert 0<len(plans)<=10 and len({p['id'] for p in plans})==len(plans), 'explicit bounded unique IDs required'
    cfg={}
    for line in Path(args.env_file).read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1);cfg[k]=v.strip().strip('"')
    base=cfg['SUPABASE_URL'].rstrip('/');key=cfg['SUPABASE_SERVICE_ROLE_KEY']
    headers={'apikey':key,'Authorization':'Bearer '+key}
    def read(table,params):
        with urlopen(Request(base+'/rest/v1/'+table+'?'+urlencode(params),headers=headers),timeout=30) as response:return json.load(response)
    def event(event_id):
        rows=read('tdnet_events',{'select':'*','id':'eq.'+event_id,'limit':'2'});assert len(rows)==1;return rows[0]
    ids='in.('+','.join(p['id'] for p in plans)+')'
    def preferences():
        return {table:read(table,{'select':'*','event_id':ids,'limit':'1000','order':'id'})
                for table in ('tdnet_event_reads','tdnet_event_stars')}
    protected=preferences();prepared=[]
    for plan in plans:
        before=event(plan['id']);patch=build_patch(before,plan);prepared.append((before,patch))
        print(json.dumps({'id':before['id'],'source_url':before['source_url'],'updated_fields':['raw_payload.extracted.revision_reason'],
                          'sections':len(plan['reason']['sections']),'characters':sum(len(s['text']) for s in plan['reason']['sections'])},ensure_ascii=False))
    audit=Path(args.audit_dir);audit.mkdir(parents=True,exist_ok=True)
    (audit/'before.json').write_text(json.dumps({'rows':[v[0] for v in prepared],'preferences':protected},ensure_ascii=False,indent=2),encoding='utf-8')
    if not args.apply: print('DRY RUN: no writes');return
    after_rows=[]
    for before,patch in prepared:
        assert event(before['id'])==before, 'event changed before apply'
        params={'id':'eq.'+before['id'],'dedupe_key':'eq.'+before['dedupe_key'],
                'raw_payload':'eq.'+json.dumps(before['raw_payload'],ensure_ascii=False,separators=(',',':'))}
        request=Request(base+'/rest/v1/tdnet_events?'+urlencode(params),data=json.dumps(patch,ensure_ascii=False).encode(),method='PATCH',
                        headers={**headers,'Content-Type':'application/json','Prefer':'return=representation'})
        with urlopen(request,timeout=30) as response:written=json.load(response)
        assert len(written)==1, 'compare-and-update did not match one row'
        after=event(before['id'])
        assert after['raw_payload']==patch['raw_payload']
        assert {k:v for k,v in before.items() if k!='raw_payload'}=={k:v for k,v in after.items() if k!='raw_payload'}
        after_rows.append(after)
    assert preferences()==protected, 'read/star state changed during repair'
    (audit/'after.json').write_text(json.dumps({'rows':after_rows,'preferences':protected},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'APPLIED: {len(after_rows)} exact IDs; only reason items changed; all other columns/read/star states preserved')


if __name__=='__main__':main()
