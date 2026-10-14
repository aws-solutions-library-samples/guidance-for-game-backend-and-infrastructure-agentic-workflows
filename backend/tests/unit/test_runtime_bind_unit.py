#!/usr/bin/env python3
"""Unit tests for the runtime listener bind decision (#470).

These tests demonstrate the behavioral contract for how the AgentCore
entrypoints choose their listen address:

- Local execution always binds to loopback (``127.0.0.1``).
- Hosted AgentCore execution (``GBAW_HOSTED_RUNTIME=true``) binds all
  interfaces (``0.0.0.0``) so the AWS-managed container network can route to
  it; inside the unpublished container the publish boundary and the
  per-request JWT verifier — not the in-container bind — are the control.
- Hosted mode combined with the local identity bypass fails closed as a
  defense-in-depth / config-drift guard: ``invoke_agent`` already ignores the
  bypass while hosted (the JWT verifier runs regardless), so refusing the
  contradictory combination at startup keeps it from taking effect silently
  rather than being the sole barrier to exposure.
- Startup invocation guidance is truthful: the anonymous curl hint is emitted
  only for the loopback dev backend with the bypass active; hosted /
  authenticated modes give authenticated-invocation guidance and never
  advertise ``http://0.0.0.0``.
- Both Python entrypoints resolve their bind through the same shared helper,
  the thin wrapper delegates to ``run_server`` (verified by hermetic execution
  as ``__main__`` without AWS), ``dev-start`` forces local mode, and the
  deployment selectors (shell + PowerShell) request the hosted bind without
  ever setting the local identity bypass.
"""

# Standard library
import importlib
import re
import sys
from pathlib import Path
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = [pytest.mark.unit]

LOOPBACK = "127.0.0.1"
ALL_INTERFACES = "0.0.0.0"  # nosec B104 - test constant, not a bind

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _reload_settings():
    """Reload config.settings so module-level env reads are re-evaluated."""
    # Local modules
    import config.settings as settings

    return importlib.reload(settings)


class TestResolveRuntimeHost:
    """The pure host-resolution helper is the single source of truth."""

    def test_defaults_to_loopback_for_local_execution(self):
        settings = _reload_settings()
        host = settings.resolve_runtime_host(
            hosted_runtime=False,
            allow_local_identity_bypass=False,
        )
        assert host == LOOPBACK

    def test_local_with_identity_bypass_still_loopback(self):
        settings = _reload_settings()
        host = settings.resolve_runtime_host(
            hosted_runtime=False,
            allow_local_identity_bypass=True,
        )
        assert host == LOOPBACK

    def test_hosted_runtime_uses_all_interfaces(self):
        settings = _reload_settings()
        host = settings.resolve_runtime_host(
            hosted_runtime=True,
            allow_local_identity_bypass=False,
        )
        assert host == ALL_INTERFACES

    def test_hosted_runtime_with_bypass_fails_closed(self):
        # Hosted request handling already ignores the local bypass and verifies
        # JWTs. Refuse the contradictory selector pair as a defense-in-depth
        # config-drift guard rather than silently accepting it.
        settings = _reload_settings()
        with pytest.raises(settings.RuntimeBindError):
            settings.resolve_runtime_host(
                hosted_runtime=True,
                allow_local_identity_bypass=True,
            )

    def test_runtime_port_is_fixed_platform_port(self):
        # AgentCore serves on the fixed platform port; there is no config knob.
        settings = _reload_settings()
        assert settings.RUNTIME_PORT == 8080

    def test_no_host_or_port_override_env_surface(self):
        # The unrequested GBAW_RUNTIME_HOST / GBAW_RUNTIME_PORT surfaces must
        # not exist: the bind is decided by mode + bypass alone.
        source = (PROJECT_ROOT / "backend" / "src" / "config" / "settings.py").read_text(encoding="utf-8")
        assert "GBAW_RUNTIME_HOST" not in source
        assert "GBAW_RUNTIME_PORT" not in source


