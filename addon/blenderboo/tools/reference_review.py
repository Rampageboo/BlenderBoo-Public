"""Reference camera calibration and CPU silhouette comparison.

All Blender reads/writes run in the dispatcher on Blender's main thread.  The
silhouette rasterizer is CPU-only; it does not invoke render/GPU operators.
"""

from __future__ import annotations

from array import array
from datetime import datetime, timezone
from math import ceil, isfinite, log, sqrt
from pathlib import Path
import tempfile
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError
from ..registry import ToolResult
from ._blender import get_bpy
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


def _number(value: Any, name: str, *, minimum=None, maximum=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise InvalidParamsError(f"{name} must be a finite number")
    value = float(value)
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise InvalidParamsError(f"{name} is outside its allowed range")
    return value


def _vec(value: Any, size: int, name: str):
    import numpy as np

    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise InvalidParamsError(f"{name} must contain {size} numbers")
    return np.array([_number(item, f"{name}[{index}]") for index, item in enumerate(value)], dtype=float)


def _path(value: Any, name: str, *, exists=False, png=False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise InvalidParamsError(f"{name} must be a non-empty path")
    path = Path(value).expanduser().resolve()
    if exists and not path.is_file():
        raise InvalidParamsError(f"{name} does not exist: {path}")
    if png and path.suffix.lower() != ".png":
        raise InvalidParamsError(f"{name} must be a PNG path")
    return path


def _output_path(bpy, supplied: Any, label: str) -> Path:
    if supplied is not None:
        return _path(supplied, "output_path", png=True)
    root = Path(bpy.data.filepath).resolve().parent if bpy.data.filepath else Path(tempfile.gettempdir())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    return root / "previews" / f"{label}_{stamp}.png"


def _image(bpy, path: Path, name: str):
    try:
        image = bpy.data.images.load(str(path), check_existing=False)
    except Exception as exc:
        raise InvalidParamsError(f"{name} is not a readable image: {path}: {exc}") from exc
    if min(image.size) <= 0:
        bpy.data.images.remove(image)
        raise InvalidParamsError(f"{name} has invalid dimensions: {path}")
    return image


def _pixels(image):
    import numpy as np

    width, height = map(int, image.size)
    raw = array("f", [0]) * (width * height * 4)
    image.pixels.foreach_get(raw)
    return np.asarray(raw, dtype=np.float32).reshape(height, width, 4).copy()


def _save_rgba(bpy, pixels, path: Path, name: str) -> None:
    import numpy as np

    height, width = pixels.shape[:2]
    image = bpy.data.images.new(name, width=width, height=height, alpha=True)
    try:
        image.pixels.foreach_set(np.asarray(pixels, dtype=np.float32).reshape(-1))
        image.update()
        path.parent.mkdir(parents=True, exist_ok=True)
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
        if not path.is_file():
            raise InvalidStateError(f"Blender did not create image: {path}")
    finally:
        bpy.data.images.remove(image)


def _world_point(scene, value: Any, index: int):
    import numpy as np

    if isinstance(value, (list, tuple)):
        return _vec(value, 3, f"correspondences[{index}].world")
    if not isinstance(value, dict):
        raise InvalidParamsError(f"correspondences[{index}].world must be coordinates or an object reference")
    if set(value) == {"empty"}:
        name = value["empty"]
        obj = scene.objects.get(name) if isinstance(name, str) else None
        if obj is None or obj.type != "EMPTY":
            raise InvalidParamsError(f"correspondences[{index}] Empty not found: {name}")
        return np.array(obj.matrix_world.translation, dtype=float)
    if set(value) in ({"object"}, {"object", "vertex_index"}):
        name = value["object"]
        obj = scene.objects.get(name) if isinstance(name, str) else None
        if obj is None:
            raise InvalidParamsError(f"correspondences[{index}] object not found: {name}")
        if "vertex_index" not in value:
            if obj.type != "EMPTY":
                raise InvalidParamsError("Object references without vertex_index must name an Empty")
            return np.array(obj.matrix_world.translation, dtype=float)
        vertex_index = value["vertex_index"]
        if obj.type != "MESH" or isinstance(vertex_index, bool) or not isinstance(vertex_index, int) or not 0 <= vertex_index < len(obj.data.vertices):
            raise InvalidParamsError(f"correspondences[{index}].vertex_index is invalid")
        return np.array(obj.matrix_world @ obj.data.vertices[vertex_index].co, dtype=float)
    raise InvalidParamsError(f"correspondences[{index}].world has unknown fields")


def _correspondences(scene, raw: Any, minimum: int):
    import numpy as np

    if not isinstance(raw, list) or len(raw) < minimum or len(raw) > 256:
        raise InvalidParamsError(f"correspondences must contain {minimum} to 256 points")
    pixels = []
    world = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"pixel", "world"}:
            raise InvalidParamsError(f"correspondences[{index}] requires pixel and world")
        pixels.append(_vec(item["pixel"], 2, f"correspondences[{index}].pixel"))
        world.append(_world_point(scene, item["world"], index))
    return np.stack(pixels), np.stack(world)


def _skew(vector):
    import numpy as np

    x, y, z = vector
    return np.array(((0, -z, y), (z, 0, -x), (-y, x, 0)), dtype=float)


def _rotation(vector):
    import numpy as np

    theta = np.linalg.norm(vector)
    if theta < 1e-12:
        return np.eye(3) + _skew(vector)
    cross = _skew(vector / theta)
    return np.eye(3) + np.sin(theta) * cross + (1 - np.cos(theta)) * cross @ cross


def _rotation_vector(matrix):
    from mathutils import Matrix
    import numpy as np

    axis, angle = Matrix(matrix.tolist()).to_quaternion().to_axis_angle()
    return np.array(axis, dtype=float) * angle


def _rq(matrix):
    import numpy as np

    q, r = np.linalg.qr(np.flipud(matrix).T)
    upper = np.fliplr(np.flipud(r.T))
    rotation = np.flipud(q.T)
    signs = np.sign(np.diag(upper))
    signs[signs == 0] = 1
    sign_matrix = np.diag(signs)
    upper = upper @ sign_matrix
    rotation = sign_matrix @ rotation
    if np.linalg.det(rotation) < 0:
        upper = -upper
        rotation = -rotation
    return upper, rotation


def _perspective_dlt(world, pixels, width, height):
    import numpy as np

    center = world.mean(axis=0)
    scale = np.sqrt(((world - center) ** 2).sum(axis=1).mean())
    if scale < 1e-10:
        raise InvalidParamsError("World points are degenerate")
    world_n = (world - center) / scale
    pixel_n = (pixels - np.array((width / 2, height / 2))) / max(width, height)
    rows = []
    for xyz, (u, v) in zip(world_n, pixel_n):
        x = np.r_[xyz, 1.0]
        rows.extend((np.r_[x, np.zeros(4), -u * x], np.r_[np.zeros(4), x, -v * x]))
    matrix = np.stack(rows)
    _, singular, vh = np.linalg.svd(matrix)
    if singular[-2] < 1e-9:
        raise InvalidParamsError("Correspondences are degenerate for perspective calibration")
    p = vh[-1].reshape(3, 4)
    world_norm = np.eye(4)
    world_norm[:3, :3] /= scale
    world_norm[:3, 3] = -center / scale
    pixel_denorm = np.array(((max(width, height), 0, width / 2),
                             (0, max(width, height), height / 2), (0, 0, 1)), dtype=float)
    p = pixel_denorm @ p @ world_norm
    if np.linalg.det(p[:, :3]) < 0:
        p = -p
    k, rotation = _rq(p[:, :3])
    if abs(k[2, 2]) < 1e-10:
        raise InvalidParamsError("Perspective calibration has singular projection")
    k /= k[2, 2]
    focal = (abs(k[0, 0]) + abs(k[1, 1])) * 0.5
    camera_center = -np.linalg.solve(p[:, :3], p[:, 3])
    if not np.isfinite(focal) or focal <= 0 or not np.all(np.isfinite(camera_center)):
        raise InvalidParamsError("Perspective calibration has invalid focal length or camera position")
    return camera_center, rotation, focal


def _look_at(camera_center, target):
    import numpy as np

    forward = target - camera_center
    forward /= np.linalg.norm(forward)
    up = np.array((0.0, 0.0, 1.0))
    if abs(forward @ up) > 0.95:
        up = np.array((0.0, 1.0, 0.0))
    right = np.cross(forward, up)
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    return np.stack((right, down, forward))


def _perspective_project(params, world, width, height, fixed_focal=None):
    import numpy as np

    center = params[:3]
    rotation = _rotation(params[3:6])
    focal = fixed_focal if fixed_focal is not None else np.exp(params[6])
    camera = (world - center) @ rotation.T
    depth = camera[:, 2]
    safe = np.where(depth > 1e-4, depth, 1e-4)
    uv = camera[:, :2] / safe[:, None] * focal + np.array((width / 2, height / 2))
    if np.any(depth <= 1e-4):
        uv[depth <= 1e-4] += 1e4
    return uv


def _orthographic_project(params, world, width, height):
    import numpy as np

    rotation = _rotation(params[:3])
    scale = np.exp(params[5])
    return world @ rotation[:2].T * scale + np.array((width / 2, height / 2)) + params[3:5]


def _refine(initial, project, observed, *, max_iter=80):
    import numpy as np

    params = np.array(initial, dtype=float)
    damping = 1e-2
    for _ in range(max_iter):
        residual = (project(params) - observed).reshape(-1)
        cost = residual @ residual
        jacobian = np.empty((len(residual), len(params)), dtype=float)
        for column in range(len(params)):
            step = 1e-5 * max(1.0, abs(params[column]))
            trial = params.copy()
            trial[column] += step
            jacobian[:, column] = ((project(trial) - observed).reshape(-1) - residual) / step
        hessian = jacobian.T @ jacobian
        gradient = jacobian.T @ residual
        diagonal = np.maximum(np.diag(hessian), 1.0)
        try:
            update = np.linalg.solve(hessian + damping * np.diag(diagonal), -gradient)
        except np.linalg.LinAlgError:
            break
        trial = params + update
        trial_cost = np.sum((project(trial) - observed) ** 2)
        if np.isfinite(trial_cost) and trial_cost < cost:
            params = trial
            damping = max(damping / 2, 1e-8)
            if np.linalg.norm(update) < 1e-8:
                break
        else:
            damping = min(damping * 4, 1e8)
    return params, float(np.sqrt(np.mean(np.sum((project(params) - observed) ** 2, axis=1))))


def _solve_perspective(world, pixels, width, height, focal_mm=None):
    import numpy as np

    fixed_focal = focal_mm * width / 36.0 if focal_mm is not None else None
    candidates = []
    if len(world) >= 6:
        try:
            center, rotation, focal = _perspective_dlt(world, pixels, width, height)
            candidates.append(np.r_[center, _rotation_vector(rotation), log(max(focal, 1.0))]
                              if fixed_focal is None else np.r_[center, _rotation_vector(rotation)])
        except (InvalidParamsError, np.linalg.LinAlgError):
            pass
    centroid = world.mean(axis=0)
    spread = max(np.linalg.norm(world - centroid, axis=1).max(), 0.01)
    pixel_span = max(np.ptp(pixels, axis=0).max(), 1.0)
    guessed_focal = fixed_focal or width
    distance = max(spread * guessed_focal / pixel_span * 2.0, spread * 2.0)
    for direction in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)):
        center = centroid + np.array(direction, dtype=float) * distance
        rotation = _look_at(center, centroid)
        base = np.r_[center, _rotation_vector(rotation)]
        candidates.append(base if fixed_focal is not None else np.r_[base, log(guessed_focal)])
    project = lambda p: _perspective_project(p, world, width, height, fixed_focal)
    solutions = [_refine(candidate, project, pixels) for candidate in candidates]
    params, rms = min(solutions, key=lambda item: item[1])
    focal = fixed_focal if fixed_focal is not None else float(np.exp(params[6]))
    if not np.isfinite(rms) or not 1.0 <= focal <= width * 100:
        raise InvalidParamsError("Perspective calibration did not converge to a valid camera")
    return params[:3], _rotation(params[3:6]), focal, project(params), rms


