"""PNG preview, review-set rendering, and structured image comparison."""

from __future__ import annotations

from datetime import datetime, timezone
from array import array
from math import atan, isfinite, tan
from pathlib import Path
import tempfile
from typing import Any, Sequence

from ..errors import InvalidParamsError, InvalidStateError
from ..registry import DEFERRED_CONTEXT_KEY, DeferredToolResult, ToolResult, active_context
from ._blender import get_bpy
from .form_views import depth_range, install_form_override, view_mode

DEFAULT_PIXEL_BUDGET = 8_294_400
HARD_PIXEL_CAP = 67_108_864
REVIEW_VIEWS = ("camera", "top", "side", "three_quarter")
REVIEW_DIRECTIONS = {
    "top": (0.0, 0.0, 1.0),
    "side": (1.0, 0.0, 0.0),
    "three_quarter": (1.0, -1.0, 0.75),
}
DETAIL_DIRECTIONS = {
    "front": (0.0, -1.0, 0.0), "back": (0.0, 1.0, 0.0),
    "left": (-1.0, 0.0, 0.0), "right": (1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0), "bottom": (0.0, 0.0, -1.0),
    "three_quarter": REVIEW_DIRECTIONS["three_quarter"],
}
ORTHO_VIEWS = ("front", "back", "left", "right", "top", "bottom")
_ACTIVE_RENDER_JOB = None


