"""Synthetic v2-baseline complementary-news tests; no real research or production writes."""
import copy
from pathlib import Path
import pytest
from tools import company_news_batch_benchmark as b
from tools import company_news_strategy_v3 as v3
from tools import company_news_strategy_v4 as v


def fixture():
    a = {'assignment_id': 'synthetic-a1', 'ticker': 'X001', 'company_name': 'Example Works',
         'search_from': '2026-08-25', 'search_to': '2026-08-31', 'strategy_version': v.VERSION}
    src = {'url': 'https://example.test/news', 'source_type': 'company_news', 'content_verified': True,
           'evidence_excerpt': 'Official company identity and recent product notices'}
    t = {'schema_version': v.SCHEMA, 'strategy_version': v.VERSION, 'assignment_id': a['assignment_id'], 'ticker': a['ticker'],
         'company_identity': {'verified_ticker': a['ticker'], 'source_url': src['url'], 'evidence': 'Identity linked to ticker'},
         'economic_exposures': [{'driver': 'customer industrial demand', 'metadata_evidence': 'Profile describes industrial customers', 'transmission_channel': 'Customer output affects component volume'},
                               {'driver': 'input metal pricing', 'metadata_evidence': 'Profile describes metal inputs', 'transmission_channel': 'Input cost changes manufacturing margin'}],
         'queries': [{'channel': c, 'query': q, 'executed': True} for c,q in [('official','Example Works products'),('counterparty','customer production'),('external_media','industrial input prices')]],
         'channel_reviews': [{'channel': c, 'status': 'CHECKED', 'evidence': 'Reviewed relevant fixed-window results', 'exposure_index': 0} for c in ['official','counterparty','external_media']],
         'regulatory_themes': [], 'query_budget_exceeded': False, 'documents': [], 'coverage_gaps': [],
         'coverage_status': 'COMPLETE', 'source_provenance': [src], 'candidate_provenance': [],
         'candidate_count': 0, 'final_event_count': 0, 'no_news_gate_pass': True,
         'window_review_complete': True, 'stopped_after_first_event': False,
         'stage_elapsed_seconds': {'company_web': 3, 'external_complement': 9}, 'total_research_elapsed_seconds': 12}
    return a,t,{'sources_checked_count':1,'items':[]}


def document(title, label):
    return {'date':'2026-08-26','title':title,'url':'https://example.test/doc.pdf','scope_label':label,
            'scope_reason':'Existing source evidence supports scope','material':label in v.IN_SCOPE,
            'body_status':'INACCESSIBLE','index_evidence':title+' index text',
            'tdnet_same_content_verified':label=='OUT_OF_SCOPE_TDNET','tdnet_url':'https://example.test/disclosure',
            'tdnet_identity_evidence':'Same dated document identity','incremental_information':False}


def select_pr(t,p):
    d=document('New customer adoption','IN_SCOPE_COMPANY_WEB')
    d.update(url='https://example.test/pr',final_url='https://example.test/pr',body_status='VERIFIED')
    t['documents'].append(d);t['source_provenance'].append({**t['source_provenance'][0],'url':d['url']})
    t['candidate_provenance']=[{'candidate_index':0,'document_url':d['url'],'selected_for_output':True,
        'selection_reason':'New material customer contract','headline':d['title'],'source_url':d['url'],
        'economic_transmission':'Customer adoption increases product orders and target revenue','event_key':'adoption'}]
    t.update(candidate_count=1,final_event_count=1,no_news_gate_pass=False)
    p.update(sources_checked_count=2,items=[{'headline':d['title'],'source_url':d['url']}])


def test_v2_baseline_without_v3_completeness():
    a,t,p=fixture();assert v.validate(t,a,p)
    assert v.contract()['baseline_strategy_version']==b.STRATEGY_V2_VERSION
    assert not v.contract()['official_index_completeness_required']
    assert not v.contract()['all_document_bodies_required']
    assert not v.contract()['strict_query_provenance_required']
    assert v.evaluation_policy()['primary_metric']=='non_tdnet_material_recall_v1'
    assert v.evaluation_policy()['common48_denominator']==27


