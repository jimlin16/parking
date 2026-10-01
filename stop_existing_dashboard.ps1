param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$port = 8765
$url = "http://127.0.0.1:$port/api/state"

function Get-Listener {
    @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Get-DashboardState {
    $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
    if (($response.Headers['Server'] -join ' ') -notmatch 'ParkingDashboard/') {
        throw 'Port 8765 is not served by this Dashboard.'
    }
    $state = $response.Content | ConvertFrom-Json
    if (-not $state.monitor -or -not $state.config -or -not $state.parking_lots) {
        throw 'Port 8765 has an unexpected Dashboard response.'
    }
    $lotKeys = @($state.parking_lots | ForEach-Object { $_.key })
    if ($lotKeys -notcontains 'p4' -or $lotKeys -notcontains 'peace') {
        throw 'Port 8765 does not have this Dashboard parking-lot list.'
    }
    return $state
}

try {
    $listeners = @(Get-Listener)
    if ($listeners.Count -eq 0) { exit 0 }
    if ($listeners.Count -ne 1) { throw 'Port 8765 has multiple listeners.' }

    $ownerPid = [int]$listeners[0].OwningProcess
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid"
    if (-not $owner -or $owner.CommandLine -notmatch '(?:^|[\s"''\\/])dashboard\.py(?:\s|"|$)') {
        throw 'Port 8765 belongs to another program; it will not be stopped.'
    }

    $state = Get-DashboardState
    try {
        $instance = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/instance" -TimeoutSec 2
        $expectedRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
        if ([int]$instance.pid -ne $ownerPid -or
            [IO.Path]::GetFullPath([string]$instance.root).TrimEnd('\') -ine $expectedRoot) {
            throw 'Port 8765 belongs to a Dashboard from another directory.'
        }
    } catch [System.Net.WebException] {
        if (-not $_.Exception.Response -or [int]$_.Exception.Response.StatusCode -ne 404) {
            throw
        }
        # Earlier Dashboard versions do not expose /api/instance. The process,
        # server header, state shape, and parking-lot list were checked above.
    }
    if ($state.monitor.phase -eq 'booking') {
        throw 'The existing Dashboard is booking. Wait for it to finish before restarting.'
    }
    if ($DryRun) {
        Write-Host "Would stop Dashboard PID $ownerPid (phase: $($state.monitor.phase))."
        exit 0
    }

    if ($state.monitor.running) {
        try {
            Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/monitor/stop' `
                -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
        } catch {
            # It may have stopped between the state query and this request.
        }
    }

    $idle = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $state = Get-DashboardState
        if ($state.monitor.phase -eq 'booking') {
            throw 'The existing Dashboard started booking. Wait for it to finish.'
        }
        if (-not $state.monitor.running -and $state.monitor.phase -eq 'idle') {
            $idle = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $idle) { throw 'The existing Dashboard did not become idle.' }

    $listeners = @(Get-Listener)
    if ($listeners.Count -ne 1 -or [int]$listeners[0].OwningProcess -ne $ownerPid) {
        throw 'The listener changed while restarting; no process was stopped.'
    }
    Write-Host "Stopping existing Dashboard PID $ownerPid..."
    Stop-Process -Id $ownerPid -Force -ErrorAction Stop
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (@(Get-Listener).Count -eq 0) { exit 0 }
        Start-Sleep -Milliseconds 250
    }
    throw 'The old Dashboard still owns port 8765.'
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
