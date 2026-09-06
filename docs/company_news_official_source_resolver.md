# Company News official source resolver

`tools/company_news_official_source.py` separates stable company identity from window-specific official coverage.

`CompanyIdentityRegistry` stores the security code, company name, normalized name, official domain, verified official URL, verification source and method, verification time, evidence hash, status, and a bounded evidence excerpt. `VERIFIED` requires an actual official body or an already verified repository artifact. Search snippets remain discovery evidence and cannot create a verified registry entry.

A verified registry entry can be projected into canonical provenance without another identity Web fetch or identity LLM worker. It becomes `STALE` when the security code, company name, or official domain changes, after a merger or reorganization signal, or after an explicit re-verification request. No unsupported time-to-live is hardcoded.

`OfficialWindowCoverage` is independent and uses `COMPLETE`, `PARTIAL`, `BLOCKED`, or `NOT_APPLICABLE`. A verified identity does not make a blocked window complete. `PARTIAL` and `BLOCKED` cannot support `no_news`.

`OfficialSourceResolver` accepts an explicit transport. Available adapters are `DirectHttpTransport`, which uses the repository's existing `requests` dependency, and `RepositoryExistingTransport`, which reads supplied saved bytes without network access. No browser-like or paid transport is claimed.

The direct transport is GET-only and unauthenticated. It clears cookies, disables environment credentials, restricts hosts to the configured official domain, rejects localhost and non-public IP addresses, limits redirects, blocks cross-domain redirects by default, enforces timeouts and response-size limits, and accepts only HTML, XHTML, PDF, or plain text. Failures are classified as HTTP 4xx/5xx, timeout, DNS, TLS, blocked redirect, rejected MIME, size limit, access denied, or unknown.

HTML and PDF bodies are normalized in Python. Navigation and scripts are removed, same-domain links and date candidates are bounded, and only a short evidence card is exposed to model context. Raw bodies and full normalized text are never included automatically.

`IdentityFailureCache` records company, domain, URL, transport, failure category, attempt time, retry count, and retry permission. The default hard cap suppresses a repeated failed URL after one attempt. Callers may supply a different explicit policy; the resolver does not invent a time-based backoff.

The offline 6444 projection remains `CompanyIdentity=UNVERIFIED` and `OfficialWindowCoverage=BLOCKED` because the saved IR index attempt returned HTTP 502 and the alternate corporate profile timed out. Existing snippets remain discovery-only, so `no_news` cannot be concluded.
