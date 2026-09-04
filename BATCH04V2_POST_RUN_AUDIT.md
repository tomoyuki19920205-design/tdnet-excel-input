# batch04v2 Post-run Gold Audit

## Executive Summary

- Completion: 192/192; failed=0
- Known Gold: 16/29 (55.17%); critical 6/7; IR/TDNET 11/12.
- Old batch04 17/29; rescues=1; regressions=2; net=-1
- Source depth: 4.7396 → 7.1927
- Runtime gates: {'p95_under_45min': True, 'max_under_55min': True, 'capacity_busy_skip_zero': True, 'overlap_zero': True}
- Wall clock 11.934 companies/hour; gaps=80.
- Novel candidates 167 raw / 165 unique; 18 blind extension packets.
- Precision: PROVISIONAL_PENDING_GOLD_EXTENSION. Novel candidates are NOT false positives.
- Matcher ambiguity: 0 node(s), 0 raw observations; excluded from unambiguous credit, not classified as novel/false positives.
- Targets pass: False; batch03 remains ON HOLD.

## Known Gold comparison

| Run | Material | Critical | Noncritical | Official | IR/TDNET | Company official | Government | External | Industry |
|---|---|---|---|---|---|---|---|---|---|
| reference01 | 25/29 | 7/7 | 18/22 | 18/21 | 12/12 | 4/5 | 2/4 | 2/2 | 1/1 |
| reference02 | 22/29 | 7/7 | 15/22 | 20/21 | 12/12 | 5/5 | 3/4 | 1/2 | 0/1 |
| batch02 | 19/29 | 7/7 | 12/22 | 18/21 | 12/12 | 5/5 | 1/4 | 0/2 | 0/1 |
| batch04 | 17/29 | 7/7 | 10/22 | 17/21 | 12/12 | 4/5 | 1/4 | 0/2 | 0/1 |
| batch04v2 | 16/29 | 6/7 | 10/22 | 16/21 | 11/12 | 4/5 | 1/4 | 0/2 | 0/1 |

## Rescue and regression

Stage counts: {'targeted_second_pass': 1}

### rescues

- 9793 ダイセキ 2026-08-31: アミタサーキュラー、ダイセキと資源循環領域で事業連携を開始 ―液体系廃棄物の再資源化で協定を締結― (volume, customer_supplier_official); MISSED → FOUND; stage=targeted_second_pass

### regressions

- 7760 ＩＭＶ 2026-08-31: 受託試験料金改定のお知らせ (pricing, company_news); FOUND → MISSED; stage=UNRESOLVED
- 5698 エンビプロ・ホールディングス 2026-08-31: 2026年６月期 決算説明会（書き起こし） (margin, company_ir); FOUND → MISSED; stage=UNRESOLVED

## Runtime and compression

| Run | Sources avg / median | Source ratio | Research sec avg / p50 / p95 / max | Time ratio | Task sec avg / p95 / max |
|---|---|---|---|---|---|
| reference01 | 7.21 / 7.00 | 1.0000 | 50.94 / 48.05 / 94.31 / 129.84 | 1.0000 | 105.14 / 169.99 / 202.33 |
| reference02 | 6.88 / 7.00 | 0.9538 | 52.29 / 50.88 / 79.08 / 85.62 | 1.0265 | 108.88 / 183.46 / 189.65 |
| batch02 | 5.64 / 5.00 | 0.7825 | 35.39 / 30.82 / 60.15 / 264.14 | 0.6947 | 149.97 / 219.30 / 367.65 |
| batch04 | 4.74 / 5.00 | 0.6575 | 23.31 / 21.05 / 41.91 / 75.85 | 0.4576 | 221.35 / 286.82 / 332.14 |
| batch04v2 | 7.19 / 7.00 | 0.9978 | 32.39 / 29.88 / 53.31 / 76.88 | 0.6358 | 464.62 / 569.88 / 704.65 |

Source depth and elapsed research are separate telemetry measurements, not proof of equivalent quality.
Task table uses productive runs only. Current all-run telemetry (including empty) and stored legacy audit may use different samples:

