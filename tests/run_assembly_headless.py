"""Blender 5.2 real-mesh qualification for assembly.analyze."""

from pathlib import Path
import sys
from time import perf_counter

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for name in list(sys.modules):
    if name == "blenderboo" or name.startswith("blenderboo."):
        del sys.modules[name]

from blenderboo.tools.assembly import analyze_assembly


def cube(name, center, size=.01):
    bpy.ops.mesh.primitive_cube_add(size=size, location=center)
    obj = bpy.context.object
    obj.name = name
    return obj


def pair(a, b, **kwargs):
    receipt = analyze_assembly({"pairs": [[a.name, b.name]], "search_margin": .02, **kwargs}).data
    assert len(receipt["pairs"]) == 1, receipt
    return receipt["pairs"][0]


bpy.ops.wm.read_factory_settings(use_empty=True)
base = cube("GapA", (0, 0, 0))
gap = cube("GapB", (.011, 0, 0))
measured = pair(base, gap, expectations=[{"pair": [base.name, gap.name], "gap_between": [.0009, .0011]}])
assert measured["status"] == "gap", measured
assert .0008 < measured["min_gap"] < .0012, measured
assert measured["expectation"]["status"] == "pass", measured
assert set(measured["focus"]) == {"point", "radius"}, measured
print("PASS 1 mm gap and render.detail focus")

border_a = cube("BorderA", (0, 2, 0))
border_b = cube("BorderB", (.010200003, 2, 0))
border = pair(border_a, border_b, contact_tolerance=.0001,
              expectations=[{"pair": [border_a.name, border_b.name], "gap_between": [0, .0002]}])
assert border["expectation"]["status"] == "fail", border
assert border["expectation"]["borderline"] is True, border
assert border["expectation"]["margin"]["gap_max_m"] < 0, border
assert border["samples_per_contact_area_m2"] is None or border["samples_per_contact_area_m2"] >= 0, border
print("PASS borderline threshold keeps fail and reports margin")

touching = cube("TouchB", (.01, 0, 0))
measured = pair(base, touching)
assert measured["status"] == "contact", measured
print("PASS touching cubes")

detached = cube("DetachedScrew", (1, 1, 1), size=.002)
assembly = analyze_assembly({"object_names": [base.name, touching.name, detached.name],
                             "search_margin": .02}).data
assert assembly["isolation_complete"] is True, assembly
assert assembly["unexpected_isolated_count"] == 1, assembly
assert [part["object"] for part in assembly["isolated_parts"]] == [detached.name], assembly
assert set(assembly["isolated_parts"][0]["focus"]) == {"point", "radius"}, assembly
expected = analyze_assembly({"object_names": [base.name, touching.name, detached.name],
                             "search_margin": .02, "expect_isolated": [detached.name]}).data
assert expected["unexpected_isolated_count"] == 0, expected
assert expected["isolated_parts"][0]["expected"] is True, expected
print("PASS detached part and expected isolation")

overlap = cube("OverlapB", (.008, 0, 0))
measured = pair(base, overlap, expectations=[{"pair": [base.name, overlap.name], "no_intersection": True}])
assert measured["status"] == "intersecting", measured
assert measured["expectation"]["status"] == "fail", measured
assert measured["max_penetration_depth"] is not None, measured
assert abs(measured["max_penetration_depth"] - .002) < .00035, measured
print("PASS 2 mm overlap and penetration depth")

# A curved tube crosses the box end face. The mesh interface is evaluated in
# world space; the body of the tube is open at its end, so depth may be null.
bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=.001, depth=.015,
                                    location=(.005, 0, -.004))
tube = bpy.context.object
tube.name = "Tube"
measured = pair(base, tube)
assert measured["status"] == "intersecting", measured
print("PASS curved tube enters box")

parent = cube("Parent", (2, 0, 0))
child = cube("Child", (2.009, 0, 0))
world = child.matrix_world.copy()
child.parent = parent
child.matrix_world = world
measured = pair(parent, child)
assert measured["status"] in {"intersecting", "contact"}, measured
print("PASS parented world transforms")

