BeforeAll {
    $ModulePath = Join-Path $PSScriptRoot '..' 'GameAgent.psd1' | Resolve-Path
    Import-Module $ModulePath -Force
}

Describe 'GameAgent Module' {
    It 'Imports without errors' {
        $module = Get-Module -Name GameAgent
        $module | Should -Not -BeNullOrEmpty
    }

    It 'Exports exactly 9 public commands' {
        $commands = (Get-Module -Name GameAgent).ExportedCommands.Keys | Sort-Object
        $commands | Should -HaveCount 9
    }

    It 'Exports all expected public commands' {
        $expected = @(
            'Add-GameAgentAdmin'
            'Deploy-GameAgent'
            'Get-GameAgentStatus'
            'Remove-GameAgent'
            'Remove-GameAgentAdmin'
            'Start-GameAgentDev'
            'Stop-GameAgentDev'
            'Test-GameAgentFull'
            'Test-GameAgentUnit'
        )
        $actual = (Get-Module -Name GameAgent).ExportedCommands.Keys | Sort-Object
        $actual | Should -Be $expected
    }

    It 'Does not export private functions' {
        $privateFunctions = @(
            'Resolve-GameAgentProfile'
            'Test-GameAgentPort'
            'Write-GameAgentStatus'
            'Invoke-GameAgentAppObservability'
            'Invoke-GameAgentAccountObservability'
            'Invoke-GameAgentKBDeploy'
            'Invoke-GameAgentKBDocDownload'
            'Invoke-GameAgentKBSeed'
            'Invoke-GameAgentKBTeardown'
            'Invoke-GameAgentPromptDeploy'
            'Invoke-GameAgentPromptTeardown'
        )
        $exported = (Get-Module -Name GameAgent).ExportedCommands.Keys
        foreach ($fn in $privateFunctions) {
            $exported | Should -Not -Contain $fn
        }
    }

    It 'Has module version 2.0.0' {
        (Get-Module -Name GameAgent).Version.ToString() | Should -Be '2.0.0'
    }
}
