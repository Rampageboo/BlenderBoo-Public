"""Blender background qualification for guarded modifiers and Geometry Nodes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

# Exercise repository source even when Blender has already discovered an installed copy.
for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo import dispatch
from blenderboo.tools import geometry as geometry_tools


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


def cube_object(
    name: str,
    location: tuple[float, float, float],
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(location=location, scale=scale)
    obj = bpy.context.object
    obj.name = name
    return obj


artifact_root = ROOT / "tests" / ".tmp_geometry_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.object
cube.name = "GeometrySmokeCube"
scene_path = artifact_dir / "geometry_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

capabilities = require_ok(
    dispatch("system.capabilities", {"limit": 200}),
    "system.capabilities",
)
assert capabilities["capability_count"] >= 134
assert capabilities["count"] >= 20
assert any(item["name"] == "modifier" for item in capabilities["toolsets"])
assert any(item["name"] == "geometry_nodes" for item in capabilities["toolsets"])

mesh = require_ok(
    dispatch("mesh.inspect", {"object_name": cube.name, "include_topology": True}),
    "mesh.inspect topology",
)
assert mesh["counts"]["vertices"] == 8
assert mesh["topology"]["closed_manifold_candidate"] is True

bevel = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": cube.name,
            "type": "BEVEL",
            "name": "SmokeBevel",
            "settings": {"width": 0.1, "segments": 2},
        },
    ),
    "modifier.add bevel",
)
assert bevel["modifier"] == "SmokeBevel"
subdivision = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": cube.name,
            "type": "SUBSURF",
            "name": "SmokeSubdivision",
            "settings": {"levels": 1, "render_levels": 1},
        },
    ),
    "modifier.add subdivision",
)
assert subdivision["modifier"] == "SmokeSubdivision"

reorder_plan = require_ok(
    dispatch(
        "modifier.reorder",
        {"object_name": cube.name, "modifier_name": "SmokeSubdivision", "index": 0},
    ),
    "modifier.reorder dry run",
)
assert reorder_plan["dry_run"] is True and reorder_plan["changed"] is False
reordered = require_ok(
    dispatch(
        "modifier.reorder",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeSubdivision",
            "index": 0,
            "dry_run": False,
        },
    ),
    "modifier.reorder apply",
)
assert reordered["changed"] is True

patched = require_ok(
    dispatch(
        "modifier.patch",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeBevel",
            "settings": {"width": 0.2, "segments": 3},
            "show_render": False,
        },
    ),
    "modifier.patch",
)
assert patched["changed"] is True
stack = require_ok(
    dispatch("modifier.inspect", {"object_name": cube.name}),
    "modifier.inspect",
)
assert [item["name"] for item in stack["modifiers"]] == ["SmokeSubdivision", "SmokeBevel"]
assert stack["modifiers"][1]["settings"]["segments"] == 3

remove_plan = require_ok(
    dispatch(
        "modifier.remove",
        {"object_name": cube.name, "modifier_name": "SmokeSubdivision"},
    ),
    "modifier.remove dry run",
)
assert remove_plan["dry_run"] is True and remove_plan["confirmation_required"] is True
removed = require_ok(
    dispatch(
        "modifier.remove",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeSubdivision",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "modifier.remove apply",
)
assert removed["changed"] is True

apply_plan = require_ok(
    dispatch(
        "modifier.apply",
        {"object_name": cube.name, "modifier_name": "SmokeBevel"},
    ),
    "modifier.apply dry run",
)
assert apply_plan["dry_run"] is True and apply_plan["confirmation_required"] is True
applied = require_ok(
    dispatch(
        "modifier.apply",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeBevel",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "modifier.apply",
)
assert applied["changed"] is True
assert cube.modifiers.get("SmokeBevel") is None
assert len(cube.data.vertices) > 8

# Qualify the hard-surface modifier expansion against Blender 5.2 RNA.
axis = bpy.data.objects.new("HardSurfaceAxis", None)
bpy.context.scene.collection.objects.link(axis)

curve_data = bpy.data.curves.new("HardSurfaceCurveData", type="CURVE")
curve_data.dimensions = "3D"
curve_spline = curve_data.splines.new("POLY")
curve_spline.points.add(1)
curve_spline.points[0].co = (0.0, 0.0, 0.0, 1.0)
curve_spline.points[1].co = (2.0, 0.0, 0.0, 1.0)
curve_target = bpy.data.objects.new("HardSurfaceCurve", curve_data)
bpy.context.scene.collection.objects.link(curve_target)

lattice_data = bpy.data.lattices.new("HardSurfaceLatticeData")
lattice_target = bpy.data.objects.new("HardSurfaceLattice", lattice_data)
bpy.context.scene.collection.objects.link(lattice_target)

texture = bpy.data.textures.new("HardSurfaceTexture", type="CLOUDS")

boolean_source = cube_object("BooleanSource", (4.0, 0.0, 0.0))
boolean_cutter = cube_object(
    "BooleanCutter", (4.75, 0.0, 0.0), scale=(0.5, 0.75, 0.75)
)
boolean = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": boolean_source.name,
            "type": "BOOLEAN",
            "name": "HardSurfaceBoolean",
            "settings": {
                "operation": "DIFFERENCE",
                "solver": "EXACT",
                "operand_type": "OBJECT",
                "object": boolean_cutter.name,
                "use_hole_tolerant": True,
            },
        },
    ),
    "modifier.add boolean",
)
assert boolean["detail"]["settings"]["object"] == boolean_cutter.name
assert boolean["detail"]["settings"]["solver"] == "EXACT"

shrink_source = cube_object("ShrinkwrapSource", (8.0, 0.0, 0.0))
shrink_source.vertex_groups.new(name="ProjectionMask").add(
    range(len(shrink_source.data.vertices)), 1.0, "REPLACE"
)
bpy.ops.mesh.primitive_uv_sphere_add(location=(8.0, 0.0, 0.0), segments=16, ring_count=8)
shrink_target = bpy.context.object
shrink_target.name = "ShrinkwrapTarget"
shrink = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": shrink_source.name,
            "type": "SHRINKWRAP",
            "name": "HardSurfaceShrinkwrap",
            "settings": {
                "wrap_method": "NEAREST_SURFACEPOINT",
                "wrap_mode": "ABOVE_SURFACE",
                "target": shrink_target.name,
                "offset": 0.01,
                "vertex_group": "ProjectionMask",
            },
        },
    ),
    "modifier.add shrinkwrap",
)
assert shrink["detail"]["settings"]["target"] == shrink_target.name

simple_source = cube_object("SimpleDeformSource", (12.0, 0.0, 0.0))
simple_deform = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": simple_source.name,
            "type": "SIMPLE_DEFORM",
            "name": "HardSurfaceSimpleDeform",
            "settings": {
                "deform_method": "TWIST",
                "deform_axis": "Z",
                "angle": 0.25,
                "limits": [0.0, 0.75],
                "origin": axis.name,
            },
        },
    ),
    "modifier.add simple deform",
)
assert simple_deform["detail"]["settings"]["limits"] == [0.0, 0.75]

curve_source = cube_object("CurveDeformSource", (16.0, 0.0, 0.0))
curve_modifier = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": curve_source.name,
            "type": "CURVE",
            "name": "HardSurfaceCurveDeform",
            "settings": {"object": curve_target.name, "deform_axis": "POS_X"},
        },
    ),
    "modifier.add curve",
)
assert curve_modifier["detail"]["settings"]["object"] == curve_target.name

lattice_source = cube_object("LatticeDeformSource", (20.0, 0.0, 0.0))
lattice_modifier = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": lattice_source.name,
            "type": "LATTICE",
            "name": "HardSurfaceLatticeDeform",
            "settings": {"object": lattice_target.name, "strength": 0.75},
        },
    ),
    "modifier.add lattice",
)
assert lattice_modifier["detail"]["settings"]["object"] == lattice_target.name

remesh_source = cube_object("RemeshSource", (24.0, 0.0, 0.0))
remesh = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": remesh_source.name,
            "type": "REMESH",
            "name": "HardSurfaceRemesh",
            "settings": {
                "mode": "VOXEL",
                "voxel_size": 0.25,
                "octree_depth": 5,
                "use_remove_disconnected": False,
            },
        },
    ),
    "modifier.add remesh",
)
assert remesh["detail"]["settings"]["mode"] == "VOXEL"

screw_source = cube_object("ScrewSource", (28.0, 0.0, 0.0))
screw = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": screw_source.name,
            "type": "SCREW",
            "name": "HardSurfaceScrew",
            "settings": {
                "object": axis.name,
                "axis": "Z",
                "angle": 6.283185307179586,
                "steps": 12,
                "render_steps": 16,
                "iterations": 1,
                "use_merge_vertices": True,
            },
        },
    ),
    "modifier.add screw",
)
assert screw["detail"]["settings"]["steps"] == 12

displace_source = cube_object("DisplaceSource", (32.0, 0.0, 0.0))
displace_source.data.uv_layers.new(name="DetailUV")
displace_source.vertex_groups.new(name="DisplaceMask").add(
    range(len(displace_source.data.vertices)), 1.0, "REPLACE"
)
displace = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": displace_source.name,
            "type": "DISPLACE",
            "name": "HardSurfaceDisplace",
            "settings": {
                "texture": texture.name,
                "texture_coords": "OBJECT",
                "texture_coords_object": axis.name,
                "direction": "NORMAL",
                "strength": 0.1,
                "uv_layer": "DetailUV",
                "vertex_group": "DisplaceMask",
            },
        },
    ),
    "modifier.add displace",
)
assert displace["detail"]["settings"]["texture"] == texture.name

boolean_collection = bpy.data.collections.new("BooleanOperands")
bpy.context.scene.collection.children.link(boolean_collection)
collection_cutter = cube_object("CollectionBooleanCutter", (36.5, 0.0, 0.0))
boolean_collection.objects.link(collection_cutter)
collection_source = cube_object("CollectionBooleanSource", (36.0, 0.0, 0.0))
collection_boolean = require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": collection_source.name,
            "type": "BOOLEAN",
            "name": "CollectionBoolean",
            "settings": {
                "operand_type": "COLLECTION",
                "collection": boolean_collection.name,
                "operation": "UNION",
            },
        },
    ),
    "modifier.add boolean collection",
)
assert collection_boolean["detail"]["settings"]["collection"] == boolean_collection.name

require_error(
    dispatch(
        "modifier.add",
        {
            "object_name": curve_source.name,
            "type": "CURVE",
            "name": "RejectedCurveSetting",
            "settings": {"not_a_blender_property": boolean_cutter.name},
        },
    ),
    "InvalidParamsError",
    "modifier unknown RNA setting rejection",
)
assert curve_source.modifiers.get("RejectedCurveSetting") is None
require_error(
    dispatch(
        "modifier.add",
        {
            "object_name": boolean_source.name,
            "type": "BOOLEAN",
            "name": "RejectedSelfBoolean",
            "settings": {"object": boolean_source.name},
        },
    ),
    "InvalidParamsError",
    "modifier self-reference rejection",
)
require_error(
    dispatch(
        "modifier.patch",
        {
            "object_name": screw_source.name,
            "modifier_name": "HardSurfaceScrew",
            "settings": {"steps": 10000, "iterations": 10000},
        },
    ),
    "InvalidParamsError",
    "modifier screw workload rejection",
)
require_error(
    dispatch(
        "modifier.patch",
        {
            "object_name": remesh_source.name,
            "modifier_name": "HardSurfaceRemesh",
            "settings": {"voxel_size": 0.000001},
        },
    ),
    "InvalidParamsError",
    "modifier remesh voxel-budget rejection",
)
require_error(
    dispatch(
        "modifier.add",
        {
            "object_name": displace_source.name,
            "type": "DISPLACE",
            "name": "RejectedMissingTexture",
            "settings": {"texture": "MissingTexture"},
        },
    ),
    "NotFoundError",
    "modifier texture reference rejection",
)

rollback_source = cube_object("ModifierRollbackSource", (40.0, 0.0, 0.0))
rollback_original = cube_object("ModifierRollbackOriginal", (40.5, 0.0, 0.0))
rollback_next = cube_object("ModifierRollbackNext", (41.0, 0.0, 0.0))
require_ok(
    dispatch(
        "modifier.add",
        {
            "object_name": rollback_source.name,
            "type": "BOOLEAN",
            "name": "RollbackBoolean",
            "settings": {"object": rollback_original.name, "operation": "DIFFERENCE"},
        },
    ),
    "modifier pointer rollback setup",
)
real_validated_settings = geometry_tools._validated_settings


def forced_runtime_failure(*args, **kwargs):
    return {"object": rollback_next, "operation": "INVALID_OPERATION"}


geometry_tools._validated_settings = forced_runtime_failure
try:
    rollback_error = require_error(
        dispatch(
            "modifier.patch",
            {
                "object_name": rollback_source.name,
                "modifier_name": "RollbackBoolean",
                "settings": {"object": rollback_next.name},
            },
        ),
        "MutationApplyError",
        "modifier pointer rollback",
    )
finally:
    geometry_tools._validated_settings = real_validated_settings
assert rollback_error["changed"] is False
assert rollback_source.modifiers["RollbackBoolean"].object is rollback_original
assert all(item["status"] == "rolled_back" for item in rollback_error["journal"])

boolean_stack = require_ok(
    dispatch(
        "modifier.inspect",
        {"object_name": boolean_source.name, "modifier_name": "HardSurfaceBoolean"},
    ),
    "modifier.inspect hard-surface settings",
)
assert boolean_stack["modifier_detail"]["settings"]["object"] == boolean_cutter.name

boolean_apply = require_ok(
    dispatch(
        "modifier.apply",
        {
            "object_name": boolean_source.name,
            "modifier_name": "HardSurfaceBoolean",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "modifier.apply boolean",
)
assert boolean_apply["changed"] is True
assert boolean_source.modifiers.get("HardSurfaceBoolean") is None
assert len(boolean_source.data.vertices) > 8
print("PASS hard-surface modifier contracts")

created = require_ok(
    dispatch(
        "geometry_nodes.create",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeGeometry",
            "node_group_name": "Smoke Geometry Group",
        },
    ),
    "geometry_nodes.create",
)
assert created["modifier"] == "SmokeGeometry"
assert created["detail"]["node_count"] == 2
assert created["detail"]["link_count"] == 1

operations = [
    {
        "op": "add_node",
        "node_type": "GeometryNodeTransform",
        "name": "SmokeTransform",
        "location": [0.0, 0.0],
    },
    {
        "op": "set_input",
        "node": "SmokeTransform",
        "input": "Translation",
        "value": [1.0, 0.0, 0.0],
    },
    {
        "op": "connect",
        "from_node": "Group Input",
        "from_output": "Geometry",
        "to_node": "SmokeTransform",
        "to_input": "Geometry",
        "replace": True,
    },
    {
        "op": "connect",
        "from_node": "SmokeTransform",
        "from_output": "Geometry",
        "to_node": "Group Output",
        "to_input": "Geometry",
        "replace": True,
    },
    {
        "op": "add_socket",
        "name": "Smoke Strength",
        "in_out": "INPUT",
        "socket_type": "NodeSocketFloat",
        "default_value": 0.5,
    },
]
patch_plan = require_ok(
    dispatch(
        "geometry_nodes.patch",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeGeometry",
            "operations": operations,
            "expected_node_count": 2,
            "expected_link_count": 1,
        },
    ),
    "geometry_nodes.patch dry run",
)
assert patch_plan["dry_run"] is True and patch_plan["confirmation_required"] is True
assert cube.modifiers["SmokeGeometry"].node_group.nodes.get("SmokeTransform") is None

graph_patch = require_ok(
    dispatch(
        "geometry_nodes.patch",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeGeometry",
            "operations": operations,
            "expected_node_count": 2,
            "expected_link_count": 1,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "geometry_nodes.patch apply",
)
assert graph_patch["changed"] is True
assert graph_patch["after"] == {"nodes": 3, "links": 2, "interface_sockets": 3}

inspection = require_ok(
    dispatch(
        "geometry_nodes.inspect",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeGeometry",
            "include_graph": True,
        },
    ),
    "geometry_nodes.inspect graph",
)
assert inspection["count"] == 1
record = inspection["modifiers"][0]
assert record["node_count"] == 3 and record["link_count"] == 2
assert any(node["name"] == "SmokeTransform" for node in record["nodes"])
strength = next(
    socket
    for socket in record["interface"]
    if socket["name"] == "Smoke Strength" and socket["in_out"] == "INPUT"
)
assert strength["identifier"]

inputs = require_ok(
    dispatch(
        "geometry_nodes.modifier_inputs",
        {
            "object_name": cube.name,
            "modifier_name": "SmokeGeometry",
            "values": {strength["identifier"]: 1.25},
            "expected_group": record["node_group"],
        },
    ),
    "geometry_nodes.modifier_inputs",
)
assert inputs["changed"] is True
assert inputs["after"][strength["identifier"]] == 1.25

rejected = dispatch(
    "geometry_nodes.patch",
    {
        "object_name": cube.name,
        "modifier_name": "SmokeGeometry",
        "operations": [
            {
                "op": "add_node",
                "node_type": "GeometryNodeScript",
                "name": "Forbidden",
            }
        ],
        "dry_run": False,
    },
)
assert rejected["ok"] is False
assert rejected["error"]["type"] == "InvalidParamsError"
assert cube.modifiers["SmokeGeometry"].node_group.nodes.get("Forbidden") is None
print("PASS geometry_nodes allowlist rejection")

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("GEOMETRY_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
