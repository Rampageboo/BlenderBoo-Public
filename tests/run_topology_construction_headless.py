"""Blender background qualification for deterministic topology construction."""

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


def face_pair(obj: bpy.types.Object, adjacent: bool) -> list[int]:
    ordered = [int(index) for index in obj.data.polygons[0].vertices]
    return [ordered[0], ordered[1 if adjacent else 2]]


def edge_indices(obj: bpy.types.Object, pairs: set[frozenset[int]]) -> list[int]:
    return [
        int(edge.index)
        for edge in obj.data.edges
        if frozenset(int(index) for index in edge.vertices) in pairs
    ]


artifact_root = ROOT / "tests" / ".tmp_topology_construction_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "topology_construction_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

# Connect splits exactly one shared face and remains dry-run-first.
connect_object = cube("ConnectCube")
connect_vertices = face_pair(connect_object, adjacent=False)
connect_operation = {
    "op": "connect",
    "selection": {
        "domain": "VERT",
        "predicate": {"kind": "INDICES", "indices": connect_vertices},
    },
}
connect_before = (
    len(connect_object.data.vertices),
    len(connect_object.data.edges),
    len(connect_object.data.polygons),
)
connect_plan = require_ok(
    dispatch(
        "mesh.edit",
        {"object_name": connect_object.name, "operations": [connect_operation]},
    ),
    "same-face connect dry run",
)
assert connect_plan["confirmation_required"] is True
assert connect_plan["journal"][0]["affected"] == 1
assert (
    len(connect_object.data.vertices),
    len(connect_object.data.edges),
    len(connect_object.data.polygons),
) == connect_before
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": connect_object.name,
            "operations": [connect_operation],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "same-face connect apply",
)
assert (
    len(connect_object.data.vertices),
    len(connect_object.data.edges),
    len(connect_object.data.polygons),
) == (8, 13, 7)

adjacent_object = cube("AdjacentConnectCube")
adjacent_before = (len(adjacent_object.data.edges), len(adjacent_object.data.polygons))
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": adjacent_object.name,
            "operations": [
                {
                    "op": "connect",
                    "selection": {
                        "domain": "VERT",
                        "predicate": {
                            "kind": "INDICES",
                            "indices": face_pair(adjacent_object, adjacent=True),
                        },
                    },
                }
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "InvalidStateError",
    "already-connected vertex rejection",
)
assert (len(adjacent_object.data.edges), len(adjacent_object.data.polygons)) == adjacent_before

# Grid Fill selects two opposite open chains; the unselected side edges are rails.
grid_mesh = bpy.data.meshes.new("GridFillMesh")
grid_mesh.from_pydata(
    [(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 2, 0), (1, 2, 0), (2, 2, 0)],
    [(0, 1), (1, 2), (3, 4), (4, 5), (0, 3), (2, 5)],
    [],
)
grid_object = bpy.data.objects.new("GridFillRails", grid_mesh)
bpy.context.scene.collection.objects.link(grid_object)
chain_indices = edge_indices(
    grid_object,
    {frozenset(pair) for pair in ((0, 1), (1, 2), (3, 4), (4, 5))},
)
assert len(chain_indices) == 4
grid_operation = {
    "op": "grid_fill",
    "selection": {
        "domain": "EDGE",
        "predicate": {"kind": "INDICES", "indices": chain_indices},
    },
    "use_interp_simple": True,
}
grid_plan = require_ok(
    dispatch("mesh.edit", {"object_name": grid_object.name, "operations": [grid_operation]}),
    "two-chain grid fill dry run",
)
assert grid_plan["confirmation_required"] is True
assert grid_plan["journal"][0]["affected"] == 2
assert len(grid_object.data.polygons) == 0
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": grid_object.name,
            "operations": [grid_operation],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "two-chain grid fill apply",
)
assert (len(grid_object.data.vertices), len(grid_object.data.edges), len(grid_object.data.polygons)) == (6, 7, 2)
assert all(len(face.vertices) == 4 for face in grid_object.data.polygons)

