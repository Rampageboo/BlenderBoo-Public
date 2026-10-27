"""Read-only, bounded lookup against the running Blender Python API."""

from __future__ import annotations

from functools import lru_cache
import importlib
import inspect
import math
import re
from types import ModuleType
from typing import Any

from ..errors import InvalidParamsError

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MAX_LIMIT = 100
_MAX_SEARCH_ENTRIES = 100_000
_MAX_DOC = 2000
_API_CHANGES = {
    # Blender 5.0 Python API change log, bpy.types.Scene / Renamed.
    "bpy.types.Scene.node_tree": {
        "replacement": "bpy.types.Scene.compositing_node_group",
        "changed_in": "5.0",
        "source_url": "https://docs.blender.org/api/5.0/change_log.html#bpy-types-scene",
    },
}


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in list(value)[:32]]
    try:
        return [_json_value(item) for item in list(value)[:32]]
    except (TypeError, ValueError, RuntimeError):
        return str(value)[:160]


def _property(prop: Any, detail: str) -> dict[str, Any]:
    item = {"identifier": prop.identifier, "type": prop.type}
    if detail == "summary":
        return item
    item.update(
        subtype=getattr(prop, "subtype", None),
        array_length=getattr(prop, "array_length", 0),
        readonly=bool(getattr(prop, "is_readonly", False)),
        unit=getattr(prop, "unit", None),
        description=str(getattr(prop, "description", ""))[:_MAX_DOC],
    )
    try:
        item["default"] = _json_value(prop.default)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        item["default"] = None
    if prop.type == "ENUM":
        item["enum_items"] = [
            {"identifier": enum.identifier, "name": enum.name,
             "description": str(getattr(enum, "description", ""))[:_MAX_DOC],
             "value": enum.value}
            for enum in list(prop.enum_items)[:200]
        ]
        item["enum_items_truncated"] = len(prop.enum_items) > 200
    return item


def _function(function: Any, detail: str) -> dict[str, Any]:
    item = {"identifier": function.identifier}
    if detail == "full":
        item["description"] = str(getattr(function, "description", ""))[:_MAX_DOC]
        item["parameters"] = [_property(prop, "full") for prop in function.parameters]
        item["returns"] = [_property(prop, "full") for prop in getattr(function, "return_values", ())]
    return item


