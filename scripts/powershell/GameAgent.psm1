# Game Agent PowerShell Module
# Main module loader

$ErrorActionPreference = 'Stop'

# Get module root path
$ModuleRoot = $PSScriptRoot

# Import private functions
$PrivateFunctions = Get-ChildItem -Path "$ModuleRoot/Private/*.ps1" -ErrorAction SilentlyContinue
foreach ($Function in $PrivateFunctions) {
    . $Function.FullName
}

# Import public functions
$PublicFunctions = Get-ChildItem -Path "$ModuleRoot/Public/*.ps1" -ErrorAction SilentlyContinue
foreach ($Function in $PublicFunctions) {
    . $Function.FullName
}

# Export public functions
Export-ModuleMember -Function $PublicFunctions.BaseName

# Module initialization
Write-Verbose "Game Agent PowerShell Module loaded"
