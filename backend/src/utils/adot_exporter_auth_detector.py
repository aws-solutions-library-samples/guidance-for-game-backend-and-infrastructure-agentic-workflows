#!/usr/bin/env python3
"""Detect ADOT exporter authentication failures in AgentCore runtime logs (#420).

Background
----------
Issue #420 is a *platform/package-owned* behavior of the AgentCore-managed AWS
Distro for OpenTelemetry (ADOT) OTLP exporter. At cold start, the exporter's
background, SigV4-signed export can fire before the runtime workload credential
provider is fully seeded. The unsigned/incomplete request is rejected with
``403 Forbidden`` / ``Missing Authentication Token`` and logged as
``Failed to export span batch code: 403, reason: Forbidden`` (or the log-batch
equivalent, or ``Failed to load AWS Credentials``). The application's own AWS
SDK calls are unaffected and telemetry delivery recovers on warm requests.

Observed evidence (deployed runtime, ~5 week lifetime): two isolated cold-start
incidents, each emitting a ``Failed to load AWS Credentials`` line immediately
followed (~16 ms later) by a ``403 Forbidden`` span-batch line — i.e. 4 log
lines but 2 incidents — both amid heavy concurrent cold-start churn, both
recovered with no persistent loss.

This module does NOT suppress those errors. It provides *pure* (no AWS I/O)
classification so a deployment or live check can surface them and, importantly,
distinguish the known transient cold-start signature (report as WARN, expected
platform behavior) from a persistent pattern (report as FAIL, a genuine
regression such as a broken IAM policy or exporter misconfiguration).

Incident clustering
--------------------
A single cold-start credential gap emits more than one log line (a credential
load failure plus one or more rejected batch exports) within a few hundred
milliseconds. To avoid over-counting, failures within ``incident_gap_ms`` of one
another are collapsed into a single *incident*; the transient budget and the
classification are expressed in incidents, not raw log lines.

Log-event shape
---------------
Events may be passed either as structured dicts with top-level ``scope`` /
``severity`` / ``body`` fields, or as raw CloudWatch log messages where the
whole JSON line is in ``body``/``message`` (the exporter's ``otel-rt-logs``
stream emits the latter). The matcher handles both: a generic token like
``Forbidden`` is only treated as a failure when it co-occurs with the exporter
batch-export signature or an exporter scope string, which prevents false
positives from unrelated bodies that merely contain the word "Forbidden".

Public-safety: this module never emits ARNs, account ids, endpoints, or raw log
bodies in its human-readable summary.
"""

from __future__ import annotations

# Standard library
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# The OTLP exporter scopes that own credential-signed export requests.
_EXPORTER_SCOPES = (
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.proto.http._log_exporter",
    "opentelemetry.exporter.otlp.proto.http.log_exporter",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
    # The ADOT AWS auth session that signs exporter requests (SigV4).
    "amazon.opentelemetry.distro.exporter.otlp.aws.common.aws_auth_session",
)

# An exporter scope appearing anywhere in the body text (raw JSON log lines embed
# the scope inside the message rather than as a separate field).
_EXPORTER_SCOPE_IN_BODY = re.compile(
    r"opentelemetry\.exporter\.otlp\.proto\.(?:http|grpc)\.|" r"amazon\.opentelemetry\.distro\.exporter\.otlp\.aws",
    re.IGNORECASE,
)

# The exporter batch-export failure signature. This is specific to the exporter
# and is the primary #420 indicator.
_BATCH_EXPORT_FAILURE = re.compile(r"failed to export .*batch .*code:\s*(\d{3})", re.IGNORECASE)

# Auth-failure reason tokens. On their own these are too generic to match; they
# must be corroborated by an exporter context (batch-export signature or an
# exporter scope). ``Failed to load AWS Credentials`` is exporter-specific
# enough in this runtime to stand alone.
_AUTH_REASON = re.compile(r"missing authentication token|\bforbidden\b", re.IGNORECASE)
_CREDENTIAL_LOAD_FAILURE = re.compile(r"failed to load aws credentials", re.IGNORECASE)

