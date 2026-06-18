#!/bin/bash
# Run Pester tests for the PowerShell GameAgent module
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

if ! command -v pwsh &>/dev/null; then
    echo "❌ PowerShell 7+ (pwsh) is required. Install from: https://github.com/PowerShell/PowerShell/releases"
    exit 1
fi

exec pwsh -NoProfile -Command "
    if (-not (Get-Module -ListAvailable Pester)) {
        Write-Host 'Installing Pester 5.x...' -ForegroundColor Cyan
        Install-Module -Name Pester -Force -Scope CurrentUser -SkipPublisherCheck
    }
    Invoke-Pester '$REPO_ROOT/scripts/powershell/Tests' -Output Detailed
"
