"""V2 official + driver search baseline, specialized for complementary news.

Pure benchmark contract: no fetching, activation, production access or v3
completeness validator. Coverage evidence describes research actually performed.
"""
from __future__ import annotations

import ast
import inspect
import math
import re
from datetime import date
from urllib.parse import urlsplit

from tools import company_news_batch_benchmark as b

VERSION = 'company_news_research_strategy_v4'
RUN_NAME = 'batch04v4'
SCHEMA = 'company_news_benchmark_strategy_telemetry_v4'
SCOPE_SCHEMA = 'news_monitor_scope_v1'
PROMPT_NAME = 'company_news_research_strategy_v4_prompt.md'
IN_SCOPE = {'IN_SCOPE_COMPANY_WEB', 'IN_SCOPE_EXTERNAL_MEDIA', 'IN_SCOPE_GOVERNMENT',
            'IN_SCOPE_COUNTERPARTY', 'IN_SCOPE_INCREMENTAL_IR'}
OUT_SCOPE = {'OUT_OF_SCOPE_TDNET', 'OUT_OF_SCOPE_TRIVIAL'}
SCOPE_LABELS = IN_SCOPE | OUT_SCOPE | {'AMBIGUOUS'}
CHANNELS = {'official', 'external_media', 'government', 'counterparty', 'incremental_ir'}
MATERIAL_TITLE = re.compile(r'価格改定|値上げ|値下げ|提携|受注|生産停止|工場停止|操業|設備投資|顧客採用|行政処分|回収|不具合|承認|供給契約|capacity|price|partnership|recall|order', re.I)


