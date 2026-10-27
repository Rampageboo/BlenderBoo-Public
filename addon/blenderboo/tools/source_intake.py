"""Inspect immutable shape sources and build disposable watertight projection proxies."""

from __future__ import annotations

from math import isfinite

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .mesh_topology import _components, _edge_use, topology_metrics
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


def _mesh_object(bpy, name):
    obj = bpy.data.objects.get(name) if isinstance(name, str) else None
    if obj is None or obj.type != "MESH":
        raise NotFoundError(f"Mesh object not found: {name}")
    return obj


def _bounds(points):
    return [[min(v[axis] for v in points) for axis in range(3)],
            [max(v[axis] for v in points) for axis in range(3)]]


def _self_intersection_count(obj, limit=100000):
    from mathutils.bvhtree import BVHTree
    mesh = obj.data
    mesh.calc_loop_triangles()
    if len(mesh.loop_triangles) > limit:
        return None
    vertices = [obj.matrix_world @ vert.co for vert in mesh.vertices]
    triangles = [tuple(tri.vertices) for tri in mesh.loop_triangles]
    if not triangles:
        return 0
    tree = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
    return sum(a < b and not set(triangles[a]) & set(triangles[b]) for a, b in tree.overlap(tree))


def inspect(params: dict) -> ToolResult:
    bpy = get_bpy()
    obj = _mesh_object(bpy, params.get("object_name"))
    points = [obj.matrix_world @ vert.co for vert in obj.data.vertices]
    if not points:
        raise InvalidStateError("Source mesh has no vertices")
    metrics = topology_metrics(obj)
    bounds = _bounds(points)
    center_x = (bounds[0][0] + bounds[1][0]) / 2
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    obj.data.calc_loop_triangles()
    triangles = [tuple(tri.vertices) for tri in obj.data.loop_triangles]
    tree = BVHTree.FromPolygons(points, triangles, all_triangles=True) if triangles else None
    errors = []
    sample = points[::max(1, len(points)//1000)]
    for point in sample:
        mirrored = Vector((2 * center_x - point.x, point.y, point.z))
        hit = tree.find_nearest(mirrored) if tree else None
        if hit and hit[0] is not None:
            errors.append(hit[3])
    textures = []
    for material in obj.data.materials:
        if material and material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    textures.append({"material": material.name, "image": node.image.name,
                                     "path": bpy.path.abspath(node.image.filepath)})
    intersections = _self_intersection_count(obj)
    warnings = []
    if intersections is None:
        warnings.append("Self-intersections skipped: over 100000 triangles")
    if metrics["boundary_edges"]:
        warnings.append("Source has open boundaries; proxy hole closure is approximate")
    if metrics["islands"] > 1:
        warnings.append("Source has disconnected islands; inspect whether small parts are intended")
    return ToolResult(data={"object": obj.name, "triangles": metrics["triangles"],
                            "islands": metrics["islands"], "island_vertices": metrics["island_vertices"],
                            "non_manifold_edges": metrics["non_manifold_edges"],
                            "boundary_edges": metrics["boundary_edges"],
                            "self_intersections": intersections,
                            "symmetry_plane_x": center_x,
                            "asymmetry_mean_m": sum(errors)/len(errors) if errors else None,
                            "asymmetry_max_m": max(errors) if errors else None,
                            "bounds": bounds, "textures": textures,
                            "pose_hints": {"fused_limb_risk": "undetermined", "reason": "Landmarks or body graph are needed to identify limbs"},
                            "focus": list(points[0])}, warnings=warnings)


def _select_only(bpy, obj):
    active = bpy.context.view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    for item in selected:
        item.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return active, selected


def _restore_selection(bpy, previous):
    for item in bpy.context.selected_objects:
        item.select_set(False)
    active, selected = previous
    for item in selected:
        if item.name in bpy.data.objects:
            item.select_set(True)
    if active and active.name in bpy.data.objects:
        bpy.context.view_layer.objects.active = active


def prepare(params: dict) -> ToolResult:
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    require_document_write_access(bpy)
    source = _mesh_object(bpy, params.get("object_name"))
    voxel_size = params.get("voxel_size")
    if isinstance(voxel_size, bool) or not isinstance(voxel_size, (int, float)) or not isfinite(voxel_size) or voxel_size <= 0:
        raise InvalidParamsError("voxel_size must be a positive finite number in metres")
    min_vertices = params.get("min_island_vertices", 8)
    if isinstance(min_vertices, bool) or not isinstance(min_vertices, int) or min_vertices < 0:
        raise InvalidParamsError("min_island_vertices must be a non-negative integer")
    symmetrize = params.get("symmetrize", False)
    if not isinstance(symmetrize, bool):
        raise InvalidParamsError("symmetrize must be a boolean")
    plane_x = params.get("symmetry_plane_x")
    if symmetrize and (isinstance(plane_x, bool) or not isinstance(plane_x, (int, float)) or not isfinite(plane_x)):
        raise InvalidParamsError("symmetry_plane_x is required when symmetrize is true")
    output_name = params.get("output_name") or f"{source.name}_projection_proxy"
    if not isinstance(output_name, str) or not output_name.strip():
        raise InvalidParamsError("output_name must be a non-empty string")
    proxy = source.copy()
    proxy.data = source.data.copy()
    proxy.name = output_name
    for modifier in list(proxy.modifiers):
        proxy.modifiers.remove(modifier)
    bpy.context.scene.collection.objects.link(proxy)
    try:
        components = _components(proxy.data)
        discarded = [part for part in components[1:] if len(part) < min_vertices]
        if discarded:
            import bmesh
            bm = bmesh.new()
            bm.from_mesh(proxy.data)
            bm.verts.ensure_lookup_table()
            bmesh.ops.delete(bm, geom=[bm.verts[i] for part in discarded for i in part], context="VERTS")
            bm.to_mesh(proxy.data)
            bm.free()
            proxy.data.update()
        before_vertices = len(proxy.data.vertices)
        before_faces = len(proxy.data.polygons)
        previous = _select_only(bpy, proxy)
        try:
            if symmetrize:
                guide = bpy.data.objects.new("CBT temporary symmetry plane", None)
                bpy.context.scene.collection.objects.link(guide)
                guide.location = (float(plane_x), 0, 0)
                try:
                    mirror = proxy.modifiers.new("Projection proxy symmetry", "MIRROR")
                    mirror.mirror_object = guide
                    mirror.use_bisect_axis[0] = True
                    mirror.use_clip = True
                    bpy.ops.object.modifier_apply(modifier=mirror.name)
                finally:
                    bpy.data.objects.remove(guide, do_unlink=True)
            proxy.data.remesh_voxel_size = float(voxel_size)
            bpy.ops.object.voxel_remesh()
        finally:
            _restore_selection(bpy, previous)
        if not proxy.data.polygons:
            raise InvalidStateError("Voxel remesh produced an empty proxy")
        from mathutils.bvhtree import BVHTree
        source.data.calc_loop_triangles()
        source_points = [source.matrix_world @ vert.co for vert in source.data.vertices]
        source_tris = [tuple(tri.vertices) for tri in source.data.loop_triangles]
        tree = BVHTree.FromPolygons(source_points, source_tris, all_triangles=True)
        distances = []
        for vertex in proxy.data.vertices:
            hit = tree.find_nearest(proxy.matrix_world @ vertex.co)
            if hit and hit[0] is not None:
                distances.append(hit[3])
        report = topology_metrics(proxy)
        bump_epoch()
        return ToolResult(data={"source": source.name, "proxy": proxy.name, "changed": True,
                                "state_token": current_state_token(bpy), "voxel_size": float(voxel_size),
                                "symmetrized": symmetrize, "symmetry_plane_x": float(plane_x) if symmetrize else None,
                                "removed_islands": len(discarded), "removed_vertices": sum(map(len, discarded)),
                                "before_remesh": {"vertices": before_vertices, "faces": before_faces},
                                "after_remesh": {"vertices": len(proxy.data.vertices), "faces": len(proxy.data.polygons)},
                                "proxy_to_original_distance_m": {"mean": sum(distances)/len(distances) if distances else None,
                                                                  "max": max(distances) if distances else None},
                                "proxy_topology": report, "focus": report.get("focus")},
                          warnings=["Original source remains unchanged and should be used for texture/detail baking"])
    except Exception:
        bpy.data.objects.remove(proxy, do_unlink=True)
        raise
