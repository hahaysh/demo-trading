[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReportPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [ValidatePattern('^[a-f0-9]{40}$')]
    [string]$SourceCommit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$reportFile = (Resolve-Path -LiteralPath $ReportPath).Path
foreach ($path in @($reportFile, [System.IO.Path]::GetFullPath($OutputDirectory))) {
    $ancestor = $path
    while ($ancestor) {
        if ((Test-Path -LiteralPath $ancestor) -and
            ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw 'Link or reparse point in artifact path.'
        }
        $ancestor = Split-Path $ancestor -Parent
    }
}
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
$allowedRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot '.local/azure-preview'))
$separator = [System.IO.Path]::DirectorySeparatorChar
if (-not $outputPath.StartsWith("$allowedRoot$separator", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Output must be a new directory below .local/azure-preview.'
}
if (Test-Path -LiteralPath $outputPath) {
    throw 'Output directory already exists; choose a new path.'
}
$python = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The existing Windows Python environment is required; no installation is performed.'
}
$reportInfo = Get-Item -LiteralPath $reportFile
if ($reportInfo.PSIsContainer -or $reportInfo.Length -gt 4 * 1024 * 1024) {
    throw 'Report must be a file no larger than 4 MiB.'
}
try {
    $report = Get-Content -LiteralPath $reportFile -Raw -Encoding utf8 | ConvertFrom-Json
} catch {
    throw 'Report is not valid JSON; source contents withheld.'
}
if ($report.mode -cne 'SYNTHETIC_OFFLINE_ONLY' -or
    $report.deployment_ready -isnot [bool] -or $report.deployment_ready -or
    $report.promoted -isnot [bool] -or $report.promoted) {
    throw 'Only an unpromoted, deployment-blocked synthetic report is permitted.'
}
$sourceHash = (Get-FileHash -LiteralPath $reportFile -Algorithm SHA256).Hash.ToLowerInvariant()
$sitePath = Join-Path $outputPath 'site'
$indexPath = Join-Path $sitePath 'index.html'
& $python -m ats.dashboard --report $reportFile --output $indexPath
if ($LASTEXITCODE -ne 0) {
    throw 'Dashboard export failed; this directory is not a deployment artifact.'
}
$files = @(Get-ChildItem -LiteralPath $sitePath -Force)
if ($files.Count -ne 1 -or $files[0].Name -cne 'index.html' -or $files[0].PSIsContainer) {
    throw 'Deployment allowlist violation: only index.html is permitted.'
}
$html = Get-Content -LiteralPath $indexPath -Raw -Encoding utf8
$match = [regex]::Match($html, '<script id="report-data" type="application/json">(.*?)</script>', [System.Text.RegularExpressions.RegexOptions]::Singleline)
if (-not $match.Success) {
    throw 'Missing embedded report.'
}
$embedded = $match.Groups[1].Value | ConvertFrom-Json
if ($null -ne $embedded.quote_evidence -or
    $embedded.report.mode -cne 'SYNTHETIC_OFFLINE_ONLY' -or
    $embedded.report_digest -cne "sha256:$sourceHash") {
    throw 'Synthetic-only artifact provenance check failed.'
}
if ($html.Contains('__ATS_REPORT_DATA__') -or -not $html.Contains('<html lang="ko">') -or
    -not $html.Contains("connect-src 'none'") -or
    (Get-FileHash -LiteralPath $reportFile -Algorithm SHA256).Hash.ToLowerInvariant() -cne $sourceHash) {
    throw 'Artifact validation failed or source report changed.'
}
$htmlDigest = 'sha256:' + (Get-FileHash -LiteralPath $indexPath -Algorithm SHA256).Hash.ToLowerInvariant()
$packagePath = Join-Path $outputPath 'package'
$null = New-Item -ItemType Directory -Path $packagePath
$zipPath = Join-Path $packagePath 'preview.zip'
$archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    $entry = $archive.CreateEntry('index.html', [System.IO.Compression.CompressionLevel]::Optimal)
    $entry.LastWriteTime = [DateTimeOffset]::new(2020, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
    $stream = $entry.Open()
    try {
        $bytes = [System.IO.File]::ReadAllBytes($indexPath)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally {
        $stream.Dispose()
    }
} finally {
    $archive.Dispose()
}
$manifest = [ordered]@{
    schemaVersion = 1
    mode = 'SYNTHETIC_OFFLINE_ONLY'
    sourceCommit = $SourceCommit
    files = @('index.html')
    reportDigest = "sha256:$sourceHash"
    htmlDigest = $htmlDigest
    zipDigest = 'sha256:' + (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    quoteEvidenceIncluded = $false
}
$manifest | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $packagePath 'manifest.json') -Encoding utf8
[pscustomobject]@{
    status = 'LOCAL_ARTIFACT_ONLY'
    deploymentApproved = $false
    artifactDirectory = $sitePath
    files = @('index.html')
    reportDigest = "sha256:$sourceHash"
    htmlDigest = $htmlDigest
    quoteEvidenceIncluded = $false
    packageDirectory = $packagePath
    zipDigest = $manifest.zipDigest
} | ConvertTo-Json -Depth 3
