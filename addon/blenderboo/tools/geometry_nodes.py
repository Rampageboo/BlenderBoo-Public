"""Bounded, atomic Geometry Nodes graph and modifier-input controls."""

from __future__ import annotations

import math
from typing import Any

from ..errors import ConfirmationRequiredError, InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


MAX_PAGE = 2000
MAX_OPERATIONS = 500
MAX_NODES = 10_000
MAX_LINKS = 50_000
MAX_INTERFACE_SOCKETS = 1000
GEOMETRY_OBJECT_TYPES = frozenset({"MESH", "CURVE", "FONT", "SURFACE", "VOLUME", "POINTCLOUD"})
ALLOWED_NODE_TYPES = frozenset(
    {
        "FunctionNodeCompare", "FunctionNodeInputBool", "FunctionNodeInputInt",
        "GeometryNodeCurvePrimitiveCircle", "GeometryNodeCurvePrimitiveLine",
        "GeometryNodeCurveToMesh", "GeometryNodeCurveToPoints", "GeometryNodeDeleteGeometry",
        "GeometryNodeDistributePointsInVolume", "GeometryNodeDistributePointsOnFaces",
        "GeometryNodeExtrudeMesh", "GeometryNodeInputIndex", "GeometryNodeInputNormal",
        "GeometryNodeInputPosition", "GeometryNodeInstanceOnPoints", "GeometryNodeJoinGeometry",
        "GeometryNodeFillCurve", "GeometryNodeMergeByDistance", "GeometryNodeMeshCircle",
        "GeometryNodeMeshCone", "GeometryNodeMeshCube", "GeometryNodeMeshCylinder",
        "GeometryNodeMeshGrid", "GeometryNodeMeshIcoSphere", "GeometryNodeMeshLine",
        "GeometryNodeMeshToCurve", "GeometryNodeMeshUVSphere", "GeometryNodePoints",
        "GeometryNodeRealizeInstances", "GeometryNodeResampleCurve", "GeometryNodeSetCurveRadius",
        "GeometryNodeSetMaterial", "GeometryNodeSetPosition", "GeometryNodeSetSplineCyclic",
        "GeometryNodeSubdivideMesh", "GeometryNodeSubdivisionSurface", "GeometryNodeSwitch",
        "GeometryNodeTrimCurve",
        "GeometryNodeTransform", "ShaderNodeCombineXYZ", "ShaderNodeMath", "ShaderNodeRGB",
        "ShaderNodeSeparateXYZ", "ShaderNodeValue", "ShaderNodeVectorMath",
    }
)
ALLOWED_NODE_PROPERTIES = frozenset(
    {"operation", "data_type", "domain", "mode", "input_type", "clamp", "use_clamp"}
)
ALLOWED_SOCKET_TYPES = frozenset(
    {
        "NodeSocketBool", "NodeSocketCollection", "NodeSocketColor", "NodeSocketFloat",
        "NodeSocketGeometry", "NodeSocketInt", "NodeSocketMaterial", "NodeSocketObject",
        "NodeSocketString", "NodeSocketVector",
    }
)
OPERATION_KINDS = frozenset(
    {
        "add_node", "remove_node", "set_input", "set_node_property",
        "connect", "disconnect", "add_socket", "remove_socket", "set_socket_default", "pair_zone",
    }
)
DESTRUCTIVE_OPERATIONS = frozenset({"remove_node", "connect", "disconnect", "remove_socket"})


def _unknown(params: dict[str, Any], allowed: set[str], label: str = "parameters") -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown {label}: {', '.join(names)}")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _optional_name(value: Any, label: str) -> str | None:
    return None if value is None else _name(value, label)


def _strict_bool(value: Any, label: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidParamsError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParamsError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InvalidParamsError(f"{label} must be a finite number from {minimum} to {maximum}")
    return result


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "name") and hasattr(value, "bl_rna"):
        return {"type": value.bl_rna.identifier, "name": value.name}
    try:
        return [_json_safe(item) for item in value]
    except TypeError:
        return str(value)


