"""Bounded object inspection and guarded object lifecycle operations."""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

from ..errors import (
    InvalidParamsError,
    InvalidStateError,
    MutationApplyError,
    NameConflictError,
    NotFoundError,
)
from ..registry import ToolResult
from ._blender import get_bpy, vector_values
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


MESH_PRIMITIVE_TYPES = frozenset(
    {
        "PLANE",
        "CUBE",
        "UV_SPHERE",
        "ICO_SPHERE",
        "CYLINDER",
        "CONE",
        "TORUS",
        "CIRCLE",
        "GRID",
        "MONKEY",
    }
)
OBJECT_CREATE_TYPES = MESH_PRIMITIVE_TYPES | {"EMPTY", "CAMERA", "LIGHT"}
LIGHT_TYPES = frozenset({"POINT", "SUN", "SPOT", "AREA"})
MAX_OBJECT_BATCH = 2000
SUPPORTED_OBJECT_MODES = {
    "OBJECT", "EDIT", "POSE", "SCULPT", "VERTEX_PAINT", "WEIGHT_PAINT", "TEXTURE_PAINT",
}

_PRIMITIVE_OPERATORS = {
    "PLANE": "primitive_plane_add",
    "CUBE": "primitive_cube_add",
    "UV_SPHERE": "primitive_uv_sphere_add",
    "ICO_SPHERE": "primitive_ico_sphere_add",
    "CYLINDER": "primitive_cylinder_add",
    "CONE": "primitive_cone_add",
    "TORUS": "primitive_torus_add",
    "CIRCLE": "primitive_circle_add",
    "GRID": "primitive_grid_add",
    "MONKEY": "primitive_monkey_add",
}


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


