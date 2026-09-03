# Company Viewer Qualitative News Monitor v1

```text
ChatGPT Work / future collector
        ↓
company_news_v1 JSON
        ↓
data/news_inbox / ingestion adapter
        ↓
SQLite canonical_news_events + canonical_news_scan_runs
        ↓
Supabase canonical tables
        ↓
api_latest_news_events + api_latest_news_scan_runs
        ↓
Company Viewer News Monitor
        ↓
future earnings carry research
```

## Desktop Work local bridge v1

The first transport smoke test uses one explicit slot and never advances by itself:

```text
data/news_work/slots/slot01/assignment.json (read-only to Work)
        ↓ Desktop Work web research
data/news_inbox/work_slot01_<assignment_id>.json
        ↓ tools/company_news_work_bridge.py process
existing company_news_v1 validator + inbox ingestion
        ↓
existing SQLite canonical tables + Supabase sync
        ↓
STOP after one completed assignment
```

The coordinator owns assignment status, state, locking, logs, validation, ingestion,
and sync. Desktop Work must not access the local database, Supabase credentials, or
the ingestion/sync scripts. `state/slot01.json` makes a post-ingestion sync retry
resume-safe; exact event and run idempotency remains owned by the canonical layer.

```powershell
python tools/company_news_work_bridge.py status
python tools/company_news_work_bridge.py process
```

`process --dry-run-sync` is reserved for local transport tests. Normal Desktop Work
output must be finalized with `process` so the existing production sync runs.

The transport layer is replaceable. A local file, cloud endpoint, or ChatGPT Work
connector can produce the same contract without changing the canonical schema or
anything downstream.

## Scheduled transport bridge v2

V2 automates transport only; the `company_news_v1`, canonical SQLite, Supabase,
and Viewer contracts remain unchanged:

```text
ChatGPT Desktop Scheduled Work (local project, slot01 only)
        ↓ atomic .tmp → .json rename
data/news_inbox/work_slot01_<assignment_id>.json
        ↓ CompanyNewsInboxWorker, every 5 minutes
validate → canonical ingest → Supabase sync
        ↓
assignment completed → processed archive → STOP
```

`tools/company_news_inbox_worker.py --once --trigger task_scheduler` performs one
bounded scan. Windows Task Scheduler supplies polling; there is no resident watcher
and no automatic next-company advance. `tools/install_company_news_worker_task.ps1`
registers `CompanyNewsInboxWorker` for the current interactive user, sets a five-minute
repetition interval, and configures `IgnoreNew`. Its Task Action is the GUI-subsystem
`wscript.exe //B //NoLogo`, which invokes
`tools/run_company_news_worker_hidden.vbs`. The wrapper uses window style `0`, waits
for the repo `.venv`'s `pythonw.exe`, and returns the worker's exact exit code to Task
Scheduler. The action keeps the repository root as its working directory.

This wrapper is required even though the selected executable is named `pythonw.exe`.
The uv-created Windows virtual environment launcher was measured as PE subsystem
`WINDOWS_CUI`; a natural scheduled launch produced
`Task Scheduler -> venv pythonw.exe -> conhost.exe / uv base python.exe`. The worker
business path itself has no subprocess, shell, PowerShell, or command-interpreter
launch. Hiding that measured launcher chain at its GUI parent removes the visible
console/focus-steal source while preserving stdout/stderr independence, synchronous
completion, timeout handling, logs, and exit status. Scheduled Work runs each task
hourly, so a refill delay of at most five minutes still leaves ample time before the
next run while reducing scheduled worker polls from 1,440 to 288 per day. A second
PID-sentinel lock provides application-level overlap protection. PID liveness uses
non-destructive Windows process handles and closes every handle after polling.

The worker isolates invalid payloads in `quarantine`, archives successful and exact
duplicate payloads under `processed`, and persists generic-run sync resume state in
`data/news_work/state/inbox_worker.json`. Work assignment resume state stays in
`state/slot01.json`. JSONL logs record `detected`, `validated`, `ingested`, `synced`,
`completed`, `quarantined`, and `failed`, plus invocation-level `worker_started`,
`worker_error`, and `worker_finished` records. The finish record includes trigger,
exit status, detected/completed/quarantined/failed counts, and processed count so
background failures remain auditable without stdout or stderr. Assignment status
changes to `completed` only after sync succeeds.

The Scheduled Work prompt is `data/news_work/SCHEDULED_TASK_SMOKE_PROMPT.txt`.
The task must run in the local project, read the current assignment on every run,
and stop without writing when its status is not `ready`. It may write only the final
contract JSON; it must not access SQLite, credentials, Supabase, or transport scripts.
Per official OpenAI guidance, the computer and ChatGPT Desktop app must be running
when a Scheduled task needs local project files.

