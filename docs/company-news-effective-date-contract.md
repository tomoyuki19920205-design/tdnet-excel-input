# Company News effective-date precision contract

Phase1.8i separates an event's effective period from its publication time. A
publication timestamp answers when a source appeared. An effective boundary
answers when a price, contract, policy, or other event starts or ends.

## Incremental IR authority

`IN_SCOPE_INCREMENTAL_IR` means that verified evidence contains information
absent from the referenced TDNET/earnings disclosure. An IR URL or IR channel
alone is insufficient. The authoritative machine-readable conditions are:

- `incremental_information` is `true`;
- `incremental_information_evidence` contains the supported delta; and
- EvidenceStore discovery/provenance permits the incremental-IR channel.

The Python aggregator applies this authority before Strategy validation. If
EvidenceStore says incremental IR is not applicable, a non-material verified
official index may be retained as `IN_SCOPE_COMPANY_WEB` coverage. It never
reclassifies a material document without delta evidence. Strategy v4/v5 then
fail closed if an incremental label has a false flag or missing delta evidence.

For saved 7760 evidence the classification is A. The verified IR index says no
entry exists in the assigned window and discovery records
`incremental_ir_applicable=false`. The saved synthesis had assigned the index
an incremental scope label even though it was only Company Web coverage.

## Effective boundaries

The existing names `valid_from` and `valid_until` remain the exact-instant
fields for compatibility. Each boundary has a date and precision companion:

| Precision | Exact field | Date field | Meaning |
| --- | --- | --- | --- |
| `datetime` | timezone-aware ISO-8601 | optional verified date | exact effective instant |
| `date` | `null` | `YYYY-MM-DD` | calendar effective date |
| `unknown` | `null` | `null` | boundary is unknown or absent |

The start fields are `valid_from`, `valid_from_date`, and
`valid_from_precision`. The end fields are `valid_until`, `valid_until_date`,
and `valid_until_precision`; this is the canonical representation of the
concept sometimes called `valid_to`.

The validator never copies `checked_at`, publication fields, or the current
date into an effective boundary. Date-only values never become midnight
timestamps. Interval validation rejects an end calendar date before the start
calendar date and compares exact instants only when both boundaries are exact.

For 7760, saved evidence says revised prices apply to quotations issued from
October 1, 2026. The correct representation is:

```json
{
  "valid_from": null,
  "valid_from_date": "2026-10-01",
  "valid_from_precision": "date",
  "valid_until": null,
  "valid_until_date": null,
  "valid_until_precision": "unknown"
}
```

## Options considered

Option 1 uses separate exact/date/precision fields. It preserves semantics,
maps directly to PostgreSQL types, supports indexes without fake instants, and
lets old readers keep reading the existing exact fields. This is implemented.

Option 2 accepts a datetime/date union in one API field and normalizes it into
separate DB columns. It is compact at the boundary but makes reader behavior
type-dependent and still needs Option 1 internally.

Option 3 changes effective boundaries to calendar dates only. It loses valid
exact instants and breaks existing TIMESTAMPTZ semantics.

Sorting and filtering use the date column for date-only boundaries and the
exact column for datetime boundaries. A date-only boundary has no ordering
within that day. Dedupe remains based on publication/event identity; effective
precision does not create a new event identity.

## Reader-first database rollout

Production was not accessed or changed in Phase1.8i. Prepared migration 021 is
the additive phase: it adds nullable precision columns, preserves exact values,
classifies existing exact/null rows, appends API view columns, and temporarily
allows omitted precision from legacy writers. It does not derive dates from
TIMESTAMPTZ values or reinterpret midnight.

After readers and the new writer are deployed, a read-only audit must confirm
no null precision remains. Prepared migration 022 then makes precision
required and installs strict cross-field constraints. The repository contains
exact and null legacy fixtures. It contains no evidence that any midnight
value is truly exact or was promoted from a date, so migration does not
reclassify such values.

The canonical schema SHA after Phase1.8i is
`828269133af3cfb91d6c863579124e346a7191130204610dc00716ffd66c1205`.

## 7760 offline replay

The original incremental-label failure and date-only `valid_from` failure are
resolved without LLM, Web, or research calls. Canonical schema and payload
validation pass. Strategy validation then exposes a separate saved-state
inconsistency: `coverage_status` is `PARTIAL` while `coverage_gaps` is empty.
The validator correctly reports `coverage status mismatch`. Phase1.8i stops at
that new failure and does not repair it.
