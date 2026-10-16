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
import shutil
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


def _discover_bash_shells() -> "tuple[str, ...]":
    """Return absolute paths to the distinct bash binaries available on this
    host, one per major version, so the shell harnesses can run under both the
    macOS system bash (3.2) and a newer bash (5.x) when it is installed.

    Only binaries that actually exist are returned; a version that is not
    installed is skipped rather than silently falling back to another one. The
    directory containing each returned path is placed ahead of the system paths
    in the harness ``PATH`` so a nested ``bash`` call resolves to the same
    version the test parametrized over.
    """
    candidates = ("/bin/bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash")
    by_major: "dict[str, str]" = {}
    for candidate in candidates:
        if not pathlib.Path(candidate).exists():
            continue
        try:
            proc = subprocess.run(
                [candidate, "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        major = proc.stdout.strip() or candidate
        # Keep the first binary seen for each major version (prefer /bin/bash).
        by_major.setdefault(major, candidate)
    # Deterministic order by major version.
    return tuple(by_major[major] for major in sorted(by_major))


# Absolute bash paths this suite runs the shell harnesses under: the macOS
# system bash 3.2 and a newer bash when installed. Parametrizing over absolute
# paths (rather than the bare name ``bash``) guarantees each case runs under the
# intended version instead of whichever ``bash`` happens to be first on PATH.
_BASH_SHELLS = _discover_bash_shells()


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
            # Run the script under the parametrized shell itself, so a
            # ``/bin/bash`` case really executes it under bash 3.2 rather than
            # whichever ``bash`` happens to be first on PATH.
            f'{shell} "{ACCOUNT_OBS_SCRIPT}"; echo "rc=$?"',
            # Surface recorded mutations on stdout so the opt-in helper's output
            # capture (2>&1) does not hide them from the test.
            'echo "MUTATIONS:"; cat "$MUTATION_LOG"; rm -f "$MUTATION_LOG"',
        )
    )
    return subprocess.run([shell, "-c", command], capture_output=True, text=True)


# Shells the opt-in path is validated against: every distinct bash major
# version installed (the macOS system bash 3.2 and a newer bash when present),
# so a bash-4+ construct cannot regress 3.2.
_OBS_SHELLS = _BASH_SHELLS


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
    # The AWS CLI only renders the quoted log-group name in JSON output. When a
    # profile's default output is 'text' (or 'table'/'yaml'), an existing group
    # is rendered WITHOUT the quoted name a JSON parse needs. This mock emulates
    # that: 'describe-log-groups' returns JSON only when '--output json' is
    # requested, and a 'text' rendering otherwise. The reader parses the output
    # as JSON, so a 'text' rendering does not parse and is classified as
    # 'unreadable' (not 'absent') — dropping '--output json' from the reader
    # therefore fails the tests.
    spans_text = "LOGGROUPS\taws/spans\t1700000000000\t/aws/spans" if spans else ""
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
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then\n'
        '    case " $* " in\n'
        f"      *' --output json '*) echo '{spans_json}'; return 0;;\n"
        f'      *) printf "%s\\n" "{spans_text}"; return 0;;\n'
        "    esac\n"
        "  fi\n"
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


