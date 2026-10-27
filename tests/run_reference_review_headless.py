"""Blender 5.2 headless camera calibration and contour regression fixtures."""

from pathlib import Path
import sys
import tempfile

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo.errors import InvalidParamsError, StaleStateError
from blenderboo.tools.reference_review import calibrate_from_reference, contour_compare


def write_image(path, width, height, white_box=None):
    image = bpy.data.images.new("ReferenceFixture", width=width, height=height, alpha=True)
    try:
        values = [0.0] * (width * height * 4)
        for y in range(height):
            for x in range(width):
                index = (y * width + x) * 4
                bright = white_box is not None and white_box[0] <= x < white_box[2] and white_box[1] <= y < white_box[3]
                values[index:index + 4] = [float(bright), float(bright), float(bright), 1.0]
        image.pixels.foreach_set(values)
        image.update()
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def project(camera, point, width, height):
    local = camera.matrix_world.inverted() @ Vector(point)
    if camera.data.type == "PERSP":
        scale = camera.data.lens * width / camera.data.sensor_width
        return [width / 2 + scale * local.x / -local.z,
                height / 2 - scale * local.y / -local.z]
    scale = width / camera.data.ortho_scale
    return [width / 2 + scale * local.x, height / 2 - scale * local.y]


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.resolution_x = 128
scene.render.resolution_y = 96
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.object
cube.name = "CalibrationCube"
camera_data = bpy.data.cameras.new("KnownCamera")
known = bpy.data.objects.new("KnownCamera", camera_data)
scene.collection.objects.link(known)
known.location = (4, -6, 3)
known.rotation_euler = (Vector((0, 0, 0)) - known.location).to_track_quat("-Z", "Y").to_euler()
camera_data.sensor_fit = "HORIZONTAL"
camera_data.sensor_width = 36
camera_data.lens = 40
bpy.context.view_layer.update()
points = [(-1, -1, -1), (1, -1, -1), (-1, 1, -1), (1, 1, -1),
          (-1, -1, 1), (1, -1, 1), (-1, 1, 1), (1, 1, 1)]

