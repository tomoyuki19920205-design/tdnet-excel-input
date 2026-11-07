from pathlib import Path
import pytest
from src.segment.edinet_segment_extractor import extract_edinet_segments,disclosed_column_name
from src.segment.edinet_canonical_bridge import edinet_result_to_canonical_segments

def test_generic_other_member_and_stale_taxonomy_label_use_printed_business():
    source=Path(__file__).parent/'fixtures/segment_partials/S100Z4OA.zip'
    result=extract_edinet_segments(str(source),ticker='157A',period='2026-06-30',quarter='FY')
    rows=edinet_result_to_canonical_segments(result)
    assert result.status=='ok'
    byname={r['segment_name']:r for r in rows}
    assert len(byname)==len(rows)==3
    assert byname['体験型投資学習事業']['sales']==1200.954
    assert byname['資産形成支援事業']['sales']==707.711
    assert byname['その他']['sales']==12.754
    assert byname['資産形成支援事業']['profit']==-6.851
    assert byname['その他']['profit']==3.673

def test_conflicting_metric_columns_never_merge():
    def f(label):return {'inline_evidence':{'column':{'role':'segment','label':label}}}
    assert disclosed_column_name([f('電子'),f('電子')])=='電子'
    assert disclosed_column_name([f('その他（注）４')])=='その他'
    with pytest.raises(ValueError,match='different business columns'):
        disclosed_column_name([f('電子'),f('薄膜')])
