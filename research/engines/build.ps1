$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$context = $PSScriptRoot
function Set-ContentTag([string]$ImageId) {
	if ($ImageId -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Invalid local image ID.' }
	$tag = 'ats-local-build:' + $ImageId.Substring(7)
	docker tag $ImageId $tag
	if ($LASTEXITCODE -ne 0) { throw 'Local content tag failed.' }
	$resolved = docker image inspect $tag --format '{{.Id}}'
	if ($LASTEXITCODE -ne 0 -or $resolved -cne $ImageId) { throw 'Local content tag mismatch.' }
	return $tag
}
docker build -t ats-qlib-smoke:0.9.7 (Join-Path $context '../qlib')
if ($LASTEXITCODE -ne 0) { throw 'Qlib dependency image build failed.' }
$base = docker image inspect ats-qlib-smoke:0.9.7 --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Cannot pin Qlib dependency image.' }
$base = Set-ContentTag $base
docker build -f "$context/Dockerfile.qlib" --build-arg "QLIB_BASE=$base" -t ats-qlib-backtest:dev $context
if ($LASTEXITCODE -ne 0) { throw 'Qlib worker build failed.' }
docker build -f "$context/Dockerfile.lean" -t ats-lean-backtest:dev $context
if ($LASTEXITCODE -ne 0) { throw 'LEAN worker build failed.' }
$qlib = docker image inspect ats-qlib-backtest:dev --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Cannot pin Qlib worker.' }
$lean = docker image inspect ats-lean-backtest:dev --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Cannot pin LEAN worker.' }
$qlib = Set-ContentTag $qlib
$lean = Set-ContentTag $lean
docker build --build-arg "QLIB_IMAGE=$qlib" --build-arg "LEAN_IMAGE=$lean" -t ats-backtest-engines:dev $context
if ($LASTEXITCODE -ne 0) { throw 'Common runtime build failed.' }
docker image inspect ats-backtest-engines:dev --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve common runtime image.' }