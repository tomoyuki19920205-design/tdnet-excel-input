# Company News Research Strategy v4 — NON_TDNET complement

Benchmark only. Root: {{REPO}}. Strategy baseline is company_news_research_strategy_v2: Company Web first, then dynamic business-driver searches. Do not inherit the v3 all-document/index-completeness, strict query-provenance or exact-name query contract. Find earnings-relevant information that daily TDNET monitoring alone does not reveal.

## Ownership, window and isolated delivery

Proceed only on an already authorized route with mode=benchmark and strategy_version=company_news_research_strategy_v4. This document does not authorize activation or scheduler changes. Read {{REPO}}/data/news_work/SCHEDULED_TASK_COMMON_PROMPT.txt only for canonical company_news_v1 JSON fields/enums. This document overrides research scope, query/coverage requirements and transport. Scope fields are benchmark sidecar fields, never canonical item fields.

Use `.\.venv\Scripts\python.exe tools/company_news_batch_benchmark.py`. Call `snapshot --task-id <route task_id>` once, exit on busy/capacity/no-assignment. Follow only that immutable snapshot, in order, at most four companies. Do not reload slots or add a fifth company. For each assignment call `research-start --task-id <task_id> --run-token <run_token> --assignment-id <assignment_id>` BEFORE any search; use the snapshot window. Call research-finish with those same arguments after research. Preserve official identity evidence linking the assigned ticker to the researched company; aliases are acceptable and external searches need not contain its name or ticker.

## A. Company Web (normally 1–2 queries)

Review official news/press/product/service/IR pages for price changes, new products/services, orders, partnerships, customer adoption, shutdowns, capacity, investment, overseas subsidiaries and post-earnings additional commentary. Do not expand search just to enumerate every index page, attachment or timely disclosure. Retain relevant in-window multi-event review after the first finding. Trivial inaccessible pages never block completion.

## B. Dynamic external complement (normally 2–4 queries; highest improvement priority)

From current company metadata, official profile and business model derive 2–3 economic exposures, each with driver, metadata_evidence and transmission_channel. Examples of generic exposures: input prices, commodity markets, customer production/pricing, equipment investment, demand, freight, property prices, energy/fuel and regulation. These are a taxonomy, never company-specific branches.

Use query_plan in tools/company_news_strategy_v4.py as a generic plan builder, supplying the actual company name, exposures, up to two relevant regulatory themes, and whether post-earnings commentary warrants a search. Adapt query language and terminology using company metadata. Do not force target-company keywords into customer or industry queries.

Counterparty is an explicit stage: check customers, suppliers, distributors, partners, end markets, domestic/overseas subsidiaries. Record a counterparty channel review even when not applicable, with a reason. Prioritize customer/supplier/partner sources, then industry/trade media, statistics/pricing indicators, relevant government sources, then major general media. Channel families for coverage are counterparty, external_media, government; industry media and general media alone are one family, not two.

A target-company mention in the article is unnecessary if the transmission is concrete: customer event → customer production/pricing/demand → target order/volume/price/margin → earnings. Same industry alone, generic company profiles and unsupported conjecture are insufficient. Store the evidence-backed chain for every selected event.

## C. Government/regulatory (normally 0–2 queries)

Search only when relevant to an identified exposure. Derive at most two themes, each linked by exposure_index: transport safety, energy rules, approvals, environmental rules, tariffs, subsidies, procurement or similar relevant developments. Do not search all agencies or require government searches when not applicable. There are no fixed agency/domain/company templates.

## D. Incremental IR (normally 0–1 query)

Earnings releases and repeated financial numbers are excluded. After earnings, a call/Q&A/presentation transcript, management commentary or supplement can qualify only with specific additional capacity constraints, customer demand, pricing, volumes, orders, margin assumptions, investment or supply constraints absent from the ordinary disclosure. Record incremental_information=true and incremental_information_evidence with the comparison basis. A transcript title alone is insufficient.

## Scope and candidate decisions

Every reviewed candidate document records date, title, url, scope_label, scope_reason, material (boolean), body_status (VERIFIED/SKIPPED/INACCESSIBLE). Follow selected material sources sufficiently to verify the actual event, date and transmission. No requirement exists to follow every official document.