with tempfile.TemporaryDirectory(prefix="cbt_reference_review_") as directory:
    root = Path(directory)
    reference = root / "blank.png"
    write_image(reference, 128, 96)
    correspondences = [{"pixel": project(known, point, 128, 96), "world": list(point)}
                       for point in points]
    exact = calibrate_from_reference({"reference_path": str(reference),
                                      "correspondences": correspondences,
                                      "camera_name": "SolvedPerspective",
                                      "output_path": str(root / "perspective_overlay.png")}).data
    assert exact["rms_px"] < 0.1, exact["rms_px"]
    assert (bpy.data.objects["SolvedPerspective"].location - known.location).length < 0.2
    assert Path(exact["overlay_path"]).is_file()
    assert exact["changed"] and isinstance(exact["state_token"], str)
    print("PASS perspective exact calibration and overlay")

    noisy = [{"pixel": [item["pixel"][0] + (index % 3 - 1) * 0.3,
                          item["pixel"][1] + (index % 2 - 0.5) * 0.4],
              "world": item["world"]} for index, item in enumerate(correspondences)]
    noisy_result = calibrate_from_reference({"reference_path": str(reference),
                                              "correspondences": noisy,
                                              "camera_name": "SolvedNoisy",
                                              "expected_state": exact["state_token"],
                                              "output_path": str(root / "noisy_overlay.png")}).data
    assert noisy_result["rms_px"] < 1.0, noisy_result["rms_px"]
    assert len(noisy_result["per_point"]) == 8
    print("PASS perspective noisy calibration residuals")

    try:
        calibrate_from_reference({"reference_path": str(reference),
                                  "correspondences": correspondences,
                                  "camera_name": "StaleCalibration",
                                  "expected_state": exact["state_token"],
                                  "output_path": str(root / "stale_overlay.png")})
    except StaleStateError:
        pass
    else:
        raise AssertionError("Stale calibration token was accepted")
    print("PASS calibration expected-state gate")

    try:
        calibrate_from_reference({"reference_path": str(reference),
                                  "correspondences": correspondences,
                                  "camera_name": "OverwriteReference",
                                  "output_path": str(reference), "overwrite": True})
    except InvalidParamsError:
        pass
    else:
        raise AssertionError("Calibration overwrote its reference input")
    print("PASS calibration input/output path separation")

    bad = [dict(item) for item in correspondences]
    bad[0] = {"pixel": [correspondences[0]["pixel"][0] + 12,
                         correspondences[0]["pixel"][1] + 8],
              "world": correspondences[0]["world"]}
    high_error = calibrate_from_reference({"reference_path": str(reference),
                                           "correspondences": bad,
                                           "camera_name": "SolvedHighError",
                                           "high_error_fraction": 0.001,
                                           "output_path": str(root / "high_error_overlay.png")})
    assert high_error.data["high_error"] and high_error.warnings
    print("PASS high-error calibration remains explicit")

    fixed = calibrate_from_reference({"reference_path": str(reference),
                                      "correspondences": correspondences[:3] + correspondences[4:5],
                                      "focal_length_mm": 40,
                                      "camera_name": "SolvedFixed",
                                      "output_path": str(root / "fixed_overlay.png")}).data
    assert fixed["rms_px"] < 0.5, fixed["rms_px"]
    print("PASS fixed-focal calibration from four noncoplanar points")

    camera_data.type = "ORTHO"
    camera_data.ortho_scale = 4.0
    ortho_points = [{"pixel": project(known, point, 128, 96), "world": list(point)}
                    for point in points]
    ortho = calibrate_from_reference({"reference_path": str(reference),
                                      "correspondences": ortho_points,
                                      "model": "orthographic", "camera_name": "SolvedOrtho",
                                      "output_path": str(root / "ortho_overlay.png")}).data
    assert ortho["rms_px"] < 0.1, ortho["rms_px"]
    assert abs(bpy.data.objects["SolvedOrtho"].data.ortho_scale - 4.0) < 0.1
    print("PASS orthographic calibration")

    try:
        calibrate_from_reference({"reference_path": str(reference),
                                  "correspondences": correspondences[:4],
                                  "model": "orthographic", "camera_name": "Degenerate",
                                  "output_path": str(root / "degenerate.png")})
    except InvalidParamsError:
        pass
    else:
        raise AssertionError("Coplanar correspondences were accepted")
    print("PASS calibration degeneracy rejection")

    # An analytically drawn square is an independent reference for the CPU
    # projection of a front-facing cube.  The comparison must worsen after a
    # known horizontal displacement.
    known.location = (0, -10, 0)
    known.rotation_euler = (Vector((0, 0, 0)) - known.location).to_track_quat("-Z", "Y").to_euler()
    camera_data.ortho_scale = 4
    bpy.context.view_layer.update()
    silhouette = root / "square_mask.png"
    write_image(silhouette, 128, 96, (32, 16, 96, 80))
    baseline = contour_compare({"reference_path": str(silhouette), "mask_path": str(silhouette),
                                "camera_name": known.name, "object_names": [cube.name],
                                "output_path": str(root / "baseline_deviation.png")}).data
    assert baseline["iou"] > 0.9, baseline["iou"]
    assert baseline["contour_distance_px"]["mean"] < 3
    assert Path(baseline["deviation_path"]).is_file()
    print("PASS baseline contour IoU and pixel/model-unit statistics")

    cube.location.x = 0.5
    bpy.context.view_layer.update()
    shifted = contour_compare({"reference_path": str(silhouette), "mask_path": str(silhouette),
                               "camera_name": known.name, "object_names": [cube.name],
                               "output_path": str(root / "shifted_deviation.png")}).data
    assert shifted["iou"] < baseline["iou"] - 0.1, (baseline["iou"], shifted["iou"])
    assert shifted["contour_distance_px"]["mean"] > baseline["contour_distance_px"]["mean"]
    assert shifted["regions"] and shifted["regions"][0]["focus"] is not None
    assert set(shifted["regions"][0]["focus"]) == {"point", "radius"}
    print("PASS known displacement worsens contour error and returns detail focus")