class TestEntrypointBindDecision:
    """Both entrypoints must resolve their bind through the shared helper."""

    def test_local_default_binds_loopback(self):
        # Local modules
        import agentcore_main as src_entry

        called = {}

        class _App:
            def run(self, host=None, port=None):
                called["host"] = host
                called["port"] = port

        with (
            patch.object(src_entry, "HOSTED_RUNTIME", False),
            patch.object(src_entry, "ALLOW_LOCAL_IDENTITY_BYPASS", True),
            patch.object(src_entry, "app", _App()),
        ):
            src_entry.run_server()

        assert called["host"] == LOOPBACK
        assert called["port"] == 8080

    def test_hosted_runtime_binds_all_interfaces(self):
        # Local modules
        import agentcore_main as src_entry

        called = {}

        class _App:
            def run(self, host=None, port=None):
                called["host"] = host

        with (
            patch.object(src_entry, "HOSTED_RUNTIME", True),
            patch.object(src_entry, "ALLOW_LOCAL_IDENTITY_BYPASS", False),
            patch.object(src_entry, "app", _App()),
        ):
            src_entry.run_server()

        assert called["host"] == ALL_INTERFACES

    def test_hosted_with_bypass_refuses_to_start(self):
        # Surface the defense-in-depth config-drift guard before app.run.
        # Local modules
        import agentcore_main as src_entry
        import config.settings as settings

        class _App:
            def run(self, host=None, port=None):  # pragma: no cover - must not run
                raise AssertionError("app.run must not be reached when the bind is refused")

        with (
            patch.object(src_entry, "HOSTED_RUNTIME", True),
            patch.object(src_entry, "ALLOW_LOCAL_IDENTITY_BYPASS", True),
            patch.object(src_entry, "app", _App()),
        ):
            with pytest.raises(settings.RuntimeBindError):
                src_entry.run_server()

    def test_wrapper_as_main_delegates_exactly_once_without_aws(self, monkeypatch):
        """Hermetic execution of the thin wrapper as ``__main__``.

        The wrapper (``backend/agentcore_main.py``) dynamically loads
        ``src/agentcore_main.py`` via ``importlib`` and, under ``__main__``,
        calls the loaded module's ``run_server``. Loading the real src module
        would import the full agent stack and touch AWS. Here we stub the
        dynamic load so a fake src module stands in, then prove that running
        the wrapper as ``__main__``:

          * performs exactly one ``run_server()`` delegation, and
          * never calls ``app.run`` itself (no hardcoded bind), and
          * needs no AWS: the stub module is what gets "loaded".

        This replaces the earlier source-string assertions with a behavioral
        test of the delegation itself.
        """
        # Standard library
        import importlib.util as _ilu
        import runpy
        import types

        wrapper_path = PROJECT_ROOT / "backend" / "agentcore_main.py"

        run_server_calls = []
        app_run_calls = []

        class _FakeApp:
            def run(self, *args, **kwargs):  # pragma: no cover - must not run
                app_run_calls.append((args, kwargs))
                raise AssertionError("wrapper must delegate to run_server, not call app.run itself")

        def _fake_run_server():
            run_server_calls.append(True)

        # Build the stand-in src module the wrapper expects to "load".
        fake_src = types.ModuleType("src_agentcore_main")
        fake_src.app = _FakeApp()
        fake_src.run_server = _fake_run_server

        real_spec_from_file = _ilu.spec_from_file_location
        real_module_from_spec = _ilu.module_from_spec

        def _fake_spec_from_file_location(name, location, *a, **k):
            # Only intercept the wrapper's load of src/agentcore_main.py; leave
            # any other importlib use intact.
            if str(location).endswith("agentcore_main.py") and "src" in str(location):
                loader = types.SimpleNamespace(exec_module=lambda module: None)
                return types.SimpleNamespace(loader=loader, name=name)
            return real_spec_from_file(name, location, *a, **k)

        def _fake_module_from_spec(spec):
            if getattr(spec, "name", None) == "src_agentcore_main":
                return fake_src
            return real_module_from_spec(spec)

        monkeypatch.setattr(_ilu, "spec_from_file_location", _fake_spec_from_file_location)
        monkeypatch.setattr(_ilu, "module_from_spec", _fake_module_from_spec)
        # The wrapper prepends backend/src to sys.path. Give it a disposable
        # list so that hermetic execution cannot leak that mutation to later tests.
        monkeypatch.setattr(sys, "path", list(sys.path))

        # Execute the wrapper file as though it were run as `python agentcore_main.py`.
        runpy.run_path(str(wrapper_path), run_name="__main__")

        assert run_server_calls == [True], "wrapper must delegate exactly one run_server() call"
        assert app_run_calls == [], "wrapper must not call app.run itself"

    def test_wrapper_source_has_no_hardcoded_all_interface_bind(self):
        # A cheap structural guard alongside the behavioral test above: the
        # wrapper must not reintroduce a literal 0.0.0.0 bind.
        source = (PROJECT_ROOT / "backend" / "agentcore_main.py").read_text(encoding="utf-8")
        assert "0.0.0.0" not in source, "wrapper must not hardcode an all-interface bind"

    def test_architecture_example_uses_the_shared_bind_resolver(self):
        architecture = (PROJECT_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        assert 'app.run(host="0.0.0.0"' not in architecture
        assert "run_server()" in architecture
        assert "Local development always binds to loopback" in architecture


class TestStartupMessageReflectsHost:
    """Startup output must report the actual listener address and give
    truthful, boundary-appropriate invocation guidance."""

    def _capture_messages(self, *, hosted, bypass):
        # Local modules
        import agentcore_main as src_entry

        class _App:
            def run(self, host=None, port=None):
                pass

        messages = []
        with (
            patch.object(src_entry, "HOSTED_RUNTIME", hosted),
            patch.object(src_entry, "ALLOW_LOCAL_IDENTITY_BYPASS", bypass),
            patch.object(src_entry, "app", _App()),
            patch.object(src_entry.logger, "info", side_effect=lambda m, *a, **k: messages.append(str(m))),
        ):
            src_entry.run_server()
        return "\n".join(messages)

    def test_run_server_logs_resolved_host(self):
        joined = self._capture_messages(hosted=False, bypass=False)
        assert LOOPBACK in joined

    def test_local_bypass_emits_anonymous_curl_hint(self):
        # Only the loopback dev backend with the identity bypass active may
        # advertise an anonymous curl — that is the sole case where an
        # unauthenticated invocation actually succeeds.
        joined = self._capture_messages(hosted=False, bypass=True)
        assert "curl" in joined
        assert "/invocations" in joined
        assert f"http://{LOOPBACK}:8080" in joined
        # Never advertise a wildcard bind as a reachable URL.
        assert f"http://{ALL_INTERFACES}" not in joined

    def test_hosted_mode_gives_authenticated_guidance_not_anonymous_curl(self):
        joined = self._capture_messages(hosted=True, bypass=False)
        assert "curl -X POST" not in joined
        assert "Cognito access token" in joined
        # The hosted bind host is a wildcard; it must never appear in a URL.
        assert f"http://{ALL_INTERFACES}" not in joined

    def test_local_without_bypass_gives_authenticated_guidance(self):
        # Loopback bind, but the JWT verifier runs (bypass off), so an
        # anonymous curl would be rejected: guidance must be authenticated.
        joined = self._capture_messages(hosted=False, bypass=False)
        assert "curl -X POST" not in joined
        assert "Cognito access token" in joined


class TestDevStartScript:
    """dev-start sets the local bypass; it must never publish beyond loopback
    and must force local runtime mode regardless of inherited env / .env.local."""

    def _source(self):
        return (PROJECT_ROOT / "scripts" / "dev" / "start.sh").read_text(encoding="utf-8")

    def test_dev_start_sets_bypass_but_not_wider_bind(self):
        source = self._source()
        # dev-start enables the local identity bypass ...
        assert "GBAW_ALLOW_LOCAL_IDENTITY_BYPASS=true" in source
        assert not re.search(r"GBAW_RUNTIME_HOST\s*=\s*0\.0\.0\.0", source)

    def test_dev_start_forces_hosted_runtime_false(self):
        # The discriminating check: dev-start must unconditionally export
        # GBAW_HOSTED_RUNTIME=false so an inherited GBAW_HOSTED_RUNTIME or a
        # value in ui/.env.local cannot flip the bypass-enabled dev backend
        # into the hosted all-interface bind. It must NOT set it to true.
        source = self._source()
        assert re.search(
            r"export\s+GBAW_HOSTED_RUNTIME=false", source
        ), "dev-start must unconditionally export GBAW_HOSTED_RUNTIME=false"
        # It must never actively export the hosted flag as true (prose mentions
        # of the string in comments are fine; an active `export ...=true` is not).
        assert not re.search(r"export\s+GBAW_HOSTED_RUNTIME=true", source)

    def test_dev_start_exports_hosted_false_before_launching_backend(self):
        # Order matters: the export must precede the python launch so the child
        # process inherits it (and load_dotenv, which does not override the
        # process environment, cannot then reintroduce hosted mode from
        # ui/.env.local).
        source = self._source()
        export_idx = source.find("export GBAW_HOSTED_RUNTIME=false")
        launch_idx = source.find("python src/agentcore_main.py")
        assert export_idx != -1, "expected GBAW_HOSTED_RUNTIME=false export"
        assert launch_idx != -1, "expected the backend launch command"
        assert export_idx < launch_idx, "GBAW_HOSTED_RUNTIME=false must be exported before launching the backend"


class TestDeploymentSelectorsDoNotBypassIdentity:
    """Hosted deployment selectors request 0.0.0.0 only with identity ON.

    The all-interface bind is safe in the hosted container precisely because
    the JWT verifier runs (the bypass is OFF). Both the shell deploy path and
    the PowerShell env-args builder must set GBAW_HOSTED_RUNTIME=true and must
    never set GBAW_ALLOW_LOCAL_IDENTITY_BYPASS, so hosted config can never
    reach the fail-closed combination.
    """

    def test_shell_deploy_sets_hosted_without_local_bypass(self):
        source = (PROJECT_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        assert "GBAW_HOSTED_RUNTIME=true" in source
        assert "GBAW_ALLOW_LOCAL_IDENTITY_BYPASS" not in source

    def test_powershell_deploy_sets_hosted_without_local_bypass(self):
        ps_path = PROJECT_ROOT / "scripts" / "powershell" / "Private" / "New-GameAgentAgentCoreEnvArgs.ps1"
        source = ps_path.read_text(encoding="utf-8")
        assert "GBAW_HOSTED_RUNTIME=true" in source
        assert "GBAW_ALLOW_LOCAL_IDENTITY_BYPASS" not in source
