"""Blind scope packets, orthogonal response import and gated scorer skeleton.

No research, database access, scheduler calls or writes to existing artifacts.
Only scope_common48 is writable. Adjudicators receive its blind/ directory ONLY.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.company_news_atomic import atomic_write_json
from tools.company_news_strategy_v4 import IN_SCOPE, SCOPE_LABELS

PACKET_SCHEMA = 'company_news_scope_adjudication_packet_v1'
RESPONSE_SCHEMA = 'company_news_scope_adjudication_response_v1'
MANIFEST_SCHEMA = 'company_news_scope_adjudication_manifest_v1'
STATE_SCHEMA = 'company_news_scope_adjudication_state_v1'
EVENT_KEYS = {'event_id', 'ticker', 'company_name', 'published_at', 'publication_precision',
              'title', 'summary', 'source_urls', 'source_type', 'channel', 'category',
              'importance', 'earnings_relevance', 'materiality_context', 'evidence',
              'source_provenance', 'tdnet_same_content_evidence'}
ITEM_KEYS = {'event_id', 'scope_label', 'confidence', 'rationale', 'tdnet_redundancy',
             'incremental_information', 'evidence'}
FORBIDDEN = re.compile(r'found_by|candidate.presence|reference0[12]|ref0[12]|batch0[234](?:v[234])?|'
                       r'recall|precision(?!_date)|miss(?:ed|ing|_info)?\b|company_news_research_strategy|strategy[_ -](?:v\d|stage|version|name)|'
                       r'event-v2-[a-f0-9]+|ext-[a-f0-9]{8,}|gold_packet|gold_extension|'
                       r'どのrun|見逃し|検出状況|発見元', re.I)


class ScopeError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ScopeError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def packet_sha(packet):
    value = {k: v for k, v in packet.items() if k != 'packet_sha256'}
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def is_url(value):
    return isinstance(value, str) and urlsplit(value).scheme in {'http', 'https'} and bool(urlsplit(value).hostname)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def directory(root, benchmark_id):
    require(bool(re.fullmatch(r'batchbench-[A-Za-z0-9_-]+', benchmark_id)), 'invalid benchmark ID')
    return Path(root).resolve() / 'data/news_work/batch_benchmark' / benchmark_id / 'quality_v2/matcher_v2/scope_common48'


def blind_check(packet):
    require(set(packet) == {'schema_version', 'benchmark_id', 'packet_id', 'packet_sha256', 'events'}, 'unexpected packet fields')
    require(packet['schema_version'] == PACKET_SCHEMA, 'packet schema mismatch')
    for event in packet['events']:
        require(set(event) == EVENT_KEYS, 'unexpected event fields')
        require(re.fullmatch(r'scope-event-[a-f0-9]{20}', event['event_id']), 'nonblind event ID')
        require(all(is_url(u) for u in event['source_urls']), 'invalid source URL')
        require(nonempty(event['summary']) and event['evidence'], 'event context/evidence missing')
        for e in event['evidence']:
            require(set(e) == {'url', 'source_type', 'supports'} and is_url(e['url']) and nonempty(e['supports']), 'invalid public evidence')
        for source in event['source_provenance']:
            require(set(source) == {'url', 'source_type', 'role'}, 'invalid public provenance')
        # publication_precision is an explicitly permitted date-fidelity field.
        scan = {k: v for k, v in event.items() if k != 'publication_precision'}
        require(not FORBIDDEN.search(json.dumps(scan, ensure_ascii=False)), 'blind field/text leakage')
    require(len({e['event_id'] for e in packet['events']}) == len(packet['events']), 'duplicate event in packet')
    require(packet['packet_sha256'] == packet_sha(packet), 'packet SHA mismatch')


def public_event(event, scope_row, benchmark_id):
    """Allowlist only. Raw IDs, packet origin and detection never cross boundary."""
    ident = 'scope-event-' + sha((benchmark_id + ':' + scope_row['cluster_id']).encode())[:20]
    evidence = [{'url': e['url'], 'source_type': e.get('source_type'), 'supports': e['supports']}
                for e in event.get('evidence', [])]
    source_rows = []
    for e in evidence:
        source_rows.append({'url': e['url'], 'source_type': e['source_type'], 'role': 'supporting_evidence'})
    for key, role in [('primary_source_url', 'adjudicated_primary'), ('source_url', 'original_source')]:
        if event.get(key):
            source_rows.append({'url': event[key], 'source_type': event.get('primary_source_type' if key.startswith('primary') else 'source_type'), 'role': role})
    # Uniform source roles avoid leaking storage origin through different shapes.
    urls = sorted({u for u in scope_row.get('evidence_urls', []) if is_url(u)} | {s['url'] for s in source_rows})
    source_rows = sorted({(s['url'], s['source_type'] or 'unspecified'): {'url': s['url'], 'source_type': s['source_type'] or 'unspecified', 'role': 'existing_evidence'}
                          for s in source_rows}.values(), key=lambda s: (s['url'], s['source_type']))
    # TDNET equivalence is never inferred from a title or domain here.
    tdnet = [e for e in evidence if re.search(r'TDNET|TDnet|適時開示', e['supports'])]
    return {'event_id': ident, 'ticker': scope_row['ticker'], 'company_name': scope_row['company'],
            'published_at': scope_row['date'], 'publication_precision': 'date', 'title': scope_row['headline'],
            'summary': event.get('summary') or event.get('materiality_reason') or event.get('rationale'),
            'source_urls': urls, 'source_type': event.get('primary_source_type'), 'channel': event.get('earnings_channel'),
            'category': event.get('category'), 'importance': event.get('importance'), 'earnings_relevance': event.get('earnings_relevance'),
            'materiality_context': {'verdict': 'VALID_MATERIAL', 'reason': event.get('materiality_reason') or event.get('rationale')},
            'evidence': evidence, 'source_provenance': source_rows,
            'tdnet_same_content_evidence': {'status': 'REVIEW_EXISTING_EVIDENCE' if tdnet else 'NOT_ESTABLISHED', 'evidence': tdnet}}


def response_schema():
    string = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    item = {'type': 'object', 'additionalProperties': False, 'required': sorted(ITEM_KEYS), 'properties': {
        'event_id': {'type': 'string', 'pattern': '^scope-event-[a-f0-9]{20}$'},
        'scope_label': {'enum': sorted(SCOPE_LABELS)}, 'confidence': {'enum': ['HIGH', 'MEDIUM', 'LOW']},
        'rationale': string, 'tdnet_redundancy': {'enum': ['CONFIRMED', 'NOT_CONFIRMED', 'NOT_APPLICABLE', 'UNKNOWN']},
        'incremental_information': {'enum': ['YES', 'NO', 'UNKNOWN']},
        'evidence': {'type': 'array', 'minItems': 1, 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['url', 'supports'], 'properties': {'url': {'type': 'string', 'pattern': '^https?://[^/\\s]+'}, 'supports': string}}}}}
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'title': RESPONSE_SCHEMA,
            'type': 'object', 'additionalProperties': False,
            'required': ['schema_version', 'benchmark_id', 'packet_id', 'packet_sha256', 'adjudicated_at', 'items'],
            'properties': {'schema_version': {'const': RESPONSE_SCHEMA}, 'benchmark_id': string,
                'packet_id': {'type': 'string', 'pattern': '^scope_packet_[0-9]{3}$'},
                'packet_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'adjudicated_at': {'type': 'string', 'format': 'date-time'}, 'items': {'type': 'array', 'minItems': 1, 'items': item}}}


def build_bundle(scope, truth, *, expected_common=20, packet_size=5):
    require(5 <= packet_size <= 10, 'packet size must be 5..10')
    require(scope['benchmark_id'] == truth['benchmark_id'], 'benchmark mismatch')
    by_id = {e['match_cluster_id']: e for e in truth['events']}
    require(len(by_id) == len(truth['events']), 'duplicate truth identity')
    eligible = [e for e in scope['events'] if e['common48'] and e['gold_verdict'] == 'VALID_MATERIAL' and e['scope_label'] == 'AMBIGUOUS']
    require(len(eligible) == expected_common, 'Common48 ambiguous Material count mismatch')
    chosen_ids = {e['cluster_id'] for e in eligible}
    require(len(chosen_ids) == len(eligible), 'duplicate target')
    critical = [e for e in scope['events'] if e['known_critical'] and e['scope_label'] == 'AMBIGUOUS']
    extra = [e for e in critical if e['cluster_id'] not in chosen_ids]
    packet_groups = []
    for group in [eligible, extra]:
        ordered = sorted(group, key=lambda e: (e['ticker'], e['date'], e['headline']))
        packet_groups.extend(ordered[i:i+packet_size] for i in range(0, len(ordered), packet_size))
    packets, records, mapping = [], [], {}
    for number, group in enumerate(packet_groups, 1):
        events = []
        for row in group:
            e = by_id[row['cluster_id']]
            require(e['verdict'] == 'VALID_MATERIAL', 'nonmaterial truth target')
            require((e['ticker'], e['company'], e['date'], e['headline']) == (row['ticker'], row['company'], row['date'], row['headline']), 'scope/truth identity mismatch')
            public = public_event(e, row, scope['benchmark_id']); events.append(public)
            require(public['event_id'] not in mapping, 'event ID collision')
            mapping[public['event_id']] = {'cluster_id': row['cluster_id'], 'common48': row['common48'],
                'known_critical': row['known_critical'], 'ticker': row['ticker'], 'packet_id': f'scope_packet_{number:03d}'}
        packet = {'schema_version': PACKET_SCHEMA, 'benchmark_id': scope['benchmark_id'],
                  'packet_id': f'scope_packet_{number:03d}', 'events': events}
        packet['packet_sha256'] = packet_sha(packet); blind_check(packet); packets.append(packet)
        records.append({'packet_id': packet['packet_id'], 'filename': packet['packet_id'] + '.json',
                        'event_count': len(events), 'event_ids': [e['event_id'] for e in events],
                        'packet_sha256': packet['packet_sha256'], 'file_sha256': sha(json_text(packet).encode())})
    manifest = {'schema_version': MANIFEST_SCHEMA, 'benchmark_id': scope['benchmark_id'],
                'packet_schema': PACKET_SCHEMA, 'response_schema': RESPONSE_SCHEMA, 'packets': records,
                'event_count': len(mapping), 'packet_count': len(records)}
    private = {'benchmark_id': scope['benchmark_id'], 'mapping': mapping,
               'common48_ambiguous_count': len(eligible), 'supplemental_critical_count': len(extra),
               'critical_ambiguous_coverage': [{'ticker': e['ticker'], 'cluster_id': e['cluster_id'], 'included': True} for e in critical],
               'manifest_sha256': sha(json_text(manifest).encode()), 'input_sha256': {}}
    return manifest, packets, private


def build_from_repo(root, benchmark_id):
    base = directory(root, benchmark_id).parent
    paths = [base/'strategy_v4_scope_review/news_monitor_scope_v1.json', base/'post_run_batch04v2/batch04v2_extended_gold_final_audit.json']
    raw = [p.read_bytes() for p in paths]
    scope, extended = [json.loads(x.decode('utf-8-sig')) for x in raw]
    require(scope['schema_version'] == 'news_monitor_scope_v1', 'scope schema mismatch')
    require(scope['benchmark_id'] == benchmark_id == extended['benchmark_id'], 'input benchmark mismatch')
    common = set(extended['common48']['tickers'])
    require(all(e['common48'] == (e['ticker'] in common) for e in scope['events']), 'Common48 membership mismatch')
    result = build_bundle(scope, extended['normalized_truth_view'])
    result[2]['input_sha256'] = {str(p.resolve()): sha(content) for p, content in zip(paths, raw)}
    instructions = (Path(root)/'config/company_news_scope_adjudication_instructions_v1.md').read_text(encoding='utf-8')
    result[2]['support_sha256'] = {'ADJUDICATION_INSTRUCTIONS.md': sha(instructions.encode()),
                                 'response_schema.json': sha(json_text(response_schema()).encode())}
    return result


def verify_bundle(base):
    base = Path(base)
    manifest_path = base/'blind/manifest.json'
    private = read_json(base/'private/control.json')
    require(sha(manifest_path.read_bytes()) == private['manifest_sha256'], 'manifest changed')
    manifest = read_json(manifest_path)
    require(manifest['schema_version'] == MANIFEST_SCHEMA and manifest['benchmark_id'] == private['benchmark_id'], 'manifest identity mismatch')
    for path, expected in private['input_sha256'].items():
        require(sha(Path(path).read_bytes()) == expected, 'frozen input changed')
    for name, expected in private.get('support_sha256', {}).items():
        require(name in {'ADJUDICATION_INSTRUCTIONS.md', 'response_schema.json'}, 'unsafe support file')
        require(sha((base/'blind'/name).read_bytes()) == expected, 'rubric/schema changed')
    packets, ids = {}, []
    for record in manifest['packets']:
        require(record['filename'] == record['packet_id'] + '.json' and re.fullmatch(r'scope_packet_\d{3}', record['packet_id']), 'unsafe packet filename')
        raw = (base/'blind'/record['filename']).read_bytes()
        require(sha(raw) == record['file_sha256'], 'packet file SHA mismatch')
        packet = json.loads(raw.decode('utf-8')); blind_check(packet)
        require(packet['packet_sha256'] == record['packet_sha256'] and packet['packet_id'] == record['packet_id']
                and packet['benchmark_id'] == manifest['benchmark_id'], 'packet manifest mismatch')
        event_ids = [e['event_id'] for e in packet['events']]
        require(event_ids == record['event_ids'] and len(event_ids) == record['event_count'], 'packet coverage mismatch')
        ids.extend(event_ids); packets[packet['packet_id']] = packet
    require(len(packets) == manifest['packet_count'] and len(ids) == len(set(ids)) == manifest['event_count'], 'bundle duplicate/count mismatch')
    require(set(ids) == set(private['mapping']), 'private mapping coverage mismatch')
    for ident in ids:
        expected_id = 'scope-event-' + sha((manifest['benchmark_id'] + ':' + private['mapping'][ident]['cluster_id']).encode())[:20]
        require(ident == expected_id, 'private mapping identity changed')
        require(ident in [e['event_id'] for e in packets[private['mapping'][ident]['packet_id']]['events']], 'mapping packet mismatch')
    return manifest, packets, private


def validate_response(response, packet):
    blind_check(packet)
    require(isinstance(response, dict) and set(response) == {'schema_version', 'benchmark_id', 'packet_id', 'packet_sha256', 'adjudicated_at', 'items'}, 'response schema fields mismatch')
    require(response['schema_version'] == RESPONSE_SCHEMA, 'response schema version')
    for key in ('benchmark_id', 'packet_id', 'packet_sha256'):
        require(response[key] == packet[key], 'response ' + key + ' mismatch')
    require(nonempty(response['adjudicated_at']), 'adjudication timestamp missing')
    try:
        timestamp = datetime.fromisoformat(response['adjudicated_at'].replace('Z', '+00:00'))
    except ValueError as exc:
        raise ScopeError('invalid adjudication datetime') from exc
    require(timestamp.tzinfo is not None, 'timezone required')
    items = response['items']; require(isinstance(items, list), 'items must be array')
    expected = {e['event_id'] for e in packet['events']}; seen = set()
    for item in items:
        require(isinstance(item, dict) and set(item) == ITEM_KEYS, 'response item fields mismatch')
        ident = item['event_id']; require(ident in expected and ident not in seen, 'unknown/duplicate event'); seen.add(ident)
        require(item['scope_label'] in SCOPE_LABELS, 'invalid scope label')
        require(item['confidence'] in {'HIGH', 'MEDIUM', 'LOW'} and nonempty(item['rationale']), 'confidence/rationale invalid')
        require(item['tdnet_redundancy'] in {'CONFIRMED', 'NOT_CONFIRMED', 'NOT_APPLICABLE', 'UNKNOWN'}, 'invalid redundancy enum')
        require(item['incremental_information'] in {'YES', 'NO', 'UNKNOWN'}, 'invalid incremental enum')
        if item['scope_label'] == 'OUT_OF_SCOPE_TDNET':
            require(item['tdnet_redundancy'] == 'CONFIRMED' and item['incremental_information'] == 'NO', 'TDNET exclusion needs confirmed equivalence and no increment')
        if item['scope_label'] == 'IN_SCOPE_INCREMENTAL_IR':
            require(item['incremental_information'] == 'YES', 'incremental IR needs YES')
        if item['scope_label'] in IN_SCOPE:
            require(item['tdnet_redundancy'] != 'CONFIRMED' or item['incremental_information'] == 'YES', 'in-scope conflicts with same-content TDNET')
        require(isinstance(item['evidence'], list) and item['evidence'], 'response evidence required')
        for e in item['evidence']:
            require(isinstance(e, dict) and set(e) == {'url', 'supports'} and is_url(e['url']) and nonempty(e['supports']), 'response evidence invalid')
    require(seen == expected, 'missing event')
    return response


@contextmanager
def import_lock(base):
    path = base/'private/import.lock'
    # No stale-PID heuristics or process signals. Crash leftovers fail closed.
    try:
        handle = path.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ScopeError('scope import busy; inspect stale lock manually if needed') from exc
    try:
        yield
    finally:
        handle.close()
        path.unlink()


def import_response(root, benchmark_id, input_path):
    base = directory(root, benchmark_id)
    _, packets, private = verify_bundle(base)
    response = read_json(input_path)
    require(response.get('packet_id') in packets, 'response packet unknown')
    validate_response(response, packets[response['packet_id']])  # No mutation before complete validation.
    with import_lock(base):
        manifest, packets, private = verify_bundle(base)
        validate_response(response, packets[response['packet_id']])
        state_path = base/'private/state.json'
        state = read_json(state_path) if state_path.exists() else {'schema_version': STATE_SCHEMA,
            'benchmark_id': benchmark_id, 'manifest_sha256': private['manifest_sha256'], 'responses': {}}
        require(state['schema_version'] == STATE_SCHEMA and state['benchmark_id'] == benchmark_id
                and state['manifest_sha256'] == private['manifest_sha256'], 'scope state identity mismatch')
        for pid, saved in state['responses'].items():
            require(pid in packets, 'unknown imported packet')
            validate_response(saved, packets[pid])
        prior = state['responses'].get(response['packet_id'])
        if prior is not None:
            require(prior == response, 'different response already imported; explicit reviewed revision needed')
            return {'status': 'ALREADY_IMPORTED', 'idempotent': True, 'state_path': str(state_path)}
        state['responses'][response['packet_id']] = response
        # One atomic state contains both responses and provenance; no split ledger update.
        atomic_write_json(state_path, state)
        return {'status': 'SCOPE_RESPONSES_COMPLETE' if len(state['responses']) == len(manifest['packets']) else 'SCOPE_RESPONSES_PARTIAL',
                'idempotent': False, 'imported_packet_count': len(state['responses']), 'state_path': str(state_path)}


def score_common48(scope, private, responses, found_cluster_ids):
    """Pure scorer skeleton. Responses must be validated by the caller first.

    All 20 decisions must exist; unresolved scope is disclosed and excluded.
    A zero denominator has recall=None, never a free 100% pass.
    """
    labels = {}
    for response in responses:
        for item in response['items']:
            require(item['event_id'] in private['mapping'] and item['event_id'] not in labels, 'duplicate/unknown scoring response')
            require(item['scope_label'] in SCOPE_LABELS, 'invalid scoring label')
            labels[item['event_id']] = item['scope_label']
    require(set(labels) == set(private['mapping']), 'adjudication incomplete; formal metric blocked')
    updated = {v['cluster_id']: labels[k] for k, v in private['mapping'].items()}
    material = [e for e in scope['events'] if e['common48'] and e['gold_verdict'] == 'VALID_MATERIAL']
    scoped = {e['cluster_id'] for e in material if updated.get(e['cluster_id'], e['scope_label']) in IN_SCOPE}
    ambiguous = {e['cluster_id'] for e in material if updated.get(e['cluster_id'], e['scope_label']) == 'AMBIGUOUS'}
    critical = {e['cluster_id'] for e in material if e['known_critical']} & scoped
    found = set(found_cluster_ids)
    return {'metric': 'non_tdnet_material_recall_v1', 'denominator': len(scoped), 'found': len(scoped & found),
            'recall': len(scoped & found)/len(scoped) if scoped else None, 'ambiguous_excluded': len(ambiguous),
            'scope_fully_resolved': not ambiguous, 'old_critical_in_scope_count': len(critical),
            'old_critical_in_scope_recall': len(critical & found)/len(critical) if critical else None,
            'old_critical_is_production_hard_gate': False, 'production_approval': False}


def score_imported_common48(root, benchmark_id, found_cluster_ids):
    """Read-only trusted entry: verify frozen inputs and every imported response.

    There is deliberately no score CLI or automatic call during preparation.
    The supplied found IDs must come from a separately approved matching audit.
    """
    base = directory(root, benchmark_id)
    _, packets, private = verify_bundle(base)
    path = base/'private/state.json'
    require(path.exists(), 'adjudication incomplete; no response state')
    state = read_json(path)
    require(state.get('schema_version') == STATE_SCHEMA and state.get('benchmark_id') == benchmark_id
            and state.get('manifest_sha256') == private['manifest_sha256'], 'scope state identity mismatch')
    require(set(state['responses']) == set(packets), 'adjudication incomplete; formal metric blocked')
    for pid, response in state['responses'].items():
        validate_response(response, packets[pid])
    scope_path = base.parent/'strategy_v4_scope_review/news_monitor_scope_v1.json'
    require(str(scope_path.resolve()) in private['input_sha256'], 'scope input is not pinned')
    return score_common48(read_json(scope_path), private, list(state['responses'].values()), found_cluster_ids)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import-scope-adjudication-v1')
    imp.add_argument('--benchmark-id', required=True); imp.add_argument('--input', type=Path, required=True)
    verify = sub.add_parser('verify-scope-packets-v1'); verify.add_argument('--benchmark-id', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'import-scope-adjudication-v1':
            value = import_response(args.root, args.benchmark_id, args.input)
        else:
            manifest, _, _ = verify_bundle(directory(args.root, args.benchmark_id))
            value = {'status': 'VERIFIED', 'packet_count': manifest['packet_count'], 'event_count': manifest['event_count']}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ScopeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr); return 1


if __name__ == '__main__':
    raise SystemExit(main())
