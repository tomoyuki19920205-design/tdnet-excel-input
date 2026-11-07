import zipfile
from pathlib import Path
from bs4 import BeautifulSoup
from src.segment.semantic_facts import column_evidence,disclosed_metric
from src.segment.edinet_segment_extractor import _classify_member,classify_concept,extract_edinet_segments
FIX=Path(__file__).parent/'fixtures/segment_partials'

def facts(ticker,member,tag):
    with zipfile.ZipFile(FIX/f'reconciliation_{ticker}.zip') as source:
        for name in source.namelist():
            if not name.endswith('.htm'):continue
            soup=BeautifulSoup(source.read(name),'html.parser')
            for fact in soup.find_all('ix:nonfraction'):
                if member in str(fact.get('contextref')) and str(fact.get('name')).endswith(tag):
                    yield fact

def test_actual_intersegment_elimination_does_not_backtrack_to_section_heading():
    rows=list(facts('7752','IntersegmentMember','NetSalesIFRS'))
    assert rows
    assert all(column_evidence(f)['role']=='adjustment_or_consolidated' for f in rows)
    assert all(column_evidence(f)['label']=='セグメント間取引' for f in rows)
    assert _classify_member('jpcrp030000-asr_E02275-000IntersegmentMember')=='adjustment'

def test_actual_equity_profit_component_is_not_a_segment_profit_measure():
    rows=list(facts('8002','LifestyleReportableSegmentMember','ShareOfProfitLossOfInvestmentsAccountedForUsingEquityMethodIFRS'))
    assert rows
    for fact in rows:
        assert column_evidence(fact)['label'].replace(' ','')=='ライフスタイル'
        metric,label=disclosed_metric(fact)
        assert metric is None and label=='持分法による投資損益'
        assert classify_concept(fact['name'],label)[0]=='other'

def test_nested_internal_revenue_still_resolves_actual_business():
    soup=BeautifulSoup('''<table><tr><td></td><td>当連結会計年度</td></tr>
    <tr><td>デジタルサービス</td><td></td></tr>
    <tr><td>セグメント間の内部売上高</td><td><ix:nonfraction name="x:Sales" contextref="x" unitref="JPY">100</ix:nonfraction></td></tr></table>''','html.parser')
    result=column_evidence(soup.find('ix:nonfraction'))
    assert result['label']=='デジタルサービス' and result['role']=='segment'
