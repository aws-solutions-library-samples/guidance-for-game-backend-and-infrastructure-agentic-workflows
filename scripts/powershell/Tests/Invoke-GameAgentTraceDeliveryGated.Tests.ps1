BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentTraceDelivery.ps1' | Resolve-Path)
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentTraceDeliveryGated.ps1' | Resolve-Path)

    $script:RuntimeArn = 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1'

    # Shadow the status helper so the WARNING text (written via
    # Write-GameAgentStatus) is captured for assertions; the real helper routes
    # the WARNING through Write-Host, which Pester does not capture.
    $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    function Write-GameAgentStatus {
        param([string]$Message, [string]$Type = 'Info')
        $script:StatusMessages.Add("$Type`: $Message")
    }

    # Build an InvokeAws closure that answers the destination read and status
    # polls plus the delivery calls. $DestinationStatusSequence feeds the
    # bounded wait: the first value is returned by the one-shot JSON read, and
    # the remaining values by each --query Status poll (the last repeats).
    function New-TestGateInvoker {
        param(
            [string]$Destination = 'CloudWatchLogs',
            [string[]]$StatusSequence = @('ACTIVE'),
            [string]$DestinationReadError = '',
            [string]$CreateError = '',
            [System.Collections.Generic.List[string]]$Calls
        )

        $destArn = 'arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination'
        # GetNewClosure binds the block to its own module scope, where $script:
        # variables are empty, so capture the runtime ARN as a local.
        $runtimeArn = $script:RuntimeArn
        $firstStatus = $StatusSequence[0]
        $pollStatuses = if ($StatusSequence.Count -gt 1) { $StatusSequence[1..($StatusSequence.Count - 1)] } else { @($StatusSequence[-1]) }
        $pollQueue = [System.Collections.Generic.Queue[string]]::new()
        foreach ($value in $pollStatuses) { $pollQueue.Enqueue($value) }

        return {
            param([string[]]$AwsArgs)
            $operation = "$($AwsArgs[0]) $($AwsArgs[1])"
            $Calls.Add($operation)

            if ($operation -eq 'xray get-trace-segment-destination') {
                if ($DestinationReadError) { throw $DestinationReadError }
                if ($AwsArgs -contains 'Status') {
                    $value = if ($pollQueue.Count -gt 1) { $pollQueue.Dequeue() } else { $pollQueue.Peek() }
                    return $value
                }
                return "{""Destination"":""$Destination"",""Status"":""$firstStatus""}"
            }

            switch ($operation) {
                'logs put-delivery-source' { return '{}' }
                'logs put-delivery-destination' { return "{""deliveryDestination"":{""arn"":""$destArn""}}" }
                'logs get-delivery-source' {
                    return "{""deliverySource"":{""name"":""rt-1-traces-source"",""logType"":""TRACES"",""resourceArns"":[""$runtimeArn""]}}"
                }
                'logs get-delivery-destination' {
                    if ($AwsArgs -contains '--query') { return $destArn }
                    return "{""deliveryDestination"":{""deliveryDestinationType"":""XRAY"",""arn"":""$destArn""}}"
                }
                'logs create-delivery' {
                    if ($CreateError) { throw $CreateError }
                    return '{}'
                }
                'logs describe-deliveries' {
                    return "{""deliveries"":[{""deliverySourceName"":""rt-1-traces-source"",""deliveryDestinationArn"":""$destArn"",""deliveryDestinationType"":""XRAY""}]}"
                }
                default { throw "Unexpected operation: $operation" }
            }
        }.GetNewClosure()
    }
}

