function Invoke-GameAgentTraceDeliveryGated {
    <#
    .SYNOPSIS
        Step 2b gate: create the runtime trace delivery only when Transaction
        Search is active, otherwise warn and skip without failing the deploy.
    .DESCRIPTION
        Mirrors the ensure_runtime_trace_delivery_gated logic in
        scripts/deploy.sh. The per-runtime trace delivery binds only when the
        account trace segment destination is 'CloudWatchLogs' and 'ACTIVE'. On a
        fresh account the destination can stay PENDING for several minutes after
        'agentcore launch' switches it, so this gate decides whether to create
        the delivery now, wait for it, or defer it without failing the deploy:

          - CloudWatchLogs + ACTIVE: create and verify the delivery via
            Invoke-GameAgentTraceDelivery; every failure stays fatal (it throws),
            as issue #471 requires a failed delivery to fail the deploy.
          - CloudWatchLogs + not yet ACTIVE: wait for a bounded, configurable
            budget; if it becomes ACTIVE, create and verify as above.
          - Not CloudWatchLogs, still not ACTIVE after the wait, or an unreadable
            read: write one WARNING naming the state and skip the delivery. The
            deploy continues and the read-only Step 5c recheck still reports.

        The wait budget is overridable with the same
        GBAW_TRACE_DESTINATION_ACTIVE_* environment variables the shell gate
        honors (defaults 30 attempts, 10s apart).
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

    # Bounded poll for the trace destination to reach ACTIVE after an enable.
    # Sourced from the same GBAW_OBSERVABILITY/TRACE_DESTINATION_ACTIVE_*
    # environment variables the shell gate honors; non-numeric or empty values
    # fall back to the defaults so the gate never aborts on a bad knob.
    $waitAttempts = 30
    $waitDelay = 10
    $parsed = 0
    if ([int]::TryParse($env:GBAW_TRACE_DESTINATION_ACTIVE_MAX_ATTEMPTS, [ref]$parsed) -and $parsed -ge 1) {
        $waitAttempts = $parsed
    }
    if ([int]::TryParse($env:GBAW_TRACE_DESTINATION_ACTIVE_RETRY_SECONDS, [ref]$parsed) -and $parsed -ge 0) {
        $waitDelay = $parsed
    }

    # Read the account X-Ray trace segment destination and status in one place.
    # Returns a PSCustomObject with Destination and Status; a failed or
    # unparseable read yields 'UNREADABLE' for both so the caller treats a read
    # failure the same as a not-yet-active destination. Read-only.
    function Read-TraceSegmentDestination {
        try {
            $raw = (& $InvokeAws @('xray', 'get-trace-segment-destination', '--region', $Region, '--output', 'json') | Out-String)
            $payload = $raw | ConvertFrom-Json
            $destination = if ($payload.Destination) { [string]$payload.Destination } else { 'UNREADABLE' }
            $status = if ($payload.Status) { [string]$payload.Status } else { 'UNREADABLE' }
            return [PSCustomObject]@{ Destination = $destination; Status = $status }
        } catch {
            return [PSCustomObject]@{ Destination = 'UNREADABLE'; Status = 'UNREADABLE' }
        }
    }

    # Bounded poll until the destination reports ACTIVE. Returns $true as soon as
    # it is ACTIVE, or $false when the budget is exhausted.
    function Wait-TraceDestinationActive {
        for ($attempt = 1; $attempt -le $waitAttempts; $attempt++) {
            $status = 'UNREADABLE'
            try {
                $status = (& $InvokeAws @('xray', 'get-trace-segment-destination', '--region', $Region, '--query', 'Status', '--output', 'text') | Out-String).Trim()
            } catch {
                $status = 'UNREADABLE'
            }
            if ($status -eq 'ACTIVE') { return $true }
            if ($attempt -lt $waitAttempts -and $waitDelay -gt 0) { Start-Sleep -Seconds $waitDelay }
        }
        return $false
    }

    $state = Read-TraceSegmentDestination
    $destination = $state.Destination
    $status = $state.Status

    $deliveryArgs = @{
        RuntimeId    = $RuntimeId
        RuntimeArn   = $RuntimeArn
        Region       = $Region
        InvokeAws    = $InvokeAws
        MaxAttempts  = $MaxAttempts
        RetrySeconds = $RetrySeconds
    }

    if ($destination -eq 'CloudWatchLogs' -and $status -eq 'ACTIVE') {
        Invoke-GameAgentTraceDelivery @deliveryArgs
        return
    }

    if ($destination -eq 'CloudWatchLogs') {
        Write-Host "  Trace segment destination is CloudWatchLogs but not yet ACTIVE (status: $status); waiting..."
        if (Wait-TraceDestinationActive) {
            Invoke-GameAgentTraceDelivery @deliveryArgs
            return
        }
    }

    Write-GameAgentStatus "WARNING: Skipping runtime trace delivery setup. X-Ray trace segment destination is '$destination' (status: $status), not an active 'CloudWatchLogs' destination. Traces will not reach CloudWatch Logs until X-Ray Transaction Search is active for this account. Check it with: aws xray get-trace-segment-destination --region $Region and re-run the deployment after it reports CloudWatchLogs and ACTIVE." -Type Warning
}
