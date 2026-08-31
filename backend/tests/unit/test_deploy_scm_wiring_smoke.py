#!/usr/bin/env python3
"""Smoke test: ``scripts/deploy.sh`` wires the Source Control Connector read-only.

These are fast text/structure assertions over ``scripts/deploy.sh`` (no AWS calls, no
shelling out to the deploy). They pin the read-only-split (issue #268) deploy invariants:

* **No write-credential wiring** — the removed ``GBAW_SCM_CREDENTIAL_SECRET_ARN`` env name,
  the ``$SCM_CREDENTIAL_SECRET_ARN`` shell variable, and the ``ScmCredentialSecretArn``
  base-stack parameter are ABSENT everywhere in deploy.sh.
* **Single source / no drift** — the SAME resolved value
  (``$SCM_READ_CREDENTIAL_SECRET_ARN``) drives BOTH the ``ScmReadCredentialSecretArn``
  base-stack parameter (Step 1) AND the ``GBAW_SCM_READ_CREDENTIAL_SECRET_ARN`` runtime env
  var (Step 5b), so the runtime credential-acquisition config and the scoped IAM grant
  cannot diverge (Req 3.3).
* **KB-independent audit destination** — the ``GBAW_SCM_*`` runtime env args (including
  ``GBAW_SCM_AUDIT_LOG_GROUP``) are emitted under their own guards, never gated on the
  Knowledge Base IDs, and ``LAUNCH_ENV_ARGS`` appends ``SCM_ENV_ARGS`` outside any KB
  conditional.
* **ARN-only, no raw credential value** — only the ARN variable/env-name flows through.

Validates: Requirements 3.3
"""

# Standard library
import re
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


# --- Locate deploy.sh ---------------------------------------------------------------------

# tests/unit/<this file> -> tests/unit -> tests -> backend -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_PATH = _REPO_ROOT / "scripts" / "deploy.sh"

# The single resolved shell variable that both destinations must read from (read credential).
_ARN_VAR = "SCM_READ_CREDENTIAL_SECRET_ARN"
_ARN_ENV_NAME = "GBAW_SCM_READ_CREDENTIAL_SECRET_ARN"
_BASE_PARAM_NAME = "ScmReadCredentialSecretArn"

# Removed write-credential wiring — must not reappear anywhere in deploy.sh.
_REMOVED_ENV_NAME = "GBAW_SCM_CREDENTIAL_SECRET_ARN"
_REMOVED_ARN_VAR = "SCM_CREDENTIAL_SECRET_ARN"
_REMOVED_BASE_PARAM_NAME = "ScmCredentialSecretArn"
# The removed legacy (non-ARN) credential setting; must not reappear either.
_REMOVED_SECRET_ID_ENV_NAME = "GBAW_SCM_CREDENTIAL_SECRET_ID"

# Shell variables holding Knowledge Base IDs — the SCM wiring must be independent of these.
_KB_ID_VARS = ("GAMELIFT_KB_ID", "EKS_KB_ID", "COST_KB_ID")


# --- Fixtures -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def deploy_text() -> str:
    """Return the raw text of ``scripts/deploy.sh``."""
    assert _DEPLOY_PATH.is_file(), f"deploy.sh not found at {_DEPLOY_PATH}"
    return _DEPLOY_PATH.read_text()


@pytest.fixture(scope="module")
def deploy_lines(deploy_text: str) -> list:
    """Return ``deploy.sh`` split into individual lines."""
    return deploy_text.splitlines()


# --- Helpers ------------------------------------------------------------------------------


def _enclosing_guard(lines: list, target_substr: str) -> str:
    """Return the nearest preceding ``if`` guard line for the line containing target_substr.

    Walks backwards from the matched line to the closest line whose stripped form starts
    with ``if `` — i.e. the immediate one-level shell guard the statement sits under.
    """
    index = next(
        (i for i, line in enumerate(lines) if target_substr in line and not line.lstrip().startswith("#")),
        None,
    )
    assert index is not None, f"could not find a non-comment line containing: {target_substr!r}"
    for line in reversed(lines[:index]):
        stripped = line.strip()
        if stripped.startswith("if "):
            return stripped
    raise AssertionError(f"no enclosing 'if' guard found for: {target_substr!r}")


def _non_comment_text(text: str) -> str:
    """Return deploy.sh text with whole-line and trailing ``#`` comments stripped.

    Used for assertions about what the script *does* (not what its comments mention), so a
    reference to a name inside an explanatory comment never satisfies or breaks a test.
    """
    out = []
    for line in text.splitlines():
        # Drop a trailing comment while ignoring '#' inside single/double quotes.
        in_single = in_double = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


# --- Tests: no write-credential wiring ----------------------------------------------------


