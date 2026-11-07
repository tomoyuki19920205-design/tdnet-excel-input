from pathlib import Path
import copy
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
from src.segment.xhtml_segment_tables import literal_metric_completions
FIX=Path(__file__).parent/'fixtures/segment_partials'

def extract(doc,period,quarter):
    result=extract_segments_from_xbrl_zip_detailed(str(FIX/(doc+'.zip')),period=period,quarter=quarter,include_context_evidence=True)
    assert result.status=='success_with_rows'
    return {(r.period,r.raw_segment_name):r for r in result.segments}

def test_literal_profit_fills_individual_sales_without_changing_peer():
    rows=extract('20260610567509','2027-01-31','1Q')
    old=rows['2026-01-31','Otehr'];current=rows['2027-01-31','Plastic']
    assert (old.sales,old.profit)==(-4.4,-31.036)
    assert (current.sales,current.profit)==(414.574,-1.236)
    assert current.raw_json['table_facts']['profit']['raw_text']=='△1,236'
    assert current.raw_json['table_facts']['profit']['evidence_type']=='xhtml_table_cell'
    assert current.raw_json['sales_fact']['million_value']==414.574

def test_literal_sales_fills_profit_and_preserves_revenue_basis():
    rows=extract('20261029560207','2027-03-31','2Q')
    r=rows['2027-03-31','Fire Alarm Systems']
    assert (r.sales,r.profit)==(33662,5812)
    assert r.raw_json['sales_basis']=='customer_contract_revenue'
    assert r.raw_json['profit_basis']=='operating_profit'
    assert r.raw_json['profit_fact']['million_value']==5812

def test_different_sales_basis_and_amount_never_fill_missing_profit():
    rows=extract('20241111518218','2025-03-31','2Q')
    r=rows['2025-03-31','Petroleum Business']
    assert r.sales==1175886 and r.profit is None
    assert not r.raw_json.get('table_facts')

def test_literal_merge_respects_nil_period_scope_and_zero():
    primary={'raw_segment_label':'電子','sales':0,'profit':None,'period_start':'2026-01-01','period_end':'2026-03-31','is_consolidated':True,'currency':'JPY','sales_basis':'external_customers'}
    table={**primary,'profit':-2,'profit_label':'セグメント利益','profit_basis':'company_defined_unknown','facts':{'profit':{'status':'disclosed_numeric','raw_text':'△2'}}}
    assert literal_metric_completions([primary],[table])['電子']['profit']['value']==-2
    for change in ({'_profit_fact_explicit_nil':True},{'period_end':'2026-06-30'},{'is_consolidated':False},{'sales_basis':'including_intersegment'},{'sales':1}):
        assert literal_metric_completions([{**primary,**change}],[table])=={}
    assert literal_metric_completions([primary],[table,{**table,'profit':3}])=={}