The unattended fixture uses a temporary task and isolated root/database with
`--dry-run-sync`. It validates natural Task Scheduler launch, inbox detection,
canonical ingest, completion, and cleanup without touching production news tables or
Supabase. The real one-slot Scheduled Work run is the separate production smoke that
can advance the verdict from ready to fully unattended confirmation.

The 2026-08-29 natural-run fixture completed with Windows Task Scheduler result `0`,
one isolated canonical event, one isolated scan run, assignment phase `completed`,
processed archive present, and `output_detected_by=task_scheduler`. Its temporary
task, files, and database were then removed. The production worker subsequently ran
an empty-inbox poll with result `0`; no production news row was created by either
verification.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/install_company_news_worker_task.ps1
Get-ScheduledTask -TaskName CompanyNewsInboxWorker
```

## One-slot company queue and auto advance v3

V3 keeps the successful schema, ingestion, sync, and Viewer path unchanged and
adds a durable queue before the existing slot assignment:

```text
data/news_work/queue/company_queue.jsonl + queue_state.json
        ↓ deterministic pending company
data/news_work/slots/slot01/assignment.json
        ↓ same generic Scheduled Work reads current assignment every run
company_news_v1 JSON → news_inbox
        ↓ CompanyNewsInboxWorker
validate → canonical ingest → Supabase sync → assignment completed
        ↓ only after the completed canonical scan and successful sync
queue entry completed → atomic next assignment → next company
        ↓ after the fifth pilot company
queue_status completed → slot phase idle → STOP
```

`tools/company_news_queue.py` owns the queue files, queue PID lock, deterministic
ordering, retry count, scan-history window, and assignment transition journal. The
worker calls its reconciliation hook only when runtime queue files exist, so an
uninitialized queue leaves the V2 production path unchanged. Assignment replacement
uses both the queue lock and the existing Windows-safe slot lock. The transition is
persisted before the assignment is atomically replaced; after a crash, the next
worker run either finishes that exact transition or observes the already-written
assignment, preventing duplicate advance.

The initial search window is seven calendar days inclusive. A future revisit uses
the last successful `canonical_news_scan_runs.checked_at` date, clamped to the
current date. An `items=[]` payload is a normal completed scan. A validation-stage
failure gets one retry (`max_attempts=2`, including the first attempt), then the
entry becomes `failed` and the queue moves on. A sync-stage failure never advances:
the existing processed payload and `ingested` bridge state resume sync first.

The tracked `data/news_work/queue/README.md` is documentation only; no production
queue JSON is committed or initialized by this implementation. `init-pilot` creates
an inert five-company fixture queue by default. Writing the first real slot01
assignment requires an explicit human `--activate`:

```powershell
python tools/company_news_queue.py status
python tools/company_news_queue.py init-pilot
python tools/company_news_queue.py resume --activate
python tools/company_news_queue.py pause
python tools/company_news_queue.py reset-pilot --confirm RESET-PILOT
```

The reusable Work instruction is
`data/news_work/SCHEDULED_TASK_SLOT01_PROMPT.txt`. It contains no ticker, company,
or assignment ID; every Scheduled run reads `slot01/assignment.json` and exits
without research or output unless the current status is `ready`.

## Contract and atomicity

`company_news_v1` requires `run_id`, ticker, timezone-aware `checked_at`, collector
type, and `items` (which may be empty). Each item contains a headline, timezone-aware
publication time, http/https source, taxonomy fields, summary, rationale, and temporal
validity. A payload is validated in full and committed as one SQLite transaction. A
bad item quarantines the whole run; where its identity is valid, the failure is saved
in the scan ledger.

## Identity and copyright

`dedupe_key = SHA-256(ticker + canonical URL + published_at + normalized headline)`.
The event UUID is deterministically derived from that key. URL tracking parameters
and fragments are removed, but different sources are never semantically merged.
Raw payload JSON is retained for reclassification; article bodies are not accepted,
known full-text/HTML fields are rejected, structured items are size-limited, and
evidence excerpts are capped at 1,000 characters.

## Operations

```powershell
python tools/ingest_company_news.py
python tools/sync_company_news.py --dry-run
python tools/sync_company_news.py
```

Successful inbox files move to `processed`; invalid files move to `quarantine` with
an adjacent error message. Viewer read models omit `raw_payload`, join the existing
company master for display, and are indexed and queried with server-side filters,
sort keys, and limits.

## Five-slot global queue (v4)

The v4 coordinator generalizes the proven slot01 path without changing
`company_news_v1`, canonical tables, Supabase, or Viewer contracts:

```text
one global company_queue.jsonl
        |
        +-- slot01 assignment -- Scheduled Work 01 --+
        +-- slot02 assignment -- Scheduled Work 02 --+
        +-- slot03 assignment -- Scheduled Work 03 --+--> common news_inbox
        +-- slot04 assignment -- Scheduled Work 04 --+          |
        +-- slot05 assignment -- Scheduled Work 05 --+          v
                                                    one CompanyNewsInboxWorker
                                                             |
                                                   canonical DB -> Supabase -> Viewer
                                                             |
                                                   completed slot is refilled
