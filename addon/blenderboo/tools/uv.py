"""Bounded UV inspection, layer lifecycle, projection, transform, unwrap, and packing."""

from __future__ import annotations

import math
from typing import Any, Callable

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy, vector_values
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


MAX_PAGE = 2000
DEFAULT_UV_BUDGET = 250_000
MAX_UV_BUDGET = 5_000_000
MAX_UV_LAYERS = 64
MAX_FACE_INDICES = 100_000
PROJECTION_MODES = frozenset({"PLANAR", "CUBE", "CYLINDER", "SPHERE"})
PLANES = frozenset({"XY", "XZ", "YZ"})


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


def _vector(value: Any, label: str, length: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise InvalidParamsError(f"{label} must be a {length}-number array")
    return [_number(item, label, -1_000_000.0, 1_000_000.0) for item in value]


def _enum(value: Any, label: str, allowed: frozenset[str]) -> str:
    result = _name(value, label).upper()
    if result not in allowed:
        raise InvalidParamsError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return result


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _page(params: dict[str, Any]) -> tuple[int, int]:
    limit = _integer(params.get("limit", 200), "limit", 1, MAX_PAGE)
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError("cursor must be a decimal string returned by uv.inspect")


def _budget(params: dict[str, Any], count: int, label: str) -> int:
    maximum = _integer(
        params.get("max_uv_items", DEFAULT_UV_BUDGET),
        "max_uv_items",
        1,
        MAX_UV_BUDGET,
    )
    if count > maximum:
        raise InvalidStateError(f"{label} requires {count} UV items, above max_uv_items {maximum}")
    return maximum


def _mesh_object(bpy: Any, object_name: str, *, mutate: bool = False) -> Any:
    obj = bpy.context.scene.objects.get(object_name)
    if obj is None or obj.type != "MESH" or obj.data is None:
        raise NotFoundError(f"Mesh object not found in current scene: {object_name}")
    if mutate:
        if bpy.context.mode != "OBJECT":
            raise InvalidStateError("UV mutation requires Blender Object mode")
        if obj.data.users > 1:
            raise InvalidStateError("UV mutation refuses shared mesh datablocks; make the mesh data single-user first")
    return obj


def _layer(mesh: Any, name: str | None) -> Any:
    if name is not None:
        layer = mesh.uv_layers.get(name)
        if layer is None:
            raise NotFoundError(f"UV layer not found: {name}")
        return layer
    layer = mesh.uv_layers.active
    if layer is None:
        raise InvalidStateError("Mesh has no active UV layer")
    return layer


def _set_active_layer(mesh: Any, layer: Any) -> None:
    for index, candidate in enumerate(mesh.uv_layers):
        if candidate is layer or candidate.name == layer.name:
            mesh.uv_layers.active_index = index
            return
    raise NotFoundError(f"UV layer is no longer attached to the mesh: {layer.name}")


def _bounds(values: list[list[float]]) -> dict[str, list[float]] | None:
    if not values:
        return None
    return {
        "min": [min(value[axis] for value in values) for axis in range(2)],
        "max": [max(value[axis] for value in values) for axis in range(2)],
    }


def _layer_summary(mesh: Any, layer: Any) -> dict[str, Any]:
    values = [vector_values(item.uv) for item in layer.data]
    summary = {
        "name": layer.name,
        "active": mesh.uv_layers.active is layer,
        "active_render": bool(getattr(layer, "active_render", False)),
        "active_clone": bool(getattr(layer, "active_clone", False)),
        "loop_count": len(layer.data),
        "bounds": _bounds(values),
        "selected_loops": sum(bool(getattr(item, "select", False)) for item in layer.data),
        "pinned_loops": sum(bool(getattr(item, "pin_uv", False)) for item in layer.data),
    }
    return summary


def _loop_polygon_indices(mesh: Any) -> list[int]:
    result = [-1] * len(mesh.loops)
    for polygon in mesh.polygons:
        for loop_index in polygon.loop_indices:
            result[loop_index] = polygon.index
    return result


def _uv_close(left: Any, right: Any, epsilon: float = 1e-6) -> bool:
    return abs(float(left[0]) - float(right[0])) <= epsilon and abs(float(left[1]) - float(right[1])) <= epsilon


def _island_audit(mesh: Any, layer: Any) -> dict[str, Any]:
    parent = list(range(len(mesh.polygons)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    edges: dict[tuple[int, int], list[tuple[int, dict[int, Any]]]] = {}
    zero_area = 0
    for polygon in mesh.polygons:
        loops = list(polygon.loop_indices)
        area = 0.0
        for offset, loop_index in enumerate(loops):
            next_loop = loops[(offset + 1) % len(loops)]
            current_uv = layer.data[loop_index].uv
            next_uv = layer.data[next_loop].uv
            area += float(current_uv[0]) * float(next_uv[1]) - float(next_uv[0]) * float(current_uv[1])
            left_vertex = mesh.loops[loop_index].vertex_index
            right_vertex = mesh.loops[next_loop].vertex_index
            key = tuple(sorted((left_vertex, right_vertex)))
            edges.setdefault(key, []).append(
                (polygon.index, {left_vertex: current_uv.copy(), right_vertex: next_uv.copy()})
            )
        if abs(area) <= 1e-12:
            zero_area += 1
    for records in edges.values():
        for index, (left_polygon, left_uvs) in enumerate(records):
            for right_polygon, right_uvs in records[index + 1 :]:
                if all(vertex in right_uvs and _uv_close(uv, right_uvs[vertex]) for vertex, uv in left_uvs.items()):
                    union(left_polygon, right_polygon)
    roots = {find(polygon.index) for polygon in mesh.polygons}
    return {
        "island_count": len(roots),
        "zero_area_faces": zero_area,
        "mesh_edge_count": len(edges),
    }


def _selected_uv_quality(mesh: Any, layer: Any, polygons: list[Any]) -> dict[str, Any]:
    zero_area_faces = 0
    densities = []
    for polygon in polygons:
        loops = list(polygon.loop_indices)
        twice_area = 0.0
        for offset, index in enumerate(loops):
            next_index = loops[(offset + 1) % len(loops)]
            left, right = layer.data[index].uv, layer.data[next_index].uv
            twice_area += float(left[0]) * float(right[1]) - float(right[0]) * float(left[1])
        uv_area = abs(twice_area) / 2.0
        if uv_area <= 1e-12:
            zero_area_faces += 1
        if polygon.area > 1e-12 and uv_area > 1e-12:
            densities.append(math.sqrt(uv_area / float(polygon.area)))
    return {
        "checked_faces": len(polygons),
        "zero_area_faces": zero_area_faces,
        "texel_density_min": min(densities) if densities else None,
        "texel_density_max": max(densities) if densities else None,
        "texel_density_spread": max(densities) / min(densities) if densities else None,
        "overlap_check": "not_performed",
    }


def inspect_uv(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"object_name", "layer_name", "include_loops", "include_islands", "max_uv_items", "limit", "cursor"})
    object_name = _name(params.get("object_name"), "object_name")
    layer_name = _optional_name(params.get("layer_name"), "layer_name")
    include_loops = _strict_bool(params.get("include_loops"), "include_loops", False)
    include_islands = _strict_bool(params.get("include_islands"), "include_islands", False)
    max_items = _integer(params.get("max_uv_items", DEFAULT_UV_BUDGET), "max_uv_items", 1, MAX_UV_BUDGET)
    limit, offset = _page(params)
    bpy = get_bpy()
    obj = _mesh_object(bpy, object_name)
    mesh = obj.data
    layers = list(mesh.uv_layers)
    if len(layers) > MAX_UV_LAYERS:
        raise InvalidStateError(f"Mesh has more than {MAX_UV_LAYERS} UV layers")
    total_items = len(mesh.loops) * (
        len(layers) + int(include_loops) + int(include_islands)
    )
    if total_items > max_items:
        raise InvalidStateError(f"UV inspection requires {total_items} UV items, above max_uv_items {max_items}")
    selected_layer = _layer(mesh, layer_name) if layer_name is not None or include_loops or include_islands else mesh.uv_layers.active
    result: dict[str, Any] = {
        "object": obj.name,
        "mesh": mesh.name,
        "vertex_count": len(mesh.vertices),
        "polygon_count": len(mesh.polygons),
        "loop_count": len(mesh.loops),
        "layer_count": len(layers),
        "active_layer": mesh.uv_layers.active.name if mesh.uv_layers.active else None,
        "layers": [_layer_summary(mesh, layer) for layer in layers],
        "state_token": current_state_token(bpy),
    }
    if selected_layer is not None and include_islands:
        result["island_audit"] = {"layer": selected_layer.name, **_island_audit(mesh, selected_layer)}
    if selected_layer is not None and include_loops:
        polygon_indices = _loop_polygon_indices(mesh)
        end = min(offset + limit, len(selected_layer.data))
        page = [
            {
                "loop_index": index,
                "polygon_index": polygon_indices[index],
                "vertex_index": mesh.loops[index].vertex_index,
                "uv": vector_values(item.uv),
                "selected": bool(getattr(item, "select", False)),
                "pinned": bool(getattr(item, "pin_uv", False)),
            }
            for index in range(offset, end)
            for item in (selected_layer.data[index],)
        ]
        next_offset = end
        result.update(
            loop_layer=selected_layer.name,
            loops=page,
            next_cursor=str(next_offset) if next_offset < len(selected_layer.data) else None,
            truncated=next_offset < len(selected_layer.data),
        )
    return ToolResult(data=result)


def _audit_single_uv(params: dict[str, Any], overlap_result: dict[str, Any]) -> ToolResult:
    """Measure absolute density and raster overlap without changing the scene."""
    _unknown(params, {"object_name", "layer_name", "texture_size", "density_floor_px_per_cm", "target_px_per_cm", "max_overlap_pct", "raster_size", "max_uv_items"})
    object_name = _name(params.get("object_name"), "object_name")
    texture_size = _integer(params.get("texture_size"), "texture_size", 32, 16384)
    raster_size = _integer(params.get("raster_size", 512), "raster_size", 64, 4096)
    floor = _number(params.get("density_floor_px_per_cm", 0), "density_floor_px_per_cm", 0, 100000)
    target = None if "target_px_per_cm" not in params else _number(params["target_px_per_cm"], "target_px_per_cm", 0, 100000)
    max_overlap = None if "max_overlap_pct" not in params else _number(params["max_overlap_pct"], "max_overlap_pct", 0, 100)
    bpy = get_bpy()
    obj = _mesh_object(bpy, object_name)
    mesh = obj.data
    _budget(params, len(mesh.loops), "UV audit")
    layer = _layer(mesh, _optional_name(params.get("layer_name"), "layer_name"))
    if len(mesh.polygons) > 100000:
        raise InvalidStateError("UV audit is limited to 100000 polygons")
    metres_per_unit = float(bpy.context.scene.unit_settings.scale_length)
    if not math.isfinite(metres_per_unit) or metres_per_unit <= 0:
        raise InvalidStateError("Scene scale_length must be positive")

    parent = list(range(len(mesh.polygons)))
    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    edge_uses: dict[tuple[int, int], list[tuple[int, dict[int, tuple[float, float]]]]] = {}
    density_by_face: list[float | None] = []
    outside_faces = 0
    degenerate_faces = 0
    for poly in mesh.polygons:
        loops = list(poly.loop_indices)
        uv = [tuple(map(float, layer.data[i].uv)) for i in loops]
        if any(not (0 <= u <= 1 and 0 <= v <= 1) for u, v in uv):
            outside_faces += 1
        signed = sum(uv[i][0] * uv[(i + 1) % len(uv)][1] - uv[(i + 1) % len(uv)][0] * uv[i][1] for i in range(len(uv)))
        uv_area = abs(signed) * 0.5
        world = [obj.matrix_world @ mesh.vertices[mesh.loops[i].vertex_index].co for i in loops]
        world_area = sum(((world[i] - world[0]).cross(world[i + 1] - world[0])).length * 0.5 for i in range(1, len(world) - 1))
        density = texture_size * math.sqrt(uv_area / world_area) / (100 * metres_per_unit) if uv_area > 1e-12 and world_area > 1e-12 else None
        density_by_face.append(density)
        if density is None:
            degenerate_faces += 1
        for offset, loop_index in enumerate(loops):
            next_index = loops[(offset + 1) % len(loops)]
            a = mesh.loops[loop_index].vertex_index
            b = mesh.loops[next_index].vertex_index
            edge_uses.setdefault(tuple(sorted((a, b))), []).append((poly.index, {a: uv[offset], b: uv[(offset + 1) % len(uv)]}))
    for uses in edge_uses.values():
        for left_index, left_uv in uses:
            for right_index, right_uv in uses:
                if left_index >= right_index or not all(vertex in right_uv and _uv_close(value, right_uv[vertex]) for vertex, value in left_uv.items()):
                    continue
                a, b = find(left_index), find(right_index)
                parent[b] = a
    island_faces: dict[int, list[int]] = {}
    for poly in mesh.polygons:
        island_faces.setdefault(find(poly.index), []).append(poly.index)
    island_rows = []
    for indices in island_faces.values():
        values = [density_by_face[i] for i in indices if density_by_face[i] is not None]
        minimum = min(values) if values else None
        island_rows.append({"face_count": len(indices), "min_px_per_cm": minimum, "below_floor": minimum is None or minimum < floor})

    covered = overlap_result["covered_raster_pixels"]
    overlap_pct = overlap_result["overlap_pct"]
    densities = sorted(value for value in density_by_face if value is not None)
    def percentile(q: float) -> float | None:
        if not densities:
            return None
        position = q * (len(densities) - 1)
        lo = int(position)
        hi = min(lo + 1, len(densities) - 1)
        return densities[lo] + (densities[hi] - densities[lo]) * (position - lo)
    checks = {
        "target_px_per_cm": None if target is None else (percentile(0.05) is not None and percentile(0.05) >= target),
        "max_overlap_pct": None if max_overlap is None else (overlap_pct is not None and overlap_pct <= max_overlap),
        "density_floor": all(not row["below_floor"] for row in island_rows),
        "uv_in_unit_square": outside_faces == 0,
        "nondegenerate": degenerate_faces == 0,
    }
    return ToolResult(data={
        "object": obj.name, "layer": layer.name, "texture_size": texture_size,
        "raster_size": raster_size, "scale_length_metres": metres_per_unit,
        "px_per_cm": {"p5": percentile(0.05), "median": percentile(0.5), "p95": percentile(0.95)},
        "overlap_pct": overlap_pct, "covered_raster_pixels": covered,
        "island_count": len(island_rows), "islands_below_floor": sum(row["below_floor"] for row in island_rows),
        "islands": island_rows[:128], "islands_truncated": len(island_rows) > 128,
        "outside_unit_square_faces": outside_faces, "degenerate_faces": degenerate_faces,
        "checks": checks, "pass": all(value for value in checks.values() if value is not None),
        "state_token": current_state_token(bpy),
    })


def _prepare_mutation(params: dict[str, Any]) -> tuple[Any, Any]:
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, _name(params.get("object_name"), "object_name"), mutate=True)
    return bpy, obj


def create_uv_layer(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "name", "copy_from", "set_active", "max_uv_items", "expected_state"})
    name = _name(params.get("name"), "name")
    copy_from = _optional_name(params.get("copy_from"), "copy_from")
    set_active = _strict_bool(params.get("set_active"), "set_active", True)
    bpy, obj = _prepare_mutation(params)
    mesh = obj.data
    _budget(params, len(mesh.loops), "UV layer creation")
    if len(mesh.uv_layers) >= MAX_UV_LAYERS:
        raise InvalidStateError(f"Mesh already has the maximum {MAX_UV_LAYERS} UV layers")
    if mesh.uv_layers.get(name) is not None:
        raise InvalidStateError(f"UV layer already exists: {name}")
    source = _layer(mesh, copy_from) if copy_from is not None else None
    source_values = [vector_values(item.uv) for item in source.data] if source is not None else None
    previous_active = mesh.uv_layers.active_index if mesh.uv_layers.active else None
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": mesh.name}])
    created = None
    try:
        created = mesh.uv_layers.new(name=name, do_init=False)
        if source_values is not None:
            for item, value in zip(created.data, source_values):
                item.uv = value
        if set_active:
            _set_active_layer(mesh, created)
        mesh.update()
    except Exception as exc:
        rollback_errors = []
        if created is not None and mesh.uv_layers.get(created.name) is created:
            try:
                mesh.uv_layers.remove(created)
            except Exception as rollback_exc:
                rollback_errors.append(f"remove partial UV layer: {rollback_exc}")
        if previous_active is not None and previous_active < len(mesh.uv_layers):
            try:
                mesh.uv_layers.active_index = previous_active
            except Exception as rollback_exc:
                rollback_errors.append(f"restore active UV layer: {rollback_exc}")
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"uv.layer_create failed: {exc}", transaction) from exc
    summary = _layer_summary(mesh, created)
    transaction.update(
        object=obj.name,
        mesh=mesh.name,
        layer=created.name,
        copied_from=copy_from,
        changed=True,
        journal=[{"op": "uv.layer_create", "target": created.name, "before": None, "after": summary, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def rename_uv_layer(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "layer_name", "new_name", "expected_state"})
    layer_name = _name(params.get("layer_name"), "layer_name")
    new_name = _name(params.get("new_name"), "new_name")
    bpy, obj = _prepare_mutation(params)
    mesh = obj.data
    layer = _layer(mesh, layer_name)
    if new_name != layer.name and mesh.uv_layers.get(new_name) is not None:
        raise InvalidStateError(f"UV layer already exists: {new_name}")
    before = layer.name
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": mesh.name}])
    try:
        layer.name = new_name
        after = layer.name
    except Exception as exc:
        rollback_error = None
        try:
            layer.name = before
        except Exception as rollback_exc:
            rollback_error = rollback_exc
        transaction.update(changed=rollback_error is not None)
        if rollback_error is not None:
            transaction["warnings"].append(f"restore UV layer name: {rollback_error}")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"uv.layer_rename failed: {exc}", transaction) from exc
    changed = before != after
    transaction.update(
        object=obj.name,
        mesh=mesh.name,
        before=before,
        after=after,
        changed=changed,
        journal=[{"op": "uv.layer_rename", "target": before, "before": before, "after": after, "status": "applied" if changed else "unchanged"}],
    )
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def set_active_uv_layer(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "layer_name", "set_render", "set_clone", "expected_state"})
    layer_name = _name(params.get("layer_name"), "layer_name")
    set_render = _strict_bool(params.get("set_render"), "set_render", False)
    set_clone = _strict_bool(params.get("set_clone"), "set_clone", False)
    bpy, obj = _prepare_mutation(params)
    mesh = obj.data
    layer = _layer(mesh, layer_name)
    before = {
        "active": mesh.uv_layers.active.name if mesh.uv_layers.active else None,
        "render": next((item.name for item in mesh.uv_layers if getattr(item, "active_render", False)), None),
        "clone": next((item.name for item in mesh.uv_layers if getattr(item, "active_clone", False)), None),
    }
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": mesh.name}])
    try:
        _set_active_layer(mesh, layer)
        if set_render:
            layer.active_render = True
        if set_clone:
            layer.active_clone = True
    except Exception as exc:
        rollback_errors = []
        for field, attr in (("render", "active_render"), ("clone", "active_clone")):
            previous = before[field]
            if previous is None:
                continue
            try:
                setattr(mesh.uv_layers.get(previous), attr, True)
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {field} UV layer: {rollback_exc}")
        if before["active"] is not None:
            try:
                _set_active_layer(mesh, mesh.uv_layers.get(before["active"]))
            except Exception as rollback_exc:
                rollback_errors.append(f"restore active UV layer: {rollback_exc}")
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"uv.layer_set_active failed: {exc}", transaction) from exc
    after = {
        "active": mesh.uv_layers.active.name if mesh.uv_layers.active else None,
        "render": next((item.name for item in mesh.uv_layers if getattr(item, "active_render", False)), None),
        "clone": next((item.name for item in mesh.uv_layers if getattr(item, "active_clone", False)), None),
    }
    changed = before != after
    transaction.update(
        object=obj.name,
        mesh=mesh.name,
        before=before,
        after=after,
        changed=changed,
        journal=[{"op": "uv.layer_set_active", "target": layer.name, "before": before, "after": after, "status": "applied" if changed else "unchanged"}],
    )
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_uv_layer(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "layer_name", "max_uv_items", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    layer_name = _name(params.get("layer_name"), "layer_name")
    _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name, mutate=True)
    mesh = obj.data
    layer = _layer(mesh, layer_name)
    _budget(params, len(layer.data), "UV layer removal preflight")
    summary = _layer_summary(mesh, layer)
    journal = [{"op": "uv.layer_remove", "target": layer.name, "before": summary, "after": None, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="uv.layer_remove",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": mesh.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "mesh": mesh.name, "layer": layer.name, "recovery": "Restore a checkpoint to recover a removed UV layer."},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        mesh.uv_layers.remove(layer)
        mesh.update()
        journal[0]["status"] = "applied"
    except Exception as exc:
        partial = mesh.uv_layers.get(layer_name) is None
        transaction["changed"] = partial
        if partial:
            transaction["warnings"].append("UV layer removal may have completed; restore the preflight checkpoint")
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"uv.layer_remove failed: {exc}", transaction) from exc
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _face_selection(mesh: Any, value: Any) -> list[Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise InvalidParamsError("face_selection must be an object")
    _unknown(value, {"indices", "material_index", "normal_direction", "min_dot"}, "face_selection fields")
    indices = value.get("indices")
    index_set = None
    if indices is not None:
        if not isinstance(indices, list) or not indices or len(indices) > MAX_FACE_INDICES:
            raise InvalidParamsError(f"face_selection.indices must contain 1 to {MAX_FACE_INDICES} indices")
        index_set = {
            _integer(index, "face index", 0, max(0, len(mesh.polygons) - 1))
            for index in indices
        }
    material_index = None
    if "material_index" in value:
        material_index = _integer(value["material_index"], "material_index", 0, 32767)
    direction = None
    min_dot = _number(value.get("min_dot", 0.0), "min_dot", -1.0, 1.0)
    if "normal_direction" in value:
        direction = _vector(value["normal_direction"], "normal_direction", 3)
        length = math.sqrt(sum(component * component for component in direction))
        if length <= 1e-12:
            raise InvalidParamsError("normal_direction must be non-zero")
        direction = [component / length for component in direction]
    elif "min_dot" in value:
        raise InvalidParamsError("min_dot requires normal_direction")
    selected = []
    for polygon in mesh.polygons:
        if index_set is not None and polygon.index not in index_set:
            continue
        if material_index is not None and polygon.material_index != material_index:
            continue
        if direction is not None and sum(float(polygon.normal[axis]) * direction[axis] for axis in range(3)) < min_dot:
            continue
        selected.append(polygon)
    if not selected:
        raise InvalidStateError("face_selection matched no polygons")
    return selected


def _selected_loop_indices(polygons: list[Any]) -> list[int]:
    return [loop_index for polygon in polygons for loop_index in polygon.loop_indices]


def _project_coordinate(co: Any, normal: Any, mode: str, plane: str) -> list[float]:
    x, y, z = (float(co[index]) for index in range(3))
    if mode == "PLANAR":
        return {"XY": [x, y], "XZ": [x, z], "YZ": [y, z]}[plane]
    if mode == "CUBE":
        axis = max(range(3), key=lambda index: abs(float(normal[index])))
        return ([y, z] if axis == 0 else [x, z] if axis == 1 else [x, y])
    radius_xy = math.hypot(x, y)
    u = math.atan2(y, x) / math.tau + 0.5
    if mode == "CYLINDER":
        return [u, z]
    radius = math.sqrt(x * x + y * y + z * z)
    v = 0.5 if radius <= 1e-12 else math.asin(max(-1.0, min(1.0, z / radius))) / math.pi + 0.5
    return [u, v]


def _normalize_values(values: dict[int, list[float]]) -> None:
    bounds = _bounds(list(values.values()))
    if bounds is None:
        return
    size = [bounds["max"][axis] - bounds["min"][axis] for axis in range(2)]
    for value in values.values():
        for axis in range(2):
            value[axis] = 0.5 if size[axis] <= 1e-12 else (value[axis] - bounds["min"][axis]) / size[axis]


def _uv_preflight(
    bpy: Any,
    obj: Any,
    layer_name: str,
    create_layer: bool,
    polygons: list[Any],
    values: dict[int, list[float]],
    action: str,
    dry_run: bool,
    expected_state: Any,
    extra: dict[str, Any],
) -> tuple[dict[str, Any], Any | None]:
    mesh = obj.data
    existing = mesh.uv_layers.get(layer_name)
    if existing is None and not create_layer:
        raise NotFoundError(f"UV layer not found: {layer_name}; set create_layer true to create it")
    loop_indices = sorted(values)
    before_values = [vector_values(existing.data[index].uv) for index in loop_indices] if existing is not None else []
    changed = existing is None or any(
        before_values[offset] != values[index]
        for offset, index in enumerate(loop_indices)
    )
    journal = [{
        "op": action,
        "target": layer_name,
        "polygon_count": len(polygons),
        "loop_count": len(loop_indices),
        "before_bounds": _bounds(before_values),
        "after_bounds": _bounds([values[index] for index in loop_indices]),
        "status": "planned" if changed else "unchanged",
    }]
    transaction, apply = prepare_destructive_action(
        action=action,
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": mesh.name}],
        journal=journal,
        dry_run=dry_run,
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"object": obj.name, "mesh": mesh.name, "layer": layer_name, **extra},
    )
    if not apply:
        return transaction, None
    if not changed:
        transaction["changed"] = False
        return transaction, existing
    require_document_write_access(bpy)
    check_expected_state(expected_state, bpy)
    created = None
    layer = existing
    before = {index: vector_values(existing.data[index].uv) for index in loop_indices} if existing is not None else {}
    try:
        if layer is None:
            created = mesh.uv_layers.new(name=layer_name, do_init=False)
            layer = created
        for index in loop_indices:
            layer.data[index].uv = values[index]
        _set_active_layer(mesh, layer)
        mesh.update()
        journal[0]["status"] = "applied"
    except Exception as exc:
        rollback_errors = []
        if created is not None and mesh.uv_layers.get(created.name) is created:
            try:
                mesh.uv_layers.remove(created)
            except Exception as rollback_exc:
                rollback_errors.append(f"remove created UV layer: {rollback_exc}")
        elif layer is not None:
            for index, value in before.items():
                try:
                    layer.data[index].uv = value
                except Exception as rollback_exc:
                    rollback_errors.append(f"restore UV loop {index}: {rollback_exc}")
                    break
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"{action} failed: {exc}", transaction) from exc
    transaction.update(
        changed=True,
        created_layer=created is not None,
        before_bounds=journal[0]["before_bounds"],
        after_bounds=journal[0]["after_bounds"],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction, layer


def project_uv(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "layer_name", "create_layer", "mode", "plane", "face_selection", "normalize", "scale", "offset", "max_uv_items", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    layer_name = _name(params.get("layer_name"), "layer_name")
    create_layer = _strict_bool(params.get("create_layer"), "create_layer", False)
    mode = _enum(params.get("mode"), "mode", PROJECTION_MODES)
    plane = _enum(params.get("plane", "XY"), "plane", PLANES)
    if mode != "PLANAR" and "plane" in params:
        raise InvalidParamsError("plane is valid only for PLANAR projection")
    normalize = _strict_bool(params.get("normalize"), "normalize", False)
    scale = _vector(params.get("scale", [1, 1]), "scale", 2)
    offset = _vector(params.get("offset", [0, 0]), "offset", 2)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name, mutate=True)
    mesh = obj.data
    polygons = _face_selection(mesh, params.get("face_selection"))
    selected_loop_count = sum(len(polygon.loop_indices) for polygon in polygons)
    _budget(params, len(mesh.loops) if create_layer and mesh.uv_layers.get(layer_name) is None else selected_loop_count, "UV projection")
    values: dict[int, list[float]] = {}
    for polygon in polygons:
        polygon_values = []
        for loop_index in polygon.loop_indices:
            co = mesh.vertices[mesh.loops[loop_index].vertex_index].co
            polygon_values.append((loop_index, _project_coordinate(co, polygon.normal, mode, plane)))
        if mode in {"CYLINDER", "SPHERE"} and polygon_values:
            us = [value[0] for _, value in polygon_values]
            if max(us) - min(us) > 0.5:
                for _, value in polygon_values:
                    if value[0] < 0.5:
                        value[0] += 1.0
        values.update(polygon_values)
    if normalize:
        _normalize_values(values)
    for value in values.values():
        value[0] = value[0] * scale[0] + offset[0]
        value[1] = value[1] * scale[1] + offset[1]
    transaction, _ = _uv_preflight(
        bpy, obj, layer_name, create_layer, polygons, values,
        "uv.project", dry_run, params.get("expected_state"),
        {"mode": mode, "plane": plane if mode == "PLANAR" else None, "normalized": normalize},
    )
    return transaction


def transform_uv(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "layer_name", "face_selection", "translation", "rotation", "scale", "pivot", "max_uv_items", "dry_run", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    layer_name = _name(params.get("layer_name"), "layer_name")
    translation = _vector(params.get("translation", [0, 0]), "translation", 2)
    rotation = _number(params.get("rotation", 0.0), "rotation", -math.tau * 1000, math.tau * 1000)
    scale = _vector(params.get("scale", [1, 1]), "scale", 2)
    pivot_mode = _name(params.get("pivot", "MEDIAN"), "pivot").upper()
    if pivot_mode not in {"ORIGIN", "CENTER", "MEDIAN"}:
        raise InvalidParamsError("pivot must be ORIGIN, CENTER, or MEDIAN")
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name, mutate=True)
    mesh = obj.data
    layer = _layer(mesh, layer_name)
    polygons = _face_selection(mesh, params.get("face_selection"))
    loop_indices = _selected_loop_indices(polygons)
    _budget(params, len(loop_indices), "UV transform")
    before = {index: vector_values(layer.data[index].uv) for index in loop_indices}
    all_values = list(before.values())
    if pivot_mode == "ORIGIN":
        pivot = [0.0, 0.0]
    elif pivot_mode == "CENTER":
        bounds = _bounds(all_values)
        pivot = [(bounds["min"][axis] + bounds["max"][axis]) * 0.5 for axis in range(2)]
    else:
        pivot = [sum(value[axis] for value in all_values) / len(all_values) for axis in range(2)]
    cosine, sine = math.cos(rotation), math.sin(rotation)
    values = {}
    for index, value in before.items():
        x = (value[0] - pivot[0]) * scale[0]
        y = (value[1] - pivot[1]) * scale[1]
        values[index] = [
            pivot[0] + x * cosine - y * sine + translation[0],
            pivot[1] + x * sine + y * cosine + translation[1],
        ]
    transaction, _ = _uv_preflight(
        bpy, obj, layer_name, False, polygons, values,
        "uv.transform", dry_run, params.get("expected_state"),
        {"translation": translation, "rotation": rotation, "scale": scale, "pivot": pivot_mode},
    )
    return transaction


def _uv_item_selection(item: Any) -> dict[str, bool]:
    return {
        name: bool(getattr(item, name))
        for name in ("select", "select_edge")
        if hasattr(item, name)
    }


def _restore_object_context(bpy: Any, selected: list[Any], active: Any, warnings: list[str]) -> None:
    try:
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as exc:
        warnings.append(f"restore Object mode: {exc}")
    try:
        for obj in list(bpy.context.selected_objects):
            obj.select_set(False)
        for obj in selected:
            if bpy.context.scene.objects.get(obj.name) is obj:
                obj.select_set(True)
        bpy.context.view_layer.objects.active = (
            active if active is not None and bpy.context.scene.objects.get(active.name) is active else None
        )
    except Exception as exc:
        warnings.append(f"restore object selection: {exc}")


def _run_uv_operator(
    params: dict[str, Any],
    *,
    action: str,
    operation_settings: dict[str, Any],
    invoke: Callable[[Any], Any],
) -> dict[str, Any]:
    object_name = _name(params.get("object_name"), "object_name")
    layer_name = _name(params.get("layer_name"), "layer_name")
    create_layer = _strict_bool(params.get("create_layer"), "create_layer", False)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name, mutate=True)
    mesh = obj.data
    polygons = _face_selection(mesh, params.get("face_selection"))
    selected_polygon_indices = {polygon.index for polygon in polygons}
    _budget(params, len(mesh.loops), f"{action} rollback snapshot")
    existing = mesh.uv_layers.get(layer_name)
    if existing is None and not create_layer:
        raise NotFoundError(f"UV layer not found: {layer_name}; set create_layer true to create it")
    loop_indices = _selected_loop_indices(polygons)
    before_bounds = _bounds([vector_values(existing.data[index].uv) for index in loop_indices]) if existing else None
    journal = [{
        "op": action,
        "target": layer_name,
        "polygon_count": len(polygons),
        "loop_count": len(loop_indices),
        "before_bounds": before_bounds,
        "after_bounds": None,
        "status": "planned",
    }]
    transaction, apply = prepare_destructive_action(
        action=action,
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": mesh.name}],
        journal=journal,
        dry_run=dry_run,
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={
            "object": obj.name,
            "mesh": mesh.name,
            "layer": layer_name,
            "settings": operation_settings,
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    selected_before = list(bpy.context.selected_objects)
    active_before = bpy.context.view_layer.objects.active
    polygon_selection = [bool(polygon.select) for polygon in mesh.polygons]
    active_uv_index = mesh.uv_layers.active_index if mesh.uv_layers.active else None
    created = None
    created_name = None
    layer = existing
    uv_before = [vector_values(item.uv) for item in existing.data] if existing else None
    uv_selection_before = [_uv_item_selection(item) for item in existing.data] if existing else None
    operation_error = None
    operator_finished = False
    quality = None
    rollback_errors: list[str] = []
    cleanup_errors: list[str] = []
    try:
        if layer is None:
            created = mesh.uv_layers.new(name=layer_name, do_init=False)
            layer = created
            created_name = created.name
            uv_selection_before = [_uv_item_selection(item) for item in layer.data]
        _set_active_layer(mesh, layer)
        for selected in list(bpy.context.selected_objects):
            selected.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        loop_polygons = _loop_polygon_indices(mesh)
        for polygon in mesh.polygons:
            polygon.select = polygon.index in selected_polygon_indices
        for index, item in enumerate(layer.data):
            selected = loop_polygons[index] in selected_polygon_indices
            if hasattr(item, "select"):
                item.select = selected
            if hasattr(item, "select_edge"):
                item.select_edge = selected
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_mode(type="FACE")
        result = invoke(bpy)
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender returned {sorted(result)}")
        operator_finished = True
        bpy.ops.object.mode_set(mode="OBJECT")
        mesh = obj.data
        layer = mesh.uv_layers.get(layer_name)
        if layer is None:
            raise InvalidStateError(f"UV operator completed but layer disappeared: {layer_name}")
        polygons = [mesh.polygons[index] for index in sorted(selected_polygon_indices) if index < len(mesh.polygons)]
        if len(polygons) != len(selected_polygon_indices):
            raise InvalidStateError("UV operator changed the polygon indices selected for unwrap")
        loop_indices = _selected_loop_indices(polygons)
        if loop_indices and max(loop_indices) >= len(layer.data):
            raise InvalidStateError("UV operator returned a layer shorter than the mesh loop count")
        journal[0]["after_bounds"] = _bounds([vector_values(layer.data[index].uv) for index in loop_indices])
        quality = _selected_uv_quality(mesh, layer, polygons)
        journal[0]["status"] = "applied"
    except Exception as exc:
        operation_error = exc
        try:
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception as rollback_exc:
            rollback_errors.append(f"leave Edit mode before UV rollback: {rollback_exc}")
        mesh = obj.data
        layer = mesh.uv_layers.get(layer_name)
        if created_name is not None and layer is not None:
            try:
                mesh.uv_layers.remove(layer)
                layer = None
            except Exception as rollback_exc:
                rollback_errors.append(f"remove created UV layer: {rollback_exc}")
        elif layer is not None and uv_before is not None:
            if len(layer.data) != len(uv_before):
                rollback_errors.append("UV loop count changed; cannot restore prior UV coordinates")
            else:
                for index, value in enumerate(uv_before):
                    try:
                        layer.data[index].uv = value
                    except Exception as rollback_exc:
                        rollback_errors.append(f"restore UV loop {index}: {rollback_exc}")
                        break
        if operator_finished:
            journal[0]["status"] = "partial" if rollback_errors else "rolled_back"
    finally:
        _restore_object_context(bpy, selected_before, active_before, cleanup_errors)
        if bpy.context.mode == "OBJECT":
            try:
                mesh = obj.data
                layer = mesh.uv_layers.get(layer_name)
                for polygon, selected in zip(mesh.polygons, polygon_selection):
                    polygon.select = selected
                if active_uv_index is not None and active_uv_index < len(mesh.uv_layers):
                    mesh.uv_layers.active_index = active_uv_index
                if uv_selection_before is not None and layer is not None:
                    for item, selection in zip(layer.data, uv_selection_before):
                        for name, value in selection.items():
                            setattr(item, name, value)
                mesh.update()
            except Exception as cleanup_exc:
                cleanup_errors.append(f"restore mesh/UV selection: {cleanup_exc}")
    if operation_error is not None:
        warnings = [*rollback_errors, *cleanup_errors]
        transaction.update(changed=bool(rollback_errors or cleanup_errors), warnings=warnings)
        if rollback_errors or cleanup_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"{action} failed: {operation_error}", transaction) from operation_error
    transaction.update(
        changed=True,
        created_layer=created is not None,
        before_bounds=before_bounds,
        after_bounds=journal[0]["after_bounds"],
        uv_quality=quality,
        warnings=cleanup_errors,
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    if cleanup_errors:
        raise MutationApplyError(f"{action} applied but context restoration failed", transaction)
    return transaction


def unwrap_uv(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "layer_name", "create_layer", "face_selection", "method", "margin", "fill_holes", "correct_aspect", "angle_limit", "island_margin", "area_weight", "max_uv_items", "dry_run", "expected_state"})
    method = _name(params.get("method", "ANGLE_BASED"), "method").upper()
    if method not in {"ANGLE_BASED", "CONFORMAL", "SMART", "MINIMUM_STRETCH"}:
        raise InvalidParamsError("method must be ANGLE_BASED, CONFORMAL, SMART, or MINIMUM_STRETCH")
    margin = _number(params.get("margin", 0.001), "margin", 0.0, 1.0)
    fill_holes = _strict_bool(params.get("fill_holes"), "fill_holes", True)
    correct_aspect = _strict_bool(params.get("correct_aspect"), "correct_aspect", True)
    if method == "SMART":
        if "fill_holes" in params or "correct_aspect" in params:
            raise InvalidParamsError("SMART does not accept fill_holes or correct_aspect")
        angle_limit = _number(params.get("angle_limit", math.radians(66.0)), "angle_limit", 0.0, math.pi / 2)
        island_margin = _number(params.get("island_margin", margin), "island_margin", 0.0, 1.0)
        area_weight = _number(params.get("area_weight", 0.0), "area_weight", 0.0, 1.0)
        return _run_uv_operator(
            params,
            action="uv.unwrap",
            operation_settings={"method": method, "angle_limit": angle_limit, "island_margin": island_margin, "area_weight": area_weight},
            invoke=lambda bpy: bpy.ops.uv.smart_project(angle_limit=angle_limit, island_margin=island_margin, area_weight=area_weight),
        )
    if any(key in params for key in ("angle_limit", "island_margin", "area_weight")):
        raise InvalidParamsError("angle_limit, island_margin, and area_weight require SMART method")
    if method == "MINIMUM_STRETCH":
        bpy = get_bpy()
        available = {item.identifier for item in bpy.ops.uv.unwrap.get_rna_type().properties["method"].enum_items}
        if method not in available:
            raise InvalidStateError("MINIMUM_STRETCH unwrap requires Blender 4.3 or newer with SLIM support")
    return _run_uv_operator(
        params,
        action="uv.unwrap",
        operation_settings={
            "method": method,
            "margin": margin,
            "fill_holes": fill_holes,
            "correct_aspect": correct_aspect,
        },
        invoke=lambda bpy: bpy.ops.uv.unwrap(
            method=method,
            fill_holes=fill_holes,
            correct_aspect=correct_aspect,
            margin=margin,
        ),
    )


def pack_uv(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "layer_name", "face_selection", "rotate", "scale_to_fit", "average_island_scale", "margin", "max_uv_items", "dry_run", "expected_state"})
    rotate = _strict_bool(params.get("rotate"), "rotate", True)
    scale_to_fit = _strict_bool(params.get("scale_to_fit"), "scale_to_fit", True)
    average_scale = _strict_bool(params.get("average_island_scale"), "average_island_scale", False)
    margin = _number(params.get("margin", 0.001), "margin", 0.0, 1.0)
    forwarded = dict(params)
    forwarded["create_layer"] = False
    return _run_uv_operator(
        forwarded,
        action="uv.pack",
        operation_settings={
            "rotate": rotate,
            "scale_to_fit": scale_to_fit,
            "average_island_scale": average_scale,
            "margin": margin,
        },
        invoke=lambda bpy: (
            bpy.ops.uv.average_islands_scale() if average_scale else {"FINISHED"},
            bpy.ops.uv.pack_islands(rotate=rotate, scale=scale_to_fit, margin=margin),
        )[-1],
    )