def _strict_bool(value: Any, label: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _vector3(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise InvalidParamsError(f"{label} must be a three-number array")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise InvalidParamsError(f"{label} must be a three-number array")
        number = float(item)
        if not math.isfinite(number):
            raise InvalidParamsError(f"{label} values must be finite")
        result.append(number)
    return result


def _optional_vector3(params: dict[str, Any], name: str, default: Iterable[float]) -> list[float]:
    return list(default) if name not in params else _vector3(params[name], name)


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _page_params(params: dict[str, Any]) -> tuple[int, int]:
    limit = params.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise InvalidParamsError("limit must be an integer from 1 to 2000")
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError("cursor must be a non-negative decimal string returned by object.inspect")


def _object_names(value: Any, label: str = "objects") -> list[str]:
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, list) or not raw or len(raw) > MAX_OBJECT_BATCH:
        raise InvalidParamsError(
            f"{label} must be a name or a non-empty list of at most {MAX_OBJECT_BATCH} names"
        )
    names = [_name(item, label) for item in raw]
    if len(names) != len(set(names)):
        raise InvalidParamsError(f"{label} must not contain duplicate names")
    return names


def _unique_object_name(objects: Any, requested: str) -> str:
    """Return Blender-style deterministic suffixing without creating a datablock."""
    if objects.get(requested) is None:
        return requested
    index = 1
    while True:
        candidate = f"{requested}.{index:03d}"
        if objects.get(candidate) is None:
            return candidate
        index += 1


def _custom_property_keys(obj: Any) -> list[str]:
    keys = getattr(obj, "keys", None)
    if not callable(keys):
        return []
    return sorted(str(key) for key in keys() if str(key) != "_RNA_UI")


def _rotation_order(obj: Any) -> str:
    mode = str(getattr(obj, "rotation_mode", "XYZ"))
    return mode if mode in {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"} else "XYZ"


def _world_transform(obj: Any) -> dict[str, list[float]]:
    location, rotation, scale = obj.matrix_world.decompose()
    return {
        "location": vector_values(location),
        "rotation_euler": vector_values(rotation.to_euler(_rotation_order(obj))),
        "scale": vector_values(scale),
    }


def _local_transform(obj: Any) -> dict[str, list[float]]:
    return {
        "location": vector_values(obj.location),
        "rotation_euler": vector_values(obj.rotation_euler),
        "scale": vector_values(obj.scale),
    }


def _object_detail(obj: Any) -> dict[str, Any]:
    return {
        "name": obj.name,
        "type": obj.type,
        "transform": _local_transform(obj),
        "world_transform": _world_transform(obj),
        "dimensions": vector_values(obj.dimensions),
        "modifiers": [modifier.name for modifier in getattr(obj, "modifiers", ())],
        "constraints": [constraint.name for constraint in getattr(obj, "constraints", ())],
        "custom_properties": _custom_property_keys(obj),
        "collections": sorted(collection.name for collection in obj.users_collection),
        "visible": bool(obj.visible_get()),
        "selected": bool(obj.select_get()),
        "parent": obj.parent.name if obj.parent else None,
    }


def _object_summary(obj: Any) -> dict[str, Any]:
    return {
        "name": obj.name,
        "type": obj.type,
        "collections": sorted(collection.name for collection in obj.users_collection),
        "visible": bool(obj.visible_get()),
        "selected": bool(obj.select_get()),
        "parent": obj.parent.name if obj.parent else None,
    }


def _update_view_layer(bpy: Any) -> None:
    """Make matrix_world and visibility reads reflect preceding RNA changes."""
    bpy.context.view_layer.update()


def _matrix_max_delta(left: Any, right: Any) -> float:
    return max(abs(float(left[row][column]) - float(right[row][column])) for row in range(4) for column in range(4))


def _filter_objects(
    objects: Iterable[Any],
    *,
    name: str | None = None,
    name_pattern: re.Pattern[str] | None = None,
    object_type: str | None = None,
    collection: str | None = None,
    visible: bool | None = None,
    selected: bool | None = None,
    parent: str | None = None,
    material: str | None = None,
    custom_property: str | None = None,
    custom_value_supplied: bool = False,
    custom_value: Any = None,
) -> list[Any]:
    result = []
    for obj in objects:
        if name is not None and name not in obj.name:
            continue
        if name_pattern is not None and name_pattern.search(obj.name) is None:
            continue
        if object_type is not None and obj.type != object_type:
            continue
        if collection is not None and collection not in {item.name for item in obj.users_collection}:
            continue
        if visible is not None and bool(obj.visible_get()) != visible:
            continue
        if selected is not None and bool(obj.select_get()) != selected:
            continue
        if parent is not None and (obj.parent is None or obj.parent.name != parent):
            continue
        if material is not None and material not in {
            slot.material.name for slot in getattr(obj, "material_slots", ()) if slot.material is not None
        }:
            continue
        if custom_property is not None:
            keys = getattr(obj, "keys", lambda: ())()
            if custom_property not in keys:
                continue
            if custom_value_supplied and obj[custom_property] != custom_value:
                continue
        result.append(obj)
    return sorted(result, key=lambda item: item.name)


def inspect_object(params: dict[str, Any]) -> ToolResult:
    _unknown(
        params,
        {
            "name", "name_regex", "type", "collection", "visible", "selected",
            "parent", "material", "custom_property", "custom_value",
            "limit", "cursor", "object_name",
        },
    )
    substring = _optional_name(params.get("name"), "name")
    regex_value = _optional_name(params.get("name_regex"), "name_regex")
    try:
        pattern = re.compile(regex_value) if regex_value is not None else None
    except re.error as exc:
        raise InvalidParamsError(f"name_regex is invalid: {exc}") from exc
    object_type = _optional_name(params.get("type"), "type")
    if object_type is not None:
        object_type = object_type.upper()
    collection = _optional_name(params.get("collection"), "collection")
    visible = None if "visible" not in params else _strict_bool(params["visible"], "visible")
    selected = None if "selected" not in params else _strict_bool(params["selected"], "selected")
    parent = _optional_name(params.get("parent"), "parent")
    material = _optional_name(params.get("material"), "material")
    custom_property = _optional_name(params.get("custom_property"), "custom_property")
    if "custom_value" in params and custom_property is None:
        raise InvalidParamsError("custom_value requires custom_property")
    custom_value = params.get("custom_value")
    if "custom_value" in params and not (
        custom_value is None or isinstance(custom_value, (str, int, float, bool))
    ):
        raise InvalidParamsError("custom_value must be a JSON scalar or null")
    object_name = _optional_name(params.get("object_name"), "object_name")
    limit, offset = _page_params(params)
    bpy = get_bpy()
    _update_view_layer(bpy)
    if collection is not None and _find_scene_collection(bpy.context.scene.collection, collection) is None:
        raise NotFoundError(f"Collection not found in current scene: {collection}")
    if parent is not None and bpy.context.scene.objects.get(parent) is None:
        raise NotFoundError(f"Parent object not found in current scene: {parent}")
    if material is not None and bpy.data.materials.get(material) is None:
        raise NotFoundError(f"Material not found: {material}")
    objects = _filter_objects(
        bpy.context.scene.objects,
        name=substring,
        name_pattern=pattern,
        object_type=object_type,
        collection=collection,
        visible=visible,
        selected=selected,
        parent=parent,
        material=material,
        custom_property=custom_property,
        custom_value_supplied="custom_value" in params,
        custom_value=custom_value,
    )
    page = objects[offset : offset + limit]
    next_offset = offset + len(page)
    truncated = next_offset < len(objects)
    data: dict[str, Any] = {
        "count": len(objects),
        "objects": [_object_summary(obj) for obj in page],
        "next_cursor": str(next_offset) if truncated else None,
        "truncated": truncated,
        "state_token": current_state_token(bpy),
    }
    if object_name is not None:
        obj = bpy.context.scene.objects.get(object_name)
        if obj is None:
            raise NotFoundError(f"Object not found in current scene: {object_name}")
        data["object_detail"] = _object_detail(obj)
    return ToolResult(data=data)


def set_object_selection(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"objects", "selected", "exclusive", "active", "set_active", "expected_state"})
    names = _object_names(params.get("objects"))
    selected = _strict_bool(params.get("selected"), "selected")
    exclusive = _strict_bool(params.get("exclusive"), "exclusive", False)
    if exclusive and not selected:
        raise InvalidParamsError("exclusive is valid only when selected is true")
    set_active = _strict_bool(params.get("set_active"), "set_active", False)
    manage_active = "active" in params or set_active
    if "active" in params:
        active_name = _optional_name(params.get("active"), "active")
        if active_name is not None and (not selected or active_name not in names):
            raise InvalidParamsError("active must be null or one of the objects being selected")
    else:
        active_name = None
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("object.set_selection requires Blender Object mode")
    objects = [_scene_object(bpy, name) for name in names]
    affected = list(objects)
    if exclusive:
        for obj in bpy.context.selected_objects:
            if obj not in affected:
                affected.append(obj)
    if len(affected) > MAX_OBJECT_BATCH:
        raise InvalidStateError(
            f"Selection change would affect more than {MAX_OBJECT_BATCH} objects"
        )
    previous_active = bpy.context.view_layer.objects.active
    previous_active_name = previous_active.name if previous_active else None
    before = {obj.name: bool(obj.select_get()) for obj in affected}
    affected_datablocks = [{"type": "Object", "name": obj.name} for obj in affected]
    if manage_active:
        affected_datablocks.append({"type": "ViewLayer", "name": bpy.context.view_layer.name})
    journal = []
    try:
        if exclusive:
            for obj in affected:
                if obj not in objects:
                    obj.select_set(False)
        for obj in objects:
            obj.select_set(selected)
        if manage_active:
            bpy.context.view_layer.objects.active = (
                _scene_object(bpy, active_name) if active_name is not None else None
            )
        for obj in affected:
            after_selected = bool(obj.select_get())
            journal.append(
                {
                    "op": "set_selection",
                    "target": obj.name,
                    "before": before[obj.name],
                    "after": after_selected,
                    "status": "applied" if before[obj.name] != after_selected else "unchanged",
                }
            )
        if manage_active:
            current_active = bpy.context.view_layer.objects.active
            current_active_name = current_active.name if current_active else None
            journal.append(
                {
                    "op": "set_active_object",
                    "target": bpy.context.view_layer.name,
                    "before": previous_active_name,
                    "after": current_active_name,
                    "status": "applied" if previous_active_name != current_active_name else "unchanged",
                }
            )
    except Exception as exc:
        rollback_errors = []
        for obj in affected:
            try:
                obj.select_set(before[obj.name])
            except Exception as rollback_exc:
                rollback_errors.append(f"restore selection {obj.name}: {rollback_exc}")
        try:
            bpy.context.view_layer.objects.active = previous_active
        except Exception as rollback_exc:
            rollback_errors.append(f"restore active object: {rollback_exc}")
        transaction = new_transaction(affected_datablocks)
        transaction.update(
            objects=names,
            selected=selected,
            exclusive=exclusive,
            changed=bool(rollback_errors),
            journal=journal,
            warnings=rollback_errors,
        )
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"object.set_selection failed: {exc}", transaction) from exc
    changed = any(before[obj.name] != bool(obj.select_get()) for obj in affected) or (
        manage_active and previous_active is not bpy.context.view_layer.objects.active
    )
    transaction = new_transaction(affected_datablocks)
    transaction.update(
        objects=names,
        selected=selected,
        exclusive=exclusive,
        active=bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
        changed=changed,
        journal=journal,
    )
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _find_scene_collection(root: Any, name: str) -> Any | None:
    if root.name == name:
        return root
    for child in root.children:
        found = _find_scene_collection(child, name)
        if found is not None:
            return found
    return None


