# Strategy v4: v2 baseline specialized for NON_TDNET complement

This implementation uses the v2 Company Web + economic-driver search flow, not v3 document completeness. `contract.baseline_strategy_version` is `company_news_research_strategy_v2`; prepare selects the persisted `batch04v2` assignment order. The v3 module/prompt/run remain frozen. V4 does not call or import the v3 validator.

Company Web uses 1–2 searches. From metadata/profile evidence derive 2–3 economic exposures, then explicitly review counterparties and search industry/media/statistics, followed by up to two relevant regulatory themes and optional incremental IR. External queries need not mention the target company. The generic query_plan function builds this plan without company/ticker/source-URL tables. Selected events require in-window verified material scope and a concrete economic transmission; same industry alone is insufficient.

Exclude routine TDNET results/guidance/issuance/capital policy/fixed-asset gains and simple mirrors. Incremental IR needs a specific additional management/Q&A/transcript insight beyond the ordinary release. Trivial notices are excluded unless concrete material economic relevance justifies inclusion.

No-news requires Company Web plus two checked exposure-appropriate families among external_media/counterparty/government. Industry and general media are one family. Counterparty review is explicit; NOT_APPLICABLE does not count as checked. Identity remains verified against the assigned ticker, but query strings do not require an exact name/ticker. Channel evidence replaces strict per-query/body provenance.

Query targets: Company Web 1–2, external/counterparty 2–4, government 0–2, incremental IR 0–1; typical total 3–6. Overruns are telemetry only. No all-index enumeration or all-PDF reading is required. Only inaccessible in-scope material sources produce PARTIAL. Trivial/index/budget gaps do not. PARTIAL with empty items is not confirmed no-news; the existing isolated finalizer saves result_type=partial. Known material cannot silently disappear without a candidate decision. No automatic retries.

Primary metric: non_tdnet_material_recall_v1, fixed Common48 denominator 27. Minimum 16/27, preferred 18/27. Minimum scope counts: Company Web 6/7, External Media 4/8, Government 2/4, Counterparty 3/7, Incremental IR 1/1; preferred external 5/8, government 3/4, counterparty 4/7. Old Critical7 is a reference metric only, not a hard gate. Same-cohort success is diagnostic, never Production authorization.

Measure actual Company Web/external stage times, total company research time, source count, task elapsed p50/p95/max, busy/capacity skips, stale, overlap and conditional throughput. These are future-run measurements: prepare performs no research and cannot establish achieved recall or throughput.

Preparation is authorized only for batch04v4, batch size 4, 192 companies, window 2026-08-25–2026-08-31, order SHA 3191f7fd27034417eefd44b2f536699ef700cd3eb6d6b80c7cd014ae817d992e. Output is isolated under data/news_benchmark_inbox. Tasks01–08 remain PAUSED. No activation, no Production worker/queue/DB/Supabase/Viewer operations.

The offline leakage scanner checks runtime code + generic prompt for cohort identity literals, identity branches, all hardcoded source URLs and evaluation artifact references. Legitimate assignment identities are separate inputs. It does not ingest miss lists, event titles or event URLs. Aggregate scope deficits alone informed the flow.

## Human test command

Run from the repository in PowerShell. Use a fresh short basetemp directory to avoid Windows path-length failures:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/test_company_news_strategy_v4.py tests/test_company_news_strategy_v3.py tests/test_company_news_strategy_v2.py tests/test_company_news_scope_adjudication_v1.py tests/test_company_news_batch_benchmark.py -q --tb=short -p no:cacheprovider --basetemp="$env:TEMP/cnv4-human"
```

Do not use a basetemp containing unrelated files: pytest manages that directory. Synthetic tests cover scope, no-news, advisory budgets, source gaps, query planning, leakage, partial finalization and inert isolated preparation. Preparation is distinct from activation; inspect the actual run manifest and handoff audit before any later explicit activation request.
