BeforeAll {
    $ModulePath = Join-Path $PSScriptRoot '..' 'GameAgent.psd1' | Resolve-Path
    Import-Module $ModulePath -Force

    # Dot-source the private function so it's available in test scope
    . (Join-Path $PSScriptRoot '..' 'Private' 'Write-GameAgentStatus.ps1' | Resolve-Path)
}

Describe 'Write-GameAgentStatus' {
    BeforeEach {
        Mock Write-Host {}
    }

    It 'Uses rocket emoji and Cyan for Info type' {
        Write-GameAgentStatus -Message 'Starting deploy' -Type Info
        Should -Invoke Write-Host -Times 1 -ParameterFilter {
            $Object -eq "`u{1F680} Starting deploy" -and $ForegroundColor -eq 'Cyan'
        }
    }

    It 'Uses checkmark emoji and Green for Success type' {
        Write-GameAgentStatus -Message 'Done' -Type Success
        Should -Invoke Write-Host -Times 1 -ParameterFilter {
            $Object -eq "`u{2705} Done" -and $ForegroundColor -eq 'Green'
        }
    }

    It 'Uses warning emoji and Yellow for Warning type' {
        Write-GameAgentStatus -Message 'Watch out' -Type Warning
        Should -Invoke Write-Host -Times 1 -ParameterFilter {
            $Object -eq "`u{26A0}`u{FE0F} Watch out" -and $ForegroundColor -eq 'Yellow'
        }
    }

    It 'Uses X emoji and Red for Error type' {
        Write-GameAgentStatus -Message 'Failed' -Type Error
        Should -Invoke Write-Host -Times 1 -ParameterFilter {
            $Object -eq "`u{274C} Failed" -and $ForegroundColor -eq 'Red'
        }
    }

    It 'Defaults to Info type when -Type is not specified' {
        Write-GameAgentStatus -Message 'Hello'
        Should -Invoke Write-Host -Times 1 -ParameterFilter {
            $Object -eq "`u{1F680} Hello" -and $ForegroundColor -eq 'Cyan'
        }
    }

    It 'Rejects invalid -Type values' {
        { Write-GameAgentStatus -Message 'test' -Type 'Critical' } | Should -Throw
    }
}
