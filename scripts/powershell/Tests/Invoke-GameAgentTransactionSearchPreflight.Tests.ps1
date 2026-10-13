BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentTransactionSearchPreflight.ps1' | Resolve-Path)

    $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    function Write-GameAgentStatus {
        param([string]$Message, [string]$Type)
        $script:StatusMessages.Add("$Type`: $Message")
    }

    # Builds an `aws` mock returning canned, synthetic responses for the three
    # read-only queries the preflight makes. All values are synthetic.
    function Set-PreflightAwsMock {
        param(
            [string]$Destination = 'XRay',
            [bool]$ToolkitPolicyPresent = $false,
            [bool]$DefaultIndexingPresent = $true,
            [bool]$ProjectGrantPresent = $false,
            [bool]$DestinationReadFails = $false,
            [bool]$PoliciesReadFails = $false,
            [bool]$IndexingReadFails = $false
        )
        $destLocal = $Destination
        $toolkitLocal = $ToolkitPolicyPresent
        $indexingPresentLocal = $DefaultIndexingPresent
        $grantLocal = $ProjectGrantPresent
        $destFailLocal = $DestinationReadFails
        $policiesFailLocal = $PoliciesReadFails
        $indexingFailLocal = $IndexingReadFails

        $policyEntries = [System.Collections.Generic.List[string]]::new()
        if ($toolkitLocal) {
            $policyEntries.Add('{"policyName":"TransactionSearchXRayAccess","policyDocument":"{\"Version\":\"2012-10-17\",\"Statement\":[]}"}')
        }
        if ($grantLocal) {
            $policyEntries.Add('{"policyName":"GameAgentTransactionSearchXRayAccess","policyDocument":"{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"xray.amazonaws.com\"},\"Action\":\"logs:PutLogEvents\",\"Resource\":[\"arn:aws:logs:us-west-2:123456789012:log-group:aws/spans:*\"]}]}"}')
        }
        $policiesJson = '{"resourcePolicies":[' + ($policyEntries -join ',') + ']}'
        $indexingJson = if ($indexingPresentLocal) {
            '{"IndexingRules":[{"Name":"Default","Rule":{"Probabilistic":{"DesiredSamplingPercentage":1}}}]}'
        } else {
            '{"IndexingRules":[]}'
        }

        $mock = {
            $op = "$($args[0]) $($args[1])"
            switch ($op) {
                'xray get-trace-segment-destination' {
                    if ($destFailLocal) { $global:LASTEXITCODE = 254; return '' }
                    $global:LASTEXITCODE = 0; return $destLocal
                }
                'logs describe-resource-policies' {
                    if ($policiesFailLocal) { $global:LASTEXITCODE = 254; return '' }
                    $global:LASTEXITCODE = 0; return $policiesJson
                }
                'xray get-indexing-rules' {
                    if ($indexingFailLocal) { $global:LASTEXITCODE = 254; return '' }
                    $global:LASTEXITCODE = 0; return $indexingJson
                }
                default { $global:LASTEXITCODE = 1; return '' }
            }
        }.GetNewClosure()
        Set-Item -Path function:global:aws -Value $mock
    }
}

Describe 'Invoke-GameAgentTransactionSearchPreflight' {
    BeforeEach {
        $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
        $global:LASTEXITCODE = 0
    }
    AfterEach {
        Remove-Item -Path function:aws -ErrorAction SilentlyContinue
    }

    It 'Reports no change (returns true) when the account matches every toolkit condition' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -DefaultIndexingPresent $true
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @()
        $result | Should -BeTrue
        ($script:StatusMessages -join ' ') | Should -Match 'would make NO account-wide'
        ($script:StatusMessages -join ' ') | Should -Not -Match 'WARNING'
    }

    It 'Warns when the toolkit would add its resource policy' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $false -DefaultIndexingPresent $true
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @()
        $result | Should -BeTrue
        ($script:StatusMessages -join ' ') | Should -Match 'WARNING'
    }

    It 'Warns when the toolkit would change the trace destination' {
        Set-PreflightAwsMock -Destination 'XRay' -ToolkitPolicyPresent $true -DefaultIndexingPresent $true
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @()
        $result | Should -BeTrue
        ($script:StatusMessages -join ' ') | Should -Match 'WARNING'
    }

    It 'Warns when the toolkit would create the Default indexing rule' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -DefaultIndexingPresent $false
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @()
        $result | Should -BeTrue
        ($script:StatusMessages -join ' ') | Should -Match 'WARNING'
    }

    It 'Reports UNKNOWN (not no-change) when a read fails' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -PoliciesReadFails $true
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @()
        $result | Should -BeTrue
        ($script:StatusMessages -join ' ') | Should -Match 'UNKNOWN'
        ($script:StatusMessages -join ' ') | Should -Not -Match 'would make NO account-wide'
    }

    It 'Stop switch returns false (halts) when the toolkit would change something' {
        Set-PreflightAwsMock -Destination 'XRay' -ToolkitPolicyPresent $false -DefaultIndexingPresent $true
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() -StopOnAccountObservabilityChange
        $result | Should -BeFalse
        ($script:StatusMessages -join ' ') | Should -Match "Stopping before 'agentcore launch'"
    }

    It 'Stop switch returns false (halts) on an UNKNOWN read failure (fail closed)' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -IndexingReadFails $true
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() -StopOnAccountObservabilityChange
        $result | Should -BeFalse
    }

    It 'Stop switch is a no-op (returns true) when the toolkit would change nothing' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -DefaultIndexingPresent $true
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() -StopOnAccountObservabilityChange
        $result | Should -BeTrue
    }

    It 'Notes the project grant but says the toolkit still adds its own policy' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $false -ProjectGrantPresent $true -DefaultIndexingPresent $true
        $result = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @()
        $result | Should -BeTrue
        ($script:StatusMessages -join ' ') | Should -Match 'WARNING'
    }
}
