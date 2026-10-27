"""Disposable Blender fixture: measurable bleed cleanup and restored sampling.

Run: blender --background --factory-startup --python tests/run_skin_audit_headless.py
No production file is opened or saved.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.skin_audit import audit_skin


def fixture():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    data = bpy.data.armatures.new("AuditRigData")
    rig = bpy.data.objects.new("AuditRig", data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    for name, x in (("Left", -1), ("Right", 1)):
        bone = data.edit_bones.new(name)
        bone.head, bone.tail = (x, 0, 0), (x, 4, 0)
    bpy.ops.object.mode_set(mode="OBJECT")
    points, faces = [], []
    for x in (-1, 1):
        offset = len(points)
        for y in (0, 2, 4):
            points.extend([(x - .2, y, .1), (x + .2, y, .1)])
        for row in (0, 2):
            a = offset + row
            faces.extend([(a, a + 1, a + 2), (a + 1, a + 3, a + 2)])
    mesh_data = bpy.data.meshes.new("AuditBodyData")
    mesh_data.from_pydata(points, [], faces)
    mesh_data.update()
    mesh = bpy.data.objects.new("AuditBody", mesh_data)
    bpy.context.scene.collection.objects.link(mesh)
    left = mesh.vertex_groups.new(name="Left")
    right = mesh.vertex_groups.new(name="Right")
    left.add(list(range(6)), 1, "REPLACE")
    right.add(list(range(6, 12)), 1, "REPLACE")
    modifier = mesh.modifiers.new("Armature", "ARMATURE")
    modifier.object = rig
    # The geometric plane is tested after distinct mesh/rig object transforms.
    rig.location = (3, 2, 1)
    mesh.location = rig.location
    rig.scale = (1.2, 1.2, 1.2)
    mesh.scale = rig.scale
    bpy.context.scene.frame_set(7, subframe=.25)
    rig.pose.bones["Left"].rotation_mode = "QUATERNION"
    rig.pose.bones["Left"].rotation_quaternion = (.995004, 0, .099833, 0)
    bpy.context.view_layer.update()
    return mesh, rig, left, right


def fingerprint(mesh, rig):
    animation = rig.animation_data
    return {
        "frame": [bpy.context.scene.frame_current, bpy.context.scene.frame_subframe],
        "objects": sorted(bpy.data.objects.keys()), "meshes": sorted(bpy.data.meshes.keys()),
        "actions": sorted(bpy.data.actions.keys()), "groups": {
            str(vertex.index): [(entry.group, float(entry.weight)) for entry in vertex.groups]
            for vertex in mesh.data.vertices},
        "pose": {bone.name: [bone.rotation_mode, [value for row in bone.matrix_basis for value in row]]
                 for bone in rig.pose.bones},
        "pose_channels": {bone.name: {field: list(getattr(bone, field)) for field in (
            "location", "scale", "rotation_euler", "rotation_quaternion", "rotation_axis_angle")}
            for bone in rig.pose.bones},
        "pose_position": rig.data.pose_position,
        "action": animation.action.name if animation and animation.action else None,
        "slot": getattr(animation.action_slot, "identifier", None) if animation else None,
        "slot_identifier": getattr(animation, "last_slot_identifier", None) if animation else None,
        "has_animation": animation is not None,
        "nla": animation.use_nla if animation else None,
        "blend": animation.action_blend_type if animation else None,
        "influence": animation.action_influence if animation else None,
        "basis": [value for row in rig.matrix_basis for value in row],
        "selection": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name,
    }


def assert_restored(before, mesh, rig):
    after = fingerprint(mesh, rig)
    # Blender decomposes matrix_basis when restoring quaternion/euler values.
    for name in before["pose"]:
        assert before["pose"][name][0] == after["pose"][name][0]
        assert max(abs(a-b) for a, b in zip(before["pose"][name][1], after["pose"][name][1])) < 1e-6
        for field, values in before["pose_channels"][name].items():
            assert max(abs(a-b) for a, b in zip(values, after["pose_channels"][name][field])) < 1e-6, (name, field)
    assert max(abs(a-b) for a, b in zip(before["basis"], after["basis"])) < 1e-6
    for key in before.keys() - {"pose", "pose_channels", "basis"}:
        assert before[key] == after[key], (key, before[key], after[key])


mesh, rig, left, right = fixture()
samples = [{"name": "right_lift", "bones": [{"bone": "Right", "axis": "X", "degrees": 90}]}]
params = {"object_name": mesh.name, "samples": samples}
left.add([5], .7, "REPLACE")
right.add([5], .3, "REPLACE")
left.add(list(range(6, 12)), .3, "REPLACE")
right.add(list(range(6, 12)), .7, "REPLACE")
before = fingerprint(mesh, rig)
bled = audit_skin(params)
assert_restored(before, mesh, rig)
json.dumps(bled, allow_nan=False)
left_bad = next(item for item in bled["per_bone"] if item["bone"] == "Left")
assert "side_bleed" in left_bad["flags"], left_bad
assert "far_spill" in left_bad["flags"], left_bad
assert bled["samples"][0]["edge_stretch"]["max"] > 2, bled["samples"]

# Target only the deliberately injected membership errors. This fixture does
# not recommend nearest-bone/sharp reweighting at real organic joints.
right.remove([5])
left.add([5], 1, "REPLACE")
left.remove(list(range(6, 12)))
right.add(list(range(6, 12)), 1, "REPLACE")
before = fingerprint(mesh, rig)
clean = audit_skin(params)
assert_restored(before, mesh, rig)
left_clean = next(item for item in clean["per_bone"] if item["bone"] == "Left")
assert left_clean["side_bleed"]["weight_fraction"] < left_bad["side_bleed"]["weight_fraction"]
assert left_clean["far_spill"]["weight_fraction"] < left_bad["far_spill"]["weight_fraction"]
assert clean["samples"][0]["edge_stretch"]["max"] < bled["samples"][0]["edge_stretch"]["max"]
assert abs(clean["samples"][0]["edge_stretch"]["p99"] - 1) < 1e-5
assert clean["samples"][0]["self_intersections"]["added_count"] == 0
assert clean["threshold_policy"] == "advisory_only" and clean["changed"] is False

# A quaternion action must evaluate in its original mode, and sampling must
# restore active action/slot, frame/subframe, influence and existing pose.
bone = rig.pose.bones["Left"]
bone.rotation_quaternion = (1, 0, 0, 0)
bone.keyframe_insert("rotation_quaternion", frame=1)
bone.rotation_quaternion = (.70710678, .70710678, 0, 0)
bone.keyframe_insert("rotation_quaternion", frame=5)
action = rig.animation_data.action
action.name = "AuditQuaternionClip"
rig.animation_data.action_blend_type = "ADD"
rig.animation_data.action_influence = .6
bpy.context.scene.frame_set(7, subframe=.25)
before = fingerprint(mesh, rig)
animated = audit_skin({"object_name": mesh.name,
                       "samples": [{"name": "action_sample", "action_name": action.name, "frame": 5}]})
assert_restored(before, mesh, rig)
assert animated["samples"][0]["status"] == "measured", animated["samples"]
assert abs(animated["samples"][0]["edge_stretch"]["max"] - 1) < 1e-5

# Incomplete intersection coverage cannot claim zero newly added pairs.
before = fingerprint(mesh, rig)
bounded = audit_skin({**params, "max_pair_checks": 1})
assert_restored(before, mesh, rig)
assert not bounded["coverage"]["deformation_complete"]
assert bounded["samples"][0]["self_intersections"]["added_count"] is None

# Failure during evaluated collection must also restore sampling state.
before = fingerprint(mesh, rig)
try:
    audit_skin({**params, "max_triangles": 1})
    raise AssertionError("Expected triangle budget failure")
except InvalidStateError:
    pass
assert_restored(before, mesh, rig)

before = fingerprint(mesh, rig)
try:
    audit_skin({"object_name": mesh.name, "samples": [{"name": "missing", "action_name": "NoSuchAction", "frame": 1}]})
    raise AssertionError("Expected missing action")
except Exception as exc:
    assert "Action not found" in str(exc), exc
assert_restored(before, mesh, rig)

# Actions which key arbitrary custom properties cannot be safely represented
# by the transform-only restoration contract and must fail before sampling.
rig["audit_custom"] = .5
rig.keyframe_insert('["audit_custom"]', frame=1)
before = fingerprint(mesh, rig)
try:
    audit_skin({"object_name": mesh.name, "samples": [{"name": "custom", "action_name": action.name, "frame": 1}]})
    raise AssertionError("Expected non-transform action rejection")
except InvalidParamsError as exc:
    assert "transform-only" in str(exc), exc
assert_restored(before, mesh, rig)
assert rig["audit_custom"] == .5

print(json.dumps({"status": "PASS", "bled": {"side_bleed_fraction": left_bad["side_bleed"]["weight_fraction"],
                    "far_spill_fraction": left_bad["far_spill"]["weight_fraction"],
                    "pose_edge_max": bled["samples"][0]["edge_stretch"]["max"]},
                  "clean": {"side_bleed_fraction": left_clean["side_bleed"]["weight_fraction"],
                    "far_spill_fraction": left_clean["far_spill"]["weight_fraction"],
                    "pose_edge_max": clean["samples"][0]["edge_stretch"]["max"]},
                  "quaternion_action": animated["samples"][0]}, allow_nan=False))
