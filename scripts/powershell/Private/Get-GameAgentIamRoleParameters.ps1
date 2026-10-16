function Get-GameAgentIamRoleParameters {
    <#
    Resolves the optional IAM role placement used by every stack that creates a
    role, matching scripts/deploy.sh: the process environment wins, then
    ui/.env.local, then the root path and no permissions boundary. Returns the
    RolePath= and PermissionsBoundaryArn= CloudFormation parameter overrides.
    #>
    [CmdletBinding()]
    param()

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $envLocal = Join-Path $repoRoot 'ui/.env.local'

    $values = @{}
    foreach ($name in @('GBAW_IAM_ROLE_PATH', 'GBAW_IAM_PERMISSIONS_BOUNDARY_ARN')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if (-not $value -and (Test-Path $envLocal)) {
            $match = Select-String -Path $envLocal -Pattern "^$name=(.*)$" | Select-Object -Last 1
            if ($match) { $value = ($match.Matches[0].Groups[1].Value -replace '\s', '') }
        }
        $values[$name] = $value
    }

    $rolePath = if ($values['GBAW_IAM_ROLE_PATH']) { $values['GBAW_IAM_ROLE_PATH'] } else { '/' }
    $boundaryArn = if ($values['GBAW_IAM_PERMISSIONS_BOUNDARY_ARN']) { $values['GBAW_IAM_PERMISSIONS_BOUNDARY_ARN'] } else { '' }

    return @("RolePath=$rolePath", "PermissionsBoundaryArn=$boundaryArn")
}