def _object_names(value: Any, minimum: int = 1) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= 128:
        raise InvalidParamsError(f"object_names must contain {minimum} to 128 names")
    names = [_name(item, "object_names item") for item in value]
    if len(set(names)) != len(names):
        raise InvalidParamsError("object_names must be distinct")
    return names


def _raster_overlap(triangles: Any, size: int, pair_limit: int, names: list[str]) -> dict[str, Any]:
    """Bounded pixel-centre evidence, retaining all triangle owners at collisions.

    Triangle edges are excluded: evidence is resolution-dependent, not an exact
    polygon-intersection proof. Distinct triangles in one folded face count.
    """
    from array import array
    owner = array("i", [-1]) * (size * size)
    collisions: dict[int, list[int]] = {}
    identities = []
    candidates = extra_owners = 0
    for triangle_id, (name, face, points) in enumerate(triangles):
        identities.append((name, face))
        if any(not math.isfinite(v) for point in points for v in point):
            raise InvalidStateError("UV audit refuses non-finite UV coordinates")
        a, b, c = points
        denominator = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(denominator) < 1e-14:
            continue
        x0 = max(0, math.floor(min(a[0], b[0], c[0])*size))
        x1 = min(size-1, math.ceil(max(a[0], b[0], c[0])*size))
        y0 = max(0, math.floor(min(a[1], b[1], c[1])*size))
        y1 = min(size-1, math.ceil(max(a[1], b[1], c[1])*size))
        candidates += max(0, x1-x0+1)*max(0, y1-y0+1)
        if candidates > 100_000_000:
            raise InvalidStateError("UV raster exceeded 100 million candidate pixels; lower raster_size or split the asset")
        for y in range(y0, y1+1):
            py = (y+0.5)/size
            for x in range(x0, x1+1):
                px = (x+0.5)/size
                u = ((b[1]-c[1])*(px-c[0])+(c[0]-b[0])*(py-c[1]))/denominator
                v = ((c[1]-a[1])*(px-c[0])+(a[0]-c[0])*(py-c[1]))/denominator
                if min(u, v, 1-u-v) <= 1e-9:
                    continue
                cell = y*size+x
                if owner[cell] < 0:
                    owner[cell] = triangle_id
                else:
                    extra_owners += 1
                    if extra_owners > 2_000_000:
                        raise InvalidStateError("UV overlap evidence exceeds 2 million collision samples; lower raster_size")
                    collisions.setdefault(cell, [owner[cell]]).append(triangle_id)
    covered = self_count = cross_count = pair_work = 0
    per_object = {name: {"covered_raster_pixels": 0, "overlap_raster_pixels": 0} for name in names}
    pairs: dict[tuple[Any, ...], int] = {}
    truncated = False
    for cell, first in enumerate(owner):
        if first < 0:
            continue
        covered += 1
        ids = collisions.get(cell)
        if ids is None:
            per_object[identities[first][0]]["covered_raster_pixels"] += 1
            continue
        object_counts: dict[str, int] = {}
        for triangle_id in ids:
            name = identities[triangle_id][0]
            object_counts[name] = object_counts.get(name, 0)+1
        for name, count in object_counts.items():
            per_object[name]["covered_raster_pixels"] += 1
            if count > 1:
                per_object[name]["overlap_raster_pixels"] += 1
        self_count += any(count > 1 for count in object_counts.values())
        cross_count += len(object_counts) > 1
        pixel_pairs = set()
        for offset, left in enumerate(ids):
            for right in ids[offset+1:]:
                pair_work += 1
                if pair_work > 5_000_000:
                    raise InvalidStateError("UV overlap pair work exceeds 5 million comparisons; lower raster_size")
                a, b = sorted((identities[left], identities[right]))
                key = (*a, *b)
                if key in pixel_pairs:
                    continue
                pixel_pairs.add(key)
                if key in pairs:
                    pairs[key] += 1
                elif len(pairs) < pair_limit:
                    pairs[key] = 1
                else:
                    truncated = True
    for row in per_object.values():
        row["overlap_pct"] = 100*row["overlap_raster_pixels"]/row["covered_raster_pixels"] if row["covered_raster_pixels"] else None
    return {
        "covered_raster_pixels": covered,
        "overlap_raster_pixels": len(collisions),
        "overlap_pct": 100*len(collisions)/covered if covered else None,
        "self_overlap_raster_pixels": self_count,
        "cross_object_overlap_raster_pixels": cross_count,
        "overlap_pairs": [{"object_a": a, "face_a": fa, "object_b": b, "face_b": fb,
                           "kind": "self" if a == b else "cross_object", "pixels": count}
                          for (a, fa, b, fb), count in sorted(pairs.items())],
        "overlap_pairs_truncated": truncated,
        "reported_overlap_pair_count": len(pairs),
        "per_object": per_object,
        "overlap_method": "triangle_pixel_centres_excluding_edges",
        "overlap_scope": "unit_square",
    }


