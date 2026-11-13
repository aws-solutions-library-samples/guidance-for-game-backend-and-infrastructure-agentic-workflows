function ConvertTo-GameAgentEksDiscoveryClusters {
    <#
    .SYNOPSIS
        Validates and normalizes the optional Agones discovery enrollment scope (#529).

    .DESCRIPTION
        Takes a comma-separated list of EKS cluster names (GBAW_EKS_DISCOVERY_CLUSTERS)
        and returns it trimmed and re-joined with commas. Each entry must match the
        runtime grammar in agents/agones_discovery.py: a letter or digit, then up to
        99 letters, digits, '.', '_' or '-'. Empty entries, duplicates, more than 20
        names, and multi-line values throw, so a typo fails the deploy instead of
        being silently dropped by the runtime. Rules match
        normalize_eks_discovery_clusters in scripts/deploy.sh.

        An empty or whitespace-only value returns '' : the variable is then not
        passed and the Agones inventory tool fails closed (not_enrolled).
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [AllowEmptyString()][AllowNull()][string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) { return '' }
    if ($Value -match "[`r`n]") {
        throw 'GBAW_EKS_DISCOVERY_CLUSTERS must be one line.'
    }

    $names = [System.Collections.Generic.List[string]]::new()
    foreach ($entry in $Value.Split(',')) {
        $name = $entry.Trim()
        if (-not $name) {
            throw 'GBAW_EKS_DISCOVERY_CLUSTERS has an empty entry.'
        }
        if ($name -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\z') {
            throw "GBAW_EKS_DISCOVERY_CLUSTERS entry '$name' is not a valid EKS cluster name."
        }
        if ($names.Contains($name)) {
            throw "GBAW_EKS_DISCOVERY_CLUSTERS lists '$name' more than once."
        }
        $names.Add($name)
    }
    if ($names.Count -gt 20) {
        throw "GBAW_EKS_DISCOVERY_CLUSTERS lists $($names.Count) clusters; the limit is 20."
    }
    return ($names -join ',')
}