def _target_collection(bpy: Any, name: str | None) -> Any:
    root = bpy.context.scene.collection
    if name is not None:
        collection = _find_scene_collection(root, name)
        if collection is None:
            raise NotFoundError(f"Collection not found in current scene: {name}")
        return collection
    active = getattr(bpy.context, "collection", None)
    if active is not None and _find_scene_collection(root, active.name) is active:
        return active
    return root


def _scene_object(bpy: Any, name: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    return obj


def _snapshot_selection(bpy: Any) -> tuple[list[Any], Any]:
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("Mesh primitive creation requires Blender Object mode")
    return list(bpy.context.selected_objects), bpy.context.view_layer.objects.active


def _restore_selection(bpy: Any, snapshot: tuple[list[Any], Any]) -> None:
    selected, active = snapshot
    for obj in list(bpy.context.selected_objects):
        obj.select_set(False)
    scene_objects = bpy.context.scene.objects
    for obj in selected:
        if scene_objects.get(obj.name) is obj:
            obj.select_set(True)
    bpy.context.view_layer.objects.active = (
        active if active is not None and scene_objects.get(active.name) is active else None
    )


def _link_only(obj: Any, collection: Any) -> None:
    if collection not in obj.users_collection:
        collection.objects.link(obj)
    for existing in list(obj.users_collection):
        if existing is not collection:
            existing.objects.unlink(obj)


def _data_collection(bpy: Any, data: Any) -> Any | None:
    identifier = _data_identifier(data)
    return {
        "Mesh": getattr(bpy.data, "meshes", None),
        "Curve": getattr(bpy.data, "curves", None),
        "Camera": getattr(bpy.data, "cameras", None),
        "Light": getattr(bpy.data, "lights", None),
        "Armature": getattr(bpy.data, "armatures", None),
        "Lattice": getattr(bpy.data, "lattices", None),
        "MetaBall": getattr(bpy.data, "metaballs", None),
        "PointCloud": getattr(bpy.data, "pointclouds", None),
        "Volume": getattr(bpy.data, "volumes", None),
        "Speaker": getattr(bpy.data, "speakers", None),
        "Curves": getattr(bpy.data, "hair_curves", None),
        "GreasePencilv3": getattr(bpy.data, "grease_pencils_v3", None),
        "GreasePencil": getattr(bpy.data, "grease_pencils", None),
    }.get(identifier)


def _remove_new_object(bpy: Any, obj: Any, *, remove_data: bool) -> None:
    data = obj.data
    if bpy.data.objects.get(obj.name) is obj:
        bpy.data.objects.remove(obj, do_unlink=True)
    if remove_data and data is not None and getattr(data, "users", 1) == 0:
        data_collection = _data_collection(bpy, data)
        if data_collection is not None:
            data_collection.remove(data)
        else:
            bpy.data.batch_remove(ids=(data,))


def _create_mesh_primitive(
    bpy: Any,
    object_type: str,
    name: str | None,
    location: list[float],
    rotation: list[float],
    scale: list[float],
    collection: Any,
) -> Any:
    snapshot = _snapshot_selection(bpy)
    created = None
    try:
        operator = getattr(bpy.ops.mesh, _PRIMITIVE_OPERATORS[object_type])
        result = operator(
            align="WORLD",
            location=location,
            rotation=rotation,
            scale=scale,
        )
        if "FINISHED" not in result or bpy.context.object is None:
            raise InvalidStateError(f"Blender did not create {object_type}")
        created = bpy.context.object
        if name is not None:
            created.name = name
        _link_only(created, collection)
        return created
    except Exception:
        if created is not None and bpy.data.objects.get(created.name) is created:
            data = created.data
            bpy.data.objects.remove(created, do_unlink=True)
            if data is not None and getattr(data, "users", 1) == 0:
                data_collection = _data_collection(bpy, data)
                if data_collection is not None:
                    data_collection.remove(data)
        raise
    finally:
        _restore_selection(bpy, snapshot)


def create_object(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"type", "name", "location", "rotation", "scale", "collection", "light_type", "expected_state"})
    object_type = _name(params.get("type"), "type").upper()
    if object_type not in OBJECT_CREATE_TYPES:
        raise InvalidParamsError("type is not in the object.create allowlist")
    requested_name = _optional_name(params.get("name"), "name")
    location = _optional_vector3(params, "location", (0.0, 0.0, 0.0))
    rotation = _optional_vector3(params, "rotation", (0.0, 0.0, 0.0))
    scale = _optional_vector3(params, "scale", (1.0, 1.0, 1.0))
    collection_name = _optional_name(params.get("collection"), "collection")
    light_type = params.get("light_type", "POINT")
    if object_type == "LIGHT":
        light_type = _name(light_type, "light_type").upper()
        if light_type not in LIGHT_TYPES:
            raise InvalidParamsError("light_type must be POINT, SUN, SPOT, or AREA")
    elif "light_type" in params:
        raise InvalidParamsError("light_type is only valid when type is LIGHT")
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    collection = _target_collection(bpy, collection_name)
    resolved_name = _unique_object_name(
        bpy.data.objects, requested_name or object_type.title()
    )

    if object_type in MESH_PRIMITIVE_TYPES:
        obj = _create_mesh_primitive(bpy, object_type, resolved_name, location, rotation, scale, collection)
    else:
        base_name = resolved_name
        data = None
        obj = None
        try:
            if object_type == "CAMERA":
                data = bpy.data.cameras.new(base_name)
            elif object_type == "LIGHT":
                data = bpy.data.lights.new(base_name, type=light_type)
            obj = bpy.data.objects.new(base_name, data)
            obj.location = location
            obj.rotation_euler = rotation
            obj.scale = scale
            collection.objects.link(obj)
        except Exception:
            if obj is not None and bpy.data.objects.get(obj.name) is obj:
                bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and getattr(data, "users", 1) == 0:
                (bpy.data.cameras if object_type == "CAMERA" else bpy.data.lights).remove(data)
            raise
    affected = [{"type": "Object", "name": obj.name}]
    if obj.data is not None:
        affected.append({"type": obj.data.bl_rna.identifier, "name": obj.data.name})
    transaction = new_transaction(affected)
    try:
        _update_view_layer(bpy)
        detail = _object_detail(obj)
    except Exception as exc:
        failed_name = obj.name
        cleanup_error = None
        try:
            _remove_new_object(bpy, obj, remove_data=True)
        except Exception as cleanup_exc:
            cleanup_error = cleanup_exc
        transaction.update(
            object=failed_name,
            requested_name=requested_name,
            resolved_name=failed_name,
            changed=cleanup_error is not None,
            journal=[{"op": "create", "target": failed_name, "before": None, "after": None, "status": "failed"}],
        )
        if cleanup_error is not None:
            transaction["warnings"].append(f"create cleanup failed: {cleanup_error}")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"object.create failed while summarizing the new object: {exc}", transaction) from exc
    transaction.update(
        object=obj.name,
        requested_name=requested_name,
        resolved_name=obj.name,
        detail=detail,
        changed=True,
        journal=[{"op": "create", "target": obj.name, "before": None, "after": detail, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def duplicate_object(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "name", "linked", "collection", "expected_state"})
    source_name = _name(params.get("object_name"), "object_name")
    requested_name = _optional_name(params.get("name"), "name")
    linked = _strict_bool(params.get("linked"), "linked", False)
    collection_name = _optional_name(params.get("collection"), "collection")
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    source = _scene_object(bpy, source_name)
    collection = _target_collection(bpy, collection_name)
    resolved_name = _unique_object_name(
        bpy.data.objects, requested_name or f"{source.name}_copy"
    )
    copy = None
    copied_data = None
    try:
        copy = source.copy()
        if source.data is not None and not linked:
            copied_data = source.data.copy()
            copy.data = copied_data
        copy.name = resolved_name
        collection.objects.link(copy)
    except Exception:
        if copy is not None and bpy.data.objects.get(copy.name) is copy:
            bpy.data.objects.remove(copy, do_unlink=True)
        if copied_data is not None and getattr(copied_data, "users", 1) == 0:
            data_collection = _data_collection(bpy, copied_data)
            if data_collection is not None:
                data_collection.remove(copied_data)
        raise
    transaction = new_transaction([{"type": "Object", "name": source.name}, {"type": "Object", "name": copy.name}])
    try:
        _update_view_layer(bpy)
        detail = _object_detail(copy)
    except Exception as exc:
        failed_name = copy.name
        cleanup_error = None
        try:
            _remove_new_object(bpy, copy, remove_data=not linked)
        except Exception as cleanup_exc:
            cleanup_error = cleanup_exc
        transaction.update(
            source=source.name,
            object=failed_name,
            requested_name=requested_name,
            resolved_name=failed_name,
            linked=linked,
            changed=cleanup_error is not None,
            journal=[{"op": "duplicate", "target": failed_name, "before": None, "after": None, "status": "failed"}],
        )
        if cleanup_error is not None:
            transaction["warnings"].append(f"duplicate cleanup failed: {cleanup_error}")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"object.duplicate failed while summarizing the new object: {exc}", transaction) from exc
    transaction.update(
        source=source.name,
        object=copy.name,
        requested_name=requested_name,
        resolved_name=copy.name,
        linked=linked,
        detail=detail,
        changed=True,
        journal=[{"op": "duplicate", "target": copy.name, "before": None, "after": detail, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def rename_object(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "name", "allow_suffix", "expected_state"})
    old_name = _name(params.get("object_name"), "object_name")
    requested_name = _name(params.get("name"), "name")
    allow_suffix = _strict_bool(params.get("allow_suffix"), "allow_suffix", False)
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, old_name)
    conflict = bpy.data.objects.get(requested_name)
    if conflict is not None and conflict is not obj and not allow_suffix:
        raise NameConflictError(
            f"Object name is already taken: {requested_name}",
            {"conflict": {"type": "Object", "name": conflict.name}},
        )
    before = obj.name
    if before != requested_name:
        obj.name = (
            _unique_object_name(bpy.data.objects, requested_name)
            if conflict is not None and conflict is not obj
            else requested_name
        )
    after = obj.name
    changed = before != after
    transaction = new_transaction([{"type": "Object", "name": before}])
    transaction.update(
        object=after,
        requested_name=requested_name,
        resolved_name=after,
        changed=changed,
        journal=[{"op": "rename", "target": before, "before": before, "after": after, "status": "applied" if changed else "present"}],
    )
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def set_object_visibility(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"objects", "hide_viewport", "hide_render", "hide_select", "expected_state"})
    names = _object_names(params.get("objects"))
    values = {}
    for field in ("hide_viewport", "hide_render", "hide_select"):
        if field in params:
            values[field] = _strict_bool(params[field], field)
    if not values:
        raise InvalidParamsError("At least one visibility field is required")
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    objects = [_scene_object(bpy, name) for name in names]
    transaction = new_transaction([{"type": "Object", "name": obj.name} for obj in objects])
    journal = [
        {
            "op": "set_visibility",
            "target": obj.name,
            "before": {field: bool(getattr(obj, field)) for field in values},
            "after": dict(values),
            "status": "pending",
        }
        for obj in objects
    ]
    transaction.update(objects=[obj.name for obj in objects], changed=False, journal=journal)
    for obj, entry in zip(objects, journal):
        before = entry["before"]
        after = entry["after"]
        changed = before != after
        try:
            if changed:
                for field, value in values.items():
                    setattr(obj, field, value)
            entry["status"] = "applied" if changed else "present"
        except Exception as exc:
            entry["status"] = "failed"
            transaction["changed"] = any(
                item["status"] == "applied" for item in journal
            ) or any(bool(getattr(obj, field)) != before[field] for field in values)
            if transaction["changed"]:
                bump_epoch()
            transaction["state_token"] = current_state_token(bpy)
            raise MutationApplyError(
                f"object.set_visibility failed while updating {obj.name}: {exc}",
                transaction,
            ) from exc
    transaction["changed"] = any(item["status"] == "applied" for item in journal)
    if transaction["changed"]:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _would_create_parent_cycle(obj: Any, parent: Any | None) -> bool:
    current = parent
    while current is not None:
        if current is obj:
            return True
        current = current.parent
    return False


def parent_object(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "parent", "keep_transform", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    if "parent" not in params:
        raise InvalidParamsError("parent is required and must be an object name or null")
    parent_name = _optional_name(params.get("parent"), "parent")
    keep_transform = _strict_bool(params.get("keep_transform"), "keep_transform", True)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    _validate_expected(params)
    bpy = get_bpy()
    if not dry_run:
        require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    parent = _scene_object(bpy, parent_name) if parent_name is not None else None
    if _would_create_parent_cycle(obj, parent):
        raise InvalidParamsError("parent would create an object hierarchy cycle")
    _update_view_layer(bpy)
    before = {"parent": obj.parent.name if obj.parent else None, "local_transform": _local_transform(obj), "world_transform": _world_transform(obj)}
    changed = obj.parent is not parent
    after_plan = {
        "parent": parent.name if parent else None,
        "keep_transform": keep_transform,
        "world_transform": before["world_transform"] if keep_transform else None,
    }
    journal = [{"op": "parent" if parent else "unparent", "target": obj.name, "before": before, "after": after_plan, "status": "planned" if changed and dry_run else ("pending" if changed else "present")}]
    affected = [{"type": "Object", "name": obj.name}]
    if parent is not None:
        affected.append({"type": "Object", "name": parent.name})
    transaction, apply = prepare_destructive_action(
        action="object.parent",
        affected_datablocks=affected,
        journal=journal,
        dry_run=dry_run,
        require_confirm=False,
        state_token=current_state_token(bpy),
    )
    transaction.update(object=obj.name, parent=parent.name if parent else None, keep_transform=keep_transform)
    if not apply or not changed:
        return transaction
    old_parent = obj.parent
    old_parent_inverse = obj.matrix_parent_inverse.copy()
    old_basis = obj.matrix_basis.copy()
    world = obj.matrix_world.copy()
    try:
        obj.parent = parent
        if keep_transform:
            obj.matrix_world = world
        else:
            obj.matrix_parent_inverse.identity()
        _update_view_layer(bpy)
        if keep_transform and _matrix_max_delta(obj.matrix_world, world) > 1e-5:
            raise RuntimeError("Blender could not preserve the requested world transform")
        journal[0]["after"] = {"parent": parent.name if parent else None, "local_transform": _local_transform(obj), "world_transform": _world_transform(obj)}
        journal[0]["status"] = "applied"
        transaction["changed"] = True
    except Exception as exc:
        rollback_error = None
        try:
            obj.parent = old_parent
            obj.matrix_parent_inverse = old_parent_inverse
            obj.matrix_basis = old_basis
            _update_view_layer(bpy)
        except Exception as rollback_exc:
            rollback_error = rollback_exc
        journal[0]["status"] = "failed"
        transaction["changed"] = rollback_error is not None
        if rollback_error is not None:
            transaction["warnings"].append(f"parent rollback failed: {rollback_error}")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"object.parent failed: {exc}", transaction) from exc
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def set_object_mode(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"mode", "object_name", "dry_run", "expected_state"})
    mode = _name(params.get("mode"), "mode").upper()
    if mode not in SUPPORTED_OBJECT_MODES:
        raise InvalidParamsError(f"mode must be one of {', '.join(sorted(SUPPORTED_OBJECT_MODES))}")
    object_name = _optional_name(params.get("object_name"), "object_name")
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    active = bpy.context.view_layer.objects.active
    if object_name is not None:
        requested = _scene_object(bpy, object_name)
        if active is not requested and not dry_run:
            raise InvalidStateError("object.mode_set object_name must already be the active object")
        active = requested
    if active is None:
        raise InvalidStateError("object.mode_set requires an active object")
    before = str(bpy.context.mode)
    changed = before != mode and not (mode == "EDIT" and before.startswith("EDIT_"))
    journal = [{"op": "mode_set", "target": active.name, "before": before, "after": mode, "status": "planned" if changed else "present"}]
    transaction, apply = prepare_destructive_action(
        action="object.mode_set",
        affected_datablocks=[{"type": "Object", "name": active.name}],
        journal=journal,
        dry_run=dry_run,
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": active.name, "before_mode": before, "requested_mode": mode},
    )
    if not apply or not changed:
        return transaction
    require_document_write_access(bpy)
    try:
        result = bpy.ops.object.mode_set(mode=mode)
    except Exception as exc:
        journal[0]["status"] = "failed"
        transaction["changed"] = str(bpy.context.mode) != before
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"object.mode_set failed: {exc}", transaction) from exc
    after = str(bpy.context.mode)
    if "FINISHED" not in result or (after != mode and not (mode == "EDIT" and after.startswith("EDIT_"))):
        journal[0]["status"] = "failed"
        transaction["changed"] = after != before
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"object.mode_set did not enter requested mode {mode}", transaction)
    journal[0]["status"] = "applied"
    transaction.update(changed=True, after_mode=after)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _world_to_local_matrix(obj: Any, desired_world: Any) -> Any:
    """Convert a desired world matrix to object basis space, including parent inverse."""
    if obj.parent is None:
        return desired_world
    effective_parent = obj.parent.matrix_world @ obj.matrix_parent_inverse
    return effective_parent.inverted_safe() @ desired_world


