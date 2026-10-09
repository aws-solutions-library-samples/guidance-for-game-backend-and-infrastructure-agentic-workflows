BeforeAll {
    . (Join-Path $PSScriptRoot '..' 'Private' 'New-GameAgentAwsInvoker.ps1' | Resolve-Path)
}

Describe 'New-GameAgentAwsInvoker' {
    AfterEach {
        Remove-Item -Path function:aws -ErrorAction SilentlyContinue
        $global:LASTEXITCODE = 0
    }

    It 'Invokes the AWS CLI directly and returns its output on success' {
        $script:received = $null
        function global:aws {
            $script:received = $args
            $global:LASTEXITCODE = 0
            return 'ok-output'
        }
        $invoker = New-GameAgentAwsInvoker -ProfileArgs @()
        $result = & $invoker @('sts', 'get-caller-identity')
        "$result" | Should -Match 'ok-output'
    }

    It 'Throws when the AWS CLI exits non-zero' {
        function global:aws {
            $global:LASTEXITCODE = 254
            return 'An error occurred (AccessDeniedException)'
        }
        $invoker = New-GameAgentAwsInvoker -ProfileArgs @() -Label 'AWS delivery command'
        { & $invoker @('logs', 'create-delivery') } | Should -Throw '*AWS delivery command failed*'
    }

    It 'Appends the resolved profile arguments to each call' {
        $script:received = @()
        function global:aws {
            $script:received = $args
            $global:LASTEXITCODE = 0
            return ''
        }
        $invoker = New-GameAgentAwsInvoker -ProfileArgs @('--profile', 'demo')
        & $invoker @('logs', 'describe-deliveries') | Out-Null
        $script:received | Should -Contain '--profile'
        $script:received | Should -Contain 'demo'
    }

    It 'Resolves aws correctly when the invoker is passed into another function' {
        # Guards against the GetNewClosure() scoping blocker: the closure must
        # call `& aws` directly, not a function nested in a caller's scope.
        function global:aws {
            $global:LASTEXITCODE = 0
            return 'nested-ok'
        }
        function Invoke-ThroughBoundary {
            param([scriptblock]$InvokeAws)
            return (& $InvokeAws @('sts', 'get-caller-identity'))
        }
        $invoker = New-GameAgentAwsInvoker -ProfileArgs @()
        $result = Invoke-ThroughBoundary -InvokeAws $invoker
        "$result" | Should -Match 'nested-ok'
    }
}
