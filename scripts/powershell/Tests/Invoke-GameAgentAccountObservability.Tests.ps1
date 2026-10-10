BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentAccountObservability.ps1' | Resolve-Path)

    $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    function Write-GameAgentStatus {
        param([string]$Message, [string]$Type)
        $script:StatusMessages.Add("$Type`: $Message")
    }

    # Builds an `aws` mock that returns canned, synthetic responses and records
    # every mutating call name (so a mutation hidden behind an output capture is
    # still observable to the test).
    function Set-ObsAwsMock {
        param(
            [string]$Destination = 'XRay',
            [bool]$SpansExists = $false,
            [bool]$DestinationUnreadable = $false,
            [string]$IndexingPercent = '5',
            [bool]$IndexingUnreadable = $false,
            [string]$Status = 'ACTIVE',
            [string]$PoliciesJson = '{"resourcePolicies":[]}',
            [string]$FailOperation = '',
            [string]$AccountId = '123456789012',
            [bool]$IdentityStderrWarning = $false,
            [System.Collections.Generic.List[string]]$Mutations
        )
        $destLocal = $Destination
        $spansLocal = $SpansExists
        $unreadableLocal = $DestinationUnreadable
        $indexingLocal = $IndexingPercent
        $indexingUnreadableLocal = $IndexingUnreadable
        $statusLocal = $Status
        $policiesLocal = $PoliciesJson
        $failLocal = $FailOperation
        $accountLocal = $AccountId
        $identityWarnLocal = $IdentityStderrWarning
        $mutationsLocal = $Mutations

        $mock = {
            $op = "$($args[0]) $($args[1])"
            $allArgs = $args -join ' '
            switch ($op) {
                'sts get-caller-identity' {
                    # A successful lookup may still print a diagnostic to stderr.
                    if ($identityWarnLocal) { Write-Error 'a deprecation notice on stderr' -ErrorAction Continue }
                    $global:LASTEXITCODE = 0; return $accountLocal
                }
                'xray get-trace-segment-destination' {
                    if ($unreadableLocal) { $global:LASTEXITCODE = 254; return '' }
                    $global:LASTEXITCODE = 0
                    if ($allArgs -match '\bStatus\b') { return $statusLocal }
                    return $destLocal
                }
                'xray get-indexing-rules' {
                    if ($indexingUnreadableLocal) { $global:LASTEXITCODE = 254; return '' }
                    $global:LASTEXITCODE = 0; return $indexingLocal
                }
                'logs describe-log-groups' {
                    $global:LASTEXITCODE = 0
                    if ($spansLocal) { return '{"logGroups":[{"logGroupName":"aws/spans"}]}' }
                    return '{"logGroups":[]}'
                }
                'logs describe-resource-policies' { $global:LASTEXITCODE = 0; return $policiesLocal }
                default {
                    $mutationsLocal.Add($op)
                    if ($failLocal -and $op -eq $failLocal) {
                        $global:LASTEXITCODE = 254
                        return 'An error occurred (AccessDeniedException): not authorized'
                    }
                    $global:LASTEXITCODE = 0
                    return ''
                }
            }
        }.GetNewClosure()
        Set-Item -Path function:global:aws -Value $mock
    }
}

