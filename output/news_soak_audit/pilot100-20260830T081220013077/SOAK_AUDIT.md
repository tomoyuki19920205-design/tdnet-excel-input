# Company News Monitor 100-company soak audit

Queue: `pilot100-20260830T081220013077`

## Final result

- Started: 2026-08-30 08:29:21 JST
- Completed: 2026-08-30 12:42:20 JST
- Elapsed: 15,179 seconds
- Result: 100 completed / 0 pending / 0 active / 0 failed
- News present: 43; no news: 57
- Throughput: 23.716977 companies/hour (569.207458/day)
- 3,800-company projection: 160.223 hours, or 6.676 days

The throughput is computed from queue activation to queue completion. It therefore includes the initial wait until the next scheduled phase. The remaining seven-day buffer is about 7.8 hours and is not sufficient to absorb a long PC outage, rate limit, or network outage with high confidence.

## Stale payloads

All three stale files were byte-identical reappearances of already processed payloads. The first copies were ingested at 08:34:18-20. The same assignment files reappeared while the next assignments owned the slots and were safely quarantined at 08:37:18. No stale copy was canonically ingested a second time and no current assignment was completed or advanced by a stale copy.

| Task | Slot | Company / ticker | Stale assignment | Payload time | Duplicate detected | Current assignment at detection | Canonical result | Classification |
|---|---|---|---|---|---|---|---|---|
| task05 | slot13 | あさひ / 3333 | `slot13-20260830T081220013077-000013` | checked 08:32:33; file mtime 08:33:32 | 08:37:18 | `slot13-20260830T081220013077-000025` | one scan, zero events | A: expected/safely rejected duplicate replay |
| task05 | slot14 | 大成建設 / 1801 | `slot14-20260830T081220013077-000014` | checked 08:32:33; file mtime 08:33:32 | 08:37:18 | `slot14-20260830T081220013077-000026` | one scan, one event | A: expected/safely rejected duplicate replay |
| task05 | slot15 | サークレイス / 5029 | `slot15-20260830T081220013077-000015` | checked 08:32:33; file mtime 08:33:32 | 08:37:18 | `slot15-20260830T081220013077-000027` | one scan, one event | A: expected/safely rejected duplicate replay |

The payload schema has no `created_at` field, so the report preserves `checked_at` plus filesystem mtime instead of inventing a creation timestamp. Quarantine destinations are the same filenames under `data/news_inbox/quarantine/`, each with `Work payload has no matching current assignment`. The evidence cannot distinguish a producer re-save from a OneDrive replay, but the identical hashes and delayed reappearance establish duplicate delivery rather than new research output.

## Validation and retry incident

The single `validation_failure_count` was not a schema validation failure.

- Company: 玉井商船 (9127)
- Task / slot: task06 / slot18
- First assignment: `slot18-20260830T081220013077-000018`
- First output: valid `company_news_v1`, `items=[]`, four sources checked
- First lifecycle: validated -> ingested -> synced -> runtime state write failed
- Exact error: `[WinError 5] Access is denied`, replacing `data/news_work/state/slot18.json.tmp` with `slot18.json`
- Retry assignment: `slot18-20260830T081220013077-000028`
- Retry result: success, `items=[]`, three sources checked, completed 09:40:00 JST

The worker classified any exception whose durable state did not end at `ingested` or `synced` as a validation failure. Its exception cleanup rewrote the in-memory `completed` phase and then caused queue retry even though the canonical scan and sync had succeeded. This explains both `validation_failure_count=1` and `retry_count=1`; there was no invalid field, invalid enum, incomplete atomic payload, or failure sidecar.

The canonical database contains one scan row for each distinct assignment (`000018` and `000028`). There are no duplicate scan primary keys, duplicate event dedupe keys, or duplicate queue completions. The extra scan is a trace of the unnecessary retry, not an idempotency violation.

## Atomic-write root cause and hardening

The old implementation used a fixed sibling name such as `slot18.json.tmp`, `queue_state.json.tmp`, or `company_queue.jsonl.tmp`, wrote through `Path.write_text`, and immediately called `os.replace`. Handles were closed before replace and queue, slot, and worker writers have process locks, so an unclosed internal handle or concurrent Company News writer is not supported by the evidence. The same transient WinError 5 occurred against several unrelated runtime targets throughout the soak, which is consistent with a short external filesystem filter lock under the OneDrive-backed repository (OneDrive is likely, but antivirus/indexer cannot be excluded from the available logs).

Hardening:

- one shared same-directory atomic writer for queue, bridge, worker, and task snapshot state;
- unique `.<target>.<pid>.<uuid>.tmp` files;
- explicit flush and `fsync` before replace;
- Windows WinError 5/32 only: bounded 50/100/200/400/800 ms backoff (six total replace attempts, 1.55 seconds maximum sleep);
- non-transient and exhausted errors remain fail-closed;
- target is never deleted before replacement and is preserved on failure;
- completed assignment plus canonical run is never downgraded to failed solely because the final bridge-state write failed;
- only canonical/assignment payload contract errors increment `validation_failure_count`.

No runtime path was migrated. The hardened writer is an appropriate first response, but OneDrive remains a residual availability risk for high-frequency mutable state. Move runtime state to a local non-synced directory only if a follow-up soak still records replace retries/exhaustion; such a migration needs explicit operator approval and a documented recovery procedure.

## Scheduled-run utilization

The frozen operator snapshot reported 48 scheduled runs. Task-run records through task04 at 14:25 reconcile this as:

- Productive runs: 34
- Empty/no-ready runs: 14 (all after queue completion)
- Busy-skipped runs observed: 0
- Overlap detected: 0
- Unique company completions per productive run: 100 / 34 = 2.941176
- Work successes per productive run including the one retry: 101 / 34 = 2.970588
- Companies per all 48 runs: 100 / 48 = 2.083333

Task durations were under five minutes and same-task starts were about one hour apart. Later empty task records continued to arrive after the frozen 48-run snapshot and are intentionally excluded from these frozen utilization figures.

## Residual risks and release recommendation

- The seven-day capacity buffer is only about 7.8 hours.
- The available evidence identifies an external transient lock class but cannot prove which Windows filter process owned each lock.
- Busy exits are not persisted as dedicated run records, so `busy_skipped_runs=0` means no observed guard artifact, not a complete external scheduler audit.
- A full human-side regression run is still required.
- Run one additional unattended hardening soak before creating the 3,800-company production queue. It should demonstrate zero exhausted atomic retries, zero avoidable company retries, zero duplicate completion, and unchanged 8x3 snapshot behavior.
