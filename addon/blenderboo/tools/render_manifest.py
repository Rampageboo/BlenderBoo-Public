"""Structured render-output manifests and non-mutating preflight validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import InvalidParamsError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .compositor import _scene_tree
from .session_state import current_state_token


PASS_ATTRIBUTES = (
    "use_pass_combined", "use_pass_z", "use_pass_mist", "use_pass_position",
    "use_pass_normal", "use_pass_vector", "use_pass_uv", "use_pass_object_index",
    "use_pass_material_index", "use_pass_emit", "use_pass_environment",
    "use_pass_ambient_occlusion", "use_pass_shadow", "use_pass_diffuse_direct",
    "use_pass_diffuse_indirect", "use_pass_diffuse_color", "use_pass_glossy_direct",
    "use_pass_glossy_indirect", "use_pass_glossy_color", "use_pass_transmission_direct",
    "use_pass_transmission_indirect", "use_pass_transmission_color",
    "use_pass_subsurface_direct", "use_pass_subsurface_indirect", "use_pass_subsurface_color",
    "use_pass_cryptomatte_object", "use_pass_cryptomatte_material", "use_pass_cryptomatte_asset",
)


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(names)}")


def _strict_bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _optional_name(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _scene(bpy: Any, name: str | None) -> Any:
    if name is None:
        return bpy.context.scene
    scene = bpy.data.scenes.get(name)
    if scene is None:
        raise NotFoundError(f"Scene not found: {name}")
    return scene


def _resolved_path(bpy: Any, raw: str) -> str:
    if not raw:
        return ""
    try:
        return str(Path(bpy.path.abspath(raw)).expanduser().resolve())
    except Exception:
        return str(Path(raw).expanduser().absolute())


def _joined_output_pattern(base_path: str, fragment: str) -> str:
    if not base_path:
        return fragment
    return base_path.rstrip("/\\") + "/" + fragment.lstrip("/\\")


def _format(settings: Any) -> dict[str, Any]:
    return {
        name: getattr(settings, name)
        for name in ("file_format", "color_mode", "color_depth", "compression", "quality")
        if hasattr(settings, name)
    }


def _view_layer(layer: Any) -> dict[str, Any]:
    passes = {
        name.removeprefix("use_pass_"): bool(getattr(layer, name))
        for name in PASS_ATTRIBUTES
        if hasattr(layer, name)
    }
    aovs = [
        {
            "name": aov.name,
            "type": str(getattr(aov, "type", "")),
            "valid": bool(getattr(aov, "is_valid", True)),
        }
        for aov in getattr(layer, "aovs", ())
    ]
    lightgroups = [group.name for group in getattr(layer, "lightgroups", ())]
    result: dict[str, Any] = {
        "name": layer.name,
        "enabled": bool(getattr(layer, "use", True)),
        "passes": passes,
        "aovs": aovs,
        "lightgroups": lightgroups,
        "material_override": getattr(getattr(layer, "material_override", None), "name", None),
    }
    for name in ("pass_cryptomatte_depth", "use_pass_cryptomatte_accurate"):
        if hasattr(layer, name):
            result[name] = getattr(layer, name)
    return result


def _slot_records(node: Any, base_path: str, bpy: Any, include_paths: bool) -> list[dict[str, Any]]:
    modern = hasattr(node, "file_output_items")
    slots = list(node.file_output_items if modern else getattr(node, "file_slots", ()))
    inputs = list(node.inputs)
    records = []
    for index, slot in enumerate(slots):
        relative = str(getattr(slot, "name", "")) if modern else str(getattr(slot, "path", ""))
        socket = inputs[index] if index < len(inputs) else None
        record: dict[str, Any] = {
            "index": index,
            "name": socket.name if socket is not None else str(getattr(slot, "name", "")),
            "path_fragment": relative,
            "linked": bool(socket.is_linked) if socket is not None else False,
            "use_node_format": not bool(getattr(slot, "override_node_format", False)) if modern else bool(getattr(slot, "use_node_format", True)),
        }
        if include_paths:
            fragment = str(getattr(node, "file_name", "")) + relative if modern else relative
            combined = _joined_output_pattern(base_path, fragment)
            record["resolved_path_pattern"] = _resolved_path(bpy, combined)
        records.append(record)
    return records


def _compositor_read(scene: Any, bpy: Any, include_paths: bool) -> dict[str, Any]:
    try:
        tree = _scene_tree(scene)
    except Exception as exc:
        return {"enabled": False, "composite_outputs": [], "file_outputs": [], "warning": {"code": "compositor.unreadable", "message": str(exc)}}
    if tree is None:
        return {"enabled": False, "composite_outputs": [], "file_outputs": []}
    composite_outputs = []
    file_outputs = []
    try:
        graph_nodes = sorted(tree.nodes, key=lambda item: item.name)
        node_count, link_count = len(tree.nodes), len(tree.links)
    except Exception as exc:
        return {"enabled": False, "composite_outputs": [], "file_outputs": [], "warning": {"code": "compositor.unreadable", "message": str(exc)}}
    for node in graph_nodes:
        if node.bl_idname in {"CompositorNodeComposite", "NodeGroupOutput"}:
            image = node.inputs.get("Image")
            composite_outputs.append(
                {
                    "node": node.name,
                    "image_linked": bool(image and image.is_linked),
                    "mute": bool(getattr(node, "mute", False)),
                }
            )
        elif node.bl_idname == "CompositorNodeOutputFile":
            base_path = str(getattr(node, "directory", getattr(node, "base_path", "")))
            record: dict[str, Any] = {
                "node": node.name,
                "base_path_configured": bool(base_path),
                "format": _format(node.format),
                "slots": _slot_records(node, base_path, bpy, include_paths),
                "mute": bool(getattr(node, "mute", False)),
            }
            if hasattr(node, "file_name"):
                record["file_name"] = str(node.file_name)
            if include_paths:
                record["base_path"] = base_path
                record["resolved_base_path"] = _resolved_path(bpy, base_path)
            file_outputs.append(record)
    return {
        "enabled": True,
        "node_count": node_count,
        "link_count": link_count,
        "composite_outputs": composite_outputs,
        "file_outputs": file_outputs,
    }


def _compositor(scene: Any, bpy: Any, include_paths: bool) -> dict[str, Any]:
    try:
        return _compositor_read(scene, bpy, include_paths)
    except Exception as exc:
        return {
            "enabled": False, "composite_outputs": [], "file_outputs": [],
            "warning": {"code": "compositor.unreadable", "message": str(exc)},
        }


def _manifest(bpy: Any, scene: Any, include_paths: bool) -> dict[str, Any]:
    render = scene.render
    raw_primary = str(render.filepath)
    primary: dict[str, Any] = {
        "configured": bool(raw_primary),
        "format": _format(render.image_settings),
        "use_file_extension": bool(getattr(render, "use_file_extension", True)),
    }
    if include_paths:
        primary["path"] = raw_primary
        primary["resolved_path"] = _resolved_path(bpy, raw_primary)
    return {
        "schema_version": "1.0",
        "scene": scene.name,
        "document_saved": bool(bpy.data.filepath),
        "state_token": current_state_token(bpy),
        "engine": render.engine,
        "camera": scene.camera.name if scene.camera else None,
        "frame": {
            "current": int(scene.frame_current),
            "start": int(scene.frame_start),
            "end": int(scene.frame_end),
            "step": int(scene.frame_step),
            "fps": float(render.fps) / float(render.fps_base),
        },
        "resolution": {
            "x": int(render.resolution_x),
            "y": int(render.resolution_y),
            "percentage": int(render.resolution_percentage),
            "pixel_aspect_x": float(render.pixel_aspect_x),
            "pixel_aspect_y": float(render.pixel_aspect_y),
        },
        "film_transparent": bool(render.film_transparent),
        "primary_output": primary,
        "view_layers": [_view_layer(layer) for layer in scene.view_layers],
        "compositor": _compositor(scene, bpy, include_paths),
    }


def inspect_render_manifest(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"scene", "include_paths"})
    scene_name = _optional_name(params.get("scene"), "scene")
    include_paths = _strict_bool(params.get("include_paths"), "include_paths", False)
    bpy = get_bpy()
    return ToolResult(data=_manifest(bpy, _scene(bpy, scene_name), include_paths))


def _issue(severity: str, code: str, message: str, **context: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if context:
        record["context"] = context
    return record


def validate_render_manifest(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"scene", "include_paths", "require_composite", "require_file_output"})
    scene_name = _optional_name(params.get("scene"), "scene")
    include_paths = _strict_bool(params.get("include_paths"), "include_paths", False)
    require_composite = _strict_bool(params.get("require_composite"), "require_composite", True)
    require_file_output = _strict_bool(params.get("require_file_output"), "require_file_output", False)
    bpy = get_bpy()
    scene = _scene(bpy, scene_name)
    manifest = _manifest(bpy, scene, include_paths)
    issues: list[dict[str, Any]] = []
    if manifest["camera"] is None:
        issues.append(_issue("error", "camera.missing", "The scene has no active render camera."))
    if not any(layer["enabled"] for layer in manifest["view_layers"]):
        issues.append(_issue("error", "view_layers.none_enabled", "No view layer is enabled for rendering."))
    resolution = manifest["resolution"]
    if resolution["x"] * resolution["y"] * resolution["percentage"] * resolution["percentage"] <= 0:
        issues.append(_issue("error", "resolution.invalid", "The effective render resolution is zero."))
    primary = manifest["primary_output"]
    if not primary["configured"]:
        issues.append(_issue("warning", "primary_output.path_empty", "The primary render output path is empty."))
    compositor = manifest["compositor"]
    if "warning" in compositor:
        warning = compositor["warning"]
        issues.append(_issue("warning", warning["code"], warning["message"]))
    if require_composite:
        if not compositor["enabled"]:
            issues.append(_issue("error", "compositor.disabled", "The scene compositor is disabled."))
        elif not compositor["composite_outputs"]:
            issues.append(_issue("error", "composite.missing", "No Composite output node exists."))
        elif not any(item["image_linked"] and not item["mute"] for item in compositor["composite_outputs"]):
            issues.append(_issue("error", "composite.unlinked", "No active Composite output has a linked Image input."))
    file_outputs = compositor["file_outputs"]
    if require_file_output and not file_outputs:
        issues.append(_issue("error", "file_output.missing", "No File Output node exists."))
    for output in file_outputs:
        if output["mute"]:
            continue
        if not output["base_path_configured"]:
            issues.append(_issue("warning", "file_output.base_path_empty", "File Output base path is empty.", node=output["node"]))
        if not output["slots"]:
            issues.append(_issue("error", "file_output.no_slots", "File Output node has no slots.", node=output["node"]))
        for slot in output["slots"]:
            if not slot["linked"]:
                issues.append(_issue("warning", "file_output.slot_unlinked", "File Output slot is not linked.", node=output["node"], slot=slot["name"]))
    logical_paths: dict[str, list[str]] = {}
    if compositor["enabled"]:
        for node in _scene_tree(scene).nodes:
            if node.bl_idname != "CompositorNodeOutputFile" or bool(getattr(node, "mute", False)):
                continue
            base_path = str(getattr(node, "directory", getattr(node, "base_path", "")))
            inputs = list(node.inputs)
            modern = hasattr(node, "file_output_items")
            for index, slot in enumerate(node.file_output_items if modern else getattr(node, "file_slots", ())):
                fragment = str(getattr(node, "file_name", "")) + str(getattr(slot, "name", "")) if modern else str(getattr(slot, "path", ""))
                key = _resolved_path(bpy, _joined_output_pattern(base_path, fragment)).casefold()
                socket_name = inputs[index].name if index < len(inputs) else str(index)
                logical_paths.setdefault(key, []).append(f"{node.name}:{socket_name}")
    for consumers in logical_paths.values():
        if len(consumers) > 1:
            issues.append(_issue("error", "output.path_collision", "Multiple enabled outputs resolve to the same path pattern.", outputs=consumers))
    errors = sum(issue["severity"] == "error" for issue in issues)
    warnings = sum(issue["severity"] == "warning" for issue in issues)
    return ToolResult(
        data={
            "valid": errors == 0,
            "error_count": errors,
            "warning_count": warnings,
            "issues": issues,
            "manifest": manifest,
        }
    )
