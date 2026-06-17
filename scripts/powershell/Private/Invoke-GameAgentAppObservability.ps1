function Invoke-GameAgentAppObservability {
    <# Sets retention on auto-created log groups for a runtime. #>
    [CmdletBinding()]
    param([string]$RuntimeId, [string]$Region, [string[]]$ProfileArgs, [string]$ProjectName = 'game-agent', [int]$RetentionDays = 14)

    Write-GameAgentStatus "Setting up observability for runtime: $RuntimeId" -Type Info

    # Helper: set retention if log group exists but has none
    function Set-LogGroupRetention {
        param([string]$LogGroup, [string]$Desc)
        $lg = (& aws logs describe-log-groups --region $Region @ProfileArgs --log-group-name-exact $LogGroup --query 'logGroups[0]' --output json 2>$null) | Out-String
        if ($lg.Trim() -eq 'null' -or -not $lg.Trim()) {
            Write-Host "  $Desc`: log group not found (will be created on first use)"
            return
        }
        $info = $lg | ConvertFrom-Json
        if ($info.retentionInDays -eq $RetentionDays) {
            Write-Host "  $Desc`: already has ${RetentionDays}-day retention"
        } else {
            & aws logs put-retention-policy --log-group-name $LogGroup --retention-in-days $RetentionDays --region $Region @ProfileArgs 2>$null | Out-Null
            Write-Host "  Set ${RetentionDays}-day retention: $LogGroup"
        }
    }

    # Helper: set retention on all log groups matching a prefix that lack retention
    function Set-PrefixRetention {
        param([string]$Prefix, [string]$Desc)
        $groups = (& aws logs describe-log-groups --region $Region @ProfileArgs --log-group-name-prefix $Prefix `
            --query 'logGroups[?!retentionInDays].logGroupName' --output json 2>$null) | ConvertFrom-Json
        if (-not $groups -or $groups.Count -eq 0) {
            Write-Host "  $Desc`: all log groups have retention (or none exist yet)"
            return
        }
        foreach ($lg in $groups) {
            & aws logs put-retention-policy --log-group-name $lg --retention-in-days $RetentionDays --region $Region @ProfileArgs 2>$null | Out-Null
            Write-Host "  Set ${RetentionDays}-day retention: $lg"
        }
    }

    Set-LogGroupRetention "/aws/bedrock-agentcore/runtimes/$RuntimeId-DEFAULT" 'AgentCore Runtime logs'
    Set-LogGroupRetention "/aws/bedrock-agentcore/runtimes/$RuntimeId/adot-rt-logs" 'ADOT runtime logs'
    Set-PrefixRetention "/ecs/$ProjectName-frontend" 'ECS Express logs'
    Set-PrefixRetention '/aws/vendedlogs/bedrock-agentcore/memory' 'AgentCore Memory logs'

    Write-GameAgentStatus 'Application observability setup complete' -Type Success
}
