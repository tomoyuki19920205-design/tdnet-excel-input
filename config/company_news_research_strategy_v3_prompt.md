# Isolated Company News Research Strategy v3

Use only when the existing route returns mode=benchmark and strategy_version=company_news_research_strategy_v3. Never use for a production route. Work in {{REPO}}. First read {{REPO}}\data\news_work\SCHEDULED_TASK_COMMON_PROMPT.txt for canonical company_news_v1 fields, enums and source priorities. This document overrides its transport and coverage rules, not its JSON schema.

Run once: `.\.venv\Scripts\python.exe tools/company_news_batch_benchmark.py snapshot --task-id <route task_id>`. If busy, exit without research or writes. Require the v3 snapshot. Process only the immutable returned assignments, in order, at most four. Never reload queues or take a fifth company. Every company has its own 3–6 search-action budget: 2–3 official queries and 1–3 focused external queries. Do not borrow a later company's reserved budget. Opening discovered documents is traversal, not another search query. Do not silently truncate coverage when a budget or tool limit is exhausted: finalize a failure for that company and continue the other snapshot companies.

For each assignment, call `research-start --task-id <task_id> --run-token <run_token> --assignment-id <assignment_id>` using the same benchmark CLI. Use only its company_name, ticker, search_from and search_to. Do not expand the date window to today. Read the snapshot strategy_contract. No company-specific special branches or fixed regulator/agency names are permitted.

## A. Official document coverage

Discover the company's official IR/news indexes and applicable disclosure listings using company_name and ticker. Enumerate ALL entries published within the assignment window on the relevant indexes. Record date, title, href, document_type, index_url and requires_follow for each distinct document. Record index scan evidence, pagination completion and the exact scanned window. Finding an index, a search snippet or a publication notice is NOT body-level coverage.

Follow each linked PDF, attachment and alternate document. Also follow HTML when the index alone cannot support a nonmaterial decision. Traverse relevant supporting links; record each required document separately even when several documents describe the same financial event or share a date. Check all relevant documents before event-level dedupe. Do not stop after the first item. A body extraction failure is not a successful follow. Use an accessible official alternate where possible, record original href plus final_url; if required content remains unavailable, fail the company rather than claiming coverage.

Skip only an HTML item whose substantive index text alone establishes it is nonmaterial. Record reason_code=index_sufficient_nonmaterial and a concrete reason. Do not skip a document as a duplicate before reading it. Required linked documents may not resolve merely to an index page.

official_document_coverage_complete is true only after explicit window enumeration and all required follows. official_coverage_complete mirrors that stronger gate. If zero items exist, record an explicit completed zero-item scan, not an empty unsearched list. official_follow_ratio = followed count / enumerated count, or 1.0 for a verified zero-item scan. A permitted nonmaterial HTML skip can lower that ratio without omitting required work.

## B. Focused external/regulatory pass

Build business_metadata from the assignment sector and an actually verified official business description; include its source_url. Derive at most two regulatory themes from sector/business exposure, not the company's identity. The pure helper in tools/company_news_strategy_v3.py defines the exact taxonomy and regulatory_query format. It performs no research. Use regulatory_themes(metadata) to obtain theme/exposure/terms and regulatory_query(company_name,ticker,theme) to build each required regulatory query. Only terms appropriate to the exposed business are selected; do not search all terms or insert a fixed agency name. If no theme applies, record the completed review and use a narrowly company-specific external check within the same budget.

For government, local-government and verified official-regulator results, explicitly review whether a dated company-linked event exists. Store candidate/no_event/duplicate and reason in regulatory_candidates_reviewed. Link candidate/duplicate decisions to candidate_index. Merge company/regulator mirrors at event level, not by suppressing regulatory discovery.

## C. Pruned targeted pass

A targeted query requires BOTH a justified trigger and a sourced business exposure → economic transmission sentence BEFORE searching. Allowed triggers: material_candidate_zero, source_diversity_insufficient, explicit_external_exposure, external_driver_uncovered. Record driver_reason, business_exposure, transmission_channel and exposure_source_url. Broad sector-news searches without a concrete economic link are prohibited. No grounded driver means no driver query; record the skip reason and finish the focused company/regulatory check instead. Check that all relevant external exposures are covered; if coverage cannot be completed, report failure, not no-news.

Prefer industry associations, government, customer/supplier official sources and reputable specialist media. Generic company profiles, ordinary stock introductions and recycled business descriptions can inform exposure but are not final news events. Record such candidates as information_type=generic_profile, selected_for_output=false. A genuinely new business event found on such a page needs its own dated substantive evidence and economic transmission; do not rename a profile to force selection.

## Completion and telemetry

Every normal result, including items=[], requires official_document_coverage_complete, external_coverage_complete and window_scan_complete. Coverage booleans are supported by the arrays below, not self-certifying flags. Up to 20 candidates and 10 final events per company; overflow is failure, never silent truncation. Research failure is never items=[].

