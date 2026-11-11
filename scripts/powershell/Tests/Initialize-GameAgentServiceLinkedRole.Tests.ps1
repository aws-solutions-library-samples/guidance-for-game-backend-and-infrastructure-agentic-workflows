BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Initialize-GameAgentServiceLinkedRole.ps1' | Resolve-Path)

    $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    function Write-GameAgentStatus {
        param([string]$Message, [string]$Type)
        $script:StatusMessages.Add("$Type`: $Message")
    }

    # Record waits instead of sleeping.
    $script:Sleeps = [System.Collections.Generic.List[int]]::new()
    function Start-Sleep {
        param([int]$Seconds)
        $script:Sleeps.Add($Seconds)
    }

    $script:NoSuchEntity = 'An error occurred (NoSuchEntity) when calling the GetRole operation: The role with name AWSServiceRoleForECS cannot be found.'
    $script:Taken = 'An error occurred (InvalidInput) when calling the CreateServiceLinkedRole operation: Service role name AWSServiceRoleForECS has been taken in this account, please try a different suffix.'

    # Builds a stateful fake over synthetic IAM state. It answers only the two
    # calls the function is expected to send, with the expected arguments, and
    # throws on any other call so a changed request fails the spec. A
    # successful create (or a "has been taken" create) makes the role visible
    # to later get-role calls unless -NeverVisible is set.
    function New-TestIamInvoker {
        param(
            [bool]$RoleExists = $false,
            [string]$GetRoleError = $script:NoSuchEntity,
            [string]$CreateError = '',
            [bool]$CreateMakesRole = $true,
            [bool]$NeverVisible = $false,
            [System.Collections.Generic.List[string]]$Calls
        )

        $state = @{ RoleExists = $RoleExists }
        $expectedGet = 'iam get-role --role-name AWSServiceRoleForECS --region us-west-2 --query Role.RoleName --output text'
        $expectedCreate = 'iam create-service-linked-role --aws-service-name ecs.amazonaws.com --region us-west-2 --query Role.RoleName --output text'

        return {
            param([string[]]$AwsArgs)
            $joined = $AwsArgs -join ' '
            $operation = "$($AwsArgs[0]) $($AwsArgs[1])"
            $Calls.Add($operation)

            switch ($joined) {
                $expectedGet {
                    if ($state.RoleExists -and -not $NeverVisible) { return 'AWSServiceRoleForECS' }
                    throw "AWS IAM command failed: $GetRoleError"
                }
                $expectedCreate {
                    if ($CreateMakesRole) { $state.RoleExists = $true }
                    if ($CreateError) { throw "AWS IAM command failed: $CreateError" }
                    return 'AWSServiceRoleForECS'
                }
                default { throw "Unexpected AWS call: $joined" }
            }
        }.GetNewClosure()
    }

    function Invoke-TestServiceLinkedRole {
        param([scriptblock]$InvokeAws, [string]$PropagationSeconds = '0', [string]$PollSeconds = '0')
        Initialize-GameAgentServiceLinkedRole `
            -ServiceName 'ecs.amazonaws.com' `
            -RoleName 'AWSServiceRoleForECS' `
            -Region 'us-west-2' `
            -InvokeAws $InvokeAws `
            -PropagationSeconds $PropagationSeconds `
            -PollSeconds $PollSeconds
    }
}

