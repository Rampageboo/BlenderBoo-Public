"""Blender background qualification for true loop cut and directional edge slide."""

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


def cube(name: str) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.object
    obj.name = name
    return obj


def vertical_edge_index(obj: bpy.types.Object) -> int:
    for edge in obj.data.edges:
        first, second = (obj.data.vertices[index].co for index in edge.vertices)
        if abs(float((second - first).z)) > 1.9:
            return int(edge.index)
    raise AssertionError("cube has no vertical edge")


def loop_slide_operations(
    obj: bpy.types.Object,
    factor: float,
    direction: list[float] | None = None,
) -> list[dict]:
    return [
        {
            "op": "loop_cut",
            "selection": {
                "domain": "EDGE",
                "predicate": {"kind": "EDGE_RING", "seed_index": vertical_edge_index(obj)},
            },
            "cuts": 1,
        },
        {
            "op": "edge_slide",
            "direction": direction if direction is not None else [0.0, 0.0, 1.0],
            "factor": factor,
        },
    ]


artifact_root = ROOT / "tests" / ".tmp_topology_precision_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "topology_precision_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

# Dry-run plans the complete chain without touching the source mesh.
positive = cube("PositiveSlideCube")
positive_counts = (len(positive.data.vertices), len(positive.data.edges), len(positive.data.polygons))
positive_ops = loop_slide_operations(positive, 0.25)
planned = require_ok(
    dispatch("mesh.edit", {"object_name": positive.name, "operations": positive_ops}),
    "loop cut plus edge slide dry run",
)
assert planned["dry_run"] is True and planned["confirmation_required"] is True
assert planned["journal"][0]["op"] == "loop_cut"
assert planned["journal"][0]["affected"] == 4
assert planned["journal"][1]["op"] == "edge_slide"
assert planned["journal"][1]["affected"] == 4
assert (len(positive.data.vertices), len(positive.data.edges), len(positive.data.polygons)) == positive_counts

# Apply writes once after both operations succeed and places the new ring at z=0.25.
applied = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": positive.name,
            "operations": positive_ops,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "loop cut plus positive edge slide apply",
)
assert applied["changed"] is True
assert (len(positive.data.vertices), len(positive.data.edges), len(positive.data.polygons)) == (12, 20, 10)
positive_ring = [
    vertex for vertex in positive.data.vertices
    if math.isclose(float(vertex.co.z), 0.25, abs_tol=1e-6)
]
assert len(positive_ring) == 4

# A negative factor picks the opposite topological side.
negative = cube("NegativeSlideCube")
negative_ops = loop_slide_operations(negative, -0.5)
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": negative.name,
            "operations": negative_ops,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "loop cut plus negative edge slide apply",
)
negative_ring = [
    vertex for vertex in negative.data.vertices
    if math.isclose(float(vertex.co.z), -0.5, abs_tol=1e-6)
]
assert len(negative_ring) == 4

# A partial edge ring is refused before any source writeback.
partial = cube("PartialRingCube")
partial_counts = (len(partial.data.vertices), len(partial.data.edges), len(partial.data.polygons))
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": partial.name,
            "operations": [
                {
                    "op": "loop_cut",
                    "selection": {
                        "domain": "EDGE",
                        "predicate": {
                            "kind": "INDICES",
                            "indices": [vertical_edge_index(partial)],
                        },
                    },
                }
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "InvalidStateError",
    "partial loop cut rejection",
)
assert (len(partial.data.vertices), len(partial.data.edges), len(partial.data.polygons)) == partial_counts

# A direction perpendicular to both side edges rejects the whole in-memory chain.
ambiguous = cube("AmbiguousSlideCube")
ambiguous_counts = (len(ambiguous.data.vertices), len(ambiguous.data.edges), len(ambiguous.data.polygons))
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": ambiguous.name,
            "operations": loop_slide_operations(ambiguous, 0.25, [1.0, 0.0, 0.0]),
            "dry_run": False,
            "confirm": True,
        },
    ),
    "InvalidStateError",
    "ambiguous edge slide atomic rejection",
)
assert (len(ambiguous.data.vertices), len(ambiguous.data.edges), len(ambiguous.data.polygons)) == ambiguous_counts

