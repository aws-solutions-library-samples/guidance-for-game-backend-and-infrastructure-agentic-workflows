BeforeAll {
    $ModulePath = Join-Path $PSScriptRoot '..' 'GameAgent.psd1' | Resolve-Path
    Import-Module $ModulePath -Force

    # Dot-source the private function so it's available in test scope
    . (Join-Path $PSScriptRoot '..' 'Private' 'Test-GameAgentPort.ps1' | Resolve-Path)
}

Describe 'Test-GameAgentPort' {
    It 'Returns $true for a port with an active listener' {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        try {
            $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
            Test-GameAgentPort -Port $port | Should -BeTrue
        } finally {
            $listener.Stop()
        }
    }

    It 'Returns $false for a port with no listener' {
        # Use a high ephemeral port that is almost certainly not in use
        $port = 39471
        Test-GameAgentPort -Port $port | Should -BeFalse
    }

    It 'Does not leak sockets across multiple calls' {
        # Call multiple times on a closed port — should not throw or accumulate errors
        $port = 39472
        for ($i = 0; $i -lt 5; $i++) {
            { Test-GameAgentPort -Port $port } | Should -Not -Throw
        }
    }

    It 'Returns $false after a listener is stopped' {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
        $listener.Stop()

        # Small delay to ensure OS releases the socket
        Start-Sleep -Milliseconds 100
        Test-GameAgentPort -Port $port | Should -BeFalse
    }
}
