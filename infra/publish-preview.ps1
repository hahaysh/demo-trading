[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PackageDirectory,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-f0-9]{40}$')]
    [string]$ExpectedCommit,
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'

function Get-StreamDigest([System.IO.Stream]$Stream) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return 'sha256:' + [Convert]::ToHexString($sha.ComputeHash($Stream)).ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Read-PreviewPackage([string]$Directory, [string]$Commit) {
    $folder = (Resolve-Path -LiteralPath $Directory).Path
    $ancestor = $folder
    while ($ancestor) {
        if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw 'Reparse point in package path.'
        }
        $ancestor = Split-Path $ancestor -Parent
    }
    $files = @(Get-ChildItem -LiteralPath $folder -Force)
    if ($files.Count -ne 2) { throw 'Package must contain only preview.zip and manifest.json.' }
    foreach ($file in $files) {
        if ($file.PSIsContainer -or $file.Name -cnotin @('preview.zip', 'manifest.json') -or
            ($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { throw 'Package allowlist violation.' }
    }
    $manifestPath = Join-Path $folder 'manifest.json'
    $zipPath = Join-Path $folder 'preview.zip'
    if ((Get-Item $manifestPath).Length -gt 65536 -or (Get-Item $zipPath).Length -gt 4MB) {
        throw 'Package size limit exceeded.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1 -or $manifest.sourceCommit -cne $Commit -or
        $manifest.mode -cne 'SYNTHETIC_OFFLINE_ONLY' -or
        $manifest.quoteEvidenceIncluded -isnot [bool] -or $manifest.quoteEvidenceIncluded -or
        @($manifest.files).Count -ne 1 -or $manifest.files[0] -cne 'index.html') {
        throw 'Manifest scope or commit mismatch.'
    }
    $digest = 'sha256:' + (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($manifest.zipDigest -cne $digest) { throw 'ZIP digest mismatch.' }
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        if ($archive.Entries.Count -ne 1 -or $archive.Entries[0].FullName -cne 'index.html' -or
            $archive.Entries[0].Length -gt 4MB -or $archive.Entries[0].Length -eq 0) {
            throw 'ZIP entry allowlist or size violation.'
        }
        $stream = $archive.Entries[0].Open()
        try { $htmlDigest = Get-StreamDigest $stream } finally { $stream.Dispose() }
        if ($manifest.htmlDigest -cne $htmlDigest) { throw 'HTML digest mismatch.' }
        $stream = $archive.Entries[0].Open()
        $reader = [System.IO.BinaryReader]::new($stream)
        try { $bytes = $reader.ReadBytes(4MB + 1) } finally { $reader.Dispose(); $stream.Dispose() }
        if ($bytes.Length -gt 4MB) { throw 'Expanded HTML too large.' }
        $html = [System.Text.UTF8Encoding]::new($false, $true).GetString($bytes)
    } finally { $archive.Dispose() }
    $match = [regex]::Match($html, '<script id="report-data" type="application/json">(.*?)</script>', [System.Text.RegularExpressions.RegexOptions]::Singleline)
    if (-not $match.Success) { throw 'Embedded report missing.' }
    $embedded = $match.Groups[1].Value | ConvertFrom-Json
    if ($null -ne $embedded.quote_evidence -or $embedded.report.mode -cne 'SYNTHETIC_OFFLINE_ONLY' -or
        $embedded.report.promoted -isnot [bool] -or $embedded.report.promoted -or
        $embedded.report.deployment_ready -isnot [bool] -or $embedded.report.deployment_ready -or
        $embedded.report_digest -cne $manifest.reportDigest -or
        -not $html.Contains('<html lang="ko">') -or -not $html.Contains("connect-src 'none'") -or
        $html.Contains('__ATS_REPORT_DATA__')) { throw 'Synthetic-only HTML boundary failed.' }
    return [pscustomobject]@{ ZipPath = $zipPath; Manifest = $manifest }
}

function Assert-PreviewConfiguration($Site, $Auth, $Config, [bool]$ScmBasic, [bool]$FtpBasic) {
    $aad = $Auth.identityProviders.azureActiveDirectory
    $principals = $aad.validation.defaultAuthorizationPolicy.allowedPrincipals
    if ($Site.properties.state -ne 'Running' -or $Site.properties.publicNetworkAccess -ne 'Enabled' -or
        $Site.location -notin @('Korea Central', 'koreacentral') -or
        $Site.properties.serverFarmId -ine '/subscriptions/199bfb5d-fa2d-4765-88cf-016129ccb7de/resourceGroups/atsview-rg/providers/Microsoft.Web/serverfarms/asp-atsview-dev-ac2a' -or
        -not $Site.properties.httpsOnly -or -not $Auth.httpSettings.requireHttps -or
        -not $Auth.platform.enabled -or -not $Auth.globalValidation.requireAuthentication -or
        $Auth.globalValidation.unauthenticatedClientAction -ne 'RedirectToLoginPage' -or
        @($Auth.globalValidation.excludedPaths | Where-Object { $_ }).Count -ne 0 -or
        -not $aad.enabled -or $aad.registration.clientId -ne 'f9b51430-79ef-4686-9e39-affb1a844935' -or
        $aad.registration.openIdIssuer -ne 'https://login.microsoftonline.com/93b885b6-4629-4327-bacf-a2e5175a8ede/v2.0' -or
        @($aad.validation.allowedAudiences).Count -ne 1 -or
        $aad.validation.allowedAudiences[0] -ne 'f9b51430-79ef-4686-9e39-affb1a844935' -or
        @($principals.identities).Count -ne 1 -or $principals.identities[0] -ne 'adb9b0df-9bb9-4822-b938-e6b88343b5eb' -or
        @($principals['groups'] | Where-Object { $_ }).Count -ne 0 -or
        $Auth.login.tokenStore.enabled -or $ScmBasic -or $FtpBasic -or
        $Config.ftpsState -ne 'Disabled' -or $Config.minTlsVersion -notin @('1.2', '1.3') -or
        $Config.scmMinTlsVersion -notin @('1.2', '1.3') -or $Config.alwaysOn -or -not $Config.use32BitWorkerProcess) {
        throw 'Hosted preview security configuration differs from the approved boundary.'
    }
    foreach ($provider in $Auth.identityProviders.Keys) {
        $settings = $Auth.identityProviders[$provider]
        if ($provider -eq 'customOpenIdConnectProviders' -and $settings -and $settings.Count -ne 0) {
            throw 'Unexpected custom identity provider.'
        }
        if ($provider -ne 'azureActiveDirectory' -and $settings -and
            $settings.ContainsKey('enabled') -and $settings.enabled -and $settings['registration'] -and
            @($settings['registration'].Values | Where-Object { $_ }).Count -ne 0) {
            throw 'Unexpected enabled identity provider.'
        }
    }
}

$package = Read-PreviewPackage -Directory $PackageDirectory -Commit $ExpectedCommit
$package.Manifest | ConvertTo-Json -Depth 3
if (-not $Execute) { return }
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:GITHUB_REPOSITORY -ne 'hahaysh/demo-trading' -or
    $env:GITHUB_REF -ne 'refs/heads/main' -or $env:GITHUB_EVENT_NAME -notin @('push', 'workflow_dispatch') -or
    $env:GITHUB_SHA -cne $ExpectedCommit) { throw 'Publishing is restricted to the approved main-branch workflow.' }

