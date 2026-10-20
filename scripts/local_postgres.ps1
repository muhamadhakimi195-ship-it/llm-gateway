<#
Start/stop only this project's isolated PostgreSQL cluster on loopback port 55432.
Uses installed PostgreSQL binaries, not the user's existing Windows service.
The password is PUBLIC DEVELOPMENT DATA. Never expose this cluster to a network.
#>
param(
    [ValidateSet('start', 'stop', 'status')][string]$Action = 'start',
    [string]$BinDirectory = 'C:\Program Files\PostgreSQL\18\bin'
)
$ErrorActionPreference = 'Stop'
$gatewayRoot = Split-Path -Parent $PSScriptRoot
$clusterPath = Join-Path $gatewayRoot 'data\native-postgres'
$logPath = Join-Path $gatewayRoot 'data\native-postgres.log'
$pgControl = Join-Path $BinDirectory 'pg_ctl.exe'
if (-not (Test-Path -LiteralPath $pgControl)) { throw 'PostgreSQL 18 binaries were not found.' }

function Invoke-CheckedPg {
    param([string]$Executable, [string[]]$Arguments)
    & (Join-Path $BinDirectory $Executable) @Arguments
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL command failed: $Executable" }
}

if ($Action -eq 'status') {
    & $pgControl status -D $clusterPath
    exit $LASTEXITCODE
}
if ($Action -eq 'stop') {
    Invoke-CheckedPg 'pg_ctl.exe' @('stop', '-D', $clusterPath, '-m', 'fast', '-w', '-t', '15')
    exit
}

if (-not (Test-Path -LiteralPath (Join-Path $clusterPath 'PG_VERSION'))) {
    # Never initialize over an existing nonempty directory or change port 5432.
    if (Test-Path -LiteralPath $clusterPath) { throw 'Cluster path exists but is not initialized.' }
    New-Item -ItemType Directory -Path (Join-Path $gatewayRoot 'data') -Force | Out-Null
    $passwordFile = Join-Path $gatewayRoot 'data\native-postgres-password.txt'
    Set-Content -LiteralPath $passwordFile -Value 'local-gateway-db-change-me' -Encoding ascii
    Invoke-CheckedPg 'initdb.exe' @(
        '-D', $clusterPath, '-U', 'gateway', '--auth=scram-sha-256',
        '--pwfile', $passwordFile, '--encoding=UTF8', '--locale=C'
    )
}
& $pgControl status -D $clusterPath *> $null
if ($LASTEXITCODE -ne 0) {
    Invoke-CheckedPg 'pg_ctl.exe' @(
        'start', '-D', $clusterPath, '-l', $logPath,
        '-o', '-h 127.0.0.1 -p 55432', '-w', '-t', '15'
    )
}
$previousPassword = $env:PGPASSWORD
try {
    $env:PGPASSWORD = 'local-gateway-db-change-me'
    $existing = & (Join-Path $BinDirectory 'psql.exe') -X -At -h 127.0.0.1 -p 55432 `
        -U gateway -d postgres -c "SELECT 1 FROM pg_database WHERE datname = 'llm_gateway'"
    if ($LASTEXITCODE -ne 0) { throw 'Could not query the isolated PostgreSQL cluster.' }
    if ($existing -ne '1') {
        Invoke-CheckedPg 'createdb.exe' @('-h', '127.0.0.1', '-p', '55432', '-U', 'gateway', 'llm_gateway')
    }
} finally {
    $env:PGPASSWORD = $previousPassword
}
Write-Output 'Project PostgreSQL is ready at 127.0.0.1:55432, database llm_gateway.'
