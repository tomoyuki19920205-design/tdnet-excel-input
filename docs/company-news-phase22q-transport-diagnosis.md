# Company News Phase2.2q transport diagnosis

## Scope and preserved runs

The analysis treats `phase22-run-005` as an immutable completed source run.
Its manifest SHA-256 is
`667dee946eb635eb0a6051b0d42093bd64abefad229d4c5105032e7e03818a6e`
and its queue SHA-256 is
`35821a31ef82bc1c0cd6dcc477bd3a7cc54ad30333797d34ff845ca23404d655`.
No retry, repair, resume, or source-run write is part of Phase2.2q.

## Root cause

The run-004 and run-005 5237 attempts used the same HTTPS GET URL, Schannel
transport ID, helper implementation fingerprint, SNI, TLS 1.2/1.3 policy,
hostname verification, Windows system trust and no proxy. Run-004 received
HTTP 200 and body SHA-256
`3d17b0290ba1665a01d6ab330a5c6c731a426ae6682ac632bb46fc2d7ab67e57`.
Run-005 reached `SslStream.AuthenticateAsClient` after TCP connection but
failed before any peer certificate was exposed. Its deepest saved exception
was `System.ComponentModel.Win32Exception` with the Windows message meaning
that no credentials were available in the security package. This corresponds
to the local Schannel `SEC_E_NO_CREDENTIALS` signature, rather than a remote
certificate-chain failure.

The one separately authorized probe used the same exact URL and transport from
the unrestricted local execution boundary. It received HTTP 200, the same
12,847 bytes and the same body hash as run-004. This rules out URL, DNS, remote
body and remote certificate changes as the cause. The differing factor was the
restricted parent execution context inherited by the run-005 helper process.

The run-005 8836 result is independent. Its saved IR authority URLs are HTTP.
The HTTPS-only Schannel policy rejected them before wire I/O, so its
`ACCESS_DENIED` is a local URL-policy refusal, not HTTP 403, process denial or
TLS failure.

## Durable transport evidence

Schannel helper v5 records the helper executable and script path/hash, argv,
parent process IDs, execution user, working directory, hashed proxy-variable
presence, timeout, helper exit code, DNS/TCP/TLS/body cumulative timings, SNI,
enabled and negotiated TLS protocol, certificate/hostname validation policy,
revocation policy and the complete HRESULT/native-error chain. Proxy values are
never persisted and the raw TCP transport does not consume proxy variables.

DNS and TCP are separate stages. Certificate-chain and hostname failures remain
domain-certificate failures. HTTP 403 remains remote HTTP. Timeout remains
ambiguous. Only evidence-backed local signatures are eligible for a systemic
circuit breaker.

## Run-level circuit breaker

`SYSTEMIC_TRANSPORT_FAILURE_DOMAIN_THRESHOLD` is 3. Each company saves a
`transport-infra-observation.json`. A run stops with the current assignment in
`INFRA_STOP` when the same local/systemic signature occurs consecutively on
three distinct domains with no successful transport fetch. The manifest stores
the threshold, signature, domains and trigger time. Certificate, timeout,
ordinary DNS, HTTP and isolated SAFE_UNKNOWN results do not trigger it.

Aggregation now reports queue/fail-closed behavior, publication safety, token
measurement, live E2E status and live provenance reconciliation separately. A
terminal run with zero successful transport fetches, zero research samples and
only SAFE_UNKNOWN/INFRA_STOP outcomes has `overall_evaluation=BLOCKED`.

## Frozen SHA audit

File SHA uses `sha256(path.read_bytes())`. Logical SHA uses UTF-8 canonical JSON
with sorted keys and separators `(',', ':')` through
`company_news_multi_runner._canonical_sha`. The cohort logical digest covers
the ordered fields `position`, `source_universe_position`, `security_code`,
`company_name` and `bucket`, as implemented by `FrozenCohort.load`. Registry
logical SHA covers `CompanyIdentityRegistry.to_dict()`. Official Plan logical
SHA covers the parsed complete plan object.

| Artifact | File SHA-256 | Logical SHA-256 |
|---|---|---|
| Frozen Cohort | `30a9ce96c874f1490544a352474fbcb52ab1bef3890fe208d54256fcded0c8f1` | `fc38bd476c68a0e47d75c59d664eb4fc2387c5a5c2580beece5668f395ff909e` |
| CompanyIdentityRegistry | `9ea8d58f7dac3519eb2476b78e86ad38ae9ab5b35b969ea4922e97d3ec795aab` | `407b711232b832abd2485684ae3a2fad3ca0aadf5f0bdfad16aafbe42d541b0b` |
| Official Plan | `0c2be199328268a8ebc51a739b186b2023076827caac49bb56c16cc0831b32e7` | `c127f165b1c2b73932f503f0e3e8028eef4349087638310bc22ff654fa2fadf1` |

The earlier logical values match exactly. There is no underlying artifact
change and no notation-only discrepancy in these six full digest values.
