from pathlib import Path
import pytest
from src.analysis.segment_detection_v4 import run_segment_detection_v4
from src.segment.pdf_segment_evidence import verified_pdf_records

FIX=Path(__file__).parent/'fixtures/segment_partials'

@pytest.mark.parametrize('ticker,period,quarter,first_name,current_sales,current_profit,prior_sales,prior_profit,page',[
    ('9249','2026-09-30','2Q','ファシリティ事業',3036.377,339.577,2631.417,285.747,13),
    ('6645','2026-03-31','3Q','IAB',294212,28538,269481,28602,18),
])
def test_current_table_is_bound_to_its_original_dated_heading(ticker,period,quarter,first_name,current_sales,current_profit,prior_sales,prior_profit,page):
    result=run_segment_detection_v4(str(FIX/f'{ticker}_period_table_binding.pdf'),ticker=ticker)
    rows=verified_pdf_records(result,period,quarter)
    current=next(r for r in rows if r['period']==period and r['segment_name']==first_name)
    previous=next(r for r in rows if r['period']!=period and r['segment_name']==first_name)
    assert (current['segment_sales'],current['segment_profit'])==(current_sales,current_profit)
    assert (previous['segment_sales'],previous['segment_profit'])==(prior_sales,prior_profit)
    assert current['segment_metadata']['source_page']==page
    assert current['segment_metadata']['period_start']!=previous['segment_metadata']['period_start']
    assert (current['segment_metadata']['source_page'],current['segment_metadata']['source_table_cells']['table_index'])!=(previous['segment_metadata']['source_page'],previous['segment_metadata']['source_table_cells']['table_index'])
