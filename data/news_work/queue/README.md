# Company News queue

`company_queue.jsonl` and `queue_state.json` are runtime files created by
`tools/company_news_queue.py`. They are intentionally absent from source control:
this directory does not contain an active production queue.

`init-pilot` creates an inert five-company, one-slot fixture queue by default for
backward compatibility. `--slots 5 --count 15` creates the inert parallel pilot.
It does not replace any slot assignment until a human explicitly activates it.
The deterministic pilot order comes from the latest ordinary-stock rows in the
existing company master, sorted by ticker; it does not use an investment ranking.

Queue entries use `pending`, `assigned`, `completed`, `failed`, or `paused` and
persist `assigned_slot`, assignment ID, attempt count, last error, last successful
check time, and next eligibility. The queue state persists configured slots, the
global assignment sequence, and one transition journal entry per slot. Files are
replaced atomically, and the journal repairs interrupted assignment handoffs after
restart without assigning one company to multiple slots.
