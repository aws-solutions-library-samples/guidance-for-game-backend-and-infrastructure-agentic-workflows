function Test-GameAgentUnit {
    <#
    .SYNOPSIS
        Runs backend and frontend unit tests.
    .DESCRIPTION
        PS equivalent of test-unit.sh. Runs pytest unit tests and npm tests.
    .EXAMPLE
        Test-GameAgentUnit
    #>
    [CmdletBinding()]
    param()

    $ErrorActionPreference = 'Stop'
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $failed = $false

    Write-Host ''
    Write-GameAgentStatus 'Game Agent - Unit Tests' -Type Info
    Write-Host ('=' * 60)

    # Backend unit tests
    Write-Host ''
    Write-GameAgentStatus 'Backend Unit Tests' -Type Info
    Push-Location (Join-Path $repoRoot 'backend')
    try {
        $venvPython = if ($IsWindows) { '.venv/Scripts/python.exe' } else { '.venv/bin/python' }
        if (-not (Test-Path $venvPython)) {
            Write-GameAgentStatus 'Creating .venv with uv sync...' -Type Info
            uv sync
        }

        & $venvPython -m pytest tests/unit/ -v --tb=short --maxfail=5
        if ($LASTEXITCODE -ne 0) { $failed = $true }

        Write-Host ''
        Write-GameAgentStatus 'Coverage Report' -Type Info
        & $venvPython -m pytest tests/unit/ --cov=src --cov-report=term-missing --cov-report=html:htmlcov -q
        if ($LASTEXITCODE -ne 0) { $failed = $true }
    } finally { Pop-Location }

    # Frontend unit tests
    Write-Host ''
    Write-GameAgentStatus 'Frontend Unit Tests' -Type Info
    Push-Location (Join-Path $repoRoot 'ui')
    try {
        if (-not (Test-Path 'node_modules')) {
            Write-GameAgentStatus 'Installing frontend dependencies...' -Type Info
            npm install
        }
        npm test -- --passWithNoTests --watchAll=false --silent
        if ($LASTEXITCODE -ne 0) { $failed = $true }
    } finally { Pop-Location }

    # PowerShell module (Pester) tests
    $pesterTests = Join-Path $repoRoot 'scripts' 'powershell' 'Tests'
    if (Test-Path $pesterTests) {
        Write-Host ''
        Write-GameAgentStatus 'PowerShell Module Tests (Pester)' -Type Info
        if (-not (Get-Module -ListAvailable Pester)) {
            Write-GameAgentStatus 'Installing Pester 5.x...' -Type Info
            Install-Module -Name Pester -Force -Scope CurrentUser -SkipPublisherCheck
        }
        $pesterResult = Invoke-Pester $pesterTests -Output Detailed -PassThru
        if ($pesterResult.FailedCount -gt 0) { $failed = $true }
    }

    Write-Host ''
    if ($failed) {
        Write-GameAgentStatus 'SOME TESTS FAILED' -Type Error
        throw 'Unit tests failed'
    } else {
        Write-GameAgentStatus 'Unit tests completed!' -Type Success
        Write-Host "Coverage report: backend/htmlcov/index.html" -ForegroundColor Cyan
    }
    Write-Host ''
}
