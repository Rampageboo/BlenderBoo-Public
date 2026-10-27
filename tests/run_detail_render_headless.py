"""Blender 5.2 background qualification for detail review outputs."""

from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for name in list(sys.modules):
    if name == "blenderboo" or name.startswith("blenderboo."):
        del sys.modules[name]

from blenderboo.tools.render import (
    render_contact_sheet, render_detail, render_ortho_set, render_reference_match,
)


def dimensions(path):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        return tuple(image.size)
    finally:
        bpy.data.images.remove(image)


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.render.engine = "BLENDER_WORKBENCH"
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.object
cube.name = "DetailCube"
bpy.ops.mesh.primitive_cube_add(location=(3, 0, 0))
other = bpy.context.object
other.name = "OtherCube"
initial_camera = bpy.context.scene.camera
initial_visibility = {obj.name: obj.hide_render for obj in bpy.context.scene.objects}

with tempfile.TemporaryDirectory(prefix="cbt_detail_render_") as temp:
    root = Path(temp)
    detail = render_detail({"object_name": cube.name, "direction": "front", "projection": "orthographic",
                            "isolate": True, "width": 96, "height": 64,
                            "output_path": str(root / "detail.png")}).data
    assert dimensions(detail["path"]) == (96, 64)
    assert detail["camera"]["projection"] == "orthographic"
    assert detail["camera"]["target_center"] == [0.0, 0.0, 0.0]
    print("PASS render.detail object close-up")

    point = render_detail({"point": [1, 2, 3], "radius": 0.25,
                           "direction": [1, -1, 1], "width": 64, "height": 64,
                           "output_path": str(root / "point.png")}).data
    assert point["camera"]["target_center"] == [1.0, 2.0, 3.0]
    print("PASS render.detail point radius")

    bbox = render_detail({"bounds": {"min": [-1, -2, -3], "max": [1, 2, 3]},
                          "width": 64, "height": 64,
                          "output_path": str(root / "bounds.png")}).data
    assert bbox["camera"]["target_center"] == [0.0, 0.0, 0.0]
    print("PASS render.detail world bounds")

    views = render_ortho_set({"object_names": [cube.name, other.name], "shared_scale": True,
                              "width": 64, "height": 64,
                              "output_prefix": str(root / "ortho")}).data
    assert set(views["views"]) == {"front", "back", "left", "right", "top", "bottom"}
    scales = {entry["camera_params"]["ortho_scale"] for entry in views["manifest"].values()}
    assert len(scales) == 1
    assert all(dimensions(entry["path"]) == (64, 64) for entry in views["manifest"].values())
    print("PASS render.ortho_set six views and shared scale")

    reference = render_reference_match({
        "reference_path": detail["path"], "camera": {
            "location": detail["camera"]["location"],
            "rotation_euler": detail["camera"]["rotation_euler"],
            "projection": "orthographic", "ortho_scale": detail["camera"]["ortho_scale"],
        },
        "output_prefix": str(root / "matched"), "opacity": 0.4,
    }).data
    assert dimensions(reference["render_path"]) == (96, 64)
    assert dimensions(reference["side_by_side_path"]) == (192, 64)
    assert dimensions(reference["overlay_path"]) == (96, 64)
    print("PASS render.reference_match render, side sheet, overlay")

    sheet = render_contact_sheet({
        "images": [{"path": detail["path"], "label": "FRONT"},
                   {"path": point["path"], "label": "POINT"}],
        "columns": 2, "output_path": str(root / "contact.png"),
    }).data
    assert dimensions(sheet["path"]) == (192, 82)
    print("PASS render.contact_sheet dimensions and labels")

assert bpy.context.scene.camera is initial_camera
assert {obj.name: obj.hide_render for obj in bpy.context.scene.objects} == initial_visibility
assert all(not obj.name.startswith(("CodexDetailCamera", "CodexReferenceCamera"))
           for obj in bpy.data.objects)
print("PASS render detail state restored")
