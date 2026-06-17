function Test-GameAgentFull {
    <#
    .SYNOPSIS
        Runs the full smart test suite (auto-detects deployment status).
    .DESCRIPTION
        PS equivalent of test-full.sh. Runs unit, integration, E2E, AI eval,
        and stress tests based on what's available (deployed stack, localhost, or unit-only).
    .PARAMETER Profile
        AWS CLI profile.
    .PARAMETER Region
        AWS region. Default: us-west-2.
    .EXAMPLE
        Test-GameAgentFull -Profile demo
    #>
    [CmdletBinding()]
    param(
        [string]$Profile,
        [string]$Region = 'us-west-2'
    )

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $backendPath = Join-Path $repoRoot 'backend'
    $uiPath = Join-Path $repoRoot 'ui'
    $failed = 0

    $resolved = Resolve-GameAgentProfile -Profile $Profile
    $Profile = $resolved.Profile
    $profileArgs = $resolved.ProfileArgs

    Write-Host ''
    Write-GameAgentStatus 'Game Agent - Smart Test Suite' -Type Info
    Write-Host ('=' * 60)

    # ── Detect environment ──
    Write-Host ''
    Write-GameAgentStatus 'Checking test environment...' -Type Info

    $localhostBackend = Test-GameAgentPort -Port 8080
    $localhostFrontend = Test-GameAgentPort -Port 3000
    if ($localhostBackend) { Write-GameAgentStatus 'Localhost backend detected (port 8080)' -Type Success }
    if ($localhostFrontend) { Write-GameAgentStatus 'Localhost frontend detected (port 3000)' -Type Success }

    $deploymentDetected = $false
    $runtimeId = ''; $runtimeArn = ''; $frontendUrl = ''
    $agentcoreConfig = Join-Path $backendPath '.bedrock_agentcore.yaml'
    if (Test-Path $agentcoreConfig) {
        $rid = ((yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' $agentcoreConfig 2>$null) | Out-String).Trim()
        if ($rid -and $rid -ne 'null') {
            & aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id $rid --region $Region @profileArgs 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $deploymentDetected = $true
                $runtimeId = $rid
                $runtimeArn = ((yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' $agentcoreConfig 2>$null) | Out-String).Trim()
                $frontendUrl = (& aws cloudformation describe-stacks --stack-name game-agent-frontend --region $Region @profileArgs `
                    --query 'Stacks[0].Outputs[?OutputKey==`ServiceUrl`].OutputValue' --output text 2>$null) | Out-String
                $frontendUrl = $frontendUrl.Trim()
                Write-GameAgentStatus "Deployed stack available (Runtime: $runtimeId)" -Type Success
            }
        }
    }

    $testMode = if ($deploymentDetected) { 'deployed' } elseif ($localhostBackend) { 'localhost' } else { 'unit-only' }
    Write-GameAgentStatus "Test Mode: $($testMode.ToUpper())" -Type Info

    # Ensure deps
    $venvPython = if ($IsWindows) { "$backendPath/.venv/Scripts/python.exe" } else { "$backendPath/.venv/bin/python" }
    if (-not (Test-Path $venvPython)) {
        Push-Location $backendPath
        try {
            uv sync
            if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit code $LASTEXITCODE)" }
        } finally { Pop-Location }
    }
    if (-not (Test-Path (Join-Path $uiPath 'node_modules'))) {
        Push-Location $uiPath
        try {
            npm install
            if ($LASTEXITCODE -ne 0) { throw "npm install failed (exit code $LASTEXITCODE)" }
        } finally { Pop-Location }
    }

    # ── 1. Unit Tests ──
    Write-Host ''
    Write-GameAgentStatus '1. Unit Tests (Fast feedback)' -Type Info
    try { Test-GameAgentUnit } catch { $failed = 1 }

    # ── 2. Integration Tests ──
    Write-Host ''
    Write-GameAgentStatus '2. Integration Tests' -Type Info
    if ($testMode -eq 'unit-only') {
        Write-GameAgentStatus 'Skipping (deploy with Deploy-GameAgent for integration tests)' -Type Warning
    } else {
        Push-Location $backendPath
        try {
            if ($testMode -eq 'deployed') {
                $env:AGENTCORE_RUNTIME_ID = $runtimeId
                $env:AGENTCORE_RUNTIME_ARN = $runtimeArn
                $env:FRONTEND_URL = $frontendUrl
            }
            & $venvPython -m pytest tests/integration/ -m 'not slow' -v --tb=short --maxfail=5 --timeout=60
            if ($LASTEXITCODE -ne 0) { $failed = 1 }
        } finally { Pop-Location }
    }

    # ── 3. Frontend E2E Tests ──
    Write-Host ''
    Write-GameAgentStatus '3. Frontend E2E Tests' -Type Info
    if ($localhostFrontend -or $testMode -eq 'deployed') {
        Push-Location $uiPath
        try {
            npx playwright install --with-deps 2>$null
            npx playwright test
            if ($LASTEXITCODE -ne 0) { $failed = 1 }
        } finally { Pop-Location }
    } else {
        Write-GameAgentStatus 'Skipping (frontend not available)' -Type Warning
    }

    # ── 4. AI Evaluation Tests ──
    Write-Host ''
    Write-GameAgentStatus '4. AI Evaluation Tests' -Type Info
    if ($testMode -eq 'deployed') {
        Push-Location $backendPath
        try {
            & $venvPython -m pytest tests/ai_evals/ -m 'ai_eval' -v --tb=short --maxfail=3 --timeout=60
            if ($LASTEXITCODE -ne 0) { $failed = 1 }
        } finally { Pop-Location }
    } else {
        Write-GameAgentStatus 'Skipping (AI evals require deployed stack)' -Type Warning
    }

    # ── 5. Stress Tests ──
    Write-Host ''
    Write-GameAgentStatus '5. Stress/Performance Tests' -Type Info
    if ($testMode -eq 'deployed') {
        $perfTests = Join-Path $backendPath 'tests/performance'
        if (Test-Path "$perfTests/test_*.py") {
            Push-Location $backendPath
            try {
                & $venvPython -m pytest tests/performance/ -m 'stress' -v --tb=short --maxfail=2 --timeout=300
                if ($LASTEXITCODE -ne 0) { $failed = 1 }
            } finally { Pop-Location }
        } else {
            Write-GameAgentStatus 'No stress tests found (skipping)' -Type Warning
        }
    } else {
        Write-GameAgentStatus 'Skipping (stress tests require deployed stack)' -Type Warning
    }

    # ── Summary ──
    Write-Host ''
    Write-Host ('=' * 60) -ForegroundColor $(if ($failed) { 'Red' } else { 'Green' })
    if ($failed) {
        Write-GameAgentStatus 'SOME TESTS FAILED — Review output above' -Type Error
        throw 'Test suite had failures'
    } else {
        Write-GameAgentStatus "ALL TESTS PASSED (mode: $testMode)" -Type Success
    }
    Write-Host "Coverage report: backend/htmlcov/index.html" -ForegroundColor Cyan
    Write-Host ''
}
