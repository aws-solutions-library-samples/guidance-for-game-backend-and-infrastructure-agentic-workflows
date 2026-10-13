BeforeAll {
    $PrivateDir = Join-Path $PSScriptRoot '..' 'Private'
    . (Join-Path $PrivateDir 'Get-GameAgentTraceDestination.ps1' | Resolve-Path)
    . (Join-Path $PrivateDir 'Invoke-GameAgentTransactionSearchPreflight.ps1' | Resolve-Path)

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

        # Count how many times describe-resource-policies is called, so a test
        # can assert the preflight reads it exactly once per run.
        $global:PreflightPolicyReadCount = 0

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
                    $global:PreflightPolicyReadCount++
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
        $global:PreflightPolicyReadCount = 0
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
        # Case-sensitive: the success message contains 'NO' (which -Match 'WOULD'
        # would never hit), but the per-item change markers are what must be
        # absent. -MatchExactly is case-sensitive, unlike -Match.
        $text | Should -Not -MatchExactly 'WARNING'
        $text | Should -Not -MatchExactly 'WOULD (ADD|CHANGE|SET)'
    }

    It 'Reads describe-resource-policies exactly once per run' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $false -ProjectGrantPresent $true -DefaultIndexingPresent $true
        $null = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        # Both the toolkit name-only check and the project-grant annotation reuse
        # one cached read, so the API is hit once even when the grant is present.
        $global:PreflightPolicyReadCount | Should -Be 1
    }

    It 'Warns and names the resource-policy add' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $false -DefaultIndexingPresent $true
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -MatchExactly 'WARNING'
        $text | Should -MatchExactly "WOULD ADD 'TransactionSearchXRayAccess'"
        # The rollback caution about the legacy policy name must be present.
        $text | Should -Match 'legacy policy'
    }

    It 'Warns and names the trace-destination change' {
        Set-PreflightAwsMock -Destination 'XRay' -ToolkitPolicyPresent $true -DefaultIndexingPresent $true
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -MatchExactly 'WARNING'
        $text | Should -MatchExactly 'WOULD CHANGE XRay -> CloudWatchLogs'
        # No resource-policy add here, so no policy delete command is printed.
        $text | Should -Not -Match 'delete-resource-policy'
    }

    It 'Warns and names the indexing-rule change as UpdateIndexingRule to 1%' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -DefaultIndexingPresent $false
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -MatchExactly 'WARNING'
        $text | Should -MatchExactly "WOULD SET 'Default' to 1% \(UpdateIndexingRule\)"
        # The rollback line must print a VALID update-indexing-rule command: the
        # JSON `--rule` argument keeps its double quotes (PowerShell backtick
        # escaping), not a backslash-mangled form.
        $text | Should -Match 'update-indexing-rule --name Default'
        $text | Should -Match '--rule ''\{"Probabilistic":\{"DesiredSamplingPercentage":<prior>\}\}'''
        $text | Should -Not -Match 'DesiredSamplingPercentage\\'
    }

    It 'Reports UNKNOWN (not no-change) when a read fails' {
        Set-PreflightAwsMock -Destination 'CloudWatchLogs' -ToolkitPolicyPresent $true -PoliciesReadFails $true
        $out = Invoke-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() 6>&1
        $text = ($script:StatusMessages -join ' ') + ' ' + (($out | ForEach-Object { "$_" }) -join ' ')
        $out[-1] | Should -BeTrue
        $text | Should -MatchExactly 'UNKNOWN'
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

Describe 'Resolve-GameAgentStopSwitch' {
    BeforeAll {
        . (Join-Path $PSScriptRoot '..' 'Private' 'Resolve-GameAgentStopSwitch.ps1' | Resolve-Path)
    }

    # The real helper Deploy-GameAgent calls to map the env var to the switch.
    # Reverting it to a case-insensitive comparison (-eq) would make 'TRUE'
    # return $true and fail the 'TRUE' case below.
    It 'Enables only for the exact, case-sensitive value ''true''' -TestCases @(
        @{ Value = 'true'; Expected = $true }
        @{ Value = 'TRUE'; Expected = $false }
        @{ Value = 'True'; Expected = $false }
        @{ Value = '1'; Expected = $false }
        @{ Value = 'yes'; Expected = $false }
        @{ Value = ' true'; Expected = $false }
        @{ Value = ''; Expected = $false }
    ) {
        param($Value, $Expected)
        (Resolve-GameAgentStopSwitch -Value $Value) | Should -Be $Expected
    }
}

Describe 'Assert-GameAgentTransactionSearchPreflight' {
    BeforeAll {
        . (Join-Path $PSScriptRoot '..' 'Private' 'Assert-GameAgentTransactionSearchPreflight.ps1' | Resolve-Path)
    }
    AfterEach {
        Remove-Item -Path function:Invoke-GameAgentTransactionSearchPreflight -ErrorAction SilentlyContinue
    }

    # The real guard Deploy-GameAgent calls before every `agentcore launch`.
    It 'Throws when the preflight returns false (fail closed)' {
        function Invoke-GameAgentTransactionSearchPreflight { return $false }
        { Assert-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() -StopOnAccountObservabilityChange } |
            Should -Throw -ExpectedMessage '*Stopped before agentcore launch*'
    }

    It 'Does not throw when the preflight returns true' {
        function Invoke-GameAgentTransactionSearchPreflight { return $true }
        { Assert-GameAgentTransactionSearchPreflight -Region 'us-west-2' -ProfileArgs @() } |
            Should -Not -Throw
    }
}

Describe 'Deploy-GameAgent wiring (static source checks)' {
    BeforeAll {
        $script:DeploySource = Get-Content (Join-Path $PSScriptRoot '..' 'Public' 'Deploy-GameAgent.ps1' | Resolve-Path) -Raw
        $script:DeployLines = $script:DeploySource -split "`n"
    }

    # Every `uv run agentcore launch` must be preceded (within a few lines) by
    # the shared fail-closed guard, so no launch path skips the preflight.
    # Deleting a guard, or replacing it with a non-throwing call, fails this.
    # Comment lines in the window are ignored so a commented-out guard cannot
    # satisfy the check.
    It 'Guards every agentcore launch with Assert-GameAgentTransactionSearchPreflight' {
        $launchIdx = @()
        for ($i = 0; $i -lt $script:DeployLines.Count; $i++) {
            if ($script:DeployLines[$i] -match 'uv run agentcore launch' -and
                $script:DeployLines[$i].TrimStart() -notlike '#*') {
                $launchIdx += $i
            }
        }
        $launchIdx.Count | Should -BeGreaterThan 0
        foreach ($idx in $launchIdx) {
            # Scan a wider span and keep the last 6 non-comment lines so
            # interleaved comments do not push the real guard out of view.
            $start = [Math]::Max(0, $idx - 12)
            $windowLines = $script:DeployLines[$start..($idx - 1)] | Where-Object { $_.TrimStart() -notlike '#*' }
            $window = (@($windowLines) | Select-Object -Last 6) -join "`n"
            $window | Should -Match 'Assert-GameAgentTransactionSearchPreflight'
        }
    }

    # The env-to-switch mapping must go through the case-sensitive helper, not an
    # inline -eq. Reverting to -eq (or dropping the helper) fails this.
    It 'Maps the stop-switch env var through Resolve-GameAgentStopSwitch' {
        $script:DeploySource | Should -Match 'Resolve-GameAgentStopSwitch -Value \$env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE'
        $script:DeploySource | Should -Not -Match '\$env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE -ceq'
        $script:DeploySource | Should -Not -Match '\$env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE -eq'
    }

    # The early stop-switch preflight must run after Step 1.7 and before Step 2,
    # mirroring the shell path, so a predicted change halts up front rather than
    # after the KB and observability steps.
    It 'Runs the early stop-switch preflight between Step 1.7 and Step 2' {
        $step17 = ($script:DeploySource -split "`n" | Select-String -SimpleMatch 'Step 1.7:' | Select-Object -First 1).LineNumber
        $earlyGuard = ($script:DeploySource -split "`n" | Select-String -Pattern 'if \(\$StopOnAccountObservabilityChange\)' | Select-Object -First 1).LineNumber
        $step2 = ($script:DeploySource -split "`n" | Select-String -SimpleMatch 'Step 2: Launching AgentCore Runtime' | Select-Object -First 1).LineNumber
        $step17 | Should -Not -BeNullOrEmpty
        $earlyGuard | Should -Not -BeNullOrEmpty
        $step2 | Should -Not -BeNullOrEmpty
        $earlyGuard | Should -BeGreaterThan $step17
        $earlyGuard | Should -BeLessThan $step2
    }

    # The early block's ORDER is not enough: it must actually call the preflight.
    # Require an uncommented Assert-GameAgentTransactionSearchPreflight line
    # between the early-guard `if` and Step 2, and that it is NOT wrapped in a
    # try/catch (which would swallow the fail-closed throw and let the deploy
    # continue). Deleting the call, commenting it out, or wrapping it in
    # try/catch all fail this.
    It 'Calls Assert-GameAgentTransactionSearchPreflight directly in the early block' {
        $lines = $script:DeploySource -split "`n"
        $earlyGuard = ($lines | Select-String -Pattern 'if \(\$StopOnAccountObservabilityChange\)' | Select-Object -First 1).LineNumber
        $step2 = ($lines | Select-String -SimpleMatch 'Step 2: Launching AgentCore Runtime' | Select-Object -First 1).LineNumber
        $earlyGuard | Should -Not -BeNullOrEmpty
        $step2 | Should -Not -BeNullOrEmpty
        # Select-String LineNumber is 1-based; slice the lines strictly between
        # the early guard and the Step 2 banner.
        $blockLines = $lines[$earlyGuard..($step2 - 2)]
        # Uncommented preflight calls in the early block.
        $calls = @($blockLines | Where-Object {
                $_.TrimStart() -notlike '#*' -and $_ -match 'Assert-GameAgentTransactionSearchPreflight'
            })
        $calls.Count | Should -BeGreaterThan 0
        # The early block must not wrap the preflight in a try/catch, which would
        # swallow its terminating stop and defeat the fail-closed intent.
        $hasTry = @($blockLines | Where-Object { $_.TrimStart() -notlike '#*' -and $_ -match '\btry\b' }).Count
        $hasTry | Should -Be 0
    }
}
