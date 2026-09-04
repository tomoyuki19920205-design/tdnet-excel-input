"""Synthetic scope regressions; never touch real runs or production state."""
import copy
from pathlib import Path

import pytest

from tools import company_news_batch_benchmark as b
from tools import company_news_strategy_v3 as v3
from tools import company_news_strategy_v4 as v


def fixture():
    a = {'assignment_id': 'synthetic-a1', 'ticker': 'X001', 'company_name': 'Example Works',
         'search_from': '2026-08-25', 'search_to': '2026-08-31', 'strategy_version': v.VERSION}
    index = 'https://example.test/news'
    src = {'url': index, 'source_type': 'company_news', 'content_verified': True,
           'evidence_excerpt': 'Official identity and dated index content', 'checked_at': '2026-09-04T00:00:00+00:00'}
    t = {'schema_version': v.SCHEMA, 'strategy_version': v.VERSION, 'assignment_id': a['assignment_id'], 'ticker': a['ticker'],
         'company_identity': {'verified_ticker': a['ticker'], 'source_url': index, 'evidence': 'Ticker and company identity', 'aliases': []},
         'queries': [{'channel': c, 'query': 'Example Works X001 ' + c, 'executed': True, 'result_evidence': 'Reviewed results'}
                     for c in ('official', 'external_media', 'government')],
         'query_budget_exceeded': False, 'query_budget_note': '',
         'official_indexes': [{'url': index, 'window_from': a['search_from'], 'window_to': a['search_to'],
                               'enumeration_complete': True, 'scan_evidence': 'All relevant dated pages enumerated'}],
         'documents': [], 'coverage_gaps': [], 'coverage_status': 'COMPLETE', 'source_provenance': [src],
         'candidate_provenance': [], 'candidate_count': 0, 'final_event_count': 0,
         'no_news_gate_pass': True, 'stopped_after_first_event': False, 'total_research_elapsed_seconds': 12}
    return a, t, {'sources_checked_count': 1, 'items': []}


def document(title, label):
    return {'date': '2026-08-26', 'title': title, 'url': 'https://example.test/doc.pdf', 'scope_label': label,
            'scope_reason': 'Evidence-backed scope reason', 'body_status': 'INACCESSIBLE',
            'index_evidence': title + ' index text', 'tdnet_same_content_verified': True,
            'tdnet_url': 'https://www.release.tdnet.info/inbs/example.pdf',
            'tdnet_identity_evidence': 'Same dated disclosure identifier and title', 'incremental_information': False}


@pytest.mark.parametrize('title,label', [
    ('決算短信', 'OUT_OF_SCOPE_TDNET'), ('人事異動', 'OUT_OF_SCOPE_TDNET'),
    ('夏季休業日程', 'OUT_OF_SCOPE_TRIVIAL'), ('児童図書の寄付', 'OUT_OF_SCOPE_TRIVIAL'),
    ('ESGサイトランキング', 'OUT_OF_SCOPE_TRIVIAL')])
def test_unread_out_of_scope_pdf_is_not_failure(title, label):
    a, t, p = fixture(); t['documents'] = [document(title, label)]
    assert v.validate(t, a, p)['coverage_status'] == 'COMPLETE'


@pytest.mark.parametrize('title', ['会社HP価格改定', '会社HP大型提携'])
def test_unread_material_candidate_is_partial_not_no_news(title):
    a, t, p = fixture(); d = document(title, 'IN_SCOPE_COMPANY_WEB'); d['attempt_evidence'] = 'Primary and alternate retrieval failed'
    t.update(documents=[d], coverage_status='PARTIAL', no_news_gate_pass=False,
             coverage_gaps=[{'url': d['url'], 'kind': 'in_scope_document', 'reason': 'Body unavailable', 'retry_required': True}])
    assert v.validate(t, a, p)['coverage_status'] == 'PARTIAL'
    t['no_news_gate_pass'] = True
    with pytest.raises(b.BenchmarkError, match='no-news'): v.validate(t, a, p)


