"""Deterministic E5 bounded-autonomy runtime service (issue #439, track A).

``AutonomyRuntimeService`` is a pure, deterministic *assembler*. Given only
server-owned trusted inputs it produces the two hash-bound v2 documents defined
by the frozen issue #438 contract:

* the **autonomous decision** — the deterministic, time-boxed authorization. A
  non-denied decision is ``authorized`` at ``operate`` authority with the single
  reason code ``APPROVED_AUTONOMOUS``; any guardrail breach denies with the
  specific closed reason code(s). It is *never* a human approval and carries no
  human-approval field.
* the **prepared autonomous operation** — the immutable, idempotent intent an
  autonomous executor would carry forward, whose ``authority.decision`` mirrors
  the v1 prepared operation with ``authorized``/``denied``.

The service delegates every semantic decision to the frozen #438 contract
(:func:`operations.contracts.autonomy.evaluate_autonomy_policy` and the contract
validators). It computes no risk, no bounds, and no reason codes of its own: it
reads the deterministic evaluator result and binds it into hash-bound documents,
then re-validates the pair through :func:`validate_autonomous_operation_binding`
so a produced document that fails the frozen contract fails closed here.

The one canonical E1 observation is projected onto the exact policy target via
the frozen contract's own projector, so the embedded ``current_state`` evidence
is byte-identical to what the binding validator recomputes.

**Boundaries this module enforces (ADR 0001 / AGENTS.md):**

* It accepts **no** model or request-body input for identity, policy, limits,
  authorization, playbook, executor, or credentials. The only inputs are a
  resolved server-owned policy, one canonical E1 observation, the six trusted
  authority inputs, the trusted automation principal, the strict durable window
  state, the proposed capacity triple, a correlation envelope, and the evaluated
  instant. Unknown keyword arguments are rejected (``TypeError``).
* It performs no AWS, runtime, or infrastructure work, reads no environment,
  holds no credential, and exposes no dispatch/execute/provider-write method.
* Bounded autonomy is **default disabled**: a ``disabled`` deployment mode (or
  any authority below ``operate``) yields a truthful ``denied`` decision.
"""

from __future__ import annotations

# Standard library
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Local modules
from operations.autonomy_playbook_definition import autonomy_playbook_definition
from operations.contracts.autonomy import (
    AUTONOMY_CONTRACT_VERSION,
    REQUIRED_EXECUTION_AUTHORITY,
    autonomous_decision_hash,
    autonomous_prepared_hash,
    evaluate_autonomy_policy,
    project_observation_evidence,
    validate_autonomous_operation_binding,
    validate_autonomy_contract,
)

_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_UNIT_SEPARATOR = "\u001f"

_DECISION_SCHEMA = "gamelift-capacity-autonomous-decision"
_OPERATION_SCHEMA = "gamelift-capacity-autonomous-operation"


class AutonomyRuntimeError(ValueError):
    """A runtime-assembly precondition was violated (fail closed)."""


@dataclass(frozen=True, slots=True)
class AutonomyRuntimeInputs:
    """The complete, trusted, server-owned input tuple for one evaluation.

    Every field is server-owned. There is deliberately no field for a model or
    request-body identity, a client-supplied policy, client-supplied limits, an
    executor credential, or a human approval. ``slots=True`` makes any unknown
    keyword argument a ``TypeError`` at construction, so a caller cannot smuggle
    a forbidden field through.
    """

    policy: dict[str, Any]
    observation: dict[str, Any]
    authority_inputs: dict[str, str]
    automation_principal: dict[str, str]
    window_state: dict[str, Any]
    requested: dict[str, int]
    correlation: dict[str, str]
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class PreparedAutonomousOperation:
    """The hash-bound decision + prepared operation produced by the runtime."""

    decision: dict[str, Any]
    operation: dict[str, Any]
    decision_hash: str
    prepared_hash: str

    @property
    def authorized(self) -> bool:
        """Whether the deterministic decision authorized the single write."""
        return bool(self.decision["decision"] == "authorized")


def _hex_to_id_body(digest: str) -> str:
    """Map a hex digest onto the ``[a-z0-9]{26}`` id body used by operation ids."""
    value = int(digest, 16)
    body: list[str] = []
    for _ in range(26):
        value, index = divmod(value, 36)
        body.append(_ID_ALPHABET[index])
    return "".join(reversed(body))


