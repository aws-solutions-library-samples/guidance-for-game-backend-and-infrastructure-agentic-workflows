function Deploy-GameAgent {
    <#
    .SYNOPSIS
        Deploys Game Agent infrastructure to AWS.
    .DESCRIPTION
        Full deployment matching deploy.sh: base infra, guardrails, prompts,
        account observability, AgentCore Runtime, CW delivery, observability stack,
        Knowledge Bases, seed KBs, wire KBs, frontend (Docker), security.
    .PARAMETER ProjectName
        Project name for resource naming. Default: "game-agent".
    .PARAMETER Region
        AWS region. Default: "us-west-2".
    .PARAMETER Profile
        AWS CLI profile. Default: reads from ui/.env.local or uses "default".
    .PARAMETER SkipFrontend
        Skip frontend steps (6-8) even if Docker is available.
    .EXAMPLE
        Deploy-GameAgent
    .EXAMPLE
        Deploy-GameAgent -Profile demo -Region us-east-1
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [string]$ProjectName = 'game-agent',
        [string]$Region = 'us-west-2',
        [string]$Profile,
        [switch]$SkipFrontend
    )

    $ErrorActionPreference = 'Stop'
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $infraPath = Join-Path $repoRoot 'infrastructure/cloudformation'
    $backendPath = Join-Path $repoRoot 'backend'
    $uiPath = Join-Path $repoRoot 'ui'

    # Resolve profile
    $resolved = Resolve-GameAgentProfile -Profile $Profile
    $Profile = $resolved.Profile
    $profileArgs = $resolved.ProfileArgs

    # Helper: run aws cli and throw on failure
    function Invoke-Aws {
        param([Parameter(ValueFromRemainingArguments)][string[]]$AwsArgs)
        $allArgs = $AwsArgs + $profileArgs
        $result = & aws @allArgs 2>&1
        if ($LASTEXITCODE -ne 0) { throw "aws $($allArgs -join ' ') failed: $result" }
        return $result
    }

    # Helper: deploy a CFN stack (matches bash `aws cloudformation deploy`)
    function Deploy-Stack {
        param([string]$StackName, [string]$TemplateFile, [string[]]$Params, [string]$Caps = 'CAPABILITY_NAMED_IAM')
        Write-GameAgentStatus "Deploying stack: $StackName..." -Type Info
        $deployArgs = @('cloudformation', 'deploy',
            '--template-file', $TemplateFile,
            '--stack-name', $StackName,
            '--capabilities', $Caps,
            '--region', $Region,
            '--no-fail-on-empty-changeset')
        if ($Params) { $deployArgs += '--parameter-overrides'; $deployArgs += $Params }
        $deployArgs += $profileArgs
        $output = & aws @deployArgs 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Stack deploy failed for ${StackName}: $output" }
        Write-GameAgentStatus "Stack deployed: $StackName" -Type Success
    }

    # Helper: get CFN output value
    function Get-StackOutput {
        param([string]$StackName, [string]$OutputKey)
        $val = Invoke-Aws cloudformation describe-stacks `
            --stack-name $StackName --region $Region `
            --query "Stacks[0].Outputs[?OutputKey==``$OutputKey``].OutputValue" --output text
        return ($val | Out-String).Trim()
    }

    Write-Host ''
    Write-Host ('=' * 60) -ForegroundColor Cyan
    Write-GameAgentStatus 'Game Agent - Complete Deployment' -Type Info
    Write-Host ('=' * 60) -ForegroundColor Cyan
    Write-Host "Region:  $Region"
    Write-Host "Profile: $($Profile ?? 'default')"
    Write-Host ''

    # ── Prerequisites ──
    Write-GameAgentStatus 'Checking prerequisites...' -Type Info
    foreach ($cmd in @('aws', 'uv', 'yq')) {
        if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
            throw "$cmd not found. Install it before running Deploy-GameAgent."
        }
    }
    if (-not (Get-Command jq -ErrorAction SilentlyContinue)) {
        Write-GameAgentStatus 'jq not found, attempting install...' -Type Warning
        if ($IsMacOS -and (Get-Command brew -ErrorAction SilentlyContinue)) {
            brew install jq
        } elseif ($IsWindows -and (Get-Command winget -ErrorAction SilentlyContinue)) {
            winget install jqlang.jq --accept-source-agreements --accept-package-agreements
        } elseif ($IsLinux -and (Get-Command apt-get -ErrorAction SilentlyContinue)) {
            sudo apt-get install -y jq
        } else {
            throw 'jq not found. Install manually: https://jqlang.github.io/jq/download/'
        }
    }

    $dockerAvailable = $false
    if ((Get-Command docker -ErrorAction SilentlyContinue) -and (-not $SkipFrontend)) {
        try { docker info 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $dockerAvailable = $true } } catch {}
    }
    if (-not $dockerAvailable) {
        Write-GameAgentStatus 'Docker not available — frontend steps (6-8) will be skipped.' -Type Warning
    }

    # Verify credentials
    $identity = (Invoke-Aws sts get-caller-identity --region $Region --output json) | ConvertFrom-Json
    Write-GameAgentStatus "Authenticated as: $($identity.Arn)" -Type Success
    Write-Host ''

    if (-not $PSCmdlet.ShouldProcess("Game Agent infrastructure in $Region", 'Deploy')) {
        Write-GameAgentStatus 'Deployment skipped (-WhatIf mode)' -Type Warning
        return
    }

    # ── Step 0: Download KB documentation ──
    Write-GameAgentStatus 'Step 0: Downloading KB documentation...' -Type Info
    Invoke-GameAgentKBDocDownload -Region $Region -ProfileArgs $profileArgs
    Write-Host ''

    # ── Step 1: Base infrastructure ──
    Write-GameAgentStatus 'Step 1: Deploying base infrastructure...' -Type Info

    # Source Control Connector: resolve the connector settings from the environment or
    # backend/.env.local so this PowerShell deployment produces the SAME connector config +
    # IAM state as scripts/deploy.sh. A local reader is used because the module's
    # Read-EnvVar helper is not defined until Step 1.6 (after this base-stack deploy).
    $scmEnvFile = Join-Path $backendPath '.env.local'
    $scmEnvContent = if (Test-Path $scmEnvFile) { Get-Content $scmEnvFile -Raw } else { '' }
    function Read-ScmSetting {
        param([string]$Name)
        # Prefer a process env var; fall back to backend/.env.local (matches bash).
        $envValue = [Environment]::GetEnvironmentVariable($Name)
        if ($envValue) { return $envValue.Trim() }
        if ($scmEnvContent -match "(?m)^$Name=(.+)$") { return $Matches[1].Trim() }
        return ''
    }
    $scmReadCredentialSecretArn = Read-ScmSetting 'GBAW_SCM_READ_CREDENTIAL_SECRET_ARN'
    $scmAuditLogGroup           = Read-ScmSetting 'GBAW_SCM_AUDIT_LOG_GROUP'

    # Normalize the enablement flag to a canonical 'true'/'false' using the same truthy set
    # as connector.config ({true,1,yes}, case-insensitive). A disabled deployment must carry
    # NO connector secret env var and NO connector secret IAM permission.
    $scmEnabledRaw = (Read-ScmSetting 'GBAW_SCM_CONNECTOR_ENABLED').ToLowerInvariant()
    $scmConnectorEnabled = if (@('true', '1', 'yes') -contains $scmEnabledRaw) { 'true' } else { 'false' }

    # The read-credential ARN is only delivered to the base stack (for the scoped
    # GetSecretValue grant) when the connector is enabled; disabled => empty so the
    # ScmReadCredentialActive condition is false and no secret permission is granted.
    $scmBaseReadArn = if ($scmConnectorEnabled -eq 'true') { $scmReadCredentialSecretArn } else { '' }

    $baseParams = @(
        "ProjectName=$ProjectName",
        "ScmReadCredentialSecretArn=$scmBaseReadArn",
        "ScmConnectorEnabled=$scmConnectorEnabled"
    )
    # Pass the audit log-group name only when configured, mirroring the bash guard.
    if ($scmAuditLogGroup) { $baseParams += "ScmAuditLogGroupName=$scmAuditLogGroup" }

    # Build the connector runtime env hashtable once (reused at both agentcore launch call
    # sites). Every GBAW_SCM_* tuning value is read the same way the bash loop reads them;
    # only non-empty values are emitted by New-GameAgentAgentCoreEnvArgs. The read-credential
    # ARN is included ONLY when the connector is enabled, so a disabled deployment carries no
    # connector secret env var (parity with the gated IAM grant).
    $scmRuntimeEnv = @{
        GBAW_SCM_CONNECTOR_ENABLED         = Read-ScmSetting 'GBAW_SCM_CONNECTOR_ENABLED'
        GBAW_SCM_PROVIDER                  = Read-ScmSetting 'GBAW_SCM_PROVIDER'
        GBAW_SCM_PROVIDER_BASE_URL         = Read-ScmSetting 'GBAW_SCM_PROVIDER_BASE_URL'
        GBAW_SCM_REPO_ALLOWLIST            = Read-ScmSetting 'GBAW_SCM_REPO_ALLOWLIST'
        GBAW_SCM_AUTHORIZED_GROUPS         = Read-ScmSetting 'GBAW_SCM_AUTHORIZED_GROUPS'
        GBAW_SCM_AUDIT_LOG_GROUP           = $scmAuditLogGroup
        GBAW_SCM_RATE_LIMIT_MAX            = Read-ScmSetting 'GBAW_SCM_RATE_LIMIT_MAX'
        GBAW_SCM_RATE_LIMIT_WINDOW_SECONDS = Read-ScmSetting 'GBAW_SCM_RATE_LIMIT_WINDOW_SECONDS'
        GBAW_SCM_PROVIDER_TIMEOUT_SECONDS  = Read-ScmSetting 'GBAW_SCM_PROVIDER_TIMEOUT_SECONDS'
        GBAW_SCM_RETRY_MAX_ATTEMPTS        = Read-ScmSetting 'GBAW_SCM_RETRY_MAX_ATTEMPTS'
        GBAW_SCM_MAX_FILES_PER_REQUEST     = Read-ScmSetting 'GBAW_SCM_MAX_FILES_PER_REQUEST'
        GBAW_SCM_MAX_CONTENT_BYTES         = Read-ScmSetting 'GBAW_SCM_MAX_CONTENT_BYTES'
    }
    if ($scmConnectorEnabled -eq 'true' -and $scmReadCredentialSecretArn) {
        $scmRuntimeEnv['GBAW_SCM_READ_CREDENTIAL_SECRET_ARN'] = $scmReadCredentialSecretArn
    }

    Deploy-Stack -StackName "$ProjectName-infrastructure" `
        -TemplateFile (Join-Path $infraPath '01-base-infrastructure.yaml') `
        -Params $baseParams
    Write-Host ''

    # ── Step 1.5: Guardrails ──
    Write-GameAgentStatus 'Step 1.5: Deploying Bedrock Guardrails...' -Type Info
    Deploy-Stack -StackName "$ProjectName-guardrails" `
        -TemplateFile (Join-Path $infraPath '04-bedrock-guardrails.yaml') `
        -Params @("ProjectName=$ProjectName")

    $guardrailId = Get-StackOutput "$ProjectName-guardrails" 'GuardrailId'
    Write-GameAgentStatus "Guardrails deployed: $guardrailId" -Type Success
    Write-Host ''

    # ── Step 1.6: Managed Prompts ──
    Write-GameAgentStatus 'Step 1.6: Deploying Bedrock Managed Prompts...' -Type Info
    $env:AWS_REGION = $Region
    if ($Profile) { $env:AWS_PROFILE = $Profile }
    Invoke-GameAgentPromptDeploy -Region $Region -ProfileArgs $profileArgs

    # Read prompt ARNs from backend/.env.local
    $envFile = Join-Path $backendPath '.env.local'
    $envContent = if (Test-Path $envFile) { Get-Content $envFile -Raw } else { '' }
    function Read-EnvVar { param([string]$Name) if ($envContent -match "(?m)^$Name=(.+)$") { $Matches[1].Trim() } else { '' } }
    $orchestratorPromptArn = Read-EnvVar 'GBAW_ORCHESTRATOR_PROMPT_ARN'
    $gameliftPromptArn     = Read-EnvVar 'GBAW_GAMELIFT_PROMPT_ARN'
    $eksPromptArn          = Read-EnvVar 'GBAW_EKS_PROMPT_ARN'
    $costPromptArn         = Read-EnvVar 'GBAW_COST_PROMPT_ARN'
    Write-GameAgentStatus 'Managed Prompts deployed' -Type Success
    Write-Host ''

    # ── Step 1.7: Account-wide observability ──
    Write-GameAgentStatus 'Step 1.7: Setting up account-wide observability...' -Type Info
    Invoke-GameAgentAccountObservability -Region $Region -ProfileArgs $profileArgs
    Write-Host ''

    # ── Step 2: AgentCore Runtime ──
    Write-GameAgentStatus 'Step 2: Launching AgentCore Runtime...' -Type Info
    Push-Location $backendPath
    try {
        Write-GameAgentStatus 'Installing backend dependencies...' -Type Info
        uv sync 2>$null
        if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit code $LASTEXITCODE)" }

        # Resolve both model roles through the canonical Python configuration.
        $settingsLoader = Join-Path $repoRoot 'config/load_deployment_settings.py'
        $modelSettingsJson = (uv run python $settingsLoader --format json --models-only) | Out-String
        if ($LASTEXITCODE -ne 0) { throw "model settings resolution failed (exit code $LASTEXITCODE)" }
        $modelSettings = $modelSettingsJson | ConvertFrom-Json
        $orchestratorModelId = $modelSettings.GBAW_ORCHESTRATOR_MODEL_ID
        $specialistModelId = $modelSettings.GBAW_SPECIALIST_MODEL_ID
        Write-Host "   Orchestrator model: $orchestratorModelId"
        Write-Host "   Specialist model:   $specialistModelId"

        $agentCoreEnvArgs = New-GameAgentAgentCoreEnvArgs `
            -OrchestratorModelId $orchestratorModelId `
            -SpecialistModelId $specialistModelId `
            -GuardrailId $guardrailId `
            -OrchestratorPromptArn $orchestratorPromptArn `
            -GameLiftPromptArn $gameliftPromptArn `
            -EksPromptArn $eksPromptArn `
            -CostPromptArn $costPromptArn `
            -ScmEnv $scmRuntimeEnv

        $executionRoleArn = Get-StackOutput "$ProjectName-infrastructure" 'AgentCoreExecutionRoleArn'
        Write-Host "Using execution role: $executionRoleArn"

        # Export requirements.txt from uv.lock
        Write-GameAgentStatus 'Exporting requirements.txt...' -Type Info
        uv export --format requirements-txt --no-dev --no-hashes --output-file requirements.txt.tmp 2>$null
        if ($LASTEXITCODE -ne 0) { throw "uv export failed (exit code $LASTEXITCODE)" }
        $oldDeps = if (Test-Path requirements.txt) { (Get-Content requirements.txt | Where-Object { $_ -notmatch '^#|^\s*$' }) -join "`n" } else { '' }
        $newDeps = (Get-Content requirements.txt.tmp | Where-Object { $_ -notmatch '^#|^\s*$' }) -join "`n"
        if ($oldDeps -ne $newDeps) {
            Write-GameAgentStatus 'requirements.txt out of sync, updating...' -Type Warning
            # `uv export` already writes a complete, correct file (its own 2-line
            # header + full dependency list), so use it as-is. The previous
            # `-TotalCount 21` header copy assumed a 21-line header and duplicated
            # the first ~19 packages.
            Move-Item requirements.txt.tmp requirements.txt -Force
        } else {
            Remove-Item requirements.txt.tmp -ErrorAction SilentlyContinue
        }
        Write-GameAgentStatus 'requirements.txt verified' -Type Success

        # Configure or skip
        $existingRuntime = ''
        if (Test-Path '.bedrock_agentcore.yaml') {
            $existingRuntime = (yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml 2>$null) | Out-String
            $existingRuntime = $existingRuntime.Trim()
            if ($existingRuntime -eq 'null') { $existingRuntime = '' }
        }

        if (-not $existingRuntime) {
            Write-GameAgentStatus 'Configuring AgentCore (first deploy)...' -Type Info
            uv run agentcore configure `
                --entrypoint agentcore_main.py `
                --name gameagentruntime `
                --region $Region `
                --execution-role $executionRoleArn `
                --requirements-file requirements.txt `
                --non-interactive
            if ($LASTEXITCODE -ne 0) { throw "agentcore configure failed (exit code $LASTEXITCODE)" }
        } else {
            Write-GameAgentStatus 'AgentCore already configured, skipping configure step' -Type Info
        }

        # Note: no Dockerfile patching needed for MCP servers. ccapi-mcp-server (which
        # needed a writable .schemas dir) was replaced by aws-api-mcp-server, whose
        # log/working-dir are redirected to /tmp via environment variables in
        # utils/mcp_client_factory.create_mcp_client (no filesystem patch needed).

        # Launch or skip
        if (-not $existingRuntime) {
            Write-GameAgentStatus 'Launching new AgentCore Runtime (CodeBuild)...' -Type Info
            uv run agentcore launch --auto-update-on-conflict @agentCoreEnvArgs
            if ($LASTEXITCODE -ne 0) { throw "agentcore launch failed (exit code $LASTEXITCODE)" }
            Start-Sleep -Seconds 10
        } else {
            Write-GameAgentStatus "Runtime already exists: $existingRuntime" -Type Info
        }

        $runtimeArn = ((yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml) | Out-String).Trim()
        $runtimeId = $runtimeArn.Split('/')[-1]
        Write-GameAgentStatus "AgentCore Runtime ready: $runtimeId" -Type Success
        Write-Host ''

        # ── Step 2b: CloudWatch delivery for runtime traces ──
        Write-GameAgentStatus 'Step 2b: Ensuring CloudWatch delivery for runtime traces...' -Type Info
        $deliverySourceName = "$runtimeId-traces-source"
        $deliveryDestName = "$runtimeId-traces-destination"

        try { Invoke-Aws logs put-delivery-source --name $deliverySourceName --log-type TRACES --resource-arn $runtimeArn --region $Region | Out-Null }
        catch { <# already exists #> }
        Write-Host '  Delivery source OK'

        $deliveryDestArn = ''
        try {
            $destResult = (Invoke-Aws logs put-delivery-destination --name $deliveryDestName --delivery-destination-type XRAY --region $Region) | ConvertFrom-Json
            $deliveryDestArn = $destResult.deliveryDestination.arn
        } catch {
            $accountId = $identity.Account
            $deliveryDestArn = "arn:aws:logs:${Region}:${accountId}:delivery-destination:${deliveryDestName}"
        }
        Write-Host '  Delivery destination OK'

        if ($deliveryDestArn) {
            try { Invoke-Aws logs create-delivery --delivery-source-name $deliverySourceName --delivery-destination-arn $deliveryDestArn --region $Region | Out-Null }
            catch { <# already exists #> }
            Write-Host '  Delivery OK'
        }
        Write-GameAgentStatus 'Runtime traces delivery configured' -Type Success
        Write-Host ''
    } finally { Pop-Location }

    # ── Step 3: Observability stack ──
    Write-GameAgentStatus 'Step 3: Deploying observability stack...' -Type Info
    Deploy-Stack -StackName "$ProjectName-observability" `
        -TemplateFile (Join-Path $infraPath '03-agentcore-observability.yaml') `
        -Params @("ProjectName=$ProjectName", "RuntimeId=$runtimeId")
    Write-Host ''

    # ── Step 4: Knowledge Bases ──
    Write-GameAgentStatus 'Step 4: Deploying Knowledge Bases...' -Type Info
    Invoke-GameAgentKBDeploy -Region $Region -ProfileArgs $profileArgs -ProjectName $ProjectName
    Write-Host ''

    # ── Step 5: Seed Knowledge Bases ──
    Write-GameAgentStatus 'Step 5: Seeding Knowledge Bases...' -Type Info
    foreach ($kb in @('gamelift', 'eks', 'cost')) {
        Invoke-GameAgentKBSeed -KBName $kb -Region $Region -ProfileArgs $profileArgs -ProjectName $ProjectName
    }
    Write-GameAgentStatus 'Knowledge Bases seeded' -Type Success
    Write-Host ''

    # ── Step 5b: Wire KB IDs to AgentCore Runtime ──
    Write-GameAgentStatus 'Step 5b: Wiring Knowledge Bases to AgentCore Runtime...' -Type Info
    Push-Location $backendPath
    try {
        # Re-read env file after deploy-kb.sh updated it
        $envContent = if (Test-Path '.env.local') { Get-Content '.env.local' -Raw } else { '' }
        $gameliftKbId = Read-EnvVar 'GBAW_GAMELIFT_KB_ID'
        $eksKbId      = Read-EnvVar 'GBAW_EKS_KB_ID'
        $costKbId     = Read-EnvVar 'GBAW_COST_KB_ID'

        if ($gameliftKbId) { Write-Host "   GameLift KB: $gameliftKbId" }
        if ($eksKbId) { Write-Host "   EKS KB:      $eksKbId" }
        if ($costKbId) { Write-Host "   Cost KB:     $costKbId" }

        # Always pass the complete resolved environment so model-only updates
        # are applied and optional service settings are preserved together.
        $agentCoreEnvArgs = New-GameAgentAgentCoreEnvArgs `
            -OrchestratorModelId $orchestratorModelId `
            -SpecialistModelId $specialistModelId `
            -GuardrailId $guardrailId `
            -OrchestratorPromptArn $orchestratorPromptArn `
            -GameLiftPromptArn $gameliftPromptArn `
            -EksPromptArn $eksPromptArn `
            -CostPromptArn $costPromptArn `
            -GameLiftKbId $gameliftKbId `
            -EksKbId $eksKbId `
            -CostKbId $costKbId `
            -ScmEnv $scmRuntimeEnv
        uv run agentcore launch --auto-update-on-conflict @agentCoreEnvArgs
        if ($LASTEXITCODE -ne 0) { throw "agentcore launch (runtime environment update) failed (exit code $LASTEXITCODE)" }
        Write-GameAgentStatus 'AgentCore Runtime updated with role models and available service configuration' -Type Success
    } finally { Pop-Location }
    Write-Host ''

    # ── Steps 6-8: Frontend (requires Docker) ──
    $frontendUrl = ''
    if ($dockerAvailable) {
        # Step 6: Build and push frontend container
        Write-GameAgentStatus 'Step 6: Building and pushing frontend container...' -Type Info
        Push-Location $uiPath
        try {
            $frontendEcrRepo = Get-StackOutput "$ProjectName-infrastructure" 'FrontendRepositoryUri'
            Write-Host "Frontend ECR: $frontendEcrRepo"

            $ecrPassword = Invoke-Aws ecr get-login-password --region $Region
            $ecrPassword | docker login --username AWS --password-stdin $frontendEcrRepo
            docker build --platform linux/amd64 -t "${frontendEcrRepo}:latest" .
            docker push "${frontendEcrRepo}:latest"
            Write-GameAgentStatus 'Frontend container pushed' -Type Success

            # Step 6b: SBOM generation
            if (Get-Command syft -ErrorAction SilentlyContinue) {
                Write-GameAgentStatus 'Step 6b: Generating SBOMs...' -Type Info
                $sbomScript = Join-Path $repoRoot 'scripts/generate-sbom.sh'
                bash $sbomScript "${frontendEcrRepo}:latest"
                Write-GameAgentStatus 'SBOMs generated' -Type Success
            } else {
                Write-GameAgentStatus 'Syft not installed, skipping SBOM generation' -Type Warning
            }
        } finally { Pop-Location }
        Write-Host ''

        # Step 7: Deploy frontend
        Write-GameAgentStatus 'Step 7: Deploying frontend...' -Type Info
        Deploy-Stack -StackName "$ProjectName-frontend" `
            -TemplateFile (Join-Path $infraPath '02-frontend-ecs-express.yaml') `
            -Params @("ProjectName=$ProjectName", "RuntimeId=$runtimeId")

        $frontendUrl = Get-StackOutput "$ProjectName-frontend" 'ServiceUrl'
        Write-GameAgentStatus "Frontend deployed: https://$frontendUrl" -Type Success
        Write-Host ''

        # Step 7b: App observability
        Write-GameAgentStatus 'Step 7b: Setting log retention on auto-created log groups...' -Type Info
        Invoke-GameAgentAppObservability -RuntimeId $runtimeId -Region $Region -ProfileArgs $profileArgs -ProjectName $ProjectName
        Write-Host ''

        # Step 8: Security infrastructure
        Write-GameAgentStatus 'Step 8: Deploying security infrastructure...' -Type Info
        $frontendAlbArn = Get-StackOutput "$ProjectName-frontend" 'LoadBalancerArn'
        Write-Host "   ALB ARN: $frontendAlbArn"

        Deploy-Stack -StackName "$ProjectName-security" `
            -TemplateFile (Join-Path $infraPath '05-security-infrastructure.yaml') `
            -Params @("ProjectName=$ProjectName", "FrontendResourceArn=$frontendAlbArn", "RateLimitPerIP=2000", "AuthAdminRateLimitPerIP=100", "CloudTrailRetentionDays=90", "AIChatMode=true")

        # Enable Inspector
        Write-GameAgentStatus 'Step 8b: Enabling AWS Inspector for ECR scanning...' -Type Info
        try { Invoke-Aws inspector2 enable --resource-types ECR --region $Region | Out-Null }
        catch { Write-GameAgentStatus 'Inspector could not be enabled (may not be available)' -Type Warning }

        Write-GameAgentStatus 'Security infrastructure deployed' -Type Success
        Write-Host ''
    } else {
        Write-GameAgentStatus 'Steps 6-8: Skipped (Docker not available)' -Type Warning
        Write-Host ''
    }

    # ── Summary ──
    Write-Host ('=' * 60) -ForegroundColor Green
    if ($dockerAvailable) {
        Write-GameAgentStatus 'Deployment Complete!' -Type Success
    } else {
        Write-GameAgentStatus 'Deployment Partially Complete (backend only)' -Type Warning
    }
    Write-Host ('=' * 60) -ForegroundColor Green
    Write-Host ''

    if ($frontendUrl) { Write-Host "Frontend: https://$frontendUrl" -ForegroundColor Green; Write-Host '' }

    Write-Host 'Infrastructure IDs:' -ForegroundColor Cyan
    Write-Host "   Runtime ID:   $runtimeId"
    Write-Host "   Guardrail ID: $guardrailId"
    Write-Host "   GameLift KB:  $gameliftKbId"
    Write-Host "   EKS KB:       $eksKbId"
    Write-Host "   Cost KB:      $costKbId"
    Write-Host ''

    if ($dockerAvailable) {
        Write-Host 'Next Steps:' -ForegroundColor Cyan
        Write-Host "   1. Create admin user: Add-GameAgentAdmin -Profile $($Profile ?? 'default')"
        Write-Host "   2. Access frontend: https://$frontendUrl"
    } else {
        Write-Host 'Frontend was not deployed — Docker is required for Steps 6-8.' -ForegroundColor Yellow
    }
    Write-Host ''
}
