function New-GameAgentAwsInvoker {
    <#
    .SYNOPSIS
        Builds a scriptblock that runs the AWS CLI directly and fails on error.
    .DESCRIPTION
        Returns a closure that appends the resolved profile arguments to its own
        arguments, invokes the AWS CLI with `& aws`, and throws when the CLI
        exits non-zero. Because the closure calls `& aws` directly (rather than a
        function nested inside the caller), it resolves correctly when passed to
        another function such as Invoke-GameAgentWafReconciliation or
        Invoke-GameAgentTraceDelivery. stderr is merged with `2>&1` so native CLI
        diagnostics are captured, then split back out: the closure returns stdout
        only, so a CLI warning on stderr cannot corrupt the caller's JSON or ARN
        parsing, and the thrown message carries the stderr text so the caller's
        error classifier can read the AWS error code.
    .PARAMETER ProfileArgs
        The resolved AWS CLI profile arguments (for example @('--profile','demo')
        or an empty array).
    .PARAMETER Label
        A short, public-safe label used in the thrown message.
    #>
    [CmdletBinding()]
    param(
        [string[]]$ProfileArgs = @(),
        [string]$Label = 'AWS command'
    )

    $profileArgsLocal = @($ProfileArgs)
    $labelLocal = $Label
    return {
        param([string[]]$AwsArgs)
        $allArgs = $AwsArgs + $profileArgsLocal
        # Merge stderr into the stream so native CLI diagnostics are captured,
        # then split the merged items: stderr surfaces as ErrorRecord objects
        # while stdout is everything else. Returning stdout only keeps a CLI
        # warning on stderr out of the caller's JSON/ARN parsing; the stderr
        # text is carried in the thrown message for the error classifier.
        $merged = & aws @allArgs 2>&1
        $stdoutItems = @($merged | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
        $stderrItems = @($merged | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })
        if ($LASTEXITCODE -ne 0) {
            $stderrText = ($stderrItems | ForEach-Object { $_.ToString() }) -join "`n"
            throw "$labelLocal failed: $stderrText"
        }
        return $stdoutItems
    }.GetNewClosure()
}
