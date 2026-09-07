# Company News Phase2.2p provenance reconciliation

Phase2.2p keeps company identity authority separate from event and context
evidence when both refer to the same retrieved document. URL comparison uses
the shared Company News canonicalizer for scheme and host casing, default
ports, trailing slashes, tracking parameters, and query ordering. A declared
URL and a same-domain redirect target are equivalent only when the durable
resolver journal records a successful response whose body hash agrees with
the frozen identity evidence.

The canonical payload still contains one provenance row per document, as
required by the unchanged canonical schema. The Formal
CompanyIdentityRegistry controls the identity URL and `company_official`
classification. Model evidence can supply event or context linkage, but it
cannot replace Registry identity fields. Every input record, role, authority
tier, evidence ID, differing field, body hash, transport assurance, and
official domain is retained in the separate
`provenance-reconciliation.json` audit artifact.

Reconciliation fails closed when body hashes, transport assurance, official
domain, or resolver-backed document equivalence conflict. It also rejects
cross-domain redirect equivalence. Unrelated third-party provenance remains a
third-party source and cannot become company identity evidence.

When multiple selected candidate rows describe one event and source, Python
keeps the unique candidate whose document URL equals the primary source URL.
Supporting candidates remain in telemetry with their linkage and selection
reason, but are not counted as additional final events. If there is no unique
primary-source candidate, validation fails closed.

The canonical schema is unchanged. Saved run-004 artifacts are replay inputs
only and are never rewritten. Offline replay performs no HTTP, Web, or model
calls.