Describe 'Invoke-GameAgentAccountObservability' {
    BeforeEach {
        $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
        $global:LASTEXITCODE = 0
    }
    AfterEach {
        Remove-Item -Path function:aws -ErrorAction SilentlyContinue
    }

    It 'Default mode makes no mutation when the account already supports Transaction Search' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        $mutations | Should -Not -Contain 'xray update-trace-segment-destination'
        $mutations | Should -Not -Contain 'xray update-indexing-rule'
        $mutations | Should -Not -Contain 'logs put-resource-policy'
    }

    It 'Default mode prints unknown when the destination read fails' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -DestinationUnreadable $true -Mutations $mutations
        # Default mode must not mutate even when it cannot read the destination,
        # and it must still warn that the account does not yet support the runtime.
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        $mutations | Should -Not -Contain 'xray update-trace-segment-destination'
        ($script:StatusMessages -join ' ') | Should -Match 'does NOT yet appear to support'
    }

    It 'Opt-in enables the shared settings, preserves the indexing rule, and never disables the destination' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -IndexingPercent '25' -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        $mutations | Should -Contain 'logs put-resource-policy'
        $mutations | Should -Contain 'xray update-trace-segment-destination'
        # Indexing rule left unchanged (no explicit percentage requested).
        $mutations | Should -Not -Contain 'xray update-indexing-rule'
        ($script:StatusMessages -join ' ') | Should -Match 'opt-in is ENABLED'
    }

    It 'Opt-in changes the indexing rule only when an explicit percent is set' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -IndexingPercent '25' -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability -DefaultIndexingPercent '1'
        $mutations | Should -Contain 'xray update-indexing-rule'
    }

    It 'Opt-in refuses to change an unreadable destination and throws' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -DestinationUnreadable $true -SpansExists $true -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        } | Should -Throw '*Cannot read the current trace destination*'
        $mutations.Count | Should -Be 0
    }

    It 'Opt-in refuses before any shared write when the requested percent is invalid' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability -DefaultIndexingPercent 'abc'
        } | Should -Throw '*must be an integer in `[0, 100`]*'
        $mutations.Count | Should -Be 0
    }

    It 'Opt-in refuses before any shared write when the indexing rule is unreadable and a change is requested' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -IndexingUnreadable $true -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability -DefaultIndexingPercent '10'
        } | Should -Throw '*Unable to read the current X-Ray default indexing rule*'
        $mutations.Count | Should -Be 0
    }

    It 'Opt-in treats an unreadable indexing rule as non-fatal when no change is requested' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -IndexingUnreadable $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        # The policy and destination are still written; only the indexing rule is skipped.
        $mutations | Should -Contain 'logs put-resource-policy'
        $mutations | Should -Contain 'xray update-trace-segment-destination'
        $mutations | Should -Not -Contain 'xray update-indexing-rule'
        ($script:StatusMessages -join ' ') | Should -Match 'configured \(opt-in\)'
    }

    It 'Opt-in counts only an Effect Allow statement as an existing grant' {
        # A Deny-only statement must not be read as satisfying the requirement,
        # so the project-owned policy is still written.
        $deny = '{"resourcePolicies":[{"policyName":"deny-only","policyDocument":"{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Deny\",\"Principal\":{\"Service\":\"xray.amazonaws.com\"},\"Action\":\"logs:PutLogEvents\",\"Resource\":[\"arn:aws:logs:us-west-2:123456789012:log-group:aws/spans:*\"]}]}"}]}'
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -PoliciesJson $deny -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        $mutations | Should -Contain 'logs put-resource-policy'
    }

    It 'Opt-in exempts an existing project-owned policy name from the 10-policy limit' {
        # Nine unrelated policies plus one already using the project-owned name:
        # updating the owned policy is an upsert, so the limit does not apply.
        $others = (0..8 | ForEach-Object { "{""policyName"":""p$_"",""policyDocument"":""{\""Version\"":\""2012-10-17\"",\""Statement\"":[]}""}" }) -join ','
        $owned = "{""policyName"":""GameAgentTransactionSearchXRayAccess"",""policyDocument"":""{\""Version\"":\""2012-10-17\"",\""Statement\"":[]}""}"
        $policies = "{""resourcePolicies"":[$others,$owned]}"
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -PoliciesJson $policies -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        $mutations | Should -Contain 'logs put-resource-policy'
    }

    It 'Opt-in compares the indexing percent numerically so 1 equals 1.0' {
        # The CLI reports an integer rule as e.g. '1.0'; a requested '1' must be
        # read as already matching and send no update.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -IndexingPercent '1.0' -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability -DefaultIndexingPercent '1'
        $mutations | Should -Not -Contain 'xray update-indexing-rule'
    }

    It 'Opt-in proceeds with the right account when the identity lookup warns on stderr' {
        # A successful lookup may print a warning to stderr; dropping the
        # ErrorRecord items keeps it out of the 12-digit account check.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -IdentityStderrWarning $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        $mutations | Should -Contain 'logs put-resource-policy'
        ($script:StatusMessages -join ' ') | Should -Match 'configured \(opt-in\)'
    }

    It 'Opt-in preserves an existing CloudWatchLogs destination without toggling it' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        $mutations | Should -Contain 'logs put-resource-policy'
        $mutations | Should -Not -Contain 'xray update-trace-segment-destination'
    }

    It 'Opt-in refuses before any shared write when the identity lookup returns no account ID' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -AccountId '' -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        } | Should -Throw '*identity lookup returned no account ID*'
        $mutations.Count | Should -Be 0
    }

    It 'Opt-in throws a bounded error when a mutation fails' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -FailOperation 'logs put-resource-policy' -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        } | Should -Throw '*resource policy failed*'
    }

    It 'Opt-in skips the policy when a legacy grant already covers aws/spans' {
        $legacy = '{"resourcePolicies":[{"policyName":"TransactionSearchXRayAccess","policyDocument":"{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"xray.amazonaws.com\"},\"Action\":\"logs:PutLogEvents\",\"Resource\":[\"arn:aws:logs:us-west-2:123456789012:log-group:aws/spans:*\"]}]}"}]}'
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -PoliciesJson $legacy -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        $mutations | Should -Not -Contain 'logs put-resource-policy'
        ($script:StatusMessages -join ' ') | Should -Match 'configured \(opt-in\)'
    }

    It 'Opt-in fails when the account is already at the 10-policy limit' {
        $entries = (0..9 | ForEach-Object { "{""policyName"":""p$_"",""policyDocument"":""{\""Version\"":\""2012-10-17\"",\""Statement\"":[]}""}" }) -join ','
        $policies = "{""resourcePolicies"":[$entries]}"
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -PoliciesJson $policies -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        } | Should -Throw '*maximum of 10*'
        $mutations | Should -Not -Contain 'logs put-resource-policy'
    }
}
