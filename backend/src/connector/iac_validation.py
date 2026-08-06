"""In-memory validation of agent-proposed Infrastructure-as-Code (IaC) content.

This module validates proposed IaC file contents *before* any branch, commit, or
pull-request operation runs (Req 11.1). It operates purely on in-memory strings so
it is friendly to a read-only filesystem: nothing is written to disk.

Supported formats:

- **CloudFormation** (``"cloudformation"``): parsed with a CloudFormation-aware YAML
  loader that tolerates the intrinsic short tags (``!Ref``, ``!Sub``, ``!GetAtt``,
  ...). Because JSON is a subset of YAML, a single parse handles both YAML and JSON
  templates. Structural checks require a non-empty top-level ``Resources`` mapping in
  which every resource declares a ``Type``.
- **Terraform** (``"terraform"``): parsed with ``python-hcl2``. That dependency is
  imported lazily inside :func:`validate_terraform` so this module imports cleanly
  even when the package is not yet installed; a clear error is raised if it is
  unavailable at call time.

Any parse or structural failure raises :class:`IaCValidationError`, which always
names the offending file and the reason (Req 11.1, 11.2).
"""

# Standard library
from typing import Any, Iterable, Optional, Tuple

# Third-party packages
import yaml

CLOUDFORMATION = "cloudformation"
TERRAFORM = "terraform"

_SUPPORTED_FORMATS = frozenset({CLOUDFORMATION, TERRAFORM})


class IaCValidationError(Exception):
    """Raised when proposed IaC content fails to parse or is structurally invalid.

    Always carries the offending ``file`` name and a human-readable ``reason`` so the
    connector can return a validation-error message that identifies the failure
    (Req 11.2).
    """

    def __init__(self, file: str, reason: str) -> None:
        self.file = file
        self.reason = reason
        super().__init__(f"IaC validation failed for '{file}': {reason}")


class _CfnLoader(yaml.SafeLoader):
    """A ``SafeLoader`` subclass that understands CloudFormation short-form tags.

    A dedicated subclass keeps the intrinsic-tag constructors off the global
    ``SafeLoader`` so the rest of the codebase's YAML parsing is unaffected.
    """


def _cfn_multi_constructor(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> Any:
    """Represent any ``!Tag`` intrinsic as a plain mapping so parsing succeeds.

    The exact expansion does not matter for validation; we only need the document to
    parse into ordinary Python structures. Scalars, sequences, and mappings are all
    handled so tags such as ``!Ref``, ``!GetAtt [A, B]``, and ``!Sub`` all load.
    """
    key = f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {key: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {key: loader.construct_sequence(node)}
    if isinstance(node, yaml.MappingNode):
        return {key: loader.construct_mapping(node)}
    return {key: None}


# Register the intrinsic-tag handler for every tag beginning with "!" (e.g. "!Ref").
_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


def validate_cloudformation(path: str, content: str) -> None:
    """Validate a single CloudFormation YAML/JSON template.

    Raises :class:`IaCValidationError` naming ``path`` if the content does not parse
    or does not have a non-empty top-level ``Resources`` mapping whose entries each
    declare a ``Type`` (Req 11.1, 11.2).
    """
    if not isinstance(content, str) or not content.strip():
        raise IaCValidationError(path, "content is empty")

    try:
        parsed = yaml.load(content, Loader=_CfnLoader)  # noqa: S506 - custom safe subclass
    except yaml.YAMLError as exc:
        raise IaCValidationError(path, f"could not parse CloudFormation template: {exc}") from exc

    if not isinstance(parsed, dict):
        raise IaCValidationError(path, "template root is not a mapping")

    resources = parsed.get("Resources")
    if resources is None:
        raise IaCValidationError(path, "missing required top-level 'Resources' section")
    if not isinstance(resources, dict) or not resources:
        raise IaCValidationError(path, "top-level 'Resources' must be a non-empty mapping")

    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            raise IaCValidationError(path, f"resource '{logical_id}' is not a mapping")
        if not resource.get("Type"):
            raise IaCValidationError(path, f"resource '{logical_id}' is missing a 'Type'")


def validate_terraform(path: str, content: str) -> None:
    """Validate a single Terraform HCL document.

    ``python-hcl2`` is imported lazily so this module remains importable before the
    dependency is installed (it is added in task 6.1). Raises
    :class:`IaCValidationError` naming ``path`` if the parser is unavailable or the
    content fails to parse (Req 11.1, 11.2).
    """
    if not isinstance(content, str) or not content.strip():
        raise IaCValidationError(path, "content is empty")

    try:
        # Third-party packages
        import hcl2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise IaCValidationError(
            path,
            "Terraform validation is unavailable because 'python-hcl2' is not installed",
        ) from exc

    try:
        hcl2.loads(content)
    except Exception as exc:  # python-hcl2 raises a variety of parser errors
        raise IaCValidationError(path, f"could not parse Terraform HCL: {exc}") from exc


def _normalize_format(iac_format: Optional[str], path: str) -> str:
    if not isinstance(iac_format, str):
        raise IaCValidationError(path, "missing IaC format")
    normalized = iac_format.strip().lower()
    if normalized not in _SUPPORTED_FORMATS:
        raise IaCValidationError(path, f"unsupported IaC format: '{iac_format}'")
    return normalized


def _extract_path_content(entry: Any) -> Tuple[str, Any]:
    """Pull ``(path, content)`` from a ProposedFile-like object, dict, or pair."""
    path = getattr(entry, "path", None)
    content = getattr(entry, "content", None)
    if path is not None or content is not None:
        return str(path) if path is not None else "<unknown>", content
    if isinstance(entry, dict):
        return str(entry.get("path", "<unknown>")), entry.get("content")
    if isinstance(entry, (tuple, list)) and len(entry) >= 2:
        return str(entry[0]), entry[1]
    raise IaCValidationError("<unknown>", "unrecognized file entry shape")


def _resolve_entry_format(entry: Any, default: Optional[str]) -> Optional[str]:
    """Prefer a per-file ``iac_format`` (e.g. on a ProposedFile), else the default."""
    per_file = getattr(entry, "iac_format", None)
    if per_file is None and isinstance(entry, dict):
        per_file = entry.get("iac_format")
    return per_file or default


def validate_iac(files: Iterable[Any], iac_format: Optional[str] = None) -> None:
    """Validate every proposed file for its declared IaC format.

    ``files`` is any iterable of file entries; each entry may be a ProposedFile-like
    object (with ``path``/``content`` and optionally ``iac_format`` attributes), a
    ``{"path", "content", "iac_format"?}`` mapping, or a ``(path, content)`` pair.
    ``iac_format`` is the fallback format used for entries that do not carry their
    own.

    Raises :class:`IaCValidationError` on the first file that fails to parse or is
    structurally invalid (Req 11.1, 11.2). Returns ``None`` when all files are valid.
    """
    for entry in files:
        path, content = _extract_path_content(entry)
        resolved = _normalize_format(_resolve_entry_format(entry, iac_format), path)
        if resolved == CLOUDFORMATION:
            validate_cloudformation(path, content)
        else:
            validate_terraform(path, content)
