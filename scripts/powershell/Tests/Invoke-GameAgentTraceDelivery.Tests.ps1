BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentTraceDelivery.ps1' | Resolve-Path)

    $DestArn = 'arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination'
    $SourceName = 'rt-1-traces-source'

    function New-TestDeliveryInvoker {
        param(
            [string]$CreateError = '',
            [string[]]$CreateErrorSequence = @(),
            [string]$DescribeSourceName = 'rt-1-traces-source',
            [string]$DescribeDestArn = 'arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination',
            [System.Collections.Generic.List[string]]$Calls
        )

        $createQueue = [System.Collections.Generic.Queue[string]]::new()
        foreach ($value in $CreateErrorSequence) { $createQueue.Enqueue($value) }

        return {
            param([string[]]$AwsArgs)
            $operation = "$($AwsArgs[0]) $($AwsArgs[1])"
            $Calls.Add($operation)

            switch ($operation) {
                'logs put-delivery-source' { return '{}' }
                'logs put-delivery-destination' {
                    return '{"deliveryDestination":{"arn":"arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination"}}'
                }
                'logs create-delivery' {
                    $err = ''
                    if ($createQueue.Count -gt 0) { $err = $createQueue.Dequeue() }
                    elseif ($CreateError) { $err = $CreateError }
                    if ($err) { throw $err }
                    return '{}'
                }
                'logs describe-deliveries' {
                    return "{""deliveries"":[{""deliverySourceName"":""$DescribeSourceName"",""deliveryDestinationArn"":""$DescribeDestArn""}]}"
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
            -RuntimeArn 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1' `
            -AccountId '123456789012' `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -RetrySeconds 0

        $calls | Should -Contain 'logs describe-deliveries'
    }

    It 'Treats a genuine conflict as idempotent success' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker -CreateError 'An error occurred (ConflictException)' -Calls $calls

        Invoke-GameAgentTraceDelivery `
            -RuntimeId 'rt-1' `
            -RuntimeArn 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1' `
            -AccountId '123456789012' `
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
                -RuntimeArn 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1' `
                -AccountId '123456789012' `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0
        } | Should -Throw '*non-retryable*'
    }

    It 'Retries a retryable error then succeeds' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker `
            -CreateErrorSequence @('ThrottlingException: Rate exceeded', '') `
            -Calls $calls

        Invoke-GameAgentTraceDelivery `
            -RuntimeId 'rt-1' `
            -RuntimeArn 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1' `
            -AccountId '123456789012' `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -MaxAttempts 3 `
            -RetrySeconds 0

        @($calls | Where-Object { $_ -eq 'logs create-delivery' }).Count | Should -Be 2
    }

    It 'Fails when retryable errors exhaust the retry budget' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestDeliveryInvoker -CreateError 'ThrottlingException: Rate exceeded' -Calls $calls

        {
            Invoke-GameAgentTraceDelivery `
                -RuntimeId 'rt-1' `
                -RuntimeArn 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1' `
                -AccountId '123456789012' `
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
                -RuntimeArn 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1' `
                -AccountId '123456789012' `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0
        } | Should -Throw '*not active*'
    }
}
