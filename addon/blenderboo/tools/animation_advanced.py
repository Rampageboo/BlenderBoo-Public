"""Bounded NLA, generated transform-driver, and shape-key controls."""

from __future__ import annotations

import math
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .skinning import _capture_context, _restore_context, _select_only


MAX_PAGE = 2000
MAX_OPERATIONS = 100
MAX_NLA_TRACKS = 1000
MAX_NLA_STRIPS = 10_000
MAX_DRIVERS = 20_000
MAX_SHAPE_KEYS = 1000
MAX_SHAPE_POINTS = 5_000_000
MAX_PATCH_POINTS = 250_000
FRAME_MIN = -1_048_574
FRAME_MAX = 1_048_574
ANIMATED_PROPERTIES = {
    "location": 3,
    "rotation_euler": 3,
    "rotation_quaternion": 4,
    "rotation_axis_angle": 4,
    "scale": 3,
}
TRANSFORM_TYPES = frozenset(
    {"LOC_X", "LOC_Y", "LOC_Z", "ROT_X", "ROT_Y", "ROT_Z", "SCALE_X", "SCALE_Y", "SCALE_Z"}
)
TRANSFORM_SPACES = frozenset({"WORLD_SPACE", "TRANSFORM_SPACE", "LOCAL_SPACE"})
NLA_BLEND_TYPES = frozenset({"REPLACE", "COMBINE", "ADD", "SUBTRACT", "MULTIPLY"})
NLA_EXTRAPOLATION = frozenset({"NOTHING", "HOLD", "HOLD_FORWARD"})
SHAPE_INTERPOLATIONS = frozenset({"KEY_LINEAR", "KEY_CARDINAL", "KEY_CATMULL_ROM", "KEY_BSPLINE"})


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


