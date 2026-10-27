"""Bounded image lifecycle, material-slot, and texture-binding tools."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..errors import InvalidParamsError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


IMAGE_EXTENSIONS = frozenset({".bmp", ".cin", ".dpx", ".exr", ".hdr", ".jpeg", ".jpg", ".png", ".psd", ".sgi", ".tga", ".tif", ".tiff", ".webp"})
ALPHA_MODES = frozenset({"STRAIGHT", "PREMUL", "CHANNEL_PACKED", "NONE"})
INTERPOLATIONS = frozenset({"Closest", "Linear", "Cubic", "Smart"})
EXTENSIONS = frozenset({"REPEAT", "EXTEND", "CLIP"})
PROJECTIONS = frozenset({"FLAT", "BOX", "SPHERE", "TUBE"})
DEFAULT_PIXEL_BUDGET = 16_777_216
HARD_PIXEL_BUDGET = 268_435_456
DEFAULT_POLYGON_BUDGET = 250_000
HARD_POLYGON_BUDGET = 5_000_000


def _unknown(params: dict[str, Any], allowed: set[str], label: str = "parameters") -> None:
    extra = sorted(set(params) - allowed)
    if extra:
        raise InvalidParamsError(f"Unknown {label}: {', '.join(extra)}")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


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


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidParamsError(f"{label} must be a finite number")
    return float(value)


def _vector(value: Any, label: str, size: int, *, minimum: float | None = None, maximum: float | None = None) -> list[float]:
    if not isinstance(value, list) or len(value) != size:
        raise InvalidParamsError(f"{label} must be a {size}-number array")
    result = [_finite(item, label) for item in value]
    if minimum is not None and any(item < minimum for item in result):
        raise InvalidParamsError(f"{label} values must be at least {minimum}")
    if maximum is not None and any(item > maximum for item in result):
        raise InvalidParamsError(f"{label} values must be at most {maximum}")
    return result


def _validate_expected(params: dict[str, Any]) -> None:
    value = params.get("expected_state")
    if value is not None and (not isinstance(value, str) or not value):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _cursor(params: dict[str, Any], maximum: int = 2000) -> tuple[int, int]:
    limit = _integer(params.get("limit", 200), "limit", 1, maximum)
    value = params.get("cursor")
    if value is None:
        return limit, 0
    if isinstance(value, str) and 1 <= len(value) <= 18 and value.isascii() and value.isdecimal():
        return limit, int(value)
    raise InvalidParamsError("cursor must be a non-negative decimal string returned by this inspect tool")


def _image(bpy: Any, name: Any):
    resolved = bpy.data.images.get(_name(name, "image"))
    if resolved is None:
        raise NotFoundError(f"Image not found: {name}")
    return resolved


def _image_summary(image: Any, include_filepath: bool = False) -> dict[str, Any]:
    size = [int(item) for item in image.size]
    result = {
        "name": image.name,
        "size": size,
        "pixel_count": size[0] * size[1] if len(size) == 2 else 0,
        "source": image.source,
        "colorspace": image.colorspace_settings.name,
        "alpha_mode": image.alpha_mode,
        "packed": image.packed_file is not None,
        "user_count": int(image.users),
        "use_fake_user": bool(image.use_fake_user),
        "is_dirty": bool(image.is_dirty),
    }
    if include_filepath:
        result["filepath"] = image.filepath
    return result


def inspect_image(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"image", "include_filepath", "limit", "cursor"})
    include_filepath = _strict_bool(params.get("include_filepath"), "include_filepath", False)
    limit, offset = _cursor(params)
    bpy = get_bpy()
    if params.get("image") is not None:
        images = [_image(bpy, params["image"])]
    else:
        images = sorted(bpy.data.images, key=lambda item: item.name)
    page = images[offset : offset + limit]
    next_offset = offset + len(page)
    return ToolResult(data={
        "count": len(images),
        "images": [_image_summary(item, include_filepath) for item in page],
        "truncated": next_offset < len(images),
        "next_cursor": str(next_offset) if next_offset < len(images) else None,
        "state_token": current_state_token(bpy),
    })


def load_image(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"filepath", "name", "check_existing", "expected_state"})
    filepath = _name(params.get("filepath"), "filepath")
    name = params.get("name")
    if name is not None:
        name = _name(name, "name")
    check_existing = _strict_bool(params.get("check_existing"), "check_existing", True)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    resolved = Path(bpy.path.abspath(filepath)).expanduser().resolve()
    if resolved.suffix.lower() not in IMAGE_EXTENSIONS:
        raise InvalidParamsError("filepath extension is not in the image allowlist")
    if not resolved.is_file():
        raise NotFoundError(f"Image file not found: {resolved}")
    require_document_write_access(bpy)
    before = list(bpy.data.images)
    transaction = new_transaction([{"type": "ImageFile", "name": resolved.name}])
    transaction.update(action="image.load", source_file=resolved.name, journal=[])
    loaded = None
    try:
        loaded = bpy.data.images.load(str(resolved), check_existing=check_existing)
        created = not any(item == loaded for item in before)
        old_name = loaded.name
        if name is not None:
            if not created and name != loaded.name:
                raise InvalidParamsError("name cannot rename an existing image returned by check_existing")
            collision = bpy.data.images.get(name)
            if collision is not None and collision != loaded:
                raise InvalidParamsError(f"Image name already exists: {name}")
            loaded.name = name
        transaction["journal"].append({"op": "image.load", "target": loaded.name, "created": created, "renamed_from": old_name if old_name != loaded.name else None, "status": "applied"})
    except Exception as exc:
        if loaded is not None and not any(item == loaded for item in before):
            try:
                bpy.data.images.remove(loaded)
            except Exception:
                transaction["warnings"].append("New image datablock could not be removed during rollback")
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"image.load failed: {exc}", transaction) from exc
    transaction["changed"] = bool(transaction["journal"][0]["created"] or transaction["journal"][0]["renamed_from"])
    transaction["image"] = _image_summary(loaded)
    if transaction["changed"]:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def create_image(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"name", "width", "height", "color", "alpha", "float_buffer", "is_data", "max_pixels", "expected_state"})
    name = _name(params.get("name"), "name")
    width = _integer(params.get("width"), "width", 1, 32768)
    height = _integer(params.get("height"), "height", 1, 32768)
    color = _vector(params.get("color", [0.0, 0.0, 0.0, 1.0]), "color", 4, minimum=0.0, maximum=1.0)
    alpha = _strict_bool(params.get("alpha"), "alpha", True)
    float_buffer = _strict_bool(params.get("float_buffer"), "float_buffer", False)
    is_data = _strict_bool(params.get("is_data"), "is_data", False)
    max_pixels = _integer(params.get("max_pixels", DEFAULT_PIXEL_BUDGET), "max_pixels", 1, HARD_PIXEL_BUDGET)
    if width * height > max_pixels:
        raise InvalidParamsError(f"image.create would allocate {width * height} pixels, above max_pixels {max_pixels}")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.data.images.get(name) is not None:
        raise InvalidParamsError(f"Image name already exists: {name}")
    require_document_write_access(bpy)
    transaction = new_transaction([{"type": "Image", "name": name}])
    transaction.update(action="image.create", journal=[])
    image = None
    try:
        image = bpy.data.images.new(name, width=width, height=height, alpha=alpha, float_buffer=float_buffer, is_data=is_data)
        image.generated_color = color
        transaction["journal"].append({"op": "image.create", "target": image.name, "status": "applied"})
    except Exception as exc:
        if image is not None:
            try:
                bpy.data.images.remove(image)
            except Exception:
                transaction["warnings"].append("New image datablock could not be removed during rollback")
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"image.create failed: {exc}", transaction) from exc
    transaction["changed"] = True
    transaction["image"] = _image_summary(image)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def patch_image(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"image", "colorspace", "alpha_mode", "use_fake_user", "expected_state"})
    image_name = _name(params.get("image"), "image")
    if not any(key in params for key in ("colorspace", "alpha_mode", "use_fake_user")):
        raise InvalidParamsError("image.patch requires at least one patch field")
    colorspace = params.get("colorspace")
    if colorspace is not None:
        colorspace = _name(colorspace, "colorspace")
    alpha_mode = params.get("alpha_mode")
    if alpha_mode is not None and alpha_mode not in ALPHA_MODES:
        raise InvalidParamsError("alpha_mode must be STRAIGHT, PREMUL, CHANNEL_PACKED, or NONE")
    fake_user = params.get("use_fake_user")
    if fake_user is not None:
        fake_user = _strict_bool(fake_user, "use_fake_user")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    image = _image(bpy, image_name)
    before = {"colorspace": image.colorspace_settings.name, "alpha_mode": image.alpha_mode, "use_fake_user": bool(image.use_fake_user)}
    after = dict(before)
    if colorspace is not None:
        after["colorspace"] = colorspace
    if alpha_mode is not None:
        after["alpha_mode"] = alpha_mode
    if fake_user is not None:
        after["use_fake_user"] = fake_user
    changed = before != after
    if not changed:
        return {"image": image.name, "before": before, "after": after, "changed": False, "state_token": current_state_token(bpy)}
    require_document_write_access(bpy)
    transaction = new_transaction([{"type": "Image", "name": image.name}])
    transaction.update(action="image.patch", image=image.name, before=before, after=after, journal=[])
    try:
        image.colorspace_settings.name = after["colorspace"]
        image.alpha_mode = after["alpha_mode"]
        image.use_fake_user = after["use_fake_user"]
    except Exception as exc:
        rollback_failed = False
        try:
            image.colorspace_settings.name = before["colorspace"]
            image.alpha_mode = before["alpha_mode"]
            image.use_fake_user = before["use_fake_user"]
        except Exception:
            rollback_failed = True
        if rollback_failed:
            transaction["warnings"].append("Rollback was incomplete")
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"image.patch failed: {exc}", transaction) from exc
    transaction["changed"] = True
    transaction["journal"].append({"op": "image.patch", "target": image.name, "before": before, "after": after, "status": "applied"})
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _material_object(bpy: Any, name: Any, *, mutate: bool = False):
    object_name = _name(name, "object_name")
    obj = bpy.context.scene.objects.get(object_name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {object_name}")
    data = getattr(obj, "data", None)
    if data is None or not hasattr(data, "materials"):
        raise InvalidParamsError(f"Object does not support material slots: {object_name}")
    if mutate and int(data.users) > 1:
        raise InvalidParamsError("Object data is shared; make it single-user before changing material slots")
    return obj


def _slot_summary(obj: Any, index: int) -> dict[str, Any]:
    slot = obj.material_slots[index]
    material = slot.material
    polygons = getattr(obj.data, "polygons", ())
    return {
        "index": index,
        "slot_name": slot.name,
        "material": material.name if material is not None else None,
        "material_user_count": int(material.users) if material is not None else 0,
        "polygon_count": sum(1 for polygon in polygons if polygon.material_index == index),
    }


def inspect_material_slots(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"object_name", "limit", "cursor"})
    limit, offset = _cursor(params, 1000)
    bpy = get_bpy()
    obj = _material_object(bpy, params.get("object_name"))
    count = len(obj.material_slots)
    stop = min(count, offset + limit)
    return ToolResult(data={
        "object": obj.name,
        "object_data": obj.data.name,
        "data_user_count": int(obj.data.users),
        "slot_count": count,
        "slots": [_slot_summary(obj, index) for index in range(offset, stop)],
        "truncated": stop < count,
        "next_cursor": str(stop) if stop < count else None,
        "state_token": current_state_token(bpy),
    })


def assign_material_slot(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "material", "slot_index", "expected_state"})
    material_name = _name(params.get("material"), "material")
    slot_index = params.get("slot_index")
    if slot_index is not None:
        slot_index = _integer(slot_index, "slot_index", 0, 999)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _material_object(bpy, params.get("object_name"), mutate=True)
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise NotFoundError(f"Material not found: {material_name}")
    materials = obj.data.materials
    if slot_index is None:
        slot_index = len(materials)
    if slot_index > len(materials):
        raise InvalidParamsError("slot_index may address an existing slot or equal slot_count to append")
    before = materials[slot_index].name if slot_index < len(materials) and materials[slot_index] is not None else None
    append = slot_index == len(materials)
    if not append and materials[slot_index] == material:
        return {"object": obj.name, "slot_index": slot_index, "material": material.name, "changed": False, "state_token": current_state_token(bpy)}
    require_document_write_access(bpy)
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "Material", "name": material.name}])
    transaction.update(action="material.slot_assign", object=obj.name, slot_index=slot_index, journal=[])
    try:
        if append:
            materials.append(material)
        else:
            materials[slot_index] = material
    except Exception as exc:
        try:
            if append and len(materials) > slot_index:
                materials.pop(index=slot_index)
            elif not append:
                materials[slot_index] = bpy.data.materials.get(before) if before is not None else None
        except Exception:
            transaction["warnings"].append("Material-slot rollback was incomplete")
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"material.slot_assign failed: {exc}", transaction) from exc
    transaction["changed"] = True
    transaction["journal"].append({"op": "material.slot_assign", "slot_index": slot_index, "before": before, "after": material.name, "status": "applied"})
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_material_slot(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "slot_index", "max_polygons", "dry_run", "confirm", "expected_state"})
    slot_index = _integer(params.get("slot_index"), "slot_index", 0, 999)
    max_polygons = _integer(params.get("max_polygons", DEFAULT_POLYGON_BUDGET), "max_polygons", 1, HARD_POLYGON_BUDGET)
    _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params.get("confirm"), "confirm")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _material_object(bpy, params.get("object_name"), mutate=True)
    if slot_index >= len(obj.data.materials):
        raise NotFoundError(f"Material slot not found: {slot_index}")
    polygons = list(getattr(obj.data, "polygons", ()))
    if len(polygons) > max_polygons:
        raise InvalidParamsError(f"material.slot_remove would scan {len(polygons)} polygons, above max_polygons {max_polygons}")
    slots_before = [material.name if material is not None else None for material in obj.data.materials]
    indices_before = [int(polygon.material_index) for polygon in polygons]
    journal = [{"op": "material.slot_remove", "slot_index": slot_index, "material": slots_before[slot_index], "affected_polygon_count": sum(1 for value in indices_before if value == slot_index), "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="material.slot_remove",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": obj.data.__class__.__name__, "name": obj.data.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "slot_index": slot_index, "recovery": "Restore a checkpoint or use the retained rollback snapshot if apply reports an error."},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        obj.data.materials.pop(index=slot_index)
    except Exception as exc:
        try:
            obj.data.materials.clear()
            for material_name in slots_before:
                obj.data.materials.append(bpy.data.materials.get(material_name) if material_name is not None else None)
            for polygon, value in zip(polygons, indices_before):
                polygon.material_index = value
        except Exception:
            transaction["warnings"].append("Material slots or polygon assignments could not be fully restored")
            transaction["changed"] = True
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"material.slot_remove failed: {exc}", transaction) from exc
    journal[0]["status"] = "applied"
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _node(nodes: Any, name: str, node_type: str, created: list[Any]):
    node = nodes.get(name)
    if node is not None:
        if node.bl_idname != node_type:
            raise InvalidParamsError(f"Node {name} exists with type {node.bl_idname}, not {node_type}")
        return node
    node = nodes.new(node_type)
    node.name = name
    node.label = name
    created.append(node)
    return node


def _target_socket(nodes: Any, target: Any, default_node: Any, default_input: str, label: str):
    if target is None:
        node, input_name = default_node, default_input
    else:
        if not isinstance(target, dict):
            raise InvalidParamsError(f"{label} must be an object")
        _unknown(target, {"node", "input"}, f"{label} fields")
        node_name = _name(target.get("node"), f"{label}.node")
        input_name = _name(target.get("input"), f"{label}.input")
        node = nodes.get(node_name)
        if node is None:
            raise NotFoundError(f"Target node not found: {node_name}")
    socket = node.inputs.get(input_name) if node is not None else None
    if socket is None:
        raise NotFoundError(f"Target input not found: {input_name}")
    return node, socket


def _mapping(params: dict[str, Any]) -> dict[str, list[float]] | None:
    value = params.get("mapping")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidParamsError("mapping must be an object")
    _unknown(value, {"location", "rotation", "scale"}, "mapping fields")
    return {
        "Location": _vector(value.get("location", [0.0, 0.0, 0.0]), "mapping.location", 3),
        "Rotation": _vector(value.get("rotation", [0.0, 0.0, 0.0]), "mapping.rotation", 3),
        "Scale": _vector(value.get("scale", [1.0, 1.0, 1.0]), "mapping.scale", 3),
    }


def bind_material_texture(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"material", "image", "image_node", "uv_map", "mapping", "color_target", "alpha_target", "interpolation", "extension", "projection", "replace_existing", "allow_shared", "expected_user_count", "dry_run", "confirm", "expected_state"})
    material_name = _name(params.get("material"), "material")
    image_name = _name(params.get("image"), "image")
    image_node_name = _name(params.get("image_node", "CBT Image Texture"), "image_node")
    uv_map = params.get("uv_map")
    if uv_map is not None:
        uv_map = _name(uv_map, "uv_map")
    mapping = _mapping(params)
    if mapping is not None and uv_map is None:
        raise InvalidParamsError("mapping requires uv_map so the vector chain is explicit")
    interpolation = params.get("interpolation", "Linear")
    extension = params.get("extension", "REPEAT")
    projection = params.get("projection", "FLAT")
    if interpolation not in INTERPOLATIONS:
        raise InvalidParamsError("interpolation must be Closest, Linear, Cubic, or Smart")
    if extension not in EXTENSIONS:
        raise InvalidParamsError("extension must be REPEAT, EXTEND, or CLIP")
    if projection not in PROJECTIONS:
        raise InvalidParamsError("projection must be FLAT, BOX, SPHERE, or TUBE")
    replace_existing = _strict_bool(params.get("replace_existing"), "replace_existing", False)
    allow_shared = _strict_bool(params.get("allow_shared"), "allow_shared", False)
    _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params.get("confirm"), "confirm")
    expected_users = params.get("expected_user_count")
    if expected_users is not None:
        expected_users = _integer(expected_users, "expected_user_count", 0, 1_000_000_000)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise NotFoundError(f"Material not found: {material_name}")
    image = _image(bpy, image_name)
    if not material.use_nodes or material.node_tree is None:
        raise InvalidParamsError("material.texture_bind requires a material with use_nodes enabled")
    if expected_users is not None and int(material.users) != expected_users:
        raise InvalidParamsError(f"expected_user_count {expected_users} does not match actual user count {material.users}")
    if int(material.users) > 1 and not allow_shared:
        raise InvalidParamsError("Material is shared; set allow_shared true with expected_user_count after reviewing affected users")
    if int(material.users) > 1 and allow_shared and expected_users is None:
        raise InvalidParamsError("expected_user_count is required when allow_shared is true for a shared material")
    nodes, links = material.node_tree.nodes, material.node_tree.links
    principled = next((node for node in nodes if node.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    color_node, color_socket = _target_socket(nodes, params.get("color_target"), principled, "Base Color", "color_target")
    alpha_target = params.get("alpha_target")
    alpha_node = alpha_socket = None
    if alpha_target is not None:
        alpha_node, alpha_socket = _target_socket(nodes, alpha_target, principled, "Alpha", "alpha_target")
    image_node = nodes.get(image_node_name)
    if image_node is not None and image_node.bl_idname != "ShaderNodeTexImage":
        raise InvalidParamsError(f"Node {image_node_name} is not an image texture node")
    uv_node_name = f"{image_node_name} UV Map"
    mapping_node_name = f"{image_node_name} Mapping"
    uv_node = nodes.get(uv_node_name) if uv_map is not None else None
    if uv_node is not None and uv_node.bl_idname != "ShaderNodeUVMap":
        raise InvalidParamsError(f"Node {uv_node_name} is not a UV Map node")
    mapping_node = nodes.get(mapping_node_name) if mapping is not None else None
    if mapping_node is not None and mapping_node.bl_idname != "ShaderNodeMapping":
        raise InvalidParamsError(f"Node {mapping_node_name} is not a Mapping node")

    def unexpected(socket: Any, expected_node: Any) -> list[Any]:
        return [link for link in socket.links if expected_node is None or link.from_node != expected_node]

    conflicts = unexpected(color_socket, image_node)
    if alpha_socket is not None:
        conflicts += unexpected(alpha_socket, image_node)
    if uv_map is not None and image_node is not None:
        conflicts += unexpected(image_node.inputs["Vector"], mapping_node if mapping is not None else uv_node)
    if mapping_node is not None:
        conflicts += unexpected(mapping_node.inputs["Vector"], uv_node)
    if conflicts and not replace_existing:
        raise InvalidParamsError("Target inputs already have links; set replace_existing true, inspect the dry run, then confirm")

    def linked_from(socket: Any, source_node: Any, output_name: str) -> bool:
        return (
            source_node is not None
            and len(socket.links) == 1
            and socket.links[0].from_node == source_node
            and socket.links[0].from_socket == source_node.outputs.get(output_name)
        )

    would_change = (
        image_node is None
        or image_node.image != image
        or image_node.interpolation != interpolation
        or image_node.extension != extension
        or image_node.projection != projection
        or not linked_from(color_socket, image_node, "Color")
        or (alpha_socket is not None and not linked_from(alpha_socket, image_node, "Alpha"))
    )
    if uv_map is not None:
        vector_node = mapping_node if mapping is not None else uv_node
        would_change = would_change or uv_node is None or getattr(uv_node, "uv_map", "") != uv_map
        would_change = would_change or image_node is None or not linked_from(image_node.inputs["Vector"], vector_node, "Vector" if mapping is not None else "UV")
    if mapping is not None:
        would_change = would_change or mapping_node is None or uv_node is None
        if mapping_node is not None:
            would_change = would_change or any(list(mapping_node.inputs[key].default_value) != value for key, value in mapping.items())
            would_change = would_change or not linked_from(mapping_node.inputs["Vector"], uv_node, "UV")
    journal = [{
        "op": "material.texture_bind",
        "material": material.name,
        "image": image.name,
        "image_node": image_node_name,
        "color_target": {"node": color_node.name, "input": color_socket.name},
        "alpha_target": {"node": alpha_node.name, "input": alpha_socket.name} if alpha_socket is not None else None,
        "uv_map": uv_map,
        "mapping": mapping,
        "replaced_link_count": len(conflicts),
        "would_change": bool(would_change),
        "status": "planned",
    }]
    transaction, apply = prepare_destructive_action(
        action="material.texture_bind",
        affected_datablocks=[{"type": "Material", "name": material.name}, {"type": "Image", "name": image.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=bool(conflicts),
        state_token=current_state_token(bpy),
        extra={"material": material.name, "affected_user_count": int(material.users), "replacement_required": bool(conflicts)},
    )
    if not apply:
        return transaction
    if not would_change:
        journal[0]["status"] = "unchanged"
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    created: list[Any] = []
    link_snapshots: list[tuple[Any, list[tuple[Any, Any]]]] = []
    prior: dict[str, Any] = {}
    try:
        image_node = _node(nodes, image_node_name, "ShaderNodeTexImage", created)
        prior["image"] = image_node.image
        prior["interpolation"] = image_node.interpolation
        prior["extension"] = image_node.extension
        prior["projection"] = image_node.projection
        image_node.image = image
        image_node.interpolation = interpolation
        image_node.extension = extension
        image_node.projection = projection
        vector_source = None
        if uv_map is not None:
            uv_node = _node(nodes, uv_node_name, "ShaderNodeUVMap", created)
            prior["uv_map"] = getattr(uv_node, "uv_map", "")
            uv_node.uv_map = uv_map
            vector_source = uv_node.outputs.get("UV")
            if mapping is not None:
                mapping_node = _node(nodes, mapping_node_name, "ShaderNodeMapping", created)
                prior["mapping"] = {key: list(mapping_node.inputs[key].default_value) for key in mapping}
                for key, value in mapping.items():
                    mapping_node.inputs[key].default_value = value
                link_snapshots.append((mapping_node.inputs["Vector"], [(link.from_socket, link.to_socket) for link in mapping_node.inputs["Vector"].links]))
                for link in list(mapping_node.inputs["Vector"].links):
                    links.remove(link)
                links.new(vector_source, mapping_node.inputs["Vector"])
                vector_source = mapping_node.outputs.get("Vector")
            link_snapshots.append((image_node.inputs["Vector"], [(link.from_socket, link.to_socket) for link in image_node.inputs["Vector"].links]))
            for link in list(image_node.inputs["Vector"].links):
                links.remove(link)
            links.new(vector_source, image_node.inputs["Vector"])
        for target_socket, source_socket in ((color_socket, image_node.outputs.get("Color")), (alpha_socket, image_node.outputs.get("Alpha"))):
            if target_socket is None:
                continue
            existing = list(target_socket.links)
            if len(existing) == 1 and existing[0].from_socket == source_socket:
                continue
            link_snapshots.append((target_socket, [(link.from_socket, link.to_socket) for link in existing]))
            for link in existing:
                links.remove(link)
            links.new(source_socket, target_socket)
    except Exception as exc:
        try:
            existing_uv_node = nodes.get(uv_node_name)
            if existing_uv_node is not None and "uv_map" in prior:
                existing_uv_node.uv_map = prior["uv_map"]
            existing_mapping_node = nodes.get(mapping_node_name)
            if existing_mapping_node is not None and "mapping" in prior:
                for key, value in prior["mapping"].items():
                    existing_mapping_node.inputs[key].default_value = value
            for target_socket, original_links in reversed(link_snapshots):
                for link in list(target_socket.links):
                    links.remove(link)
                for from_socket, to_socket in original_links:
                    if nodes.get(from_socket.node.name) == from_socket.node and nodes.get(to_socket.node.name) == to_socket.node:
                        links.new(from_socket, to_socket)
            for node in list(created):
                if nodes.get(node.name) == node:
                    nodes.remove(node)
            existing_image_node = nodes.get(image_node_name)
            if existing_image_node is not None and existing_image_node.bl_idname == "ShaderNodeTexImage":
                for key in ("image", "interpolation", "extension", "projection"):
                    if key in prior:
                        setattr(existing_image_node, key, prior[key])
        except Exception:
            transaction["warnings"].append("Texture-binding rollback was incomplete; restore a checkpoint before continuing")
            transaction["changed"] = True
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"material.texture_bind failed: {exc}", transaction) from exc
    journal[0]["status"] = "applied"
    transaction["changed"] = True
    transaction["nodes"] = {"image": image_node.name, "uv_map": uv_node_name if uv_map is not None else None, "mapping": mapping_node_name if mapping is not None else None}
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
