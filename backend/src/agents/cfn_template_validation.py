"""Read-only validation of untrusted, model-generated CloudFormation drafts.

The GameLift specialist writes CloudFormation from documentation and memory, so
it can produce a template that parses but does not deploy (for example a
property name that does not exist on the resource). This module lets the
specialist check its draft before answering. Four checks run and are reported
separately:

* ``syntax``: bounded, safe parsing (``utils.cfn_yaml``) that rejects aliases,
  unsupported tags, duplicate keys, malformed intrinsics, and oversized input.
* ``templateValidation``: CloudFormation ``ValidateTemplate`` (syntax and
  references).
* ``registrySchema``: the live ``DescribeType`` schema for each ``AWS::`` type
  (property names, required and read-only properties, enum values, value
  shapes, and ``Fn::GetAtt`` attributes).
* ``productPolicy``: reviewed, code-owned rules (deprecated container-fleet
  shape, literal account IDs, unrestricted ingress, literal IP addresses,
  resource count).

A check that was denied, throttled, or unavailable makes the result
``incomplete``, which is never reported as valid. Even a ``valid`` draft stays
``untrusted`` and ``applied: False``: validation does not authorize, approve,
or deploy anything, and the result lists what was not validated.

Nothing here creates, updates, or deletes resources. Results are a bounded,
code-owned projection: statuses, typed problem codes, the template's own
logical IDs and property paths (charset-filtered and length-capped), and short
lists of allowed property or enum names taken from the AWS schema. The one
provider-authored string that crosses into model context is the
``ValidateTemplate`` error message, because it describes the caller's own
template; it is reduced to a short, charset-filtered line with ARNs, account
IDs, URLs, and IP addresses removed.
"""

# Standard library
import json
import re
import threading
from typing import Any

# Third-party packages
from botocore.exceptions import BotoCoreError, ClientError

# Local modules
from utils.cfn_yaml import MAX_TEMPLATE_BYTES, TemplateParseError, is_intrinsic, load_cfn_template

__all__ = ["MAX_TEMPLATE_BYTES", "validate_template_text"]

MAX_ERRORS = 25
MAX_RESOURCE_TYPES = 15
MAX_ALLOWED_NAMES = 40
MAX_TEXT_CHARS = 300
MAX_PATH_CHARS = 200

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_INCOMPLETE = "incomplete"

PROBLEM_UNKNOWN_PROPERTY = "unknown_property"
PROBLEM_MISSING_REQUIRED = "missing_required_property"
PROBLEM_READ_ONLY = "read_only_property"
PROBLEM_INVALID_VALUE = "invalid_enum_value"
PROBLEM_WRONG_SHAPE = "wrong_value_shape"
PROBLEM_UNKNOWN_TYPE = "unknown_resource_type"
PROBLEM_UNKNOWN_ATTRIBUTE = "unknown_getatt_attribute"
PROBLEM_OUT_OF_RANGE = "value_out_of_range"

_RESOURCE_TYPE_RE = re.compile(r"^AWS::[A-Za-z0-9]+::[A-Za-z0-9]+$")
_SAFE_TEXT_RE = re.compile(r"[^A-Za-z0-9 _.,:;()\[\]{}'\"/=<>+*#|-]")
_ARN_RE = re.compile(r"arn:[A-Za-z0-9-]*:[^\s'\"\]]*")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_ACCOUNT_RE = re.compile(r"\d{12}")
_IP_RE = re.compile(r"\d+(?:\.\d+){3}(?:/\d+)?")

# Process-lifetime cache of registry schemas; they change rarely and are not
# account-specific. Only successful lookups are cached.
_schema_cache: dict[str, dict[str, Any]] = {}
_schema_lock = threading.Lock()


def _safe_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value)
    for pattern, replacement in ((_ARN_RE, "<arn>"), (_URL_RE, "<url>"), (_ACCOUNT_RE, "<id>"), (_IP_RE, "<ip>")):
        text = pattern.sub(replacement, text)
    text = _SAFE_TEXT_RE.sub("", text.replace("\n", " "))
    return text[:limit]


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc.response, dict) else ""
        if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            return "access_denied"
        if code in {"Throttling", "ThrottlingException"}:
            return "throttled"
        if code == "TypeNotFoundException":
            return "not_found"
        return "provider_error"
    if isinstance(exc, BotoCoreError):
        return "provider_error"
    return "provider_error"


