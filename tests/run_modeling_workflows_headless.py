"""Blender background qualification for precision and multi-object modeling."""

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
from blenderboo.tools import mesh_objects as mesh_object_tools


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
    return result["data"]


def mesh_counts(obj: bpy.types.Object) -> dict[str, int]:
    return {
        "vertices": len(obj.data.vertices),
        "edges": len(obj.data.edges),
        "faces": len(obj.data.polygons),
        "loops": len(obj.data.loops),
    }


def point_object(name: str, points: list[tuple[float, float, float]]) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(points, [], [])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def object_from_bmesh(name: str, bm: bmesh.types.BMesh) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    bm.to_mesh(mesh)
    mesh.update(calc_edges=True, calc_edges_loose=True)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def select_only(obj: bpy.types.Object) -> None:
    for selected in list(bpy.context.selected_objects):
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


artifact_root = ROOT / "tests" / ".tmp_modeling_workflows_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "modeling_workflows_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

capabilities = require_ok(dispatch("system.capabilities", {"limit": 200}), "system.capabilities")
assert capabilities["capability_count"] == 134
mesh_toolset = next(item for item in capabilities["toolsets"] if item["name"] == "mesh")
assert mesh_toolset["tool_count"] == 5

# Precision operations share mesh.edit's one-shot BMesh writeback.
align_obj = point_object("AlignPoints", [(0, 0, -1), (1, 2, 3), (5, -2, 9)])
aligned = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": align_obj.name,
            "operations": [
                {
                    "op": "align",
                    "selection": {"domain": "VERT", "predicate": {"kind": "ALL"}},
                    "axis": "Z",
                    "mode": "VALUE",
                    "value": 2.5,
                }
            ],
            "dry_run": False,
        },
    ),
    "mesh.edit align",
)
assert aligned["changed"] is True
assert all(math.isclose(vertex.co.z, 2.5, abs_tol=1e-7) for vertex in align_obj.data.vertices)

distribute_obj = point_object("DistributePoints", [(0, 0, 0), (1, 0, 0), (4, 0, 0), (10, 0, 0)])
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": distribute_obj.name,
            "operations": [
                {
                    "op": "distribute",
                    "selection": {"domain": "VERT", "predicate": {"kind": "ALL"}},
                    "axis": "X",
                }
            ],
            "dry_run": False,
        },
    ),
    "mesh.edit distribute",
)
distributed = sorted(float(vertex.co.x) for vertex in distribute_obj.data.vertices)
assert all(
    math.isclose(actual, expected, abs_tol=1e-6)
    for actual, expected in zip(distributed, (0.0, 10.0 / 3.0, 20.0 / 3.0, 10.0))
)

snap_obj = point_object("SnapPoints", [(0.26, 0.74, 1.1), (1.24, -0.14, 2.2)])
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": snap_obj.name,
            "operations": [
                {
                    "op": "snap",
                    "selection": {"domain": "VERT", "predicate": {"kind": "ALL"}},
                    "mode": "GRID",
                    "axes": ["X", "Y"],
                    "increment": [0.5, 0.25, 1.0],
                },
                {"op": "snap", "mode": "POINT", "axes": ["Z"], "point": [99, 99, -2]},
            ],
            "dry_run": False,
        },
    ),
    "mesh.edit grid and point snap",
)
snapped = [tuple(float(value) for value in vertex.co) for vertex in snap_obj.data.vertices]
assert all(math.isclose(point[2], -2.0, abs_tol=1e-7) for point in snapped)
assert snapped[0][:2] == (0.5, 0.75)
assert snapped[1][:2] == (1.0, -0.25)

plane_obj = point_object("PlaneSnap", [(1, 2, 3), (-1, -2, 4)])
before_plane = [tuple(vertex.co) for vertex in plane_obj.data.vertices]
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": plane_obj.name,
            "operations": [
                {
                    "op": "snap",
                    "selection": {"domain": "VERT", "predicate": {"kind": "ALL"}},
                    "mode": "PLANE",
                    "point": [0, 0, 1],
                    "normal": [0, 0, 1],
                }
            ],
            "dry_run": False,
        },
    ),
    "mesh.edit plane snap",
)
assert all(math.isclose(vertex.co.z, 1.0, abs_tol=1e-7) for vertex in plane_obj.data.vertices)

