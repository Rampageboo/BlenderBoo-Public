"""Material inspection and RNA-validated node-graph mutation."""

from __future__ import annotations

import math
from typing import Any

from ..errors import InvalidParamsError, MutationApplyError, NameConflictError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


_OPERATION_KINDS = frozenset({"add_node", "remove_node", "set_input", "set_node_property", "ramp_add", "ramp_remove", "ramp_set", "connect", "disconnect", "set_material_flag"})


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidParamsError("values must be finite JSON-safe values")
        return value
    try:
        return [_json_safe(item) for item in value]
    except TypeError as exc:
        raise InvalidParamsError("values must be JSON-safe scalars or arrays") from exc


def _strict_bool(params: dict[str, Any], name: str, default: bool) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{name} must be a boolean")
    return value


def _node_page_params(params: dict[str, Any]) -> tuple[int, int]:
    limit = params.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise InvalidParamsError("limit must be an integer from 1 to 2000")
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError("cursor must be a non-negative decimal string returned by material.inspect")


def _objects_in_collection(collection) -> set[Any]:
    objects = set(collection.objects)
    for child in collection.children:
        objects.update(_objects_in_collection(child))
    return objects


def _material_objects(bpy, material, scope: set[Any] | None = None) -> list[Any]:
    return [
        obj for obj in bpy.context.scene.objects
        if (scope is None or obj in scope)
        and any(slot.material == material for slot in getattr(obj, "material_slots", ()))
    ]


def _socket_summary(socket, index: int | None = None) -> dict[str, Any]:
    result = {
        "name": socket.name, "identifier": str(getattr(socket, "identifier", socket.name)),
        "type": str(getattr(socket, "bl_idname", "")),
        "index": index, "linked": bool(socket.is_linked),
        "editable": bool(hasattr(socket, "default_value") and not socket.is_linked and getattr(socket, "enabled", True)),
    }
    if not socket.is_linked and hasattr(socket, "default_value"):
        result["default_value"] = _json_safe(socket.default_value)
    return result


def _node_summary(node) -> dict[str, Any]:
    result = {
        "name": node.name, "type": node.bl_idname,
        "inputs": [_socket_summary(socket, index) for index, socket in enumerate(node.inputs)],
        "outputs": [_socket_summary(socket, index) for index, socket in enumerate(node.outputs)],
    }
    if hasattr(node, "bl_rna"):
        result["writable_properties"] = sorted(
            prop.identifier for prop in node.bl_rna.properties
            if not prop.is_readonly and prop.type not in {"POINTER", "COLLECTION"}
            and prop.identifier not in {"name", "label", "location", "id_data", "rna_type"}
        )
    if hasattr(node, "color_ramp"):
        result["color_ramp"] = [
            {"index": index, "position": float(element.position), "color": _json_safe(element.color)}
            for index, element in enumerate(node.color_ramp.elements)
        ]
    if node.bl_idname == "ShaderNodeGroup":
        result["group"] = getattr(getattr(node, "node_tree", None), "name", None)
    return result


def _socket_lookup(sockets, selector: Any, label: str):
    if isinstance(selector, bool):
        raise InvalidParamsError(f"{label} must be a socket name or index")
    if isinstance(selector, int):
        try:
            return sockets[selector]
        except (IndexError, KeyError):
            raise NotFoundError(f"{label} socket index not found: {selector}") from None
    if isinstance(selector, str) and selector:
        by_identifier = [socket for socket in sockets if getattr(socket, "identifier", None) == selector]
        matches = by_identifier or [socket for socket in sockets if socket.name == selector]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise InvalidParamsError(f"{label} socket name is ambiguous; use its identifier or index: {selector}")
        raise NotFoundError(f"{label} socket not found: {selector}")
    raise InvalidParamsError(f"{label} must be a socket name or index")


def _node_lookup(nodes, name: Any):
    if not isinstance(name, str) or not name:
        raise InvalidParamsError("node name must be a non-empty string")
    node = nodes.get(name)
    if node is None:
        raise NotFoundError(f"Node not found: {name}")
    return node


