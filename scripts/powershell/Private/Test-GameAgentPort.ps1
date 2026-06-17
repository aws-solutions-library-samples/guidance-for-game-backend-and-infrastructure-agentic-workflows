function Test-GameAgentPort {
    <#
    .SYNOPSIS
        Tests if a TCP port is listening. Cross-platform.
    .PARAMETER Port
        The port number to test.
    .EXAMPLE
        Test-GameAgentPort -Port 8080
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)]
        [int]$Port
    )

    $client = $null
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $client.ConnectAsync('127.0.0.1', $Port).Wait(1000) | Out-Null
        return $client.Connected
    } catch {
        return $false
    } finally {
        if ($client) { $client.Dispose() }
    }
}
