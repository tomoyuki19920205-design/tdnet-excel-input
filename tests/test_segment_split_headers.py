import json,zipfile
from pathlib import Path
from bs4 import BeautifulSoup
import pytest
from src.segment.edinet_segment_extractor import extract_edinet_segments
from src.segment.edinet_canonical_bridge import edinet_result_to_canonical_segments
from src.segment.semantic_facts import column_evidence
FIX=Path(__file__).parent/'fixtures/segment_partials'

@pytest.mark.parametrize('doc', ['S100TQEX','S100R3HY','S100V8ZV','S100OD03'])
def test_original_split_headers_period_captions_and_amendment_files(doc):
    meta=next(r for r in json.loads((FIX/'split_header_sources.json').read_text(encoding='utf-8')) if r['doc']==doc)
    result=extract_edinet_segments(str(FIX/(doc+'.zip')),**{k:meta[k] for k in ('ticker','period','quarter','period_role')})
    assert result.status=='ok',result.review_hint
    rows=edinet_result_to_canonical_segments(result)
    assert rows and len({r['segment_name'] for r in rows})==len(rows)
    for row in rows:
        for metric in ('sales','profit'):
            if row.get(metric) is not None:
                column=row['flags'][metric+'_fact']['inline_evidence']['column']
                assert column['role']=='segment'
                assert column['label'] not in ('持分法による投資損益','セグメント間内部収益')
    if doc=='S100TQEX':assert (rows[0]['sales'],rows[0]['profit'])==(236513,1394)
    if doc=='S100V8ZV':
        assert (rows[0]['sales'],rows[0]['profit'])==(18557.812,331.576)
        assert rows[0]['flags']['sales_basis']=='external_customers'
    if doc=='S100OD03':assert rows[0]['segment_name']=='繊維'

def test_unrelated_or_mismatched_preceding_header_cannot_bind_a_business():
    header='<table><tr><td></td><td>報告セグメント</td></tr><tr><td></td><td>電子</td></tr></table>'
    body='<table><tr><td>売上高</td><td><ix:nonfraction name="x:Revenue" contextref="x" unitref="JPY">3</ix:nonfraction></td></tr></table>'
    for text in (header+'<p>別の注記</p>'+body,header+body.replace('<td>売上高</td>','<td>売上高</td><td>4</td>')):
        soup=BeautifulSoup(text,'html.parser');assert column_evidence(soup.find('ix:nonfraction'))['role']=='unresolved'
