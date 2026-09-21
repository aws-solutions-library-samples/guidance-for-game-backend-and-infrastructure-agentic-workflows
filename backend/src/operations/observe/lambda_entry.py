"""AWS Lambda entrypoint for the optional E1 operations observation plane.

The frozen handler is ``operations.observe.lambda_entry.handler`` (GitHub issue
#413). This module is the stable, importable packaging seam owned by the
infrastructure agent. It reads the frozen environment contract, honors the
``GBAW_OPERATIONS_MODE`` kill switch, and fails closed with a typed 503 until the
issue #413 core observation implementation is registered.

Design constraints bound here (see ADR 0005 and the E1 runbook):

* ``GBAW_OPERATIONS_MODE`` gates the runtime: any value other than ``observe``
  is treated as disabled and the handler refuses to serve.
* The handler never fabricates a successful observation; absence of a
  registered core implementation is a fail-closed ``503`` (retryable), never a
  partial or placeholder success.
* No provider payload, fleet id, account id, or ARN is emitted in error bodies.

The handler intentionally contains no GameLift, DynamoDB, or serialization logic
of its own: core registers the observation callable via
:func:`register_observer`, keeping the provider read + persistence budget
enforcement in the owning component.
"""

from __future__ import annotations

# Standard library
import json
import os
from typing import Any, Callable, Optional

# The observation callable is registered by issue #413 core. Until then the
# handler fails closed. Kept module-private so the seam is explicit.
_Observer = Callable[[dict, Any], dict]
_observer: Optional[_Observer] = None


def register_observer(observer: _Observer) -> None:
    """Register the core observation callable.

    Issue #413 core calls this at import time to wire the real synchronous,
    read-only observation path. The infrastructure seam does not implement it.
    """
    global _observer
    _observer = observer


def _operations_mode() -> str:
    return os.environ.get("GBAW_OPERATIONS_MODE", "disabled").strip()


def _json_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def handler(event: dict, context: Any) -> dict:
    """Frozen Lambda handler entrypoint.

    Fails closed when the plane is disabled or when no core observation callable
    has been registered. Delegates to the registered observer otherwise.
    """
    if _operations_mode() != "observe":
        # Kill switch: the plane is disabled. Refuse without side effects.
        return _json_response(
            503,
            {"error": "OPERATIONS_DISABLED", "retryable": False},
        )

    if _observer is None:
        # No core implementation wired: fail closed, never a placeholder success.
        return _json_response(
            503,
            {"error": "OBSERVER_UNAVAILABLE", "retryable": True},
        )

    return _observer(event, context)
