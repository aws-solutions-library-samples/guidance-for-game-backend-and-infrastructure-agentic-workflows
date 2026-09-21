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

Fail-closed on query failure
----------------------------
The classifier is only meaningful when the log query that produced ``events``
actually ran. When the upstream CloudWatch Logs query itself fails — a bad/absent
AWS profile, an authorization/throttling error, a malformed query, or a missing
log group — there are zero events for a reason unrelated to health, and treating
that empty result as ``clean`` would be a fail-*open* PASS that hides a broken
check. Callers must instead build an ``unavailable`` report (see
``unavailable_report``); it is a distinct, bounded, public-safe result that maps
to a non-blocking WARN (never a clean PASS) so ``validate-deployment`` surfaces
that the check could not run. The known transient cold-start WARN and the
persistent regression FAIL are unchanged.

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

# Classification labels.
CLASSIFICATION_CLEAN = "clean"
CLASSIFICATION_TRANSIENT = "transient_cold_start"
CLASSIFICATION_PERSISTENT = "persistent"
# The log query that feeds the classifier could not run (bad/absent profile,
# authorization/throttle error, malformed query, or missing log group). This is
# NOT "clean": we could not observe the runtime, so the check is unavailable.
CLASSIFICATION_UNAVAILABLE = "unavailable"

# Process exit statuses (also the contract with check-exporter-auth.sh /
# validate-deployment.sh). 0 = clean/transient (PASS/WARN), 1 = persistent
# (FAIL), 2 = unavailable (the check could not run -> WARN, never a clean PASS).
EXIT_OK = 0
EXIT_PERSISTENT = 1
EXIT_UNAVAILABLE = 2


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
    classification: str  # "clean" | "transient_cold_start" | "persistent" | "unavailable"
    line_count: int = 0  # raw matching log lines (pre-clustering)
    failure_timestamps: list[int] = field(default_factory=list)  # one per incident
    cold_start_adjacent: int = 0
    transient_budget: int = DEFAULT_TRANSIENT_BUDGET
    # Bounded, public-safe reason for an ``unavailable`` classification (e.g.
    # "log query failed"). Never contains raw stderr, ARNs, accounts, or bodies.
    unavailable_reason: str = ""

    @property
    def has_failures(self) -> bool:
        return self.failure_count > 0

    def is_warning(self) -> bool:
        """Non-blocking WARN conditions.

        Both the KNOWN transient cold-start signature (expected platform
        behavior) and an ``unavailable`` check (the query could not run) are
        surfaced as a non-blocking WARN — never as a silent clean PASS.
        """
        return self.classification in (CLASSIFICATION_TRANSIENT, CLASSIFICATION_UNAVAILABLE)

    def is_blocking(self) -> bool:
        """Persistent failures are a blocking FAIL (unexpected regression)."""
        return self.classification == CLASSIFICATION_PERSISTENT

    def is_unavailable(self) -> bool:
        """True when the log query could not run, so nothing could be observed."""
        return self.classification == CLASSIFICATION_UNAVAILABLE

    def exit_status(self) -> int:
        """Exit-code contract: 0 clean/transient, 1 persistent, 2 unavailable.

        ``unavailable`` gets its own non-zero code so the shell wrapper can map
        it to a WARN (the check could not run) distinctly from a persistent FAIL,
        and can never let a failed query fall through to a clean PASS.
        """
        if self.is_blocking():
            return EXIT_PERSISTENT
        if self.is_unavailable():
            return EXIT_UNAVAILABLE
        return EXIT_OK

    def summary(self) -> str:
        """Public-safe, one-line human summary. No ARNs / accounts / endpoints / bodies."""
        if self.classification == CLASSIFICATION_CLEAN:
            return "ADOT exporter auth: OK — no exporter 403/credential failures found."
        if self.classification == CLASSIFICATION_TRANSIENT:
            return (
                f"ADOT exporter auth: WARN — {self.failure_count} transient cold-start "
                f"exporter incident(s) (all adjacent to an instance transition; known "
                f"platform behavior, telemetry recovers). See issue #420."
            )
        if self.classification == CLASSIFICATION_UNAVAILABLE:
            reason = self.unavailable_reason or "log query failed"
            return (
                f"ADOT exporter auth: WARN — check could not run ({reason}); "
                f"exporter auth status is UNKNOWN, not clean. Verify AWS "
                f"profile/permissions/log group and re-run. See issue #420."
            )
        return (
            f"ADOT exporter auth: FAIL — {self.failure_count} exporter 403/credential "
            f"incident(s) with a persistent pattern (not bounded to cold start). "
            f"Investigate exporter credentials/IAM; this exceeds the known #420 "
            f"transient signature."
        )


# A compact allow-list of public-safe reason phrases. The shell wrapper passes a
# short token describing *why* the query failed; we normalize it to one of these
# so no raw stderr (which could carry ARNs/accounts/endpoints) ever reaches the
# summary. Any unrecognized token collapses to the generic phrase.
_SAFE_REASONS = {
    "query_failed": "log query failed",
    "profile_not_found": "AWS profile not found",
    "access_denied": "access denied to log group",
    "throttled": "request throttled",
    "log_group_missing": "log group not found",
    "invalid_query": "malformed log query",
}


def unavailable_report(reason: str = "query_failed") -> ExporterAuthReport:
    """Build a bounded, public-safe ``unavailable`` report.

    Use this when the upstream CloudWatch Logs query could not run (bad/absent
    profile, authorization/throttle error, malformed query, or missing log
    group). ``reason`` is a short token normalized against a fixed allow-list;
    unknown tokens collapse to a generic phrase so no raw stderr is ever
    surfaced.
    """
    safe = _SAFE_REASONS.get(str(reason).strip().lower(), _SAFE_REASONS["query_failed"])
    return ExporterAuthReport(
        failure_count=0,
        classification=CLASSIFICATION_UNAVAILABLE,
        line_count=0,
        failure_timestamps=[],
        cold_start_adjacent=0,
        unavailable_reason=safe,
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

    Note: this function assumes the log query that produced ``events`` actually
    ran. A failed query must NOT be routed here (its empty result would look
    ``clean``); callers build :func:`unavailable_report` instead.
    """
    transitions = list(instance_transitions)
    failures = [e for e in events if is_exporter_auth_failure(e)]
    line_ts = [int(e.get("timestamp", 0)) for e in failures]
    incident_ts = _cluster_incidents(line_ts, incident_gap_ms)
    count = len(incident_ts)

    if count == 0:
        return ExporterAuthReport(
            failure_count=0,
            classification=CLASSIFICATION_CLEAN,
            line_count=len(failures),
            failure_timestamps=[],
            cold_start_adjacent=0,
            transient_budget=transient_budget,
        )

    adjacent = sum(1 for ts in incident_ts if _near_any_transition(ts, transitions, cold_start_window_ms))

    all_adjacent = adjacent == count
    within_budget = count <= transient_budget

    classification = CLASSIFICATION_TRANSIENT if (all_adjacent and within_budget) else CLASSIFICATION_PERSISTENT

    return ExporterAuthReport(
        failure_count=count,
        classification=classification,
        line_count=len(failures),
        failure_timestamps=incident_ts,
        cold_start_adjacent=adjacent,
        transient_budget=transient_budget,
    )
