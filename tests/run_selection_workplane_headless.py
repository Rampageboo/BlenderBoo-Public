"""Blender background qualification for path/region selection and object workplanes."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys

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


def matrix_delta(left, right) -> float:
    return max(
        abs(float(left[row][column]) - float(right[row][column]))
        for row in range(4)
        for column in range(4)
    )


artifact_root = ROOT / "tests" / ".tmp_selection_workplane_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "selection_workplane_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

# A 3x3 vertex grid provides deterministic equal-cost paths and two face materials.
mesh = bpy.data.meshes.new("SelectionGridMesh")
mesh.from_pydata(
    [(float(x), float(y), 0.0) for y in range(3) for x in range(3)],
    [],
    [(0, 1, 4, 3), (1, 2, 5, 4), (3, 4, 7, 6), (4, 5, 8, 7)],
)
mesh.polygons[0].material_index = 0
mesh.polygons[1].material_index = 1
mesh.polygons[2].material_index = 0
mesh.polygons[3].material_index = 1
grid = bpy.data.objects.new("SelectionGrid", mesh)
bpy.context.scene.collection.objects.link(grid)

shortest_ops = [
    {
        "op": "select",
        "domain": "VERT",
        "predicate": {
            "kind": "SHORTEST_PATH",
            "start_index": 0,
            "end_index": 8,
            "metric": "TOPOLOGY",
        },
    },
    {"op": "transform", "translation": [0.0, 0.0, 1.0]},
]
shortest_before = [vertex.co.copy() for vertex in mesh.vertices]
shortest_plan = require_ok(
    dispatch("mesh.edit", {"object_name": grid.name, "operations": shortest_ops}),
    "deterministic shortest path dry run",
)
assert shortest_plan["journal"][0]["selection"]["matched"] == 5
assert all((vertex.co - before).length <= 1e-9 for vertex, before in zip(mesh.vertices, shortest_before))
require_ok(
    dispatch(
        "mesh.edit",
        {"object_name": grid.name, "operations": shortest_ops, "dry_run": False},
    ),
    "deterministic shortest path apply",
)
raised = {int(vertex.index) for vertex in mesh.vertices if math.isclose(float(vertex.co.z), 1.0, abs_tol=1e-6)}
assert raised == {0, 1, 2, 5, 8}

region_plan = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": grid.name,
            "operations": [
                {
                    "op": "select",
                    "domain": "FACE",
                    "predicate": {
                        "kind": "REGION",
                        "seed_indices": [0],
                        "steps": 10,
                        "within": {"kind": "MATERIAL", "material_index": 0},
                    },
                }
            ],
        },
    ),
    "material-constrained face region dry run",
)
assert region_plan["journal"][0]["selection"]["matched"] == 2
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": grid.name,
            "operations": [
                {
                    "op": "select",
                    "domain": "FACE",
                    "predicate": {
                        "kind": "REGION",
                        "seed_indices": [0],
                        "steps": 10,
                        "max_elements": 1,
                    },
                }
            ],
        },
    ),
    "InvalidStateError",
    "region result budget rejection",
)
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": grid.name,
            "operations": [
                {
                    "op": "select",
                    "domain": "FACE",
                    "predicate": {
                        "kind": "SHORTEST_PATH",
                        "start_index": 0,
                        "end_index": 1,
                        "within": {"kind": "MATERIAL", "material_index": 0},
                    },
                }
            ],
        },
    ),
    "InvalidStateError",
    "shortest path endpoint constraint rejection",
)

# A transformed, non-uniformly scaled tilted face defines a world-space object frame.
reference_mesh = bpy.data.meshes.new("ReferencePlaneMesh")
reference_mesh.from_pydata(
    [(0.0, 0.0, 0.0), (2.0, 0.0, 2.0), (2.0, 2.0, 2.0), (0.0, 2.0, 0.0)],
    [],
    [(0, 1, 2, 3)],
)
reference = bpy.data.objects.new("ReferencePlane", reference_mesh)
bpy.context.scene.collection.objects.link(reference)
reference.location = (3.0, -2.0, 1.0)
reference.rotation_euler = (0.2, -0.3, 0.4)
reference.scale = (2.0, 1.0, 0.5)
bpy.context.view_layer.update()
reference_edge = next(
    int(edge.index)
    for edge in reference_mesh.edges
    if set(int(index) for index in edge.vertices) == {0, 1}
)

bpy.ops.object.empty_add(type="PLAIN_AXES", location=(5.0, 1.0, -2.0))
parent = bpy.context.object
parent.name = "PlacementParent"
parent.rotation_euler.z = 0.35
bpy.ops.mesh.primitive_cube_add(size=1.0)
target = bpy.context.object
target.name = "PlacedPart"
target.scale = (1.5, 0.75, 2.0)
target.parent = parent
target.matrix_parent_inverse.identity()
bpy.context.view_layer.update()

placement = {
    "object_name": target.name,
    "reference_object": reference.name,
    "plane": {"source": "FACE", "face_index": 0, "edge_index": reference_edge},
    "offset": [1.0, 2.0, 0.5],
}
target_before = target.matrix_world.copy()
target_scale_before = target.matrix_world.decompose()[2]
placement_plan = require_ok(
    dispatch("object.align_to_workplane", placement),
    "object workplane placement dry run",
)
assert placement_plan["dry_run"] is True
assert placement_plan["changed"] is False
assert placement_plan["would_change"] is True
assert matrix_delta(target.matrix_world, target_before) <= 1e-9
world_frame = placement_plan["frame"]["world"]
origin = Vector(world_frame["origin"])
u_axis = Vector(world_frame["u_axis"])
v_axis = Vector(world_frame["v_axis"])
normal = Vector(world_frame["normal"])
expected_location = origin + u_axis * 1.0 + v_axis * 2.0 + normal * 0.5
assert (Vector(placement_plan["planned"]["world"]["location"]) - expected_location).length <= 1e-6

placement_apply = require_ok(
    dispatch("object.align_to_workplane", {**placement, "dry_run": False}),
    "object workplane placement apply",
)
assert placement_apply["changed"] is True
location, rotation, scale = target.matrix_world.decompose()
assert (location - expected_location).length <= 1e-5
assert (rotation @ Vector((1.0, 0.0, 0.0)) - u_axis).length <= 1e-5
assert (rotation @ Vector((0.0, 1.0, 0.0)) - v_axis).length <= 1e-5
assert (rotation @ Vector((0.0, 0.0, 1.0)) - normal).length <= 1e-5
assert (scale - target_scale_before).length <= 1e-5

aligned_matrix = target.matrix_world.copy()
require_error(
    dispatch(
        "object.align_to_workplane",
        {**placement, "axis_map": ["U", "V", "-N"], "dry_run": False},
    ),
    "InvalidParamsError",
    "reflecting workplane axis map rejection",
)
assert matrix_delta(target.matrix_world, aligned_matrix) <= 1e-9

# Explicit frames work against non-mesh reference objects as well.
explicit_plan = require_ok(
    dispatch(
        "object.align_to_workplane",
        {
            "object_name": target.name,
            "reference_object": parent.name,
            "plane": {
                "source": "EXPLICIT",
                "origin": [0.0, 0.0, 0.0],
                "normal": [0.0, 0.0, 1.0],
                "u_axis": [1.0, 0.0, 0.0],
            },
            "align_location": False,
            "axis_map": ["V", "U", "-N"],
        },
    ),
    "explicit object workplane dry run",
)
assert explicit_plan["frame"]["local"]["source"] == "EXPLICIT"
assert explicit_plan["planned"]["world"]["location"] == placement_apply["world"]["location"]

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("SELECTION_WORKPLANE_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
