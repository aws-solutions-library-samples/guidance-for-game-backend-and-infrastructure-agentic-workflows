function Invoke-GameAgentKBTeardown {
    <# Tears down all 3 Knowledge Base stacks: empties buckets, deletes stacks, cleans .env.local. #>
    [CmdletBinding()]
    param([string]$Region, [string[]]$ProfileArgs, [string]$ProjectName = 'game-agent')

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $envFile = Join-Path $repoRoot 'backend/.env.local'

    function Remove-KBStack {
        param([string]$KBName)
        $stackName = "$ProjectName-kb-$KBName"

        & aws cloudformation describe-stacks --stack-name $stackName --region $Region @ProfileArgs 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-GameAgentStatus "Stack $stackName does not exist, skipping" -Type Warning
            return
        }

        # Empty document bucket
        $bucket = (& aws cloudformation describe-stacks --stack-name $stackName --region $Region @ProfileArgs `
            --query 'Stacks[0].Outputs[?OutputKey==`DocumentBucketName`].OutputValue' --output text 2>$null) | Out-String
        $bucket = $bucket.Trim()
        if ($bucket) {
            & aws s3api head-bucket --bucket $bucket --region $Region @ProfileArgs 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-GameAgentStatus "Emptying $KBName document bucket..." -Type Info
                & aws s3 rm "s3://$bucket" --recursive --region $Region @ProfileArgs 2>$null | Out-Null
            }
        }

        Write-GameAgentStatus "Deleting $KBName KB stack..." -Type Info
        & aws cloudformation delete-stack --stack-name $stackName --region $Region @ProfileArgs
        & aws cloudformation wait stack-delete-complete --stack-name $stackName --region $Region @ProfileArgs
        Write-GameAgentStatus "$KBName KB torn down" -Type Success
    }

    foreach ($kb in @('gamelift', 'eks', 'cost')) { Remove-KBStack $kb }

    # Clean .env.local
    if (Test-Path $envFile) {
        $content = Get-Content $envFile | Where-Object { $_ -notmatch '^(GAMELIFT|EKS|COST|KNOWLEDGE_BASE)_KB_ID=' }
        Set-Content $envFile $content
    }
    Write-GameAgentStatus 'All Knowledge Bases torn down' -Type Success
}
