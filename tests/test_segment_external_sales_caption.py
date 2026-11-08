import json
from pathlib import Path
import pytest
from bs4 import BeautifulSoup
from src.segment import semantic_facts
S=Path(__file__).resolve().parent/'fixtures'/'segment_partials'

@pytest.fixture
def semantic():
    return semantic_facts

def test_original_external_sales_keep_period_business_and_profit(semantic):
 from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
 expected=json.loads((S/'9432_external_caption_202609.json').read_text(encoding='utf8'));baseline=json.loads((S/'9432_external_caption_202609_baseline.json').read_text(encoding='utf8'))
 result=extract_segments_from_xbrl_zip_detailed(str(S/'9432_external_caption_202609.zip'),period='2027-03-31',quarter='2Q',include_context_evidence=True)
 assert result.status=='success_with_rows';rows={(r.period,r.raw_segment_name):r for r in result.segments};assert len(rows)==8
 for period,key in [('2027-03-31','current_external_sales'),('2026-03-31','previous_external_sales')]:
  for name,value in expected[key].items():
   row=rows[period,name];assert row.sales==value
   assert row.raw_json['sales_basis']=='external_customers'
   assert row.raw_json['raw_sales_label']=='営業収益（外部顧客に対するもの）'
 for old in baseline:assert rows[old['period'],old['name']].profit==old['profit']

@pytest.mark.parametrize('prefix,expected',[
 ('<p>営業収益：</p><div>（単位：百万円）</div><p></p>','営業収益'),
 ('<p>営業収益：</p><p>別の情報</p>',None),
 ('<p>営業費用：</p>',None),
 ('<p>営業収益：</p><table><tr><td>別表</td></tr></table>',None),
 ('<p>売上高：</p><div><ix:nonfraction>1</ix:nonfraction></div>',None),
 ('',None),
])
def test_external_caption_cannot_cross_unrelated_content(semantic,prefix,expected):
 soup=BeautifulSoup(prefix+'<div><table id="target"><tr><td>事業A</td><td>1</td></tr></table></div>','html.parser')
 assert semantic._adjacent_sales_caption(soup.find(id='target'))==expected
