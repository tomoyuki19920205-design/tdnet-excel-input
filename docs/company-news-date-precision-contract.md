# Company News date precision contract and migration plan

This plan is based on saved Phase1.8 artifacts and the repository contracts at
commit `711f485c306b1d5f79b89d5a65ec8540c228712d`. It does not alter the
production database, API, viewer, routing, or research architecture.

## Current contract and defect

`strategy_telemetry.documents[].date` is a required string in the generated
schema. The schema limits it to ten characters but has no minimum length,
format, pattern, nullable form, or unknown sentinel. Strategy v4, inherited by
v5, unconditionally calls `date.fromisoformat()` and requires every document
to fall in the assignment window. Therefore `""` passes structural validation
and crashes semantic validation. There is no valid current representation for
an unknown document date.

The document list is semantically a list of assigned-window candidate
documents. Undated index pages and out-of-window identity/background documents
must remain in evidence/source provenance and must not be presented as dated
window candidates. A future schema should allow `date: null` for provenance
records or, preferably, separate provenance records from candidate documents.
It must never substitute `checked_at`, retrieval time, the current date, or a
dummy date.

The production `published_at` contract is different. `validate_payload()`
requires a timezone-aware ISO datetime, uses it in the event dedupe key, and
copies `checked_at` separately to `first_seen_at`, `last_seen_at`, and
`checked_at`. SQLite stores the value as non-null text. Migration 016 stores it
as `TIMESTAMPTZ NOT NULL`, indexes it for date ordering, and exposes it through
`api_latest_news_events`. This evidence defines `published_at` as an actual
publication instant. No repository contract defines midnight as a
representative publication time.

## Proposed contract

Use an explicit three-state representation:

| Precision | `published_at` | `published_date` | Meaning |
| --- | --- | --- | --- |
| `datetime` | timezone-aware datetime | matching calendar date | Exact instant is known |
| `date` | null | date | Calendar date only is known |
| `unknown` | null | null | Neither date nor time is known |

This is Option 1. It is preferred because it preserves meaning at the API
boundary, allows exact existing rows to remain unchanged, and distinguishes
date-only from unknown without inference. Option 2, nullable `published_at`
plus `published_date` with precision inferred from nulls, needs the same DB/API
changes but makes invalid or ambiguous combinations harder to reject. No safe
existing Company News production contract (Option 3) supports date-only values;
the scope-adjudication artifact has a `publication_precision=date` convention,
but it is not the production event schema.

Newest-first sorting should use the known calendar date first, put exact
instants ahead of date-only records on the same date, and put unknown records
last. Date-only ties need a stable identifier and must not claim intraday
order. Dedupe material should include the precision and either the exact
datetime, the calendar date, or `unknown`, together with the existing ticker,
canonical URL, and normalized headline.

## Migration plan

1. Add nullable `published_date DATE` and a constrained
   `published_precision` enum/text column. Make `published_at` nullable and add
   checks enforcing the three valid combinations.
2. Backfill all existing non-null `published_at` rows as `datetime`, deriving
   `published_date` from the stored instant without changing the instant.
3. Version the payload validator and API view to return all three fields. Keep
   exact-datetime inputs and outputs byte-for-byte compatible where current
   normalization already permits it.
4. Update ordering and dedupe together, then update the viewer to show a date
   for `date`, a timestamp for `datetime`, and an explicit unknown state.
5. Deploy readers that accept both versions before writers emit date-only or
   unknown events. Run a dry migration and rollback rehearsal before any live
   schema or data change.

Until that migration is approved, date-only evidence must fail closed before
production publication. Converting `2026-08-31` to midnight in any timezone is
not compatible with the meaning of `published_at`.