- IN_SCOPE_COMPANY_WEB / IN_SCOPE_EXTERNAL_MEDIA / IN_SCOPE_COUNTERPARTY / IN_SCOPE_GOVERNMENT: incremental material information from the corresponding channel, within the window, with a concrete earnings transmission.
- IN_SCOPE_INCREMENTAL_IR: requires the explicit additional information described above.
- OUT_OF_SCOPE_TDNET: financial results, guidance revisions, issuance/offering, capital policy, fixed-asset gains and simple mirrors of timely disclosures, without additional information. Record tdnet_same_content_verified=true, tdnet_url, tdnet_identity_evidence, incremental_information=false. Sufficient document/index identity evidence allows skipping its body.
- OUT_OF_SCOPE_TRIVIAL: routine recruitment, event attendance, CSR/donations, holidays, corporate profiles etc. Record index_evidence. A specific material revenue/demand transmission can justify an in-scope exception; evaluate the event, not a keyword alone. Do not label potentially material pricing/orders/production disruption trivial.
- AMBIGUOUS: scope not established. Keep the uncertainty and decision rationale, do not publish it as verified in-scope information or invent exclusivity.

Deduplicate events, not entire company/date buckets. Every candidate selection/rejection gets a reason. Keep verified selected events even if another material source is inaccessible.

## Coverage and budgets

Typical total: 3–6 queries/company; extra searches are allowed when useful. Navigation is not a query. Query count alone never causes failure. Record actual queries (channel, query, executed=true), query_budget_exceeded=(total>6) and an optional note; there is no requirement to embed company identity or attach body-level provenance to each query.

Before confirmed no_news require Company Web CHECKED AND at least two CHECKED, exposure-appropriate families from external_media/counterparty/government. Each CHECKED channel has actual search records and a concise channel-level evidence summary. Complement channel reviews link to exposure_index. A zero-result query can count when honestly reviewed. NOT_APPLICABLE and PARTIAL reviews do not count as CHECKED. Continue relevant window review, do not stop at the first item.

Only an inaccessible IN_SCOPE material source after reasonable attempts creates a coverage gap: record attempt_evidence on its document and coverage_gaps with url, reason, retry_required=true. Set coverage_status=PARTIAL and no_news_gate_pass=false; empty items here are PARTIAL, not no-news. Trivial or TDNET pages, incomplete index pagination and budget overruns never create gaps by themselves. Do not use failure to punish document completeness. No automatic retries are authorized. Wrong company/ownership, unusable research or invalid/unwritable output remain genuine failures.

## Sidecar schema

Use schema_version=company_news_benchmark_strategy_telemetry_v4, strategy_version=company_news_research_strategy_v4, assignment_id and ticker exactly from snapshot. Required fields:

- company_identity: verified_ticker, source_url, evidence. URL must be in verified official source_provenance. No exact snapshot spelling requirement for queries.
- economic_exposures: 2–3 objects, driver, metadata_evidence, transmission_channel.
- queries: channel (official/external_media/counterparty/government/incremental_ir), query, executed=true.
- regulatory_themes: zero to two objects, theme, exposure_index.
- channel_reviews: unique channel, status (CHECKED/NOT_APPLICABLE/PARTIAL), evidence, and exposure_index for checked complement channels. Always include official and counterparty reviews.
- source_provenance: unique url, source_type, content_verified=true, evidence_excerpt. Count unique verified sources in canonical sources_checked_count. Unread sources are not counted.
- documents: reviewed candidate documents with the scope fields above. VERIFIED documents have final_url in source_provenance. No exhaustive official index inventory is required.
- coverage_gaps: material inaccessible-source entries only, as above; [] if none.
- candidate_provenance: candidate_index contiguous from 0, document_url, selected_for_output, selection_reason. Selected entries additionally have headline, source_url=verified final_url, economic_transmission, unique event_key; the document must be material=true and in scope. Selected headline/URL pairs must exactly match canonical items.
- coverage_status: COMPLETE or PARTIAL; no_news_gate_pass true only for empty items, no gaps and the Company Web plus two-family gate.
- query_budget_exceeded: boolean total>6; candidate_count; final_event_count; stopped_after_first_event=false; window_review_complete=true.
- stage_elapsed_seconds: company_web, external_complement, optionally government and incremental_ir; nonnegative actual elapsed times without double counting. total_research_elapsed_seconds covers their sum. Harness independently records company and task timing, source count, skips and overlap for post-run p50/p95/max and conditional throughput.

Write canonical JSON only to assignment.temp_path and sidecar only to assignment.strategy_telemetry_temp_path. Call `finalize-company --task-id <task_id> --run-token <run_token> --assignment-id <assignment_id>` for validated atomic save. Do not directly rename final outputs. For genuine failure use existing company_news_benchmark_failure_v1 sidecar and finalize-company --failure. Continue remaining snapshot companies. Finally `release-task --task-id <task_id> --run-token <run_token>` once.

Never access evaluation/adjudication artifacts or previous-run findings during research. No company-specific benchmark answers or source URL lists. No Production inbox/queue/DB/Supabase/Viewer writes, worker/bridge/ingest/sync, scheduler edits, activation, or automatic retries. Output stays in the benchmark assignment paths.
