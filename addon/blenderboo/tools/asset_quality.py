"""Read-only, bounded topology acceptance checks on an object's base mesh."""

from __future__ import annotations

from collections import defaultdict
from math import acos, degrees, isfinite

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .mesh_topology import topology_report


SAMPLE_LIMIT = 32
MAX_VERTICES = 100000
MAX_TRIANGLES = 100000
MAX_OVERLAP_CANDIDATES = 500000
BVH_CHUNK_SIZE = 128
MAX_ALLOWED_LOOP_BREAKS = 256


def _number(params, name, default=None, *, minimum=None, maximum=None):
    value = params.get(name, default)
    if value is None and default is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not isfinite(value) or (minimum is not None and value < minimum)
            or (maximum is not None and value > maximum)):
        raise InvalidParamsError(f"{name} must be a finite number in the stated range")
    return float(value)


def _thresholds(params):
    result = {
        "sliver_min_edge": _number(params, "sliver_min_edge", 0.0, minimum=0),
        "sliver_min_angle_deg": _number(params, "sliver_min_angle_deg", 1.0, minimum=0, maximum=180),
        "symmetry_plane_x": _number(params, "symmetry_plane_x"),
        "symmetry_tolerance_m": _number(params, "symmetry_tolerance_m", minimum=0),
        "require_quads": params.get("require_quads", True),
        "triangle_budget": params.get("triangle_budget"),
    }
    if not isinstance(result["require_quads"], bool):
        raise InvalidParamsError("require_quads must be a boolean")
    budget = result["triangle_budget"]
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
        raise InvalidParamsError("triangle_budget must be a positive integer")
    if result["symmetry_tolerance_m"] is not None and result["symmetry_plane_x"] is None:
        raise InvalidParamsError("symmetry_tolerance_m requires symmetry_plane_x")
    return result


def _face_shape(points):
    """World-space shortest edge and smallest polygon corner angle (degrees)."""
    lengths, angles = [], []
    for index, point in enumerate(points):
        previous = points[index - 1] - point
        following = points[(index + 1) % len(points)] - point
        lengths.append(following.length)
        product = previous.length * following.length
        # A collapsed edge is a zero-angle defect, including when thresholds are zero.
        angles.append(degrees(acos(max(-1.0, min(1.0, previous.dot(following) / product))))
                      if product > 0 else 0.0)
    return min(lengths, default=0.0), min(angles, default=0.0)


def _allowed_loop_breaks(value, vertex_count):
    if not isinstance(value, list) or len(value) > MAX_ALLOWED_LOOP_BREAKS:
        raise InvalidParamsError(f"allowed_loop_break_vertices must be a list of at most {MAX_ALLOWED_LOOP_BREAKS} indices")
    if any(isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < vertex_count
           for index in value):
        raise InvalidParamsError("allowed_loop_break_vertices must contain existing base-mesh integer vertex indices")
    if len(set(value)) != len(value):
        raise InvalidParamsError("allowed_loop_break_vertices must contain unique indices")
    return sorted(value)


def _loop_continuity(points, faces, edges, sharp_edges, allowed_loop_break_vertices=()):
    """Partition edges into maximal opposite-edge paths through regular quad fans.

    A regular fan has four incident edges, four quad faces and two faces per
    edge. Boundary/sharp vertices intentionally end paths. A non-4-valent
    interior vertex is an unexpected endpoint unless its exact index was
    explicitly allowed. Other irregular fans remain failures.
    """
    edge_faces = defaultdict(set)
    incident = defaultdict(list)
    for face_index, face in enumerate(faces):
        for i, a in enumerate(face):
            edge_faces[tuple(sorted((a, face[(i + 1) % len(face)])))].add(face_index)
    edge_keys = [tuple(sorted(edge)) for edge in edges]
    for index, (a, b) in enumerate(edge_keys):
        incident[a].append(index)
        incident[b].append(index)
    graph = [set() for _ in edges]
    unexpected, irregular, allowed = set(), set(), set()
    declared = set(allowed_loop_break_vertices)
    for vertex, linked in incident.items():
        if (any(len(edge_faces[edge_keys[e]]) == 1 for e in linked)
                or any(e in sharp_edges for e in linked)):
            continue
        if len(linked) != 4:
            (allowed if vertex in declared else unexpected).add(vertex)
            continue
        fan = set().union(*(edge_faces[edge_keys[e]] for e in linked))
        if (len(fan) != 4 or any(len(faces[f]) != 4 for f in fan)
                or any(len(edge_faces[edge_keys[e]]) != 2 for e in linked)):
            irregular.add(vertex)
            continue
        for edge in linked:
            opposite = [other for other in linked if other != edge
                        and not edge_faces[edge_keys[edge]] & edge_faces[edge_keys[other]]]
            if len(opposite) == 1:
                graph[edge].add(opposite[0])
            else:
                irregular.add(vertex)
    visited = set()
    total = broken = closed = ambiguous = unbroken = 0
    for seed in range(len(edges)):
        if seed in visited:
            continue
        stack, component = [seed], []
        visited.add(seed)
        while stack:
            edge = stack.pop()
            component.append(edge)
            for neighbor in graph[edge]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        vertices = {v for edge in component for v in edge_keys[edge]}
        total += 1
        broken += bool(vertices & unexpected)
        ambiguous += bool(vertices & irregular)
        unbroken += not bool(vertices & (unexpected | irregular))
        closed += all(len(graph[edge]) == 2 for edge in component)
    bad, accepted = sorted(unexpected), sorted(allowed)
    return {"method": "maximal_opposite_edge_paths_in_quad_fans", "path_count": total,
            "closed_path_count": closed, "broken_path_count": broken,
            "ambiguous_path_count": ambiguous,
            "unbroken_share": unbroken / total if total else None,
            "unexpected_endpoint_count": len(bad),
            "positions": [list(points[v]) for v in bad[:SAMPLE_LIMIT]],
            "vertices": bad[:SAMPLE_LIMIT], "irregular_vertex_count": len(irregular),
            "irregular_positions": [list(points[v]) for v in sorted(irregular)[:SAMPLE_LIMIT]],
            "allowed_endpoint_count": len(accepted), "allowed_endpoint_vertices": accepted[:SAMPLE_LIMIT],
            "allowed_endpoint_positions": [list(points[v]) for v in accepted[:SAMPLE_LIMIT]],
            "truncated": any(len(group) > SAMPLE_LIMIT for group in (bad, irregular, accepted))}


