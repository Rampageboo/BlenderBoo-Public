"""Bounded compositor inspection and rollback-backed node-graph mutation."""

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
MAX_OPERATIONS = 100
MAX_NODES = 10_000
MAX_LINKS = 50_000
MAX_FILE_SLOTS = 256

# Custom script and group nodes are deliberately excluded. The compositor API is
# intended to assemble common built-in pipelines, not expose arbitrary execution.
ALLOWED_NODE_TYPES = frozenset(
    {
        "CompositorNodeAlphaOver", "CompositorNodeBlur", "CompositorNodeBrightContrast",
        "CompositorNodeColorBalance", "CompositorNodeComposite", "NodeGroupOutput", "CompositorNodeCrop",
        "CompositorNodeCryptomatteV2", "CompositorNodeDenoise", "CompositorNodeDilateErode",
        "CompositorNodeFilter", "CompositorNodeGlare", "CompositorNodeHueSat",
        "CompositorNodeIDMask", "CompositorNodeInvert", "CompositorNodeLensdist",
        "CompositorNodeMapRange", "CompositorNodeMath", "CompositorNodeMixRGB",
        "CompositorNodeNormalize", "CompositorNodeOutputFile", "CompositorNodePremulKey",
        "CompositorNodeRGB", "CompositorNodeRGBToBW", "CompositorNodeRLayers",
        "CompositorNodeScale", "CompositorNodeSetAlpha", "CompositorNodeSplitViewer",
        "CompositorNodeSwitch", "CompositorNodeTransform", "CompositorNodeTranslate",
        "CompositorNodeValue", "CompositorNodeViewer", "CompositorNodeZcombine",
    }
)
ALLOWED_NODE_PROPERTIES = frozenset(
    {
        "operation", "blend_type", "use_alpha", "filter_type", "quality", "size",
        "threshold", "mix", "glare_type", "iterations", "streaks", "angle_offset",
        "use_crop_size", "relative", "wrap_axis", "space", "interpolation", "use_crop",
    }
)
ALLOWED_FILE_FORMATS = frozenset({"BMP", "IRIS", "JPEG", "JPEG2000", "OPEN_EXR", "PNG", "TARGA", "TARGA_RAW", "TIFF", "WEBP"})
ALLOWED_COLOR_MODES = frozenset({"BW", "RGB", "RGBA"})
ALLOWED_COLOR_DEPTHS = frozenset({"8", "10", "12", "16", "32"})
OPERATION_KINDS = frozenset(
    {
        "add_node", "remove_node", "set_input", "set_node_property", "connect",
        "disconnect", "set_file_output", "add_file_slot", "set_file_slot", "remove_file_slot",
    }
)
DESTRUCTIVE_OPERATIONS = frozenset({"remove_node", "disconnect", "remove_file_slot"})


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
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    try:
        return [_json_safe(item) for item in value]
    except TypeError:
        return str(value)


