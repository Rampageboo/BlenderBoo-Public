"""Blender headless qualification: fixed detail framing and ortho output names."""
from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.errors import InvalidParamsError
from blenderboo.tools.render import (
    preflight_render_options, render_compare, render_detail, render_ortho_set,
)


def rejected(call, fragment):
    try:
        call()
    except InvalidParamsError as exc:
        assert fragment in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected InvalidParamsError containing {fragment}")


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
bpy.ops.mesh.primitive_cube_add(size=0.8, location=(-0.7, 0, 0))
low = bpy.context.object
low.name = "Low"
bpy.ops.mesh.primitive_cube_add(size=0.6, location=(0.7, 0, 0))
high = bpy.context.object
high.name = "High"
low.hide_render = True
low.hide_set(True)
collection = low.users_collection[0]
collection.hide_render = True
before = {obj.name: (obj.hide_render, obj.hide_get()) for obj in scene.objects}
before_collection = collection.hide_render
before_camera = scene.camera
bounds = {"min": [-1.5, -1, -1], "max": [1.5, 1, 1]}

with tempfile.TemporaryDirectory(prefix="blenderboo_render_usp_") as temp:
    root = Path(temp)
    common = {"bounds": bounds, "direction": "front", "projection": "orthographic",
              "isolate": True, "width": 96, "height": 64}
    options = {**common, "object_names": [low.name], "output_path": str(root / "low.png")}
    preflight_render_options("detail", options, bpy)
    assert {obj.name: (obj.hide_render, obj.hide_get()) for obj in scene.objects} == before
    low_result = render_detail(options).data
    high_result = render_detail({**common, "object_name": high.name,
                                 "output_path": str(root / "high.png")}).data
    assert low_result["camera"] == high_result["camera"], "subjects changed fixed framing"
    assert render_compare({"image_a": low_result["path"], "image_b": high_result["path"]})["changed_pixels"] > 0
    assert {obj.name: (obj.hide_render, obj.hide_get()) for obj in scene.objects} == before
    assert collection.hide_render == before_collection
    low.hide_set(False)
    low_visible = render_detail({**options, "output_path": str(root / "low_viewport_visible.png")}).data
    assert render_compare({"image_a": low_result["path"], "image_b": low_visible["path"]})["changed_pixels"] == 0
    low.hide_set(True)
    without_isolation = render_detail({**options, "isolate": False,
                                       "output_path": str(root / "named_no_isolation.png")}).data
    bounds_only = render_detail({**common, "isolate": False,
                                "output_path": str(root / "bounds_only.png")}).data
    assert render_compare({"image_a": without_isolation["path"],
                           "image_b": bounds_only["path"]})["changed_pixels"] == 0
    assert {obj.name: (obj.hide_render, obj.hide_get()) for obj in scene.objects} == before
    assert collection.hide_render == before_collection
    print("PASS fixed framing, isolation, viewport hiding, and visibility restoration")

    # Names accompanying bounds are validated even if isolation is disabled.
    rejected(lambda: preflight_render_options("detail", {**options, "isolate": False,
             "object_names": ["Missing"]}, bpy), "Object not found")
    for extra in ({"point": [0, 0, 0], "radius": 1}, {"object_name": high.name},
                  {"radius": 1}, {"object_names": []}, {"object_names": [low.name, low.name]}):
        rejected(lambda extra=extra: preflight_render_options("detail", {**options, **extra}, bpy),
                 "")
    rejected(lambda: preflight_render_options("ortho_set", {"bounds": bounds,
             "object_names": [low.name]}, bpy), "exactly one")
    print("PASS selector ambiguity and missing subject guards")

    ortho_options = {"object_name": low.name, "isolate": True, "views": ["front", "top"],
                     "width": 64, "height": 64, "output_dir": str(root), "output_prefix": "fixed.png"}
    preflight_render_options("ortho_set", ortho_options, bpy)
    ortho = render_ortho_set(ortho_options).data
    assert {v: Path(e["path"]).name for v, e in ortho["manifest"].items()} == {
        "front": "fixed_front.png", "top": "fixed_top.png"}
    signatures = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.glob("fixed_*.png")}
    rejected(lambda: render_ortho_set(ortho_options), "overwrite is false")
    rejected(lambda: preflight_render_options("ortho_set", ortho_options, bpy), "overwrite is false")
    assert signatures == {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.glob("fixed_*.png")}
    render_ortho_set({**ortho_options, "overwrite": True})
    assert {obj.name: (obj.hide_render, obj.hide_get()) for obj in scene.objects} == before
    assert collection.hide_render == before_collection
    assert scene.camera is before_camera
    assert all(not obj.name.startswith("CodexDetailCamera") for obj in scene.objects)
    print("PASS deterministic ortho filenames, overwrite guard, and state restoration")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
bpy.ops.mesh.primitive_cube_add(size=0.3, location=(-0.5, 0, 2.5))
target = bpy.context.object
target.name = "AntennaHead"
bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0.5, 0, 0))
lower = bpy.context.object
lower.name = "TripodBelow"
with tempfile.TemporaryDirectory(prefix="blenderboo_detail_clip_") as temp:
    root = Path(temp)
    options = {"bounds": {"min": [-1, -1, 2.2], "max": [1, 1, 2.8]},
               "direction": "top", "projection": "orthographic",
               "width": 128, "height": 128}
    uncut = render_detail({**options, "output_path": str(root / "uncut.png")}).data
    clipped = render_detail({**options, "clip_to_bounds": True,
                             "output_path": str(root / "clipped.png")}).data
    assert clipped["camera"]["clip_scope"] == "camera_depth"
    assert clipped["camera"]["clip_end"] < uncut["camera"]["clip_end"]
    assert render_compare({"image_a": uncut["path"], "image_b": clipped["path"]})["changed_pixels"] > 0
    lower.hide_render = True
    expected = render_detail({**options, "clip_to_bounds": True,
                              "output_path": str(root / "expected.png")}).data
    assert render_compare({"image_a": clipped["path"], "image_b": expected["path"]})["changed_pixels"] == 0
    assert scene.camera is None and all(not obj.name.startswith("CodexDetailCamera") for obj in scene.objects)
    rejected(lambda: preflight_render_options("detail", {"object_name": target.name,
             "clip_to_bounds": True, "output_path": str(root / "invalid.png")}, bpy),
             "requires explicit bounds")
    print("PASS camera-depth bounds clipping and scene restoration")
