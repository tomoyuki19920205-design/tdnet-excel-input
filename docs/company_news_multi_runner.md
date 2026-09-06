# Company News frozen-cohort runner

`tools/company_news_multi_runner.py` is the experimental, production-isolated
controller for a previously frozen Company News cohort.  The historical
four-company Phase1 fixture remains in `company_news_lightweight.py`; it is no
longer the authority for generic multi-company runs.

## Cohort and lifecycle safety

`prepare` validates the logical cohort SHA, byte SHA, contiguous positions,
strictly increasing frozen-universe positions, unique four-character security
codes, approved count, and explicit maximum.  The manifest freezes both hashes
and the ordered ticker list.  Every acquisition reloads the source artifact and
fails before execution if its bytes, logical SHA, count, or order changed.

Run states are `PREPARED`, `RUNNING`, `STOPPED`, `COMPLETED`, and `FAILED`.
Company states are `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, and
`SAFE_UNKNOWN`.  Queue, manifest, guard, telemetry, and audit documents use
atomic replacement.  A stopped run cannot acquire another company until the
separate `approve-resume` command records explicit approval.  There is no
automatic retry, repair, reconciliation, or resume.

Each runtime assignment contains only assignment id, ticker, company name,
window, strategy version, and frozen position.  Bucket, historical result,
selection rationale, old events, Gold URLs, and historical artifacts never
enter the company runtime.  A runtime-input audit records the canonical input
hash and scans exact prior-company identity markers.  The canonical
`company_news_lightweight.run_company` function constructs a new EvidenceStore
and SearchRecordRegistry for every call, while every model stage remains an
ephemeral process.

## Official-source preflight

`ConfiguredOfficialProvider` runs before model execution.  It loads a stable
`CompanyIdentityRegistry`, tries `RepositoryExistingTransport`, and uses
`OfficialSourceResolver` with `windows-schannel-v1` only when `--allow-live-http`
is explicitly present.  Schannel uses a company-local durable HTTP journal and
raw-body spool.  Missing verified identity or incomplete required official
families produces `SAFE_UNKNOWN` with zero model calls.

The official plan is frozen metadata, never search output:

```json
{
  "family_urls": {
    "5237": {
      "news": "https://official.example/news/",
      "ir": "https://official.example/ir/",
      "announcements": "https://official.example/notices/"
    }
  },
  "repository_artifacts": {
    "https://official.example/news/": {
      "path": "C:/saved/news.html",
      "mime_type": "text/html",
      "status": 200
    }
  }
}
```

URLs must come from stable identity metadata or a verified official parent.
The integration exposes an exact delegated-resource path with redirect count
zero.  It also classifies each discovered document through the shared TDNET and
IR Nightly scope authority before body retrieval.  Covered documents use zero
body requests; in-scope exact URLs use the same non-LLM resolver, whose HTML and
PDF normalizers retain the distinct `URL_DISCOVERED`, `BODY_VERIFIED`, and
`ACCESS_FAILED` outcomes.  Index discovery alone never becomes verified body
evidence.

## Execution gates

After research, Python injects assignment identity and checked time, derives
provenance/search/coverage/materiality state, applies IR Nightly and TDNET
scope, validates canonical/Strategy v4/Strategy v5/payload contracts, and runs
the independent exhaustive semantic audit.  A semantic failure, artifact
collision, guard leak, missing exact telemetry, or more than 500,000 tokens
stops acquisition before the next company.  More than 400,000 tokens is only a
warning.  Correctly fail-closed official access produces `SAFE_UNKNOWN` and may
continue.

The runner writes only beneath its supplied run directory.  It contains no
Production inbox, database, Supabase, Viewer, scheduler, or routing path.

## Commands

Prepare the approved twelve-company run without research:

```powershell
..\.venv\Scripts\python.exe tools\company_news_multi_runner.py prepare `
  --cohort-file artifacts\token_efficiency_phase22\phase22-fixed-cohort.json `
  --run-dir artifacts\token_efficiency_phase22\phase22-run-001 `
  --cohort-sha256 fc38bd476c68a0e47d75c59d664eb4fc2387c5a5c2580beece5668f395ff909e `
  --approved-company-count 12 --max-companies 12 `
  --search-from 2026-08-25 --search-to 2026-08-31 `
  --identity-registry artifacts\token_efficiency_phase22\identity-registry.json `
  --official-plan artifacts\token_efficiency_phase22\official-plan.json
```

The prepare manifest also freezes byte hashes for the identity registry and
official plan; changing either blocks acquisition.  `run-next` acquires at most
one company.  Omitting `--allow-live-http` makes the
official phase repository-only.  `aggregate` is read-only apart from stdout.
After a gate stop, `approve-resume` must be invoked under a separate explicit
authorization before another `run-next` can acquire work.