def _workplane_axis_mapping(value: Any) -> tuple[list[tuple[int, float]], list[str]]:
    raw = ["U", "V", "N"] if value is None else value
    if not isinstance(raw, list) or len(raw) != 3:
        raise InvalidParamsError("axis_map must contain exactly three signed workplane axes")
    indices = {"U": 0, "V": 1, "N": 2}
    mapping: list[tuple[int, float]] = []
    normalized: list[str] = []
    for item in raw:
        name = _name(item, "axis_map entry").upper()
        sign = -1.0 if name.startswith("-") else 1.0
        axis = name[1:] if name.startswith("-") else name
        if axis not in indices:
            raise InvalidParamsError("axis_map entries must be U, V, N, -U, -V, or -N")
        mapping.append((indices[axis], sign))
        normalized.append(f"-{axis}" if sign < 0.0 else axis)
    absolute_axes = [index for index, _sign in mapping]
    if len(set(absolute_axes)) != 3:
        raise InvalidParamsError("axis_map must use U, V, and N exactly once")
    inversions = sum(
        1
        for left in range(3)
        for right in range(left + 1, 3)
        if absolute_axes[left] > absolute_axes[right]
    )
    determinant_sign = (-1 if inversions % 2 else 1)
    for _index, sign in mapping:
        determinant_sign *= int(sign)
    if determinant_sign != 1:
        raise InvalidParamsError("axis_map must define a right-handed rotation without reflection")
    return mapping, normalized


