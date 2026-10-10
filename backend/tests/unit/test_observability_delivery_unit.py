"""Behavioral tests for scoped, verified observability deployment helpers.

These tests exercise the shell helpers in ``scripts/deploy.sh`` (runtime trace
delivery classification, bounded retry, ARN resolution, and verification) and
``scripts/infrastructure/setup-account-observability.sh`` (scoped, opt-in,
state-preserving account-wide setup) with mocked AWS CLI responses. All values
are synthetic.
"""

# Standard library
import pathlib
import re
import subprocess

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts/deploy.sh"
ACCOUNT_OBS_SCRIPT = PROJECT_ROOT / "scripts/infrastructure/setup-account-observability.sh"

DELIVERY_FUNCTION_NAMES = (
    "is_resolved_deployment_value",
    "classify_delivery_error",
    "run_delivery_mutation",
    "delivery_is_active",
    "delivery_resource_matches_runtime",
    "resolve_delivery_destination_arn",
    "ensure_runtime_trace_delivery",
)


def _function_source(script: str, name: str) -> str:
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", script)
    assert match, f"{name} not found"
    return match.group(0)


def _delivery_harness(aws_mock: str, invocation: str) -> "subprocess.CompletedProcess[str]":
    """Run a bash harness that sources the delivery helpers over a mocked ``aws``."""
    content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    functions = "\n".join(_function_source(content, name) for name in DELIVERY_FUNCTION_NAMES)
    command = "\n".join(
        (
            "set -o pipefail",
            'AWS_REGION="us-west-2"',
            "GBAW_DELIVERY_RETRY_SECONDS=0",
            functions,
            "sleep() { :; }",
            aws_mock,
            invocation,
        )
    )
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True)


# ---------------------------------------------------------------------------
# classify_delivery_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_text,expected",
    [
        ("An error occurred (ConflictException) when calling CreateDelivery", "conflict"),
        ("An error occurred (ResourceAlreadyExistsException) when calling PutDeliverySource", "conflict"),
        ("An error occurred (ThrottlingException): Rate exceeded", "retryable"),
        ("An error occurred (ServiceUnavailableException)", "retryable"),
        ("An error occurred (InternalFailure)", "retryable"),
        ("Could not connect to the endpoint URL: https://logs.us-west-2.amazonaws.com/", "retryable"),
        ("An error occurred (AccessDeniedException): not authorized", "fatal"),
        ("An error occurred (ValidationException): bad input", "fatal"),
    ],
)
def test_classify_delivery_error(error_text, expected):
    result = _delivery_harness("", f'classify_delivery_error "{error_text}"')
    assert result.returncode == 0
    assert result.stdout.strip() == expected


def test_classify_delivery_error_ignores_free_text_already_exists():
    """A validation error that merely mentions 'already exists' is not a conflict."""
    text = "An error occurred (ValidationException): a resource with that name already exists elsewhere"
    result = _delivery_harness("", f'classify_delivery_error "{text}"')
    assert result.stdout.strip() == "fatal"


def test_classify_delivery_error_ignores_500_in_free_text():
    """A bare '500' in free text (request id / ARN) must not be read as a retryable 500."""
    text = (
        "An error occurred (AccessDeniedException): arn:aws:sts::123456789012:assumed-role/x "
        "is not authorized; request id 500-503-abc"
    )
    result = _delivery_harness("", f'classify_delivery_error "{text}"')
    assert result.stdout.strip() == "fatal"


@pytest.mark.parametrize("code", ["500", "502", "503", "504"])
def test_classify_delivery_error_numeric_http_codes_are_retryable(code):
    """A modeled numeric HTTP status in the '(<code>)' form is retryable, not fatal."""
    text = f"An error occurred ({code}) when calling CreateDelivery: service error"
    result = _delivery_harness("", f'classify_delivery_error "{text}"')
    assert result.stdout.strip() == "retryable"


# ---------------------------------------------------------------------------
# run_delivery_mutation (0 success | 1 fatal/exhausted | 2 conflict)
# ---------------------------------------------------------------------------


