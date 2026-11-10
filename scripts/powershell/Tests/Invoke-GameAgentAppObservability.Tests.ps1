BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentAppObservability.ps1' | Resolve-Path)

    $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
    function Write-GameAgentStatus {
        param([string]$Message, [string]$Type)
        $script:StatusMessages.Add("$Type`: $Message")
    }

    # Capture Write-Host output so a spec can assert the reported outcome.
    $script:HostMessages = [System.Collections.Generic.List[string]]::new()
    function Write-Host {
        param(
            [Parameter(ValueFromRemainingArguments = $true, Position = 0)]$Object,
            [switch]$NoNewline,
            $ForegroundColor,
            $BackgroundColor,
            $Separator
        )
        $script:HostMessages.Add(($Object -join ' '))
    }

    $script:FrontendGroup = '/aws/ecs/default/game-agent-frontend-a1b2'
    $script:TaskDefinition = 'arn:aws:ecs:us-west-2:123456789012:task-definition/default-game-agent-frontend:7'

    # Builds an `aws` mock over synthetic state. It answers only the queries the
    # function is expected to send, rejects the unknown --log-group-name-exact
    # option the way the AWS CLI does (exit 252), records every
    # put-retention-policy call as '<log group>=<days>', and records any other
    # call in $Unexpected so a changed query fails the spec.
    function Set-AppObsAwsMock {
        param(
            # Service name -> PRIMARY task definition ARN (services in the 'default' cluster)
            [hashtable]$Services = @{},
            # Task definition ARN -> awslogs-group option
            [hashtable]$TaskDefinitions = @{},
            # Log group name -> retention in days ($null when the group has none)
            [hashtable]$LogGroups = @{},
            [bool]$EcsUnreadable = $false,
            [bool]$PutFails = $false,
            [System.Collections.Generic.List[string]]$Puts,
            [System.Collections.Generic.List[string]]$Unexpected
        )
        $servicesLocal = $Services
        $taskDefinitionsLocal = $TaskDefinitions
        $logGroupsLocal = $LogGroups
        $ecsUnreadableLocal = $EcsUnreadable
        $putFailsLocal = $PutFails
        $putsLocal = $Puts
        $unexpectedLocal = $Unexpected

        $mock = {
            $argList = @($args | ForEach-Object { "$_" })
            $op = "$($argList[0]) $($argList[1])"
            $opt = @{}
            for ($i = 2; $i -lt $argList.Count - 1; $i++) {
                if ($argList[$i].StartsWith('--')) { $opt[$argList[$i]] = $argList[$i + 1] }
            }
            $query = $opt['--query']
            $global:LASTEXITCODE = 0

            switch ($op) {
                'ecs describe-services' {
                    if ($ecsUnreadableLocal) { $global:LASTEXITCODE = 254; return }
                    if ($query -ne "services[?status=='ACTIVE'] | [0].deployments[?status=='PRIMARY'] | [0].taskDefinition") { break }
                    if ($opt['--cluster'] -eq 'default' -and $servicesLocal.ContainsKey($opt['--services'])) {
                        return $servicesLocal[$opt['--services']]
                    }
                    return 'None'
                }
                'ecs describe-task-definition' {
                    if ($ecsUnreadableLocal) { $global:LASTEXITCODE = 254; return }
                    if ($query -ne "taskDefinition.containerDefinitions[?logConfiguration.logDriver=='awslogs'] | [0].logConfiguration.options" -or
                        $opt['--output'] -ne 'json') { break }
                    if (-not $taskDefinitionsLocal.ContainsKey($opt['--task-definition'])) { $global:LASTEXITCODE = 254; return }
                    return ([ordered]@{
                            'awslogs-group'         = $taskDefinitionsLocal[$opt['--task-definition']]
                            'awslogs-region'        = 'us-west-2'
                            'awslogs-stream-prefix' = 'ecs'
                        } | ConvertTo-Json)
                }
                'logs describe-log-groups' {
                    if ($argList -contains '--log-group-name-exact') { $global:LASTEXITCODE = 252; return }
                    $prefix = "$($opt['--log-group-name-prefix'])"
                    $names = @($logGroupsLocal.Keys | Where-Object { $_.StartsWith($prefix) } | Sort-Object)
                    if ($query -eq 'logGroups[?!retentionInDays].logGroupName') {
                        return (ConvertTo-Json -InputObject @($names | Where-Object { -not $logGroupsLocal[$_] }))
                    }
                    if ($query -eq "logGroups[?logGroupName=='$prefix']" -and $opt['--output'] -eq 'json') {
                        $rows = @($names | Where-Object { $_ -eq $prefix } | ForEach-Object {
                                $row = [ordered]@{ logGroupName = $_ }
                                if ($logGroupsLocal[$_]) { $row.retentionInDays = $logGroupsLocal[$_] }
                                $row
                            })
                        return (ConvertTo-Json -InputObject $rows -Depth 3)
                    }
                }
                'logs put-retention-policy' {
                    $putsLocal.Add("$($opt['--log-group-name'])=$($opt['--retention-in-days'])")
                    if ($putFailsLocal) { $global:LASTEXITCODE = 254 }
                    return
                }
            }
            $unexpectedLocal.Add($argList -join ' ')
            $global:LASTEXITCODE = 2
        }.GetNewClosure()
        Set-Item -Path function:global:aws -Value $mock
    }

    function Invoke-AppObservability {
        param([string]$ProjectName = 'game-agent')
        Invoke-GameAgentAppObservability -RuntimeId 'gameagentruntime-EXAMPLE' -Region 'us-west-2' -ProfileArgs @() -ProjectName $ProjectName
    }
}