def _require_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AutonomyRuntimeError("evaluated_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    """Render a UTC instant as an RFC 3339 ``...Z`` timestamp (no microseconds)."""
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    """Parse an RFC 3339 instant, failing closed on a naive (offset-less) value.

    ``datetime.astimezone`` on a *naive* datetime silently reinterprets it in the
    host's local timezone, so a timestamp without an explicit UTC marker would
    skew the decision/window expiry by the host's UTC offset. The runtime must
    never guess a timezone: an offset-less timestamp is rejected rather than
    assumed to be local time.
    """
    if not isinstance(value, str):
        raise AutonomyRuntimeError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutonomyRuntimeError("timestamp is not a valid ISO-8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutonomyRuntimeError("timestamp must carry an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


class AutonomyRuntimeService:
    """Pure deterministic assembler of v2 autonomous decisions and operations.

    Stateless: constructing the service captures no client, credential, or
    configuration. Repeated calls with identical inputs return byte-identical
    documents and identifiers.
    """

    __slots__ = ()

    def prepare(self, inputs: AutonomyRuntimeInputs) -> PreparedAutonomousOperation:
        """Produce the hash-bound decision + prepared operation for one request.

        Delegates the entire authorization judgment to the frozen #438 evaluator,
        binds the deterministic result into the two v2 documents, and re-validates
        the pair through the frozen binding validator. Any contract failure
        raises (fail closed); no partial or unvalidated document is returned.
        """
        policy = inputs.policy
        evaluated_at = _require_utc(inputs.evaluated_at)
        now_epoch = int(evaluated_at.timestamp())

        # The trusted observation is projected onto the exact policy target /
        # location / tenant / workspace / enrollment via the frozen contract's
        # own projector. This validates the E1 observation and rejects any
        # observation that is not the policy's own.
        evidence = project_observation_evidence(inputs.observation, policy)

        # The single source of truth for the authorization judgment.
        evaluation = evaluate_autonomy_policy(
            policy=policy,
            authority_inputs=inputs.authority_inputs,
            automation_principal=inputs.automation_principal,
            observation=evidence,
            requested=inputs.requested,
            window_state=inputs.window_state,
            now_epoch_seconds=now_epoch,
        )

        decision_expires_at = self._decision_expiry(evidence, policy, evaluated_at)
        operation_id = self._operation_id(policy, evidence, inputs.requested, inputs.window_state)
        decision_id = f"autz.{operation_id}"

        decision = self._build_decision(
            inputs=inputs,
            policy=policy,
            evidence=evidence,
            evaluation=evaluation,
            decision_id=decision_id,
            evaluated_at=_iso_z(evaluated_at),
            decision_expires_at=decision_expires_at,
        )
        decision_hash = autonomous_decision_hash(decision)

        operation = self._build_operation(
            inputs=inputs,
            policy=policy,
            evidence=evidence,
            evaluation=evaluation,
            decision=decision,
            decision_hash=decision_hash,
            operation_id=operation_id,
            created_at=evidence["observed_at"],
            decision_expires_at=decision_expires_at,
        )
        prepared_hash = operation["prepared_hash"]

        # Re-validate the produced pair against the frozen #438 contract. A
        # document the service assembled that does not satisfy the binding must
        # fail closed rather than be returned.
        validate_autonomous_operation_binding(operation, decision, policy, inputs.observation, inputs.window_state)

        return PreparedAutonomousOperation(
            decision=decision,
            operation=operation,
            decision_hash=decision_hash,
            prepared_hash=prepared_hash,
        )

    # -- Assembly helpers ----------------------------------------------------

    def _decision_expiry(self, evidence: dict[str, Any], policy: dict[str, Any], evaluated_at: datetime) -> str:
        observation_expiry = _parse_iso(evidence["expires_at"])
        ttl_deadline = evaluated_at + timedelta(seconds=policy["decision_ttl_seconds"])
        return _iso_z(min(observation_expiry, ttl_deadline))

    def _operation_id(
        self,
        policy: dict[str, Any],
        evidence: dict[str, Any],
        requested: dict[str, int],
        window_state: dict[str, Any],
    ) -> str:
        """Derive a deterministic operation id from the trusted idempotent intent.

        A retry of the same policy revision, observation, requested change, and
        window-state revision reproduces the same id (and therefore the same
        prepared hash). No model or request-body value contributes.
        """
        fingerprint = _UNIT_SEPARATOR.join(
            [
                policy["policy_id"],
                policy["policy_version"],
                policy["policy_hash"],
                evidence["target"]["fleet_id"],
                evidence["target"]["location"],
                evidence["observation_hash"],
                str(requested["desired"]),
                str(requested["minimum"]),
                str(requested["maximum"]),
                window_state["state_id"],
                str(window_state["state_revision"]),
            ]
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"op_{_hex_to_id_body(digest)}"

    def _idempotency_token(self, operation_id: str) -> str:
        """Derive the idempotency token deterministically from the operation id."""
        body = _hex_to_id_body(hashlib.sha256(operation_id.encode("utf-8")).hexdigest())
        return f"idem_{body}{body[:2]}"

    def _build_decision(
        self,
        *,
        inputs: AutonomyRuntimeInputs,
        policy: dict[str, Any],
        evidence: dict[str, Any],
        evaluation: dict[str, Any],
        decision_id: str,
        evaluated_at: str,
        decision_expires_at: str,
    ) -> dict[str, Any]:
        decision = {
            "decision_contract_version": AUTONOMY_CONTRACT_VERSION,
            "decision_id": decision_id,
            "phase": "operate",
            "profile": policy["profile"],
            "action": policy["action"],
            "provider": policy["provider"],
            "capability": dict(policy["capability"]),
            "automation_principal": dict(inputs.automation_principal),
            "policy": {
                "policy_id": policy["policy_id"],
                "policy_version": policy["policy_version"],
                "policy_hash": policy["policy_hash"],
            },
            "target": dict(policy["target"]),
            "current_state": evidence,
            "window_state": {
                "state_id": inputs.window_state["state_id"],
                "state_revision": inputs.window_state["state_revision"],
                "state_hash": inputs.window_state["state_hash"],
            },
            "requested": dict(inputs.requested),
            "calculated_risk": evaluation["calculated_risk"],
            "authority_inputs": dict(inputs.authority_inputs),
            "effective_authority": evaluation["effective_authority"],
            "decision": evaluation["decision"],
            "reason_codes": list(evaluation["reason_codes"]),
            "required_execution_authority": REQUIRED_EXECUTION_AUTHORITY,
            "evaluated_at": evaluated_at,
            "decision_expires_at": decision_expires_at,
            "correlation": dict(inputs.correlation),
        }
        validate_autonomy_contract(_DECISION_SCHEMA, decision)
        return decision

    def _build_operation(
        self,
        *,
        inputs: AutonomyRuntimeInputs,
        policy: dict[str, Any],
        evidence: dict[str, Any],
        evaluation: dict[str, Any],
        decision: dict[str, Any],
        decision_hash: str,
        operation_id: str,
        created_at: str,
        decision_expires_at: str,
    ) -> dict[str, Any]:
        change = evaluation["change"]
        operation = {
            "operation_contract_version": AUTONOMY_CONTRACT_VERSION,
            "operation_id": operation_id,
            "idempotency_token": self._idempotency_token(operation_id),
            "phase": "operate",
            "profile": policy["profile"],
            "action": policy["action"],
            "provider": policy["provider"],
            "created_at": created_at,
            "decision_expires_at": decision_expires_at,
            "playbook": dict(policy["playbook"]),
            "capability": dict(policy["capability"]),
            "automation_principal": {
                **inputs.automation_principal,
                "tenant_id": policy["tenant_id"],
                "workspace_id": policy["workspace_id"],
            },
            "correlation": dict(inputs.correlation),
            "target": dict(policy["target"]),
            "parameters": {
                "requested": dict(inputs.requested),
                "change": dict(change),
            },
            "current_state": evidence,
            "window_state": {
                "state_id": inputs.window_state["state_id"],
                "state_revision": inputs.window_state["state_revision"],
                "state_hash": inputs.window_state["state_hash"],
            },
            "resource_enrollment": dict(policy["resource_enrollment"]),
            "autonomy_policy": {
                "policy_id": policy["policy_id"],
                "policy_version": policy["policy_version"],
                "policy_hash": policy["policy_hash"],
            },
            "decision": {"decision_id": decision["decision_id"], "decision_hash": decision_hash},
            "calculated_risk": evaluation["calculated_risk"],
            "authority": {
                "authority_inputs": dict(inputs.authority_inputs),
                "effective_authority": evaluation["effective_authority"],
                "decision": evaluation["decision"],
                "reason_codes": list(evaluation["reason_codes"]),
            },
            "retry_policy": autonomy_playbook_definition()["retry_policy"],
            "executor_binding": dict(policy["executor_binding"]),
            "required_execution_authority": REQUIRED_EXECUTION_AUTHORITY,
        }
        operation["prepared_hash"] = autonomous_prepared_hash(operation)
        validate_autonomy_contract(_OPERATION_SCHEMA, operation)
        return operation