def _intersection_report(points, triangles, triangle_faces, faces, parts, centers):
    """BVH triangle overlaps, deduplicated to non-adjacent original face pairs.

    Chunking bounds each native overlap allocation. A global candidate cap
    bounds dense/degenerate cases; incomplete counts are explicitly lower bounds.
    """
    from mathutils.bvhtree import BVHTree

    order = sorted(range(len(triangles)), key=lambda i: sum(points[v].x for v in triangles[i]) / 3)
    chunks = []
    for start in range(0, len(order), BVH_CHUNK_SIZE):
        indices = order[start:start + BVH_CHUNK_SIZE]
        used = {v for i in indices for v in triangles[i]}
        lower = tuple(min(points[v][axis] for v in used) for axis in range(3))
        upper = tuple(max(points[v][axis] for v in used) for axis in range(3))
        # Each BVH owns only its chunk vertices, avoiding O(mesh_size * chunks).
        local = sorted(used)
        remap = {v: i for i, v in enumerate(local)}
        tree = BVHTree.FromPolygons([points[v] for v in local],
                                   [tuple(remap[v] for v in triangles[i]) for i in indices],
                                   all_triangles=True, epsilon=0.0)
        chunks.append((lower, upper, indices, tree))
    chunks.sort(key=lambda chunk: chunk[0][0])
    unique, samples, candidates = set(), [], 0
    inside_positions, between_positions = [], []
    inside = between = 0
    complete = True
    face_vertices = [set(face) for face in faces]
    for left_index, (lower, upper, indices, tree) in enumerate(chunks):
        for right_index in range(left_index, len(chunks)):
            other_lower, other_upper, other_indices, other_tree = chunks[right_index]
            if other_lower[0] > upper[0]:
                break
            if any(other_lower[axis] > upper[axis] or lower[axis] > other_upper[axis] for axis in (1, 2)):
                continue
            for a, b in tree.overlap(other_tree):
                candidates += 1
                if candidates > MAX_OVERLAP_CANDIDATES:
                    complete = False
                    break
                first, second = triangle_faces[indices[a]], triangle_faces[other_indices[b]]
                if first == second:
                    continue
                pair = tuple(sorted((first, second)))
                if pair in unique or face_vertices[first] & face_vertices[second]:
                    continue
                unique.add(pair)
                same_part = parts[first] == parts[second]
                inside += same_part
                between += not same_part
                position = list((centers[first] + centers[second]) * 0.5)
                split_positions = inside_positions if same_part else between_positions
                if len(split_positions) < SAMPLE_LIMIT:
                    split_positions.append(position)
                if len(samples) < SAMPLE_LIMIT:
                    samples.append({"faces": list(pair), "parts": [parts[v] for v in pair],
                                    "classification": "inside_part" if same_part else "between_parts",
                                    "position": position})
            if not complete:
                break
        if not complete:
            break
    return {"status": "measured" if complete else "incomplete", "count": len(unique),
            "inside_parts": inside, "between_parts": between,
            "inside_part_positions": inside_positions, "between_part_positions": between_positions,
            "positions": [sample["position"] for sample in samples], "samples": samples,
            "position_method": "midpoint_of_face_centers_not_exact_contact",
            "candidate_count": min(candidates, MAX_OVERLAP_CANDIDATES),
            "candidate_limit": MAX_OVERLAP_CANDIDATES, "triangle_limit": MAX_TRIANGLES,
            "count_is_lower_bound": not complete,
            "truncated": len(unique) > SAMPLE_LIMIT,
            "reason": None if complete else "overlap_candidate_limit"}


