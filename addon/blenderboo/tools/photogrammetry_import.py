"""Conservative RealityScan camera import for an already aligned point-cloud mesh.

The candidate CSV pose convention is based on an empirical RealityScan export, not on known
2D-to-3D correspondences. Applying it therefore requires explicit confirmation.
"""

from __future__ import annotations

from collections import Counter
from math import isfinite, radians
from pathlib import Path

from ..errors import (
    ConfirmationRequiredError, InvalidParamsError, InvalidStateError,
    MutationApplyError, NameConflictError, NotFoundError,
)
from ..registry import ToolResult
from ._blender import get_bpy
from .photogrammetry_csv import parse_realityscan_csv
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


_ALLOWED = {
    "csv_path", "images_dir", "pointcloud_object", "collection_name", "mode",
    "dry_run", "confirm_unverified_orientation", "expected_state", "image_names",
}
_MODES = {"centered_zero", "csv_principal"}
_ORIENTATION = (
    "candidate RealityScan pose: R_blender = D @ Rz(yaw) @ Rx(pitch) @ "
    "Ry(-roll) @ D, D=diag(1,-1,-1); no known-point reprojection validation"
)


def _name(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InvalidParamsError(f"{field} must be a non-empty name")
    return value


def _bool(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{field} must be boolean")
    return value


def _similarity(obj):
    """Return the positive uniform scale and proper rotation of the cloud."""
    from mathutils import Matrix

    transform = obj.matrix_world.copy()
    if any(not isfinite(float(v)) for row in transform for v in row):
        raise InvalidStateError("pointcloud_object has a non-finite world transform")
    if any(abs(float(transform[3][j]) - (1.0 if j == 3 else 0.0)) > 1e-7
           for j in range(4)):
        raise InvalidStateError("pointcloud_object transform is not affine")
    basis = transform.to_3x3()
    columns = [basis.col[i].copy() for i in range(3)]
    scales = [column.length for column in columns]
    scale = sum(scales) / 3.0
    if scale <= 1e-9 or any(abs(item / scale - 1.0) > 1e-5 for item in scales):
        raise InvalidStateError("pointcloud_object requires a nonzero uniform scale")
    if any(abs(columns[i].dot(columns[j])) / (scale * scale) > 1e-5
           for i in range(3) for j in range(i + 1, 3)):
        raise InvalidStateError("pointcloud_object transform contains shear")
    rotation = Matrix(tuple(tuple(float(basis[i][j]) / scale for j in range(3))
                            for i in range(3)))
    if abs(rotation.determinant() - 1.0) > 1e-4:
        raise InvalidStateError("pointcloud_object requires a proper (non-reflected) similarity")
    return transform, rotation, scale


def _blender_pose(row: dict, cloud_transform, cloud_rotation):
    from mathutils import Matrix, Vector

    d = Matrix(((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0)))
    rz = Matrix.Rotation(radians(row["yaw"]), 3, "Z")
    rx = Matrix.Rotation(radians(row["pitch"]), 3, "X")
    ry = Matrix.Rotation(radians(-row["roll"]), 3, "Y")
    rotation = cloud_rotation @ d @ rz @ rx @ ry @ d
    center = cloud_transform @ Vector((row["x"], row["y"], row["alt"]))
    world = rotation.to_4x4()
    world.translation = center
    return world


def _selected_rows(parsed: dict, requested) -> list[dict]:
    rows = parsed["cameras"]
    if requested is None:
        selected = rows
    else:
        if (not isinstance(requested, list) or not requested or
                any(not isinstance(name, str) or not name for name in requested)):
            raise InvalidParamsError("image_names must be a non-empty list of CSV image names")
        keys = [name.casefold() for name in requested]
        if len(keys) != len(set(keys)):
            raise InvalidParamsError("image_names contains duplicates")
        requested_keys = set(keys)
        known = {row["name"].casefold() for row in rows}
        missing = [name for name in requested if name.casefold() not in known]
        if missing:
            raise InvalidParamsError(f"image_names not found in CSV: {missing[:5]}")
        selected = [row for row in rows if row["name"].casefold() in requested_keys]
    no_image = [row["name"] for row in selected if row.get("image_size") is None]
    if no_image:
        raise InvalidParamsError(f"Matching PNG images are missing: {no_image[:5]}")
    return selected


def import_photogrammetry(params: dict) -> ToolResult:
    """Preview or import selected standard-GUI RealityScan CSV cameras.

    This creates only camera datablocks in a new collection. It never changes
    the active scene camera or render resolution, and reports no pixel residual
    because the CSV/point cloud contain no known image correspondences.
    """
    unknown = sorted(set(params) - _ALLOWED)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {unknown}")
    csv_path = params.get("csv_path")
    images_dir = params.get("images_dir")
    if not isinstance(csv_path, str) or not csv_path or not Path(csv_path).is_absolute():
        raise InvalidParamsError("csv_path must be an absolute CSV path")
    if not isinstance(images_dir, str) or not images_dir or not Path(images_dir).is_absolute():
        raise InvalidParamsError("images_dir must be an absolute directory")
    cloud_name = _name(params.get("pointcloud_object"), "pointcloud_object")
    collection_name = _name(params.get("collection_name", "RealityScan Cameras"),
                            "collection_name")
    mode = params.get("mode", "csv_principal")
    if mode not in _MODES:
        raise InvalidParamsError("mode must be centered_zero or csv_principal")
    dry_run = _bool(params.get("dry_run", True), "dry_run")
    confirmed = _bool(params.get("confirm_unverified_orientation", False),
                      "confirm_unverified_orientation")

    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    cloud = bpy.context.scene.objects.get(cloud_name)
    if cloud is None:
        raise NotFoundError(f"pointcloud_object not found in active scene: {cloud_name}")
    if cloud.type != "MESH" or not cloud.data or len(cloud.data.vertices) < 3:
        raise InvalidParamsError("pointcloud_object must be a mesh with at least three vertices")
    cloud_transform, cloud_rotation, scale = _similarity(cloud)
    parsed = parse_realityscan_csv(csv_path, images_dir)
    rows = _selected_rows(parsed, params.get("image_names"))
    nonzero_distortion = sum(any(abs(row[key]) > 1e-12 for key in
                                 ("k1", "k2", "k3", "k4", "t1", "t2")) for row in rows)
    camera_kind = ("diagnostic_mixed" if mode == "centered_zero" else
                   "diagnostic_distortion_ignored" if nonzero_distortion else "reproduction")
    selected_names = {row["name"] for row in rows}
    warning_counts = dict(Counter(item["code"] for item in parsed["warnings"]
                                  if item["name"] in selected_names))
    # Keep object identity stable when callers import a subset of the CSV.
    names = [f"RS_Camera_{row['source_line'] - 1:06d}" for row in rows]
    conflicts = [name for name in names if bpy.data.objects.get(name) or bpy.data.cameras.get(name)]
    if bpy.data.collections.get(collection_name):
        conflicts.append(collection_name)
    preview = []
    for name, row in zip(names[:5], rows[:5]):
        world = _blender_pose(row, cloud_transform, cloud_rotation)
        preview.append({
            "object_name": name, "image_name": row["name"],
            "image_size": row["image_size"], "focal_35mm": row["f_35mm"],
            "csv_center": [row[key] for key in ("x", "y", "alt")],
            "world_center": [float(v) for v in world.translation],
            "yaw_pitch_roll_degrees": [row[key] for key in ("yaw", "pitch", "roll")],
        })
    data = {
        "dry_run": dry_run, "changed": False, "format": "realityscan_standard_csv",
        "camera_kind": camera_kind,
        "csv_path": parsed["source_path"], "images_dir": str(Path(images_dir).resolve()),
        "pointcloud_object": cloud.name, "cloud_uniform_scale": scale,
        "collection_name": collection_name, "camera_count": len(rows),
        "mode": mode, "preview": preview, "warning_counts": warning_counts,
        "name_conflicts": conflicts[:10], "orientation": _ORIENTATION,
        "orientation_verified": False, "acceptance": "unverified_orientation",
        "reprojection_error_px": {
            "centered_zero": None, "csv_principal": None, "solved_distortion": None,
        },
        "reprojection_error_reason": (
            "No known 2D-to-3D correspondences are present; the CSV does not identify "
            "its solved distortion model"
        ),
        "distortion_applied": False, "render_settings_changed": False,
        "render_resolution_note": "Set output resolution to each camera's image_size when rendering an overlay",
        "state_token": current_state_token(bpy),
    }
    warnings = ["Camera orientation is an empirical candidate, without known-point pixel validation."]
    if mode == "centered_zero":
        warnings.append("Diagnostic only: centered_zero mixes solved RealityScan poses with an unsolved centered principal point and is expected to misalign images. Use csv_principal or a constrained COLMAP package for reference cameras.")
    else:
        warnings.append("CSV pose orientation remains provisional; camera.import_colmap_package is the verified handoff path.")
    if nonzero_distortion:
        warnings.append(f"{nonzero_distortion} selected cameras have nonzero raw distortion; Blender does not reproduce it, so this import is diagnostic.")
    if warning_counts.get("large_distortion"):
        warnings.append(f"{warning_counts['large_distortion']} selected cameras have large raw distortion coefficients; distortion was not applied.")
    if dry_run:
        return ToolResult(data=data, warnings=warnings)
    if conflicts:
        raise NameConflictError("Camera or collection names already exist", data)
    if not confirmed:
        raise ConfirmationRequiredError(
            "Import requires confirm_unverified_orientation=true after reviewing the preview", data)
    require_document_write_access(bpy)
    collection = None
    created_objects = []
    created_cameras = []
    try:
        collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(collection)
        for name, row in zip(names, rows):
            camera = bpy.data.cameras.new(name)
            created_cameras.append(camera)
            camera.type = "PERSP"
            camera.lens = row["f_35mm"]
            camera.sensor_fit = "HORIZONTAL"
            camera.sensor_width = 36.0
            width, height = row["image_size"]
            if mode == "csv_principal":
                camera.shift_x = -row["px_norm"]
                # RealityScan's GUI principal offsets are normalized by image width.
                camera.shift_y = row["py_norm"]
            else:
                camera.shift_x = camera.shift_y = 0.0
            obj = bpy.data.objects.new(name, camera)
            created_objects.append(obj)
            collection.objects.link(obj)
            obj.matrix_world = _blender_pose(row, cloud_transform, cloud_rotation)
            obj["cbt_photo_image"] = str(Path(images_dir) / row["name"])
            obj["cbt_photo_image_size"] = row["image_size"]
            obj["cbt_photo_csv"] = parsed["source_path"]
            obj["cbt_photo_source_line"] = row["source_line"]
            obj["cbt_photo_principal_mode"] = mode
            obj["cbt_camera_kind"] = data["camera_kind"]
            obj["cbt_camera_run"] = parsed["source_path"]
            obj["cbt_photo_orientation_verified"] = False
            obj["cbt_photo_raw_principal"] = [row["px_norm"], row["py_norm"]]
            obj["cbt_photo_raw_distortion"] = [row[key] for key in
                                                ("k1", "k2", "k3", "k4", "t1", "t2")]
    except Exception as exc:
        cleanup_errors = []
        for obj in reversed(created_objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove camera object {obj.name}: {cleanup_exc}")
        for camera in reversed(created_cameras):
            try:
                bpy.data.cameras.remove(camera, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove camera datablock {camera.name}: {cleanup_exc}")
        if collection is not None:
            try:
                bpy.data.collections.remove(collection, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove collection {collection.name}: {cleanup_exc}")
        if cleanup_errors:
            bump_epoch()
        data.update(changed=bool(cleanup_errors), rollback_errors=cleanup_errors,
                    state_token=current_state_token(bpy))
        raise MutationApplyError(f"camera.import_photogrammetry failed: {exc}", data) from exc
    bump_epoch()
    data.update(dry_run=False, changed=True, created_camera_count=len(created_objects),
                created_camera_names=[obj.name for obj in created_objects[:10]],
                state_token=current_state_token(bpy))
    return ToolResult(data=data, warnings=warnings)
