from pathlib import Path
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
def test_numbered_external_sales_caption_preserves_formal_correction():
    p=Path(__file__).parent/'fixtures/segment_partials/numbered_metric_4751.zip'
    result=extract_segments_from_xbrl_zip_detailed(str(p),period='2021-09-30',quarter='FY',include_context_evidence=True)
    rows=[r for r in result.segments if r.period=='2021-09-30']
    assert len(rows)==5 and all(r.sales is not None and r.profit is not None for r in rows)
    advertising=next(r for r in rows if r.raw_segment_name=='インターネット広告')
    assert (advertising.sales,advertising.profit)==(301771,22258)
    assert advertising.raw_json['sales_basis']=='external_customers'
    assert advertising.raw_json['raw_sales_label'].startswith('(1)')
