"""Versioned data-only workflow templates that expand into validated typed steps."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
import re
from typing import Any

from .workflow import validate_steps


MAX_TEMPLATE_PARAMETER_BYTES = 65_536
_OMIT = object()


class WorkflowTemplateError(ValueError):
    pass


_VIEW_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "enum": ["camera", "top", "side", "three_quarter"]},
    "minItems": 1,
    "maxItems": 4,
    "uniqueItems": True,
}
_OUTPUT_DIR_SCHEMA = {
    "type": ["string", "null"],
    "minLength": 1,
    "maxLength": 1024,
    "default": None,
}
_OUTPUT_PREFIX_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
}
_NAME_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 255}
_STATE_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 255}
_FRAME_SCHEMA = {"type": ["number", "null"], "minimum": -1048574, "maximum": 1048574, "default": None}
_CONFIRM_SCHEMA = {"type": ["boolean", "null"], "enum": [True, None], "default": None}
_INDEX_LIST_SCHEMA = {
    "type": "array",
    "items": {"type": "integer", "minimum": 0, "maximum": 5_000_000},
    "minItems": 1,
    "maxItems": 100_000,
    "uniqueItems": True,
}
_VECTOR3_SCHEMA = {
    "type": "array",
    "items": {"type": "number", "minimum": -1_000_000, "maximum": 1_000_000},
    "minItems": 3,
    "maxItems": 3,
}


_TEMPLATES: dict[str, dict[str, Any]] = {
    "scene_review_v1": {
        "version": 1,
        "description": "Inspect the active scene and render a bounded canonical review set.",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "output_dir": _OUTPUT_DIR_SCHEMA,
                "output_prefix": {**_OUTPUT_PREFIX_SCHEMA, "default": "scene_review"},
                "views": {**_VIEW_SCHEMA, "default": ["camera", "top", "side", "three_quarter"]},
                "width": {"type": "integer", "minimum": 1, "maximum": 4096, "default": 960},
                "height": {"type": "integer", "minimum": 1, "maximum": 4096, "default": 540},
                "overwrite": {"type": "boolean", "default": False},
                "allow_large": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "steps": [
            {"id": "inspect", "tool": "scene.inspect"},
            {
                "id": "review",
                "tool": "render.review_set",
                "arguments": {
                    "output_dir": {"$param": "output_dir", "omit_if_none": True},
                    "output_prefix": {"$param": "output_prefix"},
                    "views": {"$param": "views"},
                    "width": {"$param": "width"},
                    "height": {"$param": "height"},
                    "overwrite": {"$param": "overwrite"},
                    "allow_large": {"$param": "allow_large"},
                },
            },
        ],
    },
    "object_move_review_v1": {
        "version": 1,
        "description": "Checkpoint, move one object in local space with a state precondition and exact prior-local-position compensation, then inspect and render review views.",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "object_name": _NAME_SCHEMA,
                "location": {
                    "type": "array",
                    "items": {"type": "number", "minimum": -1000000, "maximum": 1000000},
                    "minItems": 3,
                    "maxItems": 3,
                },
                "checkpoint_tag": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                    "default": "object-move",
                },
                "output_dir": _OUTPUT_DIR_SCHEMA,
                "output_prefix": {**_OUTPUT_PREFIX_SCHEMA, "default": "object_move_review"},
                "views": {**_VIEW_SCHEMA, "default": ["camera", "three_quarter"]},
                "width": {"type": "integer", "minimum": 1, "maximum": 4096, "default": 960},
                "height": {"type": "integer", "minimum": 1, "maximum": 4096, "default": 540},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["object_name", "location"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "id": "inspect",
                "tool": "object.inspect",
                "arguments": {"object_name": {"$param": "object_name"}},
            },
            {
                "id": "checkpoint",
                "tool": "checkpoint.save",
                "arguments": {"tag": {"$param": "checkpoint_tag"}},
            },
            {
                "id": "move",
                "tool": "object.transform",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "location": {"$param": "location"},
                    "space": "local",
                    "expected_state": {"$ref": "inspect.data.state_token"},
                },
                "rollback": {
                    "tool": "object.transform",
                    "arguments": {
                        "object_name": {"$param": "object_name"},
                        "location": {"$ref": "inspect.data.object_detail.transform.location"},
                        "space": "local",
                        "expected_state": {"$ref": "move.data.state_token"},
                    },
                },
            },
            {
                "id": "verify",
                "tool": "object.inspect",
                "arguments": {"object_name": {"$param": "object_name"}},
            },
            {
                "id": "review",
                "tool": "render.review_set",
                "arguments": {
                    "output_dir": {"$param": "output_dir", "omit_if_none": True},
                    "output_prefix": {"$param": "output_prefix"},
                    "views": {"$param": "views"},
                    "width": {"$param": "width"},
                    "height": {"$param": "height"},
                    "overwrite": {"$param": "overwrite"},
                },
            },
        ],
    },
    "animation_health_v1": {
        "version": 1,
        "description": "Inspect one object's Action data and run bounded animation dependency/range validation.",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "object_name": _NAME_SCHEMA,
                "action_name": {**_NAME_SCHEMA, "type": ["string", "null"], "default": None},
                "include_keyframes": {"type": "boolean", "default": False},
                "max_keys_per_curve": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "frame_start": _FRAME_SCHEMA,
                "frame_end": _FRAME_SCHEMA,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "id": "inspect",
                "tool": "animation.inspect",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "action_name": {"$param": "action_name", "omit_if_none": True},
                    "include_keyframes": {"$param": "include_keyframes"},
                    "max_keys_per_curve": {"$param": "max_keys_per_curve"},
                },
            },
            {
                "id": "validate",
                "tool": "animation.validate",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "action_name": {"$param": "action_name", "omit_if_none": True},
                    "frame_start": {"$param": "frame_start", "omit_if_none": True},
                    "frame_end": {"$param": "frame_end", "omit_if_none": True},
                },
            },
        ],
    },
    "boolean_opening_v1": {
        "version": 1,
        "description": "Add and verify one exact-target Boolean opening modifier with an optimistic state precondition and removable compensation.",
        "run_options": {"rollback_on_failure": True, "confirm_rollback": True},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "object_name": _NAME_SCHEMA,
                "cutter_object": _NAME_SCHEMA,
                "expected_state": _STATE_SCHEMA,
                "modifier_name": {**_NAME_SCHEMA, "default": "CBT_BooleanOpening"},
                "solver": {"type": "string", "enum": ["FLOAT", "EXACT", "MANIFOLD"], "default": "EXACT"},
                "use_hole_tolerant": {"type": "boolean", "default": False},
            },
            "required": ["object_name", "cutter_object", "expected_state"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "id": "add_boolean",
                "tool": "modifier.add",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "type": "BOOLEAN",
                    "name": {"$param": "modifier_name"},
                    "settings": {
                        "operation": "DIFFERENCE",
                        "solver": {"$param": "solver"},
                        "operand_type": "OBJECT",
                        "object": {"$param": "cutter_object"},
                        "use_hole_tolerant": {"$param": "use_hole_tolerant"},
                    },
                    "expected_state": {"$param": "expected_state"},
                },
                "rollback": {
                    "tool": "modifier.remove",
                    "arguments": {
                        "object_name": {"$param": "object_name"},
                        "modifier_name": {"$ref": "add_boolean.data.modifier"},
                        "dry_run": False,
                        "confirm": True,
                    },
                },
            },
            {
                "id": "verify_boolean",
                "tool": "modifier.inspect",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "modifier_name": {"$ref": "add_boolean.data.modifier"},
                },
            },
        ],
    },
    "mirror_array_v1": {
        "version": 1,
        "description": "Add a bounded Mirror then Array stack with state chaining and exact modifier-removal compensation.",
        "run_options": {"rollback_on_failure": True, "confirm_rollback": True},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "object_name": _NAME_SCHEMA,
                "expected_state": _STATE_SCHEMA,
                "mirror_name": {**_NAME_SCHEMA, "default": "CBT_Mirror"},
                "array_name": {**_NAME_SCHEMA, "default": "CBT_Array"},
                "mirror_object": {**_NAME_SCHEMA, "type": ["string", "null"], "default": None},
                "use_axis": {
                    "type": "array",
                    "items": {"type": "boolean"},
                    "minItems": 3,
                    "maxItems": 3,
                    "default": [True, False, False],
                },
                "use_clip": {"type": "boolean", "default": True},
                "merge_threshold": {"type": "number", "minimum": 0, "maximum": 1_000_000, "default": 0.001},
                "count": {"type": "integer", "minimum": 1, "maximum": 100_000, "default": 2},
                "relative_offset": {**_VECTOR3_SCHEMA, "default": [1.0, 0.0, 0.0]},
            },
            "required": ["object_name", "expected_state"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "id": "add_mirror",
                "tool": "modifier.add",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "type": "MIRROR",
                    "name": {"$param": "mirror_name"},
                    "settings": {
                        "use_axis": {"$param": "use_axis"},
                        "use_clip": {"$param": "use_clip"},
                        "merge_threshold": {"$param": "merge_threshold"},
                        "mirror_object": {"$param": "mirror_object", "omit_if_none": True},
                    },
                    "expected_state": {"$param": "expected_state"},
                },
                "rollback": {
                    "tool": "modifier.remove",
                    "arguments": {
                        "object_name": {"$param": "object_name"},
                        "modifier_name": {"$ref": "add_mirror.data.modifier"},
                        "dry_run": False,
                        "confirm": True,
                    },
                },
            },
            {
                "id": "add_array",
                "tool": "modifier.add",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "type": "ARRAY",
                    "name": {"$param": "array_name"},
                    "settings": {
                        "count": {"$param": "count"},
                        "use_relative_offset": True,
                        "relative_offset_displace": {"$param": "relative_offset"},
                    },
                    "expected_state": {"$ref": "add_mirror.data.state_token"},
                },
                "rollback": {
                    "tool": "modifier.remove",
                    "arguments": {
                        "object_name": {"$param": "object_name"},
                        "modifier_name": {"$ref": "add_array.data.modifier"},
                        "dry_run": False,
                        "confirm": True,
                    },
                },
            },
            {
                "id": "verify_array",
                "tool": "modifier.inspect",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "modifier_name": {"$ref": "add_array.data.modifier"},
                },
            },
        ],
    },
    "panel_inset_v1": {
        "version": 1,
        "description": "Dry-run-first atomic inset/depth panel operation on exact face indices.",
        "run_options": {"rollback_on_failure": False},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "object_name": _NAME_SCHEMA,
                "face_indices": _INDEX_LIST_SCHEMA,
                "expected_state": _STATE_SCHEMA,
                "thickness": {"type": "number", "minimum": 0.000001, "maximum": 1_000_000},
                "depth": {"type": "number", "minimum": -1_000_000, "maximum": 1_000_000, "default": -0.02},
                "use_even_offset": {"type": "boolean", "default": True},
                "use_boundary": {"type": "boolean", "default": True},
                "max_elements": {"type": "integer", "minimum": 1, "maximum": 5_000_000, "default": 500_000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": _CONFIRM_SCHEMA,
            },
            "required": ["object_name", "face_indices", "expected_state", "thickness"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "id": "panel",
                "tool": "mesh.edit",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "operations": [
                        {
                            "op": "inset",
                            "selection": {
                                "domain": "FACE",
                                "predicate": {"kind": "INDICES", "indices": {"$param": "face_indices"}},
                            },
                            "thickness": {"$param": "thickness"},
                            "depth": {"$param": "depth"},
                            "use_even_offset": {"$param": "use_even_offset"},
                            "use_boundary": {"$param": "use_boundary"},
                        }
                    ],
                    "max_elements": {"$param": "max_elements"},
                    "dry_run": {"$param": "dry_run"},
                    "confirm": {"$param": "confirm", "omit_if_none": True},
                    "expected_state": {"$param": "expected_state"},
                },
            }
        ],
    },
    "support_loop_pair_v1": {
        "version": 1,
        "description": "Dry-run-first atomic one-segment bevel that creates a support-edge pair around exact edge indices.",
        "run_options": {"rollback_on_failure": False},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "object_name": _NAME_SCHEMA,
                "edge_indices": _INDEX_LIST_SCHEMA,
                "expected_state": _STATE_SCHEMA,
                "offset": {"type": "number", "minimum": 0.000001, "maximum": 1_000_000},
                "clamp_overlap": {"type": "boolean", "default": True},
                "max_elements": {"type": "integer", "minimum": 1, "maximum": 5_000_000, "default": 500_000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": _CONFIRM_SCHEMA,
            },
            "required": ["object_name", "edge_indices", "expected_state", "offset"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "id": "support",
                "tool": "mesh.edit",
                "arguments": {
                    "object_name": {"$param": "object_name"},
                    "operations": [
                        {
                            "op": "bevel",
                            "selection": {
                                "domain": "EDGE",
                                "predicate": {"kind": "INDICES", "indices": {"$param": "edge_indices"}},
                            },
                            "affect": "EDGES",
                            "offset": {"$param": "offset"},
                            "segments": 1,
                            "clamp_overlap": {"$param": "clamp_overlap"},
                        }
                    ],
                    "max_elements": {"$param": "max_elements"},
                    "dry_run": {"$param": "dry_run"},
                    "confirm": {"$param": "confirm", "omit_if_none": True},
                    "expected_state": {"$param": "expected_state"},
                },
            }
        ],
    },
}


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


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


def _validate_value(label: str, value: Any, schema: dict[str, Any]) -> None:
    accepted = schema.get("type")
    accepted_types = {accepted} if isinstance(accepted, str) else set(accepted or [])
    actual = _json_type(value)
    if actual == "integer" and "number" in accepted_types:
        actual = "number"
    if actual not in accepted_types:
        raise WorkflowTemplateError(f"{label} must have JSON type {' or '.join(sorted(accepted_types))}")
    if actual == "null":
        return
    if "enum" in schema and value not in schema["enum"]:
        raise WorkflowTemplateError(f"{label} must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise WorkflowTemplateError(f"{label} is too short")
        if len(value) > schema.get("maxLength", len(value)):
            raise WorkflowTemplateError(f"{label} is too long")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            raise WorkflowTemplateError(f"{label} does not match its required pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise WorkflowTemplateError(f"{label} must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise WorkflowTemplateError(f"{label} is below its minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise WorkflowTemplateError(f"{label} exceeds its maximum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise WorkflowTemplateError(f"{label} has too few items")
        if len(value) > schema.get("maxItems", len(value)):
            raise WorkflowTemplateError(f"{label} has too many items")
        if schema.get("uniqueItems") is True:
            serialized = [json.dumps(item, sort_keys=True, ensure_ascii=True) for item in value]
            if len(serialized) != len(set(serialized)):
                raise WorkflowTemplateError(f"{label} must not contain duplicate items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(f"{label}[{index}]", item, item_schema)


def _resolve_parameters(name: str, supplied: Any) -> dict[str, Any]:
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict) or not all(isinstance(key, str) for key in supplied):
        raise WorkflowTemplateError("parameters must be an object with string keys")
    try:
        encoded = json.dumps(supplied, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowTemplateError("parameters must contain only finite JSON values") from exc
    if len(encoded) > MAX_TEMPLATE_PARAMETER_BYTES:
        raise WorkflowTemplateError(f"parameters exceed {MAX_TEMPLATE_PARAMETER_BYTES} bytes")
    schema = _TEMPLATES[name]["parameter_schema"]
    properties = schema["properties"]
    unknown = sorted(set(supplied) - set(properties))
    if unknown:
        raise WorkflowTemplateError(f"Unknown template parameters: {', '.join(unknown)}")
    required = set(schema.get("required", []))
    missing = sorted(required - set(supplied))
    if missing:
        raise WorkflowTemplateError(f"Missing template parameters: {', '.join(missing)}")
    resolved: dict[str, Any] = {}
    for parameter, parameter_schema in properties.items():
        if parameter in supplied:
            value = supplied[parameter]
        elif "default" in parameter_schema:
            value = deepcopy(parameter_schema["default"])
        else:
            continue
        _validate_value(parameter, value, parameter_schema)
        resolved[parameter] = deepcopy(value)
    if name == "animation_health_v1":
        start = resolved.get("frame_start")
        end = resolved.get("frame_end")
        if start is not None and end is not None and start > end:
            raise WorkflowTemplateError("frame_start must be less than or equal to frame_end")
    if name == "boolean_opening_v1" and resolved["object_name"] == resolved["cutter_object"]:
        raise WorkflowTemplateError("object_name and cutter_object must be different")
    if name == "mirror_array_v1":
        if resolved["mirror_name"] == resolved["array_name"]:
            raise WorkflowTemplateError("mirror_name and array_name must be different")
        if not any(resolved["use_axis"]):
            raise WorkflowTemplateError("use_axis must enable at least one axis")
    if name in {"panel_inset_v1", "support_loop_pair_v1"}:
        if resolved["dry_run"] is False and resolved.get("confirm") is not True:
            raise WorkflowTemplateError("dry_run: false requires confirm: true")
    return resolved


def _expand_value(value: Any, parameters: dict[str, Any]) -> Any:
    if isinstance(value, list):
        result = []
        for item in value:
            expanded = _expand_value(item, parameters)
            if expanded is not _OMIT:
                result.append(expanded)
        return result
    if not isinstance(value, dict):
        return deepcopy(value)
    if "$param" in value:
        if set(value) - {"$param", "omit_if_none"}:
            raise WorkflowTemplateError("template parameter placeholders have unknown fields")
        parameter = value["$param"]
        if not isinstance(parameter, str) or parameter not in parameters:
            raise WorkflowTemplateError("template references an unresolved parameter")
        resolved = deepcopy(parameters[parameter])
        if resolved is None and value.get("omit_if_none") is True:
            return _OMIT
        return resolved
    result: dict[str, Any] = {}
    for key, item in value.items():
        expanded = _expand_value(item, parameters)
        if expanded is not _OMIT:
            result[key] = expanded
    return result


def list_templates() -> dict[str, Any]:
    templates = [
        {
            "name": name,
            "version": definition["version"],
            "description": definition["description"],
            "step_count": len(definition["steps"]),
            "run_options": deepcopy(definition.get("run_options", {"rollback_on_failure": False})),
            "template_digest": _digest(definition),
        }
        for name, definition in sorted(_TEMPLATES.items())
    ]
    return {"count": len(templates), "templates": templates, "catalog_digest": _digest(_TEMPLATES)}


def describe_template(name: str) -> dict[str, Any]:
    definition = _TEMPLATES.get(name)
    if definition is None:
        raise WorkflowTemplateError(f"Unknown workflow template: {name}")
    return {
        "name": name,
        "version": definition["version"],
        "description": definition["description"],
        "run_options": deepcopy(definition.get("run_options", {"rollback_on_failure": False})),
        "parameter_schema": deepcopy(definition["parameter_schema"]),
        "step_outline": [
            {
                "id": step["id"],
                "tool": step["tool"],
                "has_rollback": step.get("rollback") is not None,
            }
            for step in definition["steps"]
        ],
        "template_digest": _digest(definition),
    }


def expand_template(name: str, parameters: Any = None) -> dict[str, Any]:
    definition = _TEMPLATES.get(name)
    if definition is None:
        raise WorkflowTemplateError(f"Unknown workflow template: {name}")
    resolved = _resolve_parameters(name, parameters)
    expanded = _expand_value(definition["steps"], resolved)
    if not isinstance(expanded, list):
        raise WorkflowTemplateError("template did not expand to a workflow step list")
    steps = validate_steps(expanded)
    return {
        "name": name,
        "version": definition["version"],
        "template_digest": _digest(definition),
        "parameters": resolved,
        "steps": steps,
        "step_count": len(steps),
        "run_options": deepcopy(definition.get("run_options", {"rollback_on_failure": False})),
    }