def test_removed_write_credential_env_name_absent(deploy_text: str):
    """(Req 3.3) The removed write-credential env name ``GBAW_SCM_CREDENTIAL_SECRET_ARN`` does
    not appear anywhere in deploy.sh (comments included)."""
    assert _REMOVED_ENV_NAME not in deploy_text, f"{_REMOVED_ENV_NAME} write wiring must be removed"


def test_removed_write_credential_shell_var_absent(deploy_text: str):
    """(Req 3.3) The removed write-credential shell variable ``$SCM_CREDENTIAL_SECRET_ARN`` is
    gone. Guarded with word boundaries so the read variable (a superstring) is not matched."""
    non_comment = _non_comment_text(deploy_text)
    assert not re.search(rf"\b{_REMOVED_ARN_VAR}\b", non_comment), (
        f"{_REMOVED_ARN_VAR} write shell variable must be removed"
    )


def test_removed_write_base_stack_param_absent(deploy_text: str):
    """(Req 3.3) The removed ``ScmCredentialSecretArn=`` base-stack parameter is gone. Word
    boundary avoids matching the read param ``ScmReadCredentialSecretArn``."""
    non_comment = _non_comment_text(deploy_text)
    assert not re.search(rf"(?<!Read){_REMOVED_BASE_PARAM_NAME}=", non_comment), (
        f"{_REMOVED_BASE_PARAM_NAME}= write base-stack param must be removed"
    )


def test_removed_legacy_secret_id_absent(deploy_text: str):
    """(Req 3.3) The removed legacy ``GBAW_SCM_CREDENTIAL_SECRET_ID`` setting is absent."""
    assert _REMOVED_SECRET_ID_ENV_NAME not in deploy_text, (
        f"{_REMOVED_SECRET_ID_ENV_NAME} must be absent"
    )


# --- Tests: single source / no drift (read credential) ------------------------------------


def test_read_arn_resolved_once_from_env_or_env_local(deploy_text: str):
    """(Req 3.3) The read-credential ARN is resolved once into ``$SCM_READ_CREDENTIAL_SECRET_ARN``
    from the environment or backend/.env.local — a single source of truth."""
    pattern = re.compile(rf'^{_ARN_VAR}="\$\{{{_ARN_ENV_NAME}:-.*\}}"', re.MULTILINE)
    assert pattern.search(deploy_text), (
        f"{_ARN_VAR} must be resolved once from ${_ARN_ENV_NAME} (env or .env.local)"
    )


def test_same_read_arn_source_drives_base_param_and_runtime_env(deploy_text: str):
    """(Req 3.3 + PR #319 finding 3) The read-credential ARN is single-sourced from
    ``$SCM_READ_CREDENTIAL_SECRET_ARN``, then gated on the connector being ENABLED before it
    reaches BOTH the ``ScmReadCredentialSecretArn`` base-stack parameter AND the
    ``GBAW_SCM_READ_CREDENTIAL_SECRET_ARN`` runtime env var — single source, no drift, and no
    secret wiring on a disabled deployment."""
    non_comment = _non_comment_text(deploy_text)

    # The enablement-gated intermediate is derived from the single resolved read ARN.
    assert re.search(
        rf'SCM_BASE_READ_ARN="\${_ARN_VAR}"', non_comment
    ), "the base-stack ARN must be gated via $SCM_BASE_READ_ARN derived from $SCM_READ_CREDENTIAL_SECRET_ARN"
    # The base-stack parameter reads the gated intermediate (empty when disabled).
    assert re.search(
        rf'{_BASE_PARAM_NAME}="\$SCM_BASE_READ_ARN"', non_comment
    ), "base-stack ScmReadCredentialSecretArn parameter must read $SCM_BASE_READ_ARN"
    # The runtime env var reads the SAME single-sourced read ARN.
    assert re.search(
        rf"{_ARN_ENV_NAME}=\${_ARN_VAR}\b", non_comment
    ), "runtime GBAW_SCM_READ_CREDENTIAL_SECRET_ARN must read the SAME $SCM_READ_CREDENTIAL_SECRET_ARN"
    # The base stack is also told the enablement flag.
    assert re.search(
        r'ScmConnectorEnabled="\$SCM_CONNECTOR_ENABLED"', non_comment
    ), "the base stack must receive ScmConnectorEnabled=$SCM_CONNECTOR_ENABLED"


def test_read_credential_env_and_base_param_gated_on_enabled(deploy_text: str):
    """(PR #319 finding 3) Both the runtime read-credential env var and the base-stack ARN are
    gated on ``$SCM_CONNECTOR_ENABLED`` being 'true', so a disabled deployment carries no
    connector secret env var and no connector secret grant."""
    non_comment = _non_comment_text(deploy_text)
    # The runtime env append requires the connector to be enabled.
    assert re.search(
        r'if\s+\[\s+"\$SCM_CONNECTOR_ENABLED"\s+=\s+"true"\s+\]\s+&&\s+\[\s+-n\s+"\$SCM_READ_CREDENTIAL_SECRET_ARN"',
        non_comment,
    ), "runtime read-credential env append must be gated on $SCM_CONNECTOR_ENABLED=true"
    # The base-stack ARN intermediate is empty unless enabled.
    assert re.search(
        r'if\s+\[\s+"\$SCM_CONNECTOR_ENABLED"\s+=\s+"true"\s+\];\s+then\s+SCM_BASE_READ_ARN="\$SCM_READ_CREDENTIAL_SECRET_ARN"',
        non_comment,
    ), "SCM_BASE_READ_ARN must be the ARN only when enabled, else empty"


