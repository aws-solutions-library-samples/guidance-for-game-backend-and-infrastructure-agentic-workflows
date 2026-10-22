function Get-GameAgentTraceDestination {
    <#
    .SYNOPSIS
        Read the account's current X-Ray trace segment destination (read-only).
    .DESCRIPTION
        Shared reader for the X-Ray trace segment destination so the account
        observability path and the Transaction Search preflight use one
        implementation rather than duplicating it. Returns the enum value
        'XRay' or 'CloudWatchLogs', or 'UNREADABLE' when the read does not
        succeed (including any other or empty value). Each AWS read is wrapped
        so native-stderr handling on PowerShell 7.0/7.1 (where redirected
        stderr can surface as a terminating error under
        $ErrorActionPreference='Stop') yields 'UNREADABLE' instead of throwing.
    #>
    [CmdletBinding()]
    param(
        [string]$Region,
        [string[]]$ProfileArgs
    )
    try {
        $raw = (& aws xray get-trace-segment-destination --region $Region @ProfileArgs --query 'Destination' --output text 2>$null)
    } catch {
        return 'UNREADABLE'
    }
    if ($LASTEXITCODE -ne 0) { return 'UNREADABLE' }
    $dest = ("$raw").Trim()
    # Case-sensitive, matching the shell reader's `case` match: any other casing
    # is not a value the API returns, so it is UNREADABLE, not a known state.
    if ($dest -ceq 'XRay' -or $dest -ceq 'CloudWatchLogs') { return $dest }
    return 'UNREADABLE'
}
