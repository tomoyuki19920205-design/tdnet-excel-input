from pathlib import Path
from types import SimpleNamespace
import copy
import pytest
from src.analysis.segment_detection_v4 import run_segment_detection_v4,SegmentRecordV4
from src.segment.pdf_segment_evidence import (bind_pdf_period_evidence,pdf_fiscal_period,
    pdf_period_section,verified_pdf_records)

FIXTURES=Path(__file__).parent/'fixtures'/'segment_partials'

@pytest.mark.parametrize('ticker,period,quarter,expected',[
 ('3393','2027-03-31','2Q',(10117.437,1088.766)),
 ('3843','2027-04-30','1Q',(3084.344,467.207)),
])
def test_real_pdf_table_unit_period_and_loss(ticker,period,quarter,expected):
 result=run_segment_detection_v4(str(FIXTURES/f'{ticker}_202611.pdf'),ticker=ticker)
 rows=verified_pdf_records(result,period,quarter,current_only=True)
 assert len(rows)==4
 assert (rows[0]['segment_sales'],rows[0]['segment_profit'])==expected
 assert all(r['period']==period and r['segment_metadata']['sales_basis']=='including_intersegment' for r in rows)
 assert rows[-1]['segment_sales'] is None and rows[-1]['segment_profit']<0
 assert all(r['segment_metadata']['profit_basis']=='operating_profit' for r in rows)
 assert all(r['segment_metadata']['source_table_cells']['header']==r['segment_name'] for r in rows)
 assert verified_pdf_records(result,period,quarter,current_only=True)==rows
 assert verified_pdf_records(result,period,'FY',current_only=True)==[]

def test_fiscal_year_end_is_not_quarter_end():
 assert pdf_fiscal_period('2027-03-31','2Q','current','2026-09-30')=='2027-03-31'
 assert pdf_fiscal_period('2027-04-30','1Q','current','2026-07-31')=='2027-04-30'
 assert pdf_fiscal_period('2027-03-31','2Q','previous','2025-09-30')=='2026-03-31'
 assert pdf_fiscal_period('2027-03-31','2Q','current','2026-12-31') is None

def test_notes_do_not_cross_period_section():
 text='前連結会計年度（自2024年4月1日至2025年3月31日）単位：百万円 セグメント利益は営業利益と一致。当連結会計年度（自2025年4月1日至2026年3月31日）単位：百万円 セグメント利益は売上総利益と一致。'
 assert '営業利益' not in pdf_period_section(text,'current')
 assert '売上総利益' not in pdf_period_section(text,'previous')
 assert bind_pdf_period_evidence(text,'current')['period_end']=='2026-03-31'

def test_ambiguous_units_or_unbound_dates_never_use_pl_unit():
 assert bind_pdf_period_evidence('単位：百万円 セグメント利益 100','current') is None
 text='当連結会計年度（自2025年4月1日至2026年3月31日）単位：百万円 単位：千円'
 assert bind_pdf_period_evidence(text,'current') is None

def test_zero_fraction_loss_and_no_qtd_copy():
 proof=bind_pdf_period_evidence('当第2四半期連結累計期間（自2026年4月1日至2026年9月30日）単位：千円','current')
 segment=SegmentRecordV4(segment_name='電子',segment_sales=0,segment_profit=-500,segment_metadata={**proof,'source_table_cells':{'header':'電子'}})
 result=SimpleNamespace(extracted_periods=[SimpleNamespace(period_type='current',segments=[segment])])
 row=verified_pdf_records(result,'2027-03-31','2Q')[0]
 assert row['segment_sales']==0 and row['segment_profit']==-0.5
 assert segment.segment_profit==-500
 segment.segment_metadata['period_start']='2026-07-01'
 assert verified_pdf_records(result,'2027-03-31','2Q')==[]

def test_worker_preserves_fiscal_identity_and_table_metadata():
    from lib.backfill.worker_v4 import _try_pdf_source_v4
    filing=SimpleNamespace(ticker='3393',expected_period='2027-03-31',expected_quarter='2Q',disclosure_date='2026-11-06',title='2027年3月期 第2四半期決算短信')
    candidate=_try_pdf_source_v4(str(FIXTURES/'3393_202611.pdf'),filing,{'period':'2026-09-30','quarter':'2Q'},'20261104564263',{})
    rows=candidate.segment_records
    assert {r['period'] for r in rows}=={'2026-03-31','2027-03-31'}
    assert rows[0]['segment_metadata']['display_unit']=='千円'
    assert rows[0]['segment_sales']==9010.763
    assert rows[0]['unit_multiplier']==1000000
