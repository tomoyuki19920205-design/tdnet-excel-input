import json
from pathlib import Path
from src.segment.disclosure_evidence import single_segment_omission
from src.analysis.segment_detection_v4 import _is_single_segment_omission_text

def test_actual_prior_only_omission_does_not_hide_new_current_segments():
    source=json.loads((Path(__file__).parent/'fixtures/segment_partials/prior_only_omission_source.json').read_text(encoding='utf-8'))
    text=source['texts'][0]['text']
    assert 'ＡＩデータセンター' in text
    assert single_segment_omission(text) is None
    assert not _is_single_segment_omission_text(text)

def test_explicit_current_omission_preserves_its_evidence():
    text='当社は情報サービスの単一セグメントであるため、セグメント情報の記載を省略しております。'
    proof=single_segment_omission(text)
    assert proof['period_role']=='current' and '単一セグメント' in proof['statement']
    assert _is_single_segment_omission_text(text)

def test_geographical_revenue_exemption_is_not_business_profit_nondisclosure():
    text='本邦の外部顧客への売上高が90％を超えるため、地域ごとの情報の記載を省略しております。'
    assert single_segment_omission(text) is None
    assert not _is_single_segment_omission_text(text)

def test_textblock_with_quantitative_table_requires_table_period_binding():
    assert single_segment_omission('単一セグメントであるため記載を省略しております。',table_count=1) is None
