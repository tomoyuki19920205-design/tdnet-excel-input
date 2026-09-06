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

## Durable HTTP telemetry

`DurableArtifactStore.prepare` runs before transport execution. It creates the primary artifact directory when possible, performs a write/flush/fsync/read-back/delete probe, and also establishes a runtime-selected local spool through Python's temporary-directory facility. The spool is authoritative during a run; writes to the primary directory are best-effort mirrors. If the primary fails after a response, the spool copy remains intact. If neither location passes the probe, the run stops with `ARTIFACT_STORAGE_UNAVAILABLE` before an HTTP callable can run.

`DirectHttpTransport(journal=...)` requires a durable `REQUEST_STARTED` record before every wire-level GET, including each same-domain redirect hop. `JournaledTransport` provides the same ordering for transports whose `fetch` call represents one request. `HttpFetchJournal` keeps one atomic JSON file per request and records `PLANNED`, `REQUEST_STARTED`, `RESPONSE_RECEIVED`, `NORMALIZED`, `FAILED`, and `OUTCOME_LOST` transitions. Every rewrite uses a temporary file, flush, `fsync`, and atomic replace. A response record is written immediately after transport control returns and before body normalization. It includes the final URL, status, a bounded safe-header subset, MIME type, byte count, SHA-256, access state, exception category, and elapsed time. Raw bodies are not stored in the journal or projected into model context.

Request counts are reconstructed from valid journal files containing `REQUEST_STARTED`; corrupt or partial JSON is reported separately and never counted as a formal result. A journal ending at `REQUEST_STARTED` is detected as an interrupted request. `OUTCOME_LOST` is a terminal state for a known attempt whose outcome was not persisted, and the failure cache suppresses an immediate retry for the same company, URL, and transport while preserving the existing different-URL and different-transport policy.

The Phase1.9g request to `https://www.sanden.co.jp/company/about/` is projected as `OUTCOME_LOST` with reason `TELEMETRY_PERSISTENCE_FAILURE`. This projection records the known attempt only. It does not infer an HTTP status, response size, hash, identity result, search record, or official-window result.

## TLS trust backends

The legacy `direct_http` transport uses Requests' verified managed-CA path (`verify=True`, currently the environment's certifi bundle). The recorded `windows-system-trust-v1` identifier is retained for artifact compatibility, but its precise semantic description is `windows-root-bundle-v1`: it enumerates X.509 certificates from the Windows `ROOT` store and loads those exported roots into an OpenSSL `PROTOCOL_TLS_CLIENT` context. It is not the Windows native chain engine.

`WindowsSchannelTransport` is the distinct native implementation. Its transport ID is `windows-schannel-v1` and its current implementation fingerprint is `dotnet10-sslstream-schannel-cryptoapi-v3`. Python sends a strict JSON request to `company_news_windows_schannel_fetch.ps1`; the helper resolves and rejects non-public destinations, connects once, and uses .NET `SslStream` with Windows Schannel/CryptoAPI. The certificate callback accepts only `SslPolicyErrors.None`. System trust, hostname verification, TLS 1.2+, HTTPS-only requests, GET-only operation, response limits, MIME checks, and same-domain redirect checks remain mandatory. Certificate downloads and revocation network checks are disabled so one resolver request cannot create hidden follow-up traffic.

The helper returns one machine-readable JSON result on stdout. It includes bounded safe headers, body bytes as base64, elapsed time, the transport fingerprint, and a certificate summary. Empty and no-body responses use an explicit zero-length byte array and an empty base64 string. Failures after an HTTP response preserve the acquired status, MIME type, safe headers, and TLS evidence. Stage-aware categories distinguish `DNS`, `TLS`, `HTTP`, `BODY_READ`, `BODY_ENCODING`, `EMPTY_BODY`, and `NORMALIZATION`; a post-response conversion failure is never rewritten as DNS. Python applies normal body normalization and exposes only bounded evidence cards to model-facing code. Raw bodies never enter model context.

`discover_official_dynamic_sources` is an offline-only parser for saved official HTML. It reports explicit same-domain script sources and explicit API, JSON, XML, CSV, or AJAX-like paths found in inline scripts and `data-*` attributes. It rejects cross-domain references and static text does not produce guessed endpoints. Calling code must separately authorize every fetch.

Schannel redirects may follow at most the caller's explicit hop limit. Every destination must remain HTTPS, inside the configured official domain, and pass the same public-address validation before a child request starts. Loops, downgrades, cross-domain locations, and redirects beyond the limit are blocked. The original redirect and child request have separate journal records; the child records its parent request, hop, source URL, Location value, and target URL.

When `retain_raw_html=True`, each size-accepted HTML/XHTML response is atomically saved under `raw-http/<request-id>.html` in both the authoritative spool and best-effort primary artifact tree before normalization. The journal records its relative path, byte count, and SHA-256. This option stores no headers, cookies, credentials, or request secrets, and raw HTML is never added to model context automatically. PDF and rejected oversized bodies are not retained.

Transport provenance is derived from the implementation. The legacy identities remain `direct_http` and `direct_http/windows-system-trust-v1`; the latter aliases `direct_http/windows-root-bundle-v1` for retry-cache decisions, so a semantic rename cannot bypass a recorded failure. `windows-schannel-v1` is a separate retry identity because it uses the native Schannel/CryptoAPI implementation. Earlier failure records remain intact.

Certificate failures are classified as `TLS_CA_CHAIN`, `TLS_HOSTNAME`, `TLS_EXPIRED`, `TLS_NOT_YET_VALID`, `TLS_PROTOCOL`, `TLS_HANDSHAKE`, or `TLS_UNKNOWN`. A system-store initialization or verification-policy failure is `TLS_TRUST_INIT` and occurs before the request callable. HTTP journaling, SSRF checks, same-domain redirect limits, MIME limits, response-size limits, and timeout handling are unchanged.
