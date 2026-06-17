function Invoke-GameAgentPromptTeardown {
    <# Deletes all game-agent-* managed prompts and cleans .env.local. #>
    [CmdletBinding()]
    param([string]$Region, [string[]]$ProfileArgs)

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $envFile = Join-Path $repoRoot 'backend/.env.local'

    Write-GameAgentStatus 'Tearing down Bedrock Managed Prompts...' -Type Info

    $promptIds = (& aws bedrock-agent list-prompts --region $Region @ProfileArgs `
        --query "promptSummaries[?starts_with(name, 'game-agent-')].id" --output text 2>$null) | Out-String
    $promptIds = $promptIds.Trim()

    if (-not $promptIds) {
        Write-GameAgentStatus 'No game-agent prompts found, skipping' -Type Warning
    } else {
        foreach ($id in ($promptIds -split '\s+')) {
            if (-not $id) { continue }
            $name = (& aws bedrock-agent get-prompt --prompt-identifier $id --region $Region @ProfileArgs `
                --query 'name' --output text 2>$null) | Out-String
            $name = $name.Trim()
            Write-GameAgentStatus "Deleting prompt: $name ($id)..." -Type Info
            & aws bedrock-agent delete-prompt --prompt-identifier $id --region $Region @ProfileArgs 2>$null | Out-Null
        }
    }

    # Clean .env.local
    if (Test-Path $envFile) {
        $content = Get-Content $envFile | Where-Object { $_ -notmatch '^(ORCHESTRATOR|GAMELIFT|EKS|COST)_PROMPT_ARN=' }
        Set-Content $envFile $content
    }
    Write-GameAgentStatus 'Managed Prompts torn down' -Type Success
}