def _solve_orthographic(world, pixels, width, height):
    import numpy as np

    design = np.column_stack((world, np.ones(len(world))))
    affine, _, rank, _ = np.linalg.lstsq(design, pixels, rcond=None)
    if rank < 4:
        raise InvalidParamsError("Orthographic correspondences are degenerate")
    rows = affine[:3, :].T
    first = rows[0] / np.linalg.norm(rows[0])
    second = rows[1] - first * (rows[1] @ first)
    if np.linalg.norm(second) < 1e-8:
        raise InvalidParamsError("Orthographic projection axes are degenerate")
    second /= np.linalg.norm(second)
    third = np.cross(first, second)
    rotation = np.stack((first, second, third))
    scale = max((np.linalg.norm(rows[0]) + np.linalg.norm(rows[1])) / 2, 1e-6)
    translation = affine[3, :] - np.array((width / 2, height / 2))
    initial = np.r_[_rotation_vector(rotation), translation, log(scale)]
    project = lambda p: _orthographic_project(p, world, width, height)
    params, rms = _refine(initial, project, pixels)
    rotation = _rotation(params[:3])
    scale = float(np.exp(params[5]))
    centroid = world.mean(axis=0)
    desired = -(params[3:5] / scale)
    center = centroid.copy()
    center += rotation[0] * (desired[0] - rotation[0] @ centroid)
    center += rotation[1] * (desired[1] - rotation[1] @ centroid)
    center -= rotation[2] * max(np.linalg.norm(world - centroid, axis=1).max() * 3, 1.0)
    return center, rotation, scale, project(params), rms


