"""Synthetic blind scope packet and import regressions. No real adjudication."""
import copy
import json
from pathlib import Path

import pytest

from tools import company_news_scope_adjudication_v1 as s

BID = 'batchbench-scope-test'


def fixture():
    rows, truth = [], []
    for i in range(20):
        ident = ('ext-' if i % 2 else 'event-v2-') + f'{i:016x}'
        row = {'cluster_id': ident, 'ticker': f'X{i:03d}', 'company': f'Example {i}',
               'date': '2026-08-26', 'headline': f'Synthetic capacity event {i}',
               'gold_verdict': 'VALID_MATERIAL', 'scope_label': 'AMBIGUOUS',
               'common48': True, 'known_critical': i < 3, 'evidence_urls': [f'https://example.test/{i}']}
        rows.append(row)
        truth.append({'match_cluster_id': ident, 'ticker': row['ticker'], 'company': row['company'],
            'date': row['date'], 'headline': row['headline'], 'verdict': 'VALID_MATERIAL',
            'summary': 'Capacity information with company linkage', 'materiality_reason': 'Affects sales volume',
            'primary_source_url': row['evidence_urls'][0], 'primary_source_type': 'company_news',
            'category': 'capacity', 'importance': 'medium', 'earnings_relevance': 'likely', 'earnings_channel': 'capacity',
            'evidence': [{'url': row['evidence_urls'][0], 'source_type': 'company_news', 'supports': 'New capacity evidence'}],
            'found_by': {'reference01': True, 'batch04v3': False}, 'strategy_name': 'forbidden private value'})
    return {'benchmark_id': BID, 'events': rows}, {'benchmark_id': BID, 'events': truth}


def bundle():
    return s.build_bundle(*fixture())


def response(packet, label='AMBIGUOUS'):
    return {'schema_version': s.RESPONSE_SCHEMA, 'benchmark_id': BID, 'packet_id': packet['packet_id'],
        'packet_sha256': packet['packet_sha256'], 'adjudicated_at': '2026-09-04T12:00:00+09:00',
        'items': [{'event_id': e['event_id'], 'scope_label': label, 'confidence': 'LOW',
                   'rationale': 'Comparison evidence remains incomplete', 'tdnet_redundancy': 'UNKNOWN',
                   'incremental_information': 'UNKNOWN',
                   'evidence': [{'url': 'https://example.test/evidence', 'supports': 'Available document reviewed'}]}
                  for e in packet['events']]}


def test_blind_allowlist_opaque_ids_and_determinism():
    original, truth = fixture(); before = copy.deepcopy((original, truth))
    m, packets, private = s.build_bundle(original, truth)
    assert len(packets) == 4 and [len(p['events']) for p in packets] == [5]*4
    assert len(private['mapping']) == 20 and private['supplemental_critical_count'] == 0
    for packet in packets:
        s.blind_check(packet)
        for e in packet['events']:
            assert set(e) == s.EVENT_KEYS
            assert 'found_by' not in str(e) and 'event-v2-' not in str(e) and 'ext-' not in str(e)
    assert (original, truth) == before
    assert (m, packets, private) == s.build_bundle(original, truth)


@pytest.mark.parametrize('key,value', [('common48', False), ('gold_verdict', 'VALID_NON_MATERIAL'), ('scope_label', 'IN_SCOPE_COMPANY_WEB')])
def test_targets_only_common48_ambiguous_material(key, value):
    scope, truth = fixture(); scope['events'][0][key] = value
    with pytest.raises(s.ScopeError, match='count'): s.build_bundle(scope, truth)


def test_duplicate_target_rejected_and_critical_extra_separate():
    scope, truth = fixture(); scope['events'][-1] = copy.deepcopy(scope['events'][0])
    with pytest.raises(s.ScopeError, match='duplicate target'): s.build_bundle(scope, truth)
    scope, truth = fixture()
    extra = dict(scope['events'][0], cluster_id='extra-critical', ticker='X999', common48=False)
    scope['events'].append(extra)
    truth['events'].append(dict(truth['events'][0], match_cluster_id='extra-critical', ticker='X999'))
    _, packets, private = s.build_bundle(scope, truth)
    assert len(packets) == 5 and private['supplemental_critical_count'] == 1
    assert len(private['mapping']) == 21


@pytest.mark.parametrize('field,content', [('summary', 'reference01 found it'), ('summary', 'batch04v3 miss'),
                                        ('summary', 'strategy v4'), ('source_urls', ['https://host.test/batch02/result'])])
def test_embedded_blind_leak_rejected(field, content):
    _, packets, _ = bundle(); p = packets[0]; p['events'][0][field] = content
    p['packet_sha256'] = s.packet_sha(p)
    with pytest.raises(s.ScopeError, match='leakage'): s.blind_check(p)


def test_corporate_strategy_url_is_not_experiment_leak():
    scope, truth = fixture()
    truth['events'][0]['evidence'][0]['url'] = 'https://example.test/about/strategy'
    assert s.build_bundle(scope, truth)


def test_packet_sha_and_response_identity():
    _, packets, _ = bundle(); p = packets[0]; r = response(p)
    assert s.validate_response(r, p) == r
    p['events'][0]['title'] += ' changed'
    with pytest.raises(s.ScopeError, match='SHA'): s.validate_response(r, p)


