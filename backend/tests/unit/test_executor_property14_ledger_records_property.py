#!/usr/bin/env python3
"""Property-based test for ledger recording of every attempt/outcome.

Feature: source-control-connector-executor, Property 14 (design → Correctness Properties).
For every execution — including those that fail, retry, or reconcile — each individual attempt
and the terminal state change / provider result / recovery outcome are recorded as append-only
:class:`~connector.executor.models.LedgerEntry` entries on the store (Req 4.10, 8.6, 10.6). The
generator crosses four interleavings against the failing mutating operation:

- ``clean`` — no injected failure (a straight-through executed write);
- ``reconcile`` — the op applies its effect then raises a transient error (reconciled, executed);
- ``retry`` — the op raises a transient error once then succeeds on retry (executed);
- ``error`` — the op raises a non-transient error that propagates (recorded recovery, error).

The invariant asserted holds across all of them: the ledger is non-empty, strictly append-only
(sequences are the contiguous range ``0..n-1``), records a durable ``intent``, and records a
terminal ``outcome``/``recovery`` entry consistent with the executor's returned status.
``connector.service.time.sleep`` is neutralized so retry backoff never actually waits.

Validates: Requirements 4.10, 8.6, 10.6
"""

# Standard library
import json
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import (
    DefaultIdentityContract278,
    DefaultOperationContracts277,
)
from connector.executor.approval import ApprovalService
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.handler import Executor, ExecutorDependencies
from connector.executor.models import (
    DraftedChange,
    ExecutorEvent,
    RequesterIdentity,
    RiskLevel,
    TargetSelector,
)
from connector.executor.preparation import PreparationService
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from connector.provider import ProviderConflictError, ProviderTransientError
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_WORKFLOW_ROLE = "arn:aws:iam::123456789012:role/ScmWorkflowRole"
_WRITE_SECRET = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm-write"
_MUTATING_OPS = ("create_branch", "commit_files", "open_change_proposal")
_SCENARIOS = ("clean", "reconcile", "retry", "error")


def _policy() -> AuthorizationPolicy:
    return AuthorizationPolicy(
        entries=(
            AllowlistEntry(
                repo="org/iac",
                target_branches=("main",),
                path_prefixes=("infra/",),
                extensions=(".yaml",),
            ),
        )
    )


def _layers() -> tuple[PolicyLayer, ...]:
    return (PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),)


def _acquirer(secret_arn: str, *, source: str) -> str:
    return "write-token"


@st.composite
def _cfn_files(draw: st.DrawFn) -> tuple[ProposedFile, ...]:
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    files: list[ProposedFile] = []
    for index, name in enumerate(names):
        resource_type = draw(st.sampled_from(["AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::SNS::Topic"]))
        content = json.dumps({"Resources": {f"Res{index}": {"Type": resource_type}}})
        files.append(ProposedFile(path=f"infra/{name}.yaml", content=content, iac_format="cloudformation"))
    return tuple(files)


def _prep_service(provider: FakeProvider, store: InMemoryOperationStore) -> PreparationService:
    return PreparationService(
        provider=provider,
        store=store,
        contracts=DefaultOperationContracts277(),
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        policy_layers=_layers(),
    )


def _deps(provider: FakeProvider, store: InMemoryOperationStore) -> ExecutorDependencies:
    return ExecutorDependencies(
        store=store,
        contracts=DefaultOperationContracts277(),
        provider=provider,
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        workflow_role_arn=_WORKFLOW_ROLE,
        write_secret_arn=_WRITE_SECRET,
        policy_layers=_layers(),
        credential_acquirer=_acquirer,
    )


def _prepare_and_approve(provider: FakeProvider, store: InMemoryOperationStore, files: tuple[ProposedFile, ...]) -> str:
    draft = DraftedChange(
        files=files,
        iac_format="cloudformation",
        target=TargetSelector(repository="org/iac", branch="main"),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )
    result = _prep_service(provider, store).prepare(
        draft, requester=RequesterIdentity(subject="user-1", groups=("infra",))
    )
    assert result.status == "prepared"
    ApprovalService(store=store, identity=DefaultIdentityContract278()).approve(
        result.operation_id,
        approval_ctx={"subject": "approver-1", "groups": ["infra"]},
        source="web",
    )
    return str(result.operation_id)


def _program_failure(provider: FakeProvider, scenario: str, failing_op: str) -> None:
    """Program the failing interleaving onto ``failing_op`` per the scenario."""
    if scenario == "reconcile":
        # Effect lands, then a transient error — reconcile-before-retry resolves it.
        provider.apply_then_fail(failing_op, ProviderTransientError("ambiguous transient"), times=1)
    elif scenario == "retry":
        # A transient error raised BEFORE the effect, once — the retry then succeeds.
        provider.fail_times(failing_op, ProviderTransientError("transient"), times=1)
    elif scenario == "error":
        # A non-transient error propagates immediately (recorded as recovery, status error).
        provider.fail(failing_op, ProviderConflictError("permanent conflict"))


# Feature: source-control-connector-executor, Property 14: Every execution attempt and outcome is appended to the ledger
@settings(max_examples=100)
@given(
    files=_cfn_files(),
    scenario=st.sampled_from(_SCENARIOS),
    failing_op=st.sampled_from(_MUTATING_OPS),
)
def test_property14_every_attempt_and_outcome_is_appended(
    files: tuple[ProposedFile, ...], scenario: str, failing_op: str
) -> None:
    """Across clean/reconcile/retry/error interleavings the ledger is append-only and records a
    durable intent plus a terminal outcome/recovery consistent with the returned status
    (Req 4.10, 8.6, 10.6)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store, files)
    _program_failure(provider, scenario, failing_op)

    with mock.patch("connector.service.time.sleep", return_value=None):
        outcome = Executor(_deps(provider, store)).handle(
            ExecutorEvent(operation_id=operation_id),
            {"caller_identity": _WORKFLOW_ROLE},
        )

    ledger = store.list_ledger(operation_id)
    assert ledger, "no ledger entries were appended for the execution"

    # Append-only: sequences are the contiguous, strictly increasing range 0..n-1.
    sequences = [entry.sequence for entry in ledger]
    assert sequences == list(range(len(ledger)))

    events = {entry.event for entry in ledger}
    # A durable intent is recorded before the first mutating op.
    assert "intent" in events

    if scenario == "error":
        assert outcome.status == "error"
        # The recovery/error outcome is recorded; no false success.
        assert "recovery" in events
        assert not any(entry.outcome == "executed" for entry in ledger)
    else:
        assert outcome.status == "executed"
        # A terminal executed outcome entry is present, plus per-step attempt entries.
        assert any(entry.event == "outcome" and entry.outcome == "executed" for entry in ledger)
        assert any(entry.event == "attempt" for entry in ledger)
