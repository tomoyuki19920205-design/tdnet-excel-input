"""Original TDnet attachment with no segment XBRL facts or TextBlock."""
from pathlib import Path
import zipfile
from src.segment.xhtml_segment_tables import extract_attachment_tables
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
from src.segment.profit_basis import profit_basis
from src.segment.semantic_facts import column_role

FIX=Path(__file__).parent/'fixtures/segment_partials/plain_attachment_20230112588103.zip'

def test_real_plain_attachment_uses_cumulative_dates_and_disclosed_company_measure():
    result=extract_segments_from_xbrl_zip_detailed(str(FIX),period='2023-02-28',quarter='3Q')
    assert result.status=='success_with_rows'
    rows={(r.period,r.normalized_segment_name):(r.sales,r.profit) for r in result.segments}
    assert len(rows)==6
    assert [(r.sales,r.profit) for r in result.segments if r.period=='2023-02-28']==[(1493,1055),(46783,10729),(4520,1772)]
    assert [(r.sales,r.profit) for r in result.segments if r.period=='2022-02-28']==[(1548,1124),(23601,2120),(4236,1814)]
    for r in result.segments:
        assert r.normalized_ticker=='2337' and r.quarter=='3Q' and r.is_consolidated is True
        assert r.extraction_method=='xhtml_attachment'
        assert r.raw_json['profit_basis']=='company_defined_unknown'
        f=r.raw_json['table_facts']['profit']
        assert f['evidence_type']=='xhtml_attachment_table_cell'
        assert not f['textblock_tag'] and not f['textblock_context']
        assert f['period_end'].endswith('-11-30')
        assert f['period_start'].endswith('-03-01')
        assert 'ALL-IN' in f['row_label']

def test_plain_attachment_rejects_wrong_dates_and_missing_scope():
    with zipfile.ZipFile(FIX) as z:
        html=z.read('XBRLData/Attachment/qualitative.htm').decode('utf-8')
    assert extract_attachment_tables(html,period_start='2022-04-01',period_end='2022-11-30',is_consolidated=True)==[]
    assert extract_attachment_tables(html,period_start='2022-03-01',period_end='2022-11-30',is_consolidated=None)==[]

def test_statement_total_and_all_in_measure_never_become_ordinary_business_profit():
    for label in ('四半期連結財務諸表計上額','中間財務諸表計上額','連結財務諸表計上額'):
        assert column_role(label)=='adjustment_or_consolidated'
    assert profit_basis('セグメント利益（ALL-IN営業利益）')=='company_defined_unknown'
    assert profit_basis('営業利益')=='operating_profit'

def test_plain_attachment_requires_explicit_segment_section():
    html='<p>自2022年3月1日至2022年11月30日</p><p>単位：百万円</p><table><tr><td></td><td>電子</td><td>薄膜</td></tr><tr><td>外部顧客への売上高</td><td>0</td><td>2</td></tr><tr><td>セグメント利益</td><td>0</td><td>△1</td></tr></table>'
    args=dict(period_start='2022-03-01',period_end='2022-11-30',is_consolidated=True)
    assert extract_attachment_tables(html,**args)==[]
    rows=extract_attachment_tables('<h2>セグメント情報</h2>'+html,**args)
    assert [(r['sales'],r['profit']) for r in rows]==[(0,0),(2,-1)]

def test_separate_adjustment_table_never_uses_a_data_dash_as_business_header():
    path=FIX.parent/'plain_attachment_20221208576529.zip'
    result=extract_segments_from_xbrl_zip_detailed(str(path),period='2023-04-30',quarter='2Q')
    assert result.status=='success_with_rows'
    assert all(r.raw_segment_name not in ('－','-','調整額','四半期連結損益計算書計上額') for r in result.segments)
    previous=[r for r in result.segments if r.period=='2022-04-30']
    assert len(previous)==4
    assert sorted(r.profit for r in previous)==[-7.836,329.906,544.42,1187.503]
