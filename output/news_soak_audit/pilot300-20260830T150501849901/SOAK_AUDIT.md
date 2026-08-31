# Company News Monitor 300-company hardening soak final audit

Queue: `pilot300-20260830T150501849901`

Audit window: 2026-08-30 15:05:01 JST (queue creation) through 2026-08-31 03:35:05 JST (queue completion). Historical errors from the 100-company soak and later empty Scheduled Work runs are excluded unless explicitly identified as comparison evidence.

## Executive judgment

The 300-company pipeline completed without a company loss, schema failure, stale payload, company retry, duplicate, queue corruption, or unrecovered synchronization failure. All 300 payloads validate as `company_news_v1`; they map one-to-one to 300 completed queue entries and 300 completed canonical scan rows. The 212 generated event dedupe keys are unique and all 212 are present in the canonical event table.

The atomic-write hardening had no surfaced `PermissionError`, WinError 5/32, replace exhaustion, temporary-file residue, or avoidable re-research during this soak. The helper does not persist successful internal replace-attempt counts, so `atomic transient retry count = 0 observed` is an observability result, not proof that no successful internal retry occurred. Exhaustion is directly evidenced as zero.

One worker invocation failed after three payloads had already been canonically ingested because Supabase DNS resolution failed. The queue remained blocked; it did not advance those slots. The next natural PT5M worker invocation resumed the three processed payloads and completed synchronization without Web re-research or duplicate canonical rows.

Pipeline correctness passes. Weekly release readiness is **WARN** because the measured 3,800-company duration leaves only 9.93 hours (5.9%) of a seven-day window, while PC sleep/update, Scheduled Work delay, or a longer network outage can consume that margin. Supabase is currently 2.25 GiB, but the contracted tier/capacity ceiling is not safely discoverable from the repository. Confirm uptime and Supabase headroom before activation.

Final judgment: `WARN_WORK_NEWS_300_SOAK_REQUIRES_REVIEW`

## Final queue state

| Metric | Result |
|---|---:|
| queue status | completed |
| completed / total | 300 / 300 |
| failed / pending / active | 0 / 0 / 0 |
| assignment sequence | 300 |
| started | 2026-08-30 15:06:20 JST |
| completed | 2026-08-31 03:35:05 JST |
| elapsed | 44,925 seconds = 12.4792 hours |
| slots | all slot01-slot24 idle; no next assignment |
| unique assignment IDs / queue positions / tickers | 300 / 300 / 300 |
| non-null final queue errors | 0 |

The last completed company was クロスキャット (2307), assignment `slot24-20260830T150501849901-000285`, completed by task08/slot24. Queue positions 286-300 completed later in other slots; assignment sequence and completion order are intentionally different.

## News results and payload integrity

- No-news companies: 154 (51.33%).
- News-present companies: 146 (48.67%).
- Generated events: 212.
- Average events per news-present company: 1.4521.
- Event-count distribution: 154 companies with 0; 97 with 1; 38 with 2; 5 with 3; 6 with 4.
- Processed artifacts: 300 files, 300 unique run IDs, 389,939 bytes; missing or duplicate run artifacts: 0.
- Full canonical validation: 300/300 valid; ticker mismatch: 0; invalid schema: 0.
- Two sources produced multiple legitimately distinct categories for the same ticker/headline/URL/date (three events for 7716 and four for 7226). Their canonical dedupe keys are distinct. This is not cross-company or canonical duplication.

## Reliability audit

### Atomic write

Search terms included `PermissionError`, WinError 5, WinError 32, `os.replace`, `atomic_write_retry`, `atomic_write_retry_exhausted`, temp collision, and fsync error across Company News runtime logs and artifacts in the audit window.

| Metric | Result |
|---|---:|
| WinError 5 | 0 |
| WinError 32 | 0 |
| surfaced atomic transient retries | 0 |
| retry exhausted | 0 |
| residual `.tmp` / lock files | 0 |

