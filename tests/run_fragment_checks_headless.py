"""Blender qualification for v0.7.3 fragment evidence and array connectivity."""

from pathlib import Path
from time import perf_counter
import sys

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for name in list(sys.modules):
    if name == "blenderboo" or name.startswith("blenderboo."):
        del sys.modules[name]

from blenderboo.tools.geometry import apply_modifier
from blenderboo.tools.mesh_topology import component_summary, topology_report


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1)
body = bpy.context.object
body.name = "FragmentBody"
body.scale = (2.0, 0.2, 0.2)
bpy.ops.mesh.primitive_cube_add(size=1, location=(0.88, 0, 0))
cutter = bpy.context.object
cutter.name = "FragmentCutter"
cutter.scale = (0.2, 0.5, 0.5)
modifier = body.modifiers.new("SplitSliver", "BOOLEAN")
modifier.operation = "DIFFERENCE"
modifier.solver = "EXACT"
modifier.object = cutter
split = apply_modifier({"object_name": body.name, "modifier_name": modifier.name,
                        "dry_run": False, "confirm": True,
                        "fragment_small_ratio": 0.02})
assert split["changed"] is True, split
assert any(item["reason"] == "new_islands" for item in split.get("suspicious_change", [])), split
finding = split["suspicious_change"][0]
assert finding["before"] == 1 and finding["after"] == 2, finding
assert finding["small_islands"] and finding["small_islands"][0]["bbox_volume_ratio"] <= 0.02, finding
report = topology_report({"object_name": body.name}).data
assert report["islands"] == 2 and report["smallest_islands"], report
assert set(report["smallest_islands"][0]["focus"]) == {"point", "radius"}, report
print("PASS Boolean sliver and topology focus")


bpy.ops.mesh.primitive_cube_add(size=1, location=(5, 0, 0))
clean = bpy.context.object
clean.name = "CleanBody"
clean_bevel = clean.modifiers.new("CleanBevel", "BEVEL")
clean_bevel.width = 0.05
clean_result = apply_modifier({"object_name": clean.name, "modifier_name": clean_bevel.name,
                               "dry_run": False, "confirm": True})
assert not any(item["reason"] == "new_islands" for item in clean_result.get("suspicious_change", [])), clean_result
print("PASS clean modifier has no new island warning")


# A sparse mesh stresses the old per-vertex Python neighbor-list path. Mesh
# creation is excluded from the measured array-connectivity interval.
mesh = bpy.data.meshes.new("Sparse1_5M")
mesh.vertices.add(1_500_000)
mesh.vertices.foreach_set("co", np.zeros(4_500_000, dtype=np.float32))
start = perf_counter()
sparse = component_summary(mesh)
elapsed = perf_counter() - start
assert sparse["islands"] == 1_500_000, sparse["islands"]
assert sparse["smallest_vertices"] == 1, sparse
print(f"PERFORMANCE 1.5M sparse vertices component_summary: {elapsed:.3f} s")
