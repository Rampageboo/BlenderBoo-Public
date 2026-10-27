"""Source-to-fitted texture baking and measured rig deformation review."""

from __future__ import annotations

from array import array
from hashlib import blake2b
import math
from pathlib import Path
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .bake import run_bake
from .session_lease import require_document_write_access
from .session_state import check_expected_state, current_state_token


_MAP_TYPES = {"normal": "NORMAL", "ao": "AO", "base_color": "DIFFUSE"}


def _geometry_digest(obj: Any) -> str:
    """Hash original mesh coordinates and connectivity without evaluating modifiers."""
    mesh = obj.data
    digest = blake2b(digest_size=16)
    for collection, attribute, typecode, components in (
        (mesh.vertices, "co", "f", 3),
        (mesh.loops, "vertex_index", "i", 1),
        (mesh.polygons, "loop_total", "i", 1),
    ):
        values = array(typecode, [0]) * (len(collection) * components)
        if values:
            collection.foreach_get(attribute, values)
            digest.update(values.tobytes())
        digest.update(str(len(collection)).encode())
    digest.update(repr(tuple(tuple(float(v) for v in row) for row in obj.matrix_world)).encode())
    return digest.hexdigest()


def _image_lighting_evidence(image: Any) -> dict[str, Any] | None:
    """Directional luminance fit on a small uniform grid; advisory only."""
    width, height = (int(v) for v in image.size)
    if width < 8 or height < 8 or not image.has_data:
        return None
    samples = []
    pixels = image.pixels
    grid = 16
    for row in range(grid):
        y = min(height - 1, int((row + .5) * height / grid))
        for column in range(grid):
            x = min(width - 1, int((column + .5) * width / grid))
            rgba = pixels[(y * width + x) * 4:(y * width + x) * 4 + 4]
            if len(rgba) != 4 or rgba[3] <= .01:
                continue
            luminance = .2126 * rgba[0] + .7152 * rgba[1] + .0722 * rgba[2]
            samples.append(((column + .5) / grid - .5,
                            (row + .5) / grid - .5, float(luminance)))
    if len(samples) < 32:
        return None
    mean = sum(item[2] for item in samples) / len(samples)
    variance = sum((item[2] - mean) ** 2 for item in samples) / len(samples)
    x_var = sum(item[0] ** 2 for item in samples) / len(samples)
    y_var = sum(item[1] ** 2 for item in samples) / len(samples)
    slope_x = sum(item[0] * (item[2] - mean) for item in samples) / len(samples) / max(x_var, 1e-12)
    slope_y = sum(item[1] * (item[2] - mean) for item in samples) / len(samples) / max(y_var, 1e-12)
    explained = (slope_x ** 2 * x_var + slope_y ** 2 * y_var) / max(variance, 1e-12)
    range_luma = max(item[2] for item in samples) - min(item[2] for item in samples)
    return {"image": image.name, "samples": len(samples),
            "directional_r_squared": round(min(1.0, explained), 4),
            "luminance_range": round(range_luma, 4),
            "possible_baked_lighting": bool(explained >= .6 and range_luma >= .25),
            "method": "16x16_luminance_plane_fit_advisory"}


def _source_texture_evidence(sources: list[Any]) -> list[dict[str, Any]]:
    evidence = []
    seen = set()
    for obj in sources:
        for slot in obj.material_slots:
            material = slot.material
            if material is None or not material.use_nodes or material.node_tree is None:
                continue
            tree = material.node_tree
            for node in tree.nodes:
                if node.type != "TEX_IMAGE" or node.image is None or node.image.as_pointer() in seen:
                    continue
                # Only direct Base Color links are certain. Other graphs may
                # contain masks, normals, roughness, or packed textures.
                direct_base_color = any(link.from_node is node and link.to_node.type == "BSDF_PRINCIPLED"
                                        and link.to_socket.name == "Base Color" for link in tree.links)
                if not direct_base_color:
                    continue
                seen.add(node.image.as_pointer())
                item = _image_lighting_evidence(node.image)
                if item is not None:
                    item.update(source=obj.name, material=material.name)
                    evidence.append(item)
    return evidence


