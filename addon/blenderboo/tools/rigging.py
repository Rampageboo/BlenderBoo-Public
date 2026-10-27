"""Bounded armature, edit-bone, pose, and pose-constraint controls."""

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
MAX_BONES = 10_000
MAX_CREATE_BONES = 512
MAX_OPERATIONS = 100
MAX_CONSTRAINT_DEPENDENCIES = 2000
MAX_CONSTRAINT_DRIVER_SOURCES = 100_000
INHERIT_SCALE_MODES = frozenset(
    {"FULL", "FIX_SHEAR", "ALIGNED", "AVERAGE", "NONE", "NONE_LEGACY"}
)
ROTATION_MODES = frozenset(
    {"QUATERNION", "XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX", "AXIS_ANGLE"}
)
CONSTRAINT_TYPES = frozenset(
    {
        "COPY_LOCATION",
        "COPY_ROTATION",
        "COPY_SCALE",
        "COPY_TRANSFORMS",
        "DAMPED_TRACK",
        "IK",
        "LIMIT_LOCATION",
        "LIMIT_ROTATION",
        "LIMIT_SCALE",
        "STRETCH_TO",
        "TRACK_TO",
    }
)
CONSTRAINT_SPACES = frozenset({"WORLD", "POSE", "LOCAL_WITH_PARENT", "LOCAL"})
TRACK_AXES = frozenset({"TRACK_X", "TRACK_Y", "TRACK_Z", "TRACK_NEGATIVE_X", "TRACK_NEGATIVE_Y", "TRACK_NEGATIVE_Z"})
UP_AXES = frozenset({"UP_X", "UP_Y", "UP_Z"})
CONSTRAINT_FIELDS = {
    "COPY_LOCATION": frozenset({"target", "subtarget", "influence", "mute", "owner_space", "target_space", "use_offset"}),
    "COPY_ROTATION": frozenset({"target", "subtarget", "influence", "mute", "owner_space", "target_space", "mix_mode"}),
    "COPY_SCALE": frozenset({"target", "subtarget", "influence", "mute", "owner_space", "target_space", "use_offset"}),
    "COPY_TRANSFORMS": frozenset({"target", "subtarget", "influence", "mute", "owner_space", "target_space", "mix_mode"}),
    "DAMPED_TRACK": frozenset({"target", "subtarget", "influence", "mute", "head_tail", "track_axis"}),
    "TRACK_TO": frozenset({"target", "subtarget", "influence", "mute", "head_tail", "track_axis", "up_axis", "use_target_z"}),
    "STRETCH_TO": frozenset({"target", "subtarget", "influence", "mute", "head_tail", "rest_length", "bulge"}),
    "IK": frozenset({"target", "subtarget", "pole_target", "pole_subtarget", "influence", "mute", "chain_count", "pole_angle", "use_tail"}),
    "LIMIT_LOCATION": frozenset({"influence", "mute", "owner_space", "use_min_x", "use_min_y", "use_min_z", "use_max_x", "use_max_y", "use_max_z", "min_x", "min_y", "min_z", "max_x", "max_y", "max_z"}),
    "LIMIT_ROTATION": frozenset({"influence", "mute", "owner_space", "use_limit_x", "use_limit_y", "use_limit_z", "min_x", "min_y", "min_z", "max_x", "max_y", "max_z"}),
    "LIMIT_SCALE": frozenset({"influence", "mute", "owner_space", "use_min_x", "use_min_y", "use_min_z", "use_max_x", "use_max_y", "use_max_z", "min_x", "min_y", "min_z", "max_x", "max_y", "max_z"}),
}


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


def _vector(value: Any, label: str, size: int, minimum: float = -1_000_000.0, maximum: float = 1_000_000.0) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise InvalidParamsError(f"{label} must contain exactly {size} numbers")
    return [_number(item, f"{label}[{index}]", minimum, maximum) for index, item in enumerate(value)]


def _vector_values(value: Any) -> list[float]:
    return [float(item) for item in value]


def _enum(value: Any, label: str, allowed: frozenset[str]) -> str:
    name = _name(value, label).upper()
    if name not in allowed:
        raise InvalidParamsError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return name