def test_run_delivery_mutation_succeeds_on_clean_create():
    aws_mock = 'aws() { echo "{}"; return 0; }'
    result = _delivery_harness(aws_mock, 'run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"')
    assert result.returncode == 0
    assert "rc=0" in result.stdout


def test_run_delivery_mutation_signals_conflict_with_rc_2():
    aws_mock = 'aws() { echo "An error occurred (ConflictException)" >&2; return 254; }'
    result = _delivery_harness(aws_mock, 'run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"')
    assert "rc=2" in result.stdout


def test_run_delivery_mutation_fails_on_non_conflict_error():
    """Red-green regression: a genuine authorization failure must fail, not be
    silently treated as an idempotent already-exists result."""
    aws_mock = 'aws() { echo "An error occurred (AccessDeniedException): not authorized" >&2; return 254; }'
    result = _delivery_harness(aws_mock, 'run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"')
    assert "rc=1" in result.stdout
    assert "non-retryable" in result.stderr


def test_run_delivery_mutation_retries_then_succeeds(tmp_path):
    count_file = tmp_path / "retry-count"
    aws_mock = (
        "aws() {\n"
        f'  COUNT_FILE="{count_file}"\n'
        '  n=0; [ -f "$COUNT_FILE" ] && n=$(cat "$COUNT_FILE")\n'
        '  n=$((n+1)); echo "$n" > "$COUNT_FILE"\n'
        '  if [ "$n" -lt 3 ]; then echo "An error occurred (ThrottlingException): Rate exceeded" >&2; return 254; fi\n'
        '  echo "{}"; return 0\n'
        "}"
    )
    invocation = (
        'run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"\n' f'echo "attempts=$(cat {count_file})"'
    )
    result = _delivery_harness(aws_mock, invocation)
    assert result.returncode == 0
    assert "rc=0" in result.stdout
    assert "attempts=3" in result.stdout


def test_run_delivery_mutation_fails_when_retryable_errors_exhaust_budget():
    aws_mock = 'aws() { echo "An error occurred (ThrottlingException): Rate exceeded" >&2; return 254; }'
    invocation = 'GBAW_DELIVERY_MAX_ATTEMPTS=3 run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"'
    result = _delivery_harness(aws_mock, invocation)
    assert "rc=1" in result.stdout
    assert "retryable AWS error" in result.stderr


def test_run_delivery_mutation_caps_max_attempts_at_20():
    """An over-large env knob is clamped to the same cap the PowerShell path uses."""
    aws_mock = (
        "aws() {\n"
        '  COUNT_FILE="$CF"\n'
        '  n=0; [ -f "$COUNT_FILE" ] && n=$(cat "$COUNT_FILE")\n'
        '  n=$((n+1)); echo "$n" > "$COUNT_FILE"\n'
        '  echo "An error occurred (ThrottlingException)" >&2; return 254\n'
        "}"
    )
    invocation = (
        'CF="$(mktemp)"; export CF\n'
        'GBAW_DELIVERY_MAX_ATTEMPTS=500 run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"\n'
        'echo "attempts=$(cat "$CF")"; rm -f "$CF"'
    )
    result = _delivery_harness(aws_mock, invocation)
    assert "rc=1" in result.stdout
    assert "attempts=20" in result.stdout


# ---------------------------------------------------------------------------
# resolve_delivery_destination_arn (no fabricated ARN)
# ---------------------------------------------------------------------------


def test_resolve_delivery_destination_arn_uses_api_value():
    arn = "arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination"
    aws_mock = f'aws() {{ echo "{arn}"; return 0; }}'
    result = _delivery_harness(aws_mock, 'resolve_delivery_destination_arn rt-1-traces-destination; echo "rc=$?"')
    assert result.returncode == 0
    assert arn in result.stdout
    assert "rc=0" in result.stdout


def test_resolve_delivery_destination_arn_fails_when_unresolved():
    aws_mock = 'aws() { echo "None"; return 0; }'
    result = _delivery_harness(aws_mock, 'resolve_delivery_destination_arn rt-1-traces-destination; echo "rc=$?"')
    assert "rc=1" in result.stdout


# ---------------------------------------------------------------------------
# delivery_is_active (verification: dest type + source binding)
# ---------------------------------------------------------------------------