```

There is one queue, not five queues. Each entry records `assigned_slot` and
`assignment_id`; an assigned company can therefore belong to only one slot. The
queue state records a configurable `slot_count`, per-slot state, and a per-slot
transition journal. New queues accept `--slots` and `--count`; the existing defaults
remain one slot and five companies.

Lock order is strictly:

```text
worker lock -> global queue lock -> one slot lock
```

The coordinator never holds two slot locks simultaneously. Assignment selection,
entry ownership, and transition creation occur under the global lock. Each slot
assignment is then atomically replaced under that slot lock. A crash after planning
or during a slot write leaves a durable transition for only that slot; the next
worker run completes it before selecting another company. A slot assignment is
owned only when `queue_id`, `slot_id`, `queue_position`, and the persisted
`assignment_id` agree. Assignment-name pattern matching is not ownership.

The single worker routes `work_slotNN_<assignment_id>.json` to the matching current
slot. A late file for an old assignment is quarantined and cannot complete or advance
the current assignment. Canonical run IDs preserve existing database idempotency.
Duplicate completion therefore cannot increment queue completion or allocate a
second next company.

Completion immediately refills only the freed slot; there is no batch barrier.
Validation failure retries the same company in the same slot once, then marks that
entry failed and refills the slot. Ingested data with failed Supabase sync stays on
the same assignment and resumes from the processed payload. While paused, completed
or failed assignments may be reconciled, but empty slots are not refilled. Resume
fills every empty slot under the global lock. The queue becomes completed only when
every entry is completed or failed, no transition remains, and all slots are idle.

Terminal unmanaged assignments are copied verbatim to
`slots/slotNN/history/<assignment_id>.json` before replacement. Non-terminal
unmanaged assignments (`ready`, `running`, `processing`, or an unknown status) block
only their slot and are never overwritten.

For the isolated 15-company real pilot, initialize but do not activate with:

```powershell
python tools/company_news_queue.py init-pilot --slots 5 --count 15
python tools/company_news_queue.py status
```

If the runtime files contain a completed predecessor queue, `init-pilot` first
copies its exact JSONL/state files to `data/news_work/queue/history/<queue_id>/`.
Active, paused, fixture, partially missing, or conflicting history files are never
replaced. This preserves the successful five-company pilot while allowing the new
pilot to receive a different queue ID.

After the five Scheduled Work tasks are configured, activation is explicit:

```powershell
python tools/company_news_queue.py activate
python tools/company_news_queue.py status
```

Scheduled Work prompts share
`data/news_work/SCHEDULED_TASK_COMMON_PROMPT.txt`; each thin slot prompt reads only
its own `slots/slotNN/assignment.json`. Recommended staggering is slot01 at `:00`,
slot02 at `:10`, slot03 at `:20`, slot04 at `:30`, and slot05 at `:40` each hour.

## v5: eight batched Scheduled Tasks and 24 logical slots

The 100-company soak uses eight Scheduled Work tasks while retaining one global
queue and one Windows inbox worker:

```text
GLOBAL QUEUE
     |
24 logical slots
     |
8 Scheduled Tasks (each owns 3 slots)
     |
Web research (immutable start-of-run snapshot)
     |
common news_inbox
     |
single Windows worker
     |
canonical DB -> Supabase -> NEWS Viewer
     |
