from pathlib import Path
import pytest
from src.analysis.segment_detection_v4 import run_segment_detection_v4
from src.segment.pdf_segment_evidence import verified_pdf_records

FIX=Path(__file__).parent/'fixtures/segment_partials'

@pytest.mark.parametrize('ticker,period,quarter,name,current,previous,page,heading_page',[
 ('6554','2023-09-30','1Q','ソリューション事業',(2510,233),(2328,211),2,2),
 ('2990','2026-03-31','FY','分譲事業',(28197,265),(40454,797),19,18),
 ('2990','2025-03-31','FY','分譲事業',(40454,797),(34616,1464),19,18),
])
def test_original_correction_and_continued_table(ticker,period,quarter,name,current,previous,page,heading_page):
 result=run_segment_detection_v4(str(FIX/f'{ticker}_{period}_positioned.pdf'),ticker=ticker)
 rows=verified_pdf_records(result,period,quarter)
 now=next(r for r in rows if r['period']==period and r['segment_name']==name)
 old=next(r for r in rows if r['period']!=period and r['segment_name']==name)
 assert (now['segment_sales'],now['segment_profit'])==current
 assert (old['segment_sales'],old['segment_profit'])==previous
 assert now['segment_metadata']['source_page']==page
 assert now['segment_metadata']['heading_page']==heading_page
 assert now['segment_metadata']['display_unit']=='百万円'
 if ticker=='6554':
  assert all(r['segment_metadata']['correction_section']=='後' for r in rows)
  assert next(r for r in rows if r['period']==period and r['segment_name']=='AR/VR事業')['segment_profit']==-26

def test_wrapped_table_unit_remains_in_its_dated_period():
 result=run_segment_detection_v4(str(FIX/'4351_wrapped_unit.pdf'),ticker='4351')
 rows=verified_pdf_records(result,'2022-12-31','2Q')
 current=next(r for r in rows if r['period']=='2022-12-31' and r['segment_name']=='サービサー事業')
 assert (current['segment_sales'],current['segment_profit'])==(462.907,279.289)
 assert current['segment_metadata']['display_unit']=='千円'
 assert current['segment_metadata']['period_start']=='2022-01-01'
 assert current['segment_metadata']['source_page']==13

def test_equal_sales_do_not_select_the_correction_before_profit():
 result=run_segment_detection_v4(str(FIX/'7081_correction_sections.pdf'),ticker='7081')
 rows=verified_pdf_records(result,'2023-12-31','2Q')
 assert len(rows)==4
 rental=next(r for r in rows if r['segment_name']=='レンタル関連事業')
 ict=next(r for r in rows if r['segment_name']=='ＩＣＴ事業')
 assert (rental['segment_sales'],rental['segment_profit'])==(8363.272,1053.802)
 assert ict['segment_profit']==178.515
 assert all(r['segment_metadata']['correction_section']=='後' for r in rows)
