# Company IR full bootstrap report (2026-08-20)

Global notification gate remained OFF for the entire run. This report does
not approve or close Phase 2 and does not enable production notifications.

## Discovery coverage

| Mutually-exclusive status | Companies |
|---|---:|
| discovered | 2,607 |
| no_official_url | 935 |
| ir_not_found | 69 |
| fetch_failed | 229 |
| js_required | 49 |
| other_terminal_status | 0 |
| **Total** | **3,889** |

The status sum equals the TSE target universe (3,889). Official URLs were
available for 2,954 companies. The first discovery pass is complete because
every ticker has a terminal status; unresolved terminal statuses remain in the
Nightly retry rotation.

## Sources and baseline

- Active monitor sources: 7,266
- Sources with a successful initial baseline: 7,226
- Active sources still without a successful baseline: 40
- Companies for which every active source completed baseline: 2,571
- Verified external active sources: 295
- External sources missing official-page provenance: 0
- Excess candidates retained for audit, not monitored: 4,975
- Rejected candidates retained for audit: 64

## Assets and notification safety

- Stored asset audit rows: 10,792
- Canonical asset identities: 10,767
- Baseline rows: 10,768
- Pending rows: 0
- Suppressed identity-duplicate audit rows: 24
- Notified rows: 0
- Global notification gate: OFF

The 24 suppressed rows came from changing PDF cache-buster/render-dimension
query parameters and one legacy title-context identity. They are retained as
audit evidence with `suppression_reason=identity_duplicate` and cannot publish.
No genuine post-baseline publication was observed during this bootstrap.

## Final all-source dry-run

The final read-only pass covered all 7,266 active sources:

- Extracted asset appearances: 10,033
- Source fetch failures (DNS/404/timeout/etc.): 848
- New assets: 0
- Pending: 0
- Notifications: 0
- Publish failures: 0
- TDnet duplicate notifications: 0

The SQLite integrity check returned `ok`, with zero foreign-key violations.
A read-only Supabase count for `company_ir_material` and `company_ir_video`
events from 2026-08-19 20:30 JST through the final audit returned zero.

