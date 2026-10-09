function Invoke-GameAgentAccountObservability {
    <#
    .SYNOPSIS
        Scoped, opt-in, state-preserving account-wide observability setup.
    .DESCRIPTION
        X-Ray Transaction Search and its supporting CloudWatch Logs configuration
        (trace segment destination, default indexing rule, shared Logs resource
        policy) are account-wide, cross-application settings. This function only
        mutates them when -ConfigureAccountObservability is supplied. The default
        path is read-only: it reports current state, detects whether the account
        already supports what the runtime needs, and prints the opt-in instruction
        if it does not, WITHOUT failing the deployment for that reason alone.

        Mirrors scripts/infrastructure/setup-account-observability.sh.
    #>
    [CmdletBinding()]
    param(
        [string]$Region,
        [string[]]$ProfileArgs,
        [switch]$ConfigureAccountObservability,
        [string]$ResourcePolicyName = 'GameAgentTransactionSearchXRayAccess'
    )

    Write-GameAgentStatus "Account-wide observability (region $Region)..." -Type Info

    function Get-TraceDestination {
        $dest = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --query 'Destination' --output text 2>$null) | Out-String
        $dest = $dest.Trim()
        if ([string]::IsNullOrWhiteSpace($dest)) { return 'NOT_CONFIGURED' }
        return $dest
    }

    function Test-SpansLogGroup {
        $lgCheck = (& aws logs describe-log-groups --log-group-name-prefix 'aws/spans' --region $Region @ProfileArgs --output json 2>$null) | ConvertFrom-Json
        return (($lgCheck.logGroups | Where-Object { $_.logGroupName -eq 'aws/spans' }).Count -gt 0)
    }

    if (-not $ConfigureAccountObservability) {
        $destination = Get-TraceDestination
        $supported = ($destination -eq 'CloudWatchLogs') -and (Test-SpansLogGroup)
        Write-Host '  Default mode: no account-wide X-Ray or CloudWatch Logs changes will be made.'
        Write-Host "      Current X-Ray trace segment destination: $destination"
        if ($supported) {
            Write-Host '  Account already supports Transaction Search (destination CloudWatchLogs, aws/spans present).'
            Write-Host '      Runtime trace delivery will be configured and verified in a later step.'
        } else {
            Write-GameAgentStatus 'Account does NOT yet appear to support Transaction Search for the runtime.' -Type Warning
            Write-Host '      To let the deployment configure the shared settings, re-run with the opt-in:'
            Write-Host '          Deploy-GameAgent -ConfigureAccountObservability'
            Write-Host '      Or enable Transaction Search once in the AWS X-Ray / CloudWatch console.'
        }
        Write-Host '  Continuing deployment (default account-observability is non-blocking).'
        return
    }

    # ── Opt-in mutation path (state-preserving) ──
    $accountId = ((& aws sts get-caller-identity --query Account --output text --region $Region @ProfileArgs) | Out-String).Trim()

    Write-GameAgentStatus 'Account-wide observability opt-in is ENABLED.' -Type Warning
    Write-Host '      The following SHARED, account-wide settings may be created or changed:'
    Write-Host "        1. X-Ray trace segment destination  -> CloudWatchLogs (region $Region)"
    Write-Host '        2. X-Ray default indexing rule       -> 1% probabilistic sampling'
    Write-Host "        3. CloudWatch Logs resource policy   -> $ResourcePolicyName"
    Write-Host '      These affect every X-Ray / Transaction Search consumer in the account.'
    Write-Host ''

    $destination = Get-TraceDestination
    Write-Host "  Current X-Ray trace segment destination (preserved unless changed below): $destination"

    # Resource policy uses a project-owned name, so it never overwrites an unrelated policy.
    Write-Host "  Ensuring project-owned CloudWatch Logs resource policy ($ResourcePolicyName)..."
    $policy = @{
        Version = '2012-10-17'
        Statement = @(@{
            Sid = 'TransactionSearchXRayAccess'
            Effect = 'Allow'
            Principal = @{ Service = 'xray.amazonaws.com' }
            Action = 'logs:PutLogEvents'
            Resource = @(
                "arn:aws:logs:${Region}:${accountId}:log-group:aws/spans:*",
                "arn:aws:logs:${Region}:${accountId}:log-group:/aws/application-signals/data:*"
            )
            Condition = @{
                ArnLike = @{ 'aws:SourceArn' = "arn:aws:xray:${Region}:${accountId}:*" }
                StringEquals = @{ 'aws:SourceAccount' = $accountId }
            }
        })
    } | ConvertTo-Json -Depth 10 -Compress
    & aws logs put-resource-policy --policy-name $ResourcePolicyName --policy-document $policy --region $Region @ProfileArgs | Out-Null

    # Enable Transaction Search only if not already enabled. Additive enable, never a disable/re-enable toggle.
    if ($destination -eq 'CloudWatchLogs') {
        Write-Host '  X-Ray trace destination already CloudWatchLogs — left unchanged'
    } elseif ($destination -eq 'NOT_CONFIGURED' -or $destination -eq 'XRay') {
        Write-Host '  Enabling Transaction Search (destination -> CloudWatchLogs)...'
        Write-Host "     Rollback: aws xray update-trace-segment-destination --destination $destination --region $Region"
        & aws xray update-trace-segment-destination --destination CloudWatchLogs --region $Region @ProfileArgs | Out-Null
        Write-Host '  X-Ray trace destination set to CloudWatchLogs'
    } else {
        Write-GameAgentStatus "Unexpected trace destination '$destination'; leaving it unchanged." -Type Warning
    }

    Write-Host '  Configuring default X-Ray indexing rule (1% sampling, free tier)...'
    Write-Host "     Rollback: review 'aws xray get-indexing-rules' and restore the prior DesiredSamplingPercentage."
    & aws xray update-indexing-rule --name 'Default' --rule '{"Probabilistic": {"DesiredSamplingPercentage": 1}}' --region $Region @ProfileArgs | Out-Null
    Write-Host '  X-Ray indexing rule configured'

    if (Test-SpansLogGroup) {
        Write-Host '  aws/spans log group present'
    } else {
        Write-GameAgentStatus 'aws/spans log group not visible yet; AWS creates it shortly after enabling Transaction Search.' -Type Warning
    }

    Write-GameAgentStatus 'Account-wide observability configured (opt-in)' -Type Success
}
