param(
    [string]$Url = 'http://127.0.0.1:8765/health',
    [int]$IntervalSeconds = 60,
    [int]$FailureThreshold = 2,
    [switch]$Once
)

if ($IntervalSeconds -lt 10 -or $FailureThreshold -lt 1) {
    throw 'IntervalSeconds must be at least 10 and FailureThreshold at least 1.'
}

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
$dataRoot = if ($env:CHROME_MANAGER_DATA_ROOT) { $env:CHROME_MANAGER_DATA_ROOT } else { Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path '.data' }
$logPath = Join-Path $dataRoot 'logs\watchdog.log'
$logDirectory = Split-Path -Parent $logPath
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$handler = New-Object System.Net.Http.HttpClientHandler
$handler.UseProxy = $false
$client = New-Object System.Net.Http.HttpClient -ArgumentList $handler
$client.Timeout = [TimeSpan]::FromSeconds(8)
$failures = 0
$alerting = $false

function Write-WatchdogEvent([string]$level, [string]$message) {
    $line = '{0} {1} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $level, $message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Output $line
}

try {
    while ($true) {
        $healthy = $false
        $reason = 'unknown'
        try {
            $response = $client.GetAsync($Url).GetAwaiter().GetResult()
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
            $healthy = $response.IsSuccessStatusCode -and $body.status -eq 'ok'
            $reason = 'HTTP {0}, status={1}' -f [int]$response.StatusCode, $body.status
            if ($body.cdp_failed) { $reason += ', cdp_failed=' + (@($body.cdp_failed) -join ',') }
            if ($body.alerts) { $reason += ', alerts=' + (@($body.alerts) -join '; ') }
        } catch {
            $failure = $_.Exception
            while ($failure.InnerException) { $failure = $failure.InnerException }
            $reason = $failure.GetType().Name
        }

        if ($healthy) {
            $failures = 0
            if ($alerting) {
                Write-WatchdogEvent 'RECOVERED' 'ChromeManager health check is healthy again.'
                $alerting = $false
            }
        } else {
            $failures++
            if (-not $alerting -and $failures -ge $FailureThreshold) {
                Write-WatchdogEvent 'ALERT' ('ChromeManager health check failed: {0}' -f $reason)
                $alerting = $true
            }
        }

        if ($Once) {
            Write-Output ('{0}: {1}' -f $(if ($healthy) { 'OK' } else { 'FAILED' }), $reason)
            if ($healthy) { exit 0 } else { exit 1 }
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    $client.Dispose()
    $handler.Dispose()
}
