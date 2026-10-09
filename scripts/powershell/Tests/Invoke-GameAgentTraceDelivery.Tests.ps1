BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentTraceDelivery.ps1' | Resolve-Path)

    $script:RuntimeArn = 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1'

    function New-TestDeliveryInvoker {
        param(
            [string]$CreateError = '',
            [string[]]$CreateErrorSequence = @(),
            [string]$SourceConflict = '',
            [string]$DestConflict = '',
            [string[]]$DescribeErrorSequence = @(),
            [string]$DescribeSourceName = 'rt-1-traces-source',
            [string]$SourceResourceArn = 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1',
            [string]$DestType = 'XRAY',
            [System.Collections.Generic.List[string]]$Calls
        )

        $createQueue = [System.Collections.Generic.Queue[string]]::new()
        foreach ($value in $CreateErrorSequence) { $createQueue.Enqueue($value) }
        $describeQueue = [System.Collections.Generic.Queue[string]]::new()
        foreach ($value in $DescribeErrorSequence) { $describeQueue.Enqueue($value) }
        $destArn = 'arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination'

        return {
            param([string[]]$AwsArgs)
            $operation = "$($AwsArgs[0]) $($AwsArgs[1])"
            $Calls.Add($operation)

            switch ($operation) {
                'logs put-delivery-source' {
                    if ($SourceConflict) { throw $SourceConflict }
                    return '{}'
                }
                'logs put-delivery-destination' {
                    if ($DestConflict) { throw $DestConflict }
                    return "{""deliveryDestination"":{""arn"":""$destArn""}}"
                }
                'logs get-delivery-source' {
                    return "{""deliverySource"":{""name"":""rt-1-traces-source"",""logType"":""TRACES"",""resourceArns"":[""$SourceResourceArn""]}}"
                }
                'logs get-delivery-destination' {
                    # The ARN-resolution path passes --query; return the bare ARN
                    # for --output text, else the full JSON object.
                    if ($AwsArgs -contains '--query') { return $destArn }
                    return "{""deliveryDestination"":{""deliveryDestinationType"":""$DestType"",""arn"":""$destArn""}}"
                }
                'logs create-delivery' {
                    $err = ''
                    if ($createQueue.Count -gt 0) { $err = $createQueue.Dequeue() }
                    elseif ($CreateError) { $err = $CreateError }
                    if ($err) { throw $err }
                    return '{}'
                }
                'logs describe-deliveries' {
                    if ($describeQueue.Count -gt 0) {
                        $err = $describeQueue.Dequeue()
                        if ($err) { throw $err }
                    }
                    return "{""deliveries"":[{""deliverySourceName"":""$DescribeSourceName"",""deliveryDestinationArn"":""$destArn"",""deliveryDestinationType"":""XRAY""}]}"
                }
                default { throw "Unexpected operation: $operation" }
            }
        }.GetNewClosure()
    }
}

Describe 'Invoke-GameAgentTraceDelivery' {
    It 'Succeeds when delivery is created and verified active' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker -Calls $calls

        Invoke-GameAgentTraceDelivery `
            -RuntimeId 'rt-1' `
            -RuntimeArn $script:RuntimeArn `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -RetrySeconds 0

        $calls | Should -Contain 'logs describe-deliveries'
    }

    It 'Treats a create-delivery conflict as idempotent success' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker -CreateError 'An error occurred (ConflictException) when calling CreateDelivery' -Calls $calls

        Invoke-GameAgentTraceDelivery `
            -RuntimeId 'rt-1' `
            -RuntimeArn $script:RuntimeArn `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -RetrySeconds 0

        $calls | Should -Contain 'logs describe-deliveries'
    }

    It 'Fails on a non-conflict authorization error' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker -CreateError 'An error occurred (AccessDeniedException): not authorized' -Calls $calls

        {
            Invoke-GameAgentTraceDelivery `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0
        } | Should -Throw '*non-retryable*'
    }

    It 'Does not classify free text that merely mentions already exists as a conflict' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker -CreateError 'An error occurred (ValidationException): a name that already exists elsewhere' -Calls $calls

        {
            Invoke-GameAgentTraceDelivery `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0
        } | Should -Throw '*non-retryable*'
    }

    It 'Retries a retryable error then succeeds' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker `
            -CreateErrorSequence @('An error occurred (ThrottlingException): Rate exceeded', '') `
            -Calls $calls

        Invoke-GameAgentTraceDelivery `
            -RuntimeId 'rt-1' `
            -RuntimeArn $script:RuntimeArn `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -MaxAttempts 3 `
            -RetrySeconds 0

        @($calls | Where-Object { $_ -eq 'logs create-delivery' }).Count | Should -Be 2
    }

    It 'Fails when retryable errors exhaust the retry budget' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker -CreateError 'An error occurred (ThrottlingException): Rate exceeded' -Calls $calls

        {
            Invoke-GameAgentTraceDelivery `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -MaxAttempts 3 `
                -RetrySeconds 0
        } | Should -Throw '*retryable AWS error*'
    }

    It 'Fails verification when the delivery is not active for the intended source' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker -DescribeSourceName 'mismatch-source' -Calls $calls

        {
            Invoke-GameAgentTraceDelivery `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0
        } | Should -Throw '*not active*'
    }

    It 'Fails verification when the delivery source is not bound to the runtime' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker `
            -SourceResourceArn 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/other' `
            -Calls $calls

        {
            Invoke-GameAgentTraceDelivery `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0
        } | Should -Throw '*not active*'
    }

    It 'Fails on a conflicting source that is not bound to this runtime' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker `
            -SourceConflict 'An error occurred (ConflictException) when calling PutDeliverySource' `
            -SourceResourceArn 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/other' `
            -Calls $calls

        {
            Invoke-GameAgentTraceDelivery `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0
        } | Should -Throw '*conflicting delivery source*'
    }

    It 'Resolves the destination ARN from the API when put-delivery-destination conflicts' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker `
            -DestConflict 'An error occurred (ConflictException) when calling PutDeliveryDestination' `
            -Calls $calls

        Invoke-GameAgentTraceDelivery `
            -RuntimeId 'rt-1' `
            -RuntimeArn $script:RuntimeArn `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -RetrySeconds 0

        # On a destination conflict the ARN is read from the API, never fabricated.
        $calls | Should -Contain 'logs get-delivery-destination'
        $calls | Should -Contain 'logs describe-deliveries'
    }

    It 'Retries describe-deliveries on a transient error then verifies active' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker `
            -DescribeErrorSequence @('An error occurred (ThrottlingException): Rate exceeded', '') `
            -Calls $calls

        Invoke-GameAgentTraceDelivery `
            -RuntimeId 'rt-1' `
            -RuntimeArn $script:RuntimeArn `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -MaxAttempts 3 `
            -RetrySeconds 0

        @($calls | Where-Object { $_ -eq 'logs describe-deliveries' }).Count | Should -BeGreaterThan 1
    }
}
