BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'Invoke-GameAgentWafReconciliation.ps1' | Resolve-Path)

    function Write-GameAgentStatus {
        param([string]$Message, [string]$Type)
    }

    $RequiredRules = @(
        'RateLimitAuthPaths'
        'RateLimitAdminPaths'
        'RateLimitPerIP'
        'AWSManagedRulesCommonRuleSet'
        'AWSManagedRulesSQLiRuleSet'
        'AWSManagedRulesKnownBadInputsRuleSet'
    )

    function New-TestWafInvoker {
        param(
            [string[]]$ActiveSequence,
            [string[]]$AssociationSequence = @('SUCCESS'),
            [string[]]$RuleNames = $RequiredRules,
            [System.Collections.Generic.List[string]]$Calls
        )

        $activeQueue = [System.Collections.Generic.Queue[string]]::new()
        foreach ($value in $ActiveSequence) { $activeQueue.Enqueue($value) }
        $associationQueue = [System.Collections.Generic.Queue[string]]::new()
        foreach ($value in $AssociationSequence) { $associationQueue.Enqueue($value) }
        $lastActiveValue = $ActiveSequence[-1]
        $lastAssociationValue = $AssociationSequence[-1]

        return {
            param([string[]]$AwsArgs)
            $operation = "$($AwsArgs[0]) $($AwsArgs[1])"
            $Calls.Add($operation)

            switch ($operation) {
                'wafv2 get-web-acl-for-resource' {
                    $value = if ($activeQueue.Count -gt 0) { $activeQueue.Dequeue() } else { $lastActiveValue }
                    if ($value -eq 'TRANSIENT') { throw 'WAFUnavailableEntityException' }
                    return $value
                }
                'wafv2 associate-web-acl' {
                    $value = if ($associationQueue.Count -gt 0) { $associationQueue.Dequeue() } else { $lastAssociationValue }
                    if ($value -eq 'TRANSIENT') { throw 'WAFUnavailableEntityException' }
                    return ''
                }
                'wafv2 get-web-acl' {
                    return ($RuleNames -join "`t")
                }
                default { throw "Unexpected operation: $operation" }
            }
        }.GetNewClosure()
    }
}

Describe 'Invoke-GameAgentWafReconciliation' {
    It 'Skips association when the expected ACL and rules are active' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestWafInvoker -ActiveSequence @('expected/name/id') -Calls $calls

        Invoke-GameAgentWafReconciliation `
            -ExpectedWebAclArn 'expected/name/id' `
            -ResourceArn 'resource-arn' `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -RetrySeconds 0

        $calls | Should -Not -Contain 'wafv2 associate-web-acl'
        $calls | Should -Contain 'wafv2 get-web-acl'
    }

    It 'Retries transient association and lookup failures before converging' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestWafInvoker `
            -ActiveSequence @('platform/name/id', 'TRANSIENT', 'expected/name/id') `
            -AssociationSequence @('TRANSIENT', 'SUCCESS') `
            -Calls $calls

        Invoke-GameAgentWafReconciliation `
            -ExpectedWebAclArn 'expected/name/id' `
            -ResourceArn 'resource-arn' `
            -Region 'us-west-2' `
            -InvokeAws $invokeAws `
            -AssociationMaxAttempts 3 `
            -VerificationMaxAttempts 3 `
            -RetrySeconds 0

        @($calls | Where-Object { $_ -eq 'wafv2 associate-web-acl' }).Count | Should -Be 2
        $calls | Should -Contain 'wafv2 get-web-acl'
    }

    It 'Fails when transient association errors exhaust the retry budget' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestWafInvoker `
            -ActiveSequence @('platform/name/id') `
            -AssociationSequence @('TRANSIENT') `
            -Calls $calls

        {
            Invoke-GameAgentWafReconciliation `
                -ExpectedWebAclArn 'expected/name/id' `
                -ResourceArn 'resource-arn' `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -AssociationMaxAttempts 3 `
                -RetrySeconds 0
        } | Should -Throw '*retry budget*'

        @($calls | Where-Object { $_ -eq 'wafv2 associate-web-acl' }).Count | Should -Be 3
    }

    It 'Fails closed when the active ACL lacks required rules' {
        $calls = [System.Collections.Generic.List[string]]::new()
        $invokeAws = New-TestWafInvoker `
            -ActiveSequence @('expected/name/id') `
            -RuleNames @('RateLimitPerIP', 'AWSManagedRulesCommonRuleSet') `
            -Calls $calls

        {
            Invoke-GameAgentWafReconciliation `
                -ExpectedWebAclArn 'expected/name/id' `
                -ResourceArn 'resource-arn' `
                -Region 'us-west-2' `
                -InvokeAws $invokeAws `
                -VerificationMaxAttempts 2 `
                -RetrySeconds 0
        } | Should -Throw '*did not converge*'

        $calls | Should -Not -Contain 'wafv2 associate-web-acl'
    }
}