_ACTIVE_DELIVERIES = (
    '{"deliveries":[{"deliverySourceName":"rt-1-traces-source",'
    '"deliveryDestinationArn":"arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination",'
    '"deliveryDestinationType":"XRAY"}]}'
)
_SOURCE_BOUND = (
    '{"deliverySource":{"name":"rt-1-traces-source","logType":"TRACES",'
    '"resourceArns":["arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1"]}}'
)
_DEST_ARN = "arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination"
_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1"


def _active_invocation(deliveries: str, source_json: str) -> str:
    return (
        f"delivery_is_active '{deliveries}' 'rt-1-traces-source' "
        f"'{_DEST_ARN}' '{_RUNTIME_ARN}' '{source_json}'; echo \"rc=$?\""
    )


def test_delivery_is_active_matches_source_dest_type_and_binding():
    result = _delivery_harness("", _active_invocation(_ACTIVE_DELIVERIES, _SOURCE_BOUND))
    assert "rc=0" in result.stdout


def test_delivery_is_active_fails_when_destination_missing():
    deliveries = (
        '{"deliveries":[{"deliverySourceName":"rt-1-traces-source",'
        '"deliveryDestinationArn":"arn:aws:logs:us-west-2:123456789012:delivery-destination:other",'
        '"deliveryDestinationType":"XRAY"}]}'
    )
    result = _delivery_harness("", _active_invocation(deliveries, _SOURCE_BOUND))
    assert "rc=1" in result.stdout


def test_delivery_is_active_fails_when_destination_type_not_xray():
    deliveries = (
        '{"deliveries":[{"deliverySourceName":"rt-1-traces-source",'
        f'"deliveryDestinationArn":"{_DEST_ARN}","deliveryDestinationType":"S3"}}]}}'
    )
    result = _delivery_harness("", _active_invocation(deliveries, _SOURCE_BOUND))
    assert "rc=1" in result.stdout


def test_delivery_is_active_fails_when_source_not_bound_to_runtime():
    source_json = (
        '{"deliverySource":{"name":"rt-1-traces-source","logType":"TRACES",'
        '"resourceArns":["arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/other"]}}'
    )
    result = _delivery_harness("", _active_invocation(_ACTIVE_DELIVERIES, source_json))
    assert "rc=1" in result.stdout


# ---------------------------------------------------------------------------
# ensure_runtime_trace_delivery (end-to-end over mocked aws)
# ---------------------------------------------------------------------------

_RUNTIME_ARGS = f'ensure_runtime_trace_delivery rt-1 {_RUNTIME_ARN}; echo "rc=$?"'


def _runtime_aws_mock(
    *,
    describe_matches: bool,
    source_bound: bool = True,
    create_error: str | None = None,
    describe_error: str | None = None,
) -> str:
    source_name = "rt-1-traces-source"
    described_source = source_name if describe_matches else "mismatch-source"
    runtime_arn = _RUNTIME_ARN if source_bound else "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/other"
    create_block = f'    echo "{create_error}" >&2; return 254' if create_error else '    echo "{}"; return 0'
    describe_block = (
        f'    echo "{describe_error}" >&2; return 254'
        if describe_error
        else (
            f'    echo \'{{"deliveries":[{{"deliverySourceName":"{described_source}",'
            f'"deliveryDestinationArn":"{_DEST_ARN}","deliveryDestinationType":"XRAY"}}]}}\'; return 0'
        )
    )
    return (
        "aws() {\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-source" ]; then echo "{}"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-destination" ]; then\n'
        f'    echo \'{{"deliveryDestination":{{"arn":"{_DEST_ARN}"}}}}\'; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "create-delivery" ]; then\n'
        f"{create_block}\n"
        "  fi\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "get-delivery-source" ]; then\n'
        f'    echo \'{{"deliverySource":{{"name":"{source_name}","logType":"TRACES",'
        f'"resourceArns":["{runtime_arn}"]}}}}\'; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-deliveries" ]; then\n'
        f"{describe_block}\n"
        "  fi\n"
        '  echo "unexpected: $*" >&2; return 1\n'
        "}"
    )