def _camera_transform(center, cv_rotation):
    import numpy as np
    from mathutils import Matrix

    blender_rotation = np.stack((cv_rotation[0], -cv_rotation[1], -cv_rotation[2]), axis=1)
    rotation = Matrix(blender_rotation.tolist())
    transform = rotation.to_4x4()
    transform.translation = center
    return transform


def _draw_mark(image, x, y, color, radius=3):
    height, width = image.shape[:2]
    for yy in range(max(0, y - radius), min(height, y + radius + 1)):
        for xx in range(max(0, x - radius), min(width, x + radius + 1)):
            if (xx - x) ** 2 + (yy - y) ** 2 <= radius * radius:
                image[height - 1 - yy, xx, :3] = color
                image[height - 1 - yy, xx, 3] = 1


def _draw_line(image, start, end, color):
    import numpy as np

    count = max(1, int(np.linalg.norm(end - start) * 2))
    for t in np.linspace(0, 1, count):
        x, y = np.rint(start + (end - start) * t).astype(int)
        _draw_mark(image, x, y, color, radius=1)


def calibrate_from_reference(params: dict[str, Any]) -> ToolResult:
    """Solve camera pose/intrinsics from measured image/world correspondences."""
    import numpy as np

    allowed = {"reference_path", "correspondences", "model", "camera_name", "focal_length_mm",
               "output_path", "overwrite", "high_error_fraction", "expected_state"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    model = params.get("model", "perspective")
    if model not in {"perspective", "orthographic"}:
        raise InvalidParamsError("model must be perspective or orthographic")
    camera_name = params.get("camera_name")
    if not isinstance(camera_name, str) or not camera_name.strip():
        raise InvalidParamsError("camera_name must be a non-empty string")
    focal_mm = params.get("focal_length_mm")
    if focal_mm is not None:
        if model != "perspective":
            raise InvalidParamsError("focal_length_mm applies only to perspective cameras")
        focal_mm = _number(focal_mm, "focal_length_mm", minimum=1)
    high_fraction = _number(params.get("high_error_fraction", 0.01), "high_error_fraction", minimum=0, maximum=1)
    overwrite = params.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise InvalidParamsError("overwrite must be a boolean")
    reference_path = _path(params.get("reference_path"), "reference_path", exists=True)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    require_document_write_access(bpy)
    scene = bpy.context.scene
    output = _output_path(bpy, params.get("output_path"), "calibration")
    if output == reference_path:
        raise InvalidParamsError("output_path must differ from reference_path")
    if output.exists() and not overwrite:
        raise InvalidParamsError(f"Output already exists: {output}")
    image = _image(bpy, reference_path, "reference_path")
    try:
        width, height = map(int, image.size)
        if width * height > 16_777_216:
            raise InvalidParamsError("Reference image exceeds 16 million pixels")
        pixels, world = _correspondences(scene, params.get("correspondences"),
                                        4 if focal_mm is not None or model == "orthographic" else 6)
        if np.linalg.matrix_rank(world - world.mean(axis=0), tol=1e-8) < 3:
            raise InvalidParamsError("World points must span 3D; coplanar or collinear points are insufficient")
        if np.linalg.matrix_rank(pixels - pixels.mean(axis=0), tol=1e-8) < 2:
            raise InvalidParamsError("Image points are collinear or duplicated")
        if np.any(pixels[:, 0] < 0) or np.any(pixels[:, 0] >= width) or np.any(pixels[:, 1] < 0) or np.any(pixels[:, 1] >= height):
            raise InvalidParamsError("Correspondence pixels must lie inside the reference image")
        if model == "perspective":
            center, rotation, focal_px, projected, rms = _solve_perspective(world, pixels, width, height, focal_mm)
            lens_mm = focal_px * 36.0 / width
            ortho_scale = None
        else:
            center, rotation, scale, projected, rms = _solve_orthographic(world, pixels, width, height)
            lens_mm = None
            ortho_scale = width / scale
        if not np.all(np.isfinite(projected)) or not np.isfinite(rms):
            raise InvalidParamsError("Calibration produced non-finite reprojections")
        overlay = _pixels(image)
        errors = np.linalg.norm(projected - pixels, axis=1)
        for actual, prediction in zip(pixels, projected):
            _draw_line(overlay, actual, prediction, (1.0, 1.0, 0.0))
            _draw_mark(overlay, *np.rint(actual).astype(int), (1.0, 0.0, 0.0))
            _draw_mark(overlay, *np.rint(prediction).astype(int), (0.0, 1.0, 0.0))
        _save_rgba(bpy, overlay, output, "CodexCalibrationOverlay")

        camera = scene.objects.get(camera_name)
        if camera is not None and camera.type != "CAMERA":
            raise InvalidParamsError(f"Existing object is not a camera: {camera_name}")
        created = camera is None
        camera_data = None
        shared_original_data = None
        old = None
        old_render = (scene.render.resolution_x, scene.render.resolution_y,
                      scene.render.resolution_percentage, scene.render.pixel_aspect_x,
                      scene.render.pixel_aspect_y)
        try:
            if created:
                camera_data = bpy.data.cameras.new(camera_name)
                camera = bpy.data.objects.new(camera_name, camera_data)
                scene.collection.objects.link(camera)
            else:
                old = (camera.matrix_world.copy(), camera.data.type, camera.data.lens,
                       camera.data.ortho_scale, camera.data.sensor_fit, camera.data.sensor_width,
                       camera.data.shift_x, camera.data.shift_y)
                if camera.data.users > 1:
                    shared_original_data = camera.data
                    camera_data = camera.data.copy()
                    camera.data = camera_data
            camera.matrix_world = _camera_transform(center, rotation)
            camera.data.type = "PERSP" if model == "perspective" else "ORTHO"
            camera.data.sensor_fit = "HORIZONTAL"
            camera.data.sensor_width = 36.0
            camera.data.shift_x = camera.data.shift_y = 0.0
            if model == "perspective":
                camera.data.lens = lens_mm
            else:
                camera.data.ortho_scale = ortho_scale
            scene.render.resolution_x = width
            scene.render.resolution_y = height
            scene.render.resolution_percentage = 100
            scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1
            bpy.context.view_layer.update()
        except Exception:
            if created:
                if camera is not None:
                    bpy.data.objects.remove(camera, do_unlink=True)
                if camera_data is not None:
                    bpy.data.cameras.remove(camera_data)
            elif old is not None:
                if shared_original_data is not None:
                    camera.data = shared_original_data
                    bpy.data.cameras.remove(camera_data)
                (camera.matrix_world, camera.data.type, camera.data.lens,
                 camera.data.ortho_scale, camera.data.sensor_fit, camera.data.sensor_width,
                 camera.data.shift_x, camera.data.shift_y) = old
            (scene.render.resolution_x, scene.render.resolution_y,
             scene.render.resolution_percentage, scene.render.pixel_aspect_x,
             scene.render.pixel_aspect_y) = old_render
            bump_epoch()
            raise

        bump_epoch()

        per_point = [{"index": int(i), "error_px": float(error),
                      "observed": pixels[i].tolist(), "projected": projected[i].tolist()}
                     for i, error in enumerate(errors)]
        worst = sorted(per_point, key=lambda entry: entry["error_px"], reverse=True)[:5]
        high = bool(rms > high_fraction * sqrt(width * width + height * height))
        warnings = [f"Calibration RMS {rms:.2f}px exceeds {high_fraction:.1%} of image diagonal"] if high else []
        return ToolResult(data={
            "camera_name": camera.name, "model": model, "width": width, "height": height,
            "camera": {"location": list(camera.matrix_world.translation),
                       "rotation_euler": list(camera.matrix_world.to_euler()),
                       "lens": float(camera.data.lens), "ortho_scale": float(camera.data.ortho_scale)},
            "rms_px": float(rms), "per_point": per_point, "worst_points": worst,
            "overlay_path": str(output), "high_error": high,
            "changed": True, "state_token": current_state_token(bpy),
        }, warnings=warnings)
    finally:
        bpy.data.images.remove(image)


def _camera_projection(camera, width: int, height: int):
    import numpy as np

    data = camera.data
    if data.type not in {"PERSP", "ORTHO"}:
        raise InvalidParamsError("camera_name must identify a perspective or orthographic camera")
    if abs(data.shift_x) > 1e-8 or abs(data.shift_y) > 1e-8:
        raise InvalidParamsError("Camera shift is unsupported for contour comparison; calibrate with centered principal point")
    transform = camera.matrix_world
    center = np.array(transform.translation, dtype=float)
    right = np.array(transform.col[0][:3], dtype=float)
    down = -np.array(transform.col[1][:3], dtype=float)
    forward = -np.array(transform.col[2][:3], dtype=float)
    for axis in (right, down, forward):
        axis /= np.linalg.norm(axis)
    rotation = np.stack((right, down, forward))
    if data.type == "PERSP":
        vertical_fit = data.sensor_fit == "VERTICAL" or (data.sensor_fit == "AUTO" and height > width)
        focal = data.lens * (height / data.sensor_height if vertical_fit else width / data.sensor_width)
        if focal <= 0:
            raise InvalidParamsError("Camera focal length must be positive")
        scale = focal
    else:
        if data.ortho_scale <= 0:
            raise InvalidParamsError("Camera orthographic scale must be positive")
        scale = width / data.ortho_scale
    return center, rotation, scale, data.type == "ORTHO", float(data.clip_start), float(data.clip_end)


def _project_mesh_vertices(world, center, rotation, scale, orthographic, width, height):
    import numpy as np

    camera = (world - center) @ rotation.T
    depth = camera[:, 2]
    if orthographic:
        xy = camera[:, :2] * scale
    else:
        xy = camera[:, :2] / np.maximum(depth[:, None], 1e-8) * scale
    return xy + np.array((width / 2, height / 2)), depth


def _raster_triangle(uv, depths, zbuffer):
    import numpy as np

    height, width = zbuffer.shape
    min_x = max(0, int(np.floor(uv[:, 0].min())))
    max_x = min(width - 1, int(np.ceil(uv[:, 0].max())))
    min_y = max(0, int(np.floor(uv[:, 1].min())))
    max_y = min(height - 1, int(np.ceil(uv[:, 1].max())))
    if max_x < min_x or max_y < min_y:
        return
    a, b, c = uv
    denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(denominator) < 1e-10:
        return
    x = np.arange(min_x, max_x + 1, dtype=float) + 0.5
    for y in range(min_y, max_y + 1):
        yy = y + 0.5
        w0 = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (yy - c[1])) / denominator
        w1 = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (yy - c[1])) / denominator
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue
        # Perspective-correct depth.  Orthographic interpolation has the same
        # visible ordering; harmonic interpolation is monotone for positive z.
        depth = 1.0 / (w0 / depths[0] + w1 / depths[1] + w2 / depths[2])
        segment = zbuffer[y, min_x:max_x + 1]
        closer = inside & (depth < segment)
        segment[closer] = depth[closer]


