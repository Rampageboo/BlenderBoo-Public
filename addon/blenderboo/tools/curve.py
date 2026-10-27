"""Bounded legacy Curve inspection and guarded spline lifecycle tools."""

from __future__ import annotations

import math
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy, vector_values
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


MAX_PAGE = 2000
MAX_CONTROL_POINTS = 100_000
SPLINE_TYPES = frozenset({"POLY", "BEZIER", "NURBS"})
HANDLE_TYPES = frozenset({"FREE", "VECTOR", "ALIGNED", "AUTO", "AUTO_CLAMPED"})
DIMENSIONS = frozenset({"2D", "3D"})
FILL_MODES = frozenset({"FULL", "HALF", "BACK", "FRONT"})


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


def _vector(value: Any, label: str, length: int = 3) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise InvalidParamsError(f"{label} must be a {length}-number array")
    return [_number(item, label, -1_000_000.0, 1_000_000.0) for item in value]


def _enum(value: Any, label: str, allowed: frozenset[str]) -> str:
    result = _name(value, label).upper()
    if result not in allowed:
        raise InvalidParamsError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return result


def _page(params: dict[str, Any], tool: str) -> tuple[int, int]:
    limit = _integer(params.get("limit", 200), "limit", 1, MAX_PAGE)
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError(f"cursor must be a decimal string returned by {tool}")


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _prepare_mutation(params: dict[str, Any]) -> Any:
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("Curve mutation requires Blender Object mode")
    return bpy


def _curve_object(bpy: Any, object_name: str, *, single_user: bool = False) -> Any:
    obj = bpy.context.scene.objects.get(object_name)
    if obj is None or obj.type != "CURVE" or obj.data is None:
        raise NotFoundError(f"Curve object not found in current scene: {object_name}")
    if single_user and obj.data.users > 1:
        raise InvalidStateError("Curve editing refuses shared Curve datablocks; make the data single-user first")
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
        collection = _find_collection(root, name)
        if collection is None:
            raise NotFoundError(f"Collection not found in current scene: {name}")
        return collection
    active = getattr(bpy.context, "collection", None)
    return active if active is not None and _find_collection(root, active.name) is active else root


def _unique_curve_name(bpy: Any, requested: str) -> str:
    if bpy.data.objects.get(requested) is None and bpy.data.curves.get(requested) is None:
        return requested
    index = 1
    while (
        bpy.data.objects.get(f"{requested}.{index:03d}") is not None
        or bpy.data.curves.get(f"{requested}.{index:03d}") is not None
    ):
        index += 1
    return f"{requested}.{index:03d}"


def _point_count(spline: Any) -> int:
    return len(spline.bezier_points) if spline.type == "BEZIER" else len(spline.points)


def _point_coordinate(point: Any, spline_type: str) -> list[float]:
    values = vector_values(point.co)
    return values[:3]


def _distance(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((left[index] - right[index]) ** 2 for index in range(3)))


def _alignment_violation(point: Any) -> bool:
    if point.handle_left_type != "ALIGNED" and point.handle_right_type != "ALIGNED":
        return False
    co = vector_values(point.co)
    left = [float(point.handle_left[index]) - co[index] for index in range(3)]
    right = [float(point.handle_right[index]) - co[index] for index in range(3)]
    left_length = math.sqrt(sum(value * value for value in left))
    right_length = math.sqrt(sum(value * value for value in right))
    if left_length <= 1e-12 or right_length <= 1e-12:
        return True
    cosine = sum(left[index] * right[index] for index in range(3)) / (left_length * right_length)
    return cosine > -0.9999


