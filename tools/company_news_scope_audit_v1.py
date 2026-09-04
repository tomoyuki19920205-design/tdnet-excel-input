"""Pure, conservative scope review projections; no writes, research or relabeling Gold.

This is not a new Gold adjudicator. AMBIGUOUS remains outside the *confirmed*
denominator, and the upper bound is published to prevent flattering recall.
"""
from __future__ import annotations

import re
from collections import Counter

from tools import company_news_batch_benchmark as b
from tools.company_news_strategy_v4 import SCOPE_SCHEMA, IN_SCOPE, SCOPE_LABELS


def scope_proposal(event, urls):
    title = event.get('headline', '')
    kind = event.get('primary_source_type')
    verdict = event['verdict']
    incremental = bool(re.search(r'書き起こし|質疑|質問.*回答|説明会|transcript|Q&A', title, re.I))
    if verdict == 'OUT_OF_WINDOW':
        return 'AMBIGUOUS', '対象期間外の既存判定を維持。scope denominatorには含めず個別review。'
    if incremental:
        return 'AMBIGUOUS', '追加情報候補だが、TDNET本文との差分が既存artifactだけでは確定しない。IN_SCOPE_INCREMENTAL_IR候補としてreview。'
    if kind == 'government':
        return 'IN_SCOPE_GOVERNMENT', '既存adjudicationで官公庁一次情報を確認。Goldのmaterial判定・企業への伝播理由を保持。'
    if kind == 'customer_supplier_official' or kind == 'industry_association':
        return 'IN_SCOPE_COUNTERPARTY', '既存adjudicationの取引先・業界団体情報。企業固有の伝播理由はGold原文を参照。'
    tdnet = kind == 'tdnet' or any(re.search(r'tdnet[^?#]*\.pdf|/disclosure/\d{8}/\d+\.pdf|urn:newsml:tdnet', u, re.I) for u in urls)
    if tdnet:
        return 'OUT_OF_SCOPE_TDNET', '既存eventに同一eventのTDNET文書URL/tdnet source labelあり。追加IR情報を示すタイトルではない。元のmaterial判定は不変。'
    if verdict == 'VALID_NON_MATERIAL':
        return 'OUT_OF_SCOPE_TRIVIAL', '既存Goldで投資材料性なしと裁定済み。scope軸で対象外へ投影（新たなmaterial裁定ではない）。'
    if kind == 'reliable_news' and not re.search(r'決算|業績予想|配当|株式分割|自己株式|TOB', title):
        return 'IN_SCOPE_EXTERNAL_MEDIA', '既存adjudicationが信頼できる報道のmaterial eventと確認。通常の決算・資本政策転載を示すタイトルではない。'
    return 'AMBIGUOUS', 'company_ir/company_news等のsource名だけでは非TDNET独自性を証明できない。元記事とTDNET同一内容の有無を別途review。'