class _Collector:
    """Bounded problem list."""

    def __init__(self) -> None:
        self.problems: list[dict[str, Any]] = []
        self.truncated = False

    def add(self, resource: str, path: str, problem: str, allowed: list[str] | None = None) -> None:
        if len(self.problems) >= MAX_ERRORS:
            self.truncated = True
            return
        entry: dict[str, Any] = {
            "resource": _safe_text(resource, 255),
            "path": _safe_text(path, MAX_PATH_CHARS),
            "problem": problem,
        }
        if allowed:
            entry["allowed"] = [_safe_text(name, 100) for name in sorted(allowed)[:MAX_ALLOWED_NAMES]]
        self.problems.append(entry)


def _resolve(schema: dict[str, Any], node: Any, depth: int = 0) -> dict[str, Any]:
    """Follow local ``$ref`` pointers ("#/definitions/X")."""
    while isinstance(node, dict) and "$ref" in node and depth < 10:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return {}
        target: Any = schema
        for part in ref[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        node = target
        depth += 1
    return node if isinstance(node, dict) else {}


def _schema_types(node: dict[str, Any]) -> set[str]:
    declared = node.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {t for t in declared if isinstance(t, str)}
    if "properties" in node:
        return {"object"}
    if "items" in node:
        return {"array"}
    return set()


def _check_value(
    schema: dict[str, Any],
    node: dict[str, Any],
    value: Any,
    resource: str,
    path: str,
    out: _Collector,
    depth: int = 0,
) -> None:
    if depth > 12 or is_intrinsic(value):
        return
    node = _resolve(schema, node)
    if not node or any(key in node for key in ("oneOf", "anyOf", "allOf")):
        # Composite schemas are not checked here; ValidateTemplate and the
        # resource provider remain the authority for those shapes.
        return
    types = _schema_types(node)

    if isinstance(value, dict):
        if types and "object" not in types:
            out.add(resource, path, PROBLEM_WRONG_SHAPE)
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            closed = node.get("additionalProperties") is False and not node.get("patternProperties")
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key in properties:
                    _check_value(schema, properties[key], child, resource, child_path, out, depth + 1)
                elif closed:
                    out.add(resource, child_path, PROBLEM_UNKNOWN_PROPERTY, list(properties))
            required = node.get("required")
            if isinstance(required, list):
                for key in required:
                    if isinstance(key, str) and key not in value:
                        out.add(resource, f"{path}.{key}" if path else key, PROBLEM_MISSING_REQUIRED)
        return

    if isinstance(value, list):
        if types and "array" not in types:
            out.add(resource, path, PROBLEM_WRONG_SHAPE)
            return
        items = node.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(value[:100]):
                _check_value(schema, items, element, resource, f"{path}[{index}]", out, depth + 1)
        return

    if types and types <= {"object", "array"}:
        out.add(resource, path, PROBLEM_WRONG_SHAPE)
        return
    enum = node.get("enum")
    if isinstance(enum, list) and isinstance(value, str) and value not in enum:
        out.add(resource, path, PROBLEM_INVALID_VALUE, [str(v) for v in enum])
        return
    # Numeric and length bounds from the schema. Regex "pattern" constraints are
    # not evaluated: they are written for the registry's validator, and running
    # arbitrary patterns over untrusted values is not worth the risk here.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = node.get("minimum"), node.get("maximum")
        if (isinstance(low, (int, float)) and value < low) or (isinstance(high, (int, float)) and value > high):
            out.add(resource, path, PROBLEM_OUT_OF_RANGE, [f"minimum {low}", f"maximum {high}"])
    elif isinstance(value, str):
        low, high = node.get("minLength"), node.get("maxLength")
        if (isinstance(low, int) and len(value) < low) or (isinstance(high, int) and len(value) > high):
            out.add(resource, path, PROBLEM_OUT_OF_RANGE, [f"minLength {low}", f"maxLength {high}"])


def _check_resource(schema: dict[str, Any], logical_id: str, properties: Any, out: _Collector) -> None:
    if properties is None:
        properties = {}
    if is_intrinsic(properties):
        return
    if not isinstance(properties, dict):
        out.add(logical_id, "Properties", PROBLEM_WRONG_SHAPE)
        return
    read_only = {
        pointer.rsplit("/", 1)[-1]
        for pointer in schema.get("readOnlyProperties", [])
        if isinstance(pointer, str) and pointer.count("/") == 2 and pointer.startswith("/properties/")
    }
    for key in properties:
        if key in read_only:
            out.add(logical_id, str(key), PROBLEM_READ_ONLY)
    root: dict[str, Any] = {
        key: schema.get(key) for key in ("properties", "required", "additionalProperties") if key in schema
    }
    _check_value(schema, root, properties, logical_id, "", out)


def _collect_getatts(node: Any, path: str, found: list[tuple[str, str, str]], depth: int = 0) -> None:
    """Collect (location, target logical ID, attribute) for every literal Fn::GetAtt."""
    if depth > 40 or len(found) >= 500:
        return
    if isinstance(node, dict):
        getatt = node.get("Fn::GetAtt") if len(node) == 1 else None
        if isinstance(getatt, list) and len(getatt) == 2 and all(isinstance(part, str) for part in getatt):
            found.append((path, getatt[0], getatt[1]))
            return
        for key, child in node.items():
            _collect_getatts(child, f"{path}.{key}" if path else str(key), found, depth + 1)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            _collect_getatts(child, f"{path}[{index}]", found, depth + 1)


def _getatt_attributes(schema: dict[str, Any]) -> set[str] | None:
    """Attributes Fn::GetAtt can return: the schema's read-only properties, dotted."""
    pointers = schema.get("readOnlyProperties")
    if not isinstance(pointers, list) or not pointers:
        return None  # Schema does not declare attributes; do not guess.
    return {
        pointer[len("/properties/") :].replace("/", ".")
        for pointer in pointers
        if isinstance(pointer, str) and pointer.startswith("/properties/")
    }


def _get_schema(client: Any, type_name: str) -> dict[str, Any]:
    with _schema_lock:
        cached = _schema_cache.get(type_name)
    if cached is not None:
        return cached
    response = client.describe_type(Type="RESOURCE", TypeName=type_name)
    schema = json.loads(response["Schema"])
    if not isinstance(schema, dict):
        raise ValueError("schema is not an object")
    with _schema_lock:
        _schema_cache[type_name] = schema
    return schema


# ---------------------------------------------------------------------------
# Reviewed product policy
#
# Rules this product applies to every draft, independent of AWS validation.
# "error" findings make the draft invalid; "review" findings are surfaced so the
# user checks them, but are legitimate in some deployments.
# ---------------------------------------------------------------------------

MAX_RESOURCES = 40
_OPEN_CIDRS = {"0.0.0.0/0", "::/0"}
_LITERAL_ACCOUNT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_LITERAL_IP_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\d.])")
# Placeholders the Bedrock Guardrail writes when it anonymizes a value (for
# example {IP_ADDRESS}). A template containing one cannot deploy as written.
# CloudFormation Fn::Sub variables (${Name}) are not matched.
GUARDRAIL_PLACEHOLDER_RE = re.compile(
    r"(?<!\$)\{(?:IP_ADDRESS|EMAIL|PHONE|USERNAME|NAME|ADDRESS|URL|AWS Account ID|Internal Hostnames)\}"
)


