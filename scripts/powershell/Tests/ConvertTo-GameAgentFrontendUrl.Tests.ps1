BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'ConvertTo-GameAgentFrontendUrl.ps1' | Resolve-Path)
}

Describe 'ConvertTo-GameAgentFrontendUrl' {
    # The frontend ServiceUrl output has been returned both as a bare host and
    # with its https:// scheme (issue #641); every form yields one scheme.
    It 'Returns exactly one https:// scheme for ServiceUrl <ServiceUrl>' -ForEach @(
        @{ ServiceUrl = 'ga-example.ecs.us-west-2.on.aws' }
        @{ ServiceUrl = 'https://ga-example.ecs.us-west-2.on.aws' }
        @{ ServiceUrl = 'https://ga-example.ecs.us-west-2.on.aws/' }
        @{ ServiceUrl = 'http://ga-example.ecs.us-west-2.on.aws' }
    ) {
        ConvertTo-GameAgentFrontendUrl -ServiceUrl $ServiceUrl |
            Should -BeExactly 'https://ga-example.ecs.us-west-2.on.aws'
    }

    It 'Trims the line ending that Out-String adds to CLI output' {
        ConvertTo-GameAgentFrontendUrl -ServiceUrl "https://ga-example.ecs.us-west-2.on.aws`n" |
            Should -BeExactly 'https://ga-example.ecs.us-west-2.on.aws'
    }

    It 'Returns an empty string for a missing output so callers still treat it as absent' -ForEach @(
        @{ ServiceUrl = '' }
        @{ ServiceUrl = "  `n" }
        @{ ServiceUrl = $null }
    ) {
        ConvertTo-GameAgentFrontendUrl -ServiceUrl $ServiceUrl | Should -BeExactly ''
    }
}
