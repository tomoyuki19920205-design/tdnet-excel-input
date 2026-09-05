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
| `datetime` | timezone-aware datetime | source date when retained, otherwise null | Exact instant is known |
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

1. Deploy readers that accept legacy exact-datetime rows and all three new
   precision states while writers still emit the legacy shape.
2. Apply the additive migration in `020_company_news_date_precision.sql`.
   Backfill existing non-null `published_at` rows as `datetime` without deriving
   `published_date`: PostgreSQL does not retain the original input offset needed
   to reconstruct the source calendar date safely.
3. Update the API view to return all three fields. Keep
   exact-datetime inputs and outputs byte-for-byte compatible where current
   normalization already permits it.
4. Switch writers to the explicit three-field contract. Update ordering and
   dedupe together, then update the viewer to show a date
   for `date`, a timestamp for `datetime`, and an explicit unknown state.
5. Strengthen and validate the cross-field constraint after all writers are
   confirmed compatible. Run a dry migration and rollback rehearsal before any
   live schema or data change.

Until that migration is approved, date-only evidence must fail closed before
production publication. Converting `2026-08-31` to midnight in any timezone is
not compatible with the meaning of `published_at`.

## Phase1.8h offline result

The Phase1.8i effective-date extension changes the canonical schema SHA-256 to
`828269133af3cfb91d6c863579124e346a7191130204610dc00716ffd66c1205`.
Effective dates keep a separate semantic role and contract; see
`docs/company-news-effective-date-contract.md`.
The in-memory migration rehearsal passes legacy exact-instant preservation,
date-only and unknown inserts, old/new reader shapes, and rejection of all
three contradictory field combinations.

The saved 7760 replay passes the new canonical publication and document-date
contracts. Full final validation remains blocked by two independent saved
payload defects: an undated IR index is labelled `IN_SCOPE_INCREMENTAL_IR`
while its evidence says no incremental entry exists, and `valid_from` is a
date-only value while the existing temporal validator requires a timezone-aware
datetime. Phase1.8h records these failures without changing their meaning or
fabricating a temporal value. The PostgreSQL migration remains unapplied.
