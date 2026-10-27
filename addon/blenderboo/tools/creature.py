"""Body-graph planning, quad base generation, and fixed-topology surface fitting."""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .mesh_topology import connectivity_hash, topology_metrics
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


_PLAN_DIR = Path(__file__).resolve().parent.parent / "data" / "body_plans"
_GRAPH_KEY = "cbt_body_graph"


def _point(raw, name):
    if not isinstance(raw, (list, tuple)) or len(raw) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in raw):
        raise InvalidParamsError(f"{name} must be three finite numbers")
    return [float(value) for value in raw]


def _graph(value):
    if not isinstance(value, dict) or not isinstance(value.get("joints"), list) or len(value["joints"]) < 2:
        raise InvalidParamsError("graph.joints must contain at least two joints")
    if len(value["joints"]) > 256:
        raise InvalidParamsError("graph has more than 256 joints")
    joints = []
    names = set()
    for index, raw in enumerate(value["joints"]):
        if not isinstance(raw, dict):
            raise InvalidParamsError(f"joint {index} must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise InvalidParamsError(f"joint {index} needs a unique non-empty name")
        names.add(name)
        radius = raw.get("radius")
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not isfinite(radius) or radius <= 0:
            raise InvalidParamsError(f"joint {name} radius must be positive")
        parent = raw.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise InvalidParamsError(f"joint {name} parent must be a name or null")
        symmetry = raw.get("symmetry_pair")
        if symmetry is not None and not isinstance(symmetry, str):
            raise InvalidParamsError(f"joint {name} symmetry_pair must be a name or null")
        loops = []
        for key, default, low, high in (("loops_around", 8, 4, 64),
                                        ("loops_along", 2, 1, 32),
                                        ("joint_extra_loops", 1, 0, 8)):
            count = raw.get(key, default)
            if isinstance(count, bool) or not isinstance(count, int) or not low <= count <= high:
                raise InvalidParamsError(f"joint {name} {key} must be {low}..{high}")
            loops.append(count)
        joints.append({"name": name, "position": _point(raw.get("position"), f"joint {name} position"),
                       "radius": float(radius), "parent": parent, "symmetry_pair": symmetry,
                       "loops_around": loops[0], "loops_along": loops[1], "joint_extra_loops": loops[2]})
    by_name = {joint["name"]: joint for joint in joints}
    roots = [joint for joint in joints if joint["parent"] is None]
    if len(roots) != 1:
        raise InvalidParamsError("graph must have exactly one root")
    for joint in joints:
        parent = joint["parent"]
        if parent is not None and (parent not in by_name or parent == joint["name"]):
            raise InvalidParamsError(f"joint {joint['name']} has invalid parent")
        pair = joint["symmetry_pair"]
        if pair is not None and (pair not in by_name or by_name[pair]["symmetry_pair"] != joint["name"]):
            raise InvalidParamsError(f"joint {joint['name']} has non-reciprocal symmetry_pair")
        seen = {joint["name"]}
        cursor = parent
        while cursor is not None:
            if cursor in seen:
                raise InvalidParamsError("graph contains a parent cycle")
            seen.add(cursor)
            cursor = by_name[cursor]["parent"]
    parts = value.get("separate_parts", [])
    if not isinstance(parts, list) or any(not isinstance(p, dict) or not isinstance(p.get("name"), str) or not isinstance(p.get("parent_joint"), str) or p["parent_joint"] not in by_name for p in parts):
        raise InvalidParamsError("separate_parts must name a part and parent_joint")
    body_plan = value.get("body_plan", "custom")
    if not isinstance(body_plan, str) or not body_plan.strip():
        raise InvalidParamsError("graph.body_plan must be a non-empty string")
    return {"body_plan": body_plan, "joints": joints, "separate_parts": parts}


def _graph_from_object(obj):
    raw = obj.get(_GRAPH_KEY)
    if not isinstance(raw, str):
        raise InvalidStateError(f"{obj.name} has no body graph")
    try:
        return _graph(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise InvalidStateError(f"Stored body graph is invalid: {exc}") from exc


def graph_set(params: dict) -> ToolResult:
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    require_document_write_access(bpy)
    name = params.get("object_name")
    obj = bpy.data.objects.get(name) if isinstance(name, str) else None
    if obj is None:
        raise NotFoundError(f"Object not found: {name}")
    graph = _graph(params.get("graph"))
    encoded = json.dumps(graph, sort_keys=True, separators=(",", ":"))
    obj[_GRAPH_KEY] = encoded
    bump_epoch()
    return ToolResult(data={"object": obj.name, "graph_hash": sha256(encoded.encode()).hexdigest(),
                            "joints": len(graph["joints"]), "changed": True,
                            "state_token": current_state_token(bpy)})


def graph_inspect(params: dict) -> ToolResult:
    bpy = get_bpy()
    name = params.get("object_name")
    obj = bpy.data.objects.get(name) if isinstance(name, str) else None
    if obj is None:
        raise NotFoundError(f"Object not found: {name}")
    graph = _graph_from_object(obj)
    return ToolResult(data={"object": obj.name, "graph": graph,
                            "graph_hash": sha256(obj[_GRAPH_KEY].encode()).hexdigest(),
                            "focus": graph["joints"][0]["position"]})


def graph_suggest(params: dict) -> ToolResult:
    bpy = get_bpy()
    plan_name = params.get("body_plan")
    if plan_name not in {"humanoid", "quadruped", "reptile"}:
        raise InvalidParamsError("body_plan must be humanoid, quadruped, or reptile")
    plan = json.loads((_PLAN_DIR / f"{plan_name}.json").read_text(encoding="utf-8"))
    landmarks = {}
    for obj in bpy.context.scene.objects:
        label = obj.get("cbt_landmark")
        if obj.type == "EMPTY" and isinstance(label, str):
            landmarks[label] = list(obj.matrix_world.translation)
    proxy_name = params.get("proxy_name")
    proxy = bpy.data.objects.get(proxy_name) if isinstance(proxy_name, str) else None
    if proxy is None or proxy.type != "MESH":
        raise NotFoundError(f"Proxy mesh not found: {proxy_name}")
    bounds = [proxy.matrix_world @ vertex.co for vertex in proxy.data.vertices]
    if not bounds:
        raise InvalidStateError("Proxy has no vertices")
    center = [(min(p[axis] for p in bounds) + max(p[axis] for p in bounds)) / 2 for axis in range(3)]
    height = max(p[2] for p in bounds) - min(p[2] for p in bounds)
    radius = max(height * 0.04, 1e-4)
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    proxy.data.calc_loop_triangles()
    proxy_triangles = [tuple(tri.vertices) for tri in proxy.data.loop_triangles]
    tree = BVHTree.FromPolygons(bounds, proxy_triangles, all_triangles=True) if proxy_triangles else None
    def measured_radius(position, fallback):
        if tree is None:
            return fallback, "default"
        center_point = Vector(position)
        hits = []
        for direction in (Vector((1, 0, 0)), Vector((-1, 0, 0)),
                          Vector((0, 1, 0)), Vector((0, -1, 0)),
                          Vector((0, 0, 1)), Vector((0, 0, -1))):
            hit = tree.ray_cast(center_point, direction, max(height * 2, 1.0))
            if hit[0] is not None and hit[3] > 1e-5:
                hits.append(hit[3])
        if len(hits) >= 2:
            hits.sort()
            return max(1e-4, sum(hits[:min(4, len(hits))]) / min(4, len(hits))), "ray_cast"
        return fallback, "default"
    joints = []
    for definition in plan["joints"]:
        label = definition["name"]
        marked = landmarks.get(label)
        if marked is not None:
            position = marked
            confidence = 1.0
        else:
            normalized = definition["default_position"]
            extents = [max(p[axis] for p in bounds) - min(p[axis] for p in bounds) for axis in range(3)]
            position = [center[axis] + normalized[axis] * extents[axis] for axis in range(3)]
            confidence = 0.25
        pair = definition.get("symmetry_pair")
        if marked is None and pair in landmarks:
            mirrored = landmarks[pair]
            position = [2 * center[0] - mirrored[0], mirrored[1], mirrored[2]]
            confidence = 0.6
        joint_radius, radius_evidence = measured_radius(position, radius * definition.get("radius_factor", 1))
        joints.append({"name": label, "parent": definition.get("parent"), "position": position,
                       "radius": joint_radius,
                       "symmetry_pair": definition.get("symmetry_pair"),
                       "confidence": confidence, "evidence": "landmark" if marked else "mirrored_landmark" if confidence == 0.6 else "body_plan_default",
                       "radius_evidence": radius_evidence})
    proposal = _graph({"body_plan": plan_name, "joints": joints, "separate_parts": []})
    for joint, source in zip(proposal["joints"], joints):
        joint["confidence"] = source["confidence"]
        joint["evidence"] = source["evidence"]
        joint["radius_evidence"] = source["radius_evidence"]
    warnings = [f"{sum(j['confidence'] < 1 for j in joints)} joints use low-confidence defaults; place landmark Empties and review before graph_set"] if any(j["confidence"] < 1 for j in joints) else []
    return ToolResult(data={"proposal": proposal, "landmarks_found": sorted(landmarks),
                            "applied": False, "focus": center}, warnings=warnings)


def landmark_set(params: dict) -> ToolResult:
    """Place or move a named landmark Empty; no shape mesh is edited."""
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    require_document_write_access(bpy)
    label = params.get("name")
    if not isinstance(label, str) or not label.strip():
        raise InvalidParamsError("name must be a non-empty landmark name")
    position = _point(params.get("position"), "position")
    object_name = params.get("object_name") or f"Landmark.{label}"
    if not isinstance(object_name, str) or not object_name.strip():
        raise InvalidParamsError("object_name must be a non-empty string")
    obj = bpy.data.objects.get(object_name)
    if obj is not None and (obj.type != "EMPTY" or obj.get("cbt_landmark") not in (None, label)):
        raise InvalidParamsError("object_name already identifies an unrelated object")
    if obj is None:
        obj = bpy.data.objects.new(object_name, None)
        bpy.context.scene.collection.objects.link(obj)
        obj.empty_display_type = "SPHERE"
        obj.empty_display_size = 0.05
    obj.location = position
    obj["cbt_landmark"] = label
    bump_epoch()
    return ToolResult(data={"object": obj.name, "landmark": label, "position": position,
                            "changed": True, "state_token": current_state_token(bpy), "focus": position})


def _selected_context(bpy, obj):
    previous_active = bpy.context.view_layer.objects.active
    previous_selected = list(bpy.context.selected_objects)
    for selected in previous_selected:
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return previous_active, previous_selected


def _restore_context(bpy, previous):
    for selected in bpy.context.selected_objects:
        selected.select_set(False)
    active, selected = previous
    for obj in selected:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    if active is not None and active.name in bpy.data.objects:
        bpy.context.view_layer.objects.active = active


def _similarity_transform(source_points, target_points):
    """Least-squares similarity transform in world coordinates (Umeyama)."""
    import numpy as np
    source = np.asarray(source_points, dtype=float)
    target = np.asarray(target_points, dtype=float)
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3 or len(source) < 3:
        raise InvalidParamsError("landmark_pairs needs at least three 3D pairs")
    a = source - source.mean(axis=0)
    b = target - target.mean(axis=0)
    if np.linalg.matrix_rank(a) < 2:
        raise InvalidParamsError("landmark_pairs source positions are degenerate")
    u, singular, vh = np.linalg.svd(b.T @ a / len(source))
    reflect = np.eye(3)
    if np.linalg.det(u @ vh) < 0:
        reflect[-1, -1] = -1
    rotation = u @ reflect @ vh
    variance = np.sum(a * a) / len(source)
    scale = float(np.trace(np.diag(singular) @ reflect) / variance)
    translation = target.mean(axis=0) - scale * rotation @ source.mean(axis=0)
    return scale, rotation, translation


def _chain_surface(graph):
    """Build indexed quad tube for a nonbranching graph, with triangle transitions."""
    from math import cos, pi, sin
    from mathutils import Vector
    by_name = {joint["name"]: joint for joint in graph["joints"]}
    children = {name: [] for name in by_name}
    for joint in graph["joints"]:
        if joint["parent"] is not None:
            children[joint["parent"]].append(joint["name"])
    if any(len(names) > 1 for names in children.values()):
        return None
    root = next(j["name"] for j in graph["joints"] if j["parent"] is None)
    chain = [by_name[root]]
    while children[chain[-1]["name"]]:
        chain.append(by_name[children[chain[-1]["name"]][0]])
    points = [Vector(j["position"]) for j in chain]
    rings = []
    ring_regions = []
    for i, joint in enumerate(chain):
        if i == 0:
            rings.append((points[i], joint["radius"], joint["loops_around"]))
            ring_regions.append(joint["name"])
            continue
        parent = chain[i-1]
        a, b = points[i-1], points[i]
        steps = joint["loops_along"] + joint["joint_extra_loops"] * 2
        for step in range(1, steps + 1):
            t = step / steps
            rings.append((a.lerp(b, t), parent["radius"] * (1-t) + joint["radius"] * t,
                          joint["loops_around"]))
            ring_regions.append(joint["name"])
    if len(rings) < 2:
        raise InvalidParamsError("Body graph chain needs at least two rings")
    vertices = []
    ring_indices = []
    tangent_prev = None
    u = None
    for i, (point, radius, around) in enumerate(rings):
        tangent = (rings[min(i+1, len(rings)-1)][0] - rings[max(i-1, 0)][0]).normalized()
        if tangent_prev is None:
            reference = Vector((1, 0, 0)) if abs(tangent.x) < 0.9 else Vector((0, 1, 0))
            u = (reference - tangent * reference.dot(tangent)).normalized()
        else:
            u = tangent_prev.rotation_difference(tangent) @ u
            u = (u - tangent * u.dot(tangent)).normalized()
        v = tangent.cross(u)
        indices = []
        for k in range(around):
            theta = 2 * pi * k / around
            indices.append(len(vertices))
            vertices.append(tuple(point + radius * (u * cos(theta) + v * sin(theta))))
        ring_indices.append(indices)
        tangent_prev = tangent
    faces = []
    for lower, upper in zip(ring_indices, ring_indices[1:]):
        a_count, b_count = len(lower), len(upper)
        a = b = 0
        while a < a_count or b < b_count:
            next_a = (a+1)/a_count if a < a_count else float("inf")
            next_b = (b+1)/b_count if b < b_count else float("inf")
            if abs(next_a - next_b) < 1e-9:
                faces.append((lower[a % a_count], lower[(a+1) % a_count],
                              upper[(b+1) % b_count], upper[b % b_count]))
                a += 1
                b += 1
            elif next_a < next_b:
                faces.append((lower[a % a_count], lower[(a+1) % a_count], upper[b % b_count]))
                a += 1
            else:
                faces.append((lower[a % a_count], upper[(b+1) % b_count], upper[b % b_count]))
                b += 1
    start = len(vertices)
    vertices.append(tuple(rings[0][0]))
    end = len(vertices)
    vertices.append(tuple(rings[-1][0]))
    for k, ring in enumerate((ring_indices[0], ring_indices[-1])):
        for i in range(len(ring)):
            if k == 0:
                faces.append((start, ring[(i+1) % len(ring)], ring[i]))
            else:
                faces.append((end, ring[i], ring[(i+1) % len(ring)]))
    return vertices, faces, ring_indices, ring_regions


def base_from_graph(params: dict) -> ToolResult:
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    require_document_write_access(bpy)
    graph_name = params.get("graph_object")
    holder = bpy.data.objects.get(graph_name) if isinstance(graph_name, str) else None
    if holder is None:
        raise NotFoundError(f"Graph object not found: {graph_name}")
    graph = _graph_from_object(holder)
    name = params.get("output_name") or f"{holder.name}_base"
    if not isinstance(name, str) or not name.strip():
        raise InvalidParamsError("output_name must be a non-empty string")
    chain = _chain_surface(graph)
    if chain is not None:
        vertices, faces, rings, regions = chain
        mesh = bpy.data.meshes.new(f"{name}_mesh")
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(obj)
        try:
            groups = {}
            for ring, region in zip(rings, regions):
                if region not in groups:
                    groups[region] = obj.vertex_groups.new(name=f"region_{region}")
                groups[region].add(ring, 1.0, "REPLACE")
            obj[_GRAPH_KEY] = holder[_GRAPH_KEY]
            report = topology_metrics(obj)
            if report["non_manifold_edges"]:
                raise InvalidStateError("Generated chain base is non-manifold")
            loop_counts = {j["name"]: j["loops_around"] for j in graph["joints"]}
            bump_epoch()
            return ToolResult(data={"object": obj.name, "graph_object": holder.name,
                                    "topology": report, "joint_loops": loop_counts,
                                    "generation": "explicit_quad_rings", "changed": True,
                                    "state_token": current_state_token(bpy), "focus": report.get("focus")})
        except Exception:
            bpy.data.objects.remove(obj, do_unlink=True)
            raise
    around_values = {j["loops_around"] for j in graph["joints"]}
    if len(around_values) != 1 or next(iter(around_values)) not in {4, 8, 16, 32, 64}:
        raise InvalidParamsError("Branching Skin base currently requires one shared loops_around value of 4, 8, 16, 32, or 64; use a template or split branches otherwise")
    from mathutils import Vector
    joints = {j["name"]: j for j in graph["joints"]}
    vertices = [tuple(j["position"]) for j in graph["joints"]]
    vertex_names = [j["name"] for j in graph["joints"]]
    edges = []
    for child in graph["joints"]:
        if child["parent"] is None:
            continue
        parent = joints[child["parent"]]
        start = Vector(parent["position"])
        end = Vector(child["position"])
        if (end - start).length <= 1e-6:
            raise InvalidParamsError(f"Zero-length segment: {parent['name']} -> {child['name']}")
        last = vertex_names.index(parent["name"])
        steps = child["loops_along"] + child["joint_extra_loops"]
        for step in range(1, steps):
            t = step / steps
            vertices.append(tuple(start.lerp(end, t)))
            vertex_names.append(f"{parent['name']}->{child['name']}:{step}")
            next_index = len(vertices) - 1
            edges.append((last, next_index))
            last = next_index
        edges.append((last, vertex_names.index(child["name"])))
    mesh = bpy.data.meshes.new(f"{holder.name}_base_mesh")
    mesh.from_pydata(vertices, edges, [])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    previous = _selected_context(bpy, obj)
    try:
        skin = obj.modifiers.new("Body graph skin", "SKIN")
        layer = mesh.skin_vertices[0].data
        for index, label in enumerate(vertex_names):
            if label in joints:
                r = joints[label]["radius"]
            else:
                parent_name, rest = label.split("->", 1)
                child_name, step = rest.rsplit(":", 1)
                child = joints[child_name]
                t = int(step) / (child["loops_along"] + child["joint_extra_loops"])
                r = joints[parent_name]["radius"] * (1 - t) + child["radius"] * t
            layer[index].radius = (r, r)
        root_name = next(j["name"] for j in graph["joints"] if j["parent"] is None)
        layer[vertex_names.index(root_name)].use_root = True
        bpy.ops.object.modifier_apply(modifier=skin.name)
        levels = {4: 0, 8: 1, 16: 2, 32: 3, 64: 4}[next(iter(around_values))]
        if levels:
            subdivision = obj.modifiers.new("Circumference loops", "SUBSURF")
            subdivision.subdivision_type = "SIMPLE"
            subdivision.levels = levels
            bpy.ops.object.modifier_apply(modifier=subdivision.name)
    except Exception:
        bpy.data.objects.remove(obj, do_unlink=True)
        raise
    finally:
        _restore_context(bpy, previous)
    mesh = obj.data
    # Region groups are nearest graph segments in world space.
    segments = [(p["name"], child["name"], Vector(p["position"]), Vector(child["position"]))
                for child in graph["joints"] if child["parent"] is not None
                for p in [joints[child["parent"]]]]
    groups = {b: obj.vertex_groups.new(name=f"region_{a}_{b}") for a, b, _, _ in segments}
    for vertex in mesh.vertices:
        point = obj.matrix_world @ vertex.co
        def distance(segment):
            _, _, a, b = segment
            t = max(0, min(1, (point - a).dot(b - a) / (b - a).length_squared))
            return (point - a.lerp(b, t)).length_squared
        region = min(segments, key=distance)[1]
        groups[region].add([vertex.index], 1.0, "REPLACE")
    obj[_GRAPH_KEY] = holder[_GRAPH_KEY]
    report = topology_metrics(obj)
    bump_epoch()
    return ToolResult(data={"object": obj.name, "graph_object": holder.name,
                            "topology": report, "joint_loops": {j["name"]: next(iter(around_values)) for j in graph["joints"]},
                            "generation": "skin_branch_uniform_circumference",
                            "changed": True, "state_token": current_state_token(bpy), "focus": report.get("focus")},
                      warnings=["Skin-based base mesh is quad-dominant; inspect poles and bend-zone loops before fitting"])


def wrap_to_target(params: dict) -> ToolResult:
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    require_document_write_access(bpy)
    source_name = params.get("object_name")
    target_name = params.get("target_name")
    source = bpy.data.objects.get(source_name) if isinstance(source_name, str) else None
    target = bpy.data.objects.get(target_name) if isinstance(target_name, str) else None
    if source is None or source.type != "MESH" or target is None or target.type != "MESH" or source == target:
        raise InvalidParamsError("object_name and target_name must identify distinct meshes")
    if not source.data.vertices or not source.data.polygons:
        raise InvalidStateError("Fitting source needs a non-empty mesh surface")
    output_name = params.get("output_name") or f"{source.name}_fitted"
    if not isinstance(output_name, str) or not output_name.strip():
        raise InvalidParamsError("output_name must be a non-empty string")
    iterations = params.get("iterations", 8)
    strength = params.get("projection_strength", 0.65)
    smooth = params.get("smooth_strength", 0.15)
    if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= 100:
        raise InvalidParamsError("iterations must be 1..100")
    for value, label in ((strength, "projection_strength"), (smooth, "smooth_strength")):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value) or not 0 <= value <= 1:
            raise InvalidParamsError(f"{label} must be 0..1")
    raw_pairs = params.get("landmark_pairs", [])
    if not isinstance(raw_pairs, list) or (raw_pairs and len(raw_pairs) < 3):
        raise InvalidParamsError("landmark_pairs must be empty or contain at least three pairs")
    pairs = []
    for index, pair in enumerate(raw_pairs):
        if not isinstance(pair, dict):
            raise InvalidParamsError(f"landmark_pairs[{index}] must be an object")
        pairs.append((_point(pair.get("source"), f"landmark_pairs[{index}].source"),
                      _point(pair.get("target"), f"landmark_pairs[{index}].target")))
    stiffness = params.get("region_stiffness", {})
    if not isinstance(stiffness, dict) or any(not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or not 0 <= v <= 1 for k, v in stiffness.items()):
        raise InvalidParamsError("region_stiffness must map vertex-group names to 0..1")
    symmetry = params.get("symmetry_plane_x")
    if symmetry is not None and (isinstance(symmetry, bool) or not isinstance(symmetry, (int, float)) or not isfinite(symmetry)):
        raise InvalidParamsError("symmetry_plane_x must be a finite number")
    coverage_tolerance = params.get("coverage_tolerance", 0.05)
    if isinstance(coverage_tolerance, bool) or not isinstance(coverage_tolerance, (int, float)) or not isfinite(coverage_tolerance) or coverage_tolerance <= 0:
        raise InvalidParamsError("coverage_tolerance must be a positive distance in metres")
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
    target_mesh = evaluated.to_mesh()
    try:
        target_mesh.calc_loop_triangles()
        target_vertices = [evaluated.matrix_world @ vertex.co for vertex in target_mesh.vertices]
        target_triangles = [tuple(tri.vertices) for tri in target_mesh.loop_triangles]
        if not target_triangles:
            raise InvalidStateError("Target has no surface triangles")
        tree = BVHTree.FromPolygons(target_vertices, target_triangles, all_triangles=True)
        before_hash = connectivity_hash(source.data)
        copy = source.copy()
        copy.data = source.data.copy()
        copy.name = output_name
        bpy.context.scene.collection.objects.link(copy)
        try:
            inverse = copy.matrix_world.inverted_safe()
            points = [copy.matrix_world @ vertex.co for vertex in copy.data.vertices]
            original_points = [point.copy() for point in points]
            if pairs:
                import numpy as np
                scale, rotation, translation = _similarity_transform([p[0] for p in pairs], [p[1] for p in pairs])
                points = [Vector(scale * rotation @ np.asarray(point, dtype=float) + translation) for point in points]
                # Gaussian landmark offsets provide a bounded non-rigid start.
                aligned_landmarks = [Vector(scale * rotation @ np.asarray(src, dtype=float) + translation) for src, _ in pairs]
                residual_vectors = [Vector(dst) - aligned for (_, dst), aligned in zip(pairs, aligned_landmarks)]
                span = max((a - b).length for a in aligned_landmarks for b in aligned_landmarks)
                sigma2 = max((span * 0.25) ** 2, 1e-8)
                for i, point in enumerate(points):
                    weights = [__import__("math").exp(-(point - landmark).length_squared / (2 * sigma2)) for landmark in aligned_landmarks]
                    total = sum(weights)
                    if total > 1e-8:
                        points[i] = point + sum((offset * weight for offset, weight in zip(residual_vectors, weights)), Vector((0, 0, 0))) / total
            neighbors = [[] for _ in points]
            for edge in copy.data.edges:
                a, b = edge.vertices
                neighbors[a].append(b)
                neighbors[b].append(a)
            vertex_stiffness = [0.0] * len(points)
            for vertex in copy.data.vertices:
                values = []
                for membership in vertex.groups:
                    group = copy.vertex_groups[membership.group]
                    if group.name in stiffness:
                        values.append(float(stiffness[group.name]) * membership.weight)
                vertex_stiffness[vertex.index] = min(1.0, max(values, default=0.0))
            symmetry_pairs = []
            if symmetry is not None:
                from mathutils.kdtree import KDTree
                kd = KDTree(len(points))
                for index, point in enumerate(points):
                    kd.insert(point, index)
                kd.balance()
                tolerance = max((max(p.x for p in points) - min(p.x for p in points)) * 0.01, 1e-5)
                seen = set()
                for index, point in enumerate(points):
                    mirror = Vector((2 * symmetry - point.x, point.y, point.z))
                    _, partner, distance = kd.find(mirror)
                    if distance <= tolerance and index != partner and index not in seen and partner not in seen:
                        symmetry_pairs.append((index, partner))
                        seen.update((index, partner))
            for _ in range(iterations):
                updated = []
                for index, point in enumerate(points):
                    location, _, _, _ = tree.find_nearest(point)
                    local_strength = strength * (1 - vertex_stiffness[index])
                    projected = point.lerp(location, local_strength) if location is not None else point
                    if neighbors[index]:
                        average = sum((points[n] for n in neighbors[index]), Vector((0, 0, 0))) / len(neighbors[index])
                        projected = projected.lerp(average, smooth * (1 - vertex_stiffness[index]))
                    updated.append(projected)
                for a, b in symmetry_pairs:
                    left, right = updated[a], updated[b]
                    average_x = (abs(left.x - symmetry) + abs(right.x - symmetry)) / 2
                    side = 1 if original_points[a].x >= symmetry else -1
                    shared_y = (left.y + right.y) / 2
                    shared_z = (left.z + right.z) / 2
                    updated[a] = Vector((symmetry + side * average_x, shared_y, shared_z))
                    updated[b] = Vector((symmetry - side * average_x, shared_y, shared_z))
                points = updated
            # End on the actual surface after Laplacian relaxation.
            points = [hit if hit is not None else point
                      for point in points for hit in [tree.find_nearest(point)[0]]]
            distances = []
            for index, point in enumerate(points):
                copy.data.vertices[index].co = inverse @ point
                location, _, _, distance = tree.find_nearest(point)
                if location is not None:
                    distances.append(distance)
            copy.data.update()
            after_hash = connectivity_hash(copy.data)
            if before_hash != after_hash:
                raise InvalidStateError("Connectivity changed while fitting")
            # Reverse coverage exposes target features not represented by the base.
            fitted_vertices = [copy.matrix_world @ vertex.co for vertex in copy.data.vertices]
            copy.data.calc_loop_triangles()
            fitted_tree = BVHTree.FromPolygons(fitted_vertices, [tuple(tri.vertices) for tri in copy.data.loop_triangles], all_triangles=True)
            reverse = [(fitted_tree.find_nearest(point) or (None, None, None, None))[3] for point in target_vertices]
            reverse = [value for value in reverse if value is not None]
            coverage = sum(distance <= coverage_tolerance for distance in reverse) / len(reverse) if reverse else None
            worst_target = sorted(enumerate(reverse), key=lambda pair: pair[1], reverse=True)[:10]
            worst = sorted(enumerate(distances), key=lambda pair: pair[1], reverse=True)[:10]
            report = topology_metrics(copy)
            landmark_residuals = []
            if pairs:
                for source_landmark, target_landmark in pairs:
                    original = Vector(source_landmark)
                    nearest_index = min(range(len(original_points)), key=lambda i: (original_points[i] - original).length_squared)
                    landmark_residuals.append({"source": source_landmark, "target": target_landmark,
                                               "fitted_vertex": nearest_index,
                                               "residual_m": (points[nearest_index] - Vector(target_landmark)).length})
            flipped = 0
            collapsed = 0
            for face in copy.data.polygons:
                indices = list(face.vertices)
                if len(indices) < 3:
                    continue
                a, b, c = indices[:3]
                before_normal = (original_points[b] - original_points[a]).cross(original_points[c] - original_points[a])
                after_normal = (points[b] - points[a]).cross(points[c] - points[a])
                if after_normal.length <= 1e-12:
                    collapsed += 1
                elif before_normal.dot(after_normal) < 0:
                    flipped += 1
            bump_epoch()
            return ToolResult(data={"object": copy.name, "target": target.name, "changed": True,
                                    "state_token": current_state_token(bpy),
                                    "topology_proof": {"before": before_hash, "after": after_hash, "unchanged": True},
                                    "distance_fitted_to_target": {"mean": sum(distances)/len(distances), "max": max(distances)},
                                    "distance_target_to_fitted": {"mean": sum(reverse)/len(reverse) if reverse else None,
                                                                  "max": max(reverse) if reverse else None},
                                    "coverage": {"tolerance_m": float(coverage_tolerance),
                                                 "fraction": coverage,
                                                 "worst_target_vertices": [{"vertex": i, "distance": d,
                                                                             "focus": list(target_vertices[i])}
                                                                            for i, d in worst_target]},
                                    "landmark_residuals": landmark_residuals,
                                    "flipped_faces": flipped, "collapsed_faces": collapsed,
                                    "face_quality_change": {"before": topology_metrics(source)["face_area"],
                                                            "after": report["face_area"]},
                                    "worst_vertices": [{"vertex": i, "distance": d, "focus": list(points[i])} for i, d in worst],
                                    "topology": report, "focus": list(points[worst[0][0]]) if worst else None})
        except Exception:
            bpy.data.objects.remove(copy, do_unlink=True)
            raise
    finally:
        evaluated.to_mesh_clear()