class _PolicyCollector:
    def __init__(self) -> None:
        self.findings: list[dict[str, str]] = []
        self.truncated = False

    def add(self, rule: str, severity: str, resource: str, path: str) -> None:
        if len(self.findings) >= MAX_ERRORS:
            self.truncated = True
            return
        self.findings.append(
            {"rule": rule, "severity": severity, "resource": _safe_text(resource, 255), "path": _safe_text(path, 200)}
        )


def _walk_literals(node: Any, path: str, out: list[tuple[str, str]], depth: int = 0) -> None:
    """Collect (path, literal string) pairs, skipping intrinsic functions."""
    if depth > 40 or len(out) > 5_000 or is_intrinsic(node):
        return
    if isinstance(node, dict):
        for key, child in node.items():
            _walk_literals(child, f"{path}.{key}" if path else str(key), out, depth + 1)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            _walk_literals(child, f"{path}[{index}]", out, depth + 1)
    elif isinstance(node, str):
        out.append((path, node))


def _check_policy(resources: dict[str, Any], out: _PolicyCollector) -> None:
    if len(resources) > MAX_RESOURCES:
        out.add("too_many_resources", "error", "Resources", "Resources")
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        type_name = resource.get("Type")
        properties = resource.get("Properties") if isinstance(resource.get("Properties"), dict) else {}
        if type_name == "AWS::GameLift::Fleet" and "ContainerGroupsConfiguration" in properties:
            out.add(
                "deprecated_container_fleet_shape", "error", str(logical_id), "Properties.ContainerGroupsConfiguration"
            )
        literals: list[tuple[str, str]] = []
        _walk_literals(properties, "Properties", literals)
        for path, value in literals:
            if GUARDRAIL_PLACEHOLDER_RE.search(value):
                out.add("masked_placeholder", "error", str(logical_id), path)
            elif _LITERAL_ACCOUNT_RE.search(value):
                out.add("literal_account_id", "error", str(logical_id), path)
            elif value.strip() in _OPEN_CIDRS:
                out.add("unrestricted_ingress", "review", str(logical_id), path)
            elif _LITERAL_IP_RE.search(value):
                out.add("literal_ip_address", "review", str(logical_id), path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

CHECK_OK = "ok"
CHECK_INVALID = "invalid"
CHECK_DENIED = "denied"
CHECK_THROTTLED = "throttled"
CHECK_UNAVAILABLE = "unavailable"
CHECK_INCOMPLETE = "incomplete"
CHECK_SKIPPED = "skipped"

# What a passing result does NOT establish. Code-owned so the answer can state
# its limits truthfully, whatever the model writes around it.
NOT_VALIDATED = (
    "deployment in a specific account (quotas, existing names, service availability in the Region)",
    "IAM permissions of the person deploying",
    "game server behavior, capacity, or cost",
    "security review of the resources and their access",
)

_ERROR_CHECK = {"access_denied": CHECK_DENIED, "throttled": CHECK_THROTTLED}


def _result(status: str, checks: dict[str, str], **fields: Any) -> dict[str, Any]:
    return {
        "status": status,
        "untrusted": True,
        "applied": False,
        "checks": checks,
        "notValidated": list(NOT_VALIDATED),
        "templateErrors": fields.pop("templateErrors", []),
        "problems": fields.pop("problems", []),
        "policyFindings": fields.pop("policyFindings", []),
        **fields,
    }


def _unparsed(code: str) -> dict[str, Any]:
    skipped = {"templateValidation": CHECK_SKIPPED, "registrySchema": CHECK_SKIPPED, "productPolicy": CHECK_SKIPPED}
    return _result(STATUS_INVALID, {"syntax": CHECK_INVALID, **skipped}, templateErrors=[code])


def validate_template_text(template: str, client: Any) -> dict[str, Any]:
    """Validate an untrusted CloudFormation draft with read-only checks.

    Returns a bounded, code-owned result. ``status`` is ``valid`` only when
    every check ran and passed; any check that was denied, throttled, or
    unavailable makes the result ``incomplete``, never ``valid``. The draft is
    always reported as ``untrusted`` and ``applied: False``.
    """
    if not isinstance(template, str) or not template.strip():
        return _unparsed("empty_template")
    body = template.strip()
    if body.startswith("```"):
        # Accept a fenced block as pasted from an answer.
        body = re.sub(r"^```[\w.+-]*[ \t]*\n|\n```[ \t]*$", "", body)

    try:
        parsed = load_cfn_template(body)
    except TemplateParseError as exc:
        return _unparsed(exc.code)
    resources = parsed.get("Resources") if isinstance(parsed, dict) else None
    if not isinstance(resources, dict) or not resources:
        return _unparsed("missing_resources_section")

    checks = {"syntax": CHECK_OK}
    template_errors: list[str] = []

    # 1. CloudFormation's own syntax and reference validation.
    try:
        client.validate_template(TemplateBody=body)
        checks["templateValidation"] = CHECK_OK
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc.response, dict) else ""
        if code == "ValidationError":
            message = exc.response.get("Error", {}).get("Message", "")
            template_errors.append(_safe_text(message) or "validation_error")
            checks["templateValidation"] = CHECK_INVALID
        else:
            checks["templateValidation"] = _ERROR_CHECK.get(_error_code(exc), CHECK_UNAVAILABLE)
    except Exception as exc:
        checks["templateValidation"] = _ERROR_CHECK.get(_error_code(exc), CHECK_UNAVAILABLE)

    # 2. Registry schema checks for each AWS:: resource type.
    out = _Collector()
    checked: list[str] = []
    unchecked: list[str] = []
    schema_states: set[str] = set()
    by_type: dict[str, list[tuple[str, Any]]] = {}
    schemas: dict[str, dict[str, Any]] = {}
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            out.add(str(logical_id), "", PROBLEM_WRONG_SHAPE)
            continue
        type_name = resource.get("Type")
        if not isinstance(type_name, str) or not type_name.startswith("AWS::"):
            continue  # Custom resources and modules are out of scope.
        if not _RESOURCE_TYPE_RE.match(type_name):
            out.add(str(logical_id), "Type", PROBLEM_UNKNOWN_TYPE)
            continue
        by_type.setdefault(type_name, []).append((str(logical_id), resource.get("Properties")))

    for index, (type_name, entries) in enumerate(sorted(by_type.items())):
        if index >= MAX_RESOURCE_TYPES:
            unchecked.append(type_name)
            schema_states.add(CHECK_INCOMPLETE)
            continue
        try:
            schema = _get_schema(client, type_name)
        except Exception as exc:
            code = _error_code(exc)
            if code == "not_found":
                for logical_id, _ in entries:
                    out.add(logical_id, "Type", PROBLEM_UNKNOWN_TYPE)
            else:
                unchecked.append(type_name)
                schema_states.add(_ERROR_CHECK.get(code, CHECK_UNAVAILABLE))
            continue
        checked.append(type_name)
        schemas[type_name] = schema
        for logical_id, properties in entries:
            _check_resource(schema, logical_id, properties, out)

    # Fn::GetAtt attributes must exist on the target type; ValidateTemplate only
    # checks that the target resource exists.
    getatts: list[tuple[str, str, str]] = []
    for section in ("Resources", "Outputs"):
        _collect_getatts(parsed.get(section), section, getatts)
    for location, target, attribute in getatts:
        target_resource = resources.get(target)
        target_type = target_resource.get("Type") if isinstance(target_resource, dict) else None
        schema = schemas.get(target_type) if isinstance(target_type, str) else None
        attributes = _getatt_attributes(schema) if schema else None
        if attributes is not None and attribute not in attributes:
            owner = location.split(".", 2)[1] if location.count(".") >= 1 else location
            out.add(owner, f"{location} (GetAtt {target}.{attribute})", PROBLEM_UNKNOWN_ATTRIBUTE, list(attributes))

    if out.problems:
        checks["registrySchema"] = CHECK_INVALID
    elif schema_states:
        checks["registrySchema"] = sorted(schema_states)[0]
    else:
        checks["registrySchema"] = CHECK_OK

    # 3. Reviewed product policy.
    policy = _PolicyCollector()
    _check_policy(resources, policy)
    policy_errors = [f for f in policy.findings if f["severity"] == "error"]
    checks["productPolicy"] = CHECK_INVALID if policy_errors else CHECK_OK

    failed = any(state == CHECK_INVALID for state in checks.values())
    incomplete = any(state not in {CHECK_OK, CHECK_INVALID} for state in checks.values())
    status = STATUS_INVALID if failed else STATUS_INCOMPLETE if incomplete else STATUS_VALID

    extra: dict[str, Any] = {"checkedResourceTypes": checked}
    if unchecked:
        extra["uncheckedResourceTypes"] = unchecked
    if out.truncated or policy.truncated:
        extra["truncated"] = True
    return _result(
        status,
        checks,
        templateErrors=template_errors,
        problems=out.problems,
        policyFindings=policy.findings,
        **extra,
    )