After completing research, call `research-finish --task-id <task_id> --run-token <run_token> --assignment-id <assignment_id>`. Save canonical JSON only to assignment.temp_path and v3 telemetry only to assignment.strategy_telemetry_temp_path. Both must be benchmark-isolated. The telemetry schema below is a structural template; replace placeholders and arrays with actual observations. Do not copy true/zero defaults as observations. The validator tools.company_news_strategy_v3.validate is authoritative.

```json
{
  "schema_version": "company_news_benchmark_strategy_telemetry_v3",
  "strategy_version": "company_news_research_strategy_v3",
  "assignment_id": "<snapshot assignment_id>",
  "ticker": "<snapshot ticker>",
  "official_index_found": true,
  "official_index_url": "<verified primary index URL>",
  "official_indexes": [{"url":"<index URL>","enumeration_complete":true,"window_from":"<search_from>","window_to":"<search_to>","scan_evidence":"<pagination and window scan evidence>"}],
  "official_items_in_window": [{"date":"<YYYY-MM-DD>","title":"<actual title>","href":"<document URL>","document_type":"pdf","index_url":"<index URL>","requires_follow":true}],
  "official_items_followed": [{"href":"<document URL>","final_url":"<verified body URL>","document_type":"pdf","content_verified":true,"evidence_excerpt":"<short substantive body evidence>"}],
  "official_items_skipped": [],
  "official_follow_ratio": 1.0,
  "official_zero_items_confirmed": false,
  "official_document_coverage_complete": true,
  "official_coverage_complete": true,
  "window_scan_complete": true,
  "official_queries_attempted": ["<actual company_name ticker official query 1>","<actual company_name ticker official query 2>"],
  "external_queries_attempted": [{"kind":"company_external_check","query":"<actual company_name ticker focused query>","reason":"<specific verification purpose>"}],
  "external_sources_found": ["<actual external source URL>"],
  "external_coverage_complete": true,
  "business_metadata": {"sector":"<sector>","business_description":"<verified business description>","source_url":"<verified source URL>"},
  "regulatory_themes": [],
  "regulatory_review_complete": true,
  "regulatory_candidates_reviewed": [],
  "driver_groups": [],
  "targeted_pass_triggers": [],
  "targeted_pass_skip_reason": "<only when no grounded targeted query is needed>",
  "source_provenance": [{"url":"<each unique verified source URL>","source_type":"<actual source type>","content_verified":true,"evidence_excerpt":"<short supporting evidence>","checked_at":"<timezone-aware ISO-8601>"}],
  "source_diversity": ["<exact unique source types in source_provenance>"],
  "candidate_event_count": 0,
  "final_event_count": 0,
  "candidate_provenance": [],
  "no_news_gate_pass": true,
  "stopped_after_first_event": false,
  "official_stage_elapsed_seconds": 0.0,
  "regulatory_stage_elapsed_seconds": 0.0,
  "targeted_stage_elapsed_seconds": 0.0,
  "total_research_elapsed_seconds": 0.0
}
```

For regulatory queries, external_queries_attempted uses kind=regulatory_pass and theme_index. For targeted queries, kind=targeted_second_pass and driver_index. Each entry includes actual query and reason. Each driver contains business_exposure, transmission_channel, driver_reason, exposure_source_url. Regulatory review entries contain url, decision, reason and candidate_index when applicable. official_items_skipped entries contain href, reason_code and reason.

If no external bodies could be verified after valid focused searches, each external query also needs result_evidence describing what was inspected and why no usable result existed. A source_diversity_insufficient trigger additionally requires source_diversity_before_targeted (integer below 2). An external_driver_uncovered trigger requires uncovered_driver_indexes. A material_candidate_zero trigger cannot contradict selected official candidates. Metadata and trigger records must be factual, not fabricated to pass validation.

candidate_provenance covers every candidate exactly once, with contiguous candidate_index starting at 0, stage (official_first/regulatory_pass/targeted_second_pass/company_external_check), headline, source_url, selected_for_output, information_type (event/generic_profile), selection_reason, and economic_transmission for selected events. Selected headline/URL pairs must exactly match final JSON items after URL normalization. Every selected source and followed body must appear in source_provenance. sources_checked_count equals the number of unique verified provenance URLs. Source-type diversity must reflect actual sources, not attempted searches. Empty verified external results require documented focused-search evidence, never invented source entries. Do not infer hidden reasoning time or inflate elapsed time.

Finalize normal results with `.\.venv\Scripts\python.exe tools/company_news_batch_benchmark.py finalize-company --task-id <task_id> --run-token <run_token> --assignment-id <assignment_id>`. Do not directly rename to a final path. The CLI validates canonical JSON plus v3 telemetry before its atomic save.

On failure, use the existing company_news_benchmark_failure_v1 sidecar in temp_path with task_id, slot_id, assignment_id, ticker, company_name, nonempty error_type/error_message, sources_attempted array and timezone-aware created_at; finalize with --failure. Continue remaining snapshot companies. After all are finalized, call `release-task --task-id <task_id> --run-token <run_token>` with the same CLI exactly once.

Forbidden: production inbox/assignments/queue, database writes, Supabase, production worker/bridge/ingest/sync, scheduler edits, changing immutable research/adjudication artifacts, and any company outside the snapshot. This prompt does not authorize activation or a new run.