def _vector(value: Any, label: str, size: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise InvalidParamsError(f"{label} must contain exactly {size} numbers")
    return [_number(item, f"{label}[{index}]", -1_000_000.0, 1_000_000.0) for index, item in enumerate(value)]


def _enum(value: Any, label: str, allowed: frozenset[str]) -> str:
    result = _name(value, label).upper()
    if result not in allowed:
        raise InvalidParamsError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return result


def _page(params: dict[str, Any], tool: str) -> tuple[int, int]:
    limit = _integer(params.get("limit", 200), "limit", 1, MAX_PAGE)
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError(f"cursor must be a decimal string returned by {tool}")


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _scene_object(bpy: Any, name: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    return obj


def _find_named(collection: Any, name: str) -> Any | None:
    getter = getattr(collection, "get", None)
    if callable(getter):
        found = getter(name)
        if found is not None:
            return found
    return next((item for item in collection if item.name == name), None)


def _nla_budget(tracks: Any) -> tuple[int, int]:
    track_count = len(tracks)
    strip_count = sum(len(track.strips) for track in tracks)
    if track_count > MAX_NLA_TRACKS or strip_count > MAX_NLA_STRIPS:
        raise InvalidStateError("NLA data exceeds the supported track or strip budget")
    return track_count, strip_count


def _strip_record(strip: Any) -> dict[str, Any]:
    return {
        "name": strip.name,
        "type": str(strip.type),
        "action": strip.action.name if strip.action is not None else None,
        "frame_start": float(strip.frame_start),
        "frame_end": float(strip.frame_end),
        "action_frame_start": float(strip.action_frame_start),
        "action_frame_end": float(strip.action_frame_end),
        "scale": float(strip.scale),
        "repeat": float(strip.repeat),
        "blend_type": str(strip.blend_type),
        "extrapolation": str(strip.extrapolation),
        "influence": float(strip.influence),
        "mute": bool(strip.mute),
    }


def inspect_nla(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "include_strips", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    include_strips = _strict_bool(params.get("include_strips"), "include_strips", True)
    limit, offset = _page(params, "animation.nla_inspect")
    bpy = get_bpy()
    obj = _scene_object(bpy, object_name)
    animation_data = obj.animation_data
    tracks = animation_data.nla_tracks if animation_data is not None else []
    track_count, strip_count = _nla_budget(tracks)
    ordered = sorted(tracks, key=lambda track: track.name.casefold())
    page = ordered[offset:offset + limit]
    items = []
    for track in page:
        record = {
            "name": track.name,
            "mute": bool(track.mute),
            "is_solo": bool(track.is_solo),
            "lock": bool(track.lock),
            "strip_count": len(track.strips),
        }
        if include_strips:
            record["strips"] = [_strip_record(strip) for strip in track.strips]
        items.append(record)
    next_offset = offset + len(page)
    return {
        "object": obj.name,
        "active_action": animation_data.action.name if animation_data is not None and animation_data.action is not None else None,
        "track_count": track_count,
        "strip_count": strip_count,
        "items": items,
        "page": {"offset": offset, "limit": limit, "returned": len(items), "next_cursor": str(next_offset) if next_offset < len(ordered) else None},
        "state_token": current_state_token(bpy),
    }


def _nla_settings(value: Any, label: str, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError(f"{label} must be an object")
    if kind == "track":
        allowed = {"mute", "is_solo", "lock"}
    else:
        allowed = {"frame_end", "action_frame_start", "action_frame_end", "scale", "repeat", "blend_type", "extrapolation", "influence", "mute"}
    _unknown(value, allowed, f"{label} fields")
    settings: dict[str, Any] = {}
    for field, item in value.items():
        if field in {"mute", "is_solo", "lock"}:
            settings[field] = _strict_bool(item, f"{label}.{field}")
        elif field in {"frame_end", "action_frame_start", "action_frame_end"}:
            settings[field] = _number(item, f"{label}.{field}", FRAME_MIN, FRAME_MAX)
        elif field in {"scale", "repeat"}:
            settings[field] = _number(item, f"{label}.{field}", 0.000001, 1_000_000.0)
        elif field == "influence":
            settings[field] = _number(item, f"{label}.{field}", 0.0, 1.0)
        elif field == "blend_type":
            settings[field] = _enum(item, f"{label}.{field}", NLA_BLEND_TYPES)
        elif field == "extrapolation":
            settings[field] = _enum(item, f"{label}.{field}", NLA_EXTRAPOLATION)
    return settings


def _nla_operation(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError(f"operations[{index}] must be an object")
    op = _name(value.get("op"), f"operations[{index}].op").lower()
    if op == "add_track":
        _unknown(value, {"op", "track", "settings"}, f"operations[{index}] fields")
        return {"op": op, "track": _name(value.get("track"), f"operations[{index}].track"), "settings": _nla_settings(value.get("settings", {}), f"operations[{index}].settings", "track")}
    if op == "patch_track":
        _unknown(value, {"op", "track", "settings"}, f"operations[{index}] fields")
        settings = _nla_settings(value.get("settings"), f"operations[{index}].settings", "track")
        if not settings:
            raise InvalidParamsError(f"operations[{index}] patch has no settings")
        return {"op": op, "track": _name(value.get("track"), f"operations[{index}].track"), "settings": settings}
    if op == "add_strip":
        _unknown(value, {"op", "track", "name", "action", "frame_start", "settings"}, f"operations[{index}] fields")
        return {
            "op": op,
            "track": _name(value.get("track"), f"operations[{index}].track"),
            "name": _name(value.get("name"), f"operations[{index}].name"),
            "action": _name(value.get("action"), f"operations[{index}].action"),
            "frame_start": _integer(value.get("frame_start"), f"operations[{index}].frame_start", FRAME_MIN, FRAME_MAX),
            "settings": _nla_settings(value.get("settings", {}), f"operations[{index}].settings", "strip"),
        }
    if op == "patch_strip":
        _unknown(value, {"op", "track", "name", "settings"}, f"operations[{index}] fields")
        settings = _nla_settings(value.get("settings"), f"operations[{index}].settings", "strip")
        if not settings:
            raise InvalidParamsError(f"operations[{index}] patch has no settings")
        return {"op": op, "track": _name(value.get("track"), f"operations[{index}].track"), "name": _name(value.get("name"), f"operations[{index}].name"), "settings": settings}
    raise InvalidParamsError("animation.nla_patch operations support: add_track, patch_track, add_strip, patch_strip")


def patch_nla(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "operations", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    raw = params.get("operations")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OPERATIONS:
        raise InvalidParamsError(f"operations must contain 1 to {MAX_OPERATIONS} items")
    operations = [_nla_operation(value, index) for index, value in enumerate(raw)]
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    animation_data = obj.animation_data
    tracks = animation_data.nla_tracks if animation_data is not None else []
    track_count, strip_count = _nla_budget(tracks)
    track_names = {track.name for track in tracks}
    strip_names = {track.name: {strip.name for strip in track.strips} for track in tracks}
    for index, operation in enumerate(operations):
        track_name = operation["track"]
        if operation["op"] == "add_track":
            if track_name in track_names:
                raise InvalidStateError(f"operations[{index}] NLA track already exists: {track_name}")
            track_names.add(track_name)
            strip_names[track_name] = set()
        else:
            if track_name not in track_names:
                raise NotFoundError(f"operations[{index}] NLA track not found: {track_name}")
            if operation["op"] == "add_strip":
                if bpy.data.actions.get(operation["action"]) is None:
                    raise NotFoundError(f"operations[{index}] Action not found: {operation['action']}")
                if operation["name"] in strip_names[track_name]:
                    raise InvalidStateError(f"operations[{index}] NLA strip already exists: {operation['name']}")
                strip_names[track_name].add(operation["name"])
            elif operation["op"] == "patch_strip" and operation["name"] not in strip_names[track_name]:
                raise NotFoundError(f"operations[{index}] NLA strip not found: {operation['name']}")
    if len(track_names) > MAX_NLA_TRACKS or sum(len(names) for names in strip_names.values()) > MAX_NLA_STRIPS:
        raise InvalidStateError("NLA patch would exceed the track or strip budget")
    journal = [{"op": operation["op"], "target": f"{operation['track']}:{operation.get('name', '')}".rstrip(":"), "before": None, "after": {key: value for key, value in operation.items() if key != "op"}, "status": "planned"} for operation in operations]
    transaction, apply = prepare_destructive_action(
        action="animation.nla_patch",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "before_track_count": track_count, "before_strip_count": strip_count},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    created_animation_data = obj.animation_data is None
    animation_data = obj.animation_data_create()
    tracks = animation_data.nla_tracks
    applied: list[dict[str, Any]] = []
    try:
        for entry, operation in zip(journal, operations):
            if operation["op"] == "add_track":
                track = tracks.new()
                applied.append({"op": "add_track", "tracks": tracks, "track": track})
                track.name = operation["track"]
                if track.name != operation["track"]:
                    raise RuntimeError(f"Blender changed requested NLA track name {operation['track']} to {track.name}")
                for field, value in operation["settings"].items():
                    setattr(track, field, value)
            elif operation["op"] == "patch_track":
                track = _find_named(tracks, operation["track"])
                before = {field: getattr(track, field) for field in operation["settings"]}
                applied.append({"op": "patch", "item": track, "before": before})
                entry["before"] = before
                for field, value in operation["settings"].items():
                    setattr(track, field, value)
            elif operation["op"] == "add_strip":
                track = _find_named(tracks, operation["track"])
                action = bpy.data.actions[operation["action"]]
                strip = track.strips.new(operation["name"], operation["frame_start"], action)
                applied.append({"op": "add_strip", "strips": track.strips, "strip": strip})
                if strip.name != operation["name"]:
                    raise RuntimeError(f"Blender changed requested NLA strip name {operation['name']} to {strip.name}")
                for field, value in operation["settings"].items():
                    setattr(strip, field, value)
            else:
                track = _find_named(tracks, operation["track"])
                strip = _find_named(track.strips, operation["name"])
                before = {field: getattr(strip, field) for field in operation["settings"]}
                applied.append({"op": "patch", "item": strip, "before": before})
                entry["before"] = before
                for field, value in operation["settings"].items():
                    setattr(strip, field, value)
            entry["status"] = "applied"
        after_tracks, after_strips = _nla_budget(tracks)
    except Exception as exc:
        rollback_errors = []
        try:
            for inverse in reversed(applied):
                if inverse["op"] == "add_track":
                    inverse["tracks"].remove(inverse["track"])
                elif inverse["op"] == "add_strip":
                    inverse["strips"].remove(inverse["strip"])
                else:
                    for field, value in inverse["before"].items():
                        setattr(inverse["item"], field, value)
            if created_animation_data and len(tracks) == 0 and animation_data.action is None:
                obj.animation_data_clear()
        except Exception as rollback_exc:
            rollback_errors.append(f"restore NLA data: {rollback_exc}")
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.nla_patch failed: {exc}", transaction) from exc
    transaction.update(changed=bool(applied), after_track_count=after_tracks, after_strip_count=after_strips)
    if applied:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_nla(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "track", "strip", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    track_name = _name(params.get("track"), "track")
    strip_name = _optional_name(params.get("strip"), "strip")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    animation_data = obj.animation_data
    if animation_data is None:
        raise NotFoundError(f"Object has no NLA data: {obj.name}")
    tracks = animation_data.nla_tracks
    track = _find_named(tracks, track_name)
    if track is None:
        raise NotFoundError(f"NLA track not found: {track_name}")
    if strip_name is None:
        if len(track.strips):
            raise InvalidStateError("NLA track is not empty; remove its strips first")
        target = track.name
        before = {
            "name": track.name, "mute": bool(track.mute),
            "is_solo": bool(track.is_solo), "lock": bool(track.lock),
            "index": list(tracks).index(track), "strip_count": 0,
        }
        operation = "remove_track"
    else:
        strip = _find_named(track.strips, strip_name)
        if strip is None:
            raise NotFoundError(f"NLA strip not found: {track_name}:{strip_name}")
        target = f"{track.name}:{strip.name}"
        before = {**_strip_record(strip), "index": list(track.strips).index(strip)}
        operation = "remove_strip"
    transaction, apply = prepare_destructive_action(
        action="animation.nla_remove",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=[{
            "op": operation, "target": target,
            "before": before, "after": None, "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=True, state_token=current_state_token(bpy),
        extra={"object": obj.name, "track": track.name, "strip": strip_name},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        if strip_name is None:
            tracks.remove(track)
            removed = _find_named(tracks, track_name) is None
        else:
            track.strips.remove(strip)
            removed = _find_named(track.strips, strip_name) is None
        if not removed:
            raise RuntimeError("Blender did not remove the requested NLA item")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        transaction.update(changed=False)
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.nla_remove failed: {exc}", transaction) from exc
    transaction.update(changed=True, removed=operation)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def _driver_target(obj: Any, target_kind: str, bone_name: str | None) -> Any:
    if target_kind == "object":
        return obj
    if obj.type != "ARMATURE" or obj.pose is None:
        raise InvalidParamsError(f"Object has no pose bones: {obj.name}")
    bone = obj.pose.bones.get(bone_name)
    if bone is None:
        raise NotFoundError(f"Pose bone not found: {bone_name}")
    return bone


def _driver_record(curve: Any) -> dict[str, Any]:
    driver = curve.driver
    variables = []
    for variable in driver.variables:
        targets = []
        for target in variable.targets:
            targets.append(
                {
                    "id": target.id.name if target.id is not None else None,
                    "bone_target": str(getattr(target, "bone_target", "")),
                    "transform_type": str(getattr(target, "transform_type", "")),
                    "transform_space": str(getattr(target, "transform_space", "")),
                }
            )
        variables.append({"name": variable.name, "type": variable.type, "targets": targets})
    return {
        "data_path": str(curve.data_path),
        "array_index": int(curve.array_index),
        "is_valid": bool(curve.is_valid),
        "type": str(driver.type),
        "expression": str(driver.expression),
        "variables": variables,
    }


def inspect_drivers(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    limit, offset = _page(params, "animation.driver_inspect")
    bpy = get_bpy()
    obj = _scene_object(bpy, object_name)
    animation_data = obj.animation_data
    drivers = list(animation_data.drivers) if animation_data is not None else []
    if len(drivers) > MAX_DRIVERS:
        raise InvalidStateError(f"Object has more than {MAX_DRIVERS} drivers")
    ordered = sorted(drivers, key=lambda curve: (str(curve.data_path), int(curve.array_index)))
    page = ordered[offset:offset + limit]
    next_offset = offset + len(page)
    return {
        "object": obj.name,
        "driver_count": len(ordered),
        "items": [_driver_record(curve) for curve in page],
        "page": {"offset": offset, "limit": limit, "returned": len(page), "next_cursor": str(next_offset) if next_offset < len(ordered) else None},
        "state_token": current_state_token(bpy),
    }


def create_driver(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "target", "bone", "property", "index", "source_object", "source_bone", "source_transform", "source_space", "multiplier", "offset", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    target_kind = _name(params.get("target", "object"), "target").lower()
    if target_kind not in {"object", "pose_bone"}:
        raise InvalidParamsError("target must be object or pose_bone")
    bone_name = _name(params.get("bone"), "bone") if target_kind == "pose_bone" else None
    if target_kind == "object" and "bone" in params:
        raise InvalidParamsError("bone is valid only for a pose_bone target")
    property_name = _name(params.get("property"), "property")
    if property_name not in ANIMATED_PROPERTIES:
        raise InvalidParamsError(f"property must be one of: {', '.join(sorted(ANIMATED_PROPERTIES))}")
    array_index = _integer(params.get("index"), "index", 0, ANIMATED_PROPERTIES[property_name] - 1)
    source_object_name = _name(params.get("source_object"), "source_object")
    source_bone_name = _optional_name(params.get("source_bone"), "source_bone")
    source_transform = _enum(params.get("source_transform"), "source_transform", TRANSFORM_TYPES)
    source_space = _enum(params.get("source_space", "WORLD_SPACE"), "source_space", TRANSFORM_SPACES)
    multiplier = _number(params.get("multiplier", 1.0), "multiplier", -1_000_000.0, 1_000_000.0)
    offset = _number(params.get("offset", 0.0), "offset", -1_000_000_000.0, 1_000_000_000.0)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    target = _driver_target(obj, target_kind, bone_name)
    source_obj = _scene_object(bpy, source_object_name)
    if source_bone_name is not None:
        if source_obj.type != "ARMATURE" or source_obj.pose is None or source_obj.pose.bones.get(source_bone_name) is None:
            raise NotFoundError(f"Source pose bone not found: {source_object_name}:{source_bone_name}")
    data_path = target.path_from_id(property_name) if target_kind == "pose_bone" else property_name
    existing = [] if obj.animation_data is None else [curve for curve in obj.animation_data.drivers if curve.data_path == data_path and curve.array_index == array_index]
    if existing:
        raise InvalidStateError(f"Driver already exists: {data_path}[{array_index}]")
    expression = f"(source * {multiplier:.17g}) + ({offset:.17g})"
    journal = [{"op": "driver_create", "target": f"{data_path}[{array_index}]", "before": None, "after": {"source_object": source_obj.name, "source_bone": source_bone_name, "source_transform": source_transform, "source_space": source_space, "expression": expression}, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="animation.driver_create",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Object", "name": source_obj.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "data_path": data_path, "array_index": array_index},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    curve = None
    try:
        curve = target.driver_add(property_name, array_index)
        driver = curve.driver
        while driver.variables:
            driver.variables.remove(driver.variables[0])
        driver.type = "SCRIPTED"
        variable = driver.variables.new()
        variable.name = "source"
        variable.type = "TRANSFORMS"
        variable_target = variable.targets[0]
        variable_target.id = source_obj
        variable_target.bone_target = source_bone_name or ""
        variable_target.transform_type = source_transform
        variable_target.transform_space = source_space
        driver.expression = expression
        journal[0]["status"] = "applied"
    except Exception as exc:
        cleanup_errors = []
        if curve is not None:
            try:
                target.driver_remove(property_name, array_index)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove created driver: {cleanup_exc}")
        transaction.update(changed=bool(cleanup_errors), warnings=cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.driver_create failed: {exc}", transaction) from exc
    transaction.update(changed=True, driver=_driver_record(curve))
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_driver(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {
        "object_name", "target", "bone", "property", "index",
        "dry_run", "confirm", "expected_state",
    })
    object_name = _name(params.get("object_name"), "object_name")
    target_kind = _name(params.get("target", "object"), "target").lower()
    if target_kind not in {"object", "pose_bone"}:
        raise InvalidParamsError("target must be object or pose_bone")
    bone_name = _name(params.get("bone"), "bone") if target_kind == "pose_bone" else None
    if target_kind == "object" and "bone" in params:
        raise InvalidParamsError("bone is valid only for a pose_bone target")
    property_name = _name(params.get("property"), "property")
    if property_name not in ANIMATED_PROPERTIES:
        raise InvalidParamsError(f"property must be one of: {', '.join(sorted(ANIMATED_PROPERTIES))}")
    array_index = _integer(params.get("index"), "index", 0, ANIMATED_PROPERTIES[property_name] - 1)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    target = _driver_target(obj, target_kind, bone_name)
    data_path = target.path_from_id(property_name) if target_kind == "pose_bone" else property_name
    matches = [] if obj.animation_data is None else [
        curve for curve in obj.animation_data.drivers
        if curve.data_path == data_path and curve.array_index == array_index
    ]
    if not matches:
        raise NotFoundError(f"Driver not found: {data_path}[{array_index}]")
    if len(matches) != 1:
        raise InvalidStateError(f"Multiple drivers match: {data_path}[{array_index}]")
    record = _driver_record(matches[0])
    transaction, apply = prepare_destructive_action(
        action="animation.driver_remove",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=[{
            "op": "remove_driver", "target": f"{data_path}[{array_index}]",
            "before": record, "after": None, "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=True, state_token=current_state_token(bpy),
        extra={"object": obj.name, "data_path": data_path, "array_index": array_index},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        removed = bool(target.driver_remove(property_name, array_index))
        if not removed:
            raise RuntimeError("Blender did not remove the requested driver")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        transaction.update(changed=False)
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.driver_remove failed: {exc}", transaction) from exc
    transaction.update(changed=True, removed_driver=record)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def _shape_object(bpy: Any, name: str) -> Any:
    obj = _scene_object(bpy, name)
    if obj.type != "MESH" or obj.data is None:
        raise InvalidParamsError(f"Shape-key tools currently require a mesh object: {name}")
    return obj


def _shape_keys(obj: Any) -> Any | None:
    return getattr(obj.data, "shape_keys", None)


def _shape_key(obj: Any, name: str) -> Any:
    keys = _shape_keys(obj)
    key = keys.key_blocks.get(name) if keys is not None else None
    if key is None:
        raise NotFoundError(f"Shape key not found: {name}")
    return key


def _shape_record(key: Any) -> dict[str, Any]:
    return {
        "name": key.name,
        "value": float(key.value),
        "slider_min": float(key.slider_min),
        "slider_max": float(key.slider_max),
        "mute": bool(key.mute),
        "interpolation": str(key.interpolation),
        "relative_key": key.relative_key.name if key.relative_key is not None else None,
        "vertex_group": str(key.vertex_group),
        "point_count": len(key.data),
    }


def inspect_shape_keys(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    limit, offset = _page(params, "animation.shape_key_inspect")
    bpy = get_bpy()
    obj = _shape_object(bpy, object_name)
    keys = _shape_keys(obj)
    blocks = list(keys.key_blocks) if keys is not None else []
    if len(blocks) > MAX_SHAPE_KEYS:
        raise InvalidStateError(f"Object has more than {MAX_SHAPE_KEYS} shape keys")
    ordered = sorted(blocks, key=lambda key: key.name.casefold())
    page = ordered[offset:offset + limit]
    next_offset = offset + len(page)
    return {
        "object": obj.name,
        "shape_key_datablock": keys.name if keys is not None else None,
        "shape_key_count": len(ordered),
        "items": [_shape_record(key) for key in page],
        "page": {"offset": offset, "limit": limit, "returned": len(page), "next_cursor": str(next_offset) if next_offset < len(ordered) else None},
        "state_token": current_state_token(bpy),
    }


def create_shape_key(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "name", "from_mix", "relative_key", "vertex_group", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    name = _name(params.get("name"), "name")
    from_mix = _strict_bool(params.get("from_mix"), "from_mix", False)
    relative_name = _optional_name(params.get("relative_key"), "relative_key")
    vertex_group = _optional_name(params.get("vertex_group"), "vertex_group")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _shape_object(bpy, object_name)
    keys = _shape_keys(obj)
    if keys is None and name != "Basis":
        raise InvalidStateError("Create the Basis shape key before non-basis keys")
    if keys is not None and keys.key_blocks.get(name) is not None:
        raise InvalidStateError(f"Shape key already exists: {name}")
    if keys is not None and len(keys.key_blocks) >= MAX_SHAPE_KEYS:
        raise InvalidStateError(f"Object already has the maximum {MAX_SHAPE_KEYS} shape keys")
    relative = _shape_key(obj, relative_name) if relative_name is not None else None
    if vertex_group is not None and obj.vertex_groups.get(vertex_group) is None:
        raise NotFoundError(f"Vertex group not found: {vertex_group}")
    journal = [{"op": "shape_key_create", "target": name, "before": None, "after": {"from_mix": from_mix, "relative_key": relative_name, "vertex_group": vertex_group}, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="animation.shape_key_create",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": obj.data.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "shape_key": name},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _capture_context(bpy)
    key = None
    warnings: list[str] = []
    cleanup_errors: list[str] = []
    try:
        _select_only(bpy, obj)
        key = obj.shape_key_add(name=name, from_mix=from_mix)
        if key.name != name:
            raise RuntimeError(f"Blender changed requested shape-key name {name} to {key.name}")
        if relative is not None:
            key.relative_key = relative
        if vertex_group is not None:
            key.vertex_group = vertex_group
        journal[0]["status"] = "applied"
    except Exception as exc:
        if key is not None:
            try:
                obj.shape_key_remove(key)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove created shape key: {cleanup_exc}")
        _restore_context(bpy, snapshot, warnings)
        transaction.update(changed=bool(cleanup_errors), warnings=warnings + cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.shape_key_create failed: {exc}", transaction) from exc
    _restore_context(bpy, snapshot, warnings)
    transaction.update(changed=True, shape_key=_shape_record(key), warnings=warnings)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_shape_key(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "name", "max_points", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    name = _name(params.get("name"), "name")
    max_points = _integer(params.get("max_points", 250_000), "max_points", 1, MAX_SHAPE_POINTS)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _shape_object(bpy, object_name)
    keys = _shape_keys(obj)
    key = _shape_key(obj, name)
    if len(key.data) > max_points:
        raise InvalidStateError(f"Shape key has {len(key.data)} points; max_points is {max_points}")
    blocks = list(keys.key_blocks)
    if key.name == blocks[0].name and len(blocks) > 1:
        raise InvalidStateError("Basis cannot be removed while dependent shape keys exist")
    dependents = [
        block.name for block in blocks
        if block.name != key.name and block.relative_key.name == key.name
    ]
    if dependents:
        raise InvalidStateError(
            f"Shape key {name} is the relative key for {len(dependents)} keys: {', '.join(dependents[:10])}"
        )
    value_path = key.path_from_id("value")
    references: list[str] = []
    animation_data = getattr(keys, "animation_data", None)
    if animation_data is not None:
        from .animation import _action_fcurves

        actions = []
        if animation_data.action is not None:
            actions.append(animation_data.action)
        actions.extend(
            strip.action for track in animation_data.nla_tracks for strip in track.strips
            if getattr(strip, "action", None) is not None
        )
        for action in actions:
            for curve in _action_fcurves(action):
                if curve.data_path == value_path or curve.data_path.startswith(key.path_from_id()):
                    references.append(f"Action:{action.name}:{curve.data_path}")
        for curve in animation_data.drivers:
            if curve.data_path == value_path or curve.data_path.startswith(key.path_from_id()):
                references.append(f"Driver:{curve.data_path}[{curve.array_index}]")
    for source in (*bpy.data.objects, *bpy.data.shape_keys):
        source_animation = getattr(source, "animation_data", None)
        if source_animation is None:
            continue
        for curve in source_animation.drivers:
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id == keys and str(getattr(target, "data_path", "")).startswith(key.path_from_id()):
                        references.append(f"DriverTarget:{source.name}:{curve.data_path}")
    if references:
        raise InvalidStateError(
            f"Shape key {name} has {len(references)} animation/driver dependencies; "
            f"remove them first ({', '.join(references[:10])})"
        )
    record = _shape_record(key)
    transaction, apply = prepare_destructive_action(
        action="animation.shape_key_remove",
        affected_datablocks=[
            {"type": "Object", "name": obj.name},
            {"type": "Key", "name": keys.name},
        ],
        journal=[{
            "op": "remove_shape_key", "target": key.name,
            "before": record, "after": None, "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=True, state_token=current_state_token(bpy),
        extra={"object": obj.name, "shape_key": key.name, "point_count": len(key.data)},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _capture_context(bpy)
    warnings: list[str] = []
    try:
        _select_only(bpy, obj)
        obj.shape_key_remove(key)
        remaining = _shape_keys(obj)
        if remaining is not None and remaining.key_blocks.get(name) is not None:
            raise RuntimeError("Blender did not remove the requested shape key")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        _restore_context(bpy, snapshot, warnings)
        transaction.update(changed=False, warnings=warnings)
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.shape_key_remove failed: {exc}", transaction) from exc
    _restore_context(bpy, snapshot, warnings)
    transaction.update(changed=True, removed_shape_key=record, warnings=warnings)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def _shape_positions(value: Any, point_count: int) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_PATCH_POINTS:
        raise InvalidParamsError(f"positions must be an array with at most {MAX_PATCH_POINTS} items")
    result = []
    seen = set()
    for item_index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidParamsError(f"positions[{item_index}] must be an object")
        _unknown(item, {"index", "co"}, f"positions[{item_index}] fields")
        index = _integer(item.get("index"), f"positions[{item_index}].index", 0, point_count - 1)
        if index in seen:
            raise InvalidParamsError("positions must not repeat point indices")
        seen.add(index)
        result.append({"index": index, "co": _vector(item.get("co"), f"positions[{item_index}].co", 3)})
    return result


def patch_shape_key(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "name", "value", "slider_min", "slider_max", "mute", "interpolation", "relative_key", "vertex_group", "clear_vertex_group", "positions", "max_points", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    name = _name(params.get("name"), "name")
    max_points = _integer(params.get("max_points", 250_000), "max_points", 1, MAX_SHAPE_POINTS)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _shape_object(bpy, object_name)
    key = _shape_key(obj, name)
    if len(key.data) > max_points:
        raise InvalidStateError(f"Shape key has {len(key.data)} points; max_points is {max_points}")
    settings: dict[str, Any] = {}
    if "value" in params:
        settings["value"] = _number(params["value"], "value", -10.0, 10.0)
    if "slider_min" in params:
        settings["slider_min"] = _number(params["slider_min"], "slider_min", -10.0, 10.0)
    if "slider_max" in params:
        settings["slider_max"] = _number(params["slider_max"], "slider_max", -10.0, 10.0)
    if "mute" in params:
        settings["mute"] = _strict_bool(params["mute"], "mute")
    if "interpolation" in params:
        settings["interpolation"] = _enum(params["interpolation"], "interpolation", SHAPE_INTERPOLATIONS)
    if "relative_key" in params:
        relative_name = _name(params["relative_key"], "relative_key")
        relative = _shape_key(obj, relative_name)
        if relative is key:
            raise InvalidParamsError("A shape key cannot be relative to itself")
        cursor = relative
        seen = set()
        while cursor is not None and id(cursor) not in seen:
            if cursor is key:
                raise InvalidParamsError("relative_key would create a shape-key dependency cycle")
            seen.add(id(cursor))
            next_key = getattr(cursor, "relative_key", None)
            cursor = None if next_key is cursor else next_key
        settings["relative_key"] = relative
    if "vertex_group" in params:
        vertex_group = _name(params["vertex_group"], "vertex_group")
        if obj.vertex_groups.get(vertex_group) is None:
            raise NotFoundError(f"Vertex group not found: {vertex_group}")
        settings["vertex_group"] = vertex_group
    clear_vertex_group = _strict_bool(params.get("clear_vertex_group"), "clear_vertex_group", False)
    if clear_vertex_group:
        if "vertex_group" in params:
            raise InvalidParamsError("vertex_group and clear_vertex_group cannot be used together")
        settings["vertex_group"] = ""
    positions = _shape_positions(params.get("positions"), len(key.data))
    if not settings and not positions:
        raise InvalidParamsError("animation.shape_key_patch requires settings or positions")
    resulting_min = settings.get("slider_min", float(key.slider_min))
    resulting_max = settings.get("slider_max", float(key.slider_max))
    if resulting_max < resulting_min:
        raise InvalidParamsError("slider_max must be greater than or equal to slider_min")
    journal = [{"op": "shape_key_patch", "target": name, "before": None, "after": {"settings": {field: (value.name if field == "relative_key" and value is not None else value) for field, value in settings.items()}, "position_count": len(positions)}, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="animation.shape_key_patch",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Key", "name": obj.data.shape_keys.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=bool(positions),
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "shape_key": name, "position_count": len(positions)},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    before_settings = {field: getattr(key, field) for field in settings}
    before_positions = {item["index"]: key.data[item["index"]].co.copy() for item in positions}
    snapshot = _capture_context(bpy)
    warnings: list[str] = []
    try:
        _select_only(bpy, obj)
        if "slider_min" in settings or "slider_max" in settings:
            key.slider_min = min(float(key.slider_min), resulting_min, resulting_max)
            key.slider_max = max(float(key.slider_max), resulting_min, resulting_max)
            key.slider_min = resulting_min
            key.slider_max = resulting_max
        for field, value in settings.items():
            if field in {"slider_min", "slider_max"}:
                continue
            setattr(key, field, value)
        for item in positions:
            key.data[item["index"]].co = item["co"]
        journal[0]["status"] = "applied"
    except Exception as exc:
        rollback_errors = []
        try:
            if "slider_min" in before_settings or "slider_max" in before_settings:
                original_min = float(before_settings.get("slider_min", key.slider_min))
                original_max = float(before_settings.get("slider_max", key.slider_max))
                key.slider_min = min(float(key.slider_min), original_min, original_max)
                key.slider_max = max(float(key.slider_max), original_min, original_max)
                key.slider_min = original_min
                key.slider_max = original_max
            for field, value in before_settings.items():
                if field in {"slider_min", "slider_max"}:
                    continue
                setattr(key, field, value)
            for index, co in before_positions.items():
                key.data[index].co = co
        except Exception as rollback_exc:
            rollback_errors.append(f"restore shape key: {rollback_exc}")
        _restore_context(bpy, snapshot, warnings)
        transaction.update(changed=bool(rollback_errors), warnings=warnings + rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.shape_key_patch failed: {exc}", transaction) from exc
    _restore_context(bpy, snapshot, warnings)
    transaction.update(changed=True, shape_key=_shape_record(key), warnings=warnings)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
