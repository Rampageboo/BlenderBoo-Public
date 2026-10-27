"""Known bevel-to-cube tangent-space bake on Blender 5.2.

Run with blender --background --factory-startup --python tests/run_bake_headless.py
"""
from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.tools.bake import run_bake

assert bpy.app.version >= (5, 2, 0), bpy.app.version
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add()
low = bpy.context.object
low.name = "Low bake cube"
if not low.data.uv_layers:
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project()
    bpy.ops.object.mode_set(mode="OBJECT")
uv_name = low.data.uv_layers.active.name
material = bpy.data.materials.new("Bake target material")
material.use_nodes = True
low.data.materials.append(material)

bpy.ops.mesh.primitive_cube_add()
high = bpy.context.object
high.name = "High bevel cube"
source_material = bpy.data.materials.new("Bake source material")
source_material.use_nodes = True
high.data.materials.append(source_material)
bevel = high.modifiers.new("Known bevel", "BEVEL")
bevel.width = 0.2
bevel.segments = 4
bpy.ops.object.modifier_apply(modifier=bevel.name)

previous_engine = bpy.context.scene.render.engine
previous_active = bpy.context.view_layer.objects.active
with tempfile.TemporaryDirectory() as temp_dir:
    path = Path(temp_dir) / "normal.png"
    receipt = run_bake({
        "type": "NORMAL", "sources": [high.name], "target": low.name,
        "uv_layer": uv_name, "image": "Known bevel normal", "resolution": 256,
        "margin": 8, "cage_extrusion": 0.05, "max_ray_distance": 0.3,
        "output_path": str(path), "normal_g": "POS_Y",
    })
    assert receipt["changed"] and path.is_file() and path.stat().st_size > 0, receipt
    assert receipt["coverage_ratio"] > 0.1, receipt
    image = bpy.data.images[receipt["image"]]
    colors = [image.pixels[index:index + 3] for index in range(0, len(image.pixels), 4 * 113)]
    assert any(abs(pixel[1] - 0.5) > 0.08 for pixel in colors), "Expected green-channel bevel gradients"
    assert receipt["normal_g"] == "POS_Y"
assert bpy.context.scene.render.engine == previous_engine
assert bpy.context.view_layer.objects.active == previous_active
print("PASS bake.run known bevel normal")
