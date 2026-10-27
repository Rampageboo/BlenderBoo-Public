"""Typed camera, light, world, and persistent render-setting controls."""

from __future__ import annotations

import math
from typing import Any, Callable

from ..errors import InvalidParamsError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy, vector_values
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


MAX_STAGE_ITEMS = 2000
CAMERA_TYPES = frozenset({"PERSP", "ORTHO", "PANO"})
LIGHT_TYPES = frozenset({"POINT", "SUN", "SPOT", "AREA"})
AREA_SHAPES = frozenset({"SQUARE", "RECTANGLE", "DISK", "ELLIPSE"})
IMAGE_FORMATS = frozenset({"PNG", "JPEG", "OPEN_EXR", "TIFF"})


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(names)}")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _optional_name(value: Any, label: str) -> str | None:
    return None if value is None else _name(value, label)


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParamsError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise InvalidParamsError(f"{label} must be a finite number")
    if minimum is not None and (
        result <= minimum if exclusive_minimum else result < minimum
    ):
        comparison = "greater than" if exclusive_minimum else "at least"
        raise InvalidParamsError(f"{label} must be {comparison} {minimum}")
    if maximum is not None and result > maximum:
        raise InvalidParamsError(f"{label} must be at most {maximum}")
    return result


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidParamsError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _color(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise InvalidParamsError(f"{label} must be a three-number RGB array")
    return [_number(item, label, minimum=0.0, maximum=1.0) for item in value]


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _page_params(params: dict[str, Any], tool: str) -> tuple[int, int]:
    limit = params.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_STAGE_ITEMS:
        raise InvalidParamsError(f"limit must be an integer from 1 to {MAX_STAGE_ITEMS}")
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError(f"cursor must be a decimal string returned by {tool}")


def _prepare_mutation(params: dict[str, Any]) -> Any:
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    return bpy


def _transform_summary(obj: Any) -> dict[str, list[float]]:
    return {
        "location": vector_values(obj.location),
        "rotation_euler": vector_values(obj.rotation_euler),
        "scale": vector_values(obj.scale),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [float(item) if isinstance(item, (int, float)) else item for item in value]
    return value


Update = tuple[str, Callable[[], Any], Callable[[Any], None], Any]


def _apply_updates(
    bpy: Any,
    *,
    action: str,
    affected: list[dict[str, str]],
    target_key: str,
    target_name: str,
    updates: list[Update],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = {name: _json_value(getter()) for name, getter, _, _ in updates}
    requested = {name: _json_value(value) for name, _, _, value in updates}
    changed_names = [name for name in requested if before[name] != requested[name]]
    journal = [
        {
            "op": action,
            "target": target_name,
            "field": name,
            "before": before[name],
            "after": requested[name],
            "status": "planned" if name in changed_names else "unchanged",
        }
        for name in requested
    ]
    transaction = new_transaction(affected)
    applied: list[Update] = []
    try:
        for update in updates:
            name, _, setter, value = update
            if name not in changed_names:
                continue
            applied.append(update)
            setter(value)
            next(item for item in journal if item["field"] == name)["status"] = "applied"
        after = {name: _json_value(getter()) for name, getter, _, _ in updates}
    except Exception as exc:
        rollback_errors = []
        for name, _, setter, _ in reversed(applied):
            try:
                setter(before[name])
                next(item for item in journal if item["field"] == name)["status"] = "rolled_back"
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {name}: {rollback_exc}")
        transaction.update(
            **{target_key: target_name},
            changed=bool(rollback_errors),
            journal=journal,
            warnings=rollback_errors,
        )
        if extra:
            transaction.update(extra)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"{action} failed: {exc}", transaction) from exc
    transaction.update(
        **{target_key: target_name},
        before=before,
        after=after,
        changed=bool(changed_names),
        journal=journal,
    )
    if extra:
        transaction.update(extra)
    if changed_names:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _camera_detail(scene: Any, obj: Any) -> dict[str, Any]:
    data = obj.data
    return {
        "object": obj.name,
        "data": data.name,
        "active": scene.camera is obj,
        "type": data.type,
        "lens": float(data.lens),
        "ortho_scale": float(data.ortho_scale),
        "clip_start": float(data.clip_start),
        "clip_end": float(data.clip_end),
        "shift_x": float(data.shift_x),
        "shift_y": float(data.shift_y),
        "sensor_width": float(data.sensor_width),
        "sensor_height": float(data.sensor_height),
        "transform": _transform_summary(obj),
        "collections": sorted(item.name for item in obj.users_collection),
    }


def inspect_camera(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"name", "active", "object_name", "limit", "cursor"})
    substring = _optional_name(params.get("name"), "name")
    active = None if "active" not in params else _strict_bool(params["active"], "active")
    detail_name = _optional_name(params.get("object_name"), "object_name")
    limit, offset = _page_params(params, "camera.inspect")
    bpy = get_bpy()
    scene = bpy.context.scene
    cameras = sorted(
        (
            obj for obj in scene.objects
            if obj.type == "CAMERA"
            and (substring is None or substring in obj.name)
            and (active is None or (scene.camera is obj) == active)
        ),
        key=lambda item: item.name,
    )
    page = cameras[offset : offset + limit]
    next_offset = offset + len(page)
    data: dict[str, Any] = {
        "count": len(cameras),
        "active_camera": scene.camera.name if scene.camera else None,
        "cameras": [_camera_detail(scene, obj) for obj in page],
        "next_cursor": str(next_offset) if next_offset < len(cameras) else None,
        "truncated": next_offset < len(cameras),
        "state_token": current_state_token(bpy),
    }
    if detail_name is not None:
        obj = scene.objects.get(detail_name)
        if obj is None or obj.type != "CAMERA":
            raise NotFoundError(f"Camera object not found in current scene: {detail_name}")
        data["camera_detail"] = _camera_detail(scene, obj)
    return ToolResult(data=data)


def patch_camera(params: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "type", "lens", "ortho_scale", "clip_start", "clip_end",
        "shift_x", "shift_y", "sensor_width", "sensor_height",
    }
    _unknown(params, {"object_name", "expected_state", *fields})
    object_name = _name(params.get("object_name"), "object_name")
    if not any(field in params for field in fields):
        raise InvalidParamsError("At least one camera field must be supplied")
    bpy = _prepare_mutation(params)
    obj = bpy.context.scene.objects.get(object_name)
    if obj is None or obj.type != "CAMERA":
        raise NotFoundError(f"Camera object not found in current scene: {object_name}")
    data = obj.data
    values: dict[str, Any] = {}
    if "type" in params:
        value = _name(params["type"], "type").upper()
        if value not in CAMERA_TYPES:
            raise InvalidParamsError("type must be PERSP, ORTHO, or PANO")
        values["type"] = value
    for name, minimum, maximum, exclusive in (
        ("lens", 1.0, 5000.0, False),
        ("ortho_scale", 0.0, 1000000.0, True),
        ("clip_start", 0.0, 1000000.0, True),
        ("clip_end", 0.0, 1000000000.0, True),
        ("shift_x", -10.0, 10.0, False),
        ("shift_y", -10.0, 10.0, False),
        ("sensor_width", 1.0, 1000.0, False),
        ("sensor_height", 1.0, 1000.0, False),
    ):
        if name in params:
            values[name] = _number(
                params[name], name, minimum=minimum, maximum=maximum,
                exclusive_minimum=exclusive,
            )
    final_start = values.get("clip_start", float(data.clip_start))
    final_end = values.get("clip_end", float(data.clip_end))
    if final_start >= final_end:
        raise InvalidParamsError("clip_start must be less than clip_end")
    order = ["type", "lens", "ortho_scale", "clip_start", "clip_end", "shift_x", "shift_y", "sensor_width", "sensor_height"]
    updates = [
        (
            name,
            lambda name=name: getattr(data, name),
            lambda value, name=name: setattr(data, name, value),
            values[name],
        )
        for name in order if name in values
    ]
    return _apply_updates(
        bpy,
        action="camera.patch",
        affected=[{"type": "Object", "name": obj.name}, {"type": "Camera", "name": data.name}],
        target_key="camera",
        target_name=obj.name,
        updates=updates,
    )


def set_active_camera(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "expected_state"})
    if "object_name" not in params:
        raise InvalidParamsError("object_name is required and may be null")
    object_name = _optional_name(params.get("object_name"), "object_name")
    bpy = _prepare_mutation(params)
    scene = bpy.context.scene
    camera = None
    if object_name is not None:
        camera = scene.objects.get(object_name)
        if camera is None or camera.type != "CAMERA":
            raise NotFoundError(f"Camera object not found in current scene: {object_name}")
    return _apply_updates(
        bpy,
        action="camera.set_active",
        affected=[{"type": "Scene", "name": scene.name}] + (
            [{"type": "Object", "name": camera.name}] if camera is not None else []
        ),
        target_key="scene",
        target_name=scene.name,
        updates=[(
            "active_camera",
            lambda: scene.camera.name if scene.camera else None,
            lambda value: setattr(scene, "camera", scene.objects.get(value) if value else None),
            object_name,
        )],
    )


