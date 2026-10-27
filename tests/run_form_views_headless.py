"""Blender headless fixture for deterministic form-view renders and restoration."""

from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for name in list(sys.modules):
    if name == "blenderboo" or name.startswith("blenderboo."):
        del sys.modules[name]

from blenderboo.tools.render import render_contact_sheet, render_detail, render_ortho_set


def rgba(path):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        return tuple(image.size), list(image.pixels)
    finally:
        bpy.data.images.remove(image)


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
bpy.ops.mesh.primitive_cube_add(location=(-2, 0, 0))
cube = bpy.context.object
cube.name = "BevelledCube"
bevel = cube.modifiers.new("FixtureBevel", "BEVEL")
bevel.width = 0.15
bevel.segments = 3
cube.modifiers.new("WeightedNormals", "WEIGHTED_NORMAL")

bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1)
sphere = bpy.context.object
sphere.name = "FacetedSphere"
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, location=(2, 0, 0))
smooth = bpy.context.object
smooth.name = "SmoothSphere"
for face in smooth.data.polygons:
    face.use_smooth = True

# A recessed panel: broad face behind four raised edge strips.
bpy.ops.mesh.primitive_cube_add(location=(0, 3, -0.12), scale=(1.0, 1.0, 0.10))
panel = bpy.context.object
panel.name = "RecessedPanel"
for index, location, scale in (
    (0, (-1.0, 3, 0.08), (0.12, 1.0, 0.12)),
    (1, (1.0, 3, 0.08), (0.12, 1.0, 0.12)),
    (2, (0, 2.0, 0.08), (1.0, 0.12, 0.12)),
    (3, (0, 4.0, 0.08), (1.0, 0.12, 0.12)),
):
    bpy.ops.mesh.primitive_cube_add(location=location, scale=scale)
    bpy.context.object.name = f"PanelRim{index}"

baseline_engine = scene.render.engine
baseline_override = bpy.context.view_layer.material_override
baseline_camera = scene.camera
baseline_material_count = len(bpy.data.materials)

with tempfile.TemporaryDirectory(prefix="cbt_form_views_") as temp:
    output = Path(temp)
    paths = {}
    for mode in ("lit", "solid", "normal", "matcap", "curvature", "depth", "wireframe_on_shaded"):
        result = render_detail({
            "object_name": sphere.name, "isolate": True, "direction": "three_quarter",
            "projection": "orthographic", "width": 192, "height": 128,
            "view_mode": mode, "output_path": str(output / f"{mode}.png"),
        }).data
        size, pixels = rgba(result["path"])
        assert size == (192, 128), (mode, size)
        assert any(value > 0.05 for value in pixels[:3 * 192 * 128]), mode
        assert result["view_mode"] == mode
        if mode == "depth":
            depth = result["depth_range"]
            assert depth["far"] > depth["near"] >= 0
            assert depth["units"] == "Blender units"
        assert scene.render.engine == baseline_engine, mode
        assert bpy.context.view_layer.material_override is baseline_override, mode
        assert scene.camera is baseline_camera, mode
        assert len(bpy.data.materials) == baseline_material_count, mode
        paths[mode] = result["path"]
    print("PASS render.detail seven form modes and restored state")

    eevee = next((item.identifier for item in scene.render.bl_rna.properties["engine"].enum_items
                  if item.identifier.startswith("BLENDER_EEVEE")), None)
    assert eevee is not None, "Blender 5.2 Eevee renderer unavailable"
    scene.render.engine = eevee
    unlit = render_detail({"object_name": sphere.name, "isolate": True,
                           "view_mode": "lit", "width": 64, "height": 64,
                           "output_path": str(output / "unlit_warning.png")})
    assert any("No render-visible Light objects" in item for item in unlit.warnings), unlit.warnings
    assert scene.render.engine == eevee
    scene.render.engine = baseline_engine
    print("PASS lightless lit warning and Workbench solid fallback")

    flat = rgba(paths["normal"])[1]
    smooth_result = render_detail({
        "object_name": smooth.name, "isolate": True, "direction": "three_quarter",
        "projection": "orthographic", "width": 192, "height": 128,
        "view_mode": "normal", "output_path": str(output / "smooth_normal.png"),
    }).data
    smooth_pixels = rgba(smooth_result["path"])[1]
    assert sum(abs(a - b) for a, b in zip(flat, smooth_pixels)) > 100.0
    print("PASS normal view distinguishes faceted and smooth surfaces")

    views = render_ortho_set({
        "object_name": cube.name, "isolate": True, "views": ["front", "top"],
        "width": 192, "height": 128, "view_mode": "depth",
        "output_prefix": str(output / "ortho_depth"),
    }).data
    assert views["view_mode"] == "depth"
    assert all(entry["depth_range"]["far"] > entry["depth_range"]["near"]
               for entry in views["manifest"].values())
    print("PASS render.ortho_set depth range per view")

    sheet = render_contact_sheet({
        "images": [{"path": paths["matcap"]}, {"path": paths["curvature"]}],
        "view_mode": "matcap", "output_path": str(output / "sheet.png"),
    }).data
    assert sheet["view_mode"] == "matcap"
    assert rgba(sheet["path"])[0] == (384, 146)
    print("PASS contact sheet mode label")

assert scene.render.engine == baseline_engine
assert bpy.context.view_layer.material_override is baseline_override
assert scene.camera is baseline_camera
assert len(bpy.data.materials) == baseline_material_count
print("PASS form-view state fully restored")
