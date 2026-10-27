"""Synthetic contour-fit integration. Run after goal_deform is integrated.

blender --background --factory-startup --python tests/run_contour_fit_headless.py
No production files are loaded; references live in a temporary directory.
"""
from pathlib import Path
import sys
import tempfile

import bpy
import numpy as np
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.contour_fit import fit_contours, _read_view, _measure
from blenderboo.tools.goal_deform import read_context
from blenderboo.tools.reference_review import _save_rgba


def coords(obj):
    values = np.empty((len(obj.data.vertices), 3), np.float32)
    obj.data.vertices.foreach_get("co", values.ravel())
    return values


def state():
    return {"objects": tuple(bpy.data.objects.keys()), "images": tuple(bpy.data.images.keys()),
            "meshes": tuple(bpy.data.meshes.keys()), "materials": tuple(bpy.data.materials.keys()),
            "cameras": [(o.name, tuple(v for row in o.matrix_world for v in row), o.data.ortho_scale)
                        for o in bpy.data.objects if o.type == "CAMERA"],
            "selection": tuple(o.name for o in bpy.context.selected_objects),
            "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
            "render_camera": bpy.context.scene.camera, "frame": bpy.context.scene.frame_current}


def refused(params, expected):
    before, scene_before = coords(obj), state()
    try:
        fit_contours(params)
    except (InvalidParamsError, InvalidStateError) as exc:
        assert expected.lower() in str(exc).lower(), str(exc)
    else:
        raise AssertionError(f"Expected refusal: {expected}")
    assert np.array_equal(coords(obj), before)
    assert state() == scene_before


bpy.ops.wm.read_factory_settings(use_empty=True)
vertices = [(x, y, z) for z in (-1., 0., 1.) for x, y in ((-1., -1.), (1., -1.), (1., 1.), (-1., 1.))]
vertices += [(0., 0., 1.), (0., 0., -1.)]
faces = []
for k in (0, 4):
    for i in range(4):
        j = (i + 1) % 4
        faces.append((k + i, k + j, k + j + 4, k + i + 4))
for i in range(4):
    j = (i + 1) % 4
    faces.extend(((8 + i, 8 + j, 12), (j, i, 13)))
mesh = bpy.data.meshes.new("ContourFixtureMesh")
mesh.from_pydata(vertices, [], faces)
mesh.update()
obj = bpy.data.objects.new("ContourFixture", mesh)
bpy.context.scene.collection.objects.link(obj)
group = obj.vertex_groups.new(name="Region")
group.add(list(range(4, 13)), 1., "REPLACE")
pins = obj.vertex_groups.new(name="Pins")
pins.add([12], 1., "REPLACE")
for name, location in (("Front", (0, -5, 0)), ("Side", (5, 0, 0))):
    data = bpy.data.cameras.new(name)
    camera = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(camera)
    data.type, data.ortho_scale = "ORTHO", 4
    camera.location = location
    camera.rotation_euler = (-Vector(location)).to_track_quat("-Z", "Y").to_euler()
bpy.context.view_layer.update()

views = [{"camera_name": "Front", "width": 128, "height": 128,
          "contour": [[25.6, 32], [102.4, 32], [96, 96], [32, 96]]},
         {"camera_name": "Side", "width": 128, "height": 128,
          "contour": [[28.8, 32], [99.2, 32], [96, 96], [32, 96]]}]
params = {"object_name": obj.name, "region": {"vertex_group": "Region"}, "pinned_vertex_group": "Pins",
          "max_displacement_mm": 300, "symmetry": {"normal": [1, 0, 0], "offset_m": 0., "pair_tolerance_mm": .01},
          "views": views}
original, original_scene = coords(obj), state()
planned = fit_contours(params).data
assert not planned["changed"]
assert np.array_equal(coords(obj), original)
assert state() == original_scene
for view in planned["details"]["views"]:
    assert view["after"]["contour_distance_px"]["mean"] < view["before"]["contour_distance_px"]["mean"] * .25, view
    assert view["after"]["iou"] > view["before"]["iou"], view
print("PASS dry-run two-view error reduction and no hidden state mutation")

