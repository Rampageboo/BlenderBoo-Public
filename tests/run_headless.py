"""Blender background smoke test for the v0.5 add-on tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import sys

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

# A developer's installed add-on can already be imported when Blender starts.
# The smoke must exercise the repository source, not that installed copy.
for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo import (
    dispatch,
    register as register_addon,
    start_bridge,
    stop_bridge,
    unregister as unregister_addon,
)
from blenderboo.errors import InvalidStateError
import blenderboo.bridge_ui as bridge_ui


def bounded_debug_json(value: object, max_chars: int = 4096) -> str:
    """Retain useful failure evidence without flooding Blender or MCP logs."""
    serialized = json.dumps(value, indent=2, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized
    return serialized[:max_chars] + "\n... <debug JSON truncated>"


def report_pass(label: str, result: dict | None = None) -> None:
    suffix = ""
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("type"), str):
            suffix = f" [{error['type']}]"
    print(f"PASS {label}{suffix}")


def require_ok(result: dict, label: str) -> dict:
    if not result["ok"]:
        raise AssertionError(f"{label} failed:\n{bounded_debug_json(result)}")
    json.dumps(result)
    report_pass(label)
    return result["data"]


artifact_dir = ROOT / "tests" / ".tmp_headless"
if artifact_dir.exists():
    shutil.rmtree(artifact_dir)
artifact_dir.mkdir(parents=True)
registry_path = artifact_dir / "instances.json"
os.environ["CBT_INSTANCE_REGISTRY"] = str(registry_path)
config_path = artifact_dir / "config.toml"
config_path.write_text(
    "[mcp_servers.blenderboo]\n"
    "command = \"python\"\n"
    "env = { CBT_INSTANCE_REGISTRY = \"placeholder\" }\n",
    encoding="utf-8",
)
os.environ["CBT_CONFIG_PATH"] = str(config_path)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
cube = bpy.context.object
cube.name = "SmokeCube"
cube.scale = (1.0, 1.5, 0.75)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
bpy.ops.object.light_add(type="AREA", location=(4.0, -4.0, 6.0))
light = bpy.context.object
light.name = "SmokeLight"
light.data.energy = 1000.0
light.data.shape = "DISK"
light.data.size = 5.0
light.rotation_euler = ((Vector((0.0, 0.0, 0.0)) - light.location).to_track_quat("-Z", "Y").to_euler())
cube.select_set(True)
bpy.context.view_layer.objects.active = cube
base_file = artifact_dir / "smoke_scene.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(base_file), check_existing=False)

inspection = require_ok(dispatch("scene.inspect", {"object_name": "SmokeCube"}), "scene.inspect")
assert inspection["objects"]["counts_by_type"]["MESH"] == 1
assert inspection["object_detail"]["name"] == "SmokeCube"
assert "names_by_type" not in inspection["objects"]
assert "names" not in inspection
assert inspection["selection"]["selected_count"] == 2
assert inspection["collections"]["object_count"] == 2
assert "objects" not in inspection["collections"]
assert inspection["filepath"] == str(base_file)
assert inspection["saved"] is True
assert inspection["dirty"] is False
assert isinstance(inspection["state_token"], str) and inspection["state_token"]
initial_state_token = inspection["state_token"]

all_names = []
cursor = None
while True:
    name_params = {"include_names": True, "limit": 1}
    if cursor is not None:
        name_params["cursor"] = cursor
    name_page = require_ok(dispatch("scene.inspect", name_params), "scene.inspect names page")
    all_names.extend(name_page["names"])
    cursor = name_page["next_cursor"]
    assert name_page["truncated"] is (cursor is not None)
    if cursor is None:
        break
assert all_names == [
    {"name": "SmokeCube", "type": "MESH"},
    {"name": "SmokeLight", "type": "LIGHT"},
]

first = require_ok(dispatch("checkpoint.save", {"tag": "smoke"}), "checkpoint.save first")
second = require_ok(dispatch("checkpoint.save", {"tag": "smoke"}), "checkpoint.save second")
assert first["path"] != second["path"]
assert first["index"] == 1 and second["index"] == 2
listed = require_ok(dispatch("checkpoint.list", {}), "checkpoint.list")
assert listed["count"] == 2

refused = dispatch("checkpoint.restore", {"name": first["name"]})
assert not refused["ok"] and refused["error"]["type"] == "ConfirmationRequiredError"
report_pass("checkpoint.restore confirmation gate", refused)

lease = require_ok(
    dispatch(
        "session.acquire_lease",
        {"scope": "document", "ttl": 60},
        context={"client_id": "headless-smoke"},
    ),
    "session.acquire_lease",
)
assert lease["active"] and lease["acquired"] and lease["held_by"] == "headless-smoke"
assert lease["ttl"] == 60 and 0 < lease["expires_in"] <= 60
bpy.ops.object.empty_add(type="PLAIN_AXES")
bpy.context.object.name = "UnsavedMarker"
restored = require_ok(
    dispatch(
        "checkpoint.restore",
        {"name": first["name"], "confirm": True, "expected_state": initial_state_token},
        context={"client_id": "headless-smoke"},
    ),
    "checkpoint.restore",
)
assert restored["restored"] is True
assert restored["changed"] is True
assert restored["client_id"] == "headless-smoke"
assert restored["state_token"] != initial_state_token
assert bpy.data.objects.get("UnsavedMarker") is None
lease_identity = require_ok(
    dispatch("session.whoami", {}, context={"client_id": "headless-smoke"}),
    "session.whoami lease holder after restore",
)
assert lease_identity["client_id"] == "headless-smoke"
assert lease_identity["lease"]["lease_id"] == lease["lease_id"]
listed_after_restore = require_ok(dispatch("checkpoint.list", {}), "checkpoint.list after restore")
assert listed_after_restore["count"] == 2 and listed_after_restore["base"] == "smoke_scene"
traversal = dispatch("checkpoint.restore", {"name": "../outside.blend", "confirm": True})
assert not traversal["ok"] and traversal["error"]["type"] == "InvalidParamsError"
report_pass("checkpoint.restore path confinement", traversal)

# Typed material repair loop: inspect, dry run, apply, prove a failed
# precondition is non-mutating, then persist through an active version save.
created_material = require_ok(
    dispatch(
        "material.create",
        {
            "name": "SmokeCreatedMaterial",
            "use_fake_user": True,
            "expected_state": restored["state_token"],
        },
        context={"client_id": "headless-smoke"},
    ),
    "material.create",
)
assert created_material["changed"]
assert created_material["material"] == "SmokeCreatedMaterial"
assert created_material["detail"]["uses_nodes"]
assert bpy.data.materials["SmokeCreatedMaterial"].use_fake_user
material_state = created_material["state_token"]
duplicate_material = dispatch(
    "material.create",
    {"name": "SmokeCreatedMaterial", "expected_state": material_state},
    context={"client_id": "headless-smoke"},
)
assert not duplicate_material["ok"] and duplicate_material["error"]["type"] == "NameConflictError"
report_pass("material.create collision gate", duplicate_material)

material = bpy.data.materials.new("SmokeLeaf")
material.use_nodes = True
cube = bpy.data.objects["SmokeCube"]
cube.data.materials.append(material)
nodes = material.node_tree.nodes
principled = next(node for node in nodes if node.bl_idname == "ShaderNodeBsdfPrincipled")
inspection = require_ok(
    dispatch("material.inspect", {"material": "SmokeLeaf", "include_nodes": True, "limit": 1}),
    "material.inspect",
)
assert inspection["materials"][0]["has_output_node"]
assert inspection["materials"][0]["truncated"]
operations = [
    {"op": "add_node", "node_type": "ShaderNodeNewGeometry", "name": "CBT_BackfaceGeometry", "location": [-600, 0]},
    {"op": "add_node", "node_type": "ShaderNodeMixRGB", "name": "CBT_UndersideMix", "location": [-300, 0]},
    {"op": "set_input", "node": "CBT_UndersideMix", "input": "Color1", "value": [0.02, 0.12, 0.03, 1.0]},
    {"op": "connect", "from_node": "CBT_BackfaceGeometry", "from_output": "Backfacing", "to_node": "CBT_UndersideMix", "to_input": "Fac"},
    {"op": "connect", "from_node": "CBT_UndersideMix", "from_output": "Color", "to_node": principled.name, "to_input": "Base Color"},
    {"op": "set_material_flag", "flag": "use_backface_culling", "value": False},
]
dry_run = require_ok(
    dispatch(
        "material.patch",
        {
            "material": "SmokeLeaf",
            "operations": operations,
            "expected_state": material_state,
        },
    ),
    "material.patch dry run",
)
assert dry_run["dry_run"] and not dry_run["changed"]
assert all(entry["status"] == "planned" for entry in dry_run["journal"])
assert nodes.get("CBT_BackfaceGeometry") is None
blocked_apply = dispatch(
    "material.patch",
    {
        "material": "SmokeLeaf",
        "operations": operations,
        "dry_run": False,
        "expected_state": material_state,
    },
    context={"client_id": "competing-client"},
)
assert not blocked_apply["ok"] and blocked_apply["error"]["type"] == "LockedError"
assert blocked_apply["data"]["held_by"] == "headless-smoke"
assert "lease_id" not in blocked_apply["data"]
assert nodes.get("CBT_BackfaceGeometry") is None
report_pass("document lease rejects competing write", blocked_apply)
applied = require_ok(
    dispatch(
        "material.patch",
        {
            "material": "SmokeLeaf", "operations": operations,
            "preconditions": [{"kind": "node_present", "name": "CBT_BackfaceGeometry", "present": False}],
            "dry_run": False,
            "expected_state": material_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "material.patch apply",
)
assert applied["changed"] and not applied["dry_run"]
assert applied["state_token"] != material_state
assert nodes.get("CBT_BackfaceGeometry") is not None
roughness = principled.inputs["Roughness"].default_value
stale_abort = dispatch(
    "material.patch",
    {
        "material": "SmokeLeaf",
        "operations": [
            {"op": "set_input", "node": principled.name, "input": "Roughness", "value": 0.2}
        ],
        "dry_run": False,
        "expected_state": restored["state_token"],
    },
    context={"client_id": "headless-smoke"},
)
assert not stale_abort["ok"] and stale_abort["error"]["type"] == "StaleStateError"
assert principled.inputs["Roughness"].default_value == roughness
report_pass("material.patch stale-state gate", stale_abort)
precondition_abort = dispatch(
    "material.patch",
    {
        "material": "SmokeLeaf",
        "preconditions": [{"kind": "input_equals", "node": principled.name, "input": "Roughness", "value": 0.123}],
        "operations": [{"op": "set_input", "node": principled.name, "input": "Roughness", "value": 0.2}],
        "dry_run": False,
    },
    context={"client_id": "headless-smoke"},
)
assert not precondition_abort["ok"] and precondition_abort["error"]["type"] == "InvalidParamsError"
assert principled.inputs["Roughness"].default_value == roughness
report_pass("material.patch precondition gate", precondition_abort)
renewed_lease = require_ok(
    dispatch(
        "session.renew_lease",
        {"lease_id": lease["lease_id"], "ttl": 120},
        context={"client_id": "headless-smoke"},
    ),
    "session.renew_lease",
)
assert renewed_lease["renewed"] and renewed_lease["ttl"] == 120
version = require_ok(
    dispatch(
        "scene.save_version",
        {"tag": "material-repair", "expected_state": applied["state_token"]},
        context={"client_id": "headless-smoke"},
    ),
    "scene.save_version",
)
assert Path(version["new_path"]).is_file() and Path(bpy.data.filepath).resolve() == Path(version["new_path"])
assert version["state_token"] != applied["state_token"]
assert version["client_id"] == "headless-smoke"
bpy.ops.wm.open_mainfile(filepath=version["new_path"], load_ui=False)
persisted = require_ok(dispatch("material.inspect", {"material": "SmokeLeaf", "include_nodes": True}), "material.inspect persisted")
assert any(node["name"] == "CBT_BackfaceGeometry" for node in persisted["materials"][0]["nodes"])

identity = require_ok(dispatch("session.whoami", {}), "session.whoami stopped")
assert identity["pid"] > 0
assert Path(identity["filepath"]).resolve() == Path(bpy.data.filepath).resolve()
assert identity["saved"] is True
assert isinstance(identity["dirty"], bool)
assert identity["state_token"] == version["state_token"]
assert isinstance(identity["bridge"]["instance_id"], str) and identity["bridge"]["instance_id"]
assert identity["bridge"] == {
    "running": False,
    "host": "127.0.0.1",
    "port": None,
    "instance_id": identity["bridge"]["instance_id"],
    "started": None,
    "token_location": None,
    "registry_path": str(registry_path),
    "registry_error": None,
    "queue_depth": 0,
    "queue_capacity": 64,
    "active_connections": 0,
    "connection_capacity": 16,
    "client_inflight": 0,
    "active_clients": 0,
    "per_client_inflight_limit": 4,
}
assert identity["pending_calls"] == 0
assert identity["client_id"] is None
assert identity["lease"]["active"] is True
assert identity["lease"]["held_by"] == "headless-smoke"
released_lease = require_ok(
    dispatch(
        "session.release_lease",
        {"lease_id": lease["lease_id"]},
        context={"client_id": "headless-smoke"},
    ),
    "session.release_lease",
)
assert released_lease["released"] is True

# Blender 5.2 accepts Collection children by name/lookup, but rejects an RNA
# Collection instance in bpy_prop_collection.__contains__.  Creating a
# collection forces the returned detail to resolve its parent and guards this
# compatibility boundary against regression.
collection_state = identity["state_token"]
created_collection = require_ok(
    dispatch(
        "collection.create",
        {
            "name": "SliceCollection",
            "expected_state": collection_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "collection.create Blender 5.2 parent lookup",
)
assert created_collection["changed"]
assert created_collection["detail"]["parents"] == [bpy.context.scene.collection.name]
collection_state = created_collection["state_token"]
collection_inspection = require_ok(
    dispatch(
        "collection.inspect",
        {"name": "SliceCollection"},
        context={"client_id": "headless-smoke"},
    ),
    "collection.inspect Blender 5.2 parent lookup",
)
assert collection_inspection["collections"][0]["parents"] == [bpy.context.scene.collection.name]

# v0.5 Slice 1 object lifecycle: build a tiny hierarchy entirely through typed
# tools, prove dry-run gates are non-mutating, then delete back to the baseline.
object_baseline_count = len(bpy.context.scene.objects)
object_state = collection_state
object_selection_before = sorted(obj.name for obj in bpy.context.selected_objects)
object_active_before = bpy.context.view_layer.objects.active
created_parent = require_ok(
    dispatch(
        "object.create",
        {
            "type": "EMPTY",
            "name": "SliceParent",
            "location": [1.0, 0.0, 0.0],
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.create parent",
)
assert created_parent["changed"] and created_parent["resolved_name"] == "SliceParent"
assert created_parent["client_id"] == "headless-smoke"
assert created_parent["journal"][0]["status"] == "applied"
assert created_parent["state_token"] != object_state
object_state = created_parent["state_token"]

created_child = require_ok(
    dispatch(
        "object.create",
        {
            "type": "CUBE",
            "name": "SliceChild",
            "collection": bpy.context.scene.collection.name,
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.create child",
)
assert created_child["changed"] and created_child["detail"]["type"] == "MESH"
assert len(bpy.context.scene.objects) == object_baseline_count + 2
assert sorted(obj.name for obj in bpy.context.selected_objects) == object_selection_before
assert bpy.context.view_layer.objects.active is object_active_before
object_state = created_child["state_token"]

transformed_child = require_ok(
    dispatch(
        "object.transform",
        {
            "object_name": "SliceChild",
            "location": [2.0, 0.0, 0.0],
            "scale": [0.5, 0.75, 1.0],
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.transform local",
)
assert transformed_child["changed"]
assert transformed_child["local"]["location"] == [2.0, 0.0, 0.0]
object_state = transformed_child["state_token"]

parent_count_before = len(bpy.context.scene.objects)
parent_plan = require_ok(
    dispatch(
        "object.parent",
        {
            "object_name": "SliceChild",
            "parent": "SliceParent",
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.parent dry run",
)
assert parent_plan["dry_run"] and not parent_plan["changed"]
assert parent_plan["journal"][0]["status"] == "planned"
assert bpy.data.objects["SliceChild"].parent is None
assert len(bpy.context.scene.objects) == parent_count_before
assert parent_plan["state_token"] == object_state

parented = require_ok(
    dispatch(
        "object.parent",
        {
            "object_name": "SliceChild",
            "parent": "SliceParent",
            "dry_run": False,
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.parent apply",
)
assert parented["changed"] and bpy.data.objects["SliceChild"].parent.name == "SliceParent"
assert parented["journal"][0]["status"] == "applied"
object_state = parented["state_token"]

world_transform = require_ok(
    dispatch(
        "object.transform",
        {
            "object_name": "SliceChild",
            "location": [4.0, 3.0, 2.0],
            "space": "world",
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.transform world",
)
assert all(abs(actual - expected) < 1e-5 for actual, expected in zip(world_transform["world"]["location"], [4.0, 3.0, 2.0]))
object_state = world_transform["state_token"]

duplicated = require_ok(
    dispatch(
        "object.duplicate",
        {
            "object_name": "SliceChild",
            "name": "SliceCopy",
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.duplicate",
)
assert duplicated["resolved_name"] == "SliceCopy" and not duplicated["linked"]
assert bpy.data.objects["SliceCopy"].data is not bpy.data.objects["SliceChild"].data
object_state = duplicated["state_token"]

renamed = require_ok(
    dispatch(
        "object.rename",
        {
            "object_name": "SliceCopy",
            "name": "SliceRenamed",
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.rename",
)
assert renamed["resolved_name"] == "SliceRenamed" and bpy.data.objects.get("SliceCopy") is None
object_state = renamed["state_token"]

visibility = require_ok(
    dispatch(
        "object.set_visibility",
        {
            "objects": ["SliceChild", "SliceRenamed"],
            "hide_render": True,
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.set_visibility",
)
assert visibility["changed"] and all(bpy.data.objects[name].hide_render for name in visibility["objects"])
object_state = visibility["state_token"]

filtered = require_ok(
    dispatch(
        "object.inspect",
        {
            "name": "Slice",
            "name_regex": "(Child|Renamed)$",
            "type": "MESH",
            "collection": bpy.context.scene.collection.name,
            "visible": True,
            "selected": False,
            "limit": 1,
            "object_name": "SliceChild",
        },
    ),
    "object.inspect filtered",
)
assert filtered["count"] == 2 and len(filtered["objects"]) == 1
assert filtered["truncated"] and filtered["next_cursor"] == "1"
assert filtered["object_detail"]["parent"] == "SliceParent"
filtered_next = require_ok(
    dispatch(
        "object.inspect",
        {"name": "Slice", "type": "MESH", "limit": 1, "cursor": filtered["next_cursor"]},
    ),
    "object.inspect next page",
)
assert len(filtered_next["objects"]) == 1

delete_names = ["SliceChild", "SliceRenamed", "SliceParent"]
delete_count_before = len(bpy.context.scene.objects)
delete_plan = require_ok(
    dispatch(
        "object.delete",
        {"objects": delete_names, "expected_state": object_state},
        context={"client_id": "headless-smoke"},
    ),
    "object.delete dry run",
)
assert delete_plan["dry_run"] and not delete_plan["changed"]
assert all(entry["status"] == "planned" for entry in delete_plan["journal"])
assert len(delete_plan["orphaned_data_candidates"]) == 2
assert len(bpy.context.scene.objects) == delete_count_before

delete_unconfirmed = dispatch(
    "object.delete",
    {"objects": delete_names, "dry_run": False, "expected_state": object_state},
    context={"client_id": "headless-smoke"},
)
assert not delete_unconfirmed["ok"]
assert delete_unconfirmed["error"]["type"] == "ConfirmationRequiredError"
assert not delete_unconfirmed["data"]["changed"]
assert len(bpy.context.scene.objects) == delete_count_before
report_pass("object.delete confirmation gate", delete_unconfirmed)

deleted = require_ok(
    dispatch(
        "object.delete",
        {
            "objects": delete_names,
            "dry_run": False,
            "confirm": True,
            "expected_state": object_state,
        },
        context={"client_id": "headless-smoke"},
    ),
    "object.delete apply",
)
assert deleted["changed"] and all(entry["status"] == "applied" for entry in deleted["journal"])
assert deleted["state_token"] != object_state
assert len(bpy.context.scene.objects) == object_baseline_count
assert all(bpy.data.objects.get(name) is None for name in delete_names)

preview_path = artifact_dir / "preview.png"
preview = require_ok(
    dispatch("render.preview", {"width": 320, "height": 180, "output_path": str(preview_path)}),
    "render.preview",
)
assert Path(preview["path"]).is_file()
assert preview["width"] == 320 and preview["height"] == 180
assert bpy.context.scene.camera is None
assert bpy.data.objects.get("CodexPreviewCamera") is None
overwrite_refused = dispatch(
    "render.preview",
    {"width": 320, "height": 180, "output_path": str(preview_path)},
)
assert not overwrite_refused["ok"] and overwrite_refused["error"]["type"] == "InvalidParamsError"
assert str(preview_path.resolve()) in overwrite_refused["error"]["message"]
report_pass("render.preview overwrite gate", overwrite_refused)
require_ok(
    dispatch(
        "render.preview",
        {"width": 320, "height": 180, "output_path": str(preview_path), "overwrite": True},
    ),
    "render.preview overwrite true",
)

budget_refused = dispatch("render.preview", {"width": 4096, "height": 2160})
assert not budget_refused["ok"] and budget_refused["error"]["type"] == "InvalidParamsError"
assert "allow_large: true" in budget_refused["error"]["message"]
report_pass("render.preview pixel budget", budget_refused)

screenshot_headless = dispatch("scene.screenshot", {})
assert not screenshot_headless["ok"] and screenshot_headless["error"]["type"] == "InvalidStateError"
assert screenshot_headless["error"]["message"] == "no active 3D viewport available"
report_pass("scene.screenshot headless guard", screenshot_headless)

camera_data = bpy.data.cameras.new("SmokeCamera")
camera = bpy.data.objects.new("SmokeCamera", camera_data)
bpy.context.scene.collection.objects.link(camera)
camera.location = (5.0, -5.0, 4.0)
camera.rotation_euler = ((Vector((0.0, 0.0, 0.0)) - camera.location).to_track_quat("-Z", "Y").to_euler())
bpy.context.scene.camera = camera
active_preview_path = artifact_dir / "preview_active_camera.png"
active_preview_result = dispatch(
    "render.preview",
    {"width": 160, "height": 90, "output_path": str(active_preview_path)},
)
active_preview = require_ok(active_preview_result, "render.preview active camera")
assert Path(active_preview["path"]).is_file()
assert active_preview_result["warnings"] == []
assert bpy.context.scene.camera is camera

# Review evidence: four fixed views, collection visibility overrides, exact state
# restoration, overwrite refusal, structured comparison, and a diff artifact.
visible_collection = bpy.data.collections.new("ReviewVisible")
hidden_collection = bpy.data.collections.new("ReviewHidden")
bpy.context.scene.collection.children.link(visible_collection)
bpy.context.scene.collection.children.link(hidden_collection)
cube = bpy.data.objects["SmokeCube"]
light = bpy.data.objects["SmokeLight"]
visible_collection.objects.link(cube)
visible_collection.objects.link(light)
if cube.name in bpy.context.scene.collection.objects:
    bpy.context.scene.collection.objects.unlink(cube)
if light.name in bpy.context.scene.collection.objects:
    bpy.context.scene.collection.objects.unlink(light)
visible_collection.hide_render = False
hidden_collection.hide_render = True
scene = bpy.context.scene
render = scene.render
review_state = (
    scene.camera,
    render.filepath,
    render.resolution_x,
    render.resolution_y,
    render.resolution_percentage,
    render.image_settings.file_format,
    visible_collection.hide_render,
    hidden_collection.hide_render,
)
review_object_visibility = {obj.name: bool(obj.hide_render) for obj in scene.objects}
review_prefix = artifact_dir / "review"
review_result = dispatch(
    "render.review_set",
    {
        "width": 160,
        "height": 90,
        "output_prefix": str(review_prefix),
        "show_only": ["ReviewVisible"],
    },
)
review = require_ok(review_result, "render.review_set")
assert review["views"] == ["camera", "top", "side", "three_quarter"]
assert list(review["manifest"]) == review["views"]
for view, entry in review["manifest"].items():
    assert set(entry) == {"path", "camera", "width", "height", "warnings"}
    assert Path(entry["path"]).is_file(), view
    assert entry["width"] == 160 and entry["height"] == 90
assert review_result["warnings"] == []
assert review_state == (
    scene.camera,
    render.filepath,
    render.resolution_x,
    render.resolution_y,
    render.resolution_percentage,
    render.image_settings.file_format,
    visible_collection.hide_render,
    hidden_collection.hide_render,
)
assert review_object_visibility == {obj.name: bool(obj.hide_render) for obj in scene.objects}
assert not any(obj.name.startswith("CodexReview") for obj in bpy.data.objects)
assert not any(camera_data.name.startswith("CodexReview") for camera_data in bpy.data.cameras)

missing_collection = dispatch(
    "render.review_set",
    {"views": ["top"], "output_prefix": str(artifact_dir / "missing"), "hide": ["MissingCollection"]},
)
assert not missing_collection["ok"]
assert missing_collection["error"]["type"] == "InvalidParamsError"
assert "MissingCollection" in missing_collection["error"]["message"]
assert not (artifact_dir / "missing_top.png").exists()
report_pass("render.review_set collection validation", missing_collection)

review_overwrite_refused = dispatch(
    "render.review_set",
    {"width": 160, "height": 90, "output_prefix": str(review_prefix)},
)
assert not review_overwrite_refused["ok"]
assert review_overwrite_refused["error"]["type"] == "InvalidParamsError"
assert "overwrite is false" in review_overwrite_refused["error"]["message"]
report_pass("render.review_set overwrite gate", review_overwrite_refused)

image_pointers_before = {image.as_pointer() for image in bpy.data.images}
top_path = review["manifest"]["top"]["path"]
side_path = review["manifest"]["side"]["path"]
comparison = require_ok(
    dispatch("render.compare", {"image_a": top_path, "image_b": side_path}),
    "render.compare",
)
assert comparison["width"] == 160 and comparison["height"] == 90
assert comparison["total_pixels"] == 160 * 90
assert 0 <= comparison["changed_pixels"] <= comparison["total_pixels"]
assert 0.0 <= comparison["changed_fraction"] <= 1.0
assert comparison["mean_abs_rgb_delta"] >= 0.0
if comparison["changed_pixels"]:
    assert comparison["bbox"] is not None
assert image_pointers_before == {image.as_pointer() for image in bpy.data.images}

diff_path = artifact_dir / "review_diff.png"
comparison_with_diff = require_ok(
    dispatch(
        "render.compare",
        {
            "image_a": top_path,
            "image_b": side_path,
            "threshold": 8,
            "write_diff": True,
            "diff_output_path": str(diff_path),
        },
    ),
    "render.compare write diff",
)
assert comparison_with_diff["diff_path"] == str(diff_path.resolve())
assert diff_path.is_file()
assert image_pointers_before == {image.as_pointer() for image in bpy.data.images}

diff_overwrite_refused = dispatch(
    "render.compare",
    {"image_a": top_path, "image_b": side_path, "write_diff": True, "diff_output_path": str(diff_path)},
)
assert not diff_overwrite_refused["ok"]
assert diff_overwrite_refused["error"]["type"] == "InvalidParamsError"
assert "overwrite is false" in diff_overwrite_refused["error"]["message"]
report_pass("render.compare diff overwrite gate", diff_overwrite_refused)

dimension_mismatch = dispatch(
    "render.compare",
    {"image_a": str(preview_path), "image_b": top_path},
)
assert not dimension_mismatch["ok"]
assert dimension_mismatch["error"]["type"] == "InvalidParamsError"
assert "image_a is 320x180" in dimension_mismatch["error"]["message"]
assert "image_b is 160x90" in dimension_mismatch["error"]["message"]
assert image_pointers_before == {image.as_pointer() for image in bpy.data.images}
report_pass("render.compare dimension mismatch", dimension_mismatch)

scene.camera = None
no_camera_result = dispatch(
    "render.review_set",
    {"views": ["camera", "top"], "width": 80, "height": 45, "output_prefix": str(artifact_dir / "no_camera")},
)
no_camera = require_ok(no_camera_result, "render.review_set camera skip")
assert no_camera["views"] == ["top"]
assert list(no_camera["manifest"]) == ["top"]
assert any("Skipped camera view" in warning for warning in no_camera_result["warnings"])
assert scene.camera is None
assert not any(obj.name.startswith("CodexReview") for obj in bpy.data.objects)
assert not any(camera_data.name.startswith("CodexReview") for camera_data in bpy.data.cameras)
scene.camera = camera

# Bridge ownership: a live same-port second start fails loudly, while a clean
# stop and restart reclaims the same port.
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    bridge_port = probe.getsockname()[1]
bridge_token = "headless-exclusive-bind-token-at-least-32-chars"
started = start_bridge(bridge_token, port=bridge_port)
assert started["running"] is True and started["port"] == bridge_port
assert started["instance_id"]
assert started["token_location"] == "runtime"
registry = json.loads(registry_path.read_text(encoding="utf-8"))
assert len(registry["instances"]) == 1
assert registry["instances"][0]["id"] == started["instance_id"]
assert registry["instances"][0]["port"] == bridge_port
assert bridge_token not in json.dumps(registry)
running_identity = require_ok(dispatch("session.whoami", {}), "session.whoami running")
assert running_identity["bridge"]["running"] is True
assert running_identity["bridge"]["port"] == bridge_port
assert running_identity["bridge"]["queue_depth"] == 0
assert running_identity["bridge"]["queue_capacity"] == 64
assert running_identity["bridge"]["active_connections"] == 0
assert running_identity["bridge"]["per_client_inflight_limit"] == 4
try:
    start_bridge(bridge_token, port=bridge_port)
except InvalidStateError as exc:
    assert "address in use" in str(exc)
else:
    raise AssertionError("A second bridge start on the live port did not fail")
stop_bridge()
assert json.loads(registry_path.read_text(encoding="utf-8"))["instances"] == []
restarted = start_bridge(bridge_token, port=bridge_port)
assert restarted["running"] is True and restarted["port"] == bridge_port
stop_bridge()
print("PASS bridge exclusive bind and clean rebind")

register_addon()
assert hasattr(bpy.types.WindowManager, "codex_blender_bridge")
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    panel_port = probe.getsockname()[1]
bpy.context.window_manager.codex_blender_bridge.port = panel_port
assert bpy.ops.codex_blender.bridge_start() == {"FINISHED"}
panel_first_token = bridge_ui._SESSION_TOKEN
assert panel_first_token is not None
assert len(panel_first_token) >= 32
assert "CBT_BRIDGE_TOKEN" in config_path.read_text(encoding="utf-8")
panel_registry = json.loads(registry_path.read_text(encoding="utf-8"))
assert len(panel_registry["instances"]) == 1
assert panel_registry["instances"][0]["port"] == panel_port
assert panel_first_token not in json.dumps(panel_registry)
assert bpy.ops.codex_blender.bridge_rotate_token() == {"FINISHED"}
panel_second_token = bridge_ui._SESSION_TOKEN
assert panel_second_token is not None
assert len(panel_second_token) >= 32 and panel_second_token != panel_first_token
assert panel_second_token in config_path.read_text(encoding="utf-8")
assert panel_first_token not in registry_path.read_text(encoding="utf-8")
assert panel_second_token not in registry_path.read_text(encoding="utf-8")
assert bpy.ops.codex_blender.bridge_stop() == {"FINISHED"}
assert bridge_ui._SESSION_TOKEN is None
assert bpy.context.window_manager.clipboard == ""
assert json.loads(registry_path.read_text(encoding="utf-8"))["instances"] == []
unregister_addon()
assert not hasattr(bpy.types.WindowManager, "codex_blender_bridge")
print("PASS bridge UI start, rotate, stop, and unregister")

print("HEADLESS_SMOKE_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