@pytest.mark.parametrize('title,label', [('決算短信','OUT_OF_SCOPE_TDNET'),('業績修正','OUT_OF_SCOPE_TDNET'),('増資・売出し','OUT_OF_SCOPE_TDNET'),('固定資産売却益','OUT_OF_SCOPE_TDNET'),('夏季休業日程','OUT_OF_SCOPE_TRIVIAL'),('児童図書の寄付','OUT_OF_SCOPE_TRIVIAL')])
def test_inaccessible_out_of_scope_nonfailure(title,label):
    a,t,p=fixture();t['documents']=[document(title,label)];assert v.validate(t,a,p)['coverage_status']=='COMPLETE'


@pytest.mark.parametrize('label', sorted(v.IN_SCOPE))
def test_material_inclusion(label):
    a,t,p=fixture();select_pr(t,p);d=t['documents'][0];d['scope_label']=label
    if label=='IN_SCOPE_INCREMENTAL_IR':d.update(incremental_information=True,incremental_information_evidence='Q&A adds capacity constraint absent from earnings release')
    assert v.validate(t,a,p)


def test_incremental_ir_requires_actual_delta():
    a,t,p=fixture();select_pr(t,p);t['documents'][0]['scope_label']='IN_SCOPE_INCREMENTAL_IR'
    with pytest.raises(b.BenchmarkError,match='delta'):v.validate(t,a,p)


def test_tdnet_mirror_cannot_be_selected_as_web():
    a,t,p=fixture();select_pr(t,p);t['documents'][0]['tdnet_same_content_verified']=True
    with pytest.raises(b.BenchmarkError,match='repeats TDNET'):v.validate(t,a,p)


def test_economic_transmission_required():
    a,t,p=fixture();select_pr(t,p);t['candidate_provenance'][0].pop('economic_transmission')
    with pytest.raises(b.BenchmarkError,match='economic transmission'):v.validate(t,a,p)


def test_inaccessible_material_partial_and_not_no_news():
    a,t,p=fixture();d=document('Material supply agreement','IN_SCOPE_COUNTERPARTY');d['attempt_evidence']='Primary and alternate inaccessible'
    t.update(documents=[d],coverage_status='PARTIAL',no_news_gate_pass=False,coverage_gaps=[{'url':d['url'],'reason':'Material source inaccessible','retry_required':True}])
    assert v.validate(t,a,p)['coverage_status']=='PARTIAL'
    t['no_news_gate_pass']=True
    with pytest.raises(b.BenchmarkError,match='no-news'):v.validate(t,a,p)


def test_trivial_cannot_force_partial():
    a,t,p=fixture();d=document('Holiday notice','OUT_OF_SCOPE_TRIVIAL');t['documents']=[d]
    t['coverage_gaps']=[{'url':d['url'],'reason':'Unread','retry_required':True}]
    with pytest.raises(b.BenchmarkError,match='only inaccessible'):v.validate(t,a,p)


def test_known_material_cannot_disappear_into_no_news():
    a,t,p=fixture();d=document('Supply agreement','IN_SCOPE_COMPANY_WEB');d['body_status']='SKIPPED';t['documents']=[d]
    with pytest.raises(b.BenchmarkError,match='silently skipped'):v.validate(t,a,p)
    a,t,p=fixture();select_pr(t,p);t['candidate_provenance']=[];t.update(candidate_count=0,final_event_count=0,no_news_gate_pass=True);p['items']=[]
    with pytest.raises(b.BenchmarkError,match='candidate decision'):v.validate(t,a,p)


def test_potential_material_not_trivial():
    a,t,p=fixture();t['documents']=[document('価格改定','OUT_OF_SCOPE_TRIVIAL')]
    with pytest.raises(b.BenchmarkError,match='material title'):v.validate(t,a,p)


def test_budget_overrun_without_note_not_failure():
    a,t,p=fixture();t['queries'] += [copy.deepcopy(t['queries'][0]) for _ in range(10)]
    t['query_budget_exceeded']=True;assert v.validate(t,a,p)


