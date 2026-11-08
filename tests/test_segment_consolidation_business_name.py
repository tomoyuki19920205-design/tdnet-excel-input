from pathlib import Path
import pytest
from src.segment.semantic_facts import is_nonconsolidated_context
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
from src.segment.edinet_segment_extractor import extract_edinet_segments

FIX = Path(__file__).parent / 'fixtures/segment_partials'


def test_business_member_is_not_a_financial_statement_scope():
    member = 'tse-acedjpfr-34330:ThermalSprayProcessingNonConsolidatedReportableSegmentMember'
    dims = [{'dimension':'jpcrp_cor:OperatingSegmentsAxis', 'member':member}]
    assert not is_nonconsolidated_context('CurrentYearDuration_'+member.replace(':',''), dims)
    assert is_nonconsolidated_context('CurrentYearDuration_NonConsolidatedMember_BusinessMember')
    assert is_nonconsolidated_context('CurrentYearDuration_NonConsolidated')
    assert not is_nonconsolidated_context('CurrentYearDuration_ConsolidatedMember')


def test_formal_scope_dimension_is_authoritative():
    dim = {'axis':'jppfs_cor:ConsolidatedOrNonConsolidatedAxis', 'member':'jppfs_cor:NonConsolidatedMember'}
    assert is_nonconsolidated_context('opaque_context_1', [dim])
    consolidated = {**dim, 'member':'jppfs_cor:ConsolidatedMember'}
    assert not is_nonconsolidated_context('CurrentYearDuration_NonConsolidatedMember', [consolidated])
    with pytest.raises(ValueError, match='conflicting'):
        is_nonconsolidated_context('opaque_context_1', [dim, consolidated])


def test_real_tdnet_parent_business_stays_in_consolidated_segment_table():
    r = extract_segments_from_xbrl_zip_detailed(str(FIX/'consolidation_business_20260511522245.zip'), period='2026-03-31', quarter='FY')
    assert r.status == 'success_with_rows'
    assert len(r.segments) == 8
    assert all(x.is_consolidated and x.raw_json['is_consolidated'] for x in r.segments)
    parent = [x for x in r.segments if x.raw_segment_name == 'Thermal Spray Processing Non Consolidated']
    assert [(x.period,x.sales,x.profit) for x in parent] == [('2025-03-31',40447,8868),('2026-03-31',41873,8802)]


def test_real_edinet_does_not_drop_parent_business_from_consolidated_table():
    r = extract_edinet_segments(str(FIX/'consolidation_business_S100W0IW.zip'), ticker='3433', period='2025-03-31', quarter='FY')
    assert r.status == 'ok'
    assert len(r.segments) == 4
    parent = [x for x in r.segments if '溶射加工' in x.segment_name_raw]
    assert len(parent) == 1
    assert (parent[0].sales,parent[0].profit) == (39213,8868)
    assert all(x.metadata['is_consolidated'] for x in r.segments)
