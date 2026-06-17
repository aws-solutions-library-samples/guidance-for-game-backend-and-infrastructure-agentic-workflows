function Get-GameAgentStatus {
    <#
    .SYNOPSIS
        Gets the status of Game Agent deployment.
    .DESCRIPTION
        Checks AgentCore config, CloudFormation stacks, and dev servers.
        Matches check-deployment.sh behavior.
    .PARAMETER ProjectName
        Project name. Default: "game-agent".
    .PARAMETER Region
        AWS region. Default: "us-west-2".
    .PARAMETER Profile
        AWS CLI profile. Default: reads from ui/.env.local.
    .EXAMPLE
        Get-GameAgentStatus
    #>
    [CmdletBinding()]
    param(
        [string]$ProjectName = 'game-agent',
        [string]$Region = 'us-west-2',
        [string]$Profile
    )

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $uiPath = Join-Path $repoRoot 'ui'
    $backendPath = Join-Path $repoRoot 'backend'

    # Resolve profile
    $resolved = Resolve-GameAgentProfile -Profile $Profile
    $Profile = $resolved.Profile
    $profileArgs = $resolved.ProfileArgs

    Write-GameAgentStatus 'Checking Game Agent status...' -Type Info
    Write-Host ''

    # ── AgentCore Runtime ──
    Write-Host 'AgentCore Runtime:' -ForegroundColor Cyan
    Write-Host ('=' * 60)
    $agentcoreConfig = Join-Path $backendPath '.bedrock_agentcore.yaml'
    $runtimeId = ''
    $runtimeStatus = 'NOT CONFIGURED'
    if (Test-Path $agentcoreConfig) {
        $runtimeId = ((yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' $agentcoreConfig 2>$null) | Out-String).Trim()
        if ($runtimeId -and $runtimeId -ne 'null') {
            $rtInfo = & aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id $runtimeId --region $Region @profileArgs --output json 2>$null
            if ($LASTEXITCODE -eq 0) {
                $rt = $rtInfo | ConvertFrom-Json
                $runtimeStatus = $rt.status
            } else {
                $runtimeStatus = 'NOT FOUND IN AWS'
            }
        }
    }
    $rtColor = if ($runtimeStatus -eq 'ACTIVE') { 'Green' } elseif ($runtimeStatus -like '*NOT*') { 'Gray' } else { 'Yellow' }
    Write-Host "  Runtime ID: $runtimeId" -NoNewline
    Write-Host (' ' * [Math]::Max(1, 30 - $runtimeId.Length)) -NoNewline
    Write-Host $runtimeStatus -ForegroundColor $rtColor
    Write-Host ('=' * 60)
    Write-Host ''

    # ── CloudFormation Stacks ──
    Write-Host 'CloudFormation Stacks:' -ForegroundColor Cyan
    Write-Host ('=' * 60)

    $stacks = @(
        "$ProjectName-infrastructure",
        "$ProjectName-guardrails",
        "$ProjectName-observability",
        "$ProjectName-kb-gamelift",
        "$ProjectName-kb-eks",
        "$ProjectName-kb-cost",
        "$ProjectName-frontend",
        "$ProjectName-security"
    )

    $deployedCount = 0
    foreach ($stackName in $stacks) {
        $info = & aws cloudformation describe-stacks --stack-name $stackName --region $Region @profileArgs --output json 2>$null
        if ($LASTEXITCODE -eq 0) {
            $s = ($info | ConvertFrom-Json).Stacks[0]
            $status = $s.StackStatus
            $color = switch -Wildcard ($status) {
                '*COMPLETE' { 'Green' }
                '*IN_PROGRESS' { 'Yellow' }
                '*FAILED' { 'Red' }
                '*ROLLBACK*' { 'Red' }
                default { 'White' }
            }
            Write-Host "  $stackName" -NoNewline
            Write-Host (' ' * [Math]::Max(1, 40 - $stackName.Length)) -NoNewline
            Write-Host $status -ForegroundColor $color
            if ($status -like '*COMPLETE' -and $status -notlike '*ROLLBACK*') { $deployedCount++ }
        } else {
            Write-Host "  $stackName" -NoNewline
            Write-Host (' ' * [Math]::Max(1, 40 - $stackName.Length)) -NoNewline
            Write-Host 'NOT DEPLOYED' -ForegroundColor Gray
        }
    }

    Write-Host ('=' * 60)
    $totalStacks = $stacks.Count
    $summaryColor = if ($deployedCount -eq $totalStacks) { 'Green' } elseif ($deployedCount -gt 0) { 'Yellow' } else { 'Gray' }
    Write-Host "Deployed: $deployedCount / $totalStacks stacks" -ForegroundColor $summaryColor
    Write-Host ''

    # ── Frontend URL ──
    if ($deployedCount -gt 0) {
        $frontendUrl = & aws cloudformation describe-stacks --stack-name "$ProjectName-frontend" --region $Region @profileArgs `
            --query 'Stacks[0].Outputs[?OutputKey==`ServiceUrl`].OutputValue' --output text 2>$null
        if ($LASTEXITCODE -eq 0 -and $frontendUrl) {
            Write-Host "Frontend: https://$frontendUrl" -ForegroundColor Green
            Write-Host ''
        }
    }

    # ── Dev servers ──
    Write-Host 'Development Servers:' -ForegroundColor Cyan
    Write-Host ('=' * 60)
    foreach ($svc in @(@{Name='Backend'; Port=8080}, @{Name='Frontend'; Port=3000})) {
        $running = Test-GameAgentPort -Port $svc.Port
        Write-Host "  $($svc.Name) (port $($svc.Port))" -NoNewline
        Write-Host (' ' * [Math]::Max(1, 35 - $svc.Name.Length)) -NoNewline
        if ($running) { Write-Host 'RUNNING' -ForegroundColor Green }
        else { Write-Host 'STOPPED' -ForegroundColor Gray }
    }
    Write-Host ('=' * 60)
    Write-Host ''

    # Next steps
    if ($deployedCount -eq 0) {
        Write-Host 'Next steps:' -ForegroundColor Yellow
        Write-Host '  Deploy infrastructure: Deploy-GameAgent'
    } elseif ($deployedCount -eq $totalStacks) {
        Write-Host 'Deployment complete! ✅' -ForegroundColor Green
    }
    Write-Host ''
}
