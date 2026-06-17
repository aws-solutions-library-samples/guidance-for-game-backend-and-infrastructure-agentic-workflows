function Resolve-GameAgentProfile {
    <# Resolves AWS profile from parameter or ui/.env.local. Returns @{Profile; ProfileArgs}. #>
    [CmdletBinding()]
    param([string]$Profile)

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    if (-not $Profile) {
        $envLocal = Join-Path $repoRoot 'ui/.env.local'
        if (Test-Path $envLocal) {
            $m = Select-String -Path $envLocal -Pattern '^AWS_PROFILE=(.+)' | Select-Object -First 1
            if ($m) { $Profile = $m.Matches.Groups[1].Value.Trim() }
        }
    }
    return @{
        Profile     = $Profile
        ProfileArgs = if ($Profile) { @('--profile', $Profile) } else { @() }
    }
}
