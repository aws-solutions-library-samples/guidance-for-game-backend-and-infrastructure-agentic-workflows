function Invoke-GameAgentTraceDelivery {
    <#
    .SYNOPSIS
        Creates and verifies the CloudWatch Logs trace delivery for the runtime.
    .DESCRIPTION
        Mirrors the Step 2b logic in scripts/deploy.sh. Delivery mutations
        (put-delivery-source, put-delivery-destination, create-delivery)
        distinguish a genuine already-exists conflict (idempotent success) from
        authorization, validation, throttling, and service errors. Retryable
        errors are retried with a small bounded backoff; non-retryable errors
        fail with a bounded, public-safe diagnostic that names only the
        operation. The delivery is then queried and the function fails unless the
        intended source and destination are active.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$RuntimeId,

        [Parameter(Mandatory)]
        [string]$RuntimeArn,

        [Parameter(Mandatory)]
        [string]$AccountId,

        [Parameter(Mandatory)]
        [string]$Region,

        [Parameter(Mandatory)]
        [scriptblock]$InvokeAws,

        [ValidateRange(1, 20)]
        [int]$MaxAttempts = 4,

        [ValidateRange(0, 60)]
        [int]$RetrySeconds = 5
    )

    $deliverySourceName = "$RuntimeId-traces-source"
    $deliveryDestName = "$RuntimeId-traces-destination"

    function Get-DeliveryErrorClass {
        param([string]$ErrorText)
        if ($ErrorText -match 'ConflictException|ResourceAlreadyExistsException|already exists|AlreadyExists') {
            return 'conflict'
        }
        if ($ErrorText -match 'ThrottlingException|Throttling|TooManyRequestsException|RequestLimitExceeded|ServiceUnavailable|InternalFailure|InternalServerException|\b500\b|\b503\b') {
            return 'retryable'
        }
        return 'fatal'
    }

    # Runs one delivery mutation. Returns the raw stdout on success, '' on an
    # idempotent conflict, and throws on a non-retryable or budget-exhausting error.
    function Invoke-DeliveryMutation {
        param([string]$Label, [string[]]$Arguments)
        for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
            try {
                $result = & $InvokeAws $Arguments
                return ($result | Out-String)
            } catch {
                $class = Get-DeliveryErrorClass $_.Exception.Message
                switch ($class) {
                    'conflict' {
                        Write-Host "  $Label already exists"
                        return ''
                    }
                    'retryable' {
                        if ($attempt -lt $MaxAttempts) {
                            Write-Host "  ${Label}: retryable AWS error, retrying ($attempt/$MaxAttempts)..."
                            if ($RetrySeconds -gt 0) { Start-Sleep -Seconds $RetrySeconds }
                            continue
                        }
                        throw "$Label did not succeed after $MaxAttempts attempts (retryable AWS error)."
                    }
                    default {
                        throw "$Label failed with a non-retryable AWS error."
                    }
                }
            }
        }
        throw "$Label did not succeed within the retry budget."
    }

    Invoke-DeliveryMutation 'Delivery source' @(
        'logs', 'put-delivery-source',
        '--name', $deliverySourceName,
        '--log-type', 'TRACES',
        '--resource-arn', $RuntimeArn,
        '--region', $Region
    ) | Out-Null
    Write-Host '  Delivery source ready'

    $destStdout = Invoke-DeliveryMutation 'Delivery destination' @(
        'logs', 'put-delivery-destination',
        '--name', $deliveryDestName,
        '--delivery-destination-type', 'XRAY',
        '--region', $Region
    )
    $deliveryDestArn = ''
    if (-not [string]::IsNullOrWhiteSpace($destStdout)) {
        try { $deliveryDestArn = ($destStdout | ConvertFrom-Json).deliveryDestination.arn } catch { $deliveryDestArn = '' }
    }
    if ([string]::IsNullOrWhiteSpace($deliveryDestArn)) {
        $deliveryDestArn = "arn:aws:logs:${Region}:${AccountId}:delivery-destination:${deliveryDestName}"
    }
    Write-Host '  Delivery destination ready'

    Invoke-DeliveryMutation 'Delivery' @(
        'logs', 'create-delivery',
        '--delivery-source-name', $deliverySourceName,
        '--delivery-destination-arn', $deliveryDestArn,
        '--region', $Region
    ) | Out-Null
    Write-Host '  Delivery ready'

    # Verification: the deployment must not succeed unless the intended source
    # and destination are actually bound.
    $deliveriesRaw = ''
    try {
        $deliveriesRaw = (& $InvokeAws @('logs', 'describe-deliveries', '--region', $Region, '--output', 'json') | Out-String)
    } catch {
        throw 'Unable to verify runtime trace delivery.'
    }

    $active = $false
    try {
        $parsed = $deliveriesRaw | ConvertFrom-Json
        $deliveries = if ($null -ne $parsed.deliveries) { $parsed.deliveries } else { $parsed }
        foreach ($delivery in @($deliveries)) {
            if ($delivery.deliverySourceName -eq $deliverySourceName -and
                $delivery.deliveryDestinationArn -eq $deliveryDestArn) {
                $active = $true
                break
            }
        }
    } catch {
        $active = $false
    }

    if (-not $active) {
        throw 'Runtime trace delivery is not active for the intended source and destination.'
    }
    Write-Host '  Delivery verified active'
}
