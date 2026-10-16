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
        touching a real venv; defaults to running the Python guard from inside
        the backend directory (the working directory `agentcore launch` resolves
        the project from, so uv's child process discovers the same environment)
        so it measures the same interpreter uv embeds into the packaged console
        scripts.
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
            # Run the shared guard from inside the backend directory — the same
            # working directory `agentcore launch` resolves the project from — so
            # uv's child process discovers the project from that physical working
            # directory and selects the same environment, and the guard measures
            # the interpreter path that actually ships. `$PWD.ProviderPath` passes
            # the location as entered (it maps only the PowerShell drive root, and
            # may keep a symlink spelling); it affects only the remedy wording,
            # which canonicalizes directories before comparing.
            Push-Location $Backend
            try {
                $physicalBackend = $PWD.ProviderPath
                $output = (uv run python $Script --backend-dir $physicalBackend 2>&1) | Out-String
                $exit = $LASTEXITCODE
            } finally { Pop-Location }
            return @{ ExitCode = $exit; Output = $output }
        }
    }

    $result = & $GuardRunner $guardScript $BackendPath
    if ($result.ExitCode -ne 0) {
        throw ("Backend venv interpreter path is not portable (issue #517):`n" + ($result.Output).ToString().Trim())
    }
}
