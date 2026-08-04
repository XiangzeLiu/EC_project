# TEMP_LATENCY_DIAGNOSTIC: admin-only collector; remove after the incident.
# Run on the Client device, not on the TS server.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetHost,
    [int]$DurationSeconds = 600,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Continue"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$collectorStartedUtc = [DateTime]::UtcNow
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path ([Environment]::GetFolderPath("Desktop")) "SC_Client_LatencyDiagnostics_$stamp"
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

"TEMP_LATENCY_DIAGNOSTIC; delete this directory after the incident is explained." |
    Set-Content -LiteralPath (Join-Path $OutputDirectory "README_REMOVE_AFTER_INCIDENT.txt") -Encoding UTF8
Get-Date -Format o | Set-Content -LiteralPath (Join-Path $OutputDirectory "collector_started.txt") -Encoding UTF8

tracert.exe -d -h 30 -w 1000 $TargetHost |
    Set-Content -LiteralPath (Join-Path $OutputDirectory "tracert_start.txt") -Encoding UTF8
$pathpingOutput = Join-Path $OutputDirectory "pathping_start.txt"
$pathpingJob = Start-Job -ScriptBlock {
    param($Target, $Destination)
    pathping.exe -n -q 20 -p 250 -w 1000 $Target |
        Set-Content -LiteralPath $Destination -Encoding UTF8
} -ArgumentList $TargetHost, $pathpingOutput
w32tm.exe /query /status | Set-Content -LiteralPath (Join-Path $OutputDirectory "clock_status.txt") -Encoding UTF8

$systemMetricsOutput = Join-Path $OutputDirectory "system_metrics.csv"
$typeperfJob = Start-Job -ScriptBlock {
    param($Samples, $Destination)
    typeperf.exe "\Processor(_Total)\% Processor Time" "\System\Processor Queue Length" "\TCPv4\Segments Retransmitted/sec" -si 1 -sc $Samples -f CSV -o $Destination -y | Out-Null
} -ArgumentList $DurationSeconds, $systemMetricsOutput

$ping = New-Object System.Net.NetworkInformation.Ping
$pingRows = [System.Collections.Generic.List[string]]::new()
$pingRows.Add("utc,beijing,status,latency_ms")
$probeStarted = [DateTime]::UtcNow
while (([DateTime]::UtcNow - $probeStarted).TotalSeconds -lt $DurationSeconds) {
    $iterationStarted = [DateTime]::UtcNow
    $utc = [DateTime]::UtcNow
    $beijing = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId($utc, "China Standard Time")
    try {
        $reply = $ping.Send($TargetHost, 1000)
        if ($reply.Status -eq [System.Net.NetworkInformation.IPStatus]::Success) {
            $pingRows.Add("$($utc.ToString('o')),$($beijing.ToString('o')),success,$([int]$reply.RoundtripTime)")
        } else {
            $pingRows.Add("$($utc.ToString('o')),$($beijing.ToString('o')),$($reply.Status),")
        }
    } catch {
        $pingRows.Add("$($utc.ToString('o')),$($beijing.ToString('o')),exception,")
    }
    $remainingMs = 1000 - [int](([DateTime]::UtcNow - $iterationStarted).TotalMilliseconds)
    if ($remainingMs -gt 0) { Start-Sleep -Milliseconds $remainingMs }
}
$pingRows | Set-Content -LiteralPath (Join-Path $OutputDirectory "network_ping.csv") -Encoding UTF8

Wait-Job -Job $typeperfJob -Timeout 15 | Out-Null
Receive-Job -Job $typeperfJob -ErrorAction SilentlyContinue | Out-Null
Stop-Job -Job $typeperfJob -ErrorAction SilentlyContinue
Remove-Job -Job $typeperfJob -Force -ErrorAction SilentlyContinue
Wait-Job -Job $pathpingJob -Timeout 15 | Out-Null
Receive-Job -Job $pathpingJob -ErrorAction SilentlyContinue | Out-Null
Stop-Job -Job $pathpingJob -ErrorAction SilentlyContinue
Remove-Job -Job $pathpingJob -Force -ErrorAction SilentlyContinue
tracert.exe -d -h 30 -w 1000 $TargetHost |
    Set-Content -LiteralPath (Join-Path $OutputDirectory "tracert_end.txt") -Encoding UTF8
$clientDiagnosticRoot = Join-Path $env:APPDATA "SC Client\diagnostics"
if (Test-Path -LiteralPath $clientDiagnosticRoot) {
    $clientLogs = Join-Path $OutputDirectory "client_application_logs"
    New-Item -ItemType Directory -Force -Path $clientLogs | Out-Null
    Get-ChildItem -LiteralPath $clientDiagnosticRoot -Filter "client_latency_*.jsonl" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -ge $collectorStartedUtc.AddMinutes(-1) } |
        Copy-Item -Destination $clientLogs -Force
}
Get-Date -Format o | Set-Content -LiteralPath (Join-Path $OutputDirectory "collector_finished.txt") -Encoding UTF8
Write-Host "Diagnostics saved to: $OutputDirectory"