@pytest.mark.parametrize('removed',['official','counterparty','external_media'])
def test_no_news_requires_official_and_two_families(removed):
    a,t,p=fixture();t['channel_reviews']=[dict(c,status='NOT_APPLICABLE') if c['channel']==removed else c for c in t['channel_reviews']]
    with pytest.raises(b.BenchmarkError,match='no-news'):v.validate(t,a,p)


def test_government_can_replace_external_when_exposure_relevant():
    a,t,p=fixture();t['queries'][2]['channel']='government';t['queries'][2]['query']='industrial safety regulation'
    t['channel_reviews'][2]['channel']='government';t['regulatory_themes']=[{'theme':'industrial safety','exposure_index':0}]
    assert v.validate(t,a,p)
    t['regulatory_themes']=[]
    with pytest.raises(b.BenchmarkError,match='exposure-derived'):v.validate(t,a,p)


def test_dynamic_exposure_plan_has_counterparty_no_target_name_requirement():
    a,t,p=fixture();plan=v.query_plan(a['company_name'],t['economic_exposures'],['industrial safety'],True)
    assert len(plan)==6 and {q['channel'] for q in plan}==v.CHANNELS
    assert a['ticker'] not in ' '.join(q['query'] for q in plan)
    assert a['company_name'] not in next(q['query'] for q in plan if q['channel']=='counterparty')
    with pytest.raises(b.BenchmarkError):v.query_plan(a['company_name'],t['economic_exposures'],['a','b','c'])
    with pytest.raises(b.BenchmarkError):v.query_plan(a['company_name'],t['economic_exposures'][:1])


def test_identity_still_fails_closed_but_query_alias_is_allowed():
    a,t,p=fixture();t['queries'][0]['query']='Verified brand products';assert v.validate(t,a,p)
    t['company_identity']['verified_ticker']='X002'
    with pytest.raises(b.BenchmarkError,match='identity'):v.validate(t,a,p)


def test_no_index_or_strict_query_provenance_requirement():
    a,t,p=fixture();assert 'official_indexes' not in t
    assert all('result_evidence' not in q for q in t['queries'])
    assert v.validate(t,a,p)


def test_synthetic_leakage_injected_identity_url_and_history():
    companies=[{'ticker':'X001','company_name':'Example Works'}]
    for bad in ['Example Works', 'X001', 'https://example.test/private-event', 'historical_scoring_v1/misses.csv']:
        assert v.leakage_scan(bad,{},companies)['status']=='FAIL'
    assert v.leakage_scan(v.prompt_text(Path(b.__file__).parents[1]),{},companies)['status']=='PASS'


def test_v3_contract_unchanged_and_generic_prompt_firewall():
    assert v3.contract()['document_coverage_required'] is True
    assert v3.evaluation_policy()['critical_required'] == 7
    assert not v.evaluation_policy()['legacy_critical7_is_unfiltered_gate']
    assert v.leakage_scan(v.prompt_text(Path(b.__file__).parents[1]), {}, [])['hit_count'] == 0


def test_partial_finalize_is_not_no_news(monkeypatch):
    a, t, p = fixture(); t.update(coverage_status='PARTIAL', no_news_gate_pass=False)
    root = Path('synthetic-no-write').resolve(); rd = b._run_dir(root, 'batchbench-v4-test', v.RUN_NAME)
    out = root/'data/news_benchmark_inbox/batchbench-v4-test/batch04v4'
    a.update(temp_path=str(out/'a.tmp'), output_path=str(out/'a.json'), strategy_telemetry_temp_path=str(rd/'a.tmp'), strategy_telemetry_path=str(rd/'a.json'))
    stamp = '2026-09-04T00:00:00+00:00'
    state = {'research_started_at': stamp, 'research_finished_at': stamp, 'research_elapsed_seconds': 0}
    snap = {'benchmark_id': 'batchbench-v4-test', 'run_name': v.RUN_NAME, 'run_id': 'r', 'task_id': 'task01',
            'assignments': [dict(a, slot_id='bslot001', company_position=1)], 'company_state': {a['assignment_id']: state}}
    monkeypatch.setattr(b, '_owned_guard', lambda *args: (rd/'active.json', snap))
    monkeypatch.setattr(b, '_active_run', lambda *args: ({}, rd, {'strategy_version': v.VERSION}))
    monkeypatch.setattr(b, '_read_json', lambda path: t if path == rd/'a.tmp' else p)
    monkeypatch.setattr(b, '_validate_success', lambda *args: (1, 0, 'no_news'))
    monkeypatch.setattr(v, 'validate', lambda *args: t)  # Integration dispatch/result-type only; validator tested above.
    for name in ('replace_with_retry', 'atomic_write_json', '_append_jsonl'):
        monkeypatch.setattr(b, name, lambda *args: None)
    result = b.finalize_company(root, 'task01', 'token', a['assignment_id'])
    assert result['result_type'] == 'partial' and result['strategy_v4']['no_news_gate_pass'] is False
    assert not root.exists()


