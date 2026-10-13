function Invoke-GameAgentTransactionSearchPreflight {
    <#
    .SYNOPSIS
        Read-only preflight that predicts the AgentCore starter toolkit's
        account-wide Transaction Search changes before `agentcore launch`.
    .DESCRIPTION
        Independently of this repository's opt-in, `agentcore launch` runs the
        starter toolkit's enable_transaction_search_if_needed whenever runtime
        observability is enabled (the same switch that selects the
        opentelemetry-instrument entrypoint). In toolkit 0.3.11 there is no flag
        to keep that OTEL entrypoint while skipping only this account-wide step,
        so the toolkit may change SHARED, account-wide X-Ray / CloudWatch Logs
        settings during launch. This function predicts exactly which of those
        changes the toolkit WOULD make, using the toolkit's own conditions, so
        the deployment can warn (and optionally stop) before the launch.

        Mirrors run_transaction_search_preflight in
        scripts/infrastructure/setup-account-observability.sh. The toolkit's
        three conditions (services/xray.py), reproduced here read-only:
          1. resource policy  - created iff NO CloudWatch Logs resource policy
             named literally 'TransactionSearchXRayAccess' exists (NAME only, so
             this project's 'GameAgentTransactionSearchXRayAccess' does not
             satisfy it: the toolkit still adds its own policy).
          2. trace destination - set to CloudWatchLogs iff not already so.
          3. indexing rule     - a Default rule is created iff NO X-Ray indexing
             rule named 'Default' exists.

        Returns $true when it is safe to launch (no change, or the operator
        accepted the warning) and $false when the fail-closed stop switch is set
        and the toolkit would (or might) change account-wide settings. A read
        failure is treated as a possible change (UNKNOWN), never as "no change",
        so the stop switch halts on it too.
    #>
    [CmdletBinding()]
    param(
        [string]$Region,
        [string[]]$ProfileArgs,
        # Fail-closed stop switch. Only the exact value 'true' stops the deploy
        # before `agentcore launch`; any other value is treated as off. Mirrors
        # the shell GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE environment knob.
        [switch]$StopOnAccountObservabilityChange,
        # Project-owned policy name, used only to annotate the warning when the
        # project grant is present but the toolkit will still add its own.
        [string]$ResourcePolicyName = $(
            if ($env:GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME) {
                $env:GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME
            } else {
                'GameAgentTransactionSearchXRayAccess'
            }
        )
    )

    $toolkitPolicyName = 'TransactionSearchXRayAccess'

    # Returns 'present', 'absent', or 'unreadable' for a resource policy named
    # literally 'TransactionSearchXRayAccess' (the toolkit's name-only check).
    function Get-ToolkitResourcePolicyPresence {
        $raw = (& aws logs describe-resource-policies --region $Region @ProfileArgs --output json 2>$null)
        if ($LASTEXITCODE -ne 0) { return 'unreadable' }
        try { $parsed = $raw | ConvertFrom-Json } catch { return 'unreadable' }
        foreach ($policy in @($parsed.resourcePolicies)) {
            if ($policy.policyName -eq $toolkitPolicyName) { return 'present' }
        }
        return 'absent'
    }

    # Returns $true if any resource policy already grants xray.amazonaws.com
    # logs:PutLogEvents on an 'aws/spans' log group (Effect Allow), regardless of
    # name. Used only to annotate the warning.
    function Test-ProjectSpansGrant {
        $raw = (& aws logs describe-resource-policies --region $Region @ProfileArgs --output json 2>$null)
        if ($LASTEXITCODE -ne 0) { return $false }
        try { $parsed = $raw | ConvertFrom-Json } catch { return $false }
        foreach ($policy in @($parsed.resourcePolicies)) {
            try { $doc = $policy.policyDocument | ConvertFrom-Json } catch { continue }
            foreach ($statement in @($doc.Statement)) {
                if ($statement.Effect -ne 'Allow') { continue }
                $services = @($statement.Principal.Service)
                $actions = @($statement.Action)
                $resources = @($statement.Resource)
                if (($services -contains 'xray.amazonaws.com') -and
                    ($actions -contains 'logs:PutLogEvents') -and
                    ($resources | Where-Object { "$_" -like '*aws/spans*' })) {
                    return $true
                }
            }
        }
        return $false
    }

    # Returns XRay | CloudWatchLogs, or UNREADABLE.
    function Get-PreflightTraceDestination {
        $raw = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --query 'Destination' --output text 2>$null)
        if ($LASTEXITCODE -ne 0) { return 'UNREADABLE' }
        $dest = ("$raw").Trim()
        if ($dest -eq 'XRay' -or $dest -eq 'CloudWatchLogs') { return $dest }
        return 'UNREADABLE'
    }

    # Returns 'present', 'absent', or 'unreadable' for a Default indexing rule
    # (the toolkit keys off a rule NAMED 'Default' existing).
    function Get-DefaultIndexingRulePresence {
        $raw = (& aws xray get-indexing-rules --region $Region @ProfileArgs --output json 2>$null)
        if ($LASTEXITCODE -ne 0) { return 'unreadable' }
        try { $parsed = $raw | ConvertFrom-Json } catch { return 'unreadable' }
        foreach ($rule in @($parsed.IndexingRules)) {
            if ($rule.Name -eq 'Default') { return 'present' }
        }
        return 'absent'
    }

    Write-Host '  Preflight: predicting AgentCore starter toolkit Transaction Search changes...'
    Write-Host "      (the toolkit runs these during 'agentcore launch' when runtime"
    Write-Host '       observability is enabled; they are account-wide and shared)'

    $changes = [System.Collections.Generic.List[string]]::new()
    $unknown = $false

    # 1. Resource policy (name-only check).
    $policyPresence = Get-ToolkitResourcePolicyPresence
    switch ($policyPresence) {
        'present' {
            Write-Host '      - CloudWatch Logs resource policy: no change'
            Write-Host "        (a policy named '$toolkitPolicyName' already exists)"
        }
        'absent' {
            $changes.Add("Adds a CloudWatch Logs resource policy named '$toolkitPolicyName'")
            Write-Host "      - CloudWatch Logs resource policy: WOULD ADD '$toolkitPolicyName'"
            Write-Host "        (grants xray.amazonaws.com logs:PutLogEvents on 'aws/spans' and"
            Write-Host "         '/aws/application-signals/data')"
        }
        default {
            $unknown = $true
            Write-Host '      - CloudWatch Logs resource policy: UNKNOWN (could not read resource policies)'
        }
    }

    # 2. Trace segment destination.
    $destination = Get-PreflightTraceDestination
    if ($destination -eq 'CloudWatchLogs') {
        Write-Host '      - X-Ray trace segment destination: no change (already CloudWatchLogs)'
    } elseif ($destination -eq 'UNREADABLE') {
        $unknown = $true
        Write-Host '      - X-Ray trace segment destination: UNKNOWN (could not read destination)'
    } else {
        $changes.Add("Sets the X-Ray trace segment destination to CloudWatchLogs (region $Region)")
        Write-Host "      - X-Ray trace segment destination: WOULD CHANGE $destination -> CloudWatchLogs"
        Write-Host "        (region $Region; shared by every X-Ray caller)"
    }

    # 3. Default indexing rule.
    $indexingPresence = Get-DefaultIndexingRulePresence
    switch ($indexingPresence) {
        'present' {
            Write-Host "      - X-Ray default indexing rule: no change (a 'Default' rule already exists)"
        }
        'unreadable' {
            $unknown = $true
            Write-Host '      - X-Ray default indexing rule: UNKNOWN (could not read indexing rules)'
        }
        default {
            $changes.Add("Creates the X-Ray 'Default' indexing rule (1% probabilistic sampling)")
            Write-Host "      - X-Ray default indexing rule: WOULD CREATE a 'Default' rule (1% sampling)"
        }
    }

    # Classify: 0 no change, 1 would change, 2 unknown (possible change).
    if ($unknown) {
        $wouldChange = 2
        Write-GameAgentStatus "Preflight could not read one or more account settings; the toolkit's effect is UNKNOWN. Treating it as a possible shared change (not 'no change')." -Type Warning
    } elseif ($changes.Count -eq 0) {
        $wouldChange = 0
        Write-GameAgentStatus 'Preflight: the AgentCore starter toolkit would make NO account-wide X-Ray or CloudWatch Logs change.' -Type Success
    } else {
        $wouldChange = 1
        Write-Host ''
        Write-GameAgentStatus "WARNING: 'agentcore launch' WILL change SHARED, account-wide settings via the AgentCore starter toolkit (NOT this repository's opt-in). These affect every X-Ray / Transaction Search consumer in the account:" -Type Warning
        foreach ($change in $changes) { Write-Host "        - $change" }
        if ($policyPresence -eq 'absent' -and (Test-ProjectSpansGrant)) {
            Write-Host "      Note: this repository's project-owned policy ('$ResourcePolicyName')"
            Write-Host '      already grants the needed access, but the toolkit checks only for a policy'
            Write-Host "      named '$toolkitPolicyName', so it adds its own duplicate policy."
        }
        Write-Host '      Cleanup / rollback after launch (run only once no other workload relies on them):'
        Write-Host "        - Resource policy: aws logs delete-resource-policy --policy-name $toolkitPolicyName --region $Region"
        Write-Host "        - Trace destination: aws xray update-trace-segment-destination --destination $destination --region $Region"
        Write-Host "          (only if it was changed from '$destination' above; omit if it was already CloudWatchLogs)"
        Write-Host '      To stop the deploy before this happens, re-run with'
        Write-Host '      -StopOnAccountObservabilityChange (deploy halts before agentcore launch).'
    }

    if ($StopOnAccountObservabilityChange -and $wouldChange -ne 0) {
        Write-GameAgentStatus "StopOnAccountObservabilityChange is set and the AgentCore starter toolkit would change (or might change) account-wide X-Ray / CloudWatch Logs settings. Stopping before 'agentcore launch' so no shared setting is modified. Enable Transaction Search intentionally (for example -ConfigureAccountObservability or once in the AWS console), then re-run without the stop switch." -Type Error
        return $false
    }
    return $true
}
