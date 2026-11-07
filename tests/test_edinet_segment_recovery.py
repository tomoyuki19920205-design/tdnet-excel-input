from pathlib import Path
from src.segment.edinet_segment_extractor import extract_edinet_segments, classify_concept, EdinetSegmentRecord, derive_quarter_values
from src.segment.edinet_canonical_bridge import edinet_result_to_canonical_segments
FIX=Path(__file__).parent/'fixtures'/'segment_partials'

def test_real_thousand_yen_loss_other_nil_and_nonconsolidated_scope():
    result=extract_edinet_segments(str(FIX/'S100YJGX.zip'),ticker='4558',period='2026-03-31',quarter='FY')
    assert result.status=='ok'
    rows={r['segment_name']:r for r in edinet_result_to_canonical_segments(result)}
    assert len(rows)==4
    assert (rows['卸売部門']['sales'],rows['卸売部門']['profit'])==(1909.446,-22.058)
    assert rows['その他']['sales']==1.84 and rows['その他']['profit'] is None
    assert rows['その他']['flags']['explicit_nil_facts']
    assert all(r['flags']['is_consolidated'] is False for r in rows.values())
    assert all(r['flags']['period_start']=='2025-04-01' and r['flags']['period_end']=='2026-03-31' for r in rows.values())

def test_furuya_six_real_segments_keep_gross_profit_and_original_labels():
    result=extract_edinet_segments(str(FIX/'S100Z403.zip'),ticker='7826',period='2026-06-30',quarter='FY')
    rows=edinet_result_to_canonical_segments(result)
    assert len(rows)==6
    assert next(r for r in rows if r['segment_name']=='薄膜')['profit']==9494
    assert all(r['flags']['profit_basis']=='gross_profit' for r in rows)
    assert all(r['flags']['profit_label']=='セグメント利益' for r in rows)

def test_wrong_company_or_fiscal_period_is_not_relabelled_as_requested():
    for args in ({'ticker':'9999','period':'2026-06-30'},{'ticker':'7826','period':'2026-03-31'}):
        result=extract_edinet_segments(str(FIX/'S100Z403.zip'),quarter='FY',**args)
        assert result.status=='error' and not result.segments

def test_extension_requires_label_not_financing_or_ratio_substrings():
    assert classify_concept('FinancialRevenueIFRS')[0]=='other'
    assert classify_concept('OperatingProfitMargin')[0]=='other'
    assert classify_concept('CustomAdjustedMeasure','セグメント利益')[0]=='profit'

def test_quarter_differences_respect_year_definition_and_each_measure():
    common={'period_start':'2025-04-01','currency':'JPY','is_consolidated':True,'sales_basis':'external_customers','profit_basis':'gross_profit'}
    prior=EdinetSegmentRecord(segment_name_norm='紙',sales=100,profit=20,metadata={**common,'period_end':'2025-06-30'})
    current=EdinetSegmentRecord(segment_name_norm='紙',sales=220,profit=40,metadata={**common,'period_end':'2025-09-30','profit_basis':'operating_profit'})
    result=derive_quarter_values({'1Q':[prior],'2Q':[current]},'2Q')[0]
    assert result.sales_qtd==120 and result.profit_qtd is None
    current.metadata['period_start']='2026-04-01'
    result=derive_quarter_values({'1Q':[prior],'2Q':[current]},'2Q')[0]
    assert result.sales_qtd is None and result.profit_qtd is None