def _matrix_transform(matrix: Any, rotation_order: str) -> dict[str, list[float]]:
    location, rotation, scale = matrix.decompose()
    return {
        "location": vector_values(location),
        "rotation_euler": vector_values(rotation.to_euler(rotation_order)),
        "scale": vector_values(scale),
    }


def _world_workplane_frame(reference: Any, plane: Any) -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    if not isinstance(plane, dict):
        raise InvalidParamsError("plane must be an object")
    source = str(plane.get("source", "")).upper()
    if source == "FACE" and (reference.type != "MESH" or reference.data is None):
        raise InvalidStateError("FACE workplane reference_object must be a mesh")
    import bmesh

    from .mesh_edit import _workplane_frame

    bm = bmesh.new()
    try:
        if reference.type == "MESH" and reference.data is not None:
            bm.from_mesh(reference.data)
        origin, u_axis, _v_axis, normal, local_details = _workplane_frame(bm, plane)
    finally:
        bm.free()

    world_matrix = reference.matrix_world
    linear = world_matrix.to_3x3()
    world_origin = world_matrix @ origin
    world_normal = linear.inverted_safe().transposed() @ normal
    world_u = linear @ u_axis
    if world_normal.length_squared <= 1e-20:
        raise InvalidStateError("reference_object transform makes the workplane normal degenerate")
    world_normal.normalize()
    world_u = world_u - world_normal * float(world_u.dot(world_normal))
    if world_u.length_squared <= 1e-20:
        raise InvalidStateError("reference_object transform makes the workplane U axis degenerate")
    world_u.normalize()
    world_v = world_normal.cross(world_u)
    if world_v.length_squared <= 1e-20:
        raise InvalidStateError("reference_object transform could not form a workplane V axis")
    world_v.normalize()
    world_u = world_v.cross(world_normal).normalized()
    details = {
        "reference_object": reference.name,
        "local": local_details,
        "world": {
            "origin": vector_values(world_origin),
            "u_axis": vector_values(world_u),
            "v_axis": vector_values(world_v),
            "normal": vector_values(world_normal),
        },
    }
    return world_origin, world_u, world_v, world_normal, details


