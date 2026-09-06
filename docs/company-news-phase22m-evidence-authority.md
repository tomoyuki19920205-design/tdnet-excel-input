# Company News Phase2.2m evidence authority

Final synthesis events reference bounded EvidenceStore records through exact
`supporting_evidence_ids`. Python creates each stable ID from the canonical source
URL and rejects unknown, duplicate, empty, or ambiguous references. The source
title remains authoritative source metadata; the final `headline` remains a
presentation field and may be a faithful paraphrase.

Saved legacy artifacts without evidence IDs may use a fallback only when one
verified, non-index-only evidence card uniquely matches the canonical source URL.
The fallback does not compare the presentation headline with the source title.
Multiple matches, missing body evidence, or an unknown ID fail closed.

The runner now checkpoints aggregate company usage immediately after the final
model usage event and before deterministic validation. Exact failed usage is
stored as `MEASURED`; incomplete observed usage is `PARTIAL_USAGE`; started work
without recoverable usage is `UNKNOWN`; work that never entered research is
`NOT_MEASURED`. Token aggregation includes exact failed assignments while quality
statistics continue to count only valid completed companies.

Failed-run audits and saved-evidence replay write separate artifacts and verify
that the source run manifest and queue remain byte-for-byte unchanged. These
operations perform no research, HTTP, Web, or model calls.
