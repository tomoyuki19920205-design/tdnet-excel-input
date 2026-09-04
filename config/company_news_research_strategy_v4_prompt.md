# Company News Research Strategy v4 — complementary non-TDNET research

Benchmark/R&D only. Use only a route with mode=benchmark and strategy_version=company_news_research_strategy_v4. Work in {{REPO}}. Do not change a production prompt or route. This task complements daily TDNET monitoring: find material information that TDNET alone does not provide, not duplicate routine timely disclosures.

Read {{REPO}}\data\news_work\SCHEDULED_TASK_COMMON_PROMPT.txt for canonical company_news_v1 fields/enums. This v4 document overrides its research scope, coverage and transport rules, NOT its JSON schema. The authoritative sidecar validator is tools/company_news_strategy_v4.py. Scope fields belong only in the benchmark telemetry, never in canonical news items.

Use `.\.venv\Scripts\python.exe tools/company_news_batch_benchmark.py` for the following benchmark-only commands. Run `snapshot --task-id <route task_id>` once. Busy means exit. Process only its immutable assignments, in order, maximum four companies; never reload a slot to obtain a fifth company. For each company call `research-start --task-id <task_id> --run-token <run_token> --assignment-id <assignment_id>` BEFORE searching. Use only that company's ticker, identity and search window. After research call research-finish with the same arguments.

## Evidence-based scope triage

Assign every discovered document a news_monitor_scope_v1 label with scope_reason. Do not infer scope from URL domain, company/date or topic alone.

- IN_SCOPE_COMPANY_WEB: company-site PR, partnership, customer adoption, product/service, orders, supply contracts, pricing, production disruption/resumption, capital investment, capacity, defects/recalls, approvals or other company-specific information with material incremental value beyond TDNET. Check available index/disclosure evidence for same-content TDNET mirrors; when uncertain record AMBIGUOUS, not fabricated exclusivity.
- IN_SCOPE_EXTERNAL_MEDIA: substantive new reporting by credible general, specialist, industry, local or overseas media. Merely reproducing a TDNET disclosure is not incremental reporting.
- IN_SCOPE_GOVERNMENT: company-linked or strongly transmitted government, municipality or regulatory announcements, orders, sanctions, approvals, tenders, subsidies or policy changes. Review government and company mirrors at event level.
- IN_SCOPE_COUNTERPARTY: customer/supplier/partner/subsidiary/competitor/association announcements with an evidenced economic link to the target company. Same industry alone is insufficient.
- IN_SCOPE_INCREMENTAL_IR: conference, Q&A, transcript or video information NOT already in the TDNET materials. Record the specific incremental_information_evidence and comparison basis. Earnings announcements themselves are not incremental IR. If the difference cannot be established, AMBIGUOUS.
- OUT_OF_SCOPE_TDNET: confirmed same-content timely disclosure with no incremental information. Includes routine financial statements, guidance/dividend changes, equity issuance, buybacks, splits, TOB, securities/fixed-asset gains and personnel announcements. Record tdnet_same_content_verified=true, tdnet_url, tdnet_identity_evidence (matching document/title/date/content evidence), incremental_information=false. An inaccessible PDF can be skipped when index/document identity establishes this; its body is not mandatory.
- OUT_OF_SCOPE_TRIVIAL: clearly minor routine notices supported by title/index evidence, including office holiday schedules, routine donations/CSR, ESG website rankings or meeting invitations. Store index_evidence. Do not mechanically apply this to a material subsidy, production shutdown, price change, major partnership, order, investment, customer adoption or administrative action. Those require body follow.
- AMBIGUOUS: insufficient evidence of scope or TDNET incremental value. Attempt relevant body/comparison follow; unresolved access is PARTIAL, never invent an OUT label to pass.

## Research and coverage

Find official news/PR indexes first, enumerate relevant in-window entries and record scanned pagination/window. Triage before PDF follow. All official PDFs are NOT mandatory coverage: follow in-scope and unresolved potentially material candidates only. Follow discovered URLs by navigation/document retrieval, not repeated search. Index reach/snippets are not body verification of a selected event. Record alternate body URLs where needed. Deduplicate EVENTS after reviewing material information; same date or company alone is not duplication. Do not stop after the first item.

Execute official, external-media and focused government search channels for each company. Derive regulatory themes from actual sector/business exposure (at most two); no fixed company/agency hacks. Preserve focused customer/supplier/industry searches when a sourced economic driver warrants them, and prune broad ungrounded sector queries. Store actual query text, executed=true and result_evidence for each, including zero-result searches. Use ticker plus an identifying name; NFKC/case/whitespace variants are allowed. Verified English names, brand names and common aliases require official source evidence linking the alias to the assignment ticker. A wrong company fails closed. Literal Japanese-name or exact regulatory-query-template matching is not required.

