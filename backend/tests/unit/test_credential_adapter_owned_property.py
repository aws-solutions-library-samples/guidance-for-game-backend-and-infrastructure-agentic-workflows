#!/usr/bin/env python3
"""Property-based test that credential acquisition is adapter-owned and provider-neutral.

The v2 pass moved credential acquisition out of the connector core and into the
Provider_Adapter, behind the provider-neutral :class:`~connector.provider.ProviderAuth`
contract (Req 11.1). The connector core (``connector.service``) does not import or call
``get_secret`` at all: it never fetches a credential of its own. Instead, each adapter
acquires its credential inside ``ProviderAuth.apply`` — a token-based adapter fetches a
bearer token and sets an ``Authorization`` header, an IAM-native/SigV4 adapter signs the
outbound request with the runtime role (no secret fetch) — and both satisfy the *identical*
neutral contract. When acquisition fails the adapter raises :class:`ProviderAuthError`,
which the core maps to a safe, no-retry, secret-free error (fail-closed). A single
ARN-valued setting is the sole credential source (Req 11.2).

This test proves Property V2 as a universally-quantified statement over
``operation × auth_outcome × auth_model``:

  1. **Adapter-owned acquisition.** The connector core exposes no ``get_secret`` symbol and
     issues zero credential fetches; the *only* place credentials are acquired is the
     installed :class:`ProviderAuth.apply` (spied), and the flow never touches Secrets
     Manager directly.
  2. **Fail-closed, no retry.** A credential-acquisition failure surfaces as a
     :class:`ProviderAuthError` that fails the operation closed without retrying the
     auth-bearing operation and without attempting any provider mutation.
  3. **Interchangeable credential models.** A token-based auth and an IAM-native/SigV4 auth
     satisfy the identical ``ProviderAuth`` contract, and the core behaves identically
     regardless of which model is installed (same outcome, same provider-operation
     sequence).
  4. **No credential leakage.** The acquired credential value never appears in the returned
     result nor in any audit/log field.

Validates: Requirements 11.1, 11.2
"""

# Standard library
import itertools
import string
from typing import Any
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
import utils.security as security
from connector import service
from connector.config import AllowlistEntry, SourceControlConfig
from connector.models import ProposedFile
from connector.provider import OutboundRequest, ProviderAuth, ProviderAuthError
from connector.service import propose_change, read_iac_files
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit


# --- Fixed request/config fixtures -----------------------------------------

_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"

# The single ARN-valued credential setting (AdapterConfig.credential_secret_arn).
_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm/github-token-AbCdEf"

# A benign, injection-free intent/title/description so the terminal outcome is decided by
# the credential model under test, not the input-validation gate.
_INTENT = "update the storage bucket configuration in the service infrastructure template"
_TITLE = "Update bucket configuration"
_DESCRIPTION = "Adjust the storage bucket settings to match the requested configuration."

# Structurally valid CloudFormation so IaC validation passes and propose reaches provider ops.
_VALID_CFN = "Resources:\n  MyBucket:\n    Type: AWS::S3::Bucket\n"

# Deterministic base head SHA for the target branch.
_BASE_SHA = "basesha0000000000000000000000000000000000"

# Monotonic source of unique user ids so each propose starts with a fresh rate-limit budget.
_user_ids = itertools.count(1)


# --- Fake, provider-neutral ProviderAuth (token + IAM-native models) -------


class _FakeProviderAuth(ProviderAuth):
    """A small fake :class:`ProviderAuth` covering both credential models.

    Never touches real Secrets Manager. It records every ``apply`` invocation (so the test
    can spy on where credential acquisition happens) and models two interchangeable schemes:

    - ``"token"``: acquires a bearer token and sets an ``Authorization: Bearer`` header
      (mirrors :class:`~connector.github_provider.GitHubTokenAuth`).
    - ``"iam_native"``: signs the outbound request with SigV4-style signature headers using
      the runtime identity — no token fetch at all.

    When ``fails`` is set, acquisition fails closed with a :class:`ProviderAuthError` and no
    credential material is attached, exactly as a real adapter would on an unavailable
    credential (Req 11.1).
    """

    def __init__(self, *, model: str, credential: str, fails: bool) -> None:
        self.model = model
        self.credential = credential
        self.fails = fails
        self.apply_calls = 0

    def apply(self, request: OutboundRequest) -> None:
        self.apply_calls += 1
        if self.fails:
            raise ProviderAuthError("source-control credential could not be acquired")
        if self.model == "token":
            request.headers["Authorization"] = f"Bearer {self.credential}"
        else:  # iam_native / SigV4 stub — signs with the runtime role, no secret fetch.
            request.headers["Authorization"] = (
                "AWS4-HMAC-SHA256 Credential=runtime-role/20240101/us-west-2/codecommit/aws4_request"
            )
            request.headers["X-Amz-Date"] = "20240101T000000Z"
            request.headers["X-Amz-Security-Token"] = self.credential


