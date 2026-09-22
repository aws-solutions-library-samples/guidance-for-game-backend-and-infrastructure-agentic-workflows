"""Scanner-verifiable contract tests for the E4 operational disable scripts
(GitHub issue #416).

Two operational disable paths sit above the reversible API-lever disable
(disable-operations-control.sh):

* ``disable-all-operations.sh`` — deploys a fresh, all-disabled kill-switch
  document via the AppConfig IMMEDIATE (hard-down) strategy, so the deployment-
  wide master switch (operations_enabled=false) engages at 100% with no bake.
* ``disable-operations-capability.sh`` — disables exactly one capability's
  phases (default: the whole capability) while leaving operations_enabled and
  other phases as-is, also via the immediate strategy.

Both are default-zero in effect (the document they write disables, never
enables), reversible (a later enabling document can be issued), verify an
explicit profile/account/region before any write, and require an explicit
confirmation. These tests read the scripts as data and never call AWS.
"""

# Standard library
import pathlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
DISABLE_ALL = PROJECT_ROOT / "scripts/infrastructure/disable-all-operations.sh"
DISABLE_CAPABILITY = PROJECT_ROOT / "scripts/infrastructure/disable-operations-capability.sh"


def _read(path):
    assert path.exists(), f"missing script: {path}"
    return path.read_text(encoding="utf-8")


def test_disable_all_uses_immediate_strategy_and_disables_master_switch():
    body = _read(DISABLE_ALL)
    assert "get-caller-identity" in body
    assert "--confirm" in body
    # It writes an all-disabled document (operations_enabled false) and deploys
    # via the immediate hard-down strategy.
    assert '"operations_enabled": false' in body
    assert "operations-immediate" in body or "ImmediateDeploymentStrategy" in body
    assert "create-hosted-configuration-version" in body
    assert "start-deployment" in body
    # It never issues an enabling document.
    assert '"operations_enabled": true' not in body


def test_disable_all_is_reversible_and_documented():
    body = _read(DISABLE_ALL)
    # Reversible: the doc notes re-enable via the control plane, and it does not
    # delete any resource.
    assert "delete-stack" not in body
    assert "reversible" in body.lower()


def test_disable_capability_targets_one_capability_immediately():
    body = _read(DISABLE_CAPABILITY)
    assert "get-caller-identity" in body
    assert "--confirm" in body
    assert "gamelift.capacity-adjustment" in body
    assert "operations-immediate" in body or "ImmediateDeploymentStrategy" in body
    assert "create-hosted-configuration-version" in body
    assert "start-deployment" in body
    # A capability disable sets phase booleans to false; it never enables.
    assert '"execute": false' in body
    assert "delete-stack" not in body


def test_both_scripts_fail_closed_without_confirmation():
    for path in (DISABLE_ALL, DISABLE_CAPABILITY):
        body = _read(path)
        # A refusal path exists when --confirm is absent.
        assert "Refusing" in body