def _raster_objects(bpy, scene, objects, projection, width, height):
    import numpy as np

    center, rotation, scale, orthographic, clip_start, clip_end = projection
    depth = np.full((height, width), np.inf, dtype=np.float32)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    triangles = 0
    clipped = 0
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            mesh.calc_loop_triangles()
            if not mesh.vertices or not mesh.loop_triangles:
                continue
            vertices = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", vertices)
            local = vertices.reshape(-1, 3)
            transform = np.array(evaluated.matrix_world, dtype=float)
            world = local @ transform[:3, :3].T + transform[:3, 3]
            uv, vertex_depth = _project_mesh_vertices(world, center, rotation, scale,
                                                      orthographic, width, height)
            for triangle in mesh.loop_triangles:
                indices = list(triangle.vertices)
                z = vertex_depth[indices]
                if np.any(z <= clip_start) or np.any(z >= clip_end):
                    clipped += 1
                    continue
                _raster_triangle(uv[indices], z, depth)
                triangles += 1
        finally:
            evaluated.to_mesh_clear()
    return np.isfinite(depth), depth, triangles, clipped


def _contour(mask):
    import numpy as np

    eroded = mask.copy()
    eroded[1:, :] &= mask[:-1, :]
    eroded[:-1, :] &= mask[1:, :]
    eroded[:, 1:] &= mask[:, :-1]
    eroded[:, :-1] &= mask[:, 1:]
    eroded[0, :] = eroded[-1, :] = False
    eroded[:, 0] = eroded[:, -1] = False
    return mask & ~eroded


