BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Resolve-GameAgentPlatformVersion.ps1' | Resolve-Path)
}

Describe 'Resolve-GameAgentPlatformVersion' {
    It 'Defaults to V1 when the setting is <Label>' -TestCases @(
        @{ Label = 'null'; Value = $null }
        @{ Label = 'empty'; Value = '' }
    ) {
        param($Label, $Value)

        Resolve-GameAgentPlatformVersion -Value $Value | Should -BeExactly 'V1'
    }

    It 'Accepts the exact value <Value>' -TestCases @(
        @{ Value = 'V1' }
        @{ Value = 'V2' }
    ) {
        param($Value)

        Resolve-GameAgentPlatformVersion -Value $Value | Should -BeExactly $Value
    }

    It 'Rejects <Label> like the shell path does' -TestCases @(
        @{ Label = 'a lowercase value'; Value = 'v2' }
        @{ Label = 'an unknown version'; Value = 'V3' }
        @{ Label = 'a padded value'; Value = ' V2' }
        @{ Label = 'a bare number'; Value = '2' }
    ) {
        param($Label, $Value)

        { Resolve-GameAgentPlatformVersion -Value $Value } |
            Should -Throw '*GBAW_AGENTCORE_PLATFORM_VERSION*must be V1 or V2*'
    }
}