# HTTP status codes that indicate an authentication/authorization failure.
_AUTH_STATUS_CODES = {"401", "403"}

# Signatures that look similar but are a DIFFERENT, already-handled failure
# (upstream starter-toolkit #457: missing ``aws/spans`` log group). These must
# NOT be classified as #420 auth failures.
_EXCLUDE_PATTERNS = (
    re.compile(r"resourcenotfound", re.IGNORECASE),
    re.compile(r"log group does not exist", re.IGNORECASE),
)

# A failure is considered "cold-start adjacent" if it occurs within this many
# milliseconds of an instance transition (new ``service.instance.id`` appearing).
DEFAULT_COLD_START_WINDOW_MS = 60_000

# Failures within this many ms of each other are the same incident.
DEFAULT_INCIDENT_GAP_MS = 5_000

# The maximum number of auth-failure INCIDENTS still consistent with the KNOWN
# transient blip. More than this (even near a transition) is a real regression.
DEFAULT_TRANSIENT_BUDGET = 3


def _body_of(event: dict[str, Any]) -> str:
    return str(event.get("body") or event.get("message") or "")


def _scope_of(event: dict[str, Any]) -> str:
    return str(event.get("scope") or "")


def _has_exporter_context(scope: str, body: str) -> bool:
    """True if the event clearly originates from the OTLP exporter."""
    if scope and any(scope == s or scope.startswith(s) for s in _EXPORTER_SCOPES):
        return True
    return bool(_EXPORTER_SCOPE_IN_BODY.search(body))


def is_exporter_auth_failure(event: dict[str, Any]) -> bool:
    """Return True if ``event`` is an ADOT OTLP exporter authentication failure.

    Matching rules (any one qualifies), after excluding the #457 case:
      1. A batch-export failure with a 401/403 status code, e.g.
         ``Failed to export span batch code: 403, reason: Forbidden``.
      2. ``Failed to load AWS Credentials`` (exporter credential gap).
      3. A generic auth reason (``Forbidden`` / ``Missing Authentication Token``)
         *only when* corroborated by an exporter context (exporter scope field or
         an exporter scope embedded in the body). This avoids false positives
         from unrelated bodies that merely contain the word "Forbidden".
    """
    body = _body_of(event)
    if not body:
        return False

    if any(p.search(body) for p in _EXCLUDE_PATTERNS):
        return False

    scope = _scope_of(event)

    # Rule 1: batch-export failure with an auth status code.
    m = _BATCH_EXPORT_FAILURE.search(body)
    if m and m.group(1) in _AUTH_STATUS_CODES:
        return True

    # Rule 2: explicit credential-load failure.
    if _CREDENTIAL_LOAD_FAILURE.search(body):
        return True

    # Rule 3: generic auth reason, but only with exporter context.
    if _AUTH_REASON.search(body) and _has_exporter_context(scope, body):
        return True

    return False


def _cluster_incidents(timestamps: list[int], gap_ms: int) -> list[int]:
    """Collapse near-simultaneous failure timestamps into one per incident.

    Returns the earliest timestamp of each incident cluster, in order.
    """
    if not timestamps:
        return []
    ordered = sorted(timestamps)
    incidents = [ordered[0]]
    for ts in ordered[1:]:
        if ts - incidents[-1] > gap_ms:
            incidents.append(ts)
    return incidents


def _near_any_transition(ts_ms: int, transitions: Iterable[int], window_ms: int) -> bool:
    return any(abs(ts_ms - t) <= window_ms for t in transitions)