def _edt_1d(values):
    """Exact squared Euclidean transform and nearest finite site (1D)."""
    import numpy as np

    sites = np.flatnonzero(np.isfinite(values))
    length = len(values)
    if len(sites) == 0:
        return np.full(length, np.inf), np.full(length, -1, dtype=np.int32)
    envelope = np.empty(len(sites), dtype=np.int32)
    boundaries = np.empty(len(sites) + 1, dtype=float)
    envelope[0] = sites[0]
    boundaries[0] = -np.inf
    boundaries[1] = np.inf
    k = 0
    for q in sites[1:]:
        while True:
            p = envelope[k]
            intersection = ((values[q] + q * q) - (values[p] + p * p)) / (2 * (q - p))
            if intersection > boundaries[k] or k == 0:
                break
            k -= 1
        if intersection <= boundaries[k] and k == 0:
            envelope[0] = q
            boundaries[1] = np.inf
        else:
            k += 1
            envelope[k] = q
            boundaries[k] = intersection
            boundaries[k + 1] = np.inf
    distances = np.empty(length, dtype=float)
    nearest = np.empty(length, dtype=np.int32)
    k = 0
    for q in range(length):
        while boundaries[k + 1] < q:
            k += 1
        p = envelope[k]
        distances[q] = (q - p) ** 2 + values[p]
        nearest[q] = p
    return distances, nearest


