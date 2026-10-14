#!/usr/bin/env python3
"""Unit tests for the ADOT exporter authentication-failure detector (#420).

Root cause of #420 is platform/package-owned: the AgentCore-managed ADOT OTLP
exporter can emit a transient ``Failed to export ... code: 403, reason: Forbidden``
at cold start, when its background SigV4-signed export fires before the runtime
workload credential provider is fully seeded. The application's own AWS SDK calls
are unaffected and telemetry delivery recovers on warm requests.

This detector does NOT suppress those errors. It classifies exporter
authentication failures found in runtime logs so a deployment/live check can:
  * confirm the KNOWN transient cold-start signature (adjacent to an instance
    transition, bounded incident count) as a WARN, and
  * escalate a PERSISTENT pattern (sustained failures not tied to a cold start)
    as a FAIL that would indicate a genuine regression (e.g. an IAM/config break).

The logic is pure (no AWS I/O) so it is fully unit-testable.
"""

# Standard library
import re
import sys
from importlib import util as _import_util
from pathlib import Path

# Third-party packages
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.fast]

# Load the module under test by path to avoid depending on package layout.
# Register it in sys.modules before executing so dataclass introspection works.
_MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "utils" / "adot_exporter_auth_detector.py"
_spec = _import_util.spec_from_file_location("adot_exporter_auth_detector", _MODULE_PATH)
detector = _import_util.module_from_spec(_spec)
sys.modules["adot_exporter_auth_detector"] = detector
_spec.loader.exec_module(detector)

classify_exporter_events = detector.classify_exporter_events
is_exporter_auth_failure = detector.is_exporter_auth_failure
ExporterAuthReport = detector.ExporterAuthReport

# --- Fixtures: representative log-event dicts (timestamp ms, scope, severity, body) ---

_EXPORTER_SCOPE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"


def _evt(ts_ms, body, scope=_EXPORTER_SCOPE, severity="ERROR"):
    return {"timestamp": ts_ms, "scope": scope, "severity": severity, "body": body}


def _raw_evt(ts_ms, body):
    """A raw CloudWatch message with the whole JSON line in `body` (no top-level scope)."""
    return {"timestamp": ts_ms, "body": body}


# A realistic raw otel-rt-logs JSON line for the #420 span-batch 403 failure.
_RAW_403_SPAN = (
    '{"resource":{"attributes":{"service.name":"gameagentruntime.DEFAULT"}},'
    '"scope":{"name":"opentelemetry.exporter.otlp.proto.http.trace_exporter"},'
    '"severityText":"ERROR",'
    '"body":"Failed to export span batch code: 403, reason: Forbidden"}'
)

# The credential-load failure line the exporter/aws-auth-session emits ~16ms before
# the 403 in the same cold-start incident (observed in the deployed runtime).
_RAW_CRED_LOAD = (
    '{"scope":{"name":"opentelemetry.exporter.otlp.proto.http.trace_exporter"},'
    '"severityText":"ERROR","body":"Failed to load AWS Credentials"}'
)

# A raw line that merely contains the word "Forbidden" but is NOT an exporter
# auth failure (e.g. a tool-result body or a security-policy note).
_RAW_FORBIDDEN_NOISE = (
    '{"scope":{"name":"strands.telemetry.tracer"},"severityText":"",'
    '"body":"tool result: access Forbidden by policy in session xyz"}'
)


# =============================================================================
# is_exporter_auth_failure — signature matcher
# =============================================================================


def test_matches_403_forbidden_span_batch():
    assert is_exporter_auth_failure(_evt(1, "Failed to export span batch code: 403, reason: Forbidden")) is True


def test_matches_403_missing_authentication_token_log_batch():
    assert (
        is_exporter_auth_failure(_evt(1, "Failed to export logs batch code: 403 ... Missing Authentication Token"))
        is True
    )


def test_matches_failed_to_load_aws_credentials():
    assert is_exporter_auth_failure(_evt(1, "Failed to load AWS Credentials", severity="ERROR")) is True


def test_matches_raw_json_blob_403_span_batch():
    # otel-rt-logs delivers the whole JSON line in the message body (no top-level scope).
    assert is_exporter_auth_failure(_raw_evt(1, _RAW_403_SPAN)) is True


def test_matches_raw_json_blob_credential_load():
    assert is_exporter_auth_failure(_raw_evt(1, _RAW_CRED_LOAD)) is True


def test_ignores_missing_log_group_400_resourcenotfound():
    # This is upstream #457, a DIFFERENT (already-worked-around) failure mode.
    e = _evt(
        1, "Failed to export batch code: 400, reason: The specified log group does not exist. ResourceNotFoundException"
    )
    assert is_exporter_auth_failure(e) is False


