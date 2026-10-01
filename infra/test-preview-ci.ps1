[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageDirectory,
    [Parameter(Mandatory = $true)][string]$ExpectedCommit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$null = . (Join-Path $PSScriptRoot 'publish-preview.ps1') -PackageDirectory $PackageDirectory -ExpectedCommit $ExpectedCommit
$sourcePackage = (Resolve-Path $PackageDirectory).Path
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('ats-preview-ci-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $testRoot
$checks = [System.Collections.Generic.List[string]]::new()

function Assert-Rejected([string]$Name, [scriptblock]$Action) {
    $rejected = $false
    try { $null = & $Action } catch { $rejected = $true }
    if (-not $rejected) { throw "Expected rejection: $Name" }
    $checks.Add($Name)
}

function New-PackageFixture([string]$Name) {
    $directory = Join-Path $testRoot $Name
    $null = New-Item -ItemType Directory -Path $directory
    Copy-Item -LiteralPath (Join-Path $sourcePackage 'preview.zip'), (Join-Path $sourcePackage 'manifest.json') -Destination $directory
    return $directory
}

function Update-FixtureHtml([string]$Directory, [scriptblock]$Mutation) {
    $zipPath = Join-Path $Directory 'preview.zip'
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $reader = [System.IO.StreamReader]::new($archive.Entries[0].Open())
        try { $html = $reader.ReadToEnd() } finally { $reader.Dispose() }
    } finally { $archive.Dispose() }
    $match = [regex]::Match($html, '<script id="report-data" type="application/json">(.*?)</script>', [System.Text.RegularExpressions.RegexOptions]::Singleline)
    $data = $match.Groups[1].Value | ConvertFrom-Json -AsHashtable
    & $Mutation $data
    $html = $html.Replace($match.Groups[1].Value, ($data | ConvertTo-Json -Depth 100 -Compress))
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($html)
    Remove-Item -LiteralPath $zipPath
    $archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        $stream = $archive.CreateEntry('index.html').Open()
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    } finally { $archive.Dispose() }
    $manifestPath = Join-Path $Directory 'manifest.json'
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $stream = [System.IO.MemoryStream]::new($bytes)
    try { $manifest.htmlDigest = Get-StreamDigest $stream } finally { $stream.Dispose() }
    $manifest.zipDigest = 'sha256:' + (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest | ConvertTo-Json -Depth 4 | Set-Content $manifestPath -Encoding utf8
}

try {
    $null = Read-PreviewPackage $sourcePackage $ExpectedCommit
    $checks.Add('valid-package')
    Assert-Rejected 'wrong-commit' { Read-PreviewPackage $sourcePackage ('0' * 40) }
    $fixture = New-PackageFixture 'extra-file'
    'not deployable' | Set-Content (Join-Path $fixture 'extra.txt')
    Assert-Rejected 'extra-file' { Read-PreviewPackage $fixture $ExpectedCommit }
    $fixture = New-PackageFixture 'tampered-zip'
    [System.IO.File]::AppendAllText((Join-Path $fixture 'preview.zip'), 'tampered')
    Assert-Rejected 'tampered-zip' { Read-PreviewPackage $fixture $ExpectedCommit }
    $fixture = New-PackageFixture 'unexpected-entry'
    $zip = Join-Path $fixture 'preview.zip'
    $archive = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Update)
    try { $null = $archive.CreateEntry('../outside.html') } finally { $archive.Dispose() }
    $manifestPath = Join-Path $fixture 'manifest.json'
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $manifest.zipDigest = 'sha256:' + (Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest | ConvertTo-Json -Depth 4 | Set-Content $manifestPath -Encoding utf8
    Assert-Rejected 'unexpected-zip-entry' { Read-PreviewPackage $fixture $ExpectedCommit }
    foreach ($case in @(
        @{name='quote-evidence'; mutate={param($data) $data.quote_evidence=@{source_id='forbidden'}}},
        @{name='promoted'; mutate={param($data) $data.report.promoted=$true}},
        @{name='deployment-ready'; mutate={param($data) $data.report.deployment_ready=$true}},
        @{name='non-synthetic'; mutate={param($data) $data.report.mode='REAL_DATA'}}
    )) {
        $fixture = New-PackageFixture $case.name
        Update-FixtureHtml $fixture $case.mutate
        Assert-Rejected $case.name { Read-PreviewPackage $fixture $ExpectedCommit }
    }
    $baseline = @{
        site=@{location='Korea Central';properties=@{state='Running';publicNetworkAccess='Enabled';httpsOnly=$true;serverFarmId='/subscriptions/199bfb5d-fa2d-4765-88cf-016129ccb7de/resourceGroups/atsview-rg/providers/Microsoft.Web/serverfarms/asp-atsview-dev-ac2a'}}
        auth=@{
            platform=@{enabled=$true};httpSettings=@{requireHttps=$true};globalValidation=@{requireAuthentication=$true;excludedPaths=@();unauthenticatedClientAction='RedirectToLoginPage'};login=@{tokenStore=@{enabled=$false}}
            identityProviders=@{azureActiveDirectory=@{enabled=$true;registration=@{clientId='f9b51430-79ef-4686-9e39-affb1a844935';openIdIssuer='https://login.microsoftonline.com/93b885b6-4629-4327-bacf-a2e5175a8ede/v2.0'};validation=@{allowedAudiences=@('f9b51430-79ef-4686-9e39-affb1a844935');defaultAuthorizationPolicy=@{allowedPrincipals=@{identities=@('adb9b0df-9bb9-4822-b938-e6b88343b5eb');groups=@()}}}};customOpenIdConnectProviders=@{}}
        }
        config=@{ftpsState='Disabled';minTlsVersion='1.2';scmMinTlsVersion='1.2';alwaysOn=$false;use32BitWorkerProcess=$true}
    }
    Assert-PreviewConfiguration $baseline.site $baseline.auth $baseline.config $false $false
    $checks.Add('valid-hosted-policy')
    $withoutGroups = $baseline | ConvertTo-Json -Depth 20 | ConvertFrom-Json -AsHashtable
    $withoutGroups.auth.identityProviders.azureActiveDirectory.validation.defaultAuthorizationPolicy.allowedPrincipals.Remove('groups')
    Assert-PreviewConfiguration $withoutGroups.site $withoutGroups.auth $withoutGroups.config $false $false
    $checks.Add('optional-groups-omitted')
    $platformDefaults = $baseline | ConvertTo-Json -Depth 20 | ConvertFrom-Json -AsHashtable
    $platformDefaults.auth.identityProviders.google = @{enabled=$true;registration=@{clientId=$null;clientSecretSettingName=$null}}
    Assert-PreviewConfiguration $platformDefaults.site $platformDefaults.auth $platformDefaults.config $false $false
    $checks.Add('unconfigured-platform-provider-default')
    Assert-Rejected 'basic-auth-enabled' { Assert-PreviewConfiguration $baseline.site $baseline.auth $baseline.config $true $false }
    foreach ($case in @(
        @{name='anonymous-enabled'; mutate={param($copy) $copy.auth.globalValidation.requireAuthentication=$false}},
        @{name='anonymous-action'; mutate={param($copy) $copy.auth.globalValidation.unauthenticatedClientAction='AllowAnonymous'}},
        @{name='wrong-region'; mutate={param($copy) $copy.site.location='eastus'}},
        @{name='excluded-path'; mutate={param($copy) $copy.auth.globalValidation.excludedPaths=@('/index.html')}},
        @{name='extra-principal'; mutate={param($copy) $copy.auth.identityProviders.azureActiveDirectory.validation.defaultAuthorizationPolicy.allowedPrincipals.identities+=@('other-user')}},
        @{name='allowed-group'; mutate={param($copy) $copy.auth.identityProviders.azureActiveDirectory.validation.defaultAuthorizationPolicy.allowedPrincipals.groups=@('other-group')}},
        @{name='extra-provider'; mutate={param($copy) $copy.auth.identityProviders.google=@{enabled=$true;registration=@{clientId='unexpected-app'}}}},
        @{name='custom-provider'; mutate={param($copy) $copy.auth.identityProviders.customOpenIdConnectProviders=@{other=@{enabled=$true}}}},
        @{name='wrong-issuer'; mutate={param($copy) $copy.auth.identityProviders.azureActiveDirectory.registration.openIdIssuer='https://login.microsoftonline.com/common/v2.0'}},
        @{name='halted-site'; mutate={param($copy) $copy.site.properties.publicNetworkAccess='Disabled'}}
    )) {
        $copy = $baseline | ConvertTo-Json -Depth 20 | ConvertFrom-Json -AsHashtable
        & $case.mutate $copy
        Assert-Rejected $case.name { Assert-PreviewConfiguration $copy.site $copy.auth $copy.config $false $false }
    }
    [pscustomobject]@{checksPassed=$checks.Count;checks=@($checks);cloudOperationsPerformed=$false} | ConvertTo-Json -Depth 3
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force
}