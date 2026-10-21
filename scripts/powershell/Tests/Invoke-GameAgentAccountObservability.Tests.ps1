BeforeAll {
    # Invoke-GameAgentAccountObservability delegates its trace-destination read
    # to the shared Get-GameAgentTraceDestination helper, so dot-source that
    # helper first; otherwise every spec that reaches the read fails with
    # "The term 'Get-GameAgentTraceDestination' is not recognized".
    . (Join-Path $PSScriptRoot '..' 'Private' 'Get-GameAgentTraceDestination.ps1' | Resolve-Path)
    # The -StopOnAccountObservabilityChange switch defaults to
    # (Resolve-GameAgentStopSwitch -Value $env:...), so the helper must be loaded
    # before the function; otherwise every call that omits the switch fails at
    # parameter binding with "The term 'Resolve-GameAgentStopSwitch' is not
    # recognized".
    . (Join-Path $PSScriptRoot '..' 'Private' 'Resolve-GameAgentStopSwitch.ps1' | Resolve-Path)
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentAccountObservability.ps1' | Resolve-Path)

    $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    function Write-GameAgentStatus {
        param([string]$Message, [string]$Type)
        $script:StatusMessages.Add("$Type`: $Message")
    }

    # Capture Write-Host output so a spec can assert the printed remedy text, not
    # only the Write-GameAgentStatus warnings. The guidance body (the toggle
    # commands, the console/launch advice, the opt-in instruction) is written
    # with Write-Host, so without this capture no spec can fail on that text.
    $script:HostMessages = [System.Collections.Generic.List[string]]::new()
    function Write-Host {
        param(
            [Parameter(ValueFromRemainingArguments = $true, Position = 0)]$Object,
            [switch]$NoNewline,
            $ForegroundColor,
            $BackgroundColor,
            $Separator
        )
        $script:HostMessages.Add(($Object -join ' '))
    }

    # Builds an `aws` mock that returns canned, synthetic responses and records
    # every mutating call name (so a mutation hidden behind an output capture is
    # still observable to the test).
    function Set-ObsAwsMock {
        param(
            [string]$Destination = 'XRay',
            [bool]$SpansExists = $false,
            [bool]$SpansUnreadable = $false,
            [string]$SpansRaw = '__UNSET__',
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
        $spansUnreadableLocal = $SpansUnreadable
        $spansRawLocal = $SpansRaw
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
                    if ($spansUnreadableLocal) { $global:LASTEXITCODE = 254; return '' }
                    $global:LASTEXITCODE = 0
                    # The AWS CLI only renders the quoted log-group name in JSON
                    # output. Emulate that: return JSON only when '--output json'
                    # is requested, and a 'text' rendering otherwise. The reader
                    # parses the output as JSON, so a 'text' rendering does not
                    # parse and reads as 'unreadable' (not 'absent'), which pins
                    # the PowerShell '--output json' request.
                    if ($allArgs -notmatch '--output json') {
                        if ($spansLocal) { return "LOGGROUPS`taws/spans`t1700000000000" }
                        return ''
                    }
                    # A raw override feeds arbitrary parsed-JSON shapes (an empty
                    # object/array, null) or non-JSON text to the reader so a
                    # spec can pin how each shape is classified.
                    if ($spansRawLocal -ne '__UNSET__') { return $spansRawLocal }
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
        $script:HostMessages = [System.Collections.Generic.List[string]]::new()
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
        # The default-mode line scopes its "no changes" claim to this step, since
        # a later launch step may still make account-wide changes.
        ($script:HostMessages -join ' ') | Should -Match 'this step makes no account-wide X-Ray or CloudWatch Logs changes'
        ($script:HostMessages -join ' ') | Should -Not -Match 'no account-wide X-Ray or CloudWatch Logs changes will be made'
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

    It 'Opt-in refuses before any shared write when the requested percent has a leading zero' -ForEach @(
        @{ Percent = '05' }
        @{ Percent = '007' }
    ) {
        # A leading-zero value is rejected before any write so the shell and
        # PowerShell paths accept exactly the same inputs; '05' is not a valid
        # JSON number, which the shell path would otherwise send verbatim.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability -DefaultIndexingPercent $Percent
        } | Should -Throw '*must be an integer in `[0, 100`]*'
        $mutations.Count | Should -Be 0
    }

    It 'Opt-in accepts a canonical percent with no leading zero and writes the rule' -ForEach @(
        @{ Percent = '0' }
        @{ Percent = '5' }
        @{ Percent = '100' }
    ) {
        # Canonical integers in [0, 100] are accepted; the mock reports a
        # different current value so a change is always requested and written.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -IndexingPercent '7' -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability -DefaultIndexingPercent $Percent
        $mutations | Should -Contain 'xray update-indexing-rule'
    }

    It 'Opt-in refuses before any shared write when the requested percent is above 100' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability -DefaultIndexingPercent '101'
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

    It 'Opt-in fails closed when aws/spans is still absent after a first-time enable' {
        # A first-time API enable (destination XRay -> CloudWatchLogs) does not
        # create the AWS-reserved aws/spans log group (starter-toolkit issue
        # 457). The opt-in enables additively and never toggles, so a
        # still-absent group must fail with the one-time manual step, not report
        # success.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $false -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        } | Should -Throw "*'aws/spans' log group does not exist*"
        $mutations | Should -Contain 'xray update-trace-segment-destination'
    }

    It 'Opt-in names the still-PENDING destination when the wait budget expires and aws/spans is absent' {
        # The destination transition can take longer than the wait budget. When
        # the wait expires (Status never ACTIVE) and 'aws/spans' is still
        # absent, the failure must name the still-PENDING destination (wait and
        # re-run, or check its status) rather than blaming a first-time enable.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $false -Status 'PENDING' -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability -ActiveMaxAttempts 2 -ActiveRetrySeconds 0
        } | Should -Throw '*still PENDING*'
        $mutations | Should -Contain 'xray update-trace-segment-destination'
    }

    It 'Opt-in fails with an accurate message when the aws/spans read is unreadable' {
        # A denied/throttled describe must be reported as unreadable, not as a
        # missing group, so the operator is not sent to create a group that may
        # already exist.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -SpansUnreadable $true -Mutations $mutations
        {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        } | Should -Throw '*could not be read*'
    }

    It 'Opt-in completes when aws/spans is present after the enable' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        $mutations | Should -Contain 'xray update-trace-segment-destination'
        ($script:StatusMessages -join ' ') | Should -Match 'configured \(opt-in\)'
    }

    It 'ReportOnly reports active without mutating when Transaction Search is set up' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ReportOnly
        $mutations.Count | Should -Be 0
    }

    It 'ReportOnly prints the destination toggle when the destination is CloudWatchLogs and aws/spans is absent' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $false -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ReportOnly
        $mutations.Count | Should -Be 0
        ($script:StatusMessages -join ' ') | Should -Match 'does not yet appear active'
        $host_ = $script:HostMessages -join "`n"
        # The disable/re-enable toggle is the correct remedy from CloudWatchLogs.
        $host_ | Should -Match 'update-trace-segment-destination --destination XRay'
        $host_ | Should -Match 'update-trace-segment-destination --destination CloudWatchLogs'
        # The recheck never offers the opt-in (it cannot create the group).
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'ReportOnly points to the toolkit warning and the console (no launch, no opt-in) when the destination is XRay and aws/spans is absent' {
        # The recheck runs AFTER the final launch. If the destination is still
        # XRay, the launch did not switch it, so the recheck must point to the
        # toolkit's "Transaction Search configuration failed" warning and the
        # one-time console enable, NOT back to another launch and NOT to the
        # XRay -> CloudWatchLogs toggle pair or the opt-in.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $false -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ReportOnly
        $mutations.Count | Should -Be 0
        ($script:StatusMessages -join ' ') | Should -Match 'does not yet appear active'
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'Transaction Search configuration failed'
        $host_ | Should -Match 'AWS X-Ray / CloudWatch console'
        $host_ | Should -Not -Match 'agentcore launch'
        $host_ | Should -Not -Match 'update-trace-segment-destination'
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'ReportOnly reports an existing aws/spans as present (not missing) when the destination is XRay' {
        # A launch whose Transaction Search step failed on an account that
        # already had an 'aws/spans' grant leaves the destination at XRay with
        # the group present. The recheck must NOT claim the group is missing;
        # only the destination needs switching (console or opt-in).
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ReportOnly
        $mutations.Count | Should -Be 0
        ($script:StatusMessages -join ' ') | Should -Match 'does not yet appear active'
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'already exists'
        $host_ | Should -Not -Match 'is not present'
        $host_ | Should -Match 'AWS X-Ray / CloudWatch console'
        $host_ | Should -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'ReportOnly reports unreadable aws/spans and prints no toggle commands' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -SpansUnreadable $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ReportOnly
        $mutations.Count | Should -Be 0
        ($script:StatusMessages -join ' ') | Should -Match 'does not yet appear active'
        $host_ = $script:HostMessages -join "`n"
        # Unreadable spans: confirm in the console, no toggle commands.
        $host_ | Should -Match 'could not be read'
        $host_ | Should -Not -Match 'update-trace-segment-destination'
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'ReportOnly reports unreadable aws/spans when the destination is XRay and prints no toggle or launch' {
        # XRay destination with an unreadable 'aws/spans' read: the state is
        # unknown, so the recheck must say the read failed and must NOT print a
        # toggle, the opt-in, or another launch.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansUnreadable $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ReportOnly
        $mutations.Count | Should -Be 0
        ($script:StatusMessages -join ' ') | Should -Match 'does not yet appear active'
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'could not be read'
        $host_ | Should -Not -Match 'update-trace-segment-destination'
        $host_ | Should -Not -Match 'agentcore launch'
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'Get-SpansLogGroupState classifies parsed-JSON shapes as absent and non-JSON as unreadable' -ForEach @(
        @{ Raw = '{}';   Expected = 'absent' }
        @{ Raw = '[]';   Expected = 'absent' }
        @{ Raw = 'null'; Expected = 'absent' }
        @{ Raw = '{"logGroups":null}'; Expected = 'absent' }
        @{ Raw = 'not json at all';    Expected = 'unreadable' }
    ) {
        # Feed each shape to the reader through a readable CloudWatchLogs
        # destination so the two states are distinguishable from the printed
        # remedy: 'absent' prints the disable/re-enable toggle, while
        # 'unreadable' prints 'could not be read' and no toggle. The shell
        # reader is pinned to the same mapping by
        # test_spans_reader_state_alignment.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansRaw $Raw -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ReportOnly
        $mutations.Count | Should -Be 0
        $host_ = $script:HostMessages -join "`n"
        if ($Expected -eq 'absent') {
            $host_ | Should -Match 'update-trace-segment-destination --destination XRay'
            $host_ | Should -Not -Match 'could not be read'
        } else {
            $host_ | Should -Match 'could not be read'
            $host_ | Should -Not -Match 'update-trace-segment-destination'
        }
    }

    It 'ReportOnly confirms in the console and prints no toggle when the destination is unreadable' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -DestinationUnreadable $true -SpansExists $false -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ReportOnly
        $mutations.Count | Should -Be 0
        ($script:StatusMessages -join ' ') | Should -Match 'does not yet appear active'
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'could not be read'
        $host_ | Should -Not -Match 'update-trace-segment-destination'
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'Default mode offers the opt-in only when aws/spans already exists' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        $mutations.Count | Should -Be 0
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'Default mode points to the console or launch (no opt-in) when the destination is XRay and aws/spans is absent' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $false -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        $mutations.Count | Should -Be 0
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'agentcore launch'
        $host_ | Should -Match 'AWS X-Ray / CloudWatch console'
        $host_ | Should -Not -Match 'update-trace-segment-destination'
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'Default mode prints the toggle (no opt-in) when the destination is CloudWatchLogs and aws/spans is absent' {
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $false -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        $mutations.Count | Should -Be 0
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'update-trace-segment-destination --destination XRay'
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'Default mode does not offer the opt-in when the destination is unreadable even if aws/spans is present' {
        # The opt-in refuses before any write when it cannot read the
        # destination, so an unreadable destination must not be sent to the
        # opt-in; it is treated like the unreadable branch.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -DestinationUnreadable $true -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        $mutations.Count | Should -Be 0
        $host_ = $script:HostMessages -join "`n"
        # Positive assertion: the unreadable branch is actually printed, so this
        # spec cannot pass merely because nothing was written.
        $host_ | Should -Match 'could not be read'
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
        $host_ | Should -Not -Match 'update-trace-segment-destination'
    }

    It 'Default mode under the stop switch offers the opt-in with the matching policy name' {
        # Destination XRay, 'aws/spans' present. With the stop switch set, the
        # bare default-named opt-in would halt on the toolkit's name-only check
        # and loop. The remedy must offer the matching policy name and the
        # no-other-grant condition, not the bare default opt-in.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $true -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -StopOnAccountObservabilityChange
        $mutations.Count | Should -Be 0
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME=TransactionSearchXRayAccess'
        $host_ | Should -Match 'no other'
    }

    It 'Default mode XRay/aws-spans-absent remedy drops the launch promise under the stop switch' {
        # Write-SpansRemedy's stop-switch branch: the deploy halts before
        # 'agentcore launch', so the remedy must say the stop switch is set and
        # point to the console, not promise a launch destination switch or a
        # Step 5c toggle.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $false -Mutations $mutations
        Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -StopOnAccountObservabilityChange
        $mutations.Count | Should -Be 0
        $host_ = $script:HostMessages -join "`n"
        $host_ | Should -Match 'stop switch is set'
        $host_ | Should -Match 'AWS X-Ray / CloudWatch console'
        $host_ | Should -Not -Match 'the post-launch recheck \(Step 5c\)'
        $host_ | Should -Not -Match 'Deploy-GameAgent -ConfigureAccountObservability'
    }

    It 'The stop-switch parameter default reads the environment and treats TRUE as off' -ForEach @(
        @{ EnvValue = 'true'; ExpectStopWording = $true }
        @{ EnvValue = 'TRUE'; ExpectStopWording = $false }
        @{ EnvValue = '';     ExpectStopWording = $false }
    ) {
        # When the caller omits -StopOnAccountObservabilityChange, the parameter
        # default resolves it from GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE
        # through the case-sensitive helper. On the XRay/'aws/spans'-absent path
        # the stop-switch branch prints 'stop switch is set', so its presence
        # tells us whether the env value armed the switch.
        $mutations = [System.Collections.Generic.List[string]]::new()
        Set-ObsAwsMock -Destination 'XRay' -SpansExists $false -Mutations $mutations
        $prior = $env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE
        try {
            $env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE = $EnvValue
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        } finally {
            if ($null -eq $prior) {
                Remove-Item Env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE -ErrorAction SilentlyContinue
            } else {
                $env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE = $prior
            }
        }
        $host_ = $script:HostMessages -join "`n"
        if ($ExpectStopWording) {
            $host_ | Should -Match 'stop switch is set'
        } else {
            $host_ | Should -Not -Match 'stop switch is set'
        }
    }
}


Describe 'Deploy-GameAgent recheck wiring' {
    BeforeAll {
        $script:DeployScript = (Join-Path $PSScriptRoot '..' 'Public' 'Deploy-GameAgent.ps1' | Resolve-Path)
        $script:DeployContent = Get-Content $script:DeployScript -Raw
    }

    It 'dispatches the read-only recheck after the final launch' {
        # The recheck must run after the last `agentcore launch` (Step 5b) so it
        # reports the post-launch state even on a refresh deploy.
        $lastLaunch = $script:DeployContent.LastIndexOf('agentcore launch')
        $recheck = $script:DeployContent.IndexOf('-ReportOnly')
        $lastLaunch | Should -BeGreaterThan -1
        $recheck | Should -BeGreaterThan -1
        $recheck | Should -BeGreaterThan $lastLaunch
    }

    It 'runs the recheck non-blocking under a Continue preference inside a child scope' {
        # The recheck is read-only and must not fail the deploy, even if a
        # redirected native stderr would otherwise terminate under 'Stop'. The
        # 'Continue' preference must live inside the recheck child scriptblock
        # (& { ... }); a function-scope assignment would wrongly relax the
        # surrounding 'Stop' for the frontend steps. Pin the whole shape:
        # & { $ErrorActionPreference = 'Continue'; Invoke-...-ReportOnly }.
        $script:DeployContent | Should -Match "&\s*\{\s*\r?\n\s*\`$ErrorActionPreference\s*=\s*'Continue'\s*\r?\n\s*Invoke-GameAgentAccountObservability[^\r\n]*-ReportOnly"
    }

    It 'wraps the recheck in try/catch so a terminating error cannot fail the deploy' {
        # Matches the shell call site's '|| true': any terminating error from the
        # recheck is downgraded to a non-blocking warning rather than propagating
        # through the finally and failing the deploy.
        $script:DeployContent | Should -Match "try\s*\{\s*\r?\n\s*&\s*\{"
        $script:DeployContent | Should -Match "catch\s*\{\s*\r?\n\s*Write-GameAgentStatus[^\r\n]*-Type Warning"
    }

    It 'honors the opt-in environment variable with a case-sensitive comparison' {
        # Match bash's exact 'true' check so 'TRUE' does not opt in on one shell
        # but not the other.
        $script:DeployContent | Should -Match 'GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY -ceq '
    }

    It 'passes the stop switch into the Step 1.7 account-observability call' {
        # Step 1.7 must forward -StopOnAccountObservabilityChange so the default
        # XRay/'aws/spans'-absent remedy knows the deploy halts before launch and
        # the matching-name opt-in offer is used.
        $script:DeployContent | Should -Match 'Invoke-GameAgentAccountObservability[^\r\n]*-StopOnAccountObservabilityChange'
    }
}
