BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Test-GameAgentRuntimePath.ps1' | Resolve-Path)

    # Repository root (…/scripts/powershell/Tests -> up three) so the guard
    # script path resolves to the real shared preflight.
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' '..' '..')).Path
    $BackendPath = Join-Path $RepoRoot 'backend'

    # A GuardRunner that records its invocation and returns a scripted result,
    # so the test never invokes uv or touches a real venv.
    function New-GuardRunner {
        param(
            [int]$ExitCode,
            [string]$Output,
            [System.Collections.Generic.List[string]]$Calls
        )
        return {
            param([string]$Script, [string]$Backend)
            $Calls.Add("$Script|$Backend")
            return @{ ExitCode = $ExitCode; Output = $Output }
        }.GetNewClosure()
    }
}

Describe 'Test-GameAgentRuntimePath' {
    It 'Returns a measured-pass result when the shared guard reports a bootable path' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $runner = New-GuardRunner -ExitCode 0 -Output '' -Calls $calls

        $result = $null
        { $result = Test-GameAgentRuntimePath -BackendPath $BackendPath -RepoRoot $RepoRoot -GuardRunner $runner } |
            Should -Not -Throw
        $calls.Count | Should -Be 1
        $result.Skipped | Should -BeFalse
    }

    It 'Returns a skipped result without throwing when the guard skips on native Windows' {
        # Exit code 3 is the guard's dedicated "skipped" signal (a native Windows
        # host, out of scope; see #610). It is non-fatal: the guard measured
        # nothing, so the function must not throw and must report Skipped so the
        # caller prints the skip line instead of the Success line.
        $calls = [System.Collections.Generic.List[string]]::new()
        $runner = New-GuardRunner -ExitCode 3 `
            -Output 'RUNTIME_PATH_CHECK_SKIPPED_NATIVE_WINDOWS Runtime interpreter path not checked on native Windows (see #610)' `
            -Calls $calls

        $result = $null
        { $result = Test-GameAgentRuntimePath -BackendPath $BackendPath -RepoRoot $RepoRoot -GuardRunner $runner } |
            Should -Not -Throw
        $calls.Count | Should -Be 1
        $result.Skipped | Should -BeTrue
        $result.Output | Should -Match '610'
    }

    It 'Throws with the guard remediation when the path is unbootable' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $runner = New-GuardRunner -ExitCode 1 `
            -Output 'Backend venv interpreter path is 139 filesystem bytes (over the 124-byte limit).' `
            -Calls $calls

        { Test-GameAgentRuntimePath -BackendPath $BackendPath -RepoRoot $RepoRoot -GuardRunner $runner } |
            Should -Throw '*124-byte limit*'
    }

    It 'Invokes the shared cross-platform guard script, not a reimplementation' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $runner = New-GuardRunner -ExitCode 0 -Output '' -Calls $calls

        Test-GameAgentRuntimePath -BackendPath $BackendPath -RepoRoot $RepoRoot -GuardRunner $runner

        $parts = $calls[0] -split '\|', 2
        $parts[0] | Should -Be (Join-Path $RepoRoot 'scripts/infrastructure/check_runtime_path.py')
        $parts[1] | Should -Be $BackendPath
    }

    It 'Throws before any guard run when the shared guard script is missing' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $runner = New-GuardRunner -ExitCode 0 -Output '' -Calls $calls

        { Test-GameAgentRuntimePath -BackendPath $BackendPath -RepoRoot (Join-Path $PSScriptRoot 'no-such-root') -GuardRunner $runner } |
            Should -Throw '*Runtime path guard not found*'
        $calls.Count | Should -Be 0
    }

    It 'Default runner runs the shared guard from the physical backend directory' {
        # Selection correctness: the default GuardRunner must run the shared
        # Python guard inside the uv-selected environment, from the backend
        # directory `agentcore launch` resolves the project from, so uv's child
        # process discovers the same environment and the guard measures the real
        # <env>/bin/python3 (honoring UV_PROJECT_ENVIRONMENT) rather than a
        # reconstructed path. Assert against the function body so the test needs
        # no uv or real venv.
        $body = (Get-Command Test-GameAgentRuntimePath).ScriptBlock.ToString()
        $body | Should -Match 'Push-Location \$Backend'
        $body | Should -Match '\$PWD\.ProviderPath'
        $body | Should -Match 'uv run python \$Script'
        # The default runner must not pin a logical --project path that bypasses
        # the physical working directory uv's child process would discover.
        $body | Should -Not -Match 'uv run --project \$Backend'
    }
}