def test_account_obs_default_points_to_console_or_launch_when_xray_and_spans_absent():
    # Destination XRay, 'aws/spans' absent: the opt-in cannot create the group,
    # and a first-time enable FROM 'XRay' (XRay -> CloudWatchLogs) does not
    # create it either. So the default warning must point to the console or the
    # deploy's own launch + recheck, and must NOT print the XRay -> CloudWatchLogs
    # toggle pair or recommend the opt-in.
    result = _account_obs_harness("false", _obs_mock(destination="XRay", spans=False))
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stdout
    assert "agentcore launch" in result.stdout
    # No destination-toggle commands when the destination is XRay.
    assert "update-trace-segment-destination" not in result.stdout


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_default_stop_switch_does_not_promise_launch_when_xray_and_spans_absent(shell):
    # Destination XRay, 'aws/spans' absent, with the stop switch set to exactly
    # 'true'. The stop-switch preflight halts the deploy before 'agentcore
    # launch', so the Step 1.7 remedy must NOT promise that the launch switches
    # the destination or that a Step 5c recheck prints the toggle. It must point
    # to the one-time console enable instead.
    result = _account_obs_harness(
        "false",
        _obs_mock(destination="XRay", spans=False),
        extra_env="export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true",
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "The stop switch is set" in result.stdout
    assert "stops before" in result.stdout
    assert "'agentcore launch'" in result.stdout
    assert "AWS X-Ray / CloudWatch console" in result.stdout
    # The launch/Step 5c promise must be gone when the stop switch is set.
    assert "the post-launch recheck (Step 5c)" not in result.stdout
    assert "'agentcore launch' switches the" not in result.stdout
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stdout
    # No wording may promise that a launch (or "the deployment") reaches the
    # passing state: the stop switch halts before any launch, so nothing will
    # switch the destination.
    assert "nothing will switch the destination" in result.stdout
    assert "let the deployment" not in result.stdout.lower()
    assert "launch' switch it" not in result.stdout
    assert "launch' will switch" not in result.stdout


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_default_stop_switch_off_keeps_launch_wording(shell):
    # Any value other than exactly 'true' leaves the stop switch off, so the
    # default remedy keeps the launch + Step 5c wording.
    result = _account_obs_harness(
        "false",
        _obs_mock(destination="XRay", spans=False),
        extra_env="export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=TRUE",
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "The stop switch is set" not in result.stdout
    assert "agentcore launch" in result.stdout
    assert "the post-launch recheck (Step 5c)" in result.stdout


def test_account_obs_default_prints_toggle_when_cloudwatchlogs_and_spans_absent():
    # Destination already CloudWatchLogs but 'aws/spans' absent: a disable/
    # re-enable toggle (or the console) creates the group, so the toggle pair is
    # the correct remedy here. The opt-in is still not recommended (it cannot
    # create the group).
    result = _account_obs_harness("false", _obs_mock(destination="CloudWatchLogs", spans=False))
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stdout
    assert "update-trace-segment-destination --destination XRay" in result.stdout
    assert "update-trace-segment-destination --destination CloudWatchLogs" in result.stdout


def test_account_obs_default_reads_spans_with_json_output():
    # An existing 'aws/spans' group is only visible in JSON output. The default
    # path must request JSON so a profile whose default output is 'text' cannot
    # make a configured account read as 'absent'. The mock renders the group in
    # 'text' form unless '--output json' is requested; with JSON requested the
    # account reads as supported and no remedy is printed.
    result = _account_obs_harness("false", _obs_mock(destination="CloudWatchLogs", spans=True))
    assert "rc=0" in result.stdout
    assert "already supports Transaction Search" in result.stdout
    assert "update-trace-segment-destination" not in result.stdout


def test_account_obs_default_offers_opt_in_only_when_spans_present():
    # Destination XRay but 'aws/spans' already present: the opt-in can finish the
    # job (switch the destination / add the policy), so the opt-in is offered and
    # the full scope is listed.
    result = _account_obs_harness("false", _obs_mock(destination="XRay", spans=True))
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" in result.stdout
    # The "would change" scope lists all three shared settings before opt-in.
    assert "X-Ray trace segment destination" in result.stdout
    assert "X-Ray default indexing rule" in result.stdout
    assert "CloudWatch Logs resource policy" in result.stdout


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_default_stop_switch_opt_in_offer_names_matching_policy(shell):
    # Destination XRay, 'aws/spans' present, stop switch set to exactly 'true'.
    # The default-named opt-in writes 'GameAgentTransactionSearchXRayAccess',
    # which never satisfies the toolkit, so a stop-switch run would halt on the
    # policy name and leave the account looping. The Step 1.7 offer must instead
    # carry the matching policy name and state the no-other-grant condition (or
    # point to the stop-switch remedy), never the bare default-named opt-in.
    result = _account_obs_harness(
        "false",
        _obs_mock(destination="XRay", spans=True),
        extra_env="export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true",
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME=TransactionSearchXRayAccess" in result.stdout
    assert "no other" in result.stdout
    # The bare default-named opt-in line must not appear on its own: every
    # opt-in command printed here carries the matching policy name.
    for line in result.stdout.splitlines():
        if "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" in line and line.strip().endswith("./deploy-all.sh"):
            assert "GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME=TransactionSearchXRayAccess" in line


def test_account_obs_default_does_not_offer_opt_in_when_destination_unreadable_and_spans_present():
    # An unreadable destination with 'aws/spans' present must NOT offer the
    # opt-in. The opt-in refuses before any write when it cannot read the
    # destination, so recommending it would send the operator to a path that
    # fails. Treat it like the unreadable branch: confirm in the console, no
    # toggle commands.
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then return 254; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then\n'
        '    case " $* " in\n'
        '      *\' --output json \'*) echo \'{"logGroups":[{"logGroupName":"aws/spans"}]}\'; return 0;;\n'
        '      *) printf "%s\\n" "LOGGROUPS\taws/spans"; return 0;;\n'
        "    esac\n"
        "  fi\n"
        '  echo "MUTATION: $1 $2" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )
    result = _account_obs_harness("false", aws_mock)
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stdout
    assert "unknown (read failed)" in result.stdout
    assert "update-trace-segment-destination" not in result.stdout


def test_account_obs_default_reports_unreadable_spans_separately():
    # A denied/throttled describe is reported as 'unknown', not as 'absent', and
    # the opt-in is not recommended for an unknown state.
    mock = (
        "aws() {\n"
        '  if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi\n'
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then echo "CloudWatchLogs"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo "An error occurred (ThrottlingException)" >&2; return 254; fi\n'
        '  echo "MUTATION: $1 $2" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )
    result = _account_obs_harness("false", mock)
    assert "rc=0" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    assert "could not be read" in result.stdout
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stdout


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
    # It must refuse before any shared write when it cannot read the destination.
    assert "MUTATION:" not in result.stdout.split("MUTATIONS:", 1)[1]


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


def test_account_obs_optin_refuses_before_any_write_when_percent_is_invalid():
    """An invalid indexing percentage is validated before any shared write.

    ``GBAW_XRAY_DEFAULT_INDEXING_PERCENT=abc`` is rejected during the read-and-
    validate phase, so the opt-in refuses with zero mutations rather than
    leaving the resource policy or trace destination half-changed.
    """
    result = _account_obs_harness(
        "true",
        _obs_mock(destination="XRay", spans=True, indexing_percent="5"),
        extra_env='export GBAW_XRAY_DEFAULT_INDEXING_PERCENT="abc"',
    )
    assert "rc=1" in result.stdout
    assert "must be an integer in [0, 100]" in result.stderr
    assert "MUTATION:" not in result.stdout.split("MUTATIONS:", 1)[1]


def test_account_obs_optin_refuses_before_any_write_when_indexing_unreadable_and_change_requested():
    """An unreadable indexing rule with a change requested refuses before any write.

    When a percentage is requested, the current indexing rule is a hard
    prerequisite. If ``get-indexing-rules`` fails, the opt-in refuses during the
    read-and-validate phase with zero mutations.
    """
    aws_mock = (
        "aws() {\n"
        '  if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi\n'
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then echo "XRay"; return 0; fi\n'
        '  if [ "$1" = "xray" ] && [ "$2" = "get-indexing-rules" ]; then return 254; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-resource-policies" ]; then echo \'{"resourcePolicies":[]}\'; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{"logGroups":[]}\'; return 0; fi\n'
        '  echo "MUTATION: $1 $2" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )
    result = _account_obs_harness(
        "true",
        aws_mock,
        extra_env='export GBAW_XRAY_DEFAULT_INDEXING_PERCENT="10"',
    )
    assert "rc=1" in result.stdout
    assert "Unable to read the current X-Ray default indexing rule" in result.stderr
    assert "MUTATION:" not in result.stdout.split("MUTATIONS:", 1)[1]


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


@pytest.mark.parametrize("shell", _OBS_SHELLS)
@pytest.mark.parametrize("account_output", ["", "None", "not-an-account"])
def test_account_obs_optin_refuses_when_the_account_lookup_is_not_an_account_id(account_output, shell):
    """A lookup that does not return a 12-digit account fails before any shared write."""
    mock = _obs_mock(destination="XRay", spans=True).replace(
        'echo "123456789012"; return 0', f'echo "{account_output}"; return 0'
    )
    result = _account_obs_harness("true", mock, shell=shell)
    assert "rc=1" in result.stdout
    assert "identity lookup returned no account ID" in result.stderr
    assert "MUTATION:" not in result.stdout.split("MUTATIONS:", 1)[1]


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_optin_proceeds_when_identity_lookup_warns_on_stderr(shell):
    """A successful identity lookup that also prints a warning to stderr proceeds.

    The CLI may print a deprecation or TLS warning to stderr while still
    returning the account on stdout. Capturing the streams separately keeps the
    warning out of the 12-digit check, so the opt-in uses the real account and
    scopes the shared grant to it.
    """
    mock = _obs_mock(destination="XRay", spans=True).replace(
        'if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi',
        'if [ "$1" = "sts" ]; then echo "a deprecation notice" >&2; echo "123456789012"; return 0; fi',
    )
    result = _account_obs_harness("true", mock, shell=shell)
    assert "rc=0" in result.stdout
    assert "identity lookup returned no account ID" not in result.stderr
    assert "MUTATION: logs put-resource-policy" in result.stdout
    assert '"aws:SourceAccount": "123456789012"' in result.stdout


# ---------------------------------------------------------------------------
# AgentCore starter toolkit Transaction Search preflight (read-only prediction)
# ---------------------------------------------------------------------------
#
# `agentcore launch` runs the starter toolkit's enable_transaction_search_if_needed
# whenever runtime observability is enabled. These tests exercise the read-only
# preflight that predicts, using the toolkit's own conditions, which account-wide
# X-Ray / CloudWatch Logs settings the toolkit WOULD change, plus the fail-closed
# stop switch that halts the deploy before launch. All values are synthetic.


def _preflight_harness(
    aws_mock: str,
    extra_env: str = "",
    shell: str = "bash",
) -> "subprocess.CompletedProcess[str]":
    """Source setup-account-observability.sh and run the toolkit preflight."""
    command = "\n".join(
        (
            'export AWS_REGION="us-west-2"',
            extra_env,
            aws_mock,
            f'source "{ACCOUNT_OBS_SCRIPT}"',
            # Mirror the deploy.sh call site, which checks the status in an `if`
            # so a non-zero return is handled rather than aborting under `set -e`.
            'if run_transaction_search_preflight; then echo "rc=0"; else echo "rc=$?"; fi',
        )
    )
    return subprocess.run([shell, "-c", command], capture_output=True, text=True)


def _preflight_mock(
    *,
    destination: str = "XRay",
    toolkit_policy_present: bool = False,
    default_indexing_present: bool = True,
    project_grant_present: bool = False,
    destination_read_fails: bool = False,
    policies_read_fails: bool = False,
    indexing_read_fails: bool = False,
    spans: bool = False,
    spans_read_fails: bool = False,
) -> str:
    # describe-resource-policies feeds both the toolkit name-only check and the
    # project-owned grant annotation (find_existing_spans_policy).
    policy_entries = []
    if toolkit_policy_present:
        policy_entries.append(
            '{"policyName":"TransactionSearchXRayAccess",'
            '"policyDocument":"{\\"Version\\":\\"2012-10-17\\",\\"Statement\\":[]}"}'
        )
    if project_grant_present:
        policy_entries.append(
            '{"policyName":"GameAgentTransactionSearchXRayAccess",'
            '"policyDocument":"{\\"Version\\":\\"2012-10-17\\",\\"Statement\\":[{\\"Effect\\":\\"Allow\\",'
            '\\"Principal\\":{\\"Service\\":\\"xray.amazonaws.com\\"},\\"Action\\":\\"logs:PutLogEvents\\",'
            '\\"Resource\\":[\\"arn:aws:logs:us-west-2:123456789012:log-group:aws/spans:*\\"]}]}"}'
        )
    policies_json = '{"resourcePolicies":[' + ",".join(policy_entries) + "]}"
    indexing_json = (
        '{"IndexingRules":[{"Name":"Default","Rule":{"Probabilistic":{"DesiredSamplingPercentage":1}}}]}'
        if default_indexing_present
        else '{"IndexingRules":[]}'
    )
    dest_block = "    return 254" if destination_read_fails else f'    echo "{destination}"; return 0'
    policies_block = "    return 254" if policies_read_fails else f"    echo '{policies_json}'; return 0"
    indexing_block = "    return 254" if indexing_read_fails else f"    echo '{indexing_json}'; return 0"
    spans_json = '{"logGroups":[{"logGroupName":"aws/spans"}]}' if spans else '{"logGroups":[]}'
    spans_body = "    return 254" if spans_read_fails else f"echo '{spans_json}'; return 0"
    return (
        "aws() {\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then\n'
        f"{dest_block}\n"
        "  fi\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-resource-policies" ]; then\n'
        f"{policies_block}\n"
        "  fi\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-indexing-rules" ]; then\n'
        f"{indexing_block}\n"
        "  fi\n"
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then\n'
        '    case " $* " in\n'
        f"      *' --output json '*) {spans_body};;\n"
        "      *) return 0;;\n"
        "    esac\n"
        "  fi\n"
        '  echo "unexpected: $*" >&2; return 1\n'
        "}"
    )


_PREFLIGHT_SHELLS = _BASH_SHELLS


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_reports_no_change_in_one_line_when_toolkit_would_change_nothing(shell):
    """When the account already matches every toolkit condition, the preflight
    prints a single no-change line and no warning."""
    result = _preflight_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=True, default_indexing_present=True),
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "would make NO account-wide" in result.stdout
    assert "WARNING" not in result.stdout
    assert "WOULD" not in result.stdout


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_warns_when_toolkit_would_add_resource_policy(shell):
    """A missing 'TransactionSearchXRayAccess' policy is predicted as an add."""
    result = _preflight_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=False, default_indexing_present=True),
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "WARNING" in result.stdout
    assert "WOULD ADD 'TransactionSearchXRayAccess'" in result.stdout
    assert "AgentCore starter toolkit" in result.stdout


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_warns_when_toolkit_would_change_trace_destination(shell):
    """A non-CloudWatchLogs destination is predicted as a change to CloudWatchLogs."""
    result = _preflight_harness(
        _preflight_mock(destination="XRay", toolkit_policy_present=True, default_indexing_present=True),
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "WARNING" in result.stdout
    assert "WOULD CHANGE XRay -> CloudWatchLogs" in result.stdout


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_warns_when_toolkit_would_create_default_indexing_rule(shell):
    """No 'Default' indexing rule is predicted as an UpdateIndexingRule write."""
    result = _preflight_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=True, default_indexing_present=False),
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "WARNING" in result.stdout
    assert "WOULD SET 'Default' to 1% (UpdateIndexingRule)" in result.stdout


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_notes_project_grant_but_toolkit_still_adds_its_policy(shell):
    """When the project-owned policy already grants access but the toolkit's
    name-checked policy is absent, the warning says the toolkit still adds its own."""
    result = _preflight_harness(
        _preflight_mock(
            destination="CloudWatchLogs",
            toolkit_policy_present=False,
            project_grant_present=True,
            default_indexing_present=True,
        ),
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "WOULD ADD 'TransactionSearchXRayAccess'" in result.stdout
    assert "GameAgentTransactionSearchXRayAccess" in result.stdout
    assert "adds its own duplicate policy" in result.stdout


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_reports_unknown_not_no_change_on_read_failure(shell):
    """A failed read is reported as UNKNOWN (a possible change), never 'no change'."""
    result = _preflight_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=True, policies_read_fails=True),
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "UNKNOWN" in result.stdout
    assert "effect is UNKNOWN" in result.stdout
    assert "would make NO account-wide" not in result.stdout


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_stop_switch_halts_when_toolkit_would_change(shell):
    """The exact opt-in GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true stops before launch."""
    result = _preflight_harness(
        _preflight_mock(destination="XRay", toolkit_policy_present=False, default_indexing_present=True),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert "rc=1" in result.stdout
    assert "Stopping before 'agentcore launch'" in result.stderr


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_stop_switch_remedy_leads_with_console_when_spans_absent(shell):
    """When the stop switch halts and 'aws/spans' is absent, the remedy must lead
    with the one-time console enable and must NOT recommend the opt-in run: the
    opt-in cannot create 'aws/spans', so without it the opt-in writes the policy,
    switches the destination, and still stops at Step 1.7."""
    result = _preflight_harness(
        _preflight_mock(destination="XRay", toolkit_policy_present=False, default_indexing_present=True, spans=False),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert "rc=1" in result.stdout
    assert "Stopping before 'agentcore launch'" in result.stderr
    assert "AWS X-Ray / CloudWatch console" in result.stderr
    # The opt-in route must not be offered when 'aws/spans' is absent.
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stderr


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_stop_switch_remedy_offers_opt_in_only_when_spans_present(shell):
    """When 'aws/spans' already exists, the opt-in can finish the job (switch the
    destination / add the policy), so the remedy may offer the opt-in route in
    addition to the console."""
    result = _preflight_harness(
        _preflight_mock(destination="XRay", toolkit_policy_present=False, default_indexing_present=True, spans=True),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert "rc=1" in result.stdout
    assert "Stopping before 'agentcore launch'" in result.stderr
    assert "AWS X-Ray / CloudWatch console" in result.stderr
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" in result.stderr
    # The offered opt-in command must carry the matching policy name so it
    # actually satisfies the toolkit's name-only check.
    assert "GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME=TransactionSearchXRayAccess" in result.stderr
    # The route works only on an account with no other 'aws/spans' grant, so the
    # remedy must state that condition rather than offer the opt-in unqualified.
    assert "no other" in result.stderr


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_stop_switch_halts_on_unknown_read_failure(shell):
    """Fail-closed: the stop switch also halts when the toolkit's effect is UNKNOWN."""
    result = _preflight_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=True, indexing_read_fails=True),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert "rc=1" in result.stdout
    assert "Stopping before 'agentcore launch'" in result.stderr


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_stop_switch_remedy_does_not_offer_opt_in_when_spans_unreadable(shell):
    """When the 'aws/spans' read itself fails, its state is unknown, so the
    remedy must NOT offer the opt-in route (which is correct only when the group
    is known to exist). It must lead with the console instead."""
    result = _preflight_harness(
        _preflight_mock(
            destination="XRay",
            toolkit_policy_present=False,
            default_indexing_present=True,
            spans_read_fails=True,
        ),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert "rc=1" in result.stdout
    assert "Stopping before 'agentcore launch'" in result.stderr
    assert "AWS X-Ray / CloudWatch console" in result.stderr
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stderr


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_stop_switch_remedy_never_promises_a_launch(shell):
    """The stop switch halts before 'agentcore launch', so the remedy must never
    tell the operator to let a launch reach the passing state — not when
    'aws/spans' is absent and not when it is present."""
    for spans in (False, True):
        result = _preflight_harness(
            _preflight_mock(
                destination="XRay",
                toolkit_policy_present=False,
                default_indexing_present=True,
                spans=spans,
            ),
            extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
            shell=shell,
        )
        assert "rc=1" in result.stdout
        # No wording that promises a launch will finish the job.
        assert "'agentcore launch' switch" not in result.stderr
        assert "let the deployment" not in result.stderr.lower()
        assert "will switch" not in result.stderr.lower()


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_stop_switch_unknown_read_remedy_leads_with_restoring_the_read(shell):
    """When the halt is caused only by an unreadable state (for example expired
    credentials), the remedy must lead with restoring the read rather than the
    console-enable guidance for a definite change."""
    result = _preflight_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=True, policies_read_fails=True),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert "rc=1" in result.stdout
    assert "Stopping before 'agentcore launch'" in result.stderr
    # Lead with the read fix: credentials/permissions, not a console enable.
    assert "could not be read" in result.stderr
    assert "credentials" in result.stderr or "permission" in result.stderr


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_preflight_stop_switch_does_not_halt_when_no_change(shell):
    """The stop switch is a no-op when the toolkit would change nothing."""
    result = _preflight_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=True, default_indexing_present=True),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "would make NO account-wide" in result.stdout


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
@pytest.mark.parametrize("stop_value", ["TRUE", "1", "yes", "True", " true", ""])
def test_preflight_stop_switch_only_exact_true_enables(stop_value, shell):
    """Any non-exact value of the stop switch is treated as off (deploy continues)."""
    result = _preflight_harness(
        _preflight_mock(destination="XRay", toolkit_policy_present=False, default_indexing_present=True),
        extra_env=f'export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="{stop_value}"',
        shell=shell,
    )
    assert "rc=0" in result.stdout
    assert "WARNING" in result.stdout
    assert "Stopping before" not in result.stderr


# ---------------------------------------------------------------------------
# deploy.sh wiring: the preflight runs before `agentcore launch`, and the stop
# switch prevents the launch from being invoked at all.
# ---------------------------------------------------------------------------
#
# These tests assert the integration contract at the launch call sites in
# scripts/deploy.sh without running the full deployment: the preflight output
# must precede any launch, and GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true
# must stop before `agentcore launch` is ever called.


def test_deploy_guards_both_agentcore_launch_sites_with_the_preflight():
    """Static check: every `agentcore launch` in deploy.sh is immediately
    preceded by the subprocess preflight guard, so no launch path skips it, and
    deploy.sh does not source the account-observability script (which would leak
    its `set -euo pipefail` into the deploy's shell options)."""
    content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    # deploy.sh must run the preflight as a subprocess, never source it.
    assert (
        "source" not in content or "setup-account-observability.sh" not in content.split("source", 1)[1][:80]
    ), "deploy.sh must not source setup-account-observability.sh"
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not (
            stripped.startswith("source ") and "setup-account-observability" in stripped
        ), "deploy.sh must call the preflight as a subprocess, not source it"
    lines = content.splitlines()
    launch_lines = [
        i for i, line in enumerate(lines) if "agentcore launch" in line and not line.lstrip().startswith("#")
    ]
    assert launch_lines, "expected at least one agentcore launch in deploy.sh"
    for idx in launch_lines:
        # Build the preceding window from NON-COMMENT lines only, so a
        # commented-out guard above a launch cannot satisfy the check. Scan a
        # wider span and keep the last 6 non-comment lines so interleaved
        # comments do not push the real guard out of view.
        window_lines = [l for l in lines[max(0, idx - 12) : idx] if not l.lstrip().startswith("#")][-6:]
        window = "\n".join(window_lines)
        assert "run_toolkit_preflight" in window, f"agentcore launch at line {idx + 1} is not guarded by the preflight"
        # The guard must be fail-closed: `if ! run_toolkit_preflight; then exit 1`.
        # A neutralized guard (e.g. `run_toolkit_preflight || true`) still contains
        # the call but does not stop the launch, so reject it explicitly.
        assert (
            "if ! run_toolkit_preflight" in window
        ), f"agentcore launch at line {idx + 1} must use the fail-closed `if ! run_toolkit_preflight` guard"
        assert (
            "run_toolkit_preflight || true" not in window
        ), f"agentcore launch at line {idx + 1} has a neutralized (|| true) preflight guard"


def _launch_wiring_harness(
    aws_mock: str,
    extra_env: str = "",
    shell: str = "bash",
) -> "subprocess.CompletedProcess[str]":
    """Reproduce the deploy.sh launch call site: source the helpers, then run
    the exact guard idiom with a stubbed `agentcore launch`."""
    command = "\n".join(
        (
            "set -e",
            'export AWS_REGION="us-west-2"',
            extra_env,
            aws_mock,
            # Stub the launch so no AWS call is made; record that it ran.
            'uv() { echo "LAUNCH_INVOKED: agentcore $*"; }',
            f'source "{ACCOUNT_OBS_SCRIPT}"',
            # The exact guard used in scripts/deploy.sh before `agentcore launch`.
            "if ! run_transaction_search_preflight; then exit 1; fi",
            "uv run agentcore launch --auto-update-on-conflict",
        )
    )
    return subprocess.run([shell, "-c", command], capture_output=True, text=True)


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_launch_wiring_preflight_output_precedes_launch(shell):
    """The preflight report is printed before `agentcore launch` is invoked."""
    result = _launch_wiring_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=True, default_indexing_present=True),
        shell=shell,
    )
    assert "LAUNCH_INVOKED" in result.stdout
    preflight_pos = result.stdout.index("Preflight:")
    launch_pos = result.stdout.index("LAUNCH_INVOKED")
    assert preflight_pos < launch_pos


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_launch_wiring_stop_switch_prevents_launch(shell):
    """With the stop switch and a predicted change, `agentcore launch` is never invoked."""
    result = _launch_wiring_harness(
        _preflight_mock(destination="XRay", toolkit_policy_present=False, default_indexing_present=True),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert result.returncode == 1
    assert "LAUNCH_INVOKED" not in result.stdout
    assert "Stopping before 'agentcore launch'" in result.stderr


@pytest.mark.parametrize("shell", _PREFLIGHT_SHELLS)
def test_launch_wiring_proceeds_to_launch_when_no_change(shell):
    """With no predicted change, the deploy proceeds to `agentcore launch`."""
    result = _launch_wiring_harness(
        _preflight_mock(destination="CloudWatchLogs", toolkit_policy_present=True, default_indexing_present=True),
        extra_env='export GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="true"',
        shell=shell,
    )
    assert result.returncode == 0
    assert "LAUNCH_INVOKED" in result.stdout


# ---------------------------------------------------------------------------
# End-to-end deploy.sh harness: run the REAL scripts/deploy.sh over PATH stubs,
# with and without Docker, and under both shells. This is the only test that
# exercises how deploy.sh integrates the preflight (as a subprocess) and that a
# backend-only deploy completes. It catches a sourced-script option leak and a
# missing preflight guard, which the isolated-function tests above cannot.
# ---------------------------------------------------------------------------
#
# The harness builds a throwaway project tree whose scripts/deploy.sh and
# scripts/infrastructure/setup-account-observability.sh are the real files, while
# every other sub-script is a no-op. A stubbed `aws`, `uv`, and `yq` on PATH keep
# the run fully offline; the `uv` stub records every `agentcore launch` so a test
# can assert whether a launch happened. All identifiers are synthetic.

_AWS_STUB = r"""#!/usr/bin/env bash
# Offline `aws` stub. Answers the few reads deploy.sh and the preflight make.
svc="$1"; op="$2"
_query=""
for a in "$@"; do
  case "$prev" in --query) _query="$a";; esac
  prev="$a"
done
case "$svc $op" in
  "sts get-caller-identity") echo "123456789012";;
  "cloudformation deploy") :;;
  "cloudformation describe-stacks")
    case "$_query" in
      *UserPoolId*) echo "us-west-2_EXAMPLE";;
      *UserPoolClientId*) echo "exampleclientid000000000000";;
      *GuardrailId*) echo "gr-example0000";;
      *CostReportSnapshotTableName*) echo "game-agent-cost-snapshots";;
      *AgentCoreExecutionRoleArn*) echo "arn:aws:iam::123456789012:role/example-exec";;
      *FrontendRepositoryUri*) echo "123456789012.dkr.ecr.us-west-2.amazonaws.com/example";;
      *ServiceUrl*) echo "example.us-west-2.elb.amazonaws.com";;
      *LoadBalancerArn*) echo "arn:aws:elasticloadbalancing:us-west-2:123456789012:loadbalancer/app/example/00";;
      *WebACLArn*) echo "arn:aws:wafv2:us-west-2:123456789012:regional/webacl/example/00";;
      *CloudTrailArn*) echo "arn:aws:cloudtrail:us-west-2:123456789012:trail/example";;
      *) echo "None";;
    esac
    ;;
  "logs describe-resource-policies") echo "$STUB_POLICIES_JSON";;
  "xray get-trace-segment-destination")
    # Normally a fixed value. When STUB_TRACE_DEST_FIRST is set, the first
    # STUB_TRACE_DEST_FIRST_COUNT reads (default 1) return it and every later
    # read returns $STUB_TRACE_DESTINATION, using a call counter file. This lets
    # a test keep the up-front reads compliant (so the deploy proceeds past
    # Step 2) while a later per-launch re-read sees a change, exercising the
    # launch-site guard's halting path.
    if [ -n "${STUB_TRACE_DEST_FIRST:-}" ]; then
      _tc="${STUB_TRACE_DEST_COUNTER:-/tmp/stub_trace_dest_counter_unset}"
      _tfirst="${STUB_TRACE_DEST_FIRST_COUNT:-1}"
      _tn=0
      [ -f "$_tc" ] && _tn="$(cat "$_tc")"
      _tn=$((_tn + 1))
      echo "$_tn" > "$_tc"
      if [ "$_tn" -le "$_tfirst" ]; then echo "$STUB_TRACE_DEST_FIRST"; else echo "$STUB_TRACE_DESTINATION"; fi
    else
      echo "$STUB_TRACE_DESTINATION"
    fi
    ;;
  "xray get-indexing-rules") echo "$STUB_INDEXING_JSON";;
  "logs put-delivery-source") :;;
  "logs put-delivery-destination") :;;
  "logs create-delivery") :;;
  "logs get-delivery-destination") echo "${STUB_DELIVERY_DEST_ARN:-arn:aws:logs:us-west-2:123456789012:delivery-destination:example}";;
  "logs describe-deliveries")
    printf '{"deliveries":[{"deliverySourceName":"%s","deliveryDestinationArn":"%s","deliveryDestinationType":"XRAY"}]}' \
      "${STUB_DELIVERY_SOURCE_NAME}" "${STUB_DELIVERY_DEST_ARN:-arn:aws:logs:us-west-2:123456789012:delivery-destination:example}"
    ;;
  "logs get-delivery-source")
    printf '{"deliverySource":{"resourceArns":["%s"]}}' "${STUB_RUNTIME_ARN_RESOLVED}"
    ;;
  "elbv2 modify-load-balancer-attributes") echo "190";;
  "inspector2 enable") :;;
  "ecr get-login-password") echo "example-token";;
  "wafv2 get-web-acl-for-resource") echo "${STUB_WAF_ACL_ARN:-arn:aws:wafv2:us-west-2:123456789012:regional/webacl/example/00}";;
  "wafv2 associate-web-acl") :;;
  "wafv2 get-web-acl")
    # All six rules the reconciler requires, so convergence succeeds first pass.
    printf 'RateLimitAuthPaths\tRateLimitAdminPaths\tRateLimitPerIP\tAWSManagedRulesCommonRuleSet\tAWSManagedRulesSQLiRuleSet\tAWSManagedRulesKnownBadInputsRuleSet\n'
    ;;
  *) echo '{}';;
esac
exit 0
"""

# `uv` stub. Records `agentcore launch` lines to $STUB_LAUNCH_LOG, no-ops the
# rest, and emits the model/identity exports deploy.sh evals.
_UV_STUB = r"""#!/usr/bin/env bash
args="$*"
case "$args" in
  *"agentcore launch"*)
    echo "LAUNCH_INVOKED: uv $args"
    echo "uv $args" >> "$STUB_LAUNCH_LOG"
    ;;
  *"agentcore configure"*) echo "configured";;
  "sync"*) :;;
  "export "*)
    # deploy.sh runs: uv export ... --output-file requirements.txt.tmp
    out=""
    prev=""
    for a in "$@"; do
      case "$prev" in --output-file) out="$a";; esac
      prev="$a"
    done
    [ -n "$out" ] && printf '# generated\nexample-package==1.0.0\n' > "$out"
    ;;
  *"load_deployment_settings.py"*)
    case "$args" in
      *--models-only*)
        echo 'export GBAW_ORCHESTRATOR_MODEL_ID=example-orchestrator'
        echo 'export GBAW_SPECIALIST_MODEL_ID=example-specialist'
        ;;
      *--identity-only*)
        echo 'export GBAW_TENANT_ID=example-tenant'
        echo 'export GBAW_WORKSPACE_ID=example-workspace'
        ;;
    esac
    ;;
  *) :;;
esac
exit 0
"""

# `yq` stub. deploy.sh reads the runtime ARN from .bedrock_agentcore.yaml. An
# empty value makes deploy.sh take the "new runtime" (launch) path; a non-empty
# value makes it skip the first launch.
_YQ_STUB = r"""#!/usr/bin/env bash
# deploy.sh reads the runtime ARN from .bedrock_agentcore.yaml twice on the
# new-runtime path: first to check existence (must be empty so it launches),
# then after launch to resolve the ARN (must be the real value). A call counter
# distinguishes the two so the harness exercises the full launch path AND the
# downstream trace-delivery verification with a resolvable runtime ARN.
_c="${STUB_YQ_COUNTER:-/tmp/stub_yq_counter_unset}"
n=0
[ -f "$_c" ] && n="$(cat "$_c")"
n=$((n + 1))
echo "$n" > "$_c"
if [ "$n" -eq 1 ] && [ "${STUB_RUNTIME_ARN:-}" = "" ]; then
  echo ""
else
  echo "${STUB_RUNTIME_ARN:-arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/gameagentruntime-EXAMPLE}"
fi
exit 0
"""

_DOCKER_STUB = r"""#!/usr/bin/env bash
# `docker info` must succeed for deploy.sh to treat Docker as available. Drain
# stdin so a piped `docker login --password-stdin` never races the writer into
# SIGPIPE under `pipefail` (which would make deploy.sh exit 141).
cat >/dev/null 2>&1
exit 0
"""

# "Docker absent" stub. deploy.sh decides Docker is available with
# `command -v docker && docker info`. A CI runner has a real docker on
# /usr/bin, so simply omitting a stub lets the Docker-absent cases pick up the
# real daemon and attempt a real ECR login. This stub, placed first on PATH,
# shadows any system docker and fails `info` so `DOCKER_AVAILABLE=false` is
# deterministic. Any non-`info` subcommand also fails, so nothing reaches a
# real daemon. stdin is drained for symmetry with the available stub.
_DOCKER_STUB_UNAVAILABLE = r"""#!/usr/bin/env bash
cat >/dev/null 2>&1
exit 1
"""


def _write_stub(path: "pathlib.Path", body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _build_deploy_tree(tmp_path: "pathlib.Path") -> "pathlib.Path":
    """Create a throwaway project tree whose deploy.sh and the account-observability
    script are the real files and every other sub-script is a no-op."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "infrastructure").mkdir(parents=True)
    (repo / "infrastructure" / "cloudformation").mkdir(parents=True)
    (repo / "backend").mkdir(parents=True)
    (repo / "config").mkdir(parents=True)
    (repo / "ui").mkdir(parents=True)

    # Real scripts under test.
    (repo / "scripts" / "deploy.sh").write_text(DEPLOY_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "scripts" / "infrastructure" / "setup-account-observability.sh").write_text(
        ACCOUNT_OBS_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )

    # No-op sub-scripts deploy.sh invokes by path. deploy-kb.sh additionally
    # drops a marker file so a test can assert whether the Knowledge Base step
    # was reached (it must NOT be, when the stop switch halts before Step 2).
    noop = "#!/usr/bin/env bash\nexit 0\n"
    kb_marker = repo / "kb_ran.marker"
    for rel in (
        "scripts/infrastructure/download-kb-docs.sh",
        "scripts/infrastructure/deploy-prompts.sh",
        "scripts/infrastructure/seed-kb-gamelift.sh",
        "scripts/infrastructure/seed-kb-eks.sh",
        "scripts/infrastructure/seed-kb-cost.sh",
        "scripts/infrastructure/setup-app-observability.sh",
        "scripts/generate-sbom.sh",
    ):
        p = repo / rel
        p.write_text(noop, encoding="utf-8")
        p.chmod(0o755)
    kb = repo / "scripts/infrastructure/deploy-kb.sh"
    kb.write_text(f'#!/usr/bin/env bash\ntouch "{kb_marker}"\nexit 0\n', encoding="utf-8")
    kb.chmod(0o755)

    # Template files referenced via --template-file (the aws stub ignores them).
    for name in (
        "00-solution-tracking.yaml",
        "01-base-infrastructure.yaml",
        "02-frontend-ecs-express.yaml",
        "03-agentcore-observability.yaml",
        "04-bedrock-guardrails.yaml",
        "05-security-infrastructure.yaml",
    ):
        (repo / "infrastructure" / "cloudformation" / name).write_text("", encoding="utf-8")

    # Minimal generated state deploy.sh reads.
    (repo / "backend" / ".env.local").write_text(
        "GBAW_ORCHESTRATOR_PROMPT_ARN=arn:aws:bedrock:us-west-2:123456789012:prompt/example-orch\n"
        "GBAW_GAMELIFT_PROMPT_ARN=arn:aws:bedrock:us-west-2:123456789012:prompt/example-gl\n"
        "GBAW_EKS_PROMPT_ARN=arn:aws:bedrock:us-west-2:123456789012:prompt/example-eks\n"
        "GBAW_COST_PROMPT_ARN=arn:aws:bedrock:us-west-2:123456789012:prompt/example-cost\n"
        "GBAW_GAMELIFT_KB_ID=EXAMPLEKB01\n"
        "GBAW_EKS_KB_ID=EXAMPLEKB02\n"
        "GBAW_COST_KB_ID=EXAMPLEKB03\n",
        encoding="utf-8",
    )
    (repo / "backend" / ".bedrock_agentcore.yaml").write_text(
        "agents:\n  gameagentruntime:\n    bedrock_agentcore:\n      agent_arn: ''\n",
        encoding="utf-8",
    )
    # deploy.sh greps an existing requirements.txt to diff it against the
    # freshly exported one. Provide an (empty) file so the grep reads a real
    # file instead of printing "requirements.txt: No such file or directory" to
    # stderr; the diff against the uv stub's export is non-empty, so deploy.sh
    # takes its normal "out of sync, updating" path.
    (repo / "backend" / "requirements.txt").write_text("", encoding="utf-8")
    (repo / "config" / "load_deployment_settings.py").write_text("", encoding="utf-8")
    (repo / "ui" / ".env.local").write_text("AWS_PROFILE=\n", encoding="utf-8")
    return repo


def _run_deploy_e2e(
    tmp_path: "pathlib.Path",
    *,
    shell: str = "/bin/bash",
    docker: bool = False,
    stop_switch: bool = False,
    runtime_exists: bool = False,
    trace_destination: str = "CloudWatchLogs",
    trace_dest_first: str = "",
    trace_dest_first_count: int = 1,
    policies_json: str = '{"resourcePolicies":[{"policyName":"TransactionSearchXRayAccess","policyDocument":"{}"}]}',
    indexing_json: str = '{"IndexingRules":[{"Name":"Default"}]}',
) -> "subprocess.CompletedProcess[str]":
    repo = _build_deploy_tree(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_stub(bindir / "aws", _AWS_STUB)
    _write_stub(bindir / "uv", _UV_STUB)
    _write_stub(bindir / "yq", _YQ_STUB)
    _write_stub(bindir / "jq", "#!/usr/bin/env bash\n" + "printf '{\"customJWTAuthorizer\":{}}'\n")
    # Neutralize real waits so the harness runs well under the pytest timeout.
    _write_stub(bindir / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    # Always install a docker stub ahead of the system PATH so a real docker on
    # a CI runner's /usr/bin can never satisfy deploy.sh's
    # `command -v docker && docker info` probe. With docker=True the stub
    # reports available; with docker=False it fails `info`, so the harness is
    # hermetic on hosts with and without a real Docker daemon.
    _write_stub(bindir / "docker", _DOCKER_STUB if docker else _DOCKER_STUB_UNAVAILABLE)

    # deploy.sh runs nested `bash "$SCRIPT_DIR/.../setup-account-observability.sh"`
    # calls. Put a directory containing the parametrized shell (named `bash`)
    # ahead of /usr/bin so those nested calls use the SAME bash version this
    # case runs under, rather than the system bash. The stub bin dir still comes
    # first so the mocked `aws`/`uv`/`yq` win.
    shelldir = tmp_path / "shell"
    shelldir.mkdir()
    (shelldir / "bash").symlink_to(shell)

    launch_log = tmp_path / "launches.log"
    launch_log.write_text("", encoding="utf-8")

    resolved_arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/gameagentruntime-EXAMPLE"
    runtime_id = resolved_arn.rsplit("/", 1)[-1]

    env = {
        "PATH": f"{bindir}:{shelldir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AWS_REGION": "us-west-2",
        "STUB_LAUNCH_LOG": str(launch_log),
        "STUB_TRACE_DESTINATION": trace_destination,
        "STUB_POLICIES_JSON": policies_json,
        "STUB_INDEXING_JSON": indexing_json,
        # When runtime_exists, yq reports a non-empty ARN on the FIRST read, so
        # deploy.sh skips the first (new-runtime) launch and only Step 5b runs.
        "STUB_RUNTIME_ARN": resolved_arn if runtime_exists else "",
        "STUB_RUNTIME_ARN_RESOLVED": resolved_arn,
        "STUB_DELIVERY_SOURCE_NAME": f"{runtime_id}-traces-source",
        "STUB_DELIVERY_DEST_ARN": "arn:aws:logs:us-west-2:123456789012:delivery-destination:example",
        "STUB_YQ_COUNTER": str(tmp_path / "yq_counter"),
        # Keep bounded-retry verification fast and single-pass.
        "GBAW_DELIVERY_MAX_ATTEMPTS": "1",
        "GBAW_DELIVERY_RETRY_SECONDS": "0",
    }
    if trace_dest_first:
        env["STUB_TRACE_DEST_FIRST"] = trace_dest_first
        env["STUB_TRACE_DEST_FIRST_COUNT"] = str(trace_dest_first_count)
        env["STUB_TRACE_DEST_COUNTER"] = str(tmp_path / "trace_dest_counter")
    if stop_switch:
        env["GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE"] = "true"

    result = subprocess.run(
        [shell, str(repo / "scripts" / "deploy.sh")],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    # Attach the recorded launches for assertions.
    result.launches = launch_log.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    return result


_E2E_SHELLS = _BASH_SHELLS


@pytest.mark.parametrize("shell", _E2E_SHELLS)
@pytest.mark.parametrize("docker", [False, True])
@pytest.mark.parametrize("runtime_exists", [False, True])
def test_deploy_e2e_default_completes_and_launches(tmp_path, shell, docker, runtime_exists):
    """A default deploy completes (exit 0), with and without Docker, on both the
    new-runtime and existing-runtime paths. The Docker-absent (backend-only)
    path is documented as supported; it must not abort at the final summary."""
    result = _run_deploy_e2e(tmp_path, shell=shell, docker=docker, runtime_exists=runtime_exists)
    assert result.returncode == 0, f"stdout tail:\n{result.stdout[-800:]}\nstderr:\n{result.stderr[-800:]}"
    # New-runtime path launches twice (initial launch + Step 5b update); the
    # existing-runtime path skips the first launch and only Step 5b runs.
    expected_launches = 1 if runtime_exists else 2
    assert result.launches.count("agentcore launch") == expected_launches  # type: ignore[attr-defined]
    if docker:
        assert "Deployment Complete!" in result.stdout
    else:
        assert "Deployment Partially Complete (backend only)" in result.stdout


@pytest.mark.parametrize("shell", _E2E_SHELLS)
@pytest.mark.parametrize("docker", [False, True])
def test_deploy_e2e_default_prints_exactly_one_no_change_line_per_launch(tmp_path, shell, docker):
    """On a compliant account (nothing predicted to change), each `agentcore
    launch` is preceded by exactly one no-change preflight line and no WARNING
    or per-item WOULD detail is printed."""
    result = _run_deploy_e2e(tmp_path, shell=shell, docker=docker)
    assert result.returncode == 0
    launches = result.launches.count("agentcore launch")  # type: ignore[attr-defined]
    no_change_lines = result.stdout.count("would make NO account-wide")
    assert no_change_lines == launches == 2
    # No toolkit-preflight change was predicted, so none of its per-item change
    # markers appear. (The Step 1.7 default-mode report may mention the opt-in
    # separately; scope this to the toolkit preflight's own WOULD markers.)
    assert "WARNING: 'agentcore launch'" not in result.stdout
    assert "WOULD ADD" not in result.stdout
    assert "WOULD CHANGE" not in result.stdout
    assert "WOULD SET" not in result.stdout


@pytest.mark.parametrize("shell", _E2E_SHELLS)
@pytest.mark.parametrize("runtime_exists", [False, True])
def test_deploy_e2e_stop_switch_halts_before_any_launch(tmp_path, shell, runtime_exists):
    """With the stop switch set and a predicted change, the deploy exits non-zero
    before the Step 2 banner, records zero `agentcore launch` calls, and never
    reaches the Knowledge Base step — on both runtime paths."""
    result = _run_deploy_e2e(
        tmp_path,
        shell=shell,
        runtime_exists=runtime_exists,
        stop_switch=True,
        trace_destination="XRay",
        policies_json='{"resourcePolicies":[]}',
        indexing_json='{"IndexingRules":[]}',
    )
    assert result.returncode != 0
    assert result.launches.count("agentcore launch") == 0  # type: ignore[attr-defined]
    assert "Stopping before 'agentcore launch'" in result.stderr
    # The early stop-switch preflight halts BEFORE Step 2 and the KB steps.
    assert "Step 2: Launching AgentCore Runtime" not in result.stdout
    assert not (tmp_path / "repo" / "kb_ran.marker").exists()


@pytest.mark.parametrize("shell", _E2E_SHELLS)
@pytest.mark.parametrize("runtime_exists", [False, True])
def test_deploy_e2e_stop_switch_with_nothing_predicted_completes(tmp_path, shell, runtime_exists):
    """With the stop switch set but a compliant account (nothing predicted to
    change), the deploy is a full refresh: it exits 0 and reaches its launches."""
    result = _run_deploy_e2e(tmp_path, shell=shell, runtime_exists=runtime_exists, stop_switch=True)
    assert result.returncode == 0, f"stderr:\n{result.stderr[-800:]}"
    expected_launches = 1 if runtime_exists else 2
    assert result.launches.count("agentcore launch") == expected_launches  # type: ignore[attr-defined]


@pytest.mark.parametrize("shell", _E2E_SHELLS)
def test_deploy_e2e_preflight_output_precedes_each_launch(tmp_path, shell):
    """The preflight report is printed before each `agentcore launch`."""
    result = _run_deploy_e2e(tmp_path, shell=shell)
    assert result.returncode == 0
    out = result.stdout
    first_launch = out.index("LAUNCH_INVOKED")
    first_preflight = out.index("Preflight:")
    assert first_preflight < first_launch
    # Every launch is preceded by at least one preflight line before it.
    assert out.count("Preflight:") >= out.count("LAUNCH_INVOKED")


@pytest.mark.parametrize("shell", _E2E_SHELLS)
def test_deploy_e2e_stop_switch_halts_at_launch_guard_when_state_changes_later(tmp_path, shell):
    """A per-launch guard still halts when a later read differs from the up-front
    one. The trace destination reads 'CloudWatchLogs' on the FIRST read (so the
    up-front stop-switch preflight sees a compliant account and the deploy passes
    Step 2) and 'XRay' on every later read, so the first launch-site guard
    predicts a change and halts. With the stop switch set this exercises the
    launch-site guard's halting path (not the up-front block): exit non-zero,
    zero launches, but the Step 2 banner IS present."""
    result = _run_deploy_e2e(
        tmp_path,
        shell=shell,
        stop_switch=True,
        trace_dest_first="CloudWatchLogs",
        # The two up-front trace-destination reads (the Step 1.7 default-mode
        # report and the up-front stop-switch preflight) see a compliant
        # account; the next read, inside the first launch-site guard, sees the
        # change and halts.
        trace_dest_first_count=2,
        trace_destination="XRay",
    )
    assert result.returncode != 0
    assert result.launches.count("agentcore launch") == 0  # type: ignore[attr-defined]
    # The up-front preflight passed, so the deploy reached Step 2 before the
    # launch-site guard halted it.
    assert "Step 2: Launching AgentCore Runtime" in result.stdout
    assert "Stopping before 'agentcore launch'" in result.stderr


# ===========================================================================
# Account-wide observability default/opt-in and post-launch recheck
# ===========================================================================


# ---------------------------------------------------------------------------
# Account-wide observability opt-in: a first-time enable cannot create the
# AWS-reserved 'aws/spans' log group, so the opt-in must fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_optin_fails_when_spans_absent_after_enable(shell):
    """A first-time API enable (destination XRay -> CloudWatchLogs) does not
    create the AWS-reserved 'aws/spans' log group (starter-toolkit issue 457).
    The opt-in enables additively and never toggles, so when 'aws/spans' is still
    absent after the enable it must fail with the one-time manual step, not exit 0
    with a soft warning that reports success it did not achieve.
    """
    result = _account_obs_harness("true", _obs_mock(destination="XRay", spans=False), shell=shell)
    assert "rc=1" in result.stdout
    # The destination enable still happens, but the run fails on the missing group.
    assert "MUTATION: xray update-trace-segment-destination" in result.stdout
    assert "'aws/spans' log group does not exist" in result.stderr
    # The one-time remediation names both the console and the toggle path.
    assert "update-trace-segment-destination --destination XRay" in result.stderr
    assert "Account-wide observability configured" not in result.stdout


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_account_obs_optin_fails_with_unreadable_message_when_spans_read_fails(shell):
    """A denied or throttled 'logs:DescribeLogGroups' must be reported as
    'unreadable' with an accurate message, not diagnosed as a missing group."""
    mock = (
        "aws() {\n"
        '  if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi\n'
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then\n'
        '    case " $* " in\n'
        '      *" Status "*) echo "ACTIVE"; return 0;;\n'
        '      *) echo "XRay"; return 0;;\n'
        "    esac\n"
        "  fi\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-indexing-rules" ]; then echo "5"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo "An error occurred (AccessDeniedException)" >&2; return 254; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-resource-policies" ]; then echo \'{"resourcePolicies":[]}\'; return 0; fi\n'
        '  echo "MUTATION: $1 $2 ${3:-}" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )
    result = _account_obs_harness("true", mock, shell=shell)
    assert "rc=1" in result.stdout
    assert "could not be read" in result.stderr
    assert "does not exist" not in result.stderr
    assert "Account-wide observability configured" not in result.stdout


# ---------------------------------------------------------------------------
# Post-launch Transaction Search recheck (read-only, non-blocking)
# ---------------------------------------------------------------------------


def _recheck_harness(aws_mock: str, shell: str = "bash") -> "subprocess.CompletedProcess[str]":
    """Run the script's read-only 'report-transaction-search' mode."""
    command = "\n".join(
        (
            'export AWS_REGION="us-west-2"',
            'MUTATION_LOG="$(mktemp)"; export MUTATION_LOG',
            aws_mock,
            "export -f aws",
            f'{shell} "{ACCOUNT_OBS_SCRIPT}" report-transaction-search; echo "rc=$?"',
            'echo "MUTATIONS:"; cat "$MUTATION_LOG"; rm -f "$MUTATION_LOG"',
        )
    )
    return subprocess.run([shell, "-c", command], capture_output=True, text=True)


def _recheck_mock(*, destination: str = "CloudWatchLogs", spans: bool = True) -> str:
    spans_json = '{"logGroups":[{"logGroupName":"aws/spans"}]}' if spans else '{"logGroups":[]}'
    spans_text = "LOGGROUPS\taws/spans\t1700000000000\t/aws/spans" if spans else ""
    return (
        "aws() {\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then\n'
        f'    echo "{destination}"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then\n'
        '    case " $* " in\n'
        f"      *' --output json '*) echo '{spans_json}'; return 0;;\n"
        f'      *) printf "%s\\n" "{spans_text}"; return 0;;\n'
        "    esac\n"
        "  fi\n"
        '  echo "MUTATION: $1 $2" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_recheck_reports_active_without_mutation(shell):
    result = _recheck_harness(_recheck_mock(destination="CloudWatchLogs", spans=True), shell=shell)
    assert "rc=0" in result.stdout
    assert "Transaction Search is active" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_recheck_warns_when_spans_absent_without_mutation_or_failure(shell):
    """After launch, a missing 'aws/spans' is surfaced as a non-blocking WARN;
    the recheck never mutates account state and never fails the deploy. The
    advice points to the console or the one-time toggle, not to the opt-in."""
    result = _recheck_harness(_recheck_mock(destination="CloudWatchLogs", spans=False), shell=shell)
    assert "rc=0" in result.stdout
    assert "does not yet appear active" in result.stdout
    assert "'aws/spans' log group is not present" in result.stdout
    assert "update-trace-segment-destination --destination XRay" in result.stdout
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_recheck_stays_read_only_when_opt_in_is_exported(shell):
    """Even with GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY exported, the recheck mode
    takes precedence and makes no shared write: the recheck is always read-only."""
    command = "\n".join(
        (
            'export AWS_REGION="us-west-2"',
            'export GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY="true"',
            'MUTATION_LOG="$(mktemp)"; export MUTATION_LOG',
            _recheck_mock(destination="XRay", spans=False),
            "export -f aws",
            f'{shell} "{ACCOUNT_OBS_SCRIPT}" report-transaction-search; echo "rc=$?"',
            'echo "MUTATIONS:"; cat "$MUTATION_LOG"; rm -f "$MUTATION_LOG"',
        )
    )
    result = subprocess.run([shell, "-c", command], capture_output=True, text=True)
    assert "rc=0" in result.stdout
    assert "does not yet appear active" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_recheck_reports_unreadable_spans_without_mutation_or_failure(shell):
    """A denied/throttled describe is reported as 'unknown' in the recheck,
    still read-only and non-failing."""
    mock = (
        "aws() {\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then\n'
        '    echo "CloudWatchLogs"; return 0; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo "An error occurred (AccessDeniedException)" >&2; return 254; fi\n'
        '  echo "MUTATION: $1 $2" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )
    result = _recheck_harness(mock, shell=shell)
    assert "rc=0" in result.stdout
    assert "could not be read" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_recheck_confirms_in_console_when_destination_unreadable(shell):
    """When the recheck cannot read the destination, it must point to the
    console and must NOT print a destination toggle (the current destination is
    unknown, so toggling it blindly could disable a working destination)."""
    mock = (
        "aws() {\n"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then return 254; fi\n'
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then echo \'{"logGroups":[]}\'; return 0; fi\n'
        '  echo "MUTATION: $1 $2" >> "$MUTATION_LOG"; return 0\n'
        "}"
    )
    result = _recheck_harness(mock, shell=shell)
    assert "rc=0" in result.stdout
    assert "does not yet appear active" in result.stdout
    assert "could not be" in result.stdout
    assert "update-trace-segment-destination" not in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_recheck_warns_when_destination_not_cloudwatchlogs(shell):
    """After launch, a destination still 'XRay' with 'aws/spans' absent is
    surfaced as a non-blocking WARN. The launch has already run and did not
    switch the destination, so the recheck must point to the toolkit's
    "Transaction Search configuration failed" warning and the one-time console
    enable, NOT to another launch, NOT to the XRay -> CloudWatchLogs toggle
    pair, and NOT to the opt-in. Pins the recheck XRay/absent branch."""
    result = _recheck_harness(_recheck_mock(destination="XRay", spans=False), shell=shell)
    assert "rc=0" in result.stdout
    assert "does not yet appear active" in result.stdout
    assert "not 'CloudWatchLogs'" in result.stdout
    assert "Transaction Search configuration failed" in result.stdout
    assert "enable Transaction Search" in result.stdout
    # The launch already ran, so the recheck must not send the operator back to
    # the deploy's own launch, and must not print a destination toggle.
    assert "agentcore launch" not in result.stdout
    assert "update-trace-segment-destination" not in result.stdout
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" not in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]


@pytest.mark.parametrize("shell", _OBS_SHELLS)
def test_recheck_reports_existing_spans_as_present_when_destination_xray(shell):
    """Recheck with destination 'XRay' and 'aws/spans' already PRESENT: the
    group exists, so the remedy must NOT claim it is missing. Only the trace
    destination needs switching to 'CloudWatchLogs' (console or the opt-in).
    This is the state a launch leaves behind when its Transaction Search step
    failed on an account that already had an 'aws/spans' grant."""
    result = _recheck_harness(_recheck_mock(destination="XRay", spans=True), shell=shell)
    assert "rc=0" in result.stdout
    assert "does not yet appear active" in result.stdout
    assert "already exists" in result.stdout
    # It must NOT say the group is missing/not present.
    assert "is not present" not in result.stdout
    assert "does not exist" not in result.stdout
    # The destination needs switching; the opt-in is one route for this state.
    assert "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true" in result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]


# ---------------------------------------------------------------------------
# Behavior matrix: mode x destination x aws/spans state
# ---------------------------------------------------------------------------


def _spans_reader_harness(describe_output: str, exit_code: int, shell: str = "bash"):
    """Source the whole script (which only runs main when executed directly) and
    call spans_log_group_state over a mocked 'aws logs describe-log-groups'."""
    aws_mock = "aws() {\n" f'  printf "%s" {describe_output}; return {exit_code};\n' "}"
    command = "\n".join(
        (
            'AWS_REGION="us-west-2"',
            f'source "{ACCOUNT_OBS_SCRIPT}"',
            aws_mock,
            "spans_log_group_state",
        )
    )
    return subprocess.run([shell, "-c", command], capture_output=True, text=True)


@pytest.mark.parametrize("shell", _OBS_SHELLS)
@pytest.mark.parametrize(
    "describe_output,exit_code,expected",
    [
        ('\'{"logGroups":[{"logGroupName":"aws/spans"}]}\'', 0, "present"),
        ("'{\"logGroups\":[]}'", 0, "absent"),
        ('\'{"logGroups":[{"logGroupName":"aws/spans-archive"}]}\'', 0, "absent"),
        # Parsed JSON that lacks an 'aws/spans' entry maps to 'absent' in BOTH
        # shells (empty object, empty list, null, logGroups:null).
        ("'{}'", 0, "absent"),
        ("'[]'", 0, "absent"),
        ("'null'", 0, "absent"),
        ("'{\"logGroups\":null}'", 0, "absent"),
        # Output that cannot be parsed as JSON at all is 'unreadable', never
        # 'absent'. A text/table rendering of an existing group lands here.
        ("'not json at all'", 0, "unreadable"),
        ('"LOGGROUPS\taws/spans"', 0, "unreadable"),
        # A non-zero CLI exit (denied / throttled) is 'unreadable'.
        ("''", 254, "unreadable"),
    ],
)
def test_spans_reader_state_alignment(describe_output, exit_code, expected, shell):
    """Pin the shell reader's state mapping so it matches the PowerShell reader:
    only a non-JSON parse (or a non-zero exit) is 'unreadable'; any parsed JSON
    without an exact 'aws/spans' entry is 'absent'."""
    result = _spans_reader_harness(describe_output, exit_code, shell=shell)
    assert result.stdout.strip() == expected, result.stdout + result.stderr


@pytest.mark.parametrize("shell", _OBS_SHELLS)
@pytest.mark.parametrize("mode_args", [[], ["--toolkit-preflight"]])
def test_script_fails_fast_when_python3_is_missing(shell, mode_args):
    """A standalone run of the setup script without python3 on PATH must stop at
    the start of main with a clear 'python3 not found' message, before any AWS
    read, rather than letting the describe calls be misread as denied or
    throttled. The readers parse JSON with python3, so the check guards them.
    The toolkit-preflight dispatch bypasses main(), so it carries its own guard
    and must fail the same way."""
    # Resolve the shell binary with the real PATH, then run it with an empty
    # PATH so python3 cannot be found. Everything main needs to reach the check
    # (variable assignments, 'command -v', 'echo') is a shell builtin, so no
    # PATH entries are required past locating the interpreter itself.
    shell_bin = shutil.which(shell) or shell
    result = subprocess.run(
        [shell_bin, str(ACCOUNT_OBS_SCRIPT), *mode_args],
        capture_output=True,
        text=True,
        env={"PATH": "", "AWS_REGION": "us-west-2"},
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "python3 not found" in combined, combined
    # It must not reach the read path and blame a denied/throttled call.
    assert "denied, throttled" not in combined, combined


# One mock drives every cell of the matrix. 'describe-log-groups' renders JSON
# only when '--output json' is requested; without the flag it renders a 'text'
# listing that does not parse as JSON, which the reader classifies as
# 'unreadable' (not 'absent'), so a reader that drops the flag fails every cell.
# 'present' is matched on the EXACT name 'aws/spans', never a prefix such as
# 'aws/spans-archive'. 'unreadable' is a non-zero CLI exit for the relevant read.


def _matrix_mock(destination: str, spans: str) -> str:
    # spans: "present" | "absent" | "unreadable" | "archive_only" | "non_json"
    if spans == "present":
        spans_json = '{"logGroups":[{"logGroupName":"aws/spans"}]}'
    elif spans == "archive_only":
        # Only 'aws/spans-archive' — a prefix match would wrongly call this
        # present; the exact-name check must call it absent.
        spans_json = '{"logGroups":[{"logGroupName":"aws/spans-archive"}]}'
    else:
        spans_json = '{"logGroups":[]}'
    spans_text = "LOGGROUPS\taws/spans\t1700000000000" if spans in ("present", "archive_only") else ""

    if destination == "UNREADABLE":
        dest_block = '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then return 254; fi\n'
    else:
        dest_block = (
            '  if [ "$1" = "xray" ] && [ "$2" = "get-trace-segment-destination" ]; then\n'
            f'    echo "{destination}"; return 0; fi\n'
        )

    if spans == "unreadable":
        spans_block = (
            '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then\n'
            '    echo "An error occurred (AccessDeniedException)" >&2; return 254; fi\n'
        )
    elif spans == "non_json":
        # Exit 0 but not JSON (a profile rendering table/text output). Must be
        # classified 'unreadable', not 'absent'.
        spans_block = (
            '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then\n'
            '    printf "%s\\n" "not-json-output aws/spans"; return 0; fi\n'
        )
    else:
        spans_block = (
            '  if [ "$1" = "logs" ] && [ "$2" = "describe-log-groups" ]; then\n'
            '    case " $* " in\n'
            f"      *' --output json '*) echo '{spans_json}'; return 0;;\n"
            f'      *) printf "%s\\n" "{spans_text}"; return 0;;\n'
            "    esac\n"
            "  fi\n"
        )
    return (
        "aws() {\n"
        '  if [ "$1" = "sts" ]; then echo "123456789012"; return 0; fi\n'
        f"{dest_block}"
        '  if [ "$1" = "xray" ] && [ "$2" = "get-indexing-rules" ]; then echo "5"; return 0; fi\n'
        f"{spans_block}"
        '  if [ "$1" = "logs" ] && [ "$2" = "describe-resource-policies" ]; then echo \'{"resourcePolicies":[]}\'; return 0; fi\n'
        '  echo "MUTATION: $1 $2 ${3:-}" >> "$MUTATION_LOG"\n'
        "  return 0\n"
        "}"
    )


# Each cell lists substrings that must appear and substrings that must not, so a
# change to the cell's routing fails the test. Keyed by (mode, destination,
# spans).
_MATRIX_CELLS = {
    # default mode
    ("default", "CloudWatchLogs", "present"): (["already supports Transaction Search"], []),
    ("default", "CloudWatchLogs", "absent"): (
        ["already 'CloudWatchLogs'", "update-trace-segment-destination --destination XRay"],
        ["GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true"],
    ),
    ("default", "CloudWatchLogs", "unreadable"): (
        ["could not be read"],
        ["update-trace-segment-destination"],
    ),
    ("default", "CloudWatchLogs", "non_json"): (
        ["could not be read"],
        ["update-trace-segment-destination"],
    ),
    ("default", "CloudWatchLogs", "archive_only"): (
        ["already 'CloudWatchLogs'", "update-trace-segment-destination --destination XRay"],
        [],
    ),
    ("default", "XRay", "present"): (
        ["GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true", "X-Ray trace segment destination"],
        [],
    ),
    ("default", "XRay", "absent"): (
        ["agentcore launch", "switches the", "AWS X-Ray / CloudWatch console"],
        ["update-trace-segment-destination", "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true"],
    ),
    ("default", "XRay", "unreadable"): (
        ["could not be read"],
        ["update-trace-segment-destination"],
    ),
    ("default", "XRay", "archive_only"): (
        ["agentcore launch", "switches the"],
        ["update-trace-segment-destination"],
    ),
    ("default", "UNREADABLE", "present"): (
        ["could not be read"],
        ["update-trace-segment-destination", "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true"],
    ),
    ("default", "UNREADABLE", "absent"): (
        ["could not be read"],
        ["update-trace-segment-destination", "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true"],
    ),
    ("default", "UNREADABLE", "unreadable"): (
        ["could not be read"],
        ["update-trace-segment-destination"],
    ),
    # recheck mode
    ("recheck", "CloudWatchLogs", "present"): (["Transaction Search is active"], []),
    ("recheck", "CloudWatchLogs", "absent"): (
        ["already 'CloudWatchLogs'", "update-trace-segment-destination --destination XRay"],
        ["GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true"],
    ),
    ("recheck", "CloudWatchLogs", "unreadable"): (
        ["could not be read"],
        ["update-trace-segment-destination"],
    ),
    ("recheck", "CloudWatchLogs", "non_json"): (
        ["could not be read"],
        ["update-trace-segment-destination"],
    ),
    ("recheck", "CloudWatchLogs", "archive_only"): (
        ["update-trace-segment-destination --destination XRay"],
        [],
    ),
    ("recheck", "XRay", "present"): (
        ["already exists", "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true", "AWS X-Ray / CloudWatch console"],
        ["is not present", "does not exist"],
    ),
    ("recheck", "XRay", "absent"): (
        ["Transaction Search configuration failed", "not 'CloudWatchLogs'", "AWS X-Ray / CloudWatch console"],
        ["update-trace-segment-destination", "GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true", "agentcore launch"],
    ),
    ("recheck", "XRay", "unreadable"): (
        ["not 'CloudWatchLogs'", "could not be read"],
        ["update-trace-segment-destination"],
    ),
    ("recheck", "XRay", "archive_only"): (
        ["Transaction Search configuration failed"],
        ["update-trace-segment-destination"],
    ),
    ("recheck", "UNREADABLE", "present"): (
        ["could not be read"],
        # The not-CloudWatchLogs line must not print for an unreadable
        # destination.
        ["not 'CloudWatchLogs'", "update-trace-segment-destination"],
    ),
    ("recheck", "UNREADABLE", "absent"): (
        ["could not be read"],
        ["not 'CloudWatchLogs'", "update-trace-segment-destination"],
    ),
    ("recheck", "UNREADABLE", "unreadable"): (
        ["could not be read"],
        ["update-trace-segment-destination"],
    ),
}


def _run_matrix_cell(mode: str, destination: str, spans: str, shell: str):
    mock = _matrix_mock(destination, spans)
    if mode == "recheck":
        return _recheck_harness(mock, shell=shell)
    return _account_obs_harness("false", mock, shell=shell)


@pytest.mark.parametrize("cell", list(_MATRIX_CELLS.keys()), ids=lambda c: f"{c[0]}-{c[1]}-{c[2]}")
def test_remedy_behavior_matrix(cell):
    """Pin every cell of the behavior matrix (mode x destination x aws/spans
    state) to its distinguishing message route. Both the default Step 1.7 path
    and the Step 5c recheck stay read-only (no mutation) and return 0."""
    mode, destination, spans = cell
    must_contain, must_not_contain = _MATRIX_CELLS[cell]
    result = _run_matrix_cell(mode, destination, spans, "bash")
    assert "rc=0" in result.stdout, result.stdout
    assert "MUTATION" not in result.stdout.split("MUTATIONS:", 1)[1]
    for needle in must_contain:
        assert needle in result.stdout, f"{cell}: expected {needle!r}\n{result.stdout}"
    for needle in must_not_contain:
        assert needle not in result.stdout, f"{cell}: unexpected {needle!r}\n{result.stdout}"


# The whole matrix is byte-identical under the login `bash` and the macOS system
# `/bin/bash` (3.2). Re-run the cells that exercise the keyed remedy branches
# under `/bin/bash` so a bash-4+ construct cannot regress 3.2.
_MATRIX_PARITY_CELLS = [
    ("default", "XRay", "present"),
    ("default", "XRay", "absent"),
    ("recheck", "XRay", "present"),
    ("recheck", "XRay", "absent"),
    ("recheck", "XRay", "unreadable"),
    ("recheck", "UNREADABLE", "present"),
    ("default", "CloudWatchLogs", "non_json"),
]


@pytest.mark.parametrize("cell", _MATRIX_PARITY_CELLS, ids=lambda c: f"{c[0]}-{c[1]}-{c[2]}")
def test_remedy_behavior_matrix_under_bash_3_2(cell):
    mode, destination, spans = cell
    must_contain, must_not_contain = _MATRIX_CELLS[cell]
    result = _run_matrix_cell(mode, destination, spans, "/bin/bash")
    assert "rc=0" in result.stdout, result.stdout
    for needle in must_contain:
        assert needle in result.stdout, f"{cell}: expected {needle!r}\n{result.stdout}"
    for needle in must_not_contain:
        assert needle not in result.stdout, f"{cell}: unexpected {needle!r}\n{result.stdout}"


# ---------------------------------------------------------------------------
# Post-launch recheck wiring in deploy.sh (static source check)
# ---------------------------------------------------------------------------


def test_deploy_script_runs_recheck_after_the_final_launch_and_non_blocking():
    """deploy.sh must dispatch the read-only recheck ('report-transaction-search')
    and must do so AFTER the final (Step 5b) launch, so the recheck reports the
    post-launch state even on a refresh deploy where only Step 5b launches. The
    call site must be non-blocking at the shell level ('|| true')."""
    content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "report-transaction-search" in content
    # The recheck dispatch is non-blocking regardless of the child's exit status.
    recheck_line = next(line for line in content.splitlines() if "report-transaction-search" in line)
    assert recheck_line.rstrip().endswith("|| true")
    # The recheck follows the final launch (the last 'agentcore launch' in the
    # file), so it observes the state the run's last launch left behind.
    last_launch = content.rfind("agentcore launch")
    recheck = content.find("report-transaction-search")
    assert last_launch != -1 and recheck != -1
    assert recheck > last_launch