def _dimension(params: dict[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16384:
        raise InvalidParamsError(f"{name} must be an integer from 1 to 16384")
    return value


def _boolean_param(params: dict[str, Any], name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{name} must be a boolean")
    return value


def _validate_pixel_budget(width: int, height: int, allow_large: bool) -> None:
    pixels = width * height
    if pixels > HARD_PIXEL_CAP:
        raise InvalidParamsError(
            f"Requested {width}x{height} ({pixels:,} pixels) exceeds the hard pixel cap of "
            f"{HARD_PIXEL_CAP:,}; allow_large cannot exceed this cap. The default budget is "
            f"{DEFAULT_PIXEL_BUDGET:,} pixels and allow_large: true raises it only to the hard cap."
        )
    if pixels > DEFAULT_PIXEL_BUDGET and not allow_large:
        raise InvalidParamsError(
            f"Requested {width}x{height} ({pixels:,} pixels) exceeds the default pixel budget of "
            f"{DEFAULT_PIXEL_BUDGET:,}; set allow_large: true to allow up to {HARD_PIXEL_CAP:,} pixels."
        )


def _validate_overwrite(path: Path, overwrite: bool) -> None:
    if path.exists() and overwrite is not True:
        raise InvalidParamsError(f"Output already exists and overwrite is false: {path}")


def _non_empty_string(params: dict[str, Any], name: str, *, required: bool = False) -> str | None:
    value = params.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        if required:
            raise InvalidParamsError(f"{name} is required and must be a non-empty string")
        raise InvalidParamsError(f"{name} must be a non-empty string")
    return value


def _name_list_param(params: dict[str, Any], name: str) -> list[str] | None:
    if name not in params:
        return None
    value = params[name]
    if not isinstance(value, list):
        raise InvalidParamsError(f"{name} must be a list of collection names")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise InvalidParamsError(f"{name} must contain only non-empty collection names")
    if len(set(value)) != len(value):
        raise InvalidParamsError(f"{name} must not contain duplicate collection names")
    return list(value)


def _review_views(params: dict[str, Any]) -> list[str]:
    if "views" not in params:
        return list(REVIEW_VIEWS)
    value = params["views"]
    if not isinstance(value, list) or not value:
        raise InvalidParamsError("views must be a non-empty list of preset view names")
    if not all(isinstance(item, str) for item in value):
        raise InvalidParamsError("views must contain only preset view names")
    invalid = sorted(set(value) - set(REVIEW_VIEWS))
    if invalid:
        raise InvalidParamsError(
            f"Unknown review views: {', '.join(invalid)}; expected a subset of {', '.join(REVIEW_VIEWS)}"
        )
    if len(set(value)) != len(value):
        raise InvalidParamsError("views must not contain duplicates")
    requested = set(value)
    return [view for view in REVIEW_VIEWS if view in requested]


def _threshold(params: dict[str, Any]) -> int:
    value = params.get("threshold", 8)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise InvalidParamsError("threshold must be an integer from 0 to 255")
    return value


def _output_path(bpy, supplied: Any) -> Path:
    if supplied is not None:
        if not isinstance(supplied, str) or not supplied.strip():
            raise InvalidParamsError("output_path must be a non-empty string")
        path = Path(supplied).expanduser()
    else:
        root = Path(bpy.data.filepath).resolve().parent if bpy.data.filepath else Path(tempfile.gettempdir())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = root / "previews" / f"preview_{stamp}.png"
    if path.suffix.lower() != ".png":
        path = path.with_suffix(".png")
    return path.resolve()


def _review_output_paths(bpy, params: dict[str, Any], views: Sequence[str]) -> dict[str, Path]:
    output_dir = _non_empty_string(params, "output_dir")
    output_prefix = _non_empty_string(params, "output_prefix")
    if output_dir is not None and output_prefix is not None:
        raise InvalidParamsError("output_dir and output_prefix are mutually exclusive")

    if output_prefix is not None:
        prefix = Path(output_prefix).expanduser()
        if prefix.suffix.lower() == ".png":
            prefix = prefix.with_suffix("")
        prefix = prefix.resolve()
        if prefix.exists() and prefix.is_dir():
            raise InvalidParamsError(f"output_prefix must not be a directory: {prefix}")
        return {view: prefix.parent / f"{prefix.name}_{view}.png" for view in views}

    if output_dir is not None:
        directory = Path(output_dir).expanduser().resolve()
    else:
        root = Path(bpy.data.filepath).resolve().parent if bpy.data.filepath else Path(tempfile.gettempdir())
        directory = (root / "previews").resolve()
    if directory.exists() and not directory.is_dir():
        raise InvalidParamsError(f"output_dir is not a directory: {directory}")

    for index in range(1, 1_000_000):
        prefix = directory / f"review_set_{index:03d}"
        paths = {view: prefix.parent / f"{prefix.name}_{view}.png" for view in views}
        if not any(path.exists() for path in paths.values()):
            return paths
    raise InvalidStateError(f"Could not allocate a review-set sequence in {directory}")


def _ortho_output_paths(bpy, params: dict[str, Any], views: Sequence[str]) -> dict[str, Path]:
    """Combine an explicit directory and basename without changing legacy paths."""
    output_dir = _non_empty_string(params, "output_dir")
    output_prefix = _non_empty_string(params, "output_prefix")
    if output_dir is not None and output_prefix is not None:
        if output_prefix in {".", ".."} or any(char in output_prefix for char in ("/", "\\", ":")):
            raise InvalidParamsError("With output_dir, output_prefix must be a basename without a path")
        directory = Path(output_dir).expanduser().resolve()
        if directory.exists() and not directory.is_dir():
            raise InvalidParamsError(f"output_dir is not a directory: {directory}")
        params = {**params, "output_prefix": str(directory / output_prefix)}
        del params["output_dir"]
    return _review_output_paths(bpy, params, views)


def _object_is_render_visible(obj, visible_collection_names: set[str]) -> bool:
    if obj.hide_render:
        return False
    collections = list(obj.users_collection)
    return not collections or any(collection.name in visible_collection_names for collection in collections)


def _visible_scene_collection_names(scene) -> set[str]:
    visible: set[str] = set()

    def walk(collection, parent_visible: bool) -> None:
        collection_visible = parent_visible and not collection.hide_render
        if collection_visible:
            visible.add(collection.name)
        for child in collection.children:
            walk(child, collection_visible)

    walk(scene.collection, True)
    return visible


def _auto_camera(
    bpy,
    scene,
    width: int,
    height: int,
    direction: Sequence[float] = REVIEW_DIRECTIONS["three_quarter"],
    name: str = "CodexPreviewCamera",
):
    from mathutils import Vector

    geometry_types = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME", "GPENCIL", "GREASEPENCIL"}
    visible_collections = _visible_scene_collection_names(scene)
    points = [
        obj.matrix_world @ Vector(corner)
        for obj in scene.objects
        if obj.type in geometry_types and _object_is_render_visible(obj, visible_collections)
        for corner in obj.bound_box
    ]
    if points:
        minimum = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
        maximum = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
        center = (minimum + maximum) * 0.5
        radius = max(max((point - center).length for point in points), 0.5)
    else:
        center = Vector((0.0, 0.0, 0.0))
        radius = 1.0

    camera_data = None
    camera = None
    try:
        camera_data = bpy.data.cameras.new(name)
        camera = bpy.data.objects.new(name, camera_data)
        scene.collection.objects.link(camera)
        view_direction = Vector(direction).normalized()
        horizontal_half_fov = camera_data.angle * 0.5
        vertical_half_fov = atan(tan(horizontal_half_fov) * height / width)
        limiting_half_fov = min(horizontal_half_fov, vertical_half_fov)
        distance = radius / tan(limiting_half_fov) * 1.35
        camera.location = center + view_direction * distance
        camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        camera_data.clip_start = max(0.01, distance - radius * 2.0)
        camera_data.clip_end = max(1000.0, distance + radius * 4.0)
        return camera, camera_data
    except Exception as primary_error:
        cleanup_errors: list[str] = []
        _cleanup_camera(bpy, camera, camera_data, cleanup_errors)
        if cleanup_errors:
            primary_error.add_note("Cleanup errors: " + "; ".join(cleanup_errors))
        raise


def _render_state(scene) -> tuple[Any, ...]:
    render = scene.render
    return (
        scene.camera,
        render.filepath,
        render.resolution_x,
        render.resolution_y,
        render.resolution_percentage,
        render.image_settings.file_format,
    )


def _restore_render_state(scene, state: tuple[Any, ...]) -> None:
    render = scene.render
    (
        scene.camera,
        render.filepath,
        render.resolution_x,
        render.resolution_y,
        render.resolution_percentage,
        render.image_settings.file_format,
    ) = state


def _configure_render(scene, camera, output: Path, width: int, height: int) -> None:
    scene.camera = camera
    render = scene.render
    render.filepath = str(output)
    render.resolution_x = width
    render.resolution_y = height
    render.resolution_percentage = 100
    render.image_settings.file_format = "PNG"


def _render_still(bpy, scene, camera, output: Path, width: int, height: int) -> None:
    before = _file_signature(output)
    _configure_render(scene, camera, output, width, height)
    result = bpy.ops.render.render(write_still=True)
    if "FINISHED" not in result or _file_signature(output) is None or _file_signature(output) == before:
        raise InvalidStateError(f"Blender did not create render output: {output}")


def _file_signature(path: Path):
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


def _cleanup_camera(bpy, camera, camera_data, cleanup_errors: list[str]) -> None:
    try:
        if camera is not None:
            bpy.data.objects.remove(camera, do_unlink=True)
    except Exception as exc:
        cleanup_errors.append(f"remove temporary camera object: {exc}")
    try:
        if camera_data is not None and bpy.data.cameras.get(camera_data.name) is camera_data:
            bpy.data.cameras.remove(camera_data)
    except Exception as exc:
        cleanup_errors.append(f"remove temporary camera data: {exc}")


def _raise_after_cleanup(primary_error: Exception | None, cleanup_errors: list[str], success_label: str) -> None:
    if primary_error is not None:
        if cleanup_errors:
            primary_error.add_note("Cleanup errors: " + "; ".join(cleanup_errors))
        raise primary_error
    if cleanup_errors:
        raise InvalidStateError(f"{success_label}, but cleanup failed: " + "; ".join(cleanup_errors))


def render_preview(params: dict[str, Any]) -> ToolResult:
    unknown = sorted(set(params) - {"width", "height", "output_path", "overwrite", "allow_large"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    width = _dimension(params, "width", 960)
    height = _dimension(params, "height", 540)
    overwrite = _boolean_param(params, "overwrite")
    allow_large = _boolean_param(params, "allow_large")
    _validate_pixel_budget(width, height, allow_large)
    bpy = get_bpy()
    output = _output_path(bpy, params.get("output_path"))
    _validate_overwrite(output, overwrite)
    output.parent.mkdir(parents=True, exist_ok=True)

    scene = bpy.context.scene
    previous = _render_state(scene)
    temporary_camera = None
    temporary_data = None
    warnings: list[str] = []
    result_data = None
    primary_error = None
    try:
        camera = scene.camera
        if camera is None:
            temporary_camera, temporary_data = _auto_camera(bpy, scene, width, height)
            camera = temporary_camera
            warnings.append("No active scene camera; used a temporary auto-framed camera")
        _render_still(bpy, scene, camera, output, width, height)
        result_data = ToolResult(
            data={"path": str(output), "width": width, "height": height, "camera": camera.name},
            warnings=warnings,
        )
    except Exception as exc:
        primary_error = exc

    cleanup_errors: list[str] = []
    try:
        _restore_render_state(scene, previous)
    except Exception as exc:
        cleanup_errors.append(f"restore render state: {exc}")
    _cleanup_camera(bpy, temporary_camera, temporary_data, cleanup_errors)
    _raise_after_cleanup(primary_error, cleanup_errors, "Preview rendered")
    return result_data


def _collection_visibility_plan(bpy, hide: list[str] | None, show_only: list[str] | None):
    collections = list(bpy.data.collections)
    by_name = {collection.name: collection for collection in collections}
    supplied = set(hide or []) | set(show_only or [])
    missing = sorted(supplied - set(by_name))
    if missing:
        raise InvalidParamsError(f"Collections not found: {', '.join(missing)}")
    if hide is None and show_only is None:
        return collections, None, None

    show_names: set[str] | None = None
    show_scope_names: set[str] | None = None
    if show_only is not None:
        show_names = set(show_only)

        def add_descendants(collection) -> None:
            for child in collection.children:
                if child.name not in show_names:
                    show_names.add(child.name)
                    add_descendants(child)

        for name in list(show_names):
            add_descendants(by_name[name])
        show_scope_names = set(show_names)
        changed = True
        while changed:
            changed = False
            for collection in collections:
                if collection.name not in show_names and any(
                    child.name in show_names for child in collection.children
                ):
                    show_names.add(collection.name)
                    changed = True

    hide_names = set(hide or [])
    desired = []
    for collection in collections:
        hidden = collection.hide_render
        if show_names is not None:
            hidden = collection.name not in show_names
        if collection.name in hide_names:
            hidden = True
        desired.append((collection, bool(hidden)))
    return collections, desired, show_scope_names


def _manifest_entry(path: Path, camera: str, width: int, height: int, warnings: list[str] | None = None):
    return {
        "path": str(path),
        "camera": camera,
        "width": width,
        "height": height,
        "warnings": list(warnings or []),
    }


def render_review_set(params: dict[str, Any]) -> ToolResult:
    allowed = {
        "output_dir", "output_prefix", "views", "width", "height", "overwrite",
        "allow_large", "hide", "show_only",
    }
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    views = _review_views(params)
    width = _dimension(params, "width", 960)
    height = _dimension(params, "height", 540)
    overwrite = _boolean_param(params, "overwrite")
    allow_large = _boolean_param(params, "allow_large")
    hide = _name_list_param(params, "hide")
    show_only = _name_list_param(params, "show_only")
    _non_empty_string(params, "output_dir")
    _non_empty_string(params, "output_prefix")
    if params.get("output_dir") is not None and params.get("output_prefix") is not None:
        raise InvalidParamsError("output_dir and output_prefix are mutually exclusive")
    _validate_pixel_budget(width, height, allow_large)

    bpy = get_bpy()
    scene = bpy.context.scene
    original_camera = scene.camera
    collections, visibility_plan, show_scope_names = _collection_visibility_plan(bpy, hide, show_only)
    rendered_views = [view for view in views if view != "camera" or original_camera is not None]
    paths = _review_output_paths(bpy, params, rendered_views)
    for path in paths.values():
        _validate_overwrite(path, overwrite)
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    previous_render = _render_state(scene)
    previous_visibility = [(collection, bool(collection.hide_render)) for collection in collections]
    previous_object_visibility = [(obj, bool(obj.hide_render)) for obj in scene.objects]
    warnings: list[str] = []
    if "camera" in views and original_camera is None:
        warnings.append("Skipped camera view because the scene has no active camera")
    manifest: dict[str, dict[str, Any]] = {}
    primary_error = None
    try:
        if visibility_plan is not None:
            for collection, hidden in visibility_plan:
                collection.hide_render = hidden
        if show_scope_names is not None:
            for obj in scene.objects:
                if not any(collection.name in show_scope_names for collection in obj.users_collection):
                    obj.hide_render = True
        for view in rendered_views:
            camera = original_camera
            camera_data = None
            temporary_camera = None
            view_error = None
            try:
                if view != "camera":
                    temporary_camera, camera_data = _auto_camera(
                        bpy,
                        scene,
                        width,
                        height,
                        REVIEW_DIRECTIONS[view],
                        f"CodexReview{''.join(part.title() for part in view.split('_'))}Camera",
                    )
                    camera = temporary_camera
                _render_still(bpy, scene, camera, paths[view], width, height)
                manifest[view] = _manifest_entry(paths[view], camera.name, width, height)
            except Exception as exc:
                view_error = exc
            per_view_cleanup: list[str] = []
            try:
                scene.camera = original_camera
            except Exception as exc:
                per_view_cleanup.append(f"restore scene camera: {exc}")
            _cleanup_camera(bpy, temporary_camera, camera_data, per_view_cleanup)
            _raise_after_cleanup(view_error, per_view_cleanup, "Review view rendered")
    except Exception as exc:
        primary_error = exc

    cleanup_errors: list[str] = []
    try:
        _restore_render_state(scene, previous_render)
    except Exception as exc:
        cleanup_errors.append(f"restore render state: {exc}")
    for collection, hidden in previous_visibility:
        try:
            collection.hide_render = hidden
        except Exception as exc:
            cleanup_errors.append(f"restore collection {collection.name} visibility: {exc}")
    for obj, hidden in previous_object_visibility:
        try:
            obj.hide_render = hidden
        except Exception as exc:
            cleanup_errors.append(f"restore object {obj.name} visibility: {exc}")
    _raise_after_cleanup(primary_error, cleanup_errors, "Review set rendered")
    return ToolResult(data={"views": list(manifest), "manifest": manifest}, warnings=warnings)


def _vector3(value: Any, name: str):
    from mathutils import Vector

    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in value
    ):
        raise InvalidParamsError(f"{name} must be three finite numbers")
    return Vector(tuple(float(v) for v in value))


def normalize_render_options(mode: str, options: dict[str, Any]) -> dict[str, Any]:
    """Use the same numeric-vector shape for typed, direct, and child renders."""
    normalized = dict(options)

    def numbers(value: Any, label: str) -> list[float]:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise InvalidParamsError(f"{label} must be three finite numbers")
        result = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float, str)):
                raise InvalidParamsError(f"{label} must be three finite numbers")
            try:
                number = float(item)
            except ValueError as exc:
                raise InvalidParamsError(f"{label} must be three finite numbers") from exc
            if not isfinite(number):
                raise InvalidParamsError(f"{label} must be three finite numbers")
            result.append(number)
        return result

    if mode in {"detail", "ortho_set"}:
        if "point" in normalized:
            normalized["point"] = numbers(normalized["point"], "point")
        if "bounds" in normalized and isinstance(normalized["bounds"], dict):
            normalized["bounds"] = {key: numbers(value, f"bounds.{key}") if key in {"min", "max"}
                                    else value for key, value in normalized["bounds"].items()}
    if mode == "detail" and "direction" in normalized and not isinstance(normalized["direction"], str):
        normalized["direction"] = numbers(normalized["direction"], "direction")
    if mode == "reference_match" and isinstance(normalized.get("camera"), dict):
        camera = dict(normalized["camera"])
        for key in ("location", "rotation_euler"):
            if key in camera:
                camera[key] = numbers(camera[key], f"camera.{key}")
        normalized["camera"] = camera
    return normalized


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
        raise InvalidParamsError(f"{name} must be a positive finite number")
    return float(value)


def _target_bounds(bpy, scene, params: dict[str, Any]):
    """Resolve one target to world-space bounds without changing document state."""
    from mathutils import Vector

    selectors = [key for key in ("object_name", "object_names", "point", "bounds") if key in params]
    if len(selectors) != 1:
        raise InvalidParamsError("Provide exactly one of object_name, object_names, point, or bounds")
    key = selectors[0]
    if key == "point":
        if "radius" not in params:
            raise InvalidParamsError("point requires radius")
        center = _vector3(params["point"], "point")
        radius = _positive_number(params["radius"], "radius")
        delta = Vector((radius, radius, radius))
        return center - delta, center + delta, []
    if "radius" in params:
        raise InvalidParamsError("radius is only valid with point")
    if key == "bounds":
        value = params[key]
        if not isinstance(value, dict) or set(value) != {"min", "max"}:
            raise InvalidParamsError("bounds must contain min and max vectors")
        minimum = _vector3(value["min"], "bounds.min")
        maximum = _vector3(value["max"], "bounds.max")
        if any(minimum[i] >= maximum[i] for i in range(3)):
            raise InvalidParamsError("bounds.max must exceed bounds.min on each axis")
        return minimum, maximum, []
    names = [params[key]] if key == "object_name" else params[key]
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
        raise InvalidParamsError(f"{key} must name one or more objects")
    if len(set(names)) != len(names):
        raise InvalidParamsError("object_names must not contain duplicates")
    objects = []
    points = []
    for name in names:
        obj = scene.objects.get(name)
        if obj is None:
            raise InvalidParamsError(f"Object not found in active scene: {name}")
        objects.append(obj)
        points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not points:
        raise InvalidParamsError("Target objects have no bounds")
    return (
        Vector(tuple(min(point[i] for point in points) for i in range(3))),
        Vector(tuple(max(point[i] for point in points) for i in range(3))),
        objects,
    )


def _detail_target_bounds(bpy, scene, params: dict[str, Any]):
    """Explicit framing bounds may accompany named isolation subjects."""
    names_key = next((key for key in ("object_name", "object_names") if key in params), None)
    if "bounds" not in params or names_key is None:
        return _target_bounds(bpy, scene, params)
    if "point" in params or {"object_name", "object_names"} <= set(params):
        raise InvalidParamsError("bounds may be combined with only one of object_name or object_names")
    minimum, maximum, _ = _target_bounds(
        bpy, scene, {key: value for key, value in params.items() if key != names_key})
    names = [params[names_key]] if names_key == "object_name" else params[names_key]
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
        raise InvalidParamsError(f"{names_key} must name one or more objects")
    if len(set(names)) != len(names):
        raise InvalidParamsError("object_names must not contain duplicates")
    objects = []
    for name in names:
        obj = scene.objects.get(name)
        if obj is None:
            raise InvalidParamsError(f"Object not found in active scene: {name}")
        objects.append(obj)
    return minimum, maximum, objects


def _detail_direction(value: Any):
    if value is None:
        value = "three_quarter"
    if isinstance(value, str):
        if value not in DETAIL_DIRECTIONS:
            raise InvalidParamsError(f"Unknown direction: {value}")
        value = DETAIL_DIRECTIONS[value]
    direction = _vector3(value, "direction")
    if direction.length < 1e-9:
        raise InvalidParamsError("direction must be nonzero")
    return direction.normalized()


def _detail_camera(bpy, scene, bounds, direction, width, height, projection, ortho_scale=None,
                   clip_to_bounds=False):
    from mathutils import Vector

    minimum, maximum = bounds
    center = (minimum + maximum) * 0.5
    corners = [Vector((x, y, z)) for x in (minimum.x, maximum.x) for y in (minimum.y, maximum.y)
               for z in (minimum.z, maximum.z)]
    camera_data = None
    camera = None
    try:
        camera_data = bpy.data.cameras.new("CodexDetailCamera")
        camera = bpy.data.objects.new("CodexDetailCamera", camera_data)
        scene.collection.objects.link(camera)
        camera.location = center + direction * max((maximum - minimum).length, 1.0)
        camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        inverse = camera.rotation_euler.to_matrix().inverted()
        local = [inverse @ (point - center) for point in corners]
        half_x = max(abs(point.x) for point in local)
        half_y = max(abs(point.y) for point in local)
        half_depth = max(abs(point.z) for point in local)
        aspect = width / height
        if projection == "orthographic":
            camera_data.type = "ORTHO"
            camera_data.ortho_scale = ortho_scale or max(2 * half_x, 2 * half_y * aspect, 0.01) * 1.15
            distance = max(half_depth * 3, 1.0)
        else:
            camera_data.type = "PERSP"
            half_horizontal = camera_data.angle * 0.5
            half_vertical = atan(tan(half_horizontal) / aspect)
            distance = max(half_x / tan(half_horizontal), half_y / tan(half_vertical), 0.1) * 1.2 + half_depth
        camera.location = center + direction * distance
        if clip_to_bounds:
            margin = max(0.0001, half_depth * 0.0001)
            camera_data.clip_start = max(0.001, distance - half_depth - margin)
            camera_data.clip_end = distance + half_depth + margin
        else:
            camera_data.clip_start = max(0.001, distance - half_depth * 2)
            camera_data.clip_end = max(1000.0, distance + half_depth * 4)
        receipt = {
            "location": list(camera.location), "rotation_euler": list(camera.rotation_euler),
            "projection": projection, "lens": float(camera_data.lens),
            "ortho_scale": float(camera_data.ortho_scale), "target_center": list(center),
            "direction": list(direction),
            "clip_to_bounds": clip_to_bounds,
            "clip_scope": "camera_depth" if clip_to_bounds else "none",
            "clip_start": float(camera_data.clip_start),
            "clip_end": float(camera_data.clip_end),
        }
        return camera, camera_data, receipt
    except Exception:
        errors: list[str] = []
        _cleanup_camera(bpy, camera, camera_data, errors)
        raise


def _detail_params(params, allowed):
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    width = _dimension(params, "width", 960)
    height = _dimension(params, "height", 540)
    _validate_pixel_budget(width, height, _boolean_param(params, "allow_large"))
    return width, height, _boolean_param(params, "overwrite"), _boolean_param(params, "isolate")


def _isolate_objects(scene, targets):
    if not targets:
        raise InvalidParamsError("isolate requires object_name or object_names")
    keep = {obj.as_pointer() for obj in targets}
    previous_objects = [(obj, bool(obj.hide_render)) for obj in scene.objects]
    previous_collections = []
    target_collections = {collection.as_pointer() for obj in targets for collection in obj.users_collection}

    def reveal(collection):
        previous_collections.append((collection, bool(collection.hide_render)))
        descendant_target = collection.as_pointer() in target_collections
        for child in collection.children:
            descendant_target = reveal(child) or descendant_target
        if descendant_target:
            collection.hide_render = False
        return descendant_target

    reveal(scene.collection)
    for obj in scene.objects:
        if obj.as_pointer() in keep:
            obj.hide_render = False
        elif obj.type not in {"CAMERA", "LIGHT"}:
            obj.hide_render = True
    return previous_objects, previous_collections


def _restore_objects(previous, errors):
    if not previous:
        return
    objects, collections = previous
    for obj, hidden in objects:
        try:
            obj.hide_render = hidden
        except Exception as exc:
            errors.append(f"restore object {obj.name} visibility: {exc}")
    for collection, hidden in collections:
        try:
            collection.hide_render = hidden
        except Exception as exc:
            errors.append(f"restore collection {collection.name} visibility: {exc}")


def _finish_render_job(job, error=None):
    global _ACTIVE_RENDER_JOB
    bpy = job["bpy"]
    for handlers, callback in ((bpy.app.handlers.render_complete, job["complete"]),
                               (bpy.app.handlers.render_cancel, job["cancel"])):
        if callback in handlers:
            handlers.remove(callback)
    _ACTIVE_RENDER_JOB = None
    if error is not None:
        try:
            job["plan"].throw(error)
        except Exception as cleanup_error:
            if cleanup_error is not error:
                error.add_note(f"Render cleanup: {cleanup_error}")
        job["deferred"].reject(error)


def _advance_render_job(job, completed=False, cancelled=False):
    global _ACTIVE_RENDER_JOB
    if _ACTIVE_RENDER_JOB is not job:
        return None
    if completed and job.get("phase") != "rendering":
        return None
    if completed:
        job["phase"] = "idle"
        job["completion_queued"] = False
    if job.get("cancel_requested"):
        cancelled = True
    if cancelled:
        _finish_render_job(job, InvalidStateError("Blender render was cancelled"))
        return None
    try:
        if completed:
            path = job["path"]
            after = _file_signature(path)
            if after is None or after == job["before"]:
                raise InvalidStateError(f"Blender did not create render output: {path}")
        request = next(job["plan"])
        camera, path, width, height = request
        job["path"] = path
        job["before"] = _file_signature(path)
        job["phase"] = "rendering"
        job["completion_queued"] = False
        _configure_render(job["scene"], camera, path, width, height)
        bpy = job["bpy"]
        window = bpy.context.window
        if window is None:
            raise InvalidStateError("A Blender window is required for deferred rendering")
        with bpy.context.temp_override(window=window):
            outcome = bpy.ops.render.render("INVOKE_DEFAULT", write_still=True)
        if "RUNNING_MODAL" not in outcome and "FINISHED" not in outcome:
            raise InvalidStateError(f"Blender did not start render: {outcome}")
        if "FINISHED" in outcome and not job["completion_queued"]:
            job["completion_queued"] = True
            bpy.app.timers.register(lambda: _advance_render_job(job, completed=True), first_interval=0.01)
    except StopIteration as done:
        for handlers, callback in ((job["bpy"].app.handlers.render_complete, job["complete"]),
                                   (job["bpy"].app.handlers.render_cancel, job["cancel"])):
            if callback in handlers:
                handlers.remove(callback)
        _ACTIVE_RENDER_JOB = None
        job["deferred"].resolve(done.value)
    except Exception as exc:
        _finish_render_job(job, exc)
    return None


def _execute_render_plan(bpy, scene, plan):
    global _ACTIVE_RENDER_JOB
    if getattr(bpy.app, "background", False):
        while True:
            try:
                camera, path, width, height = next(plan)
            except StopIteration as done:
                return done.value
            try:
                _render_still(bpy, scene, camera, path, width, height)
            except Exception as exc:
                try:
                    plan.throw(exc)
                except Exception:
                    raise
                raise
    if not (active_context() or {}).get(DEFERRED_CONTEXT_KEY):
        raise InvalidStateError("GUI rendering requires bridge-managed deferred execution")
    if _ACTIVE_RENDER_JOB is not None:
        raise InvalidStateError("Another detail render is active; retry after it completes")
    deferred = DeferredToolResult()
    job = {"bpy": bpy, "scene": scene, "plan": plan, "deferred": deferred,
           "path": None, "phase": "idle", "cancel_requested": False, "completion_queued": False}

    def completed(*_):
        if job["phase"] == "rendering" and not job["completion_queued"]:
            job["completion_queued"] = True
            bpy.app.timers.register(lambda: _advance_render_job(job, completed=True), first_interval=0.05)

    def cancelled(*_):
        if job["phase"] == "rendering" and not job["completion_queued"]:
            job["completion_queued"] = True
            bpy.app.timers.register(lambda: _advance_render_job(job, cancelled=True), first_interval=0.05)

    job["complete"] = completed
    job["cancel"] = cancelled
    def request_cancel():
        job["cancel_requested"] = True
        if job["phase"] == "idle":
            bpy.app.timers.register(lambda: _advance_render_job(job, cancelled=True), first_interval=0.01)

    deferred.set_cancel_callback(request_cancel)
    try:
        bpy.app.handlers.render_complete.append(completed)
        bpy.app.handlers.render_cancel.append(cancelled)
        _ACTIVE_RENDER_JOB = job
        bpy.app.timers.register(lambda: _advance_render_job(job), first_interval=0.01)
    except Exception:
        for handlers, callback in ((bpy.app.handlers.render_complete, completed),
                                   (bpy.app.handlers.render_cancel, cancelled)):
            if callback in handlers:
                handlers.remove(callback)
        _ACTIVE_RENDER_JOB = None
        raise
    return deferred


def render_detail(params: dict[str, Any]) -> ToolResult | DeferredToolResult:
    """Render fixed bounds with optional named subjects for render isolation.

    Isolation changes render visibility temporarily; hide_set/viewport hiding
    does not affect renders. Bounds plus names keep framing independent of subjects.
    clip_to_bounds limits camera depth to the bounds without changing scene mesh.
    """
    params = normalize_render_options("detail", params)
    mode = view_mode(params)
    target_keys = {"object_name", "object_names", "point", "radius", "bounds"}
    width, height, overwrite, isolate = _detail_params(params, target_keys | {
        "direction", "projection", "isolate", "width", "height", "output_path", "overwrite", "allow_large", "view_mode",
        "clip_to_bounds",
    })
    clip_to_bounds = _boolean_param(params, "clip_to_bounds")
    if clip_to_bounds and "bounds" not in params:
        raise InvalidParamsError("clip_to_bounds requires explicit bounds")
    if mode == "depth" and (width < 128 or height < 48):
        raise InvalidParamsError("depth view requires at least 128x48 pixels for its numeric legend")
    projection = params.get("projection", "perspective")
    if projection not in {"perspective", "orthographic"}:
        raise InvalidParamsError("projection must be perspective or orthographic")
    direction = _detail_direction(params.get("direction"))
    bpy = get_bpy()
    scene = bpy.context.scene
    lighting_warnings = []
    minimum, maximum, objects = _detail_target_bounds(bpy, scene, params)
    output = _output_path(bpy, params.get("output_path"))
    _validate_overwrite(output, overwrite)
    output.parent.mkdir(parents=True, exist_ok=True)
    previous = _render_state(scene)
    previous_visibility = []
    camera = camera_data = None
    def plan():
        nonlocal camera, camera_data, previous_visibility
        error = None
        result = None
        restore_form = None
        try:
            if isolate:
                previous_visibility = _isolate_objects(scene, objects)
            if mode == "lit" and scene.render.engine != "BLENDER_WORKBENCH" and not any(
                    obj.type == "LIGHT" and not obj.hide_render for obj in scene.objects):
                lighting_warnings.append(
                    "No render-visible Light objects; lit output may be dark or black. Use view_mode:solid for a light-independent review."
                )
            camera, camera_data, receipt = _detail_camera(
                bpy, scene, (minimum, maximum), direction, width, height, projection,
                clip_to_bounds=clip_to_bounds,
            )
            depth = depth_range((minimum, maximum), camera, scene) if mode == "depth" else None
            restore_form = install_form_override(bpy, scene, mode, depth)
            yield camera, output, width, height
            if depth is not None:
                _add_depth_legend(bpy, output, depth)
            result = ToolResult(data={"path": str(output), "width": width, "height": height,
                                      "camera": receipt, "view_mode": mode,
                                      **({"depth_range": depth} if depth is not None else {})},
                                warnings=lighting_warnings)
        except Exception as exc:
            error = exc
        cleanup: list[str] = []
        if restore_form is not None:
            cleanup.extend(restore_form())
        try:
            _restore_render_state(scene, previous)
        except Exception as exc:
            cleanup.append(f"restore render state: {exc}")
        _restore_objects(previous_visibility, cleanup)
        _cleanup_camera(bpy, camera, camera_data, cleanup)
        _raise_after_cleanup(error, cleanup, "Detail rendered")
        return result

    return _execute_render_plan(bpy, scene, plan())


def render_ortho_set(params: dict[str, Any]) -> ToolResult | DeferredToolResult:
    """Render orthographic views; directory plus prefix uses deterministic names.

    With output_dir, output_prefix is a basename; alone it may include a path.
    Existing explicit-prefix outputs require overwrite=True.
    """
    params = normalize_render_options("ortho_set", params)
    mode = view_mode(params)
    target_keys = {"object_name", "object_names", "point", "radius", "bounds"}
    width, height, overwrite, isolate = _detail_params(params, target_keys | {
        "views", "shared_scale", "isolate", "width", "height", "output_dir", "output_prefix",
        "overwrite", "allow_large", "view_mode",
    })
    if mode == "depth" and (width < 128 or height < 48):
        raise InvalidParamsError("depth view requires at least 128x48 pixels for its numeric legend")
    views = params.get("views", list(ORTHO_VIEWS))
    if not isinstance(views, list) or not views or any(view not in ORTHO_VIEWS for view in views):
        raise InvalidParamsError("views must be a non-empty subset of front/back/left/right/top/bottom")
    if len(set(views)) != len(views):
        raise InvalidParamsError("views must not contain duplicates")
    shared_scale = params.get("shared_scale", True)
    if not isinstance(shared_scale, bool):
        raise InvalidParamsError("shared_scale must be a boolean")
    bpy = get_bpy()
    scene = bpy.context.scene
    minimum, maximum, objects = _target_bounds(bpy, scene, params)
    paths = _ortho_output_paths(bpy, params, views)
    for path in paths.values():
        _validate_overwrite(path, overwrite)
        path.parent.mkdir(parents=True, exist_ok=True)
    diameter = (maximum - minimum).length
    scale = max(diameter * max(1.0, width / height) * 1.15, 0.01) if shared_scale else None
    previous = _render_state(scene)
    previous_visibility = []

    def plan():
        nonlocal previous_visibility
        manifest = {}
        error = None
        try:
            if isolate:
                previous_visibility = _isolate_objects(scene, objects)
            for view in views:
                camera = camera_data = None
                view_error = None
                restore_form = None
                try:
                    camera, camera_data, receipt = _detail_camera(
                        bpy, scene, (minimum, maximum), _detail_direction(view), width, height,
                        "orthographic", scale,
                    )
                    depth = depth_range((minimum, maximum), camera, scene) if mode == "depth" else None
                    restore_form = install_form_override(bpy, scene, mode, depth)
                    yield camera, paths[view], width, height
                    if depth is not None:
                        _add_depth_legend(bpy, paths[view], depth)
                    manifest[view] = {**_manifest_entry(paths[view], camera.name, width, height),
                                      "camera_params": receipt, "view_mode": mode,
                                      **({"depth_range": depth} if depth is not None else {})}
                except Exception as exc:
                    view_error = exc
                cleanup: list[str] = []
                if restore_form is not None:
                    cleanup.extend(restore_form())
                try:
                    scene.camera = previous[0]
                except Exception as exc:
                    cleanup.append(f"restore scene camera: {exc}")
                _cleanup_camera(bpy, camera, camera_data, cleanup)
                _raise_after_cleanup(view_error, cleanup, "Orthographic view rendered")
        except Exception as exc:
            error = exc
        cleanup = []
        try:
            _restore_render_state(scene, previous)
        except Exception as exc:
            cleanup.append(f"restore render state: {exc}")
        _restore_objects(previous_visibility, cleanup)
        _raise_after_cleanup(error, cleanup, "Orthographic set rendered")
        return ToolResult(data={"views": views, "manifest": manifest,
                                "shared_scale": scale, "view_mode": mode})

    return _execute_render_plan(bpy, scene, plan())


def _image_pixels(image):
    pixels = array("f", [0.0]) * (int(image.size[0]) * int(image.size[1]) * 4)
    image.pixels.foreach_get(pixels)
    return pixels


def _save_pixels(bpy, name, pixels, width, height, path):
    image = bpy.data.images.new(name, width=width, height=height, alpha=True)
    try:
        image.pixels.foreach_set(pixels)
        image.update()
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
        if not path.is_file():
            raise InvalidStateError(f"Blender did not create image: {path}")
    finally:
        bpy.data.images.remove(image)


def _paint_depth_legend(pixels, width: int, height: int, depth: dict[str, Any]) -> None:
    """Burn a numeric, fixed-color depth key into one RGBA pixel buffer."""
    if width < 128 or height < 48:
        raise InvalidParamsError("depth view requires at least 128x48 pixels for its numeric legend")
    stops = ((0.0, (0.11, 0.20, 0.76)), (0.33, (0.03, 0.77, 0.88)),
             (0.67, (0.96, 0.83, 0.16)), (1.0, (0.84, 0.11, 0.10)))
    for y in range(0, 25):
        for x in range(width):
            offset = (y * width + x) * 4
            if y < 11:
                color = (0.02, 0.02, 0.02)
            elif y < 22:
                fraction = x / max(1, width - 1)
                left, right = stops[0], stops[-1]
                for a, b in zip(stops, stops[1:]):
                    if a[0] <= fraction <= b[0]:
                        left, right = a, b
                        break
                blend = (fraction - left[0]) / (right[0] - left[0])
                color = tuple(left[1][c] * (1 - blend) + right[1][c] * blend for c in range(3))
            else:
                color = (0.02, 0.02, 0.02)
            pixels[offset:offset + 4] = array("f", (*color, 1.0))
    near_label = f"{depth['near']:.3g} BU"
    far_label = f"{depth['far']:.3g} BU"
    _draw_label(pixels, width, height, 2, 2, near_label, len(near_label))
    _draw_label(pixels, width, height, max(2, width - 6 * len(far_label) - 2), 2,
                far_label, len(far_label))


def _add_depth_legend(bpy, path: Path, depth: dict[str, Any]) -> None:
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        width, height = map(int, image.size)
        pixels = _image_pixels(image)
        _paint_depth_legend(pixels, width, height, depth)
        _save_pixels(bpy, "CodexDepthWithLegend", pixels, width, height, path)
    finally:
        bpy.data.images.remove(image)


def _reference_outputs(bpy, params):
    prefix = _non_empty_string(params, "output_prefix")
    if prefix is None:
        root = Path(bpy.data.filepath).resolve().parent if bpy.data.filepath else Path(tempfile.gettempdir())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        prefix_path = root / "previews" / f"reference_{stamp}"
    else:
        prefix_path = Path(prefix).expanduser().resolve()
        if prefix_path.suffix.lower() == ".png":
            prefix_path = prefix_path.with_suffix("")
    return {key: prefix_path.parent / f"{prefix_path.name}_{key}.png" for key in ("render", "side_by_side", "overlay")}


def _camera_receipt(camera, projection=None):
    return {
        "location": list(camera.location), "rotation_euler": list(camera.rotation_euler),
        "projection": projection or ("orthographic" if camera.data.type == "ORTHO" else "perspective"),
        "lens": float(camera.data.lens), "ortho_scale": float(camera.data.ortho_scale),
    }


def _explicit_camera(bpy, scene, spec):
    if not isinstance(spec, dict) or not {"location", "rotation_euler"} <= set(spec):
        raise InvalidParamsError("camera requires location and rotation_euler")
    if set(spec) - {"location", "rotation_euler", "projection", "lens", "ortho_scale"}:
        raise InvalidParamsError("camera contains unknown fields")
    location = _vector3(spec["location"], "camera.location")
    rotation = _vector3(spec["rotation_euler"], "camera.rotation_euler")
    projection = spec.get("projection", "perspective")
    if projection not in {"perspective", "orthographic"}:
        raise InvalidParamsError("camera.projection must be perspective or orthographic")
    lens = _positive_number(spec.get("lens", 50), "camera.lens")
    ortho_scale = _positive_number(spec.get("ortho_scale", 6), "camera.ortho_scale")
    data = bpy.data.cameras.new("CodexReferenceCamera")
    camera = None
    try:
        camera = bpy.data.objects.new("CodexReferenceCamera", data)
        scene.collection.objects.link(camera)
        camera.location = location
        camera.rotation_euler = rotation
        data.type = "ORTHO" if projection == "orthographic" else "PERSP"
        data.lens = lens
        data.ortho_scale = ortho_scale
        return camera, data
    except Exception:
        _cleanup_camera(bpy, camera, data, [])
        raise


def _compose_reference(bpy, reference, rendered, paths, opacity, mode):
    width, height = map(int, reference.size)
    ref = _image_pixels(reference)
    image = _image_pixels(rendered)
    if tuple(rendered.size) != (width, height):
        raise InvalidStateError("Rendered image dimensions differ from reference")
    side = array("f", [0.0]) * (width * 2 * height * 4)
    overlay = array("f", [0.0]) * (width * height * 4)
    for y in range(height):
        source = y * width * 4
        dest = y * width * 8
        side[dest:dest + width * 4] = ref[source:source + width * 4]
        side[dest + width * 4:dest + width * 8] = image[source:source + width * 4]
    if mode == "blend":
        for i in range(0, len(overlay), 4):
            for channel in range(3):
                overlay[i + channel] = ref[i + channel] * opacity + image[i + channel] * (1 - opacity)
            overlay[i + 3] = 1.0
    else:
        for y in range(height):
            for x in range(width):
                i = (y * width + x) * 4
                right = (y * width + min(x + 1, width - 1)) * 4
                up = (min(y + 1, height - 1) * width + x) * 4
                edge = max(abs(ref[i + c] - ref[right + c]) + abs(ref[i + c] - ref[up + c])
                           for c in range(3)) > 0.2
                for c in range(3):
                    overlay[i + c] = (1 - opacity) * image[i + c] + opacity * (1.0 if c == 0 else 0.0) if edge else image[i + c]
                overlay[i + 3] = 1.0
    _save_pixels(bpy, "CodexReferenceSheet", side, width * 2, height, paths["side_by_side"])
    _save_pixels(bpy, "CodexReferenceOverlay", overlay, width, height, paths["overlay"])


def render_reference_match(params: dict[str, Any]) -> ToolResult | DeferredToolResult:
    params = normalize_render_options("reference_match", params)
    allowed = {"reference_path", "camera_name", "camera", "output_prefix", "opacity", "overlay_mode",
               "overwrite", "allow_large"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    reference_path = Path(_non_empty_string(params, "reference_path", required=True)).expanduser().resolve()
    if not reference_path.is_file():
        raise InvalidParamsError(f"Reference image does not exist: {reference_path}")
    if "camera" in params and "camera_name" in params:
        raise InvalidParamsError("Specify camera or camera_name, not both")
    opacity = params.get("opacity", 0.5)
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not isfinite(opacity) or not 0 <= opacity <= 1:
        raise InvalidParamsError("opacity must be a number from 0 to 1")
    mode = params.get("overlay_mode", "blend")
    if mode not in {"blend", "edges"}:
        raise InvalidParamsError("overlay_mode must be blend or edges")
    overwrite = _boolean_param(params, "overwrite")
    allow_large = _boolean_param(params, "allow_large")
    bpy = get_bpy()
    scene = bpy.context.scene
    paths = _reference_outputs(bpy, params)
    for path in paths.values():
        _validate_overwrite(path, overwrite)
        path.parent.mkdir(parents=True, exist_ok=True)
    reference = bpy.data.images.load(str(reference_path), check_existing=False)
    width, height = map(int, reference.size)
    try:
        if width <= 0 or height <= 0:
            raise InvalidParamsError("Reference image has invalid dimensions")
        _validate_pixel_budget(width * 2, height, allow_large)
    except Exception:
        bpy.data.images.remove(reference)
        raise
    previous = _render_state(scene)

    def plan():
        camera = camera_data = rendered = None
        error = None
        result = None
        try:
            if "camera" in params:
                camera, camera_data = _explicit_camera(bpy, scene, params["camera"])
            elif "camera_name" in params:
                name = _non_empty_string(params, "camera_name", required=True)
                camera = scene.objects.get(name)
                if camera is None or camera.type != "CAMERA":
                    raise InvalidParamsError(f"Camera not found in active scene: {name}")
            else:
                camera = scene.camera
                if camera is None:
                    raise InvalidParamsError("No scene camera; provide camera_name or camera")
            yield camera, paths["render"], width, height
            rendered = bpy.data.images.load(str(paths["render"]), check_existing=False)
            _compose_reference(bpy, reference, rendered, paths, float(opacity), mode)
            result = ToolResult(data={"render_path": str(paths["render"]),
                                      "side_by_side_path": str(paths["side_by_side"]),
                                      "overlay_path": str(paths["overlay"]),
                                      "width": width, "height": height,
                                      "camera": _camera_receipt(camera), "overlay_mode": mode})
        except Exception as exc:
            error = exc
        cleanup: list[str] = []
        try:
            _restore_render_state(scene, previous)
        except Exception as exc:
            cleanup.append(f"restore render state: {exc}")
        if rendered is not None:
            try:
                bpy.data.images.remove(rendered)
            except Exception as exc:
                cleanup.append(f"remove rendered image: {exc}")
        try:
            bpy.data.images.remove(reference)
        except Exception as exc:
            cleanup.append(f"remove reference image: {exc}")
        _cleanup_camera(bpy, camera if camera_data is not None else None, camera_data, cleanup)
        _raise_after_cleanup(error, cleanup, "Reference comparison rendered")
        return result

    try:
        return _execute_render_plan(bpy, scene, plan())
    except Exception:
        # A GUI startup failure occurs before the generator enters its cleanup block.
        if bpy.data.images.get(reference.name) is reference:
            bpy.data.images.remove(reference)
        raise


def preflight_render_options(mode: str, options: dict[str, Any], bpy: Any) -> dict[str, Any]:
    """Validate a child-render request on the live main thread before queuing it."""
    if not isinstance(options, dict):
        raise InvalidParamsError("options must be an object")
    params = normalize_render_options(mode, options)
    scene = bpy.context.scene
    target_keys = {"object_name", "object_names", "point", "radius", "bounds"}
    if mode == "preview":
        unknown = set(params) - {"width", "height", "output_path", "overwrite", "allow_large"}
        if unknown:
            raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        width, height = _dimension(params, "width", 960), _dimension(params, "height", 540)
        _validate_pixel_budget(width, height, _boolean_param(params, "allow_large"))
        _validate_overwrite(_output_path(bpy, params.get("output_path")), _boolean_param(params, "overwrite"))
    elif mode == "review_set":
        unknown = set(params) - {"output_dir", "output_prefix", "views", "width", "height", "overwrite",
                                 "allow_large", "hide", "show_only"}
        if unknown:
            raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        views = _review_views(params)
        width, height = _dimension(params, "width", 960), _dimension(params, "height", 540)
        _validate_pixel_budget(width, height, _boolean_param(params, "allow_large"))
        _collection_visibility_plan(bpy, _name_list_param(params, "hide"), _name_list_param(params, "show_only"))
        rendered = [view for view in views if view != "camera" or scene.camera is not None]
        for path in _review_output_paths(bpy, params, rendered).values():
            _validate_overwrite(path, _boolean_param(params, "overwrite"))
    elif mode == "detail":
        view_mode(params)
        width, height, overwrite, isolate = _detail_params(params, target_keys | {
            "direction", "projection", "isolate", "width", "height", "output_path", "overwrite", "allow_large", "view_mode", "clip_to_bounds"})
        if _boolean_param(params, "clip_to_bounds") and "bounds" not in params:
            raise InvalidParamsError("clip_to_bounds requires explicit bounds")
        if params.get("view_mode") == "depth" and (width < 128 or height < 48):
            raise InvalidParamsError("depth view requires at least 128x48 pixels for its numeric legend")
        if params.get("projection", "perspective") not in {"perspective", "orthographic"}:
            raise InvalidParamsError("projection must be perspective or orthographic")
        _detail_direction(params.get("direction"))
        _, _, objects = _detail_target_bounds(bpy, scene, params)
        if isolate and not objects:
            raise InvalidParamsError("isolate requires object_name or object_names")
        _validate_overwrite(_output_path(bpy, params.get("output_path")), overwrite)
    elif mode == "ortho_set":
        view_mode(params)
        width, height, overwrite, isolate = _detail_params(params, target_keys | {
            "views", "shared_scale", "isolate", "width", "height", "output_dir", "output_prefix",
            "overwrite", "allow_large", "view_mode"})
        if params.get("view_mode") == "depth" and (width < 128 or height < 48):
            raise InvalidParamsError("depth view requires at least 128x48 pixels for its numeric legend")
        views = params.get("views", list(ORTHO_VIEWS))
        if not isinstance(views, list) or not views or any(view not in ORTHO_VIEWS for view in views) or len(set(views)) != len(views):
            raise InvalidParamsError("views must be a unique non-empty subset of orthographic views")
        if not isinstance(params.get("shared_scale", True), bool):
            raise InvalidParamsError("shared_scale must be a boolean")
        _, _, objects = _target_bounds(bpy, scene, params)
        if isolate and not objects:
            raise InvalidParamsError("isolate requires object_name or object_names")
        for path in _ortho_output_paths(bpy, params, views).values():
            _validate_overwrite(path, overwrite)
    elif mode == "reference_match":
        unknown = set(params) - {"reference_path", "camera_name", "camera", "output_prefix", "opacity",
                                 "overlay_mode", "overwrite", "allow_large"}
        if unknown:
            raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        reference_path = Path(_non_empty_string(params, "reference_path", required=True)).expanduser().resolve()
        if not reference_path.is_file():
            raise InvalidParamsError(f"Reference image does not exist: {reference_path}")
        if "camera" in params and "camera_name" in params:
            raise InvalidParamsError("Specify camera or camera_name, not both")
        if "camera" in params:
            spec = params["camera"]
            if not isinstance(spec, dict) or not {"location", "rotation_euler"} <= set(spec) or set(spec) - {
                    "location", "rotation_euler", "projection", "lens", "ortho_scale"}:
                raise InvalidParamsError("camera requires location and rotation_euler without unknown fields")
            _vector3(spec["location"], "camera.location")
            _vector3(spec["rotation_euler"], "camera.rotation_euler")
            if spec.get("projection", "perspective") not in {"perspective", "orthographic"}:
                raise InvalidParamsError("camera.projection must be perspective or orthographic")
            _positive_number(spec.get("lens", 50), "camera.lens")
            _positive_number(spec.get("ortho_scale", 6), "camera.ortho_scale")
        elif "camera_name" in params:
            name = _non_empty_string(params, "camera_name", required=True)
            camera = scene.objects.get(name)
            if camera is None or camera.type != "CAMERA":
                raise InvalidParamsError(f"Camera not found in active scene: {name}")
        elif scene.camera is None:
            raise InvalidParamsError("No scene camera; provide camera_name or camera")
        opacity = params.get("opacity", 0.5)
        if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not isfinite(opacity) or not 0 <= opacity <= 1:
            raise InvalidParamsError("opacity must be a number from 0 to 1")
        if params.get("overlay_mode", "blend") not in {"blend", "edges"}:
            raise InvalidParamsError("overlay_mode must be blend or edges")
        paths = _reference_outputs(bpy, params)
        for path in paths.values():
            _validate_overwrite(path, _boolean_param(params, "overwrite"))
        reference = bpy.data.images.load(str(reference_path), check_existing=False)
        try:
            width, height = map(int, reference.size)
            if width <= 0 or height <= 0:
                raise InvalidParamsError("Reference image has invalid dimensions")
            _validate_pixel_budget(width * 2, height, _boolean_param(params, "allow_large"))
        finally:
            bpy.data.images.remove(reference)
    else:
        raise InvalidParamsError(f"Unsupported background render mode: {mode}")
    return params


_LABEL_GLYPHS = {
    "A": ("01110","10001","10001","11111","10001","10001","10001"),
    "B": ("11110","10001","10001","11110","10001","10001","11110"),
    "C": ("01111","10000","10000","10000","10000","10000","01111"),
    "D": ("11110","10001","10001","10001","10001","10001","11110"),
    "E": ("11111","10000","10000","11110","10000","10000","11111"),
    "F": ("11111","10000","10000","11110","10000","10000","10000"),
    "G": ("01111","10000","10000","10111","10001","10001","01111"),
    "H": ("10001","10001","10001","11111","10001","10001","10001"),
    "I": ("11111","00100","00100","00100","00100","00100","11111"),
    "J": ("00111","00010","00010","00010","10010","10010","01100"),
    "K": ("10001","10010","10100","11000","10100","10010","10001"),
    "L": ("10000","10000","10000","10000","10000","10000","11111"),
    "M": ("10001","11011","10101","10101","10001","10001","10001"),
    "N": ("10001","11001","10101","10011","10001","10001","10001"),
    "O": ("01110","10001","10001","10001","10001","10001","01110"),
    "P": ("11110","10001","10001","11110","10000","10000","10000"),
    "Q": ("01110","10001","10001","10001","10101","10010","01101"),
    "R": ("11110","10001","10001","11110","10100","10010","10001"),
    "S": ("01111","10000","10000","01110","00001","00001","11110"),
    "T": ("11111","00100","00100","00100","00100","00100","00100"),
    "U": ("10001","10001","10001","10001","10001","10001","01110"),
    "V": ("10001","10001","10001","10001","10001","01010","00100"),
    "W": ("10001","10001","10001","10101","10101","10101","01010"),
    "X": ("10001","10001","01010","00100","01010","10001","10001"),
    "Y": ("10001","10001","01010","00100","00100","00100","00100"),
    "Z": ("11111","00001","00010","00100","01000","10000","11111"),
    "0": ("01110","10001","10011","10101","11001","10001","01110"),
    "1": ("00100","01100","00100","00100","00100","00100","01110"),
    "2": ("01110","10001","00001","00010","00100","01000","11111"),
    "3": ("11110","00001","00001","01110","00001","00001","11110"),
    "4": ("00010","00110","01010","10010","11111","00010","00010"),
    "5": ("11111","10000","10000","11110","00001","00001","11110"),
    "6": ("01110","10000","10000","11110","10001","10001","01110"),
    "7": ("11111","00001","00010","00100","01000","01000","01000"),
    "8": ("01110","10001","10001","01110","10001","10001","01110"),
    "9": ("01110","10001","10001","01111","00001","00001","01110"),
    "-": ("00000","00000","00000","11111","00000","00000","00000"),
    "_": ("00000","00000","00000","00000","00000","00000","11111"),
    ".": ("00000","00000","00000","00000","00000","00110","00110"),
    " ": ("00000",) * 7,
}


def _draw_label(pixels, width, height, x, y, text, max_chars):
    for char_index, char in enumerate(text.upper()[:max_chars]):
        glyph = _LABEL_GLYPHS.get(char, _LABEL_GLYPHS[" "])
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1" and x + char_index * 6 + col < width and y + row < height:
                    # Blender image pixels start at the bottom; glyph rows start at the top.
                    offset = ((y + 6 - row) * width + x + char_index * 6 + col) * 4
                    pixels[offset:offset + 4] = array("f", (1, 1, 1, 1))


def render_contact_sheet(params: dict[str, Any]) -> ToolResult:
    allowed = {"images", "output_path", "columns", "overwrite", "allow_large", "view_mode"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    mode = view_mode(params)
    entries = params.get("images")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        raise InvalidParamsError("images must contain 1 to 16 PNG entries")
    columns = params.get("columns", min(3, len(entries)))
    if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= len(entries):
        raise InvalidParamsError("columns must be an integer from 1 to the image count")
    bpy = get_bpy()
    output = _output_path(bpy, _non_empty_string(params, "output_path", required=True))
    _validate_overwrite(output, _boolean_param(params, "overwrite"))
    loaded = []
    try:
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) - {"path", "label"} or "path" not in entry:
                raise InvalidParamsError(f"images[{index}] must contain path and optional label")
            supplied = entry["path"]
            if not isinstance(supplied, str) or not supplied.strip():
                raise InvalidParamsError(f"images[{index}].path must be a non-empty string")
            path = Path(supplied).expanduser().resolve()
            if path.suffix.lower() != ".png" or not path.is_file():
                raise InvalidParamsError(f"images[{index}].path must be an existing PNG")
            if path == output:
                raise InvalidParamsError("Contact sheet must not overwrite an input image")
            label = entry.get("label", path.stem if mode == "lit" else f"{mode} {path.stem}")
            if not isinstance(label, str) or not label.strip():
                raise InvalidParamsError(f"images[{index}].label must be a non-empty string")
            image = bpy.data.images.load(str(path), check_existing=False)
            loaded.append((image, label))
        cell_width = max(int(image.size[0]) for image, _ in loaded)
        cell_height = max(int(image.size[1]) for image, _ in loaded) + 18
        rows = (len(loaded) + columns - 1) // columns
        width, height = cell_width * columns, cell_height * rows
        _validate_pixel_budget(width, height, _boolean_param(params, "allow_large"))
        pixels = array("f", [0.0]) * (width * height * 4)
        for index, (image, label) in enumerate(loaded):
            iw, ih = map(int, image.size)
            source = _image_pixels(image)
            column, row = index % columns, index // columns
            base_x = column * cell_width
            base_y = (rows - row - 1) * cell_height
            for y in range(ih):
                dest = ((base_y + 18 + y) * width + base_x) * 4
                start = y * iw * 4
                pixels[dest:dest + iw * 4] = source[start:start + iw * 4]
            _draw_label(pixels, width, height, base_x + 3, base_y + 5, label,
                        max(1, (cell_width - 6) // 6))
        output.parent.mkdir(parents=True, exist_ok=True)
        _save_pixels(bpy, "CodexContactSheet", pixels, width, height, output)
        return ToolResult(data={"path": str(output), "width": width, "height": height,
                                "images": len(loaded), "columns": columns, "view_mode": mode})
    finally:
        for image, _ in reversed(loaded):
            bpy.data.images.remove(image)


def _compare_rgba_pixels(
    pixels_a: Sequence[float],
    pixels_b: Sequence[float],
    width: int,
    height: int,
    threshold: int,
) -> dict[str, Any]:
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise InvalidParamsError("width must be a positive integer")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise InvalidParamsError("height must be a positive integer")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 255:
        raise InvalidParamsError("threshold must be an integer from 0 to 255")
    expected = width * height * 4
    if len(pixels_a) != expected or len(pixels_b) != expected:
        raise InvalidParamsError(
            f"RGBA pixel arrays must each contain {expected} values for {width}x{height}; "
            f"got {len(pixels_a)} and {len(pixels_b)}"
        )

    changed = 0
    total_delta = 0.0
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    for pixel_index in range(width * height):
        offset = pixel_index * 4
        deltas = [abs(float(pixels_a[offset + channel]) - float(pixels_b[offset + channel])) * 255.0 for channel in range(3)]
        total_delta += sum(deltas)
        if max(deltas) > threshold:
            changed += 1
            x = pixel_index % width
            y = pixel_index // width
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)

    total_pixels = width * height
    return {
        "width": width,
        "height": height,
        "total_pixels": total_pixels,
        "changed_pixels": changed,
        "changed_fraction": changed / total_pixels,
        "mean_abs_rgb_delta": total_delta / (total_pixels * 3),
        "bbox": [min_x, min_y, max_x, max_y] if changed else None,
    }


def _diff_pixels(pixels_a: Sequence[float], pixels_b: Sequence[float], threshold: int) -> list[float]:
    output: list[float] = []
    for offset in range(0, len(pixels_a), 4):
        deltas = [abs(float(pixels_a[offset + channel]) - float(pixels_b[offset + channel])) * 255.0 for channel in range(3)]
        if max(deltas) > threshold:
            output.extend((1.0, 0.0, 0.0, 1.0))
        else:
            output.extend(tuple(max(0.0, min(1.0, float(pixels_b[offset + channel]))) * 0.25 for channel in range(3)))
            output.append(1.0)
    return output


def _png_input_path(params: dict[str, Any], name: str) -> Path:
    supplied = _non_empty_string(params, name, required=True)
    path = Path(supplied).expanduser().resolve()
    if path.suffix.lower() != ".png":
        raise InvalidParamsError(f"{name} must name a PNG file: {path}")
    if not path.is_file():
        raise InvalidParamsError(f"{name} PNG does not exist: {path}")
    return path


def _diff_output_path(image_b: Path, supplied: str | None) -> Path:
    if supplied is None:
        return image_b.with_name(f"{image_b.stem}_diff.png")
    path = Path(supplied).expanduser()
    if path.suffix.lower() != ".png":
        path = path.with_suffix(".png")
    return path.resolve()


def render_compare(params: dict[str, Any]) -> dict[str, Any]:
    allowed = {"image_a", "image_b", "threshold", "write_diff", "diff_output_path", "overwrite"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    image_a_path = _png_input_path(params, "image_a")
    image_b_path = _png_input_path(params, "image_b")
    threshold = _threshold(params)
    write_diff = _boolean_param(params, "write_diff")
    overwrite = _boolean_param(params, "overwrite")
    supplied_diff = _non_empty_string(params, "diff_output_path")
    diff_path = _diff_output_path(image_b_path, supplied_diff) if write_diff else None
    if diff_path is not None:
        if diff_path in {image_a_path, image_b_path}:
            raise InvalidParamsError("diff_output_path must not overwrite image_a or image_b")
        _validate_overwrite(diff_path, overwrite)
        diff_path.parent.mkdir(parents=True, exist_ok=True)

    bpy = get_bpy()
    loaded_images = []
    generated_image = None
    primary_error = None
    result_data = None
    try:
        for label, path in (("image_a", image_a_path), ("image_b", image_b_path)):
            try:
                image = bpy.data.images.load(str(path), check_existing=False)
            except Exception as exc:
                raise InvalidParamsError(f"{label} is not a readable PNG: {path}: {exc}") from exc
            loaded_images.append(image)
            if image.file_format != "PNG":
                raise InvalidParamsError(f"{label} is not a readable PNG: {path}")
        image_a, image_b = loaded_images
        dimensions_a = (int(image_a.size[0]), int(image_a.size[1]))
        dimensions_b = (int(image_b.size[0]), int(image_b.size[1]))
        if dimensions_a != dimensions_b:
            raise InvalidParamsError(
                f"Image dimensions differ: image_a is {dimensions_a[0]}x{dimensions_a[1]}, "
                f"image_b is {dimensions_b[0]}x{dimensions_b[1]}"
            )
        pixels_a = list(image_a.pixels[:])
        pixels_b = list(image_b.pixels[:])
        result_data = _compare_rgba_pixels(
            pixels_a, pixels_b, dimensions_a[0], dimensions_a[1], threshold
        )
        if diff_path is not None:
            generated_image = bpy.data.images.new(
                "CodexRenderDiff", width=dimensions_a[0], height=dimensions_a[1], alpha=True
            )
            generated_image.pixels.foreach_set(_diff_pixels(pixels_a, pixels_b, threshold))
            generated_image.update()
            generated_image.filepath_raw = str(diff_path)
            generated_image.file_format = "PNG"
            generated_image.save()
            if not diff_path.is_file():
                raise InvalidStateError(f"Blender did not create diff image: {diff_path}")
            result_data["diff_path"] = str(diff_path)
    except Exception as exc:
        primary_error = exc

    cleanup_errors: list[str] = []
    if generated_image is not None:
        try:
            bpy.data.images.remove(generated_image)
        except Exception as exc:
            cleanup_errors.append(f"remove generated diff image: {exc}")
    for image in reversed(loaded_images):
        try:
            bpy.data.images.remove(image)
        except Exception as exc:
            cleanup_errors.append(f"remove loaded image {image.name}: {exc}")
    _raise_after_cleanup(primary_error, cleanup_errors, "Images compared")
    return result_data