$subscription = '199bfb5d-fa2d-4765-88cf-016129ccb7de'
$base = "https://management.azure.com/subscriptions/$subscription/resourceGroups/atsview-rg/providers/Microsoft.Web/sites/app-atsview-dev-ac2a"
$root = 'https://app-atsview-dev-ac2a.azurewebsites.net'
$scmRoot = 'https://app-atsview-dev-ac2a.scm.azurewebsites.net'
$token = $null
$headers = $null
$client = $null
$stage = 'authentication'

try {
    $account = az account show --subscription $subscription --query '{id:id,tenantId:tenantId}' -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $account.id -ne $subscription -or $account.tenantId -ne '93b885b6-4629-4327-bacf-a2e5175a8ede') { throw 'Azure target mismatch.' }
    $token = az account get-access-token --subscription $subscription --resource https://management.azure.com/ --query accessToken -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Azure token unavailable.' }
    $headers = @{ Authorization = "Bearer $token" }
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $handler.UseCookies = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(30)
    foreach ($phase in @('before', 'after')) {
        $stage = "$phase-policy-check"
        $site = Invoke-RestMethod -Uri "${base}?api-version=2026-08-01" -Headers $headers -TimeoutSec 30 | ConvertTo-Json -Depth 50 | ConvertFrom-Json -AsHashtable
        $auth = (Invoke-RestMethod -Uri "$base/config/authsettingsV2?api-version=2024-11-01" -Headers $headers -TimeoutSec 30 | ConvertTo-Json -Depth 50 | ConvertFrom-Json -AsHashtable).properties
        $config = (Invoke-RestMethod -Uri "$base/config/web?api-version=2024-11-01" -Headers $headers -TimeoutSec 30 | ConvertTo-Json -Depth 50 | ConvertFrom-Json -AsHashtable).properties
        $scmBasic = (Invoke-RestMethod -Uri "$base/basicPublishingCredentialsPolicies/scm?api-version=2024-11-01" -Headers $headers -TimeoutSec 30).properties.allow
        $ftpBasic = (Invoke-RestMethod -Uri "$base/basicPublishingCredentialsPolicies/ftp?api-version=2024-11-01" -Headers $headers -TimeoutSec 30).properties.allow
        Assert-PreviewConfiguration $site $auth $config $scmBasic $ftpBasic
        if ($phase -eq 'before') {
            $stage = 'upload'
            $null = Read-PreviewPackage -Directory $PackageDirectory -Commit $ExpectedCommit
            az webapp deploy --subscription $subscription --resource-group atsview-rg --name app-atsview-dev-ac2a --type zip --src-path $package.ZipPath --only-show-errors --query '{id:id,status:status,complete:complete}' -o json
            if ($LASTEXITCODE -ne 0) { throw 'Azure ZIP deployment failed.' }
        }
    }
    $stage = 'remote-hash'
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, "$scmRoot/api/vfs/site/wwwroot/index.html")
    $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $token)
    $response = $client.SendAsync($request).GetAwaiter().GetResult()
    try {
        if ([int]$response.StatusCode -ne 200) { throw 'Entra-authenticated file verification failed.' }
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        try { $remoteDigest = Get-StreamDigest $stream } finally { $stream.Dispose() }
        if ($remoteDigest -cne $package.Manifest.htmlDigest) { throw 'Published HTML digest mismatch.' }
    } finally { $response.Dispose(); $request.Dispose() }
    $stage = 'anonymous-denial'
    foreach ($path in @('/', '/index.html')) {
        $response = $client.GetAsync("$root$path").GetAwaiter().GetResult()
        try {
            $status = [int]$response.StatusCode
            if ($status -notin @(401, 302) -or $response.Headers.Contains('X-Ms-Forbidden-Ip')) { throw 'Anonymous authentication gate not confirmed.' }
            if ($status -eq 302) {
                $target = [Uri]::new([Uri]$root, $response.Headers.Location)
                if ($target.Scheme -ne 'https' -or $target.Host -notin @('app-atsview-dev-ac2a.azurewebsites.net', 'login.microsoftonline.com')) { throw 'Unexpected authentication redirect.' }
            }
        } finally { $response.Dispose() }
    }
    [pscustomobject]@{ status = 'PUBLISHED'; sourceCommit = $ExpectedCommit; htmlDigest = $remoteDigest; ownerLoginAutomated = $false; differentTenantTest = 'NOT_TESTED' } | ConvertTo-Json
} catch {
    if ($headers) {
        try {
            $null = Invoke-RestMethod -Method Patch -Uri "${base}?api-version=2026-08-01" -Headers $headers -ContentType 'application/json' -Body (@{properties=@{publicNetworkAccess='Disabled'}} | ConvertTo-Json -Depth 3) -TimeoutSec 60
            $closed = Invoke-RestMethod -Uri "${base}?api-version=2026-08-01" -Headers $headers -TimeoutSec 30
            if ($closed.properties.publicNetworkAccess -ne 'Disabled') { throw 'Ingress closure unconfirmed.' }
            Write-Output 'Public ingress disabled after failed verification; operator recovery required.'
        } catch { Write-Output 'URGENT: ingress closure could not be verified; operator intervention required.' }
    }
    throw "Preview publishing failed during $stage. Sensitive response details withheld."
} finally {
    if ($client) { $client.Dispose() }
    $token = $null
    $headers = $null
}