def audit_uv(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"object_name", "object_names", "layer_name", "texture_size", "density_floor_px_per_cm", "target_px_per_cm", "max_overlap_pct", "raster_size", "max_uv_items", "max_overlap_pairs"})
    if ("object_name" in params) == ("object_names" in params):
        raise InvalidParamsError("Provide exactly one of object_name or object_names")
    names = _object_names(params["object_names"]) if "object_names" in params else [_name(params["object_name"], "object_name")]
    _integer(params.get("texture_size"), "texture_size", 32, 16384)
    _number(params.get("density_floor_px_per_cm", 0), "density_floor_px_per_cm", 0, 100000)
    if "target_px_per_cm" in params:
        _number(params["target_px_per_cm"], "target_px_per_cm", 0, 100000)
    if "max_overlap_pct" in params:
        _number(params["max_overlap_pct"], "max_overlap_pct", 0, 100)
    size = _integer(params.get("raster_size", 512), "raster_size", 64, 4096)
    pair_limit = _integer(params.get("max_overlap_pairs", 200), "max_overlap_pairs", 1, 2000)
    bpy = get_bpy()
    objects = [_mesh_object(bpy, name) for name in names]
    _budget(params, sum(len(obj.data.loops) for obj in objects), "UV atlas audit")
    if sum(len(obj.data.polygons) for obj in objects) > 100000:
        raise InvalidStateError("UV atlas audit is limited to 100000 polygons total")
    layers = [_layer(obj.data, _optional_name(params.get("layer_name"), "layer_name")) for obj in objects]
    def triangles():
        for obj, layer in zip(objects, layers):
            obj.data.calc_loop_triangles()
            for tri in obj.data.loop_triangles:
                yield obj.name, tri.polygon_index, [tuple(map(float, layer.data[i].uv)) for i in tri.loops]
    overlap = _raster_overlap(triangles(), size, pair_limit, names)
    per_object = overlap.pop("per_object")
    rows = []
    for name in names:
        forwarded = {k: v for k, v in params.items() if k not in {"object_names", "max_overlap_pairs"}}
        forwarded["object_name"] = name
        rows.append(_audit_single_uv(forwarded, per_object[name]).data)
    if "object_name" in params:
        return ToolResult(data={**rows[0], **overlap})
    checks = {key: (None if all(row["checks"][key] is None for row in rows) else all(row["checks"][key] for row in rows)) for key in rows[0]["checks"]}
    if "max_overlap_pct" in params:
        checks["max_overlap_pct"] = overlap["overlap_pct"] is not None and overlap["overlap_pct"] <= params["max_overlap_pct"]
    return ToolResult(data={"object_names": names, "objects": rows, **overlap,
                           "texture_size": params["texture_size"], "raster_size": size,
                           "checks": checks, "pass": all(v for v in checks.values() if v is not None),
                           "state_token": current_state_token(bpy)})


