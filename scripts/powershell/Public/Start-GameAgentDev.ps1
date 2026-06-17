function Start-GameAgentDev {
    <#
    .SYNOPSIS
        Starts Game Agent development environment.
    .DESCRIPTION
        Starts backend (AgentCore Runtime) and frontend (Next.js) dev servers.
        Matches dev/start.sh behavior: hash-based dep check, env setup, PID files.
    .PARAMETER BackendPort
        Backend port. Default: 8080.
    .PARAMETER FrontendPort
        Frontend port. Default: 3000.
    .PARAMETER BackendOnly
        Start only the backend.
    .PARAMETER FrontendOnly
        Start only the frontend.
    .EXAMPLE
        Start-GameAgentDev
    #>
    [CmdletBinding()]
    param(
        [int]$BackendPort = 8080,
        [int]$FrontendPort = 3000,
        [switch]$BackendOnly,
        [switch]$FrontendOnly
    )

    $ErrorActionPreference = 'Stop'
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $backendPath = Join-Path $repoRoot 'backend'
    $frontendPath = Join-Path $repoRoot 'ui'
    $logsDir = Join-Path $repoRoot 'logs'

    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }

    Write-Host ''
    Write-Host ('=' * 60) -ForegroundColor Cyan
    Write-GameAgentStatus 'Game Agent - AgentCore Dev' -Type Info
    Write-Host ('=' * 60) -ForegroundColor Cyan
    Write-Host ''

    $backendStarted = $false
    $frontendStarted = $false

    # ── Backend ──
    if (-not $FrontendOnly) {
        Write-GameAgentStatus "Starting backend on port $BackendPort..." -Type Info

        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw 'uv not found. Install: https://docs.astral.sh/uv/getting-started/installation/'
        }

        # Kill existing if port in use
        if (Test-GameAgentPort -Port $BackendPort) {
            Write-GameAgentStatus "Port $BackendPort in use, stopping existing..." -Type Warning
            Stop-GameAgentDev -BackendOnly
            Start-Sleep -Seconds 2
        }

        Push-Location $backendPath
        try {
            # Hash-based dependency check (matches bash)
            $currentHash = (Get-FileHash pyproject.toml -Algorithm MD5).Hash
            $hashFile = '.venv/.pyproject_hash'
            $installedHash = if (Test-Path $hashFile) { Get-Content $hashFile -Raw } else { '' }
            if ($currentHash -ne $installedHash.Trim()) {
                Write-GameAgentStatus 'Syncing dependencies with UV...' -Type Info
                uv sync 2>$null
                if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit code $LASTEXITCODE)" }
                $currentHash | Set-Content $hashFile
            }

            # Set environment
            $env:PYTHONPATH = $backendPath

            # Auto-detect Memory ID
            if (Test-Path '.bedrock_agentcore.yaml') {
                $memoryId = ((yq eval '.agents.gameagentruntime.memory.memory_id' .bedrock_agentcore.yaml 2>$null) | Out-String).Trim()
                if ($memoryId -and $memoryId -ne 'null') {
                    $env:BEDROCK_AGENTCORE_MEMORY_ID = $memoryId
                    Write-GameAgentStatus "Memory enabled: $memoryId" -Type Success
                }
            }

            # Start backend
            $logFile = Join-Path $logsDir 'dev-agentcore.log'
            $pidFile = Join-Path $logsDir 'agentcore.pid'
            $venvPython = if ($IsWindows) { '.venv/Scripts/python.exe' } else { '.venv/bin/python' }

            $proc = Start-Process -FilePath $venvPython -ArgumentList 'src/agentcore_main.py' `
                -WorkingDirectory $backendPath -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err" `
                -PassThru -NoNewWindow:$false -WindowStyle Hidden
            $proc.Id | Set-Content $pidFile

            # Wait for startup
            Write-Host '  Waiting for backend...' -NoNewline
            $timeout = 30; $elapsed = 0
            while (-not (Test-GameAgentPort -Port $BackendPort) -and $elapsed -lt $timeout) {
                Start-Sleep -Seconds 1; $elapsed++; Write-Host '.' -NoNewline
            }
            Write-Host ''

            if (Test-GameAgentPort -Port $BackendPort) {
                Write-GameAgentStatus "Backend started (PID: $($proc.Id))" -Type Success
                $backendStarted = $true
            } else {
                Write-GameAgentStatus "Backend failed to start within ${timeout}s. Check $logFile" -Type Error
            }
        } finally { Pop-Location }
    }

    # ── Frontend ──
    if (-not $BackendOnly) {
        Write-Host ''
        Write-GameAgentStatus "Starting frontend on port $FrontendPort..." -Type Info

        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            throw 'npm not found. Install Node.js 18+: https://nodejs.org/'
        }

        if (Test-GameAgentPort -Port $FrontendPort) {
            Write-GameAgentStatus "Port $FrontendPort in use, stopping existing..." -Type Warning
            Stop-GameAgentDev -FrontendOnly
            Start-Sleep -Seconds 2
        }

        Push-Location $frontendPath
        try {
            # Install deps if needed
            $nodeModules = Join-Path $frontendPath 'node_modules'
            $packageJson = Join-Path $frontendPath 'package.json'
            if (-not (Test-Path $nodeModules) -or ((Get-Item $packageJson).LastWriteTime -gt (Get-Item $nodeModules).LastWriteTime)) {
                Write-GameAgentStatus 'Installing Node.js dependencies...' -Type Info
                npm install 2>$null
                if ($LASTEXITCODE -ne 0) { throw "npm install failed (exit code $LASTEXITCODE)" }
            }

            # Ensure .env.local exists
            $envLocal = Join-Path $frontendPath '.env.local'
            if (-not (Test-Path $envLocal)) {
                $example = Join-Path $frontendPath '.env.local.example'
                if (Test-Path $example) { Copy-Item $example $envLocal }
            }

            # Inject AgentCore endpoint if missing
            if (Test-Path $envLocal) {
                $envContent = Get-Content $envLocal -Raw
                if ($envContent -notmatch 'NEXT_PUBLIC_AGENTCORE_ENDPOINT') {
                    Add-Content $envLocal "`n# AgentCore Runtime Configuration"
                    Add-Content $envLocal "NEXT_PUBLIC_AGENTCORE_ENDPOINT=http://localhost:$BackendPort"
                    Add-Content $envLocal "NEXT_PUBLIC_USE_AGENTCORE=true"
                }
            }

            # Start frontend
            $logFile = Join-Path $logsDir 'dev-frontend.log'
            $pidFile = Join-Path $logsDir 'frontend.pid'
            $env:PORT = $FrontendPort

            $proc = Start-Process -FilePath npm -ArgumentList 'run', 'dev' `
                -WorkingDirectory $frontendPath -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err" `
                -PassThru -NoNewWindow:$false -WindowStyle Hidden
            $proc.Id | Set-Content $pidFile

            Write-Host '  Waiting for frontend...' -NoNewline
            $timeout = 60; $elapsed = 0
            while (-not (Test-GameAgentPort -Port $FrontendPort) -and $elapsed -lt $timeout) {
                Start-Sleep -Seconds 1; $elapsed++
                if ($elapsed % 5 -eq 0) { Write-Host '.' -NoNewline }
            }
            Write-Host ''

            if (Test-GameAgentPort -Port $FrontendPort) {
                Write-GameAgentStatus "Frontend started (PID: $($proc.Id))" -Type Success
                $frontendStarted = $true
            } else {
                Write-GameAgentStatus "Frontend failed to start within ${timeout}s. Check $logFile" -Type Error
            }
        } finally { Pop-Location }
    }

    # Summary
    Write-Host ''
    Write-Host ('=' * 60) -ForegroundColor Green
    Write-GameAgentStatus 'Development environment ready!' -Type Success
    Write-Host ('=' * 60) -ForegroundColor Green
    Write-Host ''
    if ($backendStarted) { Write-Host "Backend:  http://localhost:$BackendPort" -ForegroundColor Green }
    if ($frontendStarted) { Write-Host "Frontend: http://localhost:$FrontendPort" -ForegroundColor Green }
    Write-Host ''
    Write-Host 'Stop: Stop-GameAgentDev' -ForegroundColor Yellow
    Write-Host "Logs: Get-Content $logsDir/dev-agentcore.log -Tail 50" -ForegroundColor Cyan
    Write-Host ''
}