def test_ensure_runtime_trace_delivery_succeeds_when_verified_active():
    result = _delivery_harness(_runtime_aws_mock(describe_matches=True), _RUNTIME_ARGS)
    assert result.returncode == 0
    assert "rc=0" in result.stdout
    assert "Delivery verified active" in result.stdout


def test_ensure_runtime_trace_delivery_fails_on_non_conflict_create_error():
    aws_mock = _runtime_aws_mock(
        describe_matches=True,
        create_error="An error occurred (AccessDeniedException): not authorized",
    )
    result = _delivery_harness(aws_mock, _RUNTIME_ARGS)
    assert "rc=1" in result.stdout


def test_ensure_runtime_trace_delivery_fails_when_verification_does_not_match():
    result = _delivery_harness(_runtime_aws_mock(describe_matches=False), _RUNTIME_ARGS)
    assert "rc=1" in result.stdout
    assert "not active" in result.stderr


def test_ensure_runtime_trace_delivery_fails_when_source_not_bound_to_runtime():
    result = _delivery_harness(_runtime_aws_mock(describe_matches=True, source_bound=False), _RUNTIME_ARGS)
    assert "rc=1" in result.stdout


def test_ensure_runtime_trace_delivery_retries_describe_then_succeeds(tmp_path):
    """A single throttle on describe-deliveries is retried, not treated as fatal."""
    count_file = tmp_path / "describe-count"
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-source" ]; then echo "{}"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-destination" ]; then\n'
        f'    echo \'{{"deliveryDestination":{{"arn":"{_DEST_ARN}"}}}}\'; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "create-delivery" ]; then echo "{}"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "get-delivery-source" ]; then\n'
        f'    echo \'{{"deliverySource":{{"name":"rt-1-traces-source","logType":"TRACES",'
        f'"resourceArns":["{_RUNTIME_ARN}"]}}}}\'; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-deliveries" ]; then\n'
        f'    COUNT_FILE="{count_file}"\n'
        '    n=0; [ -f "$COUNT_FILE" ] && n=$(cat "$COUNT_FILE")\n'
        '    n=$((n+1)); echo "$n" > "$COUNT_FILE"\n'
        '    if [ "$n" -lt 2 ]; then echo "An error occurred (ThrottlingException)" >&2; return 254; fi\n'
        f'    echo \'{{"deliveries":[{{"deliverySourceName":"rt-1-traces-source",'
        f'"deliveryDestinationArn":"{_DEST_ARN}","deliveryDestinationType":"XRAY"}}]}}\'; return 0; fi\n'
        '  echo "unexpected: $*" >&2; return 1\n'
        "}"
    )
    result = _delivery_harness(aws_mock, _RUNTIME_ARGS)
    assert result.returncode == 0
    assert "rc=0" in result.stdout


def test_ensure_runtime_trace_delivery_resolves_arn_on_destination_conflict():
    """When the destination already exists (conflict, no stdout), the ARN is
    resolved from the API, never fabricated."""
    resolved_arn = _DEST_ARN
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-source" ]; then echo "{}"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-destination" ]; then\n'
        '    echo "An error occurred (ConflictException)" >&2; return 254; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "get-delivery-destination" ]; then\n'
        '    case " $* " in\n'
        f'      *" --query "*) echo "{resolved_arn}"; return 0;;\n'
        f'      *) echo \'{{"deliveryDestination":{{"name":"rt-1-traces-destination",'
        f'"deliveryDestinationType":"XRAY","arn":"{resolved_arn}"}}}}\'; return 0;;\n'
        "    esac\n"
        "  fi\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "create-delivery" ]; then echo "{}"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "get-delivery-source" ]; then\n'
        f'    echo \'{{"deliverySource":{{"name":"rt-1-traces-source","logType":"TRACES",'
        f'"resourceArns":["{_RUNTIME_ARN}"]}}}}\'; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-deliveries" ]; then\n'
        f'    echo \'{{"deliveries":[{{"deliverySourceName":"rt-1-traces-source",'
        f'"deliveryDestinationArn":"{resolved_arn}","deliveryDestinationType":"XRAY"}}]}}\'; return 0; fi\n'
        '  echo "unexpected: $*" >&2; return 1\n'
        "}"
    )
    result = _delivery_harness(aws_mock, _RUNTIME_ARGS)
    assert result.returncode == 0
    assert "rc=0" in result.stdout


