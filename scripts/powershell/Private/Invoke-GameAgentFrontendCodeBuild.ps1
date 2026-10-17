function Invoke-GameAgentFrontendCodeBuild {
    <#
    Builds and pushes the frontend image in the base stack's amd64 CodeBuild
    project, for deploy hosts without Docker. Matches
    scripts/infrastructure/build-frontend-codebuild.sh: only files git would
    track are uploaded, so ui/.env.local, node_modules, and .next never leave the host.
    #>
    [CmdletBinding()]
    param(
        [string]$Region,
        [string[]]$ProfileArgs,
        [string]$ProjectName = 'game-agent',
        [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_.-]{1,128}$')][string]$ImageTag,
        [int]$TimeoutSeconds = 2400
    )

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $uiPath = Join-Path $repoRoot 'ui'

    function Get-BaseOutput([string]$Key) {
        $value = (& aws cloudformation describe-stacks --stack-name "$ProjectName-infrastructure" --region $Region @ProfileArgs `
            --query "Stacks[0].Outputs[?OutputKey==``$Key``].OutputValue" --output text) | Out-String
        return $value.Trim()
    }

    $project = Get-BaseOutput 'FrontendBuildProjectName'
    $bucket = Get-BaseOutput 'FrontendBuildSourceBucketName'
    if (-not $project -or $project -eq 'None' -or -not $bucket -or $bucket -eq 'None') {
        throw 'Base stack does not export the frontend CodeBuild project. Redeploy the base infrastructure stack.'
    }

    # Package the files git would track (tracked plus untracked, not ignored).
    $files = & git -C $uiPath ls-files --cached --others --exclude-standard
    if ($LASTEXITCODE -ne 0 -or -not $files) { throw 'git is required to package the frontend source for CodeBuild.' }
    $files = $files | Where-Object { Test-Path -LiteralPath (Join-Path $uiPath $_) -PathType Leaf }
    $blocked = $files | Where-Object { $_ -match '(^|/)\.env(\.[^/]*)?\.local$' -or $_ -match '^node_modules/' }
    if ($blocked) { throw 'Refusing to upload: the source archive would contain local environment files or node_modules.' }

    $archive = Join-Path ([System.IO.Path]::GetTempPath()) "frontend-source-$ImageTag.zip"
    if (Test-Path $archive) { Remove-Item $archive -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::Open($archive, 'Create')
    try {
        foreach ($file in $files) {
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (Join-Path $uiPath $file), $file) | Out-Null
        }
    } finally { $zip.Dispose() }

    $key = "frontend/$ImageTag.zip"
    Write-Host "   Uploading source to s3://$bucket/$key"
    & aws s3 cp $archive "s3://$bucket/$key" --region $Region --only-show-errors @ProfileArgs
    $uploadExit = $LASTEXITCODE
    Remove-Item $archive -Force -ErrorAction SilentlyContinue
    if ($uploadExit -ne 0) { throw 'Failed to upload the frontend source archive.' }

    $buildId = ((& aws codebuild start-build --project-name $project `
        --source-location-override "$bucket/$key" `
        --environment-variables-override "name=IMAGE_TAG,value=$ImageTag,type=PLAINTEXT" `
        --region $Region @ProfileArgs --query 'build.id' --output text) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $buildId) { throw 'Failed to start the frontend CodeBuild build.' }
    Write-Host "   CodeBuild build started: $buildId"

    $waited = 0
    while ($true) {
        $state = ((& aws codebuild batch-get-builds --ids $buildId --region $Region @ProfileArgs `
            --query 'builds[0].[buildStatus,currentPhase]' --output text) | Out-String).Trim() -split '\s+'
        if ($LASTEXITCODE -ne 0) { throw "Could not read the status of CodeBuild build $buildId." }
        if ($state[0] -ne 'IN_PROGRESS') { break }
        if ($waited -ge $TimeoutSeconds) { throw "CodeBuild build $buildId did not finish within ${TimeoutSeconds}s." }
        Write-Host "   ... $($state[1]) (${waited}s)"
        Start-Sleep -Seconds 15; $waited += 15
    }
    if ($state[0] -ne 'SUCCEEDED') {
        throw "CodeBuild build $buildId finished with status $($state[0]). Logs: /aws/codebuild/$project"
    }
    Write-GameAgentStatus 'CodeBuild build succeeded' -Type Success
}
