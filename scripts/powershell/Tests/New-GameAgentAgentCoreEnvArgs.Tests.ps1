BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'New-GameAgentAgentCoreEnvArgs.ps1' | Resolve-Path)
}

Describe 'New-GameAgentAgentCoreEnvArgs' {
    It 'Omits unresolved optional values represented by <Label>' -TestCases @(
        @{ Label = 'an empty string'; Value = '' }
        @{ Label = 'the AWS CLI None sentinel'; Value = 'None' }
    ) {
        param($Label, $Value)

        $result = New-GameAgentAgentCoreEnvArgs `
            -OrchestratorModelId 'orchestrator-model' `
            -SpecialistModelId 'specialist-model' `
            -GuardrailId $Value `
            -OrchestratorPromptArn $Value `
            -GameLiftPromptArn $Value `
            -EksPromptArn $Value `
            -CostPromptArn $Value `
            -GameLiftKbId $Value `
            -EksKbId $Value `
            -CostKbId $Value

        ($result -join "`n") | Should -Be (@(
            '-env'
            'GBAW_ORCHESTRATOR_MODEL_ID=orchestrator-model'
            '-env'
            'GBAW_SPECIALIST_MODEL_ID=specialist-model'
        ) -join "`n")
    }

    It 'Includes valid optional values in stable order' {
        $result = New-GameAgentAgentCoreEnvArgs `
            -OrchestratorModelId 'orchestrator-model' `
            -SpecialistModelId 'specialist-model' `
            -GuardrailId 'guardrail-id' `
            -OrchestratorPromptArn 'orchestrator-prompt' `
            -GameLiftPromptArn 'gamelift-prompt' `
            -EksPromptArn 'eks-prompt' `
            -CostPromptArn 'cost-prompt' `
            -GameLiftKbId 'gamelift-kb' `
            -EksKbId 'eks-kb' `
            -CostKbId 'cost-kb'

        ($result -join "`n") | Should -Be (@(
            '-env'
            'GBAW_ORCHESTRATOR_MODEL_ID=orchestrator-model'
            '-env'
            'GBAW_SPECIALIST_MODEL_ID=specialist-model'
            '-env'
            'GBAW_BEDROCK_GUARDRAIL_ID=guardrail-id'
            '-env'
            'GBAW_BEDROCK_GUARDRAIL_VERSION=DRAFT'
            '-env'
            'GBAW_ORCHESTRATOR_PROMPT_ARN=orchestrator-prompt'
            '-env'
            'GBAW_GAMELIFT_PROMPT_ARN=gamelift-prompt'
            '-env'
            'GBAW_EKS_PROMPT_ARN=eks-prompt'
            '-env'
            'GBAW_COST_PROMPT_ARN=cost-prompt'
            '-env'
            'GBAW_GAMELIFT_KB_ID=gamelift-kb'
            '-env'
            'GBAW_EKS_KB_ID=eks-kb'
            '-env'
            'GBAW_COST_KB_ID=cost-kb'
        ) -join "`n")
    }

    It 'Filters optional values independently' {
        $result = New-GameAgentAgentCoreEnvArgs `
            -OrchestratorModelId 'orchestrator-model' `
            -SpecialistModelId 'specialist-model' `
            -GuardrailId 'None' `
            -OrchestratorPromptArn 'orchestrator-prompt' `
            -GameLiftPromptArn 'None' `
            -EksPromptArn 'eks-prompt' `
            -GameLiftKbId 'gamelift-kb' `
            -EksKbId 'None' `
            -CostKbId 'cost-kb'

        ($result -join "`n") | Should -Be (@(
            '-env'
            'GBAW_ORCHESTRATOR_MODEL_ID=orchestrator-model'
            '-env'
            'GBAW_SPECIALIST_MODEL_ID=specialist-model'
            '-env'
            'GBAW_ORCHESTRATOR_PROMPT_ARN=orchestrator-prompt'
            '-env'
            'GBAW_EKS_PROMPT_ARN=eks-prompt'
            '-env'
            'GBAW_GAMELIFT_KB_ID=gamelift-kb'
            '-env'
            'GBAW_COST_KB_ID=cost-kb'
        ) -join "`n")
    }
}
