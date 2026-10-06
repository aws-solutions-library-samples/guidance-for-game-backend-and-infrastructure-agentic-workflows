"""Read-only validation of model-generated CloudFormation templates.

The GameLift specialist writes CloudFormation from documentation and memory, so
it can produce a template that parses but does not deploy (for example a
property name that does not exist on the resource). This module lets the
specialist check its own template before answering, using only read-only
CloudFormation APIs:

* ``ValidateTemplate`` checks template syntax and references.
* ``DescribeType`` returns the live registry schema for each ``AWS::`` resource
  type, which is used to check property names, required properties, read-only
  properties, enum values, and basic value shapes.

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
from utils.cfn_yaml import is_intrinsic, load_cfn_template

# ValidateTemplate accepts at most 51,200 bytes in TemplateBody.
MAX_TEMPLATE_BYTES = 51_200
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


def validate_template_text(template: str, client: Any) -> dict[str, Any]:
    """Validate a CloudFormation template with read-only APIs; return a bounded result."""
    if not isinstance(template, str) or not template.strip():
        return {"status": STATUS_INVALID, "templateErrors": ["empty_template"], "problems": []}
    body = template.strip()
    if body.startswith("```"):
        # Accept a fenced block as pasted from an answer.
        body = re.sub(r"^```[\w.+-]*[ \t]*\n|\n```[ \t]*$", "", body)
    if len(body.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        return {"status": STATUS_INVALID, "templateErrors": ["template_too_large"], "problems": []}

    try:
        parsed = load_cfn_template(body)
    except Exception:
        return {"status": STATUS_INVALID, "templateErrors": ["unparseable_yaml_or_json"], "problems": []}
    resources = parsed.get("Resources") if isinstance(parsed, dict) else None
    if not isinstance(resources, dict) or not resources:
        return {"status": STATUS_INVALID, "templateErrors": ["missing_resources_section"], "problems": []}

    result: dict[str, Any] = {"templateErrors": [], "problems": []}
    incomplete_codes: set[str] = set()

    try:
        client.validate_template(TemplateBody=body)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc.response, dict) else ""
        if code == "ValidationError":
            message = exc.response.get("Error", {}).get("Message", "")
            result["templateErrors"].append(_safe_text(message) or "validation_error")
        else:
            incomplete_codes.add(_error_code(exc))
    except Exception as exc:
        incomplete_codes.add(_error_code(exc))

    out = _Collector()
    checked: list[str] = []
    unchecked: list[str] = []
    by_type: dict[str, list[tuple[str, Any]]] = {}
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
            continue
        try:
            schema = _get_schema(client, type_name)
        except Exception as exc:
            code = _error_code(exc)
            if code == "not_found":
                for logical_id, _ in entries:
                    out.add(logical_id, "Type", PROBLEM_UNKNOWN_TYPE)
            else:
                incomplete_codes.add(code)
                unchecked.append(type_name)
            continue
        checked.append(type_name)
        for logical_id, properties in entries:
            _check_resource(schema, logical_id, properties, out)

    result["problems"] = out.problems
    result["checkedResourceTypes"] = checked
    if unchecked:
        result["uncheckedResourceTypes"] = unchecked
    if out.truncated:
        result["truncated"] = True
    if result["templateErrors"] or out.problems:
        result["status"] = STATUS_INVALID
    elif incomplete_codes or unchecked:
        result["status"] = STATUS_INCOMPLETE
    else:
        result["status"] = STATUS_VALID
    if incomplete_codes:
        result["error"] = {"codes": sorted(incomplete_codes)}
    return result