logical slot refill
```

The centralized mapping is `task01 -> slot01..03`, `task02 -> slot04..06`,
through `task08 -> slot22..24`. Queue state persists `task_count`, `batch_size`,
`logical_slot_count`, and the exact `task_slots` mapping. Legacy one-slot and
five-slot queues are read as one company per task and retain their existing CLI
and assignment behavior.

Each task acquires an atomic task-level guard and snapshots its three assignment
files once with `tools/company_news_task_batch.py`. Only ready assignments in that
snapshot may be processed. Slot refill can happen immediately after any result,
but a task never rereads assignments and therefore cannot process a fourth company
in the same run. A second overlapping run sees the guard and exits; abandoned
guards become recoverable after two hours. Release records task-run counts outside
queue state for soak metrics.

Companies in one snapshot are researched sequentially and independently. A valid
no-news result is a normal `company_news_v1` payload with `items=[]`. An operational
research failure is instead an atomic `company_news_work_failure_v1` sidecar. The
single worker validates its task, slot, assignment, ticker, and queue ownership,
marks only that assignment failed, and lets the existing two-attempt retry policy
refill the slot. The other two companies remain usable. Late success or failure
files are quarantined and cannot complete a new assignment.

All queue mutation remains under the global queue lock. Lock order remains
`worker lock -> global queue lock -> one slot lock`; code never holds two slot
locks simultaneously. Assignment transitions are durable, so restart recovery
replays the same assignment IDs without incrementing the sequence twice. Pause
continues accepting terminal results but suppresses refill; resume fills all idle
logical slots. Completion requires every company terminal, every slot idle, and no
pending transition.

The soak sample is deterministic and sector-stratified using fixed hash ordering
within sectors and round-robin selection across sectors. Companies without a
successful canonical scan are selected first, avoiding heavy reuse of the recent
5- and 15-company pilots without considering news volume, stock performance, or
investment merit.

Initialize the inert 100-company fixture with:

```powershell
python tools/company_news_queue.py init-soak --count 100 --tasks 8 --batch-size 3
python tools/company_news_queue.py status
```

Only after all eight Scheduled Tasks are reviewed should a human run:

```powershell
python tools/company_news_queue.py activate
```

Activation fills at most 24 unique companies; later completion refills only the
freed logical slot. The theoretical ceiling is 24 companies/hour and 576/day.
Status reports actual elapsed throughput, retry and result counts, per-task and
per-slot completions, and a 3,800-company duration estimate; theoretical and
measured values must not be conflated. Weekly queue regeneration is intentionally
outside v5.

## v6 candidate: eight tasks, four companies per run, and 32 logical slots

The queue, worker, and task snapshot layers are parameterized by `task_count` and
`batch_size`. The proven 8x3 topology remains supported; the new production
candidate is explicitly initialized as 8x4:

```text
GLOBAL QUEUE
     |
32 logical slots
     |
8 Scheduled Tasks (each owns 4 slots)
     |
immutable start-of-run snapshot (maximum 4)
     |
common news_inbox -> one PT5M hidden worker
     |
canonical DB -> Supabase -> NEWS Viewer
```

The generated mapping is `task01 -> slot01..04`, `task02 -> slot05..08`, through
`task08 -> slot29..32`. Neither queue nor worker contains a slot24/slot32 upper
bound; the supported slot identifiers are generated from the persisted queue
configuration. Filename routing, assignment ownership, transitions, state,
history, locks, quarantine, and processed archives all use the same `slotNN`
contract.

Each task snapshots its four assignment files once. Refill may place a fifth
company into a freed slot, but that assignment is absent from the immutable
snapshot and cannot be processed until the next hourly run. A partial snapshot
processes only the ready assignments that existed at capture time. Each success
or `company_news_work_failure_v1` sidecar is independent, so one failure does not
roll back the other three results. Lock order remains `worker -> global queue ->
one slot`; no operation holds two slot locks.

Task guard busy skips and expired-guard recoveries are appended to per-task
`events.jsonl` records. This makes 8x4 overlap pressure measurable without
changing queue state or treating a safe busy exit as an empty research run.

The eight repository prompts define the 8x4 mapping while the common prompt remains
the schema/research source of truth. The corresponding setup files are stable runtime
routers: absent benchmark control selects the Production prompt and an explicit valid
benchmark control selects the isolated benchmark prompt. Task schedules remain
`:00/:08/:15/:23/:30/:38/:45/:53`.

Create the inert 200-company candidate and capture the pre-soak database baseline:

```powershell
python tools/company_news_queue.py init-soak --count 200 --tasks 8 --batch-size 4
python tools/company_news_queue.py status
python tools/company_news_soak_audit.py storage-snapshot --label before
```

Expected inert state is `fixture_ready`, 200 pending, zero active, and mapping for
32 slots. After reviewing and updating all eight Scheduled Task bodies, a human
may activate once:

```powershell
python tools/company_news_queue.py activate
```

The expected initial active state is 32 unique assignments and 168 pending. No
fifth company is processed by the same task run. The 8x3 form remains available
by passing `--tasks 8 --batch-size 3`; one-slot and five-slot modes are unchanged.

After the queue reaches `completed`, capture the post-soak snapshot and calculate
exact-window task duration, reliability, throughput, and storage projections:

```powershell
python tools/company_news_soak_audit.py audit-soak --storage-before output/news_soak_audit/<queue_id>/storage_before.json
```

This writes only sanitized audit artifacts (`storage_after.json` and
`SOAK_METRICS.json`). SQLite is opened with `mode=ro`; Supabase uses a read-only
PostgreSQL session. Metrics include average/p50/p95/max task duration, overlap,
companies per productive run, measured 3,800-company time, relation deltas,
bytes/company, 4/52/156/260-week projections, and a caveated news-only estimate
against the operator-confirmed Pro 8 GB GP3 baseline. Database disk is not the
same as Storage buckets, and `pg_database_size` does not include every provisioned
disk consumer such as WAL/system overhead.

The theoretical 8x4 ceiling is 32 companies/hour, 768/day, and 118.75 hours
(4.9479 days) for 3,800 companies. It remains a candidate until a natural
200-company soak demonstrates zero loss/duplicate/unrecovered sync failure,
companies per productive run near four, no same-task overlap, max run under 60
minutes, and a measured 3,800-company projection below seven days (strong target:
six days or less). Weekly production generation, retention deletion, automatic
reset, and Supabase capacity changes remain outside v6.

## Additive TSE 33-sector weekly stream

Sector weekly research is independent from Company News Task01-08 and from
`CompanyNewsInboxWorker`. The existing company queue, slots, prompts, canonical
tables, sync, and viewer read models are unchanged.

```text
Windows Task Scheduler: SectorWeeklyScheduler (hourly)
        ↓ no-op outside eligible JST hours