Describe 'Invoke-GameAgentTraceDeliveryGated' {
    BeforeEach {
        $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    }

    It 'Creates and verifies the delivery when the destination is CloudWatchLogs and ACTIVE' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestGateInvoker -Destination 'CloudWatchLogs' -StatusSequence @('ACTIVE') -Calls $calls

        Invoke-GameAgentTraceDeliveryGated `
            -RuntimeId 'rt-1' `
            -RuntimeArn $script:RuntimeArn `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -RetrySeconds 0

        $calls | Should -Contain 'logs create-delivery'
        $calls | Should -Contain 'logs describe-deliveries'
    }

    It 'Keeps a fatal create error fatal when the destination is ACTIVE' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestGateInvoker `
            -Destination 'CloudWatchLogs' `
            -StatusSequence @('ACTIVE') `
            -CreateError 'An error occurred (AccessDeniedException): not authorized' `
            -Calls $calls

        {
            Invoke-GameAgentTraceDeliveryGated `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0
        } | Should -Throw '*non-retryable*'
    }

    It 'Waits for a PENDING destination then creates the delivery when it becomes ACTIVE' {
        $env:GBAW_TRACE_DESTINATION_ACTIVE_MAX_ATTEMPTS = '3'
        $env:GBAW_TRACE_DESTINATION_ACTIVE_RETRY_SECONDS = '0'
        try {
            $calls = [System.Collections.Generic.List[string]]::new()
            $invokeAws = New-TestGateInvoker -Destination 'CloudWatchLogs' -StatusSequence @('PENDING', 'ACTIVE') -Calls $calls

            Invoke-GameAgentTraceDeliveryGated `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0

            $calls | Should -Contain 'logs create-delivery'
        } finally {
            Remove-Item Env:GBAW_TRACE_DESTINATION_ACTIVE_MAX_ATTEMPTS -ErrorAction SilentlyContinue
            Remove-Item Env:GBAW_TRACE_DESTINATION_ACTIVE_RETRY_SECONDS -ErrorAction SilentlyContinue
        }
    }

    It 'Warns and skips without mutation when PENDING through the whole budget' {
        $env:GBAW_TRACE_DESTINATION_ACTIVE_MAX_ATTEMPTS = '2'
        $env:GBAW_TRACE_DESTINATION_ACTIVE_RETRY_SECONDS = '0'
        try {
            $calls = [System.Collections.Generic.List[string]]::new()
            $invokeAws = New-TestGateInvoker -Destination 'CloudWatchLogs' -StatusSequence @('PENDING') -Calls $calls

            Invoke-GameAgentTraceDeliveryGated `
                -RuntimeId 'rt-1' `
                -RuntimeArn $script:RuntimeArn `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -RetrySeconds 0

            $calls | Should -Not -Contain 'logs create-delivery'
            ($script:StatusMessages -join "`n") | Should -Match 'WARNING'
            ($script:StatusMessages -join "`n") | Should -Match 'PENDING'
        } finally {
            Remove-Item Env:GBAW_TRACE_DESTINATION_ACTIVE_MAX_ATTEMPTS -ErrorAction SilentlyContinue
            Remove-Item Env:GBAW_TRACE_DESTINATION_ACTIVE_RETRY_SECONDS -ErrorAction SilentlyContinue
        }
    }

    It 'Warns and skips without mutation when the destination is XRay' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestGateInvoker -Destination 'XRay' -StatusSequence @('ACTIVE') -Calls $calls

        Invoke-GameAgentTraceDeliveryGated `
            -RuntimeId 'rt-1' `
            -RuntimeArn $script:RuntimeArn `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -RetrySeconds 0

        $calls | Should -Not -Contain 'logs create-delivery'
        ($script:StatusMessages -join "`n") | Should -Match 'WARNING'
        ($script:StatusMessages -join "`n") | Should -Match 'XRay'
    }

    It 'Warns and skips without mutation when the destination read fails' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestGateInvoker `
            -DestinationReadError 'An error occurred (AccessDeniedException): not authorized' `
            -Calls $calls

        Invoke-GameAgentTraceDeliveryGated `
            -RuntimeId 'rt-1' `
            -RuntimeArn $script:RuntimeArn `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -RetrySeconds 0

        $calls | Should -Not -Contain 'logs create-delivery'
        ($script:StatusMessages -join "`n") | Should -Match 'WARNING'
    }
}
