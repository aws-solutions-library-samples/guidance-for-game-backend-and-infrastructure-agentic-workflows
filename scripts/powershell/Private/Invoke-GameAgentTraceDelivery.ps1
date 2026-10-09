function Invoke-GameAgentTraceDelivery {
    <#
    .SYNOPSIS
        Creates and verifies the CloudWatch Logs trace delivery for the runtime.
    .DESCRIPTION
        Mirrors the Step 2b logic in scripts/deploy.sh. Delivery mutations
        (put-delivery-source, put-delivery-destination, create-delivery)
        distinguish a genuine already-exists conflict from authorization,
        validation, throttling, and service errors by the AWS error CODE, so free
        text cannot cause a misread. A conflict is treated as idempotent only for
        create-delivery; for the two put upserts, a conflict is accepted only when
        a follow-up read confirms the existing resource belongs to this runtime.
        Retryable errors are retried with a small bounded backoff; non-retryable
        errors fail with a bounded, public-safe diagnostic that names only the
        operation. The delivery is then queried and the function fails unless the
        intended source and destination are active for this runtime.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$RuntimeId,

        [Parameter(Mandatory)]
        [string]$RuntimeArn,

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

    # Classify from the modeled AWS error CODE in the standard
    # "An error occurred (<Code>) ..." form. -cmatch keeps this case-sensitive so
    # it classifies the same text the shell path does.
    function Get-DeliveryErrorClass {
        param([string]$ErrorText)
        $code = ''
        if ($ErrorText -cmatch '\(([A-Za-z]+)\)') { $code = $Matches[1] }
        switch -CaseSensitive ($code) {
            'ConflictException' { return 'conflict' }
            'ResourceAlreadyExistsException' { return 'conflict' }
            'ThrottlingException' { return 'retryable' }
            'ServiceUnavailableException' { return 'retryable' }
            'InternalFailure' { return 'retryable' }
            '500' { return 'retryable' }
            '502' { return 'retryable' }
            '503' { return 'retryable' }
            '504' { return 'retryable' }
        }
        if ($ErrorText -match 'Could not connect to the endpoint URL|Read timeout') { return 'retryable' }
        return 'fatal'
    }

    # Runs one delivery mutation. Returns the raw stdout on success, the string
    # 'CONFLICT' on an already-exists conflict, and throws on a non-retryable or
    # budget-exhausting error.
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
                        return 'CONFLICT'
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

    # Confirms an already-existing delivery resource belongs to this runtime.
    function Test-DeliveryResourceMatchesRuntime {
        param([ValidateSet('source', 'destination')][string]$Kind, [string]$Name)
        try {
            if ($Kind -eq 'source') {
                $raw = (& $InvokeAws @('logs', 'get-delivery-source', '--name', $Name, '--region', $Region, '--output', 'json') | Out-String)
                $source = ($raw | ConvertFrom-Json).deliverySource
                if ($source.logType -and $source.logType -ne 'TRACES') { return $false }
                return (@($source.resourceArns) -contains $RuntimeArn)
            } else {
                $raw = (& $InvokeAws @('logs', 'get-delivery-destination', '--name', $Name, '--region', $Region, '--output', 'json') | Out-String)
                $dest = ($raw | ConvertFrom-Json).deliveryDestination
                return ($dest.deliveryDestinationType -eq 'XRAY')
            }
        } catch {
            return $false
        }
    }

    # Delivery source (upsert). A conflict is safe only if it is bound to us.
    $sourceResult = Invoke-DeliveryMutation 'Delivery source' @(
        'logs', 'put-delivery-source',
        '--name', $deliverySourceName,
        '--log-type', 'TRACES',
        '--resource-arn', $RuntimeArn,
        '--region', $Region
    )
    if ($sourceResult -eq 'CONFLICT') {
        if (-not (Test-DeliveryResourceMatchesRuntime -Kind 'source' -Name $deliverySourceName)) {
            throw 'A conflicting delivery source exists for this runtime.'
        }
        Write-Host '  Delivery source already present for this runtime'
    } else {
        Write-Host '  Delivery source ready'
    }

    # Delivery destination (upsert). Same conflict rule applies.
    $destStdout = Invoke-DeliveryMutation 'Delivery destination' @(
        'logs', 'put-delivery-destination',
        '--name', $deliveryDestName,
        '--delivery-destination-type', 'XRAY',
        '--region', $Region
    )
    $deliveryDestArn = ''
    if ($destStdout -eq 'CONFLICT') {
        if (-not (Test-DeliveryResourceMatchesRuntime -Kind 'destination' -Name $deliveryDestName)) {
            throw 'A conflicting delivery destination exists for this runtime.'
        }
        Write-Host '  Delivery destination already present'
    } else {
        try { $deliveryDestArn = ($destStdout | ConvertFrom-Json).deliveryDestination.arn } catch { $deliveryDestArn = '' }
        Write-Host '  Delivery destination ready'
    }

    # Resolve the destination ARN from the API; never fabricate it.
    if ([string]::IsNullOrWhiteSpace($deliveryDestArn)) {
        try {
            $resolved = (& $InvokeAws @('logs', 'get-delivery-destination', '--name', $deliveryDestName, '--region', $Region, '--query', 'deliveryDestination.arn', '--output', 'text') | Out-String).Trim()
        } catch {
            $resolved = ''
        }
        if ([string]::IsNullOrWhiteSpace($resolved) -or $resolved -eq 'None') {
            throw 'Unable to resolve the delivery destination ARN.'
        }
        $deliveryDestArn = $resolved
    }

    # Delivery binding (create). A conflict here is a safe idempotent rerun.
    $deliveryResult = Invoke-DeliveryMutation 'Delivery' @(
        'logs', 'create-delivery',
        '--delivery-source-name', $deliverySourceName,
        '--delivery-destination-arn', $deliveryDestArn,
        '--region', $Region
    )
    if ($deliveryResult -eq 'CONFLICT') {
        Write-Host '  Delivery already exists'
    } else {
        Write-Host '  Delivery ready'
    }

    # Verification (bounded retry; stdout only): the deployment must not succeed
    # unless the intended source and destination are bound to this runtime.
    $verified = $false
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            $deliveriesRaw = (& $InvokeAws @('logs', 'describe-deliveries', '--region', $Region, '--output', 'json') | Out-String)
            $sourceRaw = (& $InvokeAws @('logs', 'get-delivery-source', '--name', $deliverySourceName, '--region', $Region, '--output', 'json') | Out-String)

            $parsed = $deliveriesRaw | ConvertFrom-Json
            $deliveries = if ($null -ne $parsed.deliveries) { $parsed.deliveries } else { $parsed }
            $bindingOk = $false
            foreach ($delivery in @($deliveries)) {
                if ($delivery.deliverySourceName -eq $deliverySourceName -and
                    $delivery.deliveryDestinationArn -eq $deliveryDestArn -and
                    $delivery.deliveryDestinationType -eq 'XRAY') {
                    $bindingOk = $true
                    break
                }
            }

            $source = ($sourceRaw | ConvertFrom-Json).deliverySource
            $sourceOk = (@($source.resourceArns) -contains $RuntimeArn)

            if ($bindingOk -and $sourceOk) { $verified = $true; break }
        } catch {
            $verified = $false
        }
        if ($attempt -lt $MaxAttempts -and $RetrySeconds -gt 0) { Start-Sleep -Seconds $RetrySeconds }
    }

    if (-not $verified) {
        throw 'Runtime trace delivery is not active for the intended source and destination.'
    }
    Write-Host '  Delivery verified active'
}