def _light_detail(obj: Any) -> dict[str, Any]:
    data = obj.data
    detail = {
        "object": obj.name,
        "data": data.name,
        "type": data.type,
        "energy": float(data.energy),
        "color": vector_values(data.color),
        "use_shadow": bool(data.use_shadow),
        "transform": _transform_summary(obj),
        "collections": sorted(item.name for item in obj.users_collection),
    }
    for name in ("shadow_soft_size", "shape", "size", "size_y", "spot_size", "spot_blend"):
        if hasattr(data, name):
            value = getattr(data, name)
            detail[name] = float(value) if isinstance(value, (int, float)) else value
    return detail


def inspect_light(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"name", "type", "object_name", "limit", "cursor"})
    substring = _optional_name(params.get("name"), "name")
    light_type = _optional_name(params.get("type"), "type")
    if light_type is not None:
        light_type = light_type.upper()
        if light_type not in LIGHT_TYPES:
            raise InvalidParamsError("type must be POINT, SUN, SPOT, or AREA")
    detail_name = _optional_name(params.get("object_name"), "object_name")
    limit, offset = _page_params(params, "light.inspect")
    bpy = get_bpy()
    lights = sorted(
        (
            obj for obj in bpy.context.scene.objects
            if obj.type == "LIGHT"
            and (substring is None or substring in obj.name)
            and (light_type is None or obj.data.type == light_type)
        ),
        key=lambda item: item.name,
    )
    page = lights[offset : offset + limit]
    next_offset = offset + len(page)
    data: dict[str, Any] = {
        "count": len(lights),
        "lights": [_light_detail(obj) for obj in page],
        "next_cursor": str(next_offset) if next_offset < len(lights) else None,
        "truncated": next_offset < len(lights),
        "state_token": current_state_token(bpy),
    }
    if detail_name is not None:
        obj = bpy.context.scene.objects.get(detail_name)
        if obj is None or obj.type != "LIGHT":
            raise NotFoundError(f"Light object not found in current scene: {detail_name}")
        data["light_detail"] = _light_detail(obj)
    return ToolResult(data=data)


