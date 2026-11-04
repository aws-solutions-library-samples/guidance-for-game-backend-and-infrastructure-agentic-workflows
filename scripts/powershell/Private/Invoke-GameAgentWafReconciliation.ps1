function Invoke-GameAgentWafReconciliation {
    <#
    .SYNOPSIS
        Reconciles and verifies the project WebACL on the ECS Express ALB.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ExpectedWebAclArn,

        [Parameter(Mandatory)]
        [string]$ResourceArn,

        [Parameter(Mandatory)]
        [string]$Region,

        [Parameter(Mandatory)]
        [scriptblock]$InvokeAws,

        [ValidateRange(1, 120)]
        [int]$AssociationMaxAttempts = 36,

        [ValidateRange(1, 120)]
        [int]$VerificationMaxAttempts = 36,

        [ValidateRange(0, 60)]
        [int]$RetrySeconds = 5
    )

    $requiredRules = @(
        'RateLimitAuthPaths'
        'RateLimitAdminPaths'
        'RateLimitPerIP'
        'AWSManagedRulesCommonRuleSet'
        'AWSManagedRulesSQLiRuleSet'
        'AWSManagedRulesKnownBadInputsRuleSet'
    )

    function Test-TransientWafError {
        param([string]$ErrorText)
        return $ErrorText -match 'WAFNonexistentItemException|WAFUnavailableEntityException|WAFInternalErrorException'
    }

    function Invoke-WafAws {
        param([string[]]$Arguments)
        $result = & $InvokeAws $Arguments
        return ($result | Out-String).Trim()
    }

    function Get-ActiveWebAclArn {
        return Invoke-WafAws @(
            'wafv2', 'get-web-acl-for-resource',
            '--resource-arn', $ResourceArn,
            '--region', $Region,
            '--query', 'WebACL.ARN',
            '--output', 'text'
        )
    }

    function Test-RequiredWebAclRules {
        $arnSegments = $ExpectedWebAclArn -split '/'
        if ($arnSegments.Count -lt 3) {
            throw 'Expected WebACL ARN is malformed.'
        }
        $webAclName = $arnSegments[-2]
        $webAclId = $arnSegments[-1]
        $ruleOutput = Invoke-WafAws @(
            'wafv2', 'get-web-acl',
            '--scope', 'REGIONAL',
            '--id', $webAclId,
            '--name', $webAclName,
            '--region', $Region,
            '--query', 'WebACL.Rules[].Name',
            '--output', 'text'
        )
        $ruleNames = @($ruleOutput -split '\s+' | Where-Object { $_ })
        return @($requiredRules | Where-Object { $_ -notin $ruleNames }).Count -eq 0
    }

    $activeWebAclArn = ''
    $lookupWasTransient = $false
    try {
        $activeWebAclArn = Get-ActiveWebAclArn
    } catch {
        if (Test-TransientWafError $_.Exception.Message) {
            $lookupWasTransient = $true
        } else {
            throw 'Unable to inspect the active WAF association.'
        }
    }

    if (-not $lookupWasTransient -and $activeWebAclArn -eq $ExpectedWebAclArn) {
        try {
            if (Test-RequiredWebAclRules) {
                Write-GameAgentStatus 'Expected WAF and required rules are already active on the frontend ALB' -Type Success
                return
            }
        } catch {
            if (-not (Test-TransientWafError $_.Exception.Message)) {
                throw 'Unable to inspect the project WebACL rules.'
            }
        }
    } else {
        $associated = $false
        for ($attempt = 1; $attempt -le $AssociationMaxAttempts; $attempt++) {
            try {
                Invoke-WafAws @(
                    'wafv2', 'associate-web-acl',
                    '--web-acl-arn', $ExpectedWebAclArn,
                    '--resource-arn', $ResourceArn,
                    '--region', $Region
                ) | Out-Null
                $associated = $true
                break
            } catch {
                if (-not (Test-TransientWafError $_.Exception.Message)) {
                    throw 'Unable to associate the project WebACL.'
                }
                if ($attempt -lt $AssociationMaxAttempts) {
                    Start-Sleep -Seconds $RetrySeconds
                }
            }
        }
        if (-not $associated) {
            throw 'Project WAF association did not succeed within the retry budget.'
        }
    }

    for ($attempt = 1; $attempt -le $VerificationMaxAttempts; $attempt++) {
        try {
            $activeWebAclArn = Get-ActiveWebAclArn
            if ($activeWebAclArn -eq $ExpectedWebAclArn -and (Test-RequiredWebAclRules)) {
                Write-GameAgentStatus "Project WAF association and required rules converged after $attempt check(s)" -Type Success
                return
            }
        } catch {
            if (-not (Test-TransientWafError $_.Exception.Message)) {
                throw 'Unable to verify the project WAF association and rules.'
            }
        }
        if ($attempt -lt $VerificationMaxAttempts) {
            Start-Sleep -Seconds $RetrySeconds
        }
    }

    throw 'Project WAF association and required rules did not converge.'
}
