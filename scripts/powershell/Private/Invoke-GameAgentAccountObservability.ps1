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
        # Project-owned name for the shared Logs resource policy. The default is
        # read from the GBAW-prefixed environment variable so it cannot collide
        # with an unrelated environment value.
        [string]$ResourcePolicyName = $(
            if ($env:GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME) {
                $env:GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME
            } else {
                'GameAgentTransactionSearchXRayAccess'
            }
        ),
        # Optional explicit X-Ray default indexing percentage. The default rule is
        # account-wide, so it is left unchanged unless this is set to [0, 100].
        [string]$DefaultIndexingPercent = $env:GBAW_XRAY_DEFAULT_INDEXING_PERCENT,
        # Bounded poll for the trace destination to reach ACTIVE after an enable.
        # Sourced from the same GBAW_OBSERVABILITY_ACTIVE_* environment variables
        # the shell path reads; a non-numeric or out-of-range value falls back to
        # the default so a bad knob never aborts the opt-in mid-run.
        [int]$ActiveMaxAttempts = $(
            $parsed = 0
            if ([int]::TryParse($env:GBAW_OBSERVABILITY_ACTIVE_MAX_ATTEMPTS, [ref]$parsed) -and $parsed -ge 1) {
                $parsed
            } else {
                30
            }
        ),
        [int]$ActiveRetrySeconds = $(
            $parsed = 0
            if ([int]::TryParse($env:GBAW_OBSERVABILITY_ACTIVE_RETRY_SECONDS, [ref]$parsed) -and $parsed -ge 0) {
                $parsed
            } else {
                10
            }
        )
    )

    $legacyPolicyName = 'TransactionSearchXRayAccess'

    Write-GameAgentStatus "Account-wide observability (region $Region)..." -Type Info

    # Returns XRay | CloudWatchLogs, or UNREADABLE if the read did not succeed.
    function Get-TraceDestination {
        $raw = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --query 'Destination' --output text 2>$null)
        if ($LASTEXITCODE -ne 0) { return 'UNREADABLE' }
        $dest = ("$raw").Trim()
        if ($dest -eq 'XRay' -or $dest -eq 'CloudWatchLogs') { return $dest }
        return 'UNREADABLE'
    }

    function Get-TraceStatus {
        $raw = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --query 'Status' --output text 2>$null)
        if ($LASTEXITCODE -ne 0) { return '' }
        return ("$raw").Trim()
    }

    function Test-SpansLogGroup {
        $raw = (& aws logs describe-log-groups --log-group-name-prefix 'aws/spans' --region $Region @ProfileArgs --output json 2>$null)
        if ($LASTEXITCODE -ne 0) { return $false }
        try { $lgCheck = $raw | ConvertFrom-Json } catch { return $false }
        return (($lgCheck.logGroups | Where-Object { $_.logGroupName -eq 'aws/spans' }).Count -gt 0)
    }

    # Current Default indexing sampling percentage, or UNREADABLE.
    function Get-DefaultIndexingPercent {
        $raw = (& aws xray get-indexing-rules --region $Region @ProfileArgs `
            --query "IndexingRules[?Name=='Default'].Rule.Probabilistic.DesiredSamplingPercentage | [0]" `
            --output text 2>$null)
        if ($LASTEXITCODE -ne 0) { return 'UNREADABLE' }
        $value = ("$raw").Trim()
        if ([string]::IsNullOrWhiteSpace($value) -or $value -eq 'None') { return 'UNREADABLE' }
        return $value
    }

    # Prints which shared settings MAY change on opt-in.
    function Write-AccountScope {
        Write-Host "        1. X-Ray trace segment destination  -> CloudWatchLogs (region $Region)"
        Write-Host '        2. X-Ray default indexing rule       -> unchanged unless'
        Write-Host '           GBAW_XRAY_DEFAULT_INDEXING_PERCENT is set'
        Write-Host "        3. CloudWatch Logs resource policy   -> $ResourcePolicyName"
        Write-Host "           (grants xray.amazonaws.com logs:PutLogEvents on 'aws/spans' and"
        Write-Host "            '/aws/application-signals/data')"
    }

    # Runs a single opt-in mutation, checking the CLI exit code and throwing a
    # bounded, public-safe message (no account IDs or ARNs) on failure.
    function Invoke-ObsMutation {
        param([string]$Label, [string[]]$Arguments)
        $output = (& aws @Arguments @ProfileArgs 2>&1)
        if ($LASTEXITCODE -ne 0) {
            $code = 'unknown'
            if ("$output" -match '\(([A-Za-z0-9]+)\)') { $code = $Matches[1] }
            throw "$Label failed ($code)"
        }
        return $output
    }

    if (-not $ConfigureAccountObservability) {
        $destination = Get-TraceDestination
        $destinationDisplay = if ($destination -eq 'UNREADABLE') { 'unknown (read failed)' } else { $destination }
        $supported = ($destination -eq 'CloudWatchLogs') -and (Test-SpansLogGroup)
        Write-Host '  Default mode: no account-wide X-Ray or CloudWatch Logs changes will be made.'
        Write-Host "      Current X-Ray trace segment destination: $destinationDisplay"
        if ($supported) {
            Write-Host '  Account already supports Transaction Search (destination CloudWatchLogs, aws/spans present).'
            Write-Host '      Runtime trace delivery will be configured and verified in a later step.'
        } else {
            Write-GameAgentStatus 'Account does NOT yet appear to support Transaction Search for the runtime.' -Type Warning
            Write-Host '      X-Ray Transaction Search spans require destination CloudWatchLogs and the'
            Write-Host "      AWS-reserved 'aws/spans' log group. The opt-in WOULD change these shared,"
            Write-Host '      account-wide settings (every X-Ray / Transaction Search consumer):'
            Write-AccountScope
            Write-Host '      To let the deployment configure them, re-run with the opt-in:'
            Write-Host '          Deploy-GameAgent -ConfigureAccountObservability'
            Write-Host '      Or enable Transaction Search once in the AWS X-Ray / CloudWatch console.'
        }
        Write-Host '  Continuing deployment (default account-observability is non-blocking).'
        return
    }

    # ── Opt-in mutation path (state-preserving) ──
    #
    # Read and validate everything BEFORE the first write, so a refusal or a bad
    # input never leaves a shared, account-wide setting half-changed. This
    # mirrors the ordering in scripts/infrastructure/setup-account-observability.sh.

    # Account ID for the resource-policy scope. Merge stderr with 2>&1 to capture
    # native CLI diagnostics, then drop the ErrorRecord items before Out-String,
    # as New-GameAgentAwsInvoker does, so a warning printed to stderr on a
    # successful call cannot be mistaken for part of the account ID.
    $identityMerged = (& aws sts get-caller-identity --query Account --output text --region $Region @ProfileArgs 2>&1)
    $identityStdout = @($identityMerged | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
    $accountId = ($identityStdout | Out-String).Trim()
    # An empty or malformed account would write a resource policy that never matches.
    if ($LASTEXITCODE -ne 0 -or $accountId -notmatch '^\d{12}$') {
        throw 'identity lookup returned no account ID; not changing shared settings'
    }

    Write-GameAgentStatus 'Account-wide observability opt-in is ENABLED.' -Type Warning
    Write-Host '      The following SHARED, account-wide settings may be created or changed:'
    Write-AccountScope
    Write-Host '      These affect every X-Ray / Transaction Search consumer in the account.'
    Write-Host ''

    # Read the trace destination and refuse if it cannot be read.
    $destination = Get-TraceDestination
    Write-Host "  Current X-Ray trace segment destination (preserved unless changed below): $destination"
    if ($destination -eq 'UNREADABLE') {
        throw 'Cannot read the current trace destination; not changing it'
    }

    # Validate the requested indexing percentage before any write.
    $requestedPercent = $null
    if (-not [string]::IsNullOrWhiteSpace($DefaultIndexingPercent)) {
        $parsedPercent = 0
        if (-not [int]::TryParse($DefaultIndexingPercent, [ref]$parsedPercent) -or $parsedPercent -lt 0 -or $parsedPercent -gt 100) {
            throw "GBAW_XRAY_DEFAULT_INDEXING_PERCENT='$DefaultIndexingPercent' must be an integer in [0, 100]"
        }
        $requestedPercent = $parsedPercent
    }

    # Read the indexing rule. It is only a hard requirement when a change is
    # requested; a read failure with no change requested is non-fatal.
    $currentPercent = Get-DefaultIndexingPercent
    if ($null -ne $requestedPercent -and $currentPercent -eq 'UNREADABLE') {
        throw 'Unable to read the current X-Ray default indexing rule; not changing it'
    }

    # Read the resource-policy list and decide the action before writing. Only
    # an Effect: Allow statement counts as an existing grant.
    $policiesRaw = (& aws logs describe-resource-policies --region $Region @ProfileArgs --output json 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read existing CloudWatch Logs resource policies' }
    $existingMatch = $null
    $policyCount = 0
    $owned = $false
    try {
        $policies = ($policiesRaw | ConvertFrom-Json).resourcePolicies
        foreach ($policy in @($policies)) {
            $policyCount++
            if ($policy.policyName -eq $ResourcePolicyName) { $owned = $true }
            try { $doc = $policy.policyDocument | ConvertFrom-Json } catch { continue }
            foreach ($statement in @($doc.Statement)) {
                if ($statement.Effect -ne 'Allow') { continue }
                $services = @($statement.Principal.Service)
                $actions = @($statement.Action)
                $resources = @($statement.Resource)
                if (($services -contains 'xray.amazonaws.com') -and
                    ($actions -contains 'logs:PutLogEvents') -and
                    ($resources | Where-Object { "$_" -like '*aws/spans*' })) {
                    $existingMatch = $policy.policyName
                    break
                }
            }
            if ($existingMatch) { break }
        }
    } catch {
        throw 'Unable to read existing CloudWatch Logs resource policies'
    }

    # The 10-policy limit only matters when a policy would be ADDED. Updating a
    # policy that already uses the project-owned name is an upsert, not an add.
    if (-not $existingMatch -and -not $owned -and $policyCount -ge 10) {
        throw "The account already has the maximum of 10 CloudWatch Logs resource policies in this region; cannot create '$ResourcePolicyName'. Remove an unused policy (for example a legacy '$legacyPolicyName') and re-run."
    }

    # ── All reads and validation passed; perform the writes in order. ──

    # Resource policy: reconcile against existing policies (never duplicate).
    Write-Host "  Ensuring project-owned CloudWatch Logs resource policy ($ResourcePolicyName)..."
    if ($existingMatch -and $existingMatch -ne $ResourcePolicyName) {
        Write-Host "  A resource policy ('$existingMatch') already grants xray.amazonaws.com"
        Write-Host "      logs:PutLogEvents on 'aws/spans' - leaving it in place and skipping."
        Write-Host '      See docs/OBSERVABILITY_ADOT_EXPORTER.md for when the legacy policy can be removed.'
    } else {
        $policyDocument = @{
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
        Invoke-ObsMutation 'resource policy' @('logs', 'put-resource-policy', '--policy-name', $ResourcePolicyName, '--policy-document', $policyDocument, '--region', $Region) | Out-Null
        Write-Host '  CloudWatch Logs resource policy configured'
    }

    # ── Trace destination: additive enable only; never a disable/re-enable toggle ──
    if ($destination -eq 'CloudWatchLogs') {
        Write-Host '  X-Ray trace destination already CloudWatchLogs - left unchanged'
    } elseif ($destination -eq 'XRay') {
        Write-Host '  Enabling Transaction Search (destination -> CloudWatchLogs)...'
        Write-Host "     Rollback: aws xray update-trace-segment-destination --destination $destination --region $Region"
        Invoke-ObsMutation 'trace destination' @('xray', 'update-trace-segment-destination', '--destination', 'CloudWatchLogs', '--region', $Region) | Out-Null
        Write-Host '  X-Ray trace destination set to CloudWatchLogs'
        # Bounded poll until the destination reports ACTIVE.
        $active = $false
        for ($attempt = 1; $attempt -le $ActiveMaxAttempts; $attempt++) {
            if ((Get-TraceStatus) -eq 'ACTIVE') { $active = $true; break }
            if ($attempt -lt $ActiveMaxAttempts -and $ActiveRetrySeconds -gt 0) { Start-Sleep -Seconds $ActiveRetrySeconds }
        }
        if ($active) {
            Write-Host '  Trace segment destination is ACTIVE'
        } else {
            Write-GameAgentStatus 'Trace segment destination did not reach ACTIVE within the wait budget; it may still converge shortly.' -Type Warning
        }
    } else {
        Write-GameAgentStatus "Unexpected trace destination '$destination'; leaving it unchanged." -Type Warning
    }

    # ── Default indexing rule: change only on explicit request. The current
    #    value was read above; compare numerically so an integer rule reported
    #    as e.g. '1.0' is not re-sent for a requested '1'. ──
    if ($null -eq $requestedPercent) {
        Write-Host "  X-Ray default indexing rule left unchanged (current: ${currentPercent}% sampling)."
        Write-Host '      Set GBAW_XRAY_DEFAULT_INDEXING_PERCENT to change this shared setting.'
    } else {
        $currentNumeric = 0.0
        $currentParsed = [double]::TryParse($currentPercent, [ref]$currentNumeric)
        if ($currentParsed -and [double]$requestedPercent -eq $currentNumeric) {
            Write-Host "  X-Ray default indexing rule already ${currentPercent}% - left unchanged"
        } else {
            Write-Host "  Setting X-Ray default indexing rule to ${requestedPercent}% sampling..."
            Write-Host "     Rollback: aws xray update-indexing-rule --name Default --rule '{""Probabilistic"":{""DesiredSamplingPercentage"":${currentPercent}}}' --region $Region"
            $rule = "{""Probabilistic"": {""DesiredSamplingPercentage"": ${requestedPercent}}}"
            Invoke-ObsMutation 'indexing rule' @('xray', 'update-indexing-rule', '--name', 'Default', '--rule', $rule, '--region', $Region) | Out-Null
            Write-Host "  X-Ray default indexing rule set to ${requestedPercent}%"
        }
    }

    if (Test-SpansLogGroup) {
        Write-Host '  aws/spans log group present'
    } else {
        Write-GameAgentStatus 'aws/spans log group not visible yet; AWS creates it shortly after enabling Transaction Search.' -Type Warning
    }

    Write-GameAgentStatus 'Account-wide observability configured (opt-in)' -Type Success
}