def test_ensure_runtime_trace_delivery_fails_on_conflicting_source():
    """A conflicting source NOT bound to our runtime must fail closed."""
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-source" ]; then\n'
        '    echo "An error occurred (ConflictException)" >&2; return 254; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "get-delivery-source" ]; then\n'
        '    echo \'{"deliverySource":{"name":"rt-1-traces-source","logType":"TRACES",'
        '"resourceArns":["arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/other"]}}\'; return 0; fi\n'
        '  echo "unexpected: $*" >&2; return 1\n'
        "}"
    )
    result = _delivery_harness(aws_mock, _RUNTIME_ARGS)
    assert "rc=1" in result.stdout
    assert "conflicting delivery source" in result.stderr


# ---------------------------------------------------------------------------
# Account-wide observability: scoped, opt-in, state-preserving
# ---------------------------------------------------------------------------


def _account_obs_harness(
    configure: str,
    aws_mock: str,
    extra_env: str = "",
    shell: str = "bash",
) -> "subprocess.CompletedProcess[str]":
    command = "\n".join(
        (
            f'export GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY="{configure}"',
            'export AWS_REGION="us-west-2"',
            "export GBAW_OBSERVABILITY_ACTIVE_RETRY_SECONDS=0",
            'MUTATION_LOG="$(mktemp)"; export MUTATION_LOG',
            extra_env,
            aws_mock,
            "export -f aws",
            f'bash "{ACCOUNT_OBS_SCRIPT}"; echo "rc=$?"',
            # Surface recorded mutations on stdout so the opt-in helper's output
            # capture (2>&1) does not hide them from the test.
            'echo "MUTATIONS:"; cat "$MUTATION_LOG"; rm -f "$MUTATION_LOG"',
        )
    )
    return subprocess.run([shell, "-c", command], capture_output=True, text=True)


# Shells the opt-in path is validated against: the login shell `bash` and the
# macOS system `/bin/bash` (3.2), so a bash-4+ construct cannot regress 3.2.
_OBS_SHELLS = ("bash", "/bin/bash")


def _obs_mock(
    *,
    destination: str = "XRay",
    spans: bool = False,
    indexing_percent: str = "5",
    policies_json: str = '{"resourcePolicies":[]}',
    status: str = "ACTIVE",
    fail_operation: str = "",
    fail_code: str = "AccessDeniedException",
) -> str:
    spans_json = '{"logGroups":[{"logGroupName":"aws/spans"}]}' if spans else '{"logGroups":[]}'
    # When fail_operation names a mutating "$1 $2" pair (e.g.
    # "logs put-resource-policy"), the mock returns that AWS error on stderr
    # with a non-zero exit, as the real CLI does, so the helper's exit-status
    # handling is exercised rather than mocked away.
    fail_block = ""
    if fail_operation:
        fail_block = (
            f'  if [ "$1 $2" = "{fail_operation}" ]; then\n'
            f'    echo "MUTATION: $1 $2 ${{3:-}}" >> "$MUTATION_LOG"\n'
            f'    echo "An error occurred ({fail_code}) when calling the operation" >&2\n'
            "    return 254\n"
            "  fi\n"
        )
    return (
        "aws() {\n"
        '  if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi\n'
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then\n'
        '    case " $* " in\n'
        f'      *" Status "*) echo "{status}"; return 0;;\n'
        f'      *) echo "{destination}"; return 0;;\n'
        "    esac\n"
        "  fi\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-indexing-rules" ]; then '
        f'echo "{indexing_percent}"; return 0; fi\n'
        f'  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{spans_json}\'; return 0; fi\n'
        f'  if [ "$1" = "logs" ] && [ "$2" = "describe-resource-policies" ]; then echo \'{policies_json}\'; return 0; fi\n'
        f"{fail_block}"
        '  echo "MUTATION: $1 $2 ${3:-}" >> "$MUTATION_LOG"\n'
        # Record the full resource-policy arguments so tests can check the
        # account scoping of the shared grant, not only that a write happened.
        '  if [ "$1 $2" = "logs put-resource-policy" ]; then echo "POLICY_ARGS: $*" >> "$MUTATION_LOG"; fi\n'
        "  return 0\n"
        "}"
    )


