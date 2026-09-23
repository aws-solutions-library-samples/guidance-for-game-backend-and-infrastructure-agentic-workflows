"""Cross-artifact acceptance seams for the E5 bounded-autonomy plane (#440).

The pre-existing green suites test each artifact in isolation and therefore miss
the acceptance-blocking seams the #440 review confirmed:

* the 09 AppConfig rollback monitor formed an E3004 dependency cycle
  (``AutonomyEnvironment -> AutonomyEvaluatorErrorsAlarm -> EvaluatorFunction
  -> AutonomyEnvironment``) that makes the stack undeployable;
* the 07 executor omitted the SEPARATE E5 autonomy AppConfig coordinate the
  runtime resolver requires, and mis-pointed the general AppConfig coordinate
  at the autonomy application, so the executor built no autonomous writer;
* the evaluator role retained an unused ``cloudwatch:PutMetricData`` grant even
  though the plane emits no custom metric; and
* the disable wrapper preserved only a strict subset of the 09 parameters.

These tests take the ACTUAL shipped artifacts (the two templates and the
disable wrapper) and drive them through the REAL runtime resolver / a real
CloudFormation acyclic check, so template<->runtime drift and the confirmed
regressions fail closed here.

No test in this file calls AWS.
"""

from __future__ import annotations

# Standard library
import pathlib
import re
import shutil
import subprocess

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template
from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE_09 = PROJECT_ROOT / "infrastructure/cloudformation/09-operations-autonomy.yaml"
TEMPLATE_07 = PROJECT_ROOT / "infrastructure/cloudformation/07-operations-execution.yaml"
DISABLE_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/disable-operations-autonomy.sh"