closed_mesh = bpy.data.meshes.new("ClosedGridFillMesh")
closed_mesh.from_pydata(
    [(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 2, 0), (1, 2, 0), (2, 2, 0)],
    [(0, 1), (1, 2), (3, 4), (4, 5), (0, 3), (2, 5)],
    [],
)
closed_object = bpy.data.objects.new("ClosedGridFill", closed_mesh)
bpy.context.scene.collection.objects.link(closed_object)
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": closed_object.name,
            "operations": [
                {
                    "op": "grid_fill",
                    "selection": {"domain": "EDGE", "predicate": {"kind": "ALL"}},
                }
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "InvalidStateError",
    "closed-loop grid fill rejection",
)
assert len(closed_object.data.polygons) == 0 and len(closed_object.data.edges) == 6

# Poke creates one center vertex and four triangles for a cube quad.
poke_object = cube("PokeCube")
poke_face = poke_object.data.polygons[0]
poke_center = sum(
    (poke_object.data.vertices[int(index)].co for index in poke_face.vertices),
    Vector(),
) / len(poke_face.vertices)
poke_result = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": poke_object.name,
            "operations": [
                {
                    "op": "poke",
                    "selection": {
                        "domain": "FACE",
                        "predicate": {"kind": "INDICES", "indices": [int(poke_face.index)]},
                    },
                    "center_mode": "MEAN_WEIGHTED",
                }
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "single-face poke apply",
)
assert poke_result["journal"][0]["affected"] == 5
assert (len(poke_object.data.vertices), len(poke_object.data.edges), len(poke_object.data.polygons)) == (9, 16, 9)
assert any((vertex.co - poke_center).length <= 1e-6 for vertex in poke_object.data.vertices)

# Plane Bisect without clearing either side is the deterministic headless Knife path.
knife_object = cube("KnifeBisectCube")
knife_plan = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": knife_object.name,
            "operations": [
                {
                    "op": "bisect",
                    "plane_co": [0.0, 0.0, 0.0],
                    "plane_no": [1.0, 0.0, 0.0],
                }
            ],
        },
    ),
    "no-clear plane knife dry run",
)
assert knife_plan["confirmation_required"] is True
assert (len(knife_object.data.vertices), len(knife_object.data.edges), len(knife_object.data.polygons)) == (8, 12, 6)
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": knife_object.name,
            "operations": [
                {
                    "op": "bisect",
                    "plane_co": [0.0, 0.0, 0.0],
                    "plane_no": [1.0, 0.0, 0.0],
                }
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "no-clear plane knife apply",
)
xs = [float(vertex.co.x) for vertex in knife_object.data.vertices]
assert (len(knife_object.data.vertices), len(knife_object.data.edges), len(knife_object.data.polygons)) == (12, 20, 10)
assert sum(math.isclose(value, 0.0, abs_tol=1e-6) for value in xs) == 4
assert min(xs) < -0.9 and max(xs) > 0.9

# A later rejected construction discards an earlier valid connect in the copied BMesh.
rollback_object = cube("ConstructionRollbackCube")
rollback_before = (len(rollback_object.data.vertices), len(rollback_object.data.edges), len(rollback_object.data.polygons))
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": rollback_object.name,
            "operations": [
                {
                    "op": "connect",
                    "selection": {
                        "domain": "VERT",
                        "predicate": {
                            "kind": "INDICES",
                            "indices": face_pair(rollback_object, adjacent=False),
                        },
                    },
                },
                {
                    "op": "grid_fill",
                    "selection": {"domain": "EDGE", "predicate": {"kind": "ALL"}},
                },
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "InvalidStateError",
    "topology-construction atomic rejection",
)
assert (
    len(rollback_object.data.vertices),
    len(rollback_object.data.edges),
    len(rollback_object.data.polygons),
) == rollback_before

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("TOPOLOGY_CONSTRUCTION_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
