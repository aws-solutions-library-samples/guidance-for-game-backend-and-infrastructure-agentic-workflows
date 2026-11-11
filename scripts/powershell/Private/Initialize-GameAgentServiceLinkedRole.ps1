function Initialize-GameAgentServiceLinkedRole {
    <#
    .SYNOPSIS
        Ensures an AWS service-linked role exists and is usable before a stack needs it.
    .DESCRIPTION
        Mirrors scripts/infrastructure/ensure-service-linked-role.sh. Some AWS
        services create their service-linked role on first use; in an account that
        has never used the service, the new role may not yet be usable when the
        service assumes it (issue #772: the ECS Express frontend failed with
        "Unable to assume the service linked role").

        - get-role succeeds: the role exists; nothing is created and nothing waits.
        - get-role reports NoSuchEntity: the role is created. A concurrent creation
          (InvalidInput, "has been taken in this account") counts as created.
        - get-role fails for any other reason (AccessDenied, throttling, ...): the
          function throws without attempting a create.
        - After a create, get-role is polled (bounded) until the role is visible,
          then the function waits PropagationSeconds.

        Errors are classified from the modeled AWS error CODE. Thrown messages
        name only the operation and that code; raw provider output is never
        included.
    .PARAMETER ServiceName
        The AWS service principal, for example ecs.amazonaws.com.
    .PARAMETER RoleName
        The service-linked role name, for example AWSServiceRoleForECS.
    .PARAMETER Region
        The AWS region passed to the CLI.
    .PARAMETER InvokeAws
        A scriptblock taking the AWS CLI arguments, normally from
        New-GameAgentAwsInvoker. It returns stdout and throws on a non-zero exit
        with the CLI stderr in the message.
    .PARAMETER PropagationSeconds
        The wait after a create, normally $env:GBAW_SLR_PROPAGATION_SECONDS. Empty
        selects 15; otherwise it must be an integer from 0 to 120.
    .PARAMETER PollSeconds
        The interval between visibility checks after a create, normally
        $env:GBAW_SLR_POLL_SECONDS. Empty selects 3; otherwise 0 to 30.
    .PARAMETER PollAttempts
        The number of visibility checks after a create.
    .EXAMPLE
        $invoker = New-GameAgentAwsInvoker -ProfileArgs $profileArgs -Label 'AWS IAM command'
        Initialize-GameAgentServiceLinkedRole -ServiceName 'ecs.amazonaws.com' `
            -RoleName 'AWSServiceRoleForECS' -Region $Region -InvokeAws $invoker `
            -PropagationSeconds $env:GBAW_SLR_PROPAGATION_SECONDS
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidatePattern('^[a-z0-9][a-z0-9.-]{0,127}$')]
        [string]$ServiceName,

        [Parameter(Mandatory)]
        [ValidatePattern('^[A-Za-z0-9+=,.@_-]{1,64}$')]
        [string]$RoleName,

        [Parameter(Mandatory)]
        [string]$Region,

        [Parameter(Mandatory)]
        [scriptblock]$InvokeAws,

        [AllowNull()]
        [AllowEmptyString()]
        [string]$PropagationSeconds,

        [AllowNull()]
        [AllowEmptyString()]
        [string]$PollSeconds,

        [ValidateRange(1, 60)]
        [int]$PollAttempts = 10
    )

    # Same rule as the shell helper: empty selects the default, otherwise an
    # integer of at most three digits no greater than the maximum.
    function ConvertTo-BoundedSeconds {
        param([string]$Name, [string]$Value, [int]$Default, [int]$Maximum)
        if ([string]::IsNullOrEmpty($Value)) { return $Default }
        if ($Value -notmatch '^[0-9]{1,3}$' -or [int]$Value -gt $Maximum) {
            throw "$Name must be an integer from 0 to $Maximum."
        }
        return [int]$Value
    }

    $propagationDelay = ConvertTo-BoundedSeconds 'GBAW_SLR_PROPAGATION_SECONDS' $PropagationSeconds 15 120
    $pollDelay = ConvertTo-BoundedSeconds 'GBAW_SLR_POLL_SECONDS' $PollSeconds 3 30

    function Get-AwsErrorCode {
        param([string]$ErrorText)
        if ($ErrorText -cmatch '\(([A-Za-z0-9]+)\)') { return $Matches[1] }
        return 'unknown'
    }

    $getRoleArgs = @(
        'iam', 'get-role',
        '--role-name', $RoleName,
        '--region', $Region,
        '--query', 'Role.RoleName',
        '--output', 'text'
    )

    Write-GameAgentStatus "Checking service-linked role $RoleName ($ServiceName)..." -Type Info
    try {
        & $InvokeAws $getRoleArgs | Out-Null
        Write-GameAgentStatus "Service-linked role $RoleName already exists" -Type Success
        return
    } catch {
        $getRoleError = Get-AwsErrorCode $_.Exception.Message
    }

    if ($getRoleError -cne 'NoSuchEntity') {
        throw ("Read service-linked role $RoleName failed ($getRoleError). The deployment cannot tell " +
            'whether the role exists, so it did not try to create it. Allow iam:GetRole and ' +
            'iam:CreateServiceLinkedRole for this identity, or create the role once and re-run: ' +
            "aws iam create-service-linked-role --aws-service-name $ServiceName")
    }

    Write-GameAgentStatus "Service-linked role $RoleName not found; creating it..." -Type Info
    try {
        & $InvokeAws @(
            'iam', 'create-service-linked-role',
            '--aws-service-name', $ServiceName,
            '--region', $Region,
            '--query', 'Role.RoleName',
            '--output', 'text'
        ) | Out-Null
        Write-GameAgentStatus "Created service-linked role $RoleName" -Type Success
    } catch {
        $message = $_.Exception.Message
        $createError = Get-AwsErrorCode $message
        if ($createError -ceq 'InvalidInput' -and $message.Contains('has been taken in this account')) {
            # Another caller (often the AWS service itself) created it concurrently.
            Write-GameAgentStatus "Service-linked role $RoleName was created concurrently" -Type Info
        } else {
            throw ("Create service-linked role $RoleName failed ($createError). Allow " +
                'iam:CreateServiceLinkedRole for this identity, or create the role once and re-run: ' +
                "aws iam create-service-linked-role --aws-service-name $ServiceName")
        }
    }

    # The role was just created: wait (bounded) until IAM returns it, then give
    # it time to become usable by the service that will assume it.
    $visible = $false
    $lastError = 'unknown'
    for ($attempt = 1; $attempt -le $PollAttempts; $attempt++) {
        try {
            & $InvokeAws $getRoleArgs | Out-Null
            $visible = $true
            break
        } catch {
            $lastError = Get-AwsErrorCode $_.Exception.Message
        }
        if ($attempt -lt $PollAttempts -and $pollDelay -gt 0) { Start-Sleep -Seconds $pollDelay }
    }

    if (-not $visible) {
        throw ("Service-linked role $RoleName not visible after $PollAttempts checks ($lastError). " +
            'Re-run the deployment once IAM reports the role.')
    }

    if ($propagationDelay -gt 0) {
        Write-GameAgentStatus "Waiting ${propagationDelay}s for $RoleName to propagate..." -Type Info
        Start-Sleep -Seconds $propagationDelay
    }
    Write-GameAgentStatus "Service-linked role $RoleName ready" -Type Success
}
