BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentPlatformVersion.ps1' | Resolve-Path)

    # Repository root (…/scripts/powershell/Tests -> up three) so the helper path
    # resolves to the real shared Python helper.
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' '..' '..')).Path

    # A Runner that records each invocation and returns a scripted exit code, so
    # the tests never invoke uv or AWS.
    function New-HelperRunner {
        param(
            [int]$ExitCode,
            [System.Collections.Generic.List[object]]$Calls
        )
        return {
            param([string]$Script, [string]$Backend, [string[]]$Arguments)
            $Calls.Add(@{ Script = $Script; Backend = $Backend; Arguments = $Arguments })
            return @{ ExitCode = $ExitCode }
        }.GetNewClosure()
    }
}

Describe 'Invoke-GameAgentPlatformVersion' {
    It 'Runs the shared helper from the backend directory to apply a platform version' {
        $calls = [System.Collections.Generic.List[object]]::new()
        $runner = New-HelperRunner -ExitCode 0 -Calls $calls

        Invoke-GameAgentPlatformVersion -RuntimeId 'gameagentruntime-EXAMPLE123' -Region 'us-west-2' `
            -PlatformVersion 'V2' -RepoRoot $RepoRoot -Runner $runner

        $calls.Count | Should -Be 1
        $calls[0].Script | Should -Be (Join-Path $RepoRoot 'scripts/infrastructure/agentcore_platform_version.py')
        $calls[0].Backend | Should -Be (Join-Path $RepoRoot 'backend')
        ($calls[0].Arguments -join ' ') |
            Should -BeExactly '--runtime-id gameagentruntime-EXAMPLE123 --region us-west-2 --platform-version V2'
    }

    It 'Passes --wait-only and no platform version when waiting' {
        $calls = [System.Collections.Generic.List[object]]::new()
        $runner = New-HelperRunner -ExitCode 0 -Calls $calls

        Invoke-GameAgentPlatformVersion -RuntimeId 'gameagentruntime-EXAMPLE123' -Region 'us-west-2' `
            -WaitOnly -RepoRoot $RepoRoot -Runner $runner

        ($calls[0].Arguments -join ' ') |
            Should -BeExactly '--runtime-id gameagentruntime-EXAMPLE123 --region us-west-2 --wait-only'
    }

    It 'Throws when the helper exits non-zero (fail closed)' {
        $calls = [System.Collections.Generic.List[object]]::new()
        $runner = New-HelperRunner -ExitCode 1 -Calls $calls

        { Invoke-GameAgentPlatformVersion -RuntimeId 'gameagentruntime-EXAMPLE123' -Region 'us-west-2' `
            -PlatformVersion 'V2' -RepoRoot $RepoRoot -Runner $runner } |
            Should -Throw '*apply platform version V2*exit code 1*'
    }

    It 'Throws when the wait step exits non-zero' {
        $calls = [System.Collections.Generic.List[object]]::new()
        $runner = New-HelperRunner -ExitCode 1 -Calls $calls

        { Invoke-GameAgentPlatformVersion -RuntimeId 'gameagentruntime-EXAMPLE123' -Region 'us-west-2' `
            -WaitOnly -RepoRoot $RepoRoot -Runner $runner } |
            Should -Throw '*wait for a terminal runtime status*'
    }

    It 'Rejects a platform version that is not exactly V1 or V2 before running anything' {
        $calls = [System.Collections.Generic.List[object]]::new()
        $runner = New-HelperRunner -ExitCode 0 -Calls $calls

        { Invoke-GameAgentPlatformVersion -RuntimeId 'gameagentruntime-EXAMPLE123' -Region 'us-west-2' `
            -PlatformVersion 'v2' -RepoRoot $RepoRoot -Runner $runner } | Should -Throw
        $calls.Count | Should -Be 0
    }

    It 'Throws before any run when the shared helper is missing' {
        $calls = [System.Collections.Generic.List[object]]::new()
        $runner = New-HelperRunner -ExitCode 0 -Calls $calls

        { Invoke-GameAgentPlatformVersion -RuntimeId 'gameagentruntime-EXAMPLE123' -Region 'us-west-2' `
            -PlatformVersion 'V1' -RepoRoot (Join-Path $PSScriptRoot 'no-such-root') -Runner $runner } |
            Should -Throw '*helper not found*'
        $calls.Count | Should -Be 0
    }

    It 'Default runner streams the helper through uv from the backend directory' {
        # Assert against the function body so the test needs no uv or AWS.
        $body = (Get-Command Invoke-GameAgentPlatformVersion).ScriptBlock.ToString()
        $body | Should -Match 'Push-Location \$Backend'
        $body | Should -Match 'uv run python \$Script @Arguments \| Out-Host'
        $body | Should -Match '\$exit = \$LASTEXITCODE'
    }
}

Describe 'Deploy-GameAgent platform version wiring' {
    BeforeAll {
        $deployScript = Get-Content -Raw (Join-Path $PSScriptRoot '..' 'Public' 'Deploy-GameAgent.ps1' | Resolve-Path)
    }

    It 'Resolves the shared setting before the first AWS call' {
        $resolveAt = $deployScript.IndexOf('Resolve-GameAgentPlatformVersion -Value $env:GBAW_AGENTCORE_PLATFORM_VERSION')
        $firstAwsAt = $deployScript.IndexOf('Invoke-Aws sts get-caller-identity')
        $resolveAt | Should -BeGreaterThan -1
        $firstAwsAt | Should -BeGreaterThan $resolveAt
    }

    It 'Waits before the final launch and applies the version after it' {
        $launchAt = $deployScript.LastIndexOf('uv run agentcore launch --auto-update-on-conflict @agentCoreEnvArgs')
        $waitAt = $deployScript.IndexOf('Invoke-GameAgentPlatformVersion -RuntimeId $runtimeId -Region $Region -WaitOnly')
        $applyAt = $deployScript.IndexOf('Invoke-GameAgentPlatformVersion -RuntimeId $runtimeId -Region $Region -PlatformVersion $agentCorePlatformVersion')
        $waitAt | Should -BeGreaterThan -1
        $launchAt | Should -BeGreaterThan $waitAt
        $applyAt | Should -BeGreaterThan $launchAt
    }
}
