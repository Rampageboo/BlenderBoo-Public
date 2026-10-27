"""Headless Blender source-comparison regression fixture; direct handler contract.

Run: blender --background --factory-startup --python-exit-code 1 --python tests/run_asset_source_compare_headless.py
"""
import json
from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.asset_source_compare import compare_to_source


def cube(name, size, location=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=size, location=location)
    obj = bpy.context.object
    obj.name = name
    return obj


def snapshot():
    scene = bpy.context.scene
    return {
        "objects": [(obj.name, obj.data.name if obj.data else None,
                     tuple(tuple(row) for row in obj.matrix_world), obj.hide_get(), obj.hide_render,
                     obj.hide_viewport, obj.select_get(), obj.mode,
                     tuple(tuple(v.co) for v in obj.data.vertices) if obj.type == "MESH" else None,
                     tuple((m.name, m.type, m.show_viewport, m.show_render) for m in obj.modifiers))
                    for obj in bpy.data.objects],
        "images": tuple(image.name for image in bpy.data.images),
        "meshes": tuple(mesh.name for mesh in bpy.data.meshes),
        "cameras": tuple(cam.name for cam in bpy.data.cameras),
        "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
        "camera": scene.camera.name if scene.camera else None,
        "render": (scene.render.engine, scene.render.filepath, scene.render.resolution_x,
                   scene.render.resolution_y, scene.render.resolution_percentage, scene.render.film_transparent),
        "units": (scene.unit_settings.system, scene.unit_settings.scale_length),
        "filepath": bpy.data.filepath,
    }


def compare(low, high, **kwargs):
    bpy.context.view_layer.update()
    before = snapshot()
    result = compare_to_source({"low": low.name, "high": high.name, "resolution": 96,
                                "sample_count": 1000, **kwargs}).data
    assert snapshot() == before, "Comparison modified Blender document state"
    json.dumps(result, allow_nan=False)
    return result


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.unit_settings.system = "METRIC"
bpy.context.scene.unit_settings.scale_length = .01
low = cube("Low", 2)
high = cube("High", 2)
low.location = high.location = (3, -2, 1)
low.rotation_euler = high.rotation_euler = (.2, .3, .1)
low.scale = high.scale = (-1, 2, .5)
low.hide_render = True
bpy.ops.object.camera_add(location=(8, 8, 8))
bpy.context.scene.camera = bpy.context.object
with tempfile.TemporaryDirectory() as directory:
    result = compare(low, high, spherical=True, radial_theta_steps=24, radial_phi_steps=13,
                     output_dir=directory, thresholds={"iou_min": 1, "deviation_p99_mm": .001, "radial_max_mm": .001})
    assert result["pass"] is True, result
    assert result["iou_min"] == 1, result
    assert result["deviation"]["valid_samples"] == 1000
    assert result["deviation"]["coverage"] == 1
    assert result["radial"]["paired_hits"] == 24 * 13
    assert "delta_scene_units" not in result["radial"]
    assert len(result["artifacts"]) == 14
    for artifact in result["artifacts"]:
        path = Path(artifact["path"])
        assert path.exists()
        if artifact["kind"] == "radial_delta":
            assert len(json.loads(path.read_text(encoding="utf-8"))["delta_scene_units"]) == 13
        else:
            assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    before = snapshot()
    try:
        compare_to_source({"low": low.name, "high": high.name, "output_dir": directory})
        raise AssertionError("Non-empty output directory accepted")
    except InvalidParamsError:
        pass
    assert snapshot() == before
print("PASS transformed identical geometry, read-only Blender state, PNGs and no overwrite")

# Concentric cubes produce known center-ray outer radius differences and units.
low.rotation_euler = high.rotation_euler = (0, 0, 0)
low.location = high.location = (0, 0, 0)
low.scale, high.scale = (1, 1, 1), (2, 2, 2)
result = compare(low, high, spherical=True, include_radial_grid=True, radial_theta_steps=8, radial_phi_steps=3,
                 center=[0, 0, 0], thresholds={"iou_min": .9, "deviation_p99_mm": 9, "radial_max_mm": 9})
