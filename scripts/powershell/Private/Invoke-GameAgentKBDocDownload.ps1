function Invoke-GameAgentKBDocDownload {
    <# Downloads KB documentation using the Python scraper. #>
    [CmdletBinding()]
    param([string]$Region, [string[]]$ProfileArgs)

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $backendPath = Join-Path $repoRoot 'backend'
    $scraperScript = Join-Path $repoRoot 'scripts/infrastructure/scrape_aws_docs.py'

    Write-GameAgentStatus 'Downloading KB documentation...' -Type Info
    Push-Location $backendPath
    try {
        uv sync 2>$null
        if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit code $LASTEXITCODE)" }
        $venvPython = if ($IsWindows) { '.venv/Scripts/python.exe' } else { '.venv/bin/python' }
        & $venvPython $scraperScript
        if ($LASTEXITCODE -ne 0) { throw 'KB docs download failed' }
    } finally { Pop-Location }
    Write-GameAgentStatus 'Documentation downloaded' -Type Success
}
