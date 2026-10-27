"""Bounded mesh inspection and allowlisted modifier lifecycle tools."""

from __future__ import annotations

import math
from typing import Any, Callable

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy, vector_values
from .destructive import prepare_destructive_action
from .mesh_topology import append_new_islands, capture_component_summary, fragment_small_ratio
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


MAX_PAGE = 2000
DEFAULT_TOPOLOGY_BUDGET = 250_000
MAX_TOPOLOGY_BUDGET = 5_000_000
MAX_REMESH_VOXELS = 5_000_000
MAX_SCREW_EVALUATED_STEPS = 250_000
MODIFIER_TYPES = frozenset(
    {
        "ARRAY", "BEVEL", "BOOLEAN", "CURVE", "DECIMATE", "DISPLACE",
        "LATTICE", "MIRROR", "REMESH", "SCREW", "SHRINKWRAP", "SIMPLE_DEFORM",
        "SMOOTH", "SOLIDIFY", "SUBSURF", "TRIANGULATE", "WELD", "WEIGHTED_NORMAL",
    }
)


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(names)}")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _optional_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 1024:
        raise InvalidParamsError(f"{label} must be a string no longer than 1024 characters")
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


def _number(
    value: Any,
    label: str,
    minimum: float,
    maximum: float,
    *,
    exclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParamsError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise InvalidParamsError(f"{label} must be a finite number")
    if (result <= minimum if exclusive_minimum else result < minimum) or result > maximum:
        left = ">" if exclusive_minimum else ">="
        raise InvalidParamsError(f"{label} must satisfy {left} {minimum} and <= {maximum}")
    return result


def _vector(value: Any, label: str, length: int, item: Callable[[Any, str], Any]) -> list[Any]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise InvalidParamsError(f"{label} must contain exactly {length} values")
    return [item(entry, label) for entry in value]


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _page_params(params: dict[str, Any], tool: str = "mesh.inspect") -> tuple[int, int]:
    limit = params.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise InvalidParamsError(f"limit must be an integer from 1 to {MAX_PAGE}")
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError(f"cursor must be a decimal string returned by {tool}")


def _scene_object(bpy: Any, name: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    return obj


def _mesh_object(bpy: Any, name: str) -> Any:
    obj = _scene_object(bpy, name)
    if obj.type != "MESH" or obj.data is None:
        raise InvalidStateError(f"Object is not a mesh: {name}")
    return obj


def _bounds(obj: Any) -> dict[str, Any]:
    from mathutils import Vector

    corners = [tuple(float(value) for value in corner) for corner in obj.bound_box]
    local_min = [min(corner[index] for corner in corners) for index in range(3)]
    local_max = [max(corner[index] for corner in corners) for index in range(3)]
    world_corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    world_min = [min(float(corner[index]) for corner in world_corners) for index in range(3)]
    world_max = [max(float(corner[index]) for corner in world_corners) for index in range(3)]
    return {
        "local": {"min": local_min, "max": local_max},
        "world": {"min": world_min, "max": world_max},
        "dimensions": vector_values(obj.dimensions),
    }


def _attribute_summary(attribute: Any) -> dict[str, Any]:
    return {
        "name": attribute.name,
        "domain": attribute.domain,
        "data_type": attribute.data_type,
        "length": len(attribute.data),
        "is_internal": bool(getattr(attribute, "is_internal", False)),
        "is_required": bool(getattr(attribute, "is_required", False)),
    }


def _topology_summary(mesh: Any, budget: int) -> dict[str, Any]:
    work = len(mesh.edges) + len(mesh.loops)
    if work > budget:
        raise InvalidStateError(
            f"Topology analysis requires {work} edge/loop elements, above max_elements {budget}"
        )
    edge_faces = {tuple(sorted(edge.vertices)): 0 for edge in mesh.edges}
    edge_vertices = {int(index) for edge in mesh.edges for index in edge.vertices}
    degenerate_faces = 0
    for polygon in mesh.polygons:
        if float(polygon.area) <= 1e-12:
            degenerate_faces += 1
        for edge_key in polygon.edge_keys:
            normalized = tuple(sorted(int(index) for index in edge_key))
            edge_faces[normalized] = edge_faces.get(normalized, 0) + 1
    loose_edges = sum(1 for count in edge_faces.values() if count == 0)
    boundary_edges = sum(1 for count in edge_faces.values() if count == 1)
    multi_face_edges = sum(1 for count in edge_faces.values() if count > 2)
    return {
        "analyzed_elements": work,
        "max_elements": budget,
        "loose_vertices": len(mesh.vertices) - len(edge_vertices),
        "loose_edges": loose_edges,
        "boundary_edges": boundary_edges,
        "non_manifold_edges": loose_edges + boundary_edges + multi_face_edges,
        "edges_with_more_than_two_faces": multi_face_edges,
        "degenerate_faces": degenerate_faces,
        "closed_manifold_candidate": bool(mesh.polygons) and loose_edges == 0 and boundary_edges == 0 and multi_face_edges == 0,
    }


def inspect_mesh(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"object_name", "include_topology", "max_elements", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    include_topology = _strict_bool(params.get("include_topology"), "include_topology", False)
    max_elements = _integer(
        params.get("max_elements", DEFAULT_TOPOLOGY_BUDGET),
        "max_elements",
        1,
        MAX_TOPOLOGY_BUDGET,
    )
    limit, offset = _page_params(params)
    bpy = get_bpy()
    obj = _mesh_object(bpy, object_name)
    mesh = obj.data
    attributes = sorted(mesh.attributes, key=lambda item: item.name)
    page = attributes[offset : offset + limit]
    next_offset = offset + len(page)
    materials = [
        {"slot": index, "material": material.name if material else None}
        for index, material in enumerate(mesh.materials)
    ]
    data: dict[str, Any] = {
        "object": obj.name,
        "mesh": mesh.name,
        "data_users": int(mesh.users),
        "counts": {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
            "loops": len(mesh.loops),
            "triangles_cached": len(mesh.loop_triangles),
            "materials": len(mesh.materials),
            "uv_layers": len(mesh.uv_layers),
            "color_attributes": len(getattr(mesh, "color_attributes", ())),
            "shape_keys": len(mesh.shape_keys.key_blocks) if mesh.shape_keys else 0,
        },
        "bounds": _bounds(obj),
        "materials": materials,
        "uv_layers": [layer.name for layer in mesh.uv_layers],
        "attributes": [_attribute_summary(attribute) for attribute in page],
        "attribute_count": len(attributes),
        "next_cursor": str(next_offset) if next_offset < len(attributes) else None,
        "truncated": next_offset < len(attributes),
        "topology": _topology_summary(mesh, max_elements) if include_topology else None,
        "state_token": current_state_token(bpy),
    }
    return ToolResult(data=data)


def _enum(value: Any, label: str, allowed: set[str]) -> str:
    result = _name(value, label).upper()
    if result not in allowed:
        raise InvalidParamsError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return result


def _bool_item(value: Any, label: str) -> bool:
    return _strict_bool(value, label)


def _float_item(value: Any, label: str) -> float:
    return _number(value, label, -1_000_000.0, 1_000_000.0)


def _object_reference(
    bpy: Any,
    value: Any,
    label: str,
    *,
    owner: Any | None = None,
    allowed_types: set[str] | None = None,
) -> Any | None:
    if value is None:
        return None
    obj = _scene_object(bpy, _name(value, label))
    if obj is owner:
        raise InvalidParamsError(f"{label} cannot reference the object that owns the modifier")
    if allowed_types is not None and obj.type not in allowed_types:
        raise InvalidParamsError(
            f"{label} must reference an object of type: {', '.join(sorted(allowed_types))}"
        )
    return obj


def _collection_reference(bpy: Any, value: Any, label: str, *, owner: Any | None = None) -> Any | None:
    if value is None:
        return None
    requested = _name(value, label)
    pending = [bpy.context.scene.collection]
    seen = set()
    collection = None
    while pending:
        candidate = pending.pop()
        pointer = candidate.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        if candidate.name == requested:
            collection = candidate
            break
        pending.extend(candidate.children)
    if collection is None:
        raise NotFoundError(f"Collection not found in current scene: {requested}")
    if owner is not None and any(candidate is owner for candidate in collection.all_objects):
        raise InvalidParamsError(f"{label} cannot contain the object that owns the modifier")
    return collection


def _texture_reference(bpy: Any, value: Any, label: str) -> Any | None:
    if value is None:
        return None
    requested = _name(value, label)
    texture = bpy.data.textures.get(requested)
    if texture is None:
        raise NotFoundError(f"Texture not found in current file: {requested}")
    return texture


def _vertex_group_reference(owner: Any, value: Any, label: str) -> str:
    requested = _optional_name(value, label)
    if requested and owner.vertex_groups.get(requested) is None:
        raise NotFoundError(f"Vertex group not found on {owner.name}: {requested}")
    return requested


def _uv_layer_reference(owner: Any, value: Any, label: str) -> str:
    requested = _optional_name(value, label)
    uv_layers = getattr(getattr(owner, "data", None), "uv_layers", None)
    if requested and (uv_layers is None or uv_layers.get(requested) is None):
        raise NotFoundError(f"UV layer not found on {owner.name}: {requested}")
    return requested


def _validate_modifier_workload(
    modifier_type: str,
    values: dict[str, Any],
    modifier: Any | None,
    owner: Any,
) -> None:
    if modifier_type == "REMESH":
        mode = values.get("mode", getattr(modifier, "mode", "SHARP"))
        if mode == "VOXEL":
            voxel_size = values.get("voxel_size", getattr(modifier, "voxel_size", 0.1))
            cells = math.prod(
                max(1, math.ceil(abs(float(dimension)) / voxel_size))
                for dimension in owner.dimensions
            )
            if cells > MAX_REMESH_VOXELS:
                raise InvalidParamsError(
                    f"REMESH estimated voxel cells {cells} exceed {MAX_REMESH_VOXELS}; increase voxel_size"
                )
        return
    if modifier_type == "SCREW":
        steps = values.get("steps", getattr(modifier, "steps", 16))
        render_steps = values.get("render_steps", getattr(modifier, "render_steps", 16))
        iterations = values.get("iterations", getattr(modifier, "iterations", 1))
        evaluated_steps = max(steps, render_steps) * iterations
        if evaluated_steps > MAX_SCREW_EVALUATED_STEPS:
            raise InvalidParamsError(
                f"SCREW max(steps, render_steps) * iterations must not exceed {MAX_SCREW_EVALUATED_STEPS}"
            )
        source_vertices = len(getattr(getattr(owner, "data", None), "vertices", ()))
        estimated_vertices = source_vertices * evaluated_steps
        if estimated_vertices > MAX_TOPOLOGY_BUDGET:
            raise InvalidParamsError(
                f"SCREW estimated output vertices {estimated_vertices} exceed {MAX_TOPOLOGY_BUDGET}"
            )


def _validated_settings(
    bpy: Any,
    modifier_type: str,
    settings: Any,
    *,
    owner: Any,
    modifier: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(settings, dict) or not settings:
        raise InvalidParamsError("settings must be a non-empty object")
    validators: dict[str, dict[str, Callable[[Any], Any]]] = {
        "BEVEL": {
            "width": lambda value: _number(value, "width", 0.0, 1_000_000.0),
            "segments": lambda value: _integer(value, "segments", 1, 1000),
            "limit_method": lambda value: _enum(value, "limit_method", {"NONE", "ANGLE", "WEIGHT", "VGROUP"}),
            "angle_limit": lambda value: _number(value, "angle_limit", 0.0, math.pi),
            "affect": lambda value: _enum(value, "affect", {"EDGES", "VERTICES"}),
        },
        "SUBSURF": {
            "levels": lambda value: _integer(value, "levels", 0, 12),
            "render_levels": lambda value: _integer(value, "render_levels", 0, 12),
            "subdivision_type": lambda value: _enum(value, "subdivision_type", {"CATMULL_CLARK", "SIMPLE"}),
            "show_only_control_edges": lambda value: _strict_bool(value, "show_only_control_edges"),
        },
        "MIRROR": {
            "use_axis": lambda value: _vector(value, "use_axis", 3, _bool_item),
            "use_bisect_axis": lambda value: _vector(value, "use_bisect_axis", 3, _bool_item),
            "use_clip": lambda value: _strict_bool(value, "use_clip"),
            "merge_threshold": lambda value: _number(value, "merge_threshold", 0.0, 1_000_000.0),
            "mirror_object": lambda value: _object_reference(bpy, value, "mirror_object", owner=owner),
        },
        "SOLIDIFY": {
            "thickness": lambda value: _number(value, "thickness", -1_000_000.0, 1_000_000.0),
            "offset": lambda value: _number(value, "offset", -1.0, 1.0),
            "use_rim": lambda value: _strict_bool(value, "use_rim"),
        },
        "ARRAY": {
            "count": lambda value: _integer(value, "count", 1, 100_000),
            "use_relative_offset": lambda value: _strict_bool(value, "use_relative_offset"),
            "relative_offset_displace": lambda value: _vector(value, "relative_offset_displace", 3, _float_item),
            "use_constant_offset": lambda value: _strict_bool(value, "use_constant_offset"),
            "constant_offset_displace": lambda value: _vector(value, "constant_offset_displace", 3, _float_item),
        },
        "DECIMATE": {
            "decimate_type": lambda value: _enum(value, "decimate_type", {"COLLAPSE", "UNSUBDIV", "DISSOLVE"}),
            "ratio": lambda value: _number(value, "ratio", 0.0, 1.0),
            "angle_limit": lambda value: _number(value, "angle_limit", 0.0, math.pi),
            "use_collapse_triangulate": lambda value: _strict_bool(value, "use_collapse_triangulate"),
        },
        "TRIANGULATE": {
            "quad_method": lambda value: _enum(value, "quad_method", {"BEAUTY", "FIXED", "FIXED_ALTERNATE", "SHORTEST_DIAGONAL", "LONGEST_DIAGONAL"}),
            "ngon_method": lambda value: _enum(value, "ngon_method", {"BEAUTY", "CLIP"}),
            "min_vertices": lambda value: _integer(value, "min_vertices", 4, 1000),
        },
        "WELD": {
            "merge_threshold": lambda value: _number(value, "merge_threshold", 0.0, 1_000_000.0),
            "mode": lambda value: _enum(value, "mode", {"ALL", "CONNECTED"}),
        },
        "SMOOTH": {
            "factor": lambda value: _number(value, "factor", -10.0, 10.0),
            "iterations": lambda value: _integer(value, "iterations", 1, 1000),
            "use_x": lambda value: _strict_bool(value, "use_x"),
            "use_y": lambda value: _strict_bool(value, "use_y"),
            "use_z": lambda value: _strict_bool(value, "use_z"),
        },
        "WEIGHTED_NORMAL": {
            "keep_sharp": lambda value: _strict_bool(value, "keep_sharp"),
            "weight": lambda value: _integer(value, "weight", 1, 100),
            "mode": lambda value: _enum(value, "mode", {"FACE_AREA", "CORNER_ANGLE", "FACE_ANGLE"}),
            "thresh": lambda value: _number(value, "thresh", 0.0, 10.0),
        },
        "BOOLEAN": {
            "operation": lambda value: _enum(value, "operation", {"DIFFERENCE", "INTERSECT", "UNION"}),
            "solver": lambda value: _enum(value, "solver", {"EXACT", "FLOAT", "MANIFOLD"}),
            "operand_type": lambda value: _enum(value, "operand_type", {"COLLECTION", "OBJECT"}),
            "object": lambda value: _object_reference(
                bpy, value, "object", owner=owner, allowed_types={"MESH"}
            ),
            "collection": lambda value: _collection_reference(bpy, value, "collection", owner=owner),
            "use_self": lambda value: _strict_bool(value, "use_self"),
            "use_hole_tolerant": lambda value: _strict_bool(value, "use_hole_tolerant"),
            "material_mode": lambda value: _enum(value, "material_mode", {"INDEX", "TRANSFER"}),
        },
        "SHRINKWRAP": {
            "wrap_method": lambda value: _enum(
                value, "wrap_method", {"NEAREST_SURFACEPOINT", "NEAREST_VERTEX", "PROJECT", "TARGET_PROJECT"}
            ),
            "wrap_mode": lambda value: _enum(
                value, "wrap_mode", {"ABOVE_SURFACE", "INSIDE", "ON_SURFACE", "OUTSIDE", "OUTSIDE_SURFACE"}
            ),
            "target": lambda value: _object_reference(
                bpy, value, "target", owner=owner, allowed_types={"MESH"}
            ),
            "auxiliary_target": lambda value: _object_reference(
                bpy, value, "auxiliary_target", owner=owner, allowed_types={"MESH"}
            ),
            "offset": lambda value: _number(value, "offset", -1_000_000.0, 1_000_000.0),
            "project_limit": lambda value: _number(value, "project_limit", 0.0, 1_000_000.0),
            "use_project_x": lambda value: _strict_bool(value, "use_project_x"),
            "use_project_y": lambda value: _strict_bool(value, "use_project_y"),
            "use_project_z": lambda value: _strict_bool(value, "use_project_z"),
            "use_negative_direction": lambda value: _strict_bool(value, "use_negative_direction"),
            "use_positive_direction": lambda value: _strict_bool(value, "use_positive_direction"),
            "cull_face": lambda value: _enum(value, "cull_face", {"BACK", "FRONT", "OFF"}),
            "subsurf_levels": lambda value: _integer(value, "subsurf_levels", 0, 6),
            "vertex_group": lambda value: _vertex_group_reference(owner, value, "vertex_group"),
            "invert_vertex_group": lambda value: _strict_bool(value, "invert_vertex_group"),
        },
        "SIMPLE_DEFORM": {
            "deform_method": lambda value: _enum(value, "deform_method", {"BEND", "STRETCH", "TAPER", "TWIST"}),
            "deform_axis": lambda value: _enum(value, "deform_axis", {"X", "Y", "Z"}),
            "factor": lambda value: _number(value, "factor", -1_000_000.0, 1_000_000.0),
            "angle": lambda value: _number(value, "angle", -1_000_000.0, 1_000_000.0),
            "limits": lambda value: _vector(
                value, "limits", 2, lambda item, label: _number(item, label, 0.0, 1.0)
            ),
            "origin": lambda value: _object_reference(bpy, value, "origin", owner=owner),
            "lock_x": lambda value: _strict_bool(value, "lock_x"),
            "lock_y": lambda value: _strict_bool(value, "lock_y"),
            "lock_z": lambda value: _strict_bool(value, "lock_z"),
            "vertex_group": lambda value: _vertex_group_reference(owner, value, "vertex_group"),
            "invert_vertex_group": lambda value: _strict_bool(value, "invert_vertex_group"),
        },
        "CURVE": {
            "object": lambda value: _object_reference(
                bpy, value, "object", owner=owner, allowed_types={"CURVE"}
            ),
            "deform_axis": lambda value: _enum(
                value, "deform_axis", {"NEG_X", "NEG_Y", "NEG_Z", "POS_X", "POS_Y", "POS_Z"}
            ),
        },
        "LATTICE": {
            "object": lambda value: _object_reference(
                bpy, value, "object", owner=owner, allowed_types={"LATTICE"}
            ),
            "strength": lambda value: _number(value, "strength", -1_000_000.0, 1_000_000.0),
            "vertex_group": lambda value: _vertex_group_reference(owner, value, "vertex_group"),
            "invert_vertex_group": lambda value: _strict_bool(value, "invert_vertex_group"),
        },
        "REMESH": {
            "mode": lambda value: _enum(value, "mode", {"BLOCKS", "SHARP", "SMOOTH", "VOXEL"}),
            "octree_depth": lambda value: _integer(value, "octree_depth", 1, 8),
            "scale": lambda value: _number(value, "scale", 0.0, 0.99),
            "sharpness": lambda value: _number(value, "sharpness", -1_000_000.0, 1_000_000.0),
            "use_remove_disconnected": lambda value: _strict_bool(value, "use_remove_disconnected"),
            "threshold": lambda value: _number(value, "threshold", 0.0, 1.0),
            "voxel_size": lambda value: _number(
                value, "voxel_size", 0.000001, 1_000_000.0
            ),
        },
        "SCREW": {
            "object": lambda value: _object_reference(bpy, value, "object", owner=owner),
            "axis": lambda value: _enum(value, "axis", {"X", "Y", "Z"}),
            "angle": lambda value: _number(value, "angle", -1_000_000.0, 1_000_000.0),
            "screw_offset": lambda value: _number(value, "screw_offset", -1_000_000.0, 1_000_000.0),
            "iterations": lambda value: _integer(value, "iterations", 1, 10_000),
            "steps": lambda value: _integer(value, "steps", 1, 10_000),
            "render_steps": lambda value: _integer(value, "render_steps", 1, 10_000),
            "use_merge_vertices": lambda value: _strict_bool(value, "use_merge_vertices"),
            "merge_threshold": lambda value: _number(value, "merge_threshold", 0.0, 1_000_000.0),
            "use_smooth_shade": lambda value: _strict_bool(value, "use_smooth_shade"),
            "use_normal_calculate": lambda value: _strict_bool(value, "use_normal_calculate"),
            "use_normal_flip": lambda value: _strict_bool(value, "use_normal_flip"),
            "use_stretch_u": lambda value: _strict_bool(value, "use_stretch_u"),
            "use_stretch_v": lambda value: _strict_bool(value, "use_stretch_v"),
        },
        "DISPLACE": {
            "texture": lambda value: _texture_reference(bpy, value, "texture"),
            "texture_coords": lambda value: _enum(value, "texture_coords", {"GLOBAL", "LOCAL", "OBJECT", "UV"}),
            "texture_coords_object": lambda value: _object_reference(
                bpy, value, "texture_coords_object", owner=owner
            ),
            "direction": lambda value: _enum(
                value, "direction", {"CUSTOM_NORMAL", "NORMAL", "RGB_TO_XYZ", "X", "Y", "Z"}
            ),
            "strength": lambda value: _number(value, "strength", -1_000_000.0, 1_000_000.0),
            "mid_level": lambda value: _number(value, "mid_level", -1_000_000.0, 1_000_000.0),
            "space": lambda value: _enum(value, "space", {"GLOBAL", "LOCAL"}),
            "uv_layer": lambda value: _uv_layer_reference(owner, value, "uv_layer"),
            "vertex_group": lambda value: _vertex_group_reference(owner, value, "vertex_group"),
            "invert_vertex_group": lambda value: _strict_bool(value, "invert_vertex_group"),
        },
    }
    if modifier is None:
        try:
            probe = owner.modifiers.new(name="Codex modifier validation", type=modifier_type)
        except Exception as exc:
            raise InvalidParamsError(f"Modifier type {modifier_type} is unavailable for {owner.type}: {exc}") from exc
        try:
            values = _validated_rna_settings(bpy, probe, settings)
        finally:
            owner.modifiers.remove(probe)
    else:
        values = _validated_rna_settings(bpy, modifier, settings)
    if modifier_type == "SIMPLE_DEFORM" and "limits" in values and values["limits"][0] > values["limits"][1]:
        raise InvalidParamsError("limits must be ordered from lower to upper")
    _validate_modifier_workload(modifier_type, values, modifier, owner)
    return values


def _validated_rna_settings(bpy: Any, modifier: Any, settings: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, value in settings.items():
        prop = modifier.bl_rna.properties.get(name)
        if prop is None or prop.is_readonly or name in {"rna_type", "name", "type"}:
            raise InvalidParamsError(f"Modifier setting is not writable RNA: {name}")
        kind = str(prop.type)
        length = int(getattr(prop, "array_length", 0))
        items = value if length else [value]
        if length and (not isinstance(value, (list, tuple)) or len(value) != length):
            raise InvalidParamsError(f"{name} requires an array of length {length}")
        if kind == "BOOLEAN":
            if any(not isinstance(item, bool) for item in items):
                raise InvalidParamsError(f"{name} requires boolean values")
        elif kind == "INT":
            if any(isinstance(item, bool) or not isinstance(item, int) or not prop.hard_min <= item <= prop.hard_max for item in items):
                raise InvalidParamsError(f"{name} requires integers within Blender RNA range")
        elif kind == "FLOAT":
            if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or not prop.hard_min <= item <= prop.hard_max for item in items):
                raise InvalidParamsError(f"{name} requires numbers within Blender RNA range")
        elif kind == "ENUM":
            identifiers = {item.identifier for item in prop.enum_items}
            if length or not isinstance(value, str) or value not in identifiers:
                raise InvalidParamsError(f"{name} must be one of: {', '.join(sorted(identifiers))}")
        elif kind == "STRING":
            if length or not isinstance(value, str):
                raise InvalidParamsError(f"{name} requires a string")
        elif kind == "POINTER":
            if value is None:
                values[name] = None
                continue
            if not isinstance(value, str):
                raise InvalidParamsError(f"{name} requires a datablock name")
            pointer_type = prop.fixed_type.identifier
            collections = {
                "Object": bpy.data.objects, "Collection": bpy.data.collections,
                "Texture": bpy.data.textures, "NodeTree": bpy.data.node_groups,
                "GeometryNodeTree": bpy.data.node_groups, "ShaderNodeTree": bpy.data.node_groups,
                "Material": bpy.data.materials, "Image": bpy.data.images,
            }
            collection = collections.get(pointer_type)
            if collection is None:
                raise InvalidParamsError(f"{name} references unsupported datablock type {pointer_type}")
            resolved = collection.get(value)
            if resolved is None:
                raise NotFoundError(f"{pointer_type} datablock not found: {value}")
            if pointer_type == "Object" and resolved is getattr(modifier, "id_data", None):
                raise InvalidParamsError(f"{name} cannot reference the modifier's own object")
            values[name] = resolved
            continue
        else:
            raise InvalidParamsError(f"{name} has unsupported RNA type {kind}")
        values[name] = value
    return values


def _setting_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "name") and hasattr(value, "bl_rna"):
        return value.name
    try:
        return [_setting_json(item) for item in value]
    except TypeError:
        return str(value)


def _setting_snapshot(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "bl_rna"):
        return value
    copy_method = getattr(value, "copy", None)
    if callable(copy_method):
        try:
            return copy_method()
        except (AttributeError, TypeError):
            pass
    try:
        return list(value)
    except TypeError:
        return value


def _modifier_detail(modifier: Any) -> dict[str, Any]:
    allowed = tuple(prop.identifier for prop in modifier.bl_rna.properties if not prop.is_readonly and prop.type != "COLLECTION" and prop.identifier not in {"rna_type", "name", "type"})
    settings = {}
    unavailable = []
    for name in allowed:
        if not hasattr(modifier, name):
            unavailable.append(name)
            continue
        settings[name] = _setting_json(getattr(modifier, name))
    return {
        "name": modifier.name,
        "type": modifier.type,
        "show_viewport": bool(modifier.show_viewport),
        "show_render": bool(modifier.show_render),
        "show_in_editmode": bool(modifier.show_in_editmode),
        "settings": settings,
        "unavailable_settings": unavailable,
        "writable_settings": allowed,
    }


def _validated_setting_names(modifier_type: str) -> tuple[str, ...]:
    names = {
        "BEVEL": ("width", "segments", "limit_method", "angle_limit", "affect"),
        "SUBSURF": ("levels", "render_levels", "subdivision_type", "show_only_control_edges"),
        "MIRROR": ("use_axis", "use_bisect_axis", "use_clip", "merge_threshold", "mirror_object"),
        "SOLIDIFY": ("thickness", "offset", "use_rim"),
        "ARRAY": ("count", "use_relative_offset", "relative_offset_displace", "use_constant_offset", "constant_offset_displace"),
        "DECIMATE": ("decimate_type", "ratio", "angle_limit", "use_collapse_triangulate"),
        "TRIANGULATE": ("quad_method", "ngon_method", "min_vertices"),
        "WELD": ("merge_threshold", "mode"),
        "SMOOTH": ("factor", "iterations", "use_x", "use_y", "use_z"),
        "WEIGHTED_NORMAL": ("keep_sharp", "weight", "mode", "thresh"),
        "BOOLEAN": ("operation", "solver", "operand_type", "object", "collection", "use_self", "use_hole_tolerant", "material_mode"),
        "SHRINKWRAP": ("wrap_method", "wrap_mode", "target", "auxiliary_target", "offset", "project_limit", "use_project_x", "use_project_y", "use_project_z", "use_negative_direction", "use_positive_direction", "cull_face", "subsurf_levels", "vertex_group", "invert_vertex_group"),
        "SIMPLE_DEFORM": ("deform_method", "deform_axis", "factor", "angle", "limits", "origin", "lock_x", "lock_y", "lock_z", "vertex_group", "invert_vertex_group"),
        "CURVE": ("object", "deform_axis"),
        "LATTICE": ("object", "strength", "vertex_group", "invert_vertex_group"),
        "REMESH": ("mode", "octree_depth", "scale", "sharpness", "use_remove_disconnected", "threshold", "voxel_size"),
        "SCREW": ("object", "axis", "angle", "screw_offset", "iterations", "steps", "render_steps", "use_merge_vertices", "merge_threshold", "use_smooth_shade", "use_normal_calculate", "use_normal_flip", "use_stretch_u", "use_stretch_v"),
        "DISPLACE": ("texture", "texture_coords", "texture_coords_object", "direction", "strength", "mid_level", "space", "uv_layer", "vertex_group", "invert_vertex_group"),
    }
    return names.get(modifier_type, ())


def _modifier_object(bpy: Any, object_name: str) -> Any:
    obj = _scene_object(bpy, object_name)
    if not hasattr(obj, "modifiers"):
        raise InvalidStateError(f"Object does not support modifiers: {object_name}")
    return obj


def _modifier(obj: Any, name: str) -> Any:
    modifier = obj.modifiers.get(name)
    if modifier is None:
        raise NotFoundError(f"Modifier not found on {obj.name}: {name}")
    return modifier


def inspect_modifier(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"object_name", "modifier_name", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    modifier_name = params.get("modifier_name")
    if modifier_name is not None:
        modifier_name = _name(modifier_name, "modifier_name")
    limit, offset = _page_params(params, "modifier.inspect")
    bpy = get_bpy()
    obj = _modifier_object(bpy, object_name)
    stack = list(obj.modifiers)
    page = stack[offset : offset + limit]
    next_offset = offset + len(page)
    data: dict[str, Any] = {
        "object": obj.name,
        "count": len(stack),
        "modifiers": [
            {"index": index, **_modifier_detail(modifier)}
            for index, modifier in enumerate(page, start=offset)
        ],
        "next_cursor": str(next_offset) if next_offset < len(stack) else None,
        "truncated": next_offset < len(stack),
        "state_token": current_state_token(bpy),
    }
    if modifier_name is not None:
        modifier = _modifier(obj, modifier_name)
        data["modifier_detail"] = {
            "index": stack.index(modifier),
            **_modifier_detail(modifier),
        }
    return ToolResult(data=data)


def _prepare_mutation(params: dict[str, Any]) -> Any:
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    return bpy


def add_modifier(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "type", "name", "settings", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    modifier_type = _name(params.get("type"), "type").upper()
    requested_name = params.get("name")
    if requested_name is not None:
        requested_name = _name(requested_name, "name")
    bpy = _prepare_mutation(params)
    obj = _modifier_object(bpy, object_name)
    settings = (
        _validated_settings(bpy, modifier_type, params["settings"], owner=obj)
        if "settings" in params
        else {}
    )
    if "settings" not in params:
        _validate_modifier_workload(modifier_type, settings, None, obj)
    modifier = None
    try:
        modifier = obj.modifiers.new(name=requested_name or modifier_type.title(), type=modifier_type)
        for name, value in settings.items():
            if not hasattr(modifier, name):
                raise InvalidParamsError(f"{name} is unavailable for {modifier_type} in this Blender version")
            setattr(modifier, name, value)
        detail = _modifier_detail(modifier)
    except Exception as exc:
        cleanup_errors = []
        if modifier is not None and obj.modifiers.get(modifier.name) is modifier:
            try:
                obj.modifiers.remove(modifier)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove modifier {modifier.name}: {cleanup_exc}")
        transaction = new_transaction([{"type": "Object", "name": obj.name}])
        transaction.update(
            object=obj.name,
            requested_name=requested_name,
            changed=bool(cleanup_errors),
            warnings=cleanup_errors,
            journal=[{"op": "modifier.add", "target": requested_name or modifier_type, "status": "failed"}],
        )
        if cleanup_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"modifier.add failed: {exc}", transaction) from exc
    transaction = new_transaction([{"type": "Object", "name": obj.name}])
    transaction.update(
        object=obj.name,
        modifier=modifier.name,
        requested_name=requested_name,
        resolved_name=modifier.name,
        detail=detail,
        changed=True,
        journal=[{"op": "modifier.add", "target": modifier.name, "before": None, "after": detail, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def patch_modifier(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "modifier_name", "settings", "show_viewport", "show_render", "show_in_editmode", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    modifier_name = _name(params.get("modifier_name"), "modifier_name")
    if "settings" not in params and not any(name in params for name in ("show_viewport", "show_render", "show_in_editmode")):
        raise InvalidParamsError("Supply settings or at least one modifier visibility field")
    bpy = _prepare_mutation(params)
    obj = _modifier_object(bpy, object_name)
    modifier = _modifier(obj, modifier_name)
    settings = (
        _validated_settings(
            bpy, modifier.type, params["settings"], owner=obj, modifier=modifier
        )
        if "settings" in params
        else {}
    )
    values = dict(settings)
    for name in ("show_viewport", "show_render", "show_in_editmode"):
        if name in params:
            values[name] = _strict_bool(params[name], name)
    before_values = {
        name: _setting_snapshot(getattr(modifier, name))
        for name in values
        if hasattr(modifier, name)
    }
    before = {name: _setting_json(value) for name, value in before_values.items()}
    missing = sorted(set(values) - set(before_values))
    if missing:
        raise InvalidParamsError(f"Settings unavailable in this Blender version: {', '.join(missing)}")
    journal = [
        {"op": "modifier.patch", "target": modifier.name, "field": name, "before": before[name], "after": _setting_json(value), "status": "planned"}
        for name, value in values.items()
    ]
    transaction = new_transaction([{"type": "Object", "name": obj.name}])
    applied = []
    try:
        for name, value in values.items():
            if before[name] == _setting_json(value):
                next(item for item in journal if item["field"] == name)["status"] = "present"
                continue
            applied.append(name)
            setattr(modifier, name, value)
            next(item for item in journal if item["field"] == name)["status"] = "applied"
        after = {name: _setting_json(getattr(modifier, name)) for name in values}
    except Exception as exc:
        rollback_errors = []
        for name in reversed(applied):
            try:
                setattr(modifier, name, before_values[name])
                next(item for item in journal if item["field"] == name)["status"] = "rolled_back"
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {name}: {rollback_exc}")
        transaction.update(object=obj.name, modifier=modifier.name, changed=bool(rollback_errors), journal=journal, warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"modifier.patch failed: {exc}", transaction) from exc
    changed = before != after
    transaction.update(object=obj.name, modifier=modifier.name, before=before, after=after, changed=changed, journal=journal)
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def reorder_modifier(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "modifier_name", "index", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    modifier_name = _name(params.get("modifier_name"), "modifier_name")
    index = params.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise InvalidParamsError("index must be a non-negative integer")
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _modifier_object(bpy, object_name)
    modifier = _modifier(obj, modifier_name)
    count = len(obj.modifiers)
    if index >= count:
        raise InvalidParamsError(f"index must be less than modifier count {count}")
    before_index = list(obj.modifiers).index(modifier)
    changed = before_index != index
    journal = [{"op": "modifier.reorder", "target": modifier.name, "before": before_index, "after": index, "status": "planned" if changed else "present"}]
    transaction, apply = prepare_destructive_action(
        action="modifier.reorder",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=journal,
        dry_run=dry_run,
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "modifier": modifier.name, "index": index},
    )
    if not apply or not changed:
        return transaction
    require_document_write_access(bpy)
    try:
        obj.modifiers.move(before_index, index)
        journal[0]["status"] = "applied"
    except Exception as exc:
        current = list(obj.modifiers).index(modifier)
        rollback_error = None
        if current != before_index:
            try:
                obj.modifiers.move(current, before_index)
            except Exception as rollback_exc:
                rollback_error = rollback_exc
        transaction["changed"] = rollback_error is not None
        if rollback_error is not None:
            transaction["warnings"].append(f"reorder rollback failed: {rollback_error}")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"modifier.reorder failed: {exc}", transaction) from exc
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_modifier(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "modifier_name", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    modifier_name = _name(params.get("modifier_name"), "modifier_name")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _modifier_object(bpy, object_name)
    modifier = _modifier(obj, modifier_name)
    detail = _modifier_detail(modifier)
    journal = [{"op": "modifier.remove", "target": modifier.name, "before": detail, "after": None, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="modifier.remove",
        affected_datablocks=[{"type": "Object", "name": obj.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "modifier": modifier.name},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    try:
        obj.modifiers.remove(modifier)
        journal[0]["status"] = "applied"
    except Exception as exc:
        partial = obj.modifiers.get(modifier_name) is None
        transaction["changed"] = partial
        if partial:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"modifier.remove failed: {exc}", transaction) from exc
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _mesh_counts(obj: Any) -> dict[str, int]:
    mesh = obj.data
    return {"vertices": len(mesh.vertices), "edges": len(mesh.edges), "polygons": len(mesh.polygons), "loops": len(mesh.loops)}


def apply_modifier(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "modifier_name", "dry_run", "confirm", "expected_state",
                      "fragment_small_ratio"})
    object_name = _name(params.get("object_name"), "object_name")
    modifier_name = _name(params.get("modifier_name"), "modifier_name")
    _validate_expected(params)
    small_ratio = fragment_small_ratio(params.get("fragment_small_ratio", 0.01))
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name)
    modifier = _modifier(obj, modifier_name)
    detail = _modifier_detail(modifier)
    before_counts = _mesh_counts(obj)
    journal = [{"op": "modifier.apply", "target": modifier.name, "before": {"modifier": detail, "mesh": before_counts}, "after": None, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="modifier.apply",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": obj.data.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={
            "object": obj.name,
            "modifier": modifier.name,
            "mesh_data_users": int(obj.data.users),
            "recovery": "Save a checkpoint before apply; applied geometry cannot be reconstructed from the journal.",
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    if obj.data.users > 1:
        raise InvalidStateError(
            "modifier.apply refuses shared mesh data; duplicate the mesh data first so other objects cannot be changed"
        )
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("modifier.apply requires Blender Object mode")
    before_components = capture_component_summary(transaction, obj.data, obj, small_ratio)
    selected_before = list(bpy.context.selected_objects)
    active_before = bpy.context.view_layer.objects.active
    try:
        for selected in selected_before:
            selected.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        with bpy.context.temp_override(
            object=obj,
            active_object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
        ):
            result = bpy.ops.object.modifier_apply(modifier=modifier.name)
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender returned {sorted(result)}")
        after_counts = _mesh_counts(obj)
        journal[0]["after"] = {"modifier": None, "mesh": after_counts}
        journal[0]["status"] = "applied"
        transaction.update(changed=True, before_mesh=before_counts, after_mesh=after_counts)
        append_new_islands(transaction, before_components, obj.data, obj, small_ratio)
    except Exception as exc:
        partial = obj.modifiers.get(modifier_name) is None or _mesh_counts(obj) != before_counts
        transaction["changed"] = partial
        if partial:
            journal[0]["status"] = "partial"
            transaction["warnings"].append("Modifier apply may have partially changed mesh data; restore the preflight checkpoint")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"modifier.apply failed: {exc}", transaction) from exc
    finally:
        try:
            for selected in list(bpy.context.selected_objects):
                selected.select_set(False)
            for selected in selected_before:
                if bpy.context.scene.objects.get(selected.name) is selected:
                    selected.select_set(True)
            bpy.context.view_layer.objects.active = active_before
        except Exception as cleanup_exc:
            transaction["warnings"].append(f"selection restore failed: {cleanup_exc}")
            if not transaction.get("changed"):
                transaction["changed"] = True
                bump_epoch()
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def convert_evaluated(params: dict[str, Any]) -> dict[str, Any]:
    """Copy the evaluated mesh into a new named object, optionally removing its source."""
    _unknown(params, {"object_name", "name", "keep_original", "dry_run", "confirm", "expected_state",
                      "fragment_small_ratio"})
    object_name = _name(params.get("object_name"), "object_name")
    new_name = _name(params.get("name"), "name")
    keep_original = _strict_bool(params.get("keep_original"), "keep_original", True)
    _validate_expected(params)
    small_ratio = fragment_small_ratio(params.get("fragment_small_ratio", 0.01))
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    source = _mesh_object(bpy, object_name)
    if bpy.data.objects.get(new_name) is not None:
        raise InvalidStateError(f"Object already exists: {new_name}")
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("object.convert_evaluated requires Blender Object mode")
    before = _mesh_counts(source)
    journal = [{"op": "object.convert_evaluated", "target": source.name, "before": before, "after": new_name, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="object.convert_evaluated",
        affected_datablocks=[{"type": "Object", "name": source.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=not keep_original,
        state_token=current_state_token(bpy),
        extra={"source": source.name, "new_object": new_name, "keep_original": keep_original,
               "instance_realization": "not_forced",
               "recovery": "Use an explicit Realize Instances node before conversion when instances must become mesh geometry."},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    before_components = capture_component_summary(transaction, source.data, source, small_ratio)
    evaluated = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = None
    result = None
    try:
        mesh = bpy.data.meshes.new_from_object(
            evaluated, preserve_all_data_layers=True, depsgraph=bpy.context.evaluated_depsgraph_get()
        )
        if mesh is None:
            raise InvalidStateError("Evaluated object did not yield mesh geometry")
        mesh.name = f"{new_name} Mesh"
        result = bpy.data.objects.new(new_name, mesh)
        result.matrix_world = source.matrix_world.copy()
        collections = list(source.users_collection) or [bpy.context.scene.collection]
        for collection in collections:
            collection.objects.link(result)
        after = _mesh_counts(result)
        if not keep_original:
            bpy.data.objects.remove(source, do_unlink=True)
        journal[0].update(status="applied", after=after)
        transaction.update(changed=True, result_object=result.name, mesh=mesh.name,
                           before_mesh=before, after_mesh=after,
                           material_count=len(mesh.materials), uv_layers=[layer.name for layer in mesh.uv_layers],
                           normal_handling="evaluated mesh normals copied when Blender exposes them",
                           instance_realization="not_forced")
        append_new_islands(transaction, before_components, mesh, result, small_ratio)
    except Exception as exc:
        cleanup_errors = []
        if result is not None and bpy.data.objects.get(result.name) is result:
            try:
                bpy.data.objects.remove(result, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove result object: {cleanup_exc}")
        if mesh is not None and mesh.users == 0:
            try:
                bpy.data.meshes.remove(mesh)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove result mesh: {cleanup_exc}")
        transaction.update(changed=bool(cleanup_errors), warnings=cleanup_errors)
        if cleanup_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"object.convert_evaluated failed: {exc}", transaction) from exc
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
