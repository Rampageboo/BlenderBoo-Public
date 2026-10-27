"""Read-only topology diagnostics shared by creature and hard-surface tools."""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from math import isfinite
import json

from ..errors import InvalidParamsError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy


def connectivity_hash(mesh) -> str:
    """Hash only indices/connectivity, so moving vertices leaves this unchanged."""
    digest = sha256()
    digest.update(f"v{len(mesh.vertices)};e{len(mesh.edges)};f{len(mesh.polygons)};".encode())
    for edge in mesh.edges:
        digest.update(f"{edge.vertices[0]},{edge.vertices[1]};".encode())
    for face in mesh.polygons:
        # Rotation and winding are part of the proof: topology edits must not hide.
        digest.update(",".join(str(index) for index in face.vertices).encode())
        digest.update(b";")
    return digest.hexdigest()


def _edge_use(mesh):
    uses = defaultdict(list)
    for face in mesh.polygons:
        vertices = list(face.vertices)
        for index, a in enumerate(vertices):
            b = vertices[(index + 1) % len(vertices)]
            uses[tuple(sorted((a, b)))].append(face.index)
    return uses


def _components(mesh):
    import numpy as np

    labels = _component_labels(mesh)
    if not len(labels):
        return []
    order = np.argsort(labels, kind="stable")
    boundaries = np.flatnonzero(np.diff(labels[order])) + 1
    groups = np.split(order, boundaries)
    groups.sort(key=len, reverse=True)
    return [group.tolist() for group in groups]


def _component_labels(mesh):
    """Find vertex components from bulk edge arrays without per-vertex walks."""
    import numpy as np

    count = len(mesh.vertices)
    parent = np.arange(count, dtype=np.int32)
    if not count or not len(mesh.edges):
        return parent
    edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", edges)
    edges = edges.reshape((-1, 2))
    a, b = edges[:, 0], edges[:, 1]
    while True:
        # Compress paths before hooking roots. Pointer jumping makes even a
        # long edge chain converge in logarithmic rounds.
        while np.any(parent != parent[parent]):
            parent = parent[parent]
        roots_a, roots_b = parent[a], parent[b]
        higher = np.maximum(roots_a, roots_b)
        lower = np.minimum(roots_a, roots_b)
        previous = parent.copy()
        np.minimum.at(parent, higher, lower)
        if np.array_equal(parent, previous):
            return parent