assert result["pass"] is False, result
assert abs(result["iou_min"] - .25) < .03, result
assert abs(result["deviation"]["scene_units"]["median"] - 1) < 1e-5, result
assert abs(result["deviation"]["millimetres"]["p99"] - 10) < 1e-4, result
assert abs(result["radial"]["delta_scene_units"][1][0] + 1) < 1e-5, result
assert result["radial"]["over_threshold_cells"] > 0, result
assert result["artifacts"] == []
assert len(result["failures"]) == 3, result
automatic = compare(low, high, spherical=True, radial_theta_steps=8, radial_phi_steps=3,
                    center=[0, 0, 0])
assert automatic["radial"]["region_threshold_method"] == "paired_abs_delta_p90", automatic
assert automatic["radial"]["region_threshold_mm"] > 0, automatic
print("PASS shape loss, scene-unit to millimetre conversion, outer-shell rays and threshold failures")

# Missing projected area is unavailable, even for identical edge-on geometry.
bpy.ops.mesh.primitive_plane_add(size=2)
plane = bpy.context.object
plane.name = "Plane"
result = compare(plane, plane, thresholds={"iou_min": 0})
assert result["views"]["front"]["status"] == "unavailable", result
assert result["iou_min"] is None and result["pass"] is False, result
result = compare(plane, plane, views=["top"])
assert result["iou_min"] == 1 and result["pass"] is None, result
print("PASS empty silhouette cannot pass and no thresholds imply no acceptance claim")

# Both-missed directions are incomplete coverage, even when the measured patch matches.
plane.location = (0, 0, 1)
result = compare(plane, plane, views=["top"], spherical=True, center=[0, 0, 0],
                 radial_theta_steps=16, radial_phi_steps=9, thresholds={"radial_max_mm": 100000})
assert result["radial"]["status"] == "partial", result
assert result["radial"]["both_missed"] > 0 and result["radial"]["paired_hits"] > 0, result
assert result["pass"] is False, result
print("PASS both-missed radial directions cannot produce acceptance")

# Offset meshes expose one-sided/missed radial rays, never a false radial pass.
low.location = (5, 0, 0)
result = compare(low, high, spherical=True, center=[0, 0, 0], radial_theta_steps=16, radial_phi_steps=9,
                 thresholds={"radial_max_mm": 100000})
assert result["radial"]["status"] == "partial", result
assert result["radial"]["high_only_hits"] > 0, result
assert result["pass"] is False, result
print("PASS one-sided radial hits are partial and fail radial acceptance")

# Evaluation includes enabled modifiers, without applying them or leaving meshes.
low.location = high.location = (0, 0, 0)
low.scale = high.scale = (1, 1, 1)
for obj in (low, high):
    bevel = obj.modifiers.new("CompareBevel", "BEVEL")
    bevel.width = .2
    bevel.segments = 2
result = compare(low, high, thresholds={"iou_min": 1, "deviation_p99_mm": .001})
assert result["pass"] is True and result["geometry"]["low_triangles"] > 12, result
print("PASS evaluated modifier surfaces and temporary mesh cleanup")

# Reject edit mode instead of reading a possibly stale mesh or switching mode.
bpy.context.view_layer.objects.active = low
for obj in bpy.context.selected_objects:
    obj.select_set(False)
low.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
try:
    try:
        compare_to_source({"low": low.name, "high": high.name})
        raise AssertionError("Edit mode accepted")
    except InvalidStateError:
        pass
    assert low.mode == "EDIT"
finally:
    bpy.ops.object.mode_set(mode="OBJECT")
print("PASS edit-mode rejection preserves mode")
print("ASSET_SOURCE_COMPARE_HEADLESS_OK")