def patch_light(params: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "type", "energy", "color", "use_shadow", "shadow_soft_size",
        "shape", "size", "size_y", "spot_size", "spot_blend",
    }
    _unknown(params, {"object_name", "expected_state", *fields})
    object_name = _name(params.get("object_name"), "object_name")
    if not any(field in params for field in fields):
        raise InvalidParamsError("At least one light field must be supplied")
    bpy = _prepare_mutation(params)
    obj = bpy.context.scene.objects.get(object_name)
    if obj is None or obj.type != "LIGHT":
        raise NotFoundError(f"Light object not found in current scene: {object_name}")
    data = obj.data
    values: dict[str, Any] = {}
    final_type = data.type
    if "type" in params:
        final_type = _name(params["type"], "type").upper()
        if final_type not in LIGHT_TYPES:
            raise InvalidParamsError("type must be POINT, SUN, SPOT, or AREA")
        values["type"] = final_type
    specialized = {"shape", "size", "size_y", "spot_size", "spot_blend"}
    if final_type != data.type and any(name in params for name in specialized):
        raise InvalidParamsError(
            "Change light type in one light.patch call, then patch type-specific fields in a second call"
        )
    if "energy" in params:
        values["energy"] = _number(params["energy"], "energy", minimum=0.0, maximum=1000000000.0)
    if "color" in params:
        values["color"] = _color(params["color"], "color")
    if "use_shadow" in params:
        values["use_shadow"] = _strict_bool(params["use_shadow"], "use_shadow")
    if "shadow_soft_size" in params:
        values["shadow_soft_size"] = _number(params["shadow_soft_size"], "shadow_soft_size", minimum=0.0, maximum=1000000.0)
    if "shape" in params:
        shape = _name(params["shape"], "shape").upper()
        if final_type != "AREA" or shape not in AREA_SHAPES:
            raise InvalidParamsError("shape is valid only for AREA lights and must be SQUARE, RECTANGLE, DISK, or ELLIPSE")
        values["shape"] = shape
    for name in ("size", "size_y"):
        if name in params:
            if final_type != "AREA":
                raise InvalidParamsError(f"{name} is valid only for AREA lights")
            values[name] = _number(params[name], name, minimum=0.0, maximum=1000000.0, exclusive_minimum=True)
    if "spot_size" in params:
        if final_type != "SPOT":
            raise InvalidParamsError("spot_size is valid only for SPOT lights")
        values["spot_size"] = _number(params["spot_size"], "spot_size", minimum=0.0, maximum=math.pi, exclusive_minimum=True)
    if "spot_blend" in params:
        if final_type != "SPOT":
            raise InvalidParamsError("spot_blend is valid only for SPOT lights")
        values["spot_blend"] = _number(params["spot_blend"], "spot_blend", minimum=0.0, maximum=1.0)
    order = ["type", "energy", "color", "use_shadow", "shadow_soft_size", "shape", "size", "size_y", "spot_size", "spot_blend"]
    for name in values:
        if name != "type" and not hasattr(data, name):
            raise InvalidParamsError(f"{name} is unavailable for this Blender light type/version")
    updates = [
        (
            name,
            lambda name=name: vector_values(getattr(data, name)) if name == "color" else getattr(data, name),
            lambda value, name=name: setattr(data, name, value),
            values[name],
        )
        for name in order if name in values
    ]
    return _apply_updates(
        bpy,
        action="light.patch",
        affected=[{"type": "Object", "name": obj.name}, {"type": "Light", "name": data.name}],
        target_key="light",
        target_name=obj.name,
        updates=updates,
    )


