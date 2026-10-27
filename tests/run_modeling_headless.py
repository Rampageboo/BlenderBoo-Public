"""Blender background qualification for semantic mesh modeling and measurement."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys

import bmesh
import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo import dispatch


def require_ok(result: dict, label: str) -> dict:
    json.dumps(result, allow_nan=False)
    if not result["ok"]:
        raise AssertionError(f"{label} failed:\n{json.dumps(result, indent=2)}")
    print(f"PASS {label}")
    return result["data"]


def require_error(result: dict, error_type: str, label: str) -> dict:
    json.dumps(result, allow_nan=False)
    if result["ok"] or result["error"]["type"] != error_type:
        raise AssertionError(f"{label} expected {error_type}:\n{json.dumps(result, indent=2)}")
    print(f"PASS {label}")
    return result["error"]


def object_from_bmesh(name: str, bm: bmesh.types.BMesh) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    bm.to_mesh(mesh)
    mesh.update(calc_edges=True, calc_edges_loose=True)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


artifact_root = ROOT / "tests" / ".tmp_modeling_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "modeling_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

capabilities = require_ok(dispatch("system.capabilities", {"limit": 200}), "system.capabilities")
assert capabilities["capability_count"] >= 134
mesh_toolset = next(item for item in capabilities["toolsets"] if item["name"] == "mesh")
assert mesh_toolset["tool_count"] == 5

# Local/world measurement uses the source mesh plus the object's unapplied transform.
bpy.ops.mesh.primitive_cube_add(size=2.0)
measure_cube = bpy.context.object
measure_cube.name = "MeasureCube"
measure_cube.scale = (2.0, 3.0, 4.0)
bpy.context.view_layer.update()
edge_vertices = [int(value) for value in measure_cube.data.edges[0].vertices]
face_vertices = [int(value) for value in measure_cube.data.polygons[0].vertices[:3]]
measurement = require_ok(
    dispatch(
        "mesh.measure",
        {
            "object_name": measure_cube.name,
            "queries": [
                {"type": "EDGE_LENGTH", "space": "LOCAL"},
                {"type": "EDGE_LENGTH", "space": "WORLD"},
                {"type": "FACE_AREA", "space": "LOCAL"},
                {"type": "FACE_AREA", "space": "WORLD"},
                {"type": "VERTEX_DISTANCE", "vertices": edge_vertices, "space": "WORLD"},
                {"type": "ANGLE", "vertices": face_vertices, "space": "LOCAL"},
                {
                    "type": "SELECTION_BOUNDS",
                    "domain": "EDGE",
                    "space": "WORLD",
                    "predicate": {"kind": "SHARP", "min_angle": math.radians(45.0)},
                },
                {"type": "VOLUME", "space": "LOCAL"},
                {"type": "VOLUME", "space": "WORLD"},
            ],
        },
    ),
    "mesh.measure local/world",
)
results = measurement["results"]
assert results[0]["count"] == 12 and math.isclose(results[0]["total"], 24.0, rel_tol=1e-6)
assert results[1]["count"] == 12 and math.isclose(results[1]["total"], 72.0, rel_tol=1e-6)
assert results[2]["count"] == 6 and math.isclose(results[2]["total"], 24.0, rel_tol=1e-6)
assert results[3]["count"] == 6 and math.isclose(results[3]["total"], 208.0, rel_tol=1e-6)
world_edge_points = [measure_cube.matrix_world @ measure_cube.data.vertices[index].co for index in edge_vertices]
assert math.isclose(results[4]["distance"], (world_edge_points[1] - world_edge_points[0]).length, rel_tol=1e-6)
assert math.isclose(results[5]["degrees"], 90.0, abs_tol=1e-5)
assert results[6]["matched_elements"] == 12
assert all(
    math.isclose(value, expected, rel_tol=1e-6)
    for value, expected in zip(results[6]["bounds"]["dimensions"], (4.0, 6.0, 8.0))
)
assert math.isclose(results[7]["volume"], 8.0, rel_tol=1e-6)
assert math.isclose(results[8]["volume"], 192.0, rel_tol=1e-6)

require_error(
    dispatch(
        "mesh.measure",
        {"object_name": measure_cube.name, "queries": [{"type": "VOLUME"}], "max_elements": 1},
    ),
    "InvalidStateError",
    "mesh.measure element budget",
)
measure_cube.scale = (1.0, 1.0, 1.0)

# Concave n-gon world area uses tessellation rather than an over-counting fan.
concave_mesh = bpy.data.meshes.new("ConcaveMeasureMesh")
concave_mesh.from_pydata(
    [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 2.0, 0.0), (1.0, 1.0, 0.0), (0.0, 2.0, 0.0)],
    [],
    [(0, 1, 2, 3, 4)],
)
concave_object = bpy.data.objects.new("ConcaveMeasure", concave_mesh)
bpy.context.scene.collection.objects.link(concave_object)
concave_object.scale = (2.0, 3.0, 1.0)
bpy.context.view_layer.update()
concave_area = require_ok(
    dispatch(
        "mesh.measure",
        {
            "object_name": concave_object.name,
            "queries": [
                {"type": "FACE_AREA", "space": "LOCAL"},
                {"type": "FACE_AREA", "space": "WORLD"},
            ],
        },
    ),
    "mesh.measure concave face area",
)
assert math.isclose(concave_area["results"][0]["total"], 3.0, rel_tol=1e-6)
assert math.isclose(concave_area["results"][1]["total"], 18.0, rel_tol=1e-6)

# Semantic selection on a quad grid: boundaries, coplanar regions, valence, loops, and rings.
bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4, size=3.0)
grid = bpy.context.object
grid.name = "SemanticGrid"
edge_index_by_key = {
    tuple(sorted(int(value) for value in edge.vertices)): int(edge.index)
    for edge in grid.data.edges
}
edge_face_counts = [0] * len(grid.data.edges)
for polygon in grid.data.polygons:
    for key in polygon.edge_keys:
        edge_face_counts[edge_index_by_key[tuple(sorted(int(value) for value in key))]] += 1
expected_boundary_edges = sum(1 for count in edge_face_counts if count == 1)
expected_coplanar_faces = len(grid.data.polygons)
vertex_valence = [0] * len(grid.data.vertices)
for edge in grid.data.edges:
    for vertex_index in edge.vertices:
        vertex_valence[int(vertex_index)] += 1
expected_valence_four = sum(1 for count in vertex_valence if count == 4)

def selection_plan(predicate: dict, domain: str, label: str) -> dict:
    data = require_ok(
        dispatch(
            "mesh.edit",
            {
                "object_name": grid.name,
                "operations": [{"op": "select", "domain": domain, "predicate": predicate}],
            },
        ),
        label,
    )
    return data["journal"][0]["selection"]


assert selection_plan({"kind": "BOUNDARY"}, "EDGE", "select boundary edges")["matched"] == expected_boundary_edges
assert selection_plan(
    {"kind": "COPLANAR", "seed_index": 0, "connected": True},
    "FACE",
    "select connected coplanar faces",
)["matched"] == expected_coplanar_faces
assert selection_plan({"kind": "VALENCE", "min": 4, "max": 4}, "VERT", "select valence four")["matched"] == expected_valence_four

seed_edge = next(index for index, count in enumerate(edge_face_counts) if count == 2)
assert selection_plan(
    {"kind": "EDGE_LOOP", "seed_index": seed_edge},
    "EDGE",
    "select edge loop",
)["matched"] > 1
assert selection_plan(
    {"kind": "EDGE_RING", "seed_index": seed_edge},
    "EDGE",
    "select edge ring",
)["matched"] > 1

# CONNECTED stays inside one disconnected component.
bm = bmesh.new()
first = bmesh.ops.create_cube(bm, size=2.0)["verts"]
bmesh.ops.translate(bm, verts=first, vec=Vector((-2.0, 0.0, 0.0)))
second = bmesh.ops.create_cube(bm, size=2.0)["verts"]
bmesh.ops.translate(bm, verts=second, vec=Vector((2.0, 0.0, 0.0)))
disconnected = object_from_bmesh("DisconnectedCubes", bm)
bm.free()
connected = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": disconnected.name,
            "operations": [
                {
                    "op": "select",
                    "domain": "VERT",
                    "predicate": {"kind": "CONNECTED", "seed_indices": [0]},
                }
            ],
        },
    ),
    "select connected component",
)
assert connected["journal"][0]["selection"]["matched"] == 8

# Fill one loose boundary loop.
bm = bmesh.new()
bmesh.ops.create_circle(bm, segments=8, radius=1.0, cap_ends=False)
fill_object = object_from_bmesh("FillLoop", bm)
bm.free()
filled = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": fill_object.name,
            "operations": [
                {"op": "select", "domain": "EDGE", "predicate": {"kind": "ALL"}},
                {"op": "fill"},
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.edit fill",
)
assert filled["changed"] is True and len(fill_object.data.polygons) == 1

# Bridge two loose loops.
bm = bmesh.new()
lower = bmesh.ops.create_circle(bm, segments=8, radius=1.0, cap_ends=False)["verts"]
bmesh.ops.translate(bm, verts=lower, vec=Vector((0.0, 0.0, -1.0)))
upper = bmesh.ops.create_circle(bm, segments=8, radius=1.0, cap_ends=False)["verts"]
bmesh.ops.translate(bm, verts=upper, vec=Vector((0.0, 0.0, 1.0)))
bridge_object = object_from_bmesh("BridgeLoops", bm)
bm.free()
bridged = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": bridge_object.name,
            "operations": [
                {"op": "select", "domain": "EDGE", "predicate": {"kind": "ALL"}},
                {"op": "bridge"},
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.edit bridge",
)
assert bridged["changed"] is True and len(bridge_object.data.polygons) == 8

# Triangulate is dry-run-first, confirmation-gated, and writes once.
bpy.ops.mesh.primitive_cube_add(size=2.0)
triangulate_object = bpy.context.object
triangulate_object.name = "TriangulateCube"
triangulate_ops = [
    {"op": "select", "domain": "FACE", "predicate": {"kind": "ALL"}},
    {"op": "triangulate"},
]
triangulate_plan = require_ok(
    dispatch("mesh.edit", {"object_name": triangulate_object.name, "operations": triangulate_ops}),
    "mesh.edit triangulate dry run",
)
assert triangulate_plan["dry_run"] is True and triangulate_plan["confirmation_required"] is True
assert len(triangulate_object.data.polygons) == 6
require_error(
    dispatch(
        "mesh.edit",
        {"object_name": triangulate_object.name, "operations": triangulate_ops, "dry_run": False},
    ),
    "ConfirmationRequiredError",
    "mesh.edit triangulate confirm gate",
)
triangulated = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": triangulate_object.name,
            "operations": triangulate_ops,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.edit triangulate apply",
)
assert triangulated["changed"] is True and len(triangulate_object.data.polygons) == 12

# A later invalid operation rejects the whole chain before source writeback.
bpy.ops.mesh.primitive_cube_add(size=2.0)
rollback_object = bpy.context.object
rollback_object.name = "AtomicRollbackCube"
before_counts = (len(rollback_object.data.vertices), len(rollback_object.data.edges), len(rollback_object.data.polygons))
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": rollback_object.name,
            "operations": [
                {"op": "select", "domain": "FACE", "predicate": {"kind": "ALL"}},
                {"op": "triangulate"},
                {"op": "select", "domain": "EDGE", "predicate": {"kind": "ALL"}},
                {"op": "fill"},
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "InvalidStateError",
    "mesh.edit atomic rejection",
)
after_counts = (len(rollback_object.data.vertices), len(rollback_object.data.edges), len(rollback_object.data.polygons))
assert after_counts == before_counts

# Bisect and clear one side of a cube.
bpy.ops.mesh.primitive_cube_add(size=2.0)
bisect_object = bpy.context.object
bisect_object.name = "BisectCube"
bisected = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": bisect_object.name,
            "operations": [
                {
                    "op": "bisect",
                    "plane_co": [0.0, 0.0, 0.0],
                    "plane_no": [1.0, 0.0, 0.0],
                    "clear_outer": True,
                }
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.edit bisect",
)
assert bisected["changed"] is True
xs = [float(vertex.co.x) for vertex in bisect_object.data.vertices]
assert max(xs) <= 1e-5 or min(xs) >= -1e-5
assert any(abs(value) <= 1e-5 for value in xs)

# Dissolve one regular valence-four grid vertex.
bpy.ops.mesh.primitive_grid_add(x_subdivisions=3, y_subdivisions=3, size=2.0)
dissolve_object = bpy.context.object
dissolve_object.name = "DissolveGrid"
dissolve_vertices_before = len(dissolve_object.data.vertices)
dissolved = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": dissolve_object.name,
            "operations": [
                {
                    "op": "select",
                    "domain": "VERT",
                    "predicate": {"kind": "VALENCE", "min": 4, "max": 4},
                },
                {"op": "dissolve", "domain": "VERT"},
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.edit dissolve",
)
dissolved_vertex_count = dissolved["journal"][0]["selection"]["matched"]
assert dissolved["changed"] is True
assert len(dissolve_object.data.vertices) == dissolve_vertices_before - dissolved_vertex_count

# Symmetrize a negative-X half plane into a full symmetric plane.
mesh = bpy.data.meshes.new("SymmetryPlaneMesh")
mesh.from_pydata(
    [(-2.0, -1.0, 0.0), (0.0, -1.0, 0.0), (0.0, 1.0, 0.0), (-2.0, 1.0, 0.0)],
    [],
    [(0, 1, 2, 3)],
)
symmetry_object = bpy.data.objects.new("SymmetryPlane", mesh)
bpy.context.scene.collection.objects.link(symmetry_object)
symmetrized = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": symmetry_object.name,
            "operations": [{"op": "symmetrize", "direction": "-X"}],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.edit symmetrize",
)
assert symmetrized["changed"] is True
symmetry_xs = [float(vertex.co.x) for vertex in symmetry_object.data.vertices]
assert min(symmetry_xs) < -1.9 and max(symmetry_xs) > 1.9

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("MODELING_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
