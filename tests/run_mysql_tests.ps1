param(
    [string]$MariaDbBin = 'C:\Program Files\MariaDB 12.1\bin',
    [string]$Python = '',
    [string]$Test = 'tests.test_integration'
)
$ErrorActionPreference = 'Stop'
$taskRepo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Python) { $Python = Join-Path $taskRepo 'bot\venv\Scripts\python.exe' }
$taskDbDir = Join-Path ([System.IO.Path]::GetTempPath()) ('kartelbot-tests-' + [guid]::NewGuid().ToString('N'))
$taskProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$taskProbe.Start()
$taskPort = $taskProbe.LocalEndpoint.Port
$taskProbe.Stop()
$taskProcess = $null
$taskOldPort = $env:KARTEL_TEST_DB_PORT
$taskOldWarnings = $env:PYTHONWARNINGS
$taskResult = 1
try {
    & (Join-Path $MariaDbBin 'mariadb-install-db.exe') --datadir=$taskDbDir --port=$taskPort --password=local-test-only --silent
    if ($LASTEXITCODE -ne 0) { throw 'Test database initialization failed' }
    $taskProcess = Start-Process -FilePath (Join-Path $MariaDbBin 'mysqld.exe') -ArgumentList @(
        '--no-defaults', ('--datadir="' + $taskDbDir + '"'), ('--port=' + $taskPort),
        '--bind-address=127.0.0.1', '--innodb-buffer-pool-size=64M', '--max-connections=20'
    ) -WindowStyle Hidden -PassThru
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 40; $taskAttempt++) {
        $taskSocket = [System.Net.Sockets.TcpClient]::new()
        try { $taskSocket.Connect('127.0.0.1', $taskPort); $taskReady = $true; break }
        catch { Start-Sleep -Milliseconds 250 }
        finally { $taskSocket.Dispose() }
    }
    if (-not $taskReady) { throw 'Test database failed to start' }
    $env:KARTEL_TEST_DB_PORT = [string]$taskPort
    $env:PYTHONWARNINGS = 'ignore::Warning'
    Push-Location $taskRepo
    try {
        # The portal has its own virtual environment; this runner validates only
        # the opt-in bot integration suite against the disposable MariaDB.
        & $Python -B -m unittest $Test -v
        $taskResult = $LASTEXITCODE
    } finally { Pop-Location }
} finally {
    $env:KARTEL_TEST_DB_PORT = $taskOldPort
    $env:PYTHONWARNINGS = $taskOldWarnings
    if ($taskProcess -and -not $taskProcess.HasExited) {
        Stop-Process -Id $taskProcess.Id
        $taskProcess.WaitForExit()
    }
    Write-Host "Test database logs retained at $taskDbDir"
}
exit $taskResult
