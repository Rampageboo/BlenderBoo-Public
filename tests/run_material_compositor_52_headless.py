"""Blender 5.2 smoke for material sockets and scene compositor API.

Run with blender --background --factory-startup --python tests/run_material_compositor_52_headless.py
"""
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.tools.material import inspect_material, patch_material
from blenderboo.tools.compositor import create_compositor, inspect_compositor, patch_compositor
from blenderboo.tools.render_manifest import inspect_render_manifest

assert bpy.app.version >= (5, 2, 0), bpy.app.version
material = bpy.data.materials.new("CBT 5.2 material")
material.use_nodes = True
principled = next(node for node in material.node_tree.nodes if node.bl_idname == "ShaderNodeBsdfPrincipled")
inspection = inspect_material({"material": material.name, "include_nodes": True}).data["materials"][0]
node_record = next(node for node in inspection["nodes"] if node["name"] == principled.name)
for socket in node_record["inputs"]:
    if not socket["editable"] or socket.get("default_value") is None:
        continue
    operation = {"op": "set_input", "node": principled.name, "input": socket["identifier"], "value": socket["default_value"]}
    assert patch_material({"material": material.name, "operations": [operation]})["journal"][0]["status"] == "planned", socket
weight = next(socket for socket in node_record["inputs"] if socket["name"] == "Weight")
assert patch_material({"material": material.name, "operations": [{"op": "set_input", "node": principled.name, "input": weight["identifier"], "value": 0.5}]})["journal"][0]["status"] == "planned"
assert patch_material({"material": material.name, "operations": [{"op": "disconnect", "to_node": principled.name, "to_input": weight["identifier"]}], "dry_run": False})["journal"][0]["status"] == "absent"

scene = bpy.context.scene
created = create_compositor({"dry_run": False})
assert created["changed"]
tree = scene.compositing_node_group
assert tree is not None
assert any(node.bl_idname == "NodeGroupOutput" for node in tree.nodes)
summary = inspect_compositor({}).data
assert summary["enabled"] and summary["node_count"] >= 2
manifest = inspect_render_manifest({}).data
assert manifest["compositor"]["enabled"]
assert manifest["compositor"]["composite_outputs"]

file_output = tree.nodes.new("CompositorNodeOutputFile")
file_output.name = "CBT file output"
assert patch_compositor({"operations": [{"op": "set_file_output", "node": file_output.name, "base_path": "//renders", "file_name": "beauty_"}], "dry_run": False})["changed"]
assert patch_compositor({"operations": [{"op": "add_file_slot", "node": file_output.name, "name": "Image"}], "dry_run": False})["changed"]
manifest = inspect_render_manifest({"include_paths": True}).data
assert manifest["compositor"]["file_outputs"]
assert manifest["compositor"]["file_outputs"][0]["slots"]
print("PASS material/compositor/render.manifest Blender 5.2")