Describe 'Invoke-GameAgentAppObservability frontend log group' {
    BeforeEach {
        $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
        $script:HostMessages = [System.Collections.Generic.List[string]]::new()
        $script:Puts = [System.Collections.Generic.List[string]]::new()
        $script:Unexpected = [System.Collections.Generic.List[string]]::new()
        $global:LASTEXITCODE = 0
    }
    AfterEach {
        Remove-Item -Path function:aws -ErrorAction SilentlyContinue
    }

    It 'Sets retention on the log group the task definition names' {
        Set-AppObsAwsMock -Services @{ 'game-agent-frontend' = $script:TaskDefinition } `
            -TaskDefinitions @{ $script:TaskDefinition = $script:FrontendGroup } `
            -LogGroups @{ $script:FrontendGroup = $null } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        Invoke-AppObservability

        ($script:Puts -join ',') | Should -Be "$($script:FrontendGroup)=14"
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Uses the task definition log group even outside the ECS Express prefix' {
        $customGroup = '/example/game-agent/frontend'
        Set-AppObsAwsMock -Services @{ 'game-agent-frontend' = $script:TaskDefinition } `
            -TaskDefinitions @{ $script:TaskDefinition = $customGroup } `
            -LogGroups @{ $customGroup = $null; $script:FrontendGroup = $null } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        Invoke-AppObservability

        ($script:Puts -join ',') | Should -Be "$customGroup=14"
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Falls back to the ECS Express name prefix when the service cannot be read' {
        Set-AppObsAwsMock -EcsUnreadable $true `
            -LogGroups @{ $script:FrontendGroup = $null; '/aws/ecs/default/other-app-frontend-c3d4' = $null } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        Invoke-AppObservability

        ($script:Puts -join ',') | Should -Be "$($script:FrontendGroup)=14"
        ($script:HostMessages -join "`n") | Should -BeLike '*checking /aws/ecs/default/game-agent-frontend`**'
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Keeps an existing retention policy' {
        Set-AppObsAwsMock -Services @{ 'game-agent-frontend' = $script:TaskDefinition } `
            -TaskDefinitions @{ $script:TaskDefinition = $script:FrontendGroup } `
            -LogGroups @{ $script:FrontendGroup = 30 } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        Invoke-AppObservability

        $script:Puts | Should -BeNullOrEmpty
        ($script:HostMessages -join "`n") | Should -BeLike "*$($script:FrontendGroup) already has 30-day retention*"
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Reports and leaves alone an absent log group: <Case>' -TestCases @(
        @{ Case = 'no frontend service'; WithService = $false; Expected = '*checking /aws/ecs/default/game-agent-frontend`**' }
        @{ Case = 'service without its log group'; WithService = $true; Expected = '*log group not found: /aws/ecs/default/game-agent-frontend-a1b2*' }
    ) {
        param($Case, $WithService, $Expected)
        $services = if ($WithService) { @{ 'game-agent-frontend' = $script:TaskDefinition } } else { @{} }
        Set-AppObsAwsMock -Services $services `
            -TaskDefinitions @{ $script:TaskDefinition = $script:FrontendGroup } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        { Invoke-AppObservability } | Should -Not -Throw

        $script:Puts | Should -BeNullOrEmpty
        ($script:HostMessages -join "`n") | Should -BeLike $Expected
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Uses the project name to select the frontend service' {
        $otherTaskDefinition = $script:TaskDefinition.Replace('game-agent', 'example-game')
        $otherGroup = '/aws/ecs/default/example-game-frontend-e5f6'
        Set-AppObsAwsMock -Services @{ 'game-agent-frontend' = $script:TaskDefinition; 'example-game-frontend' = $otherTaskDefinition } `
            -TaskDefinitions @{ $script:TaskDefinition = $script:FrontendGroup; $otherTaskDefinition = $otherGroup } `
            -LogGroups @{ $script:FrontendGroup = $null; $otherGroup = $null } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        Invoke-AppObservability -ProjectName 'example-game'

        ($script:Puts -join ',') | Should -Be "$otherGroup=14"
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Ignores a task definition log group name outside the CloudWatch Logs character set' {
        $invalidGroup = "$($script:FrontendGroup)`"; touch `"example"
        Set-AppObsAwsMock -Services @{ 'game-agent-frontend' = $script:TaskDefinition } `
            -TaskDefinitions @{ $script:TaskDefinition = $invalidGroup } `
            -LogGroups @{ $script:FrontendGroup = $null } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        Invoke-AppObservability

        ($script:Puts -join ',') | Should -Be "$($script:FrontendGroup)=14"
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Stops when the retention update fails, as the shell script does' {
        Set-AppObsAwsMock -Services @{ 'game-agent-frontend' = $script:TaskDefinition } `
            -TaskDefinitions @{ $script:TaskDefinition = $script:FrontendGroup } `
            -LogGroups @{ $script:FrontendGroup = $null } -PutFails $true `
            -Puts $script:Puts -Unexpected $script:Unexpected

        { Invoke-AppObservability } | Should -Throw "*Could not set 14-day retention on $($script:FrontendGroup)*"
        $script:Unexpected | Should -BeNullOrEmpty
    }
}