The shared writer uses a unique same-directory temp file, flush plus fsync, and bounded WinError 5/32 retry. Successful retry counts are returned by the helper but are not logged by callers; therefore a silent successful retry cannot be reconstructed after the fact. This is an observability limitation, not a correctness failure observed in the soak.

### Company retries, validation, stale delivery, and research failure

| Metric | Result |
|---|---:|
| total company retries (`attempt_count > 1`) | 0 |
| necessary company retries | 0 |
| unnecessary company retries | 0 |
| validation failures | 0 |
| stale payloads | 0 |
| work-research failure sidecars | 0 |
| quarantine artifacts in window | 0 |
| current-assignment corruption | 0 |
| lost companies | 0 |

There are no cases to classify or enumerate. The 100-company 玉井商船 pattern—canonical completion followed by local state failure and unnecessary Web re-research—did not recur.

### Duplicate audit

| Duplicate class | Count |
|---|---:|
| canonical event dedupe keys within the soak | 0 |
| canonical scan primary keys | 0 |
| queue completions | 0 |
| assignment IDs | 0 |
| queue positions | 0 |
| simultaneous same-ticker ownership | 0 |
| same company in multiple active slots | 0 |
| processed duplicate archive artifacts in window | 0 |

The local database contains 300/300 soak scan IDs, all `completed`, with `items_found` totaling 212. It contains 212 events linked to these run IDs, with 212 unique event IDs and 212 unique dedupe keys. None of the 212 keys resolved to a prior queue's task run.

### Sync recovery

At 19:15 JST, assignments 000097-000099 (slot04-slot06) were validated and ingested, then Supabase hostname resolution failed. The worker returned one failed invocation (`exit_status=1`, failed=3), queue reconciliation returned `blocked_sync_retry`, and no affected slot advanced.

At 19:20 JST, the next natural worker invocation completed three newly arrived payloads and resumed 000097-000099 from processed/canonical state. All six completed, the queue refilled, and the three affected queue entries retained `attempt_count=1`.

| Metric | Result |
|---|---:|
| sync retry count | 3 |
| recovered sync failures | 3 |
| unrecovered sync failures | 0 |
| Web re-research caused by sync retry | 0 |

## Scheduled Work utilization

Only task runs started inside the queue audit window are counted. Post-completion empty runs are excluded.

