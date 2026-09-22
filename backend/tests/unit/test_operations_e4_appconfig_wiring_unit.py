"""Scanner-verifiable contract tests for the ADDITIVE E4 AppConfig wiring into
the accepted 06 (observe/advise) and 07 (execution) stacks (GitHub issue #416).

E4 lets the E1/E2 operations Lambda and the E3 dispatcher/executor read the
deployment-wide kill-switch document in-process via the official AppConfig
Lambda extension. The wiring MUST be strictly additive: a deploy that leaves the
AppConfig inputs empty behaves exactly as before (no layer attached, no new IAM),
and only when the kill-switch identifiers are supplied does the extension engage.

Asserted contract, read from the templates as data (no AWS calls):

* 06 and 07 each add optional AppConfig parameters (extension layer ARN + the
  application/environment/profile identifiers), all defaulting empty so the
  wiring is opt-in and additive.
* Each runtime Lambda attaches the extension layer only when it is supplied
  (a HasAppConfigLayer-style condition), and injects the
  GBAW_OPERATIONS_APPCONFIG_* identifiers.
* Each stack grants ONLY the two AppConfig data-plane read actions
  (StartConfigurationSession + GetLatestConfiguration), scoped to the exact
  kill-switch configuration resource, and nothing else in the appconfig:
  namespace. No appconfig authoring/admin action leaks into a consumer role.
"""

# Standard library
import pathlib

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
OBSERVE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"
EXECUTE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/07-operations-execution.yaml"

# The two AppConfig data-plane read actions a kill-switch CONSUMER may hold.
APPCONFIG_READ_ACTIONS = frozenset({"appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration"})

# AppConfig authoring / admin actions no consumer role may ever hold.
APPCONFIG_FORBIDDEN_SUBSTRINGS = (
    "appconfig:CreateHostedConfigurationVersion",
    "appconfig:StartDeployment",
    "appconfig:StopDeployment",
    "appconfig:CreateApplication",
    "appconfig:CreateEnvironment",
    "appconfig:CreateConfigurationProfile",
    "appconfig:CreateDeploymentStrategy",
    "appconfig:DeleteApplication",
    "appconfig:UpdateApplication",
)


def _load(path):
    return load_cfn_template(path.read_text(encoding="utf-8"))


def _resources_of_type(template, cfn_type):
    return {name: body for name, body in template["Resources"].items() if body.get("Type") == cfn_type}


def _iter_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _iter_strings(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value)


def _policy_statements(template):
    """Yield every statement from inline role Policies AND standalone
    AWS::IAM::Policy resources (the additive kill-switch read policy is the
    latter)."""
    for body in _resources_of_type(template, "AWS::IAM::Role").values():
        for inline in body.get("Properties", {}).get("Policies", []) or []:
            for stmt in inline.get("PolicyDocument", {}).get("Statement", []) or []:
                yield stmt
    for body in _resources_of_type(template, "AWS::IAM::Policy").values():
        doc = body.get("Properties", {}).get("PolicyDocument", {})
        for stmt in doc.get("Statement", []) or []:
            yield stmt


def _all_actions(template):
    actions = []
    for stmt in _policy_statements(template):
        for key in ("Action", "NotAction"):
            if key in stmt:
                actions.extend(_iter_strings(stmt[key]))
    return actions


def _appconfig_env_vars(template):
    """Every GBAW_OPERATIONS_APPCONFIG_* env var name set on any Lambda."""
    names = set()
    for body in _resources_of_type(template, "AWS::Lambda::Function").values():
        variables = body["Properties"].get("Environment", {}).get("Variables", {})
        names.update(k for k in variables if k.startswith("GBAW_OPERATIONS_APPCONFIG_"))
    return names


TEMPLATES = {
    "06": OBSERVE_TEMPLATE,
    "07": EXECUTE_TEMPLATE,
}


@pytest.mark.parametrize("label", sorted(TEMPLATES))
def test_appconfig_parameters_added_and_default_empty(label):
    template = _load(TEMPLATES[label])
    params = template["Parameters"]
    required = {
        "AppConfigExtensionLayerArn",
        "KillSwitchApplicationId",
        "KillSwitchEnvironmentId",
        "KillSwitchProfileId",
    }
    missing = required - set(params)
    assert not missing, f"{label} missing additive AppConfig parameters: {missing}"
    # All default empty -> additive, opt-in wiring.
    for name in required:
        assert (
            params[name].get("Default", "MISSING") == ""
        ), f"{label} parameter {name} must default empty so wiring is additive"


@pytest.mark.parametrize("label", sorted(TEMPLATES))
def test_extension_layer_attached_only_when_supplied(label):
    template = _load(TEMPLATES[label])
    # A condition gates the layer attachment on a non-empty layer ARN.
    conditions = template.get("Conditions", {})
    assert any(
        "AppConfig" in name or "KillSwitch" in name for name in conditions
    ), f"{label} has no AppConfig/kill-switch condition to gate additive wiring"
    # At least one Lambda references the extension layer via !If on that
    # condition (flattened by the loader to a list beginning with the condition
    # name), so a default (empty) deploy attaches no layer.
    found = False
    for body in _resources_of_type(template, "AWS::Lambda::Function").values():
        layers = body["Properties"].get("Layers")
        if not layers:
            continue
        for layer in layers:
            # !If [HasAppConfigLayer, <arn>, !Ref AWS::NoValue] -> ["HasAppConfigLayer", ...].
            if isinstance(layer, list) and layer and "AppConfig" in str(layer[0]):
                found = True
    assert found, f"{label} does not conditionally attach the AppConfig extension layer"


@pytest.mark.parametrize("label", sorted(TEMPLATES))
def test_appconfig_identifiers_injected(label):
    template = _load(TEMPLATES[label])
    injected = _appconfig_env_vars(template)
    # Backend contract names (no "_ID"/"_PROFILE_ID" suffix): the observe/execute
    # handlers resolve the kill-switch identifiers under these exact names.
    required = {
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE",
    }
    missing = required - injected
    assert not missing, f"{label} does not inject AppConfig identifiers: {missing}"


@pytest.mark.parametrize("label", sorted(TEMPLATES))
def test_only_appconfig_read_actions_granted(label):
    template = _load(TEMPLATES[label])
    appconfig_actions = {a for a in _all_actions(template) if a.startswith("appconfig:")}
    # Exactly the two data-plane read actions (or none, but E4 wiring adds them).
    assert (
        appconfig_actions == APPCONFIG_READ_ACTIONS
    ), f"{label} appconfig actions must be exactly the read pair, got {appconfig_actions}"
    for forbidden in APPCONFIG_FORBIDDEN_SUBSTRINGS:
        for action in _all_actions(template):
            assert forbidden not in action, f"{label} leaks authoring action {action}"


@pytest.mark.parametrize("label", sorted(TEMPLATES))
def test_appconfig_read_is_scoped_not_wildcard(label):
    template = _load(TEMPLATES[label])
    for stmt in _policy_statements(template):
        actions = list(_iter_strings(stmt.get("Action", [])))
        if any(a.startswith("appconfig:") for a in actions):
            resource = stmt.get("Resource")
            assert resource != "*", f"{label} AppConfig read statement uses wildcard resource"
            blob = str(resource)
            assert "application/" in blob and "configuration/" in blob, (
                f"{label} AppConfig read is not scoped to the kill-switch " f"configuration resource: {blob}"
            )