def _validated_value(value: Any, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidParamsError(f"{label} must be finite")
        return value
    if isinstance(value, (list, tuple)) and len(value) <= 32:
        return [_validated_value(item, label) for item in value]
    raise InvalidParamsError(f"{label} must be a JSON scalar or bounded array")


def _page(params: dict[str, Any]) -> tuple[int, int]:
    limit = _integer(params.get("limit", 200), "limit", 1, MAX_PAGE)
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError("cursor must be a decimal string returned by compositor.inspect")


def _scene(bpy: Any, name: str | None) -> Any:
    if name is None:
        return bpy.context.scene
    scene = bpy.data.scenes.get(name)
    if scene is None:
        raise NotFoundError(f"Scene not found: {name}")
    return scene


def _tree(scene: Any) -> Any:
    tree = _scene_tree(scene)
    if tree is None:
        raise InvalidStateError("Scene compositor nodes are disabled; call compositor.create first")
    return tree


def _scene_tree(scene: Any) -> Any:
    """Return the active compositor group on 5.x or the legacy tree on 4.x."""
    if hasattr(scene, "compositing_node_group"):
        return scene.compositing_node_group
    return getattr(scene, "node_tree", None) if bool(getattr(scene, "use_nodes", False)) else None


def _node_lookup(nodes: Any, name: Any) -> Any:
    node = nodes.get(_name(name, "node"))
    if node is None:
        raise NotFoundError(f"Compositor node not found: {name}")
    return node


def _socket_lookup(sockets: Any, selector: Any, label: str) -> Any:
    if isinstance(selector, bool):
        raise InvalidParamsError(f"{label} must be a socket name or index")
    if isinstance(selector, int):
        try:
            return sockets[selector]
        except (IndexError, KeyError):
            raise NotFoundError(f"{label} socket index not found: {selector}") from None
    if isinstance(selector, str) and selector:
        socket = sockets.get(selector)
        if socket is None:
            raise NotFoundError(f"{label} socket not found: {selector}")
        return socket
    raise InvalidParamsError(f"{label} must be a socket name or index")


def _socket_selector(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or (isinstance(value, str) and not value):
        raise InvalidParamsError(f"{label} must be a socket name or index")


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


def _format_summary(settings: Any) -> dict[str, Any]:
    return {
        name: _json_safe(getattr(settings, name))
        for name in ("file_format", "color_mode", "color_depth", "compression")
        if hasattr(settings, name)
    }


def _file_slots(node: Any) -> list[dict[str, Any]]:
    modern = hasattr(node, "file_output_items")
    slots = list(node.file_output_items if modern else getattr(node, "file_slots", ()))
    inputs = list(node.inputs)
    records = []
    for index, slot in enumerate(slots):
        socket = inputs[index] if index < len(inputs) else None
        records.append(
            {
                "index": index,
                "name": socket.name if socket is not None else str(getattr(slot, "name", "")),
                "path": str(getattr(slot, "name", "")) if modern else str(getattr(slot, "path", "")),
                "use_node_format": not bool(getattr(slot, "override_node_format", False)) if modern else bool(getattr(slot, "use_node_format", True)),
            }
        )
    return records


def _node_summary(node: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": node.name,
        "label": node.label,
        "type": node.bl_idname,
        "location": [float(node.location[0]), float(node.location[1])],
        "mute": bool(getattr(node, "mute", False)),
        "properties": {
            name: _json_safe(getattr(node, name))
            for name in ALLOWED_NODE_PROPERTIES
            if hasattr(node, name)
        },
        "inputs": [_socket_summary(socket) for socket in node.inputs],
        "outputs": [_socket_summary(socket) for socket in node.outputs],
    }
    if node.bl_idname == "CompositorNodeOutputFile":
        result["file_output"] = {
            "base_path": str(getattr(node, "directory", getattr(node, "base_path", ""))),
            "file_name": str(getattr(node, "file_name", "")),
            "format": _format_summary(node.format),
            "slots": _file_slots(node),
        }
    if node.bl_idname == "CompositorNodeRLayers":
        result["layer"] = str(getattr(node, "layer", ""))
        result["scene"] = getattr(getattr(node, "scene", None), "name", None)
    return result


def _link_summary(link: Any) -> dict[str, Any]:
    return {
        "from_node": link.from_node.name,
        "from_output": link.from_socket.name,
        "to_node": link.to_node.name,
        "to_input": link.to_socket.name,
    }


def _graph_detail(scene: Any, limit: int, offset: int) -> dict[str, Any]:
    tree = _scene_tree(scene)
    enabled = tree is not None
    if not enabled:
        return {
            "scene": scene.name, "enabled": False, "node_count": 0, "link_count": 0,
            "nodes": [], "links": [], "truncated": False, "next_cursor": None,
        }
    nodes = [_node_summary(node) for node in sorted(tree.nodes, key=lambda item: item.name)]
    links = sorted(
        (_link_summary(link) for link in tree.links),
        key=lambda item: (item["to_node"], item["to_input"], item["from_node"], item["from_output"]),
    )
    next_offset = offset + limit
    truncated = next_offset < len(nodes) or next_offset < len(links)
    return {
        "scene": scene.name,
        "enabled": True,
        "node_count": len(nodes),
        "link_count": len(links),
        "nodes": nodes[offset:next_offset],
        "links": links[offset:next_offset],
        "truncated": truncated,
        "next_cursor": str(next_offset) if truncated else None,
    }


def inspect_compositor(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"scene", "limit", "cursor"})
    scene_name = _optional_name(params.get("scene"), "scene")
    limit, offset = _page(params)
    bpy = get_bpy()
    scene = _scene(bpy, scene_name)
    data = _graph_detail(scene, limit, offset)
    data["state_token"] = current_state_token(bpy)
    return ToolResult(data=data)


def _validate_graph_budget(tree: Any) -> None:
    if len(tree.nodes) > MAX_NODES:
        raise InvalidStateError(f"Compositor graph exceeds {MAX_NODES} nodes")
    if len(tree.links) > MAX_LINKS:
        raise InvalidStateError(f"Compositor graph exceeds {MAX_LINKS} links")
    for node in tree.nodes:
        if node.bl_idname == "CompositorNodeOutputFile" and len(getattr(node, "file_output_items", getattr(node, "file_slots", ()))) > MAX_FILE_SLOTS:
            raise InvalidStateError(f"File Output node {node.name} exceeds {MAX_FILE_SLOTS} slots")


def _snapshot_node(node: Any) -> dict[str, Any]:
    inputs = []
    for index, socket in enumerate(node.inputs):
        if hasattr(socket, "default_value"):
            inputs.append({"index": index, "value": _json_safe(socket.default_value)})
    snapshot: dict[str, Any] = {
        "name": node.name,
        "type": node.bl_idname,
        "label": node.label,
        "location": [float(node.location[0]), float(node.location[1])],
        "width": float(node.width),
        "hide": bool(node.hide),
        "mute": bool(getattr(node, "mute", False)),
        "properties": {
            name: _json_safe(getattr(node, name))
            for name in ALLOWED_NODE_PROPERTIES
            if hasattr(node, name)
        },
        "inputs": inputs,
    }
    if node.bl_idname == "CompositorNodeOutputFile":
        snapshot["file_output"] = {
            "base_path": str(getattr(node, "directory", getattr(node, "base_path", ""))),
            "file_name": str(getattr(node, "file_name", "")),
            "format": _format_summary(node.format),
            "slots": _file_slots(node),
        }
    if node.bl_idname == "CompositorNodeRLayers":
        snapshot["layer"] = str(getattr(node, "layer", ""))
        snapshot["scene"] = getattr(getattr(node, "scene", None), "name", None)
    return snapshot


def _snapshot_graph(tree: Any) -> dict[str, Any]:
    return {
        "nodes": [_snapshot_node(node) for node in tree.nodes],
        "links": [_link_summary(link) for link in tree.links],
    }


def _restore_file_slots(node: Any, snapshot: dict[str, Any]) -> None:
    modern = hasattr(node, "file_output_items")
    slots = node.file_output_items if modern else getattr(node, "file_slots", None)
    if slots is None:
        return
    slots.clear()
    for record in snapshot.get("slots", []):
        slot = slots.new("RGBA", record["name"]) if modern else slots.new(record["name"])
        if not modern:
            slot = list(slots)[-1]
        if modern:
            slot.name = record["path"]
            slot.override_node_format = not record["use_node_format"]
        else:
            slot.path = record["path"]
            slot.use_node_format = record["use_node_format"]


def _restore_node(bpy: Any, tree: Any, snapshot: dict[str, Any], node: Any) -> None:
    node.label = snapshot["label"]
    node.location = snapshot["location"]
    node.width = snapshot["width"]
    node.hide = snapshot["hide"]
    if hasattr(node, "mute"):
        node.mute = snapshot["mute"]
    for name, value in snapshot["properties"].items():
        if hasattr(node, name):
            setattr(node, name, value)
    if node.bl_idname == "CompositorNodeRLayers":
        scene_name = snapshot.get("scene")
        if scene_name and hasattr(node, "scene"):
            node.scene = bpy.data.scenes.get(scene_name)
        if hasattr(node, "layer"):
            node.layer = snapshot.get("layer", "")
    file_output = snapshot.get("file_output")
    if isinstance(file_output, dict) and node.bl_idname == "CompositorNodeOutputFile":
        if hasattr(node, "directory"):
            node.directory = file_output["base_path"]
            node.file_name = file_output.get("file_name", "")
        else:
            node.base_path = file_output["base_path"]
        for name, value in file_output["format"].items():
            if hasattr(node.format, name):
                setattr(node.format, name, value)
        _restore_file_slots(node, file_output)
    for record in snapshot["inputs"]:
        index = record["index"]
        if index < len(node.inputs) and hasattr(node.inputs[index], "default_value"):
            node.inputs[index].default_value = record["value"]


def _restore_graph(bpy: Any, tree: Any, snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    original = {record["name"]: record for record in snapshot["nodes"]}
    try:
        for node in list(tree.nodes):
            if node.name not in original:
                tree.nodes.remove(node)
    except Exception as exc:
        errors.append(f"remove added nodes: {exc}")
    for name, record in original.items():
        try:
            node = tree.nodes.get(name)
            if node is None:
                node = tree.nodes.new(record["type"])
                node.name = name
            _restore_node(bpy, tree, record, node)
        except Exception as exc:
            errors.append(f"restore node {name}: {exc}")
    try:
        tree.links.clear()
        for record in snapshot["links"]:
            source = tree.nodes.get(record["from_node"])
            target = tree.nodes.get(record["to_node"])
            if source is None or target is None:
                raise RuntimeError(f"missing endpoint for {record}")
            tree.links.new(
                _socket_lookup(source.outputs, record["from_output"], "from_output"),
                _socket_lookup(target.inputs, record["to_input"], "to_input"),
            )
    except Exception as exc:
        errors.append(f"restore links: {exc}")
    return errors


def create_compositor(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"scene", "include_viewer", "dry_run", "expected_state"})
    scene_name = _optional_name(params.get("scene"), "scene")
    include_viewer = _strict_bool(params.get("include_viewer"), "include_viewer", False)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    scene = _scene(bpy, scene_name)
    previous_tree = _scene_tree(scene)
    enabled_before = previous_tree is not None
    existing_types = {node.bl_idname for node in previous_tree.nodes} if enabled_before else set()
    planned = []
    if not enabled_before:
        planned.append("enable_compositor")
    if "CompositorNodeRLayers" not in existing_types:
        planned.append("add_render_layers")
    output_type = "NodeGroupOutput" if hasattr(scene, "compositing_node_group") else "CompositorNodeComposite"
    if output_type not in existing_types:
        planned.append("add_composite")
    if include_viewer and "CompositorNodeViewer" not in existing_types:
        planned.append("add_viewer")
    transaction = new_transaction([{"type": "Scene", "name": scene.name}])
    transaction.update(
        action="compositor.create", scene=scene.name, dry_run=dry_run, planned=planned,
        before={"enabled": enabled_before, "node_count": len(previous_tree.nodes) if enabled_before else 0},
        changed=False, state_token=current_state_token(bpy),
    )
    if dry_run:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _snapshot_graph(previous_tree) if enabled_before else None
    try:
        if not enabled_before:
            if hasattr(scene, "compositing_node_group"):
                scene.compositing_node_group = bpy.data.node_groups.new(
                    f"{scene.name} Compositing", "CompositorNodeTree"
                )
            else:
                scene.use_nodes = True
        tree = _tree(scene)
        render_layers = next((node for node in tree.nodes if node.bl_idname == "CompositorNodeRLayers"), None)
        composite = next((node for node in tree.nodes if node.bl_idname == output_type), None)
        if render_layers is None:
            render_layers = tree.nodes.new("CompositorNodeRLayers")
            render_layers.name = "CBT Render Layers"
            render_layers.label = "CBT Render Layers"
            render_layers.location = (-300.0, 0.0)
        if output_type == "NodeGroupOutput" and not any(
            getattr(item, "in_out", None) == "OUTPUT" and item.name == "Image"
            for item in tree.interface.items_tree
        ):
            tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        if composite is None:
            composite = tree.nodes.new(output_type)
            composite.name = "CBT Composite"
            composite.label = "CBT Composite"
            composite.location = (300.0, 0.0)
        image_output = _socket_lookup(render_layers.outputs, "Image", "Render Layers Image")
        image_input = _socket_lookup(composite.inputs, "Image", "Composite Image")
        if not image_input.is_linked:
            tree.links.new(image_output, image_input)
        if include_viewer:
            viewer = next((node for node in tree.nodes if node.bl_idname == "CompositorNodeViewer"), None)
            if viewer is None:
                viewer = tree.nodes.new("CompositorNodeViewer")
                viewer.name = "CBT Viewer"
                viewer.label = "CBT Viewer"
                viewer.location = (300.0, -220.0)
            viewer_input = _socket_lookup(viewer.inputs, "Image", "Viewer Image")
            if not viewer_input.is_linked:
                tree.links.new(image_output, viewer_input)
        _validate_graph_budget(tree)
    except Exception as exc:
        errors: list[str] = []
        if snapshot is not None and _scene_tree(scene) is not None:
            errors.extend(_restore_graph(bpy, _scene_tree(scene), snapshot))
        elif not enabled_before:
            try:
                if hasattr(scene, "compositing_node_group"):
                    new_tree = scene.compositing_node_group
                    scene.compositing_node_group = None
                    if new_tree is not None and new_tree.users == 0:
                        bpy.data.node_groups.remove(new_tree)
                else:
                    scene.use_nodes = False
            except Exception as rollback_exc:
                errors.append(f"disable compositor: {rollback_exc}")
        transaction.update(changed=bool(errors), warnings=errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"compositor.create failed: {exc}", transaction) from exc
    tree = _tree(scene)
    changed = bool(planned) or len(tree.links) != (len(snapshot["links"]) if snapshot is not None else 0)
    transaction.update(
        changed=changed,
        after={"enabled": True, "node_count": len(tree.nodes), "link_count": len(tree.links)},
    )
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _operations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_OPERATIONS:
        raise InvalidParamsError(f"operations must be a non-empty list of at most {MAX_OPERATIONS} objects")
    result = []
    for index, operation in enumerate(value):
        if not isinstance(operation, dict):
            raise InvalidParamsError(f"operations[{index}] must be an object")
        if operation.get("op") not in OPERATION_KINDS:
            raise InvalidParamsError(f"operations[{index}].op is not allowlisted")
        result.append(dict(operation))
    return result


def _file_output_node(nodes: Any, value: Any) -> Any:
    node = _node_lookup(nodes, value)
    if node.bl_idname != "CompositorNodeOutputFile":
        raise InvalidParamsError(f"Node is not a File Output node: {node.name}")
    return node


def _slot_lookup(node: Any, selector: Any) -> tuple[Any, Any]:
    modern = hasattr(node, "file_output_items")
    slots = list(node.file_output_items if modern else node.file_slots)
    inputs = list(node.inputs)
    if isinstance(selector, bool):
        raise InvalidParamsError("slot must be a name, path, or index")
    if isinstance(selector, int):
        if not 0 <= selector < len(slots):
            raise NotFoundError(f"File Output slot index not found: {selector}")
        return slots[selector], inputs[selector]
    if isinstance(selector, str) and selector:
        matches = [
            index for index, slot in enumerate(slots)
            if str(getattr(slot, "name", "")) == selector or str(getattr(slot, "path", "")) == selector or (index < len(inputs) and inputs[index].name == selector)
        ]
        if len(matches) != 1:
            raise NotFoundError(f"File Output slot name or path is not unique: {selector}")
        index = matches[0]
        return slots[index], inputs[index]
    raise InvalidParamsError("slot must be a name, path, or index")


def _link_key(node_name: str, socket: Any | None, selector: Any) -> tuple[str, str]:
    marker = socket.name if socket is not None else f"{type(selector).__name__}:{selector}"
    return node_name, marker


def _validate_operation(
    bpy: Any,
    tree: Any,
    operation: dict[str, Any],
    planned: set[str],
    removed: set[str],
    link_plan: dict[tuple[str, str], str],
) -> bool:
    kind = operation["op"]
    allowed = {
        "add_node": {"op", "node_type", "name", "location"},
        "remove_node": {"op", "name"},
        "set_input": {"op", "node", "input", "value"},
        "set_node_property": {"op", "node", "property", "value"},
        "connect": {"op", "from_node", "from_output", "to_node", "to_input", "replace"},
        "disconnect": {"op", "to_node", "to_input"},
        "set_file_output": {"op", "node", "base_path", "file_name", "file_format", "color_mode", "color_depth", "compression"},
        "add_file_slot": {"op", "node", "name", "path"},
        "set_file_slot": {"op", "node", "slot", "path", "use_node_format"},
        "remove_file_slot": {"op", "node", "slot"},
    }[kind]
    _unknown(operation, allowed, f"{kind} fields")
    nodes = tree.nodes
    destructive = kind in DESTRUCTIVE_OPERATIONS
    if kind == "add_node":
        node_type = operation.get("node_type")
        if node_type not in ALLOWED_NODE_TYPES:
            raise InvalidParamsError("node_type is not in the compositor allowlist")
        if getattr(bpy.types, node_type, None) is None:
            raise InvalidStateError(f"Node type is unavailable in this Blender build: {node_type}")
        name = _name(operation.get("name"), "add_node.name")
        existing = nodes.get(name)
        if existing is not None and existing.bl_idname != node_type:
            raise InvalidParamsError(f"Node {name} already exists with type {existing.bl_idname}")
        if name in removed:
            raise InvalidParamsError(f"Node cannot be removed and re-added in one patch: {name}")
        location = operation.get("location")
        if location is not None:
            if not isinstance(location, (list, tuple)) or len(location) != 2:
                raise InvalidParamsError("add_node.location must be a two-number array")
            [_number(item, "add_node.location", -1_000_000.0, 1_000_000.0) for item in location]
        if existing is None:
            planned.add(name)
        return destructive
    if kind == "remove_node":
        name = _name(operation.get("name"), "remove_node.name")
        node = _node_lookup(nodes, name)
        if node.bl_idname not in ALLOWED_NODE_TYPES:
            raise InvalidParamsError("Only allowlisted built-in compositor nodes can be removed")
        if name in planned or name in removed:
            raise InvalidParamsError(f"Node cannot be added and removed, or removed twice: {name}")
        removed.add(name)
        return True
    node_name = _name(operation.get("node" if kind not in {"connect", "disconnect"} else "to_node"), f"{kind}.node")
    if node_name in removed:
        raise InvalidParamsError(f"Operation targets a node removed earlier in the patch: {node_name}")
    node = None if node_name in planned else _node_lookup(nodes, node_name)
    if kind == "set_input":
        _socket_selector(operation.get("input"), "input")
        value = _validated_value(operation.get("value"), "value")
        if node is not None:
            socket = _socket_lookup(node.inputs, operation["input"], "input")
            planned_link = link_plan.get(_link_key(node_name, socket, operation["input"]))
            if (socket.is_linked and planned_link != "disconnect") or planned_link == "connect" or not hasattr(socket, "default_value"):
                raise InvalidParamsError("set_input requires an unlinked input socket with a default value")
            _validate_socket_value(socket, value, "value")
        return destructive
    if kind == "set_node_property":
        prop = _name(operation.get("property"), "property")
        if prop not in ALLOWED_NODE_PROPERTIES:
            raise InvalidParamsError("Node property is not allowlisted")
        value = _validated_value(operation.get("value"), "value")
        if node is not None and not hasattr(node, prop):
            raise InvalidParamsError(f"Property {prop} is unavailable on node {node.name}")
        if node is not None:
            _validate_current_value(getattr(node, prop), value, "value")
        return destructive
    if kind in {"connect", "disconnect"}:
        _socket_selector(operation.get("to_input"), "to_input")
        target_socket = None if node is None else _socket_lookup(node.inputs, operation["to_input"], "to_input")
        target_key = _link_key(node_name, target_socket, operation["to_input"])
        prior_link_plan = link_plan.get(target_key)
        if kind == "connect":
            source_name = _name(operation.get("from_node"), "connect.from_node")
            if source_name in removed:
                raise InvalidParamsError(f"Operation sources a removed node: {source_name}")
            source = None if source_name in planned else _node_lookup(nodes, source_name)
            _socket_selector(operation.get("from_output"), "from_output")
            if source is not None:
                _socket_lookup(source.outputs, operation["from_output"], "from_output")
            replace = _strict_bool(operation.get("replace"), "replace", False)
            if prior_link_plan == "connect" and not replace:
                raise InvalidParamsError("A second connection to the same input requires replace: true")
            destructive = destructive or replace or prior_link_plan == "connect"
            if target_socket is not None and target_socket.is_linked:
                current = list(target_socket.links)
                source_socket = None if source is None else _socket_lookup(source.outputs, operation["from_output"], "from_output")
                same = len(current) == 1 and source_socket is not None and current[0].from_socket == source_socket
                if not same and not replace:
                    raise InvalidStateError("Target input is already linked; set replace true and confirm the patch")
                destructive = destructive or not same
            link_plan[target_key] = "connect"
        else:
            link_plan[target_key] = "disconnect"
        return destructive
    if node is not None and node.bl_idname != "CompositorNodeOutputFile":
        raise InvalidParamsError(f"Node is not a File Output node: {node.name}")
    if kind == "set_file_output":
        if not any(name in operation for name in ("base_path", "file_name", "file_format", "color_mode", "color_depth", "compression")):
            raise InvalidParamsError("set_file_output requires at least one setting")
        if "base_path" in operation:
            _name(operation["base_path"], "base_path")
        if "file_name" in operation:
            if node is not None and not hasattr(node, "file_name"):
                raise InvalidParamsError("file_name requires Blender 5.x File Output")
            _name(operation["file_name"], "file_name")
        if "file_format" in operation and operation["file_format"] not in ALLOWED_FILE_FORMATS:
            raise InvalidParamsError("file_format is not allowlisted")
        if "color_mode" in operation and operation["color_mode"] not in ALLOWED_COLOR_MODES:
            raise InvalidParamsError("color_mode must be BW, RGB, or RGBA")
        if "color_depth" in operation and operation["color_depth"] not in ALLOWED_COLOR_DEPTHS:
            raise InvalidParamsError("color_depth is not allowlisted")
        if "compression" in operation:
            _integer(operation["compression"], "compression", 0, 100)
    elif kind == "add_file_slot":
        _name(operation.get("name"), "add_file_slot.name")
        if "path" in operation:
            _name(operation["path"], "add_file_slot.path")
    elif kind == "set_file_slot":
        if "path" not in operation and "use_node_format" not in operation:
            raise InvalidParamsError("set_file_slot requires path or use_node_format")
        if "path" in operation:
            _name(operation["path"], "set_file_slot.path")
        if "use_node_format" in operation:
            _strict_bool(operation["use_node_format"], "use_node_format")
        if node is not None:
            _slot_lookup(node, operation.get("slot"))
    elif node is not None:
        _slot_lookup(node, operation.get("slot"))
        if len(node.file_output_items if hasattr(node, "file_output_items") else node.file_slots) <= 1:
            raise InvalidStateError("A File Output node must retain at least one slot")
    return destructive


def _validate_current_value(current: Any, value: Any, label: str) -> None:
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise InvalidParamsError(f"{label} must be a boolean")
    elif isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidParamsError(f"{label} must be an integer")
    elif isinstance(current, float):
        _number(value, label, -1_000_000_000.0, 1_000_000_000.0)
    elif isinstance(current, str):
        if not isinstance(value, str):
            raise InvalidParamsError(f"{label} must be a string")
    else:
        before = _json_safe(current)
        if isinstance(before, list):
            if not isinstance(value, list) or len(value) != len(before):
                raise InvalidParamsError(f"{label} must be a {len(before)}-item array")
            [_number(item, label, -1_000_000_000.0, 1_000_000_000.0) for item in value]


def _validate_socket_value(socket: Any, value: Any, label: str) -> None:
    _validate_current_value(getattr(socket, "default_value", None), value, label)


def _apply_operation(tree: Any, operation: dict[str, Any]) -> str:
    nodes, links, kind = tree.nodes, tree.links, operation["op"]
    if kind == "add_node":
        if nodes.get(operation["name"]) is not None:
            return "present"
        node = nodes.new(operation["node_type"])
        node.name = operation["name"]
        node.label = operation["name"]
        if "location" in operation:
            node.location = operation["location"]
    elif kind == "remove_node":
        nodes.remove(_node_lookup(nodes, operation["name"]))
    elif kind == "set_input":
        socket = _socket_lookup(_node_lookup(nodes, operation["node"]).inputs, operation["input"], "input")
        if _json_safe(socket.default_value) == _json_safe(operation["value"]):
            return "present"
        socket.default_value = operation["value"]
    elif kind == "set_node_property":
        node = _node_lookup(nodes, operation["node"])
        if _json_safe(getattr(node, operation["property"])) == _json_safe(operation["value"]):
            return "present"
        setattr(node, operation["property"], operation["value"])
    elif kind == "connect":
        output = _socket_lookup(_node_lookup(nodes, operation["from_node"]).outputs, operation["from_output"], "from_output")
        input_socket = _socket_lookup(_node_lookup(nodes, operation["to_node"]).inputs, operation["to_input"], "to_input")
        if len(input_socket.links) == 1 and input_socket.links[0].from_socket == output:
            return "present"
        if input_socket.is_linked and operation.get("replace") is not True:
            raise InvalidStateError("Target input became linked before apply")
        for link in list(input_socket.links):
            links.remove(link)
        links.new(output, input_socket)
    elif kind == "disconnect":
        input_socket = _socket_lookup(_node_lookup(nodes, operation["to_node"]).inputs, operation["to_input"], "to_input")
        if not input_socket.links:
            return "absent"
        for link in list(input_socket.links):
            links.remove(link)
    elif kind == "set_file_output":
        node = _file_output_node(nodes, operation["node"])
        before = _json_safe({
            "base_path": getattr(node, "directory", getattr(node, "base_path", "")),
            "file_name": getattr(node, "file_name", None),
            **_format_summary(node.format),
        })
        if "base_path" in operation:
            if hasattr(node, "directory"):
                node.directory = operation["base_path"]
            else:
                node.base_path = operation["base_path"]
        if "file_name" in operation:
            node.file_name = operation["file_name"]
        for name in ("file_format", "color_mode", "color_depth", "compression"):
            if name in operation:
                setattr(node.format, name, operation[name])
        after = _json_safe({"base_path": getattr(node, "directory", getattr(node, "base_path", "")), "file_name": getattr(node, "file_name", None), **_format_summary(node.format)})
        if before == after:
            return "present"
    elif kind == "add_file_slot":
        node = _file_output_node(nodes, operation["node"])
        modern = hasattr(node, "file_output_items")
        slots = node.file_output_items if modern else node.file_slots
        if len(slots) >= MAX_FILE_SLOTS:
            raise InvalidStateError(f"File Output node exceeds {MAX_FILE_SLOTS} slots")
        created = slots.new("RGBA", operation["name"]) if modern else slots.new(operation["name"])
        slot = created if modern else list(slots)[-1]
        if "path" in operation:
            if modern: slot.name = operation["path"]
            else: slot.path = operation["path"]
        if created is None:
            raise RuntimeError("Blender did not create the File Output slot")
    elif kind == "set_file_slot":
        node = _file_output_node(nodes, operation["node"])
        slot, _socket = _slot_lookup(node, operation["slot"])
        changed = False
        for name in ("path", "use_node_format"):
            if name in operation:
                attr = ("name" if name == "path" else "override_node_format") if hasattr(node, "file_output_items") else name
                value = not operation[name] if attr == "override_node_format" else operation[name]
                if _json_safe(getattr(slot, attr)) == _json_safe(value):
                    continue
                setattr(slot, attr, value)
                changed = True
        if not changed:
            return "present"
    else:
        node = _file_output_node(nodes, operation["node"])
        _slot, socket = _slot_lookup(node, operation["slot"])
        if hasattr(node, "file_output_items"):
            node.file_output_items.remove(_slot)
        else:
            node.file_slots.remove(socket)
    return "applied"


def patch_compositor(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(
        params,
        {"scene", "operations", "expected_node_count", "expected_link_count", "dry_run", "confirm", "expected_state"},
    )
    scene_name = _optional_name(params.get("scene"), "scene")
    operations = _operations(params.get("operations"))
    expected_nodes = None if "expected_node_count" not in params else _integer(params["expected_node_count"], "expected_node_count", 0, MAX_NODES)
    expected_links = None if "expected_link_count" not in params else _integer(params["expected_link_count"], "expected_link_count", 0, MAX_LINKS)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm")
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    scene = _scene(bpy, scene_name)
    tree = _tree(scene)
    _validate_graph_budget(tree)
    if expected_nodes is not None and len(tree.nodes) != expected_nodes:
        raise InvalidStateError(f"expected_node_count {expected_nodes} does not match {len(tree.nodes)}")
    if expected_links is not None and len(tree.links) != expected_links:
        raise InvalidStateError(f"expected_link_count {expected_links} does not match {len(tree.links)}")
    planned: set[str] = set()
    removed: set[str] = set()
    link_plan: dict[tuple[str, str], str] = {}
    destructive = False
    for operation in operations:
        destructive = _validate_operation(bpy, tree, operation, planned, removed, link_plan) or destructive
    journal = [
        {"op": operation["op"], "request": _json_safe(operation), "status": "planned"}
        for operation in operations
    ]
    transaction = new_transaction([{"type": "Scene", "name": scene.name}])
    transaction.update(
        action="compositor.patch", scene=scene.name, dry_run=dry_run,
        confirmation_required=destructive, journal=journal,
        before={"nodes": len(tree.nodes), "links": len(tree.links)},
        changed=False, state_token=current_state_token(bpy),
    )
    if dry_run:
        return transaction
    if destructive and params.get("confirm") is not True:
        raise ConfirmationRequiredError(
            "compositor.patch requires confirm: true when removing or replacing graph structure",
            transaction,
        )
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _snapshot_graph(tree)
    try:
        for entry, operation in zip(journal, operations):
            entry["status"] = _apply_operation(tree, operation)
        _validate_graph_budget(tree)
    except Exception as exc:
        rollback_errors = _restore_graph(bpy, tree, snapshot)
        for entry in journal:
            if entry["status"] == "applied":
                entry["status"] = "rolled_back" if not rollback_errors else "rollback_failed"
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"compositor.patch failed: {exc}", transaction) from exc
    changed = any(entry["status"] == "applied" for entry in journal)
    transaction.update(changed=changed, after={"nodes": len(tree.nodes), "links": len(tree.links)})
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
