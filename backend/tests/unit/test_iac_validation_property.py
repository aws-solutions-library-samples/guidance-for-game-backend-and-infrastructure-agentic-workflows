#!/usr/bin/env python3
"""Property-based tests for in-memory IaC validation (`connector/iac_validation.py`).

Covers Correctness Property 14 from the source-control-connector design: IaC
validation precedes any provider write and rejects malformed content. The validator
is the gate the connector service runs *before* any branch/commit/PR provider
operation, so proving the gate accepts well-formed content and rejects malformed
content (always naming the offending file) is what makes "validation precedes writes"
enforceable.

Validates: Requirements 11.1, 11.2
"""

# Standard library
import json
import string

# Third-party packages
import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.iac_validation import (
    IaCValidationError,
    validate_cloudformation,
    validate_iac,
    validate_terraform,
)

pytestmark = pytest.mark.unit


# --- Hypothesis strategies -------------------------------------------------

# CloudFormation logical IDs are alphanumeric and start with a letter.
_logical_ids = st.from_regex(r"[A-Za-z][A-Za-z0-9]{0,15}", fullmatch=True)

# A truthy, well-formed resource type such as "AWS::S3::Bucket".
_resource_types = st.from_regex(r"AWS::[A-Za-z0-9]{1,12}::[A-Za-z0-9]{1,12}", fullmatch=True)

# File names ending in a CloudFormation-appropriate extension.
_cfn_paths = st.from_regex(r"[A-Za-z0-9_-]{1,20}\.(yaml|yml|json)", fullmatch=True)

# Terraform file names.
_tf_paths = st.from_regex(r"[A-Za-z0-9_-]{1,20}\.tf", fullmatch=True)


@st.composite
def _valid_cfn_template(draw) -> str:
    """A structurally valid CloudFormation template serialized as JSON or YAML.

    Every template has a non-empty top-level ``Resources`` map whose entries each
    declare a truthy ``Type`` — exactly what the validator requires.
    """
    resource = st.fixed_dictionaries(
        {"Type": _resource_types},
        optional={"Properties": st.dictionaries(_logical_ids, st.integers() | st.text(max_size=8), max_size=3)},
    )
    resources = draw(st.dictionaries(_logical_ids, resource, min_size=1, max_size=5))
    template: dict = {"Resources": resources}
    if draw(st.booleans()):
        template["AWSTemplateFormatVersion"] = "2010-09-09"
    if draw(st.booleans()):
        template["Description"] = draw(st.text(alphabet=string.ascii_letters + " ", max_size=20))

    fmt = draw(st.sampled_from(["json", "yaml"]))
    if fmt == "json":
        return json.dumps(template)
    return yaml.safe_dump(template)


@st.composite
def _malformed_cfn_template(draw) -> str:
    """Content that is guaranteed to be invalid CloudFormation.

    Each branch violates exactly one of the validator's structural rules, or is
    unparseable YAML.
    """
    category = draw(st.integers(min_value=0, max_value=6))
    if category == 0:  # empty / whitespace-only content
        return draw(st.sampled_from(["", "   ", "\n\t  \n"]))
    if category == 1:  # a mapping, but no Resources section
        return json.dumps({"Description": draw(st.text(alphabet=string.ascii_letters, max_size=10))})
    if category == 2:  # Resources present but empty
        return json.dumps({"Resources": {}})
    if category == 3:  # a resource missing its Type
        return json.dumps({"Resources": {"R1": {"Properties": {"k": "v"}}}})
    if category == 4:  # a resource that is not a mapping
        return json.dumps({"Resources": {"R1": draw(st.sampled_from(["not-a-map", "42", "true"]))}})
    if category == 5:  # top-level document is not a mapping
        return draw(st.sampled_from(["- a\n- b\n", "just a scalar\n", "12345\n"]))
    # category == 6: unparseable YAML
    return draw(st.sampled_from(["foo: [unclosed", "{unbalanced", "a: b: c", "- [\n"]))


@st.composite
def _valid_terraform(draw) -> str:
    """A small, reliably parseable Terraform HCL document (one or more blocks)."""
    names = draw(st.lists(st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True), min_size=1, max_size=3, unique=True))
    blocks = []
    for name in names:
        bucket = draw(st.from_regex(r"[a-z0-9-]{3,20}", fullmatch=True))
        blocks.append(f'resource "aws_s3_bucket" "{name}" {{\n  bucket = "{bucket}"\n}}\n')
    return "\n".join(blocks)


# --- Property 14 ------------------------------------------------------------


# Feature: source-control-connector, Property 14: IaC validation precedes writes and rejects malformed content
@settings(max_examples=100)
@given(path=_cfn_paths, content=_valid_cfn_template())
def test_property14_wellformed_cloudformation_passes(path, content):
    """Well-formed CloudFormation content passes validation without raising.

    Both the single-file entry point (`validate_cloudformation`) and the pipeline
    entry point the service calls before any write (`validate_iac`) must accept it.
    """
    validate_cloudformation(path, content)  # must not raise
    validate_iac([{"path": path, "content": content}], iac_format="cloudformation")  # must not raise


# Feature: source-control-connector, Property 14: IaC validation precedes writes and rejects malformed content
@settings(max_examples=100)
@given(path=_cfn_paths, content=_malformed_cfn_template())
def test_property14_malformed_cloudformation_rejected_naming_file(path, content):
    """Malformed CloudFormation content is rejected with an error naming the file.

    Rejection at the validation gate is what makes the connector decline before
    creating a branch or modifying the repository (Req 11.1, 11.2).
    """
    with pytest.raises(IaCValidationError) as exc_info:
        validate_cloudformation(path, content)
    assert exc_info.value.file == path
    assert exc_info.value.reason  # a human-readable reason is always present

    # The pipeline entry point rejects identically and names the same file.
    with pytest.raises(IaCValidationError) as pipeline_exc:
        validate_iac([{"path": path, "content": content}], iac_format="cloudformation")
    assert pipeline_exc.value.file == path


# Feature: source-control-connector, Property 14: IaC validation precedes writes and rejects malformed content
# deadline disabled: python-hcl2's first parse cost is variable and unrelated to correctness.
@settings(max_examples=100, deadline=None)
@given(path=_tf_paths, content=_valid_terraform())
def test_property14_wellformed_terraform_passes(path, content):
    """Parseable Terraform HCL passes validation without raising."""
    validate_terraform(path, content)  # must not raise
    validate_iac([{"path": path, "content": content}], iac_format="terraform")  # must not raise
