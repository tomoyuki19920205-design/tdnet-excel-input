# Company IR nightly monitor

`tools/scheduler_nightly.py` invokes `tools/company_ir_nightly.py` once in the
existing nightly run. It is intentionally absent from the realtime scheduler.

## Source registry

Add one row per IR library/event page to `config/company_ir_sources.csv`:

```csv
ticker,company_name,source_url
4022,ラサ工業,https://www.rasa.co.jp/ir/event/presentation.html
```

The CSV is idempotently imported into `data/company_ir_monitor.db`. Do not put
thousands of URLs in Python code; build and review this data registry instead.

## Safe initialization

The first successful fetch of each source records every matching asset with
`is_baseline=1` and emits no Viewer event. A 404 or network failure does not
complete the baseline. Only assets first observed after that successful
baseline can create an event.

Run a no-write crawl with:

```powershell
.\.venv\Scripts\python.exe tools\company_ir_nightly.py --dry-run
```

The normal run stores metadata only. HTML snapshots, PDF bodies, and video
bodies are never written. A newly discovered PDF may be streamed once to
calculate SHA-256, then discarded.

## Recovery

Viewer publication failures remain `notified=0` and are retried on the next
nightly run with the same stable event identity. TDnet matches are retained with
`suppression_reason='tdnet_duplicate'` for auditability and are never published.