atomic_obj = point_object("PrecisionAtomic", [(0, 0, 0), (2, 1, 3)])
atomic_before = [tuple(vertex.co) for vertex in atomic_obj.data.vertices]
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": atomic_obj.name,
            "operations": [
                {
                    "op": "align",
                    "selection": {"domain": "VERT", "predicate": {"kind": "ALL"}},
                    "axis": "Y",
                    "mode": "VALUE",
                    "value": 4,
                },
                {"op": "snap", "mode": "PLANE", "point": [0, 0, 0], "normal": [0, 0, 0]},
            ],
            "dry_run": False,
        },
    ),
    "InvalidParamsError",
    "mesh.edit precision atomic rejection",
)
assert [tuple(vertex.co) for vertex in atomic_obj.data.vertices] == atomic_before

# Join preserves target identity, world-space geometry, materials, UVs, and vertex groups.
context_anchor = bpy.data.objects.new("ContextAnchor", None)
bpy.context.scene.collection.objects.link(context_anchor)
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(1, 0, 0))
join_target = bpy.context.object
join_target.name = "JoinTarget"
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(4, 0, 0))
join_source = bpy.context.object
join_source.name = "JoinSource"
material_a = bpy.data.materials.new("JoinMaterialA")
material_b = bpy.data.materials.new("JoinMaterialB")
join_target.data.materials.append(material_a)
join_source.data.materials.append(material_b)
source_group = join_source.vertex_groups.new(name="SourceGroup")
source_group.add(list(range(len(join_source.data.vertices))), 0.75, "REPLACE")
target_identity = join_target.as_pointer()
select_only(context_anchor)
join_plan = require_ok(
    dispatch(
        "mesh.join",
        {"target_object": join_target.name, "source_objects": [join_source.name]},
    ),
    "mesh.join dry run",
)
assert join_plan["dry_run"] is True and join_plan["expected_after"]["vertices"] == 16
assert bpy.context.scene.objects.get("JoinSource") is join_source
require_error(
    dispatch(
        "mesh.join",
        {
            "target_object": join_target.name,
            "source_objects": [join_source.name],
            "dry_run": False,
        },
    ),
    "ConfirmationRequiredError",
    "mesh.join confirmation gate",
)
joined = require_ok(
    dispatch(
        "mesh.join",
        {
            "target_object": join_target.name,
            "source_objects": [join_source.name],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.join apply",
)
assert joined["changed"] is True
assert join_target.as_pointer() == target_identity
assert bpy.context.scene.objects.get("JoinSource") is None
assert mesh_counts(join_target) == {"vertices": 16, "edges": 24, "faces": 12, "loops": 48}
assert {slot.name for slot in join_target.data.materials} == {material_a.name, material_b.name}
assert len(join_target.data.uv_layers) >= 1
assert join_target.vertex_groups.get("SourceGroup") is not None
assert bpy.context.view_layer.objects.active is context_anchor
assert {obj.name for obj in bpy.context.selected_objects} == {context_anchor.name}
joined_bounds = require_ok(
    dispatch(
        "mesh.measure",
        {
            "object_name": join_target.name,
            "queries": [
                {
                    "type": "SELECTION_BOUNDS",
                    "domain": "VERT",
                    "predicate": {"kind": "ALL"},
                    "space": "WORLD",
                }
            ],
        },
    ),
    "mesh.join world transform preservation",
)["results"][0]["bounds"]
assert all(
    math.isclose(value, expected, abs_tol=1e-6)
    for value, expected in zip(joined_bounds["dimensions"], (5.0, 2.0, 2.0))
)

# Removing a source with object-level behavior is refused; copy-join keeps it intact.
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(8, 0, 0))
keep_target = bpy.context.object
keep_target.name = "KeepJoinTarget"
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(10, 0, 0))
keep_source = bpy.context.object
keep_source.name = "KeepJoinSource"
keep_source.modifiers.new("ProtectedBevel", "BEVEL")
require_error(
    dispatch(
        "mesh.join",
        {"target_object": keep_target.name, "source_objects": [keep_source.name]},
    ),
    "InvalidStateError",
    "mesh.join guarded source removal",
)
require_ok(
    dispatch(
        "mesh.join",
        {
            "target_object": keep_target.name,
            "source_objects": [keep_source.name],
            "keep_sources": True,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.join keep sources",
)
assert bpy.context.scene.objects.get(keep_source.name) is keep_source
assert keep_source.modifiers.get("ProtectedBevel") is not None
assert len(keep_target.data.vertices) == 16

# Force a post-join validation failure and prove target/source recovery.
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(12, 0, 0))
rollback_target = bpy.context.object
rollback_target.name = "JoinRollbackTarget"
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(14, 0, 0))
rollback_source = bpy.context.object
rollback_source.name = "JoinRollbackSource"
rollback_target_identity = rollback_target.as_pointer()
original_mesh_counts = mesh_counts(rollback_target)
real_mesh_counts = mesh_object_tools._mesh_counts