Saturday 06:00..Monday 08:00 → oldest missing current-week sector, at most one per invocation
        ↓ idempotent dedicated assignment queue only
SQLite sector_weekly_work_assignments (ready)
        ↓ ChatGPT Scheduled Task: Sector Weekly Worker (hourly, +5 minutes; last claim Monday 08:05)
claim one current-week item → start → 10-minute heartbeat → Web research → local validation/stage or atomic abandon
        ↓ Sector Weekly dedicated bridge
owner/lease/sector/period/schema validation → atomic local inbox handoff (`sync_pending`)
        ↓ Windows Task Scheduler: SectorWeeklyInboxWorker (PT5M, hidden, IgnoreNew)
hash/schema/assignment validation → idempotent canonical upsert → Supabase sync → assignment success
        ↓
SQLite canonical_sector_reports + canonical_sector_report_runs
Supabase canonical tables → api_latest_news_stream
        ↓
company-memo-app /news (company news + sector reports)
```

The Windows scheduler never calls an LLM, Web Search, OpenAI SDK, Responses API,
or an API key. The ChatGPT task performs research within the user's ChatGPT plan
and uses only local `tools/sector_weekly_work_bridge.py`
claim/start/heartbeat/abandon/stage/fail/status operations. The bridge never performs
external sync. `SectorWeeklyInboxWorker` is the only Sector Weekly transport worker;
it polls the dedicated inbox every five minutes outside the ChatGPT sandbox.
The Sector Weekly queue, payload namespace, schema, lease, and canonical tables are
separate from Company News Task01-08.

All 33 reports in one weekly batch share the exact period from the prior Saturday
06:00:00 JST through the current Saturday 05:59:59 JST. The scheduler derives the
oldest sector without an assignment rather than deriving it from the launch hour, so
a later invocation resumes the backlog after sleep or reboot. Recovery continues
through Monday 08:00 and also re-arms retryable work after all 33 assignments exist,
one item per run. A
repeated invocation at the same injected clock instant is a no-op. The stable identity is
`sector_weekly:<Saturday period-end date>:<two-digit sector code>`.

The registered task starts Saturday 06:00 JST and repeats hourly. The last assignment
slot is Monday 08:00: Saturday contributes 18 slots, Sunday 24, and Monday 9, for 51.
A twelve-hour stop removes 12 slots and leaves 39: enough for 33 first attempts plus
six temporary failures without relying on overlapping task runs. The worker follows
five minutes later, claims at most one sector, and finishes by Monday 08:55.

Claims always include the current fixed period, so previous-period retryable rows
remain visible for manual recovery but cannot pre-empt current work. The initial lease
is 15 minutes, heartbeat renewal requires the same owner and an unexpired lease, and
the total claim lifetime is capped at 55 minutes. The worker heartbeats every 10 minutes,
decides at minute 45 whether a quality-preserving stage is possible, and atomically
abandons to retry before its 50-minute hard budget when it is not. A crashed worker's
lease expires before the next hourly worker invocation. `status` exposes human and JSON
summaries with deterministic completion event keys and distinct exit codes for 33/33,
in-progress, retryable, final-failure, stale, and inconsistent states without payloads.

The installer records a `--not-before` value for the first Saturday 06:00 after
installation and registers the task disabled unless `-Enable` is explicitly supplied.
Invalid JSON, owner mismatch, lease expiry, missing Markdown, invalid sources, or a
period mismatch is rejected before handoff. Same-payload restaging is idempotent;
conflicting payloads fail closed. `sync_pending` and `sync_error` remain
`retry_pending` under the existing schema but are excluded from research claims.
Transport polling never increments `attempt_count`; only invalid research payloads
return to the research retry lane. The Windows worker alone confirms `success` after
the external sync succeeds.

## Optimal batch-size benchmark (isolated, post-8x4 baseline)

The completed `pilot200-20260831T145019404335` soak establishes the operational
performance baseline at eight tasks, batch size four, and 32 logical slots: 200/200
completed, zero failed/retry/stale/validation/sync/research failures, 50 productive
runs, average/p50/p95/max task duration 214.7/213/280/311 seconds, and measured
throughput 31.7558 companies/hour. The subsequent fixed-cohort `batch04` comparison
is formally RUNTIME PASS, RELIABILITY PASS, and QUALITY FAIL: material-event recall
is 53.45%, critical-event recall is 75%, and 7 of 30 reference news-present companies
are false no-news results. Therefore `PRODUCTION_BATCH_4_QUALITY_VALIDATED=false`.
The existing Scheduled Tasks are not changed by the refinement preparation, but a
full Production queue must not start on the assumption that batch four is validated.

The historical `run_duration_seconds` is not a worker or scheduler-polling metric.
`tools/company_news_task_batch.py snapshot` writes `started_at` immediately before
the immutable assignment snapshot is returned to ChatGPT Work. After Work completes
Web research, constructs and validates each payload, and atomically saves every
success/failure file, its final `release` call writes `completed_at`. The reported
duration is the difference between those timestamps. It therefore covers Work-side
research, payload construction, validation/save, and command overhead after snapshot;
it does not cover the later `CompanyNewsInboxWorker` run, PT5M polling delay, local
ingest, SQLite write, or Supabase sync.

`tools/company_news_batch_benchmark.py` adds a completely separate state and output
plane:

```text
data/news_work/batch_benchmark/<benchmark_id>/
  cohort.json
  runs/reference|batchNN/
    manifest.json
    tasks/task01..task08/{queue,active,companies,runs,events}.*

