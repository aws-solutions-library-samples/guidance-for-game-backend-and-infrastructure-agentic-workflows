function Invoke-GameAgentAccountObservability {
    <# Sets up account-wide observability: X-Ray Transaction Search, aws/spans log group, resource policies. #>
    [CmdletBinding()]
    param([string]$Region, [string[]]$ProfileArgs)

    Write-GameAgentStatus 'Setting up account-wide observability...' -Type Info
    $accountId = (& aws sts get-caller-identity --query Account --output text --region $Region @ProfileArgs) | Out-String
    $accountId = $accountId.Trim()

    # Step 1: Ensure aws/spans log group exists
    Write-Host '  Checking aws/spans log group...'
    $lgCheck = (& aws logs describe-log-groups --log-group-name-prefix 'aws/spans' --region $Region @ProfileArgs --output json 2>$null) | ConvertFrom-Json
    $hasLg = ($lgCheck.logGroups | Where-Object { $_.logGroupName -eq 'aws/spans' }).Count -gt 0

    if ($hasLg) {
        Write-Host '  aws/spans log group exists'
    } else {
        Write-Host '  aws/spans missing — toggling Transaction Search...'
        $currentDest = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --query 'Destination' --output text 2>$null) | Out-String
        $currentDest = $currentDest.Trim()

        if ($currentDest -eq 'CloudWatchLogs') {
            & aws xray update-trace-segment-destination --destination XRay --region $Region @ProfileArgs 2>$null | Out-Null
            $waited = 0; while ($waited -lt 300) {
                $s = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --query 'Status' --output text 2>$null) | Out-String
                if ($s.Trim() -eq 'ACTIVE') { break }; Start-Sleep 10; $waited += 10
            }
        }
        & aws xray update-trace-segment-destination --destination CloudWatchLogs --region $Region @ProfileArgs 2>$null | Out-Null
        $waited = 0; $created = $false
        while ($waited -lt 300) {
            $s = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --query 'Status' --output text 2>$null) | Out-String
            $lgc = (& aws logs describe-log-groups --log-group-name-prefix 'aws/spans' --region $Region @ProfileArgs --output json 2>$null) | ConvertFrom-Json
            $hasNow = ($lgc.logGroups | Where-Object { $_.logGroupName -eq 'aws/spans' }).Count -gt 0
            if ($s.Trim() -eq 'ACTIVE' -and $hasNow) { $created = $true; break }
            Start-Sleep 10; $waited += 10
        }
        if ($created) { Write-Host '  aws/spans log group created' }
        else { Write-GameAgentStatus 'aws/spans could not be created. OTEL trace export may fail.' -Type Warning }
    }

    # Step 2: CloudWatch Logs resource policy
    Write-Host '  Ensuring CloudWatch Logs resource policy...'
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
    & aws logs put-resource-policy --policy-name TransactionSearchXRayAccess --policy-document $policy --region $Region @ProfileArgs 2>$null | Out-Null

    # Step 3: Ensure destination is CloudWatch Logs
    $dest = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --output json 2>$null) | Out-String
    if ($dest -match 'CloudWatchLogs') { Write-Host '  X-Ray trace destination already set' }
    else {
        & aws xray update-trace-segment-destination --destination CloudWatchLogs --region $Region @ProfileArgs 2>$null | Out-Null
    }

    # Step 4: 1% sampling
    Write-Host '  Configuring span sampling (1% free tier)...'
    & aws xray update-indexing-rule --name 'Default' --rule '{"Probabilistic": {"DesiredSamplingPercentage": 1}}' --region $Region @ProfileArgs 2>$null | Out-Null

    Write-GameAgentStatus 'Account-wide observability configured' -Type Success
}
