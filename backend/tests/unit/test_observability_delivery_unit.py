"""Behavioral tests for scoped, verified observability deployment helpers.

These tests exercise the shell helpers in ``scripts/deploy.sh`` (runtime trace
delivery classification, bounded retry, and verification) and
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
    "ensure_runtime_trace_delivery",
)


def _function_source(script: str, name: str) -> str:
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", script)
    assert match, f"{name} not found"
    return match.group(0)


def _delivery_harness(aws_mock: str, invocation: str) -> subprocess.CompletedProcess[str]:
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
        ("ResourceAlreadyExistsException: the resource already exists", "conflict"),
        ("An error occurred (ThrottlingException): Rate exceeded", "retryable"),
        ("An error occurred (ServiceUnavailableException)", "retryable"),
        ("An error occurred (AccessDeniedException): not authorized", "fatal"),
        ("An error occurred (ValidationException): bad input", "fatal"),
    ],
)
def test_classify_delivery_error(error_text, expected):
    result = _delivery_harness("", f'classify_delivery_error "{error_text}"')
    assert result.returncode == 0
    assert result.stdout.strip() == expected


# ---------------------------------------------------------------------------
# run_delivery_mutation
# ---------------------------------------------------------------------------


def test_run_delivery_mutation_succeeds_on_clean_create():
    aws_mock = 'aws() { echo "{}"; return 0; }'
    result = _delivery_harness(aws_mock, 'run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"')
    assert result.returncode == 0
    assert "rc=0" in result.stdout


def test_run_delivery_mutation_treats_conflict_as_idempotent_success():
    aws_mock = 'aws() { echo "An error occurred (ConflictException)" >&2; return 254; }'
    result = _delivery_harness(aws_mock, 'run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"')
    assert result.returncode == 0
    assert "rc=0" in result.stdout
    assert "already exists" in result.stdout


def test_run_delivery_mutation_fails_on_non_conflict_error():
    """Red-green regression: a genuine authorization failure must fail, not be
    silently treated as an idempotent already-exists result."""
    aws_mock = 'aws() { echo "An error occurred (AccessDeniedException): not authorized" >&2; return 254; }'
    result = _delivery_harness(aws_mock, 'run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"')
    assert "rc=1" in result.stdout
    assert "non-retryable" in result.stderr


def test_run_delivery_mutation_retries_then_succeeds():
    aws_mock = (
        'aws() {\n'
        '  COUNT_FILE=/tmp/gbaw-o471-retry-count\n'
        '  n=0; [ -f "$COUNT_FILE" ] && n=$(cat "$COUNT_FILE")\n'
        '  n=$((n+1)); echo "$n" > "$COUNT_FILE"\n'
        '  if [ "$n" -lt 3 ]; then echo "ThrottlingException: Rate exceeded" >&2; return 254; fi\n'
        '  echo "{}"; return 0\n'
        '}'
    )
    invocation = (
        "rm -f /tmp/gbaw-o471-retry-count\n"
        'run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"\n'
        'echo "attempts=$(cat /tmp/gbaw-o471-retry-count)"\n'
        "rm -f /tmp/gbaw-o471-retry-count"
    )
    result = _delivery_harness(aws_mock, invocation)
    assert result.returncode == 0
    assert "rc=0" in result.stdout
    assert "attempts=3" in result.stdout


def test_run_delivery_mutation_fails_when_retryable_errors_exhaust_budget():
    aws_mock = 'aws() { echo "ThrottlingException: Rate exceeded" >&2; return 254; }'
    invocation = 'GBAW_DELIVERY_MAX_ATTEMPTS=3 run_delivery_mutation "Delivery" logs create-delivery; echo "rc=$?"'
    result = _delivery_harness(aws_mock, invocation)
    assert "rc=1" in result.stdout
    assert "retryable AWS error" in result.stderr


# ---------------------------------------------------------------------------
# delivery_is_active (verification)
# ---------------------------------------------------------------------------


def test_delivery_is_active_matches_source_and_destination():
    deliveries = (
        '{"deliveries":[{"deliverySourceName":"rt-1-traces-source",'
        '"deliveryDestinationArn":"arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination"}]}'
    )
    invocation = (
        f"delivery_is_active '{deliveries}' "
        "'rt-1-traces-source' "
        "'arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination'; echo \"rc=$?\""
    )
    result = _delivery_harness("", invocation)
    assert "rc=0" in result.stdout


def test_delivery_is_active_fails_when_destination_missing():
    deliveries = '{"deliveries":[{"deliverySourceName":"rt-1-traces-source","deliveryDestinationArn":"arn:aws:logs:us-west-2:123456789012:delivery-destination:other"}]}'
    invocation = (
        f"delivery_is_active '{deliveries}' "
        "'rt-1-traces-source' "
        "'arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination'; echo \"rc=$?\""
    )
    result = _delivery_harness("", invocation)
    assert "rc=1" in result.stdout


# ---------------------------------------------------------------------------
# ensure_runtime_trace_delivery (end-to-end over mocked aws)
# ---------------------------------------------------------------------------

_RUNTIME_ARGS = (
    "ensure_runtime_trace_delivery rt-1 "
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1 123456789012; "
    'echo "rc=$?"'
)


def _runtime_aws_mock(*, describe_matches: bool, create_error: str | None = None) -> str:
    dest_arn = "arn:aws:logs:us-west-2:123456789012:delivery-destination:rt-1-traces-destination"
    source_name = "rt-1-traces-source"
    described_source = source_name if describe_matches else "mismatch-source"
    create_block = (
        f'    echo "{create_error}" >&2; return 254'
        if create_error
        else '    echo "{}"; return 0'
    )
    return (
        "aws() {\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-source" ]; then echo "{}"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "put-delivery-destination" ]; then\n'
        f'    echo \'{{"deliveryDestination":{{"arn":"{dest_arn}"}}}}\'; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "create-delivery" ]; then\n'
        f"{create_block}\n"
        "  fi\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-deliveries" ]; then\n'
        f'    echo \'{{"deliveries":[{{"deliverySourceName":"{described_source}",'
        f'"deliveryDestinationArn":"{dest_arn}"}}]}}\'; return 0; fi\n'
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


# ---------------------------------------------------------------------------
# Account-wide observability: scoped, opt-in, state-preserving
# ---------------------------------------------------------------------------


def _account_obs_harness(configure: str, aws_mock: str) -> subprocess.CompletedProcess[str]:
    command = "\n".join(
        (
            f'export GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY="{configure}"',
            'export AWS_REGION="us-west-2"',
            aws_mock,
            "export -f aws",
            f'bash "{ACCOUNT_OBS_SCRIPT}"',
        )
    )
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True)


_SUPPORTED_MOCK = (
    "aws() {\n"
    '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then echo "CloudWatchLogs"; return 0; fi\n'
    '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{"logGroups":[{"logGroupName":"aws/spans"}]}\'; return 0; fi\n'
    '  echo "MUTATION_CALLED: $*" >&2; return 0\n'
    "}"
)

_UNSUPPORTED_MOCK = (
    "aws() {\n"
    '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then echo "XRay"; return 0; fi\n'
    '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{"logGroups":[]}\'; return 0; fi\n'
    '  echo "MUTATION_CALLED: $*" >&2; return 0\n'
    "}"
)


def test_account_obs_default_makes_no_mutation_when_supported():
    result = _account_obs_harness("false", _SUPPORTED_MOCK)
    assert result.returncode == 0
    assert "MUTATION_CALLED" not in result.stderr
    assert "no account-wide" in result.stdout
    assert "already supports Transaction Search" in result.stdout


def test_account_obs_default_warns_with_opt_in_instruction_when_unsupported():
    result = _account_obs_harness("false", _UNSUPPORTED_MOCK)
    # Default must not fail the deploy and must not mutate shared settings.
    assert result.returncode == 0
    assert "MUTATION_CALLED" not in result.stderr
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" in result.stdout


def test_account_obs_optin_prints_scope_and_rollback_and_mutates():
    # Capture which shared mutations happen under opt-in.
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi\n'
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then echo "XRay"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{"logGroups":[{"logGroupName":"aws/spans"}]}\'; return 0; fi\n'
        '  echo "MUTATION: $1 $2" >&2; return 0\n'
        "}"
    )
    result = _account_obs_harness("true", aws_mock)
    assert result.returncode == 0
    assert "opt-in is ENABLED" in result.stdout
    assert "may be created or changed" in result.stdout
    assert "Rollback:" in result.stdout
    # Enables Transaction Search, resource policy, and indexing rule — but never
    # disables a shared setting (no update-trace-segment-destination XRay).
    assert "MUTATION: xray update-trace-segment-destination" in result.stderr
    assert "MUTATION: logs put-resource-policy" in result.stderr
    assert "MUTATION: xray update-indexing-rule" in result.stderr


def test_account_obs_optin_preserves_existing_cloudwatchlogs_destination():
    """When Transaction Search is already enabled, opt-in must not toggle the
    shared destination (no disable/re-enable)."""
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi\n'
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then echo "CloudWatchLogs"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{"logGroups":[{"logGroupName":"aws/spans"}]}\'; return 0; fi\n'
        '  echo "MUTATION: $1 $2 ${3:-}" >&2; return 0\n'
        "}"
    )
    result = _account_obs_harness("true", aws_mock)
    assert result.returncode == 0
    assert "already CloudWatchLogs" in result.stdout
    # The destination is left unchanged — no update-trace-segment-destination call.
    assert "MUTATION: xray update-trace-segment-destination" not in result.stderr
