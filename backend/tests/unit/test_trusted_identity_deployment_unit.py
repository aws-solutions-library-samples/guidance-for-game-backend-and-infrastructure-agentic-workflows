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


def test_deploy_paths_configure_cognito_jwt_authorization_and_header_forwarding():
    bash = (PROJECT_ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    powershell = (PROJECT_ROOT / "scripts/powershell/Public/Deploy-GameAgent.ps1").read_text(encoding="utf-8")

    for deployment in (bash, powershell):
        assert "--authorizer-config" in deployment
        assert "customJWTAuthorizer" in deployment
        assert "allowedClients" in deployment
        assert "/.well-known/openid-configuration" in deployment
        assert "--request-header-allowlist" in deployment
        assert "Authorization" in deployment


def test_frontend_task_role_does_not_retain_sigv4_runtime_invocation_permissions():
    template = (PROJECT_ROOT / "infrastructure/cloudformation/01-base-infrastructure.yaml").read_text(encoding="utf-8")
    ecs_task_role = template.split("  ECSTaskRole:", 1)[1].split("  ECSTaskExecutionRole:", 1)[0]

    assert "bedrock-agentcore:InvokeAgentRuntime" not in ecs_task_role
    assert "bedrock-agentcore:InvokeAgentRuntimeForUser" not in ecs_task_role


def test_full_deployed_suite_requires_a_short_lived_access_token():
    full_test = (PROJECT_ROOT / "scripts/test/full.sh").read_text(encoding="utf-8")

    assert "GBAW_TEST_ACCESS_TOKEN is required for complete deployed JWT validation" in full_test
    assert "TEST_EMAIL and TEST_PASSWORD are required for authenticated deployed browser validation" in full_test
    assert "FAILED=1" in full_test
