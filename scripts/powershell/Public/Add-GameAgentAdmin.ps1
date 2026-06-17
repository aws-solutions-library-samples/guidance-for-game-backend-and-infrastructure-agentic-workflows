function Add-GameAgentAdmin {
    <#
    .SYNOPSIS
        Creates an admin user in the Cognito user pool.
    .DESCRIPTION
        Supports both interactive (prompts for email/password) and non-interactive usage.
        When run interactively, the password prompt is masked.
    .PARAMETER Email
        Email address for the new user. Prompted if omitted.
    .PARAMETER Password
        Password (min 8 chars, uppercase, lowercase, number, symbol). Prompted (masked) if omitted.
    .PARAMETER Profile
        AWS CLI profile. Default: reads from ui/.env.local.
    .PARAMETER Region
        AWS region. Default: us-west-2.
    .EXAMPLE
        Add-GameAgentAdmin
        # Interactive: prompts for email and password
    .EXAMPLE
        Add-GameAgentAdmin -Email admin@example.com -Password 'MyP@ss1234' -Profile demo
    #>
    [CmdletBinding()]
    param(
        [string]$Email,
        [string]$Password,
        [string]$Profile,
        [string]$Region = 'us-west-2'
    )

    $ErrorActionPreference = 'Stop'
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path

    # Interactive prompts if not provided
    if (-not $Email) {
        $Email = Read-Host 'Enter your email address'
        if (-not $Email) { throw 'Email is required' }
    }
    if (-not $Password) {
        $securePass = Read-Host 'Enter your password (min 8 chars, uppercase, lowercase, number, symbol)' -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePass)
        try { $Password = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim() }
        finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        if (-not $Password) { throw 'Password is required' }
    }

    $resolved = Resolve-GameAgentProfile -Profile $Profile
    $Profile = $resolved.Profile
    $profileArgs = $resolved.ProfileArgs

    Write-GameAgentStatus 'Fetching User Pool ID...' -Type Info
    $poolId = (& aws cloudformation describe-stacks --stack-name game-agent-infrastructure --region $Region @profileArgs `
        --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text 2>&1)
    if ($LASTEXITCODE -ne 0 -or -not $poolId) { throw 'Could not find User Pool ID. Is the stack deployed?' }
    Write-GameAgentStatus "User Pool ID: $poolId" -Type Success

    Write-GameAgentStatus 'Creating user...' -Type Info
    & aws cognito-idp admin-create-user --user-pool-id $poolId --username $Email `
        --user-attributes "Name=email,Value=$Email" 'Name=email_verified,Value=true' `
        --message-action SUPPRESS --region $Region @profileArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create user' }

    Write-GameAgentStatus 'Setting password...' -Type Info
    # Use -- to prevent AWS CLI from interpreting password characters as flags
    $passArgs = @('cognito-idp', 'admin-set-user-password',
        '--user-pool-id', $poolId, '--username', $Email,
        '--password', $Password, '--permanent', '--region', $Region) + $profileArgs
    & aws @passArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to set password' }

    Write-GameAgentStatus 'Adding user to admin group...' -Type Info
    & aws cognito-idp admin-add-user-to-group --user-pool-id $poolId --username $Email `
        --group-name admin --region $Region @profileArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to add user to admin group' }

    Write-Host ''
    Write-GameAgentStatus 'Admin user created successfully!' -Type Success
    Write-Host "  Email:    $Email" -ForegroundColor Cyan
    Write-Host ''
}
