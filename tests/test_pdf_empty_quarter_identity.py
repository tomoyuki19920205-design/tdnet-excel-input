from pathlib import Path
from types import SimpleNamespace
from src.analysis.segment_detection_v4 import run_segment_detection_v4
from src.segment.pdf_segment_evidence import verified_pdf_records,pdf_fiscal_period

FIX=Path(__file__).parent/'fixtures'/'segment_partials'

def test_real_annual_pdf_normalizes_missing_quarter_from_original_heading():
 result=run_segment_detection_v4(str(FIX/'blank_quarter_2491.pdf'),ticker='2491')
 rows=verified_pdf_records(result,'2025-12-31','')
 assert len(rows)==6
 assert {r['quarter'] for r in rows}=={'FY'}
 assert {r['period'] for r in rows}=={'2024-12-31','2025-12-31'}
 assert all(r['period']==r['segment_metadata']['period_end'] for r in rows)
 assert all(r['segment_metadata']['quarter_identity_evidence']=='original_annual_heading_and_exact_full_fiscal_duration' for r in rows)

def test_real_quarter_pdf_never_guesses_missing_quarter():
 result=run_segment_detection_v4(str(FIX/'3393_202611.pdf'),ticker='3393')
 assert verified_pdf_records(result,'2027-03-31','')==[]
 assert verified_pdf_records(result,'2027-03-31',None)==[]
 assert len(verified_pdf_records(result,'2027-03-31','2Q'))==8
 assert pdf_fiscal_period('2027-03-31','','current','2027-03-31') is None
 assert pdf_fiscal_period('2027-03-31','invalid','current','2027-03-31') is None
