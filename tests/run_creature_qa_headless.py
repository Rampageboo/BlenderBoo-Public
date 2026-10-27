"""Blender 5.2 source baking and measured ROM with restored pose/state."""

from pathlib import Path
import sys
import tempfile
from math import cos, sin, tau

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for name in list(sys.modules):
    if name == "blenderboo" or name.startswith("blenderboo."):
        del sys.modules[name]

from blenderboo.tools.creature_qa import bake_from_source
from blenderboo.errors import InvalidStateError
from blenderboo.tools.rig_rom import rom_test


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
low = bpy.context.object
low.name = "FittedLow"
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode="OBJECT")
uv_name = low.data.uv_layers.active.name
target_material = bpy.data.materials.new("FittedMaterial")
target_material.use_nodes = True
low.data.materials.append(target_material)

bpy.ops.mesh.primitive_cube_add()
source = bpy.context.object
source.name = "OriginalHigh"
source_material = bpy.data.materials.new("SourceMaterial")
source_material.use_nodes = True
source.data.materials.append(source_material)
bevel = source.modifiers.new("SourceBevel", "BEVEL")
bevel.width = .12
bevel.segments = 3
bpy.ops.object.modifier_apply(modifier=bevel.name)
source_vertex_count = len(source.data.vertices)
source_polygon_count = len(source.data.polygons)

with tempfile.TemporaryDirectory(prefix="cbt_creature_qa_") as temp:
    root = Path(temp)
    preflight = {"sources": [source.name], "target": low.name, "uv_layer": uv_name,
                 "output_dir": str(root), "maps": ["normal"]}
    original_mesh = low.data
    low.data = source.data
    try:
        try:
            bake_from_source(preflight)
            raise AssertionError("shared source mesh was accepted")
        except InvalidStateError as exc:
            assert "mesh datablock" in str(exc), exc
    finally:
        low.data = original_mesh
    low.data.materials.append(source_material)
    try:
        try:
            bake_from_source(preflight)
            raise AssertionError("shared source material was accepted")
        except InvalidStateError as exc:
            assert "shares source materials" in str(exc), exc
    finally:
        low.data.materials.pop(index=len(low.data.materials) - 1)
    source_image = bpy.data.images.new("FittedLow_normal", width=2, height=2)
    source_image_node = source_material.node_tree.nodes.new("ShaderNodeTexImage")
    source_image_node.image = source_image
    try:
        try:
            bake_from_source(preflight)
            raise AssertionError("source texture image-name collision was accepted")
        except InvalidStateError as exc:
            assert "Bake image names are used by source textures" in str(exc), exc
            assert "FittedLow_normal" in str(exc), exc
        assert not (root / "FittedLow_normal.png").exists()
    finally:
        source_material.node_tree.nodes.remove(source_image_node)
        bpy.data.images.remove(source_image)
    bake = bake_from_source({
        "sources": [source.name], "target": low.name, "uv_layer": uv_name,
        "output_dir": str(root), "image_prefix": "creature", "resolution": 64,
        "margin": 4, "cage_extrusion": .05, "max_ray_distance": .3,
        "maps": ["normal", "ao", "base_color"],
    }).data
    assert set(bake["maps"]) == {"normal", "ao", "base_color"}
    assert all(Path(item["path"]).is_file() and item["coverage_ratio"] > .05
               for item in bake["maps"].values()), bake
    assert bake["source_geometry_unchanged"]
    assert len(source.data.vertices) == source_vertex_count
    assert len(source.data.polygons) == source_polygon_count
    print("PASS original-source normal/AO/base-color bake and source digest")

    bpy.ops.wm.read_factory_settings(use_empty=True)
    verts = []
    rings = 5
    sides = 12
    for ring in range(rings):
        z = ring * .5
        for side in range(sides):
            angle = tau * side / sides
            verts.append((.35 * cos(angle), .35 * sin(angle), z))
    faces = []
    faces.append(tuple(reversed(range(sides))))
    for ring in range(rings - 1):
        for side in range(sides):
            next_side = (side + 1) % sides
            faces.append((ring * sides + side, ring * sides + next_side,
                          (ring + 1) * sides + next_side, (ring + 1) * sides + side))
    faces.append(tuple((rings - 1) * sides + side for side in range(sides)))
    mesh_data = bpy.data.meshes.new("ROMMeshData")
    mesh_data.from_pydata(verts, [], faces)
    mesh_data.update()
    mesh = bpy.data.objects.new("ROMMesh", mesh_data)
    bpy.context.scene.collection.objects.link(mesh)
    weights = mesh.vertex_groups.new(name="Tip")
    for ring in range(rings):
        weight = max(.001, min(1.0, ring / (rings - 1)))
        weights.add(list(range(ring * sides, (ring + 1) * sides)), weight, "REPLACE")
    rig_data = bpy.data.armatures.new("ROMRigData")
    rig = bpy.data.objects.new("ROMRig", rig_data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    root_bone = rig_data.edit_bones.new("Root")
    root_bone.head = (0, 0, 0)
    root_bone.tail = (0, 0, 1)
    tip_bone = rig_data.edit_bones.new("Tip")
    tip_bone.head = (0, 0, 1)
    tip_bone.tail = (0, 0, 2)
    tip_bone.parent = root_bone
    bpy.ops.object.mode_set(mode="OBJECT")
    modifier = mesh.modifiers.new("ROMArmature", "ARMATURE")
    modifier.object = rig
    bpy.context.view_layer.update()
    before_pose = {bone.name: bone.matrix_basis.copy() for bone in rig.pose.bones}
    before_engine = bpy.context.scene.render.engine
    before_camera = bpy.context.scene.camera

    outcome = rom_test({
        "object_name": mesh.name, "armature_name": rig.name, "body_plan": "custom",
        "poses": [{"name": "tip_bend", "bones": [
            {"bone": "Tip", "axis": "X", "degrees": 45}]}],
        "output_prefix": str(root / "rom"), "width": 192, "height": 192,
        "view_mode": "matcap", "max_triangles": 2000,
    }).data
    assert outcome["poses"][0]["name"] == "tip_bend"
    assert outcome["poses"][0]["edge"]["max_stretch"]["ratio"] >= 1
    assert outcome["poses"][0]["regions"][0]["sample_vertices"] > 0
    assert Path(outcome["contact_sheet"]).is_file()
    assert Path(outcome["pose_images"]["tip_bend"]).is_file()
    assert all(rig.pose.bones[name].matrix_basis == matrix for name, matrix in before_pose.items())
    assert bpy.context.scene.render.engine == before_engine
    assert bpy.context.scene.camera is before_camera
    print("PASS ROM metrics, contact sheet, and complete pose/render restoration")