def require(ok, message):
    if not ok:
        raise b.BenchmarkError('strategy v4: ' + message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def url(value):
    return text(value) and urlsplit(value).scheme in {'http', 'https'} and bool(urlsplit(value).hostname)


def contract():
    return {'strategy_version': VERSION, 'baseline_strategy_version': b.STRATEGY_V2_VERSION,
            'contract_revision': 'v2_non_tdnet_complement_v1', 'scope_schema': SCOPE_SCHEMA,
            'scope_labels': sorted(SCOPE_LABELS), 'production_write_forbidden': True,
            'research_flow': ['company_web', 'economic_exposures', 'counterparty', 'external_media', 'exposure_government', 'incremental_ir'],
            'driver_group_limit': b._strategy_v2_contract()['driver_group_limit'],
            'economic_exposure_minimum': 2, 'economic_exposure_maximum': 3,
            'query_targets': {'company_web': [1, 2], 'external_counterparty': [2, 4], 'government': [0, 2], 'incremental_ir': [0, 1], 'total': [3, 6]},
            'query_budget_is_failure_gate': False, 'regulatory_theme_limit': 2,
            'official_index_completeness_required': False, 'all_document_bodies_required': False,
            'exact_company_name_in_query_required': False, 'strict_query_provenance_required': False,
            'no_news_minimum_complement_channels': 2, 'partial_is_no_news': False,
            'partial_retry': 'REVIEW_ONLY_NO_AUTOMATIC_RERUN',
            'incremental_ir_authority': {
                'incremental_information': True,
                'delta_evidence_required': True,
                'source_channel_alone_is_sufficient': False,
            },
            'candidate_only_event_policy': 'NOT_A_FALSE_POSITIVE_PENDING_BLIND_ADJUDICATION',
            'precision_status_after_run': 'PROVISIONAL_PENDING_SCOPE_AND_BLIND_ADJUDICATION'}


def evaluation_policy():
    return {'primary_metric': 'non_tdnet_material_recall_v1', 'scope_schema': SCOPE_SCHEMA,
            'denominator': 'NORMALIZED_VALID_MATERIAL_AND_CONFIRMED_IN_SCOPE',
            'common48_denominator': 27, 'material_found_minimum': 16, 'material_found_preferred': 18,
            'scope_minimum': {'COMPANY_WEB': [6, 7], 'EXTERNAL_MEDIA': [4, 8], 'GOVERNMENT': [2, 4], 'COUNTERPARTY': [3, 7], 'INCREMENTAL_IR': [1, 1]},
            'scope_preferred': {'EXTERNAL_MEDIA': [5, 8], 'GOVERNMENT': [3, 4], 'COUNTERPARTY': [4, 7]},
            'failure_count_target': 0, 'legacy_critical7_is_unfiltered_gate': False,
            'partial_is_complete_research': False, 'same_cohort_pass_authorizes_production': False,
            'fresh_unseen_gate_required': True, 'scheduler_reliability': 'SEPARATE_GATE',
            'runtime_metrics': ['company_research_seconds', 'task_elapsed_seconds', 'p50', 'p95', 'max', 'source_count', 'busy_skip', 'capacity_skip', 'stale', 'overlap', 'conditional_throughput']}


def prompt_text(root):
    return (root / 'config' / PROMPT_NAME).read_text(encoding='utf-8').replace('{{REPO}}', str(root.resolve()))


def validate_exposures(exposures):
    require(isinstance(exposures, list) and 2 <= len(exposures) <= 3, 'derive 2..3 economic exposures')
    for x in exposures:
        require(isinstance(x, dict) and all(text(x.get(k)) for k in ('driver', 'metadata_evidence', 'transmission_channel')), 'economic exposure metadata/transmission missing')
    require(len({x['driver'].casefold() for x in exposures}) == len(exposures), 'duplicate exposure')


def query_plan(company_name, exposures, regulatory_themes=(), incremental_ir=False):
    """V2 dynamic driver groups, without company-identity constraints on external queries."""
    validate_exposures(exposures)
    require(text(company_name), 'company name missing')
    require(len(regulatory_themes) <= 2 and all(text(t) for t in regulatory_themes), 'maximum two exposure-derived regulatory themes')
    plan = [{'channel': 'official', 'query': company_name + ' news press product service pricing'},
            {'channel': 'official', 'query': company_name + ' partnership customer capacity investment'}]
    for i, x in enumerate(exposures):
        channel = 'counterparty' if i == 0 else 'external_media'
        suffix = ' customer supplier partner subsidiary demand production' if i == 0 else ' industry trade media statistics price demand'
        plan.append({'channel': channel, 'query': x['driver'] + suffix, 'exposure_index': i})
    for theme in regulatory_themes:
        plan.append({'channel': 'government', 'query': theme + ' government regulation approval safety procurement'})
    if incremental_ir:
        plan.append({'channel': 'incremental_ir', 'query': company_name + ' earnings call Q&A transcript supplemental commentary'})
    return plan


def scope_decision(item):
    label = item.get('scope_label')
    require(label in SCOPE_LABELS and text(item.get('scope_reason')), 'scope evidence missing')
    if label == 'OUT_OF_SCOPE_TDNET':
        require(item.get('tdnet_same_content_verified') is True and url(item.get('tdnet_url'))
                and text(item.get('tdnet_identity_evidence')) and item.get('incremental_information') is False,
                'TDNET equivalence not established')
    if label == 'OUT_OF_SCOPE_TRIVIAL':
        require(text(item.get('index_evidence')), 'trivial index evidence missing')
        require(not MATERIAL_TITLE.search(item.get('title', '')), 'potentially material title cannot be skipped as trivial')
    if label == 'IN_SCOPE_INCREMENTAL_IR':
        require(item.get('incremental_information') is True, 'incremental IR flag is not true')
        require(text(item.get('incremental_information_evidence')), 'incremental IR delta missing')
    if label in IN_SCOPE:
        require(item.get('tdnet_same_content_verified') is not True or item.get('incremental_information') is True, 'in-scope event repeats TDNET without increment')
    return label


def validate(value, assignment, payload):
    require(isinstance(value, dict), 'telemetry must be object')
    v = value
    require(v.get('schema_version') == SCHEMA and v.get('strategy_version') == VERSION
            and assignment.get('strategy_version') == VERSION, 'version mismatch')
    require(v.get('assignment_id') == assignment['assignment_id'] and v.get('ticker') == assignment['ticker'], 'assignment mismatch')
    sources = v.get('source_provenance')
    require(isinstance(sources, list) and sources, 'verified sources required')
    source_map = {}
    for source in sources:
        require(isinstance(source, dict) and url(source.get('url')) and text(source.get('source_type'))
                and source.get('content_verified') is True and text(source.get('evidence_excerpt')), 'verified source evidence required')
        require(source['url'] not in source_map, 'duplicate source URL')
        source_map[source['url']] = source
    require(payload.get('sources_checked_count') == len(source_map), 'source count mismatch')
    identity = v.get('company_identity', {})
    require(identity.get('verified_ticker') == assignment['ticker'] and identity.get('source_url') in source_map
            and text(identity.get('evidence')), 'wrong or unverified company identity')
    require(source_map[identity['source_url']]['source_type'] in {'company', 'company_news', 'company_ir', 'ir', 'company_official'}, 'identity needs official evidence')
    validate_exposures(v.get('economic_exposures'))
    queries = v.get('queries')
    require(isinstance(queries, list) and queries, 'searches missing')
    channels = set()
    for q in queries:
        require(isinstance(q, dict) and q.get('channel') in CHANNELS and text(q.get('query'))
                and q.get('executed') is True, 'query execution record missing')
        channels.add(q['channel'])
    require(b._strategy_v2_firewall_scan('\n'.join(q['query'] for q in queries), {})['status'] == 'PASS', 'query leakage')
    # No literal name/ticker matching, per-query body provenance, budget failure or index completeness.
    require(v.get('query_budget_exceeded') is (len(queries) > 6), 'budget accounting mismatch')
    themes = v.get('regulatory_themes')
    require(isinstance(themes, list) and len(themes) <= 2, 'maximum two regulatory themes')
    for theme in themes:
        require(isinstance(theme, dict) and text(theme.get('theme')) and type(theme.get('exposure_index')) is int
                and 0 <= theme['exposure_index'] < len(v['economic_exposures']), 'regulatory theme must link to business exposure')
    require('government' not in channels or themes, 'government search needs exposure-derived theme')
    coverage = v.get('channel_reviews')
    require(isinstance(coverage, list), 'channel reviews missing')
    reviewed = set(); seen = set()
    for c in coverage:
        require(isinstance(c, dict) and c.get('channel') in CHANNELS and c['channel'] not in seen
                and c.get('status') in {'CHECKED', 'NOT_APPLICABLE', 'PARTIAL'} and text(c.get('evidence')), 'channel review invalid')
        seen.add(c['channel'])
        if c['status'] == 'CHECKED':
            require(c['channel'] in channels, 'checked channel has no search record')
            if c['channel'] not in {'official', 'incremental_ir'}:
                require(type(c.get('exposure_index')) is int and 0 <= c['exposure_index'] < len(v['economic_exposures']), 'complement channel needs business exposure')
            if c['channel'] == 'incremental_ir':
                require(c.get('exposure_index') is None, 'incremental IR review cannot link a business exposure')
            reviewed.add(c['channel'])
    require({'official', 'counterparty'} <= seen, 'Company Web and explicit counterparty review required')
    documents = v.get('documents'); gaps = v.get('coverage_gaps')
    require(isinstance(documents, list) and isinstance(gaps, list), 'candidate documents/gaps missing')
    docs = {}
    for doc in documents:
        require(isinstance(doc, dict) and url(doc.get('url')) and text(doc.get('title')) and doc['url'] not in docs, 'document identity missing/duplicate')
        document_date = doc.get('date')
        if document_date is not None:
            try:
                parsed_document_date = date.fromisoformat(document_date)
            except (TypeError, ValueError):
                require(False, 'document date must be YYYY-MM-DD or null')
            require(date.fromisoformat(assignment['search_from']) <= parsed_document_date <= date.fromisoformat(assignment['search_to']), 'document outside window')
        scope_decision(doc)
        require(doc.get('body_status') in {'VERIFIED', 'SKIPPED', 'INACCESSIBLE'}, 'invalid body status')
        docs[doc['url']] = doc

    gap_urls = set()
    for gap in gaps:
        require(isinstance(gap, dict) and text(gap.get('reason'))
                and gap.get('retry_required') is True, 'gap needs evidence and retry reason')
        gap_type = gap.get('gap_type', 'document_access')
        require(gap_type in {'document_access', 'query_execution'}, 'invalid coverage gap type')
        if gap_type == 'document_access':
            require(gap.get('url') in docs and gap['url'] not in gap_urls,
                    'gap needs document and review reason')
            d = docs[gap['url']]
            require(d['scope_label'] in IN_SCOPE and d.get('material') is True
                    and d['body_status'] == 'INACCESSIBLE' and text(d.get('attempt_evidence')),
                    'only inaccessible in-scope material is a document gap')
            gap_urls.add(gap['url'])
        else:
            channel = gap.get('channel'); exposure_index = gap.get('exposure_index')
            require(channel in CHANNELS and gap.get('url') is None,
                    'query gap needs channel and null document URL')
            require(exposure_index is None or type(exposure_index) is int
                    and 0 <= exposure_index < len(v['economic_exposures']),
                    'query gap exposure link invalid')
            require(not any(q.get('channel') == channel and q.get('executed') is True
                            and q.get('exposure_index') == exposure_index for q in queries),
                    'query gap conflicts with executed search')
    for d in documents:
        if d['scope_label'] in IN_SCOPE and d.get('material') is True:
            require(d['body_status'] != 'SKIPPED', 'known material cannot be silently skipped')
        if d['body_status'] == 'INACCESSIBLE' and d['scope_label'] in IN_SCOPE and d.get('material') is True:
            require(d['url'] in gap_urls, 'inaccessible material needs PARTIAL')
        if d['body_status'] == 'VERIFIED':
            require(d.get('final_url') in source_map, 'verified document source missing')
    candidates = v.get('candidate_provenance')
    require(isinstance(candidates, list), 'candidate provenance missing')
    selected = []
    for i, c in enumerate(candidates):
        require(isinstance(c, dict) and c.get('candidate_index') == i and type(c.get('selected_for_output')) is bool
                and text(c.get('selection_reason')) and c.get('document_url') in docs, 'candidate identity/decision missing')
        d = docs[c['document_url']]
        if c['selected_for_output']:
            require(d['scope_label'] in IN_SCOPE and d['body_status'] == 'VERIFIED' and d.get('material') is True
                    and c.get('source_url') == d.get('final_url') and text(c.get('headline'))
                    and text(c.get('economic_transmission')) and text(c.get('event_key')), 'selected event needs material economic transmission and verified in-scope source')
            selected.append(c)
    require({d['url'] for d in documents if d['scope_label'] in IN_SCOPE and d.get('material') is True and d['body_status'] == 'VERIFIED'}
            <= {c['document_url'] for c in candidates}, 'known material needs candidate decision')
    require(len({c['event_key'] for c in selected}) == len(selected), 'event-level duplicate')
    require(sorted((c['headline'], b._normalized_url(c['source_url'])) for c in selected)
            == sorted((x['headline'], b._normalized_url(x['source_url'])) for x in payload['items']), 'output/provenance mismatch')
    partial = bool(gaps)
    require(v.get('coverage_status') == ('PARTIAL' if partial else 'COMPLETE'), 'coverage status mismatch')
    gate = not partial and 'official' in reviewed and len(reviewed & {'external_media', 'counterparty', 'government'}) >= 2
    require(v.get('no_news_gate_pass') is (gate and not payload['items']), 'no-news gate mismatch')
    if not partial and not payload['items']:
        require(gate, 'no-news requires Company Web and two exposure-appropriate complement channels')
    require(v.get('stopped_after_first_event') is False and v.get('window_review_complete') is True, 'continue relevant window review after first event')
    require(v.get('candidate_count') == len(candidates) and v.get('final_event_count') == len(selected), 'counts mismatch')
    times = v.get('stage_elapsed_seconds')
    require(isinstance(times, dict) and {'company_web', 'external_complement'} <= set(times), 'v2 stage timing missing')
    for elapsed in [*times.values(), v.get('total_research_elapsed_seconds')]:
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, 'invalid elapsed time')
    require(v['total_research_elapsed_seconds'] >= sum(times.values()), 'total shorter than stage times')
    return v


