from lib.backfill.segment_partial_check import check_xbrl_partial_segments, decide_fallback_adoption

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