def _material_flag_names(material) -> set[str]:
    return {name for name in {"use_backface_culling", "blend_method", "shadow_method", "surface_render_method"} if hasattr(material, name)}


def _flag_value(material, flag: Any) -> Any:
    if not isinstance(flag, str) or flag not in _material_flag_names(material):
        raise InvalidParamsError("flag is not allowlisted or is unavailable in this Blender version")
    return getattr(material, flag)


def _node_property(node: Any, name: Any, value: Any) -> Any:
    if not isinstance(name, str) or not name or name in {"name", "label", "location", "node_tree", "id_data", "rna_type"}:
        raise InvalidParamsError("property must name a writable scalar node RNA property")
    prop = node.bl_rna.properties.get(name) if hasattr(node, "bl_rna") else None
    if prop is None or prop.is_readonly or prop.type in {"POINTER", "COLLECTION"}:
        raise InvalidParamsError(f"Node property is unavailable or read-only: {name}")
    value = _json_safe(value)
    values = value if prop.is_array else [value]
    if prop.is_array and (not isinstance(value, list) or len(value) != prop.array_length):
        raise InvalidParamsError(f"{name} must be an array of length {prop.array_length}")
    for item in values:
        if prop.type == "BOOLEAN" and not isinstance(item, bool):
            raise InvalidParamsError(f"{name} must be boolean")
        if prop.type == "INT" and (isinstance(item, bool) or not isinstance(item, int)):
            raise InvalidParamsError(f"{name} must be integer")
        if prop.type == "FLOAT" and (isinstance(item, bool) or not isinstance(item, (int, float))):
            raise InvalidParamsError(f"{name} must be numeric")
        if prop.type in {"INT", "FLOAT"} and not prop.hard_min <= item <= prop.hard_max:
            raise InvalidParamsError(f"{name} is outside Blender RNA range")
        if prop.type == "STRING" and not isinstance(item, str):
            raise InvalidParamsError(f"{name} must be a string")
        if prop.type == "ENUM" and item not in {entry.identifier for entry in prop.enum_items}:
            raise InvalidParamsError(f"{name} is not a valid enum value")
    return value


def _ramp(node: Any) -> Any:
    ramp = getattr(node, "color_ramp", None)
    if ramp is None:
        raise InvalidParamsError(f"Node has no Color Ramp: {node.name}")
    return ramp


def _ramp_element(node: Any, index: Any) -> Any:
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(_ramp(node).elements):
        raise InvalidParamsError("ramp element index is out of range")
    return _ramp(node).elements[index]


