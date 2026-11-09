function Invoke-GameAgentPlatformVersion {
    <#
    .SYNOPSIS
        Waits for, or converges, the AgentCore Runtime platform version.
    .DESCRIPTION
        Delegates to the shared helper scripts/infrastructure/agentcore_platform_version.py,
        the same helper scripts/deploy.sh runs, from the backend directory so it
        uses the backend's locked boto3/botocore. The AWS profile and Region come
        from the caller's environment and -Region, as for the other Python steps.

        -PlatformVersion converges the runtime on V1 or V2. The helper reads the
        runtime and sends back every other configuration field unchanged, makes
        no update when the runtime already reports the requested version, and
        verifies the result.

        -WaitOnly waits for the runtime to reach a terminal status without
        changing it. Run it before an `agentcore launch` that updates the
        runtime: on platform version V2 a create or update takes several minutes,
        and an update issued before a terminal status fails with ConflictException.

        Throws on a non-zero helper exit so the Stop-preference caller aborts
        (fail closed). The helper output is streamed, not captured; it contains
        field names, versions, and statuses only, never configuration values.
    .PARAMETER RuntimeId
        AgentCore runtime ID.
    .PARAMETER Region
        AWS Region of the runtime.
    .PARAMETER PlatformVersion
        Requested platform version, V1 or V2 (case-sensitive).
    .PARAMETER WaitOnly
        Only wait for a terminal runtime status; never update.
    .PARAMETER RepoRoot
        Repository root used to locate the helper. Defaults to this module's repository.
    .PARAMETER Runner
        Optional scriptblock that runs the helper. It receives the helper path,
        the backend path, and the helper argument array, and returns a hashtable
        with an ExitCode key. Injected by tests to avoid invoking uv or AWS.
    .EXAMPLE
        Invoke-GameAgentPlatformVersion -RuntimeId $runtimeId -Region $Region -WaitOnly
    .EXAMPLE
        Invoke-GameAgentPlatformVersion -RuntimeId $runtimeId -Region $Region -PlatformVersion 'V2'
    #>
    [CmdletBinding(DefaultParameterSetName = 'Apply')]
    param(
        [Parameter(Mandatory)]
        [string]$RuntimeId,

        [Parameter(Mandatory)]
        [string]$Region,

        [Parameter(Mandatory, ParameterSetName = 'Apply')]
        [ValidateSet('V1', 'V2', IgnoreCase = $false)]
        [string]$PlatformVersion,

        [Parameter(Mandatory, ParameterSetName = 'Wait')]
        [switch]$WaitOnly,

        [string]$RepoRoot,

        [scriptblock]$Runner
    )

    if (-not $RepoRoot) {
        $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    }
    $helperScript = Join-Path $RepoRoot 'scripts/infrastructure/agentcore_platform_version.py'
    if (-not (Test-Path $helperScript)) {
        throw "AgentCore platform version helper not found: $helperScript"
    }
    $backendPath = Join-Path $RepoRoot 'backend'

    $helperArgs = @('--runtime-id', $RuntimeId, '--region', $Region)
    if ($WaitOnly) {
        $helperArgs += '--wait-only'
    } else {
        $helperArgs += @('--platform-version', $PlatformVersion)
    }

    if (-not $Runner) {
        $Runner = {
            param([string]$Script, [string]$Backend, [string[]]$Arguments)
            # Run from the backend directory so uv selects the backend project
            # environment (locked boto3/botocore). Stdout is streamed to the host
            # (Out-Host) instead of becoming this scriptblock's output, and
            # stderr is not redirected, so native stderr never becomes a
            # terminating error under the caller's Stop preference.
            Push-Location $Backend
            try {
                uv run python $Script @Arguments | Out-Host
                $exit = $LASTEXITCODE
            } finally { Pop-Location }
            return @{ ExitCode = $exit }
        }
    }

    $result = & $Runner $helperScript $backendPath $helperArgs
    if ($result.ExitCode -ne 0) {
        $action = if ($WaitOnly) { 'wait for a terminal runtime status' } else { "apply platform version $PlatformVersion" }
        throw "AgentCore platform version step failed to $action (exit code $($result.ExitCode))."
    }
}