@dataclass
class ExporterAuthReport:
    """Result of classifying exporter auth failures over a set of log events."""

    failure_count: int  # number of distinct incidents (clustered)
    classification: str  # "clean" | "transient_cold_start" | "persistent"
    line_count: int = 0  # raw matching log lines (pre-clustering)
    failure_timestamps: list[int] = field(default_factory=list)  # one per incident
    cold_start_adjacent: int = 0
    transient_budget: int = DEFAULT_TRANSIENT_BUDGET

    @property
    def has_failures(self) -> bool:
        return self.failure_count > 0

    def is_warning(self) -> bool:
        """Transient cold-start failures are a non-blocking WARN (known platform behavior)."""
        return self.classification == "transient_cold_start"

    def is_blocking(self) -> bool:
        """Persistent failures are a blocking FAIL (unexpected regression)."""
        return self.classification == "persistent"

    def exit_status(self) -> int:
        """0 for clean/transient (WARN), 1 for persistent (FAIL)."""
        return 1 if self.is_blocking() else 0

    def summary(self) -> str:
        """Public-safe, one-line human summary. No ARNs / accounts / endpoints / bodies."""
        if self.classification == "clean":
            return "ADOT exporter auth: OK — no exporter 403/credential failures found."
        if self.classification == "transient_cold_start":
            return (
                f"ADOT exporter auth: WARN — {self.failure_count} transient cold-start "
                f"exporter incident(s) (all adjacent to an instance transition; known "
                f"platform behavior, telemetry recovers). See issue #420."
            )
        return (
            f"ADOT exporter auth: FAIL — {self.failure_count} exporter 403/credential "
            f"incident(s) with a persistent pattern (not bounded to cold start). "
            f"Investigate exporter credentials/IAM; this exceeds the known #420 "
            f"transient signature."
        )


def classify_exporter_events(
    events: Iterable[dict[str, Any]],
    instance_transitions: Iterable[int],
    *,
    cold_start_window_ms: int = DEFAULT_COLD_START_WINDOW_MS,
    incident_gap_ms: int = DEFAULT_INCIDENT_GAP_MS,
    transient_budget: int = DEFAULT_TRANSIENT_BUDGET,
) -> ExporterAuthReport:
    """Classify exporter auth failures found in ``events``.

    Parameters
    ----------
    events:
        Iterable of log-event dicts with (at least) ``timestamp`` (epoch ms) and
        ``body``/``message``, optionally ``scope`` and ``severity``.
    instance_transitions:
        Epoch-ms timestamps at which a new ``service.instance.id`` first appeared
        (cold start / instance replacement).
    cold_start_window_ms:
        A failure within this window of a transition is "cold-start adjacent".
    incident_gap_ms:
        Failures within this gap collapse into one incident.
    transient_budget:
        Max number of INCIDENTS still consistent with the known transient blip.

    Classification:
        * ``clean``               — no exporter auth failures.
        * ``transient_cold_start``— incidents present, count <= budget, and ALL
          are cold-start adjacent (the known #420 behavior; WARN, non-blocking).
        * ``persistent``          — any incident not cold-start adjacent, or more
          incidents than the transient budget (FAIL, blocking).
    """
    transitions = list(instance_transitions)
    failures = [e for e in events if is_exporter_auth_failure(e)]
    line_ts = [int(e.get("timestamp", 0)) for e in failures]
    incident_ts = _cluster_incidents(line_ts, incident_gap_ms)
    count = len(incident_ts)

    if count == 0:
        return ExporterAuthReport(
            failure_count=0,
            classification="clean",
            line_count=len(failures),
            failure_timestamps=[],
            cold_start_adjacent=0,
            transient_budget=transient_budget,
        )

    adjacent = sum(1 for ts in incident_ts if _near_any_transition(ts, transitions, cold_start_window_ms))

    all_adjacent = adjacent == count
    within_budget = count <= transient_budget

    classification = "transient_cold_start" if (all_adjacent and within_budget) else "persistent"

    return ExporterAuthReport(
        failure_count=count,
        classification=classification,
        line_count=len(failures),
        failure_timestamps=incident_ts,
        cold_start_adjacent=adjacent,
        transient_budget=transient_budget,
    )
