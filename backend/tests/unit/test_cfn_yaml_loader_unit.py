"""Security regressions for the scanner-verifiable CloudFormation YAML loader (#423).

These tests pin the defense-in-depth properties of ``tests/_cfn_yaml.py``:

* CloudFormation short-form intrinsics parse into plain data structures;
* Python object/module/callable tags fail closed; and
* the custom constructors stay isolated to the dedicated loader subclass and
  never mutate the global ``yaml.SafeLoader`` registry.
"""

# Standard library
import copy

# Third-party packages
import pytest
import yaml

# Local modules
from _cfn_yaml import CloudFormationSafeLoader, load_cfn_template

pytestmark = pytest.mark.unit


# --- Intrinsic parsing -------------------------------------------------------


def test_ref_scalar_intrinsic_parses_to_plain_string():
    template = load_cfn_template("Value: !Ref MyResource\n")
    assert template == {"Value": "MyResource"}
    assert isinstance(template["Value"], str)


def test_sub_scalar_intrinsic_parses_to_plain_string():
    template = load_cfn_template("Name: !Sub '${ProjectName}-web'\n")
    assert template == {"Name": "${ProjectName}-web"}


def test_getatt_scalar_intrinsic_parses_to_plain_string():
    template = load_cfn_template("Arn: !GetAtt VectorBucket.VectorBucketArn\n")
    assert template == {"Arn": "VectorBucket.VectorBucketArn"}


def test_sequence_intrinsic_parses_to_plain_list():
    template = load_cfn_template("Joined: !Join ['-', ['a', 'b']]\n")
    assert template == {"Joined": ["-", ["a", "b"]]}
    assert isinstance(template["Joined"], list)


def test_condition_mapping_intrinsic_parses_to_plain_dict():
    template = load_cfn_template("Cond: !If {Key: Value}\n")
    assert template == {"Cond": {"Key": "Value"}}
    assert isinstance(template["Cond"], dict)


def test_result_contains_only_plain_builtin_types():
    template = load_cfn_template(
        "Resources:\n" "  Table:\n" "    Name: !Ref ProjectName\n" "    Tags: !Join ['-', ['x']]\n"
    )
    assert type(template) is dict
    resource = template["Resources"]["Table"]
    assert type(resource["Name"]) is str
    assert type(resource["Tags"]) is list


# --- Python object/module/callable tags must fail closed ---------------------


@pytest.mark.parametrize(
    "payload",
    [
        "!!python/object/apply:os.system ['echo pwned']",
        "!!python/object:subprocess.Popen []",
        "!!python/module:os",
        "!!python/name:os.system",
        "!!python/object/new:collections.OrderedDict []",
    ],
)
def test_python_object_module_and_callable_tags_fail_closed(payload):
    with pytest.raises(yaml.YAMLError):
        load_cfn_template(payload + "\n")


def test_python_apply_does_not_execute_side_effects(tmp_path):
    marker = tmp_path / "marker"
    payload = f"!!python/object/apply:pathlib.Path.write_text [!!python/object/apply:pathlib.Path ['{marker}'], 'x']\n"
    with pytest.raises(yaml.YAMLError):
        load_cfn_template(payload)
    assert not marker.exists()


# --- Registry isolation ------------------------------------------------------


def test_intrinsic_constructor_is_isolated_to_subclass():
    # The bang multi-constructor lives on the subclass, not on the base loader.
    assert "!" in CloudFormationSafeLoader.yaml_multi_constructors
    assert "!" not in yaml.SafeLoader.yaml_multi_constructors


def test_base_safeloader_still_rejects_cfn_intrinsics():
    # Proves the base loader was never taught the CloudFormation tags: a bare
    # SafeLoader must still refuse a !Ref tag.
    with pytest.raises(yaml.YAMLError):
        yaml.load("Value: !Ref MyResource\n", Loader=yaml.SafeLoader)


def test_global_multi_constructor_registry_unchanged_after_load():
    before = copy.copy(yaml.SafeLoader.yaml_multi_constructors)
    load_cfn_template("Value: !Ref MyResource\n")
    assert yaml.SafeLoader.yaml_multi_constructors == before
    assert "!" not in yaml.SafeLoader.yaml_multi_constructors


# --- Disposal contract -------------------------------------------------------


def test_loader_is_disposed_even_on_parse_error():
    disposed = {"count": 0}
    original_dispose = CloudFormationSafeLoader.dispose

    def counting_dispose(self):
        disposed["count"] += 1
        return original_dispose(self)

    CloudFormationSafeLoader.dispose = counting_dispose
    try:
        with pytest.raises(yaml.YAMLError):
            load_cfn_template("!!python/module:os\n")
    finally:
        CloudFormationSafeLoader.dispose = original_dispose
    assert disposed["count"] == 1
