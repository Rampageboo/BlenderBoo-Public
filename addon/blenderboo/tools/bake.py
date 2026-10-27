"""Cycles texture baking with explicit targets and state restoration."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ._blender import get_bpy
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction

BAKE_TYPES = frozenset({"NORMAL", "AO", "DIFFUSE", "ROUGHNESS", "EMIT"})
MAX_RESOLUTION = 4096


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise InvalidParamsError(f"{label} must be a finite number from {low} to {high}")
    return float(value)


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidParamsError(f"{label} must be an integer from {low} to {high}")
    return value


def _objects(scene: Any, names: list[str], label: str) -> list[Any]:
    found = []
    for name in names:
        obj = scene.objects.get(name)
        if obj is None:
            raise NotFoundError(f"{label} object not found in scene: {name}")
        if obj.type != "MESH":
            raise InvalidParamsError(f"{label} object must be a mesh: {name}")
        found.append(obj)
    return found


def _coverage(image: Any) -> float:
    """Estimate occupied texels using a bounded, uniform sample of the baked image."""
    width, height = image.size
    count = width * height
    stride = max(1, count // 65536)
    occupied = 0
    sampled = 0
    pixels = image.pixels
    for pixel in range(0, count, stride):
        offset = pixel * 4
        rgba = pixels[offset:offset + 4]
        if len(rgba) == 4 and rgba[3] > 0.001:
            occupied += 1
        sampled += 1
    return occupied / sampled if sampled else 0.0


def run_bake(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {
        "type", "sources", "target", "uv_layer", "image", "resolution", "margin",
        "output_path", "overwrite", "cage_object", "cage_extrusion", "max_ray_distance",
        "normal_g", "expected_state",
    })
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    bake_type = params.get("type")
    if bake_type not in BAKE_TYPES:
        raise InvalidParamsError("type must be NORMAL, AO, DIFFUSE, ROUGHNESS, or EMIT")
    target_name = _name(params.get("target"), "target")
    source_names = params.get("sources", [])
    if not isinstance(source_names, list) or not all(isinstance(name, str) and name for name in source_names) or len(set(source_names)) != len(source_names):
        raise InvalidParamsError("sources must be a list of distinct object names")
    image_name = _name(params.get("image"), "image")
    uv_layer = _name(params.get("uv_layer"), "uv_layer")
    output_path = Path(_name(params.get("output_path"), "output_path")).expanduser().resolve()
    if output_path.suffix.lower() != ".png":
        raise InvalidParamsError("output_path must end in .png")
    if output_path.exists() and params.get("overwrite") is not True:
        raise InvalidParamsError(f"Output already exists; set overwrite true: {output_path}")
    if "overwrite" in params and not isinstance(params["overwrite"], bool):
        raise InvalidParamsError("overwrite must be a boolean")
    resolution = _integer(params.get("resolution", 1024), "resolution", 16, MAX_RESOLUTION)
    margin = _integer(params.get("margin", 16), "margin", 0, 128)
    cage_extrusion = _number(params.get("cage_extrusion", 0.0), "cage_extrusion", 0.0, 1000.0)
    max_ray_distance = _number(params.get("max_ray_distance", 0.0), "max_ray_distance", 0.0, 1000.0)
    normal_g = params.get("normal_g", "POS_Y")
    if normal_g not in {"POS_Y", "NEG_Y"}:
        raise InvalidParamsError("normal_g must be POS_Y or NEG_Y")
    if "expected_state" in params:
        _name(params["expected_state"], "expected_state")

    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    scene = bpy.context.scene
    target = _objects(scene, [target_name], "target")[0]
    sources = _objects(scene, source_names, "source")
    if target in sources:
        raise InvalidParamsError("target cannot also be a source")
    if bool(sources) != bool(source_names):
        raise InvalidParamsError("sources are invalid")
    if target.mode != "OBJECT" or any(obj.mode != "OBJECT" for obj in sources):
        raise InvalidStateError("Bake objects must be in Object mode")
    if target.data.uv_layers.get(uv_layer) is None:
        raise NotFoundError(f"Target UV layer not found: {uv_layer}")
    cage_name = params.get("cage_object")
    cage = None
    if cage_name is not None:
        cage = _objects(scene, [_name(cage_name, "cage_object")], "cage")[0]
        if not sources:
            raise InvalidParamsError("cage_object requires selected-to-active sources")
    materials = {slot.material for slot in target.material_slots if slot.material is not None}
    if not materials or any(not material.use_nodes or material.node_tree is None for material in materials):
        raise InvalidStateError("Target needs node-based materials in all occupied slots")
    image = bpy.data.images.get(image_name)
    if image is not None and tuple(image.size) != (resolution, resolution):
        raise InvalidParamsError("Existing image resolution differs from requested resolution")
    if image is not None and image.source != "GENERATED":
        raise InvalidParamsError("Existing image must be a generated image")
    transaction = new_transaction([{"type": "Object", "name": target.name}, {"type": "Image", "name": image_name}])
    transaction.update(action="bake.run", type=bake_type, target=target.name, output_path=str(output_path), changed=False, state_token=current_state_token(bpy))

    selection = [(obj, obj.select_get()) for obj in scene.objects]
    previous_active = bpy.context.view_layer.objects.active
    previous_engine = scene.render.engine
    previous_uv = target.data.uv_layers.active
    active_nodes = {material: material.node_tree.nodes.active for material in materials}
    created_nodes = []
    created_image = None
    started = False
    restore_errors = []
    try:
        if image is None:
            image = bpy.data.images.new(image_name, width=resolution, height=resolution, alpha=True)
            created_image = image
        if bake_type == "NORMAL":
            image.colorspace_settings.name = "Non-Color"
        for material in materials:
            node = material.node_tree.nodes.new("ShaderNodeTexImage")
            node.name = f"CBT Bake {image_name}"
            node.image = image
            material.node_tree.nodes.active = node
            created_nodes.append(node)
        for obj, _selected in selection:
            obj.select_set(False)
        for obj in sources:
            obj.select_set(True)
        target.select_set(True)
        bpy.context.view_layer.objects.active = target
        target.data.uv_layers.active = target.data.uv_layers[uv_layer]
        scene.render.engine = "CYCLES"
        options = {
            "type": bake_type, "target": "IMAGE_TEXTURES", "save_mode": "INTERNAL",
            "use_selected_to_active": bool(sources), "margin": margin,
            "cage_extrusion": cage_extrusion, "max_ray_distance": max_ray_distance,
            "uv_layer": uv_layer, "use_clear": True,
        }
        if bake_type == "DIFFUSE":
            options["pass_filter"] = {"COLOR"}
        if bake_type == "NORMAL":
            options.update(normal_space="TANGENT", normal_g=normal_g)
        if cage is not None:
            options.update(use_cage=True, cage_object=cage.name)
        started = True
        result = bpy.ops.object.bake(**options)
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender bake returned {sorted(result)}")
        coverage = _coverage(image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.filepath_raw = str(output_path)
        image.file_format = "PNG"
        image.save()
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("Bake image save returned without a non-empty output file")
        transaction.update(
            changed=True, image=image.name, resolution=list(image.size),
            coverage_ratio=coverage, coverage_method="sampled_alpha_up_to_65536_texels",
            normal_g=normal_g if bake_type == "NORMAL" else None,
            output_bytes=output_path.stat().st_size,
        )
        bump_epoch()
    except Exception as exc:
        if not started:
            for node in created_nodes:
                node.id_data.nodes.remove(node)
            if created_image is not None:
                bpy.data.images.remove(created_image)
        else:
            transaction["changed"] = True
            transaction["partial_change_possible"] = True
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"bake.run failed: {exc}", transaction) from exc
    finally:
        try:
            scene.render.engine = previous_engine
            target.data.uv_layers.active = previous_uv
            for material, node in active_nodes.items():
                material.node_tree.nodes.active = node
            for obj, selected in selection:
                obj.select_set(selected)
            bpy.context.view_layer.objects.active = previous_active
        except Exception as exc:
            restore_errors.append(str(exc))
        transaction["warnings"].extend(f"restore state: {message}" for message in restore_errors)
    transaction["state_token"] = current_state_token(bpy)
    return transaction
