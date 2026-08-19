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

Nightly resumes about 250 unfinished companies per run. A source returning 404
is rediscovered from its stored official/IR top URL on a later maintenance run.
For the one-time initial load, `tools/company_ir_bootstrap.py` continuously runs
250-company batches with a 30-second inter-batch delay. The per-company status
is the durable checkpoint, so the command is safe to stop and rerun.

Every successfully discovered source is baselined immediately; discovery of
the other companies does not block its monitor. First-pass completion means
every ticker has one mutually-exclusive terminal status, not that every ticker
was successfully discovered. Terminal failures remain eligible for Nightly
retry.

## Safe initialization

There are two independent safeguards. The first successful fetch of each source records every matching asset with
`is_baseline=1` and emits no Viewer event. A 404 or network failure does not
complete the baseline. Only assets first observed after that successful
baseline can create an event.

In addition, `company_ir_monitor_state.notifications_enabled` defaults to `0`.
After a source baseline, a newly seen asset is stored as `pending` while this
gate is off; it is never downgraded into the baseline. Once the gate is opened,
durable pending rows publish exactly once and become `notified`. Do not enable
the gate until discovery, the all-company baseline, and the second dry-run are
complete.

External IR hosts are never crawled. A directly linked external IR library or
event page can be registered only from successfully fetched official-domain
HTML, with `provenance_url`, `is_external=1`, and
`verified_from_official=1` retained for audit.

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
