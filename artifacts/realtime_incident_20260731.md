# Realtime extraction incident investigation — 2026-07-31 JST

## Final judgment

`PASS_REALTIME_EXTRACTION_ROOT_CAUSE_FIXED_PRODUCTION_PATH_VERIFIED`

The realtime monitoring defect is fixed and the Task Scheduler production path
completed a zero-item cycle. The wider related regression command has one
pre-existing XBRL unit assertion failure outside this incident's modified
files; it is recorded below as a non-blocking baseline issue.

## Evidence and confirmed facts

* Investigation time: 2026-07-31 10:22–10:27 JST.
* Task: `TDNET_Realtime`, enabled, every 10 minutes; action is
  `run_realtime.bat`, which explicitly changes to the repository directory.
* Incident run: scheduler run `102202_15456` started at 10:22:02 JST and
  finished in 74.8 seconds.  It fetched 78 official-source J-Quants records,
  filtered 7 in-scope records, and its legacy ingest returned 4 item errors.
* The same run's event/V2 paths completed the in-scope work: 3 earnings were
  saved, 2 dividend events were saved (1 Discord notification sent), and 1
  non-target disclosure was recorded.  Supabase event writes returned HTTP
  201.  Therefore no missing production disclosure was evidenced for this
  bounded set.
* Before the fix, `pipeline_run._run_ingest` reported success whenever the
  wrapper returned normally, ignoring `summary.errors`.  In addition,
  `scheduler_realtime.main` treated a nonzero child exit as a warning but
  still returned zero.  Task Scheduler consequently reported `Last Result: 0`
  for the incident run.
* Production-path validation: task manually launched through Task Scheduler at
  10:26:18 JST, completed at 10:27:08 JST (49.1 seconds), and reported zero
  new items with exit code 0. `state/locks` was empty and no pipeline process
  remained. The next scheduled run remained 10:32 JST.

## Repair

* `tools/pipeline_run.py`: converts `summary.errors > 0` to a failed ingest
  step and normalizes counts for `pipeline_runs` monitoring.
* `tools/scheduler_realtime.py`: retains downstream processing after a child
  failure but returns nonzero if any child exit is nonzero, so Task Scheduler
  and monitoring observe the failure.
* Regression coverage added for item-level ingest errors and scheduler
  propagation of nonzero child exits.

## Test evidence

* PASS: `python -m pytest tests/test_pipeline_run.py tests/test_scheduler_realtime_deadline.py -q`
  — 56 passed.
* PASS: `python -m py_compile tools/pipeline_run.py tools/scheduler_realtime.py`.
* PASS: `git diff --check` for the four modified files.
* Baseline issue: the wider related command (`test_pipeline_run`,
  `test_scheduler_realtime_deadline`, `test_file_lock`, `test_ingest_pipeline`)
  yielded 116 passed and 1 failed: `TestIxbrlNonFractionParse` expects
  `source_unit == "百万円"` but received `"円"`. This is unrelated to this
  incident and no code in the failing area was modified; it does not invalidate
  the passing regression coverage for the repaired realtime path.

## Checkpoint

* Log: `logs/realtime_20260731.log`
* SHA-256: `F52275876BEFA915AAFFDEF4CD2AD9E4E2415D9703C9B47A424409C769758B0E`
* Resume by resolving or formally baselining the XBRL unit assertion, then
  rerun the wider related suite and the next scheduled realtime cycle.
