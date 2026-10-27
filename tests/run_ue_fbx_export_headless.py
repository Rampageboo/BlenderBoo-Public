"""Disposable UE FBX units, hierarchy, animation timing and restoration regression.

Run: blender --background --factory-startup --python tests/run_ue_fbx_export_headless.py
No production files or UE project are opened. Unreal qualification remains separate.
"""
from pathlib import Path
import json
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.tools.creature_rig import export_ue_fbx


def fixture(object_root=False):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.object.armature_add()
    rig = bpy.context.object
    rig.name = "FixtureRig"
    bpy.ops.object.mode_set(mode="EDIT")
    data = rig.data.edit_bones
    data.remove(data[0])
    root = None
    if not object_root:
        root = data.new("root")
        root.head, root.tail = (0, 0, 0), (0, 0, .2)
        root.use_deform = False
    pelvis = data.new("pelvis")
    pelvis.head, pelvis.tail, pelvis.parent = (0, 0, .8), (0, 0, 1.2), root
    tip = data.new("tip")
    tip.head, tip.tail, tip.parent = (0, 0, 1.2), (0, 0, 1.8), pelvis
    helper = data.new("helper")
    helper.head, helper.tail, helper.parent = (0, .2, .8), (0, .4, .8), pelvis
    helper.use_deform = False
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 1))
    mesh = bpy.context.object
    mesh.name = "FixtureMesh"
    mesh.scale = (.6, .4, 1.5)  # unapplied mesh object scale must round-trip correctly
    modifier = mesh.modifiers.new("Skin", "ARMATURE")
    modifier.object = rig
    mesh.vertex_groups.new(name="pelvis").add(list(range(len(mesh.data.vertices))), 1, "REPLACE")
    for name, end in (("Walk30", 31), ("Run120", 121)):
        rig.animation_data_create().action = None
        rig.pose.bones["pelvis"].location = (0, 0, 0)
        rig.pose.bones["pelvis"].keyframe_insert("location", frame=1)
        rig.pose.bones["pelvis"].location = (0, .15, 0)
        rig.pose.bones["pelvis"].keyframe_insert("location", frame=end)
        rig.animation_data.action.name = name
    rig.animation_data.action = bpy.data.actions["Walk30"]
    bpy.context.scene.frame_set(12)
    return rig, mesh


def state(rig):
    scene = bpy.context.scene
    return {"objects": sorted(o.name for o in bpy.data.objects),
            "meshes": sorted(o.name for o in bpy.data.meshes),
            "armatures": sorted(o.name for o in bpy.data.armatures),
            "actions": sorted(o.name for o in bpy.data.actions),
            "materials": sorted(o.name for o in bpy.data.materials),
            "scene": scene.name, "fps": [scene.render.fps, scene.render.fps_base],
            "frame": [scene.frame_current, scene.frame_subframe],
            "range": [scene.frame_start, scene.frame_end],
            "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
            "selected": sorted(o.name for o in bpy.context.selected_objects), "mode": bpy.context.mode,
            "pose_position": rig.data.pose_position,
            "pose": {p.name: [list(row) for row in p.matrix_basis] for p in rig.pose.bones},
            "action": rig.animation_data.action.name,
            "slot": rig.animation_data.action_slot.identifier,
            "nla": rig.animation_data.use_nla}


def export(rig, mesh, path, **kwargs):
    before = state(rig)
    receipt = export_ue_fbx({"armature_name": rig.name, "mesh_name": mesh.name,
                             "path": str(path), **kwargs}).data
    assert state(rig) == before, ("Scene state changed", before, state(rig))
    return receipt