def _page(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    if offset > len(items):
        raise InvalidParamsError("cursor is beyond the result set")
    end = min(len(items), offset + limit)
    return {"items": items[offset:end], "total": len(items),
            "next_cursor": str(end) if end < len(items) else None}


def _suggest(missing: str, candidates: list[str]) -> list[str]:
    def edit_distance(left: str, right: str) -> int:
        previous = list(range(len(right) + 1))
        for index, char in enumerate(left, 1):
            current = [index]
            for column, other in enumerate(right, 1):
                current.append(min(current[-1] + 1, previous[column] + 1,
                                   previous[column - 1] + (char != other)))
            previous = current
        return previous[-1]

    folded = missing.casefold()
    tokens = [token for token in folded.split("_") if len(token) >= 3]
    return sorted(set(candidates), key=lambda name: (
        edit_distance(folded, name.casefold()),
        -sum(token in name.casefold() for token in tokens), name,
    ))[:5]


def _rna_lookup(path: str, detail: str, limit: int, offset: int, bpy: Any) -> dict[str, Any]:
    segments = path.split(".")
    if segments[:2] == ["bpy", "types"]:
        segments = segments[2:]
    if not segments:
        raise InvalidParamsError("path must name a bpy.types member")
    name = segments.pop(0)
    cls = getattr(bpy.types, name, None)
    if cls is None or not hasattr(cls, "bl_rna"):
        return {"exists": False, "path": path,
                "suggestions": _suggest(name, [entry for entry in dir(bpy.types) if not entry.startswith("_")])}
    rna = cls.bl_rna
    owner = f"bpy.types.{name}"
    for index, member in enumerate(segments):
        props = {prop.identifier: prop for prop in rna.properties if prop.identifier != "rna_type"}
        functions = {fn.identifier: fn for fn in rna.functions}
        prop = props.get(member)
        if prop is None:
            fn = functions.get(member)
            if fn is not None and index == len(segments) - 1:
                return {"exists": True, "kind": "rna_function", "path": path,
                        "owner": owner, "function": _function(fn, detail)}
            return {"exists": False, "path": path, "owner": owner,
                    "suggestions": _suggest(member, list(props) + list(functions))}
        if index == len(segments) - 1:
            return {"exists": True, "kind": "rna_property", "path": path,
                    "owner": owner, "property": _property(prop, detail)}
        fixed_type = getattr(prop, "fixed_type", None)
        if fixed_type is None:
            return {"exists": False, "path": path, "owner": owner + "." + member,
                    "suggestions": []}
        rna = fixed_type
        owner += "." + member
    entries = sorted(
        [("property", prop.identifier, prop) for prop in rna.properties if prop.identifier != "rna_type"]
        + [("function", fn.identifier, fn) for fn in rna.functions],
        key=lambda item: (item[0], item[1]),
    )
    page = _page(entries, limit, offset)
    page["items"] = [
        {"kind": kind, **(_property(item, detail) if kind == "property" else _function(item, detail))}
        for kind, _, item in page["items"]
    ]
    result = {"exists": True, "kind": "rna_struct", "path": path,
              "base": getattr(getattr(rna, "base", None), "identifier", None)}
    result.update(page)
    return result


def _verified_change_hint(result: dict[str, Any], bpy: Any) -> dict[str, Any]:
    """Attach a curated replacement only when it exists in this Blender."""
    if result.get("exists") is not False:
        return result
    change = _API_CHANGES.get(result.get("path"))
    if change is None:
        return result
    replacement = change["replacement"]
    target = _rna_lookup(replacement, "summary", 1, 0, bpy)
    if target.get("exists") is not True:
        return result
    result["replacement"] = dict(change)
    member = replacement.rsplit(".", 1)[-1]
    result["suggestions"] = [member, *[name for name in result.get("suggestions", []) if name != member]][:5]
    return result


def _operator_lookup(path: str, detail: str, limit: int, offset: int, bpy: Any) -> dict[str, Any]:
    segments = path.split(".")
    if len(segments) != 4:
        raise InvalidParamsError("operator path must be bpy.ops.category.name")
    category = getattr(bpy.ops, segments[2], None)
    if category is None:
        return {"exists": False, "path": path,
                "suggestions": _suggest(segments[2], [name for name in dir(bpy.ops) if not name.startswith("_")])}
    op = getattr(category, segments[3], None)
    if op is None:
        return {"exists": False, "path": path,
                "suggestions": _suggest(segments[3], [name for name in dir(category) if not name.startswith("_")])}
    try:
        rna = op.get_rna_type()
    except (AttributeError, RuntimeError):
        return {"exists": False, "path": path,
                "suggestions": _suggest(segments[3], [name for name in dir(category) if not name.startswith("_")])}
    properties = sorted((prop for prop in rna.properties if prop.identifier != "rna_type"),
                        key=lambda item: item.identifier)
    result = {"exists": True, "kind": "operator", "path": path,
              "poll_note": "Operator poll requirements are not introspectable."}
    if detail == "full":
        result["description"] = str(getattr(rna, "description", ""))[:_MAX_DOC]
    page = _page(properties, limit, offset)
    page["items"] = [_property(prop, detail) for prop in page["items"]]
    result.update(page)
    return result


def _python_lookup(path: str, detail: str, bpy: Any) -> dict[str, Any]:
    segments = path.split(".")
    root = segments[0]
    if root not in ("bmesh", "mathutils"):
        raise InvalidParamsError("path must start with bpy.types, bpy.ops, bmesh, or mathutils")
    obj: Any = importlib.import_module(root)
    parent_path = root
    for segment in segments[1:]:
        child = None
        if isinstance(obj, ModuleType):
            try:
                child = importlib.import_module(parent_path + "." + segment)
            except ModuleNotFoundError as exc:
                if exc.name not in (parent_path, parent_path + "." + segment):
                    raise
        if child is None:
            child = getattr(obj, segment, None)
        if child is None:
            return {"exists": False, "path": path,
                    "suggestions": _suggest(segment, [name for name in dir(obj) if not name.startswith("_")])}
        obj = child
        parent_path += "." + segment
    result = {"exists": True, "kind": "python_callable" if callable(obj) else "python_object", "path": path}
    if detail == "full":
        if callable(obj):
            try:
                result["signature"] = str(inspect.signature(obj))[:500]
            except (ValueError, TypeError):
                result["signature"] = None
        result["docstring"] = str(inspect.getdoc(obj) or "")[:_MAX_DOC]
    return result


@lru_cache(maxsize=1)
def _search_index() -> tuple[tuple[str, ...], bool]:
    import bpy
    names: set[str] = set()
    for type_name in dir(bpy.types):
        if type_name.startswith("_"):
            continue
        cls = getattr(bpy.types, type_name, None)
        if cls is None or not hasattr(cls, "bl_rna"):
            continue
        names.add(f"bpy.types.{type_name}")
        try:
            rna = cls.bl_rna
        except (AttributeError, RuntimeError):
            continue
        names.update(f"bpy.types.{type_name}.{prop.identifier}" for prop in rna.properties)
        names.update(f"bpy.types.{type_name}.{fn.identifier}" for fn in rna.functions)
        if len(names) >= _MAX_SEARCH_ENTRIES:
            break
    for category_name in dir(bpy.ops):
        if category_name.startswith("_"):
            continue
        category = getattr(bpy.ops, category_name, None)
        if category is None:
            continue
        names.update(f"bpy.ops.{category_name}.{name}" for name in dir(category) if not name.startswith("_"))
    import bmesh
    names.update(f"bmesh.ops.{name}" for name in dir(bmesh.ops) if not name.startswith("_"))
    return tuple(sorted(names)[:_MAX_SEARCH_ENTRIES]), len(names) > _MAX_SEARCH_ENTRIES


def lookup_api(params: dict[str, Any]) -> dict[str, Any]:
    """Inspect installed API metadata without reading or changing document state."""
    import bpy

    unknown = set(params) - {"path", "search", "detail", "limit", "cursor"}
    if unknown:
        raise InvalidParamsError(f"Unknown api.lookup fields: {', '.join(sorted(unknown))}")

    path = params.get("path")
    search = params.get("search")
    if (isinstance(path, str) and bool(path)) == (isinstance(search, str) and bool(search)):
        raise InvalidParamsError("provide exactly one nonempty path or search")
    detail = params.get("detail", "summary")
    if detail not in ("summary", "full"):
        raise InvalidParamsError("detail must be summary or full")
    limit = params.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise InvalidParamsError("limit must be an integer between 1 and 100")
    cursor = params.get("cursor", "0")
    if not isinstance(cursor, str) or not cursor.isdigit() or len(cursor) > 12:
        raise InvalidParamsError("cursor must be a numeric string")
    offset = int(cursor)
    version = bpy.app.version_string
    if search is not None:
        if not isinstance(search, str) or not search.strip() or len(search) > 100:
            raise InvalidParamsError("search must be 1-100 characters")
        tokens = search.casefold().split()
        names, truncated = _search_index()
        matches = [name for name in names if all(token in name.casefold() for token in tokens)]
        return {"blender_version": version, "exists": bool(matches), "kind": "search", "search": search,
                "index_truncated": truncated,
                **_page(matches, limit, offset)}
    if not isinstance(path, str) or len(path) > 200:
        raise InvalidParamsError("path must be at most 200 characters")
    normalized = path if path.startswith(("bpy.", "bmesh.", "mathutils.")) else "bpy.types." + path
    if any(not _IDENTIFIER.fullmatch(segment) for segment in normalized.split(".")):
        raise InvalidParamsError("path must contain only public dotted identifiers")
    if normalized.startswith("bpy.types."):
        result = _verified_change_hint(_rna_lookup(normalized, detail, limit, offset, bpy), bpy)
    elif normalized.startswith("bpy.ops."):
        result = _operator_lookup(normalized, detail, limit, offset, bpy)
    else:
        result = _python_lookup(normalized, detail, bpy)
    result["blender_version"] = version
    return result
