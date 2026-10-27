"""Headless point-cloud section and nearest-surface smoke test."""

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

import bpy
from blenderboo.dispatcher import dispatch

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add(size=2)
cube = bpy.context.object
cube.name = "Target"

coords = [(1.0 + .02 * ((i + j) % 2), -0.8 + i * .16, -0.8 + j * .16)
          for i in range(11) for j in range(11)]
mesh = bpy.data.meshes.new("ScanPoints")
mesh.from_pydata(coords, [], [])
cloud = bpy.data.objects.new("Scan", mesh)
bpy.context.scene.collection.objects.link(cloud)

with tempfile.TemporaryDirectory() as directory:
    section = dispatch("pointcloud.section", {
        "pointcloud_name": "Scan", "mesh_name": "Target",
        "plane_point": [0, 0, 0], "plane_normal": [0, 0, 1],
        "band_width": .2, "roi": {"min": [-2, -2, -.2], "max": [2, 2, .2]},
        "output_path": str(Path(directory) / "section.png")})
    assert section["ok"], section
    # The scan is spread across z; a wider band includes a measured slice.
    section = dispatch("pointcloud.section", {
        "pointcloud_name": "Scan", "mesh_name": "Target",
        "plane_point": [0, 0, 0], "plane_normal": [0, 0, 1],
        "band_width": 2, "output_path": str(Path(directory) / "section-all.png")})
    assert section["ok"] and section["data"]["point_count"] > 0, section
    assert section["data"]["mesh_section_2d"], section
    assert Path(section["data"]["image_path"]).is_file(), section

    comparison = dispatch("mesh.compare_to_points", {
        "pointcloud_name": "Scan", "mesh_name": "Target", "min_points": 50,
        "output_path": str(Path(directory) / "distance.png")})
    assert comparison["ok"] and comparison["data"]["status"] == "measured", comparison
    assert comparison["data"]["distance"]["p95"] < .03, comparison
    assert Path(comparison["data"]["image_path"]).is_file(), comparison
    sparse = dispatch("mesh.compare_to_points", {"pointcloud_name": "Scan", "mesh_name": "Target",
        "min_points": 50, "roi": {"min": [0, -.1, -.1], "max": [2, .1, .1]}})
    assert sparse["ok"] and sparse["data"]["status"] == "undetermined", sparse

print("pointcloud review headless passed")