- batch04: productive avg=221.351; all-run avg=158.943; stored legacy={'average': 173.5829168064516, 'p50': 199.912669, 'p95': 281.121279, 'max': 332.138425}
- batch04v2: productive avg=464.624; all-run avg=455.268; stored legacy={'average': 455.26758797959184, 'p50': 469.670044, 'p95': 569.875649, 'max': 704.648846}

Task duration is well below the one-hour per-task recurrence; scheduler availability must be assessed separately.

## Scheduler coverage

- Expected opportunities: 129; {'PRODUCTIVE': 48, 'NO_ROUTE_OBSERVED': 80, 'OTHER': 1}
- Productive runs: 48 (expected 48); busy=0; overlap=0
- Wall-clock: 11.9340 companies/hour over 57918.534 seconds.
- Conditional scheduler-adjusted: 31.3469 companies/hour. Observed opportunities / 8 = cadence-equivalent hours; conditional estimate, not measured continuous capacity
- Theoretical cadence: 32 companies/hour; not measured availability.
- route_task does not persist attempt telemetry; absent snapshot/release is NO_ROUTE_OBSERVED, not proof of missed execution
- same task, preceding scheduled time within 60 min; inferred association, not scheduler execution proof
- Gap cause: UNDETERMINED; no sleep/app/network attribution
- Consecutive no-route intervals: [{'first_scheduled_at': '2026-09-03T22:30:00+09:00', 'count': 2, 'last_scheduled_at': '2026-09-03T22:38:00+09:00'}, {'first_scheduled_at': '2026-09-03T22:53:00+09:00', 'count': 77, 'last_scheduled_at': '2026-09-04T08:23:00+09:00'}, {'first_scheduled_at': '2026-09-04T09:23:00+09:00', 'count': 1, 'last_scheduled_at': '2026-09-04T09:23:00+09:00'}]

### All productive runs