def _apply_seams(params: dict[str, Any], bpy: Any, obj: Any, indices: set[int], action: str, extra: dict[str, Any]) -> dict[str, Any]:
    _validate_expected(params)
    changed_indices = sorted(i for i in indices if not obj.data.edges[i].use_seam)
    transaction, apply = prepare_destructive_action(
        action=action, affected_datablocks=[{"type": "Mesh", "name": obj.data.name}],
        journal=[{"action": "mark_seams", "edge_count": len(changed_indices)}],
        dry_run=params.get("dry_run"), require_confirm=False,
        state_token=current_state_token(bpy), extra={"object": obj.name, "seam_edge_indices": sorted(indices), "new_seam_count": len(changed_indices), **extra})
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    try:
        for index in changed_indices:
            obj.data.edges[index].use_seam = True
        obj.data.update()
    except Exception as exc:
        errors = []
        for index in changed_indices:
            try:
                obj.data.edges[index].use_seam = False
            except Exception as rollback:
                errors.append(str(rollback))
        transaction.update(changed=bool(errors), warnings=errors)
        if errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"{action} failed: {exc}", transaction) from exc
    transaction["changed"] = bool(changed_indices)
    if changed_indices:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _seam_target(params: dict[str, Any]) -> tuple[Any, Any]:
    bpy = get_bpy()
    obj = _mesh_object(bpy, _name(params.get("object_name"), "object_name"), mutate=True)
    if getattr(obj.data, "library", None) or getattr(obj.data, "is_editable", True) is False:
        raise InvalidStateError("Seam helpers require editable local mesh data")
    if len(obj.data.edges) > 250000 or len(obj.data.polygons) > 100000:
        raise InvalidStateError("Seam helpers are limited to 250000 edges and 100000 faces")
    return bpy, obj


