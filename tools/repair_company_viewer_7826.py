"""Scoped 7826 source replay. No financial values are embedded in this tool.

Re-extract authenticated original artifacts, preview exact existing-key changes,
and apply through the shared canonical and SQLite writers. Unrelated PL,
forecasts, dividends, memos and notifications are never touched.
"""
import argparse, hashlib, json, re, sqlite3, zipfile
from collections import defaultdict
from pathlib import Path
from dataclasses import asdict
from bs4 import BeautifulSoup
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
from src.segment.profit_basis import segment_flags, profit_label
from lib.pipeline.canonical_writer import expand_segments_rows, write_segments_canonical
from lib.pipeline.db import get_supabase_write_config
from lib.production_environment import bootstrap_production_write_environment
from tools.repair_company_viewer_6623 import _settings_value
from src.migration.migration_db import MigrationDB
import psycopg2
from psycopg2.extras import RealDictCursor


def extract_sources(manifest):
    result=[]
    for src in manifest:
        path=Path(src['zip_path'])
        assert hashlib.sha256(path.read_bytes()).hexdigest()==src['zip_sha256']
        assert hashlib.sha256(Path(src['pdf_path']).read_bytes()).hexdigest()==src['pdf_sha256']
        meta={}
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.endswith(('.htm','.html')):continue
                soup=BeautifulSoup(z.read(name),'html.parser')
                for tag in soup.find_all('ix:nonnumeric'):
                    key=str(tag.get('name') or '').split(':')[-1]
                    if key in ('CurrentFiscalYearEndDateDEI','SecurityCodeDEI','DocumentName'):
                        meta[key]=tag.get_text(strip=True)
        assert meta['SecurityCodeDEI']=='78260'
        fy=meta['CurrentFiscalYearEndDateDEI']
        extracted=extract_segments_from_xbrl_zip_detailed(str(path),period=fy,include_context_evidence=True)
        assert extracted.status=='success_with_rows'
        by_period=defaultdict(list)
        for row in extracted.segments:
            assert row.profit is not None and row.raw_json['profit_basis']=='gross_profit'
            assert row.raw_json['_context_evidence']['date_guard_status']=='PASS'
            by_period[row.period].append(row)
        for period,rows in by_period.items():
            assert len(rows)==6 and len({r.raw_segment_name for r in rows})==6
            result.append(dict(source=src,period=period,quarter=rows[0].quarter,rows=rows,
                internal_id=src['disclosed_date'].replace('-','')+'378260'))
    return result


def equivalent(old,new):
    keys=('ticker','period','quarter','segment_name','segment_key','metric','value','unit','source','filing_id','flags')
    return all((float(old[k])==float(new[k]) if k=='value' else old.get(k)==new.get(k)) for k in keys)


def prepare(existing, sources):
    groups=defaultdict(list);removals=[]
    for row in existing:
        if row['period'].endswith('-03-31') or row['segment_key']=='total of reportable segments and others member':
            # Only proven historical misbindings/aggregate rows from this issuer.
            assert row['source']=='backfill_xbrl'
            if row['period'].endswith('-03-31'):
                assert row['filing_id']=='811e59385bcd830537991cf1' and row['quarter']=='1Q'
            removals.append(row);continue
        if row['source'] not in ('xbrl','backfill_xbrl'):continue
        groups[(row['period'],row['quarter'],row['source'],row['filing_id'])].append(row)
    batches=[];changes=[];maintained=0
    for (period,q,source,fid),old_rows in groups.items():
        candidates=[s for s in sources if s['period']==period and s['quarter']==q]
        exact=[s for s in candidates if fid in (s['source']['disclosure_id'],s['internal_id'])]
        chosen=max(exact or candidates,key=lambda s:s['source']['disclosed_date'])
        by_name={r.raw_segment_name:r for r in chosen['rows']}
        segments=[]
        old_by_key={r['source_row_key']:r for r in old_rows}
        names={r['segment_name'] for r in old_rows}
        for name in sorted(names):
            row=by_name[name]
            flags=segment_flags(row.raw_json)
            flags.update(source_zip_sha256=chosen['source']['zip_sha256'],source_pdf_sha256=chosen['source']['pdf_sha256'],source_url=chosen['source']['pdf_url'],disclosure_id=chosen['source']['disclosure_id'])
            seg=dict(segment_name=name,sales=row.sales,profit=row.profit,flags=flags)
            segments.append(seg)
        args=dict(ticker='7826',period=period,quarter=q,segments=segments,source=source,filing_id=fid,unit='millions_jpy')
        expanded,_=expand_segments_rows(**args)
        changed=[]
        for row in expanded:
            old=old_by_key.get(row['source_row_key'])
            if old and float(old['value'])!=float(row['value']):
                raise ValueError('non-null value conflict: '+row['source_row_key'])
            if old and equivalent(old,row):maintained+=1;continue
            changes.append(dict(key=row['source_row_key'],old=old,new=row,action='add' if old is None else 'metadata'))
            changed.append(row['segment_name'])
        if changed:
            args['segments']=[s for s in segments if s['segment_name'] in changed]
            batches.append(args)
    return dict(batches=batches,changes=changes,removals=removals,maintained=maintained)


