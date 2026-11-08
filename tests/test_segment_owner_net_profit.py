from pathlib import Path
import datetime
import pytest
from bs4 import BeautifulSoup
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed,_extract_ixbrl_segment_data
from src.segment.profit_basis import profit_basis,segment_flags
from src.segment.semantic_facts import metric_from_label

FIX=Path(__file__).parent/'fixtures/segment_partials/2768_owner_net_profit.zip'

def test_actual_owner_net_income_keeps_original_business_dimension_and_basis():
 result=extract_segments_from_xbrl_zip_detailed(str(FIX),period='2025-03-31',quarter='1Q',include_context_evidence=True)
 assert len(result.segments)==16
 assert all(r.profit is not None for r in result.segments)
 rows={(r.period,r.raw_segment_name):r for r in result.segments}
 prior=rows['2024-03-31','Automotive'];current=rows['2025-03-31','Automotive']
 assert (prior.sales,prior.profit)==(94913,1646)
 assert (current.sales,current.profit)==(114805,-65)
 for row in result.segments:
  flags=segment_flags(row.raw_json)
  assert flags['profit_basis']=='profit_attributable_to_owners'
  assert '親会社の所有者に帰属' in flags['profit_label'].replace(' ','')
  assert row.raw_json['profit_fact']['column']['role']=='segment'
  context=row.raw_json['profit_fact']['context']
  assert 'ReportableSegmentMember' in context or 'OperatingSegmentsNotIncludedInReportableSegments' in context

@pytest.mark.parametrize('label,basis',[
 ('四半期純利益（親会社の所有者に帰属）','profit_attributable_to_owners'),
 ('親会社株主に帰属する当期純利益','profit_attributable_to_owners'),
 ('中間純損失（△）','net_profit'),
 ('当期純利益','net_profit'),
])
def test_net_income_measure_label(label,basis):
 assert metric_from_label(label)=='profit'
 assert profit_basis(label)==basis

@pytest.mark.parametrize('label',['1株当たり四半期純利益','当期純利益率','親会社の所有者に帰属する四半期純利益増減額','非支配株主に帰属する当期純利益','親会社株主に帰属する当期純利益予想'])
def test_components_rates_forecasts_are_not_segment_profit(label):
 assert metric_from_label(label) is None

def test_consolidated_owner_fact_is_not_distributed_to_segments():
 soup=BeautifulSoup('<table><tr><td>四半期純利益（親会社の所有者に帰属）</td><td><ix:nonfraction name="jpigp_cor:ProfitLossAttributableToOwnersOfParentIFRS" contextref="CurrentYTDDuration" unitref="JPY" scale="6">22140</ix:nonfraction></td></tr></table>','html.parser')
 result=_extract_ixbrl_segment_data(soup,'IFRS','1Q',{'CurrentYTDDuration':{'type':'duration','start':'2024-04-01','end':'2024-06-30','duration_days':90,'explicit_members':[]}},datetime.date(2024,6,30))
 assert result=={}
