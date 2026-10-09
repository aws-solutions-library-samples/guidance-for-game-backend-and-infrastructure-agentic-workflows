BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentAccountObservability.ps1' | Resolve-Path)

    $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    function Write-GameAgentStatus {
        param([string]$Message, [string]$Type)
        $script:StatusMessages.Add("$Type`: $Message")
    }

    # Records every `aws` invocation and returns canned, synthetic responses.
    function New-ObsAwsMock {
        param(
            [string]$Destination = 'XRay',
            [bool]$SpansExists = $false,
            [System.Collections.Generic.List[string]]$Calls
        )
        $destLocal = $Destination
        $spansLocal = $SpansExists
        return {
            param()
            $cmd = "$($args[0]) $($args[1])"
            $Calls.Add($cmd)
            switch ($cmd) {
                'sts get-caller-identity' { return '123456789012' }
                'xray get-trace-segment-destination' { return $destLocal }
                'logs describe-log-groups' {
                    if ($spansLocal) { return '{"logGroups":[{"logGroupName":"aws/spans"}]}' }
                    return '{"logGroups":[]}'
                }
                default { return '' }
            }
        }.GetNewClosure()
    }
}

Describe 'Invoke-GameAgentAccountObservability' {
    BeforeEach {
        $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    }

    It 'Default mode makes no mutation when the account already supports Transaction Search' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $mock = New-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -Calls $calls
        Set-Item -Path function:aws -Value $mock

        try {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        } finally { Remove-Item -Path function:aws -ErrorAction SilentlyContinue }

        $calls | Should -Not -Contain 'xray update-trace-segment-destination'
        $calls | Should -Not -Contain 'xray update-indexing-rule'
        $calls | Should -Not -Contain 'logs put-resource-policy'
    }

    It 'Default mode warns with the opt-in instruction when unsupported' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $mock = New-ObsAwsMock -Destination 'XRay' -SpansExists $false -Calls $calls
        Set-Item -Path function:aws -Value $mock

        try {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @()
        } finally { Remove-Item -Path function:aws -ErrorAction SilentlyContinue }

        $calls | Should -Not -Contain 'xray update-trace-segment-destination'
        ($script:StatusMessages -join ' ') | Should -Match 'does NOT yet appear to support'
    }

    It 'Opt-in enables the shared settings and never disables the destination' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $mock = New-ObsAwsMock -Destination 'XRay' -SpansExists $true -Calls $calls
        Set-Item -Path function:aws -Value $mock

        try {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        } finally { Remove-Item -Path function:aws -ErrorAction SilentlyContinue }

        $calls | Should -Contain 'logs put-resource-policy'
        $calls | Should -Contain 'xray update-trace-segment-destination'
        $calls | Should -Contain 'xray update-indexing-rule'
        ($script:StatusMessages -join ' ') | Should -Match 'opt-in is ENABLED'
    }

    It 'Opt-in preserves an existing CloudWatchLogs destination without toggling it' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $mock = New-ObsAwsMock -Destination 'CloudWatchLogs' -SpansExists $true -Calls $calls
        Set-Item -Path function:aws -Value $mock

        try {
            Invoke-GameAgentAccountObservability -Region 'us-west-2' -ProfileArgs @() -ConfigureAccountObservability
        } finally { Remove-Item -Path function:aws -ErrorAction SilentlyContinue }

        # Resource policy and indexing still applied, but destination left unchanged.
        $calls | Should -Contain 'logs put-resource-policy'
        $calls | Should -Not -Contain 'xray update-trace-segment-destination'
    }
}