def leakage_scan(prompt, assignments, companies):
    """Offline audit of generic logic; company identities are legitimate only in assignments."""
    runtime = '\n'.join(inspect.getsource(fn) for fn in (contract, query_plan, scope_decision, validate_exposures, validate))
    scan_text = prompt + '\n' + runtime
    result = b._strategy_v2_firewall_scan(prompt, assignments)
    hits = list(result['hits'])
    counts = {'company_hardcode_count': 0, 'ticker_hardcode_count': 0, 'gold_url_leakage_count': 0, 'company_specific_branch_count': 0}
    for company in companies:
        for key in ('ticker', 'company_name'):
            ident = str(company[key])
            if len(ident) >= 3 and re.search(r'(?<![A-Za-z0-9])' + re.escape(ident) + r'(?![A-Za-z0-9])', scan_text):
                counts['ticker_hardcode_count' if key == 'ticker' else 'company_hardcode_count'] += 1
                hits.append('literal_identity:' + key)
    # There are no hardcoded source URLs at all: therefore no event URL can leak.
    if re.search(r'https?://[^\s)>]+', scan_text):
        counts['gold_url_leakage_count'] += 1; hits.append('literal_source_url')
    for term in ('historical_scoring_v1', 'misses.csv', 'found_by', 'match_cluster_id', 'gold_adjudication'):
        if term in scan_text: hits.append('evaluation_input:' + term)
    for node in ast.walk(ast.parse(runtime)):
        if isinstance(node, ast.Compare) and any(isinstance(x, ast.Constant) and isinstance(x.value, (str, int)) for x in node.comparators):
            target = ast.unparse(node.left)
            if re.search(r"(?:\bcompany_name\b|\bticker\b)", target):
                counts['company_specific_branch_count'] += 1; hits.append('identity_branch')
    return {**result, **counts, 'status': 'FAIL' if hits else 'PASS', 'hits': sorted(set(hits)),
            'hit_count': len(set(hits)), 'scope': 'Generic runtime code and prompt; cohort identities only used by offline scanner, never evaluation events.'}
