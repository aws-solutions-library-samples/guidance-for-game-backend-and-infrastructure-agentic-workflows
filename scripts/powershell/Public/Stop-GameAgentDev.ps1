function Stop-GameAgentDev {
    <#
    .SYNOPSIS
        Stops Game Agent development servers.
    .DESCRIPTION
        Stops backend and frontend by PID files and port-based fallback.
        Matches dev/stop.sh behavior.
    .PARAMETER BackendOnly
        Stop only the backend.
    .PARAMETER FrontendOnly
        Stop only the frontend.
    .EXAMPLE
        Stop-GameAgentDev
    #>
    [CmdletBinding()]
    param(
        [int]$BackendPort = 8080,
        [int]$FrontendPort = 3000,
        [switch]$BackendOnly,
        [switch]$FrontendOnly
    )

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $logsDir = Join-Path $repoRoot 'logs'

    Write-Host ''
    Write-GameAgentStatus 'Stopping Game Agent development servers...' -Type Info
    Write-Host ''

    # Helper: stop by PID file
    function Stop-ByPidFile {
        param([string]$Name, [string]$PidFile)
        if (Test-Path $PidFile) {
            $pid = (Get-Content $PidFile -Raw).Trim()
            try {
                $proc = Get-Process -Id $pid -ErrorAction Stop
                Write-GameAgentStatus "Stopping $Name (PID: $pid)..." -Type Info
                Stop-Process -Id $pid -Force
                Start-Sleep -Seconds 2
                Write-GameAgentStatus "$Name stopped" -Type Success
            } catch {
                Write-GameAgentStatus "$Name PID $pid not running" -Type Warning
            }
            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        }
    }

    # Helper: stop by port (fallback)
    function Stop-ByPort {
        param([string]$Name, [int]$Port)
        if (Test-GameAgentPort -Port $Port) {
            Write-GameAgentStatus "Stopping $Name on port $Port..." -Type Info
            if ($IsWindows) {
                $pids = netstat -ano | Select-String ":$Port\s" | ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
                foreach ($p in $pids) { try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {} }
            } else {
                & lsof -ti:$Port 2>$null | ForEach-Object { kill $_ 2>$null }
                Start-Sleep -Seconds 1
                & lsof -ti:$Port 2>$null | ForEach-Object { kill -9 $_ 2>$null }
            }
            Write-GameAgentStatus "$Name stopped" -Type Success
        }
    }

    if (-not $FrontendOnly) {
        Stop-ByPidFile 'AgentCore Runtime' (Join-Path $logsDir 'agentcore.pid')
        Stop-ByPort 'AgentCore Runtime' $BackendPort
    }

    if (-not $BackendOnly) {
        Stop-ByPidFile 'Frontend' (Join-Path $logsDir 'frontend.pid')
        Stop-ByPort 'Frontend' $FrontendPort
    }

    # Cleanup remaining processes
    Write-GameAgentStatus 'Cleaning up remaining processes...' -Type Info
    if (-not $IsWindows) {
        if (-not $FrontendOnly) {
            & pkill -f 'python.*agentcore_main.py' 2>$null
            Start-Sleep -Seconds 1
            & pkill -9 -f 'python.*agentcore_main.py' 2>$null
        }
        if (-not $BackendOnly) {
            & pkill -f 'next dev' 2>$null
            Start-Sleep -Seconds 1
            & pkill -9 -f 'next dev' 2>$null
        }
    }

    # Also clean up PowerShell background jobs (legacy support)
    Get-Job -Name 'GameAgent-*' -ErrorAction SilentlyContinue | Stop-Job -PassThru | Remove-Job

    Write-Host ''
    Write-GameAgentStatus 'All development services stopped' -Type Success
    Write-Host "Log files preserved in $logsDir" -ForegroundColor Gray
    Write-Host ''
}
