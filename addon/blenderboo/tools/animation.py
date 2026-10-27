"""Bounded Action inspection, creation, keyframe editing, and validation."""

from __future__ import annotations

import math
import re
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


MAX_PAGE = 2000
MAX_FCURVES = 20_000
MAX_KEYFRAMES = 500_000
MAX_KEYS_PER_CURVE = 2000
MAX_OPERATIONS = 100
FRAME_MIN = -1_048_574
FRAME_MAX = 1_048_574
ANIMATED_PROPERTIES = {
    "location": 3,
    "rotation_euler": 3,
    "rotation_quaternion": 4,
    "rotation_axis_angle": 4,
    "scale": 3,
}
KEYFRAME_INTERPOLATIONS = frozenset({"CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD", "CUBIC", "QUART", "QUINT", "EXPO", "CIRC", "BACK", "BOUNCE", "ELASTIC"})
KEYFRAME_EASINGS = frozenset({"AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"})
KEYFRAME_HANDLE_TYPES = frozenset({"FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"})


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


def _page(params: dict[str, Any]) -> tuple[int, int]:
    limit = _integer(params.get("limit", 200), "limit", 1, MAX_PAGE)
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError("cursor must be a decimal string returned by animation.inspect")


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


def _action_for(bpy: Any, obj: Any, action_name: str | None) -> Any | None:
    if action_name is not None:
        action = bpy.data.actions.get(action_name)
        if action is None:
            raise NotFoundError(f"Action not found: {action_name}")
        return action
    animation_data = obj.animation_data
    return animation_data.action if animation_data is not None else None


def _action_fcurves(action: Any) -> list[Any]:
    """Collect legacy and layered Action curves without assuming one Blender version."""
    curves: list[Any] = []
    seen: set[int] = set()

    def add(collection: Any) -> None:
        if collection is None:
            return
        try:
            values = list(collection)
        except TypeError:
            return
        for curve in values:
            marker = id(curve)
            if marker not in seen:
                seen.add(marker)
                curves.append(curve)

    add(getattr(action, "fcurves", None))
    layers = getattr(action, "layers", None)
    if layers is not None:
        for layer in layers:
            strips = getattr(layer, "strips", None)
            if strips is None:
                continue
            for strip in strips:
                channelbags = getattr(strip, "channelbags", None)
                if channelbags is not None:
                    for channelbag in channelbags:
                        add(getattr(channelbag, "fcurves", None))
                    continue
                channelbag_method = getattr(strip, "channelbag", None)
                slots = getattr(action, "slots", None)
                if callable(channelbag_method) and slots is not None:
                    for slot in slots:
                        try:
                            channelbag = channelbag_method(slot, ensure=False)
                        except TypeError:
                            try:
                                channelbag = channelbag_method(slot)
                            except Exception:
                                continue
                        except Exception:
                            continue
                        if channelbag is not None:
                            add(getattr(channelbag, "fcurves", None))
    return curves


def _slot_records(action: Any) -> list[dict[str, Any]]:
    slots = getattr(action, "slots", None)
    if slots is None:
        return []
    records = []
    for slot in slots:
        records.append(
            {
                "identifier": str(getattr(slot, "identifier", getattr(slot, "handle", ""))),
                "name": str(getattr(slot, "name", "")),
                "target_id_type": str(getattr(slot, "target_id_type", getattr(slot, "id_type", ""))),
            }
        )
    return records


def _keyframe_record(point: Any) -> dict[str, Any]:
    co = point.co
    return {
        "frame": float(co[0]),
        "value": float(co[1]),
        "interpolation": str(point.interpolation),
        "keyframe_type": str(point.type),
    }


def _curve_record(curve: Any, include_keyframes: bool, max_keys: int) -> dict[str, Any]:
    points = curve.keyframe_points
    record = {
        "data_path": str(curve.data_path),
        "array_index": int(curve.array_index),
        "group": curve.group.name if curve.group is not None else None,
        "extrapolation": str(curve.extrapolation),
        "is_valid": bool(curve.is_valid),
        "keyframe_count": len(points),
    }
    if include_keyframes:
        count = min(len(points), max_keys)
        record["keyframes"] = [_keyframe_record(points[index]) for index in range(count)]
        record["keyframes_truncated"] = len(points) > count
    return record


def _action_summary(action: Any, curves: list[Any]) -> dict[str, Any]:
    total_keys = sum(len(curve.keyframe_points) for curve in curves)
    frame_range = getattr(action, "frame_range", None)
    return {
        "name": action.name,
        "users": int(action.users),
        "use_fake_user": bool(action.use_fake_user),
        "fcurve_count": len(curves),
        "keyframe_count": total_keys,
        "frame_range": [float(frame_range[0]), float(frame_range[1])] if frame_range is not None else None,
        "slots": _slot_records(action),
    }


