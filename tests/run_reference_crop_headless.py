"""Reference crop/grid image and coordinate smoke test."""

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

import bpy
from blenderboo.dispatcher import dispatch

with tempfile.TemporaryDirectory() as directory:
    image = bpy.data.images.new("Reference", width=64, height=64, alpha=True)
    try:
        image.pixels[:] = [0.5, 0.5, 0.5, 1.0] * (64 * 64)
        image.filepath_raw = str(Path(directory) / "reference.png")
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)
    result = dispatch("reference.crop_grid", {
        "reference_path": str(Path(directory) / "reference.png"),
        "center": [31, 29], "radius": 8, "zoom": 4, "grid_spacing": 5,
        "output_path": str(Path(directory) / "crop.png")})
    assert result["ok"], result
    data = result["data"]
    assert data["crop_origin"] == [23, 21], data
    assert data["output_size"] == [68, 68], data
    assert 25 in data["grid_x_pixels"] and 25 in data["grid_y_pixels"], data
    assert Path(data["path"]).is_file(), data

print("reference crop headless passed")