def test_verified_english_alias_and_nfkc():
    a, t, p = fixture(); a['company_name'] = '例示工業'
    t['company_identity']['aliases'] = [{'name': 'Example Works', 'verified_ticker': 'X001',
        'source_url': t['source_provenance'][0]['url'], 'evidence': 'Official English identity'}]
    assert v.validate(t, a, p)
    a['company_name'] = 'Ｅｘａｍｐｌｅ　Ｗｏｒｋｓ'; t['company_identity']['aliases'] = []
    assert v.validate(t, a, p)


def test_wrong_company_fails_closed():
    a, t, p = fixture(); t['queries'][0]['query'] = 'Different Enterprise X002 official'
    with pytest.raises(b.BenchmarkError, match='another'): v.validate(t, a, p)
    a, t, p = fixture(); t['company_identity']['verified_ticker'] = 'X002'
    with pytest.raises(b.BenchmarkError, match='identity'): v.validate(t, a, p)


def select_pr(t, p):
    d = document('New customer adoption', 'IN_SCOPE_COMPANY_WEB')
    d.update(url='https://example.test/pr.pdf', final_url='https://example.test/pr.pdf', body_status='VERIFIED')
    t['documents'].append(d)
    t['source_provenance'].append({**t['source_provenance'][0], 'url': d['url']})
    t['candidate_provenance'] = [{'candidate_index': 0, 'document_url': d['url'], 'selected_for_output': True,
        'selection_reason': 'New exclusive customer contract', 'headline': d['title'], 'source_url': d['url'],
        'economic_transmission': 'Customer contract supports revenue', 'event_key': 'customer-adoption'}]
    t.update(candidate_count=1, final_event_count=1, no_news_gate_pass=False)
    p.update(sources_checked_count=2, items=[{'headline': d['title'], 'source_url': d['url']}])


def test_same_day_tdnet_skip_preserves_exclusive_pr():
    a, t, p = fixture(); t['documents'].append(document('決算短信', 'OUT_OF_SCOPE_TDNET'))
    select_pr(t, p)
    assert v.validate(t, a, p)
    assert len(p['items']) == 1


def test_skip_and_candidate_fraud_rejected():
    a, t, p = fixture(); t['documents'] = [document('価格改定', 'OUT_OF_SCOPE_TRIVIAL')]
    with pytest.raises(b.BenchmarkError, match='material title'): v.validate(t, a, p)
    t['documents'] = [document('決算短信', 'OUT_OF_SCOPE_TDNET')]; t['documents'][0]['tdnet_same_content_verified'] = False
    with pytest.raises(b.BenchmarkError, match='equivalence'): v.validate(t, a, p)


def test_budget_is_advisory_but_missing_channel_is_failure():
    a, t, p = fixture(); t['queries'] += [copy.deepcopy(t['queries'][0]) for _ in range(4)]
    t.update(query_budget_exceeded=True, query_budget_note='Necessary extra identity discovery; now complete')
    assert v.validate(t, a, p)
    t['queries'] = [q for q in t['queries'] if q['channel'] != 'government']
    with pytest.raises(b.BenchmarkError, match='channel absent'): v.validate(t, a, p)


def test_incremental_ir_requires_delta_and_no_mutation():
    a, t, p = fixture(); select_pr(t, p)
    t['documents'][0]['scope_label'] = 'IN_SCOPE_INCREMENTAL_IR'
    with pytest.raises(b.BenchmarkError, match='delta'): v.validate(t, a, p)
    t['documents'][0]['incremental_information_evidence'] = 'Q&A adds new capacity assumption absent from compared release'
    before = copy.deepcopy(t)
    assert v.validate(t, a, p) == before


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
    b._prepare_named_run(tmp_path, bid, cohort, companies, 4, 'batch04', reference=False,
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
