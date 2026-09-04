# Company News Monitor Strategy v4 (benchmark/R&D)

## Formal purpose

Company News Monitor complements daily TDNET monitoring with non-TDNET information that can affect company earnings, valuation or risk. Repeating ordinary timely disclosures is not its quality objective. This R&D contract does not switch any production prompt, schedule, worker, schema or Viewer behavior.

## Independent scope axis

`news_monitor_scope_v1` is separate from immutable Gold verdicts. Scope labels are:

- IN_SCOPE_COMPANY_WEB: company website-exclusive, economically relevant information.
- IN_SCOPE_EXTERNAL_MEDIA: material incremental media reporting.
- IN_SCOPE_GOVERNMENT: government/regulatory information with company linkage or strong transmission.
- IN_SCOPE_COUNTERPARTY: customer/supplier/partner/subsidiary/competitor/association evidence with a concrete company impact.
- IN_SCOPE_INCREMENTAL_IR: material conference/Q&A/transcript information absent from the TDNET disclosure.
- OUT_OF_SCOPE_TDNET: same-content TDNET disclosures without added information (earnings, guidance, dividends, equity actions, TOB, gains on asset sales, personnel and similar disclosures).
- OUT_OF_SCOPE_TRIVIAL: clearly minor index-supported notices and existing adjudicated nonmaterial information.
- AMBIGUOUS: unresolved exclusivity, incremental content or scope; review explicitly, never disguise as a negative finding.

Gold Material and scope are orthogonal. Normalized VALID_MATERIAL roots with confirmed IN_SCOPE form the new recall target. OUT_OF_SCOPE misses are not quality failures. Critical uses frozen Known-Gold labels only, then the independent scope filter; do not invent criticality for Extension. Publish AMBIGUOUS counts and lower/upper denominator bounds. Do not claim a final recall score while the required scope review remains incomplete.

The conservative local scope projection uses persisted adjudication and same-event source evidence only. A company_ir/company_news source label does not establish non-TDNET exclusivity; a transcript title does not establish incremental content. These remain AMBIGUOUS until evidence review. This projection is a review artifact, not a new investment/materiality judgment or an instruction to rerun Web research.

## Runtime states

Official-first discovery, focused external media and government searches, event dedupe, source provenance and no-news discipline remain mandatory. Navigation is not a search action. Official3/total6 remain planning targets, not automatic failure thresholds. Explain overruns and unresolved gaps; do not silently truncate or expand the budget without purpose.

Only in-scope and unresolved potentially material documents require substantive body follow. Confirmed TDNET-only and clearly trivial PDFs may be skipped using index/identity evidence. Potentially material titles cannot use the trivial shortcut. Alias queries require ticker plus normalized identity or official evidence linking the alias to the company; wrong-company research fails closed.

- COMPLETE: relevant scan/channel work complete; verified selected events have body provenance; no unresolved gaps.
- PARTIAL: inaccessible in-scope body, unresolved scope or incomplete relevant scan, with explicit review/retry entries. Save verified results. Even an empty items array is `result_type=partial`, never confirmed no-news. No automatic retry is authorized.
- failure: genuine inability to conduct research, absent mandatory search channel, wrong company, ownership/protocol failure or invalid/unwritable output.

Canonical `company_news_v1` and ingest/sync contracts are unchanged. Sidecar fields and partial state live only in the benchmark harness. Runtime inputs never load Gold, scope review outcomes or prior results.

## Preparation and evaluation gate

The existing harness supports `prepare-run --batch-size 4 --run-name batch04v4 --strategy company_news_research_strategy_v4`, fixed cohort/order/window, frozen run-local prompt, immutable four-company snapshots and benchmark-only atomic save. Preparation is not activation. Neither command is executed as part of this change.

Legacy unfiltered audit refuses v4 before writes. A reviewed scope denominator and partial-aware post-run scoring are required before v4 evaluation; the full new scorer is a subsequent step, not silently substituted with the old Critical7/IR-TDNET targets. No same-cohort outcome authorizes production.

## Verification and handoff

Human command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_company_news_strategy_v4.py tests/test_company_news_strategy_v3.py tests/test_company_news_batch_benchmark.py -q
```

One Codex pytest invocation before final review: 39 passed, 3 errors during existing v3 tmp_path setup (Windows Temp access denied). No retry/workaround. Final review added prepare/scope/legacy-audit regressions and strengthened provenance/coverage checks; these final additions require Human execution. Static parse/diff and read-only audit integrity checks are separate from pytest.

Runtime audit outputs are under the selected benchmark's `quality_v2/matcher_v2/strategy_v4_scope_review/`, not committed. V3, original Gold/Extension and production remain untouched. Scope ambiguity and Human tests must be reviewed before activation.

Prompt structure follows the OpenAI Docs recommendation to make instructions, context and output expectations explicit, while the actual financial-news scope is the user's contract: [Prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering).
