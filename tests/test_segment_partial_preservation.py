from lib.backfill.segment_partial_check import check_xbrl_partial_segments, decide_fallback_adoption

def test_same_filing_metric_merge_preserves_other_measure_and_its_basis():
    from lib.backfill.segment_partial_check import merge_same_filing_missing_metrics
    import copy
    context={'period_start':'2026-04-01','period_end':'2026-09-30','currency':'JPY','is_consolidated':True}
    old=[{'segment_name':'電子','segment_sales':0,'segment_profit':None,'segment_metadata':{**context,'sales_basis':'external_customers'}}]
    new=[{'segment_name':'電子','segment_sales':10,'segment_profit':-0.5,'segment_metadata':{**context,'sales_basis':'including_intersegment','profit_basis':'gross_profit','profit_label':'売上総利益','normalization_count':1,'normalized_unit':'millions_jpy','source_table_cells':{'column_index':1}}}]
    detail={};merged=merge_same_filing_missing_metrics(old,new,detail)
    assert merged[0]['segment_sales']==0 and merged[0]['segment_profit']==-0.5
    assert merged[0]['segment_metadata']['sales_basis']=='external_customers'
    assert merged[0]['segment_metadata']['profit_basis']=='gross_profit'
    assert old[0]['segment_profit'] is None
    assert decide_fallback_adoption(old,merged,{})[0]
    new[0]['segment_metadata']['period_end']='2026-06-30'
    assert merge_same_filing_missing_metrics(old,new,{})==old
    new[0]['segment_metadata']['period_end']='2026-09-30'
    old[0]['_profit_fact_explicit_nil']=True
    assert merge_same_filing_missing_metrics(old,new,{})==old

def test_reverse_metric_merge_keeps_existing_loss():
    from lib.backfill.segment_partial_check import merge_same_filing_missing_metrics
    context={'period_start':'2026-04-01','period_end':'2026-09-30','currency':'JPY','is_consolidated':False,'profit_basis':'ordinary_profit'}
    old=[{'segment_name':'薄膜','segment_sales':None,'segment_profit':-1,'segment_metadata':context}]
    new=[{'segment_name':'薄膜','segment_sales':0,'segment_profit':99,'segment_metadata':{**context,'normalization_count':1,'normalized_unit':'millions_jpy','sales_basis':'external_customers'}}]
    merged=merge_same_filing_missing_metrics(old,new,{})
    assert merged[0]['segment_sales']==0 and merged[0]['segment_profit']==-1
    new[0]['segment_metadata']['is_consolidated']=True
    assert merge_same_filing_missing_metrics(old,new,{})==old

def test_reverse_partial_and_explicit_non_disclosure():
    rows=[{'segment_name':n,'segment_sales':None,'segment_profit':0,'period':'2027-03-31'} for n in ['A','B','C']]
    assert check_xbrl_partial_segments(rows,'1777','2027-03-31')[1] == 'segment_sales_incomplete'
    for r in rows:r['_sales_fact_explicit_nil']=True
    assert not check_xbrl_partial_segments(rows,'1777','2027-03-31')[0]
    for r in rows:r.update(segment_sales=1,segment_profit=None,profit_status='not_disclosed',profit_non_disclosure_evidence={'document':'same','row_label':'売上高のみ'})
    assert not check_xbrl_partial_segments(rows,'1777','2027-03-31')[0]
    rows[0].pop('profit_non_disclosure_evidence')
    assert check_xbrl_partial_segments(rows,'1777','2027-03-31')[1] == 'segment_profit_incomplete'

def test_fallback_cannot_erase_or_replace_existing_numeric_cells():
    old=[{'segment_name':'A','segment_sales':0,'segment_profit':-2}]
    more=[{'segment_name':'A','segment_sales':None,'segment_profit':-2},{'segment_name':'B','segment_sales':1,'segment_profit':1}]
    assert not decide_fallback_adoption(old,more,{})[0]
    more[0]['segment_sales']=100
    assert not decide_fallback_adoption(old,more,{})[0]
    more[0]['segment_sales']=0
    assert decide_fallback_adoption(old,more,{})[0]

def test_fallback_fills_only_compatible_row_with_same_peer_value():
    old=[{'segment_name':'A','segment_sales':10,'segment_profit':None,'segment_metadata':{'sales_basis':'external_customers'}}]
    new=[{'segment_name':'A','segment_sales':10,'segment_profit':-1,'segment_metadata':{'sales_basis':'including_intersegment'}}]
    assert not decide_fallback_adoption(old,new,{})[0]
    new[0]['segment_metadata']['sales_basis']='external_customers'
    assert decide_fallback_adoption(old,new,{})[0]