def component_summary(mesh, *, matrix_world=None, small_fraction=0.01, max_small=16):
    """Compact, bulk-array island evidence for changed meshes only.

    `small_fraction` is a fraction of the mesh's vertex count or world-space
    bounding-box volume. At most `max_small` candidates are returned.
    """
    import numpy as np

    if not 0 < small_fraction <= 1 or max_small < 1:
        raise ValueError("small_fraction must be in (0, 1] and max_small must be positive")
    labels = _component_labels(mesh)
    if not len(labels):
        return {"islands": 0, "smallest_vertices": 0, "smallest_faces": 0,
                "small_islands": [], "largest_vertices": []}
    _, inverse, vertex_counts = np.unique(labels, return_inverse=True, return_counts=True)
    face_counts = np.zeros(len(vertex_counts), dtype=np.int64)
    if len(mesh.polygons):
        loops = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loops)
        starts = np.empty(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("loop_start", starts)
        face_counts = np.bincount(inverse[loops[starts]], minlength=len(vertex_counts))
    ranked = np.argsort(vertex_counts, kind="stable")
    limit = max(1, int(np.ceil(len(labels) * small_fraction)))
    by_vertices = ranked[vertex_counts[ranked] <= limit][:max_small]
    candidates = ranked[:max_small] if by_vertices.size >= max_small else ranked[:max_small * 4]
    if len(vertex_counts) == 1 and not by_vertices.size:
        candidates = ranked[:0]
    points = None
    if candidates.size:
        points = np.empty((len(labels), 3), dtype=np.float64)
        mesh.vertices.foreach_get("co", points.reshape(-1))
        if matrix_world is not None:
            transform = np.asarray([list(row) for row in matrix_world], dtype=np.float64)
            points = points @ transform[:3, :3].T + transform[:3, 3]
    whole_volume = float(np.prod(points.max(axis=0) - points.min(axis=0))) if points is not None else 0.0
    details = []
    for index in candidates:
        subset = points[inverse == index]
        lower, upper = subset.min(axis=0), subset.max(axis=0)
        volume_ratio = float(np.prod(upper - lower) / whole_volume) if whole_volume > 0 else None
        if vertex_counts[index] > limit and (volume_ratio is None or volume_ratio > small_fraction):
            continue
        centre = (lower + upper) * 0.5
        details.append({"vertices": int(vertex_counts[index]), "faces": int(face_counts[index]),
                        "focus": [float(value) for value in centre],
                        "focus_radius": max(float(np.linalg.norm(upper - lower)) * 0.25, 0.001),
                        "bbox_volume_ratio": volume_ratio})
        if len(details) >= max_small:
            break
    return {"islands": len(vertex_counts), "smallest_vertices": int(vertex_counts[ranked[0]]),
            "smallest_faces": int(face_counts[ranked[0]]), "small_islands": details,
            "largest_vertices": [int(size) for size in vertex_counts[ranked[-20:]][::-1]]}


def fragment_small_ratio(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not 0 < value <= 1:
        raise InvalidParamsError("fragment_small_ratio must be a finite number in (0, 1]")
    return float(value)


def new_islands(before, after, object_name):
    """Describe a rising island count without claiming vertex identity survived an edit."""
    increase = after["islands"] - before["islands"]
    if increase <= 0:
        return None
    return {"reason": "new_islands", "object": object_name,
            "before": before["islands"], "after": after["islands"],
            "increase": increase, "small_islands": after["small_islands"][:increase]}


def aggregate_components(summaries):
    """Aggregate changed-object island evidence without conflating object joining with splitting."""
    if any(summary is None for summary in summaries):
        return None
    small = [island for summary in summaries for island in summary["small_islands"]]
    small.sort(key=lambda island: island["vertices"])
    return {"islands": sum(summary["islands"] for summary in summaries),
            "small_islands": small[:16]}


def append_new_islands(transaction, before, mesh, obj, small_fraction):
    """Post-apply informational check: diagnosis cannot change mutation outcome."""
    if before is None:
        return
    try:
        after = component_summary(mesh, matrix_world=obj.matrix_world,
                                  small_fraction=small_fraction)
        append_new_island_summaries(transaction, before, after, obj.name)
    except Exception as exc:
        transaction["warnings"].append(f"fragment check unavailable for {obj.name}: {exc}")


def append_new_island_summaries(transaction, before, after, object_name):
    if before is None or after is None:
        return
    finding = new_islands(before, after, object_name)
    if finding:
        transaction.setdefault("suspicious_change", []).append(finding)
        transaction["warnings"].append(
            f"new_islands on {object_name}: {finding['before']} -> {finding['after']}; inspect small_islands focus"
        )


def capture_component_summary(transaction, mesh, obj, small_fraction):
    try:
        return component_summary(mesh, matrix_world=obj.matrix_world,
                                 small_fraction=small_fraction)
    except Exception as exc:
        transaction["warnings"].append(f"fragment check unavailable for {obj.name}: {exc}")
        return None


def _section_loops(mesh, verts, point, normal):
    """Trace contour cycles in the plane through a mesh without changing it."""
    from collections import defaultdict
    adjacency = defaultdict(set)
    for face in mesh.polygons:
        indices = list(face.vertices)
        crossings = []
        for i, a in enumerate(indices):
            b = indices[(i + 1) % len(indices)]
            da = (verts[a] - point).dot(normal)
            db = (verts[b] - point).dot(normal)
            if da * db < -1e-14:
                crossings.append(tuple(sorted((a, b))))
        if len(crossings) == 2:
            a, b = crossings
            adjacency[a].add(b)
            adjacency[b].add(a)
    remaining = set(adjacency)
    closed = 0
    open_paths = 0
    while remaining:
        root = remaining.pop()
        stack = [root]
        component = {root}
        while stack:
            for neighbor in adjacency[stack.pop()]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        if component and all(len(adjacency[key]) == 2 for key in component):
            closed += 1
        else:
            open_paths += 1
    return {"closed": closed, "open": open_paths, "crossing_edges": len(adjacency)}


def _graph_loop_metrics(obj, mesh, verts):
    raw = obj.get("cbt_body_graph")
    if not isinstance(raw, str):
        return None
    try:
        graph = json.loads(raw)
        joints = {joint["name"]: joint for joint in graph["joints"]}
    except (ValueError, TypeError, KeyError):
        return None
    from mathutils import Vector
    result = []
    for child in graph["joints"]:
        parent_name = child.get("parent")
        if parent_name is None or parent_name not in joints:
            continue
        parent = Vector(joints[parent_name]["position"])
        endpoint = Vector(child["position"])
        delta = endpoint - parent
        if delta.length < 1e-8:
            continue
        normal = delta.normalized()
        sections = []
        # Avoid exact joint/ring planes: a slice through existing vertices is
        # topologically ambiguous and would undercount closed contours.
        for fraction in (0.23, 0.51, 0.77):
            section = _section_loops(mesh, verts, parent.lerp(endpoint, fraction), normal)
            section["fraction"] = fraction
            sections.append(section)
        result.append({"segment": [parent_name, child["name"]], "sections": sections,
                       "closed_bend_zone_loops": sum(s["closed"] for s in sections)})
    return result


def _symmetry_metrics(verts, plane_x):
    if plane_x is None or not verts:
        return None
    from mathutils import Vector
    from mathutils.kdtree import KDTree
    kd = KDTree(len(verts))
    for index, point in enumerate(verts):
        kd.insert(point, index)
    kd.balance()
    distances = []
    pairs = []
    for index, point in enumerate(verts):
        mirrored = Vector((2 * plane_x - point.x, point.y, point.z))
        _, partner, distance = kd.find(mirrored)
        distances.append(distance)
        if index <= partner:
            pairs.append([index, partner])
    return {"plane_x": plane_x, "mean_asymmetry_m": sum(distances)/len(distances),
            "max_asymmetry_m": max(distances), "pair_count": len(pairs),
            "pairs_sample": pairs[:100]}


def topology_metrics(obj, *, triangle_budget=None, tiny_face_area=None, bend_zones=None, symmetry_plane_x=None):
    mesh = obj.data
    world = obj.matrix_world
    verts = [world @ vertex.co for vertex in mesh.vertices]
    counts = Counter(len(face.vertices) for face in mesh.polygons)
    uses = _edge_use(mesh)
    valence = Counter(index for edge in mesh.edges for index in edge.vertices)
    areas = sorted(float(face.area) for face in mesh.polygons)
    edge_ratios = []
    for face in mesh.polygons:
        indices = list(face.vertices)
        lengths = [(verts[indices[(i+1) % len(indices)]] - verts[indices[i]]).length
                   for i in range(len(indices))]
        if lengths and max(lengths) > 0:
            edge_ratios.append(min(lengths) / max(lengths))
    edge_ratios.sort()
    if tiny_face_area is None:
        total = sum(areas)
        tiny_face_area = total * 1e-8 if total else 0.0
    tiny = [face.index for face in mesh.polygons if face.area <= tiny_face_area]
    degenerate = [face.index for face in mesh.polygons if face.area <= 1e-16]
    poles = []
    zones = bend_zones or []
    for index, degree in sorted(valence.items()):
        if degree == 4:
            continue
        point = verts[index]
        zone = next((z.get("name") for z in zones if (point - z["point"]).length <= z["radius"]), None)
        poles.append({"vertex": index, "valence": degree, "position": list(point), "bend_zone": zone})
    boundary = sum(len(faces) == 1 for faces in uses.values())
    non_manifold = sum(len(faces) != 2 for faces in uses.values())
    triangles = sum(max(0, count - 2) * amount for count, amount in counts.items())
    component_info = component_summary(mesh, matrix_world=world, small_fraction=1, max_small=5)
    result = {
        "object": obj.name,
        "vertices": len(mesh.vertices), "edges": len(mesh.edges), "faces": len(mesh.polygons),
        "quads": counts[4], "triangle_faces": counts[3],
        "ngons": sum(amount for count, amount in counts.items() if count > 4),
        "triangles": triangles, "triangle_budget": triangle_budget,
        "within_budget": None if triangle_budget is None else triangles <= triangle_budget,
        "boundary_edges": boundary, "non_manifold_edges": non_manifold,
        "islands": component_info["islands"],
        "island_vertices": component_info["largest_vertices"],
        "smallest_islands": [
            {**part, "focus": {"point": part["focus"], "radius": part["focus_radius"]}}
            for part in component_info["small_islands"]
        ],
        "poles": poles[:200], "pole_count": len(poles),
        "poles_in_bend_zones": [pole for pole in poles if pole["bend_zone"] is not None][:100],
        "tiny_face_area_threshold": tiny_face_area, "tiny_face_count": len(tiny), "tiny_face_indices": tiny[:100],
        "degenerate_face_count": len(degenerate), "degenerate_face_indices": degenerate[:100],
        "face_area": {"min": areas[0] if areas else None,
                      "median": areas[len(areas)//2] if areas else None,
                      "max": areas[-1] if areas else None},
        "edge_ratio": {"min": edge_ratios[0] if edge_ratios else None,
                       "median": edge_ratios[len(edge_ratios)//2] if edge_ratios else None,
                       "max": edge_ratios[-1] if edge_ratios else None},
        "connectivity_hash": connectivity_hash(mesh),
        "loop_traces": _graph_loop_metrics(obj, mesh, verts),
        "symmetry": _symmetry_metrics(verts, symmetry_plane_x),
    }
    if verts:
        result["focus"] = list(verts[0])
    return result


def topology_report(params: dict) -> ToolResult:
    bpy = get_bpy()
    name = params.get("object_name")
    obj = bpy.data.objects.get(name) if isinstance(name, str) else None
    if obj is None or obj.type != "MESH":
        raise NotFoundError(f"Mesh object not found: {name}")
    budget = params.get("triangle_budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
        raise InvalidParamsError("triangle_budget must be a positive integer")
    tiny = params.get("tiny_face_area")
    if tiny is not None and (isinstance(tiny, bool) or not isinstance(tiny, (float, int)) or not isfinite(tiny) or tiny < 0):
        raise InvalidParamsError("tiny_face_area must be a non-negative finite number")
    zones = []
    raw_zones = params.get("bend_zones", [])
    if not isinstance(raw_zones, list):
        raise InvalidParamsError("bend_zones must be a list")
    from mathutils import Vector
    for zone in raw_zones:
        if not isinstance(zone, dict) or not isinstance(zone.get("name"), str):
            raise InvalidParamsError("each bend zone needs a name, point, and radius")
        point = zone.get("point")
        radius = zone.get("radius")
        if not isinstance(point, list) or len(point) != 3 or not all(isinstance(x, (int, float)) and isfinite(x) for x in point) or not isinstance(radius, (int, float)) or not isfinite(radius) or radius <= 0:
            raise InvalidParamsError("bend zone point/radius is invalid")
        zones.append({"name": zone["name"], "point": Vector(point), "radius": float(radius)})
    symmetry = params.get("symmetry_plane_x")
    if symmetry is not None and (isinstance(symmetry, bool) or not isinstance(symmetry, (int, float)) or not isfinite(symmetry)):
        raise InvalidParamsError("symmetry_plane_x must be a finite number")
    if not zones:
        raw_graph = obj.get("cbt_body_graph")
        if isinstance(raw_graph, str):
            try:
                graph = json.loads(raw_graph)
                zones = [{"name": j["name"], "point": Vector(j["position"]), "radius": float(j["radius"]) * 1.5}
                         for j in graph["joints"] if j.get("parent")]
            except (ValueError, TypeError, KeyError):
                zones = []
    report = topology_metrics(obj, triangle_budget=budget, tiny_face_area=tiny, bend_zones=zones,
                              symmetry_plane_x=symmetry)
    warnings = []
    if report["ngons"]:
        warnings.append("Mesh contains n-gons; triangulate before game export")
    if report["within_budget"] is False:
        warnings.append("Mesh exceeds triangle budget")
    if report["poles_in_bend_zones"]:
        warnings.append("Poles occur in bend zones")
    return ToolResult(data=report, warnings=warnings)
