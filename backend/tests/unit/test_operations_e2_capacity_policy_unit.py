"""Parser- and scanner-verifiable contract tests for the OPTIONAL E2 operations
advise control plane's server-owned CAPACITY POLICY settings (GitHub issue #414).

These extend the E2 advise slice ADDITIVELY. The core (a later slice) resolves
per-fleet/location capacity bounds through a trusted enrollment/policy port, but
it requires the three frozen server-owned capacity-policy env names to be
present and validated by the deployment. This slice DEFINES and injects them:

* ``GBAW_OPERATIONS_CAPACITY_FLOOR``   — minimum desired instances a bounded
  proposal may request (server floor).
* ``GBAW_OPERATIONS_CAPACITY_CEILING`` — maximum desired instances a bounded
  proposal may request (server ceiling, a denial-of-wallet cap).
* ``GBAW_OPERATIONS_CAPACITY_MAX_STEP``— maximum single-step change in desired
  instances a bounded proposal may request.

All three are CloudFormation ``Number`` parameters, integer-typed and bounded,
with SAFE defaults ``0 / 1 / 1`` so a demo or fresh deployment is capped at ONE
instance and can never silently scale to a large fleet.

Coherence — ``floor <= ceiling`` and ``0 < max_step <= ceiling - floor`` — is a
NUMERIC bound. CloudFormation ``Rules`` have equality/set-membership functions
only (``Fn::Equals``/``Fn::Contains``/``Fn::And``/``Fn::Not``; there is no
numeric less-than), so the coherent bound is enforced in two layers that are
together at least as strict as the stated bound:

* the template ``Rules`` reject the equality-expressible incoherent cases
  (a degenerate ceiling, and a max_step that is not a positive member of the
  bounded band derived from the ``AllowedValues`` domain), and
* the deploy wrapper enforces the FULL numeric arithmetic bound on any explicit
  override before any AWS call (see the wrapper-behavior suite).

These tests never call AWS; they parse the repository-owned template as data.
"""

# Standard library
import pathlib
import re

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"

# The three frozen core env names this slice DEFINES and injects.
CAPACITY_ENV_KEYS = frozenset(
    {
        "GBAW_OPERATIONS_CAPACITY_FLOOR",
        "GBAW_OPERATIONS_CAPACITY_CEILING",
        "GBAW_OPERATIONS_CAPACITY_MAX_STEP",
    }
)

# The three parameters that back those env names, and their safe defaults.
CAPACITY_PARAMS = {
    "CapacityFloor": 0,
    "CapacityCeiling": 1,
    "CapacityMaxStep": 1,
}


@pytest.fixture(scope="module")
def template():
    return load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw():
    return TEMPLATE.read_text(encoding="utf-8")


def _observation_function(template):
    fns = [body for body in template["Resources"].values() if body["Type"] == "AWS::Lambda::Function"]
    assert len(fns) == 1, "exactly one operations Lambda function is expected"
    return fns[0]["Properties"]


# --------------------------------------------------------------------------- #
# Parameters: integer-typed, bounded, safe defaults 0/1/1
# --------------------------------------------------------------------------- #
def test_capacity_parameters_exist_and_are_numbers(template):
    params = template["Parameters"]
    for name in CAPACITY_PARAMS:
        assert name in params, f"missing capacity parameter {name}"
        assert params[name]["Type"] == "Number", f"{name} must be a Number (integer)"


def test_capacity_defaults_are_safe_zero_one_one(template):
    """Safe defaults cap a demo/fresh deploy at ONE instance: floor 0, ceiling 1,
    max_step 1. No large or 'million' default is permitted."""
    params = template["Parameters"]
    for name, expected in CAPACITY_PARAMS.items():
        assert int(params[name]["Default"]) == expected, f"{name} default must be {expected}"