| Task | Started | Finished | Companies | Seconds |
|---|---|---|---|---|
| task04 | 2026-09-03T17:25:32.217+09:00 | 2026-09-03T17:34:26.219+09:00 | 4 | 534.00 |
| task05 | 2026-09-03T17:31:09.481+09:00 | 2026-09-03T17:39:33.059+09:00 | 4 | 503.58 |
| task06 | 2026-09-03T17:39:37.350+09:00 | 2026-09-03T17:47:08.888+09:00 | 4 | 451.54 |
| task07 | 2026-09-03T17:47:14.784+09:00 | 2026-09-03T17:55:18.134+09:00 | 4 | 483.35 |
| task08 | 2026-09-03T17:55:34.557+09:00 | 2026-09-03T18:01:32.593+09:00 | 4 | 358.04 |
| task01 | 2026-09-03T18:01:23.230+09:00 | 2026-09-03T18:07:11.389+09:00 | 4 | 348.16 |
| task02 | 2026-09-03T18:10:53.964+09:00 | 2026-09-03T18:18:11.104+09:00 | 4 | 437.14 |
| task03 | 2026-09-03T18:17:09.382+09:00 | 2026-09-03T18:23:24.236+09:00 | 4 | 374.85 |
| task04 | 2026-09-03T18:24:08.935+09:00 | 2026-09-03T18:31:11.417+09:00 | 4 | 422.48 |
| task05 | 2026-09-03T18:32:07.939+09:00 | 2026-09-03T18:40:00.088+09:00 | 4 | 472.15 |
| task06 | 2026-09-03T18:39:11.039+09:00 | 2026-09-03T18:45:23.728+09:00 | 4 | 372.69 |
| task07 | 2026-09-03T18:46:15.956+09:00 | 2026-09-03T18:53:39.574+09:00 | 4 | 443.62 |
| task08 | 2026-09-03T18:55:37.549+09:00 | 2026-09-03T19:03:27.219+09:00 | 4 | 469.67 |
| task01 | 2026-09-03T19:02:10.706+09:00 | 2026-09-03T19:09:17.198+09:00 | 4 | 426.49 |
| task02 | 2026-09-03T19:10:10.115+09:00 | 2026-09-03T19:18:51.287+09:00 | 4 | 521.17 |
| task03 | 2026-09-03T19:16:05.225+09:00 | 2026-09-03T19:27:49.873+09:00 | 4 | 704.65 |
| task04 | 2026-09-03T19:25:16.248+09:00 | 2026-09-03T19:31:49.479+09:00 | 4 | 393.23 |
| task05 | 2026-09-03T19:31:18.643+09:00 | 2026-09-03T19:40:42.579+09:00 | 4 | 563.94 |
| task06 | 2026-09-03T19:39:42.457+09:00 | 2026-09-03T19:47:39.556+09:00 | 4 | 477.10 |
| task07 | 2026-09-03T19:47:10.212+09:00 | 2026-09-03T19:52:41.861+09:00 | 4 | 331.65 |
| task08 | 2026-09-03T19:55:41.427+09:00 | 2026-09-03T20:02:32.118+09:00 | 4 | 410.69 |
| task01 | 2026-09-03T20:00:41.019+09:00 | 2026-09-03T20:09:43.300+09:00 | 4 | 542.28 |
| task02 | 2026-09-03T20:10:47.341+09:00 | 2026-09-03T20:18:35.263+09:00 | 4 | 467.92 |
| task03 | 2026-09-03T20:16:37.760+09:00 | 2026-09-03T20:26:05.154+09:00 | 4 | 567.39 |
| task04 | 2026-09-03T20:24:14.169+09:00 | 2026-09-03T20:32:29.943+09:00 | 4 | 495.77 |
| task05 | 2026-09-03T20:32:11.412+09:00 | 2026-09-03T20:40:30.976+09:00 | 4 | 499.56 |
| task06 | 2026-09-03T20:39:42.149+09:00 | 2026-09-03T20:45:57.700+09:00 | 4 | 375.55 |
| task07 | 2026-09-03T20:46:35.896+09:00 | 2026-09-03T20:53:15.061+09:00 | 4 | 399.17 |
| task08 | 2026-09-03T20:54:10.867+09:00 | 2026-09-03T21:01:18.632+09:00 | 4 | 427.77 |
| task01 | 2026-09-03T21:01:50.966+09:00 | 2026-09-03T21:10:46.625+09:00 | 4 | 535.66 |
| task02 | 2026-09-03T21:10:42.424+09:00 | 2026-09-03T21:18:48.872+09:00 | 4 | 486.45 |
| task03 | 2026-09-03T21:17:41.340+09:00 | 2026-09-03T21:24:15.108+09:00 | 4 | 393.77 |
| task04 | 2026-09-03T21:25:08.839+09:00 | 2026-09-03T21:33:17.108+09:00 | 4 | 488.27 |
| task05 | 2026-09-03T21:32:36.300+09:00 | 2026-09-03T21:41:48.433+09:00 | 4 | 552.13 |
| task06 | 2026-09-03T21:40:22.484+09:00 | 2026-09-03T21:48:34.994+09:00 | 4 | 492.51 |
| task07 | 2026-09-03T21:47:15.626+09:00 | 2026-09-03T21:55:24.695+09:00 | 4 | 489.07 |
| task08 | 2026-09-03T21:55:14.739+09:00 | 2026-09-03T22:03:17.165+09:00 | 4 | 482.43 |
| task01 | 2026-09-03T22:01:50.878+09:00 | 2026-09-03T22:09:28.880+09:00 | 4 | 458.00 |
| task02 | 2026-09-03T22:09:13.174+09:00 | 2026-09-03T22:16:30.051+09:00 | 4 | 436.88 |
| task03 | 2026-09-03T22:16:44.967+09:00 | 2026-09-03T22:23:25.859+09:00 | 4 | 400.89 |
| task04 | 2026-09-03T22:23:51.648+09:00 | 2026-09-03T22:33:21.523+09:00 | 4 | 569.88 |
| task07 | 2026-09-03T22:46:20.320+09:00 | 2026-09-03T22:55:51.935+09:00 | 4 | 571.62 |
| task05 | 2026-09-04T08:32:27.552+09:00 | 2026-09-04T08:41:29.716+09:00 | 4 | 542.16 |
| task06 | 2026-09-04T08:39:52.505+09:00 | 2026-09-04T08:46:50.119+09:00 | 4 | 417.61 |
| task08 | 2026-09-04T08:54:58.121+09:00 | 2026-09-04T09:00:56.398+09:00 | 4 | 358.28 |
| task01 | 2026-09-04T09:01:55.042+09:00 | 2026-09-04T09:08:17.960+09:00 | 4 | 382.92 |
| task02 | 2026-09-04T09:10:54.713+09:00 | 2026-09-04T09:19:09.359+09:00 | 4 | 494.65 |
| task03 | 2026-09-04T09:17:02.709+09:00 | 2026-09-04T09:24:55.885+09:00 | 4 | 473.18 |

