"""Bounded armature binding, vertex-weight editing, inspection, and validation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


MAX_PAGE = 2000
MAX_VERTICES = 5_000_000
MAX_GROUPS = 10_000
MAX_OPERATIONS = 100
MAX_WEIGHT_VERTICES = 250_000
MAX_AUTO_WEIGHT_ENTRIES = 5_000_000
MAX_AUTO_WEIGHT_PAIRS = 100_000_000
WEIGHT_OPERATIONS = frozenset({"SET", "ADD", "SUBTRACT", "REMOVE"})


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
    raise InvalidParamsError("cursor must be a decimal string returned by skin.inspect")


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _mesh_object(bpy: Any, name: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    if obj.type != "MESH" or obj.data is None:
        raise InvalidParamsError(f"Object is not a mesh: {name}")
    return obj


def _armature_object(bpy: Any, name: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    if obj.type != "ARMATURE" or obj.data is None:
        raise InvalidParamsError(f"Object is not an armature: {name}")
    return obj


def _armature_modifiers(obj: Any) -> list[Any]:
    return [modifier for modifier in obj.modifiers if modifier.type == "ARMATURE"]


def _resolve_armature(bpy: Any, obj: Any, armature_name: str | None) -> tuple[Any | None, Any | None]:
    modifiers = _armature_modifiers(obj)
    if armature_name is not None:
        armature = _armature_object(bpy, armature_name)
        matching = [modifier for modifier in modifiers if getattr(modifier, "object", None) is armature]
        return armature, matching[0] if matching else None
    targeted = [modifier for modifier in modifiers if getattr(modifier, "object", None) is not None]
    if len(targeted) == 1:
        return targeted[0].object, targeted[0]
    if len(targeted) > 1:
        raise InvalidStateError("Mesh has multiple targeted Armature modifiers; provide armature_name")
    return None, modifiers[0] if len(modifiers) == 1 else None


def _budget(obj: Any, max_vertices: int) -> None:
    if len(obj.data.vertices) > max_vertices:
        raise InvalidStateError(
            f"Mesh has {len(obj.data.vertices)} vertices; max_vertices is {max_vertices}"
        )
    if len(obj.vertex_groups) > MAX_GROUPS:
        raise InvalidStateError(f"Mesh has more than {MAX_GROUPS} vertex groups")


@dataclass
class _ObjectContext:
    mode: str
    selected: list[Any]
    active: Any
    frame: int
    subframe: float


def _capture_context(bpy: Any) -> _ObjectContext:
    return _ObjectContext(
        mode=str(bpy.context.mode),
        selected=list(bpy.context.selected_objects),
        active=bpy.context.view_layer.objects.active,
        frame=int(bpy.context.scene.frame_current),
        subframe=float(bpy.context.scene.frame_subframe),
    )


def _operator_mode(mode: str) -> str:
    return {
        "EDIT_MESH": "EDIT", "EDIT_CURVE": "EDIT", "EDIT_SURFACE": "EDIT",
        "EDIT_TEXT": "EDIT", "EDIT_ARMATURE": "EDIT", "EDIT_METABALL": "EDIT",
        "PAINT_WEIGHT": "WEIGHT_PAINT", "PAINT_VERTEX": "VERTEX_PAINT",
        "PAINT_TEXTURE": "TEXTURE_PAINT", "PARTICLE": "PARTICLE_EDIT",
    }.get(mode, mode)


def _leave_current_mode(bpy: Any) -> None:
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def _select_only(bpy: Any, obj: Any) -> None:
    _leave_current_mode(bpy)
    for selected in list(bpy.context.selected_objects):
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _restore_context(bpy: Any, snapshot: _ObjectContext, warnings: list[str]) -> None:
    try:
        _leave_current_mode(bpy)
    except Exception as exc:
        warnings.append(f"restore Object mode: {exc}")
    try:
        for selected in list(bpy.context.selected_objects):
            selected.select_set(False)
        scene_objects = bpy.context.scene.objects
        for selected in snapshot.selected:
            if scene_objects.get(selected.name) is selected:
                selected.select_set(True)
        active = snapshot.active
        bpy.context.view_layer.objects.active = (
            active if active is not None and scene_objects.get(active.name) is active else None
        )
    except Exception as exc:
        warnings.append(f"restore object selection: {exc}")
    try:
        scene = bpy.context.scene
        if scene.frame_current != snapshot.frame or abs(scene.frame_subframe - snapshot.subframe) > 1e-8:
            scene.frame_set(snapshot.frame, subframe=snapshot.subframe)
    except Exception as exc:
        warnings.append(f"restore frame {snapshot.frame}+{snapshot.subframe}: {exc}")
    if snapshot.mode != "OBJECT":
        try:
            if bpy.context.view_layer.objects.active is None:
                raise RuntimeError("original active object is no longer available")
            bpy.ops.object.mode_set(mode=_operator_mode(snapshot.mode))
        except Exception as exc:
            warnings.append(f"restore {snapshot.mode} mode: {exc}")


def _weight_stats(obj: Any) -> tuple[dict[int, dict[str, Any]], int, int]:
    stats = {
        group.index: {"vertex_count": 0, "weight_sum": 0.0, "minimum": None, "maximum": None}
        for group in obj.vertex_groups
    }
    unweighted = 0
    non_normalized = 0
    for vertex in obj.data.vertices:
        total = 0.0
        for membership in vertex.groups:
            item = stats.get(membership.group)
            if item is None:
                continue
            weight = float(membership.weight)
            total += weight
            item["vertex_count"] += 1
            item["weight_sum"] += weight
            item["minimum"] = weight if item["minimum"] is None else min(item["minimum"], weight)
            item["maximum"] = weight if item["maximum"] is None else max(item["maximum"], weight)
        if total <= 1e-8:
            unweighted += 1
        elif abs(total - 1.0) > 1e-4:
            non_normalized += 1
    return stats, unweighted, non_normalized


def inspect_skin(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "armature_name", "max_vertices", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    armature_name = _optional_name(params.get("armature_name"), "armature_name")
    max_vertices = _integer(params.get("max_vertices", 250_000), "max_vertices", 1, MAX_VERTICES)
    limit, offset = _page(params)
    bpy = get_bpy()
    obj = _mesh_object(bpy, object_name)
    _budget(obj, max_vertices)
    armature, modifier = _resolve_armature(bpy, obj, armature_name)
    bone_names = {bone.name for bone in armature.data.bones} if armature is not None else set()
    stats, unweighted, non_normalized = _weight_stats(obj)
    groups = sorted(obj.vertex_groups, key=lambda group: group.name.casefold())
    page = groups[offset:offset + limit]
    items = []
    for group in page:
        item = stats[group.index]
        count = item["vertex_count"]
        items.append(
            {
                "name": group.name,
                "index": int(group.index),
                "matches_bone": group.name in bone_names if armature is not None else None,
                "vertex_count": count,
                "weight_min": item["minimum"],
                "weight_max": item["maximum"],
                "weight_mean": item["weight_sum"] / count if count else None,
            }
        )
    next_offset = offset + len(page)
    return {
        "object": obj.name,
        "mesh": obj.data.name,
        "vertex_count": len(obj.data.vertices),
        "vertex_group_count": len(groups),
        "unweighted_vertex_count": unweighted,
        "non_normalized_vertex_count": non_normalized,
        "armature": armature.name if armature is not None else None,
        "modifier": modifier.name if modifier is not None else None,
        "items": items,
        "page": {"offset": offset, "limit": limit, "returned": len(items), "next_cursor": str(next_offset) if next_offset < len(groups) else None},
        "state_token": current_state_token(bpy),
    }


def bind_skin(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "armature_name", "modifier_name", "preserve_volume", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    armature_name = _name(params.get("armature_name"), "armature_name")
    modifier_name = _name(params.get("modifier_name", "Armature"), "modifier_name")
    preserve_volume = _strict_bool(params.get("preserve_volume"), "preserve_volume", False)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name)
    armature = _armature_object(bpy, armature_name)
    if obj is armature:
        raise InvalidParamsError("A mesh cannot bind to itself")
    if obj.modifiers.get(modifier_name) is not None:
        raise InvalidStateError(f"Modifier already exists: {modifier_name}")
    existing = [modifier for modifier in _armature_modifiers(obj) if getattr(modifier, "object", None) is armature]
    if existing:
        raise InvalidStateError(f"Mesh is already bound to {armature.name} through modifier {existing[0].name}")
    journal = [{"op": "add_armature_modifier", "target": modifier_name, "before": None, "after": {"object": armature.name, "preserve_volume": preserve_volume}, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="skin.bind",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Armature", "name": armature.data.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "armature": armature.name, "modifier": modifier_name},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _capture_context(bpy)
    modifier = None
    warnings: list[str] = []
    cleanup_errors: list[str] = []
    try:
        _select_only(bpy, obj)
        modifier = obj.modifiers.new(modifier_name, "ARMATURE")
        if modifier.name != modifier_name:
            raise RuntimeError(f"Blender changed requested modifier name {modifier_name} to {modifier.name}")
        modifier.object = armature
        modifier.use_deform_preserve_volume = preserve_volume
        if modifier.object is not armature:
            raise RuntimeError("Blender did not assign the Armature modifier target")
        journal[0]["status"] = "applied"
    except Exception as exc:
        if modifier is not None and obj.modifiers.get(modifier.name) is modifier:
            try:
                obj.modifiers.remove(modifier)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove created modifier: {cleanup_exc}")
        _restore_context(bpy, snapshot, warnings)
        transaction.update(changed=bool(cleanup_errors), warnings=warnings + cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"skin.bind failed: {exc}", transaction) from exc
    _restore_context(bpy, snapshot, warnings)
    transaction.update(changed=True, modifier=modifier.name, warnings=warnings)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


@dataclass
class _BoneContext:
    selected: set[str]
    active: str | None
    edit_selection: dict[str, tuple[bool, bool, bool]] | None
    edit_active: str | None


@dataclass
class _VertexGroupState:
    name: str
    lock_weight: bool
    weights: list[tuple[int, float]]


@dataclass
class _WeightSnapshot:
    groups: list[_VertexGroupState]
    active_index: int
    entry_count: int


def unbind_skin(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {
        "object_name", "armature_name", "modifier_name",
        "dry_run", "confirm", "expected_state",
    })
    object_name = _name(params.get("object_name"), "object_name")
    armature_name = _optional_name(params.get("armature_name"), "armature_name")
    modifier_name = _optional_name(params.get("modifier_name"), "modifier_name")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name)
    candidates = _armature_modifiers(obj)
    if armature_name is not None:
        armature = _armature_object(bpy, armature_name)
        candidates = [item for item in candidates if getattr(item, "object", None) is armature]
    if modifier_name is not None:
        candidates = [item for item in candidates if item.name == modifier_name]
    if not candidates:
        raise NotFoundError("No matching Armature modifier was found")
    if len(candidates) != 1:
        raise InvalidStateError("Multiple Armature modifiers match; provide armature_name and modifier_name")
    modifier = candidates[0]
    armature = getattr(modifier, "object", None)
    index = list(obj.modifiers).index(modifier)
    settings = {
        "name": modifier.name,
        "object": armature,
        "use_deform_preserve_volume": bool(modifier.use_deform_preserve_volume),
        "use_vertex_groups": bool(modifier.use_vertex_groups),
        "use_bone_envelopes": bool(modifier.use_bone_envelopes),
        "invert_vertex_group": bool(modifier.invert_vertex_group),
        "vertex_group": str(modifier.vertex_group),
    }
    transaction, apply = prepare_destructive_action(
        action="skin.unbind",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=[{
            "op": "remove_armature_modifier", "target": modifier.name,
            "before": {
                **{key: value for key, value in settings.items() if key != "object"},
                "object": armature.name if armature is not None else None,
                "index": index,
            },
            "after": None, "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=True, state_token=current_state_token(bpy),
        extra={
            "object": obj.name, "modifier": modifier.name,
            "armature": armature.name if armature is not None else None,
            "vertex_groups_preserved": True,
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        obj.modifiers.remove(modifier)
        if obj.modifiers.get(settings["name"]) is not None:
            raise RuntimeError("Blender did not remove the Armature modifier")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        cleanup_errors = []
        if obj.modifiers.get(settings["name"]) is None:
            try:
                restored = obj.modifiers.new(settings["name"], "ARMATURE")
                for field, value in settings.items():
                    if field != "name":
                        setattr(restored, field, value)
                obj.modifiers.move(len(obj.modifiers) - 1, index)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"restore Armature modifier: {cleanup_exc}")
        transaction.update(changed=bool(cleanup_errors), warnings=cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"skin.unbind failed: {exc}", transaction) from exc
    transaction.update(changed=True)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def _pose_bone_selected(armature: Any, name: str) -> bool:
    pose_bone = armature.pose.bones.get(name) if armature.pose is not None else None
    if pose_bone is not None and hasattr(pose_bone, "select"):
        return bool(pose_bone.select)
    bone = armature.data.bones.get(name)
    return bool(getattr(bone, "select", False)) if bone is not None else False


def _set_pose_bone_selected(armature: Any, name: str, selected: bool) -> None:
    pose_bone = armature.pose.bones.get(name) if armature.pose is not None else None
    if pose_bone is not None and hasattr(pose_bone, "select"):
        pose_bone.select = selected
    bone = armature.data.bones.get(name)
    if bone is not None and hasattr(bone, "select"):
        bone.select = selected


def _capture_bone_context(bpy: Any, armature: Any) -> _BoneContext:
    selected = {
        bone.name for bone in armature.data.bones
        if _pose_bone_selected(armature, bone.name)
    }
    active = armature.data.bones.active
    edit_selection = None
    edit_active = None
    if bpy.context.mode == "EDIT_ARMATURE" and bpy.context.view_layer.objects.active is armature:
        edit_selection = {
            bone.name: (bool(bone.select), bool(bone.select_head), bool(bone.select_tail))
            for bone in armature.data.edit_bones
        }
        edit_bone = armature.data.edit_bones.active
        edit_active = edit_bone.name if edit_bone is not None else None
    return _BoneContext(
        selected=selected,
        active=active.name if active is not None else None,
        edit_selection=edit_selection,
        edit_active=edit_active,
    )


def _restore_pose_bone_context(armature: Any, snapshot: _BoneContext, warnings: list[str]) -> None:
    try:
        for bone in armature.data.bones:
            _set_pose_bone_selected(armature, bone.name, bone.name in snapshot.selected)
        armature.data.bones.active = (
            armature.data.bones.get(snapshot.active) if snapshot.active is not None else None
        )
    except Exception as exc:
        warnings.append(f"restore armature bone selection: {exc}")


def _restore_edit_bone_context(bpy: Any, armature: Any, snapshot: _BoneContext, warnings: list[str]) -> None:
    if snapshot.edit_selection is None:
        return
    try:
        if bpy.context.mode != "EDIT_ARMATURE" or bpy.context.view_layer.objects.active is not armature:
            raise RuntimeError("original armature Edit mode was not restored")
        for bone in armature.data.edit_bones:
            state = snapshot.edit_selection.get(bone.name)
            if state is not None:
                bone.select, bone.select_head, bone.select_tail = state
        armature.data.edit_bones.active = (
            armature.data.edit_bones.get(snapshot.edit_active)
            if snapshot.edit_active is not None else None
        )
    except Exception as exc:
        warnings.append(f"restore Edit-mode bone selection: {exc}")


def _count_weight_entries(obj: Any, max_entries: int, label: str) -> int:
    total = 0
    for vertex in obj.data.vertices:
        total += len(vertex.groups)
        if total > max_entries:
            raise InvalidStateError(
                f"{label} exceeds max_weight_entries {max_entries}"
            )
    return total


def _snapshot_weights(obj: Any, max_entries: int) -> _WeightSnapshot:
    states = [
        _VertexGroupState(group.name, bool(group.lock_weight), [])
        for group in obj.vertex_groups
    ]
    by_index = {group.index: states[index] for index, group in enumerate(obj.vertex_groups)}
    entry_count = 0
    for vertex in obj.data.vertices:
        for membership in vertex.groups:
            state = by_index.get(membership.group)
            if state is None:
                raise InvalidStateError(
                    f"Vertex {vertex.index} references missing vertex-group index {membership.group}"
                )
            entry_count += 1
            if entry_count > max_entries:
                raise InvalidStateError(
                    f"Existing weights exceed max_weight_entries {max_entries}"
                )
            state.weights.append((int(vertex.index), float(membership.weight)))
    return _WeightSnapshot(
        groups=states,
        active_index=int(obj.vertex_groups.active_index),
        entry_count=entry_count,
    )


def _vertex_chunks(vertex_count: int, size: int = 10_000):
    for start in range(0, vertex_count, size):
        yield list(range(start, min(start + size, vertex_count)))


def _restore_weights(obj: Any, snapshot: _WeightSnapshot) -> list[str]:
    errors: list[str] = []
    expected_names = [state.name for state in snapshot.groups]
    expected_set = set(expected_names)
    try:
        for group in sorted(
            [group for group in obj.vertex_groups if group.name not in expected_set],
            key=lambda item: item.index,
            reverse=True,
        ):
            obj.vertex_groups.remove(group)
    except Exception as exc:
        errors.append(f"remove automatic-weight groups: {exc}")
    if [group.name for group in obj.vertex_groups] != expected_names:
        errors.append("restore original vertex-group names and order: group layout changed")
    for state in snapshot.groups:
        group = obj.vertex_groups.get(state.name)
        if group is None:
            errors.append(f"restore vertex group {state.name}: group is missing")
            continue
        try:
            group.lock_weight = False
            for vertices in _vertex_chunks(len(obj.data.vertices)):
                group.remove(vertices)
            for vertex, weight in state.weights:
                group.add([vertex], weight, "REPLACE")
            group.lock_weight = state.lock_weight
        except Exception as exc:
            errors.append(f"restore vertex group {state.name}: {exc}")
    try:
        if snapshot.groups:
            obj.vertex_groups.active_index = snapshot.active_index
    except Exception as exc:
        errors.append(f"restore active vertex group: {exc}")
    return errors


def _restore_auto_weight_context(
    bpy: Any,
    obj: Any,
    object_context: _ObjectContext,
    armature: Any,
    bone_context: _BoneContext,
    active_group_index: int,
) -> list[str]:
    warnings: list[str] = []
    try:
        _leave_current_mode(bpy)
    except Exception as exc:
        warnings.append(f"restore Object mode before rig context: {exc}")
    _restore_pose_bone_context(armature, bone_context, warnings)
    try:
        if len(obj.vertex_groups):
            obj.vertex_groups.active_index = active_group_index
    except Exception as exc:
        warnings.append(f"restore active vertex group: {exc}")
    _restore_context(bpy, object_context, warnings)
    _restore_edit_bone_context(bpy, armature, bone_context, warnings)
    return warnings


def _deform_weight_stats(obj: Any, deform_bones: set[str]) -> tuple[int, int, int]:
    relevant = {
        group.index for group in obj.vertex_groups if group.name in deform_bones
    }
    weighted = 0
    unweighted = 0
    non_normalized = 0
    for vertex in obj.data.vertices:
        total = sum(
            float(membership.weight)
            for membership in vertex.groups
            if membership.group in relevant
        )
        if total <= 1e-8:
            unweighted += 1
        else:
            weighted += 1
            if abs(total - 1.0) > 1e-4:
                non_normalized += 1
    return weighted, unweighted, non_normalized


def automatic_weights(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(
        params,
        {
            "object_name", "armature_name", "max_vertices", "max_weight_entries",
            "max_candidate_pairs", "require_all_weighted", "dry_run", "confirm",
            "expected_state",
        },
    )
    object_name = _name(params.get("object_name"), "object_name")
    armature_name = _name(params.get("armature_name"), "armature_name")
    max_vertices = _integer(
        params.get("max_vertices", 250_000), "max_vertices", 1, MAX_VERTICES
    )
    max_weight_entries = _integer(
        params.get("max_weight_entries", 250_000),
        "max_weight_entries", 1, MAX_AUTO_WEIGHT_ENTRIES,
    )
    max_candidate_pairs = _integer(
        params.get("max_candidate_pairs", 5_000_000),
        "max_candidate_pairs", 1, MAX_AUTO_WEIGHT_PAIRS,
    )
    require_all_weighted = _strict_bool(
        params.get("require_all_weighted"), "require_all_weighted", True
    )
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name)
    armature = _armature_object(bpy, armature_name)
    _budget(obj, max_vertices)
    if not len(obj.data.vertices):
        raise InvalidStateError("Cannot calculate automatic weights for an empty mesh")
    if obj.data.users != 1:
        raise InvalidStateError(
            f"Mesh data {obj.data.name} has {obj.data.users} object users; make it single-user first"
        )
    resolved_armature, modifier = _resolve_armature(bpy, obj, armature_name)
    if resolved_armature is not armature or modifier is None:
        raise InvalidStateError(
            f"No Armature modifier on {obj.name} targets {armature.name}; call skin.bind first"
        )
    deform_bones = {bone.name for bone in armature.data.bones if bone.use_deform}
    if not deform_bones:
        raise InvalidStateError(f"Armature {armature.name} has no deform bones")
    candidate_pairs = len(obj.data.vertices) * len(deform_bones)
    if candidate_pairs > max_candidate_pairs:
        raise InvalidStateError(
            f"Automatic weights require up to {candidate_pairs} vertex/bone candidate pairs; "
            f"max_candidate_pairs is {max_candidate_pairs}"
        )
    existing_entries = _count_weight_entries(
        obj, max_weight_entries, "Existing weight-entry count"
    )
    existing_names = {group.name for group in obj.vertex_groups}
    new_group_names = sorted(deform_bones - existing_names)
    if len(existing_names | deform_bones) > MAX_GROUPS:
        raise InvalidStateError(f"Automatic weights would exceed {MAX_GROUPS} vertex groups")
    journal = [{
        "op": "automatic_bone_weights",
        "target": obj.name,
        "armature": armature.name,
        "deform_bone_count": len(deform_bones),
        "candidate_pair_count": candidate_pairs,
        "existing_weight_entry_count": existing_entries,
        "new_group_count": len(new_group_names),
        "status": "planned",
    }]
    transaction, apply = prepare_destructive_action(
        action="skin.auto_weights",
        affected_datablocks=[
            {"type": "Object", "name": obj.name},
            {"type": "Mesh", "name": obj.data.name},
            {"type": "Armature", "name": armature.data.name},
        ],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={
            "object": obj.name,
            "armature": armature.name,
            "modifier": modifier.name,
            "vertex_count": len(obj.data.vertices),
            "deform_bone_count": len(deform_bones),
            "candidate_pair_count": candidate_pairs,
            "existing_weight_entry_count": existing_entries,
            "new_groups": new_group_names,
            "max_weight_entries": max_weight_entries,
            "max_candidate_pairs": max_candidate_pairs,
            "require_all_weighted": require_all_weighted,
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    object_context = _capture_context(bpy)
    bone_context = _capture_bone_context(bpy, armature)
    weight_snapshot = _snapshot_weights(obj, max_weight_entries)
    restore_warnings: list[str] = []
    rollback_errors: list[str] = []
    try:
        _select_only(bpy, obj)
        armature.select_set(True)
        for bone in armature.data.bones:
            _set_pose_bone_selected(armature, bone.name, bone.name in deform_bones)
        selected_deform = {
            name for name in deform_bones if _pose_bone_selected(armature, name)
        }
        if selected_deform != deform_bones:
            missing = sorted(deform_bones - selected_deform)
            raise RuntimeError(
                f"Could not select {len(missing)} deform bones: {', '.join(missing[:10])}"
            )
        armature.data.bones.active = armature.data.bones.get(sorted(deform_bones)[0])
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="WEIGHT_PAINT")
        if not bpy.ops.paint.weight_from_bones.poll():
            raise RuntimeError("Blender automatic bone-weight operator is unavailable in prepared context")
        result = bpy.ops.paint.weight_from_bones(type="AUTOMATIC")
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender automatic bone weights returned {sorted(result)}")
        journal[0]["status"] = "operator_finished"
        _leave_current_mode(bpy)
        result_entries = _count_weight_entries(
            obj, max_weight_entries, "Automatic-weight result entry count"
        )
        missing_groups = sorted(deform_bones - {group.name for group in obj.vertex_groups})
        if missing_groups:
            raise RuntimeError(
                f"Automatic weights did not create {len(missing_groups)} deform groups: "
                f"{', '.join(missing_groups[:10])}"
            )
        weighted, unweighted, non_normalized = _deform_weight_stats(obj, deform_bones)
        if not weighted:
            raise RuntimeError("Automatic weights did not assign any effective deform weight")
        if require_all_weighted and unweighted:
            raise RuntimeError(
                f"Automatic weights left {unweighted} of {len(obj.data.vertices)} vertices unweighted"
            )
        if non_normalized:
            raise RuntimeError(
                f"Automatic weights left {non_normalized} vertices with non-normalized deform weights"
            )
        restore_warnings = _restore_auto_weight_context(
            bpy, obj, object_context, armature, bone_context, weight_snapshot.active_index
        )
        if restore_warnings:
            raise RuntimeError("Automatic weights applied but original Blender context was not fully restored")
        journal[0]["status"] = "applied"
    except Exception as exc:
        try:
            _leave_current_mode(bpy)
        except Exception as rollback_exc:
            rollback_errors.append(f"leave mutation mode for rollback: {rollback_exc}")
        rollback_errors.extend(_restore_weights(obj, weight_snapshot))
        retry_warnings = _restore_auto_weight_context(
            bpy, obj, object_context, armature, bone_context, weight_snapshot.active_index
        )
        warnings = restore_warnings + retry_warnings + rollback_errors
        rolled_back = not rollback_errors and not retry_warnings
        journal[0]["status"] = "rolled_back" if rolled_back else "rollback_incomplete"
        transaction.update(
            changed=not rolled_back,
            rolled_back=rolled_back,
            warnings=warnings,
        )
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"skin.auto_weights failed: {exc}", transaction) from exc
    created_groups = sorted({group.name for group in obj.vertex_groups} - existing_names)
    transaction.update(
        changed=True,
        created_groups=created_groups,
        result_weight_entry_count=result_entries,
        weighted_vertex_count=weighted,
        unweighted_vertex_count=unweighted,
        non_normalized_vertex_count=non_normalized,
        warnings=[],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _weight_operation(value: Any, index: int, vertex_count: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError(f"operations[{index}] must be an object")
    _unknown(value, {"op", "vertices", "weight"}, f"operations[{index}] fields")
    op = _name(value.get("op"), f"operations[{index}].op").upper()
    if op not in WEIGHT_OPERATIONS:
        raise InvalidParamsError(f"operations[{index}].op must be one of: {', '.join(sorted(WEIGHT_OPERATIONS))}")
    vertices = value.get("vertices")
    if not isinstance(vertices, list) or not vertices or len(vertices) > MAX_WEIGHT_VERTICES:
        raise InvalidParamsError(f"operations[{index}].vertices must contain 1 to {MAX_WEIGHT_VERTICES} indices")
    normalized = [_integer(item, f"operations[{index}].vertices", 0, vertex_count - 1) for item in vertices]
    if len(normalized) != len(set(normalized)):
        raise InvalidParamsError(f"operations[{index}].vertices must not contain duplicates")
    result: dict[str, Any] = {"op": op, "vertices": normalized}
    if op == "REMOVE":
        if "weight" in value:
            raise InvalidParamsError(f"operations[{index}].weight is not valid for REMOVE")
    else:
        result["weight"] = _number(value.get("weight"), f"operations[{index}].weight", 0.0, 1.0)
    return result


def patch_weights(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "group_name", "create_group", "operations", "expected_vertex_count", "max_vertices", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    group_name = _name(params.get("group_name"), "group_name")
    create_group = _strict_bool(params.get("create_group"), "create_group", False)
    max_vertices = _integer(params.get("max_vertices", 250_000), "max_vertices", 1, MAX_VERTICES)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name)
    _budget(obj, max_vertices)
    vertex_count = len(obj.data.vertices)
    expected_count = None if "expected_vertex_count" not in params else _integer(params["expected_vertex_count"], "expected_vertex_count", 0, MAX_VERTICES)
    if expected_count is not None and expected_count != vertex_count:
        raise InvalidStateError(f"expected_vertex_count {expected_count} does not match {vertex_count}")
    raw = params.get("operations")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OPERATIONS:
        raise InvalidParamsError(f"operations must contain 1 to {MAX_OPERATIONS} items")
    if vertex_count == 0:
        raise InvalidStateError("Cannot patch weights on a mesh with no vertices")
    operations = [_weight_operation(item, index, vertex_count) for index, item in enumerate(raw)]
    affected = sorted({vertex for operation in operations for vertex in operation["vertices"]})
    if len(affected) > MAX_WEIGHT_VERTICES:
        raise InvalidParamsError(f"operations affect {len(affected)} unique vertices; maximum is {MAX_WEIGHT_VERTICES}")
    group = obj.vertex_groups.get(group_name)
    if group is None and not create_group:
        raise NotFoundError(f"Vertex group not found: {group_name}; set create_group true to create it")
    destructive = any(operation["op"] in {"SET", "SUBTRACT", "REMOVE"} for operation in operations)
    journal = [{"op": f"weight_{operation['op'].lower()}", "target": group_name, "vertex_count": len(operation["vertices"]), "weight": operation.get("weight"), "status": "planned"} for operation in operations]
    transaction, apply = prepare_destructive_action(
        action="skin.weights_patch",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": obj.data.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=destructive,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "group": group_name, "create_group": create_group, "affected_vertex_count": len(affected)},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _capture_context(bpy)
    warnings: list[str] = []
    rollback_errors: list[str] = []
    created = False
    before: dict[int, float | None] = {}
    try:
        _select_only(bpy, obj)
        if group is None:
            group = obj.vertex_groups.new(name=group_name)
            created = True
            if group.name != group_name:
                raise RuntimeError(f"Blender changed requested vertex-group name {group_name} to {group.name}")
        for vertex in affected:
            try:
                before[vertex] = float(group.weight(vertex))
            except RuntimeError:
                before[vertex] = None
        for entry, operation in zip(journal, operations):
            if operation["op"] == "REMOVE":
                group.remove(operation["vertices"])
            else:
                mode = "REPLACE" if operation["op"] == "SET" else operation["op"]
                group.add(operation["vertices"], operation["weight"], mode)
            entry["status"] = "applied"
    except Exception as exc:
        try:
            _leave_current_mode(bpy)
            if group is not None and obj.vertex_groups.get(group.name) is group:
                if created:
                    obj.vertex_groups.remove(group)
                else:
                    group.remove(affected)
                    for vertex, weight in before.items():
                        if weight is not None:
                            group.add([vertex], weight, "REPLACE")
        except Exception as rollback_exc:
            rollback_errors.append(f"restore vertex weights: {rollback_exc}")
        _restore_context(bpy, snapshot, warnings)
        transaction.update(changed=bool(rollback_errors), warnings=warnings + rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"skin.weights_patch failed: {exc}", transaction) from exc
    _restore_context(bpy, snapshot, warnings)
    transaction.update(changed=True, group=group.name, created_group=created, warnings=warnings)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_vertex_group(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {
        "object_name", "group_name", "max_vertices",
        "dry_run", "confirm", "expected_state",
    })
    object_name = _name(params.get("object_name"), "object_name")
    group_name = _name(params.get("group_name"), "group_name")
    max_vertices = _integer(params.get("max_vertices", 250_000), "max_vertices", 1, MAX_VERTICES)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name)
    _budget(obj, max_vertices)
    group = obj.vertex_groups.get(group_name)
    if group is None:
        raise NotFoundError(f"Vertex group not found: {group_name}")
    membership_count = sum(
        1 for vertex in obj.data.vertices
        if any(item.group == group.index for item in vertex.groups)
    )
    if membership_count:
        raise InvalidStateError(
            f"Vertex group {group_name} has {membership_count} memberships; remove its weights first"
        )
    record = {
        "name": group.name, "index": int(group.index),
        "lock_weight": bool(group.lock_weight), "membership_count": 0,
    }
    transaction, apply = prepare_destructive_action(
        action="skin.group_remove",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=[{
            "op": "remove_empty_vertex_group", "target": group.name,
            "before": record, "after": None, "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=True, state_token=current_state_token(bpy),
        extra={"object": obj.name, "group": group.name, "membership_count": 0},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        obj.vertex_groups.remove(group)
        if obj.vertex_groups.get(group_name) is not None:
            raise RuntimeError("Blender did not remove the vertex group")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        transaction.update(changed=False)
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"skin.group_remove failed: {exc}", transaction) from exc
    transaction.update(changed=True, removed_group=record)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def normalize_weights(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {
        "object_name", "armature_name", "max_influences", "min_weight",
        "require_all_weighted", "max_vertices", "max_weight_entries",
        "dry_run", "confirm", "expected_state",
    })
    object_name = _name(params.get("object_name"), "object_name")
    armature_name = _optional_name(params.get("armature_name"), "armature_name")
    max_influences = _integer(params.get("max_influences", 4), "max_influences", 1, 64)
    min_weight = _number(params.get("min_weight", 0.0001), "min_weight", 0.0, 1.0)
    require_all_weighted = _strict_bool(params.get("require_all_weighted"), "require_all_weighted", True)
    max_vertices = _integer(params.get("max_vertices", 250_000), "max_vertices", 1, MAX_VERTICES)
    max_weight_entries = _integer(
        params.get("max_weight_entries", 250_000),
        "max_weight_entries", 1, MAX_AUTO_WEIGHT_ENTRIES,
    )
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name)
    _budget(obj, max_vertices)
    armature, modifier = _resolve_armature(bpy, obj, armature_name)
    if armature is None or modifier is None:
        raise InvalidStateError("No targeted Armature modifier was found; bind the mesh first")
    deform_names = {bone.name for bone in armature.data.bones if bone.use_deform}
    if not deform_names:
        raise InvalidStateError(f"Armature {armature.name} has no deform bones")
    groups = {group.index: group for group in obj.vertex_groups if group.name in deform_names}
    if not groups:
        raise InvalidStateError("Mesh has no vertex groups matching deform bones")
    existing_entries = _count_weight_entries(obj, max_weight_entries, "Weight-entry count")
    plans: list[tuple[int, dict[int, float]]] = []
    unweighted = 0
    pruned_entries = 0
    for vertex in obj.data.vertices:
        weights = {
            membership.group: float(membership.weight)
            for membership in vertex.groups
            if membership.group in groups and float(membership.weight) > 0.0
        }
        if not weights:
            unweighted += 1
            continue
        ordered = sorted(weights.items(), key=lambda item: (-item[1], groups[item[0]].name.casefold()))
        kept = [(index, weight) for index, weight in ordered if weight >= min_weight][:max_influences]
        if not kept:
            kept = ordered[:1]
        total = sum(weight for _, weight in kept)
        normalized = {index: weight / total for index, weight in kept}
        needs_change = set(normalized) != set(weights) or any(
            not math.isclose(normalized.get(index, 0.0), weight, rel_tol=0.0, abs_tol=1e-6)
            for index, weight in weights.items()
        )
        if not needs_change:
            continue
        locked = [groups[index].name for index in weights if groups[index].lock_weight]
        if locked:
            raise InvalidStateError(
                f"Vertex {vertex.index} requires normalization but has locked deform groups: {', '.join(sorted(locked)[:10])}"
            )
        plans.append((int(vertex.index), normalized))
        pruned_entries += len(set(weights) - set(normalized))
    if require_all_weighted and unweighted:
        raise InvalidStateError(
            f"Cannot normalize: {unweighted} of {len(obj.data.vertices)} vertices have no effective deform weight"
        )
    journal = [{
        "op": "normalize_deform_weights", "target": obj.name,
        "before": {"weight_entries": existing_entries},
        "after": {
            "changed_vertices": len(plans), "max_influences": max_influences,
            "min_weight": min_weight, "pruned_entries": pruned_entries,
        },
        "status": "planned",
    }]
    transaction, apply = prepare_destructive_action(
        action="skin.weights_normalize",
        affected_datablocks=[
            {"type": "Object", "name": obj.name},
            {"type": "Mesh", "name": obj.data.name},
            {"type": "Armature", "name": armature.data.name},
        ],
        journal=journal, dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=bool(plans), state_token=current_state_token(bpy),
        extra={
            "object": obj.name, "armature": armature.name,
            "changed_vertex_count": len(plans), "unweighted_vertex_count": unweighted,
            "pruned_weight_entry_count": pruned_entries,
            "max_influences": max_influences, "min_weight": min_weight,
        },
    )
    if not apply or not plans:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _snapshot_weights(obj, max_weight_entries)
    try:
        for vertex_index, normalized in plans:
            for group in groups.values():
                try:
                    group.remove([vertex_index])
                except RuntimeError:
                    pass
            for group_index, weight in normalized.items():
                groups[group_index].add([vertex_index], weight, "REPLACE")
        weighted, remaining_unweighted, non_normalized = _deform_weight_stats(obj, deform_names)
        if require_all_weighted and remaining_unweighted:
            raise RuntimeError(f"Normalization left {remaining_unweighted} vertices unweighted")
        if non_normalized:
            raise RuntimeError(f"Normalization left {non_normalized} vertices with non-normalized deform weights")
        journal[0]["status"] = "applied"
    except Exception as exc:
        rollback_errors = _restore_weights(obj, snapshot)
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"skin.weights_normalize failed: {exc}", transaction) from exc
    transaction.update(
        changed=True, weighted_vertex_count=weighted,
        unweighted_vertex_count=remaining_unweighted,
        non_normalized_vertex_count=non_normalized,
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def weights_limit(params: dict[str, Any]) -> dict[str, Any]:
    """Creature-facing name for the existing guarded prune/limit/normalize path."""
    _unknown(params, {
        "object_name", "armature_name", "max_influences", "prune_threshold",
        "require_all_weighted", "max_vertices", "max_weight_entries",
        "dry_run", "confirm", "expected_state",
    })
    translated = dict(params)
    if "prune_threshold" in translated:
        translated["min_weight"] = translated.pop("prune_threshold")
    result = normalize_weights(translated)
    result["operation"] = "skin.weights_limit"
    return result


def weights_transfer_by_index(params: dict[str, Any]) -> dict[str, Any]:
    """Copy template weights only when vertex/edge/face connectivity matches."""
    _unknown(params, {
        "source_name", "object_name", "group_names", "max_vertices",
        "max_weight_entries", "dry_run", "confirm", "expected_state",
    })
    source_name = _name(params.get("source_name"), "source_name")
    object_name = _name(params.get("object_name"), "object_name")
    if source_name == object_name:
        raise InvalidParamsError("source_name and object_name must differ")
    max_vertices = _integer(params.get("max_vertices", 250_000), "max_vertices", 1, MAX_VERTICES)
    max_entries = _integer(params.get("max_weight_entries", 500_000), "max_weight_entries", 1, MAX_AUTO_WEIGHT_ENTRIES)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    source = _mesh_object(bpy, source_name)
    destination = _mesh_object(bpy, object_name)
    if source.data is destination.data:
        raise InvalidStateError("Source and destination share one Mesh datablock; make the destination mesh single-user before transferring weights")
    _budget(source, max_vertices)
    _budget(destination, max_vertices)
    from .mesh_topology import connectivity_hash
    source_hash = connectivity_hash(source.data)
    destination_hash = connectivity_hash(destination.data)
    if source_hash != destination_hash:
        raise InvalidStateError("Template and destination connectivity hashes differ; index weight transfer refused")
    raw_names = params.get("group_names")
    if raw_names is None:
        names = [group.name for group in source.vertex_groups]
    elif isinstance(raw_names, list) and raw_names and all(isinstance(name, str) and name for name in raw_names) and len(set(raw_names)) == len(raw_names):
        names = raw_names
    else:
        raise InvalidParamsError("group_names must be a non-empty list of unique names")
    if not names:
        raise InvalidStateError("Source has no vertex groups to transfer")
    source_groups = {group.name: group for group in source.vertex_groups}
    missing = sorted(set(names) - set(source_groups))
    if missing:
        raise NotFoundError(f"Source groups not found: {', '.join(missing[:10])}")
    for name in names:
        existing = destination.vertex_groups.get(name)
        if existing is not None and existing.lock_weight:
            raise InvalidStateError(f"Destination vertex group is locked: {name}")
    group_index = {source_groups[name].index: name for name in names}
    records = {name: [] for name in names}
    count = 0
    for vertex in source.data.vertices:
        for membership in vertex.groups:
            name = group_index.get(membership.group)
            if name is not None:
                count += 1
                if count > max_entries:
                    raise InvalidStateError("Source selected weights exceed max_weight_entries")
                records[name].append((int(vertex.index), float(membership.weight)))
    destination_snapshot = _snapshot_weights(destination, max_entries)
    journal = [{"op": "transfer_weights_by_index", "target": destination.name,
                "before": {"weight_entries": destination_snapshot.entry_count},
                "after": {"groups": names, "entries": count, "connectivity_hash": source_hash},
                "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="skin.weights_transfer_by_index",
        affected_datablocks=[{"type": "Object", "name": destination.name},
                             {"type": "Mesh", "name": destination.data.name}],
        journal=journal, dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=True, state_token=current_state_token(bpy),
        extra={"source": source.name, "object": destination.name,
               "connectivity_hash": source_hash, "group_count": len(names), "weight_entries": count},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        for name in names:
            group = destination.vertex_groups.get(name)
            if group is None:
                group = destination.vertex_groups.new(name=name)
            for chunk in _vertex_chunks(len(destination.data.vertices)):
                group.remove(chunk)
            for index, weight in records[name]:
                group.add([index], weight, "REPLACE")
        journal[0]["status"] = "applied"
    except Exception as exc:
        rollback_errors = _restore_weights(destination, destination_snapshot)
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"skin.weights_transfer_by_index failed: {exc}", transaction) from exc
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def validate_skin(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "armature_name", "max_vertices", "normalization_tolerance", "sample_limit"})
    object_name = _name(params.get("object_name"), "object_name")
    armature_name = _optional_name(params.get("armature_name"), "armature_name")
    max_vertices = _integer(params.get("max_vertices", 250_000), "max_vertices", 1, MAX_VERTICES)
    tolerance = _number(params.get("normalization_tolerance", 0.0001), "normalization_tolerance", 0.0, 1.0)
    sample_limit = _integer(params.get("sample_limit", 50), "sample_limit", 1, 2000)
    bpy = get_bpy()
    obj = _mesh_object(bpy, object_name)
    _budget(obj, max_vertices)
    issues: list[dict[str, Any]] = []
    armature, modifier = _resolve_armature(bpy, obj, armature_name)
    if armature is None:
        issues.append({"severity": "error", "code": "missing_armature", "message": "No targeted Armature modifier was found"})
        deform_bones: set[str] = set()
    else:
        deform_bones = {bone.name for bone in armature.data.bones if bone.use_deform}
        if modifier is None:
            issues.append({"severity": "error", "code": "missing_modifier", "message": f"No Armature modifier targets {armature.name}"})
    groups_by_index = {group.index: group for group in obj.vertex_groups}
    group_names = {group.name for group in obj.vertex_groups}
    all_bone_names = {bone.name for bone in armature.data.bones} if armature is not None else set()
    missing_groups = sorted(deform_bones - group_names)
    extra_groups = sorted(group_names - all_bone_names) if armature is not None else []
    if missing_groups:
        issues.append({"severity": "warning", "code": "bones_without_groups", "count": len(missing_groups), "sample": missing_groups[:sample_limit], "message": "Deform bones do not have matching vertex groups"})
    if extra_groups:
        issues.append({"severity": "warning", "code": "groups_without_bones", "count": len(extra_groups), "sample": extra_groups[:sample_limit], "message": "Vertex groups do not match bones on the selected armature"})
    relevant_indices = {group.index for group in obj.vertex_groups if group.name in deform_bones}
    unweighted: list[int] = []
    non_normalized: list[dict[str, Any]] = []
    unweighted_count = 0
    non_normalized_count = 0
    nondeform_count = 0
    nondeform_sample: list[dict[str, Any]] = []
    influence_histogram: dict[str, int] = {}
    invalid_group_memberships: list[dict[str, Any]] = []
    for vertex in obj.data.vertices:
        total = 0.0
        influences = 0
        for membership in vertex.groups:
            if membership.group not in groups_by_index:
                if len(invalid_group_memberships) < sample_limit:
                    invalid_group_memberships.append({"vertex": int(vertex.index), "group_index": int(membership.group)})
            elif membership.group in relevant_indices:
                total += float(membership.weight)
                if membership.weight > tolerance:
                    influences += 1
            elif armature is not None and groups_by_index[membership.group].name in all_bone_names and membership.weight > tolerance:
                nondeform_count += 1
                if len(nondeform_sample) < sample_limit:
                    nondeform_sample.append({"vertex": int(vertex.index),
                                             "bone": groups_by_index[membership.group].name,
                                             "weight": float(membership.weight)})
        influence_histogram[str(influences)] = influence_histogram.get(str(influences), 0) + 1
        if deform_bones and total <= tolerance:
            unweighted_count += 1
            if len(unweighted) < sample_limit:
                unweighted.append(int(vertex.index))
        elif deform_bones and abs(total - 1.0) > tolerance:
            non_normalized_count += 1
            if len(non_normalized) < sample_limit:
                non_normalized.append({"vertex": int(vertex.index), "total": total})
    if invalid_group_memberships:
        issues.append({"severity": "error", "code": "invalid_group_membership", "sample": invalid_group_memberships, "message": "Vertices reference missing vertex-group indices"})
    if unweighted:
        issues.append({"severity": "warning", "code": "unweighted_vertices", "count": unweighted_count, "sample": unweighted, "sample_truncated": unweighted_count > len(unweighted), "message": "Vertices have no effective deform-bone weight"})
    if non_normalized:
        issues.append({"severity": "warning", "code": "non_normalized_vertices", "count": non_normalized_count, "sample": non_normalized, "sample_truncated": non_normalized_count > len(non_normalized), "message": "Deform-bone weights do not sum to one within tolerance"})
    if nondeform_count:
        issues.append({"severity": "warning", "code": "nondeform_bone_weights", "count": nondeform_count,
                       "sample": nondeform_sample, "sample_truncated": nondeform_count > len(nondeform_sample),
                       "message": "Weights target non-deforming bones"})
    errors = sum(issue["severity"] == "error" for issue in issues)
    warnings = sum(issue["severity"] == "warning" for issue in issues)
    return {
        "object": obj.name,
        "armature": armature.name if armature is not None else None,
        "modifier": modifier.name if modifier is not None else None,
        "vertex_count": len(obj.data.vertices),
        "deform_bone_count": len(deform_bones),
        "vertex_group_count": len(obj.vertex_groups),
        "unweighted_vertex_count": unweighted_count,
        "non_normalized_vertex_count": non_normalized_count,
        "nondeform_bone_weight_count": nondeform_count,
        "influence_histogram": influence_histogram,
        "issue_count": len(issues),
        "error_count": errors,
        "warning_count": warnings,
        "valid": errors == 0,
        "issues": issues,
        "state_token": current_state_token(bpy),
    }