data/news_benchmark_inbox/<benchmark_id>/<run_name>/
  benchmark_taskNN_<assignment_id>.json
```

No benchmark file is written to `data/news_inbox`. The default worker scans only the
top level of `data/news_inbox`, so it cannot discover benchmark results. The harness
imports the canonical validator but never opens a write connection to
`decision_db.db`, never invokes ingest/sync, and has no Supabase write path. The
company_news_v1 payload remains unchanged; timing is recorded only in
`company_news_benchmark_telemetry_v1` sidecars. Each company record contains the
external `research_started_at`, `research_finished_at`, validation completion,
atomic-save completion, elapsed values, position, source/event counts, and result
type. Each task record contains start/first-company/last-company/finish timestamps,
elapsed and summed research/save time, snapshot size, and success/failure counts.
Hidden reasoning or token-thinking time is neither requested nor inferred.

The eight setup files call the read-only `route` command. With no active benchmark
control they return the existing Production task prompt. Benchmark routing requires
all of `benchmark_mode=true`, a valid benchmark/run ID, and an output directory that
exactly resolves under `data/news_benchmark_inbox`. A malformed or partially present
control fails closed instead of falling back to Production. An active benchmark task
snapshots at most its candidate batch size once; later task-queue changes cannot add
an N+1 company to that run.

The fixed cohort is 192 companies, deterministically stratified across TSE 33 sector,
latest available market-cap tercile (large/mid/small), and prior news-present/no-news/
unscanned history. A deterministic 48-company subset is used for single-company
reference research. Every run uses the immutable 2026-08-25 through 2026-08-31 search
window. After the batch-four quality failure, the active candidate order is strictly
reference(1), 2, 3, then 4. Batch sizes 8, 12, 16, 20, and 24 are suspended and the
harness refuses to prepare them during this refinement. The existing cohort and
single-company reference are reused; neither is regenerated.

Reference/candidate event matching first uses normalized source URL, then a
reviewable ticker/date/category plus headline/substance token similarity rule;
ambiguous matches are listed separately. Audits report event recall, high/direct
critical-event recall, precision, no-news false negatives, URL and schema validity,
source/event density, research failures, position-quartile degradation, task
average/p50/p95/max, busy skips, overlap, measured throughput, 3,800-company duration,
weekly buffer, and local output bytes. Global-compression metrics separately report
candidate/reference source-count ratio, research-time ratio, and event recall so a
batch-wide loss is visible even when late positions do not degrade. A PASS requires
100% schema validity, zero research failure, at least 99% valid URLs, 100% critical
recall, at least 95% material
event recall, zero major no-news false negatives, p95 below 45 minutes, max below 55
minutes, and zero busy skip/overlap or late-position degradation.

For quality benchmark v2, reuse `batchbench-20260901T095759508419`; do not run
`prepare`, regenerate its completed reference, or prepare/activate batch03. The
single-company reference is not assumed to be ground truth. Instead, an independent
`reference02` is prepared from the persisted reference queue, preserving the exact
48-company order and 2026-08-25 through 2026-08-31 search window without overwriting
`reference`:

```powershell
python tools/company_news_batch_benchmark.py prepare-reference-replication --benchmark-id batchbench-20260901T095759508419
python tools/company_news_batch_benchmark.py activate-run --benchmark-id batchbench-20260901T095759508419 --run-name reference02
python tools/company_news_batch_benchmark.py status --benchmark-id batchbench-20260901T095759508419 --run-name reference02
python tools/company_news_batch_benchmark.py deactivate-run
```

After reference02 reaches 48/48 with zero failures, a human runs the symmetric
repeatability audit and builds the four-run event union:

```powershell
python tools/company_news_batch_benchmark.py audit --benchmark-id batchbench-20260901T095759508419 --run-name reference02
python tools/company_news_batch_benchmark.py audit-reference-repeatability --benchmark-id batchbench-20260901T095759508419
python tools/company_news_batch_benchmark.py build-quality-union --benchmark-id batchbench-20260901T095759508419
```

The repeatability artifact reports reference01->reference02 and
reference02->reference01 recall, critical recall, news/no-news agreement, source and
event-count agreement, Jaccard similarity, ambiguous matches, and events found by only
one run. It returns `QUALITY_METRIC_NOT_STABLE` if either directional event recall is
below 90%; 95% remains the stronger target, and critical recall targets 100%.

`quality_v2/event_union.json` clusters the reference01, reference02, batch02, and
batch04 48-company results using ticker plus normalized URL or ticker/date/category
plus headline/substance similarity. Every cluster retains pairwise URL, date-distance,
category, headline, and substance evidence. `found_by` has all four runs. Candidate-
only events remain `UNKNOWN` and are never automatically labelled false positives.
The same artifact carries an event-level persistent false-negative audit with the
reference event, source type/category/importance, each candidate result, and the best
available matcher-based reason; it separates shared batch02/batch04 misses from
candidate-specific misses. The generated `quality_v2/gold_adjudication.json` is an
isolated human-work artifact
with verdicts `VALID_MATERIAL`, `VALID_NON_MATERIAL`, `DUPLICATE`,
`MATCHER_FALSE_NEGATIVE`, `INVALID_SOURCE`, `OUT_OF_WINDOW`, and `UNKNOWN`.

The v1 union has 88 clusters and is retained unchanged as comparison evidence. Its
Gold template must not be adjudicated because review found both false splits and a
same-run many-to-one overmerge.

Matcher v2 applies this fixed pipeline: strict within-run identity dedupe, canonical
identity-key generation, cross-run candidates, HIGH-confidence matches only,
deterministic one-to-one assignment, union construction, then MEDIUM-confidence review
output. Ticker is always a hard requirement. Category, importance, and earnings
relevance never decide identity; their agreement is reported separately. Canonical
keys normalize Unicode/trademark/punctuation, bilingual language subdomains and path
tokens, 14-digit disclosure IDs embedded in mirror URLs, and allowlisted stable query
IDs such as `itemid`. Other query values remain in the page key so two articles on the
same path are not merged. Semantic similarity alone never merges two events from the
same run.

The v2 commands read only the completed reference/reference02/batch02/batch04 raw
artifacts and write new outputs under `quality_v2/matcher_v2`; they do not overwrite
the v1 union or Gold template:

```powershell
python tools/company_news_batch_benchmark.py rebuild-quality-union-v2 --benchmark-id batchbench-20260901T095759508419
python tools/company_news_batch_benchmark.py audit-reference-repeatability-v2 --benchmark-id batchbench-20260901T095759508419
```

`event_union_v2.json` and `gold_adjudication_v2.json` are the new review inputs.
`matcher_merge_candidates.json` holds probable or one-to-one-conflicting pairs without
auto-merging them. `matcher_overmerge_audit.json` compares v1 and v2 same-run members,
including the Bluebon two-product case. Human Gold v2 adjudication still prioritizes
company IR/press releases, TDNET, EDINET/SEC, government, exchange, and other official
sources. Until every v2 `UNKNOWN` is adjudicated, all reference and candidate quality
scores remain provisional and `ready_for_production_change` remains false.

The existing-artifact v2 rebuild produced 82 clusters from the v1 total of 88. It
joined seven verified false-split groups, split the one Bluebon same-run overmerge,
left zero same-run multi-member clusters, and emitted zero ambiguous candidates.
Reference01-to-reference02 all-event recall improved from 46.55% to 60.34%, the
reverse direction from 50.94% to 66.04%, and event Jaccard from 32.53% to 46.05%.
Critical recall improved from 62.5%/70.0% to 75.0%/90.0%. News-presence agreement
remained 77.08%, so the formal repeatability decision is still
`QUALITY_METRIC_NOT_STABLE`; these improvements validate matcher behavior, not the
research quality of any batch size. All 82 Gold v2 rows remain `UNKNOWN` pending human
adjudication.

After human adjudication is complete:

```powershell
python tools/company_news_batch_benchmark.py audit-gold-v2 --benchmark-id batchbench-20260901T095759508419 --adjudication data/news_work/batch_benchmark/batchbench-20260901T095759508419/quality_v2/matcher_v2/gold_adjudication_v2.json
```

Only then is batch03 reconsidered. Existing compression facts remain attached to the
run audits. Batch02's measured 15.9273 companies/hour implies 9.941 days for 3,800 and
cannot meet a weekly 3,800-company cycle in the current eight-task arrangement, even
if its later Gold quality passes. If both batch02 and batch04 fail Gold quality, the
next step is research-architecture redesign rather than larger batches.

### Blind Gold v2 adjudication packets

Gold v2 Web adjudication is performed outside the benchmark harness. The harness only
builds deterministic blind packets, validates complete packet responses, and imports
validated results. Packet generation orders all 82 clusters by ticker, event date, and
cluster ID, keeps every company's events together, and partitions them into 8-10 event
packets. Each packet SHA-256 covers canonical sorted-key JSON excluding only the
`packet_sha256` field; no timestamps are included, so identical unions produce
identical packet bytes and hashes.

For `batchbench-20260901T095759508419`, this produces nine packets with event
counts `8, 8, 9, 10, 10, 10, 10, 8, 9` (82 total).

Packets contain cluster/company identity, date, canonical headline, summary,
`why_it_matters`, and de-duplicated source URLs. They never contain `found_by`, run
labels, candidate presence, matcher evidence, or members. The separate provenance
manifest stays outside `gold_packets` and is joined only after Gold completion.

```powershell
python tools/company_news_batch_benchmark.py build-gold-packets-v2 --benchmark-id batchbench-20260901T095759508419
python tools/company_news_batch_benchmark.py gold-status-v2 --benchmark-id batchbench-20260901T095759508419
python tools/company_news_batch_benchmark.py import-gold-adjudication-v2 --benchmark-id batchbench-20260901T095759508419 --input <response.json>
```

The response contract is `company_news_gold_adjudication_response_v2`. Import rejects
schema, benchmark, packet ID, packet SHA, cluster coverage, duplicate cluster, enum,
URL, datetime, evidence, and duplicate-target violations before any Gold write. A
successful import content-addresses and atomically preserves the previous Gold file in
`gold_backups`, then atomically replaces `gold_adjudication_v2.json`. Re-importing an
identical response is a no-op. Status remains `GOLD_INCOMPLETE` while any event is
`UNKNOWN`; `GOLD_COMPLETE` only means every event is adjudicated and still requires
Gold metric review before batch03 can run.

Gold scoring collapses `DUPLICATE` and `MATCHER_FALSE_NEGATIVE` relationships to one
event, treats candidate-only `VALID_MATERIAL` events as valid discoveries, and scores
reference01, reference02, batch02, and batch04 symmetrically. It reports material
recall, critical-material recall, material precision, and company-level material/no-
news false negatives. `VALID_NON_MATERIAL`, `INVALID_SOURCE`, and `OUT_OF_WINDOW`
participate in the precision denominator; candidate-only events are never classified
as false positives without adjudication.

Only the final candidate receives the separate integration burst fixture. It contains
synthetic files under `data/news_benchmark_load`, requires a temporary SQLite copy,
disables Production Supabase writes, and exposes fields for worker duration/backlog,
SQLite waits and WinError 5/32, sync retries, IgnoreNew skips, TDNET fixture durations,
duplicates/console visibility, and test-endpoint sync duration. Batch size changes do
not change annual news volume; the benchmark itself has zero Supabase capacity delta
and records only local isolated output size. The human fills the generated result
template and audits it with:

```powershell
python tools/company_news_batch_benchmark.py audit-integration-fixture --manifest <fixture>/manifest.json --result <fixture>/human_result.json
python tools/company_news_batch_benchmark.py recommend --benchmark-id <id>
```

Recommendation walks 2, 3, and 4 in order and selects the largest contiguous candidate
whose complete audit is PASS. A quality failure cannot be offset by throughput. If two
fails, the recommendation is the single-company reference and research-architecture
redesign is required; if three fails after two passes, two is recommended; if two and
three pass while four fails, three is recommended. A batch-four failure blocks all
upward candidates. Ratios use the single-company reference rather than batch four as
their baseline. `ready_for_production_change` remains false until the matching final
candidate integration audit passes, and human review remains mandatory.
