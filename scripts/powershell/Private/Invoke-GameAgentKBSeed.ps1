function Invoke-GameAgentKBSeed {
    <# Seeds a single Knowledge Base: uploads docs to S3, starts ingestion, waits. #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$KBName,  # gamelift, eks, or cost
        [string]$Region,
        [string[]]$ProfileArgs,
        [string]$ProjectName = 'game-agent',
        [int]$MaxWaitSeconds = 120
    )

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $stackName = "$ProjectName-kb-$KBName"

    # Check if stack exists
    & aws cloudformation describe-stacks --stack-name $stackName --region $Region @ProfileArgs 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-GameAgentStatus "Stack $stackName not found, skipping seeding" -Type Warning
        return
    }

    # Get stack outputs
    $kbId = (& aws cloudformation describe-stacks --stack-name $stackName --region $Region @ProfileArgs `
        --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseId`].OutputValue' --output text) | Out-String
    $kbId = $kbId.Trim()

    $dsRaw = (& aws cloudformation describe-stacks --stack-name $stackName --region $Region @ProfileArgs `
        --query 'Stacks[0].Outputs[?OutputKey==`DataSourceId`].OutputValue' --output text) | Out-String
    # DataSourceId output may contain pipe-separated values; take the last segment
    $dsId = ($dsRaw.Trim() -split '\|')[-1]

    $bucket = (& aws cloudformation describe-stacks --stack-name $stackName --region $Region @ProfileArgs `
        --query 'Stacks[0].Outputs[?OutputKey==`DocumentBucketName`].OutputValue' --output text) | Out-String
    $bucket = $bucket.Trim()

    if (-not $kbId -or -not $dsId -or -not $bucket) {
        throw "Failed to get stack outputs for $stackName (KB=$kbId, DS=$dsId, Bucket=$bucket)"
    }

    Write-Host "Knowledge Base ID: $kbId"
    Write-Host "Data Source ID: $dsId"
    Write-Host "Document Bucket: $bucket"

    # Check docs directory
    $docsDir = Join-Path $repoRoot "docs/kb-sources/$KBName"
    if (-not (Test-Path $docsDir)) { throw "Documentation directory not found: $docsDir. Run Invoke-GameAgentKBDocDownload first." }
    $mdFiles = Get-ChildItem -Path $docsDir -Filter '*.md' -ErrorAction SilentlyContinue
    if (-not $mdFiles) { throw "No markdown files in $docsDir. Run Invoke-GameAgentKBDocDownload first." }

    # Upload docs
    Write-GameAgentStatus "Uploading $KBName documentation..." -Type Info
    & aws s3 sync $docsDir "s3://$bucket/$KBName/" --region $Region --exclude '.*' @ProfileArgs
    if ($LASTEXITCODE -ne 0) { throw 'Failed to upload documents to S3' }

    # Start ingestion
    Write-GameAgentStatus 'Starting ingestion job...' -Type Info
    $jobResult = (& aws bedrock-agent start-ingestion-job --knowledge-base-id $kbId --data-source-id $dsId `
        --description "$KBName KB seeding" --region $Region @ProfileArgs `
        --query 'ingestionJob.ingestionJobId' --output text) | Out-String
    $jobId = $jobResult.Trim()
    if (-not $jobId) { throw 'Failed to start ingestion job' }
    Write-Host "Ingestion Job ID: $jobId"

    # Wait for ingestion
    $elapsed = 0
    while ($elapsed -lt $MaxWaitSeconds) {
        $status = (& aws bedrock-agent get-ingestion-job --knowledge-base-id $kbId --data-source-id $dsId `
            --ingestion-job-id $jobId --region $Region @ProfileArgs `
            --query 'ingestionJob.status' --output text 2>$null) | Out-String
        $status = $status.Trim()

        if ($status -eq 'COMPLETE') {
            Write-GameAgentStatus "$KBName KB ingestion complete!" -Type Success
            & aws bedrock-agent get-ingestion-job --knowledge-base-id $kbId --data-source-id $dsId `
                --ingestion-job-id $jobId --region $Region @ProfileArgs `
                --query 'ingestionJob.statistics' --output table 2>$null
            return
        } elseif ($status -eq 'FAILED') {
            throw "$KBName KB ingestion failed!"
        }
        Write-Host "Status: $status (${elapsed}s elapsed)"
        Start-Sleep 5; $elapsed += 5
    }
    Write-GameAgentStatus "Ingestion still in progress after ${MaxWaitSeconds}s" -Type Warning
}
