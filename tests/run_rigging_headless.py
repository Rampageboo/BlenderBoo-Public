"""Blender background qualification for guarded automatic bone weights."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

# Exercise repository source even when Blender has discovered an installed copy.
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
        raise AssertionError(f"{label} returned an unexpected result:\n{json.dumps(result, indent=2)}")
    print(f"PASS {label}")
    return result["data"]


def make_fixture() -> tuple[object, object, object]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    mesh = bpy.context.object
    mesh.name = "Body"

    armature_data = bpy.data.armatures.new("RigData")
    armature = bpy.data.objects.new("Rig", armature_data)
    bpy.context.scene.collection.objects.link(armature)
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    root = armature_data.edit_bones.new("Root")
    root.head = (0.0, 0.0, -1.5)
    root.tail = (0.0, 0.0, 0.0)
    tip = armature_data.edit_bones.new("Tip")
    tip.head = (0.0, 0.0, 0.0)
    tip.tail = (0.0, 0.0, 1.5)
    tip.parent = root
    tip.use_connect = True
    bpy.ops.object.mode_set(mode="OBJECT")

    modifier = mesh.modifiers.new("Armature", "ARMATURE")
    modifier.object = armature
    manual = mesh.vertex_groups.new(name="Manual")
    manual.add([0], 0.25, "REPLACE")
    manual.lock_weight = True
    mesh.vertex_groups.active_index = manual.index

    bpy.ops.mesh.primitive_plane_add(location=(4.0, 0.0, 0.0))
    anchor = bpy.context.object
    anchor.name = "ContextAnchor"
    return mesh, armature, anchor


def weight_map(obj: object) -> dict[str, dict[int, float]]:
    names = {group.index: group.name for group in obj.vertex_groups}
    result = {group.name: {} for group in obj.vertex_groups}
    for vertex in obj.data.vertices:
        for membership in vertex.groups:
            result[names[membership.group]][int(vertex.index)] = float(membership.weight)
    return result


def set_user_context(anchor: object, armature: object) -> dict:
    for selected in list(bpy.context.selected_objects):
        selected.select_set(False)
    armature.pose.bones["Root"].select = True
    armature.pose.bones["Tip"].select = False
    armature.data.bones.active = armature.data.bones["Root"]
    anchor.select_set(True)
    bpy.context.view_layer.objects.active = anchor
    bpy.context.scene.frame_set(17, subframe=0.25)
    bpy.ops.object.mode_set(mode="EDIT")
    return {
        "mode": bpy.context.mode,
        "selected": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name,
        "frame": int(bpy.context.scene.frame_current),
        "subframe": float(bpy.context.scene.frame_subframe),
        "bone_selected": {
            bone.name: bool(bone.select) for bone in armature.pose.bones
        },
        "active_bone": armature.data.bones.active.name,
    }


def assert_user_context(expected: dict, armature: object) -> None:
    actual = {
        "mode": bpy.context.mode,
        "selected": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name,
        "frame": int(bpy.context.scene.frame_current),
        "subframe": float(bpy.context.scene.frame_subframe),
        "bone_selected": {
            bone.name: bool(bone.select) for bone in armature.pose.bones
        },
        "active_bone": armature.data.bones.active.name,
    }
    if actual != expected:
        raise AssertionError(
            f"Blender context changed:\nexpected={expected!r}\nactual={actual!r}"
        )


def set_armature_edit_context(armature: object) -> dict:
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for selected in list(bpy.context.selected_objects):
        selected.select_set(False)
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.context.scene.frame_set(23, subframe=0.5)
    bpy.ops.object.mode_set(mode="EDIT")
    for bone in armature.data.edit_bones:
        bone.select = False
        bone.select_head = False
        bone.select_tail = False
    root = armature.data.edit_bones["Root"]
    root.select = True
    root.select_head = True
    root.select_tail = False
    armature.data.edit_bones.active = armature.data.edit_bones["Tip"]
    return {
        "mode": bpy.context.mode,
        "selected": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name,
        "frame": int(bpy.context.scene.frame_current),
        "subframe": float(bpy.context.scene.frame_subframe),
        "edit_selection": {
            bone.name: (bool(bone.select), bool(bone.select_head), bool(bone.select_tail))
            for bone in armature.data.edit_bones
        },
        "active_edit_bone": armature.data.edit_bones.active.name,
    }


def assert_armature_edit_context(expected: dict, armature: object) -> None:
    actual = {
        "mode": bpy.context.mode,
        "selected": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name,
        "frame": int(bpy.context.scene.frame_current),
        "subframe": float(bpy.context.scene.frame_subframe),
        "edit_selection": {
            bone.name: (bool(bone.select), bool(bone.select_head), bool(bone.select_tail))
            for bone in armature.data.edit_bones
        },
        "active_edit_bone": armature.data.edit_bones.active.name,
    }
    if actual != expected:
        raise AssertionError(
            f"Armature Edit context changed:\nexpected={expected!r}\nactual={actual!r}"
        )


artifact_root = ROOT / "tests" / ".tmp_rigging_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

capabilities = require_ok(
    dispatch("system.capabilities", {"limit": 200}),
    "system.capabilities",
)
assert capabilities["capability_count"] == 134

mesh, armature, anchor = make_fixture()
before_weights = weight_map(mesh)
before_groups = [group.name for group in mesh.vertex_groups]
before_context = set_user_context(anchor, armature)

plan = require_ok(
    dispatch(
        "skin.auto_weights",
        {"object_name": mesh.name, "armature_name": armature.name},
    ),
    "skin.auto_weights dry run",
)
assert plan["dry_run"] is True and plan["changed"] is False
assert plan["confirmation_required"] is True
assert plan["candidate_pair_count"] == 16
assert plan["new_groups"] == ["Root", "Tip"]
assert [group.name for group in mesh.vertex_groups] == before_groups
assert weight_map(mesh) == before_weights
assert_user_context(before_context, armature)

require_error(
    dispatch(
        "skin.auto_weights",
        {
            "object_name": mesh.name,
            "armature_name": armature.name,
            "dry_run": False,
        },
    ),
    "ConfirmationRequiredError",
    "skin.auto_weights confirmation gate",
)
assert [group.name for group in mesh.vertex_groups] == before_groups
assert weight_map(mesh) == before_weights
assert_user_context(before_context, armature)

applied = require_ok(
    dispatch(
        "skin.auto_weights",
        {
            "object_name": mesh.name,
            "armature_name": armature.name,
            "max_weight_entries": 100,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "skin.auto_weights apply",
)
assert applied["changed"] is True
assert applied["created_groups"] == ["Root", "Tip"]
assert applied["weighted_vertex_count"] == 8
assert applied["unweighted_vertex_count"] == 0
assert set(group.name for group in mesh.vertex_groups) == {"Manual", "Root", "Tip"}
assert mesh.vertex_groups["Manual"].lock_weight is True
assert weight_map(mesh)["Manual"] == before_weights["Manual"]
assert mesh.vertex_groups.active.name == "Manual"
assert_user_context(before_context, armature)

armature_edit_context = set_armature_edit_context(armature)
reweighted = require_ok(
    dispatch(
        "skin.auto_weights",
        {
            "object_name": mesh.name,
            "armature_name": armature.name,
            "max_weight_entries": 100,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "skin.auto_weights target-armature Edit-mode restoration",
)
assert reweighted["changed"] is True
assert reweighted["created_groups"] == []
assert_armature_edit_context(armature_edit_context, armature)

validated = require_ok(
    dispatch("skin.validate", {"object_name": mesh.name, "armature_name": armature.name}),
    "skin.validate automatic weights",
)
assert validated["valid"] is True
assert validated["unweighted_vertex_count"] == 0
assert validated["non_normalized_vertex_count"] == 0
root_group = mesh.vertex_groups["Root"]
tip_group = mesh.vertex_groups["Tip"]
root_group.add([0], 0.2, "REPLACE")
tip_group.add([0], 0.8, "REPLACE")
normalize_plan = require_ok(
    dispatch(
        "skin.weights_normalize",
        {
            "object_name": mesh.name,
            "armature_name": armature.name,
            "max_influences": 1,
            "min_weight": 0.001,
        },
    ),
    "skin.weights_normalize dry run",
)
assert normalize_plan["changed"] is False
assert normalize_plan["confirmation_required"] is True
assert normalize_plan["changed_vertex_count"] >= 1
normalized = require_ok(
    dispatch(
        "skin.weights_normalize",
        {
            "object_name": mesh.name,
            "armature_name": armature.name,
            "max_influences": 1,
            "min_weight": 0.001,
            "max_weight_entries": 100,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "skin.weights_normalize apply",
)
assert normalized["changed"] is True
assert abs(tip_group.weight(0) - 1.0) < 1e-6
try:
    root_group.weight(0)
except RuntimeError:
    pass
else:
    raise AssertionError("weaker deform influence was not pruned")

created_action = require_ok(
    dispatch(
        "animation.action_create",
        {"object_name": armature.name, "name": "RigAction"},
    ),
    "animation.action_create dry run",
)
assert created_action["changed"] is False
require_ok(
    dispatch(
        "animation.action_create",
        {"object_name": armature.name, "name": "RigAction", "dry_run": False},
    ),
    "animation.action_create apply",
)
require_ok(
    dispatch(
        "animation.action_create",
        {"object_name": armature.name, "name": "AltAction", "assign": False, "dry_run": False},
    ),
    "animation alternate action create",
)
assign_plan = require_ok(
    dispatch(
        "animation.action_assign",
        {"object_name": armature.name, "action_name": "AltAction"},
    ),
    "animation.action_assign dry run",
)
assert assign_plan["confirmation_required"] is True
assigned = require_ok(
    dispatch(
        "animation.action_assign",
        {
            "object_name": armature.name,
            "action_name": "AltAction",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "animation.action_assign apply",
)
assert assigned["changed"] is True
assert armature.animation_data.action.name == "AltAction"
keyed = require_ok(
    dispatch(
        "animation.keyframe_edit",
        {
            "object_name": armature.name,
            "operations": [{
                "op": "set", "target": "pose_bone", "bone": "Root",
                "property": "rotation_euler", "index": 0,
                "frame": 12, "value": 0.25,
                "interpolation": "LINEAR", "easing": "EASE_IN",
                "handle_left_type": "VECTOR", "handle_right_type": "AUTO_CLAMPED",
            }],
            "dry_run": False,
        },
    ),
    "animation.keyframe_edit curve style",
)
assert keyed["changed"] is True
animation = require_ok(
    dispatch(
        "animation.inspect",
        {"object_name": armature.name, "include_keyframes": True},
    ),
    "animation.inspect styled key",
)
curve = next(item for item in animation["items"] if item["data_path"].endswith('rotation_euler') and item["array_index"] == 0)
assert curve["keyframes"][0]["interpolation"] == "LINEAR"
require_ok(
    dispatch(
        "animation.nla_patch",
        {
            "object_name": armature.name,
            "operations": [
                {"op": "add_track", "track": "Lifecycle"},
                {
                    "op": "add_strip", "track": "Lifecycle", "name": "RigClip",
                    "action": "RigAction", "frame_start": 1,
                },
            ],
            "dry_run": False,
        },
    ),
    "animation.nla_patch lifecycle fixture",
)
require_error(
    dispatch("animation.action_remove", {"name": "RigAction"}),
    "InvalidStateError",
    "animation.action_remove NLA dependency rejection",
)
nla_remove_plan = require_ok(
    dispatch(
        "animation.nla_remove",
        {"object_name": armature.name, "track": "Lifecycle", "strip": "RigClip"},
    ),
    "animation.nla_remove strip dry run",
)
assert nla_remove_plan["confirmation_required"] is True
require_ok(
    dispatch(
        "animation.nla_remove",
        {
            "object_name": armature.name, "track": "Lifecycle", "strip": "RigClip",
            "dry_run": False, "confirm": True,
        },
    ),
    "animation.nla_remove strip apply",
)
require_ok(
    dispatch(
        "animation.nla_remove",
        {
            "object_name": armature.name, "track": "Lifecycle",
            "dry_run": False, "confirm": True,
        },
    ),
    "animation.nla_remove empty track apply",
)
action_remove_plan = require_ok(
    dispatch("animation.action_remove", {"name": "RigAction"}),
    "animation.action_remove dry run",
)
assert action_remove_plan["confirmation_required"] is True
require_ok(
    dispatch(
        "animation.action_remove",
        {"name": "RigAction", "dry_run": False, "confirm": True},
    ),
    "animation.action_remove apply",
)
assert bpy.data.actions.get("RigAction") is None

require_ok(
    dispatch(
        "animation.driver_create",
        {
            "object_name": armature.name, "property": "location", "index": 0,
            "source_object": anchor.name, "source_transform": "LOC_X",
            "dry_run": False,
        },
    ),
    "animation.driver_create lifecycle fixture",
)
driver_remove_plan = require_ok(
    dispatch(
        "animation.driver_remove",
        {"object_name": armature.name, "property": "location", "index": 0},
    ),
    "animation.driver_remove dry run",
)
assert driver_remove_plan["confirmation_required"] is True
require_ok(
    dispatch(
        "animation.driver_remove",
        {
            "object_name": armature.name, "property": "location", "index": 0,
            "dry_run": False, "confirm": True,
        },
    ),
    "animation.driver_remove apply",
)
assert len(armature.animation_data.drivers) == 0

require_ok(
    dispatch(
        "animation.shape_key_create",
        {"object_name": mesh.name, "name": "Basis", "dry_run": False},
    ),
    "animation.shape_key_create Basis",
)
require_ok(
    dispatch(
        "animation.shape_key_create",
        {"object_name": mesh.name, "name": "Smile", "dry_run": False},
    ),
    "animation.shape_key_create Smile",
)
require_error(
    dispatch("animation.shape_key_remove", {"object_name": mesh.name, "name": "Basis"}),
    "InvalidStateError",
    "animation.shape_key_remove Basis dependency rejection",
)
shape_remove_plan = require_ok(
    dispatch("animation.shape_key_remove", {"object_name": mesh.name, "name": "Smile"}),
    "animation.shape_key_remove dry run",
)
assert shape_remove_plan["confirmation_required"] is True
require_ok(
    dispatch(
        "animation.shape_key_remove",
        {
            "object_name": mesh.name, "name": "Smile",
            "dry_run": False, "confirm": True,
        },
    ),
    "animation.shape_key_remove apply",
)
assert mesh.data.shape_keys.key_blocks.get("Smile") is None

weights_before_unbind = weight_map(mesh)
unbind_plan = require_ok(
    dispatch(
        "skin.unbind",
        {"object_name": mesh.name, "armature_name": armature.name},
    ),
    "skin.unbind dry run",
)
assert unbind_plan["confirmation_required"] is True
require_ok(
    dispatch(
        "skin.unbind",
        {
            "object_name": mesh.name, "armature_name": armature.name,
            "dry_run": False, "confirm": True,
        },
    ),
    "skin.unbind apply",
)
assert not [modifier for modifier in mesh.modifiers if modifier.type == "ARMATURE"]
assert weight_map(mesh) == weights_before_unbind
require_error(
    dispatch(
        "armature.bone_remove",
        {"object_name": armature.name, "bone": "Root"},
    ),
    "InvalidStateError",
    "armature.bone_remove child dependency rejection",
)
shape_driver = mesh.data.shape_keys.key_blocks["Basis"].driver_add("value")
shape_variable = shape_driver.driver.variables.new()
shape_variable.name = "tip_x"
shape_variable.type = "TRANSFORMS"
shape_variable.targets[0].id = armature
shape_variable.targets[0].bone_target = "Tip"
shape_variable.targets[0].transform_type = "LOC_X"
shape_variable.targets[0].transform_space = "WORLD_SPACE"
shape_driver.driver.expression = shape_variable.name
require_error(
    dispatch(
        "armature.bone_remove",
        {"object_name": armature.name, "bone": "Tip"},
    ),
    "InvalidStateError",
    "armature.bone_remove cross-datablock driver dependency rejection",
)
assert mesh.data.shape_keys.key_blocks["Basis"].driver_remove("value") is True
require_ok(
    dispatch(
        "skin.weights_patch",
        {
            "object_name": mesh.name,
            "group_name": "Tip",
            "operations": [{"op": "REMOVE", "vertices": list(range(len(mesh.data.vertices)))}],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "skin.weights_patch clear Tip group",
)
group_remove_plan = require_ok(
    dispatch(
        "skin.group_remove",
        {"object_name": mesh.name, "group_name": "Tip"},
    ),
    "skin.group_remove dry run",
)
assert group_remove_plan["confirmation_required"] is True
require_ok(
    dispatch(
        "skin.group_remove",
        {
            "object_name": mesh.name, "group_name": "Tip",
            "dry_run": False, "confirm": True,
        },
    ),
    "skin.group_remove apply",
)
assert mesh.vertex_groups.get("Tip") is None
bone_remove_plan = require_ok(
    dispatch(
        "armature.bone_remove",
        {"object_name": armature.name, "bone": "Tip"},
    ),
    "armature.bone_remove dry run",
)
assert bone_remove_plan["confirmation_required"] is True
require_ok(
    dispatch(
        "armature.bone_remove",
        {
            "object_name": armature.name, "bone": "Tip",
            "dry_run": False, "confirm": True,
        },
    ),
    "armature.bone_remove apply",
)
assert armature.data.bones.get("Tip") is None
retarget_rig = require_ok(
    dispatch(
        "armature.create",
        {
            "name": "RetargetRig",
            "bones": [{"name": "RootTarget", "head": [0, 0, 0], "tail": [0, 0, 1]}],
            "dry_run": False,
        },
    ),
    "armature.create retarget fixture",
)
target_armature = bpy.context.scene.objects[retarget_rig["object"]]
retarget_plan = require_ok(
    dispatch(
        "animation.action_retarget",
        {
            "source_object": armature.name,
            "source_action": "AltAction",
            "target_object": target_armature.name,
            "name": "Retargeted",
            "bone_map": {"Root": "RootTarget"},
        },
    ),
    "animation.action_retarget dry run",
)
assert retarget_plan["changed"] is False
retargeted = require_ok(
    dispatch(
        "animation.action_retarget",
        {
            "source_object": armature.name,
            "source_action": "AltAction",
            "target_object": target_armature.name,
            "name": "Retargeted",
            "bone_map": {"Root": "RootTarget"},
            "dry_run": False,
        },
    ),
    "animation.action_retarget apply",
)
assert retargeted["changed"] is True
assert retargeted["remapped_curve_count"] >= 1
assert target_armature.animation_data.action.name == "Retargeted"
retarget_inspect = require_ok(
    dispatch("animation.inspect", {"object_name": target_armature.name}),
    "animation.inspect retargeted action",
)
retarget_paths = [curve["data_path"] for curve in retarget_inspect["items"]]
assert any('pose.bones["RootTarget"]' in path for path in retarget_paths)
assert not any('pose.bones["Root"]' in path for path in retarget_paths)

bake_plan = require_ok(
    dispatch(
        "animation.pose_bake",
        {
            "object_name": armature.name,
            "name": "BakedPose",
            "frame_start": 1,
            "frame_end": 3,
            "bones": ["Root"],
        },
    ),
    "animation.pose_bake dry run",
)
assert bake_plan["changed"] is False
assert bake_plan["sample_count"] == 3
baked = require_ok(
    dispatch(
        "animation.pose_bake",
        {
            "object_name": armature.name,
            "name": "BakedPose",
            "frame_start": 1,
            "frame_end": 3,
            "bones": ["Root"],
            "dry_run": False,
        },
    ),
    "animation.pose_bake apply",
)
assert baked["changed"] is True
assert baked["keyframe_count"] > 0
assert armature.animation_data.action.name == "BakedPose"




bpy.ops.object.mode_set(mode="OBJECT")
mesh, armature, anchor = make_fixture()
existing_root = mesh.vertex_groups.new(name="Root")
existing_root.add([1], 0.75, "REPLACE")
mesh.vertex_groups.active_index = mesh.vertex_groups["Manual"].index
before_weights = weight_map(mesh)
before_groups = [group.name for group in mesh.vertex_groups]
before_context = set_user_context(anchor, armature)
rolled_back = require_error(
    dispatch(
        "skin.auto_weights",
        {
            "object_name": mesh.name,
            "armature_name": armature.name,
            "max_weight_entries": 2,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "MutationApplyError",
    "skin.auto_weights result-budget rollback",
)
assert rolled_back["rolled_back"] is True
assert rolled_back["changed"] is False
assert rolled_back["journal"][0]["status"] == "rolled_back"
assert [group.name for group in mesh.vertex_groups] == before_groups
assert weight_map(mesh) == before_weights
assert mesh.vertex_groups["Manual"].lock_weight is True
assert mesh.vertex_groups.active.name == "Manual"
assert_user_context(before_context, armature)

bpy.ops.object.mode_set(mode="OBJECT")
linked = mesh.copy()
linked.data = mesh.data
bpy.context.scene.collection.objects.link(linked)
require_error(
    dispatch(
        "skin.auto_weights",
        {"object_name": mesh.name, "armature_name": armature.name},
    ),
    "InvalidStateError",
    "skin.auto_weights shared-mesh rejection",
)
assert [group.name for group in mesh.vertex_groups] == before_groups
assert weight_map(mesh) == before_weights

linked.data = linked.data.copy()
require_error(
    dispatch(
        "skin.auto_weights",
        {
            "object_name": mesh.name,
            "armature_name": armature.name,
            "max_candidate_pairs": 15,
        },
    ),
    "InvalidStateError",
    "skin.auto_weights candidate-budget rejection",
)

pose_bone = armature.pose.bones["Root"]
follow = pose_bone.constraints.new("COPY_LOCATION")
follow.name = "Follow"
follow.target = anchor
follow.influence = 0.35
follow.mute = True
rotate = pose_bone.constraints.new("COPY_ROTATION")
rotate.name = "Rotate"
rotate.target = anchor
rotate.influence = 0.65
rotate.mix_mode = "REPLACE"

follow.driver_add("influence")
require_error(
    dispatch(
        "pose.constraint_patch",
        {
            "object_name": armature.name,
            "operations": [{"op": "remove", "bone": "Root", "name": "Follow"}],
        },
    ),
    "InvalidStateError",
    "pose.constraint_patch owner-driver dependency rejection",
)
follow = pose_bone.constraints.get("Follow")
assert follow is not None
assert follow.driver_remove("influence") is True

source_driver = anchor.driver_add("location", 0)
variable = source_driver.driver.variables.new()
variable.name = "constraint_influence"
variable.type = "SINGLE_PROP"
variable.targets[0].id = armature
variable.targets[0].data_path = follow.path_from_id("influence")
source_driver.driver.expression = variable.name
require_error(
    dispatch(
        "pose.constraint_patch",
        {
            "object_name": armature.name,
            "operations": [{"op": "remove", "bone": "Root", "name": "Follow"}],
        },
    ),
    "InvalidStateError",
    "pose.constraint_patch cross-object-driver dependency rejection",
)
follow = pose_bone.constraints.get("Follow")
assert follow is not None
assert anchor.driver_remove("location", 0) is True

remove_plan = require_ok(
    dispatch(
        "pose.constraint_patch",
        {
            "object_name": armature.name,
            "operations": [{"op": "remove", "bone": "Root", "name": "Follow"}],
        },
    ),
    "pose.constraint_patch remove dry run",
)
assert remove_plan["dry_run"] is True
assert remove_plan["confirmation_required"] is True
assert remove_plan["removal_count"] == 1
assert remove_plan["journal"][0]["before"]["index"] == 0
assert pose_bone.constraints.get("Follow") is not None

require_error(
    dispatch(
        "pose.constraint_patch",
        {
            "object_name": armature.name,
            "operations": [{"op": "remove", "bone": "Root", "name": "Follow"}],
            "dry_run": False,
        },
    ),
    "ConfirmationRequiredError",
    "pose.constraint_patch remove confirmation gate",
)
assert pose_bone.constraints.get("Follow") is not None

rolled_back_constraint = require_error(
    dispatch(
        "pose.constraint_patch",
        {
            "object_name": armature.name,
            "operations": [
                {"op": "remove", "bone": "Root", "name": "Follow"},
                {
                    "op": "patch",
                    "bone": "Root",
                    "name": "Rotate",
                    "settings": {"mix_mode": "NOT_A_MODE"},
                },
            ],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "MutationApplyError",
    "pose.constraint_patch remove rollback",
)
assert rolled_back_constraint["changed"] is False
assert [constraint.name for constraint in pose_bone.constraints] == ["Follow", "Rotate"]
follow = pose_bone.constraints["Follow"]
assert follow.type == "COPY_LOCATION"
assert follow.target is anchor
assert abs(float(follow.influence) - 0.35) < 1e-6
assert follow.mute is True
assert pose_bone.constraints["Rotate"].mix_mode == "REPLACE"

removed_constraint = require_ok(
    dispatch(
        "pose.constraint_patch",
        {
            "object_name": armature.name,
            "operations": [{"op": "remove", "bone": "Root", "name": "Follow"}],
            "dry_run": False,
            "confirm": True,
        },
    ),
    "pose.constraint_patch remove apply",
)
assert removed_constraint["changed"] is True
assert removed_constraint["removed_count"] == 1
assert pose_bone.constraints.get("Follow") is None
assert [constraint.name for constraint in pose_bone.constraints] == ["Rotate"]

scene_path = artifact_dir / "rigging_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("RIGGING_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
