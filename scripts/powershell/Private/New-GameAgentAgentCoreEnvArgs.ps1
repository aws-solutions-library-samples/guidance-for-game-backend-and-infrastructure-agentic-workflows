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
        # Source Control Connector runtime env values, already resolved by the caller. Keys
        # are GBAW_SCM_* names; only non-empty values are emitted, in the fixed order below,
        # matching scripts/deploy.sh. The read-credential ARN
        # (GBAW_SCM_READ_CREDENTIAL_SECRET_ARN) must be included ONLY when the connector is
        # enabled so a disabled deployment carries no connector secret env var.
        [hashtable]$ScmEnv = @{}
    )

    $result = @(
        '-env', "GBAW_ORCHESTRATOR_MODEL_ID=$OrchestratorModelId",
        '-env', "GBAW_SPECIALIST_MODEL_ID=$SpecialistModelId"
    )
    $hasGuardrail = $GuardrailId -and $GuardrailId -ne 'None'
    $optionalValues = [ordered]@{
        GBAW_BEDROCK_GUARDRAIL_ID      = if ($hasGuardrail) { $GuardrailId } else { '' }
        GBAW_BEDROCK_GUARDRAIL_VERSION = if ($hasGuardrail) { 'DRAFT' } else { '' }
        GBAW_ORCHESTRATOR_PROMPT_ARN   = $OrchestratorPromptArn
        GBAW_GAMELIFT_PROMPT_ARN       = $GameLiftPromptArn
        GBAW_EKS_PROMPT_ARN            = $EksPromptArn
        GBAW_COST_PROMPT_ARN           = $CostPromptArn
        GBAW_GAMELIFT_KB_ID             = $GameLiftKbId
        GBAW_EKS_KB_ID                  = $EksKbId
        GBAW_COST_KB_ID                 = $CostKbId
    }
    foreach ($entry in $optionalValues.GetEnumerator()) {
        if ($entry.Value -and $entry.Value -ne 'None') {
            $result += @('-env', "$($entry.Key)=$($entry.Value)")
        }
    }

    # Source Control Connector env vars, emitted in the SAME fixed order as
    # scripts/deploy.sh so the two deployment paths produce identical connector config.
    # Only non-empty values are emitted; the read-credential ARN is emitted only when the
    # caller included it (i.e. when the connector is enabled).
    $scmOrder = @(
        'GBAW_SCM_CONNECTOR_ENABLED',
        'GBAW_SCM_PROVIDER',
        'GBAW_SCM_PROVIDER_BASE_URL',
        'GBAW_SCM_REPO_ALLOWLIST',
        'GBAW_SCM_AUTHORIZED_GROUPS',
        'GBAW_SCM_AUDIT_LOG_GROUP',
        'GBAW_SCM_RATE_LIMIT_MAX',
        'GBAW_SCM_RATE_LIMIT_WINDOW_SECONDS',
        'GBAW_SCM_PROVIDER_TIMEOUT_SECONDS',
        'GBAW_SCM_RETRY_MAX_ATTEMPTS',
        'GBAW_SCM_MAX_FILES_PER_REQUEST',
        'GBAW_SCM_MAX_CONTENT_BYTES',
        'GBAW_SCM_READ_CREDENTIAL_SECRET_ARN'
    )
    foreach ($key in $scmOrder) {
        $value = $ScmEnv[$key]
        if ($value) {
            $result += @('-env', "$key=$value")
        }
    }

    return $result
}
