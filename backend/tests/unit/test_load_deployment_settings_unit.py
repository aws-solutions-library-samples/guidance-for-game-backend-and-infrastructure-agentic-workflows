"""Unit tests for deployment dotenv loading."""

# Standard library
import importlib.util
import os
import pathlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
MODULE_PATH = PROJECT_ROOT / "config" / "load_deployment_settings.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("load_deployment_settings", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_env_file_supports_export_and_inline_comments(tmp_path, monkeypatch):
    module = _load_module()
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "export GBAW_TEST_EXPORTED='exported-value'\n" "GBAW_TEST_COMMENTED=commented-value # ignored comment\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GBAW_TEST_EXPORTED", raising=False)
    monkeypatch.delenv("GBAW_TEST_COMMENTED", raising=False)

    module.load_env_file(env_file)

    assert os.environ["GBAW_TEST_EXPORTED"] == "exported-value"
    assert os.environ["GBAW_TEST_COMMENTED"] == "commented-value"


def test_load_env_file_does_not_override_process_environment(tmp_path, monkeypatch):
    module = _load_module()
    env_file = tmp_path / ".env.local"
    env_file.write_text("GBAW_TEST_PRECEDENCE=file-value\n", encoding="utf-8")
    monkeypatch.setenv("GBAW_TEST_PRECEDENCE", "process-value")

    module.load_env_file(env_file)

    assert os.environ["GBAW_TEST_PRECEDENCE"] == "process-value"


def test_resolve_identity_settings_uses_single_deployment_defaults(monkeypatch):
    module = _load_module()
    monkeypatch.delenv("GBAW_TENANT_ID", raising=False)
    monkeypatch.delenv("GBAW_WORKSPACE_ID", raising=False)

    assert module.resolve_identity_settings() == {
        "GBAW_TENANT_ID": "default-tenant",
        "GBAW_WORKSPACE_ID": "default-workspace",
    }


def test_resolve_identity_settings_prefers_nonempty_environment(monkeypatch):
    module = _load_module()
    monkeypatch.setenv("GBAW_TENANT_ID", "tenant-a")
    monkeypatch.setenv("GBAW_WORKSPACE_ID", "workspace-a")

    assert module.resolve_identity_settings() == {
        "GBAW_TENANT_ID": "tenant-a",
        "GBAW_WORKSPACE_ID": "workspace-a",
    }
