"""Read-only comparison of authoritative add-on schemas and eager MCP schemas."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Iterable


CONSTRAINT_KEYS = (
    "const",
    "enum",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
)


def schema_digest(schemas: dict[str, dict[str, Any]]) -> str:
    payload = json.dumps(schemas, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def _deref(schema: Any, root: dict[str, Any]) -> Any:
    if not isinstance(schema, dict):
        return schema
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return schema
    current: Any = root
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            return schema
        current = current[token]
    return current


def _options(schema: Any, root: dict[str, Any]) -> list[dict[str, Any]]:
    resolved = _deref(schema, root)
    if not isinstance(resolved, dict):
        return []
    any_of = resolved.get("anyOf")
    if not isinstance(any_of, list):
        return [resolved]
    result = []
    for option in any_of:
        candidate = _deref(option, root)
        if isinstance(candidate, dict) and candidate.get("type") != "null":
            result.append(candidate)
    return result


def _types(schema: Any, root: dict[str, Any]) -> set[str]:
    result = set()
    for option in _options(schema, root):
        value = option.get("type")
        if isinstance(value, str):
            result.add(value)
        elif "const" in option:
            result.add(_json_type(option["const"]))
        elif isinstance(option.get("enum"), list):
            result.update(_json_type(item) for item in option["enum"])
    return result


def _schema_for_type(schema: Any, root: dict[str, Any], expected_type: str) -> dict[str, Any]:
    """Return the sole non-null branch for a JSON type when one is unambiguous."""
    resolved = _deref(schema, root)
    if not isinstance(resolved, dict):
        return {}
    matches = [option for option in _options(resolved, root) if expected_type in _types(option, root)]
    return matches[0] if len(matches) == 1 else resolved


def _open_schema(value: Any) -> Any:
    """Normalize equivalent JSON Schema spellings for an unconstrained value."""
    return None if value is None or value is True or value == {} else value


def _additional_properties(schema: dict[str, Any]) -> Any:
    value = schema.get("additionalProperties", True)
    return True if value == {} else value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _constraint(schema: Any, root: dict[str, Any], key: str) -> Any:
    values = []
    for option in _options(schema, root):
        if key in option:
            value = option[key]
            if key == "enum" and isinstance(value, list):
                value = sorted(value, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True))
            values.append(value)
    if not values:
        return None
    first = values[0]
    return first if all(value == first for value in values[1:]) else values


def _issue(
    issues: list[dict[str, Any]],
    *,
    severity: str,
    code: str,
    tool: str,
    path: str,
    message: str,
    addon: Any = None,
    mcp: Any = None,
) -> None:
    issues.append(
        {
            "severity": severity,
            "code": code,
            "tool": tool,
            "path": path,
            "message": message,
            "addon": addon,
            "mcp": mcp,
        }
    )


def _compare_array_items(
    tool: str,
    path: str,
    addon: dict[str, Any],
    mcp: dict[str, Any],
    addon_root: dict[str, Any],
    mcp_root: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    addon_items = _open_schema(addon.get("items"))
    mcp_items = _open_schema(mcp.get("items"))
    prefix_items = mcp.get("prefixItems")
    if addon_items is not None and mcp_items is not None:
        _compare_schema(tool, f"{path}[]", addon_items, mcp_items, addon_root, mcp_root, issues)
    elif addon_items is not None and isinstance(prefix_items, list):
        for index, item in enumerate(prefix_items):
            _compare_schema(tool, f"{path}[{index}]", addon_items, item, addon_root, mcp_root, issues)
    elif addon_items is not None:
        _issue(
            issues,
            severity="error",
            code="array_items_mismatch",
            tool=tool,
            path=f"{path}[]",
            message="Add-on array item schema is absent from MCP schema",
            addon=addon_items,
        )
    elif mcp_items is not None or isinstance(prefix_items, list):
        _issue(
            issues,
            severity="error",
            code="array_items_mismatch",
            tool=tool,
            path=f"{path}[]",
            message="MCP array item schema is absent from add-on schema",
            mcp=mcp_items if mcp_items is not None else prefix_items,
        )


def _compare_schema(
    tool: str,
    path: str,
    addon_schema: Any,
    mcp_schema: Any,
    addon_root: dict[str, Any],
    mcp_root: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    addon = _deref(addon_schema, addon_root)
    mcp = _deref(mcp_schema, mcp_root)
    if not isinstance(addon, dict) or not isinstance(mcp, dict):
        if addon != mcp:
            _issue(issues, severity="error", code="schema_shape_mismatch", tool=tool, path=path, message="Schema values differ", addon=addon, mcp=mcp)
        return
    addon_types = _types(addon, addon_root)
    mcp_types = _types(mcp, mcp_root)
    if addon_types and mcp_types and addon_types != mcp_types:
        _issue(issues, severity="error", code="type_mismatch", tool=tool, path=path, message="Accepted JSON types differ", addon=sorted(addon_types), mcp=sorted(mcp_types))
    for key in CONSTRAINT_KEYS:
        addon_value = _constraint(addon, addon_root, key)
        mcp_value = _constraint(mcp, mcp_root, key)
        if addon_value != mcp_value:
            _issue(issues, severity="error", code="constraint_mismatch", tool=tool, path=f"{path}.{key}", message=f"{key} differs", addon=addon_value, mcp=mcp_value)
    addon_default = _constraint(addon, addon_root, "default")
    mcp_default = _constraint(mcp, mcp_root, "default")
    if addon_default != mcp_default and (addon_default is not None or mcp_default is not None):
        _issue(issues, severity="warning", code="default_mismatch", tool=tool, path=f"{path}.default", message="Default value differs", addon=addon_default, mcp=mcp_default)
    if "object" in addon_types and "object" in mcp_types:
        addon_object = _schema_for_type(addon, addon_root, "object")
        mcp_object = _schema_for_type(mcp, mcp_root, "object")
        addon_properties = addon_object.get("properties")
        mcp_properties = mcp_object.get("properties")
        if isinstance(addon_properties, dict) and isinstance(mcp_properties, dict):
            addon_names = set(addon_properties)
            mcp_names = set(mcp_properties)
            for name in sorted(addon_names - mcp_names):
                _issue(issues, severity="error", code="missing_mcp_field", tool=tool, path=f"{path}.{name}", message="Add-on field is absent from MCP schema")
            for name in sorted(mcp_names - addon_names):
                _issue(issues, severity="error", code="extra_mcp_field", tool=tool, path=f"{path}.{name}", message="MCP field is absent from add-on schema")
            for name in sorted(addon_names & mcp_names):
                _compare_schema(tool, f"{path}.{name}", addon_properties[name], mcp_properties[name], addon_root, mcp_root, issues)
        addon_required = set(addon_object.get("required", []))
        mcp_required = set(mcp_object.get("required", []))
        if addon_required != mcp_required:
            _issue(issues, severity="error", code="required_mismatch", tool=tool, path=f"{path}.required", message="Required fields differ", addon=sorted(addon_required), mcp=sorted(mcp_required))
        addon_extra = _additional_properties(addon_object)
        mcp_extra = _additional_properties(mcp_object)
        if isinstance(addon_extra, dict) and isinstance(mcp_extra, dict):
            _compare_schema(tool, f"{path}.additionalProperties", addon_extra, mcp_extra,
                            addon_root, mcp_root, issues)
        elif addon_extra != mcp_extra:
            _issue(issues, severity="error", code="additional_properties_mismatch", tool=tool, path=f"{path}.additionalProperties", message="Unknown-field policy differs", addon=addon_extra, mcp=mcp_extra)
    if "array" in addon_types and "array" in mcp_types:
        addon_array = _schema_for_type(addon, addon_root, "array")
        mcp_array = _schema_for_type(mcp, mcp_root, "array")
        _compare_array_items(tool, path, addon_array, mcp_array, addon_root, mcp_root, issues)


def audit_schemas(
    addon_schemas: dict[str, dict[str, Any]],
    mcp_schemas: dict[str, dict[str, Any]],
    *,
    mcp_only: Iterable[str] = (),
    ignored_mcp_fields: Iterable[str] = ("instance",),
    max_issues: int = 2000,
) -> dict[str, Any]:
    expected_mcp_only = set(mcp_only)
    ignored_fields = set(ignored_mcp_fields)
    issues: list[dict[str, Any]] = []
    addon_names = set(addon_schemas)
    mcp_names = set(mcp_schemas)
    for name in sorted(addon_names - mcp_names):
        _issue(issues, severity="error", code="missing_mcp_tool", tool=name, path="$", message="Add-on tool has no eager MCP tool")
    for name in sorted(mcp_names - addon_names - expected_mcp_only):
        _issue(issues, severity="error", code="unexpected_mcp_tool", tool=name, path="$", message="MCP tool has no add-on capability")
    for name in sorted(addon_names & mcp_names):
        addon = addon_schemas[name]
        local = json.loads(json.dumps(mcp_schemas[name]))
        properties = local.get("properties")
        if isinstance(properties, dict):
            for field in ignored_fields:
                properties.pop(field, None)
        required = local.get("required")
        if isinstance(required, list):
            local["required"] = [field for field in required if field not in ignored_fields]
        _compare_schema(name, "$", addon, local, addon, local, issues)
    total = len(issues)
    errors = sum(issue["severity"] == "error" for issue in issues)
    warnings = sum(issue["severity"] == "warning" for issue in issues)
    return {
        "aligned": errors == 0,
        "addon_tool_count": len(addon_schemas),
        "mcp_tool_count": len(mcp_schemas),
        "shared_tool_count": len(addon_names & mcp_names),
        "error_count": errors,
        "warning_count": warnings,
        "issue_count": total,
        "issues_truncated": total > max_issues,
        "issues": issues[:max_issues],
        "addon_schema_digest": schema_digest(addon_schemas),
        "mcp_schema_digest": schema_digest(mcp_schemas),
    }
