"""Isolated RealityScan CSV camera import and principal-point projection smoke."""

from pathlib import Path
import struct
import sys
import tempfile
import zlib

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.errors import (
    ConfirmationRequiredError, InvalidParamsError, InvalidStateError, NameConflictError,
)
from blenderboo.tools.photogrammetry_csv import parse_realityscan_csv
from blenderboo.tools.photogrammetry_import import import_photogrammetry


def png(path: Path, width: int, height: int) -> None:
    body = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(body)) + b"IHDR" +
                     body + struct.pack(">I", zlib.crc32(b"IHDR" + body)))


bpy.ops.wm.read_factory_settings(use_empty=True)
mesh = bpy.data.meshes.new("PointCloud")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [])
mesh.update()
cloud = bpy.data.objects.new("PointCloud", mesh)
bpy.context.scene.collection.objects.link(cloud)
csv_path = ROOT / "tests" / "fixtures" / "photogrammetry" / "cameras.csv"
rows = parse_realityscan_csv(csv_path)["cameras"]
with tempfile.TemporaryDirectory(prefix="cbt_photo_import_") as temp:
    images = Path(temp)
    for row in rows:
        png(images / row["name"], 2560, 1070)
    params = {"csv_path": str(csv_path), "images_dir": str(images),
              "pointcloud_object": cloud.name, "collection_name": "Test RealityScan Cameras",
              "mode": "csv_principal"}
    before_cameras = len(bpy.data.cameras)
    preview = import_photogrammetry(params).data
    assert preview["dry_run"] and not preview["changed"] and preview["camera_count"] == 5, preview
    default_preview = import_photogrammetry({key: value for key, value in params.items()
                                             if key != "mode"}).data
    assert default_preview["mode"] == "csv_principal"
    assert default_preview["camera_kind"] == "diagnostic_distortion_ignored"
    header, first_row, *_ = csv_path.read_text(encoding="utf-8").splitlines()
    clean_fields = first_row.split(",")
    clean_fields[10:] = ["0"] * 6
    clean_csv = images / "zero_distortion.csv"
    clean_csv.write_text(header + "\n" + ",".join(clean_fields) + "\n", encoding="utf-8")
    no_distortion = import_photogrammetry({**params, "csv_path": str(clean_csv)}).data
    assert no_distortion["camera_kind"] == "reproduction"
    assert len(bpy.data.cameras) == before_cameras
    subset = import_photogrammetry({**params, "image_names": [rows[2]["name"]]}).data
    assert subset["camera_count"] == 1 and subset["preview"][0]["object_name"] == "RS_Camera_000003", subset
    cloud.scale = (1, 2, 1)
    bpy.context.view_layer.update()
    try:
        try:
            import_photogrammetry(params)
            raise AssertionError("non-uniform cloud scale was accepted")
        except InvalidStateError as exc:
            assert "uniform scale" in str(exc), exc
    finally:
        cloud.scale = (1, 1, 1)
        bpy.context.view_layer.update()
    missing_image = images / rows[4]["name"]
    missing_image.unlink()
    try:
        import_photogrammetry({**params, "image_names": [rows[4]["name"]]})
        raise AssertionError("missing source PNG was accepted")
    except InvalidParamsError as exc:
        assert "Matching PNG images are missing" in str(exc), exc
    png(missing_image, 2560, 1070)
    malformed_image = images / rows[3]["name"]
    malformed_image.write_bytes(b"bad PNG")
    try:
        import_photogrammetry({**params, "image_names": [rows[3]["name"]]})
        raise AssertionError("malformed source PNG was accepted")
    except InvalidParamsError as exc:
        assert "Invalid PNG" in str(exc), exc
    png(malformed_image, 2560, 1070)
    try:
        import_photogrammetry({**params, "dry_run": False})
        raise AssertionError("unverified orientation was applied without confirmation")
    except ConfirmationRequiredError:
        pass
    applied = import_photogrammetry({**params, "dry_run": False,
                                    "confirm_unverified_orientation": True}).data
    assert applied["created_camera_count"] == 5 and applied["changed"], applied
    try:
        import_photogrammetry({**params, "dry_run": False,
                               "confirm_unverified_orientation": True})
        raise AssertionError("second apply with camera names already present was accepted")
    except NameConflictError:
        assert len(bpy.data.cameras) == before_cameras + 5
    assert bpy.context.scene.camera is None
    camera = bpy.data.objects["RS_Camera_000001"]
    assert list(camera["cbt_photo_image_size"]) == [2560, 1070]
    assert camera["cbt_photo_principal_mode"] == "csv_principal"
    assert camera["cbt_camera_kind"] == "diagnostic_distortion_ignored"
    assert abs(camera.data.lens - rows[0]["f_35mm"]) < 1e-5
    bpy.context.scene.render.resolution_x = 2560
    bpy.context.scene.render.resolution_y = 1070
    bpy.context.view_layer.update()
    axis_point = camera.matrix_world @ Vector((0, 0, -1))
    uv = world_to_camera_view(bpy.context.scene, camera, axis_point)
    actual_x, actual_y = uv.x * 2560, (1.0 - uv.y) * 1070
    expected_x = 1280 + rows[0]["px_norm"] * 2560
    expected_y = 535 + rows[0]["py_norm"] * 2560
    assert abs(actual_x - expected_x) < 0.05, (actual_x, expected_x)
    assert abs(actual_y - expected_y) < 0.05, (actual_y, expected_y)
    print(f"PHOTO_IMPORT_OK cameras=5 axis_px=({actual_x:.3f},{actual_y:.3f}) "
          f"expected_px=({expected_x:.3f},{expected_y:.3f})")

    real_csv = Path("D:/M4A4/M4A4_CP7.csv")
    real_images = Path("D:/M4A4/Images")
    if real_csv.is_file() and real_images.is_dir():
        real_first = parse_realityscan_csv(real_csv)["cameras"][0]["name"]
        real = import_photogrammetry({"csv_path": str(real_csv),
                                     "images_dir": str(real_images),
                                     "pointcloud_object": cloud.name,
                                     "image_names": [real_first],
                                     "collection_name": "Real CSV Dry Run"}).data
        assert real["dry_run"] and real["camera_count"] == 1 and not real["changed"]
        print("PHOTO_REAL_DRY_RUN_OK cameras=1")
