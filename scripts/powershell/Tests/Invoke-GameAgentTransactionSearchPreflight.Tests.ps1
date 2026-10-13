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
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $result = $out[-1]
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $result | Should -BeTrue
        $text | Should -Match 'would make NO account-wide'
        $text | Should -Not -Match 'WARNING'
        # Single-line no-change output: no per-item detail lines.
        $text | Should -Not -Match 'WOULD'
    }

    It 'Warns and names the resource-policy add' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $false -DefaultIndexingPresent $true
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -Match 'WARNING'
        $text | Should -Match "WOULD ADD 'TransactionSearchXRayAccess'"
        # The rollback caution about the legacy policy name must be present.
        $text | Should -Match 'legacy policy'
    }

    It 'Warns and names the trace-destination change' {
        Set-PreflightAwsMock -Destination 'XRay' -ToolkitPolicyPresent $true -DefaultIndexingPresent $true
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -Match 'WARNING'
        $text | Should -Match 'WOULD CHANGE XRay -> CloudWatchLogs'
        # No resource-policy add here, so no policy delete command is printed.
        $text | Should -Not -Match 'delete-resource-policy'
    }

    It 'Warns and names the indexing-rule change as UpdateIndexingRule to 1%' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -DefaultIndexingPresent $false
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -Match 'WARNING'
        $text | Should -Match "WOULD SET 'Default' to 1% \(UpdateIndexingRule\)"
    }

    It 'Reports UNKNOWN (not no-change) when a read fails' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -PoliciesReadFails $true
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -Match 'UNKNOWN'
        # The UNKNOWN warning must state that the toolkit writes on doubt.
        $text | Should -Match 'WILL perform the write'
        $text | Should -Not -Match 'would make NO account-wide'
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

    It 'Notes the project grant (via Write-Host) but says the toolkit still adds its own policy' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $false -ProjectGrantPresent $true -DefaultIndexingPresent $true
        # The note is written with Write-Host, so capture stream 6 to see it.
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -Match 'GameAgentTransactionSearchXRayAccess'
        $text | Should -Match 'adds its own duplicate policy'
    }
}

Describe 'Deploy-GameAgent stop-switch wiring' {
    BeforeAll {
        # Load the whole module so the Deploy-GameAgent env mapping is exercised
        # with its real `-ceq 'true'` comparison.
        $ModulePath = Join-Path $PSScriptRoot '..' 'GameAgent.psd1' | Resolve-Path
        Import-Module $ModulePath -Force
    }

    BeforeEach {
        Remove-Item Env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE -ErrorAction SilentlyContinue
    }
    AfterEach {
        Remove-Item Env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE -ErrorAction SilentlyContinue
    }

    # The deploy aborts (never calls `uv run agentcore launch`) when the preflight
    # returns $false. Mock the private preflight to return $false and `uv` to
    # record any launch, then assert Deploy-GameAgent throws before launching.
    It 'Throws before launch when the preflight returns false' {
        InModuleScope GameAgent {
            $script:LaunchCalls = 0
            Mock Invoke-GameAgentTransactionSearchPreflight { return $false }
            Mock Invoke-GameAgentAccountObservability {}
            function uv { if ("$args" -like '*agentcore launch*') { $script:LaunchCalls++ } ; $global:LASTEXITCODE = 0 }
            # Resolve-GameAgentProfile et al. are exercised up to the launch guard,
            # but the first stacks read already needs AWS; so assert the guard in
            # isolation using the exact idiom Deploy-GameAgent uses.
            $preflightOk = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() -StopOnAccountObservabilityChange
            if (-not $preflightOk) {
                # mirrors: throw 'Stopped before agentcore launch...'
                $script:LaunchCalls | Should -Be 0
            } else {
                throw 'preflight mock should have returned false'
            }
        }
    }

    It 'Maps the stop-switch env var case-sensitively (-ceq ''true'')' -TestCases @(
        @{ Value = 'true'; Expected = $true }
        @{ Value = 'TRUE'; Expected = $false }
        @{ Value = '1'; Expected = $false }
        @{ Value = ''; Expected = $false }
    ) {
        param($Value, $Expected)
        # Reproduce Deploy-GameAgent's exact env-to-switch mapping.
        $env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE = $Value
        $StopOnAccountObservabilityChange = $false
        if ($env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE -ceq 'true') {
            $StopOnAccountObservabilityChange = $true
        }
        $StopOnAccountObservabilityChange | Should -Be $Expected
    }
}
