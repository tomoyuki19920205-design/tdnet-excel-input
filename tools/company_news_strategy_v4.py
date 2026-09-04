"""Benchmark-only complementary-news contract. No fetching or state writes.

Gold scope is an independent axis. Neither this validator nor the runtime prompt
loads adjudication labels. V3 remains frozen and uses its original validator.
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date

from tools import company_news_batch_benchmark as b
from tools import company_news_strategy_v3 as v3

VERSION = 'company_news_research_strategy_v4'
RUN_NAME = 'batch04v4'
SCHEMA = 'company_news_benchmark_strategy_telemetry_v4'
SCOPE_SCHEMA = 'news_monitor_scope_v1'
PROMPT_NAME = 'company_news_research_strategy_v4_prompt.md'
IN_SCOPE = {'IN_SCOPE_COMPANY_WEB', 'IN_SCOPE_EXTERNAL_MEDIA', 'IN_SCOPE_GOVERNMENT',
            'IN_SCOPE_COUNTERPARTY', 'IN_SCOPE_INCREMENTAL_IR'}
OUT_SCOPE = {'OUT_OF_SCOPE_TDNET', 'OUT_OF_SCOPE_TRIVIAL'}
SCOPE_LABELS = IN_SCOPE | OUT_SCOPE | {'AMBIGUOUS'}
MATERIAL_TITLE = re.compile(r'価格改定|値上げ|値下げ|提携|受注|生産停止|工場停止|操業|設備投資|顧客採用|行政処分|回収|不具合|承認|供給契約|capacity|price|partnership|recall|order', re.I)


def require(ok, message):
    if not ok:
        raise b.BenchmarkError('strategy v4: ' + message)


def normalized(value):
    return ''.join(unicodedata.normalize('NFKC', value).casefold().split())


def contract():
    return {'strategy_version': VERSION, 'scope_schema': SCOPE_SCHEMA,
            'scope_labels': sorted(SCOPE_LABELS), 'production_write_forbidden': True,
            'official_query_target_max': 3, 'total_query_target_max': 6,
            'query_budget_is_failure_gate': False, 'budget_transfer_between_companies': False,
            'document_follow_counts_as_query': False, 'coverage_target': 'IN_SCOPE_AND_UNRESOLVED_CANDIDATES_ONLY',
            'required_channels': ['official', 'external_media', 'government'],
            'partial_is_no_news': False, 'partial_retry': 'REVIEW_ONLY_NO_AUTOMATIC_RERUN',
            'candidate_only_event_policy': 'NOT_A_FALSE_POSITIVE_PENDING_BLIND_ADJUDICATION',
            'precision_status_after_run': 'PROVISIONAL_PENDING_SCOPE_AND_BLIND_ADJUDICATION'}


def evaluation_policy():
    return {'scope_schema': SCOPE_SCHEMA, 'denominator': 'NORMALIZED_VALID_MATERIAL_AND_CONFIRMED_IN_SCOPE',
            'ambiguous_policy': 'REVIEW_REQUIRED_EXPOSE_LOWER_AND_UPPER_DENOMINATORS',
            'out_of_scope_miss_is_quality_failure': False, 'legacy_critical7_is_unfiltered_gate': False,
            'partial_is_complete_research': False, 'same_cohort_pass_authorizes_production': False,
            'fresh_unseen_gate_required': True, 'scheduler_reliability': 'SEPARATE_GATE'}


def prompt_text(root):
    return (root / 'config' / PROMPT_NAME).read_text(encoding='utf-8').replace('{{REPO}}', str(root.resolve()))


leakage_scan = v3.leakage_scan


def scope_decision(item):
    """Validate an evidence-backed scope claim, not an automatic materiality call."""
    label = item.get('scope_label')
    require(label in SCOPE_LABELS and v3.text(item.get('scope_reason')), 'scope evidence missing')
    if label == 'OUT_OF_SCOPE_TDNET':
        require(item.get('tdnet_same_content_verified') is True and v3.url(item.get('tdnet_url'))
                and v3.text(item.get('tdnet_identity_evidence'))
                and item.get('incremental_information') is False, 'TDNET equivalence not established')
    if label == 'OUT_OF_SCOPE_TRIVIAL':
        require(v3.text(item.get('index_evidence')), 'trivial index evidence missing')
        require(not MATERIAL_TITLE.search(item.get('title', '')), 'potentially material title cannot be skipped as trivial')
    if label == 'IN_SCOPE_INCREMENTAL_IR':
        require(v3.text(item.get('incremental_information_evidence')), 'incremental IR delta missing')
    return label


def validate(value, assignment, payload):
    require(isinstance(value, dict), 'telemetry must be object')
    v = value
    require(v.get('schema_version') == SCHEMA and v.get('strategy_version') == VERSION
            and assignment.get('strategy_version') == VERSION, 'version mismatch')
    require(v.get('assignment_id') == assignment['assignment_id'] and v.get('ticker') == assignment['ticker'], 'assignment mismatch')
    sources = v.get('source_provenance')
    require(isinstance(sources, list) and sources, 'verified provenance required')
    source_map = {}
    for s in sources:
        require(isinstance(s, dict) and v3.url(s.get('url')) and v3.text(s.get('source_type'))
                and s.get('content_verified') is True and v3.text(s.get('evidence_excerpt')), 'source body evidence required')
        v3.stamp(s.get('checked_at'))
        require(s['url'] not in source_map, 'duplicate source URL')
        source_map[s['url']] = s
    require(payload.get('sources_checked_count') == len(source_map), 'source count mismatch')
    identity = v.get('company_identity', {})
    require(identity.get('verified_ticker') == assignment['ticker'] and identity.get('source_url') in source_map
            and v3.text(identity.get('evidence')), 'wrong or unverified company identity')
    official_types = {'company', 'company_news', 'company_ir', 'ir', 'company_official'}
    require(source_map[identity['source_url']]['source_type'] in official_types, 'identity needs official evidence')
    names = {normalized(assignment['company_name'])}
    for alias in identity.get('aliases', []):
        require(v3.text(alias.get('name')) and alias.get('verified_ticker') == assignment['ticker']
                and alias.get('source_url') in source_map and v3.text(alias.get('evidence')), 'unverified alias')
        require(source_map[alias['source_url']]['source_type'] in official_types, 'alias needs official evidence')
        names.add(normalized(alias['name']))
    queries = v.get('queries')
    require(isinstance(queries, list) and queries, 'mandatory searches not executed')
    channels = set()
    for q in queries:
        require(isinstance(q, dict) and q.get('channel') in {'official', 'external_media', 'government', 'counterparty'}
                and v3.text(q.get('query')) and v3.text(q.get('result_evidence'))
                and q.get('executed') is True, 'query provenance missing')
        query = normalized(q['query'])
        ticker = unicodedata.normalize('NFKC', assignment['ticker']).casefold()
        require(re.search(r'(?<![a-z0-9])' + re.escape(ticker) + r'(?![a-z0-9])',
                          unicodedata.normalize('NFKC', q['query']).casefold())
                and any(name in query for name in names), 'query identifies another/unverified company')
        channels.add(q['channel'])
    require(set(contract()['required_channels']) <= channels, 'mandatory search channel absent')
    require(b._strategy_v2_firewall_scan('\n'.join(q['query'] for q in queries), {})['status'] == 'PASS', 'query leakage')
    exceeded = sum(q['channel'] == 'official' for q in queries) > 3 or len(queries) > 6
    require(v.get('query_budget_exceeded') is exceeded, 'budget accounting mismatch')
    require(not exceeded or v3.text(v.get('query_budget_note')), 'budget overrun must be explained')
    indexes = v.get('official_indexes')
    require(isinstance(indexes, list) and indexes, 'official index scan evidence missing')
    index_urls = set()
    gaps = v.get('coverage_gaps')
    require(isinstance(gaps, list), 'coverage gaps array missing')
    for gap in gaps:
        require(isinstance(gap, dict) and v3.url(gap.get('url')) and v3.text(gap.get('reason'))
                and gap.get('kind') in {'in_scope_document', 'ambiguous_document', 'index_scan', 'query_budget'}
                and gap.get('retry_required') is True, 'unresolved gap needs retry review')
    require(len({g['url'] for g in gaps}) == len(gaps), 'duplicate gap')
    gap_urls = {g['url'] for g in gaps}
    for index in indexes:
        require(v3.url(index.get('url')) and index.get('window_from') == assignment['search_from']
                and index.get('window_to') == assignment['search_to'] and v3.text(index.get('scan_evidence'))
                and type(index.get('enumeration_complete')) is bool, 'invalid window scan')
        require(index['url'] not in index_urls, 'duplicate index')
        if index['enumeration_complete']:
            require(index['url'] in source_map, 'completed index lacks source evidence')
        else:
            require(index['url'] in gap_urls, 'incomplete scan not recorded as partial')
        index_urls.add(index['url'])
    documents = v.get('documents')
    require(isinstance(documents, list), 'document inventory missing')
    ids = set()
    for doc in documents:
        require(isinstance(doc, dict) and v3.text(doc.get('title')) and v3.url(doc.get('url'))
                and doc['url'] not in ids, 'document identity missing/duplicate')
        ids.add(doc['url'])
        require(date.fromisoformat(assignment['search_from']) <= date.fromisoformat(doc['date'])
                <= date.fromisoformat(assignment['search_to']), 'document outside window')
        label = scope_decision(doc)
        if label == 'AMBIGUOUS':
            require(doc['url'] in gap_urls, 'unresolved scope needs partial review')
        status = doc.get('body_status')
        require(status in {'VERIFIED', 'SKIPPED', 'INACCESSIBLE'}, 'invalid body status')
        if status == 'VERIFIED':
            require(doc.get('final_url') in source_map and doc['final_url'] not in index_urls, 'body follow evidence missing')
        elif label in OUT_SCOPE:
            require(doc['url'] not in gap_urls, 'out-of-scope PDF is not a coverage gap')
        else:
            require(status == 'INACCESSIBLE' and doc['url'] in gap_urls
                    and v3.text(doc.get('attempt_evidence')), 'in-scope/unresolved body requires follow or partial')
    for gap in gaps:
        require(gap['url'] in ids | index_urls, 'gap has no inventory evidence')
    candidates = v.get('candidate_provenance')
    require(isinstance(candidates, list), 'candidate provenance missing')
    selected = []
    for i, c in enumerate(candidates):
        require(c.get('candidate_index') == i and type(c.get('selected_for_output')) is bool
                and v3.text(c.get('selection_reason')), 'candidate identity/decision missing')
        require(c.get('document_url') in ids, 'candidate document missing')
        doc = next(d for d in documents if d['url'] == c['document_url'])
        if c['selected_for_output']:
            require(doc['scope_label'] in IN_SCOPE and doc['body_status'] == 'VERIFIED'
                    and c.get('source_url') == doc.get('final_url') and v3.text(c.get('headline'))
                    and v3.text(c.get('economic_transmission')) and v3.text(c.get('event_key')), 'selected event not verified in scope')
            selected.append(c)
    require(len({c['event_key'] for c in selected}) == len(selected), 'event-level duplicate')
    require({d['url'] for d in documents if d['scope_label'] in IN_SCOPE and d['body_status'] == 'VERIFIED'}
            <= {c['document_url'] for c in candidates}, 'verified in-scope document needs selection review')
    require(sorted((c['headline'], b._normalized_url(c['source_url'])) for c in selected)
            == sorted((e['headline'], b._normalized_url(e['source_url'])) for e in payload['items']), 'output/provenance mismatch')
    partial = bool(gaps)
    require(v.get('coverage_status') == ('PARTIAL' if partial else 'COMPLETE'), 'coverage status mismatch')
    require(v.get('no_news_gate_pass') is (not partial and not payload['items']), 'partial cannot claim no-news')
    require(v.get('stopped_after_first_event') is False, 'cannot stop at first event')
    require(v.get('candidate_count') == len(candidates) and v.get('final_event_count') == len(selected), 'counts mismatch')
    elapsed = v.get('total_research_elapsed_seconds')
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, 'invalid elapsed time')
    return v