def inspect_animation(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "action_name", "include_keyframes", "max_keys_per_curve", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    action_name = _optional_name(params.get("action_name"), "action_name")
    include_keyframes = _strict_bool(params.get("include_keyframes"), "include_keyframes", False)
    max_keys = _integer(params.get("max_keys_per_curve", 200), "max_keys_per_curve", 1, MAX_KEYS_PER_CURVE)
    limit, offset = _page(params)
    bpy = get_bpy()
    obj = _scene_object(bpy, object_name)
    action = _action_for(bpy, obj, action_name)
    animation_data = obj.animation_data
    if action is None:
        return {
            "object": obj.name,
            "has_animation_data": animation_data is not None,
            "action": None,
            "items": [],
            "page": {"offset": 0, "limit": limit, "returned": 0, "next_cursor": None},
            "state_token": current_state_token(bpy),
        }
    curves = _action_fcurves(action)
    if len(curves) > MAX_FCURVES:
        raise InvalidStateError(f"Action has {len(curves)} F-Curves; maximum supported is {MAX_FCURVES}")
    total_keys = sum(len(curve.keyframe_points) for curve in curves)
    if total_keys > MAX_KEYFRAMES:
        raise InvalidStateError(f"Action has {total_keys} keyframes; maximum supported is {MAX_KEYFRAMES}")
    ordered = sorted(curves, key=lambda curve: (str(curve.data_path), int(curve.array_index)))
    page = ordered[offset:offset + limit]
    next_offset = offset + len(page)
    active_slot = getattr(animation_data, "action_slot", None) if animation_data is not None else None
    return {
        "object": obj.name,
        "assigned_action": animation_data.action.name if animation_data is not None and animation_data.action is not None else None,
        "active_slot": str(getattr(active_slot, "identifier", getattr(active_slot, "handle", ""))) if active_slot is not None else None,
        "action": _action_summary(action, curves),
        "items": [_curve_record(curve, include_keyframes, max_keys) for curve in page],
        "page": {"offset": offset, "limit": limit, "returned": len(page), "next_cursor": str(next_offset) if next_offset < len(ordered) else None},
        "state_token": current_state_token(bpy),
    }


def create_action(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "name", "assign", "replace", "fake_user", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    name = _name(params.get("name"), "name")
    assign = _strict_bool(params.get("assign"), "assign", True)
    replace = _strict_bool(params.get("replace"), "replace", False)
    fake_user = _strict_bool(params.get("fake_user"), "fake_user", False)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    if bpy.data.actions.get(name) is not None:
        raise InvalidStateError(f"Action already exists: {name}")
    previous = obj.animation_data.action if obj.animation_data is not None else None
    if assign and previous is not None and not replace:
        raise InvalidStateError(f"Object already has action {previous.name}; set replace true to assign a new action")
    replacement = assign and previous is not None
    journal = [{"op": "create_action", "target": name, "before": None, "after": {"assign_to": obj.name if assign else None, "fake_user": fake_user}, "status": "planned"}]
    if replacement:
        journal.append({"op": "assign_action", "target": obj.name, "before": previous.name, "after": name, "status": "planned"})
    transaction, apply = prepare_destructive_action(
        action="animation.action_create",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Action", "name": name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=replacement,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "action": name, "assign": assign, "replaces": previous.name if replacement else None},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    action = None
    created_animation_data = obj.animation_data is None
    try:
        action = bpy.data.actions.new(name)
        action.use_fake_user = fake_user
        journal[0]["status"] = "applied"
        if assign:
            animation_data = obj.animation_data_create()
            animation_data.action = action
            if animation_data.action is not action:
                raise RuntimeError("Blender did not assign the created Action")
            if replacement:
                journal[1]["status"] = "applied"
    except Exception as exc:
        cleanup_errors = []
        try:
            if assign and obj.animation_data is not None:
                obj.animation_data.action = previous
                if created_animation_data and previous is None:
                    obj.animation_data_clear()
        except Exception as cleanup_exc:
            cleanup_errors.append(f"restore animation data: {cleanup_exc}")
        if action is not None and bpy.data.actions.get(action.name) is action:
            try:
                action.use_fake_user = False
                if action.users == 0:
                    bpy.data.actions.remove(action)
                else:
                    cleanup_errors.append(f"created action still has {action.users} users")
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove created action: {cleanup_exc}")
        transaction.update(changed=bool(cleanup_errors), warnings=cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.action_create failed: {exc}", transaction) from exc
    transaction.update(changed=True, action=action.name, assigned=assign)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def assign_action(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "action_name", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    action_name = _optional_name(params.get("action_name"), "action_name")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    action = None
    if action_name is not None:
        action = bpy.data.actions.get(action_name)
        if action is None:
            raise NotFoundError(f"Action not found: {action_name}")
    animation_data = obj.animation_data
    previous = animation_data.action if animation_data is not None else None
    if previous is action:
        return {
            "action": "animation.action_assign", "changed": False,
            "object": obj.name, "previous_action": action_name,
            "assigned_action": action_name, "state_token": current_state_token(bpy),
        }
    transaction, apply = prepare_destructive_action(
        action="animation.action_assign",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=[{
            "op": "assign_action", "target": obj.name,
            "before": previous.name if previous is not None else None,
            "after": action.name if action is not None else None, "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=previous is not None,
        state_token=current_state_token(bpy),
        extra={
            "object": obj.name,
            "previous_action": previous.name if previous is not None else None,
            "assigned_action": action.name if action is not None else None,
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    created_animation_data = animation_data is None
    try:
        if animation_data is None:
            animation_data = obj.animation_data_create()
        animation_data.action = action
        if animation_data.action is not action:
            raise RuntimeError("Blender did not assign the requested Action")
        transaction["journal"][0]["status"] = "applied"
        if action is None and created_animation_data and not animation_data.nla_tracks and not animation_data.drivers:
            obj.animation_data_clear()
    except Exception as exc:
        cleanup_errors = []
        try:
            if obj.animation_data is None:
                animation_data = obj.animation_data_create()
            else:
                animation_data = obj.animation_data
            animation_data.action = previous
            if created_animation_data and previous is None:
                obj.animation_data_clear()
        except Exception as cleanup_exc:
            cleanup_errors.append(f"restore action assignment: {cleanup_exc}")
        transaction.update(changed=bool(cleanup_errors), warnings=cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.action_assign failed: {exc}", transaction) from exc
    transaction.update(changed=True)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def remove_action(params: dict[str, Any]) -> dict[str, Any]:
    """Remove one unused Action after proving no active or NLA references remain."""
    _unknown(params, {"name", "dry_run", "confirm", "expected_state"})
    name = _name(params.get("name"), "name")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    action = bpy.data.actions.get(name)
    if action is None:
        raise NotFoundError(f"Action not found: {name}")
    references: list[str] = []
    for obj in bpy.data.objects:
        animation_data = getattr(obj, "animation_data", None)
        if animation_data is None:
            continue
        if animation_data.action is action:
            references.append(f"Object:{obj.name}:active")
        for track in animation_data.nla_tracks:
            for strip in track.strips:
                if getattr(strip, "action", None) is action:
                    references.append(f"Object:{obj.name}:NLA:{track.name}:{strip.name}")
    if references:
        sample = ", ".join(references[:10])
        raise InvalidStateError(
            f"Action {name} still has {len(references)} object/NLA references; "
            f"unassign them first ({sample})"
        )
    if action.users > int(bool(action.use_fake_user)):
        raise InvalidStateError(f"Action {name} has unsupported non-object users: {action.users}")
    summary = _action_summary(action, _action_fcurves(action))
    transaction, apply = prepare_destructive_action(
        action="animation.action_remove",
        affected_datablocks=[{"type": "Action", "name": action.name}],
        journal=[{
            "op": "remove_action", "target": action.name,
            "before": summary, "after": None, "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=True, state_token=current_state_token(bpy),
        extra={"action_name": action.name, **summary},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        action.use_fake_user = False
        bpy.data.actions.remove(action)
        if bpy.data.actions.get(name) is not None:
            raise RuntimeError("Blender did not remove the Action")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        transaction.update(changed=bpy.data.actions.get(name) is None)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.action_remove failed: {exc}", transaction) from exc
    transaction.update(changed=True)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def bake_pose(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {
        "object_name", "name", "frame_start", "frame_end", "step", "bones",
        "visual_keying", "max_bones", "max_samples", "dry_run", "expected_state",
    })
    object_name = _name(params.get("object_name"), "object_name")
    name = _name(params.get("name"), "name")
    frame_start = _integer(params.get("frame_start"), "frame_start", FRAME_MIN, FRAME_MAX)
    frame_end = _integer(params.get("frame_end"), "frame_end", FRAME_MIN, FRAME_MAX)
    if frame_end < frame_start:
        raise InvalidParamsError("frame_end must be greater than or equal to frame_start")
    step = _integer(params.get("step", 1), "step", 1, 1000)
    visual_keying = _strict_bool(params.get("visual_keying"), "visual_keying", True)
    max_bones = _integer(params.get("max_bones", 512), "max_bones", 1, MAX_FCURVES)
    max_samples = _integer(params.get("max_samples", 100_000), "max_samples", 1, MAX_KEYFRAMES)
    raw_bones = params.get("bones")
    if raw_bones is not None:
        if not isinstance(raw_bones, list) or not raw_bones or len(raw_bones) > max_bones:
            raise InvalidParamsError(f"bones must contain 1 to {max_bones} unique names")
        bone_names = [_name(value, f"bones[{index}]") for index, value in enumerate(raw_bones)]
        if len(bone_names) != len(set(bone_names)):
            raise InvalidParamsError("bones must not contain duplicates")
    else:
        bone_names = None
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    if obj.type != "ARMATURE" or obj.pose is None:
        raise InvalidParamsError(f"Object has no pose bones: {obj.name}")
    selected_names = [bone.name for bone in obj.pose.bones] if bone_names is None else bone_names
    if len(selected_names) > max_bones:
        raise InvalidStateError(f"Bake selects {len(selected_names)} bones; max_bones is {max_bones}")
    missing = [bone for bone in selected_names if obj.pose.bones.get(bone) is None]
    if missing:
        raise NotFoundError(f"Pose bones not found: {', '.join(missing[:10])}")
    frame_count = ((frame_end - frame_start) // step) + 1
    sample_count = frame_count * len(selected_names)
    if sample_count > max_samples:
        raise InvalidStateError(f"Pose bake requires {sample_count} bone-frame samples; max_samples is {max_samples}")
    if bpy.data.actions.get(name) is not None:
        raise InvalidStateError(f"Action already exists: {name}")
    previous = obj.animation_data.action if obj.animation_data is not None else None
    transaction, apply = prepare_destructive_action(
        action="animation.pose_bake",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Action", "name": name}],
        journal=[{
            "op": "bake_pose", "target": name, "before": None,
            "after": {
                "frame_start": frame_start, "frame_end": frame_end, "step": step,
                "bone_count": len(selected_names), "visual_keying": visual_keying,
            },
            "status": "planned",
        }],
        dry_run=params.get("dry_run"), require_confirm=False,
        state_token=current_state_token(bpy),
        extra={
            "object": obj.name, "action": name, "frame_count": frame_count,
            "bone_count": len(selected_names), "sample_count": sample_count,
            "replaces_active_action": previous.name if previous is not None else None,
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    from .skinning import _capture_context, _leave_current_mode, _restore_context, _select_only

    context = _capture_context(bpy)
    previous_frame = int(bpy.context.scene.frame_current)
    previous_subframe = float(bpy.context.scene.frame_subframe)
    bone_selection = {bone.name: bool(bone.select) for bone in obj.pose.bones}
    active_bone = obj.data.bones.active.name if obj.data.bones.active is not None else None
    created_animation_data = obj.animation_data is None
    action = None
    warnings: list[str] = []
    cleanup_errors: list[str] = []
    try:
        action = bpy.data.actions.new(name)
        animation_data = obj.animation_data_create()
        animation_data.action = action
        _select_only(bpy, obj)
        for pose_bone in obj.pose.bones:
            pose_bone.select = pose_bone.name in selected_names
        obj.data.bones.active = obj.data.bones.get(selected_names[0]) if selected_names else None
        bpy.ops.object.mode_set(mode="POSE")
        result = bpy.ops.nla.bake(
            frame_start=frame_start,
            frame_end=frame_end,
            step=step,
            only_selected=bone_names is not None,
            visual_keying=visual_keying,
            clear_constraints=False,
            clear_parents=False,
            use_current_action=True,
            clean_curves=False,
            bake_types={"POSE"},
        )
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender pose bake returned {sorted(result)}")
        _leave_current_mode(bpy)
        curves = _action_fcurves(action)
        key_count = sum(len(curve.keyframe_points) for curve in curves)
        if not curves or not key_count:
            raise RuntimeError("Pose bake created no F-Curves or keyframes")
        if len(curves) > MAX_FCURVES or key_count > MAX_KEYFRAMES:
            raise InvalidStateError("Pose bake result exceeds the Action budget")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        try:
            _leave_current_mode(bpy)
            if obj.animation_data is not None:
                obj.animation_data.action = previous
                if created_animation_data and previous is None:
                    obj.animation_data_clear()
            if action is not None and bpy.data.actions.get(action.name) is action:
                action.use_fake_user = False
                if action.users == 0:
                    bpy.data.actions.remove(action)
        except Exception as cleanup_exc:
            cleanup_errors.append(f"remove failed pose bake: {cleanup_exc}")
        try:
            bpy.context.scene.frame_set(previous_frame, subframe=previous_subframe)
            for pose_bone in obj.pose.bones:
                pose_bone.select = bone_selection.get(pose_bone.name, False)
            obj.data.bones.active = obj.data.bones.get(active_bone) if active_bone is not None else None
        except Exception as cleanup_exc:
            cleanup_errors.append(f"restore bake frame/bone selection: {cleanup_exc}")
        _restore_context(bpy, context, warnings)
        transaction.update(changed=bool(cleanup_errors), warnings=warnings + cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.pose_bake failed: {exc}", transaction) from exc
    bpy.context.scene.frame_set(previous_frame, subframe=previous_subframe)
    for pose_bone in obj.pose.bones:
        pose_bone.select = bone_selection.get(pose_bone.name, False)
    obj.data.bones.active = obj.data.bones.get(active_bone) if active_bone is not None else None
    _restore_context(bpy, context, warnings)
    transaction.update(
        changed=True, action=_action_summary(action, curves),
        keyframe_count=key_count, warnings=warnings,
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def retarget_action(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {
        "source_object", "source_action", "target_object", "name", "bone_map",
        "assign", "replace", "require_all_bones", "dry_run", "confirm", "expected_state",
    })
    source_object_name = _name(params.get("source_object"), "source_object")
    source_action_name = _name(params.get("source_action"), "source_action")
    target_object_name = _name(params.get("target_object"), "target_object")
    name = _name(params.get("name"), "name")
    assign = _strict_bool(params.get("assign"), "assign", True)
    replace = _strict_bool(params.get("replace"), "replace", False)
    require_all = _strict_bool(params.get("require_all_bones"), "require_all_bones", True)
    raw_map = params.get("bone_map", {})
    if not isinstance(raw_map, dict) or len(raw_map) > 512:
        raise InvalidParamsError("bone_map must be an object with at most 512 entries")
    bone_map = {_name(key, "bone_map key"): _name(value, f"bone_map.{key}") for key, value in raw_map.items()}
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    source_obj = _scene_object(bpy, source_object_name)
    target_obj = _scene_object(bpy, target_object_name)
    if source_obj.type != "ARMATURE" or source_obj.pose is None:
        raise InvalidParamsError(f"Source object has no pose bones: {source_obj.name}")
    if target_obj.type != "ARMATURE" or target_obj.pose is None:
        raise InvalidParamsError(f"Target object has no pose bones: {target_obj.name}")
    source_action = bpy.data.actions.get(source_action_name)
    if source_action is None:
        raise NotFoundError(f"Action not found: {source_action_name}")
    if bpy.data.actions.get(name) is not None:
        raise InvalidStateError(f"Action already exists: {name}")
    for source_bone, target_bone in bone_map.items():
        if source_obj.pose.bones.get(source_bone) is None:
            raise NotFoundError(f"Source pose bone not found: {source_bone}")
        if target_obj.pose.bones.get(target_bone) is None:
            raise NotFoundError(f"Target pose bone not found: {target_bone}")
    prefix_to_name = {bone.path_from_id(): bone.name for bone in source_obj.pose.bones}
    target_prefixes = {bone.name: bone.path_from_id() for bone in target_obj.pose.bones}
    curves = _action_fcurves(source_action)
    animated_bones: set[str] = set()
    curve_bones: dict[int, str] = {}
    pattern = re.compile(r'^pose\.bones\["(?:\\.|[^"\\])*"\]')
    for curve in curves:
        match = pattern.match(str(curve.data_path))
        if match is None:
            continue
        source_bone = prefix_to_name.get(match.group(0))
        if source_bone is None:
            raise InvalidStateError(f"Cannot resolve animated source bone path: {curve.data_path}")
        animated_bones.add(source_bone)
        curve_bones[id(curve)] = source_bone
    resolved_map = dict(bone_map)
    for source_bone in animated_bones:
        if source_bone not in resolved_map and target_obj.pose.bones.get(source_bone) is not None:
            resolved_map[source_bone] = source_bone
    unmapped = sorted(animated_bones - set(resolved_map))
    if require_all and unmapped:
        raise InvalidStateError(f"Animated source bones are not mapped: {', '.join(unmapped[:10])}")
    previous = target_obj.animation_data.action if target_obj.animation_data is not None else None
    if assign and previous is not None and not replace:
        raise InvalidStateError(f"Target already has action {previous.name}; set replace true")
    replacement = assign and previous is not None
    transaction, apply = prepare_destructive_action(
        action="animation.action_retarget",
        affected_datablocks=[
            {"type": "Object", "name": source_obj.name},
            {"type": "Object", "name": target_obj.name},
            {"type": "Action", "name": name},
        ],
        journal=[{
            "op": "retarget_action", "target": name, "before": None,
            "after": {
                "source_action": source_action.name, "mapped_bones": len(resolved_map),
                "animated_bones": len(animated_bones), "assign_to": target_obj.name if assign else None,
            },
            "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=replacement, state_token=current_state_token(bpy),
        extra={
            "source_object": source_obj.name, "source_action": source_action.name,
            "target_object": target_obj.name, "action": name,
            "animated_bone_count": len(animated_bones), "mapped_bone_count": len(resolved_map),
            "unmapped_bones": unmapped, "assign": assign,
            "replaces": previous.name if replacement else None,
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    copied = None
    created_animation_data = target_obj.animation_data is None
    try:
        copied = source_action.copy()
        copied.name = name
        if copied.name != name:
            raise RuntimeError(f"Blender changed requested Action name {name} to {copied.name}")
        copied_curves = _action_fcurves(copied)
        if len(copied_curves) != len(curves):
            raise RuntimeError("Copied Action F-Curve count changed")
        remapped_count = 0
        for source_curve, copied_curve in zip(curves, copied_curves):
            source_bone = curve_bones.get(id(source_curve))
            if source_bone is None:
                target_obj.path_resolve(copied_curve.data_path)
                continue
            target_bone = resolved_map.get(source_bone)
            if target_bone is None:
                copied_curve.mute = True
                continue
            source_prefix = source_obj.pose.bones[source_bone].path_from_id()
            copied_curve.data_path = target_prefixes[target_bone] + str(source_curve.data_path)[len(source_prefix):]
            target_obj.path_resolve(copied_curve.data_path)
            remapped_count += 1
        if assign:
            target_obj.animation_data_create().action = copied
            if target_obj.animation_data.action is not copied:
                raise RuntimeError("Blender did not assign the retargeted Action")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        cleanup_errors = []
        try:
            if assign and target_obj.animation_data is not None:
                target_obj.animation_data.action = previous
                if created_animation_data and previous is None:
                    target_obj.animation_data_clear()
            if copied is not None and bpy.data.actions.get(copied.name) is copied:
                copied.use_fake_user = False
                if copied.users == 0:
                    bpy.data.actions.remove(copied)
        except Exception as cleanup_exc:
            cleanup_errors.append(f"remove failed retarget Action: {cleanup_exc}")
        transaction.update(changed=bool(cleanup_errors), warnings=cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.action_retarget failed: {exc}", transaction) from exc
    transaction.update(
        changed=True, action=_action_summary(copied, copied_curves),
        remapped_curve_count=remapped_count,
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def _key_operation(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError(f"operations[{index}] must be an object")
    allowed = {"op", "target", "bone", "property", "index", "frame", "value", "group", "interpolation", "easing", "handle_left_type", "handle_right_type"}
    _unknown(value, allowed, f"operations[{index}] fields")
    op = _name(value.get("op"), f"operations[{index}].op").lower()
    if op not in {"set", "remove"}:
        raise InvalidParamsError(f"operations[{index}].op must be set or remove")
    target = _name(value.get("target", "object"), f"operations[{index}].target").lower()
    if target not in {"object", "pose_bone"}:
        raise InvalidParamsError(f"operations[{index}].target must be object or pose_bone")
    bone = _name(value.get("bone"), f"operations[{index}].bone") if target == "pose_bone" else None
    if target == "object" and "bone" in value:
        raise InvalidParamsError(f"operations[{index}].bone is only valid for pose_bone targets")
    property_name = _name(value.get("property"), f"operations[{index}].property")
    if property_name not in ANIMATED_PROPERTIES:
        raise InvalidParamsError(f"operations[{index}].property must be one of: {', '.join(sorted(ANIMATED_PROPERTIES))}")
    array_index = _integer(value.get("index"), f"operations[{index}].index", 0, ANIMATED_PROPERTIES[property_name] - 1)
    frame = _number(value.get("frame"), f"operations[{index}].frame", FRAME_MIN, FRAME_MAX)
    result = {"op": op, "target": target, "bone": bone, "property": property_name, "index": array_index, "frame": frame}
    if op == "set":
        if "value" not in value:
            raise InvalidParamsError(f"operations[{index}].value is required for set")
        result["value"] = _number(value["value"], f"operations[{index}].value", -1_000_000_000.0, 1_000_000_000.0)
    elif "value" in value:
        raise InvalidParamsError(f"operations[{index}].value is not valid for remove")
    if "group" in value:
        result["group"] = _name(value["group"], f"operations[{index}].group")
    style_fields = {
        "interpolation": KEYFRAME_INTERPOLATIONS,
        "easing": KEYFRAME_EASINGS,
        "handle_left_type": KEYFRAME_HANDLE_TYPES,
        "handle_right_type": KEYFRAME_HANDLE_TYPES,
    }
    for field, choices in style_fields.items():
        if field in value:
            if op != "set":
                raise InvalidParamsError(f"operations[{index}].{field} is only valid for set")
            setting = _name(value[field], f"operations[{index}].{field}").upper()
            if setting not in choices:
                raise InvalidParamsError(
                    f"operations[{index}].{field} must be one of: {', '.join(sorted(choices))}"
                )
            result[field] = setting
    return result


def _style_inserted_keyframe(action: Any, target: Any, operation: dict[str, Any]) -> None:
    fields = ("interpolation", "easing", "handle_left_type", "handle_right_type")
    if not any(field in operation for field in fields):
        return
    data_path = target.path_from_id(operation["property"])
    matches = [
        curve for curve in _action_fcurves(action)
        if curve.data_path == data_path and int(curve.array_index) == operation["index"]
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Could not uniquely resolve inserted F-Curve {data_path}[{operation['index']}]")
    points = [
        point for point in matches[0].keyframe_points
        if math.isclose(float(point.co[0]), operation["frame"], abs_tol=1e-6)
    ]
    if len(points) != 1:
        raise RuntimeError(f"Could not uniquely resolve inserted keyframe at {operation['frame']:g}")
    point = points[0]
    for field in fields:
        if field in operation:
            setattr(point, field, operation[field])


def _key_operations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_OPERATIONS:
        raise InvalidParamsError(f"operations must contain 1 to {MAX_OPERATIONS} items")
    return [_key_operation(item, index) for index, item in enumerate(value)]


def _key_target(obj: Any, operation: dict[str, Any]) -> Any:
    if operation["target"] == "object":
        return obj
    if obj.type != "ARMATURE" or obj.pose is None:
        raise InvalidParamsError(f"Object has no pose bones: {obj.name}")
    pose_bone = obj.pose.bones.get(operation["bone"])
    if pose_bone is None:
        raise NotFoundError(f"Pose bone not found: {operation['bone']}")
    return pose_bone


def _operation_target_label(operation: dict[str, Any]) -> str:
    owner = operation["bone"] if operation["target"] == "pose_bone" else "object"
    return f"{owner}:{operation['property']}[{operation['index']}]@{operation['frame']:g}"


def edit_keyframes(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "operations", "expected_action", "make_single_user", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    operations = _key_operations(params.get("operations"))
    expected_action = _optional_name(params.get("expected_action"), "expected_action")
    make_single_user = _strict_bool(params.get("make_single_user"), "make_single_user", False)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    animation_data = obj.animation_data
    action = animation_data.action if animation_data is not None else None
    if action is None:
        raise InvalidStateError("Object has no assigned Action; create and assign one first")
    if expected_action is not None and action.name != expected_action:
        raise InvalidStateError(f"expected_action {expected_action} does not match {action.name}")
    if action.users > 1 and not make_single_user:
        raise InvalidStateError("Action is shared; set make_single_user true to edit only this object's copy")
    for operation in operations:
        _key_target(obj, operation)
    destructive = any(operation["op"] == "remove" for operation in operations)
    journal = [{"op": f"keyframe_{operation['op']}", "target": _operation_target_label(operation), "before": None, "after": operation.get("value"), "status": "planned"} for operation in operations]
    transaction, apply = prepare_destructive_action(
        action="animation.keyframe_edit",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Action", "name": action.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=destructive,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "action": action.name, "action_users": int(action.users), "make_single_user": make_single_user},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    working = None
    assigned_working = False
    changed = False
    original_name = action.name
    try:
        working = action.copy()
        working.name = f"{action.name} Working"
        animation_data.action = working
        assigned_working = animation_data.action is working
        if not assigned_working:
            raise RuntimeError("Blender did not assign the working Action copy")
        for entry, operation in zip(journal, operations):
            target = _key_target(obj, operation)
            kwargs = {
                "data_path": operation["property"],
                "index": operation["index"],
                "frame": operation["frame"],
            }
            if operation["op"] == "set":
                current = list(getattr(target, operation["property"]))
                entry["before"] = float(current[operation["index"]])
                updated = list(current)
                updated[operation["index"]] = operation["value"]
                setattr(target, operation["property"], updated)
                try:
                    if "group" in operation:
                        kwargs["group"] = operation["group"]
                    result = bool(target.keyframe_insert(**kwargs))
                finally:
                    setattr(target, operation["property"], current)
                if not result:
                    raise RuntimeError(f"Blender did not insert {_operation_target_label(operation)}")
                _style_inserted_keyframe(working, target, operation)
                changed = True
                entry["status"] = "applied"
            else:
                result = bool(target.keyframe_delete(**kwargs))
                if result:
                    changed = True
                    entry["status"] = "applied"
                else:
                    entry["status"] = "present"
        obj.update_tag()
        curves = _action_fcurves(working)
        total_keys = sum(len(curve.keyframe_points) for curve in curves)
        if len(curves) > MAX_FCURVES or total_keys > MAX_KEYFRAMES:
            raise InvalidStateError("Result exceeds the Action F-Curve or keyframe budget")
    except Exception as exc:
        cleanup_errors = []
        if assigned_working:
            try:
                animation_data.action = action
                assigned_working = animation_data.action is working
            except Exception as cleanup_exc:
                cleanup_errors.append(f"restore original action: {cleanup_exc}")
        if working is not None and not assigned_working and bpy.data.actions.get(working.name) is working:
            try:
                working.use_fake_user = False
                if working.users == 0:
                    bpy.data.actions.remove(working)
                else:
                    cleanup_errors.append(f"working action still has {working.users} users")
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove working action: {cleanup_exc}")
        transaction.update(changed=assigned_working or bool(cleanup_errors), warnings=cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"animation.keyframe_edit failed: {exc}", transaction) from exc
    if not changed:
        animation_data.action = action
        working.use_fake_user = False
        if working.users == 0:
            working.use_fake_user = False
            bpy.data.actions.remove(working)
        else:
            transaction["warnings"].append(f"unused working action still has {working.users} users")
        transaction.update(changed=False, result_action=action.name)
        transaction["state_token"] = current_state_token(bpy)
        return transaction
    transaction.update(changed=True, result_action=working.name, source_action=action.name)
    transaction["affected_datablocks"][1]["name"] = working.name
    if action.users == 0 and not action.use_fake_user:
        try:
            bpy.data.actions.remove(action)
            working.name = original_name
            transaction["result_action"] = working.name
            transaction["affected_datablocks"][1]["name"] = working.name
            transaction["source_action"] = None
        except Exception as cleanup_exc:
            transaction["warnings"].append(f"remove superseded action: {cleanup_exc}")
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def validate_animation(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "action_name", "frame_start", "frame_end"})
    object_name = _name(params.get("object_name"), "object_name")
    action_name = _optional_name(params.get("action_name"), "action_name")
    bpy = get_bpy()
    obj = _scene_object(bpy, object_name)
    action = _action_for(bpy, obj, action_name)
    animation_data = obj.animation_data
    nla_tracks = animation_data.nla_tracks if animation_data is not None else []
    nla_strips = [strip for track in nla_tracks for strip in track.strips]
    drivers = list(animation_data.drivers) if animation_data is not None else []
    shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    start = _number(params.get("frame_start", bpy.context.scene.frame_start), "frame_start", FRAME_MIN, FRAME_MAX)
    end = _number(params.get("frame_end", bpy.context.scene.frame_end), "frame_end", FRAME_MIN, FRAME_MAX)
    if end < start:
        raise InvalidParamsError("frame_end must be greater than or equal to frame_start")
    issues: list[dict[str, Any]] = []
    if action is None:
        if not nla_strips and not drivers and (shape_keys is None or len(shape_keys.key_blocks) <= 1):
            issues.append({"severity": "error", "code": "missing_animation", "message": f"Object {obj.name} has no assigned Action, NLA strips, or drivers"})
        curves = []
    else:
        curves = _action_fcurves(action)
        if len(curves) > MAX_FCURVES:
            raise InvalidStateError(f"Action has {len(curves)} F-Curves; maximum supported is {MAX_FCURVES}")
        total_keys = sum(len(curve.keyframe_points) for curve in curves)
        if total_keys > MAX_KEYFRAMES:
            raise InvalidStateError(f"Action has {total_keys} keyframes; maximum supported is {MAX_KEYFRAMES}")
        for curve in curves:
            label = f"{curve.data_path}[{curve.array_index}]"
            if not curve.is_valid:
                issues.append({"severity": "error", "code": "invalid_fcurve", "target": label, "message": "Blender marks this F-Curve invalid"})
            try:
                obj.path_resolve(curve.data_path)
            except Exception:
                issues.append({"severity": "error", "code": "unresolved_data_path", "target": label, "message": "The object cannot resolve this F-Curve data path"})
            frames = [float(point.co[0]) for point in curve.keyframe_points]
            seen: set[float] = set()
            duplicates = set()
            for frame in frames:
                if frame in seen:
                    duplicates.add(frame)
                seen.add(frame)
            if duplicates:
                issues.append({"severity": "warning", "code": "duplicate_keyframes", "target": label, "frames": sorted(duplicates), "message": "Multiple keyframes occupy the same frame"})
            outside = [frame for frame in frames if frame < start or frame > end]
            if outside:
                issues.append({"severity": "warning", "code": "keys_outside_range", "target": label, "count": len(outside), "message": f"Keyframes exist outside validation range {start:g}..{end:g}"})
        if action_name is None and animation_data is not None:
            slots = _slot_records(action)
            active_slot = getattr(animation_data, "action_slot", None)
            if slots and active_slot is None:
                issues.append({"severity": "error", "code": "missing_action_slot", "message": "Layered Action has slots but the object has no active Action slot"})
    for track in nla_tracks:
        for strip in track.strips:
            label = f"{track.name}:{strip.name}"
            if float(strip.frame_end) <= float(strip.frame_start):
                issues.append({"severity": "error", "code": "invalid_nla_range", "target": label, "message": "NLA strip end frame is not after its start frame"})
            if str(strip.type) == "CLIP" and strip.action is None:
                issues.append({"severity": "error", "code": "missing_nla_action", "target": label, "message": "NLA clip strip has no Action"})
    for curve in drivers:
        label = f"{curve.data_path}[{curve.array_index}]"
        if not curve.is_valid:
            issues.append({"severity": "error", "code": "invalid_driver", "target": label, "message": "Blender marks this driver F-Curve invalid"})
        for variable in curve.driver.variables:
            for target_index, target in enumerate(variable.targets):
                target_label = f"{label}:{variable.name}[{target_index}]"
                target_id = target.id
                if target_id is None:
                    issues.append({"severity": "error", "code": "missing_driver_target", "target": target_label, "message": "Driver variable target has no ID"})
                    continue
                bone_target = str(getattr(target, "bone_target", ""))
                if bone_target:
                    bones = getattr(getattr(target_id, "data", None), "bones", None)
                    if bones is None or bones.get(bone_target) is None:
                        issues.append({"severity": "error", "code": "missing_driver_bone", "target": target_label, "message": f"Driver target bone is missing: {bone_target}"})
    if shape_keys is not None:
        blocks = list(shape_keys.key_blocks)
        if len(blocks) > 1000:
            raise InvalidStateError("Object has more than 1000 shape keys; validation budget exceeded")
        expected_points = len(obj.data.vertices) if obj.type == "MESH" else None
        for key in blocks:
            if expected_points is not None and len(key.data) != expected_points:
                issues.append({"severity": "error", "code": "shape_key_point_mismatch", "target": key.name, "message": "Shape-key point count does not match mesh vertex count"})
            if key.vertex_group and obj.vertex_groups.get(key.vertex_group) is None:
                issues.append({"severity": "error", "code": "missing_shape_key_group", "target": key.name, "message": f"Shape-key vertex group is missing: {key.vertex_group}"})
            cursor = key.relative_key
            seen: set[int] = set()
            while cursor is not None and id(cursor) not in seen:
                if cursor is key:
                    issues.append({"severity": "error", "code": "shape_key_cycle", "target": key.name, "message": "Shape-key relative dependency contains a cycle"})
                    break
                seen.add(id(cursor))
                next_key = getattr(cursor, "relative_key", None)
                cursor = None if next_key is cursor else next_key
    errors = sum(issue["severity"] == "error" for issue in issues)
    warnings = sum(issue["severity"] == "warning" for issue in issues)
    return {
        "object": obj.name,
        "action": action.name if action is not None else None,
        "frame_range": [start, end],
        "fcurve_count": len(curves),
        "nla_track_count": len(nla_tracks),
        "nla_strip_count": len(nla_strips),
        "driver_count": len(drivers),
        "shape_key_count": len(shape_keys.key_blocks) if shape_keys is not None else 0,
        "issue_count": len(issues),
        "error_count": errors,
        "warning_count": warnings,
        "valid": errors == 0,
        "issues": issues,
        "state_token": current_state_token(bpy),
    }
