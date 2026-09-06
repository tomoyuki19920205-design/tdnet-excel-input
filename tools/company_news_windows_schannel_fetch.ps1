param()

$ErrorActionPreference = 'Stop'

function Write-Result([hashtable]$Value) {
    [Console]::Out.Write(($Value | ConvertTo-Json -Depth 12 -Compress))
}

function Test-PublicAddress([Net.IPAddress]$Address) {
    if ([Net.IPAddress]::IsLoopback($Address) -or $Address.IsIPv6LinkLocal -or $Address.IsIPv6Multicast) { return $false }
    if ($Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) {
        $bytes = $Address.GetAddressBytes()
        return -not (($bytes[0] -band 0xFE) -eq 0xFC -or ($bytes | Where-Object { $_ -ne 0 }).Count -eq 0)
    }
    $b = $Address.GetAddressBytes()
    if ($b[0] -in @(0, 10, 127, 224, 240)) { return $false }
    if ($b[0] -eq 192 -and $b[1] -eq 0 -and $b[2] -in @(0, 2)) { return $false }
    if ($b[0] -eq 169 -and $b[1] -eq 254) { return $false }
    if ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) { return $false }
    if ($b[0] -eq 192 -and $b[1] -eq 168) { return $false }
    if ($b[0] -eq 100 -and $b[1] -ge 64 -and $b[1] -le 127) { return $false }
    if ($b[0] -eq 198 -and (($b[1] -in @(18, 19)) -or ($b[1] -eq 51 -and $b[2] -eq 100))) { return $false }
    if ($b[0] -eq 203 -and $b[1] -eq 0 -and $b[2] -eq 113) { return $false }
    return $true
}

function Find-HeaderEnd([byte[]]$Bytes) {
    for ($i = 0; $i -le $Bytes.Length - 4; $i++) {
        if ($Bytes[$i] -eq 13 -and $Bytes[$i + 1] -eq 10 -and $Bytes[$i + 2] -eq 13 -and $Bytes[$i + 3] -eq 10) { return $i }
    }
    return -1
}

function Decode-Chunked([byte[]]$Bytes, [int]$MaxBytes) {
    $position = 0
    $output = [IO.MemoryStream]::new()
    try {
        while ($true) {
            $lineEnd = -1
            for ($i = $position; $i -lt $Bytes.Length - 1; $i++) {
                if ($Bytes[$i] -eq 13 -and $Bytes[$i + 1] -eq 10) { $lineEnd = $i; break }
            }
            if ($lineEnd -lt 0) { throw 'invalid chunk framing' }
            $line = [Text.Encoding]::ASCII.GetString($Bytes, $position, $lineEnd - $position).Split(';', 2)[0].Trim()
            $size = [Convert]::ToInt32($line, 16)
            $position = $lineEnd + 2
            if ($size -eq 0) { break }
            if ($size -lt 0 -or $position + $size + 2 -gt $Bytes.Length) { throw 'invalid chunk length' }
            if ($output.Length + $size -gt $MaxBytes) { throw 'SIZE_LIMIT' }
            $output.Write($Bytes, $position, $size)
            $position += $size
            if ($Bytes[$position] -ne 13 -or $Bytes[$position + 1] -ne 10) { throw 'invalid chunk terminator' }
            $position += 2
        }
        return $output.ToArray()
    } finally { $output.Dispose() }
}

