"""Exercise the live bake job path by launching a real Blender child.

Run: blender -b --factory-startup --python-exit-code 1 --python tests/run_ai_source_bake_background_headless.py
"""
from pathlib import Path
import sys
import tempfile
import time

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.tools.background_render import BAKE_MODE, BackgroundRenderManager


def plane(name, height):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-1, -1, height), (1, -1, height), (1, 1, height), (-1, 1, height)],
                     [], [(0, 1, 2, 3)])
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    for index, point in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
        uv.data[index].uv = point
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


bpy.ops.wm.read_factory_settings(use_empty=True)
high = plane("High", 0)
low = plane("Low", .002)
material = bpy.data.materials.new("Source")
material.use_nodes = True
material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (.3, .2, .8, 1)
high.data.materials.append(material)
before_path = bpy.data.filepath
before_meshes = sorted(mesh.name for mesh in bpy.data.meshes)
with tempfile.TemporaryDirectory(prefix="blenderboo-bake-job-") as temp:
    manager = BackgroundRenderManager(temp_root=Path(temp) / "jobs")
    try:
        receipt = manager.start({"mode": BAKE_MODE, "options": {
            "low": low.name, "high": high.name, "output_dir": str(Path(temp) / "textures"),
            "resolution": 32, "cage_extrusion": 0, "max_ray_distance": .001,
            "fallback_max_distance": .01, "margin": 1, "ao_samples": 1,
        }})
        operation_id = receipt.data["operation_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            manager.pump()
            status = manager.status(operation_id)
            if status["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.2)
        assert status["status"] == "succeeded", status
        output = status["result"]["data"]
        assert output["fallback_hit_texels"] > 0, output
        assert Path(output["paths"]["BaseColor"]).is_file(), output
        assert output["paths"]["BaseColor"] in status["output_paths"]
        assert bpy.data.filepath == before_path
        assert sorted(mesh.name for mesh in bpy.data.meshes) == before_meshes
        print("AI_SOURCE_BAKE_BACKGROUND_OK", output["fallback_hit_texels"], output["miss_texels"])
    finally:
        manager.shutdown()