def test_ignores_forbidden_word_without_exporter_context():
    # A body containing "Forbidden" but no exporter scope / batch-export signature
    # must NOT be counted (prevents false positives like the observed noise hits).
    assert is_exporter_auth_failure(_raw_evt(1, _RAW_FORBIDDEN_NOISE)) is False


def test_ignores_benign_instrumentation_warning():
    e = _evt(
        1,
        "Skipping installation of LoggingHandler ... already active",
        scope="opentelemetry.instrumentation.logging",
        severity="WARN",
    )
    assert is_exporter_auth_failure(e) is False


def test_ignores_successful_credential_load():
    e = _evt(1, "Found credentials from IAM Role: execution_role", scope="botocore.credentials", severity="INFO")
    assert is_exporter_auth_failure(e) is False


def test_ignores_application_level_error():
    e = _evt(1, "Failed to get fleet capacity for group: InvalidRequestException", scope="agents.gamelift_specialist")
    assert is_exporter_auth_failure(e) is False


# =============================================================================
# classify_exporter_events — clean / transient / persistent + incident clustering
# =============================================================================


def test_no_failures_is_clean():
    events = [
        _evt(1000, "Found credentials from IAM Role: execution_role", scope="botocore.credentials", severity="INFO"),
        _evt(2000, "Invocation completed successfully (3.3s)", scope="bedrock_agentcore.app", severity="INFO"),
    ]
    report: ExporterAuthReport = classify_exporter_events(events, instance_transitions=[1000])
    assert report.failure_count == 0
    assert report.classification == "clean"
    assert report.has_failures is False


def test_forbidden_noise_does_not_trigger_failures():
    events = [_raw_evt(1000 + i, _RAW_FORBIDDEN_NOISE) for i in range(47)]
    report = classify_exporter_events(events, instance_transitions=[])
    assert report.failure_count == 0
    assert report.classification == "clean"


def test_observed_four_lines_two_incidents_is_transient():
    # Mirrors the exact deployed evidence: two cold-start incidents, each a
    # credential-load line + a 403 line ~16ms apart (4 lines, 2 incidents), both
    # amid concurrent cold-start churn.
    t1 = 1_789_107_971_141
    t2 = 1_789_151_567_998
    events = [
        _raw_evt(t1, _RAW_CRED_LOAD),
        _raw_evt(t1 + 16, _RAW_403_SPAN),
        _raw_evt(t2, _RAW_CRED_LOAD),
        _raw_evt(t2 + 15, _RAW_403_SPAN),
    ]
    transitions = [t1 - 3_000, t2 - 4_000]  # a new instance appeared just before each
    report = classify_exporter_events(events, instance_transitions=transitions)
    assert report.line_count == 4
    assert report.failure_count == 2  # clustered into 2 incidents
    assert report.classification == "transient_cold_start"
    assert report.is_warning() is True


def test_failure_not_near_any_transition_is_persistent():
    events = [_evt(500_000, "Failed to export span batch code: 403, reason: Forbidden")]
    report = classify_exporter_events(events, instance_transitions=[0])
    assert report.classification == "persistent"


def test_many_incidents_exceed_budget_is_persistent():
    # Distinct incidents spread out in time (> incident gap apart), all near their
    # own transition, but exceeding the transient budget -> a real regression.
    transitions = []
    events = []
    for k in range(10):
        base = 1_000_000 + k * 60_000  # 60s apart => distinct incidents
        events.append(_evt(base, "Failed to export span batch code: 403, reason: Forbidden"))
        transitions.append(base - 1_000)
    report = classify_exporter_events(events, instance_transitions=transitions, transient_budget=3)
    assert report.failure_count == 10
    assert report.classification == "persistent"


def test_report_exit_status_mapping():
    clean = classify_exporter_events([], instance_transitions=[])
    assert clean.exit_status() == 0  # PASS

    transient = classify_exporter_events(
        [_evt(1000, "Failed to export span batch code: 403, reason: Forbidden")],
        instance_transitions=[900],
    )
    assert transient.exit_status() == 0  # WARN-level, non-blocking
    assert transient.is_warning() is True

    persistent = classify_exporter_events(
        [_evt(500_000, "Failed to export span batch code: 403, reason: Forbidden")],
        instance_transitions=[0],
    )
    assert persistent.exit_status() == 1  # FAIL, blocking


def test_summary_is_public_safe_no_arns_or_accounts():
    t = 1000
    events = [_raw_evt(t, _RAW_403_SPAN)]
    report = classify_exporter_events(events, instance_transitions=[t - 100])
    summary = report.summary()
    assert "arn:aws" not in summary
    assert "gameagentruntime" not in summary
    assert re.search(r"\b\d{12}\b", summary) is None
