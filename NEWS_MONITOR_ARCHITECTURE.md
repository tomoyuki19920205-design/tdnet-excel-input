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
        ↓ CompanyNewsInboxWorker, every 1 minute
validate → canonical ingest → Supabase sync
        ↓
assignment completed → processed archive → STOP
```

`tools/company_news_inbox_worker.py --once --trigger task_scheduler` performs one
bounded scan. Windows Task Scheduler supplies polling; there is no resident watcher
and no automatic next-company advance. `tools/install_company_news_worker_task.ps1`
registers `CompanyNewsInboxWorker` for the current interactive user, uses the repo
`.venv`, sets a one-minute repetition interval, and configures `IgnoreNew`. A second
PID-sentinel lock provides application-level overlap protection. PID liveness uses
non-destructive Windows process handles and closes every handle after polling.

The worker isolates invalid payloads in `quarantine`, archives successful and exact
duplicate payloads under `processed`, and persists generic-run sync resume state in
`data/news_work/state/inbox_worker.json`. Work assignment resume state stays in
`state/slot01.json`. JSONL logs record `detected`, `validated`, `ingested`, `synced`,
`completed`, `quarantined`, and `failed`. Assignment status changes to `completed`
only after sync succeeds.

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
