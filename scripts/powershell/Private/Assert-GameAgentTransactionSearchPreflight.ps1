function Assert-GameAgentTransactionSearchPreflight {
    <#
    .SYNOPSIS
        Run the read-only Transaction Search preflight before `agentcore launch`
        and throw (fail closed) when the stop switch is set and the toolkit
        would, or might, change account-wide settings.
    .DESCRIPTION
        The single guard Deploy-GameAgent calls before every `agentcore launch`.
        It delegates to Invoke-GameAgentTransactionSearchPreflight (which prints
        the prediction and honors the fail-closed stop switch) and converts a
        $false result into a terminating error so no launch is attempted. Keeping
        this in one helper means every launch site shares the same guard and the
        guard itself is testable.
    #>
    [CmdletBinding()]
    param(
        [string]$Region,
        [string[]]$ProfileArgs,
        [switch]$StopOnAccountObservabilityChange
    )
    $safe = Invoke-GameAgentTransactionSearchPreflight `
        -Region $Region `
        -ProfileArgs $ProfileArgs `
        -StopOnAccountObservabilityChange:$StopOnAccountObservabilityChange
    if (-not $safe) {
        throw 'Stopped before agentcore launch: the AgentCore starter toolkit would change account-wide observability settings and -StopOnAccountObservabilityChange is set.'
    }
}
