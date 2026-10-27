"""Read-only, world-space mesh interface measurements."""

from __future__ import annotations

from itertools import combinations
from math import isfinite
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < minimum:
        raise InvalidParamsError(f"{label} must be a finite number >= {minimum}")
    return float(value)


def _positive_int(value: Any, label: str, limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
        raise InvalidParamsError(f"{label} must be an integer from 1 to {limit}")
    return value


def _names(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
        raise InvalidParamsError(f"{label} must be a non-empty list of object names")
    if len(set(value)) != len(value):
        raise InvalidParamsError(f"{label} must not contain duplicates")
    return value


def _pair_names(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, list) or not value:
        raise InvalidParamsError("pairs must be a non-empty list of two-object lists")
    result = []
    seen = set()
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise InvalidParamsError("each pair must contain exactly two object names")
        a, b = _names(pair, "pair")
        key = tuple(sorted((a, b)))
        if key not in seen:
            result.append((a, b))
            seen.add(key)
    return result


def _expectations(value: Any) -> dict[tuple[str, str], dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise InvalidParamsError("expectations must be a list of per-pair objects")
    result = {}
    for item in value:
        if not isinstance(item, dict) or set(item) - {"pair", "contact", "gap_between", "no_intersection", "intersection_allowed"}:
            raise InvalidParamsError("expectation has unsupported fields")
        pair = _pair_names([item.get("pair")])[0]
        key = tuple(sorted(pair))
        if key in result:
            raise InvalidParamsError("duplicate expectation pair")
        spec = {k: v for k, v in item.items() if k != "pair"}
        if not spec:
            raise InvalidParamsError("expectation needs a condition")
        for name in ("contact", "no_intersection", "intersection_allowed"):
            if name in spec and not isinstance(spec[name], bool):
                raise InvalidParamsError(f"{name} must be a boolean")
        if "gap_between" in spec:
            gap = spec["gap_between"]
            if not isinstance(gap, list) or len(gap) != 2:
                raise InvalidParamsError("gap_between must be [minimum, maximum] in metres")
            lo = _number(gap[0], "gap_between minimum")
            hi = _number(gap[1], "gap_between maximum")
            if hi < lo:
                raise InvalidParamsError("gap_between maximum must be >= minimum")
            spec["gap_between"] = [lo, hi]
        if ("no_intersection" in spec and "intersection_allowed" in spec
                and spec["no_intersection"] == spec["intersection_allowed"]):
            raise InvalidParamsError("no_intersection conflicts with intersection_allowed")
        result[key] = spec
    return result


def _evaluate_expectation(status: str, gap_m: float | None, spec: dict[str, Any]) -> dict[str, Any]:
    checks = {}
    if "contact" in spec:
        if spec["contact"]:
            checks["contact"] = status == "contact" or (status == "intersecting" and spec.get("intersection_allowed", False))
        else:
            checks["contact"] = status in {"gap", "separated"}
    if "gap_between" in spec:
        lo, hi = spec["gap_between"]
        checks["gap_between"] = status != "intersecting" and gap_m is not None and lo <= gap_m <= hi
    if "no_intersection" in spec:
        checks["no_intersection"] = (status != "intersecting") if spec["no_intersection"] else (status == "intersecting")
    # True grants permission without requiring penetration; false forbids it.
    if "intersection_allowed" in spec:
        checks["intersection_allowed"] = spec["intersection_allowed"] or status != "intersecting"
    return {"status": "pass" if all(checks.values()) else "fail", "checks": checks}


def _expectation_margins(status: str, gap_m: float | None, depth_m: float | None,
                         spec: dict[str, Any], tolerance_m: float,
                         relative_band: float) -> tuple[dict[str, float | None], bool]:
    """Signed metres to numeric limits; negative means the limit is missed."""
    margins: dict[str, float | None] = {}
    limits: dict[str, float] = {}
    if "gap_between" in spec:
        lo, hi = spec["gap_between"]
        margins["gap_min_m"] = None if gap_m is None else gap_m - lo
        margins["gap_max_m"] = None if gap_m is None else hi - gap_m
        limits.update(gap_min_m=lo, gap_max_m=hi)
    if spec.get("contact") is True:
        margins["contact_tolerance_m"] = None if gap_m is None else tolerance_m - gap_m
        limits["contact_tolerance_m"] = tolerance_m
    if spec.get("no_intersection") is True or spec.get("intersection_allowed") is False:
        margins["no_intersection_m"] = -depth_m if status == "intersecting" and depth_m is not None else gap_m
        limits["no_intersection_m"] = tolerance_m
    borderline = any(value is not None and abs(value) <= max(limits[key] * relative_band, 1e-9)
                     for key, value in margins.items())
    return margins, borderline


def _classify(distance: float, tolerance: float, margin: float, *, penetrates: bool) -> str:
    if penetrates:
        return "intersecting"
    if distance <= tolerance:
        return "contact"
    if distance <= margin:
        return "gap"
    return "separated"


def _bounds_overlap(a: tuple, b: tuple, margin: float) -> bool:
    return all(a[0][axis] <= b[1][axis] + margin and b[0][axis] <= a[1][axis] + margin for axis in range(3))


def _bounds_contains(outer: tuple, inner: tuple) -> bool:
    return all(outer[0][axis] <= inner[0][axis] and inner[1][axis] <= outer[1][axis] for axis in range(3))


def _priority(a: Any, b: Any) -> tuple[int, str, str]:
    relatives = a.parent == b or b.parent == a
    shared = bool(set(a.users_collection) & set(b.users_collection))
    return (0 if relatives else 1 if shared else 2, a.name, b.name)


def _candidate_pairs(objects: list[Any], bounds: dict[str, tuple], margin: float, cap: int) -> tuple[list[tuple], int, int]:
    pairs = [(a, b) for a, b in combinations(objects, 2) if _bounds_overlap(bounds[a.name], bounds[b.name], margin)]
    pairs.sort(key=lambda pair: _priority(*pair))
    return pairs[:cap], max(0, len(pairs) - cap), len(objects) * (len(objects) - 1) // 2 - len(pairs)


def _bounds(vertices: list[Any]) -> tuple:
    return tuple(tuple(fn(v[i] for v in vertices) for i in range(3)) for fn in (min, max))


def _diagonal(bounds: tuple) -> float:
    return sum((bounds[1][i] - bounds[0][i]) ** 2 for i in range(3)) ** 0.5


def _sample(items: list[Any], limit: int) -> list[Any]:
    if len(items) <= limit:
        return items
    return [items[i * len(items) // limit] for i in range(limit)]


def _mesh_data(obj: Any, depsgraph: Any, evaluated: bool, face_budget: int) -> dict[str, Any]:
    from mathutils.bvhtree import BVHTree

    source = obj.evaluated_get(depsgraph) if evaluated else obj
    mesh = source.to_mesh()
    if mesh is None:
        raise InvalidStateError(f"{obj.name} has no mesh surface")
    try:
        if len(mesh.polygons) > face_budget:
            raise InvalidStateError(
                f"{obj.name} has {len(mesh.polygons)} faces, exceeding max_faces_per_object={face_budget}; "
                "raise the budget to analyse the full mesh"
            )
        mesh.calc_loop_triangles()
        edge_use: dict[tuple[int, int], int] = {}
        for poly in mesh.polygons:
            indices = tuple(poly.vertices)
            for index, start in enumerate(indices):
                edge = tuple(sorted((start, indices[(index + 1) % len(indices)])))
                edge_use[edge] = edge_use.get(edge, 0) + 1
        closed = bool(edge_use) and len(edge_use) == len(mesh.edges) and all(count == 2 for count in edge_use.values())
        matrix = source.matrix_world.copy()
        verts = [matrix @ vert.co for vert in mesh.vertices]
        if not verts or not mesh.loop_triangles:
            raise InvalidStateError(f"{obj.name} has no triangular surface")
        tris = [tuple(tri.vertices) for tri in mesh.loop_triangles]
        centers = [sum((verts[i] for i in tri), verts[tri[0]] * 0.0) / 3.0 for tri in tris]
        return {
            "bvh": BVHTree.FromPolygons(verts, tris, all_triangles=True),
            "vertices": verts, "triangles_indices": tris, "centers": centers, "bounds": _bounds(verts),
            "faces": len(mesh.polygons), "triangles": len(tris), "closed": closed, "name": obj.name,
        }
    finally:
        source.to_mesh_clear()


def _inside(point: Any, bvh: Any, extent: float) -> bool | None:
    from mathutils import Vector

    # Parity with three non-axis-aligned rays reduces edge/vertex ambiguity.
    directions = (Vector((1, 0.213, 0.417)), Vector((0.371, 1, 0.113)), Vector((0.197, 0.337, 1)))
    votes = 0
    step = max(extent, *(abs(float(component)) for component in point)) * 1e-6
    step = max(step, 1e-6)
    valid_rays = 0
    for direction in directions:
        direction.normalize()
        origin = point + direction * step
        count = 0
        previous_index = None
        for _ in range(1000):
            hit, _, face_index, distance = bvh.ray_cast(origin, direction, extent * 4 + 1)
            if hit is None:
                valid_rays += 1
                votes += count % 2
                break
            if distance > step and face_index != previous_index:
                count += 1
            previous_index = face_index
            origin = hit + direction * step
    if valid_rays < 2:
        return None
    return votes >= 2


def _crossing_faces(a: dict[str, Any], b: dict[str, Any], overlaps: list[tuple[int, int]], tolerance: float, extent: float) -> bool:
    """Separate transverse triangle crossings from touching coplanar faces."""
    magnitude = max(abs(float(component)) for bounds in (a["bounds"], b["bounds"]) for corner in bounds for component in corner)
    epsilon = max(extent * 1e-7, magnitude * 1e-7, 1e-7)
    threshold = max(tolerance, epsilon)
    for a_index, b_index in overlaps:
        av = [a["vertices"][i] for i in a["triangles_indices"][a_index]]
        bv = [b["vertices"][i] for i in b["triangles_indices"][b_index]]
        normal_a = (av[1] - av[0]).cross(av[2] - av[0])
        normal_b = (bv[1] - bv[0]).cross(bv[2] - bv[0])
        if normal_a.length_squared <= epsilon * epsilon or normal_b.length_squared <= epsilon * epsilon:
            continue
        da = [(v - av[0]).dot(normal_a) / normal_a.length for v in bv]
        db = [(v - bv[0]).dot(normal_b) / normal_b.length for v in av]
        if min(da) < -threshold and max(da) > threshold and min(db) < -threshold and max(db) > threshold:
            return True
    return False


def _measure_pair(a: dict[str, Any], b: dict[str, Any], tolerance: float, margin: float) -> dict[str, Any]:
    smaller, larger = (a, b) if len(a["vertices"]) <= len(b["vertices"]) else (b, a)
    sampled = []
    for source, target in ((smaller, larger), (larger, smaller)):
        points = _sample(source["vertices"], 2048) + _sample(source["centers"], 512)
        for point in points:
            closest, _, _, dist = target["bvh"].find_nearest(point)
            if closest is not None:
                sampled.append((float(dist), point, closest, source, target))
    if not sampled:
        raise InvalidStateError("Unable to find a surface distance for the selected pair")
    distance, from_point, to_point, _, _ = min(sampled, key=lambda row: row[0])
    overlaps = a["bvh"].overlap(b["bvh"])
    extent = max(_diagonal(a["bounds"]), _diagonal(b["bounds"]), 1.0)
    depths = []
    undetermined = not a["closed"] or not b["closed"]
    if overlaps or _bounds_contains(a["bounds"], b["bounds"]) or _bounds_contains(b["bounds"], a["bounds"]):
        # Face centers capture aligned overlaps whose vertices lie on a shared
        # outer boundary. Both kinds of samples remain bounded by the budget.
        for source, target in ((a, b), (b, a)):
            if not target["closed"]:
                continue
            for point in _sample(source["vertices"], 2048) + _sample(source["centers"], 512):
                nearest, _, _, depth = target["bvh"].find_nearest(point)
                if nearest is not None and depth > tolerance and _inside(point, target["bvh"], extent) is True:
                    depths.append((float(depth), point, nearest, target["name"]))
    depth_entry = max(depths, key=lambda row: row[0]) if depths else None
    # BVH overlap can also occur at a shared coplanar boundary; depth evidence
    # distinguishes penetration there. Crossing faces without interior sampled
    # vertices retain a conservative intersection classification.
    crossing = bool(overlaps) and _crossing_faces(a, b, overlaps, tolerance, extent)
    status = _classify(distance, tolerance, margin, penetrates=bool(depth_entry) or crossing)
    evidence = []
    if crossing:
        evidence.append("crossing_faces")
    if depth_entry:
        evidence.append("inside_samples")
    if not evidence:
        evidence.append("distance")
    region = [row[1] for row in sampled if row[0] <= tolerance]
    region_bounds = _bounds(region) if region else None
    contact_area = None
    if region_bounds:
        spans = sorted(region_bounds[1][axis] - region_bounds[0][axis] for axis in range(3))
        if spans[1] > 0 and spans[2] > 0:
            contact_area = spans[1] * spans[2]
    focus_point = (depth_entry[1] + depth_entry[2]) / 2 if depth_entry else (from_point + to_point) / 2
    focus_radius = max(tolerance * 4, distance * 2, min(_diagonal(smaller["bounds"]) * 0.15, extent * 0.05), 0.001)
    return {
        "status": status,
        "status_evidence": evidence,
        "penetration": "undetermined" if undetermined else "confirmed" if status == "intersecting" else "none",
        "inside_sample_targets": sorted({entry[3] for entry in depths}),
        "min_gap": 0.0 if status == "intersecting" else distance,
        "closest_points": [[float(x) for x in from_point], [float(x) for x in to_point]],
        "overlap_face_pairs": len(overlaps),
        "max_penetration_depth": depth_entry[0] if depth_entry else None,
        "penetration_point": [float(x) for x in depth_entry[1]] if depth_entry else None,
        "contact_samples": len(region),
        "contact_area": contact_area,
        "samples_per_contact_area": len(region) / contact_area if contact_area else None,
        "contact_bounds": {"min": list(region_bounds[0]), "max": list(region_bounds[1])} if region_bounds else None,
        "focus": {"point": [float(x) for x in focus_point], "radius": focus_radius},
        "samples": len(sampled),
    }


def analyze_assembly(params: dict[str, Any]) -> ToolResult:
    """Measure interfaces on real mesh surfaces without changing the document."""
    allowed = {"pairs", "object_names", "collection_name", "search_margin", "contact_tolerance",
               "max_faces_per_object", "max_pairs", "evaluated", "expectations", "borderline_relative_band",
               "expect_isolated"}
    unknown = set(params) - allowed
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    scope_count = sum(name in params for name in ("pairs", "object_names", "collection_name"))
    if scope_count != 1:
        raise InvalidParamsError("Provide exactly one of pairs, object_names, or collection_name")
    bpy = get_bpy()
    evaluated = params.get("evaluated", True)
    if not isinstance(evaluated, bool):
        raise InvalidParamsError("evaluated must be a boolean")
    face_budget = _positive_int(params.get("max_faces_per_object", 250_000), "max_faces_per_object", 2_000_000)
    pair_cap = _positive_int(params.get("max_pairs", 200), "max_pairs", 10_000)
    scale = float(bpy.context.scene.unit_settings.scale_length) or 1.0
    tolerance_m = _number(params.get("contact_tolerance", 0.0001), "contact_tolerance")
    borderline_band = _number(params.get("borderline_relative_band", 0.02), "borderline_relative_band")
    if borderline_band > 1:
        raise InvalidParamsError("borderline_relative_band must be <= 1")
    expectation_map = _expectations(params.get("expectations"))
    explicit = "pairs" in params
    if explicit:
        pair_names = _pair_names(params["pairs"])
        names = list(dict.fromkeys(name for pair in pair_names for name in pair))
    elif "object_names" in params:
        names = _names(params["object_names"], "object_names")
    else:
        collection_name = params["collection_name"]
        if not isinstance(collection_name, str) or not collection_name.strip():
            raise InvalidParamsError("collection_name must be a non-empty string")
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            raise NotFoundError(f"Collection not found: {collection_name}")
        names = [obj.name for obj in collection.all_objects if obj.type == "MESH"]
        if not names:
            raise InvalidStateError(f"Collection {collection_name} contains no mesh objects")
    objects = []
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            raise NotFoundError(f"Object not found: {name}")
        if obj.type != "MESH":
            raise InvalidParamsError(f"{name} is {obj.type}; assembly analysis needs mesh objects")
        objects.append(obj)
    if len(objects) < 2:
        raise InvalidParamsError("Analysis needs at least two mesh objects")
    expected_isolated = params.get("expect_isolated", [])
    if not isinstance(expected_isolated, list) or any(not isinstance(name, str) or not name.strip() for name in expected_isolated):
        raise InvalidParamsError("expect_isolated must be a list of object names")
    if len(set(expected_isolated)) != len(expected_isolated) or set(expected_isolated) - set(names):
        raise InvalidParamsError("expect_isolated must contain unique names within the selected scope")
    missing_expectations = set(expectation_map) - (set(tuple(sorted(pair)) for pair in pair_names) if explicit else set(combinations(sorted(names), 2)))
    if missing_expectations:
        raise InvalidParamsError("expectations reference an object pair outside the selected scope")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    meshes = {obj.name: _mesh_data(obj, depsgraph, evaluated, face_budget) for obj in objects}
    all_verts = [corner for mesh in meshes.values() for corner in mesh["bounds"]]
    scope_diagonal = _diagonal(_bounds(all_verts))
    margin_m = _number(params.get("search_margin", max(scope_diagonal * scale * 0.01, 0.001)), "search_margin")
    margin = margin_m / scale
    tolerance = tolerance_m / scale
    if explicit:
        object_map = {obj.name: obj for obj in objects}
        selected = [(object_map[a], object_map[b]) for a, b in pair_names[:pair_cap]]
        skipped_cap = max(0, len(pair_names) - pair_cap)
        skipped_bounds = 0
    else:
        selected, skipped_cap, skipped_bounds = _candidate_pairs(objects, {key: value["bounds"] for key, value in meshes.items()}, margin, pair_cap)
    result_pairs = []
    separated = 0
    connected = set()
    warnings = []
    for obj_a, obj_b in selected:
        a, b = meshes[obj_a.name], meshes[obj_b.name]
        measured = _measure_pair(a, b, tolerance, margin)
        open_names = [mesh["name"] for mesh in (a, b) if not mesh["closed"]]
        if open_names:
            warnings.append(f"Pair {obj_a.name} / {obj_b.name}: open or non-manifold mesh ({', '.join(open_names)}); penetration depth is undetermined for that surface")
        for field in ("min_gap", "max_penetration_depth"):
            if measured[field] is not None:
                measured[field] = round(measured[field] * scale, 9)
        if measured["contact_area"] is not None:
            measured["contact_area_m2"] = measured.pop("contact_area") * scale * scale
            measured["samples_per_contact_area_m2"] = measured.pop("samples_per_contact_area") / (scale * scale)
        else:
            measured.pop("contact_area")
            measured.pop("samples_per_contact_area")
            measured["contact_area_m2"] = None
            measured["samples_per_contact_area_m2"] = None
        key = tuple(sorted((obj_a.name, obj_b.name)))
        if key in expectation_map:
            expectation = _evaluate_expectation(measured["status"], measured["min_gap"], expectation_map[key])
            margins, borderline = _expectation_margins(
                measured["status"], measured["min_gap"], measured["max_penetration_depth"],
                expectation_map[key], tolerance_m, borderline_band)
            expectation.update(margin=margins, borderline=borderline)
            measured["expectation"] = expectation
        if measured["status"] == "separated" and measured.get("expectation", {}).get("status") != "fail":
            separated += 1
            continue
        if measured["status"] in {"contact", "intersecting"}:
            connected.update((obj_a.name, obj_b.name))
        measured.update({"objects": [obj_a.name, obj_b.name], "faces": [a["faces"], b["faces"]]})
        result_pairs.append(measured)
    if skipped_cap:
        warnings.append(f"{skipped_cap} candidate pairs skipped by max_pairs; increase the cap to analyse them")
    isolated_parts = []
    if not skipped_cap:
        for name in names:
            if name in connected:
                continue
            bounds = meshes[name]["bounds"]
            centre = [(bounds[0][axis] + bounds[1][axis]) / 2 for axis in range(3)]
            isolated_parts.append({
                "object": name, "expected": name in expected_isolated,
                "focus": {"point": centre,
                          "radius": max(_diagonal(bounds) * 0.2, margin * 2, 0.001)},
            })
    else:
        warnings.append("isolated_parts is incomplete while candidate pairs are skipped by max_pairs")
    warnings.append("Distances and penetration depth use bounded surface samples; depth can be null when crossing faces have no interior sample")
    return ToolResult(data={
        "pairs": result_pairs, "separated_count": separated,
        "isolated_parts": isolated_parts, "isolation_complete": not bool(skipped_cap),
        "unexpected_isolated_count": sum(not part["expected"] for part in isolated_parts),
        "candidate_pairs": len(selected) + skipped_cap,
        "analysed_pairs": len(selected), "skipped_pairs_cap": skipped_cap,
        "skipped_pairs_bounds": skipped_bounds,
        "search_margin_m": margin_m, "contact_tolerance_m": tolerance_m,
        "borderline_relative_band": borderline_band,
        "evaluated": evaluated, "distance_unit": "metres",
        "distance_method": "sampled_vertices_and_face_centers",
    }, warnings=warnings)
