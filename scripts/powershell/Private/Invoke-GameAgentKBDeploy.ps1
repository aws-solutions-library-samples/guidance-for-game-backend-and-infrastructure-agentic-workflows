function Invoke-GameAgentKBDeploy {
    <# Deploys all 3 Knowledge Base CFN stacks and writes KB IDs to .env.local. #>
    [CmdletBinding()]
    param([string]$Region, [string[]]$ProfileArgs, [string]$ProjectName = 'game-agent')

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $infraPath = Join-Path $repoRoot 'infrastructure/cloudformation'
    $envFile = Join-Path $repoRoot 'backend/.env.local'

    # Helper: check for orphaned stack (bucket deleted but stack exists)
    function Repair-OrphanedStack {
        param([string]$StackName)
        & aws cloudformation describe-stacks --stack-name $StackName --region $Region @ProfileArgs 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { return }

        $bucket = (& aws cloudformation describe-stacks --stack-name $StackName --region $Region @ProfileArgs `
            --query 'Stacks[0].Outputs[?OutputKey==`DocumentBucketName`].OutputValue' --output text 2>$null) | Out-String
        $bucket = $bucket.Trim()
        if (-not $bucket) { return }

        & aws s3api head-bucket --bucket $bucket --region $Region @ProfileArgs 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-GameAgentStatus "Orphaned stack detected: $StackName — cleaning up..." -Type Warning
            & aws cloudformation delete-stack --stack-name $StackName --region $Region @ProfileArgs
            & aws cloudformation wait stack-delete-complete --stack-name $StackName --region $Region @ProfileArgs
        } else {
            $status = (& aws cloudformation describe-stacks --stack-name $StackName --region $Region @ProfileArgs `
                --query 'Stacks[0].StackStatus' --output text 2>$null) | Out-String
            if ($status.Trim() -match 'FAILED|ROLLBACK') {
                Write-GameAgentStatus "Stack $StackName in bad state ($($status.Trim())) — cleaning up..." -Type Warning
                & aws s3 rm "s3://$bucket" --recursive --region $Region @ProfileArgs 2>$null | Out-Null
                & aws cloudformation delete-stack --stack-name $StackName --region $Region @ProfileArgs
                & aws cloudformation wait stack-delete-complete --stack-name $StackName --region $Region @ProfileArgs
            }
        }
    }

    Write-GameAgentStatus 'Checking for orphaned stacks...' -Type Info
    foreach ($kb in @('gamelift', 'eks', 'cost')) { Repair-OrphanedStack "$ProjectName-kb-$kb" }

    $kbs = @(
        @{ Name = 'gamelift'; Emoji = '🎮'; Template = 'knowledge-base-gamelift.yaml' }
        @{ Name = 'eks';      Emoji = '☸️';  Template = 'knowledge-base-eks.yaml' }
        @{ Name = 'cost';     Emoji = '💰'; Template = 'knowledge-base-cost.yaml' }
    )

    $kbIds = @{}
    foreach ($kb in $kbs) {
        $stackName = "$ProjectName-kb-$($kb.Name)"
        Write-GameAgentStatus "$($kb.Emoji) Deploying $($kb.Name) Knowledge Base..." -Type Info
        $deployArgs = @('cloudformation', 'deploy',
            '--template-file', (Join-Path $infraPath $kb.Template),
            '--stack-name', $stackName,
            '--parameter-overrides', "ProjectName=$ProjectName",
            '--capabilities', 'CAPABILITY_NAMED_IAM',
            '--no-fail-on-empty-changeset',
            '--region', $Region) + $ProfileArgs
        & aws @deployArgs 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to deploy $stackName" }

        $kbId = (& aws cloudformation describe-stacks --stack-name $stackName --region $Region @ProfileArgs `
            --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseId`].OutputValue' --output text) | Out-String
        $kbIds[$kb.Name] = $kbId.Trim()
        Write-GameAgentStatus "$($kb.Name) KB deployed: $($kbIds[$kb.Name])" -Type Success
    }

    # Write KB IDs to .env.local
    if (Test-Path $envFile) {
        $content = Get-Content $envFile | Where-Object { $_ -notmatch '^(GBAW_)?(GAMELIFT|EKS|COST|KNOWLEDGE_BASE)_KB_ID=' }
        Set-Content $envFile $content
    }
    Add-Content $envFile "GBAW_GAMELIFT_KB_ID=$($kbIds['gamelift'])"
    Add-Content $envFile "GBAW_EKS_KB_ID=$($kbIds['eks'])"
    Add-Content $envFile "GBAW_COST_KB_ID=$($kbIds['cost'])"
    Write-GameAgentStatus 'Updated backend/.env.local with KB IDs' -Type Success

    return $kbIds
}