def _background_node(world: Any) -> Any | None:
    tree = getattr(world, "node_tree", None)
    if tree is None:
        return None
    nodes = [node for node in tree.nodes if node.bl_idname == "ShaderNodeBackground"]
    return nodes[0] if len(nodes) == 1 else None


def _world_detail(scene: Any, world: Any) -> dict[str, Any]:
    background = _background_node(world) if world.use_nodes else None
    return {
        "name": world.name,
        "active": scene.world is world,
        "color": vector_values(world.color),
        "use_nodes": bool(world.use_nodes),
        "node_count": len(world.node_tree.nodes) if world.node_tree is not None else 0,
        "background_color": vector_values(background.inputs["Color"].default_value) if background else None,
        "background_strength": float(background.inputs["Strength"].default_value) if background else None,
        "custom_properties": sorted(str(key) for key in world.keys() if str(key) != "_RNA_UI"),
    }


def inspect_world(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"name", "active", "world_name", "limit", "cursor"})
    substring = _optional_name(params.get("name"), "name")
    active = None if "active" not in params else _strict_bool(params["active"], "active")
    detail_name = _optional_name(params.get("world_name"), "world_name")
    limit, offset = _page_params(params, "world.inspect")
    bpy = get_bpy()
    scene = bpy.context.scene
    worlds = sorted(
        (
            world for world in bpy.data.worlds
            if (substring is None or substring in world.name)
            and (active is None or (scene.world is world) == active)
        ),
        key=lambda item: item.name,
    )
    page = worlds[offset : offset + limit]
    next_offset = offset + len(page)
    data: dict[str, Any] = {
        "count": len(worlds),
        "active_world": scene.world.name if scene.world else None,
        "worlds": [_world_detail(scene, world) for world in page],
        "next_cursor": str(next_offset) if next_offset < len(worlds) else None,
        "truncated": next_offset < len(worlds),
        "state_token": current_state_token(bpy),
    }
    if detail_name is not None:
        world = bpy.data.worlds.get(detail_name)
        if world is None:
            raise NotFoundError(f"World not found: {detail_name}")
        data["world_detail"] = _world_detail(scene, world)
    return ToolResult(data=data)


def _unique_name(collection: Any, requested: str) -> str:
    if collection.get(requested) is None:
        return requested
    index = 1
    while collection.get(f"{requested}.{index:03d}") is not None:
        index += 1
    return f"{requested}.{index:03d}"


