# TEMP_LATENCY_DIAGNOSTIC: admin-only collector; remove after the incident.
# Run on the Client device, not on the TS server.
# The private-route mode temporarily maps the real TS domain to a supplied
# private IPv4 address, then restores the hosts file in the finally block.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetHost,
    [Parameter(Mandatory = $true)]
    [string]$PrivateIp,
    [int]$DurationSeconds = 600,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Continue"
if ($DurationSeconds -lt 1) {
    throw "DurationSeconds must be at least 1."
}
if ($TargetHost -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$') {
    throw "TargetHost contains unsupported characters."
}
$currentPrincipal = New-Object System.Security.Principal.WindowsPrincipal(
    [System.Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $currentPrincipal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this diagnostic script as Administrator because it temporarily edits the Windows hosts file."
}
$parsedPrivateIp = $null
if (-not [System.Net.IPAddress]::TryParse($PrivateIp, [ref]$parsedPrivateIp) -or
    $parsedPrivateIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw "PrivateIp must be a valid IPv4 address."
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$collectorStartedUtc = [DateTime]::UtcNow
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path ([Environment]::GetFolderPath("Desktop")) "SC_Client_LatencyDiagnostics_$stamp"
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$readmePath = Join-Path $OutputDirectory "README_REMOVE_AFTER_INCIDENT.txt"
"TEMP_LATENCY_DIAGNOSTIC; delete this directory after the incident is explained." |
    Set-Content -LiteralPath $readmePath -Encoding UTF8
Get-Date -Format o | Set-Content -LiteralPath (Join-Path $OutputDirectory "collector_started.txt") -Encoding UTF8

$hostsPath = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"
$hostsBackupPath = Join-Path $OutputDirectory "hosts_before_private_route"
$hostsMapped = $false
$originalHostsBytes = $null
$temporaryMarker = "# TEMP_LATENCY_DIAGNOSTIC_PRIVATE_ROUTE $stamp"
$pingRows = [System.Collections.Generic.List[string]]::new()
$pingOutput = Join-Path $OutputDirectory "network_ping.csv"
$typeperfJob = $null
$pathpingJob = $null

try {
    if (-not (Test-Path -LiteralPath $hostsPath)) {
        throw "Windows hosts file was not found: $hostsPath"
    }

    $originalHostsBytes = [System.IO.File]::ReadAllBytes($hostsPath)
    [System.IO.File]::WriteAllBytes($hostsBackupPath, $originalHostsBytes)

    $existingHostEntry = Get-Content -LiteralPath $hostsPath -ErrorAction Stop |
        Where-Object {
            $line = $_.Trim()
            if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
                return $false
            }
            $tokens = $line -split '\s+'
            if ($tokens.Count -lt 2) {
                return $false
            }
            @($tokens | Select-Object -Skip 1) -contains $TargetHost
        } |
        Select-Object -First 1
    if ($existingHostEntry) {
        throw "The hosts file already contains an entry for $TargetHost. Remove it or use a clean test device before collecting."
    }

    $existingHostsText = [System.IO.File]::ReadAllText($hostsPath)
    $linePrefix = ""
    if ($existingHostsText.Length -gt 0 -and
        -not ($existingHostsText.EndsWith("`r") -or $existingHostsText.EndsWith("`n"))) {
        $linePrefix = [Environment]::NewLine
    }
    $entry = "$PrivateIp`t$TargetHost`t$temporaryMarker$([Environment]::NewLine)"
    [System.IO.File]::AppendAllText(
        $hostsPath,
        "$linePrefix$entry",
        [System.Text.Encoding]::ASCII
    )
    $hostsMapped = $true

    $dnsFlushPath = Join-Path $OutputDirectory "dns_flush.txt"
    ipconfig.exe /flushdns | Set-Content -LiteralPath $dnsFlushPath -Encoding UTF8

    $resolvedAddresses = [System.Net.Dns]::GetHostAddresses($TargetHost) |
        ForEach-Object { $_.IPAddressToString }
    $resolvedAddresses | Set-Content -LiteralPath (Join-Path $OutputDirectory "private_route_resolution.txt") -Encoding UTF8
    if (-not ($resolvedAddresses -contains $PrivateIp)) {
        throw "Private route DNS verification failed: $TargetHost did not resolve to $PrivateIp."
    }

    $tcpResult = Test-NetConnection -ComputerName $TargetHost -Port 443 -InformationLevel Detailed -WarningAction SilentlyContinue
    $tcpResult | Format-List * | Set-Content -LiteralPath (Join-Path $OutputDirectory "private_tcp_443.txt") -Encoding UTF8
    if (-not $tcpResult.TcpTestSucceeded) {
        throw "Private route TCP 443 verification failed for $TargetHost ($PrivateIp)."
    }

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

    $pingRows.Add("utc,beijing,status,latency_ms")
    $ping = New-Object System.Net.NetworkInformation.Ping
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
    $pingRows | Set-Content -LiteralPath $pingOutput -Encoding UTF8

    Wait-Job -Job $typeperfJob -Timeout 15 | Out-Null
    Receive-Job -Job $typeperfJob -ErrorAction SilentlyContinue | Out-Null
    Stop-Job -Job $typeperfJob -ErrorAction SilentlyContinue
    Remove-Job -Job $typeperfJob -Force -ErrorAction SilentlyContinue
    $typeperfJob = $null
    Wait-Job -Job $pathpingJob -Timeout 15 | Out-Null
    Receive-Job -Job $pathpingJob -ErrorAction SilentlyContinue | Out-Null
    Stop-Job -Job $pathpingJob -ErrorAction SilentlyContinue
    Remove-Job -Job $pathpingJob -Force -ErrorAction SilentlyContinue
    $pathpingJob = $null
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
}
catch {
    $_ | Out-File -LiteralPath (Join-Path $OutputDirectory "collector_error.txt") -Encoding UTF8
    throw
}
finally {
    if ($pingRows.Count -gt 0) {
        $pingRows | Set-Content -LiteralPath $pingOutput -Encoding UTF8
    }
    if ($typeperfJob) {
        Stop-Job -Job $typeperfJob -ErrorAction SilentlyContinue
        Remove-Job -Job $typeperfJob -Force -ErrorAction SilentlyContinue
    }
    if ($pathpingJob) {
        Stop-Job -Job $pathpingJob -ErrorAction SilentlyContinue
        Remove-Job -Job $pathpingJob -Force -ErrorAction SilentlyContinue
    }
    if ($hostsMapped -and $null -ne $originalHostsBytes) {
        [System.IO.File]::WriteAllBytes($hostsPath, $originalHostsBytes)
        ipconfig.exe /flushdns | Set-Content -LiteralPath (Join-Path $OutputDirectory "dns_flush_restore.txt") -Encoding UTF8
        "Hosts file restored automatically at $(Get-Date -Format o)." |
            Set-Content -LiteralPath (Join-Path $OutputDirectory "hosts_restore_status.txt") -Encoding UTF8
    }
    Get-Date -Format o | Set-Content -LiteralPath (Join-Path $OutputDirectory "collector_finished.txt") -Encoding UTF8
}

Write-Host "Diagnostics saved to: $OutputDirectory"