$started = [Diagnostics.Stopwatch]::StartNew()
$request = $null
$tcp = $null
$ssl = $null
$certificateSummary = [ordered]@{ ssl_policy_errors = @(); hostname_validation = 'UNKNOWN'; leaf = $null; chain_elements = @(); chain_status = @() }
try {
    $request = ([Console]::In.ReadToEnd() | ConvertFrom-Json)
    $uri = [Uri][string]$request.url
    if ($uri.Scheme -ne 'https' -or $uri.Port -ne 443 -or $uri.UserInfo) { throw 'ACCESS_DENIED: HTTPS port 443 URL required' }
    $allowed = ([string]$request.allowed_host).Trim('.').ToLowerInvariant()
    $hostName = $uri.DnsSafeHost.Trim('.').ToLowerInvariant()
    if (-not ($hostName -eq $allowed -or $hostName.EndsWith('.' + $allowed))) { throw 'ACCESS_DENIED: host outside official domain' }
    $timeoutMs = [Math]::Max(1, [int]([double]$request.timeout_seconds * 1000))
    $maxBytes = [int]$request.max_response_bytes
    if ($maxBytes -lt 1) { throw 'ACCESS_DENIED: invalid response limit' }
    $addresses = [Net.Dns]::GetHostAddresses($hostName)
    $address = $addresses | Where-Object { Test-PublicAddress $_ } | Select-Object -First 1
    if ($null -eq $address) { throw 'ACCESS_DENIED: no public destination address' }

    $tcp = [Net.Sockets.TcpClient]::new($address.AddressFamily)
    $connectTimeout = [Threading.CancellationTokenSource]::new($timeoutMs)
    try { $tcp.ConnectAsync($address, 443, $connectTimeout.Token).GetAwaiter().GetResult() } finally { $connectTimeout.Dispose() }
    $tcp.ReceiveTimeout = $timeoutMs
    $tcp.SendTimeout = $timeoutMs

    $callback = [Net.Security.RemoteCertificateValidationCallback]{
        param($sender, $certificate, $chain, $errors)
        $certificateSummary.ssl_policy_errors = @($errors.ToString().Split(',') | ForEach-Object { $_.Trim() })
        $certificateSummary.hostname_validation = if (($errors -band [Net.Security.SslPolicyErrors]::RemoteCertificateNameMismatch) -eq 0) { 'PASS' } else { 'FAIL' }
        if ($null -ne $certificate) {
            $leaf = [Security.Cryptography.X509Certificates.X509Certificate2]::new($certificate)
            $san = $leaf.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.17' } | Select-Object -First 1
            $certificateSummary.leaf = [ordered]@{
                subject = $leaf.Subject; issuer = $leaf.Issuer
                san = if ($null -ne $san) { $san.Format($false) } else { $null }
                sha256 = $leaf.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256)
                not_before = $leaf.NotBefore.ToUniversalTime().ToString('o')
                not_after = $leaf.NotAfter.ToUniversalTime().ToString('o')
            }
        }
        if ($null -ne $chain) {
            $certificateSummary.chain_elements = @($chain.ChainElements | ForEach-Object { [ordered]@{
                subject = $_.Certificate.Subject; issuer = $_.Certificate.Issuer
                sha256 = $_.Certificate.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256)
            } })
            $certificateSummary.chain_status = @($chain.ChainStatus | ForEach-Object { [ordered]@{ status = $_.Status.ToString(); information = $_.StatusInformation.Trim() } })
        }
        return $errors -eq [Net.Security.SslPolicyErrors]::None
    }
    $ssl = [Net.Security.SslStream]::new($tcp.GetStream(), $false, $callback)
    $ssl.ReadTimeout = $timeoutMs
    $ssl.WriteTimeout = $timeoutMs
    $policy = [Security.Cryptography.X509Certificates.X509ChainPolicy]::new()
    $policy.TrustMode = [Security.Cryptography.X509Certificates.X509ChainTrustMode]::System
    $policy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
    $policy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
    $policy.DisableCertificateDownloads = $true
    $options = [Net.Security.SslClientAuthenticationOptions]::new()
    $options.TargetHost = $hostName
    $options.EnabledSslProtocols = [Security.Authentication.SslProtocols]::Tls12 -bor [Security.Authentication.SslProtocols]::Tls13
    $options.CertificateChainPolicy = $policy
    $ssl.AuthenticateAsClient($options)

    $pathAndQuery = if ($uri.PathAndQuery) { $uri.PathAndQuery } else { '/' }
    $wire = "GET $pathAndQuery HTTP/1.1`r`nHost: $hostName`r`nUser-Agent: CompanyNewsOfficialResolverSchannel/1.0`r`nAccept: text/html,application/xhtml+xml,application/pdf,text/plain`r`nConnection: close`r`n`r`n"
    $wireBytes = [Text.Encoding]::ASCII.GetBytes($wire)
    $ssl.Write($wireBytes, 0, $wireBytes.Length)
    $ssl.Flush()

    $raw = [IO.MemoryStream]::new()
    try {
        $buffer = [byte[]]::new(65536)
        while ($true) {
            $count = $ssl.Read($buffer, 0, $buffer.Length)
            if ($count -le 0) { break }
            $raw.Write($buffer, 0, $count)
            if ($raw.Length -gt $maxBytes + 65536) { throw 'SIZE_LIMIT' }
        }
        $rawBytes = $raw.ToArray()
    } finally { $raw.Dispose() }
    $headerEnd = Find-HeaderEnd $rawBytes
    if ($headerEnd -lt 0 -or $headerEnd -gt 65536) { throw 'HTTP_PROTOCOL: invalid response headers' }
    $headerText = [Text.Encoding]::ASCII.GetString($rawBytes, 0, $headerEnd)
    $lines = $headerText -split "`r`n"
    if ($lines[0] -notmatch '^HTTP/\d(?:\.\d)?\s+(\d{3})') { throw 'HTTP_PROTOCOL: invalid status line' }
    $statusCode = [int]$Matches[1]
    $headers = @{}
    foreach ($line in $lines | Select-Object -Skip 1) {
        $colon = $line.IndexOf(':')
        if ($colon -gt 0) { $headers[$line.Substring(0, $colon).Trim().ToLowerInvariant()] = $line.Substring($colon + 1).Trim() }
    }
    $bodyStart = $headerEnd + 4
    $encodedBody = if ($bodyStart -lt $rawBytes.Length) { $rawBytes[$bodyStart..($rawBytes.Length - 1)] } else { [byte[]]@() }
    $body = if (($headers['transfer-encoding'] -as [string]) -match '(?i)chunked') { Decode-Chunked $encodedBody $maxBytes } else { [byte[]]$encodedBody }
    if ($body.Length -gt $maxBytes) { throw 'SIZE_LIMIT' }
    $mime = if ($headers.ContainsKey('content-type')) { ([string]$headers['content-type']).Split(';', 2)[0].Trim().ToLowerInvariant() } else { $null }
    $safeHeaders = @{}
    foreach ($key in @('content-type','content-length','location','etag','last-modified')) { if ($headers.ContainsKey($key)) { $safeHeaders[$key] = ([string]$headers[$key]).Substring(0, [Math]::Min(1000, ([string]$headers[$key]).Length)) } }
    $started.Stop()
    Write-Result @{
        schema_version = 'company_news_schannel_helper_v1'; result = 'RESPONSE'; transport_id = 'windows-schannel-v1'
        implementation_fingerprint = 'dotnet10-sslstream-schannel-cryptoapi-v1'; requested_url = $uri.AbsoluteUri
        final_url = $uri.AbsoluteUri; status_code = $statusCode; mime_type = $mime
        response_bytes = $body.Length; body_base64 = [Convert]::ToBase64String($body)
        safe_headers = $safeHeaders; certificate_summary = $certificateSummary; elapsed_seconds = $started.Elapsed.TotalSeconds
    }
} catch {
    $started.Stop()
    $message = $_.Exception.Message
    $failure = if ($message -match '^SIZE_LIMIT') { 'SIZE_LIMIT' } elseif ($message -match '^ACCESS_DENIED') { 'ACCESS_DENIED' } elseif ($message -match 'timed out|canceled') { 'TIMEOUT' } elseif ($certificateSummary.hostname_validation -eq 'FAIL') { 'TLS_HOSTNAME' } elseif ($certificateSummary.chain_status.Count -gt 0) { 'TLS_CA_CHAIN' } elseif ($message -match 'authentication|certificate|SSL|TLS') { 'TLS_UNKNOWN' } else { 'DNS' }
    Write-Result @{
        schema_version = 'company_news_schannel_helper_v1'; result = 'FAILURE'; transport_id = 'windows-schannel-v1'
        implementation_fingerprint = 'dotnet10-sslstream-schannel-cryptoapi-v1'
        requested_url = if ($null -ne $request) { [string]$request.url } else { $null }; final_url = $null
        status_code = 0; mime_type = $null; response_bytes = $null; body_base64 = $null; safe_headers = @{}
        certificate_summary = $certificateSummary; elapsed_seconds = $started.Elapsed.TotalSeconds
        failure_class = $failure; error_detail = $message.Substring(0, [Math]::Min(2000, $message.Length))
    }
} finally {
    if ($null -ne $ssl) { $ssl.Dispose() }
    if ($null -ne $tcp) { $tcp.Dispose() }
}
