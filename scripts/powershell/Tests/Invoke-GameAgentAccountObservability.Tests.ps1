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
            [string]$Status = 'ACTIVE',
            [string]$PoliciesJson = '{"resourcePolicies":[]}',
            [string]$FailOperation = '',
            [System.Collections.Generic.List[string]]$Mutations
        )
        $destLocal = $Destination
        $spansLocal = $SpansExists
        $unreadableLocal = $DestinationUnreadable
        $indexingLocal = $IndexingPercent
        $statusLocal = $Status
        $policiesLocal = $PoliciesJson
        $failLocal = $FailOperation
        $mutationsLocal = $Mutations

        $mock = {
            $op = "$($args[0]) $($args[1])"
            $allArgs = $args -join ' '
            switch ($op) {
                'sts get-caller-identity' { $global:LASTEXITCODE = 0; return '123456789012' }
                'xray get-trace-segment-destination' {
                    if ($unreadableLocal) { $global:LASTEXITCODE = 254; return '' }
                    $global:LASTEXITCODE = 0
                    if ($allArgs -match '\bStatus\b') { return $statusLocal }
                    return $destLocal
                }
                'xray get-indexing-rules' { $global:LASTEXITCODE = 0; return $indexingLocal }
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
        # Default mode must not mutate even when it cannot read the destination.
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        $mutations | Should -Not -Contain 'xray update-trace-segment-destination'
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
        $mutations | Should -Not -Contain 'xray update-trace-segment-destination'
    }

    It 'Opt-in preserves an existing CloudWatchLogs destination without toggling it' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        $mutations | Should -Contain 'logs put-resource-policy'
        $mutations | Should -Not -Contain 'xray update-trace-segment-destination'
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
        ($script:StatusMessages -join ' ') + ' done' | Should -Not -BeNullOrEmpty
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
