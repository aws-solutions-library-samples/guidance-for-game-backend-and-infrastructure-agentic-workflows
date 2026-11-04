function Resolve-GameAgentStopSwitch {
    <#
    .SYNOPSIS
        Map the shell-equivalent stop-switch environment value to a boolean.
    .DESCRIPTION
        Mirrors the shell path, which enables the fail-closed stop switch only
        for the exact, case-sensitive value 'true'. 'TRUE', '1', 'yes', a
        leading/trailing space, and an empty or missing value are all treated as
        off. The comparison is case-sensitive (-ceq) on purpose so a shouted
        'TRUE' does not silently arm a deploy-halting switch.
    .PARAMETER Value
        The raw environment value (typically $env:GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE).
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [string]$Value
    )
    return ($Value -ceq 'true')
}
