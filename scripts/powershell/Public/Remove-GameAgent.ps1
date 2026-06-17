function Remove-GameAgent {
    <#
    .SYNOPSIS
        Removes all Game Agent infrastructure from AWS.
    .DESCRIPTION
        Full teardown matching teardown.sh: security (with bucket cleanup),
        observability, KBs, prompts, frontend, guardrails, AgentCore Runtime + Memory,
        orphaned resources, base infrastructure, access logs bucket, KB docs.
    .PARAMETER ProjectName
        Project name used during deployment. Default: "game-agent".
    .PARAMETER Region
        AWS region. Default: "us-west-2".
    .PARAMETER Profile
        AWS CLI profile. Default: reads from ui/.env.local or uses "default".
    .PARAMETER Force
        Skip confirmation prompt (equivalent to bash --yes flag).
    .EXAMPLE
        Remove-GameAgent -Force
    .EXAMPLE
        Remove-GameAgent -Profile demo
    #>
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
    param(
        [string]$ProjectName = 'game-agent',
        [string]$Region = 'us-west-2',
        [string]$Profile,
        [switch]$Force
    )

    $ErrorActionPreference = 'Stop'
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $backendPath = Join-Path $repoRoot 'backend'
    $uiPath = Join-Path $repoRoot 'ui'

    # Resolve profile
    $resolved = Resolve-GameAgentProfile -Profile $Profile
    $Profile = $resolved.Profile
    $profileArgs = $resolved.ProfileArgs

    function Invoke-Aws {
        param([Parameter(ValueFromRemainingArguments)][string[]]$AwsArgs)
        $allArgs = $AwsArgs + $profileArgs
        $result = & aws @allArgs 2>&1
        if ($LASTEXITCODE -ne 0) { throw "aws $($allArgs -join ' ') failed: $result" }
        return $result
    }

    # Helper: delete a CFN stack with wait
    function Remove-Stack {
        param([string]$StackName)
        $exists = & aws cloudformation describe-stacks --stack-name $StackName --region $Region @profileArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-GameAgentStatus "Stack not found (already deleted): $StackName" -Type Info
            return
        }
        Write-GameAgentStatus "Deleting stack: $StackName..." -Type Warning
        Invoke-Aws cloudformation delete-stack --stack-name $StackName --region $Region
        Write-GameAgentStatus "Waiting for stack deletion: $StackName..." -Type Info
        Invoke-Aws cloudformation wait stack-delete-complete --stack-name $StackName --region $Region
        Write-GameAgentStatus "Stack deleted: $StackName" -Type Success
    }

    # Helper: check if stack exists (returns bool)
    function Test-Stack {
        param([string]$StackName)
        & aws cloudformation describe-stacks --stack-name $StackName --region $Region @profileArgs 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }

    Write-Host ''
    Write-Host ('=' * 60) -ForegroundColor Red
    Write-GameAgentStatus 'WARNING: This will delete ALL Game Agent resources!' -Type Warning
    Write-Host ('=' * 60) -ForegroundColor Red
    Write-Host ''

    if ($Force) { $ConfirmPreference = 'None' }
    if (-not $PSCmdlet.ShouldProcess('all GameAgent AWS resources', 'Remove')) {
        Write-GameAgentStatus 'Teardown cancelled' -Type Warning
        return
    }

    # Verify credentials
    $accountId = ((Invoke-Aws sts get-caller-identity --region $Region --query Account --output text) | Out-String).Trim()
    Write-GameAgentStatus "Tearing down from AWS Account: $accountId" -Type Info
    Write-Host "Region: $Region"
    Write-Host ''

    # ── Step 0: Security infrastructure + CloudTrail bucket ──
    Write-GameAgentStatus 'Step 0: Deleting security infrastructure (WAF + CloudTrail)...' -Type Info
    if (Test-Stack "$ProjectName-security") {
        # Stop CloudTrail logging
        try { Invoke-Aws cloudtrail stop-logging --name "$ProjectName-trail" --region $Region | Out-Null } catch {}

        # Empty CloudTrail logs bucket (versioned)
        $ctBucket = "$ProjectName-cloudtrail-logs-$accountId-$Region"
        $bucketExists = & aws s3api head-bucket --bucket $ctBucket @profileArgs 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-GameAgentStatus "Emptying CloudTrail logs bucket (versioned)..." -Type Info
            while ($true) {
                $versions = (Invoke-Aws s3api list-object-versions --bucket $ctBucket --max-keys 1000 --output json --region $Region) | ConvertFrom-Json
                $objects = @()
                if ($versions.Versions) { $objects += $versions.Versions | ForEach-Object { @{ Key = $_.Key; VersionId = $_.VersionId } } }
                if ($versions.DeleteMarkers) { $objects += $versions.DeleteMarkers | ForEach-Object { @{ Key = $_.Key; VersionId = $_.VersionId } } }
                if ($objects.Count -eq 0) { Write-Host '   Bucket emptied'; break }
                Write-Host "   Deleting $($objects.Count) object versions..."
                $deleteJson = @{ Objects = $objects } | ConvertTo-Json -Compress -Depth 5
                $tmpFile = [System.IO.Path]::GetTempFileName()
                Set-Content $tmpFile $deleteJson
                Invoke-Aws s3api delete-objects --bucket $ctBucket --delete "file://$tmpFile" --region $Region | Out-Null
                Remove-Item $tmpFile -ErrorAction SilentlyContinue
                if (-not $versions.IsTruncated) { break }
            }
        }
        Remove-Stack "$ProjectName-security"
    } else {
        Write-GameAgentStatus 'Security stack not found' -Type Info
    }
    Write-Host ''

    # ── Step 1: Observability ──
    Write-GameAgentStatus 'Step 1: Deleting observability stack...' -Type Info
    Remove-Stack "$ProjectName-observability"
    Write-Host ''

    # ── Step 1.5: Knowledge Bases ──
    Write-GameAgentStatus 'Step 1.5: Deleting Knowledge Bases...' -Type Info
    Invoke-GameAgentKBTeardown -Region $Region -ProfileArgs $profileArgs -ProjectName $ProjectName
    Write-Host ''

    # ── Step 1.6: Managed Prompts ──
    Write-GameAgentStatus 'Step 1.6: Deleting Bedrock Managed Prompts...' -Type Info
    Invoke-GameAgentPromptTeardown -Region $Region -ProfileArgs $profileArgs
    Write-Host ''

    # ── Step 2: Frontend ──
    Write-GameAgentStatus 'Step 2: Deleting frontend...' -Type Info
    Remove-Stack "$ProjectName-frontend"
    Write-Host ''

    # ── Step 3.5: Guardrails ──
    Write-GameAgentStatus 'Step 3.5: Deleting Bedrock Guardrails...' -Type Info
    Remove-Stack "$ProjectName-guardrails"
    Write-Host ''

    # ── Step 4: AgentCore Runtime + Memory ──
    Write-GameAgentStatus 'Step 4: Deleting AgentCore Runtime and Memory...' -Type Info
    if ($Profile) { $env:AWS_PROFILE = $Profile }
    $env:AWS_REGION = $Region
    Push-Location $backendPath
    try {
        $agentcoreConfig = Join-Path $backendPath '.bedrock_agentcore.yaml'
        if (Test-Path $agentcoreConfig) {
            $runtimeArn = ((yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml 2>$null) | Out-String).Trim()
            $runtimeId = if ($runtimeArn -and $runtimeArn -ne 'null') { $runtimeArn.Split('/')[-1] } else { '' }
            $memoryId = ((yq eval '.agents.gameagentruntime.memory.memory_id' .bedrock_agentcore.yaml 2>$null) | Out-String).Trim()

            # Delete Memory first
            if ($memoryId -and $memoryId -ne 'null') {
                Write-GameAgentStatus "Deleting AgentCore Memory: $memoryId" -Type Info
                try { Invoke-Aws bedrock-agentcore-control delete-memory --memory-id $memoryId --region $Region | Out-Null; Write-GameAgentStatus 'Memory deleted' -Type Success }
                catch { Write-GameAgentStatus 'Memory already deleted or not found' -Type Warning }
            }

            # Delete Runtime
            Write-GameAgentStatus "Deleting AgentCore Runtime: $runtimeId" -Type Info
            try { uv run agentcore destroy --force --delete-ecr-repo }
            catch { Write-GameAgentStatus 'Runtime already deleted or not found' -Type Warning }

            # Wait for runtime deletion
            if ($runtimeId -and $runtimeId -ne 'null') {
                Write-GameAgentStatus 'Waiting for AgentCore Runtime deletion...' -Type Info
                $maxWait = 300; $elapsed = 0
                while ($elapsed -lt $maxWait) {
                    & aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id $runtimeId --region $Region @profileArgs 2>$null | Out-Null
                    if ($LASTEXITCODE -ne 0) { Write-GameAgentStatus 'AgentCore Runtime deleted' -Type Success; break }
                    Write-Host "   Still deleting... (${elapsed}s elapsed)"
                    Start-Sleep -Seconds 10; $elapsed += 10
                }
                if ($elapsed -ge $maxWait) { Write-GameAgentStatus "Runtime deletion timeout after ${maxWait}s" -Type Warning }
            }

            Remove-Item $agentcoreConfig -ErrorAction SilentlyContinue
        } else {
            Write-GameAgentStatus 'No runtime configuration found' -Type Warning
        }

        # ── Step 4b: Orphaned resources ──
        Write-GameAgentStatus 'Checking for orphaned AgentCore resources...' -Type Info
        $orphanRuntimes = (& aws bedrock-agentcore-control list-agent-runtimes --region $Region @profileArgs `
            --query "agentRuntimes[?starts_with(agentRuntimeName, ``game-agent``)].agentRuntimeId" --output text 2>$null) | Out-String
        $orphanRuntimes = $orphanRuntimes.Trim()
        if ($orphanRuntimes) {
            foreach ($rid in ($orphanRuntimes -split '\s+')) {
                if ($rid) {
                    Write-GameAgentStatus "Deleting orphaned runtime: $rid" -Type Info
                    try { Invoke-Aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id $rid --region $Region | Out-Null }
                    catch { Write-GameAgentStatus "Could not delete runtime $rid" -Type Warning }
                }
            }
        }

        $orphanMemories = (& aws bedrock-agentcore-control list-memories --region $Region @profileArgs `
            --query "memories[?starts_with(id, ``game-agent``)].id" --output text 2>$null) | Out-String
        $orphanMemories = $orphanMemories.Trim()
        if ($orphanMemories) {
            foreach ($mid in ($orphanMemories -split '\s+')) {
                if ($mid) {
                    Write-GameAgentStatus "Deleting orphaned memory: $mid" -Type Info
                    try { Invoke-Aws bedrock-agentcore-control delete-memory --memory-id $mid --region $Region | Out-Null }
                    catch { Write-GameAgentStatus "Could not delete memory $mid" -Type Warning }
                }
            }
        } else {
            Write-GameAgentStatus 'No orphaned memories found' -Type Success
        }
    } finally { Pop-Location }
    Write-Host ''

    # ── Step 5: Base infrastructure + access logs bucket ──
    Write-GameAgentStatus 'Step 5: Deleting base infrastructure...' -Type Info

    # Delete access logs bucket (has DeletionPolicy: Retain)
    $accessLogsBucket = "$ProjectName-access-logs-$accountId-$Region"
    $bucketCheck = & aws s3api head-bucket --bucket $accessLogsBucket @profileArgs 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-GameAgentStatus "Deleting access logs bucket: $accessLogsBucket" -Type Info
        try { Invoke-Aws s3 rb "s3://$accessLogsBucket" --force --region $Region | Out-Null }
        catch { Write-GameAgentStatus 'Could not delete access logs bucket' -Type Warning }
    }

    Remove-Stack "$ProjectName-infrastructure"
    Write-Host ''

    # ── Step 6: KB docs cleanup ──
    Write-GameAgentStatus 'Step 6: Cleaning up downloaded documentation...' -Type Info
    $kbSourcesDir = Join-Path $repoRoot 'docs/kb-sources'
    if (Test-Path $kbSourcesDir) {
        Get-ChildItem -Path $kbSourcesDir -Recurse -Filter '*.md' | Remove-Item -Force -ErrorAction SilentlyContinue
        Write-GameAgentStatus 'Removed downloaded markdown files' -Type Success
    }
    $kbCache = Join-Path $repoRoot 'docs/.kb-cache'
    if (Test-Path $kbCache) {
        Remove-Item $kbCache -Recurse -Force -ErrorAction SilentlyContinue
        Write-GameAgentStatus 'Removed scraper cache' -Type Success
    }

    Write-Host ''
    Write-Host ('=' * 60) -ForegroundColor Green
    Write-GameAgentStatus 'Teardown completed successfully!' -Type Success
    Write-Host ('=' * 60) -ForegroundColor Green
    Write-Host ''
    Write-Host 'Account-wide resources preserved:' -ForegroundColor Cyan
    Write-Host '  - /aws/spans (CloudWatch Transaction Search log group)'
    Write-Host '  - CloudWatch Transaction Search configuration'
    Write-Host '  - X-Ray resource policies'
    Write-Host ''
}
