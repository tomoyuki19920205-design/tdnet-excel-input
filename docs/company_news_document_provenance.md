# Company News document provenance

Document discovery and document-body verification are separate states.

- `DISCOVERED_IN_INDEX`: a saved index contains the title or date, but no exact document URL or body proof.
- `URL_DISCOVERED`: saved official evidence contains the exact literal URL.
- `BODY_FETCH_ATTEMPTED`: an exact-URL request started, without a verified body yet.
- `BODY_VERIFIED`: the actual document body was fetched and retained with content evidence.
- `ACCESS_FAILED`: a fetch attempt terminated without a verified body.

An `official_index` Evidence Card cannot establish `BODY_VERIFIED`. An in-scope material document in any state other than `BODY_VERIFIED` produces a `document_access` coverage gap, keeps coverage `PARTIAL`, and disables the no-news gate. Exact URLs may be derived only from literal saved official HTML links or attributes; title-to-slug guessing is rejected.

Schannel failures persist the failure stage, outer exception type, and bounded inner-exception chain. Certificate and hostname evidence takes precedence over generic handshake classification.