@pytest.mark.parametrize('change,match', [('label', 'label'), ('missing', 'missing'), ('duplicate', 'duplicate'),
    ('extra', 'unknown'), ('sha', 'SHA|sha'), ('alias', 'fields'), ('url', 'evidence'), ('datetime', 'timezone')])
def test_response_invalid_rejected(change, match):
    _, packets, _ = bundle(); p = packets[0]; r = response(p)
    if change == 'label': r['items'][0]['scope_label'] = 'YES'
    if change == 'missing': r['items'].pop()
    if change == 'duplicate': r['items'].append(copy.deepcopy(r['items'][0]))
    if change == 'extra': r['items'][0]['event_id'] = 'scope-event-' + 'f'*20
    if change == 'sha': r['packet_sha256'] = '0'*64
    if change == 'alias': r['items'][0]['label'] = r['items'][0].pop('scope_label')
    if change == 'url': r['items'][0]['evidence'][0]['url'] = 'file:///local'
    if change == 'datetime': r['adjudicated_at'] = '2026-09-04T12:00:00'
    with pytest.raises(s.ScopeError, match=match): s.validate_response(r, p)


def test_semantic_scope_consistency_and_schema_contract():
    _, packets, _ = bundle(); p = packets[0]; r = response(p)
    r['items'][0]['scope_label'] = 'OUT_OF_SCOPE_TDNET'
    with pytest.raises(s.ScopeError, match='equivalence'): s.validate_response(r, p)
    r['items'][0].update(tdnet_redundancy='CONFIRMED', incremental_information='NO')
    assert s.validate_response(r, p)
    assert set(s.response_schema()['properties']['items']['items']['required']) == s.ITEM_KEYS


def test_scorer_gates_complete_responses_and_excludes_out_and_ambiguous():
    scope, _ = fixture(); _, packets, private = bundle()
    with pytest.raises(s.ScopeError, match='incomplete'): s.score_common48(scope, private, [], [])
    responses = [response(p) for p in packets]
    responses[0]['items'][0].update(scope_label='IN_SCOPE_COMPANY_WEB', tdnet_redundancy='NOT_CONFIRMED')
    responses[0]['items'][1].update(scope_label='OUT_OF_SCOPE_TDNET', tdnet_redundancy='CONFIRMED', incremental_information='NO')
    responses[0]['items'][2]['scope_label'] = 'OUT_OF_SCOPE_TRIVIAL'
    for r, p in zip(responses, packets): s.validate_response(r, p)
    found = [private['mapping'][responses[0]['items'][0]['event_id']]['cluster_id']]
    result = s.score_common48(scope, private, responses, found)
    assert result['denominator'] == result['found'] == 1 and result['recall'] == 1
    assert result['ambiguous_excluded'] == 17
    assert result['old_critical_in_scope_count'] == 1
    assert not result['old_critical_is_production_hard_gate'] and not result['production_approval']


def test_zero_denominator_is_not_perfect_recall():
    scope, _ = fixture(); _, packets, private = bundle()
    result = s.score_common48(scope, private, [response(p) for p in packets], [])
    assert result['denominator'] == 0 and result['recall'] is None


def write_bundle(root):
    base = s.directory(root, BID); m, packets, private = bundle()
    (base/'blind').mkdir(parents=True); (base/'private').mkdir()
    for p in packets: (base/'blind'/(p['packet_id']+'.json')).write_text(s.json_text(p), encoding='utf-8', newline='')
    (base/'blind/manifest.json').write_text(s.json_text(m), encoding='utf-8', newline='')
    (base/'private/control.json').write_text(s.json_text(private), encoding='utf-8', newline='')
    return base, packets


def test_import_atomic_idempotent_scope_only(tmp_path, monkeypatch):
    base, packets = write_bundle(tmp_path)
    original = tmp_path/'gold.json'; original.write_text('frozen original', encoding='utf-8')
    before = original.read_bytes()
    p = tmp_path/'response.json'; p.write_text(s.json_text(response(packets[0])), encoding='utf-8')
    assert s.import_response(tmp_path, BID, p)['imported_packet_count'] == 1
    state = (base/'private/state.json').read_bytes()
    assert s.import_response(tmp_path, BID, p)['idempotent'] is True
    assert (base/'private/state.json').read_bytes() == state
    assert original.read_bytes() == before and not (base/'private/import.lock').exists()
    p.write_text(s.json_text(response(packets[1])), encoding='utf-8')
    def fail_write(*args): raise OSError('synthetic write failure')
    monkeypatch.setattr(s, 'atomic_write_json', fail_write)
    with pytest.raises(OSError): s.import_response(tmp_path, BID, p)
    assert (base/'private/state.json').read_bytes() == state and not (base/'private/import.lock').exists()


def test_file_tamper_or_invalid_response_has_no_mutation(tmp_path):
    base, packets = write_bundle(tmp_path)
    p = tmp_path/'response.json'; r = response(packets[0]); r['items'].pop()
    p.write_text(s.json_text(r), encoding='utf-8')
    with pytest.raises(s.ScopeError, match='missing'): s.import_response(tmp_path, BID, p)
    assert not (base/'private/state.json').exists() and not (base/'private/import.lock').exists()
    p.write_text(s.json_text(response(packets[0])), encoding='utf-8')
    target = base/'blind/scope_packet_001.json'; target.write_bytes(target.read_bytes() + b' ')
    with pytest.raises(s.ScopeError, match='file SHA'): s.import_response(tmp_path, BID, p)
    assert not (base/'private/state.json').exists()
