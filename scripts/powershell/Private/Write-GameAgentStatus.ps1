function Write-GameAgentStatus {
    <#
    .SYNOPSIS
        Writes formatted status messages with emojis and colors.

    .DESCRIPTION
        Internal helper function for consistent status messaging across all GameAgent cmdlets.

    .PARAMETER Message
        The message to display.

    .PARAMETER Type
        The type of message: Info, Success, Warning, or Error.

    .EXAMPLE
        Write-GameAgentStatus "Deployment started" -Type Info
        Write-GameAgentStatus "Stack created successfully" -Type Success
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Message,

        [Parameter()]
        [ValidateSet('Info', 'Success', 'Warning', 'Error')]
        [string]$Type = 'Info'
    )

    $emoji = switch ($Type) {
        'Success' { '✅' }
        'Error'   { '❌' }
        'Warning' { '⚠️' }
        default   { '🚀' }
    }

    $color = switch ($Type) {
        'Success' { 'Green' }
        'Error'   { 'Red' }
        'Warning' { 'Yellow' }
        default   { 'Cyan' }
    }

    Write-Host "$emoji $Message" -ForegroundColor $color
}
