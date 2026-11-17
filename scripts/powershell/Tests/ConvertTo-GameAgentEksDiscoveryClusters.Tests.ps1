BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'ConvertTo-GameAgentEksDiscoveryClusters.ps1' | Resolve-Path)
}

Describe 'ConvertTo-GameAgentEksDiscoveryClusters' {
    # Same accept/reject cases as test_deploy_script_unit.py, so the bash and
    # PowerShell deploy paths validate GBAW_EKS_DISCOVERY_CLUSTERS identically.
    It 'Normalizes <Label>' -ForEach @(
        @{ Label = 'an empty value'; Value = ''; Expected = '' }
        @{ Label = 'a whitespace-only value'; Value = '   '; Expected = '' }
        @{ Label = 'one name'; Value = 'agones-demo'; Expected = 'agones-demo' }
        @{ Label = 'padded names'; Value = ' agones-demo , arena_west.2 '; Expected = 'agones-demo,arena_west.2' }
        @{ Label = 'twenty names'; Value = ((0..19 | ForEach-Object { "c$_" }) -join ','); Expected = ((0..19 | ForEach-Object { "c$_" }) -join ',') }
    ) {
        ConvertTo-GameAgentEksDiscoveryClusters -Value $Value | Should -BeExactly $Expected
    }

    It 'Rejects <Label>' -ForEach @(
        @{ Label = 'a trailing comma'; Value = 'agones-demo,' }
        @{ Label = 'a leading comma'; Value = ',agones-demo' }
        @{ Label = 'an empty middle entry'; Value = 'a,,b' }
        @{ Label = 'a leading dash'; Value = '-starts-with-dash' }
        @{ Label = 'an inner space'; Value = 'has space' }
        @{ Label = 'a slash'; Value = 'bad/char' }
        @{ Label = 'a newline'; Value = "agones-demo`narena" }
        @{ Label = 'a 101-character name'; Value = ('a' * 101) }
        @{ Label = 'a duplicate'; Value = 'dup,dup' }
        @{ Label = 'twenty-one names'; Value = ((0..20 | ForEach-Object { "c$_" }) -join ',') }
    ) {
        { ConvertTo-GameAgentEksDiscoveryClusters -Value $Value } |
            Should -Throw -ExpectedMessage '*GBAW_EKS_DISCOVERY_CLUSTERS*'
    }
}