def _spline_audit(spline: Any) -> dict[str, Any]:
    points = list(spline.bezier_points if spline.type == "BEZIER" else spline.points)
    coordinates = [_point_coordinate(point, spline.type) for point in points]
    segment_lengths = [_distance(coordinates[index - 1], coordinates[index]) for index in range(1, len(coordinates))]
    endpoint_gap = _distance(coordinates[-1], coordinates[0]) if len(coordinates) > 1 else 0.0
    if spline.use_cyclic_u and len(coordinates) > 1:
        segment_lengths.append(endpoint_gap)
    return {
        "point_count": len(points),
        "cyclic": bool(spline.use_cyclic_u),
        "endpoint_gap": endpoint_gap,
        "zero_length_segments": sum(length <= 1e-9 for length in segment_lengths),
        "minimum_segment_length": min(segment_lengths) if segment_lengths else None,
        "maximum_segment_length": max(segment_lengths) if segment_lengths else None,
        "aligned_handle_violations": (
            sum(_alignment_violation(point) for point in points) if spline.type == "BEZIER" else 0
        ),
    }


def _point_detail(point: Any, spline_type: str, point_index: int) -> dict[str, Any]:
    detail = {
        "point_index": point_index,
        "co": _point_coordinate(point, spline_type),
        "radius": float(point.radius),
        "tilt": float(point.tilt),
    }
    if spline_type == "BEZIER":
        detail.update(
            handle_left=vector_values(point.handle_left),
            handle_right=vector_values(point.handle_right),
            handle_left_type=point.handle_left_type,
            handle_right_type=point.handle_right_type,
        )
    else:
        detail["weight"] = float(point.weight)
    return detail


def _spline_summary(spline: Any, index: int) -> dict[str, Any]:
    summary = {
        "spline_index": index,
        "type": spline.type,
        "resolution_u": int(spline.resolution_u),
        **_spline_audit(spline),
    }
    if spline.type == "NURBS":
        summary.update(
            order_u=int(spline.order_u),
            use_endpoint_u=bool(spline.use_endpoint_u),
            use_bezier_u=bool(spline.use_bezier_u),
        )
    return summary


def _curve_detail(obj: Any) -> dict[str, Any]:
    data = obj.data
    splines = [_spline_summary(spline, index) for index, spline in enumerate(data.splines)]
    return {
        "object": obj.name,
        "data": data.name,
        "dimensions": data.dimensions,
        "resolution_u": int(data.resolution_u),
        "render_resolution_u": int(data.render_resolution_u),
        "fill_mode": data.fill_mode,
        "bevel_depth": float(data.bevel_depth),
        "bevel_resolution": int(data.bevel_resolution),
        "twist_smooth": int(data.twist_smooth),
        "spline_count": len(splines),
        "control_point_count": sum(item["point_count"] for item in splines),
        "splines": splines,
        "collections": sorted(collection.name for collection in obj.users_collection),
        "transform": {
            "location": vector_values(obj.location),
            "rotation_euler": vector_values(obj.rotation_euler),
            "scale": vector_values(obj.scale),
        },
    }


def inspect_curve(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"name", "object_name", "include_points", "max_points", "limit", "cursor"})
    substring = _optional_name(params.get("name"), "name")
    object_name = _optional_name(params.get("object_name"), "object_name")
    include_points = _strict_bool(params.get("include_points"), "include_points", False)
    max_points = _integer(params.get("max_points", 10000), "max_points", 1, MAX_CONTROL_POINTS)
    limit, offset = _page(params, "curve.inspect")
    bpy = get_bpy()
    curves = sorted(
        (obj for obj in bpy.context.scene.objects if obj.type == "CURVE" and (substring is None or substring in obj.name)),
        key=lambda item: item.name,
    )
    page = curves[offset : offset + limit]
    next_offset = offset + len(page)
    result: dict[str, Any] = {
        "count": len(curves),
        "curves": [
            {
                "object": obj.name,
                "data": obj.data.name,
                "dimensions": obj.data.dimensions,
                "spline_count": len(obj.data.splines),
                "control_point_count": sum(_point_count(spline) for spline in obj.data.splines),
            }
            for obj in page
        ],
        "next_cursor": str(next_offset) if next_offset < len(curves) else None,
        "truncated": next_offset < len(curves),
        "state_token": current_state_token(bpy),
    }
    if object_name is not None:
        obj = _curve_object(bpy, object_name)
        total = sum(_point_count(spline) for spline in obj.data.splines)
        if total > max_points:
            raise InvalidStateError(
                f"Curve detail requires auditing {total} control points, above max_points {max_points}"
            )
        detail = _curve_detail(obj)
        if include_points:
            flattened = [
                {"spline_index": spline_index, **_point_detail(point, spline.type, point_index)}
                for spline_index, spline in enumerate(obj.data.splines)
                for point_index, point in enumerate(spline.bezier_points if spline.type == "BEZIER" else spline.points)
            ]
            point_page = flattened[offset : offset + limit]
            point_next = offset + len(point_page)
            detail.update(
                points=point_page,
                points_next_cursor=str(point_next) if point_next < len(flattened) else None,
                points_truncated=point_next < len(flattened),
            )
        result["curve_detail"] = detail
    return ToolResult(data=result)


