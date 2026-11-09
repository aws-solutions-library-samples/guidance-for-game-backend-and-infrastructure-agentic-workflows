function Resolve-GameAgentPlatformVersion {
    <#
    .SYNOPSIS
        Resolves the AgentCore Runtime platform version requested for a deployment.
    .DESCRIPTION
        Applies the same rule as resolve_agentcore_platform_version in
        scripts/deploy.sh to the shared GBAW_AGENTCORE_PLATFORM_VERSION setting:
        an unset or empty value selects V1, the exact values V1 and V2 are
        accepted, and anything else (including lowercase or padded values) throws.
        Matching is case-sensitive, as in the shell path, so both shells accept
        and reject the same values. Call it before any AWS change so an invalid
        value stops the deployment immediately.
    .PARAMETER Value
        The raw setting, normally $env:GBAW_AGENTCORE_PLATFORM_VERSION.
    .OUTPUTS
        'V1' or 'V2'.
    .EXAMPLE
        $platformVersion = Resolve-GameAgentPlatformVersion -Value $env:GBAW_AGENTCORE_PLATFORM_VERSION
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string]$Value
    )

    if (-not $Value) { return 'V1' }
    if ($Value -ceq 'V1' -or $Value -ceq 'V2') { return $Value }
    throw "GBAW_AGENTCORE_PLATFORM_VERSION='$Value' must be V1 or V2."
}
