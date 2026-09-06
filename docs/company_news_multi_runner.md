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

## Offline input bootstrap

`bootstrap-inputs --offline-only` builds the pre-run identity matrix without
HTTP, Web, search, or model calls.  Its authorities are limited to canonical
`CompanyIdentityRegistry` artifacts and the read-only
`company_ir_companies` security/company master.  Master URLs can identify a
known root and domain, but cannot promote an identity to `VERIFIED`; promotion
still requires a saved verified identity artifact backed by an actual official
body.  The command has no manual URL argument and never reads benchmark
answers, Gold data, cohort buckets, historical results, or old event URLs.

```powershell
..\.venv\Scripts\python.exe tools\company_news_multi_runner.py bootstrap-inputs `
  --cohort-file artifacts\token_efficiency_phase22\phase22-fixed-cohort.json `
  --cohort-sha256 fc38bd476c68a0e47d75c59d664eb4fc2387c5a5c2580beece5668f395ff909e `
  --approved-company-count 12 --max-companies 12 `
  --company-master-db data\company_ir_monitor.db `
  --repository-artifacts-root artifacts `
  --output-dir artifacts\token_efficiency_phase22\phase22-input-bootstrap `
  --offline-only
```

The bootstrap report is always written.  Frozen registry and plan files are
written only when all cohort identities are already verified offline.  An
incomplete matrix exits with status 2 and lists each missing requirement.
The result-neutral plan freezes ticker order, verified roots, allowed hosts,
transport order, and deterministic discovery/delegation policy.  It contains
no future document URLs or result-dependent endpoints.  `prepare` validates
the complete registry/plan pair before creating a manifest or queue.

With separate network authorization, the same operation accepts
`--windows-schannel`.  It deterministically constructs only
an exact HTTPS URL using this authority order: verified registry body, verified
IR Nightly body, IR Nightly's last successful company-controlled URL, company
master exact URL, then a derived `https://<official-domain>/` URL. Domain
ownership and exact fetch authority remain separate. A derived root is the
lowest authority and its failure does not establish that the company's official
source is globally blocked. Saved `www` hosts may be used; the runner never
constructs a `www` hostname. The selected URL permits one same-domain HTTPS redirect,
and promotes identity only when the normalized official body directly names
the assigned company.  If the root lacks that support, one additional URL may
be fetched only when its literal same-domain href contains a generic company,
corporate, about, or profile path marker.  The per-company request cap is two
actual journaled requests and the command hard-limits the cohort total to 24.
There is no retry, HTTP downgrade, guessed path, search, Web worker, or model
fallback.  Every request uses the company-local durable Schannel journal.

```powershell
..\.venv\Scripts\python.exe tools\company_news_multi_runner.py bootstrap-inputs `
  --cohort-file artifacts\token_efficiency_phase22\phase22-fixed-cohort.json `
  --cohort-sha256 fc38bd476c68a0e47d75c59d664eb4fc2387c5a5c2580beece5668f395ff909e `
  --approved-company-count 12 --max-companies 12 `
  --company-master-db data\company_ir_monitor.db `
  --repository-artifacts-root artifacts `
  --output-dir artifacts\token_efficiency_phase22\phase22-identity-bootstrap `
  --max-http-requests 24 --windows-schannel `
  --prepare-run-dir artifacts\token_efficiency_phase22\phase22-run-001
```

Formal registry/plan files and the `PREPARED` run are emitted only after all
cohort identities pass.  Any `UNVERIFIED` or `BLOCKED` company leaves the
research run unprepared and records the exact gap in the bootstrap report.

## Offline identity authority forensics

`forensic-identity-authority` audits the company master, successful IR Nightly
source rows, strict saved-body manifests, and a prior bootstrap report without
opening a transport. It writes a result-neutral resolution plan and classifies
each company as already verified, ready for one exact HTTPS GET, or lacking an
exact URL compatible with the verified-identity contract.

An optional reusable IR Nightly body has schema
`company_ir_nightly_verified_body_v1` and must include ticker, company name,
official domain, exact source URL, last-success timestamp, MIME type, a relative
body path, and its SHA-256. The body hash and direct company linkage are checked
through `RepositoryExistingTransport`; a configured URL or successful timestamp
alone never becomes `VERIFIED`.

```powershell
..\.venv\Scripts\python.exe tools\company_news_multi_runner.py forensic-identity-authority `
  --cohort-file artifacts\token_efficiency_phase22\phase22-fixed-cohort.json `
  --cohort-sha256 fc38bd476c68a0e47d75c59d664eb4fc2387c5a5c2580beece5668f395ff909e `
  --approved-company-count 12 --max-companies 12 `
  --company-master-db data\company_ir_monitor.db `
  --repository-artifacts-root artifacts `
  --phase22e-report artifacts\token_efficiency_phase22\phase22-identity-bootstrap\phase22-schannel-bootstrap-report.json `
  --output artifacts\token_efficiency_phase22\phase22-identity-resolution-plan.json
```

`bootstrap-known-successful` consumes that plan by exact byte SHA and performs
one logical identity fetch per frozen company through
`ir-nightly-known-successful-v1`. The transport uses the IR Nightly user agent
and `requests.Session` with certificate verification enabled, no environment
credentials, cookies, authentication, retry, or automatic redirect handling.
Every allowed same-company redirect hop is validated and journaled separately;
external, private, localhost, and link-local destinations fail closed. Final
bodies are size/MIME bounded and saved beside their durable request journals.

An HTTP exact URL may be used only when it was frozen from Nightly evidence. It
can produce a verified identity only by an observed same-domain redirect to an
HTTPS final URL. An HTTP 200 final body remains `UNVERIFIED_HTTP_ONLY`.
Formal registry/plan artifacts and a `PREPARED` runner are created only when the
whole frozen cohort is verified. The command never starts company research.

Identity matching uses the canonical Japanese name and explicitly supplied
authoritative names against bounded visible text and structured corporate
markers captured before layout removal: title, selected metadata, organization
JSON-LD, logo alt text, and footer text. Brand text alone is never promoted
unless that brand is already present in authoritative input.

An HTTP-only saved page may yield an HTTPS identity candidate only when the
saved HTML contains a literal same-domain HTTPS URL in canonical, Open Graph,
meta-refresh, base, or URL-bearing attributes. The resolver never creates the
candidate by changing the URL scheme.

CLI summaries are rendered as ASCII-escaped JSON so a successful durable write
cannot become a process failure because of a legacy Windows console encoding.
Artifact JSON remains UTF-8 with native characters.

```powershell
..\.venv\Scripts\python.exe tools\company_news_multi_runner.py bootstrap-known-successful `
  --cohort-file artifacts\token_efficiency_phase22\phase22-fixed-cohort.json `
  --cohort-sha256 fc38bd476c68a0e47d75c59d664eb4fc2387c5a5c2580beece5668f395ff909e `
  --approved-company-count 12 --max-companies 12 `
  --resolution-plan artifacts\token_efficiency_phase22\phase22-identity-resolution-plan.json `
  --resolution-plan-sha256 9ac3097e207c811bbf947901bf3ba77c6eb2cfe78f8765bad5057ca42c438b08 `
  --output-dir artifacts\token_efficiency_phase22\phase22g-identity-bootstrap `
  --prepare-run-dir artifacts\token_efficiency_phase22\phase22-run-001 `
  --max-logical-fetches 12
```

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
