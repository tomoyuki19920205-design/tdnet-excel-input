"""Real TDnet regression: gross profit is the issuer's segment measure."""
from pathlib import Path
import copy
import pytest
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
from src.analysis.segment_detection_v4 import run_segment_detection_v4
from src.segment.profit_basis import segment_flags
from lib.pipeline.canonical_writer import expand_segments_rows
from lib.backfill.segment_partial_check import check_xbrl_partial_segments
from src.migration.migration_db import MigrationDB

FIX=Path(__file__).parent/'fixtures/furuya_7826'
NAMES=['Electronics','Thin Film','Thermal','Fine Chemicals Recycling','Supply Chain Support','Other']

@pytest.mark.parametrize('doc,fy,q,previous,current',[
 ('20261105565722','2027-06-30','1Q',[320,1262,408,934,449,1076],[4837,3533,821,4160,1053,1]),
 ('20260203546192','2026-06-30','2Q',[1251,2055,765,3764,-1,36],[1076,2878,860,2489,716,1532]),
 ('20250130558889','2025-06-30','2Q',[1273,1775,1299,1986,273,5],[1251,2055,765,3764,-1,36]),
])
def test_real_standard_and_company_gross_profit(doc,fy,q,previous,current):
    result=extract_segments_from_xbrl_zip_detailed(str(FIX/(doc+'.zip')),period=fy,quarter=q,include_context_evidence=True)
    assert result.status=='success_with_rows'
    assert len(result.segments)==12
    for role,expected in [('current',current),('previous',previous)]:
        rows=[r for r in result.segments if r.raw_json['_segment_period_role']==role]
        assert {r.raw_segment_name:r.profit for r in rows}==dict(zip(NAMES,expected))
        assert all(r.raw_json['profit_basis']=='gross_profit' for r in rows)
        assert all(r.raw_json['_context_evidence']['context_start'].endswith('-07-01') for r in rows)
    if q=='1Q':
        # Six truncated components need not equal the independently reported total.
        assert sum(current)==14405 and 14408-sum(current)<len(current)

def test_pdf_keeps_short_verified_headers_and_basis():
    result=run_segment_detection_v4(str(FIX/'20261105565722.pdf'),ticker='7826')
    assert result.success
    assert len(result.segments)==6
    assert [r.segment_profit for r in result.segments]==[4837,3533,821,4160,1053,1]
    assert all('売上総利益ベース' in r.raw_profit_label for r in result.segments)

def test_missing_profit_triggers_pdf_even_with_six_sales():
    rows=[{'period':'2027-06-30','segment_name':n,'segment_sales':1,'segment_profit':None} for n in NAMES]
    assert check_xbrl_partial_segments(rows,'7826','2027-06-30')[0]
    for r in rows:r['segment_profit']=0
    assert not check_xbrl_partial_segments(rows,'7826','2027-06-30')[0]

def test_eav_no_null_overwrite_and_order_convergence():
    def expand(profit):
        return expand_segments_rows(ticker='78260',period='2027-06-30',quarter='1Q',source='xbrl',filing_id='same',unit='millions_jpy',segments=[{'segment_name':'Other','sales':15,'profit':profit,'profit_basis':'gross_profit'}])[0]
    complete=expand(0);partial=expand(None)
    assert len(complete)==2 and len(partial)==1
    states=[]
    for order in [(complete,partial),(partial,complete)]:
        saved={}
        for rows in order:
            for r in rows:saved[r['source_row_key']]=(r['value'],r['flags'])
        states.append(saved)
    assert states[0]==states[1]
    assert any(v==(0,{'profit_basis':'gross_profit'}) for v in states[0].values())

def test_sqlite_partial_same_filing_preserves_profit(tmp_path):
    db=MigrationDB(str(tmp_path/'segments.db'))
    args=dict(company_code='7826',fiscal_year_end='2027-06-30',quarter='1Q',segment_name='Other',segment_order=6,segment_sales=15,segment_profit=-1,raw_profit_label='セグメント利益（売上総利益ベース）',data_source='backfill_xbrl',extractor_route='xbrl',source_doc_type='earnings_summary',disclosure_date='2026-11-06',tdnet_doc_id='20261106378260',row_type='segment')
    assert db.upsert_segment_provenance_aware(**args).accepted
    args.update(segment_profit=None,raw_profit_label='')
    assert db.upsert_segment_provenance_aware(**args).status=='no_change'
    assert db.get_segments('7826','2027-06-30','1Q')[0]['segment_profit']==-1
    args.update(tdnet_doc_id='another-version')
    assert not db.upsert_segment_provenance_aware(**args).accepted
    db.close()