def _validate_json_value(value: Any, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidParamsError(f"{label} must be finite")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            raise InvalidParamsError(f"{label} array is too large")
        return [_validate_json_value(item, label) for item in value]
    if isinstance(value, dict):
        _unknown(value, {"kind", "name"}, f"{label} reference fields")
        return {"kind": _name(value.get("kind"), f"{label}.kind").upper(), "name": _name(value.get("name"), f"{label}.name")}
    raise InvalidParamsError(f"{label} must be a JSON scalar, bounded array, or datablock reference")


def _page(params: dict[str, Any]) -> tuple[int, int]:
    limit = _integer(params.get("limit", 200), "limit", 1, MAX_PAGE)
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError("cursor must be a decimal string returned by geometry_nodes.inspect")


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _prepare_mutation(params: dict[str, Any]) -> Any:
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("Geometry Nodes mutation requires Blender Object mode")
    return bpy


def _object(bpy: Any, name: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    if obj.type not in GEOMETRY_OBJECT_TYPES:
        raise InvalidStateError(f"Object type does not support the guarded Geometry Nodes workflow: {obj.type}")
    return obj


def _modifier(obj: Any, name: str | None) -> Any:
    if name is not None:
        modifier = obj.modifiers.get(name)
        if modifier is None or modifier.type != "NODES":
            raise NotFoundError(f"Geometry Nodes modifier not found: {obj.name}.{name}")
        return modifier
    modifiers = [modifier for modifier in obj.modifiers if modifier.type == "NODES"]
    if len(modifiers) != 1:
        raise InvalidParamsError(
            f"modifier_name is required because {obj.name} has {len(modifiers)} Geometry Nodes modifiers"
        )
    return modifiers[0]


def _node_group(modifier: Any) -> Any:
    group = modifier.node_group
    if group is None:
        raise InvalidStateError(f"Geometry Nodes modifier has no node group: {modifier.name}")
    return group


def _interface_sockets(group: Any) -> list[Any]:
    interface = getattr(group, "interface", None)
    if interface is not None and hasattr(interface, "items_tree"):
        return [item for item in interface.items_tree if getattr(item, "item_type", None) == "SOCKET"]
    return [*getattr(group, "inputs", ()), *getattr(group, "outputs", ())]


def _interface_direction(socket: Any, group: Any) -> str:
    direction = getattr(socket, "in_out", None)
    if direction in {"INPUT", "OUTPUT"}:
        return direction
    return "INPUT" if any(candidate is socket for candidate in getattr(group, "inputs", ())) else "OUTPUT"


def _interface_socket_type(socket: Any) -> str:
    return str(getattr(socket, "socket_type", None) or getattr(socket, "bl_socket_idname", None) or "")


def _interface_summary(socket: Any, group: Any) -> dict[str, Any]:
    result = {
        "name": socket.name,
        "identifier": str(getattr(socket, "identifier", "")),
        "in_out": _interface_direction(socket, group),
        "socket_type": _interface_socket_type(socket),
    }
    if hasattr(socket, "default_value"):
        result["default_value"] = _json_safe(socket.default_value)
    return result


def _socket_summary(socket: Any) -> dict[str, Any]:
    result = {
        "name": socket.name,
        "identifier": str(getattr(socket, "identifier", "")),
        "type": str(getattr(socket, "bl_idname", "")),
        "linked": bool(socket.is_linked),
    }
    if not socket.is_linked and hasattr(socket, "default_value"):
        result["default_value"] = _json_safe(socket.default_value)
    return result


def _node_summary(node: Any) -> dict[str, Any]:
    properties = {}
    for name in ALLOWED_NODE_PROPERTIES:
        if hasattr(node, name):
            properties[name] = _json_safe(getattr(node, name))
    return {
        "name": node.name,
        "label": node.label,
        "type": node.bl_idname,
        "location": [float(node.location[0]), float(node.location[1])],
        "properties": properties,
        "inputs": [_socket_summary(socket) for socket in node.inputs],
        "outputs": [_socket_summary(socket) for socket in node.outputs],
    }


def _link_summary(link: Any) -> dict[str, Any]:
    return {
        "from_node": link.from_node.name,
        "from_socket": link.from_socket.name,
        "from_identifier": str(getattr(link.from_socket, "identifier", "")),
        "to_node": link.to_node.name,
        "to_socket": link.to_socket.name,
        "to_identifier": str(getattr(link.to_socket, "identifier", "")),
    }


def _modifier_input_wrapper(modifier: Any, identifier: str) -> Any | None:
    """Return Blender 5.2+'s typed modifier-input wrapper when available."""
    try:
        properties = getattr(modifier, "properties", None)
        inputs = getattr(properties, "inputs", None)
        wrapper = getattr(inputs, identifier, None)
    except (AttributeError, TypeError):
        return None
    return wrapper if wrapper is not None and hasattr(wrapper, "value") else None


def _modifier_input_snapshot(modifier: Any, identifier: str, socket: Any) -> dict[str, Any]:
    wrapper = _modifier_input_wrapper(modifier, identifier)
    if wrapper is not None:
        return {
            "backend": "rna",
            "wrapper": wrapper,
            "value": wrapper.value,
            "type": getattr(wrapper, "type", None),
            "attribute_name": getattr(wrapper, "attribute_name", None),
        }
    try:
        keys = set(modifier.keys())
    except (AttributeError, TypeError) as exc:
        raise InvalidStateError(
            f"Blender does not expose a writable Geometry Nodes modifier input: {identifier}"
        ) from exc
    return {
        "backend": "id_property",
        "modifier": modifier,
        "identifier": identifier,
        "exists": identifier in keys,
        "value": modifier[identifier] if identifier in keys else getattr(socket, "default_value", None),
    }


def _modifier_input_json(snapshot: dict[str, Any]) -> Any:
    return _json_safe(snapshot["value"])


def _modifier_input_overridden(snapshot: dict[str, Any], socket: Any) -> bool:
    if snapshot["backend"] == "id_property":
        return bool(snapshot["exists"])
    return (
        snapshot.get("type") not in {None, "VALUE"}
        or _modifier_input_json(snapshot) != _json_safe(getattr(socket, "default_value", None))
    )


def _assign_modifier_input(snapshot: dict[str, Any], value: Any) -> None:
    if snapshot["backend"] == "id_property":
        snapshot["modifier"][snapshot["identifier"]] = value
        return
    wrapper = snapshot["wrapper"]
    if hasattr(wrapper, "type"):
        wrapper.type = "VALUE"
    wrapper.value = value


def _restore_modifier_input(snapshot: dict[str, Any]) -> None:
    if snapshot["backend"] == "id_property":
        modifier = snapshot["modifier"]
        identifier = snapshot["identifier"]
        if snapshot["exists"]:
            modifier[identifier] = snapshot["value"]
        else:
            try:
                del modifier[identifier]
            except KeyError:
                pass
        return
    wrapper = snapshot["wrapper"]
    wrapper.value = snapshot["value"]
    if snapshot.get("attribute_name") is not None and hasattr(wrapper, "attribute_name"):
        wrapper.attribute_name = snapshot["attribute_name"]
    if snapshot.get("type") is not None and hasattr(wrapper, "type"):
        wrapper.type = snapshot["type"]


def _modifier_input_values(modifier: Any, group: Any) -> dict[str, Any]:
    values = {}
    for socket in _interface_sockets(group):
        if _interface_direction(socket, group) != "INPUT" or _interface_socket_type(socket) == "NodeSocketGeometry":
            continue
        identifier = str(getattr(socket, "identifier", ""))
        if identifier:
            snapshot = _modifier_input_snapshot(modifier, identifier, socket)
            values[identifier] = {
                "name": socket.name,
                "socket_type": _interface_socket_type(socket),
                "value": _modifier_input_json(snapshot),
                "overridden": _modifier_input_overridden(snapshot, socket),
            }
    return values


def _modifier_summary(obj: Any, modifier: Any, include_graph: bool, limit: int, offset: int) -> dict[str, Any]:
    group = modifier.node_group
    result: dict[str, Any] = {
        "object": obj.name,
        "modifier": modifier.name,
        "show_viewport": bool(modifier.show_viewport),
        "show_render": bool(modifier.show_render),
        "node_group": group.name if group else None,
        "node_group_users": int(group.users) if group else 0,
    }
    if group is None:
        return result
    interface = _interface_sockets(group)
    result.update(
        node_count=len(group.nodes),
        link_count=len(group.links),
        interface_socket_count=len(interface),
        modifier_inputs=_modifier_input_values(modifier, group),
    )
    if include_graph:
        nodes = sorted(group.nodes, key=lambda node: node.name)
        links = list(group.links)
        next_offset = offset + limit
        result.update(
            interface=[_interface_summary(socket, group) for socket in interface[offset : offset + limit]],
            nodes=[_node_summary(node) for node in nodes[offset : offset + limit]],
            links=[_link_summary(link) for link in links[offset : offset + limit]],
            next_cursor=str(next_offset) if any(next_offset < size for size in (len(interface), len(nodes), len(links))) else None,
            truncated=any(next_offset < size for size in (len(interface), len(nodes), len(links))),
        )
    return result


def inspect_geometry_nodes(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"object_name", "modifier_name", "include_graph", "max_nodes", "max_links", "limit", "cursor"})
    object_name = _optional_name(params.get("object_name"), "object_name")
    modifier_name = _optional_name(params.get("modifier_name"), "modifier_name")
    if modifier_name is not None and object_name is None:
        raise InvalidParamsError("modifier_name requires object_name")
    include_graph = _strict_bool(params.get("include_graph"), "include_graph", False)
    max_nodes = _integer(params.get("max_nodes", 2000), "max_nodes", 1, MAX_NODES)
    max_links = _integer(params.get("max_links", 10000), "max_links", 1, MAX_LINKS)
    limit, offset = _page(params)
    bpy = get_bpy()
    objects = [_object(bpy, object_name)] if object_name is not None else list(bpy.context.scene.objects)
    records = []
    for obj in objects:
        for modifier in obj.modifiers:
            if modifier.type != "NODES" or (modifier_name is not None and modifier.name != modifier_name):
                continue
            if include_graph and modifier.node_group is not None:
                if len(modifier.node_group.nodes) > max_nodes:
                    raise InvalidStateError(f"Node group {modifier.node_group.name} exceeds max_nodes {max_nodes}")
                if len(modifier.node_group.links) > max_links:
                    raise InvalidStateError(f"Node group {modifier.node_group.name} exceeds max_links {max_links}")
            if modifier.node_group is not None and len(_interface_sockets(modifier.node_group)) > MAX_INTERFACE_SOCKETS:
                raise InvalidStateError(
                    f"Node group {modifier.node_group.name} exceeds {MAX_INTERFACE_SOCKETS} interface sockets"
                )
            records.append(_modifier_summary(obj, modifier, include_graph, limit, offset))
    records.sort(key=lambda item: (item["object"], item["modifier"]))
    if len(records) > MAX_PAGE:
        raise InvalidStateError(
            f"Inspection matched more than {MAX_PAGE} Geometry Nodes modifiers; filter by object_name"
        )
    return ToolResult(
        data={
            "count": len(records),
            "modifiers": records,
            "state_token": current_state_token(bpy),
        }
    )


def _new_interface_socket(group: Any, name: str, direction: str, socket_type: str) -> Any:
    interface = getattr(group, "interface", None)
    if interface is not None and hasattr(interface, "new_socket"):
        return interface.new_socket(name=name, in_out=direction, socket_type=socket_type)
    sockets = group.inputs if direction == "INPUT" else group.outputs
    return sockets.new(socket_type, name)


def _remove_interface_socket(group: Any, socket: Any) -> None:
    interface = getattr(group, "interface", None)
    if interface is not None and hasattr(interface, "remove"):
        interface.remove(socket)
        return
    sockets = group.inputs if _interface_direction(socket, group) == "INPUT" else group.outputs
    sockets.remove(socket)


def _interface_lookup(group: Any, selector: Any, direction: str | None = None) -> Any:
    if not isinstance(selector, str) or not selector:
        raise InvalidParamsError("interface socket selector must be a non-empty identifier or name")
    sockets = [
        socket for socket in _interface_sockets(group)
        if direction is None or _interface_direction(socket, group) == direction
    ]
    identifier_matches = [socket for socket in sockets if str(getattr(socket, "identifier", "")) == selector]
    if len(identifier_matches) == 1:
        return identifier_matches[0]
    name_matches = [socket for socket in sockets if socket.name == selector]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        raise InvalidParamsError(f"Interface socket name is ambiguous; use its identifier: {selector}")
    raise NotFoundError(f"Interface socket not found: {selector}")


def _node_lookup(nodes: Any, name: Any) -> Any:
    name = _name(name, "node name")
    node = nodes.get(name)
    if node is None:
        raise NotFoundError(f"Node not found: {name}")
    return node


def _socket_lookup(sockets: Any, selector: Any, label: str) -> Any:
    if isinstance(selector, bool):
        raise InvalidParamsError(f"{label} must be a socket identifier, name, or index")
    if isinstance(selector, int):
        try:
            return sockets[selector]
        except (IndexError, KeyError):
            raise NotFoundError(f"{label} socket index not found: {selector}") from None
    if isinstance(selector, str) and selector:
        identifier_matches = [socket for socket in sockets if str(getattr(socket, "identifier", "")) == selector]
        if len(identifier_matches) == 1:
            return identifier_matches[0]
        name_matches = [socket for socket in sockets if socket.name == selector]
        if len(name_matches) == 1:
            return name_matches[0]
        if len(name_matches) > 1:
            raise InvalidParamsError(f"{label} socket name is ambiguous; use identifier or index: {selector}")
        raise NotFoundError(f"{label} socket not found: {selector}")
    raise InvalidParamsError(f"{label} must be a socket identifier, name, or index")


def _validate_socket_selector(selector: Any, label: str) -> None:
    if isinstance(selector, bool) or not isinstance(selector, (str, int)):
        raise InvalidParamsError(f"{label} must be a socket identifier, name, or index")
    if isinstance(selector, str) and not selector:
        raise InvalidParamsError(f"{label} must not be empty")


def _operations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_OPERATIONS:
        raise InvalidParamsError(f"operations must be a non-empty list of at most {MAX_OPERATIONS} objects")
    result = []
    for index, operation in enumerate(value):
        if not isinstance(operation, dict):
            raise InvalidParamsError(f"operations[{index}] must be an object")
        kind = operation.get("op")
        if kind not in OPERATION_KINDS:
            raise InvalidParamsError(f"operations[{index}].op is not allowlisted")
        result.append(dict(operation))
    return result


def _validate_reference(bpy: Any, value: Any, label: str) -> None:
    normalized = _validate_json_value(value, label)
    if not isinstance(normalized, dict):
        return
    collections = {
        "COLLECTION": bpy.data.collections,
        "MATERIAL": bpy.data.materials,
        "OBJECT": bpy.data.objects,
        "NODE_GROUP": bpy.data.node_groups,
        "IMAGE": bpy.data.images,
        "TEXTURE": bpy.data.textures,
    }
    collection = collections.get(normalized["kind"])
    if collection is None:
        raise InvalidParamsError(f"{label}.kind must name a supported Blender datablock")
    if collection.get(normalized["name"]) is None:
        raise NotFoundError(f"{normalized['kind'].title()} datablock not found: {normalized['name']}")


def _validate_typed_value(socket_type: str, value: Any, label: str) -> None:
    if "Geometry" in socket_type:
        raise InvalidParamsError(f"{label} cannot assign a Geometry socket")
    pointer_kind = next(
        (kind for kind in ("Object", "Collection", "Material") if kind in socket_type),
        None,
    )
    if pointer_kind is not None:
        if value is None:
            return
        if not isinstance(value, dict) or str(value.get("kind", "")).upper() != pointer_kind.upper():
            raise InvalidParamsError(f"{label} requires a {pointer_kind.upper()} datablock reference")
        return
    if value is None:
        raise InvalidParamsError(f"{label} cannot be null for {socket_type}")
    if "Bool" in socket_type:
        if not isinstance(value, bool):
            raise InvalidParamsError(f"{label} must be a boolean")
        return
    if "Int" in socket_type:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidParamsError(f"{label} must be an integer")
        return
    if "Float" in socket_type:
        _number(value, label, -1_000_000_000.0, 1_000_000_000.0)
        return
    if "Vector" in socket_type or "Color" in socket_type:
        expected_lengths = {3} if "Vector" in socket_type else {4}
        if not isinstance(value, (list, tuple)) or len(value) not in expected_lengths:
            raise InvalidParamsError(f"{label} must be a bounded numeric vector")
        [_number(item, label, -1_000_000_000.0, 1_000_000_000.0) for item in value]
        return
    if "String" in socket_type and not isinstance(value, str):
        raise InvalidParamsError(f"{label} must be a string")


def _validate_node_type(bpy: Any, node_type: Any) -> None:
    _name(node_type, "node_type")
    if getattr(bpy.types, node_type, None) is None:
        raise InvalidParamsError(f"Node type is unavailable in this Blender build: {node_type}")
    probe = bpy.data.node_groups.new("Codex node type probe", "GeometryNodeTree")
    try:
        probe.nodes.new(node_type)
    except Exception as exc:
        raise InvalidParamsError(f"Node type cannot be added to a Geometry Nodes tree: {node_type}: {exc}") from exc
    finally:
        bpy.data.node_groups.remove(probe)


def _validate_rna_property(bpy: Any, node: Any, name: str, value: Any) -> None:
    prop = node.bl_rna.properties.get(name)
    if prop is None or prop.is_readonly or name == "rna_type":
        raise InvalidParamsError(f"Node property is not writable RNA: {name}")
    value_type = str(prop.type)
    if value_type == "POINTER":
        if value is None:
            return
        normalized = _validate_json_value(value, name)
        if not isinstance(normalized, dict):
            raise InvalidParamsError(f"{name} requires a datablock reference")
        pointer_type = prop.fixed_type.identifier
        expected = {
            "Object": "OBJECT", "Collection": "COLLECTION", "Material": "MATERIAL",
            "NodeTree": "NODE_GROUP", "GeometryNodeTree": "NODE_GROUP",
            "ShaderNodeTree": "NODE_GROUP", "Image": "IMAGE", "Texture": "TEXTURE",
        }.get(pointer_type)
        if expected is None or normalized["kind"] != expected:
            raise InvalidParamsError(f"{name} requires a {pointer_type} datablock reference")
        _validate_reference(bpy, normalized, name)
        return
    if value_type == "COLLECTION":
        raise InvalidParamsError(f"Collection node property {name} requires python.exec")
    normalized = _validate_json_value(value, name)
    if isinstance(normalized, dict):
        raise InvalidParamsError(f"{name} does not accept a datablock reference")
    length = int(getattr(prop, "array_length", 0))
    values = normalized if length else [normalized]
    if length and (not isinstance(normalized, list) or len(normalized) != length):
        raise InvalidParamsError(f"{name} requires an array of length {length}")
    if value_type == "BOOLEAN":
        if any(not isinstance(item, bool) for item in values):
            raise InvalidParamsError(f"{name} requires boolean values")
    elif value_type == "INT":
        if any(isinstance(item, bool) or not isinstance(item, int) or not prop.hard_min <= item <= prop.hard_max for item in values):
            raise InvalidParamsError(f"{name} requires integers within Blender RNA range")
    elif value_type == "FLOAT":
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not prop.hard_min <= item <= prop.hard_max for item in values):
            raise InvalidParamsError(f"{name} requires numbers within Blender RNA range")
    elif value_type == "ENUM":
        valid = {item.identifier for item in prop.enum_items}
        if length or normalized not in valid:
            raise InvalidParamsError(f"{name} must be one of: {', '.join(sorted(valid))}")
    elif value_type == "STRING":
        if length or not isinstance(normalized, str):
            raise InvalidParamsError(f"{name} requires a string")
    else:
        raise InvalidParamsError(f"Unsupported RNA property type for {name}: {value_type}")


