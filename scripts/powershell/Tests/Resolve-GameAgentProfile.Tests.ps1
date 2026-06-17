BeforeAll {
    $ModulePath = Join-Path $PSScriptRoot '..' 'GameAgent.psd1' | Resolve-Path
    Import-Module $ModulePath -Force
}

Describe 'Resolve-GameAgentProfile' {
    BeforeAll {
        # Create a temp directory tree that mimics the repo layout.
        # The function computes repo root as "$PSScriptRoot/../../.." relative to
        # Private/, so we build: <tmp>/scripts/powershell/Private/
        $TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "game-agent-test-$PID"
        $PrivateDir = Join-Path $TempRoot 'scripts' 'powershell' 'Private'
        New-Item -ItemType Directory -Path $PrivateDir -Force | Out-Null

        # Copy the real function file into the fake Private dir so $PSScriptRoot
        # points to our temp tree.
        $RealFunction = Join-Path $PSScriptRoot '..' 'Private' 'Resolve-GameAgentProfile.ps1' | Resolve-Path
        Copy-Item $RealFunction -Destination $PrivateDir

        # We'll dot-source this copy; its $PSScriptRoot will resolve to $PrivateDir,
        # so Resolve-Path(Join-Path $PSScriptRoot '../../..') -> $TempRoot.
        $FunctionFile = Join-Path $PrivateDir 'Resolve-GameAgentProfile.ps1'

        $UiDir = Join-Path $TempRoot 'ui'
        New-Item -ItemType Directory -Path $UiDir -Force | Out-Null
    }

    BeforeEach {
        # Clean up any .env.local between tests
        $EnvFile = Join-Path $TempRoot 'ui' '.env.local'
        if (Test-Path $EnvFile) { Remove-Item $EnvFile -Force }
        # Re-source the function each time so it re-reads $PSScriptRoot
        . $FunctionFile
    }

    AfterAll {
        if (Test-Path $TempRoot) { Remove-Item $TempRoot -Recurse -Force }
    }

    It 'Returns explicit profile when -Profile is provided' {
        $result = Resolve-GameAgentProfile -Profile 'demo'
        $result.Profile | Should -Be 'demo'
        $result.ProfileArgs | Should -Be @('--profile', 'demo')
    }

    It 'Reads AWS_PROFILE from ui/.env.local when no -Profile given' {
        $EnvFile = Join-Path $TempRoot 'ui' '.env.local'
        Set-Content -Path $EnvFile -Value "AWS_PROFILE=staging"
        . $FunctionFile

        $result = Resolve-GameAgentProfile
        $result.Profile | Should -Be 'staging'
        $result.ProfileArgs | Should -Be @('--profile', 'staging')
    }

    It 'Returns empty when no -Profile and no .env.local file' {
        $result = Resolve-GameAgentProfile
        $result.Profile | Should -BeNullOrEmpty
        $result.ProfileArgs | Should -HaveCount 0
    }

    It 'Returns empty when .env.local exists but has no AWS_PROFILE line' {
        $EnvFile = Join-Path $TempRoot 'ui' '.env.local'
        Set-Content -Path $EnvFile -Value "NEXT_PUBLIC_API_URL=http://localhost:8080"
        . $FunctionFile

        $result = Resolve-GameAgentProfile
        $result.Profile | Should -BeNullOrEmpty
        $result.ProfileArgs | Should -HaveCount 0
    }

    It 'Trims whitespace from the env file value' {
        $EnvFile = Join-Path $TempRoot 'ui' '.env.local'
        Set-Content -Path $EnvFile -Value "AWS_PROFILE=  myprofile  "
        . $FunctionFile

        $result = Resolve-GameAgentProfile
        $result.Profile | Should -Be 'myprofile'
        $result.ProfileArgs | Should -Be @('--profile', 'myprofile')
    }

    It 'Explicit -Profile takes precedence over .env.local' {
        $EnvFile = Join-Path $TempRoot 'ui' '.env.local'
        Set-Content -Path $EnvFile -Value "AWS_PROFILE=from-file"
        . $FunctionFile

        $result = Resolve-GameAgentProfile -Profile 'from-param'
        $result.Profile | Should -Be 'from-param'
        $result.ProfileArgs | Should -Be @('--profile', 'from-param')
    }
}