| Task | Runs | Productive | Empty | Busy | Completions | Research failures | Company retries | Companies/productive run | Average / max duration (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| task01 | 12 | 12 | 0 | 0 observed | 36 | 0 | 0 | 3.0 | 193.9 / 263 |
| task02 | 13 | 13 | 0 | 0 observed | 39 | 0 | 0 | 3.0 | 199.3 / 299 |
| task03 | 13 | 13 | 0 | 0 observed | 39 | 0 | 0 | 3.0 | 198.7 / 274 |
| task04 | 13 | 13 | 0 | 0 observed | 39 | 0 | 0 | 3.0 | 195.4 / 391 |
| task05 | 13 | 13 | 0 | 0 observed | 39 | 0 | 0 | 3.0 | 187.2 / 255 |
| task06 | 12 | 12 | 0 | 0 observed | 36 | 0 | 0 | 3.0 | 209.3 / 330 |
| task07 | 12 | 12 | 0 | 0 observed | 36 | 0 | 0 | 3.0 | 217.2 / 363 |
| task08 | 12 | 12 | 0 | 0 observed | 36 | 0 | 0 | 3.0 | 193.1 / 272 |
| **Total** | **100** | **100** | **0** | **0 observed** | **300** | **0** | **0** | **3.0** | — |

Maximum snapshot size was three; runs with four or more assignments: zero. No active guards or same-task interval overlaps remained, no stale guard was recovered, and the shortest same-task start gap was 3,395 seconds versus a maximum run duration of 391 seconds. Thus overlap/busy/lost-company evidence is zero. Busy returns are not written as dedicated run records, so “0 observed” is an artifact-plus-timing conclusion rather than a separate scheduler event counter.

## Logical slot utilization

All slot failure and company-retry counts are zero. Latency is assignment creation to canonical completion and therefore mostly reflects the next hourly Scheduled Work opportunity, not worker polling.

| Slots | Completions each | Average latency each (s) | Maximum latency each (s) |
|---|---:|---:|---:|
| slot01-slot03 | 12 | 3,412.9 | 3,643 |
| slot04-slot06 | 13 | 3,171.0 | 3,775 |
| slot07-slot09 | 13 | 3,353.6 | 4,435 |
| slot10-slot12 | 13 | 3,240.2 | 3,896 |
| slot13-slot15 | 13 | 3,359.5 | 4,136 |
| slot16-slot18 | 12 | 3,297.4 | 3,640 |
| slot19-slot21 | 12 | 3,401.7 | 3,956 |
| slot22-slot24 | 12 | 3,385.6 | 3,505 |

Completion balance is 12 or 13 per slot and 36 or 39 per task; no abnormal concentration is present. The only transient incident touched slot04-slot06 together and was an external DNS outage, not a slot-specific defect.

## Inbox worker and hidden execution

Current read-only Task Scheduler evidence:

- Task: `CompanyNewsInboxWorker`; state Ready; last result 0; missed runs 0.
- Trigger repetition: PT5M; next run remained scheduled.
- Multiple instances policy: IgnoreNew.
- Action: `wscript.exe //B //NoLogo` -> `run_company_news_worker_hidden.vbs` -> `.venv\Scripts\pythonw.exe` -> one-shot worker.
- Working directory: repository root; execution limit: PT5M.
- The VBS uses window style 0, waits for completion, and propagates the worker exit code.

Within the soak, 300 work outputs were detected and completed. There was one failed worker invocation—the recovered DNS incident—and no worker process, console, focus, or launcher error. Processed-file mtime to worker detection latency averaged 142.69 seconds, median 123.54 seconds, p95 285.00 seconds, and maximum 340.49 seconds. The small over-five-minute tail reflects schedule jitter; with three companies per hourly task, PT5M polling was not the throughput bottleneck.

TDNET runtime evidence was excluded and no TDNET file or task was inspected as Company News failure evidence.

## Throughput and weekly projection

| Metric | Result |
|---|---:|
| companies/hour | 24.0401 |
| companies/day | 576.9616 |
| utilization versus 24/hour | 100.17% |
| projected 3,800 hours | 158.0694 |
| projected 3,800 days | 6.5862 |
| buffer versus 168 hours | 9.9306 hours |
| buffer days | 0.4138 days |

The slight result above theoretical 24/hour comes from staggered task phases and measuring queue activation through final completion; it is not evidence that a steady-state task exceeds its three-company hourly contract. Reliability classification is **B: technically within seven days but thin buffer**. A network/PC/sleep/update interruption longer than about ten hours, or accumulated Scheduled Work delays, can miss the weekly target. Empty or busy runs after completion do not affect the measured soak interval.

## Database growth and capacity

All measurements were read-only. No VACUUM, DELETE, schema change, or data repair was performed.

### Current state

| Store | Database size | Events rows | Scan rows | Events heap / indexes / total relation | Scan heap / indexes / total relation |
|---|---:|---:|---:|---:|---:|
| local SQLite | 379,314,176 B (361.74 MiB) | 286 | 424 | 1,138,688 / 110,592 / 1,249,280 B | 94,208 / 49,152 / 143,360 B |
| Supabase PostgreSQL | 2,411,121,811 B (2.25 GiB) | 286 | 424 | 737,280 / 188,416 / 2,211,840 B | 122,880 / 98,304 / 262,144 B |

Supabase total relation size includes TOAST and auxiliary storage; relevant event+scan index size is 286,720 bytes and total relation size is 2,473,984 bytes. Exact Supabase row counts match SQLite. Repository configuration exposes connectivity but no plan/tier/capacity ceiling, so free/remaining capacity is not determinable without the Supabase account dashboard or management API entitlement.

### Soak-derived estimates

There was no pre-soak relation-size snapshot, so growth is inferred from current allocated relation bytes per current row, not claimed as a direct before/after measurement.

| Estimate | SQLite allocation model | Supabase total-relation model |
|---|---:|---:|
| bytes/event row | 4,368 | 7,734 |
| bytes/scan row | 338 | 618 |
| 300-company soak | 1,027,474 B (0.98 MiB) | 1,825,025 B (1.74 MiB) |
| bytes/scanned company | 3,425 | 6,083 |
| bytes/news-present company | 7,037 | 12,500 |
| bytes/generated event, including scan share | 4,847 | 8,609 |

Processed JSON artifacts added 389,939 bytes outside the database. This is not included in database projections.

### 3,800-company weekly projections

Raw assumes the 300-company event rate (212/300) and current per-row allocation continue linearly. Safety is 2x raw for row-size variation, page/index/TOAST overhead, and operational margin. Repeated-event dedupe could make actual event growth lower; the model intentionally does not depend on it.

| Horizon | SQLite raw / 2x safety | Supabase raw / 2x safety |
|---|---:|---:|
| one week | 12.41 / 24.82 MiB | 22.05 / 44.09 MiB |
| one month (52/12 weeks) | 53.78 / 107.57 MiB | 95.53 / 191.07 MiB |
| one year (52 weeks) | 0.63 / 1.26 GiB | 1.12 / 2.24 GiB |
| three years | 1.89 / 3.78 GiB | 3.36 / 6.72 GiB |
| five years | 3.15 / 6.30 GiB | 5.60 / 11.20 GiB |

The scan ledger alone adds 197,600 rows/year. At current allocation this is approximately 63.72 MiB/year in SQLite and 116.51 MiB/year in Supabase, before the 2x safety factor. A retention policy will become useful even though it is not required for immediate correctness: consider 180-day detailed scan retention, monthly aggregation of older scans, and permanent `last_successful_scan_at`. Do not apply that policy without a separate data-governance decision.

News events should remain long-lived historical qualitative evidence for earnings-carry analysis. Retention should target the repetitive scan ledger, not delete canonical news events merely to save space.

Supabase capacity risk is **unquantified/moderate**: present size and growth can be measured, but contracted capacity and remaining headroom cannot. At the conservative projection, five years can add about 11.20 GiB. Confirm the project plan, database quota, and alert thresholds before weekly production.

## Residual risks and recommendation

1. The 9.93-hour weekly throughput buffer is thin for a local Windows host exposed to sleep, restart, Windows Update, connectivity loss, and Scheduled Work delay.
2. Supabase contracted capacity and remaining headroom are unknown; current database size is 2.25 GiB.
3. Successful internal atomic retries and busy task exits are not persisted as dedicated metrics; both are zero observed rather than perfectly instrumented counts.
4. Mutable runtime state remains under OneDrive. Hardening passed this soak, but external filesystem filters remain an availability risk.
5. The projection assumes the pilot's event rate and average row size; real long-term event mix may differ.

No actual code bug was found and no code, schema, task, queue, assignment, database, Supabase row, Viewer, or TDNET change was made. No pytest run was necessary. Static/read-only checks comprised queue status, 300 payload canonical validation, queue/artifact/log reconciliation, exact SQLite checks, read-only Supabase size/count queries, Task Scheduler inspection, duplicate scans, and residue searches.

Do not activate the 3,800-company weekly queue until the operator confirms (a) sufficient awake/network availability or adds scheduling capacity, and (b) Supabase quota/headroom. No additional correctness soak is required; an optional timed resilience exercise is useful only if those operational assumptions change. Since the queue is completed, Task01-task08 may be stopped; leaving them enabled only creates no-ready/empty runs.

## Required final checklist (1-74)

1. queue_id: `pilot300-20260830T150501849901`
2. queue_status: completed
3. completed / total: 300 / 300
4. failed: 0
5. pending: 0
6. active: 0
7. assignment_sequence: 300
8. start: 2026-08-30 15:06:20 JST
9. finish: 2026-08-31 03:35:05 JST
10. elapsed: 44,925 seconds (12.4792 hours)
11. no-news count: 154
12. news-present count: 146
13. total news events generated: 212
14. average events/news-present company: 1.4521
15. stale payload count: 0
16. stale classification: no cases
17. validation failure count: 0
18. validation causes: no cases
19. total company retry count: 0
20. necessary retry count: 0
21. unnecessary retry count: 0
22. WinError 5 count: 0
23. WinError 32 count: 0
24. atomic transient retry count: 0 observed; successful internal retries are not durably instrumented
25. atomic retry exhausted count: 0
26. duplicate canonical count: 0
27. duplicate scan count: 0
28. duplicate completion count: 0
29. sync retry count: 3, all DNS-related and recovered naturally
30. unrecovered sync failure: 0
31. research failure count: 0
32. lost company count: 0
33. total scheduled runs: 100 in the exact queue window
34. productive runs: 100
35. empty runs: 0 in the window
36. busy skips: 0 observed
37. overlaps: 0
38. companies/productive run: 3.0
39. task01-task08 completions: 36, 39, 39, 39, 39, 36, 36, 36
40. slot01-slot24 completions: 12,12,12,13,13,13,13,13,13,13,13,13,13,13,13,12,12,12,12,12,12,12,12,12
41. worker failed runs: 1; next PT5M run recovered all three syncs
42. worker refill bottleneck: no; detection average 142.69s, p95 285.00s, max 340.49s
43. companies/hour: 24.0401
44. companies/day: 576.9616
45. utilization vs 24/hour: 100.17%
46. estimated hours for 3,800: 158.0694
47. estimated days for 3,800: 6.5862
48. buffer vs 168 hours: 9.9306 hours
49. buffer days: 0.4138
50. Production DB current size: SQLite 379,314,176 B; Supabase 2,411,121,811 B
51. canonical_news_events current rows: 286 local and Supabase
52. canonical_news_scan_runs current rows: 424 local and Supabase
53. events table size: SQLite total 1,249,280 B; Supabase total relation 2,211,840 B
54. scan table size: SQLite total 143,360 B; Supabase total relation 262,144 B
55. relevant index size: SQLite 159,744 B; Supabase 286,720 B
56. 300-company inferred increase: SQLite 1,027,474 B; Supabase 1,825,025 B
57. bytes/company: SQLite 3,425; Supabase 6,083
58. 3,800/week estimate: SQLite 12.41 MiB raw/24.82 MiB safe; Supabase 22.05/44.09 MiB
59. one-month projection: Supabase 95.53 MiB raw/191.07 MiB safe
60. one-year projection: Supabase 1.12 GiB raw/2.24 GiB safe
61. three-year projection: Supabase 3.36 GiB raw/6.72 GiB safe
62. five-year projection: Supabase 5.60 GiB raw/11.20 GiB safe
63. Supabase capacity risk: moderate/unquantified; tier and remaining quota unavailable
64. future scan retention policy: yes; propose only, do not implement now
65. actual code bug: none found
66. code changes: none
67. changed file: this sanitized audit report only
68. tests/static checks: no pytest; read-only validation/reconciliation/DB/Supabase/task checks passed
69. audit report: `output/news_soak_audit/pilot300-20260830T150501849901/SOAK_AUDIT.md`
70. commit ID: populated in final handoff after report-only commit
71. residual risks: thin uptime buffer, unknown Supabase quota, atomic/busy observability, OneDrive transient locks, projection variance
72. proceed to 3,800 weekly production: not yet; proceed after uptime/capacity review, without another correctness fix
73. additional soak: not required for correctness; optional resilience test only
74. stop task01-task08: yes; queue is complete and further runs are empty/no-ready
