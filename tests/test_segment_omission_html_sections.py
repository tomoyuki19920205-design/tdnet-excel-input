from pathlib import Path
from src.segment.disclosure_evidence import single_segment_omission_html

FIXTURES=Path(__file__).parent/'fixtures/segment_partials'
def actual(doc):return (FIXTURES/f'omission_{doc}.html').read_text(encoding='utf-8')

def test_new_current_businesses_are_not_hidden_by_previous_omission():
    text=actual('20261106566687')
    assert single_segment_omission_html(text) is None
    proof=single_segment_omission_html(text,period_role='previous')
    assert '2025年7月1日' in proof['period_heading']

def test_related_product_and_goodwill_tables_do_not_hide_period_omission():
    text=actual('20261014550634')
    assert single_segment_omission_html(text) is None
    proof=single_segment_omission_html(text,period_role='previous')
    assert 'IoT' in proof['statement']

def test_current_and_previous_omission_with_related_tables():
    text=actual('20261105565689')
    current=single_segment_omission_html(text)
    previous=single_segment_omission_html(text,period_role='previous')
    assert '2025年10月1日' in current['period_heading']
    assert '2024年10月1日' in previous['period_heading']

def test_no_business_omission_from_related_geography_only():
    assert single_segment_omission_html('<p>【関連情報】</p><p>単一セグメントであるため省略しております。</p>') is None

def test_numeric_table_in_same_period_keeps_ambiguous_note_unresolved():
    text='<p>当連結会計年度(自2025年1月1日至2025年12月31日)</p><table><tr><td>営業利益</td><td>15</td></tr></table><p>単一セグメントであるため記載を省略しております。</p>'
    assert single_segment_omission_html(text) is None
