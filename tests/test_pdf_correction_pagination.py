from pathlib import Path
from src.analysis.segment_detection_v4 import run_segment_detection_v4,_detect_period_label_in_text
from src.segment.pdf_segment_evidence import verified_pdf_records
FIX=Path(__file__).parent/'fixtures/segment_partials'

def test_heading_at_newline_is_not_replaced_by_previous_line_suffix():
    assert _detect_period_label_in_text('報\n前連結会計年度 (自2022年5月1日至2023年4月30日)')[0][0]=='previous'
    assert _detect_period_label_in_text('。\n当連結会計年度 (自2024年4月1日至2025年3月31日)')[0][0]=='current'

def test_actual_correction_cover_pages_do_not_drop_current_period():
    result=run_segment_detection_v4(str(FIX/'correction_20250214576241.pdf'),doc_id='20250214576241',ticker='9444')
    rows=verified_pdf_records(result,'2023-04-30','FY')
    current=[r for r in rows if r['period']=='2023-04-30']
    assert len(current)==4
    assert next(r for r in current if r['segment_name']=='移動体通信関連事業')['segment_profit']==181.656
    assert {r['segment_metadata']['source_page'] for r in current}=={18}
    assert all(r['segment_metadata']['normalization_count']==1 for r in current)
    assert all(r['segment_metadata']['profit_basis']=='ordinary_profit' for r in current)
    assert all(r['segment_metadata']['profit_label']=='セグメント利益' for r in current)
    assert {n['page'] for r in current for n in r['segment_metadata']['profit_basis_policy_evidence']}=={17}

def test_actual_ifrs_correction_keeps_both_periods_without_consolidated_column():
    result=run_segment_detection_v4(str(FIX/'correction_20250508536186.pdf'),doc_id='20250508536186',ticker='2768')
    rows=verified_pdf_records(result,'2025-03-31','FY')
    current=[r for r in rows if r['period']=='2025-03-31']
    assert len(current)==8
    assert all(r['segment_name']!='連結' for r in rows)
    assert next(r for r in current if r['segment_name']=='航空・社会インフラ')['segment_profit']==26296
    assert {r['period'] for r in rows}=={'2024-03-31','2025-03-31'}

def test_following_quarter_only_tables_do_not_displace_cumulative_tables():
    result=run_segment_detection_v4(str(FIX/'6301_202610.pdf'),ticker='6301')
    rows=verified_pdf_records(result,'2027-03-31','2Q',current_only=True)
    assert len(rows)==3
    assert {r['segment_metadata']['period_start'] for r in rows}=={'2026-04-01'}
    assert {r['segment_metadata']['source_page'] for r in rows}=={16}
    assert 'current_qtd' in {p.period_type for p in result.extracted_periods}
