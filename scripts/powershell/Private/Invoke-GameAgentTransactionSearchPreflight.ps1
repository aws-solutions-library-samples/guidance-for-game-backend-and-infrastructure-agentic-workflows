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
          3. indexing rule     - the toolkit sets the shared X-Ray 'Default'
             indexing rule to 1% via UpdateIndexingRule when NO rule named
             'Default' exists; an account that already has one satisfies the
             condition.

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

    # Read describe-resource-policies ONCE per preflight and reuse the parsed
    # result for both the toolkit name-only check and the project-grant
    # annotation, so the two cannot drift or hit the API twice. $null means the
    # read failed (unknown). The read is wrapped so native-stderr handling on
    # PowerShell 7.0/7.1 (where redirected stderr can surface as a terminating
    # error under $ErrorActionPreference='Stop') yields $null instead of
    # throwing out of the preflight and aborting the deploy.
    function Read-ResourcePoliciesOnce {
        try {
            $raw = (& aws logs describe-resource-policies --region $Region @ProfileArgs --output json 2>$null)
        } catch { return $null }
        if ($LASTEXITCODE -ne 0) { return $null }
        try { return ($raw | ConvertFrom-Json) } catch { return $null }
    }

    # Returns 'present', 'absent', or 'unreadable' for a resource policy named
    # literally 'TransactionSearchXRayAccess' (the toolkit's name-only check),
    # from the single cached describe-resource-policies read. Every comparison
    # against an AWS identifier in this preflight is case-sensitive (-ceq,
    # -cne, -ccontains, -clike), matching the toolkit and the shell readers: a
    # case variant such as 'transactionsearchxrayaccess' does not satisfy the
    # toolkit, so it must not satisfy this prediction either.
    function Get-ToolkitResourcePolicyPresence {
        param($Policies)
        if ($null -eq $Policies) { return 'unreadable' }
        foreach ($policy in @($Policies.resourcePolicies)) {
            if ($policy.policyName -ceq $toolkitPolicyName) { return 'present' }
        }
        return 'absent'
    }

    # Returns $true if any resource policy already grants xray.amazonaws.com
    # logs:PutLogEvents on an 'aws/spans' log group (Effect Allow), regardless of
    # name. Used only to annotate the warning. Reuses the single cached read.
    function Test-ProjectSpansGrant {
        param($Policies)
        return [bool](Get-SpansGrantName -Policies $Policies)
    }

    # Returns the name of the resource policy that already grants
    # xray.amazonaws.com logs:PutLogEvents on an 'aws/spans' log group (Effect
    # Allow), or $null when none does or the list could not be read. Reuses the
    # single cached read. Used to name the grant that blocks the opt-in route.
    function Get-SpansGrantName {
        param($Policies)
        if ($null -eq $Policies) { return $null }
        foreach ($policy in @($Policies.resourcePolicies)) {
            try { $doc = $policy.policyDocument | ConvertFrom-Json } catch { continue }
            foreach ($statement in @($doc.Statement)) {
                if ($statement.Effect -cne 'Allow') { continue }
                $services = @($statement.Principal.Service)
                $actions = @($statement.Action)
                $resources = @($statement.Resource)
                if (($services -ccontains 'xray.amazonaws.com') -and
                    ($actions -ccontains 'logs:PutLogEvents') -and
                    ($resources | Where-Object { "$_" -clike '*aws/spans*' })) {
                    return $policy.policyName
                }
            }
        }
        return $null
    }

    # Returns 'present', 'absent', or 'unreadable' for a Default indexing rule
    # (the toolkit keys off a rule NAMED 'Default' existing).
    function Get-DefaultIndexingRulePresence {
        try {
            $raw = (& aws xray get-indexing-rules --region $Region @ProfileArgs --output json 2>$null)
        } catch { return 'unreadable' }
        if ($LASTEXITCODE -ne 0) { return 'unreadable' }
        try { $parsed = $raw | ConvertFrom-Json } catch { return 'unreadable' }
        foreach ($rule in @($parsed.IndexingRules)) {
            if ($rule.Name -ceq 'Default') { return 'present' }
        }
        return 'absent'
    }

    # Returns 'present', 'absent', or 'unreadable' for the AWS-reserved
    # 'aws/spans' log group. Only JSON output renders the quoted group name a
    # parse needs, so request JSON and classify a text/denied read as unreadable.
    function Get-SpansLogGroupState {
        try {
            $raw = (& aws logs describe-log-groups --log-group-name-prefix 'aws/spans' --region $Region @ProfileArgs --output json 2>$null)
        } catch { return 'unreadable' }
        if ($LASTEXITCODE -ne 0) { return 'unreadable' }
        try { $parsed = $raw | ConvertFrom-Json } catch { return 'unreadable' }
        foreach ($group in @($parsed.logGroups)) {
            if ($group.logGroupName -ceq 'aws/spans') { return 'present' }
        }
        return 'absent'
    }

    $changes = [System.Collections.Generic.List[string]]::new()
    $detail = [System.Collections.Generic.List[string]]::new()
    $unknown = $false
    $policyWouldAdd = $false
    $destinationWouldChange = $false
    $indexingWouldCreate = $false
    $priorDestination = ''

    # 1. Resource policy (name-only check). Read describe-resource-policies once
    #    and reuse it for both this check and the project-grant annotation.
    $resourcePolicies = Read-ResourcePoliciesOnce
    $policyPresence = Get-ToolkitResourcePolicyPresence -Policies $resourcePolicies
    switch ($policyPresence) {
        'present' { }  # no change — no detail line (single-line no-change output)
        'absent' {
            $policyWouldAdd = $true
            $changes.Add("Adds a CloudWatch Logs resource policy named '$toolkitPolicyName'")
            $detail.Add("      - CloudWatch Logs resource policy: WOULD ADD '$toolkitPolicyName'")
            $detail.Add("        (grants xray.amazonaws.com logs:PutLogEvents on 'aws/spans' and")
            $detail.Add("         '/aws/application-signals/data')")
        }
        default {
            $unknown = $true
            $detail.Add('      - CloudWatch Logs resource policy: UNKNOWN (could not read resource policies)')
        }
    }

    # 2. Trace segment destination (shared reader, used by the opt-in path too).
    $destination = Get-GameAgentTraceDestination -Region $Region -ProfileArgs $ProfileArgs
    if ($destination -ceq 'CloudWatchLogs') {
        # no change — no detail line
    } elseif ($destination -ceq 'UNREADABLE') {
        $unknown = $true
        $detail.Add('      - X-Ray trace segment destination: UNKNOWN (could not read destination)')
    } else {
        $destinationWouldChange = $true
        $priorDestination = $destination
        $changes.Add("Sets the X-Ray trace segment destination to CloudWatchLogs (region $Region)")
        $detail.Add("      - X-Ray trace segment destination: WOULD CHANGE $destination -> CloudWatchLogs")
        $detail.Add("        (region $Region; shared by every X-Ray caller)")
    }

    # 3. Default indexing rule. Any value other than present/absent is UNKNOWN.
    $indexingPresence = Get-DefaultIndexingRulePresence
    switch ($indexingPresence) {
        'present' { }  # no change — no detail line
        'absent' {
            $indexingWouldCreate = $true
            $changes.Add("Sets the X-Ray 'Default' indexing rule to 1% via UpdateIndexingRule")
            $detail.Add("      - X-Ray default indexing rule: WOULD SET 'Default' to 1% (UpdateIndexingRule)")
        }
        default {
            $unknown = $true
            $detail.Add('      - X-Ray default indexing rule: UNKNOWN (could not read indexing rules)')
        }
    }

    # Classify: 0 no change, 1 would change, 2 unknown (possible change).
    if ($unknown) {
        $wouldChange = 2
        Write-Host '  Preflight: predicting AgentCore starter toolkit Transaction Search changes...'
        Write-Host "      (the toolkit runs these during 'agentcore launch' when runtime"
        Write-Host '       observability is enabled; they are account-wide and shared)'
        foreach ($line in $detail) { Write-Host $line }
        Write-GameAgentStatus "Preflight could not read one or more account settings, so the toolkit's effect is UNKNOWN. The toolkit writes on doubt: for each unknown item it WILL perform the write during 'agentcore launch'. Treating this as a possible shared change (not 'no change')." -Type Warning
    } elseif ($changes.Count -eq 0) {
        $wouldChange = 0
        Write-GameAgentStatus 'Preflight: the AgentCore starter toolkit would make NO account-wide X-Ray or CloudWatch Logs change.' -Type Success
    } else {
        $wouldChange = 1
        Write-Host '  Preflight: predicting AgentCore starter toolkit Transaction Search changes...'
        Write-Host "      (the toolkit runs these during 'agentcore launch' when runtime"
        Write-Host '       observability is enabled; they are account-wide and shared)'
        foreach ($line in $detail) { Write-Host $line }
        Write-Host ''
        Write-GameAgentStatus "WARNING: 'agentcore launch' WILL change SHARED, account-wide settings via the AgentCore starter toolkit (NOT this repository's opt-in). These affect every X-Ray / Transaction Search consumer in the account:" -Type Warning
        foreach ($change in $changes) { Write-Host "        - $change" }
        if ($policyWouldAdd) {
            $existingGrantName = Get-SpansGrantName -Policies $resourcePolicies
            if ($existingGrantName) {
                Write-Host "      Note: a resource policy ('$existingGrantName') already grants the needed"
                Write-Host '      access, but the toolkit checks only for a policy'
                Write-Host "      named '$toolkitPolicyName', so it adds its own duplicate policy."
            }
        }
        Write-Host '      Cleanup / rollback after launch (run only once no other workload relies on them):'
        if ($policyWouldAdd) {
            Write-Host "        - Resource policy: aws logs delete-resource-policy --policy-name $toolkitPolicyName --region $Region"
            Write-Host "          Caution: '$toolkitPolicyName' is also this repository's legacy policy"
            Write-Host '          name. Confirm the launch (not an earlier deploy) created it before deleting,'
            Write-Host '          so you do not remove a shared grant another workload relies on.'
        }
        if ($destinationWouldChange) {
            Write-Host "        - Trace destination: aws xray update-trace-segment-destination --destination $priorDestination --region $Region"
            Write-Host "          (restores the prior destination '$priorDestination')"
        }
        if ($indexingWouldCreate) {
            Write-Host "        - Indexing rule: the toolkit sets the shared 'Default' rule to 1%. To restore a"
            Write-Host '          prior percentage: aws xray update-indexing-rule --name Default `'
            Write-Host "            --rule '{`"Probabilistic`":{`"DesiredSamplingPercentage`":<prior>}}' --region $Region"
        }
        if (-not $StopOnAccountObservabilityChange) {
            Write-Host '      To stop the deploy before this happens, re-run with'
            Write-Host '      -StopOnAccountObservabilityChange (deploy halts before agentcore launch).'
        }
    }

    if ($StopOnAccountObservabilityChange -and $wouldChange -ne 0) {
        # The opt-in cannot create the AWS-reserved 'aws/spans' log group, and it
        # skips its own policy write when another policy already grants the
        # access. When the halt is a definite change, lead with the one-time
        # console enable. Offer the opt-in route only when 'aws/spans' already
        # exists AND no other policy grants that access (the cached policy read
        # decides the second part); with another grant in place, name it and
        # point to adding the toolkit-named policy directly. When the halt is
        # caused only by an unreadable state ($wouldChange -eq 2, whether or not
        # another item is a definite change), lead with restoring the read
        # instead, because the toolkit's full effect cannot be known until the
        # read succeeds.
        if ($wouldChange -eq 2) {
            $message = "StopOnAccountObservabilityChange is set and the AgentCore starter toolkit might change account-wide X-Ray / CloudWatch Logs settings. Stopping before 'agentcore launch' so 'agentcore launch' modifies no shared setting. One or more account settings could not be read, so the toolkit's full effect is unknown. First restore the read (most often expired credentials or a missing read permission for the item marked UNKNOWN above), then re-run; this preflight names any condition that still fails."
            Write-GameAgentStatus $message -Type Error
            return $false
        }
        $spansState = Get-SpansLogGroupState
        $grantName = Get-SpansGrantName -Policies $resourcePolicies
        $message = "StopOnAccountObservabilityChange is set and the AgentCore starter toolkit would change (or might change) account-wide X-Ray / CloudWatch Logs settings. Stopping before 'agentcore launch' so 'agentcore launch' modifies no shared setting. To reach a passing run without any shared change, the account must already satisfy the toolkit's three conditions: a CloudWatch Logs resource policy named exactly 'TransactionSearchXRayAccess', the X-Ray trace segment destination 'CloudWatchLogs', and an X-Ray 'Default' indexing rule. Enable Transaction Search once in the AWS X-Ray / CloudWatch console, then re-run; this preflight names any condition that still fails."
        if ($spansState -eq 'present' -and -not $grantName) {
            $message += " Because the AWS-reserved 'aws/spans' log group already exists and no other policy grants that access, this repository's opt-in with the matching policy name can also reach that state (it switches an 'XRay' destination, adds the toolkit-named policy, and leaves the shared 'Default' indexing rule for the console enable): GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true with GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME=TransactionSearchXRayAccess (PowerShell: -ConfigureAccountObservability with that policy name set)."
        } elseif ($spansState -eq 'present' -and $grantName) {
            $message += " Another policy ('$grantName') already grants 'aws/spans' access under a different name, so neither the console nor the opt-in adds a policy named 'TransactionSearchXRayAccess'. Add a policy with exactly that name, or run one deploy without the stop switch so the toolkit adds it."
        }
        Write-GameAgentStatus $message -Type Error
        return $false
    }
    return $true
}
