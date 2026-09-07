# Company News Phase2.2o frozen identity authority

Company identity is supplied by the frozen `CompanyIdentityRegistry`. Final
synthesis may not choose, alter, or infer the identity URL. The transport schema
keeps the canonical field for compatibility and allows `null`; Python replaces
it with the registry ticker, canonical company identity URL, evidence SHA,
official domain, transport assurance, and registry provenance before canonical
validation. Third-party cards remain available as event evidence but never gain
official identity authority.

Official runtime discovery may use an active `company_ir_sources` row as
`IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH` location authority only when the row has a
successful fetch timestamp, `verified_from_official=1`, and a same-domain exact
URL. The canonical IR Nightly table supplies the `ir` family classification;
URL text is not used to guess a family. Reuse does not assert current body
verification or current-window completeness. Those states still require the
new run's controlled fetch.

The frozen cohort, identity registry, official plan, order, and canonical
schema remain unchanged. Failed run-003 is never resumed or rewritten; offline
replays write separate Phase2.2o audit artifacts.