def quality_gate(params: dict) -> ToolResult:
    """Inspect base topology without updating the document, selection or epoch."""
    thresholds = _thresholds(params)
    bpy = get_bpy()
    name = params.get("object_name")
    obj = bpy.data.objects.get(name) if isinstance(name, str) else None
    if obj is None or obj.type != "MESH":
        raise NotFoundError(f"Mesh object not found: {name}")
    if obj.mode == "EDIT":
        raise InvalidStateError("Exit Edit mode before auditing base mesh data")
    mesh = obj.data
    triangle_count = sum(max(0, len(face.vertices) - 2) for face in mesh.polygons)
    if len(mesh.vertices) > MAX_VERTICES or triangle_count > MAX_TRIANGLES:
        raise InvalidParamsError(f"quality_gate is limited to {MAX_VERTICES} vertices and {MAX_TRIANGLES} triangles")
    allowed_breaks = _allowed_loop_breaks(params.get("allowed_loop_break_vertices", []), len(mesh.vertices))
    parts = [0] * len(mesh.polygons)
    attribute_name = params.get("part_attribute")
    if attribute_name is not None:
        if not isinstance(attribute_name, str) or not attribute_name:
            raise InvalidParamsError("part_attribute must name a FACE-domain INT attribute")
        attribute = mesh.attributes.get(attribute_name)
        if attribute is None or attribute.domain != "FACE" or attribute.data_type != "INT":
            raise InvalidParamsError("part_attribute must name an existing FACE-domain INT attribute")
        parts = [int(item.value) for item in attribute.data]
    scale = float(bpy.context.scene.unit_settings.scale_length)
    if not isfinite(scale) or scale <= 0:
        raise InvalidStateError("Scene scale_length must be positive and finite")
    if any(not isfinite(value) for row in obj.matrix_world for value in row):
        raise InvalidStateError("Object transform contains non-finite values")
    if any(not isfinite(value) for vertex in mesh.vertices for value in vertex.co):
        raise InvalidStateError("Mesh coordinates contain non-finite values")
    base = topology_report({"object_name": name, "triangle_budget": thresholds["triangle_budget"],
                            "symmetry_plane_x": thresholds["symmetry_plane_x"]})
    report = base.data
    report["poles"] = report["poles"][:20]
    report["poles_in_bend_zones"] = report["poles_in_bend_zones"][:20]
    report["poles_truncated"] = report["pole_count"] > len(report["poles"])
    import bmesh

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.transform(obj.matrix_world)
        bm.normal_update()
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        points = [v.co.copy() for v in bm.verts]
        faces = [tuple(v.index for v in face.verts) for face in bm.faces]
        centers = [face.calc_center_median() for face in bm.faces]
        areas = [face.calc_area() for face in bm.faces]
        slivers, sliver_count, zero_count, zero_positions = [], 0, 0, []
        for face_index, (face, area, center) in enumerate(zip(faces, areas, centers)):
            shortest, angle = _face_shape([points[v] for v in face])
            reasons = []
            if shortest < thresholds["sliver_min_edge"]:
                reasons.append("short_edge")
            if angle < thresholds["sliver_min_angle_deg"]:
                reasons.append("small_corner_angle")
            if reasons:
                sliver_count += 1
                if len(slivers) < SAMPLE_LIMIT:
                    slivers.append({"face": face_index, "position": list(center),
                                    "min_edge": shortest, "min_angle_deg": angle, "reasons": reasons})
            if area <= 0:
                zero_count += 1
                if len(zero_positions) < SAMPLE_LIMIT:
                    zero_positions.append(list(center))
        loop_triangles = bm.calc_loop_triangles()
        triangles = [tuple(loop.vert.index for loop in triangle) for triangle in loop_triangles]
        triangle_faces = [triangle[0].face.index for triangle in loop_triangles]
    finally:
        bm.free()
    edges = [tuple(edge.vertices) for edge in mesh.edges]
    sharp = mesh.attributes.get("sharp_edge")
    sharp_edges = {edge.index for edge in mesh.edges if getattr(edge, "use_edge_sharp", False)}
    if sharp is not None and sharp.domain == "EDGE" and sharp.data_type == "BOOLEAN":
        sharp_edges.update(index for index, value in enumerate(sharp.data) if value.value)
    intersections = _intersection_report(points, triangles, triangle_faces, faces, parts, centers)
    continuity = _loop_continuity(points, faces, edges, sharp_edges, allowed_breaks)
    uses = defaultdict(int)
    for face in faces:
        for i, a in enumerate(face):
            uses[tuple(sorted((a, face[(i + 1) % len(face)])))] += 1
    bad_edges = [edge for edge in edges if uses[tuple(sorted(edge))] == 0 or uses[tuple(sorted(edge))] > 2]
    failures = []

    def fail(reason, count, positions, **extra):
        if count:
            failures.append({"reason": reason, "count": count, "positions": positions[:SAMPLE_LIMIT], **extra})

    fail("empty_mesh", int(not faces), [])
    non_quads = [i for i, face in enumerate(faces) if len(face) != 4]
    if thresholds["require_quads"]:
        fail("non_quad_faces", len(non_quads), [list(centers[i]) for i in non_quads[:SAMPLE_LIMIT]])
    fail("zero_area_faces", zero_count, zero_positions)
    fail("non_manifold_or_wire_edges", len(bad_edges), [list((points[a] + points[b]) * .5) for a, b in bad_edges[:SAMPLE_LIMIT]])
    fail("self_intersections", intersections["count"], intersections["positions"])
    fail("sliver_faces", sliver_count, [item["position"] for item in slivers])
    fail("unexpected_loop_endpoints", continuity["unexpected_endpoint_count"], continuity["positions"])
    fail("irregular_loop_fans", continuity["irregular_vertex_count"], continuity["irregular_positions"])
    if report["within_budget"] is False:
        fail("triangle_budget_exceeded", report["triangles"], [list(centers[0])] if centers else [],
             triangle_budget=thresholds["triangle_budget"])
    symmetry = report.get("symmetry")
    if symmetry:
        for kind in ("mean", "max"):
            key = kind + "_asymmetry_m"
            symmetry[kind + "_asymmetry_scene_units"] = symmetry[key]
            symmetry[key] *= scale
        tolerance = thresholds["symmetry_tolerance_m"]
        symmetry["tolerance_m"] = tolerance
        symmetry["within_tolerance"] = None if tolerance is None else symmetry["max_asymmetry_m"] <= tolerance
        if symmetry["within_tolerance"] is False:
            from mathutils import Vector
            from mathutils.kdtree import KDTree

            kd = KDTree(len(points))
            for index, point in enumerate(points):
                kd.insert(point, index)
            kd.balance()
            bad_positions, bad_count = [], 0
            for point in points:
                mirror = Vector((2 * thresholds["symmetry_plane_x"] - point.x, point.y, point.z))
                if kd.find(mirror)[2] * scale > tolerance:
                    bad_count += 1
                    if len(bad_positions) < SAMPLE_LIMIT:
                        bad_positions.append(list(point))
            fail("symmetry_tolerance_exceeded", bad_count, bad_positions,
                 max_asymmetry_m=symmetry["max_asymmetry_m"], tolerance_m=tolerance)
    complete = intersections["status"] == "measured"
    if not complete:
        fail("analysis_incomplete", 1, [], detail=intersections["reason"])
    report.update({"pass": not failures, "analysis_complete": complete, "failures": failures,
                   "geometry": "base_mesh_modifiers_not_evaluated", "part_attribute": attribute_name,
                   "allowed_loop_break_vertices": allowed_breaks,
                   "thresholds": thresholds, "self_intersections": intersections,
                   "slivers": {"count": sliver_count, "samples": slivers, "truncated": sliver_count > SAMPLE_LIMIT},
                   "loop_continuity": continuity, "world_zero_area_faces": zero_count,
                   "measurement_units": {"positions": "world_scene_units", "sliver_min_edge": "world_scene_units",
                                         "symmetry_plane_x": "world_scene_units", "symmetry_distance": "meters",
                                         "topology_face_area": "object_local_units_squared",
                                         "meters_per_scene_unit": scale,
                                         "scene_unit_system": bpy.context.scene.unit_settings.system},
                   "limits": {"sample_limit": SAMPLE_LIMIT, "max_vertices": MAX_VERTICES,
                              "max_triangles": MAX_TRIANGLES,
                              "max_allowed_loop_break_vertices": MAX_ALLOWED_LOOP_BREAKS,
                              "max_overlap_candidates": MAX_OVERLAP_CANDIDATES}})
    warnings = list(base.warnings)
    if obj.modifiers:
        warnings.append("Quality gate measures base mesh; modifiers are not evaluated")
    if not complete:
        warnings.append("Intersection scan reached its candidate limit; counts are lower bounds and gate failed")
    return ToolResult(data=report, warnings=warnings)
