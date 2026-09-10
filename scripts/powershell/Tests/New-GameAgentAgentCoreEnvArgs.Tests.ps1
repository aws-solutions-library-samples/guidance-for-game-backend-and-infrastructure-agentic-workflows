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
            -SourceControlPromptArn 'source-control-prompt' `
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
            'GBAW_SOURCE_CONTROL_PROMPT_ARN=source-control-prompt'
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

    # --- PR #319 finding 7: Source Control Connector env parity with deploy.sh ---

    It 'Emits enabled connector env values INCLUDING the read credential, in deploy.sh order' {
        $scmEnv = @{
            GBAW_SCM_CONNECTOR_ENABLED         = 'true'
            GBAW_SCM_PROVIDER                  = 'github'
            GBAW_SCM_REPO_ALLOWLIST            = 'org/iac=main'
            GBAW_SCM_AUTHORIZED_GROUPS         = 'scm-writers'
            GBAW_SCM_AUDIT_LOG_GROUP           = '/scm/audit'
            GBAW_SCM_MAX_CONTENT_BYTES         = '2097152'
            GBAW_SCM_READ_CREDENTIAL_SECRET_ARN = 'arn:aws:secretsmanager:us-west-2:123456789012:secret:x-AbCdEf'
        }

        $result = New-GameAgentAgentCoreEnvArgs `
            -OrchestratorModelId 'orchestrator-model' `
            -SpecialistModelId 'specialist-model' `
            -ScmEnv $scmEnv

        ($result -join "`n") | Should -Be (@(
            '-env'
            'GBAW_ORCHESTRATOR_MODEL_ID=orchestrator-model'
            '-env'
            'GBAW_SPECIALIST_MODEL_ID=specialist-model'
            '-env'
            'GBAW_SCM_CONNECTOR_ENABLED=true'
            '-env'
            'GBAW_SCM_PROVIDER=github'
            '-env'
            'GBAW_SCM_REPO_ALLOWLIST=org/iac=main'
            '-env'
            'GBAW_SCM_AUTHORIZED_GROUPS=scm-writers'
            '-env'
            'GBAW_SCM_AUDIT_LOG_GROUP=/scm/audit'
            '-env'
            'GBAW_SCM_MAX_CONTENT_BYTES=2097152'
            '-env'
            'GBAW_SCM_READ_CREDENTIAL_SECRET_ARN=arn:aws:secretsmanager:us-west-2:123456789012:secret:x-AbCdEf'
        ) -join "`n")
    }

    It 'Omits the read credential (and unset values) for a DISABLED connector deployment' {
        # A disabled deployment: the caller does not put the read-credential ARN in the
        # hashtable, so no connector secret env var is emitted (parity with bash + the gated
        # IAM grant). Other set values are still forwarded.
        $scmEnv = @{
            GBAW_SCM_CONNECTOR_ENABLED = 'false'
            GBAW_SCM_PROVIDER          = 'github'
        }

        $result = New-GameAgentAgentCoreEnvArgs `
            -OrchestratorModelId 'orchestrator-model' `
            -SpecialistModelId 'specialist-model' `
            -ScmEnv $scmEnv

        ($result -join "`n") | Should -Be (@(
            '-env'
            'GBAW_ORCHESTRATOR_MODEL_ID=orchestrator-model'
            '-env'
            'GBAW_SPECIALIST_MODEL_ID=specialist-model'
            '-env'
            'GBAW_SCM_CONNECTOR_ENABLED=false'
            '-env'
            'GBAW_SCM_PROVIDER=github'
        ) -join "`n")
        # No connector secret env var on a disabled deployment.
        ($result -join "`n") | Should -Not -Match 'GBAW_SCM_READ_CREDENTIAL_SECRET_ARN'
    }

    It 'Emits no connector env when ScmEnv is empty (unaffected non-connector deployments)' {
        $result = New-GameAgentAgentCoreEnvArgs `
            -OrchestratorModelId 'orchestrator-model' `
            -SpecialistModelId 'specialist-model'

        ($result -join "`n") | Should -Not -Match 'GBAW_SCM_'
    }
}
