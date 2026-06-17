function Invoke-GameAgentPromptDeploy {
    <# Deploys Bedrock Managed Prompts via the Python deployer. #>
    [CmdletBinding()]
    param([string]$Region, [string[]]$ProfileArgs)

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $backendPath = Join-Path $repoRoot 'backend'
    $deployScript = Join-Path $repoRoot 'scripts/infrastructure/deploy_prompts.py'
    $envFile = Join-Path $backendPath '.env.local'

    Write-GameAgentStatus 'Deploying Bedrock Managed Prompts...' -Type Info
    Push-Location $backendPath
    try {
        uv run python $deployScript --region $Region --env-file $envFile
        if ($LASTEXITCODE -ne 0) { throw "Prompt deployment failed (exit code $LASTEXITCODE)" }
    } finally { Pop-Location }
    Write-GameAgentStatus 'Managed Prompts deployed' -Type Success
}