def seams_from_regions(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "attribute_name", "include_boundary", "dry_run", "expected_state"})
    name = _name(params.get("attribute_name", "part"), "attribute_name")
    boundary = _strict_bool(params.get("include_boundary"), "include_boundary", True)
    bpy, obj = _seam_target(params)
    attr = obj.data.attributes.get(name)
    if attr is None or attr.domain != "FACE" or attr.data_type != "INT":
        raise InvalidStateError("Region seam attribute must exist with domain FACE and type INT")
    uses = [[] for _ in obj.data.edges]
    for face in obj.data.polygons:
        for loop in face.loop_indices:
            uses[obj.data.loops[loop].edge_index].append(attr.data[face.index].value)
    if any(len(values) > 2 for values in uses):
        raise InvalidStateError("Region seams refuse non-manifold edges with more than two incident faces")
    indices = {i for i, values in enumerate(uses) if len(set(values)) > 1 or (boundary and len(values) == 1)}
    return _apply_seams(params, bpy, obj, indices, "uv.seams_from_regions", {"attribute_name": name})


def _turn_cut_rows(points: list[list[float]], threshold: float, maximum: int, closed: bool) -> list[int]:
    count = len(points)
    directions = []
    for i in range(count if closed else count-1):
        delta = [b-a for a, b in zip(points[i], points[(i+1) % count])]
        length = math.sqrt(sum(v*v for v in delta))
        if not math.isfinite(length) or length <= 1e-12:
            raise InvalidStateError("Strip rows must have finite, distinct centres")
        directions.append([v/length for v in delta])
    cuts = [0]
    accumulated = 0.0
    last = 0
    for i in range(1, count):
        if closed or i < count-1:
            dot = sum(a*b for a, b in zip(directions[i-1], directions[i % len(directions)]))
            accumulated += math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        if accumulated >= threshold or i-last >= maximum:
            cuts.append(i)
            accumulated = 0.0
            last = i
    if not closed and cuts[-1] != count-1:
        cuts.append(count-1)
    return cuts