# --- Tests: KB-independent audit destination + env wiring ---------------------------------


def test_audit_log_group_env_var_wired(deploy_text: str):
    """(Req 3.3) The audit destination env var ``GBAW_SCM_AUDIT_LOG_GROUP`` is part of the
    wired ``GBAW_SCM_*`` runtime env set."""
    assert "GBAW_SCM_AUDIT_LOG_GROUP" in _non_comment_text(
        deploy_text
    ), "GBAW_SCM_AUDIT_LOG_GROUP must be wired through the runtime env args"


def test_max_content_bytes_env_var_wired(deploy_lines: list):
    """(PR #319 finding 6) The content-size limit env var ``GBAW_SCM_MAX_CONTENT_BYTES`` is
    part of the ``GBAW_SCM_*`` names iterated by the runtime env loop, so a configured value
    reaches the runtime and replaces the default.

    The loop iterates line-continued (``\\``) bare env-var NAMES; assert the name appears as
    its own iterated token (a loop line), not merely somewhere in the file text."""
    loop_tokens = set()
    for line in deploy_lines:
        token = line.strip().rstrip("\\").strip()
        # The final env-var name on the `for` list line ends with `; do`; strip it so the
        # bare name is captured.
        if token.endswith("; do"):
            token = token[: -len("; do")].strip()
        if token.startswith("GBAW_SCM_"):
            loop_tokens.add(token)
    assert "GBAW_SCM_MAX_CONTENT_BYTES" in loop_tokens, (
        "GBAW_SCM_MAX_CONTENT_BYTES must be iterated by the runtime env loop so the configured "
        f"content-size limit reaches the runtime; loop tokens seen: {sorted(loop_tokens)}"
    )
    # The existing content-cap default lives in connector.config; the deploy loop only needs
    # to forward the env var when set (same guard as every other GBAW_SCM_* value).
    assert "GBAW_SCM_MAX_FILES_PER_REQUEST" in loop_tokens, "sibling GBAW_SCM_* names must remain wired"


def test_read_credential_env_append_guarded_by_arn_not_kb(deploy_lines: list):
    """(Req 3.3) The read-credential runtime env append is guarded by the ARN's own presence
    check, independent of any Knowledge Base ID."""
    guard = _enclosing_guard(deploy_lines, f'SCM_ENV_ARGS+=(-env "{_ARN_ENV_NAME}=${_ARN_VAR}')
    assert _ARN_VAR in guard, f"read-credential env append must guard on ${_ARN_VAR}: {guard!r}"
    for kb in _KB_ID_VARS:
        assert kb not in guard, f"read-credential env append must not be gated on {kb}: {guard!r}"


def test_launch_env_appends_scm_args_outside_kb_conditional(deploy_lines: list):
    """(Req 3.3) ``LAUNCH_ENV_ARGS`` appends ``SCM_ENV_ARGS`` under its own count guard, not
    inside a Knowledge Base conditional — so GBAW_SCM_* args ship regardless of KB IDs."""
    guard = _enclosing_guard(deploy_lines, 'LAUNCH_ENV_ARGS+=("${SCM_ENV_ARGS[@]}")')
    assert "SCM_ENV_ARGS" in guard, f"SCM_ENV_ARGS append must guard on the SCM_ENV_ARGS count: {guard!r}"
    for kb in _KB_ID_VARS:
        assert kb not in guard, f"SCM_ENV_ARGS append must not be gated on {kb}: {guard!r}"


# --- Tests: ARN-only, no raw credential value ---------------------------------------------


def test_only_read_arn_variable_flows_never_a_literal_value(deploy_text: str):
    """(Req 3.3) The read credential is delivered to the runtime as an ``-env`` arg whose
    value is the shell variable ``$SCM_READ_CREDENTIAL_SECRET_ARN`` (an ARN reference),
    never an inlined literal credential value."""
    non_comment = _non_comment_text(deploy_text)
    env_arg_values = re.findall(rf'-env "{_ARN_ENV_NAME}=([^"]*)"', non_comment)
    assert env_arg_values, f"expected a -env {_ARN_ENV_NAME}=... assignment"
    for value in env_arg_values:
        assert value == f"${_ARN_VAR}", f"{_ARN_ENV_NAME} -env value must be exactly ${_ARN_VAR}, got: {value!r}"