def test_account_obs_default_makes_no_mutation_when_supported():
    result = _account_obs_harness("false", _obs_mock(destination="CloudWatchLogs", spans=True))
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "no account-wide" in result.stdout
    assert "already supports Transaction Search" in result.stdout


def test_account_obs_default_warns_with_scope_and_opt_in_when_unsupported():
    result = _account_obs_harness("false", _obs_mock(destination="XRay", spans=False))
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" in result.stdout
    # The "would change" scope lists all three shared settings before opt-in.
    assert "X-Ray trace segment destination" in result.stdout
    assert "X-Ray default indexing rule" in result.stdout
    assert "CloudWatch Logs resource policy" in result.stdout


def test_account_obs_default_prints_unknown_when_destination_unreadable():
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then return 254; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{"logGroups":[]}\'; return 0; fi\n'
        '  echo "MUTATION: $1 $2" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )
    result = _account_obs_harness("false", aws_mock)
    assert "rc=0" in result.stdout
    assert "unknown (read failed)" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]


def test_account_obs_optin_prints_scope_and_mutates_but_preserves_indexing():
    result = _account_obs_harness("true", _obs_mock(destination="XRay", spans=True, indexing_percent="25"))
    assert "rc=0" in result.stdout
    assert "opt-in is ENABLED" in result.stdout
    # Enables Transaction Search and the resource policy.
    assert "MUTATION: xray update-trace-segment-destination" in result.stdout
    assert "MUTATION: logs put-resource-policy" in result.stdout
    # Indexing rule is read and preserved (no mutation) because no explicit
    # percentage was requested.
    assert "MUTATION: xray update-indexing-rule" not in result.stdout
    assert "left unchanged (current: 25% sampling)" in result.stdout


def test_account_obs_optin_changes_indexing_only_when_explicit_percent_set():
    result = _account_obs_harness(
        "true",
        _obs_mock(destination="CloudWatchLogs", spans=True, indexing_percent="25"),
        extra_env='export GBAW_XRAY_DEFAULT_INDEXING_PERCENT="1"',
    )
    assert "rc=0" in result.stdout
    assert "MUTATION: xray update-indexing-rule" in result.stdout
    # Rollback line names the prior value so the shared setting can be restored.
    assert 'DesiredSamplingPercentage":25' in result.stdout


def test_account_obs_optin_refuses_when_destination_unreadable():
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi\n'
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then return 254; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-resource-policies" ]; then echo \'{"resourcePolicies":[]}\'; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{"logGroups":[]}\'; return 0; fi\n'
        '  echo "MUTATION: $1 $2" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )
    result = _account_obs_harness("true", aws_mock)
    assert "rc=1" in result.stdout
    assert "Cannot read the current trace destination" in result.stderr
    # It must NOT change the destination when it cannot read it.
    assert "MUTATION: xray update-trace-segment-destination" not in result.stdout


def test_account_obs_optin_preserves_existing_cloudwatchlogs_destination():
    result = _account_obs_harness("true", _obs_mock(destination="CloudWatchLogs", spans=True))
    assert "rc=0" in result.stdout
    assert "already CloudWatchLogs" in result.stdout
    assert "MUTATION: xray update-trace-segment-destination" not in result.stdout


def test_account_obs_optin_skips_policy_when_legacy_grant_present():
    legacy = (
        '{"resourcePolicies":[{"policyName":"TransactionSearchXRayAccess",'
        '"policyDocument":"{\\"Version\\":\\"2012-10-17\\",\\"Statement\\":[{\\"Effect\\":\\"Allow\\",'
        '\\"Principal\\":{\\"Service\\":\\"xray.amazonaws.com\\"},\\"Action\\":\\"logs:PutLogEvents\\",'
        '\\"Resource\\":[\\"arn:aws:logs:us-west-2:123456789012:log-group:aws/spans:*\\"]}]}"}]}'
    )
    result = _account_obs_harness("true", _obs_mock(destination="CloudWatchLogs", spans=True, policies_json=legacy))
    assert "rc=0" in result.stdout
    assert "already grants" in result.stdout
    assert "MUTATION: logs put-resource-policy" not in result.stdout