refused({**params, "max_displacement_mm": 5, "dry_run": False, "confirm": True}, "displacement")
bad_views = [dict(view) for view in views]
bad_views[1]["camera_name"] = "Front"
refused({**params, "views": bad_views}, "perpendicular")
bad_views = [dict(view) for view in views]
bad_views[0]["contour"] = [[25.6, 20], [102.4, 20], [96, 96], [32, 96]]
refused({**params, "views": bad_views}, "vertical extents")
bad_views[0]["contour"] = [[24, 32], [104, 96], [24, 96], [104, 32]]
refused({**params, "views": bad_views}, "self-intersections")
print("PASS bound, projection, incompatible height and crossing-contour refusals are atomic")

obstacle_mesh = bpy.data.meshes.new("ClearancePlaneMesh")
obstacle_mesh.from_pydata([(1.15, -3, -3), (1.15, 3, -3), (1.15, 3, 3), (1.15, -3, 3)], [], [(0, 1, 2, 3)])
obstacle_mesh.update()
obstacle = bpy.data.objects.new("ClearancePlane", obstacle_mesh)
bpy.context.scene.collection.objects.link(obstacle)
obstacle_before = coords(obstacle)
refused({**params, "clearance": {"object_name": obstacle.name, "min_mm": 100}, "dry_run": False, "confirm": True}, "clearance")
assert np.array_equal(coords(obstacle), obstacle_before)
bpy.data.objects.remove(obstacle, do_unlink=True)
bpy.data.meshes.remove(obstacle_mesh)
assert state() == original_scene
print("PASS clearance conflict refuses without changing source or target")

with tempfile.TemporaryDirectory(prefix="blenderboo_contours_") as directory:
    mask_views = []
    for index, raw in enumerate(views):
        view = _read_view(bpy, raw, index)
        rgba = np.ones((128, 128, 4), dtype=np.float32)
        rgba[:, :, :3] = view["mask"][::-1, :, None]
        path = Path(directory) / f"view{index}.png"
        _save_rgba(bpy, rgba, path, f"MaskFixture{index}")
        mask_views.append({"camera_name": raw["camera_name"], "mask_path": str(path)})
    mask_plan = fit_contours({**params, "views": mask_views}).data
    assert all(v["after"]["iou"] > v["before"]["iou"] for v in mask_plan["details"]["views"])
    assert state() == original_scene
    assert np.array_equal(coords(obj), original)
print("PASS image masks preserve image datablocks and mesh in dry run")

applied = fit_contours({**params, "dry_run": False, "confirm": True}).data
actual = coords(obj)
assert applied["changed"]
assert np.array_equal(actual[[0, 1, 2, 3, 12, 13]], original[[0, 1, 2, 3, 12, 13]])
assert np.linalg.norm(actual - original, axis=1).max() <= .3
# The cameras themselves have float32 rotation noise, so their shared vertical
# differs from exact world Z by approximately 1.6e-7 radians.
assert np.max(abs(actual[:, 2] - original[:, 2])) < 1e-7
assert state() == original_scene
context = read_context(params)
for index, view_receipt in enumerate(applied["details"]["views"]):
    actual_metrics = _measure(context["world"] / context["unit_scale"], context["triangles"], _read_view(bpy, views[index], index))
    assert actual_metrics == view_receipt["after"], (actual_metrics, view_receipt)
for point in actual:
    reflected = point * np.array([-1, 1, 1])
    assert np.linalg.norm(actual - reflected, axis=1).min() < 1e-5
print("PASS applied bounds, exact pins/unselected vertices, symmetry and measured final silhouette receipts")

# Non-unit scene scaling: the same 0.2236 scene-unit displacement is 2.236 mm.
obj.data.vertices.foreach_set("co", original.ravel())
obj.data.update()
bpy.context.scene.unit_settings.scale_length = .01
metric_plan = fit_contours({**params, "max_displacement_mm": 3}).data
assert all(v["after"]["iou"] > v["before"]["iou"] for v in metric_plan["details"]["views"])
refused({**params, "max_displacement_mm": 1}, "displacement")
print("PASS scene scale is converted to world metres for bounded fitting")
print("CONTOUR_FIT_TESTS_PASSED")
