function ConvertTo-GameAgentFrontendUrl {
    <#
    .SYNOPSIS
        Converts the frontend stack's ServiceUrl output to one https:// URL.
    .DESCRIPTION
        The ECS Express ServiceUrl output has been returned both as a bare host
        and with its https:// scheme (issue #641). Removes one leading
        'https://', then one leading 'http://', and one trailing '/', then adds
        'https://', so the caller uses the scheme exactly once. This mirrors the
        parameter expansions in the shell scripts, so the matches are
        case-sensitive ('-creplace') like the shell's.

        Returns an empty string for empty or whitespace-only input, so a
        caller's "frontend deployed" check still treats a missing output as
        absent.
    .PARAMETER ServiceUrl
        The raw ServiceUrl output value, with or without its scheme.
    .OUTPUTS
        System.String
    .EXAMPLE
        ConvertTo-GameAgentFrontendUrl -ServiceUrl 'https://example.ecs.us-west-2.on.aws'
        Returns 'https://example.ecs.us-west-2.on.aws', as does the bare host.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string]$ServiceUrl)

    $value = "$ServiceUrl".Trim()
    if (-not $value) { return '' }
    $value = $value -creplace '^https://', '' -creplace '^http://', '' -creplace '/$', ''
    return "https://$value"
}
