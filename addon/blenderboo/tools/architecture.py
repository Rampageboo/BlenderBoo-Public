"""Bounded high-level architectural wall generation."""

from __future__ import annotations

import math
from typing import Any

from ..errors import (
    InvalidParamsError,
    InvalidStateError,
    MutationApplyError,
    NameConflictError,
    NotFoundError,
)
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


MAX_WALL_LINES = 256
MAX_OPENINGS_PER_LINE = 32
MAX_TOTAL_OPENINGS = 512
DEFAULT_ELEMENT_BUDGET = 250_000
MAX_ELEMENT_BUDGET = 5_000_000
_EPSILON = 1e-8


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(names)}")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 63:
        raise InvalidParamsError(f"{label} must be a non-empty string of at most 63 characters")
    return value


def _number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParamsError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise InvalidParamsError(f"{label} must be a finite number")
    if minimum is not None and (
        result <= minimum if exclusive_minimum else result < minimum
    ):
        comparison = "greater than" if exclusive_minimum else "at least"
        raise InvalidParamsError(f"{label} must be {comparison} {minimum}")
    if maximum is not None and result > maximum:
        raise InvalidParamsError(f"{label} must be at most {maximum}")
    return result


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidParamsError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _strict_bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _point2(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise InvalidParamsError(f"{label} must contain exactly two numbers")
    return (
        _number(value[0], f"{label}[0]", minimum=-1_000_000, maximum=1_000_000),
        _number(value[1], f"{label}[1]", minimum=-1_000_000, maximum=1_000_000),
    )


def _opening(value: Any, label: str, wall_length: float, wall_height: float) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError(f"{label} must be an object")
    _unknown(value, {"id", "kind", "offset", "width", "bottom", "height"})
    opening_id = value.get("id")
    if opening_id is not None:
        opening_id = _name(opening_id, f"{label}.id")
    kind = value.get("kind", "OPENING")
    if not isinstance(kind, str) or kind not in {"DOOR", "WINDOW", "OPENING"}:
        raise InvalidParamsError(f"{label}.kind must be DOOR, WINDOW, or OPENING")
    offset = _number(value.get("offset"), f"{label}.offset", minimum=0)
    width = _number(
        value.get("width"), f"{label}.width", minimum=0, exclusive_minimum=True
    )
    bottom = _number(value.get("bottom", 0), f"{label}.bottom", minimum=0)
    height = _number(
        value.get("height"), f"{label}.height", minimum=0, exclusive_minimum=True
    )
    if offset + width > wall_length + _EPSILON:
        raise InvalidParamsError(f"{label} extends beyond the wall length")
    if bottom + height > wall_height + _EPSILON:
        raise InvalidParamsError(f"{label} extends above the wall height")
    if kind == "DOOR" and bottom > _EPSILON:
        raise InvalidParamsError(f"{label}.bottom must be 0 for a DOOR")
    return {
        "id": opening_id,
        "kind": kind,
        "offset": offset,
        "width": width,
        "bottom": bottom,
        "height": height,
    }


def _wall_line(value: Any, index: int, wall_height: float) -> dict[str, Any]:
    label = f"wall_lines[{index}]"
    if not isinstance(value, dict):
        raise InvalidParamsError(f"{label} must be an object")
    _unknown(value, {"start", "end", "openings"})
    start = _point2(value.get("start"), f"{label}.start")
    end = _point2(value.get("end"), f"{label}.end")
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    if length <= _EPSILON:
        raise InvalidParamsError(f"{label} start and end must be distinct")
    raw_openings = value.get("openings", [])
    if not isinstance(raw_openings, list) or len(raw_openings) > MAX_OPENINGS_PER_LINE:
        raise InvalidParamsError(
            f"{label}.openings must be a list of at most {MAX_OPENINGS_PER_LINE} items"
        )
    openings = [
        _opening(item, f"{label}.openings[{opening_index}]", length, wall_height)
        for opening_index, item in enumerate(raw_openings)
    ]
    openings.sort(key=lambda item: (item["offset"], item["bottom"], item["id"] or ""))
    opening_ids = [item["id"] for item in openings if item["id"] is not None]
    if len(opening_ids) != len(set(opening_ids)):
        raise InvalidParamsError(f"{label}.openings must not contain duplicate ids")
    for left_index, left in enumerate(openings):
        for right in openings[left_index + 1:]:
            overlaps_along_wall = (
                right["offset"] < left["offset"] + left["width"] - _EPSILON
                and left["offset"] < right["offset"] + right["width"] - _EPSILON
            )
            overlaps_vertically = (
                right["bottom"] < left["bottom"] + left["height"] - _EPSILON
                and left["bottom"] < right["bottom"] + right["height"] - _EPSILON
            )
            if overlaps_along_wall and overlaps_vertically:
                raise InvalidParamsError(f"{label}.openings must not overlap in wall-local X/Z space")
    return {"start": start, "end": end, "length": length, "openings": openings}


def _mesh_counts(vertices: list[Any], faces: list[tuple[int, ...]]) -> dict[str, int]:
    edges: set[tuple[int, int]] = set()
    for face in faces:
        for left, right in zip(face, face[1:] + face[:1]):
            edges.add((left, right) if left < right else (right, left))
    return {
        "vertices": len(vertices),
        "edges": len(edges),
        "faces": len(faces),
        "loops": sum(len(face) for face in faces),
    }


def _element_total(counts: dict[str, int]) -> int:
    return sum(counts.values())


def _build_wall_mesh(
    line: dict[str, Any],
    *,
    thickness: float,
    height: float,
    base_z: float,
) -> dict[str, Any]:
    """Build one watertight wall segment with grid-aligned rectangular openings."""
    length = line["length"]
    openings = line["openings"]
    x_cuts = sorted({0.0, length, *(
        coordinate
        for opening in openings
        for coordinate in (opening["offset"], opening["offset"] + opening["width"])
    )})
    z_cuts = sorted({0.0, height, *(
        coordinate
        for opening in openings
        for coordinate in (opening["bottom"], opening["bottom"] + opening["height"])
    )})
    occupied: set[tuple[int, int]] = set()
    for x_index in range(len(x_cuts) - 1):
        x_mid = (x_cuts[x_index] + x_cuts[x_index + 1]) / 2
        for z_index in range(len(z_cuts) - 1):
            z_mid = (z_cuts[z_index] + z_cuts[z_index + 1]) / 2
            inside_opening = any(
                opening["offset"] < x_mid < opening["offset"] + opening["width"]
                and opening["bottom"] < z_mid < opening["bottom"] + opening["height"]
                for opening in openings
            )
            if not inside_opening:
                occupied.add((x_index, z_index))
    if not occupied:
        raise InvalidParamsError("openings remove the entire wall segment")

    start_x, start_y = line["start"]
    direction_x = (line["end"][0] - start_x) / length
    direction_y = (line["end"][1] - start_y) / length
    normal_x, normal_y = -direction_y, direction_x
    half = thickness / 2
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []
    vertex_indices: dict[tuple[float, float, float], int] = {}

    def vertex(local_x: float, side: float, local_z: float) -> int:
        key = (local_x, side, local_z)
        existing = vertex_indices.get(key)
        if existing is not None:
            return existing
        index = len(vertices)
        vertex_indices[key] = index
        vertices.append((
            start_x + direction_x * local_x + normal_x * side,
            start_y + direction_y * local_x + normal_y * side,
            base_z + local_z,
        ))
        return index

    def face(points: tuple[tuple[float, float, float], ...]) -> None:
        # The templates below are written inward-first for readability; reverse
        # each loop so Blender receives consistently outward-facing normals.
        faces.append(tuple(vertex(*point) for point in reversed(points)))

    for x_index, z_index in sorted(occupied):
        x0, x1 = x_cuts[x_index], x_cuts[x_index + 1]
        z0, z1 = z_cuts[z_index], z_cuts[z_index + 1]
        face(((x0, -half, z0), (x0, -half, z1), (x1, -half, z1), (x1, -half, z0)))
        face(((x0, half, z0), (x1, half, z0), (x1, half, z1), (x0, half, z1)))
        if (x_index - 1, z_index) not in occupied:
            face(((x0, -half, z0), (x0, half, z0), (x0, half, z1), (x0, -half, z1)))
        if (x_index + 1, z_index) not in occupied:
            face(((x1, -half, z0), (x1, -half, z1), (x1, half, z1), (x1, half, z0)))
        if (x_index, z_index - 1) not in occupied:
            face(((x0, -half, z0), (x1, -half, z0), (x1, half, z0), (x0, half, z0)))
        if (x_index, z_index + 1) not in occupied:
            face(((x0, -half, z1), (x0, half, z1), (x1, half, z1), (x1, -half, z1)))

    counts = _mesh_counts(vertices, faces)
    return {
        "vertices": vertices,
        "faces": faces,
        "counts": counts,
        "grid_cells": (len(x_cuts) - 1) * (len(z_cuts) - 1),
    }


def _plan_walls(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {
        "name", "wall_lines", "height", "thickness", "base_z", "collection",
        "material", "max_elements", "dry_run", "expected_state",
    })
    name = _name(params.get("name", "Walls"), "name")
    height = _number(
        params.get("height"), "height", minimum=0, maximum=10_000, exclusive_minimum=True
    )
    thickness = _number(
        params.get("thickness"), "thickness", minimum=0, maximum=1_000, exclusive_minimum=True
    )
    base_z = _number(params.get("base_z", 0), "base_z", minimum=-1_000_000, maximum=1_000_000)
    raw_lines = params.get("wall_lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= MAX_WALL_LINES:
        raise InvalidParamsError(
            f"wall_lines must be a non-empty list of at most {MAX_WALL_LINES} items"
        )
    lines = [_wall_line(item, index, height) for index, item in enumerate(raw_lines)]
    total_openings = sum(len(line["openings"]) for line in lines)
    if total_openings > MAX_TOTAL_OPENINGS:
        raise InvalidParamsError(f"wall_lines contain more than {MAX_TOTAL_OPENINGS} openings")
    max_elements = _integer(
        params.get("max_elements", DEFAULT_ELEMENT_BUDGET),
        "max_elements",
        1,
        MAX_ELEMENT_BUDGET,
    )
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    names = [name] if len(lines) == 1 else [f"{name}_{index + 1:03d}" for index in range(len(lines))]
    if any(len(item) > 63 for item in names):
        raise InvalidParamsError("generated wall object names must be at most 63 characters")
    plans = []
    total_counts = {key: 0 for key in ("vertices", "edges", "faces", "loops")}
    for index, (line, object_name) in enumerate(zip(lines, names)):
        geometry = _build_wall_mesh(line, thickness=thickness, height=height, base_z=base_z)
        for key in total_counts:
            total_counts[key] += geometry["counts"][key]
        if _element_total(total_counts) > max_elements:
            raise InvalidStateError(
                f"Wall plan exceeds max_elements {max_elements} at wall_lines[{index}]"
            )
        plans.append({"name": object_name, "line": line, "geometry": geometry})
    return {
        "name": name,
        "height": height,
        "thickness": thickness,
        "base_z": base_z,
        "collection": params.get("collection"),
        "material": params.get("material"),
        "max_elements": max_elements,
        "dry_run": dry_run,
        "plans": plans,
        "total_openings": total_openings,
        "total_counts": total_counts,
    }


def _find_collection(root: Any, name: str) -> Any | None:
    if root.name == name:
        return root
    for child in root.children:
        found = _find_collection(child, name)
        if found is not None:
            return found
    return None


def _remove_created(bpy: Any, created: list[tuple[Any, Any]]) -> list[str]:
    errors = []
    for obj, mesh in reversed(created):
        try:
            if bpy.data.objects.get(obj.name) is obj:
                bpy.data.objects.remove(obj, do_unlink=True)
        except Exception as exc:
            errors.append(f"remove object {obj.name}: {exc}")
        try:
            if getattr(mesh, "users", 1) == 0 and bpy.data.meshes.get(mesh.name) is mesh:
                bpy.data.meshes.remove(mesh)
        except Exception as exc:
            errors.append(f"remove mesh {mesh.name}: {exc}")
    return errors


def build_walls(params: dict[str, Any]) -> dict[str, Any]:
    plan = _plan_walls(params)
    bpy = get_bpy()
    if not plan["dry_run"]:
        require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("architecture.wall_build requires Blender Object mode")
    collection_name = plan["collection"]
    if collection_name is not None:
        collection_name = _name(collection_name, "collection")
        collection = _find_collection(bpy.context.scene.collection, collection_name)
        if collection is None:
            raise NotFoundError(f"Collection not found in current scene: {collection_name}")
    else:
        collection = bpy.context.scene.collection
    material_name = plan["material"]
    material = None
    if material_name is not None:
        material_name = _name(material_name, "material")
        material = bpy.data.materials.get(material_name)
        if material is None:
            raise NotFoundError(f"Material not found: {material_name}")
    for item in plan["plans"]:
        mesh_name = f"{item['name']}_Mesh"
        if bpy.data.objects.get(item["name"]) is not None:
            raise NameConflictError(
                f"Object already exists: {item['name']}",
                {"conflict": {"type": "Object", "name": item["name"]}},
            )
        if bpy.data.meshes.get(mesh_name) is not None:
            raise NameConflictError(
                f"Mesh already exists: {mesh_name}",
                {"conflict": {"type": "Mesh", "name": mesh_name}},
            )

    summaries = [
        {
            "name": item["name"],
            "start": list(item["line"]["start"]),
            "end": list(item["line"]["end"]),
            "length": item["line"]["length"],
            "opening_count": len(item["line"]["openings"]),
            "openings": item["line"]["openings"],
            "counts": item["geometry"]["counts"],
        }
        for item in plan["plans"]
    ]
    affected = [
        {"type": kind, "name": name}
        for item in plan["plans"]
        for kind, name in (("Object", item["name"]), ("Mesh", f"{item['name']}_Mesh"))
    ]
    journal = [
        {"op": "create_wall", "target": item["name"], "before": None, "after": summary, "status": "planned"}
        for item, summary in zip(plan["plans"], summaries)
    ]
    transaction, apply = prepare_destructive_action(
        action="architecture.wall_build",
        affected_datablocks=affected,
        journal=journal,
        dry_run=plan["dry_run"],
        confirm=None,
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={
            "objects": [item["name"] for item in plan["plans"]],
            "walls": summaries,
            "wall_count": len(plan["plans"]),
            "opening_count": plan["total_openings"],
            "height": plan["height"],
            "thickness": plan["thickness"],
            "base_z": plan["base_z"],
            "collection": collection.name,
            "material": material.name if material is not None else None,
            "counts": plan["total_counts"],
            "max_elements": plan["max_elements"],
        },
    )
    if not apply:
        return transaction

    created: list[tuple[Any, Any]] = []
    try:
        for index, item in enumerate(plan["plans"]):
            mesh = bpy.data.meshes.new(f"{item['name']}_Mesh")
            obj = bpy.data.objects.new(item["name"], mesh)
            created.append((obj, mesh))
            mesh.from_pydata(item["geometry"]["vertices"], [], item["geometry"]["faces"])
            mesh.update(calc_edges=True)
            if material is not None:
                mesh.materials.append(material)
            obj["cbt_architecture_kind"] = "WALL"
            obj["cbt_wall_height"] = plan["height"]
            obj["cbt_wall_thickness"] = plan["thickness"]
            collection.objects.link(obj)
            transaction["journal"][index]["status"] = "applied"
        for entry in transaction["journal"]:
            entry["status"] = "applied"
        transaction["changed"] = True
        bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        return transaction
    except Exception as exc:
        cleanup_errors = _remove_created(bpy, created)
        recovery_status = "cleanup_incomplete" if cleanup_errors else "rolled_back"
        for entry in transaction["journal"]:
            if entry["status"] == "applied":
                entry["status"] = recovery_status
        transaction["changed"] = bool(cleanup_errors)
        transaction["warnings"].extend(cleanup_errors)
        if cleanup_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(
            f"architecture.wall_build failed: {exc}", transaction
        ) from exc
