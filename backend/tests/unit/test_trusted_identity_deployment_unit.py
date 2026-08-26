"""Static deployment contract tests for trusted tenant/workspace bindings."""

# Standard library
import pathlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]


def test_frontend_template_keeps_identity_bindings_server_side():
    template = (PROJECT_ROOT / "infrastructure/cloudformation/02-frontend-ecs-express.yaml").read_text(encoding="utf-8")

    assert "TenantId:" in template
    assert "WorkspaceId:" in template
    assert "Name: GBAW_TENANT_ID" in template
    assert "Name: GBAW_WORKSPACE_ID" in template
    assert "Name: GBAW_COGNITO_AUDIENCE" in template
    assert "NEXT_PUBLIC_GBAW_TENANT_ID" not in template
    assert "NEXT_PUBLIC_GBAW_WORKSPACE_ID" not in template


def test_both_deploy_paths_pass_the_resolved_bindings():
    bash = (PROJECT_ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    powershell = (PROJECT_ROOT / "scripts/powershell/Public/Deploy-GameAgent.ps1").read_text(encoding="utf-8")

    assert "--identity-only" in bash
    assert 'TenantId="$GBAW_TENANT_ID"' in bash
    assert 'WorkspaceId="$GBAW_WORKSPACE_ID"' in bash
    assert "--identity-only" in powershell
    assert '"TenantId=$tenantId"' in powershell
    assert '"WorkspaceId=$workspaceId"' in powershell