def build_scope_audit(truth, common48, evidence_urls):
    by_id, groups = b._gold_normalized_groups_v2(truth)
    roots = {}
    for ident, members in groups.items():
        e = by_id[ident]
        urls = sorted({u for m in members for u in evidence_urls.get(m['match_cluster_id'], []) if u})
        scope, reason = scope_proposal(e, urls)
        roots[ident] = {'cluster_id': ident, 'ticker': e['ticker'], 'company': e['company'],
            'date': e['date'], 'headline': e['headline'], 'gold_verdict': e['verdict'],
            'known_critical': e['verdict'] == 'VALID_MATERIAL' and (e.get('importance') == 'high' or e.get('earnings_relevance') == 'direct'),
            'source_type': e.get('primary_source_type'), 'channel': e.get('earnings_channel'),
            'scope_label': scope, 'scope_reason': reason, 'evidence_urls': urls,
            'materiality_reason': e.get('materiality_reason') or e.get('rationale'),
            'confidence': 'LOW' if scope == 'AMBIGUOUS' else 'MEDIUM',
            'label_status': 'REVIEW_REQUIRED' if scope == 'AMBIGUOUS' else 'ARTIFACT_BASED_PROJECTION',
            'common48': e['ticker'] in common48}
    rows = [{'cluster_id': m['match_cluster_id'], 'root_id': ident, 'gold_verdict': m['verdict'],
             'scope_label': roots[ident]['scope_label'], 'inherited_from_root': m['match_cluster_id'] != ident}
            for ident, members in groups.items() for m in members]
    material = [e for e in roots.values() if e['gold_verdict'] == 'VALID_MATERIAL']
    def summary(items):
        counts = Counter(e['scope_label'] for e in items)
        confirmed = sum(counts[x] for x in IN_SCOPE)
        return {'material_total': len(items), 'scope_counts': {label: counts[label] for label in sorted(SCOPE_LABELS)},
                'confirmed_in_scope_material': confirmed, 'ambiguous_material': counts['AMBIGUOUS'],
                'denominator_lower_bound': confirmed, 'denominator_upper_bound': confirmed + counts['AMBIGUOUS'],
                'denominator_final': None if counts['AMBIGUOUS'] else confirmed,
                'source_type': dict(Counter(e['source_type'] for e in items)),
                'channel': dict(Counter(e['channel'] for e in items)),
                'in_scope_by_source_type': dict(Counter(e['source_type'] for e in items if e['scope_label'] in IN_SCOPE)),
                'in_scope_by_channel': dict(Counter(e['channel'] for e in items if e['scope_label'] in IN_SCOPE))}
    return {'schema_version': SCOPE_SCHEMA, 'benchmark_id': truth['benchmark_id'],
            'basis': 'Frozen extended audit normalized truth; no Gold mutations, no new Web research',
            'scope_projection_policy': 'Conservative artifact-based labels; exclusivity/delta gaps stay AMBIGUOUS. Not approved recall denominator until review.',
            'raw_cluster_count': len(rows), 'normalized_event_count': len(roots), 'cluster_labels': rows,
            'events': list(roots.values()), 'material_summary': summary(material),
            'common48': summary([e for e in material if e['common48']]),
            'critical7': [e for e in material if e['known_critical']],
            'ambiguous_review': [e for e in roots.values() if e['scope_label'] == 'AMBIGUOUS'],
            'gold_mutation': 0, 'extension_mutation': 0}


def classify_failure(failure):
    message = failure['error_message']; lower = message.lower()
    kind = failure['error_type'].lower()
    tags = []
    if 'budget' in lower or 'budget' in kind or 'overflow' in kind: tags.append('QUERY_BUDGET')
    if 'provenance' in kind: tags.append('QUERY_PROVENANCE')
    if 'protocol' in kind: tags.append('PROTOCOL_VIOLATION')
    if any(w in lower for w in ['holiday schedule', 'esg', '寄附', 'donation', 'shareholder-meeting']):
        tags.append('TRIVIAL_DOCUMENT')
    if 'tdnet' in lower and any(w in lower for w in ['bodies', 'body', 'disclosure']):
        tags.append('TDNET_FOLLOW')
    alias_only = 'exact snapshot' in lower or 'english alias' in lower or 'romanized spelling' in lower
    if 'PROTOCOL_VIOLATION' in tags: primary = 'PROTOCOL_VIOLATION'
    elif 'TRIVIAL_DOCUMENT' in tags: primary = 'TRIVIAL_DOCUMENT'
    elif 'QUERY_PROVENANCE' in tags: primary = 'QUERY_PROVENANCE'
    elif 'QUERY_BUDGET' in tags: primary = 'QUERY_BUDGET'
    elif 'TDNET_FOLLOW' in tags: primary = 'TDNET_FOLLOW'
    else: primary = 'OTHER'
    # Do not pretend an unnamed inaccessible IR document is a proven in-scope event.
    likely = primary in {'TRIVIAL_DOCUMENT', 'TDNET_FOLLOW'} or alias_only
    return {'classification': primary, 'secondary_tags': tags,
            'likely_avoidable_failure_under_v4': likely,
            'possible_partial_or_budget_recovery': not likely and primary != 'PROTOCOL_VIOLATION',
            'recovery_note': ('Recorded blocker is removed by scope triage/normalized identity; full success is NOT established without research.' if likely else
                             'Insufficient evidence of completed in-scope coverage/identity; do not count as rescued success.'),
            'evidence': message}
