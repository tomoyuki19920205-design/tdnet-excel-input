import zipfile
from pathlib import Path
from bs4 import BeautifulSoup
from src.segment.semantic_facts import column_evidence,column_role
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
FIX=Path(__file__).parent/'fixtures/segment_partials'

def test_new_leaf_headers_replace_previous_half_table_headers():
    with zipfile.ZipFile(FIX/'wrapped_header_5020.zip') as source:
        name=next(n for n in source.namelist() if 'acsg03' in n)
        soup=BeautifulSoup(source.read(name),'html.parser')
    total=soup.find('ix:nonfraction',attrs={'contextref':'CurrentYearDuration_ReportableSegmentsMember','name':'jpigp_cor:NetSalesIFRS'})
    assert column_evidence(total)['role']=='total'
    assert '合計' in column_evidence(total)['label']
    renewable=soup.find('ix:nonfraction',attrs={'contextref':'CurrentYearDuration_tse-acediffr-50200RenewableEnergyReportableSegmentsMember','name':'jpigp_cor:NetSalesIFRS'})
    assert column_evidence(renewable)['label'].replace(' ','')=='再生可能エネルギー'

def test_total_cannot_be_adopted_as_an_oil_and_gas_business():
    result=extract_segments_from_xbrl_zip_detailed(str(FIX/'wrapped_header_5020.zip'),period='2025-03-31',quarter='FY')
    current=[r for r in result.segments if r.period=='2025-03-31']
    assert len(current)==6
    assert all(r.sales!=11933486 and r.profit!=58437 for r in current)
    oil=next(r for r in current if 'OilAndNaturalGas' in r.raw_json['_context_evidence']['context_ref']) if current and '_context_evidence' in current[0].raw_json else next(r for r in current if 'Oil And Natural' in r.raw_segment_name)
    assert (oil.sales,oil.profit)==(242813,87407)

def test_generic_member_remains_valid_when_original_leaf_is_real_business():
    result=extract_segments_from_xbrl_zip_detailed(str(FIX/'generic_business_9535.zip'),period='2027-03-31',quarter='1Q',include_context_evidence=True)
    row=next(r for r in result.segments if r.period=='2027-03-31' and r.raw_segment_name=='その他事業')
    assert (row.sales,row.profit)==(994,49)
    assert row.raw_json['segment_identity_method']=='xhtml_column_and_peer_dimensions'

def test_reportable_total_label_is_not_a_business():
    assert column_role('報告セグメント 合計')=='total'

def test_spanning_total_group_keeps_real_business_children():
    with zipfile.ZipFile(FIX/'spanning_group_9227.zip') as source:
        name=next(n for n in source.namelist() if '/PublicDoc/0105010_honbun' in n and n.endswith('.htm'))
        soup=BeautifulSoup(source.read(name),'html.parser')
    fact=next(t for t in soup.find_all('ix:nonfraction') if 'MicrowaveSolutionBusinessMemberReportableSegmentMember' in str(t.get('contextref')) and str(t.get('name')).endswith('RevenuesFromExternalCustomers'))
    column=column_evidence(fact)
    assert column['role']=='segment' and column['label']=='マイクロ波化学関連事業'

def test_spanning_reconciliation_group_keeps_actual_other_business():
    with zipfile.ZipFile(FIX/'spanning_group_8051.zip') as source:
        name=next(n for n in source.namelist() if '/PublicDoc/0105010_honbun' in n and n.endswith('.htm'))
        soup=BeautifulSoup(source.read(name),'html.parser')
    fact=next(t for t in soup.find_all('ix:nonfraction') if str(t.get('contextref'))=='Prior1YearDuration_OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember' and str(t.get('name')).endswith('RevenuesFromExternalCustomers'))
    column=column_evidence(fact)
    assert column['role']=='segment' and column['label']=='その他 (注)１'