def _validate_operation(bpy: Any, group: Any, operation: dict[str, Any], planned_nodes: set[str], removed_nodes: set[str]) -> None:
    kind = operation["op"]
    allowed = {
        "add_node": {"op", "node_type", "name", "location"},
        "remove_node": {"op", "name"},
        "set_input": {"op", "node", "input", "value"},
        "set_node_property": {"op", "node", "property", "value"},
        "connect": {"op", "from_node", "from_output", "to_node", "to_input", "replace"},
        "disconnect": {"op", "to_node", "to_input"},
        "add_socket": {"op", "name", "in_out", "socket_type", "default_value"},
        "remove_socket": {"op", "socket"},
        "set_socket_default": {"op", "socket", "value"},
        "pair_zone": {"op", "input_node", "output_node"},
    }[kind]
    _unknown(operation, allowed, f"{kind} fields")
    nodes = group.nodes
    if kind == "pair_zone":
        input_name = _name(operation.get("input_node"), "pair_zone.input_node")
        output_name = _name(operation.get("output_node"), "pair_zone.output_node")
        if input_name in removed_nodes or output_name in removed_nodes:
            raise InvalidParamsError("pair_zone cannot target removed nodes")
        if input_name not in planned_nodes:
            _node_lookup(nodes, input_name)
        if output_name not in planned_nodes:
            _node_lookup(nodes, output_name)
        return
    if kind == "add_node":
        node_type = operation.get("node_type")
        _validate_node_type(bpy, node_type)
        name = _name(operation.get("name"), "add_node.name")
        existing = nodes.get(name)
        if existing is not None and existing.bl_idname != node_type:
            raise InvalidParamsError(f"Node {name} already exists with type {existing.bl_idname}")
        if name in removed_nodes:
            raise InvalidParamsError(f"Node cannot be removed and re-added in one patch: {name}")
        location = operation.get("location")
        if location is not None:
            if not isinstance(location, (list, tuple)) or len(location) != 2:
                raise InvalidParamsError("add_node.location must be a two-number array")
            [_number(item, "add_node.location", -1_000_000.0, 1_000_000.0) for item in location]
        if existing is None:
            planned_nodes.add(name)
        return
    if kind == "remove_node":
        name = _name(operation.get("name"), "remove_node.name")
        if name in removed_nodes:
            raise InvalidParamsError(f"Node is removed more than once in one patch: {name}")
        if name in planned_nodes:
            raise InvalidParamsError("A node added in this patch cannot also be removed")
        _node_lookup(nodes, name)
        removed_nodes.add(name)
        return
    if kind in {"set_input", "set_node_property"}:
        node_name = _name(operation.get("node"), f"{kind}.node")
        if node_name in removed_nodes:
            raise InvalidParamsError(f"Operation targets a node removed earlier in the patch: {node_name}")
        if kind == "set_node_property":
            prop = _name(operation.get("property"), "property")
        if node_name not in planned_nodes:
            node = _node_lookup(nodes, node_name)
            if kind == "set_input":
                socket = _socket_lookup(node.inputs, operation.get("input"), "input")
                if socket.is_linked or not hasattr(socket, "default_value"):
                    raise InvalidParamsError("set_input requires an unlinked input socket with a default value")
                _validate_typed_value(str(getattr(socket, "bl_idname", "")), operation.get("value"), "value")
            else:
                _validate_rna_property(bpy, node, prop, operation.get("value"))
        elif kind == "set_input":
            _validate_socket_selector(operation.get("input"), "input")
        _validate_reference(bpy, operation.get("value"), "value")
        return
    if kind in {"connect", "disconnect"}:
        target_name = _name(operation.get("to_node"), f"{kind}.to_node")
        if target_name in removed_nodes:
            raise InvalidParamsError(f"Operation targets a removed node: {target_name}")
        if target_name not in planned_nodes:
            _socket_lookup(_node_lookup(nodes, target_name).inputs, operation.get("to_input"), "to_input")
        else:
            _validate_socket_selector(operation.get("to_input"), "to_input")
        if kind == "connect":
            source_name = _name(operation.get("from_node"), "connect.from_node")
            if source_name in removed_nodes:
                raise InvalidParamsError(f"Operation sources a removed node: {source_name}")
            if source_name not in planned_nodes:
                _socket_lookup(_node_lookup(nodes, source_name).outputs, operation.get("from_output"), "from_output")
            else:
                _validate_socket_selector(operation.get("from_output"), "from_output")
            _strict_bool(operation.get("replace"), "replace", True)
        return
    if kind == "add_socket":
        socket_name = _name(operation.get("name"), "add_socket.name")
        direction = _name(operation.get("in_out"), "add_socket.in_out").upper()
        if direction not in {"INPUT", "OUTPUT"}:
            raise InvalidParamsError("add_socket.in_out must be INPUT or OUTPUT")
        socket_type = _name(operation.get("socket_type"), "add_socket.socket_type")
        if socket_type not in ALLOWED_SOCKET_TYPES:
            raise InvalidParamsError("socket_type is not allowlisted")
        matches = [
            socket for socket in _interface_sockets(group)
            if socket.name == socket_name
            and _interface_direction(socket, group) == direction
            and _interface_socket_type(socket) == socket_type
        ]
        if len(matches) > 1:
            raise InvalidStateError(
                f"Multiple matching interface sockets already exist: {direction} {socket_name}"
            )
        if "default_value" in operation:
            if socket_type == "NodeSocketGeometry":
                raise InvalidParamsError("Geometry interface sockets do not accept default_value")
            _validate_typed_value(socket_type, operation["default_value"], "default_value")
            _validate_reference(bpy, operation["default_value"], "default_value")
        return
    socket = _interface_lookup(group, operation.get("socket"))
    if kind == "set_socket_default":
        if not hasattr(socket, "default_value") or _interface_socket_type(socket) == "NodeSocketGeometry":
            raise InvalidParamsError("Interface socket does not expose an editable default value")
        _validate_typed_value(_interface_socket_type(socket), operation.get("value"), "value")
        _validate_reference(bpy, operation.get("value"), "value")