Official search target maximum is 3, total target maximum is 6 per company (navigation is not search). Do not expand these merely to chase out-of-scope PDFs or borrow another company's budget. A necessary overrun is recorded by query_budget_exceeded and query_budget_note, NOT automatically a company failure. At the target limit prefer navigation and scope triage; record remaining genuine coverage gaps rather than claiming completion. Preserve the actual number of query actions even if several queries were sent in one tool call.

COMPLETE requires completed relevant index scans and no unresolved in-scope/ambiguous body gaps, plus all required search channels. If an in-scope potentially material document is inaccessible after reasonable alternate attempts, record body_status=INACCESSIBLE, attempt_evidence and coverage_gaps with retry_required=true. This yields coverage_status=PARTIAL, not failure. Incomplete relevant index enumeration also yields PARTIAL with index_scan gap. An unresolved AMBIGUOUS scope needs an ambiguous_document review gap even if the body was opened. Out-of-scope inaccessible documents do NOT create gaps. Every verified in-scope document needs a candidate selection/rejection decision; do not silently drop it.

On PARTIAL save any verified in-scope events. If none could be verified, items=[] is an incomplete research result, NOT confirmed no-news: no_news_gate_pass=false and the benchmark finalizer records result_type=partial. Do not call it a no-news success in summaries or quality metrics. Retry entries are review-only; do not automatically rerun companies. Failure is reserved for research not being viable (including a mandatory channel never executing), wrong company, ownership/protocol failure or inability to validate/save output.

## Sidecar structure and save

All booleans/counts/evidence below must reflect actual work. Do not copy example defaults. Every selected event needs substantive verified evidence and economic_transmission. The canonical JSON must pass existing schema validation, use nonempty headline, proper enums and timezone-aware timestamps. No scope/alias fields in company_news_v1.

```json
{
  "schema_version": "company_news_benchmark_strategy_telemetry_v4",
  "strategy_version": "company_news_research_strategy_v4",
  "assignment_id": "<snapshot assignment_id>",
  "ticker": "<snapshot ticker>",
  "company_identity": {"verified_ticker":"<ticker>","source_url":"<verified official source>","evidence":"<identity evidence>","aliases":[]},
  "queries": [{"channel":"official","query":"<ticker identifying-name actual query>","executed":true,"result_evidence":"<observed results>"}],
  "query_budget_exceeded": false,
  "query_budget_note": "",
  "official_indexes": [{"url":"<index>","window_from":"<search_from>","window_to":"<search_to>","enumeration_complete":true,"scan_evidence":"<window/pagination evidence>"}],
  "documents": [],
  "coverage_gaps": [],
  "coverage_status": "COMPLETE",
  "source_provenance": [{"url":"<verified URL>","source_type":"<actual type>","content_verified":true,"evidence_excerpt":"<body evidence>","checked_at":"<timezone datetime>"}],
  "candidate_provenance": [],
  "candidate_count": 0,
  "final_event_count": 0,
  "no_news_gate_pass": true,
  "stopped_after_first_event": false,
  "total_research_elapsed_seconds": 0
}
```

The queries array above is structural only: add actual external_media and government queries (counterparty optional). Each verified alias object has name, verified_ticker, source_url and evidence. Identity/alias source URLs must occur in source_provenance, with official identity-link evidence.

Each documents entry has date, title, url, scope_label, scope_reason, body_status (VERIFIED/SKIPPED/INACCESSIBLE). VERIFIED requires final_url in source_provenance, not an index. TDNET skip requires the four TDNET fields above; trivial skip requires index_evidence. Incremental IR requires incremental_information_evidence. All official entries enumerated in the window and all external material candidates must be recorded; do not omit inconvenient candidates. A zero documents array requires genuinely completed zero-candidate scans. Every coverage_gaps entry has url (an inventoried document/index), kind (in_scope_document/ambiguous_document/index_scan/query_budget), reason, retry_required=true.

Each candidate_provenance entry has candidate_index (contiguous from 0), document_url, selected_for_output (boolean), selection_reason; selected entries also require headline, source_url=verified final_url, economic_transmission and a unique event_key. Exactly the selected headline/URL pairs must appear in canonical items. sources_checked_count equals unique verified source_provenance URLs. Unread documents never count as verified sources.

Write canonical JSON to assignment.temp_path and telemetry to assignment.strategy_telemetry_temp_path ONLY. Use `finalize-company --task-id <task_id> --run-token <run_token> --assignment-id <assignment_id>` to validate and atomically save; do not directly rename final results. For genuine failure use the existing company_news_benchmark_failure_v1 sidecar (task_id, slot_id, assignment_id, ticker, company_name, error_type, error_message, sources_attempted, created_at) and finalize-company --failure. Continue remaining snapshot companies. Finally release-task with task-id and run-token once.

Forbidden: Production inbox/queue/assignment changes, DB/Supabase writes, worker/bridge/ingest/sync, scheduler edits, Gold/extension access or mutation, prior-run results, company-specific benchmark answers, next-company generation, automatic retry/activation. This prompt does not authorize starting a run.