def _page(params: dict[str, Any]) -> tuple[int, int]:
    limit = _integer(params.get("limit", 200), "limit", 1, MAX_PAGE)
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError("cursor must be a decimal string returned by the inspection tool")


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _armature_object(bpy: Any, name: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    if obj.type != "ARMATURE" or obj.data is None:
        raise InvalidParamsError(f"Object is not an armature: {name}")
    return obj


def _find_collection(root: Any, name: str) -> Any | None:
    if root.name == name:
        return root
    for child in root.children:
        found = _find_collection(child, name)
        if found is not None:
            return found
    return None


def _target_collection(bpy: Any, name: str | None) -> Any:
    root = bpy.context.scene.collection
    if name is not None:
        found = _find_collection(root, name)
        if found is None:
            raise NotFoundError(f"Collection not found in current scene: {name}")
        return found
    active = getattr(bpy.context, "collection", None)
    return active if active is not None and _find_collection(root, active.name) is active else root


@dataclass
class _ObjectContext:
    mode: str
    selected: list[Any]
    active: Any


def _capture_context(bpy: Any) -> _ObjectContext:
    return _ObjectContext(
        mode=str(bpy.context.mode),
        selected=list(bpy.context.selected_objects),
        active=bpy.context.view_layer.objects.active,
    )


def _operator_mode(mode: str) -> str:
    return {
        "EDIT_MESH": "EDIT",
        "EDIT_CURVE": "EDIT",
        "EDIT_SURFACE": "EDIT",
        "EDIT_TEXT": "EDIT",
        "EDIT_ARMATURE": "EDIT",
        "EDIT_METABALL": "EDIT",
        "PAINT_WEIGHT": "WEIGHT_PAINT",
        "PAINT_VERTEX": "VERTEX_PAINT",
        "PAINT_TEXTURE": "TEXTURE_PAINT",
        "PARTICLE": "PARTICLE_EDIT",
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
    if snapshot.mode != "OBJECT":
        try:
            if bpy.context.view_layer.objects.active is None:
                raise RuntimeError("original active object is no longer available")
            bpy.ops.object.mode_set(mode=_operator_mode(snapshot.mode))
        except Exception as exc:
            warnings.append(f"restore {snapshot.mode} mode: {exc}")


def _bone_record(bone: Any, pose_bone: Any | None, include_constraints: bool) -> dict[str, Any]:
    record = {
        "name": bone.name,
        "parent": bone.parent.name if bone.parent is not None else None,
        "children": len(bone.children),
        "head_local": _vector_values(bone.head_local),
        "tail_local": _vector_values(bone.tail_local),
        "length": float(bone.length),
        "use_connect": bool(bone.use_connect),
        "use_deform": bool(bone.use_deform),
        "inherit_scale": str(bone.inherit_scale),
        "hide": bool(bone.hide),
    }
    if pose_bone is not None:
        pose = {
            "location": _vector_values(pose_bone.location),
            "rotation_mode": str(pose_bone.rotation_mode),
            "rotation_euler": _vector_values(pose_bone.rotation_euler),
            "rotation_quaternion": _vector_values(pose_bone.rotation_quaternion),
            "scale": _vector_values(pose_bone.scale),
            "constraint_count": len(pose_bone.constraints),
        }
        if include_constraints:
            pose["constraints"] = [_constraint_record(constraint) for constraint in pose_bone.constraints]
        record["pose"] = pose
    return record


def _constraint_record(constraint: Any) -> dict[str, Any]:
    record = {
        "name": constraint.name,
        "type": constraint.type,
        "influence": float(constraint.influence),
        "mute": bool(constraint.mute),
    }
    for field in ("target", "pole_target"):
        if hasattr(constraint, field):
            value = getattr(constraint, field)
            record[field] = value.name if value is not None else None
    for field in ("subtarget", "pole_subtarget", "owner_space", "target_space", "track_axis", "up_axis", "chain_count"):
        if hasattr(constraint, field):
            record[field] = getattr(constraint, field)
    return record


def inspect_armature(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "include_pose", "include_constraints", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    include_pose = _strict_bool(params.get("include_pose"), "include_pose", True)
    include_constraints = _strict_bool(params.get("include_constraints"), "include_constraints", False)
    if include_constraints and not include_pose:
        raise InvalidParamsError("include_constraints requires include_pose true")
    limit, offset = _page(params)
    bpy = get_bpy()
    obj = _armature_object(bpy, object_name)
    bones = sorted(obj.data.bones, key=lambda item: item.name.casefold())
    if len(bones) > MAX_BONES:
        raise InvalidStateError(f"Armature has {len(bones)} bones; maximum supported is {MAX_BONES}")
    page = bones[offset:offset + limit]
    items = []
    for bone in page:
        pose_bone = obj.pose.bones.get(bone.name) if include_pose and obj.pose is not None else None
        items.append(_bone_record(bone, pose_bone, include_constraints))
    next_offset = offset + len(page)
    return {
        "object": obj.name,
        "armature": obj.data.name,
        "display_type": str(obj.data.display_type),
        "show_in_front": bool(obj.show_in_front),
        "bone_count": len(bones),
        "items": items,
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(items),
            "next_cursor": str(next_offset) if next_offset < len(bones) else None,
        },
        "state_token": current_state_token(bpy),
    }


def _bone_spec(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError(f"bones[{index}] must be an object")
    _unknown(value, {"name", "head", "tail", "roll", "parent", "use_connect", "use_deform", "inherit_scale"}, f"bones[{index}] fields")
    head = _vector(value.get("head"), f"bones[{index}].head", 3)
    tail = _vector(value.get("tail"), f"bones[{index}].tail", 3)
    if head == tail:
        raise InvalidParamsError(f"bones[{index}] head and tail must differ")
    result = {
        "name": _name(value.get("name"), f"bones[{index}].name"),
        "head": head,
        "tail": tail,
        "roll": _number(value.get("roll", 0.0), f"bones[{index}].roll", -math.tau * 1000.0, math.tau * 1000.0),
        "parent": _optional_name(value.get("parent"), f"bones[{index}].parent"),
        "use_connect": _strict_bool(value.get("use_connect"), f"bones[{index}].use_connect", False),
        "use_deform": _strict_bool(value.get("use_deform"), f"bones[{index}].use_deform", True),
        "inherit_scale": _enum(value.get("inherit_scale", "FULL"), f"bones[{index}].inherit_scale", INHERIT_SCALE_MODES),
    }
    if result["use_connect"] and result["parent"] is None:
        raise InvalidParamsError(f"bones[{index}].use_connect requires parent")
    return result


def _bone_specs(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CREATE_BONES:
        raise InvalidParamsError(f"bones must be an array with at most {MAX_CREATE_BONES} items")
    specs = [_bone_spec(item, index) for index, item in enumerate(value)]
    names = [item["name"] for item in specs]
    if len(names) != len(set(names)):
        raise InvalidParamsError("bones must use unique names")
    known = set(names)
    for index, spec in enumerate(specs):
        if spec["parent"] is not None and spec["parent"] not in known:
            raise InvalidParamsError(f"bones[{index}].parent does not name another requested bone")
        if spec["parent"] == spec["name"]:
            raise InvalidParamsError(f"bones[{index}] cannot parent itself")
    return specs


def create_armature(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"name", "data_name", "collection", "display_type", "show_in_front", "bones", "dry_run", "expected_state"})
    name = _name(params.get("name"), "name")
    data_name = _name(params.get("data_name", name), "data_name")
    collection_name = _optional_name(params.get("collection"), "collection")
    display_type = _enum(params.get("display_type", "OCTAHEDRAL"), "display_type", frozenset({"OCTAHEDRAL", "STICK", "BBONE", "ENVELOPE", "WIRE"}))
    show_in_front = _strict_bool(params.get("show_in_front"), "show_in_front", True)
    specs = _bone_specs(params.get("bones"))
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.data.objects.get(name) is not None:
        raise InvalidStateError(f"Object already exists: {name}")
    if bpy.data.armatures.get(data_name) is not None:
        raise InvalidStateError(f"Armature datablock already exists: {data_name}")
    collection = _target_collection(bpy, collection_name)
    journal = [{"op": "create_armature", "target": name, "before": None, "after": {"data": data_name, "collection": collection.name}, "status": "planned"}]
    journal.extend({"op": "add_bone", "target": spec["name"], "before": None, "after": spec, "status": "planned"} for spec in specs)
    transaction, apply = prepare_destructive_action(
        action="armature.create",
        affected_datablocks=[{"type": "Object", "name": name}, {"type": "Armature", "name": data_name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": name, "armature": data_name, "collection": collection.name, "bone_count": len(specs)},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _capture_context(bpy)
    armature = None
    obj = None
    warnings: list[str] = []
    cleanup_errors: list[str] = []
    try:
        _leave_current_mode(bpy)
        armature = bpy.data.armatures.new(data_name)
        armature.display_type = display_type
        obj = bpy.data.objects.new(name, armature)
        obj.show_in_front = show_in_front
        collection.objects.link(obj)
        journal[0]["status"] = "applied"
        if specs:
            _select_only(bpy, obj)
            bpy.ops.object.mode_set(mode="EDIT")
            created = {}
            for entry, spec in zip(journal[1:], specs):
                bone = armature.edit_bones.new(spec["name"])
                if bone.name != spec["name"]:
                    raise RuntimeError(f"Blender changed requested bone name {spec['name']} to {bone.name}")
                bone.head = spec["head"]
                bone.tail = spec["tail"]
                bone.roll = spec["roll"]
                bone.use_deform = spec["use_deform"]
                bone.inherit_scale = spec["inherit_scale"]
                created[spec["name"]] = bone
                entry["status"] = "applied"
            for spec in specs:
                bone = created[spec["name"]]
                if spec["parent"] is not None:
                    bone.parent = created[spec["parent"]]
                bone.use_connect = spec["use_connect"]
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as exc:
        try:
            _leave_current_mode(bpy)
        except Exception as cleanup_exc:
            cleanup_errors.append(f"leave edit mode: {cleanup_exc}")
        if obj is not None and bpy.data.objects.get(obj.name) is obj:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove created object: {cleanup_exc}")
        if armature is not None and bpy.data.armatures.get(armature.name) is armature and armature.users == 0:
            try:
                bpy.data.armatures.remove(armature)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove created armature: {cleanup_exc}")
        _restore_context(bpy, snapshot, warnings)
        transaction.update(changed=bool(cleanup_errors), warnings=warnings + cleanup_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"armature.create failed: {exc}", transaction) from exc
    _restore_context(bpy, snapshot, warnings)
    transaction.update(changed=True, warnings=warnings, object=obj.name, armature=armature.name, bone_count=len(armature.bones))
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _edit_operation(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError(f"operations[{index}] must be an object")
    op = _name(value.get("op"), f"operations[{index}].op").lower()
    if op == "add":
        _unknown(value, {"op", "name", "head", "tail", "roll", "parent", "use_connect", "use_deform", "inherit_scale"}, f"operations[{index}] fields")
        result = _bone_spec({key: item for key, item in value.items() if key != "op"}, index)
        result["op"] = op
        return result
    if op == "rename":
        _unknown(value, {"op", "bone", "name"}, f"operations[{index}] fields")
        return {"op": op, "bone": _name(value.get("bone"), f"operations[{index}].bone"), "name": _name(value.get("name"), f"operations[{index}].name")}
    if op != "patch":
        raise InvalidParamsError("armature.edit operations support: add, patch, rename")
    fields = {"op", "bone", "head", "tail", "roll", "parent", "use_connect", "use_deform", "inherit_scale"}
    _unknown(value, fields, f"operations[{index}] fields")
    result: dict[str, Any] = {"op": op, "bone": _name(value.get("bone"), f"operations[{index}].bone")}
    if len(value) == 2:
        raise InvalidParamsError(f"operations[{index}] patch has no changes")
    if "head" in value:
        result["head"] = _vector(value["head"], f"operations[{index}].head", 3)
    if "tail" in value:
        result["tail"] = _vector(value["tail"], f"operations[{index}].tail", 3)
    if "roll" in value:
        result["roll"] = _number(value["roll"], f"operations[{index}].roll", -math.tau * 1000.0, math.tau * 1000.0)
    if "parent" in value:
        result["parent"] = _optional_name(value["parent"], f"operations[{index}].parent")
    for field in ("use_connect", "use_deform"):
        if field in value:
            result[field] = _strict_bool(value[field], f"operations[{index}].{field}")
    if "inherit_scale" in value:
        result["inherit_scale"] = _enum(value["inherit_scale"], f"operations[{index}].inherit_scale", INHERIT_SCALE_MODES)
    return result


def _edit_operations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_OPERATIONS:
        raise InvalidParamsError(f"operations must contain 1 to {MAX_OPERATIONS} items")
    return [_edit_operation(item, index) for index, item in enumerate(value)]


def _validate_edit_plan(existing_names: set[str], operations: list[dict[str, Any]]) -> None:
    names = set(existing_names)
    for index, operation in enumerate(operations):
        op = operation["op"]
        if op == "add":
            if operation["name"] in names:
                raise InvalidStateError(f"operations[{index}] bone already exists: {operation['name']}")
            parent = operation["parent"]
            if parent is not None and parent not in names:
                raise NotFoundError(f"operations[{index}] parent bone not found: {parent}")
            names.add(operation["name"])
        else:
            bone = operation["bone"]
            if bone not in names:
                raise NotFoundError(f"operations[{index}] bone not found: {bone}")
            if op == "rename":
                name = operation["name"]
                if name != bone and name in names:
                    raise InvalidStateError(f"operations[{index}] destination bone already exists: {name}")
                names.remove(bone)
                names.add(name)
            elif "parent" in operation:
                parent = operation["parent"]
                if parent is not None and parent not in names:
                    raise NotFoundError(f"operations[{index}] parent bone not found: {parent}")
                if parent == bone:
                    raise InvalidParamsError(f"operations[{index}] bone cannot parent itself")
    if len(names) > MAX_BONES:
        raise InvalidStateError(f"Edit would exceed the {MAX_BONES}-bone budget")


def _edit_bone_snapshot(bone: Any) -> dict[str, Any]:
    return {
        "head": bone.head.copy(),
        "tail": bone.tail.copy(),
        "roll": float(bone.roll),
        "parent": bone.parent,
        "use_connect": bool(bone.use_connect),
        "use_deform": bool(bone.use_deform),
        "inherit_scale": str(bone.inherit_scale),
    }


def _restore_edit_bone(bone: Any, before: dict[str, Any]) -> None:
    bone.use_connect = False
    bone.parent = before["parent"]
    bone.head = before["head"]
    bone.tail = before["tail"]
    bone.roll = before["roll"]
    bone.use_deform = before["use_deform"]
    bone.inherit_scale = before["inherit_scale"]
    bone.use_connect = before["use_connect"]


def edit_armature(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "operations", "expected_bone_count", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    operations = _edit_operations(params.get("operations"))
    expected_count = None if "expected_bone_count" not in params else _integer(params["expected_bone_count"], "expected_bone_count", 0, MAX_BONES)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _armature_object(bpy, object_name)
    if len(obj.data.bones) > MAX_BONES:
        raise InvalidStateError(f"Armature exceeds the {MAX_BONES}-bone budget")
    if expected_count is not None and len(obj.data.bones) != expected_count:
        raise InvalidStateError(f"expected_bone_count {expected_count} does not match {len(obj.data.bones)}")
    _validate_edit_plan({bone.name for bone in obj.data.bones}, operations)
    journal = [{"op": operation["op"], "target": operation.get("bone", operation.get("name")), "before": None, "after": operation, "status": "planned"} for operation in operations]
    transaction, apply = prepare_destructive_action(
        action="armature.edit",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Armature", "name": obj.data.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "armature": obj.data.name, "before_bone_count": len(obj.data.bones)},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _capture_context(bpy)
    applied: list[dict[str, Any]] = []
    warnings: list[str] = []
    rollback_errors: list[str] = []
    try:
        _select_only(bpy, obj)
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = obj.data.edit_bones
        for entry, operation in zip(journal, operations):
            if operation["op"] == "add":
                bone = edit_bones.new(operation["name"])
                applied.append({"op": "add", "bone": bone})
                if bone.name != operation["name"]:
                    raise RuntimeError(f"Blender changed requested bone name {operation['name']} to {bone.name}")
                bone.head = operation["head"]
                bone.tail = operation["tail"]
                bone.roll = operation["roll"]
                bone.use_deform = operation["use_deform"]
                bone.inherit_scale = operation["inherit_scale"]
                if operation["parent"] is not None:
                    parent = edit_bones.get(operation["parent"])
                    if parent is None:
                        raise RuntimeError(f"Parent disappeared during apply: {operation['parent']}")
                    bone.parent = parent
                bone.use_connect = operation["use_connect"]
            elif operation["op"] == "rename":
                bone = edit_bones.get(operation["bone"])
                before_name = bone.name
                applied.append({"op": "rename", "bone": bone, "name": before_name})
                bone.name = operation["name"]
                if bone.name != operation["name"]:
                    raise RuntimeError(f"Blender changed requested bone name {operation['name']} to {bone.name}")
            else:
                bone = edit_bones.get(operation["bone"])
                before = _edit_bone_snapshot(bone)
                applied.append({"op": "patch", "bone": bone, "before": before})
                if "parent" in operation:
                    bone.use_connect = False
                    bone.parent = edit_bones.get(operation["parent"]) if operation["parent"] is not None else None
                for field in ("head", "tail", "roll", "use_deform", "inherit_scale"):
                    if field in operation:
                        setattr(bone, field, operation[field])
                if bone.head == bone.tail:
                    raise InvalidParamsError(f"Patch makes bone {bone.name} zero length")
                if "use_connect" in operation:
                    if operation["use_connect"] and bone.parent is None:
                        raise InvalidParamsError(f"Bone {bone.name} cannot connect without a parent")
                    bone.use_connect = operation["use_connect"]
            entry["status"] = "applied"
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as exc:
        try:
            if bpy.context.mode != "EDIT_ARMATURE":
                _select_only(bpy, obj)
                bpy.ops.object.mode_set(mode="EDIT")
            for inverse in reversed(applied):
                bone = inverse["bone"]
                if inverse["op"] == "add":
                    if obj.data.edit_bones.get(bone.name) is bone:
                        obj.data.edit_bones.remove(bone)
                elif inverse["op"] == "rename":
                    bone.name = inverse["name"]
                else:
                    _restore_edit_bone(bone, inverse["before"])
        except Exception as rollback_exc:
            rollback_errors.append(f"restore edit bones: {rollback_exc}")
        try:
            _leave_current_mode(bpy)
        except Exception as rollback_exc:
            rollback_errors.append(f"leave edit mode after rollback: {rollback_exc}")
        _restore_context(bpy, snapshot, warnings)
        transaction.update(changed=bool(rollback_errors), warnings=warnings + rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"armature.edit failed: {exc}", transaction) from exc
    _restore_context(bpy, snapshot, warnings)
    transaction.update(changed=bool(applied), warnings=warnings, after_bone_count=len(obj.data.bones))
    if applied:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_bone(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "bone", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    bone_name = _name(params.get("bone"), "bone")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _armature_object(bpy, object_name)
    bone = obj.data.bones.get(bone_name)
    if bone is None:
        raise NotFoundError(f"Bone not found: {bone_name}")
    if bone.children:
        raise InvalidStateError(
            f"Bone {bone_name} has {len(bone.children)} children; remove or reparent them first"
        )
    pose_bone = obj.pose.bones.get(bone_name) if obj.pose is not None else None
    if pose_bone is not None and len(pose_bone.constraints):
        raise InvalidStateError(f"Pose bone {bone_name} has constraints; remove them first")
    custom_properties = list(bone.keys())
    if pose_bone is not None:
        custom_properties.extend(key for key in pose_bone.keys() if key not in custom_properties)
    if custom_properties:
        raise InvalidStateError(
            f"Bone {bone_name} has custom properties that are not recoverable: {', '.join(custom_properties[:10])}"
        )
    prefix = pose_bone.path_from_id() if pose_bone is not None else f'pose.bones["{bone_name}"]'
    dependencies: list[str] = []
    from .animation import _action_fcurves

    animation_data = obj.animation_data
    if animation_data is not None:
        actions = []
        if animation_data.action is not None:
            actions.append(animation_data.action)
        actions.extend(
            strip.action for track in animation_data.nla_tracks for strip in track.strips
            if getattr(strip, "action", None) is not None
        )
        for action in actions:
            for curve in _action_fcurves(action):
                if str(curve.data_path).startswith(prefix):
                    dependencies.append(f"Action:{action.name}:{curve.data_path}")
        for curve in animation_data.drivers:
            if str(curve.data_path).startswith(prefix):
                dependencies.append(f"OwnerDriver:{curve.data_path}")
    for source in bpy.data.objects:
        constraints = list(getattr(source, "constraints", ()))
        source_pose = getattr(source, "pose", None)
        if source_pose is not None:
            constraints.extend(
                constraint for item in source_pose.bones for constraint in item.constraints
            )
        for constraint in constraints:
            for target_field, subtarget_field in (
                ("target", "subtarget"), ("pole_target", "pole_subtarget")
            ):
                if (
                    getattr(constraint, target_field, None) == obj
                    and str(getattr(constraint, subtarget_field, "")) == bone_name
                ):
                    dependencies.append(f"Constraint:{source.name}:{constraint.name}")
                    if len(dependencies) > MAX_CONSTRAINT_DEPENDENCIES:
                        raise InvalidStateError(
                            f"Bone dependency scan exceeds {MAX_CONSTRAINT_DEPENDENCIES} records"
                        )

    source_count = 0
    source_ids: set[int] = set()
    for rna_property in bpy.data.bl_rna.properties:
        if str(getattr(rna_property, "type", "")) != "COLLECTION":
            continue
        collection = getattr(bpy.data, rna_property.identifier, None)
        if collection is None:
            continue
        for source in collection:
            source_key = id(source)
            if source_key in source_ids:
                continue
            source_ids.add(source_key)
            source_animation = getattr(source, "animation_data", None)
            if source_animation is None:
                continue
            source_count += 1
            if source_count > MAX_CONSTRAINT_DRIVER_SOURCES:
                raise InvalidStateError(
                    f"Bone driver-source scan exceeds {MAX_CONSTRAINT_DRIVER_SOURCES} animated datablocks"
                )
            source_type = str(getattr(getattr(source, "bl_rna", None), "identifier", "ID"))
            source_name = str(getattr(source, "name", "<unnamed>"))
            for curve in getattr(source_animation, "drivers", ()):
                driver = getattr(curve, "driver", None)
                if driver is None:
                    continue
                for variable in driver.variables:
                    for target in variable.targets:
                        if getattr(target, "id", None) is obj and (
                            str(getattr(target, "bone_target", "")) == bone_name
                            or str(getattr(target, "data_path", "")).startswith(prefix)
                        ):
                            dependencies.append(
                                f"DriverTarget:{source_type}:{source_name}:{curve.data_path}"
                            )
                            if len(dependencies) > MAX_CONSTRAINT_DEPENDENCIES:
                                raise InvalidStateError(
                                    f"Bone dependency scan exceeds {MAX_CONSTRAINT_DEPENDENCIES} records"
                                )
    bound_groups = []
    for candidate in bpy.context.scene.objects:
        if candidate.type != "MESH":
            continue
        bound = any(
            modifier.type == "ARMATURE" and getattr(modifier, "object", None) == obj
            for modifier in candidate.modifiers
        )
        if bound and candidate.vertex_groups.get(bone_name) is not None:
            bound_groups.append(candidate.name)
    if bound_groups:
        raise InvalidStateError(
            f"Bone {bone_name} still has matching vertex groups on {len(bound_groups)} bound meshes; "
            f"remove the groups first ({', '.join(bound_groups[:10])})"
        )
    if dependencies:
        raise InvalidStateError(
            f"Bone {bone_name} has {len(dependencies)} animation/constraint/driver dependencies; "
            f"remove them first ({', '.join(dependencies[:10])})"
        )
    record = {
        "name": bone.name,
        "parent": bone.parent.name if bone.parent is not None else None,
        "head_local": _vector_values(bone.head_local),
        "tail_local": _vector_values(bone.tail_local),
        "use_connect": bool(bone.use_connect),
        "use_deform": bool(bone.use_deform),
        "inherit_scale": str(bone.inherit_scale),
        "collections": [collection.name for collection in getattr(bone, "collections", ())],
    }
    transaction, apply = prepare_destructive_action(
        action="armature.bone_remove",
        affected_datablocks=[
            {"type": "Object", "name": obj.name},
            {"type": "Armature", "name": obj.data.name},
        ],
        journal=[{
            "op": "remove_bone", "target": bone.name,
            "before": record, "after": None, "status": "planned",
        }],
        dry_run=params.get("dry_run"), confirm=params.get("confirm"),
        require_confirm=True, state_token=current_state_token(bpy),
        extra={"object": obj.name, "armature": obj.data.name, "bone": bone.name},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    snapshot = _capture_context(bpy)
    warnings: list[str] = []
    try:
        _select_only(bpy, obj)
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bone = obj.data.edit_bones.get(bone_name)
        if edit_bone is None:
            raise RuntimeError("Bone disappeared before removal")
        obj.data.edit_bones.remove(edit_bone)
        bpy.ops.object.mode_set(mode="OBJECT")
        if obj.data.bones.get(bone_name) is not None:
            raise RuntimeError("Blender did not remove the bone")
        transaction["journal"][0]["status"] = "applied"
    except Exception as exc:
        try:
            _leave_current_mode(bpy)
        except Exception as mode_exc:
            warnings.append(f"leave edit mode: {mode_exc}")
        _restore_context(bpy, snapshot, warnings)
        transaction.update(changed=obj.data.bones.get(bone_name) is None, warnings=warnings)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"armature.bone_remove failed: {exc}", transaction) from exc
    _restore_context(bpy, snapshot, warnings)
    transaction.update(changed=True, removed_bone=record, warnings=warnings)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction

def inspect_pose(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "bone", "include_constraints", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    bone_name = _optional_name(params.get("bone"), "bone")
    include_constraints = _strict_bool(params.get("include_constraints"), "include_constraints", True)
    limit, offset = _page(params)
    bpy = get_bpy()
    obj = _armature_object(bpy, object_name)
    pose_bones = sorted(obj.pose.bones, key=lambda item: item.name.casefold()) if obj.pose is not None else []
    if bone_name is not None:
        pose_bone = obj.pose.bones.get(bone_name) if obj.pose is not None else None
        if pose_bone is None:
            raise NotFoundError(f"Pose bone not found: {bone_name}")
        pose_bones = [pose_bone]
        offset = 0
    page = pose_bones[offset:offset + limit]
    items = []
    for pose_bone in page:
        record = {
            "name": pose_bone.name,
            "location": _vector_values(pose_bone.location),
            "rotation_mode": str(pose_bone.rotation_mode),
            "rotation_euler": _vector_values(pose_bone.rotation_euler),
            "rotation_quaternion": _vector_values(pose_bone.rotation_quaternion),
            "rotation_axis_angle": _vector_values(pose_bone.rotation_axis_angle),
            "scale": _vector_values(pose_bone.scale),
            "constraint_count": len(pose_bone.constraints),
        }
        if include_constraints:
            record["constraints"] = [_constraint_record(constraint) for constraint in pose_bone.constraints]
        items.append(record)
    next_offset = offset + len(page)
    return {
        "object": obj.name,
        "bone_count": len(pose_bones),
        "items": items,
        "page": {"offset": offset, "limit": limit, "returned": len(items), "next_cursor": str(next_offset) if next_offset < len(pose_bones) else None},
        "state_token": current_state_token(bpy),
    }


def patch_pose(params: dict[str, Any]) -> dict[str, Any]:
    fields = {"object_name", "bone", "location", "rotation_mode", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale", "dry_run", "expected_state"}
    _unknown(params, fields)
    object_name = _name(params.get("object_name"), "object_name")
    bone_name = _name(params.get("bone"), "bone")
    values: dict[str, Any] = {}
    if "location" in params:
        values["location"] = _vector(params["location"], "location", 3)
    if "scale" in params:
        values["scale"] = _vector(params["scale"], "scale", 3, -1_000_000.0, 1_000_000.0)
    if "rotation_euler" in params:
        values["rotation_euler"] = _vector(params["rotation_euler"], "rotation_euler", 3, -math.tau * 1000.0, math.tau * 1000.0)
    if "rotation_quaternion" in params:
        values["rotation_quaternion"] = _vector(params["rotation_quaternion"], "rotation_quaternion", 4, -1_000_000.0, 1_000_000.0)
    if "rotation_axis_angle" in params:
        values["rotation_axis_angle"] = _vector(params["rotation_axis_angle"], "rotation_axis_angle", 4, -1_000_000.0, 1_000_000.0)
    if "rotation_mode" in params:
        values["rotation_mode"] = _enum(params["rotation_mode"], "rotation_mode", ROTATION_MODES)
    if not values:
        raise InvalidParamsError("pose.patch requires at least one pose transform field")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _armature_object(bpy, object_name)
    pose_bone = obj.pose.bones.get(bone_name) if obj.pose is not None else None
    if pose_bone is None:
        raise NotFoundError(f"Pose bone not found: {bone_name}")
    rollback_before = {
        "location": _vector_values(pose_bone.location),
        "rotation_mode": str(pose_bone.rotation_mode),
        "rotation_euler": _vector_values(pose_bone.rotation_euler),
        "rotation_quaternion": _vector_values(pose_bone.rotation_quaternion),
        "rotation_axis_angle": _vector_values(pose_bone.rotation_axis_angle),
        "scale": _vector_values(pose_bone.scale),
    }
    before = {field: rollback_before[field] for field in values}
    journal = [{"op": "patch", "target": bone_name, "before": before, "after": values, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="pose.patch",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "PoseBone", "name": bone_name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "bone": bone_name},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        if "rotation_mode" in values:
            pose_bone.rotation_mode = values["rotation_mode"]
        for field, value in values.items():
            if field != "rotation_mode":
                setattr(pose_bone, field, value)
        obj.update_tag()
        journal[0]["status"] = "applied"
    except Exception as exc:
        rollback_errors = []
        try:
            pose_bone.rotation_mode = rollback_before["rotation_mode"]
            for field, value in rollback_before.items():
                if field != "rotation_mode":
                    setattr(pose_bone, field, value)
            obj.update_tag()
        except Exception as rollback_exc:
            rollback_errors.append(f"restore pose transform: {rollback_exc}")
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"pose.patch failed: {exc}", transaction) from exc
    transaction["changed"] = before != values
    if transaction["changed"]:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _constraint_setting(field: str, value: Any, label: str) -> Any:
    if field in {"target", "pole_target"}:
        return _optional_name(value, label)
    if field in {"subtarget", "pole_subtarget"}:
        if value is None:
            return ""
        return _name(value, label)
    if field in {"influence", "head_tail"}:
        return _number(value, label, 0.0, 1.0)
    if field in {"rest_length"}:
        return _number(value, label, 0.0, 1_000_000.0)
    if field in {"bulge"}:
        return _number(value, label, 0.0, 1000.0)
    if field in {"pole_angle", "min_x", "min_y", "min_z", "max_x", "max_y", "max_z"}:
        return _number(value, label, -1_000_000.0, 1_000_000.0)
    if field == "chain_count":
        return _integer(value, label, 0, MAX_BONES)
    if field in {"mute", "use_offset", "use_target_z", "use_tail", "use_min_x", "use_min_y", "use_min_z", "use_max_x", "use_max_y", "use_max_z", "use_limit_x", "use_limit_y", "use_limit_z"}:
        return _strict_bool(value, label)
    if field in {"owner_space", "target_space"}:
        return _enum(value, label, CONSTRAINT_SPACES)
    if field == "track_axis":
        return _enum(value, label, TRACK_AXES)
    if field == "up_axis":
        return _enum(value, label, UP_AXES)
    if field == "mix_mode":
        return _name(value, label).upper()
    raise InvalidParamsError(f"Unsupported constraint setting: {field}")


def _constraint_operation(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError(f"operations[{index}] must be an object")
    op = _name(value.get("op"), f"operations[{index}].op").lower()
    if op not in {"add", "patch", "remove"}:
        raise InvalidParamsError("pose.constraint_patch operations support: add, patch, remove")
    base = {"op", "bone", "name", "type", "settings"}
    _unknown(value, base, f"operations[{index}] fields")
    bone = _name(value.get("bone"), f"operations[{index}].bone")
    name = _name(value.get("name"), f"operations[{index}].name")
    constraint_type = _enum(value.get("type"), f"operations[{index}].type", CONSTRAINT_TYPES) if op == "add" else None
    settings = value.get("settings", {})
    if not isinstance(settings, dict):
        raise InvalidParamsError(f"operations[{index}].settings must be an object")
    if op == "patch" and not settings:
        raise InvalidParamsError(f"operations[{index}] patch has no settings")
    if op == "remove" and ("type" in value or settings):
        raise InvalidParamsError(
            f"operations[{index}] remove accepts only op, bone, and name"
        )
    result = {"op": op, "bone": bone, "name": name, "settings": dict(settings)}
    if constraint_type is not None:
        result["type"] = constraint_type
    return result


def _resolve_constraint_settings(bpy: Any, constraint_type: str, settings: dict[str, Any], label: str) -> dict[str, Any]:
    allowed = CONSTRAINT_FIELDS[constraint_type]
    _unknown(settings, set(allowed), f"{label} settings")
    resolved = {}
    for field, value in settings.items():
        normalized = _constraint_setting(field, value, f"{label}.settings.{field}")
        if field in {"target", "pole_target"}:
            target = None if normalized is None else bpy.context.scene.objects.get(normalized)
            if normalized is not None and target is None:
                raise NotFoundError(f"Constraint target not found in current scene: {normalized}")
            normalized = target
        resolved[field] = normalized
    return resolved


def _constraint_json(settings: dict[str, Any]) -> dict[str, Any]:
    return {field: (value.name if field in {"target", "pole_target"} and value is not None else value) for field, value in settings.items()}


def _constraint_snapshot(constraint: Any) -> dict[str, Any]:
    constraint_type = str(constraint.type)
    if constraint_type not in CONSTRAINT_FIELDS:
        raise InvalidStateError(
            f"Constraint type is not recoverable by this tool: {constraint_type}"
        )
    return {
        "name": str(constraint.name),
        "type": constraint_type,
        "settings": {
            field: getattr(constraint, field)
            for field in sorted(CONSTRAINT_FIELDS[constraint_type])
        },
    }


def _path_targets_constraint(path: Any, prefix: str) -> bool:
    value = str(path or "")
    return value == prefix or value.startswith(prefix + ".") or value.startswith(prefix + "[")


def _constraint_dependencies(bpy: Any, obj: Any, constraint: Any) -> list[dict[str, Any]]:
    from .animation import _action_fcurves

    prefix = str(constraint.path_from_id())
    dependencies: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, owner: str, path: Any) -> None:
        key = (kind, owner, str(path))
        if key in seen:
            return
        if len(dependencies) >= MAX_CONSTRAINT_DEPENDENCIES:
            raise InvalidStateError(
                f"Constraint dependency scan exceeds {MAX_CONSTRAINT_DEPENDENCIES} records"
            )
        seen.add(key)
        dependencies.append({"kind": kind, "owner": owner, "data_path": str(path)})

    animation_data = obj.animation_data
    if animation_data is not None:
        for curve in getattr(animation_data, "drivers", ()):
            if _path_targets_constraint(curve.data_path, prefix):
                add("owner_driver", obj.name, curve.data_path)
        actions: dict[int, Any] = {}
        active_action = getattr(animation_data, "action", None)
        if active_action is not None:
            actions[id(active_action)] = active_action
        for track in getattr(animation_data, "nla_tracks", ()):
            for strip in track.strips:
                action = getattr(strip, "action", None)
                if action is not None:
                    actions[id(action)] = action
        for action in actions.values():
            for curve in _action_fcurves(action):
                if _path_targets_constraint(curve.data_path, prefix):
                    add("action_fcurve", action.name, curve.data_path)

    source_count = 0
    source_ids: set[int] = set()
    for rna_property in bpy.data.bl_rna.properties:
        if str(getattr(rna_property, "type", "")) != "COLLECTION":
            continue
        collection = getattr(bpy.data, rna_property.identifier, None)
        if collection is None:
            continue
        for source in collection:
            source_key = id(source)
            if source_key in source_ids:
                continue
            source_ids.add(source_key)
            source_animation = getattr(source, "animation_data", None)
            if source_animation is None:
                continue
            source_count += 1
            if source_count > MAX_CONSTRAINT_DRIVER_SOURCES:
                raise InvalidStateError(
                    f"Constraint driver-source scan exceeds {MAX_CONSTRAINT_DRIVER_SOURCES} animated datablocks"
                )
            source_type = str(getattr(getattr(source, "bl_rna", None), "identifier", "ID"))
            source_name = str(getattr(source, "name", "<unnamed>"))
            for curve in getattr(source_animation, "drivers", ()):
                driver = getattr(curve, "driver", None)
                if driver is None:
                    continue
                for variable in driver.variables:
                    for target in variable.targets:
                        if getattr(target, "id", None) is obj and _path_targets_constraint(
                            getattr(target, "data_path", ""), prefix
                        ):
                            add("driver_target", f"{source_type}:{source_name}", target.data_path)
    return dependencies


def _restore_removed_constraint(inverse: dict[str, Any]) -> Any:
    constraints = inverse["constraints"]
    snapshot = inverse["snapshot"]
    constraint = constraints.new(snapshot["type"])
    constraint.name = snapshot["name"]
    if constraint.name != snapshot["name"]:
        constraints.remove(constraint)
        raise RuntimeError(
            f"Blender changed restored constraint name {snapshot['name']} to {constraint.name}"
        )
    for field, value in snapshot["settings"].items():
        setattr(constraint, field, value)
    current_index = len(constraints) - 1
    if current_index != inverse["index"]:
        constraints.move(current_index, inverse["index"])
    return constraint


def _validate_constraint_subtarget(
    constraint: Any | None,
    settings: dict[str, Any],
    *,
    target_field: str,
    subtarget_field: str,
    label: str,
) -> None:
    if target_field not in settings and subtarget_field not in settings:
        return
    subtarget = settings.get(subtarget_field)
    if subtarget_field not in settings and constraint is not None:
        subtarget = getattr(constraint, subtarget_field, "")
    if not subtarget:
        return
    target = settings.get(target_field)
    if target_field not in settings and constraint is not None:
        target = getattr(constraint, target_field, None)
    if target is None:
        raise InvalidParamsError(f"{label}.{subtarget_field} requires {target_field}")
    if target.type != "ARMATURE" or target.data is None:
        raise InvalidParamsError(f"{label}.{subtarget_field} requires an armature {target_field}")
    if target.data.bones.get(subtarget) is None:
        raise NotFoundError(
            f"Constraint {subtarget_field} not found on {target.name}: {subtarget}"
        )


def patch_pose_constraints(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "operations", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    raw = params.get("operations")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OPERATIONS:
        raise InvalidParamsError(f"operations must contain 1 to {MAX_OPERATIONS} items")
    operations = [_constraint_operation(value, index) for index, value in enumerate(raw)]
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _armature_object(bpy, object_name)
    plans = []
    created_names: dict[str, set[str]] = {}
    operation_targets: dict[tuple[str, str], list[str]] = {}
    for operation in operations:
        operation_targets.setdefault(
            (operation["bone"], operation["name"]), []
        ).append(operation["op"])
    for target, op_names in operation_targets.items():
        if "remove" in op_names and len(op_names) > 1:
            raise InvalidParamsError(
                f"Constraint {target[0]}:{target[1]} cannot be removed and otherwise changed in one batch"
            )
    for index, operation in enumerate(operations):
        pose_bone = obj.pose.bones.get(operation["bone"]) if obj.pose is not None else None
        if pose_bone is None:
            raise NotFoundError(f"operations[{index}] pose bone not found: {operation['bone']}")
        existing = pose_bone.constraints.get(operation["name"])
        if operation["op"] == "add":
            known = created_names.setdefault(pose_bone.name, set())
            if existing is not None or operation["name"] in known:
                raise InvalidStateError(f"operations[{index}] constraint already exists: {operation['name']}")
            constraint_type = operation["type"]
            known.add(operation["name"])
        elif operation["op"] == "patch":
            if existing is None:
                raise NotFoundError(f"operations[{index}] constraint not found: {operation['name']}")
            constraint_type = existing.type
            if constraint_type not in CONSTRAINT_FIELDS:
                raise InvalidStateError(f"Constraint type is not patchable by this tool: {constraint_type}")
        else:
            if existing is None:
                raise NotFoundError(f"operations[{index}] constraint not found: {operation['name']}")
            snapshot = _constraint_snapshot(existing)
            dependencies = _constraint_dependencies(bpy, obj, existing)
            if dependencies:
                sample = ", ".join(
                    f"{item['kind']}:{item['owner']}:{item['data_path']}"
                    for item in dependencies[:5]
                )
                raise InvalidStateError(
                    f"operations[{index}] constraint has {len(dependencies)} animation/driver "
                    f"dependencies; remove them first ({sample})"
                )
            plans.append({
                **operation,
                "constraint_type": snapshot["type"],
                "settings": {},
                "snapshot": snapshot,
                "index": list(pose_bone.constraints).index(existing),
                "dependencies": dependencies,
            })
            continue
        settings = _resolve_constraint_settings(bpy, constraint_type, operation["settings"], f"operations[{index}]")
        _validate_constraint_subtarget(
            existing, settings,
            target_field="target", subtarget_field="subtarget",
            label=f"operations[{index}].settings",
        )
        _validate_constraint_subtarget(
            existing, settings,
            target_field="pole_target", subtarget_field="pole_subtarget",
            label=f"operations[{index}].settings",
        )
        plans.append({**operation, "constraint_type": constraint_type, "settings": settings})
    journal = []
    for plan in plans:
        if plan["op"] == "remove":
            before = {
                "type": plan["snapshot"]["type"],
                "index": plan["index"],
                "settings": _constraint_json(plan["snapshot"]["settings"]),
            }
            after = None
        else:
            before = None
            after = {
                "type": plan["constraint_type"],
                "settings": _constraint_json(plan["settings"]),
            }
        journal.append({
            "op": plan["op"],
            "target": f"{plan['bone']}:{plan['name']}",
            "before": before,
            "after": after,
            "status": "planned",
        })
    removal_count = sum(plan["op"] == "remove" for plan in plans)
    transaction, apply = prepare_destructive_action(
        action="pose.constraint_patch",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Armature", "name": obj.data.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=bool(removal_count),
        state_token=current_state_token(bpy),
        extra={
            "object": obj.name,
            "operation_count": len(plans),
            "removal_count": removal_count,
            "dependency_check": "owner actions/drivers plus all animated-datablock driver targets",
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    applied: list[dict[str, Any]] = []
    try:
        for entry, plan in zip(journal, plans):
            pose_bone = obj.pose.bones[plan["bone"]]
            if plan["op"] == "add":
                constraint = pose_bone.constraints.new(plan["constraint_type"])
                constraint.name = plan["name"]
                if constraint.name != plan["name"]:
                    pose_bone.constraints.remove(constraint)
                    raise RuntimeError(f"Blender changed requested constraint name {plan['name']} to {constraint.name}")
                applied.append({"op": "add", "constraints": pose_bone.constraints, "constraint": constraint})
            elif plan["op"] == "patch":
                constraint = pose_bone.constraints[plan["name"]]
                before = {field: getattr(constraint, field) for field in plan["settings"]}
                applied.append({"op": "patch", "constraint": constraint, "before": before})
                entry["before"] = _constraint_json(before)
            else:
                constraint = pose_bone.constraints[plan["name"]]
                inverse = {
                    "op": "remove",
                    "constraints": pose_bone.constraints,
                    "snapshot": plan["snapshot"],
                    "index": plan["index"],
                }
                pose_bone.constraints.remove(constraint)
                applied.append(inverse)
                entry["status"] = "applied"
                continue
            for field, value in plan["settings"].items():
                if not hasattr(constraint, field):
                    raise RuntimeError(f"Blender constraint {plan['constraint_type']} has no {field} property")
                setattr(constraint, field, value)
            entry["status"] = "applied"
        obj.update_tag()
    except Exception as exc:
        rollback_errors = []
        try:
            for inverse in reversed(applied):
                if inverse["op"] == "add":
                    inverse["constraints"].remove(inverse["constraint"])
                elif inverse["op"] == "patch":
                    for field, value in inverse["before"].items():
                        setattr(inverse["constraint"], field, value)
                else:
                    _restore_removed_constraint(inverse)
            obj.update_tag()
        except Exception as rollback_exc:
            rollback_errors.append(f"restore constraints: {rollback_exc}")
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"pose.constraint_patch failed: {exc}", transaction) from exc
    transaction["changed"] = bool(applied)
    transaction["removed_count"] = removal_count
    if applied:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def validate_armature(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "include_animation", "max_bound_meshes", "sample_limit"})
    object_name = _name(params.get("object_name"), "object_name")
    include_animation = _strict_bool(params.get("include_animation"), "include_animation", True)
    max_bound_meshes = _integer(params.get("max_bound_meshes", 2000), "max_bound_meshes", 1, 10_000)
    sample_limit = _integer(params.get("sample_limit", 50), "sample_limit", 1, 2000)
    bpy = get_bpy()
    obj = _armature_object(bpy, object_name)
    bones = list(obj.data.bones)
    if len(bones) > MAX_BONES:
        raise InvalidStateError(f"Armature has more than {MAX_BONES} bones")
    issues: list[dict[str, Any]] = []
    for bone in bones:
        if float(bone.length) <= 1e-8:
            issues.append({"severity": "error", "code": "zero_length_bone", "target": bone.name, "message": "Bone has effectively zero rest length"})
        if bone.use_connect:
            if bone.parent is None:
                issues.append({"severity": "error", "code": "connected_without_parent", "target": bone.name, "message": "Connected bone has no parent"})
            else:
                delta = bone.head_local - bone.parent.tail_local
                if float(delta.length) > 1e-6:
                    issues.append({"severity": "error", "code": "connected_head_mismatch", "target": bone.name, "message": "Connected bone head does not match parent tail"})
        cursor = bone.parent
        seen = {id(bone)}
        while cursor is not None:
            if id(cursor) in seen:
                issues.append({"severity": "error", "code": "bone_parent_cycle", "target": bone.name, "message": "Bone parent hierarchy contains a cycle"})
                break
            seen.add(id(cursor))
            cursor = cursor.parent
    target_required = CONSTRAINT_TYPES - {"LIMIT_LOCATION", "LIMIT_ROTATION", "LIMIT_SCALE"}
    if obj.pose is not None:
        for pose_bone in obj.pose.bones:
            for constraint in pose_bone.constraints:
                label = f"{pose_bone.name}:{constraint.name}"
                if constraint.type in target_required and hasattr(constraint, "target") and constraint.target is None:
                    issues.append({"severity": "error", "code": "missing_constraint_target", "target": label, "message": "Constraint requires a target object"})
                for target_field, subtarget_field in (("target", "subtarget"), ("pole_target", "pole_subtarget")):
                    target = getattr(constraint, target_field, None)
                    subtarget = str(getattr(constraint, subtarget_field, ""))
                    if not subtarget:
                        continue
                    target_bones = getattr(getattr(target, "data", None), "bones", None)
                    if target is None or target_bones is None or target_bones.get(subtarget) is None:
                        issues.append({"severity": "error", "code": "missing_constraint_bone", "target": label, "message": f"Constraint {subtarget_field} is missing: {subtarget}"})
    animation_actions = []
    if include_animation and obj.animation_data is not None:
        active_action = obj.animation_data.action
        if active_action is not None:
            animation_actions.append(active_action)
        for track in obj.animation_data.nla_tracks:
            for strip in track.strips:
                if strip.action is not None and strip.action not in animation_actions:
                    animation_actions.append(strip.action)
    if include_animation:
        from .animation import _action_fcurves

        curve_count = 0
        for action in animation_actions:
            curves = _action_fcurves(action)
            curve_count += len(curves)
            if curve_count > 20_000:
                raise InvalidStateError("Armature animation exceeds the 20000 F-Curve validation budget")
            for curve in curves:
                label = f"{action.name}:{curve.data_path}[{curve.array_index}]"
                if not curve.is_valid:
                    issues.append({"severity": "error", "code": "invalid_armature_fcurve", "target": label, "message": "Blender marks this armature F-Curve invalid"})
                try:
                    obj.path_resolve(curve.data_path)
                except Exception:
                    issues.append({"severity": "error", "code": "missing_animation_target", "target": label, "message": "Armature cannot resolve the animated data path"})
    bound_meshes = []
    deform_bones = {bone.name for bone in bones if bone.use_deform}
    for candidate in bpy.context.scene.objects:
        if candidate.type != "MESH":
            continue
        modifiers = [modifier for modifier in candidate.modifiers if modifier.type == "ARMATURE" and getattr(modifier, "object", None) is obj]
        if not modifiers:
            continue
        bound_meshes.append(candidate)
        if len(bound_meshes) > max_bound_meshes:
            raise InvalidStateError(f"More than {max_bound_meshes} meshes are bound to this armature")
        group_names = {group.name for group in candidate.vertex_groups}
        missing = sorted(deform_bones - group_names)
        if missing:
            issues.append({"severity": "warning", "code": "bound_mesh_missing_groups", "target": candidate.name, "count": len(missing), "sample": missing[:sample_limit], "message": "Bound mesh lacks vertex groups for deform bones"})
    errors = sum(issue["severity"] == "error" for issue in issues)
    warnings = sum(issue["severity"] == "warning" for issue in issues)
    return {
        "object": obj.name,
        "armature": obj.data.name,
        "bone_count": len(bones),
        "pose_bone_count": len(obj.pose.bones) if obj.pose is not None else 0,
        "animation_action_count": len(animation_actions),
        "bound_mesh_count": len(bound_meshes),
        "issue_count": len(issues),
        "error_count": errors,
        "warning_count": warnings,
        "valid": errors == 0,
        "issues": issues,
        "state_token": current_state_token(bpy),
    }