def mismatched_join_counts(mesh: bpy.types.Mesh) -> dict[str, int]:
    counts = real_mesh_counts(mesh)
    if mesh is rollback_target.data and counts["vertices"] == 16:
        counts = dict(counts)
        counts["vertices"] += 1
    return counts


mesh_object_tools._mesh_counts = mismatched_join_counts
try:
    join_error = require_error(
        dispatch(
            "mesh.join",
            {
                "target_object": rollback_target.name,
                "source_objects": [rollback_source.name],
                "dry_run": False,
                "confirm": True,
            },
        ),
        "MutationApplyError",
        "mesh.join rollback",
    )
finally:
    mesh_object_tools._mesh_counts = real_mesh_counts
assert join_error["changed"] is False
assert rollback_target.as_pointer() == rollback_target_identity
assert bpy.context.scene.objects.get("JoinRollbackSource") is not None
assert mesh_counts(rollback_target) == original_mesh_counts

# Loose separation creates deterministic result objects and preserves context.
bm = bmesh.new()
first = bmesh.ops.create_cube(bm, size=2.0)["verts"]
bmesh.ops.translate(bm, verts=first, vec=Vector((-2, 0, 0)))
second = bmesh.ops.create_cube(bm, size=2.0)["verts"]
bmesh.ops.translate(bm, verts=second, vec=Vector((2, 0, 0)))
loose_obj = object_from_bmesh("LooseSource", bm)
bm.free()
select_only(context_anchor)
loose_plan = require_ok(
    dispatch(
        "mesh.separate",
        {"object_name": loose_obj.name, "mode": "LOOSE", "name_prefix": "LoosePart"},
    ),
    "mesh.separate loose dry run",
)
assert loose_plan["plan"]["result_object_count"] == 2
loose_result = require_ok(
    dispatch(
        "mesh.separate",
        {
            "object_name": loose_obj.name,
            "mode": "LOOSE",
            "name_prefix": "LoosePart",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.separate loose apply",
)
assert loose_result["created_objects"] == ["LoosePart_001"]
assert bpy.context.view_layer.objects.active is context_anchor
assert {obj.name for obj in bpy.context.selected_objects} == {context_anchor.name}

# Material separation uses actual material indices and preserves both slots.
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(18, 0, 0))
material_obj = bpy.context.object
material_obj.name = "MaterialSource"
material_obj.data.materials.append(material_a)
material_obj.data.materials.append(material_b)
for index, polygon in enumerate(material_obj.data.polygons):
    polygon.material_index = 0 if index < 3 else 1
material_result = require_ok(
    dispatch(
        "mesh.separate",
        {
            "object_name": material_obj.name,
            "mode": "MATERIAL",
            "name_prefix": "MaterialPart",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.separate material",
)
assert material_result["created_objects"] == ["MaterialPart_001"]

# Selection separation is rolled back when duplicated boundary data exceeds the result budget.
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(22, 0, 0))
selection_obj = bpy.context.object
selection_obj.name = "SelectionRollback"
selection_identity = selection_obj.as_pointer()
selection_before = mesh_counts(selection_obj)
assert sum(selection_before.values()) == 50
selection_group = selection_obj.vertex_groups.new(name="RestoreWeights")
selection_group.add([0, 1], 0.5, "REPLACE")
separate_error = require_error(
    dispatch(
        "mesh.separate",
        {
            "object_name": selection_obj.name,
            "mode": "SELECTION",
            "selection": {
                "domain": "FACE",
                "predicate": {"kind": "NORMAL", "direction": [0, 0, 1], "min_dot": 0.9},
            },
            "name_prefix": "ShouldRollback",
            "max_elements": 50,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "MutationApplyError",
    "mesh.separate result-budget rollback",
)
assert separate_error["changed"] is False
assert selection_obj.as_pointer() == selection_identity
assert mesh_counts(selection_obj) == selection_before
assert selection_obj.vertex_groups.get("RestoreWeights") is not None
assert bpy.context.scene.objects.get("ShouldRollback_001") is None

selection_result = require_ok(
    dispatch(
        "mesh.separate",
        {
            "object_name": selection_obj.name,
            "mode": "SELECTION",
            "selection": {
                "domain": "FACE",
                "predicate": {"kind": "NORMAL", "direction": [0, 0, 1], "min_dot": 0.9},
            },
            "name_prefix": "SelectionPart",
            "max_elements": 100,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "mesh.separate semantic selection",
)
assert selection_result["created_objects"] == ["SelectionPart_001"]

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("MODELING_WORKFLOWS_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir}")
