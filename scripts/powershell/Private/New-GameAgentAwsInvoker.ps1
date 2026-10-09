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
        Invoke-GameAgentTraceDelivery. The thrown message includes the CLI output
        so the caller's error classifier can read the AWS error code.
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
        $result = & aws @allArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "$labelLocal failed: $result"
        }
        return $result
    }.GetNewClosure()
}