def test_v4_prepare_isolated_inert_and_v3_frozen(tmp_path):
    from datetime import datetime, timezone
    companies = [{'ticker': f'X{i:03d}', 'company_name': f'Synthetic Works {i}', 'sector': 'materials'} for i in range(192)]
    bid = 'batchbench-v4-synthetic'
    cohort = {'schema_version': b.BENCHMARK_SCHEMA, 'benchmark_mode': True, 'benchmark_id': bid,
              'search_from': '2026-08-25', 'search_to': '2026-08-31',
              'reference_tickers': [c['ticker'] for c in companies[:48]], 'companies': companies}
    b.atomic_write_json(b._benchmark_dir(tmp_path, bid)/'cohort.json', cohort)
    b._prepare_named_run(tmp_path, bid, cohort, companies, 4, 'batch04v2', reference=False,
                         now=datetime(2026, 9, 4, tzinfo=timezone.utc))
    frozen = b._run_dir(tmp_path, bid, v3.RUN_NAME)/'manifest.json'
    b.atomic_write_json(frozen, {'status': 'completed', 'immutable': True})
    before = frozen.read_bytes()
    b.atomic_write_text(tmp_path/'config'/v.PROMPT_NAME, v.prompt_text(Path(b.__file__).parents[1]))
    result = b.prepare_run(tmp_path, bid, 4, strategy=v.VERSION)
    assert result['status'] == 'prepared' and result['company_count'] == 192
    assert result['strategy_contract']['scope_schema'] == v.SCOPE_SCHEMA
    assert result['company_order_sha256'] == result['baseline_company_order_sha256']
    assert not b._paths(tmp_path)['active'].exists() and frozen.read_bytes() == before
    assert not (tmp_path/'data/news_inbox').exists()


def test_legacy_audit_cannot_grade_v4(monkeypatch):
    monkeypatch.setattr(b, '_read_json', lambda *args: {'strategy_version': v.VERSION})
    with pytest.raises(b.BenchmarkError, match='legacy audit'):
        b.audit_run(Path('synthetic-no-write').resolve(), 'batchbench-v4-test', v.RUN_NAME)


def test_scope_axis_does_not_mutate_gold_or_resolve_unknown_delta():
    from tools.company_news_scope_audit_v1 import build_scope_audit, classify_failure
    e = {'match_cluster_id': 'root', 'ticker': 'X001', 'company': 'Example Works', 'date': '2026-08-26',
         'headline': '決算説明会 書き起こし', 'verdict': 'VALID_MATERIAL', 'primary_source_type': 'company_ir',
         'importance': 'high', 'earnings_relevance': 'direct', 'earnings_channel': 'margin'}
    gold = {'benchmark_id': 'synthetic', 'events': [e]}; before = copy.deepcopy(gold)
    result = build_scope_audit(gold, ['X001'], {})
    assert gold == before and result['critical7'][0]['scope_label'] == 'AMBIGUOUS'
    assert result['common48']['denominator_final'] is None
    assert result['common48']['denominator_upper_bound'] == 1
    failure = {'error_type': 'protocol_violation', 'error_message': 'Search before research-start'}
    assert not classify_failure(failure)['likely_avoidable_failure_under_v4']
