@{
    ModuleVersion        = '2.0.0'
    GUID                 = 'a8f3c2d1-4b5e-6789-0abc-def123456789'
    Author               = 'Game Agent Team'
    CompanyName          = 'AWS'
    Copyright            = '(c) 2024 Amazon Web Services, Inc. All rights reserved.'
    Description          = 'PowerShell module for deploying and managing Game Agent on AWS. Uses AWS CLI for all operations.'

    PowerShellVersion    = '7.0'

    # No RequiredModules — uses AWS CLI instead of AWS.Tools
    RequiredModules      = @()

    RootModule           = 'GameAgent.psm1'

    FunctionsToExport    = @(
        'Add-GameAgentAdmin'
        'Remove-GameAgentAdmin'
        'Deploy-GameAgent'
        'Remove-GameAgent'
        'Start-GameAgentDev'
        'Stop-GameAgentDev'
        'Get-GameAgentStatus'
        'Test-GameAgentUnit'
        'Test-GameAgentFull'
    )

    CmdletsToExport      = @()
    VariablesToExport    = @()
    AliasesToExport      = @()

    PrivateData          = @{
        PSData = @{
            Tags         = @('AWS', 'Bedrock', 'AI', 'GameLift', 'EKS', 'Deployment')
            ReleaseNotes = 'v2.0: Full parity with bash scripts. Uses AWS CLI instead of AWS.Tools modules.'
        }
    }
}