def bake_from_source(params: dict[str, Any]) -> ToolResult:
    """Bake normal, AO, and base color from originals to one fitted UV mesh."""
    allowed = {"sources", "target", "uv_layer", "output_dir", "maps", "resolution",
               "image_prefix", "margin", "overwrite", "cage_object", "cage_extrusion",
               "max_ray_distance", "normal_g", "expected_state"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    names = params.get("sources")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(names):
        raise InvalidParamsError("sources must be a non-empty list of distinct mesh object names")
    target_name = params.get("target")
    if not isinstance(target_name, str) or not target_name.strip() or target_name in names:
        raise InvalidParamsError("target must name a fitted mesh distinct from every source")
    uv_layer = params.get("uv_layer")
    if not isinstance(uv_layer, str) or not uv_layer.strip():
        raise InvalidParamsError("uv_layer must be a non-empty string")
    maps = params.get("maps", ["normal", "ao", "base_color"])
    if not isinstance(maps, list) or not maps or any(item not in _MAP_TYPES for item in maps) or len(set(maps)) != len(maps):
        raise InvalidParamsError("maps must be a non-empty unique subset of normal, ao, base_color")
    output_raw = params.get("output_dir")
    if not isinstance(output_raw, str) or not output_raw.strip():
        raise InvalidParamsError("output_dir must be a non-empty directory path")
    output_dir = Path(output_raw).expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise InvalidParamsError(f"output_dir is not a directory: {output_dir}")
    prefix = params.get("image_prefix", target_name)
    if not isinstance(prefix, str) or not prefix.strip() or any(c in prefix for c in "\\/:*?\"<>|"):
        raise InvalidParamsError("image_prefix must be a non-empty file-safe name")
    if "overwrite" in params and not isinstance(params["overwrite"], bool):
        raise InvalidParamsError("overwrite must be a boolean")
    paths = {item: output_dir / f"{prefix}_{item}.png" for item in maps}
    if not params.get("overwrite", False):
        existing = [str(path) for path in paths.values() if path.exists()]
        if existing:
            raise InvalidParamsError("Bake outputs already exist: " + ", ".join(existing))

    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    scene = bpy.context.scene
    sources = []
    for name in names:
        obj = scene.objects.get(name)
        if obj is None or obj.type != "MESH":
            raise NotFoundError(f"Source mesh not found in active scene: {name}")
        sources.append(obj)
    target = scene.objects.get(target_name)
    if target is None or target.type != "MESH":
        raise NotFoundError(f"Fitted target mesh not found in active scene: {target_name}")
    if any(source.data is target.data for source in sources):
        raise InvalidStateError("Fitted target shares a mesh datablock with an original source")
    source_materials = {slot.material for source in sources for slot in source.material_slots
                        if slot.material is not None}
    target_materials = {slot.material for slot in target.material_slots if slot.material is not None}
    shared_materials = sorted(material.name for material in source_materials & target_materials)
    if shared_materials:
        raise InvalidStateError("Fitted target shares source materials that bake.run would edit: " +
                                ", ".join(shared_materials))
    source_image_names = {node.image.name for material in source_materials
                          if material.use_nodes and material.node_tree is not None
                          for node in material.node_tree.nodes
                          if node.type == "TEX_IMAGE" and node.image is not None}
    image_collisions = sorted(f"{prefix}_{item}" for item in maps
                              if f"{prefix}_{item}" in source_image_names)
    if image_collisions:
        raise InvalidStateError("Bake image names are used by source textures: " +
                                ", ".join(image_collisions))
    if target.data.uv_layers.get(uv_layer) is None:
        raise NotFoundError(f"Target UV layer not found: {uv_layer}")
    if any(obj.mode != "OBJECT" for obj in sources + [target]):
        raise InvalidStateError("Baking requires source and target meshes in Object mode")

    before = {obj.name: _geometry_digest(obj) for obj in sources}
    texture_evidence = _source_texture_evidence(sources)
    completed = {}
    try:
        for item in maps:
            arguments = {key: value for key, value in params.items()
                         if key in {"resolution", "margin", "overwrite", "cage_object",
                                    "cage_extrusion", "max_ray_distance", "normal_g"}}
            arguments.update(type=_MAP_TYPES[item], sources=names, target=target_name,
                             uv_layer=uv_layer, image=f"{prefix}_{item}",
                             output_path=str(paths[item]))
            completed[item] = run_bake(arguments)
    except Exception as exc:
        raise MutationApplyError(
            f"Source-to-fitted bake stopped after {len(completed)} completed map(s): {exc}",
            {"completed_maps": completed, "remaining_maps": [item for item in maps if item not in completed],
             "partial_change_possible": True, "source_geometry_digest_before": before,
             "state_token": current_state_token(bpy)},
        ) from exc
    after = {obj.name: _geometry_digest(obj) for obj in sources}
    if before != after:
        raise MutationApplyError("Source geometry changed during baking", {
            "completed_maps": completed, "source_geometry_digest_before": before,
            "source_geometry_digest_after": after, "state_token": current_state_token(bpy)})
    warnings = [f"Possible baked lighting in source texture {item['image']} (directional luminance gradient)"
                for item in texture_evidence if item["possible_baked_lighting"]]
    from mathutils import Vector
    focus = list(target.matrix_world @ (sum((Vector(corner) for corner in target.bound_box), Vector((0, 0, 0))) / 8))
    return ToolResult(data={"target": target_name, "sources": names,
                            "maps": {name: {"path": result["output_path"],
                                            "image": result["image"],
                                            "coverage_ratio": result["coverage_ratio"],
                                            "coverage_method": result["coverage_method"]}
                                     for name, result in completed.items()},
                            "source_geometry_unchanged": True,
                            "source_geometry_digest": after,
                            "source_texture_lighting": texture_evidence,
                            "focus": focus,
                            "changed": True, "state_token": current_state_token(bpy)},
                      warnings=warnings)
