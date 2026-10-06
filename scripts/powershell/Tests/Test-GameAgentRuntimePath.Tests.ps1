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
    It 'Passes silently when the shared guard reports a bootable path' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $runner = New-GuardRunner -ExitCode 0 -Output '' -Calls $calls

        { Test-GameAgentRuntimePath -BackendPath $BackendPath -RepoRoot $RepoRoot -GuardRunner $runner } |
            Should -Not -Throw
        $calls.Count | Should -Be 1
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

        $calls[0] | Should -BeLike '*scripts/infrastructure/check_runtime_path.py*'
        $calls[0] | Should -BeLike "*$BackendPath*"
    }

    It 'Throws before any guard run when the shared guard script is missing' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $runner = New-GuardRunner -ExitCode 0 -Output '' -Calls $calls

        { Test-GameAgentRuntimePath -BackendPath $BackendPath -RepoRoot (Join-Path $PSScriptRoot 'no-such-root') -GuardRunner $runner } |
            Should -Throw '*Runtime path guard not found*'
        $calls.Count | Should -Be 0
    }
}
