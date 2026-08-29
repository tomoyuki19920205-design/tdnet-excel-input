# Company News queue

`company_queue.jsonl` and `queue_state.json` are runtime files created by
`tools/company_news_queue.py`. They are intentionally absent from source control:
this directory does not contain an active production queue.

`init-pilot` creates an inert five-company fixture queue by default. It does not
replace `slot01/assignment.json` until a human explicitly passes `--activate`.
The deterministic pilot order comes from the latest ordinary-stock rows in the
existing company master, sorted by ticker; it does not use an investment ranking.

Queue entries use `pending`, `assigned`, `completed`, `failed`, or `paused` and
persist attempt count, last error, last successful check time, and next eligibility.
The queue state persists the current position and assignment sequence. Both files
are replaced atomically, and a persisted transition journal repairs an interrupted
assignment handoff after restart.
