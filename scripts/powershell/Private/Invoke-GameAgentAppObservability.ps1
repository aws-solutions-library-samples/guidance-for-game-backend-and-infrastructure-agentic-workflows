function Invoke-GameAgentAppObservability {
    <# Sets retention on auto-created log groups for a runtime. Mirrors scripts/infrastructure/setup-app-observability.sh. #>
    [CmdletBinding()]
    param([string]$RuntimeId, [string]$Region, [string[]]$ProfileArgs, [string]$ProjectName = 'game-agent', [int]$RetentionDays = 14)

    # The frontend is the ECS Express service "$ProjectName-frontend". Its
    # template (02-frontend-ecs-express.yaml) sets no cluster, so the service
    # runs in the 'default' cluster, and ECS Express names the service's log
    # group /aws/ecs/<cluster>/<service>-<generated suffix>.
    $frontendCluster = 'default'
    $frontendService = "$ProjectName-frontend"
    $frontendLogGroupPrefix = "/aws/ecs/$frontendCluster/$frontendService"

    # The runtime ID becomes part of a JMESPath filter below. AgentCore runtime
    # IDs use only letters, digits, underscores and hyphens; refuse anything else.
    if ($RuntimeId -notmatch '^[A-Za-z0-9_-]+\z') { throw 'Invalid runtime ID' }

    Write-GameAgentStatus "Setting up observability for runtime: $RuntimeId" -Type Info

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

    # Helper: return the frontend's log group, read from the awslogs-group
    # option of the service's current (PRIMARY) task definition, or $null when
    # the service, the task definition, or a valid CloudWatch Logs group name
    # cannot be read. The calls are wrapped so a redirected native stderr cannot
    # surface as a terminating error under an inherited 'Stop' preference
    # (PowerShell 7.0/7.1).
    function Resolve-FrontendLogGroup {
        try {
            $taskDefinition = (& aws ecs describe-services --region $Region @ProfileArgs `
                --cluster $frontendCluster --services $frontendService `
                --query "services[?status=='ACTIVE'] | [0].deployments[?status=='PRIMARY'] | [0].taskDefinition" `
                --output text 2>$null) | Out-String
        } catch { return $null }
        if ($LASTEXITCODE -ne 0) { return $null }
        $taskDefinition = $taskDefinition.Trim()
        if (-not $taskDefinition -or $taskDefinition -eq 'None') { return $null }

        # Read the whole options map as JSON: selecting the 'awslogs-group' key
        # directly needs a double-quoted JMESPath identifier, which PowerShell
        # before 7.3 does not pass intact to a native command.
        try {
            $optionsJson = (& aws ecs describe-task-definition --region $Region @ProfileArgs `
                --task-definition $taskDefinition `
                --query "taskDefinition.containerDefinitions[?logConfiguration.logDriver=='awslogs'] | [0].logConfiguration.options" `
                --output json 2>$null) | Out-String
        } catch { return $null }
        if ($LASTEXITCODE -ne 0) { return $null }
        try { $options = $optionsJson | ConvertFrom-Json } catch { return $null }
        if ($null -eq $options) { return $null }
        $logGroup = "$($options.'awslogs-group')"
        if ($logGroup.Length -gt 512 -or $logGroup -notmatch '^[A-Za-z0-9._/#-]+\z') { return $null }
        return $logGroup
    }

    # Helper: set retention on one log group if it has none (an existing policy
    # is kept). Stops on a failed update, as the shell script does under set -e.
    function Set-RetentionIfUnset {
        param([string]$LogGroup, [string]$Desc)
        $groups = $null
        try {
            $raw = (& aws logs describe-log-groups --region $Region @ProfileArgs --log-group-name-prefix $LogGroup `
                --query "logGroups[?logGroupName=='$LogGroup']" --output json 2>$null) | Out-String
            if ($LASTEXITCODE -eq 0) { $groups = @($raw | ConvertFrom-Json) }
        } catch { $groups = $null }
        if ($null -eq $groups) {
            Write-GameAgentStatus "$Desc`: could not read $LogGroup; retention unchanged" -Type Warning
            return
        }

        $group = $groups | Where-Object { $_.logGroupName -eq $LogGroup } | Select-Object -First 1
        if (-not $group) {
            Write-Host "  $Desc`: log group not found: $LogGroup"
        } elseif ($group.retentionInDays) {
            Write-Host "  $Desc`: $LogGroup already has $($group.retentionInDays)-day retention"
        } else {
            try {
                & aws logs put-retention-policy --log-group-name $LogGroup --retention-in-days $RetentionDays --region $Region @ProfileArgs 2>$null | Out-Null
                $updated = ($LASTEXITCODE -eq 0)
            } catch { $updated = $false }
            if (-not $updated) { throw "Could not set ${RetentionDays}-day retention on $LogGroup" }
            Write-Host "  $Desc`: set ${RetentionDays}-day retention: $LogGroup"
        }
    }

    # Retention is set only when a group has none; an existing policy is kept.
    Set-RetentionIfUnset "/aws/bedrock-agentcore/runtimes/$RuntimeId-DEFAULT" 'AgentCore Runtime logs'
    Set-RetentionIfUnset "/aws/bedrock-agentcore/runtimes/$RuntimeId/adot-rt-logs" 'ADOT runtime logs'

    # The frontend's log group comes from the service's task definition; if that
    # cannot be read, check every log group under the ECS Express name prefix.
    $frontendDesc = 'Frontend (ECS Express) logs'
    $frontendLogGroup = Resolve-FrontendLogGroup
    if ($frontendLogGroup) {
        Set-RetentionIfUnset $frontendLogGroup $frontendDesc
    } else {
        Write-Host "  $frontendDesc`: no log group read from service $frontendService; checking $frontendLogGroupPrefix*"
        Set-PrefixRetention $frontendLogGroupPrefix $frontendDesc
    }

    Set-PrefixRetention '/aws/vendedlogs/bedrock-agentcore/memory' 'AgentCore Memory logs'

    Write-GameAgentStatus 'Application observability setup complete' -Type Success
}