def _distance_transform(contour):
    import numpy as np

    height, width = contour.shape
    horizontal = np.empty((height, width), dtype=float)
    nearest_x = np.empty((height, width), dtype=np.int32)
    for y in range(height):
        horizontal[y], nearest_x[y] = _edt_1d(np.where(contour[y], 0.0, np.inf))
    result = np.empty((height, width), dtype=float)
    nearest_y = np.empty((height, width), dtype=np.int32)
    for x in range(width):
        result[:, x], nearest_y[:, x] = _edt_1d(horizontal[:, x])
    selected_x = np.full((height, width), -1, dtype=np.int32)
    valid = nearest_y >= 0
    ys, xs = np.nonzero(valid)
    selected_x[ys, xs] = nearest_x[nearest_y[ys, xs], xs]
    return np.sqrt(result), selected_x, nearest_y


def _mask_from_image(pixels, params, *, mask_image=False):
    import numpy as np

    rgb = pixels[:, :, :3]
    if mask_image:
        return rgb.mean(axis=2) >= _number(params.get("threshold", 0.5), "threshold", minimum=0, maximum=1)
    if "background_color" in params:
        color = _vec(params["background_color"], 3, "background_color")
        if np.any(color < 0) or np.any(color > 1):
            raise InvalidParamsError("background_color channels must be from 0 to 1")
        tolerance = _number(params.get("color_tolerance", 0.1), "color_tolerance", minimum=0, maximum=1)
        return np.linalg.norm(rgb - color, axis=2) > tolerance
    foreground = params.get("foreground", "dark")
    if foreground not in {"dark", "light"}:
        raise InvalidParamsError("foreground must be dark or light")
    threshold = _number(params.get("threshold", 0.5), "threshold", minimum=0, maximum=1)
    luminance = rgb @ np.array((0.2126, 0.7152, 0.0722))
    return luminance < threshold if foreground == "dark" else luminance >= threshold


