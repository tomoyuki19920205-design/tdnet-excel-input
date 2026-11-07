import pytest
from lib.pipeline.canonical_writer import expand_segments_rows

def expand(segments):
    return expand_segments_rows(ticker='157A',period='2026-06-30',quarter='FY',segments=segments,source='edinet_xbrl',filing_id='S100Z4OA',unit='millions_jpy')

def test_conflicting_normalized_cells_are_not_silently_last_wins():
    with pytest.raises(ValueError,match='conflicting business measures'):
        expand([{'segment_name':'Other','sales':707.711},{'segment_name':'Other ','sales':12.754}])

def test_same_amount_different_business_members_are_not_merged():
    with pytest.raises(ValueError,match='conflicting business measures'):
        expand([{'segment_name':'Other','sales':0,'flags':{'source_member':m}} for m in ('BusinessA','BusinessB')])

def test_exact_repeat_and_complementary_metrics_preserve_values():
    rows,_=expand([{'segment_name':'電子','sales':0},{'segment_name':'電子','sales':0,'profit':-0.5}])
    assert len(rows)==2
    assert {r['metric']:r['value'] for r in rows}=={'sales':0,'profit':-0.5}
    assert all(r['source_system']=='edinet' for r in rows)