@pytest.fixture(scope="module")
def template_09() -> dict:
    return load_cfn_template(TEMPLATE_09.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def template_07() -> dict:
    return load_cfn_template(TEMPLATE_07.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Finding 1: the 09 stack must be acyclic and cfn-lint clean at ERROR level.
# --------------------------------------------------------------------------- #


def _select_branch(value, operate: bool):
    """Resolve the flattened ``!If [cond, a, b]`` form to its operate/else branch.

    The scoped CFN loader flattens ``!If`` to ``[cond, when_true, when_false]``.
    Any other value passes through unchanged (plain scalars, ``!Ref`` strings).
    """
    if isinstance(value, list) and len(value) == 3 and isinstance(value[0], str):
        return value[1] if operate else value[2]
    return value


def _build_dependency_edges(template: dict) -> dict[str, set[str]]:
    """Build a resource->resources dependency graph from the flattened template.

    An edge A -> B exists when A's properties reference resource B by a flattened
    ``!Ref B`` (string == B) or ``!GetAtt B.Attr`` (string startswith ``B.``),
    or via an explicit ``DependsOn``. Deterministic literal names (``!Sub`` of a
    fixed string) do NOT create an edge, which is exactly how breaking the cycle
    with a literal function name removes the offending edge.
    """
    resources = template["Resources"]
    names = set(resources)
    edges: dict[str, set[str]] = {name: set() for name in names}

    def scan(node, owner: str) -> None:
        if isinstance(node, dict):
            for value in node.values():
                scan(value, owner)
        elif isinstance(node, list):
            for item in node:
                scan(item, owner)
        elif isinstance(node, str):
            if node in names and node != owner:
                edges[owner].add(node)
            else:
                head = node.split(".", 1)[0]
                if head in names and head != owner:
                    edges[owner].add(head)

    for name, body in resources.items():
        props = body.get("Properties", {})
        scan(props, name)
        depends = body.get("DependsOn")
        if isinstance(depends, str):
            edges[name].add(depends)
        elif isinstance(depends, list):
            edges[name].update(d for d in depends if isinstance(d, str))
    return edges


def _find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """Return one cycle (as a node list) if the graph has any, else ``None``."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in sorted(edges[node]):
            if color[nxt] == GRAY:
                return stack[stack.index(nxt) :] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in sorted(edges):
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def test_09_dependency_graph_is_acyclic(template_09: dict) -> None:
    """The monitor rollback alarm must not close a cycle back to the environment.

    This is the in-repo guard for the E3004 the review confirmed: the alarm's
    FunctionName dimension must use the deterministic function name, not a
    ``!Ref EvaluatorFunction`` that would take a resource dependency and close
    ``AutonomyEnvironment -> alarm -> EvaluatorFunction -> AutonomyEnvironment``.
    """
    edges = _build_dependency_edges(template_09)
    cycle = _find_cycle(edges)
    assert cycle is None, f"09 template has a CloudFormation dependency cycle: {' -> '.join(cycle)}"


def test_errors_alarm_does_not_depend_on_evaluator_function(template_09: dict) -> None:
    """Specifically: the errors alarm dimension is a literal name, not a Ref."""
    alarm = template_09["Resources"]["AutonomyEvaluatorErrorsAlarm"]["Properties"]
    dim = alarm["Dimensions"][0]
    assert dim["Name"] == "FunctionName"
    # Flattened !Sub keeps the literal string; a !Ref would flatten to
    # "EvaluatorFunction" (a resource name) and re-introduce the cycle.
    assert "operations-autonomy-evaluator" in dim["Value"]
    assert dim["Value"] != "EvaluatorFunction"


@pytest.mark.skipif(shutil.which("cfn-lint") is None, reason="cfn-lint not installed")
def test_09_passes_cfn_lint_at_error_level() -> None:
    """cfn-lint --non-zero-exit-code error must exit 0 on the 09 template.

    This is the tool of record for E3004; the unit suite alone is not evidence
    the stack deploys.
    """
    result = subprocess.run(
        ["cfn-lint", "--non-zero-exit-code", "error", str(TEMPLATE_09)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cfn-lint reported errors:\n{result.stdout}\n{result.stderr}"


# --------------------------------------------------------------------------- #
# Finding 2: the 07 executor env resolves through the REAL autonomy resolver,
# uses the SEPARATE autonomy AppConfig coordinate, and keeps the general
# coordinate on the E4 kill switch.
# --------------------------------------------------------------------------- #


def _executor_env(template_07: dict, *, operate: bool) -> dict[str, str]:
    """Resolve the 07 executor Lambda env for the operate/else branch.

    Non-autonomy CloudFormation refs (Refs the template threads from parameters)
    are filled with concrete, resolver-valid stand-ins so the real resolver can
    run; the autonomy-relevant keys come straight from the template's flattened
    ``!If`` structure so a wiring regression is caught.
    """
    fn = template_07["Resources"]["ExecutorFunction"]["Properties"]
    raw = fn["Environment"]["Variables"]
    fleet = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"
    sm = "arn:aws:states:us-west-2:000000000000:stateMachine:game-agent-operations-execution"
    concrete = {
        "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
        "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
        "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace.default",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": fleet,
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": f"arn:aws:gamelift:us-west-2:000000000000:fleet/{fleet}",
        "GBAW_OPERATIONS_ENROLLED_LOCATION": "us-west-2",
        "GBAW_OPERATIONS_STATE_MACHINE_ARN": sm,
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "client.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN": sm,
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE": "autonomy-profile",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE": "killswitch-profile",
        "GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT": "2772",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID": "policy.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION": "2026-09-01",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH": "sha256:" + "a" * 64,
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID": "state.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_SUBJECT": "automation.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_CLIENT": "client.gamelift-capacity-autonomy",
    }
    # Stand-ins for the AppConfig application/environment refs; the autonomy pair
    # must differ from the kill-switch pair for the coordinate to be separate.
    appconfig_stub = {
        "AutonomyApplicationId": "autonomy-app-abc",
        "AutonomyEnvironmentId": "autonomy-env-abc",
        "KillSwitchApplicationId": "killswitch-app-abc",
        "KillSwitchEnvironmentId": "killswitch-env-abc",
    }
    env: dict[str, str] = {}
    for key, value in raw.items():
        resolved = _select_branch(value, operate)
        if isinstance(resolved, str) and resolved in appconfig_stub:
            resolved = appconfig_stub[resolved]
        if isinstance(resolved, str) and resolved.startswith("arn:") is False and resolved in ("AWS::NoValue",):
            continue
        if isinstance(resolved, str) and not resolved.startswith("arn:") and resolved in concrete:
            resolved = concrete[resolved]
        env[key] = resolved
    # Overlay concrete resolver-valid values for the parameter-threaded keys.
    for key, value in concrete.items():
        if key in env:
            env[key] = value
    # Drop any AWS::NoValue placeholders (absent when not operating).
    return {k: v for k, v in env.items() if v != "AWS::NoValue"}


def test_07_operate_env_carries_separate_autonomy_appconfig_coordinate(template_07: dict) -> None:
    """When operating, 07 must inject both autonomy AppConfig keys AND keep the
    general AppConfig coordinate on the E4 kill switch."""
    fn = template_07["Resources"]["ExecutorFunction"]["Properties"]
    raw = fn["Environment"]["Variables"]
    assert "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_APPLICATION" in raw
    assert "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_ENVIRONMENT" in raw
    # The general coordinate must resolve to the KILL SWITCH ids in BOTH branches
    # (it is not an !If that repoints to the autonomy application).
    app = raw["GBAW_OPERATIONS_APPCONFIG_APPLICATION"]
    env = raw["GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT"]
    assert app == "KillSwitchApplicationId", f"general AppConfig app must stay on the kill switch, got {app!r}"
    assert env == "KillSwitchEnvironmentId", f"general AppConfig env must stay on the kill switch, got {env!r}"


def test_07_operate_env_resolves_through_real_resolver(template_07: dict) -> None:
    """The 07 operate env resolves cleanly and yields a SEPARATE autonomy
    coordinate the executor uses to build its autonomy switch client."""
    env = _executor_env(template_07, operate=True)
    settings = resolve_autonomy_evaluator_settings(env)
    # The autonomy switch coordinate must be the separate autonomy pair, not the
    # E4 kill-switch pair.
    assert settings.autonomy_appconfig_application != settings.appconfig_application
    assert settings.autonomy_switch_profile != settings.kill_switch_profile


def test_07_disabled_env_fails_closed(template_07: dict) -> None:
    """With AutonomyMode!=operate the executor resolver fails closed (v1 path)."""
    env = _executor_env(template_07, operate=False)
    with pytest.raises(ValueError):
        resolve_autonomy_evaluator_settings(env)


def test_executor_builds_autonomy_client_from_autonomy_coordinate() -> None:
    """The executor bootstrap constructs its autonomy AppConfig client from the
    SEPARATE autonomy application/environment, not the E4 kill-switch pair."""
    source = (PROJECT_ROOT / "backend/src/operations/execute/executor_entry.py").read_text(encoding="utf-8")
    # The AppConfigExtensionClient for the autonomy switch must read the autonomy
    # coordinate fields.
    block = source.split("autonomy_extension = AppConfigExtensionClient(", 1)[1].split(
        "port=autonomy_settings.appconfig_extension_port", 1
    )[0]
    assert "autonomy_settings.autonomy_appconfig_application" in block
    assert "autonomy_settings.autonomy_appconfig_environment" in block
    assert "autonomy_settings.autonomy_switch_profile" in block
    # It must NOT read the kill-switch (general) application/environment here.
    assert "autonomy_settings.appconfig_application" not in block
    assert "autonomy_settings.appconfig_environment" not in block


# --------------------------------------------------------------------------- #
# Finding 9: the evaluator role holds no unused cloudwatch:PutMetricData grant.
# --------------------------------------------------------------------------- #


def _iter_statements(role_body: dict):
    for policy in role_body["Properties"].get("Policies", []):
        for stmt in policy["PolicyDocument"]["Statement"]:
            yield stmt


def test_evaluator_role_has_no_putmetricdata(template_09: dict) -> None:
    """E5 emits no custom metric (AWS-native alarms only); the role must not
    grant cloudwatch:PutMetricData."""
    role = template_09["Resources"]["EvaluatorRole"]
    actions: set[str] = set()
    for stmt in _iter_statements(role):
        action = stmt.get("Action")
        if isinstance(action, str):
            actions.add(action)
        elif isinstance(action, list):
            actions.update(a for a in action if isinstance(a, str))
    assert "cloudwatch:PutMetricData" not in actions, "evaluator retains an unused custom-metric write grant"


# --------------------------------------------------------------------------- #
# Finding 5: the disable wrapper preserves EVERY declared non-lever 09 parameter.
# --------------------------------------------------------------------------- #


def test_disable_wrapper_derives_full_preserve_set_from_live_stack() -> None:
    """The disable wrapper must preserve EVERY declared non-lever 09 parameter.

    The wrapper derives its UsePreviousValue set from the LIVE stack's declared
    parameter keys (strictly stronger than a hardcoded list, which could drift
    as the template grows) and exempts ONLY the two levers. This test pins that
    mechanism: build the preserve set from the live keys, and set exactly
    Provisioned + AutonomyMode explicitly.
    """
    wrapper = DISABLE_WRAPPER.read_text(encoding="utf-8")
    # It must read the live parameter keys of each stack.
    assert "Stacks[0].Parameters[].ParameterKey" in wrapper
    # It must loop those keys into UsePreviousValue while exempting the levers.
    assert "UsePreviousValue=true" in wrapper
    assert "ParameterKey=AutonomyMode,ParameterValue=disabled" in wrapper
    assert "ParameterKey=Provisioned,ParameterValue=true" in wrapper
    # It must NOT hardcode a partial UsePreviousValue literal list (the old bug
    # that dropped five parameters). Fewer than a handful of literal
    # UsePreviousValue entries proves the preserve set is derived, not enumerated.
    literal_preserves = wrapper.count(",UsePreviousValue=true")
    assert literal_preserves <= 2, (
        "disable wrapper appears to hardcode a preserve list; it must derive the "
        f"full set from the live stack ({literal_preserves} literal entries found)"
    )


def test_disable_wrapper_closes_07_before_09() -> None:
    """Finding 4: the 07 executor pre-write hook is closed BEFORE the 09 plane."""
    wrapper = DISABLE_WRAPPER.read_text(encoding="utf-8")
    # Phase markers must appear in order: 07 executor first, 09 evaluator second.
    p1 = wrapper.index("Phase 1")
    p2 = wrapper.index("Phase 2")
    assert p1 < p2
    exec_idx = wrapper.index("EXECUTION_STACK_NAME", p1)
    nine_idx = wrapper.index("Phase 2")
    assert exec_idx < nine_idx
    # It must verify BOTH planes are observed disabled and only then claim success.
    assert "observed disabled" in wrapper
    success_idx = wrapper.index("Autonomy is disabled")
    # The success line must come AFTER both phase verifications.
    assert success_idx > wrapper.index("07 executor pre-write hook observed disabled")


def test_disable_wrapper_never_deletes_a_stack() -> None:
    """Disable is lever-flipping only; it must never delete either stack."""
    wrapper = DISABLE_WRAPPER.read_text(encoding="utf-8")
    assert "delete-stack" not in wrapper
