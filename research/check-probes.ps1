param(
    [Parameter(Mandatory)][string]$OutputFile
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$target = [System.IO.Path]::GetFullPath($OutputFile)
if (Test-Path -LiteralPath $target) { throw 'Probe output already exists.' }

function Invoke-ComponentProbe {
    param([string]$Image, [string]$Kind)
    $imageId = docker image inspect $Image --format '{{.Id}}'
    if ($LASTEXITCODE -ne 0) { throw "Missing local probe image: $Image" }
    $imageId = "$imageId".Trim()
    $lines = docker run --rm --network none --read-only --user 65532:65532 --cap-drop ALL `
        --security-opt no-new-privileges --pids-limit 64 --memory 1g --cpus 1 `
        --tmpfs /tmp:rw,nosuid,nodev,size=256m $imageId 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Component probe failed: $Kind`n$($lines -join "`n")" }
    $jsonLines = @($lines | ForEach-Object { "$_" } | Where-Object { $_.StartsWith('{') })
    if ($jsonLines.Count -ne 1) { throw 'Expected exactly one component result.' }
    $result = $jsonLines[0] | ConvertFrom-Json
    if ($result.kind -cne $Kind -or $result.mode -cne 'SYNTHETIC_OFFLINE_ONLY' -or
        $result.certified -cne $false -or $result.evaluation_result_created -cne $false -or
        $result.broker_requests_sent -ne 0) {
        throw 'Unexpected component result or unsupported certification claim.'
    }
    [pscustomobject]@{ image_id = $imageId; result = $result }
}

$qlib = Invoke-ComponentProbe 'ats-qlib-smoke:0.9.7' 'QlibSyntheticFeatureProbe'
$lean = Invoke-ComponentProbe 'ats-lean-indicator-smoke:2.5.18127' 'LeanSyntheticIndicatorProbe'
$qlibLock = (Get-FileHash "$PSScriptRoot/qlib/requirements.lock" -Algorithm SHA256).Hash.ToLowerInvariant()
$leanLock = (Get-FileHash "$PSScriptRoot/lean/packages.lock.json" -Algorithm SHA256).Hash.ToLowerInvariant()
if ($qlib.result.dependency_lock_sha256 -cne $qlibLock -or
    $lean.result.dependency_lock_sha256 -cne $leanLock) {
    throw 'Local dependency locks do not match the executed images.'
}
if ($qlib.result.engine_version -cne '0.9.7' -or
    $lean.result.package_version -cne '2.5.18127' -or $lean.result.full_lean_backtest -cne $false) {
    throw 'Unexpected dependency version or full-engine claim.'
}
foreach ($component in @($qlib, $lean)) {
    if ($component.result.rows.Count -ne 5 -or $component.result.signals.Count -ne 5) {
        throw 'Incomplete synthetic component output.'
    }
}
for ($index = 0; $index -lt 5; $index++) {
    if ($qlib.result.rows[$index].Count -ne 2 -or $lean.result.rows[$index].Count -ne 2) {
        throw 'Unexpected feature width.'
    }
    for ($column = 0; $column -lt 2; $column++) {
        if ([decimal]$qlib.result.rows[$index][$column] -ne [decimal]$lean.result.rows[$index][$column]) {
            throw 'Cross-component synthetic feature mismatch.'
        }
    }
    if ($qlib.result.signals[$index] -cne $lean.result.signals[$index]) {
        throw 'Cross-component synthetic signal mismatch.'
    }
}
$receipt = [ordered]@{
    kind = 'SyntheticComponentComparison'
    observed_at = [DateTimeOffset]::UtcNow.ToString('o')
    certified = $false
    full_engine_evaluations = 0
    matched_rows = 5
    runtime = @{
        network = 'none'; host_mounts = 0; read_only = $true
        cpus = 1; memory = '1g'; pids = 64; user = '65532:65532'; timeout_seconds = 60
    }
    qlib_lock_sha256 = $qlibLock
    lean_lock_sha256 = $leanLock
    qlib = $qlib
    lean = $lean
}
$directory = [System.IO.Path]::GetDirectoryName($target)
[System.IO.Directory]::CreateDirectory($directory) | Out-Null
$stream = [System.IO.File]::Open($target, [System.IO.FileMode]::CreateNew)
try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($receipt | ConvertTo-Json -Depth 12) + "`n")
    $stream.Write($bytes, 0, $bytes.Length)
} finally {
    $stream.Dispose()
}
$receipt | ConvertTo-Json -Depth 12