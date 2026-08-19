# Company IR nightly monitor

`tools/scheduler_nightly.py` invokes `tools/company_ir_nightly.py` once in the
existing nightly run. It is intentionally absent from the realtime scheduler.

## Source registry

`data/jquants.db.market_data_universe` is the single company/name master. The
discovery table stores only URL metadata; it is not a second company master.
`tools/company_ir_source_discovery.py` first reuses configured sources and the
official homepage printed in the latest company-submitted TDnet financial
statement. It then follows only IR-related links on the same official domain,
to depth 2 and at most 4 HTML pages per company.

The managed fields are `ticker`, `company_name`, `official_domain`,
`ir_top_url`, `ir_library_url`, `ir_event_url`, `discovery_status`, and
`last_validated_at`. Discovered pages are idempotently registered in
`company_ir_sources`; no 4,000-company URL list is hardcoded.

The small CSV remains an optional bootstrap/override and is always reused:

```csv
ticker,company_name,source_url
4022,ラサ工業,https://www.rasa.co.jp/ir/event/presentation.html
```

Nightly resumes 250 unfinished companies per run. A source returning 404 is
rediscovered from its stored official/IR top URL on a later maintenance run.
The monitor step is skipped while any company remains `pending` or
`official_only`, so partial discovery can never start the all-company baseline.

## Safe initialization

There are two independent safeguards. The first successful fetch of each source records every matching asset with
`is_baseline=1` and emits no Viewer event. A 404 or network failure does not
complete the baseline. Only assets first observed after that successful
baseline can create an event.

In addition, `company_ir_monitor_state.notifications_enabled` defaults to `0`.
While it is off, even an already-baselined page treats newly seen assets as
baseline and cannot publish. Do not enable it until discovery and the full
all-company baseline report are complete.

Run a no-write crawl with:

```powershell
.\.venv\Scripts\python.exe tools\company_ir_nightly.py --dry-run
.\.venv\Scripts\python.exe tools\company_ir_nightly.py --baseline-only
```

The normal run stores metadata only. HTML snapshots, PDF bodies, and video
bodies are never written. A newly discovered PDF may be streamed once to
calculate SHA-256, then discarded.

Page GETs use a bounded pool of 8 workers (`COMPANY_IR_WORKERS`, capped at 16).
All SQLite and notification decisions remain on the main thread.

## Recovery

Viewer publication failures remain `notified=0` and are retried on the next
nightly run with the same stable event identity. TDnet matches are retained with
`suppression_reason='tdnet_duplicate'` for auditability and are never published.