def _resolve_value(bpy: Any, value: Any) -> Any:
    normalized = _validate_json_value(value, "value")
    if not isinstance(normalized, dict):
        return normalized
    collection = {
        "COLLECTION": bpy.data.collections,
        "MATERIAL": bpy.data.materials,
        "OBJECT": bpy.data.objects,
        "NODE_GROUP": bpy.data.node_groups,
        "IMAGE": bpy.data.images,
        "TEXTURE": bpy.data.textures,
    }[normalized["kind"]]
    return collection.get(normalized["name"])


def _apply_operation(bpy: Any, group: Any, operation: dict[str, Any]) -> str:
    nodes = group.nodes
    links = group.links
    kind = operation["op"]
    if kind == "pair_zone":
        input_node = _node_lookup(nodes, operation["input_node"])
        output_node = _node_lookup(nodes, operation["output_node"])
        pairs = {
            "GeometryNodeSimulationInput": "GeometryNodeSimulationOutput",
            "GeometryNodeRepeatInput": "GeometryNodeRepeatOutput",
            "GeometryNodeForeachGeometryElementInput": "GeometryNodeForeachGeometryElementOutput",
        }
        if pairs.get(input_node.bl_idname) != output_node.bl_idname:
            raise InvalidParamsError("pair_zone requires a matching Simulation, Repeat, or For-Each input/output pair")
        if getattr(input_node, "paired_output", None) is output_node:
            return "present"
        input_node.pair_with_output(output_node)
        return "applied"
    if kind == "add_node":
        node = nodes.get(operation["name"])
        if node is not None:
            if "location" in operation:
                requested = [float(value) for value in operation["location"]]
                current = [float(node.location[0]), float(node.location[1])]
                if current != requested:
                    node.location = requested
                    return "applied"
            return "present"
        node = nodes.new(operation["node_type"])
        node.name = operation["name"]
        node.label = operation["name"]
        if "location" in operation:
            node.location = operation["location"]
        return "applied"
    if kind == "remove_node":
        nodes.remove(_node_lookup(nodes, operation["name"]))
        return "applied"
    if kind == "set_input":
        socket = _socket_lookup(_node_lookup(nodes, operation["node"]).inputs, operation["input"], "input")
        value = _resolve_value(bpy, operation.get("value"))
        if _json_safe(socket.default_value) == _json_safe(value):
            return "present"
        socket.default_value = value
        return "applied"
    if kind == "set_node_property":
        node = _node_lookup(nodes, operation["node"])
        prop = operation["property"]
        value = _resolve_value(bpy, operation.get("value"))
        if _json_safe(getattr(node, prop)) == _json_safe(value):
            return "present"
        setattr(node, prop, value)
        return "applied"
    if kind == "connect":
        output = _socket_lookup(_node_lookup(nodes, operation["from_node"]).outputs, operation["from_output"], "from_output")
        input_socket = _socket_lookup(_node_lookup(nodes, operation["to_node"]).inputs, operation["to_input"], "to_input")
        matching = [link for link in input_socket.links if link.from_socket == output]
        if matching and (not operation.get("replace", True) or len(input_socket.links) == 1):
            return "present"
        if operation.get("replace", True):
            for link in list(input_socket.links):
                links.remove(link)
        links.new(output, input_socket)
        return "applied"
    if kind == "disconnect":
        input_socket = _socket_lookup(_node_lookup(nodes, operation["to_node"]).inputs, operation["to_input"], "to_input")
        if not input_socket.links:
            return "absent"
        for link in list(input_socket.links):
            links.remove(link)
        return "applied"
    if kind == "add_socket":
        matches = [
            existing for existing in _interface_sockets(group)
            if existing.name == operation["name"]
            and _interface_direction(existing, group) == operation["in_out"].upper()
            and _interface_socket_type(existing) == operation["socket_type"]
        ]
        if matches:
            socket = matches[0]
            if "default_value" not in operation or not hasattr(socket, "default_value"):
                return "present"
            value = _resolve_value(bpy, operation["default_value"])
            if _json_safe(socket.default_value) == _json_safe(value):
                return "present"
            socket.default_value = value
            return "applied"
        socket = _new_interface_socket(group, operation["name"], operation["in_out"].upper(), operation["socket_type"])
        if "default_value" in operation and hasattr(socket, "default_value"):
            socket.default_value = _resolve_value(bpy, operation["default_value"])
        return "applied"
    socket = _interface_lookup(group, operation["socket"])
    if kind == "remove_socket":
        _remove_interface_socket(group, socket)
        return "applied"
    value = _resolve_value(bpy, operation.get("value"))
    if _json_safe(socket.default_value) == _json_safe(value):
        return "present"
    socket.default_value = value
    return "applied"