Describe 'Invoke-GameAgentAppObservability runtime log groups' {
    BeforeAll {
        $script:RuntimeGroup = '/aws/bedrock-agentcore/runtimes/gameagentruntime-EXAMPLE-DEFAULT'
        $script:AdotGroup = '/aws/bedrock-agentcore/runtimes/gameagentruntime-EXAMPLE/adot-rt-logs'
    }
    BeforeEach {
        $script:StatusMessages = [System.Collections.Generic.List[string]]::new()
        $script:HostMessages = [System.Collections.Generic.List[string]]::new()
        $script:Puts = [System.Collections.Generic.List[string]]::new()
        $script:Unexpected = [System.Collections.Generic.List[string]]::new()
        $global:LASTEXITCODE = 0
    }
    AfterEach {
        Remove-Item -Path function:aws -ErrorAction SilentlyContinue
    }

    It 'Sets retention on runtime and ADOT log groups that have none' {
        Set-AppObsAwsMock -LogGroups @{ $script:RuntimeGroup = $null; $script:AdotGroup = $null } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        Invoke-AppObservability

        ($script:Puts -join ',') | Should -Be "$($script:RuntimeGroup)=14,$($script:AdotGroup)=14"
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Keeps an existing runtime retention policy, longer or shorter' -TestCases @(
        @{ Days = 1 }
        @{ Days = 3653 }
    ) {
        param($Days)
        Set-AppObsAwsMock -LogGroups @{ $script:RuntimeGroup = $Days; $script:AdotGroup = $Days } `
            -Puts $script:Puts -Unexpected $script:Unexpected

        Invoke-AppObservability

        $script:Puts | Should -BeNullOrEmpty
        ($script:HostMessages -join "`n") | Should -BeLike "*$($script:RuntimeGroup) already has $Days-day retention*"
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Stops when a runtime retention update fails, as the shell script does' {
        Set-AppObsAwsMock -LogGroups @{ $script:RuntimeGroup = $null } -PutFails $true `
            -Puts $script:Puts -Unexpected $script:Unexpected

        { Invoke-AppObservability } | Should -Throw "*Could not set 14-day retention on $($script:RuntimeGroup)*"
        $script:Unexpected | Should -BeNullOrEmpty
    }

    It 'Refuses a runtime ID outside the AgentCore character set before any call' {
        Set-AppObsAwsMock -Puts $script:Puts -Unexpected $script:Unexpected

        { Invoke-GameAgentAppObservability -RuntimeId "bad'] | [*].logGroupName | ['x" -Region 'us-west-2' -ProfileArgs @() } |
            Should -Throw '*Invalid runtime ID*'
        $script:Puts | Should -BeNullOrEmpty
        $script:Unexpected | Should -BeNullOrEmpty
    }
}
