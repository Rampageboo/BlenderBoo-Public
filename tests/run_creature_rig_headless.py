"""Generated non-Epic reference skeleton and weight-transfer qualification."""

from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo.errors import InvalidStateError
from blenderboo.tools.creature import graph_set, base_from_graph
from blenderboo.tools.creature_rig import from_body_graph, validate_against_reference, export_ue_fbx
from blenderboo.tools.skinning import weights_limit, weights_transfer_by_index, validate_skin

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=0.5)
holder = bpy.context.object
holder.name = "RigGraph"
graph = {"body_plan": "custom", "joints": [
    {"name": "root_joint", "parent": None, "position": [0, 0, 0], "radius": 0.25},
    {"name": "spine", "parent": "root_joint", "position": [0, 0, 1], "radius": 0.2},
    {"name": "neck", "parent": "spine", "position": [0, 0, 1.5], "radius": 0.13},
    {"name": "head", "parent": "neck", "position": [0, 0, 1.8], "radius": 0.18},
]}
graph_set({"object_name": holder.name, "graph": graph})
base = base_from_graph({"graph_object": holder.name, "output_name": "SkinBody"}).data
body = bpy.data.objects[base["object"]]
custom = from_body_graph({"graph_object": holder.name, "output_name": "CustomRig"}).data
rig = bpy.data.objects[custom["armature"]]
assert {"root", "spine", "neck", "head"} == {bone.name for bone in rig.data.bones}
assert validate_against_reference({"armature_name": rig.name}).data["valid"]

# Generated fixture exercises auxiliary bone preservation without Epic content.
for selected in bpy.context.selected_objects:
    selected.select_set(False)
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
aux = rig.data.edit_bones.new("ik_foot_root")
aux.head = (0, 0, 0)
aux.tail = (0, -0.2, 0)
aux.parent = rig.data.edit_bones["root"]
aux.use_deform = False
bpy.ops.object.mode_set(mode="OBJECT")
reference = from_body_graph({"graph_object": holder.name, "output_name": "ReferenceCopy",
                             "reference_armature_name": rig.name,
                             "bone_map": {"root_joint": "root", "spine": "spine", "neck": "neck", "head": "head"}}).data
assert reference["mode"] == "exact_reference_copy"
copied = bpy.data.objects[reference["armature"]]
assert "ik_foot_root" in copied.data.bones
assert validate_against_reference({"armature_name": copied.name,
                                   "reference_armature_name": rig.name}).data["matches_reference"]

modifier = body.modifiers.new("Armature", "ARMATURE")
modifier.object = rig
groups = {name: body.vertex_groups.new(name=name) for name in ("spine", "neck", "head", "ik_foot_root")}
for vertex in body.data.vertices:
    groups["spine"].add([vertex.index], 0.4, "REPLACE")
    groups["neck"].add([vertex.index], 0.35, "REPLACE")
    groups["head"].add([vertex.index], 0.25, "REPLACE")
groups["ik_foot_root"].add([0], 0.2, "REPLACE")
before = validate_skin({"object_name": body.name, "armature_name": rig.name})
assert before["nondeform_bone_weight_count"] == 1
assert before["influence_histogram"]["3"] == len(body.data.vertices)
dry = weights_limit({"object_name": body.name, "armature_name": rig.name,
                     "max_influences": 2, "prune_threshold": 0.01})
assert dry["dry_run"]
applied = weights_limit({"object_name": body.name, "armature_name": rig.name,
                         "max_influences": 2, "prune_threshold": 0.01,
                         "dry_run": False, "confirm": True})
assert applied["changed"]
after = validate_skin({"object_name": body.name, "armature_name": rig.name})
assert after["influence_histogram"]["2"] == len(body.data.vertices)

target = body.copy()
target.data = body.data.copy()
target.name = "TransferTarget"
bpy.context.scene.collection.objects.link(target)
for group in list(target.vertex_groups):
    target.vertex_groups.remove(group)
plan = weights_transfer_by_index({"source_name": body.name, "object_name": target.name,
                                  "group_names": ["spine", "neck", "head"]})
assert plan["dry_run"]
transfer = weights_transfer_by_index({"source_name": body.name, "object_name": target.name,
                                      "group_names": ["spine", "neck", "head"],
                                      "dry_run": False, "confirm": True})
assert transfer["changed"] and target.vertex_groups.get("spine")
linked = body.copy()
linked.name = "LinkedMeshTarget"
bpy.context.scene.collection.objects.link(linked)
try:
    weights_transfer_by_index({"source_name": body.name, "object_name": linked.name})
    raise AssertionError("shared Mesh datablock was accepted")
except InvalidStateError:
    pass
target.data.vertices[0].co.x += 0.1
weights_transfer_by_index({"source_name": body.name, "object_name": target.name,
                           "group_names": ["spine"], "dry_run": True})  # coordinates may move
target.data.vertices[0].co.x -= 0.1
import bmesh
bm = bmesh.new()
bm.from_mesh(target.data)
bm.faces.ensure_lookup_table()
bmesh.ops.delete(bm, geom=[bm.faces[0]], context="FACES_ONLY")
bm.to_mesh(target.data)
bm.free()
try:
    weights_transfer_by_index({"source_name": body.name, "object_name": target.name})
    raise AssertionError("topology mismatch was accepted")
except InvalidStateError:
    pass

with tempfile.TemporaryDirectory(prefix="cbt_creature_rig_") as directory:
    result = export_ue_fbx({"armature_name": rig.name, "mesh_name": body.name,
                            "path": str(Path(directory) / "creature.fbx"), "roundtrip": True}).data
    assert Path(result["path"]).is_file()
    assert result["roundtrip"]["bones_match"], result["roundtrip"]
    before_bytes = Path(result["path"]).read_bytes()
    try:
        export_ue_fbx({"armature_name": rig.name, "mesh_name": body.name,
                       "path": result["path"], "roundtrip": False})
        raise AssertionError("existing export was overwritten without consent")
    except InvalidStateError:
        pass
    assert Path(result["path"]).read_bytes() == before_bytes
print("PASS creature skeleton, reference fixture, weight limit/transfer, FBX roundtrip")
