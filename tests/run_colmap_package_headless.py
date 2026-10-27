"""Run camera handoff pixel checks in a clean background Blender process.

Example:
  blender --background --python-exit-code 1 --python tests/run_colmap_package_headless.py -- \
    --package-a /path/to/package-a \
    --package-b /path/to/package-b
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
import tempfile

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
# A background Blender may auto-load an installed copy of the add-on first.
# Point its package search path at this checkout for the code under test.
import blenderboo
import blenderboo.tools
blenderboo.__path__.insert(0, str(ROOT / "addon" / "blenderboo"))
blenderboo.tools.__path__.insert(
    0, str(ROOT / "addon" / "blenderboo" / "tools"))
from blenderboo.errors import NameConflictError

from blenderboo.tools.colmap_import import (
    import_colmap_package, parse_camera_package,
)


def _project(scene, camera, world_point, width, height):
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
    uv = world_to_camera_view(scene, camera, world_point)
    return uv.x * width, (1.0 - uv.y) * height


def _check_three_points(scene, camera, source, world_mapping=None):
    """Project COLMAP camera-space points through the actual Blender camera."""
    world = (world_mapping or Matrix.Identity(4)) @ Matrix(source["matrix_world"])
    width, height = int(source["width"]), int(source["height"])
    for x, y, z in ((0.0, 0.0, 5.0), (0.3, -0.15, 5.0), (-0.5, 0.2, 6.0)):
        point = world @ Vector((x, -y, -z))
        actual = _project(scene, camera, point, width, height)
        expected = (source["fx"] * x / z + source["cx"],
                    source["fy"] * y / z + source["cy"])
        delta = math.dist(actual, expected)
        assert delta < 0.1, (source["name"], camera.name, actual, expected, delta)


def _write_synthetic_model(folder, model):
    folder.mkdir(parents=True, exist_ok=True)
    params = ("2751.875833 2751.875833 1291.854682 489.236294"
              if model == "PINHOLE" else "2751.875833 1291.854682 489.236294")
    (folder / "cameras.txt").write_text(
        f"1 {model} 2560 1070 {params}\n",
        encoding="utf-8",
    )
    (folder / "images.txt").write_text(
        "1 1 0 0 0 2 3 4 1 synthetic_frame.png\n\n",
        encoding="utf-8",
    )


def _check_transform_and_points(package):
    """A scene similarity must move both cameras and optional reference points."""
    (package / "points3D.txt").write_text(
        "1 1 2 3 255 0 0 0.1 1 0\n2 -1 0 4 0 255 0 0.2 1 1\n",
        encoding="utf-8",
    )
    mapping_object = bpy.data.objects.new("Test COLMAP scene mapping", None)
    bpy.context.scene.collection.objects.link(mapping_object)
    mapping = Matrix.Translation(Vector((10, -3, 4))) @ Matrix.Diagonal((2, 2, 2, 1))
    mapping_object.matrix_world = mapping
    params = {"package_dir": str(package), "kind": "reproduction",
              "collection_name": "Test COLMAP transformed",
              "transform_object": mapping_object.name, "import_points": True}
    preview = import_colmap_package(params).data
    assert preview["dry_run"] and preview["point_count"] == 2
    receipt = import_colmap_package({**params, "dry_run": False}).data
    assert receipt["changed"] and receipt["point_count"] == 2
    collection = bpy.data.collections[receipt["collection_name"]]
    cameras = [obj for obj in collection.objects if obj.type == "CAMERA"]
    point_objects = [obj for obj in collection.objects if obj.type == "MESH"]
    assert len(cameras) == 1 and len(point_objects) == 1
    assert len(point_objects[0].data.vertices) == 2
    row = parse_camera_package(str(package), kind="reproduction")["cameras"][0]
    expected = mapping @ Matrix(row["matrix_world"])
    assert max(abs(cameras[0].matrix_world[i][j] - expected[i][j])
               for i in range(4) for j in range(4)) < 1e-5
    assert max(abs(point_objects[0].matrix_world[i][j] - mapping[i][j])
               for i in range(4) for j in range(4)) < 1e-5
    _check_three_points(bpy.context.scene, cameras[0], row, mapping)


def _collection_camera_map(collection, source_rows):
    """Match by source frame and verify no hidden alignment was applied."""
    objects = [obj for obj in collection.objects if obj.type == "CAMERA"]
    assert len(objects) == len(source_rows), (collection.name, len(objects), len(source_rows))
    by_frame = {obj["cbt_source_frame"]: obj for obj in objects}
    assert len(by_frame) == len(objects), "source frames are not unique in collection"
    matched = {}
    for source in source_rows:
        obj = by_frame[source["name"]]
        expected_world = Matrix(source["matrix_world"])
        max_pose_error = max(abs(obj.matrix_world[i][j] - expected_world[i][j])
                             for i in range(4) for j in range(4))
        assert max_pose_error < 1e-5, (source["name"], max_pose_error)
        matched[source["name"]] = obj
    return matched


def _import_and_check(package, collection_name, expected_kind, expected_count,
                      images_dir=None):
    parsed = parse_camera_package(str(package), kind=expected_kind)
    rows = parsed["cameras"]
    assert len(rows) == expected_count, (package, len(rows), expected_count)
    assert {row["kind"] for row in rows} == {expected_kind}
    params = {"package_dir": str(package), "dry_run": True, "kind": expected_kind}
    if collection_name is not None:
        params["collection_name"] = collection_name
    if images_dir is not None:
        params["images_dir"] = str(images_dir)
    preview = import_colmap_package(params).data
    assert preview["camera_count"] == expected_count, preview
    assert not preview["changed"]
    if images_dir is not None:
        expected_missing = sum(not (images_dir / row["name"]).is_file() for row in rows)
        assert preview["missing_image_count"] == expected_missing, preview
    params["dry_run"] = False
    imported = import_colmap_package(params).data
    assert imported["camera_count"] == expected_count, imported
    camera_count = len(bpy.data.cameras)
    try:
        import_colmap_package(params)
        raise AssertionError("repeated camera package import was accepted")
    except NameConflictError:
        assert len(bpy.data.cameras) == camera_count, "conflict mutated camera data"
    assert imported["collection_name"] == preview["collection_name"]
    collection = bpy.data.collections[imported["collection_name"]]
    matched = _collection_camera_map(collection, rows)
    for row in rows:
        obj = matched[row["name"]]
        _check_three_points(bpy.context.scene, obj, row)
        assert obj["cbt_camera_kind"] == expected_kind
        assert obj["cbt_camera_run"] == row["source_run"]
        assert math.dist(tuple(obj["cbt_principal_px"]), (row["cx"], row["cy"])) < 1e-5
        if "iou" in row["metrics"]:
            assert abs(obj["cbt_iou"] - row["metrics"]["iou"]) < 1e-8
    return matched, collection.name


def main():
    cli = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-a", type=Path)
    parser.add_argument("--package-b", type=Path)
    parser.add_argument("--images-dir", type=Path)
    args = parser.parse_args(cli)

    with tempfile.TemporaryDirectory() as temp:
        for model in ("PINHOLE", "SIMPLE_PINHOLE"):
            synthetic = Path(temp) / model.lower()
            _write_synthetic_model(synthetic, model)
            _import_and_check(synthetic, f"Test COLMAP {model}", "reproduction", 1,
                              images_dir=synthetic)
        mapped = Path(temp) / "mapped"
        _write_synthetic_model(mapped, "PINHOLE")
        _check_transform_and_points(mapped)
    print("PASS synthetic PINHOLE + SIMPLE_PINHOLE nonzero-principal 2560x1070: "
          "3 projections each <0.1 px; transform + point cloud aligned")

    if args.package_a is None or args.package_b is None:
        print("SKIP optional M4A4 packages: supply --package-a and --package-b")
        return
    package_a, package_b = args.package_a, args.package_b
    if not (package_a / "cameras.json").is_file() or not (package_b / "cameras.json").is_file():
        print("SKIP optional M4A4 packages: both JSON exports are required")
        return
    first, first_collection = _import_and_check(
        package_a, None, "reproduction", 404, images_dir=args.images_dir)
    second, second_collection = _import_and_check(
        package_b, None, "constrained", 404, images_dir=args.images_dir)
    assert first_collection != second_collection
    assert len(first) + len(second) == 808
    assert len({obj.name for obj in (*first.values(), *second.values())}) == 808
    print("PASS M4A4 reproduction + constrained: 808 distinct cameras; "
          "3 projections each <0.1 px")


if __name__ == "__main__":
    main()
