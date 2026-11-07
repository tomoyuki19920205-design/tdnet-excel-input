from pathlib import Path
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
FIX=Path(__file__).parent/'fixtures/segment_partials'

def test_month_end_inference_cannot_create_a_second_fiscal_key():
    path=FIX/'exact_fiscal_20261102562230.zip'
    wrong=extract_segments_from_xbrl_zip_detailed(str(path),period='2026-09-30',quarter='FY')
    assert wrong.segments==[]
    assert wrong.reason=='requested_fiscal_year_end_differs_from_original_dei'
    actual=extract_segments_from_xbrl_zip_detailed(str(path),period='2026-09-20',quarter='FY')
    assert actual.status=='success_with_rows'
    assert {r.period for r in actual.segments}=={'2026-09-20','2025-09-20'}

def test_changed_fiscal_year_uses_published_comparative_fiscal_end():
    path=FIX/'exact_fiscal_20260513529754.zip'
    result=extract_segments_from_xbrl_zip_detailed(str(path),period='2026-03-31',quarter='FY',include_context_evidence=True)
    assert result.status=='success_with_rows'
    assert {r.period for r in result.segments}=={'2026-03-31','2024-12-31'}
    previous=[r for r in result.segments if r.period=='2024-12-31']
    assert previous and all(r.raw_json['_context_evidence']['current_or_previous']=='previous' for r in previous)

def test_comparative_leap_day_is_not_replaced_by_february_28():
    path=next(FIX.glob('exact_fiscal_2025*.zip'))
    result=extract_segments_from_xbrl_zip_detailed(str(path),period='2025-02-28',quarter='3Q')
    assert result.status=='success_with_rows'
    assert {r.period for r in result.segments}=={'2025-02-28','2024-02-29'}