class _AuthProvider(FakeProvider):
    """A ``FakeProvider`` that acquires its credential via ``ProviderAuth`` on each op.

    Mirrors a real Provider_Adapter: before delegating to the in-memory fake behavior, each
    provider operation acquires the credential through the neutral ``ProviderAuth`` contract
    (attaching it to a throwaway outbound request). This confines credential acquisition to
    the adapter layer exactly as production does, so the "core issues zero get_secret"
    property is exercised rather than vacuous. If ``apply`` raises, the operation fails
    before any effect is recorded.
    """

    def __init__(self, auth: ProviderAuth) -> None:
        super().__init__()
        self._auth = auth

    def _authenticate(self) -> None:
        self._auth.apply(OutboundRequest(headers={}))

    def get_file(self, *args: Any, **kwargs: Any):
        self._authenticate()
        return super().get_file(*args, **kwargs)

    def get_files(self, *args: Any, **kwargs: Any):
        self._authenticate()
        return super().get_files(*args, **kwargs)

    def branch_exists(self, *args: Any, **kwargs: Any) -> bool:
        self._authenticate()
        return super().branch_exists(*args, **kwargs)

    def latest_commit_sha(self, *args: Any, **kwargs: Any) -> str:
        self._authenticate()
        return super().latest_commit_sha(*args, **kwargs)

    def create_branch(self, *args: Any, **kwargs: Any) -> None:
        self._authenticate()
        return super().create_branch(*args, **kwargs)

    def commit_files(self, *args: Any, **kwargs: Any) -> str:
        self._authenticate()
        return super().commit_files(*args, **kwargs)

    def open_change_proposal(self, *args: Any, **kwargs: Any):
        self._authenticate()
        return super().open_change_proposal(*args, **kwargs)

    def find_open_change_proposal(self, *args: Any, **kwargs: Any):
        self._authenticate()
        return super().find_open_change_proposal(*args, **kwargs)


def _make_config() -> SourceControlConfig:
    """Build an enabled SourceControlConfig backed by a single allowlist entry."""
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_arn=_ARN,
        allowlist=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),),
        authorized_groups=(_GROUP,),
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def _proposed_files() -> list[ProposedFile]:
    """A single valid CloudFormation file to propose."""
    return [ProposedFile(path="template.yaml", content=_VALID_CFN, iac_format="cloudformation")]


def _make_provider(auth_model: str, auth_outcome: str, credential: str) -> tuple[_AuthProvider, _FakeProviderAuth]:
    """Build an authenticating provider + its installed fake ProviderAuth for a scenario."""
    auth = _FakeProviderAuth(
        model=auth_model, credential=credential, fails=(auth_outcome == "fails")
    )
    provider = _AuthProvider(auth)
    provider.set_head(_REPO, _BRANCH, _BASE_SHA)
    provider.add_file(_REPO, _BRANCH, "template.yaml", _VALID_CFN)
    return provider, auth


def _logger_blob(mock_logger: MagicMock) -> str:
    """Flatten every recorded logger call (name + args + kwargs) into one searchable string."""
    parts: list[str] = []
    for name, args, kwargs in mock_logger.mock_calls:
        parts.append(str(name))
        parts.extend(repr(arg) for arg in args)
        parts.extend(f"{key}={value!r}" for key, value in kwargs.items())
    return "\n".join(parts)


def _invoke(operation: str, provider: _AuthProvider, config: SourceControlConfig):
    """Invoke the requested connector entry point with the fixed, valid request."""
    if operation == "propose_change":
        return propose_change(
            _INTENT,
            _proposed_files(),
            iac_format="cloudformation",
            title=_TITLE,
            description=_DESCRIPTION,
            repository=_REPO,
            target_branch=_BRANCH,
            config=config,
            provider=provider,
        )
    return read_iac_files(
        ["template.yaml"],
        repository=_REPO,
        target_branch=_BRANCH,
        config=config,
        provider=provider,
    )