bpy.ops.mesh.primitive_plane_add(size=.01, location=(4, 0, 0))
plane = bpy.context.object
plane.name = "SolidifyPlane"
solidify = plane.modifiers.new("Thickness", "SOLIDIFY")
solidify.thickness = .002
box = cube("BelowPlane", (4, 0, -.002), size=.001)
base_result = pair(plane, box, evaluated=False)
evaluated_result = pair(plane, box, evaluated=True)
assert base_result["status"] in {"gap", "separated"}, base_result
assert evaluated_result["status"] == "intersecting", evaluated_result
print("PASS evaluated Solidify differs from base mesh")


def cylinder(name, center, radius, depth):
    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=radius, depth=depth, location=center)
    obj = bpy.context.object
    obj.name = name
    return obj


for position in (.3, 30.0):
    label = str(position).replace(".", "_")
    head = cylinder(f"PinHead_{label}", (position, 0, 0), .004, .002)
    pin = cylinder(f"Pin_{label}", (position, 0, -.004), .001, .006)
    seated = pair(head, pin, contact_tolerance=.0002)
    assert seated["status"] == "contact", seated
    assert seated["max_penetration_depth"] is None, seated
    assert seated["status_evidence"] == ["distance"], seated
    assert seated["penetration"] == "none", seated

    wall = cube(f"Wall_{label}", (position, .1, 0), .02)
    flush = cube(f"FlushTab_{label}", (position + .015, .1, 0), .01)
    flush_result = pair(wall, flush, contact_tolerance=.0002)
    assert flush_result["status"] == "contact", flush_result
    shallow = cube(f"ShallowTab_{label}", (position + .01495, .1, 0), .01)
    shallow_result = pair(wall, shallow, contact_tolerance=.0002)
    assert shallow_result["status"] == "contact", shallow_result
    assert shallow_result["max_penetration_depth"] is None, shallow_result

    deep = cube(f"DeepTab_{label}", (position + .013, .1, 0), .01)
    deep_result = pair(wall, deep, contact_tolerance=.0002)
    assert deep_result["status"] == "intersecting", deep_result
    assert deep_result["max_penetration_depth"] is not None, deep_result
    assert abs(deep_result["max_penetration_depth"] - .002) < .0004, deep_result
    assert "inside_samples" in deep_result["status_evidence"], deep_result

    box = cube(f"TubeBox_{label}", (position, .2, 0), .01)
    tube_inside = cylinder(f"TubeEnd_{label}", (position, .2, -.004), .001, .015)
    tube_result = pair(box, tube_inside, contact_tolerance=.0002)
    assert tube_result["status"] == "intersecting", tube_result

    shell_box = cube(f"ShellBox_{label}", (position, .3, 0), .01)
    bpy.ops.mesh.primitive_plane_add(size=.02, location=(position, .3, 0))
    shell = bpy.context.object
    shell.name = f"OpenShell_{label}"
    receipt = analyze_assembly({"pairs": [[shell_box.name, shell.name]], "contact_tolerance": .0002})
    shell_result = receipt.data["pairs"][0]
    assert shell_result["penetration"] == "undetermined", shell_result
    assert any(shell.name in warning for warning in receipt.warnings), receipt.warnings
    assert shell.name not in shell_result["inside_sample_targets"], shell_result
    print(f"PASS contact tolerance, true overlap, tube, open shell at world {position} m")

# Performance fixture: 400,000 triangles total, with an explicit pair so the
# full BVH construction and measurement are included in elapsed time.
bpy.ops.mesh.primitive_grid_add(x_subdivisions=318, y_subdivisions=318, size=1)
mesh_a = bpy.context.object
mesh_a.name = "LargeA"
bpy.ops.mesh.primitive_grid_add(x_subdivisions=318, y_subdivisions=318, size=1, location=(0, 0, .01))
mesh_b = bpy.context.object
mesh_b.name = "LargeB"
start = perf_counter()
measured = pair(mesh_a, mesh_b)
elapsed = perf_counter() - start
assert measured["status"] in {"gap", "separated"}, measured
print(f"PERFORMANCE two ~200k-triangle meshes: {elapsed:.3f} s")