Describe 'Initialize-GameAgentServiceLinkedRole' {
    BeforeEach {
        $script:StatusMessages.Clear()
        $script:Sleeps.Clear()
    }

    It 'Neither creates nor waits when the role already exists' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestIamInvoker -RoleExists $true -Calls $calls

        Invoke-TestServiceLinkedRole -InvokeAws $invokeAws -PropagationSeconds '15'

        ($calls -join ',') | Should -Be 'iam get-role'
        $script:Sleeps | Should -HaveCount 0
        $script:StatusMessages | Should -Contain 'Success: Service-linked role AWSServiceRoleForECS already exists'
    }

    It 'Creates a missing role, waits until it is visible, then waits the propagation delay' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestIamInvoker -Calls $calls

        Invoke-TestServiceLinkedRole -InvokeAws $invokeAws -PropagationSeconds '7'

        ($calls -join ',') | Should -Be 'iam get-role,iam create-service-linked-role,iam get-role'
        ($script:Sleeps -join ',') | Should -Be '7'
        $script:StatusMessages | Should -Contain 'Success: Created service-linked role AWSServiceRoleForECS'
        $script:StatusMessages | Should -Contain 'Success: Service-linked role AWSServiceRoleForECS ready'
    }

    It 'Uses a 15 second propagation delay when none is set' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestIamInvoker -Calls $calls

        Invoke-TestServiceLinkedRole -InvokeAws $invokeAws -PropagationSeconds ''

        ($script:Sleeps -join ',') | Should -Be '15'
    }

    It 'Treats a role that has been taken in this account as created' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestIamInvoker -CreateError $script:Taken -Calls $calls

        Invoke-TestServiceLinkedRole -InvokeAws $invokeAws

        ($calls -join ',') | Should -Be 'iam get-role,iam create-service-linked-role,iam get-role'
        $script:StatusMessages | Should -Contain 'Info: Service-linked role AWSServiceRoleForECS was created concurrently'
    }

    It 'Fails closed without creating when get-role is denied' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $denied = 'An error occurred (AccessDenied) when calling the GetRole operation: User: arn:aws:sts::123456789012:assumed-role/Example/session is not authorized'
        $invokeAws = New-TestIamInvoker -GetRoleError $denied -Calls $calls

        { Invoke-TestServiceLinkedRole -InvokeAws $invokeAws } |
            Should -Throw '*Read service-linked role AWSServiceRoleForECS failed (AccessDenied)*'

        ($calls -join ',') | Should -Be 'iam get-role'
    }

    It 'Does not include raw provider output in the thrown message' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $denied = 'An error occurred (AccessDenied) when calling the GetRole operation: User: arn:aws:sts::123456789012:assumed-role/Example/session is not authorized'
        $invokeAws = New-TestIamInvoker -GetRoleError $denied -Calls $calls

        $thrown = $null
        try { Invoke-TestServiceLinkedRole -InvokeAws $invokeAws } catch { $thrown = $_.Exception.Message }

        $thrown | Should -Not -BeNullOrEmpty
        $thrown | Should -Not -Match '123456789012'
        $thrown | Should -Not -Match 'arn:aws'
    }

    It 'Fails on any other create error' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $createDenied = 'An error occurred (AccessDenied) when calling the CreateServiceLinkedRole operation: not authorized'
        $invokeAws = New-TestIamInvoker -CreateError $createDenied -CreateMakesRole $false -Calls $calls

        { Invoke-TestServiceLinkedRole -InvokeAws $invokeAws } |
            Should -Throw '*Create service-linked role AWSServiceRoleForECS failed (AccessDenied)*'

        ($calls -join ',') | Should -Be 'iam get-role,iam create-service-linked-role'
        $script:Sleeps | Should -HaveCount 0
    }

    It 'Does not treat free text that mentions taken without InvalidInput as created' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $other = 'An error occurred (LimitExceeded) when calling the CreateServiceLinkedRole operation: has been taken in this account'
        $invokeAws = New-TestIamInvoker -CreateError $other -CreateMakesRole $false -Calls $calls

        { Invoke-TestServiceLinkedRole -InvokeAws $invokeAws } |
            Should -Throw '*Create service-linked role AWSServiceRoleForECS failed (LimitExceeded)*'
    }

    It 'Fails after a bounded number of checks when the role never becomes visible' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestIamInvoker -NeverVisible $true -Calls $calls

        { Invoke-TestServiceLinkedRole -InvokeAws $invokeAws } |
            Should -Throw '*not visible after 10 checks (NoSuchEntity)*'

        @($calls | Where-Object { $_ -eq 'iam get-role' }) | Should -HaveCount 11
        $script:Sleeps | Should -HaveCount 0
    }

    It 'Rejects an invalid propagation delay before any AWS call' -ForEach @(
        @{ Value = '121' }
        @{ Value = '-1' }
        @{ Value = 'abc' }
        @{ Value = '1.5' }
        @{ Value = '1000' }
    ) {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestIamInvoker -Calls $calls

        { Invoke-TestServiceLinkedRole -InvokeAws $invokeAws -PropagationSeconds $Value } |
            Should -Throw '*GBAW_SLR_PROPAGATION_SECONDS must be an integer from 0 to 120*'

        $calls | Should -HaveCount 0
    }

    It 'Skips the wait when the propagation delay is 0' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestIamInvoker -Calls $calls

        Invoke-TestServiceLinkedRole -InvokeAws $invokeAws -PropagationSeconds '0'

        $script:Sleeps | Should -HaveCount 0
        $script:StatusMessages | Should -Contain 'Success: Service-linked role AWSServiceRoleForECS ready'
    }

    It 'Accepts the maximum propagation delay of 120 seconds' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestIamInvoker -Calls $calls

        Invoke-TestServiceLinkedRole -InvokeAws $invokeAws -PropagationSeconds '120'

        ($script:Sleeps -join ',') | Should -Be '120'
    }
}
