# Common48 Scope Adjudication — preparation only

Strategy v4 Human regression was reported as 80 passed in 7.90s. This change does not repeat that run, prepare/activate a new research run, alter the research strategy or change original materiality/criticality.

## Artifacts and blindness

The selected benchmark's `quality_v2/matcher_v2/scope_common48/blind/` is the only directory to give an adjudicator. It contains four five-event packets, a manifest, response JSON Schema and adjudication instructions. Only Common48 VALID_MATERIAL events currently AMBIGUOUS are selected (20 expected). Any ambiguous old-critical event outside that set is placed in a separate supplemental packet, never duplicated.

Original cluster IDs may encode storage origin. Public IDs therefore use uniform opaque `scope-event-<digest>` identifiers. `private/control.json` holds the original mapping, critical-coverage checks, frozen source hashes and manifest seal. Do not expose that file, preparation report or upstream audits to the adjudicator. Public packets use a strict field allowlist and text leakage checks; company URLs ending in `/about/strategy` are legitimate source context, not an experiment name.

Dates retain original date precision. Missing category/importance/relevance values remain null rather than invented. Web provenance and prior materiality explanations are allowed; discovery run, source artifact path, experiment name, origin labels and performance information are not. No new Web research occurs in packet generation.

Packet content SHA excludes its own packet_sha256 field, uses UTF-8 canonical JSON (sorted keys, compact separators, non-ASCII preserved). Manifest additionally seals exact file bytes with file_sha256, so even a formatting-only edit is rejected. The private control also seals the manifest, rubric/schema and upstream scope/normalized-truth audit files.

## Response and import

Response schema: `company_news_scope_adjudication_response_v1`. Required top-level fields are schema_version, benchmark_id, packet_id, packet_sha256, adjudicated_at and items. Each item has event_id, scope_label, confidence, rationale, tdnet_redundancy, incremental_information and supporting URL evidence. Exact event coverage, enums, timezone timestamps and semantic TDNET/incremental consistency are checked. AMBIGUOUS is valid when evidence remains insufficient.

Once an actual independently adjudicated response exists, use:

```powershell
.\.venv\Scripts\python.exe tools/company_news_scope_adjudication_v1.py import-scope-adjudication-v1 --benchmark-id batchbench-20260901T095759508419 --input "<response JSON path>"
```

This command is implemented but not executed during preparation. It verifies frozen input/manifest/packet hashes before writing. Full validation precedes the exclusive import lock; validation repeats under the lock. One atomic `private/state.json` contains all imported scope responses. No original Gold, extension, existing scope projection or production record is modified. An identical response is idempotent; replacing an imported response with a different one requires a separately designed, explicitly reviewed revision workflow. Locks close/unlink in finally. Crash leftovers fail closed; no PID signaling or automatic stale-lock deletion.

Read-only verification command:

```powershell
.\.venv\Scripts\python.exe tools/company_news_scope_adjudication_v1.py verify-scope-packets-v1 --benchmark-id batchbench-20260901T095759508419
```

## Scorer skeleton

`score_imported_common48` verifies every stored response and rejects incomplete packet coverage before using the pure scorer. There is no scoring CLI or preparation-time metric call. Only normalized Common48 VALID_MATERIAL plus IN_SCOPE_* enters `non_tdnet_material_recall_v1`. OUT_OF_SCOPE_TDNET, OUT_OF_SCOPE_TRIVIAL and AMBIGUOUS are excluded. Remaining ambiguity is reported, not hidden; zero denominator yields null recall. Old-critical in-scope count/recall are reference metrics, never an automatic production hard gate. Found IDs must come from a separately approved matching audit.

## Human tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_company_news_scope_adjudication_v1.py -q
```

One Codex invocation: 22 passed, 2 setup errors (Windows Temp/pytest tmp_path access denied). No retries/workarounds. The two filesystem import/tamper tests require Human execution. The final read-only scorer entry was added after that invocation and received static review, not another pytest run. Existing v4 Human 80-pass evidence is preserved separately and is not a result for these new tests.

No new scope decisions, response state, formal metrics, research runs or production changes are created by this preparation. Runtime packets and reports are not committed; only the reusable module, rubric, tests and this guide are committed.
