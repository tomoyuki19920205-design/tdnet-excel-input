# Company News Phase2.2r Python/OpenSSL transport

`python-openssl-system-trust-v1` is an explicit validation-run transport. It
runs inside the current Python process and does not start the PowerShell
Schannel helper. The ordinary runner default remains `windows-schannel-v1`.

The transport creates its client context with
`ssl.create_default_context(ssl.Purpose.SERVER_AUTH)`, requires
`ssl.CERT_REQUIRED`, enables hostname verification, and sets TLS 1.2 as the
minimum. It uses the canonical hostname as SNI. There is no option to disable
certificate or hostname validation, no fallback transport, and no retry after
a transport failure.

The prepared-run manifest freezes `live_transport_profile`. `run-next` rejects
a conflicting CLI profile, so one run cannot switch transport after it starts.
Older prepared runs without the field remain bound to the historical Schannel
default. Registry identity evidence remains immutable; the selected profile is
recorded only as runtime body-fetch provenance.

Each wire attempt journals the declared and resolved URL, HTTP status, body
size and SHA-256, Python and OpenSSL versions, default CA paths, certificate
store statistics, certificate DER SHA-256, subject, issuer, SAN, validity,
negotiated TLS version, cipher and ALPN. Successful `wrap_socket` records chain
and hostname validation as PASS. DNS, TCP, TLS, certificate, hostname, timeout,
HTTP, response-size and redirect failures remain distinct.

Redirects are handled outside the wire operation. Only HTTPS redirects to the
exact same hostname are eligible, each destination is revalidated against the
public-address policy, loops fail closed, and the existing timeout, response
size and redirect-count limits apply.
