"""Original 4755 regression plus boundaries for subordinate metric labels."""
import json
from pathlib import Path
import pytest
from bs4 import BeautifulSoup
from src.segment import semantic_facts, xbrl_segment_extractor
S=Path(__file__).resolve().parent/'fixtures'/'segment_partials'

@pytest.fixture
def modules():
    return semantic_facts, xbrl_segment_extractor

def test_original_contribution_after_is_the_disclosed_segment_result(modules):
 semantic,extractor=modules
 expected=json.loads((S/'4755_contribution_202509.json').read_text(encoding='utf8'))
 result=extractor.extract_segments_from_xbrl_zip_detailed(str(S/'4755_contribution_202509.zip'),period='2025-12-31',quarter='3Q',include_context_evidence=True)
 assert result.status=='success_with_rows',result.reason
 rows={(r.period,r.raw_segment_name):r for r in result.segments}
 for period,role in [('2025-12-31','current'),('2024-12-31','previous')]:
  for name,profit in expected['expected_profit_'+role].items():
   row=rows[period,name];assert row.profit==profit
   assert row.raw_json['profit_basis']=='adjusted_operating_profit'
   assert row.raw_json['raw_profit_label']=='セグメント損益（モバイルエコシステム貢献額考慮後）'
   assert row.raw_json['profit_basis_policy_evidence']['original_policy_text']
   assert row.raw_json['profit_tag'].endswith(':afterconsideringifrs')
 assert rows['2025-12-31','Fin Tech'].sales==706780

def table(heading='セグメント損益',contribution='相互貢献額',before='考慮前',context='CurrentDuration_AReportableSegmentMember'):
 def fact(value,ctx=context):return f'<ix:nonfraction name="issuer:Measure" contextref="{ctx}" unitref="JPY" scale="6">{value}</ix:nonfraction>'
 return BeautifulSoup('<table><tr><th></th><th>事業A</th></tr>'+f'<tr><td>{heading}</td><td></td></tr><tr><td>{before}</td><td>{fact(10)}</td></tr><tr><td>{contribution}</td><td>{fact(2)}</td></tr><tr><td>考慮後</td><td>{fact(12)}</td></tr></table>','html.parser')

@pytest.mark.parametrize('heading,contribution,before',[('税引前利益','相互貢献額','考慮前'),('資産','相互貢献額','考慮前'),('セグメント損益','売上高','考慮前'),('セグメント損益','相互貢献額','予想')])
def test_bare_or_unrelated_subordinate_labels_are_not_profit(modules,heading,contribution,before):
 semantic,_=modules;soup=table(heading,contribution,before);tag=soup.find_all('ix:nonfraction')[-1]
 assert semantic.disclosed_metric(tag)[0] is None

def test_contribution_itself_is_not_profit_and_missing_policy_is_unknown(modules):
 semantic,_=modules;soup=table();tags=soup.find_all('ix:nonfraction')
 assert semantic.disclosed_metric(tags[1])[0] is None
 assert semantic.disclosed_metric(tags[2])==('profit','セグメント損益（相互貢献額考慮後）')
 assert semantic.contribution_profit_policy(soup)=={}
 tags[0]['contextref']='DifferentBusinessContext'
 assert semantic.disclosed_metric(tags[2])[0] is None

def test_non_gaap_policy_must_explicitly_bind_the_same_contribution(modules):
 semantic,_=modules;label='セグメント損益（相互貢献額考慮後）'
 soup=BeautifulSoup('<p>社内指標はNon-GAAP営業利益ベースです。</p>','html.parser')
 assert semantic.contribution_profit_policy(soup,label)=={}
 soup=BeautifulSoup('<p>社内指標はNon-GAAP営業利益ベースです。</p><p>相互貢献をセグメント損益に反映しています。</p>','html.parser')
 assert semantic.contribution_profit_policy(soup,label)['basis']=='adjusted_operating_profit'
 assert semantic.contribution_profit_policy(soup,'セグメント損益（別の貢献額考慮後）')=={}
