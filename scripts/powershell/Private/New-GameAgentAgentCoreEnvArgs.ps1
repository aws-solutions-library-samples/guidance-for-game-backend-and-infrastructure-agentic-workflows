function New-GameAgentAgentCoreEnvArgs {
    <# Builds AgentCore CLI environment arguments from resolved deployment values. #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param(
        [Parameter(Mandatory)][string]$OrchestratorModelId,
        [Parameter(Mandatory)][string]$SpecialistModelId,
        [string]$GuardrailId = '',
        [string]$OrchestratorPromptArn = '',
        [string]$GameLiftPromptArn = '',
        [string]$EksPromptArn = '',
        [string]$CostPromptArn = '',
        [string]$GameLiftKbId = '',
        [string]$EksKbId = '',
        [string]$CostKbId = '',
        [string]$TenantId = '',
        [string]$WorkspaceId = '',
        [string]$CostSnapshotTableName = '',
        [string]$CostSnapshotRequired = '',
        [string]$CostSnapshotTtlSeconds = ''
    )

    $result = @(
        '-env', 'GBAW_HOSTED_RUNTIME=true',
        '-env', "GBAW_ORCHESTRATOR_MODEL_ID=$OrchestratorModelId",
        '-env', "GBAW_SPECIALIST_MODEL_ID=$SpecialistModelId"
    )
    $hasGuardrail = $GuardrailId -and $GuardrailId -ne 'None'
    $optionalValues = [ordered]@{
        GBAW_BEDROCK_GUARDRAIL_ID       = if ($hasGuardrail) { $GuardrailId } else { '' }
        GBAW_BEDROCK_GUARDRAIL_VERSION  = if ($hasGuardrail) { 'DRAFT' } else { '' }
        GBAW_ORCHESTRATOR_PROMPT_ARN    = $OrchestratorPromptArn
        GBAW_GAMELIFT_PROMPT_ARN        = $GameLiftPromptArn
        GBAW_EKS_PROMPT_ARN             = $EksPromptArn
        GBAW_COST_PROMPT_ARN            = $CostPromptArn
        GBAW_GAMELIFT_KB_ID             = $GameLiftKbId
        GBAW_EKS_KB_ID                  = $EksKbId
        GBAW_COST_KB_ID                 = $CostKbId
        # Trusted deployment identity + shared cost report snapshot store (#365)
        GBAW_TENANT_ID                  = $TenantId
        GBAW_WORKSPACE_ID               = $WorkspaceId
        GBAW_COST_SNAPSHOT_TABLE_NAME   = $CostSnapshotTableName
        GBAW_COST_SNAPSHOT_REQUIRED     = $CostSnapshotRequired
        GBAW_COST_SNAPSHOT_TTL_SECONDS  = $CostSnapshotTtlSeconds
    }
    foreach ($entry in $optionalValues.GetEnumerator()) {
        if ($entry.Value -and $entry.Value -ne 'None') {
            $result += @('-env', "$($entry.Key)=$($entry.Value)")
        }
    }
    return $result
}
