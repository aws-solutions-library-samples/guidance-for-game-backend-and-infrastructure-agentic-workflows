function Test-GameAgentRuntimePath {
    <#
    .SYNOPSIS
        Fail fast when the backend venv interpreter path would ship an
        unbootable AgentCore runtime (issue #517).
    .DESCRIPTION
        Delegates to the shared cross-platform preflight
        (scripts/infrastructure/check_runtime_path.py) so the shell and
        PowerShell deploy paths enforce one implementation of uv's shebang
        rule — over 124 filesystem bytes or a space in the interpreter path on
        POSIX; never a false rejection on native Windows, where uv emits a
        native launcher binary rather than a /bin/sh shebang trampoline.

        Throws (so the Stop-preference caller aborts) when the resolved
        interpreter path is unbootable, surfacing the Python guard's actionable
        remediation. Intended to run right after local `uv sync` and BEFORE the
        first AWS-mutating command, so a doomed deploy leaves no AWS side
        effects.
    .PARAMETER BackendPath
        Backend package directory containing the .venv.
    .PARAMETER RepoRoot
        Repository root, used to locate the shared preflight script.
    .PARAMETER GuardRunner
        Optional scriptblock that runs the shared guard and returns a hashtable
        with ExitCode and Output keys. Injected by tests to avoid invoking uv or
        touching a real venv; defaults to running the Python guard through the
        backend venv so it measures the same interpreter uv wrote the console
        scripts against.
    .EXAMPLE
        Test-GameAgentRuntimePath -BackendPath $backendPath -RepoRoot $repoRoot
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$BackendPath,

        [Parameter(Mandatory)]
        [string]$RepoRoot,

        [scriptblock]$GuardRunner
    )

    $guardScript = Join-Path $RepoRoot 'scripts/infrastructure/check_runtime_path.py'
    if (-not (Test-Path $guardScript)) {
        throw "Runtime path guard not found: $guardScript"
    }

    if (-not $GuardRunner) {
        $GuardRunner = {
            param([string]$Script, [string]$Backend)
            # Run the shared guard through the backend venv so it measures the
            # same interpreter uv wrote the console scripts against.
            $output = (uv run --project $Backend python $Script --backend-dir $Backend 2>&1) | Out-String
            return @{ ExitCode = $LASTEXITCODE; Output = $output }
        }
    }

    $result = & $GuardRunner $guardScript $BackendPath
    if ($result.ExitCode -ne 0) {
        throw ("Backend venv interpreter path is not portable (issue #517):`n" + ($result.Output).ToString().Trim())
    }
}
