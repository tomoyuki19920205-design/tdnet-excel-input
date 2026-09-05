# Company News coverage authority

Phase1.8j makes coverage a Python-derived state. Final synthesis may describe
findings, but it is not authoritative for searches executed, applicability,
checked channels, or gaps.

## Formal state

`coverage_status` has two values in Strategy v4/v5:

- `COMPLETE`: every required applicable query has an executed
  SearchRecordRegistry record with window review, and no inaccessible in-scope
  material document remains.
- `PARTIAL`: at least one required query was not executed/window-checked, or an
  in-scope material document remains inaccessible.

`coverage_gaps` contains only observed gaps. A `query_execution` gap identifies
the required channel and exposure with a null document URL. A
`document_access` gap identifies the actual document URL and carries its saved
access-attempt evidence. Both require `retry_required=true`.

`PARTIAL` with no gaps and `COMPLETE` with gaps are invalid. The aggregator
does not create a gap merely because synthesis wrote `PARTIAL`.

## Authority inputs

The coverage matrix is derived from:

1. the validated SearchRecordRegistry for executed queries and result counts;
2. EvidenceStore links for evidence-card counts;
3. exposure channel and government applicability from saved discovery;
4. saved incremental-IR applicability; and
5. normalized documents and access-attempt evidence.

An executed query with `window_relevance_checked=true` counts as checked even
when `result_count=0`. Result count is a finding outcome, not proof that the
query was skipped. Query budget is derived from actual registry count and
remains diagnostic under Strategy v5.

`channel_reviews`, `no_news_exposure_coverage`, `no_news_gate_pass`, and
`query_budget_exceeded` are projected from the same state. Non-applicable
channels, including incremental IR, do not become gaps. The no-news gate is
separate: it can pass only for an empty output after complete required coverage
with official and two applicable complement channel families checked.

## 7760 audit

The saved matrix contains six required queries and six executed, window-checked
registry records: official one, three external-media exposure queries, and two
applicable government queries. Counterparty is non-applicable for all three
saved exposure classifications, and incremental IR is explicitly
non-applicable. No material document is inaccessible. Therefore the correct
state is `COMPLETE` with an empty gap list.

The mutation-free pre-repair audit found three class-A stale derived fields:

- `coverage_status`: saved `PARTIAL`, derived `COMPLETE`;
- counterparty review: saved `PARTIAL`, derived `NOT_APPLICABLE`;
- government review: saved `PARTIAL`, derived `CHECKED`.

No class B, C, D, or E failure was found. Python repaired only those derived
fields. The post-repair 21-rule audit, canonical schema, Strategy v4, Strategy
v5, payload, provenance, and coverage validation all pass. Fake gaps are zero.

The Phase1.8j canonical schema SHA-256 is
`d1f28aa1e09eb2ed32cdde3810a7d5bda7d6ec0d7fd2831895a4b28aba5fe34c`.