def _normalize_point(value: Any, spline_type: str, index: int) -> dict[str, Any]:
    label = f"points[{index}]"
    if spline_type == "BEZIER":
        if not isinstance(value, dict):
            raise InvalidParamsError(f"{label} must be an object for BEZIER splines")
        _unknown(
            value,
            {"co", "handle_left", "handle_right", "handle_left_type", "handle_right_type", "radius", "tilt"},
            label,
        )
        co = _vector(value.get("co"), f"{label}.co")
        return {
            "co": co,
            "handle_left": _vector(value.get("handle_left", co), f"{label}.handle_left"),
            "handle_right": _vector(value.get("handle_right", co), f"{label}.handle_right"),
            "handle_left_type": _enum(value.get("handle_left_type", "FREE"), f"{label}.handle_left_type", HANDLE_TYPES),
            "handle_right_type": _enum(value.get("handle_right_type", "FREE"), f"{label}.handle_right_type", HANDLE_TYPES),
            "radius": _number(value.get("radius", 1.0), f"{label}.radius", 0.0, 1_000_000.0),
            "tilt": _number(value.get("tilt", 0.0), f"{label}.tilt", -math.tau * 1000, math.tau * 1000),
        }
    coordinate = value.get("co") if isinstance(value, dict) else value
    if isinstance(value, dict):
        _unknown(value, {"co", "weight", "radius", "tilt"}, label)
    co = _vector(coordinate, f"{label}.co")
    weight = _number(value.get("weight", 1.0), f"{label}.weight", 0.000001, 1_000_000.0) if isinstance(value, dict) else 1.0
    radius = _number(value.get("radius", 1.0), f"{label}.radius", 0.0, 1_000_000.0) if isinstance(value, dict) else 1.0
    tilt = _number(value.get("tilt", 0.0), f"{label}.tilt", -math.tau * 1000, math.tau * 1000) if isinstance(value, dict) else 0.0
    return {"co": co, "weight": weight, "radius": radius, "tilt": tilt}


