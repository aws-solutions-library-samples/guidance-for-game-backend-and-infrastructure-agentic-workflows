#!/usr/bin/env python3
"""Fail-closed regression tests for the ADOT exporter auth detector (#420).

These cover the confirmed *fail-open* bug and its boundaries:

  * When the upstream CloudWatch Logs query cannot run (bad/absent profile,
    AccessDenied, throttle, malformed query, missing log group), the check must
    NOT report a clean PASS. It must produce a distinct, bounded, public-safe
    ``unavailable`` result that maps to a non-blocking WARN (exit 2).
  * The known transient cold-start signature still WARNs (exit 0) and a
    persistent regression still FAILs (exit 1).
  * Detector boundary cases: a 401 (not just 403) auth status, the transient
    budget edge (exactly-at-budget vs one-over), and the incident-clustering
    edge (just-inside vs just-outside the incident gap).

Written red-first: they assert the ``unavailable`` classification, the
``is_unavailable``/``exit_status`` contract, 401 matching, and the budget/cluster
edges — behaviors the pre-fix module did not provide.
"""

# Standard library
import re
import sys
from importlib import util as _import_util
from pathlib import Path

# Third-party packages
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "utils" / "adot_exporter_auth_detector.py"
_spec = _import_util.spec_from_file_location("adot_exporter_auth_detector", _MODULE_PATH)
detector = _import_util.module_from_spec(_spec)
sys.modules["adot_exporter_auth_detector"] = detector
_spec.loader.exec_module(detector)

classify_exporter_events = detector.classify_exporter_events
is_exporter_auth_failure = detector.is_exporter_auth_failure
unavailable_report = detector.unavailable_report

_EXPORTER_SCOPE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"


def _evt(ts_ms, body, scope=_EXPORTER_SCOPE, severity="ERROR"):
    return {"timestamp": ts_ms, "scope": scope, "severity": severity, "body": body}


# =============================================================================
# unavailable — the fail-closed result (was fail-open -> clean PASS)
# =============================================================================


def test_unavailable_report_is_not_clean():
    report = unavailable_report("query_failed")
    assert report.classification == "unavailable"
    assert report.is_unavailable() is True
    # Must NOT be mistaken for a clean/healthy result.
    assert report.classification != "clean"
    assert report.has_failures is False


def test_unavailable_maps_to_warn_not_pass_not_fail():
    report = unavailable_report("access_denied")
    # WARN semantics: non-blocking, but distinctly not a clean PASS.
    assert report.is_warning() is True
    assert report.is_blocking() is False
    # Distinct non-zero exit code (2) so the shell wrapper can render WARN,
    # never letting a failed query fall through to a green PASS (exit 0).
    assert report.exit_status() == detector.EXIT_UNAVAILABLE == 2


@pytest.mark.parametrize(
    "reason",
    ["query_failed", "profile_not_found", "access_denied", "throttled", "log_group_missing", "invalid_query"],
)
def test_unavailable_summary_is_public_safe(reason):
    summary = unavailable_report(reason).summary()
    assert "WARN" in summary
    assert "UNKNOWN" in summary or "could not run" in summary
    # No secrets/identifiers leak from a failure reason.
    assert "arn:aws" not in summary
    assert re.search(r"\b\d{12}\b", summary) is None


def test_unavailable_unknown_reason_collapses_to_generic():
    # An unrecognized token (which could otherwise carry raw stderr) must be
    # normalized to a safe generic phrase.
    leaky = "arn:aws:iam::123456789012:role/secret AccessDenied blah"
    report = unavailable_report(leaky)
    summary = report.summary()
    assert "arn:aws" not in summary
    assert "123456789012" not in summary
    assert report.classification == "unavailable"


def test_empty_events_from_successful_query_still_clean():
    # A genuinely successful query that returns zero events IS clean — the fix
    # must not over-correct healthy empties into WARN.
    report = classify_exporter_events([], instance_transitions=[])
    assert report.classification == "clean"
    assert report.exit_status() == 0


# =============================================================================
# Detector boundaries: 401, transient-budget edge, clustering edge
# =============================================================================


def test_matches_401_span_batch():
    # 401 (not just 403) is an auth status code and must match.
    assert is_exporter_auth_failure(_evt(1, "Failed to export span batch code: 401, reason: Unauthorized")) is True


def test_non_auth_status_code_does_not_match():
    # A 5xx export failure is not an auth failure for #420 purposes.
    assert is_exporter_auth_failure(_evt(1, "Failed to export span batch code: 500, reason: Internal")) is False


def test_transient_budget_edge_exactly_at_budget_is_transient():
    # Exactly `transient_budget` cold-start-adjacent incidents -> still transient.
    budget = 3
    events, transitions = [], []
    for k in range(budget):
        base = 1_000_000 + k * 60_000  # 60s apart -> distinct incidents
        events.append(_evt(base, "Failed to export span batch code: 403, reason: Forbidden"))
        transitions.append(base - 1_000)
    report = classify_exporter_events(events, instance_transitions=transitions, transient_budget=budget)
    assert report.failure_count == budget
    assert report.classification == "transient_cold_start"


def test_transient_budget_edge_one_over_budget_is_persistent():
    budget = 3
    events, transitions = [], []
    for k in range(budget + 1):
        base = 1_000_000 + k * 60_000
        events.append(_evt(base, "Failed to export span batch code: 403, reason: Forbidden"))
        transitions.append(base - 1_000)
    report = classify_exporter_events(events, instance_transitions=transitions, transient_budget=budget)
    assert report.failure_count == budget + 1
    assert report.classification == "persistent"


def test_clustering_edge_within_gap_is_one_incident():
    # Two failures exactly at the incident gap boundary collapse into one.
    gap = detector.DEFAULT_INCIDENT_GAP_MS
    t0 = 2_000_000
    events = [
        _evt(t0, "Failed to load AWS Credentials"),
        _evt(t0 + gap, "Failed to export span batch code: 403, reason: Forbidden"),
    ]
    report = classify_exporter_events(events, instance_transitions=[t0 - 500], incident_gap_ms=gap)
    assert report.line_count == 2
    assert report.failure_count == 1  # same incident


def test_clustering_edge_just_over_gap_is_two_incidents():
    gap = detector.DEFAULT_INCIDENT_GAP_MS
    t0 = 2_000_000
    events = [
        _evt(t0, "Failed to export span batch code: 403, reason: Forbidden"),
        _evt(t0 + gap + 1, "Failed to export span batch code: 403, reason: Forbidden"),
    ]
    # Both adjacent to their own transition so classification stays transient,
    # but they must count as two distinct incidents.
    report = classify_exporter_events(events, instance_transitions=[t0 - 200, t0 + gap - 200], incident_gap_ms=gap)
    assert report.failure_count == 2