def _journal_entry(operation: dict[str, Any]) -> dict[str, Any]:
    target = (
        operation.get("name") or operation.get("node") or operation.get("to_node")
        or operation.get("socket") or operation["op"]
    )
    return {"op": operation["op"], "target": target, "request": _json_safe(operation), "status": "planned"}


def _validate_graph_budget(group: Any) -> None:
    if len(group.nodes) > MAX_NODES:
        raise InvalidStateError(f"Resulting node group exceeds {MAX_NODES} nodes")
    if len(group.links) > MAX_LINKS:
        raise InvalidStateError(f"Resulting node group exceeds {MAX_LINKS} links")
    if len(_interface_sockets(group)) > MAX_INTERFACE_SOCKETS:
        raise InvalidStateError(f"Resulting node group exceeds {MAX_INTERFACE_SOCKETS} interface sockets")


def create_geometry_nodes(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "modifier_name", "node_group_name", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    requested_modifier = _optional_name(params.get("modifier_name"), "modifier_name") or "GeometryNodes"
    requested_group = _optional_name(params.get("node_group_name"), "node_group_name") or f"{object_name} Geometry"
    bpy = _prepare_mutation(params)
    obj = _object(bpy, object_name)
    if obj.modifiers.get(requested_modifier) is not None:
        raise InvalidStateError(f"Modifier already exists: {obj.name}.{requested_modifier}")
    group = None
    modifier = None
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "GeometryNodeTree", "name": requested_group}])
    try:
        group = bpy.data.node_groups.new(requested_group, "GeometryNodeTree")
        _new_interface_socket(group, "Geometry", "INPUT", "NodeSocketGeometry")
        _new_interface_socket(group, "Geometry", "OUTPUT", "NodeSocketGeometry")
        input_node = group.nodes.new("NodeGroupInput")
        input_node.name = "Group Input"
        input_node.location = (-200, 0)
        output_node = group.nodes.new("NodeGroupOutput")
        output_node.name = "Group Output"
        output_node.location = (200, 0)
        group.links.new(input_node.outputs.get("Geometry"), output_node.inputs.get("Geometry"))
        modifier = obj.modifiers.new(requested_modifier, "NODES")
        modifier.node_group = group
        detail = _modifier_summary(obj, modifier, True, 200, 0)
    except Exception as exc:
        cleanup_errors = []
        if modifier is not None and obj.modifiers.get(modifier.name) is modifier:
            try:
                obj.modifiers.remove(modifier)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove modifier: {cleanup_exc}")
        if group is not None and bpy.data.node_groups.get(group.name) is group and group.users == 0:
            try:
                bpy.data.node_groups.remove(group)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove node group: {cleanup_exc}")
        transaction.update(changed=bool(cleanup_errors), warnings=cleanup_errors)
        if cleanup_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"geometry_nodes.create failed: {exc}", transaction) from exc
    transaction["affected_datablocks"][1]["name"] = group.name
    transaction.update(
        object=obj.name,
        modifier=modifier.name,
        node_group=group.name,
        detail=detail,
        changed=True,
        journal=[{"op": "geometry_nodes.create", "target": modifier.name, "before": None, "after": detail, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def patch_geometry_nodes(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(
        params,
        {"object_name", "modifier_name", "operations", "expected_group", "expected_node_count", "expected_link_count", "make_single_user", "dry_run", "confirm", "expected_state"},
    )
    object_name = _name(params.get("object_name"), "object_name")
    modifier_name = _optional_name(params.get("modifier_name"), "modifier_name")
    operations = _operations(params.get("operations"))
    expected_group = _optional_name(params.get("expected_group"), "expected_group")
    expected_nodes = None if "expected_node_count" not in params else _integer(params["expected_node_count"], "expected_node_count", 0, MAX_NODES)
    expected_links = None if "expected_link_count" not in params else _integer(params["expected_link_count"], "expected_link_count", 0, MAX_LINKS)
    make_single_user = _strict_bool(params.get("make_single_user"), "make_single_user", False)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _object(bpy, object_name)
    modifier = _modifier(obj, modifier_name)
    group = _node_group(modifier)
    if expected_group is not None and group.name != expected_group:
        raise InvalidStateError(f"expected_group {expected_group} does not match {group.name}")
    if expected_nodes is not None and len(group.nodes) != expected_nodes:
        raise InvalidStateError(f"expected_node_count {expected_nodes} does not match {len(group.nodes)}")
    if expected_links is not None and len(group.links) != expected_links:
        raise InvalidStateError(f"expected_link_count {expected_links} does not match {len(group.links)}")
    if group.users > 1 and not make_single_user:
        raise InvalidStateError("Node group is shared; set make_single_user true to patch only this modifier")
    _validate_graph_budget(group)
    planned_nodes: set[str] = set()
    removed_nodes: set[str] = set()
    removed_interface_sockets: set[str] = set()
    for operation in operations:
        if operation["op"] in {"remove_socket", "set_socket_default"}:
            socket = _interface_lookup(group, operation.get("socket"))
            marker = str(getattr(socket, "identifier", "")) or f"{_interface_direction(socket, group)}:{socket.name}"
            if marker in removed_interface_sockets:
                raise InvalidParamsError("Operation targets an interface socket removed earlier in the patch")
            if operation["op"] == "remove_socket":
                removed_interface_sockets.add(marker)
        _validate_operation(bpy, group, operation, planned_nodes, removed_nodes)
    destructive = any(operation["op"] in DESTRUCTIVE_OPERATIONS for operation in operations)
    journal = [_journal_entry(operation) for operation in operations]
    transaction = new_transaction(
        [{"type": "Object", "name": obj.name}, {"type": "GeometryNodeTree", "name": group.name}]
    )
    transaction.update(
        action="geometry_nodes.patch",
        object=obj.name,
        modifier=modifier.name,
        node_group=group.name,
        node_group_users=int(group.users),
        make_single_user=make_single_user,
        dry_run=dry_run,
        confirmation_required=destructive,
        journal=journal,
        before={"nodes": len(group.nodes), "links": len(group.links), "interface_sockets": len(_interface_sockets(group))},
        state_token=current_state_token(bpy),
    )
    if dry_run:
        return transaction
    if destructive and params.get("confirm") is not True:
        raise ConfirmationRequiredError(
            "geometry_nodes.patch requires confirm: true for node, socket, or link removal",
            transaction,
        )
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("geometry_nodes.patch requires Blender Object mode")
    working = None
    swapped = False
    original_name = group.name
    try:
        working = group.copy()
        working.name = f"{group.name} Working"
        for entry, operation in zip(journal, operations):
            entry["status"] = _apply_operation(bpy, working, operation)
        _validate_graph_budget(working)
        if not any(entry["status"] == "applied" for entry in journal):
            bpy.data.node_groups.remove(working)
            working = None
            transaction.update(after=transaction["before"], changed=False)
            transaction["state_token"] = current_state_token(bpy)
            return transaction
        modifier.node_group = working
        swapped = modifier.node_group is working
        if not swapped:
            raise RuntimeError("Blender did not assign the patched node group")
        obj.update_tag()
        transaction.update(
            node_group=working.name,
            after={"nodes": len(working.nodes), "links": len(working.links), "interface_sockets": len(_interface_sockets(working))},
            changed=True,
        )
        transaction["affected_datablocks"][1]["name"] = working.name
    except Exception as exc:
        cleanup_errors = []
        if swapped:
            try:
                modifier.node_group = group
                swapped = modifier.node_group is working
            except Exception as rollback_exc:
                cleanup_errors.append(f"restore original node group: {rollback_exc}")
        if working is not None and not swapped and working.users == 0 and bpy.data.node_groups.get(working.name) is working:
            try:
                bpy.data.node_groups.remove(working)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove working node group: {cleanup_exc}")
        transaction.update(changed=swapped or bool(cleanup_errors), warnings=cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"geometry_nodes.patch failed: {exc}", transaction) from exc
    if group.users == 0 and bpy.data.node_groups.get(group.name) is group:
        try:
            bpy.data.node_groups.remove(group)
            working.name = original_name
            transaction["node_group"] = working.name
            transaction["affected_datablocks"][1]["name"] = working.name
        except Exception as cleanup_exc:
            transaction["warnings"].append(f"remove superseded node group: {cleanup_exc}")
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _modifier_input_socket(group: Any, identifier: str) -> Any:
    matches = [
        socket for socket in _interface_sockets(group)
        if _interface_direction(socket, group) == "INPUT"
        and str(getattr(socket, "identifier", "")) == identifier
    ]
    if len(matches) != 1:
        raise NotFoundError(f"Geometry Nodes input identifier not found: {identifier}")
    socket = matches[0]
    if _interface_socket_type(socket) == "NodeSocketGeometry":
        raise InvalidParamsError("Geometry interface inputs cannot be assigned as modifier values")
    return socket


def patch_geometry_nodes_inputs(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "modifier_name", "values", "expected_group", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    modifier_name = _optional_name(params.get("modifier_name"), "modifier_name")
    values = params.get("values")
    if not isinstance(values, dict) or not values or len(values) > MAX_INTERFACE_SOCKETS:
        raise InvalidParamsError("values must be a non-empty object with at most 1000 stable input identifiers")
    for identifier, value in values.items():
        _name(identifier, "input identifier")
        _validate_json_value(value, f"values.{identifier}")
    expected_group = _optional_name(params.get("expected_group"), "expected_group")
    bpy = _prepare_mutation(params)
    obj = _object(bpy, object_name)
    modifier = _modifier(obj, modifier_name)
    group = _node_group(modifier)
    if expected_group is not None and group.name != expected_group:
        raise InvalidStateError(f"expected_group {expected_group} does not match {group.name}")
    sockets = {identifier: _modifier_input_socket(group, identifier) for identifier in values}
    for identifier, value in values.items():
        _validate_typed_value(_interface_socket_type(sockets[identifier]), value, f"values.{identifier}")
    snapshots = {
        identifier: _modifier_input_snapshot(modifier, identifier, sockets[identifier])
        for identifier in values
    }
    before = {
        identifier: _modifier_input_json(snapshots[identifier])
        for identifier in values
    }
    resolved = {identifier: _resolve_value(bpy, value) for identifier, value in values.items()}
    changed = [
        identifier
        for identifier in values
        if _json_safe(resolved[identifier]) != before[identifier]
        or snapshots[identifier].get("type") not in {None, "VALUE"}
    ]
    journal = [
        {
            "op": "geometry_nodes.modifier_inputs",
            "target": identifier,
            "socket": sockets[identifier].name,
            "before": before[identifier],
            "after": _json_safe(resolved[identifier]),
            "status": "planned" if identifier in changed else "unchanged",
        }
        for identifier in values
    ]
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "GeometryNodeTree", "name": group.name}])
    applied = []
    try:
        for identifier in changed:
            applied.append(identifier)
            _assign_modifier_input(snapshots[identifier], resolved[identifier])
            next(entry for entry in journal if entry["target"] == identifier)["status"] = "applied"
        obj.update_tag()
    except Exception as exc:
        rollback_errors = []
        for identifier in reversed(applied):
            try:
                _restore_modifier_input(snapshots[identifier])
                next(entry for entry in journal if entry["target"] == identifier)["status"] = "rolled_back"
            except Exception as rollback_exc:
                rollback_errors.append(f"restore input {identifier}: {rollback_exc}")
        transaction.update(object=obj.name, modifier=modifier.name, node_group=group.name, changed=bool(rollback_errors), journal=journal, warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"geometry_nodes.modifier_inputs failed: {exc}", transaction) from exc
    transaction.update(
        object=obj.name,
        modifier=modifier.name,
        node_group=group.name,
        before=before,
        after={
            identifier: _modifier_input_json(
                _modifier_input_snapshot(modifier, identifier, sockets[identifier])
            )
            for identifier in values
        },
        changed=bool(changed),
        journal=journal,
    )
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