def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--settings-root',type=Path,required=True);p.add_argument('--state-root',type=Path,required=True);p.add_argument('--report',type=Path,required=True);p.add_argument('--apply',action='store_true');p.add_argument('--expected-plan-sha256');a=p.parse_args()
    bootstrap_production_write_environment(a.settings_root)
    sources=extract_sources(json.loads(a.manifest.read_text(encoding='utf-8')))
    conn=psycopg2.connect(_settings_value(a.settings_root))
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("select * from canonical_segments where ticker='7826' order by id")
        old=[dict(r) for r in cur.fetchall()]
        plan=prepare(old,sources)
        cur.execute("select * from api_latest_segments where ticker='7826' order by period,quarter,segment_key")
        plan['api_before']=[dict(r) for r in cur.fetchall()]
    # Hash semantic changes, excluding the regenerated update timestamp.
    semantic=[(r['key'],r['new']['value'],r['new']['flags']) for r in plan['changes']]
    digest=hashlib.sha256(json.dumps([semantic,[r['id'] for r in plan['removals']]],sort_keys=True,default=str).encode()).hexdigest()
    plan['plan_sha256']=digest
    plan['summary']={kind:sum(r['action']==kind for r in plan['changes']) for kind in ('add','metadata')}
    plan['summary'].update(remove=len(plan['removals']),maintain=plan['maintained'])
    a.report.parent.mkdir(parents=True,exist_ok=True)
    a.report.write_text(json.dumps(plan,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'summary':plan['summary'],'plan_sha256':digest}))
    if not a.apply:conn.close();return
    assert a.expected_plan_sha256==digest,'dry-run changed; review new report'
    config=get_supabase_write_config();assert config
    for batch in plan['batches']:
        result=write_segments_canonical(**batch,config=config)
        assert result['errors']==0,result
    # Keep existing wide storage in sync with the same parsed source values.
    with conn.cursor() as cur:
        for source in sources:
            for row in source['rows']:
                cur.execute("update segment_canonical set profit=%s where ticker='7826' and period=%s and quarter=%s and segment_name=%s and profit is null and sales=%s",(row.profit,row.period,row.quarter,row.raw_segment_name,row.sales))
        ids=[r['id'] for r in plan['removals']]
        if ids:cur.execute("delete from canonical_segments where ticker='7826' and id=any(%s)",(ids,))
        cur.execute("delete from segment_canonical where ticker='7826' and (period in ('2025-03-31','2026-03-31') or segment_key='total of reportable segments and others member')")
    conn.commit()
    # Use existing provenance-aware writer on existing local rows only.
    db=MigrationDB.__new__(MigrationDB);db._db_path=str(a.state_root/'decision_db.db');db._conn=sqlite3.connect(db._db_path,timeout=30)
    columns=[r[1] for r in db._conn.execute('pragma table_info(segment_financials)')]
    local=[dict(zip(columns,r)) for r in db._conn.execute("select * from segment_financials where company_code='7826' and data_source='backfill_xbrl'")]
    local_results=[]
    for old_row in local:
        period=old_row['fiscal_year_end'];name=old_row['segment_name']
        if period.endswith('-03-31') or name=='Total Of Reportable Segments And Others Member':
            db._conn.execute("delete from segment_financials where id=? and company_code='7826'",(old_row['id'],));continue
        cand=[s for s in sources if s['period']==period and s['quarter']==old_row['quarter'] and s['internal_id']==old_row['tdnet_doc_id']]
        assert len(cand)==1,(period,name)
        row=next(r for r in cand[0]['rows'] if r.raw_segment_name==name)
        assert old_row['segment_sales']==row.sales and old_row['segment_profit'] in (None,row.profit)
        args={k:old_row[k] for k in ('company_code','fiscal_year_end','quarter','segment_name','segment_order','data_source','segment_name_norm','extractor_route','source_doc_type','disclosure_date','tdnet_doc_id','row_type')}
        args.update(segment_sales=row.sales,segment_profit=row.profit,raw_profit_label=profit_label(row.raw_json['profit_tag']))
        result=db.upsert_segment_provenance_aware(**args);assert result.accepted,result
        local_results.append(asdict(result))
    db.commit();db.close()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("select * from canonical_segments where ticker='7826' order by id")
        after=prepare([dict(r) for r in cur.fetchall()],sources)
        assert not after['changes'] and not after['removals'],'second replay must be idempotent'
        cur.execute("select * from api_latest_segments where ticker='7826' order by period,quarter,segment_key")
        readback=[dict(r) for r in cur.fetchall()]
        assert len(readback)==84 and all(r['profit'] is not None for r in readback)
    plan['local_results']=local_results;plan['api_after']=readback;plan['second_replay_changes']=len(after['changes'])
    a.report.write_text(json.dumps(plan,ensure_ascii=False,indent=2,default=str),encoding='utf-8');conn.close()

if __name__=='__main__':main()
