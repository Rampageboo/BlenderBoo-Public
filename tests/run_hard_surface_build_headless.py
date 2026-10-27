"""Scoped Blender headless qualification of the supported R3 constructor domain."""
import json
from pathlib import Path
import sys

import bpy
import bmesh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.errors import NameConflictError, StaleStateError
from blenderboo.tools.hard_surface_build import build_lathe, build_sweep, build_plate, build_slab, clear_of, fair_fit
from blenderboo.tools.session_state import current_state_token


def built(result, closed=True):
    data = result.data
    assert data["changed"], data
    json.dumps(data, allow_nan=False)
    obj = bpy.data.objects[data["object_name"]]
    assert all(len(p.vertices) == 4 for p in obj.data.polygons)
    attr = obj.data.attributes[data["part_attribute"]]
    assert attr.domain == "FACE" and attr.data_type == "INT"
    assert all(v.value == data["part_id"] for v in attr.data)
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        assert all(e.is_manifold for e in bm.edges) if closed else any(e.is_boundary for e in bm.edges)
        assert all(f.calc_area() > 0 for f in bm.faces)
    finally:
        bm.free()
    return obj


def inventory():
    return (tuple(bpy.data.objects.keys()), tuple(bpy.data.meshes.keys()),
            bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
            tuple(obj.name for obj in bpy.context.selected_objects))


bpy.ops.wm.read_factory_settings(use_empty=True)
lathe_params = {"name": "Lathe", "segments": 16, "part_id": 7,
    "profile": [{"radius": 1, "z": 0, "sharp": True}, {"radius": 1.5, "z": 1, "round_radius": .1}, {"radius": 1, "z": 2}]}
lathe = built(build_lathe(lathe_params))
assert "sharp_edge" in lathe.data.attributes
assert all(poly.use_smooth for poly in lathe.data.polygons)
assert sum(edge.value for edge in lathe.data.attributes["sharp_edge"].data) >= 32
before = inventory()
try:
    build_lathe(lathe_params)
    raise AssertionError("Name collision accepted")
except NameConflictError:
    pass
assert inventory() == before
try:
    build_lathe({**lathe_params, "name": "Stale", "expected_state": "stale.0"})
    raise AssertionError("Stale state accepted")
except StaleStateError:
    pass
assert inventory() == before
print("PASS all-quad filleted lathe, face part attribute, sharp rings, name/state guards")

sweep = built(build_sweep({"name": "Sweep", "path": [{"point": p, "width": .2, "height": .3, "chamfer": .03}
    for p in ([0,0,0],[2,0,0],[2,2,.2])]}))
loop = built(build_sweep({"name": "Loop", "closed": True, "path": [{"point": p, "width": .2, "height": .2}
    for p in ([0,0,0],[3,0,0],[3,3,0],[0,3,0])]}))
print("PASS open 3D and closed planar sweeps with all-quad caps/miters")

bpy.ops.mesh.primitive_plane_add(size=20)
plane = bpy.context.object
plane.name = "TargetPlane"
bpy.context.view_layer.update()
outline = [[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]]
plate = built(build_plate({"name": "Plate", "outline": outline, "subdivisions": [4,4], "thickness": .1,
                           "target": plane.name, "part_id": 9}))
assert 0 <= min(v.co.z for v in plate.data.vertices) < 1e-4
assert .1 <= max(v.co.z for v in plate.data.vertices) < .1001
opened = built(build_plate({"name": "OpenPlate", "outline": outline, "thickness": .1, "open_back": True}), False)
before = inventory()
miss = build_plate({"name": "Missed", "outline": [[x,y,z] for x,y,z in outline], "target": plane.name,
                    "projection_direction": [0,0,1]}).data
assert miss["status"] == "unsupported" and not miss["changed"], miss
assert inventory() == before
print("PASS projected plate, thickness, open back, and projection miss without mutation")

bpy.context.view_layer.update()
original = [tuple(v.co) for v in plate.data.vertices]
plan = clear_of({"object_name": plate.name, "target": plane.name, "part_id": 9, "gap": [.2,.4]}).data
assert plan["clearance"] and not plan["changed"], plan
assert original == [tuple(v.co) for v in plate.data.vertices]
changed = clear_of({"object_name": plate.name, "target": plane.name, "part_id": 9, "gap": [.2,.4],
                    "apply": True, "expected_state": current_state_token(bpy)}).data
assert changed["changed"] and changed["status"] == "applied", changed
assert abs(min(v.co.z for v in plate.data.vertices)-.2) < 1e-6
assert abs(max(v.co.z for v in plate.data.vertices)-.3) < 1e-6
before = [tuple(v.co) for v in plate.data.vertices]
blocked = clear_of({"object_name": plate.name, "target": plane.name, "part_id": 9, "gap": [0,.01], "apply": True}).data
assert not blocked["changed"] and blocked["status"] == "partial", blocked
assert before == [tuple(v.co) for v in plate.data.vertices]
print("PASS preview and applied rigid planar clearance, bounded failure leaves geometry unchanged")

bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=2)
sphere = bpy.context.object
sphere.name = "NoisyShellSource"
bpy.context.view_layer.update()
projected_result = build_plate({"name": "SpherePlate", "outline": [[-.1,-.1,2.2],[.1,-.1,2.2],
    [.1,.1,2.2],[-.1,.1,2.2]], "subdivisions": [4,4], "thickness": .003,
    "target": sphere.name, "projection_direction": [0,0,-1]})
projected = built(projected_result)
assert 0 < projected_result.data["projection_outward_offset_scene_units"] <= .003
assert all(poly.use_smooth for poly in projected.data.polygons)
assert "sharp_edge" in projected.data.attributes
for index in range(25):
    near = projected.data.vertices[index].co
    far = projected.data.vertices[index + 25].co
    assert (far - near).dot(near) > 0, "Projected plate thickness entered the sphere"
from mathutils.bvhtree import BVHTree
depsgraph = bpy.context.evaluated_depsgraph_get()
assert not BVHTree.FromObject(projected, depsgraph).overlap(BVHTree.FromObject(sphere, depsgraph)), \
    "Projected plate still cuts through the sphere"
settings = {"source": sphere.name, "center": [0,0,0], "theta_samples": 24, "phi_samples": 13,
            "phi_range": [-50,70], "order_m": 2, "order_k": 2, "rejection_sigma": 4}
before = inventory()
analysis = fair_fit(settings).data
assert not analysis["changed"] and analysis["coverage"] == 1, analysis
assert inventory() == before
band = built(fair_fit({**settings, "apply": True, "name": "FairBand", "thickness": .1}))
plane.data.vertices[0].co.z = .5
bpy.context.view_layer.update()
unsupported = clear_of({"object_name": plate.name, "target": plane.name, "part_id": 9, "gap": [.1,.3], "apply": True}).data
assert unsupported["status"] == "unsupported" and not unsupported["changed"], unsupported
before = inventory()
polar = fair_fit({**settings, "phi_range": [-50,90], "apply": True, "name": "UnsupportedPole", "thickness": .1}).data
assert polar["status"] == "unsupported" and inventory() == before, polar
print("PASS read-only fair-fit analysis, all-quad manifold radial band and truthful unsupported curved/polar cases")
# Curved sphere clearance must prove a gap over triangles, not merely at vertices.
curved_part = built(build_plate({"name":"CurvedPart","outline":[[-.1,-.1,1.98],[.1,-.1,1.98],[.1,.1,1.98],[-.1,.1,1.98]],
                                "subdivisions":[2,2],"thickness":.04,"part_id":12}))
bpy.context.view_layer.update()
original = [tuple(v.co) for v in curved_part.data.vertices]
curved_plan = clear_of({"object_name":curved_part.name,"target":sphere.name,"part_id":12,"gap":[.1,.4],"step":.02}).data
assert curved_plan["clearance"] and not curved_plan["changed"], curved_plan
assert curved_plan["clearance"]["status"] == "certified", curved_plan
assert original == [tuple(v.co) for v in curved_part.data.vertices]
curved_applied = clear_of({"object_name":curved_part.name,"target":sphere.name,"part_id":12,"gap":[.1,.4],"step":.02,"apply":True}).data
assert curved_applied["changed"] and curved_applied["clearance"]["outside_components"] == 1, curved_applied
assert curved_applied["clearance"]["minimum_scene_units"] >= .1, curved_applied
assert curved_applied["clearance"]["maximum_scene_units"] <= .4, curved_applied
assert curved_applied["clearance"]["intersection_pairs"] == 0, curved_applied
print("PASS curved target lift with full-surface gap certificate and outside proof")

pentagon = built(build_plate({"name":"PentagonalPlate","outline":[[0,0],[2,0],[3,1],[1.5,2],[-.5,1]],
                              "subdivisions":[2,2],"thickness":.1}))
assert len(pentagon.data.polygons) > 5
triangle = built(build_plate({"name":"TriangularPlate","outline":[[0,0],[1,0],[0,1]],
                              "subdivisions":[2,2],"thickness":.1}))
print("PASS general convex 2D outlines retain all-quad manifold thickness")
slab_outline = [[-2,-1],[2,-1],[2,1],[-2,1]]
slab_opening = [[-.5,-.4],[.5,-.4],[.5,.4],[-.5,.4]]
for name, extra, expected in (("SolidSlab", {}, "solid"),
                              ("ThroughSlab", {"opening": slab_opening}, "through"),
                              ("PocketSlab", {"opening": slab_opening, "pocket_depth": .2}, "pocket")):
    result = build_slab({"name": name, "outline": slab_outline, "thickness": .5, **extra})
    obj = built(result)
    assert result.data["opening_mode"] == expected and result.data["counts"]["all_quads"], result.data
    assert result.data["thickness_direction"] == "negative_outline_normal", result.data
    assert "fregion" in obj.data.attributes and all(poly.use_smooth for poly in obj.data.polygons)
print("PASS solid slab, through opening and blind pocket with FACE region labels")

print("HARD_SURFACE_BUILD_HEADLESS_OK")
