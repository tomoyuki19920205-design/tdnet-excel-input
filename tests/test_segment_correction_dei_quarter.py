import zipfile
from pathlib import Path
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed

FIX=Path(__file__).parent/'fixtures/segment_partials'
SOURCE=FIX/'correction_dei_20260409500519.zip'

def test_corrected_filing_without_document_title_uses_original_dei():
    result=extract_segments_from_xbrl_zip_detailed(str(SOURCE),period='2026-01-31',quarter='FY',include_context_evidence=True)
    assert result.status=='success_with_rows'
    row=next(r for r in result.segments if r.period=='2026-01-31' and r.raw_segment_name=='Condominiums Business For Residence')
    assert (row.sales,row.profit)==(6287.001,712.428)
    assert row.raw_json['_context_evidence']['date_guard_status']=='PASS'

def test_original_dei_cannot_be_replaced_by_requested_quarter():
    result=extract_segments_from_xbrl_zip_detailed(str(SOURCE),period='2026-01-31',quarter='3Q')
    assert result.status=='quarter_unresolved' and not result.segments

def test_conflicting_original_quarters_do_not_enable_title_fallback(tmp_path):
    target=tmp_path/'conflicting_dei.zip'
    with zipfile.ZipFile(SOURCE) as source,zipfile.ZipFile(target,'w') as output:
        for entry in source.infolist():
            payload=source.read(entry.filename);output.writestr(entry,payload)
            if entry.filename.endswith(('.htm','.html')) and b'TypeOfCurrentPeriodDEI' in payload:
                import re
                changed=re.sub(rb'(TypeOfCurrentPeriodDEI[^>]*>)FY(<)',rb'\g<1>Q3\2',payload)
                if changed!=payload:output.writestr(entry.filename.replace('.htm','_conflict.htm'),changed)
    result=extract_segments_from_xbrl_zip_detailed(str(target),period='2026-01-31',quarter='FY')
    assert result.status=='quarter_unresolved' and not result.segments

def test_thousand_yen_correction_keeps_exact_profit():
    result=extract_segments_from_xbrl_zip_detailed(str(FIX/'correction_dei_20250617592021.zip'),period='2023-03-31',quarter='FY')
    row=next(r for r in result.segments if r.period=='2023-03-31' and r.raw_segment_name=='警備事業')
    assert (row.sales,row.profit)==(19802.965,459.435)
