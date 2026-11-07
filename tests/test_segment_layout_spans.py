from pathlib import Path
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
from src.segment.xhtml_segment_tables import extract_textblock_tables

def test_real_business_spanning_layout_columns_and_lettered_external_sales():
    path=Path(__file__).parent/'fixtures/segment_partials/layout_span_4331.zip'
    result=extract_segments_from_xbrl_zip_detailed(str(path),period='2025-12-31',quarter='FY',include_context_evidence=True)
    current=[r for r in result.segments if r.period=='2025-12-31']
    assert len(current)==2
    main=next(r for r in current if '国内ウェディング' in r.raw_segment_name)
    other=next(r for r in current if 'その他' in r.raw_segment_name)
    assert (main.sales,main.profit)==(34522,3004)
    assert (other.sales,other.profit)==(1186,356)
    assert main.raw_json['sales_basis']=='external_customers'
    assert main.raw_json['table_facts']['sales']['column_span']==2
    assert main.raw_json['raw_sales_label']=='a.外部顧客に対する売上高'
    assert len(result.segments)==4

def test_amount_spanning_two_distinct_business_headers_is_not_allocated():
    html='''<p>当事業年度 自2025年4月1日 至2026年3月31日</p><p>単位：百万円</p>
    <table><tr><td></td><td>電子</td><td>薄膜</td><td>その他</td></tr>
    <tr><td>a.外部顧客への売上高</td><td colspan="2">100</td><td>8</td></tr>
    <tr><td>セグメント利益</td><td colspan="2">10</td><td>1</td></tr></table>'''
    rows=extract_textblock_tables(html,period_start='2025-04-01',period_end='2026-03-31',is_consolidated=True,textblock_tag='SegmentInformationTextBlock')
    assert [(r['segment_name'],r['sales'],r['profit']) for r in rows]==[('その他',8,1)]
