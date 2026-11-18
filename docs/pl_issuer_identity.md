# PL issuer integrity

`src.common_ticker.normalize_ticker` removes only the fifth trailing market zero.
Numeric `27600` is `2760`; alphanumeric `276A0` is `276A`. The historical alpha
mapping is forensic reference data and must never be used in ingestion.

J-Quants actuals and forecasts now compare the row's issuer with native `Code`
and `LocalCode` before latest-row selection and field-wise correction merging.
An explicit mismatch or malformed native payload is excluded from the export.
The original SQLite row remains intact; `[ISSUER_QUARANTINE]` logs its source
code, disclosure identifier and payload hash. Actual-import statistics include
quarantined evidence and the count of legacy rows without native identifiers.

The shared assessor also accepts verified EDINET/corporate identifiers from a
master supplied by a caller. Such a master must be valid for the document date;
do not apply today's identity or fiscal period to historical mergers or years.
Name and fiscal-end differences are review signals only. Missing identifiers
are explicitly unverified, not guessed from another security's code or name.
The current database master lacks EDINET identifiers for this audited cohort,
so the J-Quants integration uses native securities identifiers rather than
pretending those optional master checks have been deployed.

Historical cleanup is a separate, explicitly scoped operation. Freeze primary
keys and evidence; classify insufficient evidence as unresolved; archive full
rows and hashes; require exact snapshot and destination checks; preserve
unaffected rows with full-row fingerprints. Batch or transaction structure must
follow the user's current authorization, without repeated approval requests.
Use existing archive/run tables; do not infer an issuer from fiscal month alone.
Generated-always IDs require `OVERRIDING SYSTEM VALUE` for restoration. Abort
restoration if a newer primary/natural key conflicts, instead of overwriting it.

Validation covers all 145 retired mapping pairs, the 276A regression, conflicting
native identifiers, malformed input, sparse corrections, forecasting, short
fiscal years and fiscal-year transitions. No paid AI service or browser is used.
Production deployment of this additional guard remains a separate approval;
the already-fixed normalizer is currently active in the production runtime.