def align_object_to_workplane(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(
        params,
        {
            "object_name", "reference_object", "plane", "offset", "axis_map",
            "align_location", "align_rotation", "dry_run", "expected_state",
        },
    )
    object_name = _name(params.get("object_name"), "object_name")
    reference_name = _name(params.get("reference_object"), "reference_object")
    offset = _optional_vector3(params, "offset", (0.0, 0.0, 0.0))
    mapping, normalized_mapping = _workplane_axis_mapping(params.get("axis_map"))
    align_location = _strict_bool(params.get("align_location"), "align_location", True)
    align_rotation = _strict_bool(params.get("align_rotation"), "align_rotation", True)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    if not align_location and not align_rotation:
        raise InvalidParamsError("At least one of align_location or align_rotation must be true")
    if not align_location and any(abs(value) > 0.0 for value in offset):
        raise InvalidParamsError("offset requires align_location true")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("object.align_to_workplane requires Blender Object mode")
    obj = _scene_object(bpy, object_name)
    reference = _scene_object(bpy, reference_name)
    _update_view_layer(bpy)
    before = {"local": _local_transform(obj), "world": _world_transform(obj)}
    origin, u_axis, v_axis, normal, frame = _world_workplane_frame(
        reference, params.get("plane")
    )

    from mathutils import Matrix

    basis = (u_axis, v_axis, normal)
    columns = [basis[index] * sign for index, sign in mapping]
    rotation_matrix = Matrix((columns[0], columns[1], columns[2])).transposed()
    current_location, current_rotation, current_scale = obj.matrix_world.decompose()
    desired_location = (
        origin + u_axis * offset[0] + v_axis * offset[1] + normal * offset[2]
        if align_location
        else current_location
    )
    desired_rotation = rotation_matrix.to_quaternion() if align_rotation else current_rotation
    desired_world = Matrix.LocRotScale(desired_location, desired_rotation, current_scale)
    desired_basis = _world_to_local_matrix(obj, desired_world)
    planned = {
        "local": _matrix_transform(desired_basis, _rotation_order(obj)),
        "world": _matrix_transform(desired_world, _rotation_order(obj)),
    }
    would_change = _matrix_max_delta(obj.matrix_world, desired_world) > 1e-8
    journal = [
        {
            "op": "align_to_workplane",
            "target": obj.name,
            "before": before,
            "after": planned,
            "status": "planned" if would_change else "present",
        }
    ]
    transaction, apply = prepare_destructive_action(
        action="object.align_to_workplane",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=journal,
        dry_run=dry_run,
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={
            "object": obj.name,
            "reference_object": reference.name,
            "frame": frame,
            "axis_map": normalized_mapping,
            "offset": offset,
            "align_location": align_location,
            "align_rotation": align_rotation,
            "planned": planned,
            "would_change": would_change,
        },
    )
    if not apply or not would_change:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    old_basis = obj.matrix_basis.copy()
    try:
        obj.matrix_basis = desired_basis
        _update_view_layer(bpy)
        if _matrix_max_delta(obj.matrix_world, desired_world) > 1e-5:
            raise RuntimeError("Blender could not realize the requested workplane transform")
        after = {"local": _local_transform(obj), "world": _world_transform(obj)}
        journal[0]["after"] = after
        journal[0]["status"] = "applied"
        transaction.update(local=after["local"], world=after["world"], changed=True)
    except Exception as exc:
        rollback_error = None
        try:
            obj.matrix_basis = old_basis
            _update_view_layer(bpy)
        except Exception as rollback_exc:
            rollback_error = rollback_exc
        journal[0]["status"] = "failed"
        transaction["changed"] = rollback_error is not None
        if rollback_error is not None:
            transaction["warnings"].append(f"workplane alignment rollback failed: {rollback_error}")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(
            f"object.align_to_workplane failed: {exc}", transaction
        ) from exc
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def transform_object(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "location", "rotation_euler", "scale", "space", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    space = params.get("space", "local")
    if space not in {"local", "world"}:
        raise InvalidParamsError("space must be 'local' or 'world'")
    changes = {field: _vector3(params[field], field) for field in ("location", "rotation_euler", "scale") if field in params}
    _validate_expected(params)
    bpy = get_bpy()
    if changes:
        require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    obj = _scene_object(bpy, object_name)
    if "rotation_euler" in changes and _rotation_order(obj) != str(getattr(obj, "rotation_mode", "XYZ")):
        raise InvalidStateError("rotation_euler cannot patch an object using quaternion or axis-angle rotation mode")
    _update_view_layer(bpy)
    before = {"local": _local_transform(obj), "world": _world_transform(obj)}
    if not changes:
        return {"object": obj.name, "local": before["local"], "world": before["world"], "changed": False, "state_token": current_state_token(bpy)}
    transaction = new_transaction([{"type": "Object", "name": obj.name}])
    old_basis = obj.matrix_basis.copy()
    try:
        if space == "local":
            for field, value in changes.items():
                setattr(obj, field, value)
        else:
            from mathutils import Euler, Matrix

            world = before["world"]
            location = changes.get("location", world["location"])
            rotation = changes.get("rotation_euler", world["rotation_euler"])
            scale = changes.get("scale", world["scale"])
            desired_world = Matrix.LocRotScale(
                location,
                Euler(rotation, _rotation_order(obj)).to_quaternion(),
                scale,
            )
            obj.matrix_basis = _world_to_local_matrix(obj, desired_world)
        _update_view_layer(bpy)
        if space == "world" and _matrix_max_delta(obj.matrix_world, desired_world) > 1e-5:
            raise RuntimeError("Blender could not realize the requested world transform")
        after = {"local": _local_transform(obj), "world": _world_transform(obj)}
    except Exception as exc:
        rollback_error = None
        try:
            obj.matrix_basis = old_basis
            _update_view_layer(bpy)
        except Exception as rollback_exc:
            rollback_error = rollback_exc
        transaction.update(
            object=obj.name,
            space=space,
            changed=rollback_error is not None,
            journal=[{"op": "transform", "target": obj.name, "before": before, "after": None, "status": "failed"}],
        )
        if rollback_error is not None:
            transaction["warnings"].append(f"transform rollback failed: {rollback_error}")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"object.transform failed: {exc}", transaction) from exc
    changed = before != after
    transaction.update(
        object=obj.name,
        space=space,
        local=after["local"],
        world=after["world"],
        changed=changed,
        journal=[{"op": "transform", "target": obj.name, "before": before, "after": after, "status": "applied" if changed else "present"}],
    )
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _data_identifier(data: Any) -> str:
    return str(getattr(getattr(data, "bl_rna", None), "identifier", type(data).__name__))


def _delete_report(objects: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    deleted = set(objects)
    data_users: dict[Any, int] = {}
    for obj in objects:
        if obj.data is not None:
            data_users[obj.data] = data_users.get(obj.data, 0) + 1
    candidates = []
    for data, removed_users in sorted(data_users.items(), key=lambda item: item[0].name):
        remaining = max(0, int(data.users) - removed_users)
        candidates.append(
            {
                "type": _data_identifier(data),
                "name": data.name,
                "users_before": int(data.users),
                "users_after_estimate": remaining,
                "orphan_candidate": remaining == 0,
                "will_remove": False,
                "reason": "retained for Blender orphan-data management",
            }
        )
    dependent_children = sorted(
        child.name
        for obj in objects
        for child in obj.children
        if child not in deleted
    )
    return candidates, dependent_children


def delete_object(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"objects", "dry_run", "confirm", "expected_state"})
    names = _object_names(params.get("objects"))
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    objects = [_scene_object(bpy, name) for name in names]
    _update_view_layer(bpy)
    data_refs = {
        (_data_identifier(obj.data), obj.data.name): obj.data
        for obj in objects
        if obj.data is not None
    }
    candidates, dependent_children = _delete_report(objects)
    details = {obj.name: _object_detail(obj) for obj in objects}
    journal = [
        {"op": "delete", "target": obj.name, "before": details[obj.name], "after": None, "status": "planned" if dry_run else "pending"}
        for obj in objects
    ]
    affected = [{"type": "Object", "name": obj.name} for obj in objects]
    affected.extend({"type": item["type"], "name": item["name"]} for item in candidates)
    transaction, apply = prepare_destructive_action(
        action="object.delete",
        affected_datablocks=affected,
        journal=journal,
        dry_run=dry_run,
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={
            "objects": [obj.name for obj in objects],
            "orphaned_data_candidates": candidates,
            "dependent_children": dependent_children,
            "removed_data": [],
            "retained_data": [item["name"] for item in candidates],
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    for index, obj in enumerate(objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            journal[index]["status"] = "applied"
            transaction["changed"] = True
        except Exception as exc:
            journal[index]["status"] = "failed"
            if transaction["changed"]:
                bump_epoch()
            transaction["state_token"] = current_state_token(bpy)
            raise MutationApplyError(f"object.delete failed while removing {obj.name}: {exc}", transaction) from exc
    for candidate in candidates:
        data = data_refs[(candidate["type"], candidate["name"])]
        candidate["users_after"] = int(data.users)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
