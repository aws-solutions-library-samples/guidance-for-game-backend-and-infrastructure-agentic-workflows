"""OPTIONAL E3 execution control plane deployable entrypoints (issue #415).

This package holds the two thin, deployable AWS Lambda entry points the
``07-operations-execution.yaml`` stack references:

* :mod:`operations.execute.dispatcher_entry` — the JWT/admin-enforced dispatch
  handler behind the execution HTTP API. It validates the approved operation,
  enforces admin authority in code, and starts the exact Step Functions state
  machine carrying ``operation_id`` ONLY.
* :mod:`operations.execute.executor_entry` — the dedicated executor invoked by
  the state machine with ``operation_id`` ONLY. It re-loads the approved
  operation, re-checks authority, and performs the bounded, pre-approved
  GameLift capacity change on exactly the enrolled fleet.

Both entrypoints resolve a frozen environment contract and FAIL CLOSED unless
the injected ``GBAW_OPERATIONS_EXECUTION_MODE`` is ``remediate`` — this is
lever 2 of the reversible two-lever emergency disable (lever 1 is the API-stage
throttle in the stack). This slice DEFINES the frozen entrypoint + env contract;
the E3 core wires the full execution business logic behind it in a later slice.
"""

# Local modules
from operations.execute.settings import (
    ExecutionDeploymentSettings,
    resolve_execution_settings,
)

__all__ = [
    "ExecutionDeploymentSettings",
    "resolve_execution_settings",
]