receipts = []
with tempfile.TemporaryDirectory(prefix="blenderboo_ue_fbx_") as directory:
    directory = Path(directory)
    for object_root in (False, True):
        rig, mesh = fixture(object_root)
        # Name collisions must not silently create Armature.001/root.001.
        collision = bpy.data.objects.new("root" if object_root else "Armature", None)
        bpy.context.scene.collection.objects.link(collision)
        path = directory / ("object_root.fbx" if object_root else "bone_root.fbx")
        receipt = export(rig, mesh, path, use_armature_deform_only=True)
        assert receipt["acceptance"] == "pass", receipt
        assert receipt["published"] and path.is_file()
        assert receipt["root"]["resolved"] == ("object_is_root" if object_root else "object_stripped")
        assert receipt["root"]["ue_root_bone"] == "root"
        assert len(receipt["root"]["ue_parent_map"]) == receipt["root"]["ue_bone_count"]
        assert receipt["roundtrip"]["root_bones"] == (["pelvis"] if object_root else ["root"])
        assert receipt["exports"][0]["serialized"]["literal_centimeters"]
        assert receipt["exports"][0]["serialized"]["unit_scale_cm"] == 1.0
        nodes = receipt["exports"][0]["serialized"]["nodes"]
        assert abs(sum(v*v for v in nodes["pelvis"]["translation_cm"])**.5 - 80) < .01
        assert "helper" not in nodes and "pelvis" in nodes
        assert receipt["roundtrip"]["mesh_bounds_max_error_scene_units"] < 1e-3
        receipts.append(receipt)
        before_bytes = path.read_bytes()
        old = export(rig, mesh, path, overwrite=True, use_armature_deform_only=True,
                     apply_scale_options="FBX_SCALE_NONE")
        assert old["acceptance"] == "fail" and not old["published"], old
        assert old["exports"][0]["serialized"]["nonunit_node_scales"], old
        assert path.read_bytes() == before_bytes
        receipts.append(old)
        for include_mesh in (False, True):
            animated = export(rig, mesh, directory / f"clips_{object_root}_{include_mesh}.fbx",
                              action_names=["Walk30", "Run120"], action_fps={"Walk30": 30, "Run120": 120},
                              include_mesh=include_mesh, use_armature_deform_only=True)
            assert animated["acceptance"] == "pass", animated
            for item in animated["exports"]:
                assert item["published"] and Path(item["path"]).is_file()
                assert item["serialized"]["nodes"].keys() == nodes.keys()
                assert item["roundtrip"]["animation_timing"]["passed"]
                assert abs(item["roundtrip"]["animation_timing"]["duration_s"] - 1) < 1e-4
            receipts.append(animated)
        explicit = export(rig, mesh, directory / f"explicit_{object_root}.fbx",
                          root_mode="object_is_root" if object_root else "object_stripped")
        assert explicit["acceptance"] == "pass", explicit
        assert "helper" in explicit["exports"][0]["serialized"]["nodes"]
        # An export operator failure must still restore source actions/context and clean staged files.
        import blenderboo.tools.ue_fbx_export as module
        original_receipt = module._serialized_receipt
        before = state(rig)
        try:
            def fail(*args, **kwargs):
                raise RuntimeError("injected qualification failure")
            module._serialized_receipt = fail
            try:
                export_ue_fbx({"armature_name": rig.name, "mesh_name": mesh.name,
                               "path": str(directory / "injected.fbx"), "action_names": ["Run120"]})
                raise AssertionError("Injected failure was ignored")
            except RuntimeError as exc:
                assert "injected" in str(exc)
        finally:
            module._serialized_receipt = original_receipt
        assert before == state(rig)
        assert not list(directory.glob("*.part.fbx"))
    # A genuinely scaled rig is also unsafe, even with the corrected unit handling.
    rig, mesh = fixture()
    rig.scale = (2, 2, 2)
    bpy.context.view_layer.update()
    scaled = export(rig, mesh, directory / "scaled.fbx")
    assert scaled["acceptance"] == "fail", scaled
    assert not (directory / "scaled.fbx").exists()
print(json.dumps({"case_count": len(receipts), "acceptances": [r["acceptance"] for r in receipts]}))
print("PASS UE FBX units, both roots, deform-only ancestry, mesh scale, 30/120 FPS actions, restoration and failure preservation")