def _normalize_spline(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParamsError("spline must be an object")
    _unknown(value, {"type", "points", "cyclic", "resolution_u", "order_u", "use_endpoint_u", "use_bezier_u"}, "spline fields")
    spline_type = _enum(value.get("type"), "spline.type", SPLINE_TYPES)
    points = value.get("points")
    if not isinstance(points, list) or not 2 <= len(points) <= MAX_CONTROL_POINTS:
        raise InvalidParamsError(f"spline.points must contain 2 to {MAX_CONTROL_POINTS} points")
    normalized = {
        "type": spline_type,
        "points": [_normalize_point(point, spline_type, index) for index, point in enumerate(points)],
        "cyclic": _strict_bool(value.get("cyclic"), "spline.cyclic", False),
        "resolution_u": _integer(value.get("resolution_u", 12), "spline.resolution_u", 1, 1024),
    }
    nurbs_fields = {"order_u", "use_endpoint_u", "use_bezier_u"}
    if spline_type == "NURBS":
        normalized.update(
            order_u=_integer(value.get("order_u", min(4, len(points))), "spline.order_u", 2, min(6, len(points))),
            use_endpoint_u=_strict_bool(value.get("use_endpoint_u"), "spline.use_endpoint_u", True),
            use_bezier_u=_strict_bool(value.get("use_bezier_u"), "spline.use_bezier_u", False),
        )
    elif any(field in value for field in nurbs_fields):
        raise InvalidParamsError("NURBS settings are valid only for NURBS splines")
    return normalized


def _write_spline(spline: Any, spec: dict[str, Any], *, create_points: bool) -> None:
    points = spline.bezier_points if spec["type"] == "BEZIER" else spline.points
    if create_points:
        points.add(len(spec["points"]) - 1)
    if len(points) != len(spec["points"]):
        raise InvalidStateError("spline.patch cannot change the control-point count")
    spline.use_cyclic_u = spec["cyclic"]
    spline.resolution_u = spec["resolution_u"]
    if spec["type"] == "NURBS":
        spline.order_u = spec["order_u"]
        spline.use_endpoint_u = spec["use_endpoint_u"]
        spline.use_bezier_u = spec["use_bezier_u"]
    for point, point_spec in zip(points, spec["points"]):
        point.co = (
            point_spec["co"]
            if spec["type"] == "BEZIER"
            else [*point_spec["co"], point_spec["weight"]]
        )
        point.radius = point_spec["radius"]
        point.tilt = point_spec["tilt"]
        if spec["type"] == "BEZIER":
            point.handle_left = point_spec["handle_left"]
            point.handle_right = point_spec["handle_right"]
            point.handle_left_type = point_spec["handle_left_type"]
            point.handle_right_type = point_spec["handle_right_type"]


def _serialize_spline(spline: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "type": spline.type,
        "cyclic": bool(spline.use_cyclic_u),
        "resolution_u": int(spline.resolution_u),
        "points": [],
    }
    if spline.type == "NURBS":
        spec.update(
            order_u=int(spline.order_u),
            use_endpoint_u=bool(spline.use_endpoint_u),
            use_bezier_u=bool(spline.use_bezier_u),
        )
    for point in spline.bezier_points if spline.type == "BEZIER" else spline.points:
        item = {
            "co": _point_coordinate(point, spline.type),
            "radius": float(point.radius),
            "tilt": float(point.tilt),
        }
        if spline.type == "BEZIER":
            item.update(
                handle_left=vector_values(point.handle_left),
                handle_right=vector_values(point.handle_right),
                handle_left_type=point.handle_left_type,
                handle_right_type=point.handle_right_type,
            )
        else:
            item["weight"] = float(point.weight)
        spec["points"].append(item)
    return spec


def _curve_settings(params: dict[str, Any], *, defaults: bool) -> dict[str, Any]:
    values: dict[str, Any] = {}
    source = {
        "dimensions": "3D", "resolution_u": 12, "render_resolution_u": 0,
        "fill_mode": "FULL", "bevel_depth": 0.0, "bevel_resolution": 4, "twist_smooth": 0,
    }
    for name, default in source.items():
        if name not in params and not defaults:
            continue
        raw = params.get(name, default)
        if name == "dimensions":
            values[name] = _enum(raw, name, DIMENSIONS)
        elif name == "fill_mode":
            values[name] = _enum(raw, name, FILL_MODES)
        elif name in {"resolution_u", "render_resolution_u"}:
            values[name] = _integer(raw, name, 0 if name == "render_resolution_u" else 1, 1024)
        elif name == "bevel_resolution":
            values[name] = _integer(raw, name, 0, 32)
        elif name == "twist_smooth":
            values[name] = _integer(raw, name, 0, 100)
        else:
            values[name] = _number(raw, name, 0.0, 1_000_000.0)
    return values


def create_curve(params: dict[str, Any]) -> dict[str, Any]:
    setting_fields = {"dimensions", "resolution_u", "render_resolution_u", "fill_mode", "bevel_depth", "bevel_resolution", "twist_smooth"}
    _unknown(params, {"name", "collection", "location", "rotation", "scale", "spline", "expected_state", *setting_fields})
    requested_name = _optional_name(params.get("name"), "name") or "Curve"
    collection_name = _optional_name(params.get("collection"), "collection")
    location = _vector(params.get("location", [0, 0, 0]), "location")
    rotation = _vector(params.get("rotation", [0, 0, 0]), "rotation")
    scale = _vector(params.get("scale", [1, 1, 1]), "scale")
    spline_spec = _normalize_spline(params.get("spline"))
    settings = _curve_settings(params, defaults=True)
    bpy = _prepare_mutation(params)
    collection = _target_collection(bpy, collection_name)
    resolved_name = _unique_curve_name(bpy, requested_name)
    data = None
    obj = None
    transaction = new_transaction([{"type": "Object", "name": resolved_name}, {"type": "Curve", "name": resolved_name}])
    try:
        data = bpy.data.curves.new(resolved_name, type="CURVE")
        for name, value in settings.items():
            setattr(data, name, value)
        spline = data.splines.new(spline_spec["type"])
        _write_spline(spline, spline_spec, create_points=True)
        obj = bpy.data.objects.new(resolved_name, data)
        obj.location = location
        obj.rotation_euler = rotation
        obj.scale = scale
        collection.objects.link(obj)
        detail = _curve_detail(obj)
    except Exception as exc:
        cleanup_errors = []
        if obj is not None and bpy.data.objects.get(obj.name) is obj:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove object: {cleanup_exc}")
        if data is not None and bpy.data.curves.get(data.name) is data and data.users == 0:
            try:
                bpy.data.curves.remove(data)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove Curve data: {cleanup_exc}")
        transaction.update(changed=bool(cleanup_errors), warnings=cleanup_errors)
        if cleanup_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"curve.create failed: {exc}", transaction) from exc
    transaction.update(
        object=obj.name,
        requested_name=requested_name,
        resolved_name=obj.name,
        detail=detail,
        changed=True,
        journal=[{"op": "curve.create", "target": obj.name, "before": None, "after": detail, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def patch_curve(params: dict[str, Any]) -> dict[str, Any]:
    setting_fields = {"dimensions", "resolution_u", "render_resolution_u", "fill_mode", "bevel_depth", "bevel_resolution", "twist_smooth"}
    _unknown(params, {"object_name", "expected_state", *setting_fields})
    object_name = _name(params.get("object_name"), "object_name")
    settings = _curve_settings(params, defaults=False)
    if not settings:
        raise InvalidParamsError("At least one Curve setting must be supplied")
    bpy = _prepare_mutation(params)
    obj = _curve_object(bpy, object_name, single_user=True)
    data = obj.data
    before = {name: getattr(data, name) for name in settings}
    changed = [name for name in settings if before[name] != settings[name]]
    journal = [
        {"op": "curve.patch", "target": obj.name, "field": name, "before": before[name], "after": value, "status": "planned" if name in changed else "unchanged"}
        for name, value in settings.items()
    ]
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "Curve", "name": data.name}])
    applied = []
    try:
        for name in changed:
            applied.append(name)
            setattr(data, name, settings[name])
            next(entry for entry in journal if entry["field"] == name)["status"] = "applied"
    except Exception as exc:
        rollback_errors = []
        for name in reversed(applied):
            try:
                setattr(data, name, before[name])
                next(entry for entry in journal if entry["field"] == name)["status"] = "rolled_back"
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {name}: {rollback_exc}")
        transaction.update(curve=obj.name, changed=bool(rollback_errors), journal=journal, warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"curve.patch failed: {exc}", transaction) from exc
    transaction.update(curve=obj.name, before=before, after={name: getattr(data, name) for name in settings}, changed=bool(changed), journal=journal)
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def add_curve_spline(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "spline", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    spec = _normalize_spline(params.get("spline"))
    bpy = _prepare_mutation(params)
    obj = _curve_object(bpy, object_name, single_user=True)
    data = obj.data
    total_points = sum(_point_count(existing) for existing in data.splines)
    if total_points + len(spec["points"]) > MAX_CONTROL_POINTS:
        raise InvalidStateError(
            f"Adding the spline would exceed the Curve limit of {MAX_CONTROL_POINTS} control points"
        )
    index = len(data.splines)
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "Curve", "name": data.name}])
    spline = None
    try:
        spline = data.splines.new(spec["type"])
        _write_spline(spline, spec, create_points=True)
        summary = _spline_summary(spline, index)
    except Exception as exc:
        cleanup_errors = []
        if spline is not None:
            try:
                data.splines.remove(spline)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove partial spline: {cleanup_exc}")
        transaction.update(curve=obj.name, changed=bool(cleanup_errors), warnings=cleanup_errors)
        if cleanup_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"curve.spline_add failed: {exc}", transaction) from exc
    transaction.update(curve=obj.name, spline_index=index, spline=summary, changed=True, journal=[{"op": "curve.spline_add", "target": index, "before": None, "after": summary, "status": "applied"}])
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def patch_curve_spline(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "spline_index", "spline", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    index = _integer(params.get("spline_index"), "spline_index", 0, MAX_CONTROL_POINTS)
    spec = _normalize_spline(params.get("spline"))
    bpy = _prepare_mutation(params)
    obj = _curve_object(bpy, object_name, single_user=True)
    data = obj.data
    if index >= len(data.splines):
        raise InvalidParamsError(f"spline_index must be less than spline count {len(data.splines)}")
    spline = data.splines[index]
    if spline.type != spec["type"]:
        raise InvalidParamsError("curve.spline_patch cannot change the spline type")
    if _point_count(spline) != len(spec["points"]):
        raise InvalidParamsError("curve.spline_patch cannot change the control-point count")
    before = _serialize_spline(spline)
    transaction = new_transaction([{"type": "Object", "name": obj.name}, {"type": "Curve", "name": data.name}])
    try:
        _write_spline(spline, spec, create_points=False)
        after = _serialize_spline(spline)
    except Exception as exc:
        rollback_errors = []
        try:
            _write_spline(spline, before, create_points=False)
        except Exception as rollback_exc:
            rollback_errors.append(f"restore spline {index}: {rollback_exc}")
        transaction.update(curve=obj.name, spline_index=index, changed=bool(rollback_errors), warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"curve.spline_patch failed: {exc}", transaction) from exc
    changed = before != after
    transaction.update(
        curve=obj.name,
        spline_index=index,
        before=before,
        after=after,
        changed=changed,
        journal=[{"op": "curve.spline_patch", "target": index, "before": before, "after": after, "status": "applied" if changed else "unchanged"}],
    )
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def remove_curve_spline(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "spline_index", "dry_run", "confirm", "expected_state"})
    object_name = _name(params.get("object_name"), "object_name")
    index = _integer(params.get("spline_index"), "spline_index", 0, MAX_CONTROL_POINTS)
    _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("curve.spline_remove requires Blender Object mode")
    obj = _curve_object(bpy, object_name, single_user=True)
    data = obj.data
    if index >= len(data.splines):
        raise InvalidParamsError(f"spline_index must be less than spline count {len(data.splines)}")
    if index != len(data.splines) - 1:
        raise InvalidStateError("curve.spline_remove currently permits only the final spline so order is recoverable")
    before = _serialize_spline(data.splines[index])
    journal = [{"op": "curve.spline_remove", "target": index, "before": before, "after": None, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="curve.spline_remove",
        affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Curve", "name": data.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={"curve": obj.name, "spline_index": index, "recovery": "The removed final spline is retained in the transaction preflight journal."},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    count_before = len(data.splines)
    try:
        data.splines.remove(data.splines[index])
        journal[0]["status"] = "applied"
    except Exception as exc:
        partial = len(data.splines) != count_before
        transaction["changed"] = partial
        if partial:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"curve.spline_remove failed: {exc}", transaction) from exc
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