def test_capacity_parameters_are_integer_bounded(template):
    """Each capacity parameter carries an explicit integer lower bound; every
    capacity parameter carries a modest upper bound so a fresh deploy cannot be
    edited to an unbounded ('million') fleet without an explicit stack update."""
    params = template["Parameters"]
    # Floor may be zero; ceiling and max_step must be at least one.
    assert int(params["CapacityFloor"]["MinValue"]) >= 0
    assert int(params["CapacityCeiling"]["MinValue"]) >= 1
    assert int(params["CapacityMaxStep"]["MinValue"]) >= 1
    # Every capacity parameter must be bounded above (denial-of-wallet control).
    for name in CAPACITY_PARAMS:
        assert "MaxValue" in params[name], f"{name} must carry a MaxValue upper bound"


def test_no_million_default_anywhere_in_capacity_params(template):
    """Regression guard: no capacity parameter default or upper bound is a large,
    unsafe value. A demo deploy must never default near a 'million' instances."""
    params = template["Parameters"]
    for name in CAPACITY_PARAMS:
        p = params[name]
        assert int(p["Default"]) <= 1, f"{name} default must be tiny (<=1) for a safe demo"
        # A sane upper bound: no capacity dimension may be edited to a runaway
        # fleet size within the template's own bounds.
        assert int(p["MaxValue"]) < 1_000_000, f"{name} MaxValue must be far below a million"


# --------------------------------------------------------------------------- #
# Env injection: all three frozen names bound to the validated parameters
# --------------------------------------------------------------------------- #
def test_capacity_env_bindings_present_on_the_handler(template):
    env = _observation_function(template)["Environment"]["Variables"]
    missing = CAPACITY_ENV_KEYS - set(env.keys())
    assert not missing, f"missing frozen capacity env bindings: {sorted(missing)}"


def test_capacity_env_bindings_reference_the_parameters(template):
    env = _observation_function(template)["Environment"]["Variables"]
    assert env["GBAW_OPERATIONS_CAPACITY_FLOOR"] == "CapacityFloor"
    assert env["GBAW_OPERATIONS_CAPACITY_CEILING"] == "CapacityCeiling"
    assert env["GBAW_OPERATIONS_CAPACITY_MAX_STEP"] == "CapacityMaxStep"


# --------------------------------------------------------------------------- #
# Rules: a coherent bound. The short-form intrinsic tags are erased by the
# scoped safe loader, so the Rule *presence* is asserted structurally and the
# coherent-bound intrinsics are asserted against the raw template text.
# --------------------------------------------------------------------------- #
def test_capacity_coherence_rule_exists(template):
    rules = template.get("Rules", {})
    assert rules, "expected a Rules block"
    rule_names = set(rules)
    assert any(
        "Capacity" in name for name in rule_names
    ), f"expected a capacity-coherence Rule; have {sorted(rule_names)}"


def _rules_text(raw):
    rules_start = raw.index("Rules:")
    resources_start = raw.index("\nResources:")
    return raw[rules_start:resources_start]


def test_capacity_rule_references_all_three_dimensions(raw):
    """The coherent-bound Rule must reference the three capacity parameters so a
    floor above the ceiling or a non-positive/oversized step is rejected at
    deploy time (equality-expressible cases) before any resource is created."""
    rules_text = _rules_text(raw)
    for token in ("CapacityFloor", "CapacityCeiling", "CapacityMaxStep"):
        assert token in rules_text, f"coherent capacity bound must reference {token}"
    assert "Assert" in rules_text, "capacity Rule must carry an Assertion"


def test_capacity_ceiling_and_step_are_positive_members(raw):
    """The ceiling and max_step must be constrained to a positive bounded domain
    (equality/set-membership) so a zero or empty value is rejected — the
    equality-expressible half of the coherent bound."""
    rules_text = _rules_text(raw)
    # A set-membership / equality guard is used (Fn::Contains or Fn::Not/Equals).
    assert re.search(
        r"Fn::Contains|!Contains|Fn::Not|!Not|Fn::And|!And", rules_text
    ), "the capacity coherence Rule must use an equality/set-membership guard"