# A face plus one of its edges defines a reproducible tilted U/V/N workplane.
mesh = bpy.data.meshes.new("WorkplaneMesh")
mesh.from_pydata(
    [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 1.0),
        (1.0, 1.0, 1.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (1.0, 1.0, 0.0),
    ],
    [(4, 5)],
    [(0, 1, 2, 3)],
)
workplane_object = bpy.data.objects.new("TiltedWorkplane", mesh)
bpy.context.scene.collection.objects.link(workplane_object)
reference_edge = next(
    int(edge.index) for edge in mesh.edges if set(int(index) for index in edge.vertices) == {0, 1}
)
loose_edge = next(
    int(edge.index) for edge in mesh.edges if set(int(index) for index in edge.vertices) == {4, 5}
)
workplane_ops = [
    {
        "op": "workplane",
        "selection": {
            "domain": "VERT",
            "predicate": {"kind": "INDICES", "indices": [4, 5]},
        },
        "plane": {"source": "FACE", "face_index": 0, "edge_index": reference_edge},
        "mode": "PROJECT",
    },
    {
        "op": "workplane",
        "plane": {"source": "FACE", "face_index": 0, "edge_index": reference_edge},
        "mode": "GRID",
        "axes": ["U", "V", "N"],
        "increment": [0.25, 0.25, 1.0],
    },
]
workplane_before = [mesh.vertices[index].co.copy() for index in (4, 5)]
workplane_plan = require_ok(
    dispatch("mesh.edit", {"object_name": workplane_object.name, "operations": workplane_ops}),
    "face-derived workplane dry run",
)
assert workplane_plan["dry_run"] is True
assert workplane_plan["confirmation_required"] is False
assert workplane_plan["journal"][0]["selection"]["workplane"]["source"] == "FACE"
assert all((mesh.vertices[index].co - before).length <= 1e-9 for index, before in zip((4, 5), workplane_before))
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": workplane_object.name,
            "operations": workplane_ops,
            "dry_run": False,
        },
    ),
    "face-derived workplane project and grid apply",
)
origin = Vector((0.5, 0.5, 0.5))
normal = Vector((-1.0, 0.0, 1.0)).normalized()
u_axis = Vector((1.0, 0.0, 1.0)).normalized()
v_axis = normal.cross(u_axis).normalized()
for index in (4, 5):
    delta = mesh.vertices[index].co - origin
    coordinates = [float(delta.dot(axis)) for axis in (u_axis, v_axis, normal)]
    assert math.isclose(coordinates[2], 0.0, abs_tol=1e-6)
    assert math.isclose(coordinates[0] / 0.25, round(coordinates[0] / 0.25), abs_tol=1e-6)
    assert math.isclose(coordinates[1] / 0.25, round(coordinates[1] / 0.25), abs_tol=1e-6)

# A face-derived frame refuses an edge that does not belong to the reference face.
invalid_workplane_before = [mesh.vertices[index].co.copy() for index in (4, 5)]
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": workplane_object.name,
            "operations": [
                {
                    "op": "workplane",
                    "selection": {
                        "domain": "VERT",
                        "predicate": {"kind": "INDICES", "indices": [4, 5]},
                    },
                    "plane": {"source": "FACE", "face_index": 0, "edge_index": loose_edge},
                    "mode": "PROJECT",
                }
            ],
            "dry_run": False,
        },
    ),
    "InvalidStateError",
    "invalid workplane reference edge rejection",
)
assert all(
    (mesh.vertices[index].co - before).length <= 1e-9
    for index, before in zip((4, 5), invalid_workplane_before)
)

# An explicit frame supports exact alignment without any scene helper state.
explicit = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": workplane_object.name,
            "operations": [
                {
                    "op": "workplane",
                    "selection": {
                        "domain": "VERT",
                        "predicate": {"kind": "INDICES", "indices": [4, 5]},
                    },
                    "plane": {
                        "source": "EXPLICIT",
                        "origin": [0.0, 0.0, 0.0],
                        "normal": [0.0, 0.0, 1.0],
                        "u_axis": [1.0, 0.0, 0.0],
                    },
                    "mode": "ALIGN",
                    "axes": ["N"],
                    "values": [0.0, 0.0, 2.0],
                }
            ],
            "dry_run": False,
        },
    ),
    "explicit workplane align apply",
)
assert explicit["changed"] is True
assert all(math.isclose(float(mesh.vertices[index].co.z), 2.0, abs_tol=1e-6) for index in (4, 5))

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("TOPOLOGY_PRECISION_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