def _contour_stats(reference_contour, model_contour, to_reference, to_model):
    import numpy as np

    if not reference_contour.any() and not model_contour.any():
        return {"mean": 0.0, "p95": 0.0, "max": 0.0}
    if not reference_contour.any() or not model_contour.any():
        return {"mean": None, "p95": None, "max": None}
    distances = np.r_[to_reference[model_contour], to_model[reference_contour]]
    return {"mean": float(distances.mean()), "p95": float(np.percentile(distances, 95)),
            "max": float(distances.max())}


def _deviation_regions(mismatch, to_reference, to_model, nearest_model_x, nearest_model_y,
                       model_contour, depth, projection, top_n):
    import numpy as np

    height, width = mismatch.shape
    visited = np.zeros_like(mismatch, dtype=bool)
    regions = []
    model_pixels = model_contour.any()
    center, rotation, scale, orthographic, _, _ = projection
    for seed_y, seed_x in np.argwhere(mismatch):
        if visited[seed_y, seed_x]:
            continue
        stack = [(int(seed_y), int(seed_x))]
        visited[seed_y, seed_x] = True
        area = 0
        peak = -1.0
        peak_xy = (seed_x, seed_y)
        while stack:
            y, x = stack.pop()
            area += 1
            distance = to_model[y, x] if not np.isfinite(depth[y, x]) else to_reference[y, x]
            if np.isfinite(distance) and distance > peak:
                peak = float(distance)
                peak_xy = (x, y)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < height and 0 <= nx < width and mismatch[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        regions.append((area, peak, peak_xy))
    regions.sort(key=lambda item: (item[0], item[1]), reverse=True)
    output = []
    for area, peak, (x, y) in regions[:top_n]:
        focus = None
        if model_pixels:
            mx, my = int(nearest_model_x[y, x]), int(nearest_model_y[y, x])
            if mx >= 0 and my >= 0 and np.isfinite(depth[my, mx]):
                z = float(depth[my, mx])
                pixel_scale = 1 / scale if orthographic else z / scale
                local = np.array(((mx + 0.5 - width / 2) * pixel_scale,
                                  (my + 0.5 - height / 2) * pixel_scale, z))
                world = center + rotation.T @ local
                focus = {"point": world.tolist(),
                         "radius": float(max(5.0, sqrt(area / 3.14159)) * pixel_scale * 1.5)}
        output.append({"pixel": [int(x), int(y)], "area_pixels": int(area),
                       "distance_px": float(peak) if peak >= 0 else None,
                       "focus": focus})
    return output


def contour_compare(params: dict[str, Any]) -> ToolResult:
    """Compare a reference mask with a CPU raster of evaluated target meshes."""
    import numpy as np

    allowed = {"reference_path", "camera_name", "object_names", "mask_path", "threshold",
               "foreground", "background_color", "color_tolerance", "output_path",
               "top_n", "overwrite", "min_iou", "borderline_band"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    reference_path = _path(params.get("reference_path"), "reference_path", exists=True)
    mask_path = _path(params["mask_path"], "mask_path", exists=True) if "mask_path" in params else None
    camera_name = params.get("camera_name")
    if not isinstance(camera_name, str) or not camera_name.strip():
        raise InvalidParamsError("camera_name must be a non-empty string")
    names = params.get("object_names")
    if not isinstance(names, list) or not names or len(names) > 64 or any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(names):
        raise InvalidParamsError("object_names must contain 1 to 64 distinct object names")
    top_n = params.get("top_n", 5)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 10:
        raise InvalidParamsError("top_n must be an integer from 1 to 10")
    min_iou = params.get("min_iou")
    if min_iou is not None:
        min_iou = _number(min_iou, "min_iou", minimum=0, maximum=1)
    borderline_band = _number(params.get("borderline_band", 0.01), "borderline_band", minimum=0, maximum=1)
    overwrite = params.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise InvalidParamsError("overwrite must be a boolean")
    bpy = get_bpy()
    scene = bpy.context.scene
    camera = scene.objects.get(camera_name)
    if camera is None or camera.type != "CAMERA":
        raise InvalidParamsError(f"Camera not found in active scene: {camera_name}")
    objects = []
    for name in names:
        obj = scene.objects.get(name)
        if obj is None or obj.type != "MESH":
            raise InvalidParamsError(f"Mesh object not found in active scene: {name}")
        objects.append(obj)
    output = _output_path(bpy, params.get("output_path"), "contour_deviation")
    if output.exists() and not overwrite:
        raise InvalidParamsError(f"Output already exists: {output}")
    if output in {reference_path, mask_path}:
        raise InvalidParamsError("output_path must differ from input image paths")
    reference = _image(bpy, reference_path, "reference_path")
    mask_image = None
    try:
        width, height = map(int, reference.size)
        if width * height > 4_194_304:
            raise InvalidParamsError("Contour comparison exceeds four million pixels")
        if mask_path is not None:
            mask_image = _image(bpy, mask_path, "mask_path")
            if tuple(mask_image.size) != (width, height):
                raise InvalidParamsError("mask_path dimensions must match reference_path")
        source = _pixels(reference)
        mask_pixels = _pixels(mask_image) if mask_image is not None else source
        # Blender image buffers are bottom-up.  All geometry/mask operations
        # use conventional top-left image coordinates.
        reference_mask = _mask_from_image(mask_pixels[::-1], params, mask_image=mask_image is not None)
        projection = _camera_projection(camera, width, height)
        model_mask, depth, triangles, clipped = _raster_objects(
            bpy, scene, objects, projection, width, height
        )
        if triangles == 0:
            raise InvalidParamsError("Target objects have no triangles inside camera clipping range")
        intersection = int(np.logical_and(reference_mask, model_mask).sum())
        union = int(np.logical_or(reference_mask, model_mask).sum())
        iou = intersection / union if union else 1.0
        reference_contour = _contour(reference_mask)
        model_contour = _contour(model_mask)
        to_reference, _, _ = _distance_transform(reference_contour)
        to_model, nearest_x, nearest_y = _distance_transform(model_contour)
        stats = _contour_stats(reference_contour, model_contour, to_reference, to_model)
        mismatch = np.logical_xor(reference_mask, model_mask)
        regions = _deviation_regions(mismatch, to_reference, to_model, nearest_x, nearest_y,
                                     model_contour, depth, projection, top_n)
        deviation = source.copy() * np.array((0.25, 0.25, 0.25, 1.0), dtype=np.float32)
        deviation[:, :, 3] = 1
        for y, x in np.argwhere(reference_contour):
            intensity = 0.35 + 0.65 * min(float(to_model[y, x]) / 10.0, 1.0)
            deviation[height - 1 - y, x, :3] = (intensity, 0.0, 0.0)
        for y, x in np.argwhere(model_contour):
            intensity = 0.35 + 0.65 * min(float(to_reference[y, x]) / 10.0, 1.0)
            deviation[height - 1 - y, x, :3] = (0.0, intensity, intensity)
        _save_rgba(bpy, deviation, output, "CodexContourDeviation")
        units = None
        if projection[3] and stats["mean"] is not None:
            unit_per_pixel = 1 / projection[2]
            units = {key: value * unit_per_pixel for key, value in stats.items()}
        warnings = []
        if clipped:
            warnings.append(f"Skipped {clipped} triangles crossing camera clipping planes")
        if not reference_contour.any() or not model_contour.any():
            warnings.append("One silhouette has no contour; distance statistics are unavailable")
        if any(region["focus"] is None for region in regions):
            warnings.append("Some deviations have no nearest rendered model surface for focus")
        acceptance = None if min_iou is None else ("borderline" if abs(iou - min_iou) <= borderline_band
                                                 else "pass" if iou > min_iou else "fail")
        return ToolResult(data={
            "camera_name": camera.name, "object_names": names, "width": width, "height": height,
            "iou": float(iou), "contour_distance_px": stats,
            "contour_distance_units": units, "deviation_path": str(output),
            "regions": regions, "reference_pixels": int(reference_mask.sum()),
            "model_pixels": int(model_mask.sum()), "triangles": triangles,
            "acceptance": acceptance, "min_iou": min_iou,
        }, warnings=warnings)
    finally:
        if mask_image is not None:
            bpy.data.images.remove(mask_image)
        bpy.data.images.remove(reference)