def _summary(material, bpy, scope: set[Any] | None, include_nodes: bool, limit: int, offset: int) -> dict[str, Any]:
    tree = material.node_tree if material.use_nodes else None
    nodes = list(tree.nodes) if tree is not None else []
    principled = next((node for node in nodes if node.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    principal_data = None
    if principled is not None:
        values = {}
        for name in ("Base Color", "Metallic", "Roughness", "Emission Color", "Emission"):
            socket = principled.inputs.get(name)
            if socket is not None:
                values[name] = _socket_summary(socket)
        principal_data = {"node": principled.name, "inputs": values}
    result: dict[str, Any] = {
        "name": material.name, "user_count": int(material.users),
        "object_count": len(_material_objects(bpy, material, scope)), "uses_nodes": bool(material.use_nodes),
        "has_output_node": any(node.bl_idname == "ShaderNodeOutputMaterial" for node in nodes), "principled": principal_data,
    }
    if include_nodes:
        node_records = [_node_summary(node) for node in sorted(nodes, key=lambda node: node.name)]
        links = [
            {"from_node": link.from_node.name, "from_socket": link.from_socket.name, "to_node": link.to_node.name, "to_socket": link.to_socket.name}
            for link in tree.links
        ] if tree else []
        # A single cursor pages both potentially large graph collections. Advancing by
        # the requested limit remains forward-progressing even after one collection ends.
        page = node_records[offset : offset + limit]
        link_page = links[offset : offset + limit]
        next_offset = offset + limit
        truncated = next_offset < len(node_records) or next_offset < len(links)
        next_cursor = str(next_offset) if truncated else None
        result.update(
            nodes=page,
            links=link_page,
            next_cursor=next_cursor,
            truncated=truncated,
        )
    return result


def inspect_material(params: dict[str, Any]) -> ToolResult:
    unknown = sorted(set(params) - {"material", "object_name", "collection", "include_nodes", "limit", "cursor"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    material_name, object_name, collection_name = params.get("material"), params.get("object_name"), params.get("collection")
    for name, value in (("material", material_name), ("object_name", object_name), ("collection", collection_name)):
        if value is not None and (not isinstance(value, str) or not value):
            raise InvalidParamsError(f"{name} must be a non-empty string")
    include_nodes = _strict_bool(params, "include_nodes", False)
    limit, offset = _node_page_params(params)
    bpy = get_bpy()
    scope: set[Any] | None = None
    if object_name is not None:
        obj = bpy.context.scene.objects.get(object_name)
        if obj is None:
            raise NotFoundError(f"Object not found in current scene: {object_name}")
        scope = {obj}
    if collection_name is not None:
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            raise NotFoundError(f"Collection not found: {collection_name}")
        collection_scope = _objects_in_collection(collection)
        scope = collection_scope if scope is None else scope & collection_scope
    if material_name is not None:
        material = bpy.data.materials.get(material_name)
        if material is None:
            raise NotFoundError(f"Material not found: {material_name}")
        materials = [material]
    elif scope is not None:
        materials = sorted({slot.material for obj in scope for slot in getattr(obj, "material_slots", ()) if slot.material}, key=lambda material: material.name)
    else:
        materials = sorted(bpy.data.materials, key=lambda material: material.name)
    return ToolResult(data={"count": len(materials), "materials": [_summary(material, bpy, scope, include_nodes, limit, offset) for material in materials]})


def create_material(params: dict[str, Any]) -> dict[str, Any]:
    """Create one independent material datablock with an optional default node tree."""
    unknown = sorted(set(params) - {"name", "use_nodes", "use_fake_user", "expected_state"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidParamsError("name must be a non-empty string")
    use_nodes = _strict_bool(params, "use_nodes", True)
    use_fake_user = _strict_bool(params, "use_fake_user", False)
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.data.materials.get(name) is not None:
        raise NameConflictError(
            f"Material name already exists: {name}",
            {"conflict": {"type": "Material", "name": name}},
        )
    material = None
    try:
        material = bpy.data.materials.new(name)
        material.use_nodes = use_nodes
        material.use_fake_user = use_fake_user
        detail = _summary(material, bpy, None, False, 1, 0)
    except Exception as exc:
        cleanup_error = None
        if material is not None and bpy.data.materials.get(material.name) is material:
            try:
                bpy.data.materials.remove(material)
            except Exception as rollback_exc:
                cleanup_error = rollback_exc
        transaction = new_transaction([{"type": "Material", "name": name}])
        transaction.update(
            material=name,
            changed=cleanup_error is not None,
            warnings=[] if cleanup_error is None else [f"create cleanup failed: {cleanup_error}"],
            journal=[{"op": "create", "target": name, "before": None, "after": None, "status": "failed"}],
        )
        if cleanup_error is not None:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"material.create failed: {exc}", transaction) from exc
    transaction = new_transaction([{"type": "Material", "name": material.name}])
    transaction.update(
        material=material.name,
        requested_name=name,
        detail=detail,
        changed=True,
        journal=[{"op": "create", "target": material.name, "before": None, "after": detail, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _operation_kind(operation: Any) -> str:
    if not isinstance(operation, dict):
        raise InvalidParamsError("each operation must be an object")
    kind = operation.get("op")
    if kind not in _OPERATION_KINDS:
        raise InvalidParamsError("op must be one of: " + ", ".join(sorted(_OPERATION_KINDS)))
    return kind


def _selector_is_valid(selector: Any, label: str) -> bool:
    if isinstance(selector, bool) or not isinstance(selector, (int, str)) or (isinstance(selector, str) and not selector):
        raise InvalidParamsError(f"{label} must be a socket name or index")
    return True


def _validate_operation(operation: dict[str, Any], nodes, material, planned_nodes: set[str], bpy=None) -> None:
    kind = _operation_kind(operation)
    allowed = {
        "add_node": {"op", "node_type", "name", "location", "group"}, "remove_node": {"op", "name"},
        "set_input": {"op", "node", "input", "value"}, "connect": {"op", "from_node", "from_output", "to_node", "to_input"},
        "disconnect": {"op", "to_node", "to_input"}, "set_material_flag": {"op", "flag", "value"},
        "set_node_property": {"op", "node", "property", "value"},
        "ramp_add": {"op", "node", "position", "color"},
        "ramp_remove": {"op", "node", "index"},
        "ramp_set": {"op", "node", "index", "position", "color"},
    }[kind]
    unknown = sorted(set(operation) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown fields for {kind}: {', '.join(unknown)}")
    if kind == "add_node":
        node_type, name = operation.get("node_type"), operation.get("name")
        if not isinstance(node_type, str) or not node_type.startswith("ShaderNode") or (bpy is not None and getattr(bpy.types, node_type, None) is None):
            raise InvalidParamsError("node_type must be available ShaderNode type")
        if node_type == "ShaderNodeGroup":
            group_name = operation.get("group")
            if not isinstance(group_name, str) or not group_name or bpy is None or bpy.data.node_groups.get(group_name) is None:
                raise InvalidParamsError("ShaderNodeGroup requires an existing group name")
            if bpy.data.node_groups[group_name].bl_idname != "ShaderNodeTree":
                raise InvalidParamsError("group must be a ShaderNodeTree")
        elif "group" in operation:
            raise InvalidParamsError("group is only valid for ShaderNodeGroup")
        if not isinstance(name, str) or not name:
            raise InvalidParamsError("add_node.name must be a non-empty string")
        location = operation.get("location")
        if location is not None and (not isinstance(location, list) or len(location) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in location)):
            raise InvalidParamsError("add_node.location must be a two-number array")
        existing = nodes.get(name)
        if existing is not None and existing.bl_idname != node_type:
            raise InvalidParamsError(f"Node {name} already exists with type {existing.bl_idname}, not {node_type}")
        if existing is not None and node_type == "ShaderNodeGroup" and getattr(getattr(existing, "node_tree", None), "name", None) != operation["group"]:
            raise InvalidParamsError(f"Node {name} already references a different shader group")
    elif kind == "remove_node":
        _node_lookup(nodes, operation.get("name"))
    elif kind == "set_input":
        node_name = operation.get("node")
        if node_name in planned_nodes:
            _selector_is_valid(operation.get("input"), "input"); _json_safe(operation.get("value")); return
        node = _node_lookup(nodes, node_name); socket = _socket_lookup(node.inputs, operation.get("input"), "input")
        if socket.is_linked or not hasattr(socket, "default_value"):
            raise InvalidParamsError("set_input requires an unlinked input socket with default_value")
        _json_safe(operation.get("value"))
    elif kind == "set_node_property":
        node_name = operation.get("node")
        if node_name in planned_nodes:
            raise InvalidParamsError("set_node_property on a newly added node requires a following patch")
        _node_property(_node_lookup(nodes, node_name), operation.get("property"), operation.get("value"))
    elif kind in {"ramp_add", "ramp_remove", "ramp_set"}:
        node_name = operation.get("node")
        if node_name in planned_nodes:
            raise InvalidParamsError("Color Ramp edits on a newly added node require a following patch")
        node = _node_lookup(nodes, node_name)
        ramp = _ramp(node)
        if kind in {"ramp_remove", "ramp_set"}:
            _ramp_element(node, operation.get("index"))
        if kind == "ramp_remove" and len(ramp.elements) <= 2:
            raise InvalidParamsError("Color Ramp must retain at least two elements")
        if kind in {"ramp_add", "ramp_set"}:
            if kind == "ramp_add" or "position" in operation:
                position = operation.get("position")
                if isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position) or not 0 <= position <= 1:
                    raise InvalidParamsError("position must be within 0..1")
            if "color" in operation:
                color = operation["color"]
                if not isinstance(color, list) or len(color) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in color):
                    raise InvalidParamsError("color must be four finite values within 0..1")
            if kind == "ramp_set" and "position" not in operation and "color" not in operation:
                raise InvalidParamsError("ramp_set requires position or color")
    elif kind == "connect":
        source_name, target_name = operation.get("from_node"), operation.get("to_node")
        if source_name in planned_nodes:
            _selector_is_valid(operation.get("from_output"), "from_output")
        else:
            _socket_lookup(_node_lookup(nodes, source_name).outputs, operation.get("from_output"), "from_output")
        if target_name in planned_nodes:
            _selector_is_valid(operation.get("to_input"), "to_input")
        else:
            _socket_lookup(_node_lookup(nodes, target_name).inputs, operation.get("to_input"), "to_input")
    elif kind == "disconnect":
        if operation.get("to_node") in planned_nodes:
            _selector_is_valid(operation.get("to_input"), "to_input")
        else:
            _socket_lookup(_node_lookup(nodes, operation.get("to_node")).inputs, operation.get("to_input"), "to_input")
    else:
        _flag_value(material, operation.get("flag")); _json_safe(operation.get("value"))


def _validate_precondition(precondition: Any, nodes, material) -> None:
    if not isinstance(precondition, dict):
        raise InvalidParamsError("each precondition must be an object")
    kind = precondition.get("kind")
    allowed = {"node_present": {"kind", "name", "present"}, "input_equals": {"kind", "node", "input", "value"}, "flag_equals": {"kind", "flag", "value"}}.get(kind)
    if allowed is None:
        raise InvalidParamsError("precondition kind must be node_present, input_equals, or flag_equals")
    unknown = sorted(set(precondition) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown fields for {kind}: {', '.join(unknown)}")
    if kind == "node_present":
        name, present = precondition.get("name"), precondition.get("present")
        if not isinstance(name, str) or not name or not isinstance(present, bool):
            raise InvalidParamsError("node_present requires name and boolean present")
        if (nodes.get(name) is not None) != present:
            raise InvalidParamsError(f"Precondition failed: node {name} present is not {present}")
    elif kind == "input_equals":
        node = _node_lookup(nodes, precondition.get("node")); socket = _socket_lookup(node.inputs, precondition.get("input"), "input")
        if socket.is_linked or _json_safe(socket.default_value) != _json_safe(precondition.get("value")):
            raise InvalidParamsError(f"Precondition failed: input {node.name}.{socket.name} does not equal expected value")
    else:
        if _json_safe(_flag_value(material, precondition.get("flag"))) != _json_safe(precondition.get("value")):
            raise InvalidParamsError(f"Precondition failed: flag {precondition.get('flag')} does not equal expected value")


def _journal_entry(operation: dict[str, Any], nodes, material, planned_nodes: set[str] | None = None) -> dict[str, Any]:
    planned_nodes = planned_nodes or set()
    kind = operation["op"]
    if kind == "add_node":
        node = nodes.get(operation["name"])
        return {"op": kind, "target": operation["name"], "before": _node_summary(node) if node else None, "after": {"name": operation["name"], "type": operation["node_type"]}}
    if kind == "remove_node":
        node = _node_lookup(nodes, operation["name"]); return {"op": kind, "target": node.name, "before": _node_summary(node), "after": None}
    if kind == "set_input":
        if operation["node"] in planned_nodes:
            return {"op": kind, "target": f"{operation['node']}.{operation['input']}", "before": None, "after": _json_safe(operation["value"])}
        node = _node_lookup(nodes, operation["node"]); socket = _socket_lookup(node.inputs, operation["input"], "input")
        return {"op": kind, "target": f"{node.name}.{socket.name}", "before": _json_safe(socket.default_value), "after": _json_safe(operation["value"])}
    if kind == "set_node_property":
        node = _node_lookup(nodes, operation["node"])
        prop = operation["property"]
        return {"op": kind, "target": f"{node.name}.{prop}", "before": _json_safe(getattr(node, prop)), "after": _json_safe(operation["value"])}
    if kind in {"ramp_add", "ramp_remove", "ramp_set"}:
        node = _node_lookup(nodes, operation["node"])
        element = _ramp_element(node, operation["index"]) if kind != "ramp_add" else None
        before = None if element is None else {"position": float(element.position), "color": _json_safe(element.color)}
        after = None if kind == "ramp_remove" else {name: _json_safe(operation[name]) for name in ("position", "color") if name in operation}
        return {"op": kind, "target": f"{node.name}.color_ramp", "before": before, "after": after}
    if kind in {"connect", "disconnect"}:
        target = f"{operation['to_node']}.{operation['to_input']}"
        before = []
        if operation["to_node"] not in planned_nodes:
            node = _node_lookup(nodes, operation["to_node"]); socket = _socket_lookup(node.inputs, operation["to_input"], "to_input")
            target = f"{node.name}.{socket.name}"; before = [{"from_node": link.from_node.name, "from_socket": link.from_socket.name} for link in socket.links]
        if kind == "disconnect":
            after = []
        else:
            source_socket = operation["from_output"]
            if operation["from_node"] not in planned_nodes:
                source_socket = _socket_lookup(_node_lookup(nodes, operation["from_node"]).outputs, source_socket, "from_output").name
            after = {"from_node": operation["from_node"], "from_socket": source_socket}
        return {"op": kind, "target": target, "before": before, "after": after}
    flag = operation["flag"]
    return {"op": kind, "target": flag, "before": _json_safe(_flag_value(material, flag)), "after": _json_safe(operation["value"])}


def _apply_operation(operation: dict[str, Any], nodes, links, material, bpy=None) -> str:
    kind = operation["op"]
    if kind == "add_node":
        node = nodes.get(operation["name"])
        if node is not None: return "present"
        node = nodes.new(operation["node_type"]); node.name = operation["name"]; node.label = operation["name"]
        if "group" in operation:
            node.node_tree = bpy.data.node_groups[operation["group"]]
        if operation.get("location") is not None: node.location = operation["location"]
    elif kind == "remove_node": nodes.remove(_node_lookup(nodes, operation["name"]))
    elif kind == "set_input":
        socket = _socket_lookup(_node_lookup(nodes, operation["node"]).inputs, operation["input"], "input")
        if _json_safe(socket.default_value) == _json_safe(operation["value"]): return "present"
        socket.default_value = operation["value"]
    elif kind == "set_node_property":
        node = _node_lookup(nodes, operation["node"])
        prop = operation["property"]
        if _json_safe(getattr(node, prop)) == _json_safe(operation["value"]): return "present"
        setattr(node, prop, operation["value"])
    elif kind == "ramp_add":
        node = _node_lookup(nodes, operation["node"])
        element = _ramp(node).elements.new(operation["position"])
        if "color" in operation: element.color = operation["color"]
    elif kind == "ramp_remove":
        node = _node_lookup(nodes, operation["node"])
        _ramp(node).elements.remove(_ramp_element(node, operation["index"]))
    elif kind == "ramp_set":
        node = _node_lookup(nodes, operation["node"])
        element = _ramp_element(node, operation["index"])
        before = (float(element.position), _json_safe(element.color))
        if "position" in operation: element.position = operation["position"]
        if "color" in operation: element.color = operation["color"]
        if before == (float(element.position), _json_safe(element.color)): return "present"
    elif kind == "connect":
        output = _socket_lookup(_node_lookup(nodes, operation["from_node"]).outputs, operation["from_output"], "from_output")
        input_socket = _socket_lookup(_node_lookup(nodes, operation["to_node"]).inputs, operation["to_input"], "to_input")
        if len(input_socket.links) == 1 and input_socket.links[0].from_socket == output: return "present"
        for link in list(input_socket.links): links.remove(link)
        links.new(output, input_socket)
    elif kind == "disconnect":
        input_socket = _socket_lookup(_node_lookup(nodes, operation["to_node"]).inputs, operation["to_input"], "to_input")
        if not input_socket.links: return "absent"
        for link in list(input_socket.links): links.remove(link)
    else:
        if _json_safe(_flag_value(material, operation["flag"])) == _json_safe(operation["value"]): return "present"
        setattr(material, operation["flag"], operation["value"])
    return "applied"


def patch_material(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"material", "operations", "preconditions", "dry_run", "expected_user_count", "expected_state"})
    if unknown: raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    material_name, operations, preconditions = params.get("material"), params.get("operations"), params.get("preconditions", [])
    if not isinstance(material_name, str) or not material_name: raise InvalidParamsError("material must be a non-empty string")
    if not isinstance(operations, list) or not operations or not all(isinstance(operation, dict) for operation in operations): raise InvalidParamsError("operations must be a non-empty list of objects")
    if not isinstance(preconditions, list): raise InvalidParamsError("preconditions must be a list")
    dry_run = _strict_bool(params, "dry_run", True); expected_users = params.get("expected_user_count")
    if expected_users is not None and (isinstance(expected_users, bool) or not isinstance(expected_users, int) or expected_users < 0): raise InvalidParamsError("expected_user_count must be a non-negative integer")
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")
    bpy = get_bpy()
    if not dry_run:
        require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    material = bpy.data.materials.get(material_name)
    if material is None: raise NotFoundError(f"Material not found: {material_name}")
    if not material.use_nodes or material.node_tree is None: raise InvalidParamsError("material.patch requires a material with use_nodes enabled")
    if expected_users is not None and material.users != expected_users: raise InvalidParamsError(f"expected_user_count {expected_users} does not match actual user count {material.users}")
    nodes = material.node_tree.nodes
    for precondition in preconditions: _validate_precondition(precondition, nodes, material)
    planned_nodes: set[str] = set()
    for operation in operations:
        _validate_operation(operation, nodes, material, planned_nodes, bpy)
        if operation["op"] == "add_node" and nodes.get(operation["name"]) is None:
            planned_nodes.add(operation["name"])
    transaction = new_transaction([{"type": "Material", "name": material.name}])
    transaction.update(material=material.name, dry_run=dry_run, affected_user_count=int(material.users), journal=[])
    if dry_run:
        planned_nodes = set()
        for operation in operations:
            entry = _journal_entry(operation, nodes, material, planned_nodes); entry["status"] = "planned"; transaction["journal"].append(entry)
            if operation["op"] == "add_node" and nodes.get(operation["name"]) is None:
                planned_nodes.add(operation["name"])
        transaction["state_token"] = current_state_token(bpy)
        return transaction
    for operation in operations:
        entry = _journal_entry(operation, nodes, material)
        try: status = _apply_operation(operation, nodes, material.node_tree.links, material, bpy)
        except Exception as exc:
            entry["status"] = "failed"; transaction["journal"].append(entry)
            transaction["changed"] = any(item["status"] == "applied" for item in transaction["journal"])
            if transaction["changed"]:
                bump_epoch()
            transaction["state_token"] = current_state_token(bpy)
            raise MutationApplyError(f"material.patch failed while applying {operation['op']}: {exc}", transaction) from exc
        entry["status"] = status; transaction["journal"].append(entry)
        if status == "applied": transaction["changed"] = True
    if transaction["changed"]:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
