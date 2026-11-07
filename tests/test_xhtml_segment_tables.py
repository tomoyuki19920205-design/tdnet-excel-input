from pathlib import Path
import zipfile
from src.segment.xhtml_segment_tables import extract_textblock_tables
from src.segment.edinet_segment_extractor import extract_edinet_segments

FIX=Path(__file__).parent/'fixtures'/'segment_partials'

def parse(html,start='2025-04-01',end='2026-03-31'):
    return extract_textblock_tables(html,period_start=start,period_end=end,is_consolidated=False,textblock_tag='NotesSegmentInformationTextBlock',context_ref='CurrentYearDuration')

def test_real_6439_current_table_preserves_thousands_losses_and_scope():
    r=extract_edinet_segments(str(FIX/'S100YIME.zip'),ticker='6439',period='2026-03-31',quarter='FY')
    assert r.status=='ok'
    assert [(s.segment_name_raw,s.sales,s.profit) for s in r.segments]==[('鋳物事業',4658.588,-163.571),('不動産賃貸事業',142.67,61.116),('発電・売電事業',82.989,48.992)]
    assert all(s.metadata['is_consolidated'] is False and s.metadata['profit_basis']=='operating_profit' for s in r.segments)
    f=r.segments[0].metadata['table_facts']['profit']
    assert f['raw_text']=='△163,571' and f['display_unit']=='千円' and f['period_start']=='2025-04-01'
    assert f['evidence_type']=='xhtml_table_cell'

def test_real_prior_only_table_never_becomes_current():
    r=extract_edinet_segments(str(FIX/'S100YY3Q.zip'),ticker='2792',period='2026-05-31',quarter='FY')
    assert r.status=='no_segments' and r.segments==[]
    html=(FIX/'S100YY3Q_textblock.html').read_text(encoding='utf-8')
    assert parse(html,start='2025-06-01',end='2026-05-31')==[]
    prior=parse(html,start='2024-06-01',end='2025-05-31')
    assert prior and all(r['period_end']=='2025-05-31' for r in prior)

TABLE='''<p>当事業年度（自 2025年4月1日 至 2026年3月31日）</p><p>単位：千円</p><table>
<tr><td></td><td colspan="2">報告セグメント</td><td rowspan="2">その他（注）1</td><td rowspan="2">合計</td><td rowspan="2">連結財務諸表計上額</td></tr>
<tr><td></td><td>電子</td><td>長い<br/>事業名</td></tr>
<tr><td>外部顧客への売上高</td><td>0</td><td>1,234</td><td>8</td><td>1,242</td><td>1,242</td></tr>
<tr><td>セグメント利益</td><td>△1</td><td>－</td><td>0</td><td>△1</td><td>△1</td></tr></table>'''

def test_short_long_other_zero_dash_and_unknown_basis():
    rows=parse(TABLE)
    assert [(r['segment_name'],r['sales'],r['profit']) for r in rows]==[('電子',0,-0.001),('長い 事業名',1.234,None),('その他',0.008,0)]
    assert all(r['profit_basis']=='company_defined_unknown' for r in rows)
    assert rows[1]['facts']['profit']['status']=='explicit_dash'
    assert all(r['sales_basis']=='external_customers' for r in rows)

def test_geographic_sales_without_profit_is_not_business_segment():
    html=TABLE.replace('セグメント利益','地域別資産')
    assert parse(html)==[]

def test_period_mismatch_or_missing_unit_stays_unresolved():
    assert parse(TABLE,start='2025-10-01')==[]
    assert parse(TABLE.replace('単位：千円',''))==[]

def test_all_profits_dash_does_not_erase_numeric_sales():
    rows=parse(TABLE.replace('<td>△1</td>','<td>－</td>').replace('<td>0</td><td>△1</td>','<td>－</td><td>－</td>'))
    assert rows and rows[0]['sales']==0 and rows[0]['profit'] is None

def test_tdnet_normal_route_reads_plain_textblock_with_dei_dates(tmp_path):
    from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
    from src.segment.profit_basis import segment_flags
    content='''<html><xbrli:context id="CurrentYearDuration"><xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<ix:nonNumeric name="tse:DocumentName">2026年3月期 決算短信</ix:nonNumeric>
<ix:nonNumeric name="jpdei_cor:SecurityCodeDEI">99990</ix:nonNumeric>
<ix:nonNumeric name="jpdei_cor:CurrentFiscalYearStartDateDEI">2025-04-01</ix:nonNumeric>
<ix:nonNumeric name="jpdei_cor:CurrentFiscalYearEndDateDEI">2026-03-31</ix:nonNumeric>
<ix:nonNumeric name="jpdei_cor:CurrentPeriodEndDateDEI">2026-03-31</ix:nonNumeric>
<ix:nonNumeric name="jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI">false</ix:nonNumeric>
<ix:nonNumeric name="tse:NotesSegmentInformationTextBlock" contextRef="CurrentYearDuration">'''+TABLE+'''</ix:nonNumeric></html>'''
    path=tmp_path/'filing.zip'
    with zipfile.ZipFile(path,'w') as z:z.writestr('XBRLData/Attachment/0106010-scsg-tse-acedjpfr-99990-2026-03-31-ixbrl.htm',content)
    result=extract_segments_from_xbrl_zip_detailed(str(path),period='2026-03-31',quarter='FY')
    assert result.status=='success_with_rows',result
    assert [(r.raw_segment_name,r.sales,r.profit) for r in result.segments]==[('電子',0,-0.001),('長い 事業名',1.234,None),('その他',0.008,0)]
    assert segment_flags(result.segments[0].raw_json)['table_facts']['profit']['raw_text']=='△1'