def _run(operation: str, auth_model: str, auth_outcome: str, credential: str, user_id: str) -> dict[str, Any]:
    """Run one scenario and return a normalized, comparable outcome.

    Patches ``connector.service.logger`` to capture the audit surface, guards that neither
    the core nor the adapter touch Secrets Manager (``get_secret`` is never called), and
    normalizes the terminal outcome so token and IAM-native runs can be compared directly.
    """
    security._rate_limit_windows.clear()
    provider, auth = _make_provider(auth_model, auth_outcome, credential)
    config = _make_config()

    status = ""
    result_repr = ""
    raised_auth_error = False
    audit_blob = ""

    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "sess"})
    try:
        with (
            patch("connector.service.logger", new=MagicMock()) as mock_logger,
            patch("utils.secrets.get_secret", new=MagicMock()) as core_secret_fetch,
            patch("connector.github_provider.get_secret", new=MagicMock()) as adapter_secret_fetch,
        ):
            try:
                out = _invoke(operation, provider, config)
                if operation == "propose_change":
                    status = out.status
                    result_repr = f"{out.status}|{out.message}|{out.proposal_id}|{out.proposal_url}"
                else:
                    status = "read_ok"
                    result_repr = repr(out)
            except ProviderAuthError as exc:
                raised_auth_error = True
                status = "auth_error"
                result_repr = repr(exc)
            audit_blob = _logger_blob(mock_logger)

        # No credential fetch is issued by the core or through the real Secrets Manager
        # helper: acquisition is entirely adapter-owned behind ProviderAuth (Req 11.1).
        assert not core_secret_fetch.called
        assert not adapter_secret_fetch.called
    finally:
        reset_request_context(token)

    return {
        "status": status,
        "raised_auth_error": raised_auth_error,
        "apply_calls": auth.apply_calls,
        "created_branches": list(provider.created_branches),
        "commits": list(provider.commits),
        "pull_requests": list(provider.pull_requests),
        "op_sequence": list(provider.call_operations),
        "blob": result_repr + "\n" + audit_blob,
    }


# --- Strategies ------------------------------------------------------------

# Random, high-entropy, secret-looking credential values so an accidental collision with
# ordinary output words is effectively impossible and a match would be a genuine leak.
_credentials = st.builds(
    lambda prefix, body: prefix + body,
    st.sampled_from(["ghp_", "ghs_", "github_pat_11A", "xoxb-", "AKIA", "glpat-"]),
    st.text(alphabet=string.ascii_letters + string.digits, min_size=24, max_size=48),
)

_operations = st.sampled_from(["read_iac_files", "propose_change"])
_auth_outcomes = st.sampled_from(["acquired", "fails"])
_auth_models = st.sampled_from(["token", "iam_native"])


# --- Property V2 -----------------------------------------------------------


# Feature: source-control-connector-v2, Property V2: credential acquisition is adapter-owned behind a neutral auth contract
@settings(max_examples=100)
@given(
    operation=_operations,
    auth_outcome=_auth_outcomes,
    auth_model=_auth_models,
    credential=_credentials,
)
def test_credential_acquisition_is_adapter_owned(operation, auth_outcome, auth_model, credential):
    """Credential acquisition is adapter-owned behind a neutral, interchangeable contract.

    For any operation, credential model, and acquisition outcome: the connector core issues
    no credential fetch (adapter-owned via ``ProviderAuth``), an acquisition failure fails
    closed with no retry and no mutation, a token adapter and an IAM-native adapter are
    interchangeable with identical core behavior, and the credential never leaks into the
    result or audit (Req 11.1, 11.2).
    """
    # (1) The connector core has no ``get_secret`` symbol: it cannot fetch a credential.
    assert not hasattr(service, "get_secret"), (
        "connector.service must not import or call get_secret; credential acquisition is "
        "adapter-owned behind ProviderAuth"
    )

    primary = _run(operation, auth_model, auth_outcome, credential, f"user-{next(_user_ids)}")

    # (1) Credential acquisition happened only through the installed ProviderAuth.apply.
    assert primary["apply_calls"] >= 1

    if auth_outcome == "fails":
        # (2) Fail-closed with no retry of the auth-bearing op and no provider mutation.
        assert primary["apply_calls"] == 1, "credential-acquisition failure was retried"
        assert primary["created_branches"] == []
        assert primary["commits"] == []
        assert primary["pull_requests"] == []
        if operation == "propose_change":
            assert primary["status"] == "error"
        else:
            assert primary["raised_auth_error"] is True
    else:
        if operation == "propose_change":
            assert primary["status"] == "created"
            assert len(primary["pull_requests"]) == 1
        else:
            assert primary["status"] == "read_ok"

    # (4) The credential value never appears in the result or any audit/log field.
    assert credential not in primary["blob"], "credential leaked from the primary run"

    # (3) Interchangeability: the other credential model yields identical core behavior.
    other_model = "iam_native" if auth_model == "token" else "token"
    other = _run(operation, other_model, auth_outcome, credential, f"user-{next(_user_ids)}")

    assert other["status"] == primary["status"]
    assert other["raised_auth_error"] == primary["raised_auth_error"]
    assert other["op_sequence"] == primary["op_sequence"]
    assert len(other["pull_requests"]) == len(primary["pull_requests"])
    assert other["apply_calls"] == primary["apply_calls"]
    assert credential not in other["blob"], "credential leaked from the interchangeable run"
