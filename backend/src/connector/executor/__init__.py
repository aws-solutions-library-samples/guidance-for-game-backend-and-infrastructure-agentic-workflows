"""Source Control Connector **executor** package (the write half).

This package owns the *write* side of the Source Control integration described in
``.kiro/specs/source-control-connector-executor``: the deterministic ``PreparationService``,
the write-once ``Prepared_Operation_Store`` + append-only ``Audit_Ledger``, the trusted
``Approval_Surface`` binding, the durable Step Functions workflow, and the isolated executor
Lambda that is the sole holder of the provider write credential.

Per Architecture Update v1.3 the write authority lives **outside** the chat/model runtime:
agents draft and recommend, deterministic services validate and prepare an exact operation, a
human approves that exact operation outside model control, and an isolated executor performs
only the approved action. The trust boundary between "draft" and "execute" lives in
infrastructure (IAM), not application code.

This foundation module set establishes the shared vocabulary and consuming seams so the
executor/preparation logic can be built and property-tested now, against in-repo default
adapters, before the accepted foundation contracts (#277–#280) land:

- :mod:`connector.executor.models` — the frozen data models (drafted change, prepared
  operation, approval record, executor event, outcomes, ...).
- :mod:`connector.executor.seams` — the four consuming ``Protocol`` interfaces this spec
  depends on (``OperationContracts277``, ``IdentityContract278``,
  ``StateRecoveryContract279``, ``ThreatsControls280``).
- :mod:`connector.executor.adapters` — in-repo **default/prototype** implementations of those
  seams, to be replaced by the accepted contracts (gated tasks 11.1–11.3).
- :mod:`connector.executor.authorization` — the two-layer authorization (capability posture,
  request-time check, effective-authority intersection, target authorization).
- :mod:`connector.executor.store` — the write-once / append-only / conditional-transition
  store adapter implementing ``StateRecoveryContract279``.

The baseline write logic (reconcile-before-retry, deterministic branch naming, base-revision
verification, intent/outcome audit, and the ``SourceControlProvider`` write subset) is
**reused** from :mod:`connector.service` and :mod:`connector.provider`, not reimplemented.
"""
