function Remove-GameAgentAdmin {
    <#
    .SYNOPSIS
        Deletes a user from the Cognito user pool.
    .PARAMETER Email
        Email of the user to delete. If omitted, lists all users.
    .PARAMETER All
        Delete all users (equivalent to purge-cognito-users.sh).
    .PARAMETER Profile
        AWS CLI profile.
    .PARAMETER Region
        AWS region. Default: us-west-2.
    .EXAMPLE
        Remove-GameAgentAdmin -Email admin@example.com -Profile demo
    .EXAMPLE
        Remove-GameAgentAdmin -All -Profile demo
    #>
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
    param(
        [string]$Email,
        [switch]$All,
        [switch]$Force,
        [string]$Profile,
        [string]$Region = 'us-west-2'
    )

    $ErrorActionPreference = 'Stop'
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path

    $resolved = Resolve-GameAgentProfile -Profile $Profile
    $Profile = $resolved.Profile
    $profileArgs = $resolved.ProfileArgs

    if (-not $Email -and -not $All) { throw 'Specify -Email <address> or -All' }

    $poolId = (& aws cloudformation describe-stacks --stack-name game-agent-infrastructure --region $Region @profileArgs `
        --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text 2>&1)
    if ($LASTEXITCODE -ne 0 -or -not $poolId) { throw 'Could not find User Pool ID. Is the stack deployed?' }

    if ($Force) { $ConfirmPreference = 'None' }

    if ($All) {
        $users = (& aws cognito-idp list-users --user-pool-id $poolId --region $Region @profileArgs `
            --query 'Users[].Username' --output text) -split '\s+'
        if (-not $users -or $users[0] -eq '') {
            Write-GameAgentStatus 'No users found' -Type Warning; return
        }
        Write-Host "Users: $($users -join ', ')"
        if (-not $PSCmdlet.ShouldProcess("all $($users.Count) users in pool $poolId", 'Delete')) {
            Write-GameAgentStatus 'Cancelled' -Type Warning; return
        }
        foreach ($u in $users) {
            & aws cognito-idp admin-delete-user --user-pool-id $poolId --username $u --region $Region @profileArgs | Out-Null
            Write-GameAgentStatus "Deleted: $u" -Type Success
        }
    } else {
        if (-not $PSCmdlet.ShouldProcess("user $Email from pool $poolId", 'Delete')) {
            Write-GameAgentStatus 'Cancelled' -Type Warning; return
        }
        & aws cognito-idp admin-delete-user --user-pool-id $poolId --username $Email --region $Region @profileArgs | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to delete user $Email" }
        Write-GameAgentStatus "Deleted: $Email" -Type Success
    }

    Write-Host ''
    Write-Host 'Recreate with: Add-GameAgentAdmin -Email <email> -Password <pass>' -ForegroundColor Cyan
}
