# Run with: powershell -ExecutionPolicy Bypass -File .\start.ps1
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$appUrl = 'http://127.0.0.1:5000'
$appDirectory = $PSScriptRoot

function Test-SmartLibReady {
    try {
        $payload = Invoke-RestMethod -Uri "$appUrl/api/session" -TimeoutSec 2 -ErrorAction Stop
        return ($payload.PSObject.Properties.Name -contains 'user') -and
            ($payload.csrf_token -is [string]) -and
            ($payload.csrf_token.Length -gt 0) -and
            ($null -ne $payload.settings.loan_days) -and
            ($null -ne $payload.settings.max_loans) -and
            ($null -ne $payload.settings.fine_per_day) -and
            ($payload.PSObject.Properties.Name -contains 'backup_max_mb')
    }
    catch {
        return $false
    }
}

function Test-AppPortOccupied {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync('127.0.0.1', 5000)
        return $connection.Wait(1000) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

if (Test-SmartLibReady) {
    Write-Host "SmartLibPro is already running. Opening $appUrl"
    Start-Process -FilePath $appUrl
    return
}

if (Test-AppPortOccupied) {
    throw 'Port 5000 is already in use by another application, or SmartLibPro is not responding. Close the application using that port and run this launcher again.'
}

$pythonPath = Join-Path $appDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw 'Python was not found. Install Python, or create .venv and install requirements.txt before running this launcher.'
    }
    $pythonPath = $pythonCommand.Source
}

$instanceDirectory = Join-Path $appDirectory 'instance'
New-Item -ItemType Directory -Path $instanceDirectory -Force | Out-Null
$stdoutPath = Join-Path $instanceDirectory 'server.stdout.log'
$stderrPath = Join-Path $instanceDirectory 'server.stderr.log'
$appPath = Join-Path $appDirectory 'app.py'
$previousHost = [Environment]::GetEnvironmentVariable('SMARTLIB_HOST', 'Process')
$previousPort = [Environment]::GetEnvironmentVariable('SMARTLIB_PORT', 'Process')
try {
    $env:SMARTLIB_HOST = '127.0.0.1'
    $env:SMARTLIB_PORT = '5000'
    $server = Start-Process -FilePath $pythonPath -ArgumentList @('-u', ('"' + $appPath + '"')) `
        -WorkingDirectory $appDirectory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
}
finally {
    [Environment]::SetEnvironmentVariable('SMARTLIB_HOST', $previousHost, 'Process')
    [Environment]::SetEnvironmentVariable('SMARTLIB_PORT', $previousPort, 'Process')
}

Write-Host 'Starting SmartLibPro...'
$startupTimer = [System.Diagnostics.Stopwatch]::StartNew()
while ($startupTimer.Elapsed.TotalSeconds -lt 20) {
    if (Test-SmartLibReady) {
        Write-Host "SmartLibPro is ready. Opening $appUrl"
        Start-Process -FilePath $appUrl
        return
    }
    $server.Refresh()
    if ($server.HasExited) {
        break
    }
    Start-Sleep -Milliseconds 300
}

$errorDetails = if (Test-Path -LiteralPath $stderrPath) {
    (Get-Content -LiteralPath $stderrPath -Tail 15) -join [Environment]::NewLine
} else {
    'No server error log was created.'
}
throw "SmartLibPro did not become ready within 20 seconds. See $stderrPath for details. Install requirements.txt if dependencies are missing.$([Environment]::NewLine)$errorDetails"