def test_account_obs_optin_fails_when_policy_limit_reached():
    policies = (
        '{"resourcePolicies":['
        + ",".join(
            f'{{"policyName":"p{i}","policyDocument":"{{\\"Version\\":\\"2012-10-17\\",\\"Statement\\":[]}}"}}'
            for i in range(10)
        )
        + "]}"
    )
    result = _account_obs_harness("true", _obs_mock(destination="CloudWatchLogs", spans=True, policies_json=policies))
    assert "rc=1" in result.stdout
    assert "maximum of 10" in result.stderr
    assert "MUTATION: logs put-resource-policy" not in result.stdout


# ---------------------------------------------------------------------------
# Account-wide observability opt-in: a failed shared mutation must fail the run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_optin_fails_when_resource_policy_mutation_fails(shell):
    """A failed put-resource-policy must fail the opt-in, not report success.

    The resource policy is a shared, account-wide grant. If the write fails, the
    deployment must surface a bounded failure and stop, never print the
    'configured' success banner.
    """
    result = _account_obs_harness(
        "true",
        _obs_mock(
            destination="XRay",
            spans=True,
            fail_operation="logs put-resource-policy",
            fail_code="LimitExceededException",
        ),
        shell=shell,
    )
    assert "rc=1" in result.stdout
    assert "resource policy failed (LimitExceededException)" in result.stderr
    assert "Account-wide observability configured" not in result.stdout


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_optin_fails_when_destination_mutation_fails(shell):
    """A failed update-trace-segment-destination must fail the opt-in.

    Enabling Transaction Search is a shared, account-wide change. A failed write
    must stop with a bounded diagnostic rather than falling through to the
    success banner, which would report Transaction Search as enabled when it is
    not.
    """
    result = _account_obs_harness(
        "true",
        _obs_mock(
            destination="XRay",
            spans=True,
            fail_operation="xray update-trace-segment-destination",
            fail_code="AccessDeniedException",
        ),
        shell=shell,
    )
    assert "rc=1" in result.stdout
    assert "trace destination failed (AccessDeniedException)" in result.stderr
    assert "Account-wide observability configured" not in result.stdout


# ---------------------------------------------------------------------------
# Account-wide observability opt-in: the shared grant is scoped to the caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_optin_scopes_the_resource_policy_to_the_caller_account(shell):
    """The shared resource policy names the caller's account in every ARN and condition.

    An empty account would produce ``arn:aws:logs:<region>::...`` resources and an
    empty ``aws:SourceAccount`` condition, so the opt-in would write a grant that
    never matches X-Ray's delivery.
    """
    result = _account_obs_harness("true", _obs_mock(destination="XRay", spans=True), shell=shell)
    assert "rc=0" in result.stdout
    assert "MUTATION: logs put-resource-policy" in result.stdout
    assert "arn:aws:logs:us-west-2:123456789012:log-group:aws/spans:*" in result.stdout
    assert "arn:aws:logs:us-west-2:123456789012:log-group:/aws/application-signals/data:*" in result.stdout
    assert "arn:aws:xray:us-west-2:123456789012:*" in result.stdout
    assert '"aws:SourceAccount": "123456789012"' in result.stdout
    assert "arn:aws:logs:us-west-2::" not in result.stdout


@pytest.mark.parametrize("account_output", ["", "None", "not-an-account"])
def test_account_obs_optin_refuses_when_the_account_lookup_is_not_an_account_id(account_output):
    """A lookup that does not return a 12-digit account fails before any shared write."""
    mock = _obs_mock(destination="XRay", spans=True).replace(
        'echo "123456789012"; return 0', f'echo "{account_output}"; return 0'
    )
    result = _account_obs_harness("true", mock)
    assert "rc=1" in result.stdout
    assert "identity lookup returned no account ID" in result.stderr
    assert "MUTATION:" not in result.stdout.split("MUTATIONS:", 1)[1]