def cut_strips_by_turn(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "strips", "turn_degrees", "max_segments", "dry_run", "expected_state"})
    threshold = _number(params.get("turn_degrees", 15), "turn_degrees", 0.001, 180)
    maximum = _integer(params.get("max_segments", 10), "max_segments", 1, 10000)
    strips = params.get("strips")
    if not isinstance(strips, list) or not 1 <= len(strips) <= 128:
        raise InvalidParamsError("strips must contain 1 to 128 ordered edge-row strips")
    bpy, obj = _seam_target(params)
    mesh = obj.data
    edge_faces = [set() for _ in mesh.edges]
    for face in mesh.polygons:
        for loop in face.loop_indices:
            edge_faces[mesh.loops[loop].edge_index].add(face.index)
    indices: set[int] = set()
    reports = []
    total_rows = 0
    for strip in strips:
        if not isinstance(strip, dict):
            raise InvalidParamsError("Each strip must be an object")
        _unknown(strip, {"edge_rows", "closed"}, "strip fields")
        closed = _strict_bool(strip.get("closed"), "closed", False)
        rows = strip.get("edge_rows")
        if not isinstance(rows, list) or not (3 if closed else 2) <= len(rows) <= 10000:
            raise InvalidParamsError("edge_rows requires 2 (open) or 3 (closed) to 10000 rows")
        total_rows += len(rows)
        if total_rows > 10000:
            raise InvalidParamsError("At most 10000 strip rows are supported per call")
        points, row_faces, seen_vertices = [], [], set()
        for row in rows:
            if not isinstance(row, list) or not 1 <= len(row) <= 1000:
                raise InvalidParamsError("Each edge row requires 1 to 1000 indices")
            ids = [_integer(i, "edge index", 0, len(mesh.edges)-1) for i in row]
            if len(set(ids)) != len(ids):
                raise InvalidParamsError("Edge rows cannot repeat indices")
            graph: dict[int, set[int]] = {}
            for index in ids:
                if not 1 <= len(edge_faces[index]) <= 2:
                    raise InvalidStateError("Strip rows require manifold surface edges")
                a, b = mesh.edges[index].vertices
                graph.setdefault(a, set()).add(b)
                graph.setdefault(b, set()).add(a)
            if sum(len(v) == 1 for v in graph.values()) != 2 or any(len(v) > 2 for v in graph.values()):
                raise InvalidStateError("Each strip row must be an open unbranched edge chain")
            visited, stack = set(), [next(iter(graph))]
            while stack:
                vertex = stack.pop()
                if vertex not in visited:
                    visited.add(vertex)
                    stack.extend(graph[vertex]-visited)
            if visited != set(graph) or visited & seen_vertices:
                raise InvalidStateError("Strip rows must be connected and vertex-disjoint")
            seen_vertices.update(visited)
            coordinates = [obj.matrix_world @ mesh.vertices[i].co for i in sorted(visited)]
            points.append([sum(v[axis] for v in coordinates)/len(coordinates) for axis in range(3)])
            row_faces.append(set().union(*(edge_faces[i] for i in ids)))
        for i in range(len(rows) if closed else len(rows)-1):
            common = row_faces[i] & row_faces[(i+1) % len(rows)]
            if (len(common) != len(rows[i]) or len(rows[i]) != len(rows[(i+1) % len(rows)])
                    or any(len(mesh.polygons[f].vertices) != 4 for f in common)
                    or any(len(edge_faces[e] & common) != 1 for e in rows[i] + rows[(i+1) % len(rows)])):
                raise InvalidStateError("Consecutive strip rows must bound a complete matching quad band")
        cuts = _turn_cut_rows(points, threshold, maximum, closed)
        for index in cuts:
            indices.update(rows[index])
        reports.append({"row_count": len(rows), "cut_rows": cuts, "closed": closed})
    return _apply_seams(params, bpy, obj, indices, "uv.cut_strips_by_turn", {"strips": reports, "turn_degrees": threshold, "max_segments": maximum, "turn_space": "world_row_centres"})