## Extension / precision

Gold82 drawn from reference48; novel extraction covers all192, so extension creates selection/coverage asymmetry for reference-only runs

Existing normalized Gold + extension VALID_MATERIAL; normalize duplicate chains across both without overwriting original. Score all five frozen outputs with matcher v2; report common48 and full192 separately, unresearched reference companies not as missed. Precision final only after all extension verdicts; no-news FN counts material tickers with no output events.

No Web adjudication has been started. Human approval is required. Existing Gold82 is immutable.

## Legacy audit

LEGACY_REFERENCE_METRIC; not final Gold quality

```json
{
  "available": true,
  "reference_event_count": 58,
  "candidate_event_count": 52,
  "matched_event_count": 22,
  "event_recall": 0.3793103448275862,
  "critical_event_count": 8,
  "critical_event_recall": 0.375,
  "precision": 0.4230769230769231,
  "no_news_false_negative_count": 4,
  "no_news_false_negative_rate": 0.13333333333333333,
  "no_news_false_negative_tickers": [
    "2882",
    "2669",
    "6445",
    "583A"
  ],
  "ambiguous_match_count": 0,
  "ambiguous_matches": []
}
```

## Safety

Web research=0; Production writes=0; Scheduled Task changes=0; raw SHA mutation=0. No batch03 or rerun.
All input SHA pairs and detailed matcher/scheduler evidence are in the adjacent JSON artifacts.

## Judgment

WARN_BATCH04V2_POST_RUN_REQUIRES_REVIEW
Known-Gold targets FAIL and critical regression requires review. Source recovery is promising only on depth, not validated quality. Proceed only to approved blind extension adjudication; do not rerun research or start batch03.

## Tests and operator commands

Verification: 117 passed in 10.10s (one outside-sandbox run). Initial sandbox attempt: 63 passed, 54 tmp_path fixture setup errors; no ACL workaround or test weakening.

From the repository root, human verification:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_company_news_post_run_v2.py tests/test_company_news_strategy_v2.py tests/test_company_news_quality_matcher_v2.py tests/test_company_news_gold_packets_v2.py tests/test_company_news_batch_benchmark.py -q
```

Offline audit and packet preparation only (neither performs Web research nor changes scheduler/runtime control):

```powershell
.\.venv\Scripts\python.exe tools/company_news_batch_benchmark.py audit-strategy-v2-run --benchmark-id batchbench-20260901T095759508419 --run-name batch04v2
.\.venv\Scripts\python.exe tools/company_news_batch_benchmark.py build-gold-extension-v2 --benchmark-id batchbench-20260901T095759508419 --run-name batch04v2
```

After separately approved human blind adjudication only, import a response using `import-gold-extension-v2 --benchmark-id batchbench-20260901T095759508419 --run-name batch04v2 --input <response.json>`. This importer stores extension-only response sidecars, checks canonical response schema, SHA, exact coverage, evidence and duplicate cycles, and never overwrites original Gold82. No response has been imported in this task. Final combined-truth scoring is specified in the extension manifest; no final extended score is claimed before adjudication.

Artifacts are under `data/news_work/batch_benchmark/batchbench-20260901T095759508419/quality_v2/matcher_v2/`:

- `post_run_batch04v2/batch04v2_gold_known_audit.json`
- `post_run_batch04v2/batch04v2_rescue_analysis.json`
- `post_run_batch04v2/batch04v2_scheduler_coverage.json` (all opportunities and 48 productive runs)
- `post_run_batch04v2/input_sha256.json` (before/after hashes)
- `gold_extension_batch04v2/gold_extension_manifest.json`
- `gold_extension_batch04v2/gold_extension_001.json` through `gold_extension_018.json`
- `gold_extension_batch04v2/ADJUDICATION_INSTRUCTIONS.md`

Only evaluator code, its CLI wiring, synthetic tests, and this report are committed. Runtime/analysis JSON and original research artifacts are not committed.