def create_world(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"name", "color", "use_nodes", "background_color", "background_strength", "set_active", "expected_state"})
    requested_name = _name(params.get("name"), "name")
    color = _color(params.get("color", [0.05, 0.05, 0.05]), "color")
    use_nodes = _strict_bool(params.get("use_nodes", False), "use_nodes")
    set_active = _strict_bool(params.get("set_active", False), "set_active")
    background_color = _color(params.get("background_color", color), "background_color")
    background_strength = _number(params.get("background_strength", 1.0), "background_strength", minimum=0.0, maximum=1000000.0)
    if ("background_color" in params or "background_strength" in params) and not use_nodes:
        raise InvalidParamsError("background_color and background_strength require use_nodes: true")
    bpy = _prepare_mutation(params)
    scene = bpy.context.scene
    resolved_name = _unique_name(bpy.data.worlds, requested_name)
    world = None
    previous_world = scene.world
    try:
        world = bpy.data.worlds.new(resolved_name)
        world.color = color
        world.use_nodes = use_nodes
        if use_nodes:
            background = _background_node(world)
            if background is None:
                raise RuntimeError("Blender did not create one default Background node")
            background.inputs["Color"].default_value = [*background_color, 1.0]
            background.inputs["Strength"].default_value = background_strength
        if set_active:
            scene.world = world
        detail = _world_detail(scene, world)
    except Exception as exc:
        cleanup_errors = []
        try:
            scene.world = previous_world
        except Exception as cleanup_exc:
            cleanup_errors.append(f"restore active world: {cleanup_exc}")
        if world is not None and bpy.data.worlds.get(world.name) is world:
            try:
                bpy.data.worlds.remove(world)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove world {world.name}: {cleanup_exc}")
        transaction = new_transaction([{"type": "World", "name": resolved_name}, {"type": "Scene", "name": scene.name}])
        transaction.update(
            requested_name=requested_name,
            resolved_name=resolved_name,
            changed=bool(cleanup_errors),
            warnings=cleanup_errors,
            journal=[{"op": "world.create", "target": resolved_name, "status": "failed"}],
        )
        if cleanup_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"world.create failed: {exc}", transaction) from exc
    transaction = new_transaction([{"type": "World", "name": world.name}] + ([{"type": "Scene", "name": scene.name}] if set_active else []))
    transaction.update(
        world=world.name,
        requested_name=requested_name,
        resolved_name=world.name,
        set_active=set_active,
        detail=detail,
        changed=True,
        journal=[{"op": "world.create", "target": world.name, "before": None, "after": detail, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def patch_world(params: dict[str, Any]) -> dict[str, Any]:
    fields = {"color", "use_nodes", "background_color", "background_strength"}
    _unknown(params, {"world_name", "expected_state", *fields})
    world_name = _name(params.get("world_name"), "world_name")
    if not any(field in params for field in fields):
        raise InvalidParamsError("At least one world field must be supplied")
    bpy = _prepare_mutation(params)
    world = bpy.data.worlds.get(world_name)
    if world is None:
        raise NotFoundError(f"World not found: {world_name}")
    background_requested = "background_color" in params or "background_strength" in params
    if background_requested and not world.use_nodes:
        raise InvalidParamsError("Enable use_nodes in a prior world.patch call before patching Background inputs")
    background = _background_node(world) if background_requested else None
    if background_requested and background is None:
        raise InvalidParamsError("world.patch requires exactly one Background node for background fields")
    updates: list[Update] = []
    if "color" in params:
        value = _color(params["color"], "color")
        updates.append(("color", lambda: vector_values(world.color), lambda item: setattr(world, "color", item), value))
    if "use_nodes" in params:
        value = _strict_bool(params["use_nodes"], "use_nodes")
        if background_requested and not value:
            raise InvalidParamsError("Cannot disable use_nodes while patching Background inputs")
        updates.append(("use_nodes", lambda: bool(world.use_nodes), lambda item: setattr(world, "use_nodes", item), value))
    if "background_color" in params:
        value = [*_color(params["background_color"], "background_color"), 1.0]
        updates.append((
            "background_color",
            lambda: vector_values(background.inputs["Color"].default_value),
            lambda item: setattr(background.inputs["Color"], "default_value", item),
            value,
        ))
    if "background_strength" in params:
        value = _number(params["background_strength"], "background_strength", minimum=0.0, maximum=1000000.0)
        updates.append((
            "background_strength",
            lambda: float(background.inputs["Strength"].default_value),
            lambda item: setattr(background.inputs["Strength"], "default_value", item),
            value,
        ))
    return _apply_updates(
        bpy,
        action="world.patch",
        affected=[{"type": "World", "name": world.name}],
        target_key="world",
        target_name=world.name,
        updates=updates,
    )


def set_active_world(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"world_name", "expected_state"})
    if "world_name" not in params:
        raise InvalidParamsError("world_name is required and may be null")
    world_name = _optional_name(params.get("world_name"), "world_name")
    bpy = _prepare_mutation(params)
    scene = bpy.context.scene
    world = None
    if world_name is not None:
        world = bpy.data.worlds.get(world_name)
        if world is None:
            raise NotFoundError(f"World not found: {world_name}")
    return _apply_updates(
        bpy,
        action="world.set_active",
        affected=[{"type": "Scene", "name": scene.name}] + (
            [{"type": "World", "name": world.name}] if world is not None else []
        ),
        target_key="scene",
        target_name=scene.name,
        updates=[(
            "active_world",
            lambda: scene.world.name if scene.world else None,
            lambda value: setattr(scene, "world", bpy.data.worlds.get(value) if value else None),
            world_name,
        )],
    )


def _available_render_engines(scene: Any) -> list[str]:
    try:
        enum_items = scene.render.bl_rna.properties["engine"].enum_items
        return sorted(item.identifier for item in enum_items)
    except Exception:
        return sorted({str(scene.render.engine)})


def _render_detail(scene: Any) -> dict[str, Any]:
    render = scene.render
    detail = {
        "scene": scene.name,
        "engine": render.engine,
        "available_engines": _available_render_engines(scene),
        "resolution_x": int(render.resolution_x),
        "resolution_y": int(render.resolution_y),
        "resolution_percentage": int(render.resolution_percentage),
        "film_transparent": bool(render.film_transparent),
        "fps": int(render.fps),
        "fps_base": float(render.fps_base),
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "frame_step": int(scene.frame_step),
        "image_format": render.image_settings.file_format,
        "filepath": render.filepath,
        "active_camera": scene.camera.name if scene.camera else None,
        "active_world": scene.world.name if scene.world else None,
    }
    cycles = getattr(scene, "cycles", None)
    if cycles is not None:
        for field, attr in (("cycles_samples", "samples"), ("cycles_use_denoising", "use_denoising")):
            if hasattr(cycles, attr):
                detail[field] = getattr(cycles, attr)
    eevee = getattr(scene, "eevee", None)
    if eevee is not None and hasattr(eevee, "use_raytracing"):
        detail["eevee_use_raytracing"] = bool(eevee.use_raytracing)
    view = scene.view_settings
    for field, attr in (("view_transform", "view_transform"), ("look", "look"),
                        ("exposure", "exposure"), ("gamma", "gamma")):
        if hasattr(view, attr):
            detail[field] = getattr(view, attr)
    return detail


def render_settings(params: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "engine", "resolution_x", "resolution_y", "resolution_percentage",
        "film_transparent", "fps", "fps_base", "frame_start", "frame_end",
        "frame_step", "image_format", "filepath",
        "cycles_samples", "cycles_use_denoising", "eevee_use_raytracing",
        "view_transform", "look", "exposure", "gamma",
    }
    _unknown(params, {"expected_state", *fields})
    bpy = get_bpy()
    scene = bpy.context.scene
    if not any(field in params for field in fields):
        check_expected_state(params.get("expected_state"), bpy)
        return {**_render_detail(scene), "changed": False, "state_token": current_state_token(bpy)}
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    render = scene.render
    values: dict[str, Any] = {}
    if "engine" in params:
        engine = _name(params["engine"], "engine")
        available = _available_render_engines(scene)
        if engine not in available:
            raise InvalidParamsError(f"engine is unavailable; choose one of: {', '.join(available)}")
        values["engine"] = engine
    for name in ("resolution_x", "resolution_y"):
        if name in params:
            values[name] = _integer(params[name], name, 1, 16384)
    if "resolution_percentage" in params:
        values["resolution_percentage"] = _integer(params["resolution_percentage"], "resolution_percentage", 1, 100)
    if "film_transparent" in params:
        values["film_transparent"] = _strict_bool(params["film_transparent"], "film_transparent")
    if "fps" in params:
        values["fps"] = _integer(params["fps"], "fps", 1, 240)
    if "fps_base" in params:
        values["fps_base"] = _number(params["fps_base"], "fps_base", minimum=0.0, maximum=1000.0, exclusive_minimum=True)
    for name, minimum, maximum in (("frame_start", -1048574, 1048574), ("frame_end", -1048574, 1048574), ("frame_step", 1, 1048574)):
        if name in params:
            values[name] = _integer(params[name], name, minimum, maximum)
    final_start = values.get("frame_start", int(scene.frame_start))
    final_end = values.get("frame_end", int(scene.frame_end))
    if final_start > final_end:
        raise InvalidParamsError("frame_start must not exceed frame_end")
    if "image_format" in params:
        image_format = _name(params["image_format"], "image_format").upper()
        if image_format not in IMAGE_FORMATS:
            raise InvalidParamsError("image_format must be PNG, JPEG, OPEN_EXR, or TIFF")
        values["image_format"] = image_format
    if "filepath" in params:
        values["filepath"] = _name(params["filepath"], "filepath")
    cycles = getattr(scene, "cycles", None)
    eevee = getattr(scene, "eevee", None)
    view = scene.view_settings
    if "cycles_samples" in params:
        if cycles is None or not hasattr(cycles, "samples"):
            raise InvalidStateError("Cycles samples are unavailable in this Blender build")
        values["cycles_samples"] = _integer(params["cycles_samples"], "cycles_samples", 1, 65536)
    if "cycles_use_denoising" in params:
        if cycles is None or not hasattr(cycles, "use_denoising"):
            raise InvalidStateError("Cycles denoising is unavailable in this Blender build")
        values["cycles_use_denoising"] = _strict_bool(params["cycles_use_denoising"], "cycles_use_denoising")
    if "eevee_use_raytracing" in params:
        if eevee is None or not hasattr(eevee, "use_raytracing"):
            raise InvalidStateError("Eevee raytracing is unavailable in this Blender build")
        values["eevee_use_raytracing"] = _strict_bool(params["eevee_use_raytracing"], "eevee_use_raytracing")
    for name in ("view_transform", "look"):
        if name in params:
            value = _name(params[name], name)
            prop = view.bl_rna.properties[name]
            available = {item.identifier for item in prop.enum_items}
            if value not in available:
                raise InvalidParamsError(f"{name} is unavailable; choose one of: {', '.join(sorted(available))}")
            values[name] = value
    if "exposure" in params:
        values["exposure"] = _number(params["exposure"], "exposure", minimum=-32.0, maximum=32.0)
    if "gamma" in params:
        values["gamma"] = _number(params["gamma"], "gamma", minimum=0.001, maximum=5.0)
    updates: list[Update] = []
    render_fields = ["engine", "resolution_x", "resolution_y", "resolution_percentage", "film_transparent", "fps", "fps_base", "filepath"]
    for name in render_fields:
        if name in values:
            updates.append((name, lambda name=name: getattr(render, name), lambda value, name=name: setattr(render, name, value), values[name]))
    for name in ("frame_start", "frame_end", "frame_step"):
        if name in values:
            updates.append((name, lambda name=name: getattr(scene, name), lambda value, name=name: setattr(scene, name, value), values[name]))
    if "image_format" in values:
        updates.append((
            "image_format",
            lambda: render.image_settings.file_format,
            lambda value: setattr(render.image_settings, "file_format", value),
            values["image_format"],
        ))
    for field, owner, attr in (
        ("cycles_samples", cycles, "samples"),
        ("cycles_use_denoising", cycles, "use_denoising"),
        ("eevee_use_raytracing", eevee, "use_raytracing"),
        ("view_transform", view, "view_transform"),
        ("look", view, "look"),
        ("exposure", view, "exposure"),
        ("gamma", view, "gamma"),
    ):
        if field in values:
            updates.append((field, lambda owner=owner, attr=attr: getattr(owner, attr),
                            lambda value, owner=owner, attr=attr: setattr(owner, attr, value), values[field]))
    transaction = _apply_updates(
        bpy,
        action="render.settings",
        affected=[{"type": "Scene", "name": scene.name}],
        target_key="scene",
        target_name=scene.name,
        updates=updates,
    )
    transaction["settings"] = _render_detail(scene)
    return transaction
