"""Structured scene inspection."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
from pathlib import Path
from time import perf_counter
import traceback
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import (
    DEFERRED_CONTEXT_KEY,
    DeferredToolResult,
    ToolResult,
    active_context,
)
from ._blender import get_bpy, vector_values
from .render import (
    DETAIL_DIRECTIONS, _dimension, _detail_direction, _draw_label, _output_path,
    _paint_depth_legend,
    _target_bounds, _validate_overwrite, _validate_pixel_budget,
)
from .form_views import install_form_override, view_mode
from .session_state import current_state_token
from .versioning import file_state


_SCREENSHOT_DRAW_TIMEOUT_SECONDS = 10.0
_ACTIVE_SCREENSHOT_JOB: "_ViewportScreenshotJob | None" = None


def _collection_tree(collection) -> dict[str, Any]:
    return {
        "name": collection.name,
        "object_count": len(collection.objects),
        "children": [_collection_tree(child) for child in collection.children],
    }


def _object_detail(obj) -> dict[str, Any]:
    return {
        "name": obj.name,
        "type": obj.type,
        "transform": {
            "location": vector_values(obj.location),
            "rotation_euler": vector_values(obj.rotation_euler),
            "scale": vector_values(obj.scale),
        },
        "dimensions": vector_values(obj.dimensions),
        "modifiers": [modifier.name for modifier in obj.modifiers],
        "constraints": [constraint.name for constraint in obj.constraints],
        "collections": sorted(collection.name for collection in obj.users_collection),
        "visible": bool(obj.visible_get()),
    }


def _pagination_params(params: dict[str, Any]) -> tuple[bool, int, int]:
    include_names = params.get("include_names", False)
    if not isinstance(include_names, bool):
        raise InvalidParamsError("include_names must be a boolean")

    limit = params.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise InvalidParamsError("limit must be an integer from 1 to 2000")

    cursor = params.get("cursor")
    if cursor is None:
        offset = 0
    elif (
        isinstance(cursor, str)
        and 1 <= len(cursor) <= 18
        and cursor.isascii()
        and cursor.isdecimal()
    ):
        offset = int(cursor)
    else:
        raise InvalidParamsError("cursor must be a non-negative decimal string returned by scene.inspect")
    return include_names, limit, offset


def _paginate_names(
    names: list[dict[str, str]], limit: int, offset: int
) -> tuple[list[dict[str, str]], str | None, bool]:
    page = names[offset : offset + limit]
    next_offset = offset + len(page)
    truncated = next_offset < len(names)
    return page, str(next_offset) if truncated else None, truncated


def inspect_scene(params: dict[str, Any]) -> ToolResult:
    bpy = get_bpy()
    unknown = sorted(set(params) - {"object_name", "include_names", "limit", "cursor"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    object_name = params.get("object_name")
    if object_name is not None and not isinstance(object_name, str):
        raise InvalidParamsError("object_name must be a string")
    include_names, limit, offset = _pagination_params(params)

    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    counts_by_type: dict[str, int] = {}
    for obj in scene.objects:
        counts_by_type[obj.type] = counts_by_type.get(obj.type, 0) + 1

    warnings: list[str] = []
    try:
        statistics = scene.statistics(view_layer)
    except Exception as exc:
        statistics = None
        warnings.append(f"Scene statistics unavailable: {exc}")

    document = file_state(bpy)
    data: dict[str, Any] = {
        "scene": scene.name,
        "filepath": str(bpy.data.filepath or ""),
        "saved": bool(bpy.data.filepath),
        "dirty": bool(bpy.data.is_dirty),
        "document": document,
        "state_token": current_state_token(bpy),
        "objects": {
            "total": len(scene.objects),
            "counts_by_type": dict(sorted(counts_by_type.items())),
        },
        "collections": _collection_tree(scene.collection),
        "selection": {
            "selected_count": len(bpy.context.selected_objects),
            "active": view_layer.objects.active.name if view_layer.objects.active else None,
        },
        "stats": {
            "summary": statistics,
            "collection_count": len(bpy.data.collections),
            "material_count": len(bpy.data.materials),
            "world": scene.world.name if scene.world else None,
            "camera": scene.camera.name if scene.camera else None,
            "frame_current": scene.frame_current,
        },
        "object_detail": None,
    }
    if include_names:
        names = sorted(
            ({"name": obj.name, "type": obj.type} for obj in scene.objects),
            key=lambda item: (item["name"], item["type"]),
        )
        page, next_cursor, truncated = _paginate_names(names, limit, offset)
        data.update(names=page, next_cursor=next_cursor, truncated=truncated)
    if object_name is not None:
        obj = scene.objects.get(object_name)
        if obj is None:
            raise NotFoundError(f"Object not found in current scene: {object_name}")
        data["object_detail"] = _object_detail(obj)
    return ToolResult(data=data, warnings=warnings)


def _active_viewport(bpy: Any) -> tuple[Any, Any, Any]:
    if bool(getattr(getattr(bpy, "app", None), "background", False)):
        raise InvalidStateError("no active 3D viewport available")
    context = bpy.context
    active_area = getattr(context, "area", None)
    areas = [active_area] if getattr(active_area, "type", None) == "VIEW_3D" else []
    screen = getattr(context, "screen", None)
    if screen is None:
        screen = getattr(getattr(context, "window", None), "screen", None)
    areas.extend(area for area in getattr(screen, "areas", ()) if getattr(area, "type", None) == "VIEW_3D" and area is not active_area)
    for area in areas:
        region = next((item for item in getattr(area, "regions", ()) if getattr(item, "type", None) == "WINDOW"), None)
        space = getattr(getattr(area, "spaces", None), "active", None)
        if region is not None and space is not None:
            return area, region, space
    raise InvalidStateError("no active 3D viewport available")


def _region_view_3d(bpy: Any, region: Any, space: Any) -> Any:
    """Return the RegionView3D associated with the selected viewport."""
    rv3d = getattr(space, "region_3d", None)
    if rv3d is None:
        rv3d = getattr(region, "data", None)
    if rv3d is None and getattr(bpy.context, "region", None) is region:
        rv3d = getattr(bpy.context, "region_data", None)
    if rv3d is None:
        raise InvalidStateError("active 3D viewport has no RegionView3D available")
    return rv3d


def _viewport_matrices(rv3d: Any, region: Any, width: int, height: int) -> tuple[Any, Any]:
    """Preserve the current view while adapting its projection to the output aspect."""
    view_matrix = rv3d.view_matrix
    try:
        projection_matrix = rv3d.window_matrix.copy()
    except AttributeError:
        projection_matrix = rv3d.window_matrix

    # RegionView3D.window_matrix is derived for the live region dimensions.  The
    # view matrix remains valid at any output resolution, but a different output
    # aspect needs its horizontal projection term adjusted to avoid stretching.
    region_width = int(region.width)
    region_height = int(region.height)
    if region_width > 0 and region_height > 0:
        aspect_scale = (region_width / region_height) / (width / height)
        try:
            projection_matrix[0][0] *= aspect_scale
        except (TypeError, AttributeError, IndexError):
            # Blender's mathutils.Matrix is mutable.  Keep this defensive path for
            # unusual API builds whose matrix proxy cannot be copied or assigned.
            # The documented unmodified matrix still renders correctly at the
            # viewport's native dimensions.
            projection_matrix = rv3d.window_matrix
    return view_matrix, projection_matrix


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParamsError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidParamsError(f"{label} must be a finite number")
    return number


def _object_orbit_target(obj: Any) -> list[float]:
    """Return the world-space center of an object's bounding box."""
    from mathutils import Vector

    corners = getattr(obj, "bound_box", None)
    matrix_world = getattr(obj, "matrix_world", None)
    if corners and matrix_world is not None:
        points = [matrix_world @ Vector(corner) for corner in corners]
        if points:
            center = sum(points, Vector((0.0, 0.0, 0.0))) / len(points)
            return [float(center.x), float(center.y), float(center.z)]
    location = getattr(matrix_world, "translation", None)
    if location is None:
        location = getattr(obj, "location", (0.0, 0.0, 0.0))
    return [float(location[0]), float(location[1]), float(location[2])]


def _default_orbit_distance(obj: Any | None) -> float:
    if obj is None:
        return 10.0
    dimensions = [abs(float(value)) for value in getattr(obj, "dimensions", ())]
    return max(max(dimensions, default=0.0) * 2.5, 1.0)


def _orbit_view_matrix(
    target: list[float], azimuth_degrees: float, elevation_degrees: float, distance: float
) -> Any:
    """Build a Blender view matrix without changing the visible viewport."""
    from mathutils import Matrix, Vector

    azimuth = math.radians(azimuth_degrees)
    elevation = math.radians(elevation_degrees)
    horizontal = distance * math.cos(elevation)
    eye = Vector(
        (
            target[0] + horizontal * math.cos(azimuth),
            target[1] + horizontal * math.sin(azimuth),
            target[2] + distance * math.sin(elevation),
        )
    )
    target_vector = Vector(target)
    rotation = (target_vector - eye).to_track_quat("-Z", "Y").to_matrix().to_4x4()
    camera_world = Matrix.Translation(eye) @ rotation
    return camera_world.inverted()


def _screenshot_view(
    bpy: Any, view: Any, current_view_matrix: Any
) -> tuple[Any, dict[str, Any]]:
    if view is None:
        view = {"mode": "current"}
    if not isinstance(view, dict):
        raise InvalidParamsError("view must be an object")
    allowed = {
        "mode",
        "object_name",
        "target",
        "azimuth_degrees",
        "elevation_degrees",
        "distance",
    }
    unknown = sorted(set(view) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown view parameters: {', '.join(unknown)}")

    mode = view.get("mode", "current")
    if mode not in {"current", "orbit"}:
        raise InvalidParamsError("view.mode must be 'current' or 'orbit'")
    orbit_fields = allowed - {"mode"}
    if mode == "current":
        supplied = sorted(set(view) & orbit_fields)
        if supplied:
            raise InvalidParamsError(
                "Orbit-only view parameters require view.mode='orbit': "
                + ", ".join(supplied)
            )
        return current_view_matrix, {"view_mode": "current"}

    object_name = view.get("object_name")
    if object_name is not None and (not isinstance(object_name, str) or not object_name):
        raise InvalidParamsError("view.object_name must be a non-empty string")
    explicit_target = view.get("target")
    if object_name is not None and explicit_target is not None:
        raise InvalidParamsError("view.object_name and view.target are mutually exclusive")

    obj = None
    if object_name is not None:
        obj = bpy.context.scene.objects.get(object_name)
        if obj is None:
            raise NotFoundError(f"Object not found in current scene: {object_name}")
        target = _object_orbit_target(obj)
    elif explicit_target is None:
        target = [0.0, 0.0, 0.0]
    else:
        if not isinstance(explicit_target, (list, tuple)) or len(explicit_target) != 3:
            raise InvalidParamsError("view.target must contain exactly three coordinates")
        target = [
            _finite_number(value, f"view.target[{index}]")
            for index, value in enumerate(explicit_target)
        ]

    azimuth = _finite_number(view.get("azimuth_degrees", 45.0), "view.azimuth_degrees")
    elevation = _finite_number(
        view.get("elevation_degrees", 30.0), "view.elevation_degrees"
    )
    if not -89.9 <= elevation <= 89.9:
        raise InvalidParamsError("view.elevation_degrees must be from -89.9 to 89.9")
    distance = _finite_number(
        view.get("distance", _default_orbit_distance(obj)), "view.distance"
    )
    if distance <= 0:
        raise InvalidParamsError("view.distance must be greater than zero")

    metadata: dict[str, Any] = {
        "view_mode": "orbit",
        "view_target": target,
        "azimuth_degrees": azimuth,
        "elevation_degrees": elevation,
        "distance": distance,
    }
    if object_name is not None:
        metadata["target_object"] = object_name
    return _orbit_view_matrix(target, azimuth, elevation, distance), metadata


def _draw_view3d(offscreen: Any, bpy: Any, space: Any, region: Any, view_matrix: Any, projection_matrix: Any) -> None:
    """Call Blender's documented keyword-only GPUOffScreen API."""
    offscreen.draw_view3d(
        bpy.context.scene,
        bpy.context.view_layer,
        space,
        region,
        view_matrix,
        projection_matrix,
        do_color_management=True,
    )


def _ubyte_rgba_to_pixels(buffer: Any, width: int, height: int) -> array:
    """Flatten Blender's UBYTE GPU buffer and normalize it for Image.pixels."""
    expected_size = width * height * 4
    # gpu.types.Buffer is commonly multi-dimensional. Blender's own GPU
    # readback example flattens it through the public ``dimensions`` property
    # before iterating; treating its buffer protocol as raw bytes can instead
    # reinterpret FLOAT storage and corrupt every pixel.
    if hasattr(buffer, "dimensions"):
        try:
            buffer.dimensions = expected_size
        except Exception as exc:
            raise InvalidStateError(f"Could not flatten GPU framebuffer: {exc}") from exc
    try:
        channel_count = len(buffer)
    except TypeError as exc:
        raise InvalidStateError("GPU framebuffer is not a sized channel buffer") from exc
    if channel_count != expected_size:
        raise InvalidStateError(
            f"GPU framebuffer returned {channel_count} channels; expected {expected_size} "
            f"for {width}x{height} RGBA"
        )
    try:
        return array("f", (channel / 255.0 for channel in buffer))
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidStateError(f"GPU framebuffer returned invalid UBYTE channels: {exc}") from exc


def _depth_capture_pixels(framebuffer: Any, projection_matrix: Any, width: int, height: int):
    """Colorize true GPU depth with a fixed ramp and a numeric camera-space key."""
    from mathutils import Vector

    raw = framebuffer.read_depth(0, 0, width, height)
    expected = width * height
    if hasattr(raw, "dimensions"):
        raw.dimensions = expected
    if len(raw) != expected:
        raise InvalidStateError(f"GPU depth framebuffer returned {len(raw)} values; expected {expected}")
    inverse = projection_matrix.inverted()
    distances = array("f", [0.0]) * expected
    valid = []
    for index, sample in enumerate(raw):
        value = float(sample)
        if not math.isfinite(value) or value >= 1.0 or value < 0.0:
            continue
        camera_point = inverse @ Vector((0.0, 0.0, 2.0 * value - 1.0, 1.0))
        if abs(camera_point.w) < 1e-12:
            continue
        distance = -camera_point.z / camera_point.w
        if math.isfinite(distance) and distance > 0:
            distances[index] = distance
            valid.append(distance)
    if not valid:
        raise InvalidStateError("Depth capture has no visible geometry")
    near, far = min(valid), max(valid)
    if far - near < 1e-9:
        far = near + max(1e-6, near * 1e-6)
    stops = ((0.0, (0.11, 0.20, 0.76)), (0.33, (0.03, 0.77, 0.88)),
             (0.67, (0.96, 0.83, 0.16)), (1.0, (0.84, 0.11, 0.10)))
    pixels = array("f", [0.0]) * (expected * 4)
    for index, distance in enumerate(distances):
        offset = index * 4
        if distance <= 0:
            pixels[offset:offset + 4] = array("f", (0.02, 0.02, 0.02, 1.0))
            continue
        fraction = max(0.0, min(1.0, (distance - near) / (far - near)))
        for left, right in zip(stops, stops[1:]):
            if left[0] <= fraction <= right[0]:
                blend = (fraction - left[0]) / (right[0] - left[0])
                color = tuple(left[1][channel] * (1 - blend) + right[1][channel] * blend
                              for channel in range(3))
                pixels[offset:offset + 4] = array("f", (*color, 1.0))
                break
    receipt = {"near": near, "far": far, "units": "Blender units"}
    _paint_depth_legend(pixels, width, height, receipt)
    return pixels, receipt


@dataclass
class _ViewportScreenshotJob:
    bpy: Any
    area: Any
    region: Any
    space: Any
    view_matrix: Any
    projection_matrix: Any
    output: Path
    width: int
    height: int
    shading_type: str | None
    view_info: dict[str, Any]
    deferred: DeferredToolResult
    phase: str = "waiting"
    handler: Any = None
    timeout_callback: Any = None
    finalize_callback: Any = None
    pixels: array | None = None
    error: Exception | None = None
    error_traceback: str = ""
    capturing: bool = False
    capture_views: list[tuple[str, Any, Any]] | None = None
    captured_pixels: list[array] | None = None
    columns: int = 1
    shading_override: str | None = None
    overlays_override: bool | None = None
    wireframe_override: bool | None = None
    started_at: float = 0.0
    draw_ms: float = 0.0
    png_write_ms: float = 0.0
    previous_view_settings: tuple[Any, Any, Any] | None = None
    view_mode: str = "lit"
    form_restore: Any = None
    depth_ranges: dict[str, dict[str, Any]] | None = None


def _restore_capture_view_settings(job: _ViewportScreenshotJob) -> list[str]:
    previous = job.previous_view_settings
    job.previous_view_settings = None
    if previous is None:
        return []
    errors = []
    if job.form_restore is not None:
        errors.extend(job.form_restore())
        job.form_restore = None
    shading, overlays, wireframe = previous
    for target, field, value in (
        (getattr(job.space, "shading", None), "type", shading),
        (getattr(job.space, "overlay", None), "show_overlays", overlays),
        (getattr(job.space, "overlay", None), "show_wireframes", wireframe),
    ):
        try:
            if target is not None and getattr(target, field) != value:
                setattr(target, field, value)
        except Exception as exc:
            errors.append(f"restore viewport {field}: {exc}")
    return errors


def _apply_capture_view_settings(job: _ViewportScreenshotJob) -> None:
    """Change RNA on the bridge timer, never while the GPU draw callback runs."""
    shading = getattr(job.space, "shading", None)
    overlay = getattr(job.space, "overlay", None)
    job.previous_view_settings = (
        getattr(shading, "type", None),
        getattr(overlay, "show_overlays", None),
        getattr(overlay, "show_wireframes", None),
    )
    if job.shading_override is not None and shading.type != job.shading_override:
        shading.type = job.shading_override
    if job.overlays_override is not None and overlay.show_overlays != job.overlays_override:
        overlay.show_overlays = job.overlays_override
    if job.wireframe_override is not None and overlay.show_wireframes != job.wireframe_override:
        overlay.show_wireframes = job.wireframe_override
    if job.view_mode in {"normal", "matcap", "curvature"}:
        job.form_restore = install_form_override(job.bpy, job.bpy.context.scene, job.view_mode)


def _remove_screenshot_handles(job: _ViewportScreenshotJob) -> list[str]:
    """Remove every registered UI callback independently."""
    errors: list[str] = []
    if job.handler is not None:
        handler = job.handler
        job.handler = None
        try:
            job.bpy.types.SpaceView3D.draw_handler_remove(handler, "WINDOW")
        except Exception as exc:
            errors.append(f"remove viewport draw handler: {exc}")
    timers = job.bpy.app.timers
    for attribute, label in (
        ("timeout_callback", "unregister screenshot timeout"),
        ("finalize_callback", "unregister screenshot finalizer"),
    ):
        callback = getattr(job, attribute)
        setattr(job, attribute, None)
        if callback is None:
            continue
        try:
            if timers.is_registered(callback):
                timers.unregister(callback)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return errors


def _clear_active_screenshot_job(job: _ViewportScreenshotJob) -> None:
    global _ACTIVE_SCREENSHOT_JOB
    if _ACTIVE_SCREENSHOT_JOB is job:
        _ACTIVE_SCREENSHOT_JOB = None


def _cancel_screenshot_job(job: _ViewportScreenshotJob) -> None:
    """Cancel UI resources after the bridge has already completed the waiter."""
    if job.phase == "finished":
        return
    job.phase = "finished"
    job.pixels = None
    job.captured_pixels = None
    cleanup_errors = _remove_screenshot_handles(job)
    cleanup_errors.extend(_restore_capture_view_settings(job))
    _clear_active_screenshot_job(job)
    if cleanup_errors:
        raise InvalidStateError(
            "Viewport screenshot cancellation cleanup failed: "
            + "; ".join(cleanup_errors)
        )


def _set_screenshot_error(
    job: _ViewportScreenshotJob, error: Exception, traceback_text: str
) -> None:
    if job.error is None:
        job.error = error
        job.error_traceback = traceback_text
    else:
        job.error.add_note(f"Additional screenshot error: {error}")


def _schedule_screenshot_finalizer(job: _ViewportScreenshotJob) -> None:
    if job.phase == "finished" or job.finalize_callback is not None:
        return

    def callback() -> None:
        job.finalize_callback = None
        _finalize_screenshot_job(job)
        return None

    job.finalize_callback = callback
    try:
        job.bpy.app.timers.register(callback, first_interval=0.0)
    except Exception as exc:
        job.finalize_callback = None
        _set_screenshot_error(job, exc, traceback.format_exc())
        cleanup_errors = _remove_screenshot_handles(job)
        cleanup_errors.extend(_restore_capture_view_settings(job))
        if cleanup_errors:
            job.error.add_note("Cleanup errors: " + "; ".join(cleanup_errors))
        job.phase = "finished"
        _clear_active_screenshot_job(job)
        job.deferred.reject(job.error, job.error_traceback)


def _screenshot_timeout_callback(job: _ViewportScreenshotJob) -> None:
    job.timeout_callback = None
    if job.phase == "waiting":
        _set_screenshot_error(
            job,
            InvalidStateError("Timed out waiting for the active 3D viewport to draw"),
            "Viewport screenshot draw-handler timeout",
        )
        job.phase = "captured"
        _schedule_screenshot_finalizer(job)
    return None


def _screenshot_draw_callback(job: _ViewportScreenshotJob) -> None:
    """Capture only inside a real View3D WINDOW draw context."""
    if job.phase != "waiting" or job.capturing:
        return

    # SpaceView3D draw handlers are registered per region type, but Blender's
    # global bpy.context can still expose the timer-dispatch area while the
    # handler runs.  Requiring bpy.context.area/region identity therefore drops
    # valid draws.  Entering this WINDOW handler is the GPU-context guarantee;
    # draw_view3d itself uses the explicitly captured target space and region.

    job.capturing = True
    cleanup_errors: list[str] = []
    draw_started = perf_counter()
    try:
        import gpu
        views = job.capture_views or [("current", job.view_matrix, job.projection_matrix)]
        pixels: list[array] = []
        depth_ranges = {}
        for label, view_matrix, projection_matrix in views:
            offscreen = gpu.types.GPUOffScreen(job.width, job.height)
            try:
                with offscreen.bind():
                    _draw_view3d(
                        offscreen, job.bpy, job.space, job.region,
                        view_matrix, projection_matrix,
                    )
                    framebuffer = gpu.state.active_framebuffer_get()
                    if framebuffer is None:
                        raise InvalidStateError("GPU offscreen did not expose an active framebuffer")
                    if job.view_mode == "depth":
                        captured, depth = _depth_capture_pixels(
                            framebuffer, projection_matrix, job.width, job.height,
                        )
                        depth_ranges[label] = depth
                    else:
                        buffer = framebuffer.read_color(
                            0, 0, job.width, job.height, 4, 0, "UBYTE"
                        )
                        captured = _ubyte_rgba_to_pixels(buffer, job.width, job.height)
                pixels.append(captured)
            finally:
                try:
                    offscreen.free()
                except Exception as exc:
                    cleanup_errors.append(f"free GPU offscreen buffer: {exc}")
        if job.capture_views is None:
            job.pixels = pixels[0]
        else:
            job.captured_pixels = pixels
        if depth_ranges:
            job.depth_ranges = depth_ranges
    except Exception as exc:
        _set_screenshot_error(job, exc, traceback.format_exc())
    finally:
        job.draw_ms = (perf_counter() - draw_started) * 1000.0
        job.capturing = False

    if cleanup_errors:
        if job.error is None:
            _set_screenshot_error(
                job,
                InvalidStateError("Viewport GPU cleanup failed: " + "; ".join(cleanup_errors)),
                "Viewport GPU cleanup failed after draw-handler capture",
            )
        else:
            job.error.add_note("Cleanup errors: " + "; ".join(cleanup_errors))
    job.phase = "captured"
    _schedule_screenshot_finalizer(job)


def _finalize_screenshot_job(job: _ViewportScreenshotJob) -> None:
    """Save captured CPU pixels outside the GPU draw callback and resolve MCP."""
    if job.phase == "finished":
        return
    image = None
    result: ToolResult | None = None
    if job.error is None:
        try:
            if job.capture_views is None:
                pixels = job.pixels
                output_width, output_height = job.width, job.height
            else:
                if job.captured_pixels is None:
                    raise InvalidStateError("Viewport draw completed without captured views")
                pixels, output_width, output_height = _compose_fast_capture(
                    job.captured_pixels, [view[0] for view in job.capture_views],
                    job.width, job.height, job.columns,
                )
            if pixels is None:
                raise InvalidStateError("Viewport draw completed without captured pixels")
            image = job.bpy.data.images.new(
                "CodexViewportScreenshot",
                width=output_width,
                height=output_height,
                alpha=True,
            )
            image.pixels.foreach_set(pixels)
            image.filepath_raw = str(job.output)
            image.file_format = "PNG"
            png_started = perf_counter()
            image.save()
            job.png_write_ms = (perf_counter() - png_started) * 1000.0
            if not job.output.is_file():
                raise InvalidStateError(
                    f"Blender did not create viewport screenshot: {job.output}"
                )
            data = {
                "path": str(job.output),
                "width": output_width,
                "height": output_height,
                "area_type": job.area.type,
                "shading_type": job.shading_type,
            }
            data.update(job.view_info)
            if job.view_mode != "lit" or job.capture_views is not None:
                data["view_mode"] = job.view_mode
            if job.depth_ranges:
                data["depth_ranges"] = job.depth_ranges
                if len(job.depth_ranges) == 1:
                    data["depth_range"] = next(iter(job.depth_ranges.values()))
            if job.started_at:
                data["timings_ms"] = {
                    "draw": round(job.draw_ms, 3),
                    "png_write": round(job.png_write_ms, 3),
                    "total": round((perf_counter() - job.started_at) * 1000.0, 3),
                }
            if job.capture_views is not None:
                data.update(
                    views=[view[0] for view in job.capture_views],
                    columns=job.columns,
                    cell_width=job.width,
                    cell_height=job.height,
                )
            result = ToolResult(data=data)
        except Exception as exc:
            _set_screenshot_error(job, exc, traceback.format_exc())

    cleanup_errors: list[str] = []
    if image is not None:
        try:
            job.bpy.data.images.remove(image)
        except Exception as exc:
            cleanup_errors.append(f"remove temporary screenshot image: {exc}")
    cleanup_errors.extend(_remove_screenshot_handles(job))
    cleanup_errors.extend(_restore_capture_view_settings(job))
    if cleanup_errors:
        if job.error is None:
            _set_screenshot_error(
                job,
                InvalidStateError(
                    "Viewport screenshot cleanup failed: " + "; ".join(cleanup_errors)
                ),
                "Viewport screenshot cleanup failed after capture",
            )
        else:
            job.error.add_note("Cleanup errors: " + "; ".join(cleanup_errors))

    job.phase = "finished"
    job.pixels = None
    job.captured_pixels = None
    _clear_active_screenshot_job(job)
    if job.error is not None:
        job.deferred.reject(job.error, job.error_traceback)
    else:
        assert result is not None
        job.deferred.resolve(result)


def _compose_fast_capture(
    captures: list[array], labels: list[str], width: int, height: int, columns: int
) -> tuple[array, int, int]:
    """Join draw readbacks in memory; one PNG write serves the whole call."""
    if not captures or len(captures) != len(labels) or not 1 <= columns <= len(captures):
        raise InvalidParamsError("Capture views and columns do not match")
    if len(captures) == 1:
        return captures[0], width, height
    rows = (len(captures) + columns - 1) // columns
    cell_height = height + 18
    sheet_width, sheet_height = width * columns, cell_height * rows
    pixels = array("f", [0.0]) * (sheet_width * sheet_height * 4)
    for index, (capture, label) in enumerate(zip(captures, labels)):
        if len(capture) != width * height * 4:
            raise InvalidStateError(f"Capture {index} has an unexpected pixel count")
        column, row = index % columns, index // columns
        base_x = column * width
        base_y = (rows - row - 1) * cell_height
        for y in range(height):
            source = y * width * 4
            target = ((base_y + 18 + y) * sheet_width + base_x) * 4
            pixels[target:target + width * 4] = capture[source:source + width * 4]
        _draw_label(pixels, sheet_width, sheet_height, base_x + 3, base_y + 5,
                    label, max(1, (width - 6) // 6))
    return pixels, sheet_width, sheet_height


def _fast_capture_matrix(bounds: tuple[Any, Any], direction: Any, width: int,
                         height: int, projection: str) -> tuple[Any, Any, dict[str, Any]]:
    """Fit world bounds in an offscreen projection without changing the viewport."""
    from mathutils import Matrix, Vector

    minimum, maximum = bounds
    center = (minimum + maximum) * 0.5
    corners = [Vector((x, y, z)) for x in (minimum.x, maximum.x)
               for y in (minimum.y, maximum.y) for z in (minimum.z, maximum.z)]
    direction = Vector(direction).normalized()
    rotation = (-direction).to_track_quat("-Z", "Y").to_matrix().to_4x4()
    inverse_rotation = rotation.inverted().to_3x3()
    local = [inverse_rotation @ (point - center) for point in corners]
    half_x = max(abs(point.x) for point in local)
    half_y = max(abs(point.y) for point in local)
    half_depth = max(abs(point.z) for point in local)
    aspect = width / height
    if projection == "orthographic":
        half_h = max(half_y, half_x / aspect, 0.005) * 1.15
        half_w = half_h * aspect
        distance = max(half_depth * 3, 1.0)
    else:
        half_fov_y = math.radians(50.0) / 2
        half_fov_x = math.atan(math.tan(half_fov_y) * aspect)
        distance = max(half_x / math.tan(half_fov_x),
                       half_y / math.tan(half_fov_y), 0.1) * 1.2 + half_depth
    eye = center + direction * distance
    view = (Matrix.Translation(eye) @ rotation).inverted()
    near = max(0.001, distance - half_depth * 2.0)
    far = max(near + 1.0, distance + half_depth * 4.0, 1000.0)
    if projection == "orthographic":
        projection_matrix = Matrix(((1 / half_w, 0, 0, 0),
                                    (0, 1 / half_h, 0, 0),
                                    (0, 0, -2 / (far - near), -(far + near) / (far - near)),
                                    (0, 0, 0, 1)))
    else:
        y = 1 / math.tan(half_fov_y)
        projection_matrix = Matrix(((y / aspect, 0, 0, 0),
                                    (0, y, 0, 0),
                                    (0, 0, -(far + near) / (far - near),
                                     -2 * far * near / (far - near)),
                                    (0, 0, -1, 0)))
    return view, projection_matrix, {
        "target_center": list(center), "direction": list(direction),
        "projection": projection,
    }


def fast_capture_scene(params: dict[str, Any]) -> DeferredToolResult:
    """Capture one or more framed viewport views in one deferred draw callback."""
    global _ACTIVE_SCREENSHOT_JOB
    allowed = {"output_path", "overwrite", "width", "height", "object_name",
               "object_names", "point", "radius", "bounds", "views", "projection",
               "shading", "overlays", "wireframe", "columns", "view_mode"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    bpy = get_bpy()
    area, region, space = _active_viewport(bpy)
    width = _dimension(params, "width", 512)
    height = _dimension(params, "height", 512)
    mode = view_mode(params)
    if mode == "depth" and (width < 128 or height < 48):
        raise InvalidParamsError("depth view requires at least 128x48 pixels for its numeric legend")
    selectors = [key for key in ("object_name", "object_names", "point", "bounds") if key in params]
    if len(selectors) > 1:
        raise InvalidParamsError("Provide at most one target selector")
    if "radius" in params and "point" not in params:
        raise InvalidParamsError("radius requires point")
    target = None
    if selectors:
        target_params = {key: params[key] for key in selectors}
        if "radius" in params:
            target_params["radius"] = params["radius"]
        minimum, maximum, _objects = _target_bounds(bpy, bpy.context.scene, target_params)
        target = (minimum, maximum)
    views = params.get("views", ["three_quarter"] if target else ["current"])
    if not isinstance(views, list) or not 1 <= len(views) <= 6 or any(
        not isinstance(view, str) or view not in {"current", *DETAIL_DIRECTIONS}
        for view in views
    ):
        raise InvalidParamsError("views must contain 1 to 6 named viewport directions")
    if len(set(views)) != len(views):
        raise InvalidParamsError("views must not contain duplicates")
    if target is None and any(view != "current" for view in views):
        raise InvalidParamsError("Named direction views require a target")
    if target is not None and "current" in views:
        raise InvalidParamsError("A target requires named direction views")
    projection = params.get("projection", "perspective")
    if projection not in {"perspective", "orthographic"}:
        raise InvalidParamsError("projection must be perspective or orthographic")
    shading = params.get("shading", "current")
    if shading not in {"current", "solid", "material"}:
        raise InvalidParamsError("shading must be current, solid, or material")
    for key in ("overlays", "wireframe", "overwrite"):
        if key in params and not isinstance(params[key], bool):
            raise InvalidParamsError(f"{key} must be a boolean")
    columns = params.get("columns", min(3, len(views)))
    if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= len(views):
        raise InvalidParamsError("columns must be an integer from 1 to view count")
    sheet_width = width * columns
    sheet_height = (height + (18 if len(views) > 1 else 0)) * ((len(views) + columns - 1) // columns)
    _validate_pixel_budget(sheet_width, sheet_height, False)
    output = _output_path(bpy, params.get("output_path"))
    _validate_overwrite(output, params.get("overwrite", False))
    if not (active_context() or {}).get(DEFERRED_CONTEXT_KEY):
        raise InvalidStateError("scene.fast_capture requires bridge-managed deferred UI draw execution")
    if _ACTIVE_SCREENSHOT_JOB is not None:
        raise InvalidStateError("A viewport capture is active; retry after it completes")
    rv3d = _region_view_3d(bpy, region, space)
    current_view, current_projection = _viewport_matrices(rv3d, region, width, height)
    capture_views = []
    receipts = []
    for name in views:
        if name == "current":
            capture_views.append((name, current_view, current_projection))
            receipts.append({"name": name, "projection": "current"})
        else:
            view_matrix, projection_matrix, info = _fast_capture_matrix(
                target, _detail_direction(name), width, height, projection,
            )
            capture_views.append((name, view_matrix, projection_matrix))
            receipts.append({"name": name, **info})
    output.parent.mkdir(parents=True, exist_ok=True)
    deferred = DeferredToolResult()
    job = _ViewportScreenshotJob(
        bpy=bpy, area=area, region=region, space=space,
        view_matrix=current_view, projection_matrix=current_projection,
        output=output, width=width, height=height,
        shading_type=("SOLID" if mode == "depth" else
                      "MATERIAL" if mode in {"normal", "matcap"} else
                      "RENDERED" if mode == "curvature" else
                      shading if shading != "current" else getattr(getattr(space, "shading", None), "type", None)),
        view_info={"view_receipts": receipts}, deferred=deferred,
        capture_views=capture_views, columns=columns,
        shading_override=("SOLID" if mode == "depth" else
                          "MATERIAL" if mode in {"normal", "matcap"} else
                          "RENDERED" if mode == "curvature" else
                          {"solid": "SOLID", "material": "MATERIAL"}.get(shading)),
        overlays_override=True if mode == "wireframe_on_shaded" else
                          False if mode != "lit" else params.get("overlays"),
        wireframe_override=(True if mode == "wireframe_on_shaded" else
                            False if mode != "lit" else params.get("wireframe")),
        started_at=perf_counter(),
        view_mode=mode,
    )
    deferred.set_cancel_callback(lambda: _cancel_screenshot_job(job))
    _ACTIVE_SCREENSHOT_JOB = job
    try:
        _apply_capture_view_settings(job)
        job.handler = bpy.types.SpaceView3D.draw_handler_add(
            _screenshot_draw_callback, (job,), "WINDOW", "POST_PIXEL"
        )
        job.timeout_callback = lambda: _screenshot_timeout_callback(job)
        bpy.app.timers.register(job.timeout_callback,
                                first_interval=_SCREENSHOT_DRAW_TIMEOUT_SECONDS)
        area.tag_redraw()
    except Exception:
        _cancel_screenshot_job(job)
        raise
    return deferred


def screenshot_scene(params: dict[str, Any]) -> DeferredToolResult:
    """Capture a current or independent orbit view in the next real draw cycle."""
    global _ACTIVE_SCREENSHOT_JOB
    allowed = {"output_path", "overwrite", "width", "height", "view"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    overwrite = params.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise InvalidParamsError("overwrite must be a boolean")
    bpy = get_bpy()
    area, region, space = _active_viewport(bpy)
    width = _dimension(params, "width", int(region.width))
    height = _dimension(params, "height", int(region.height))
    _validate_pixel_budget(width, height, False)
    output = _output_path(bpy, params.get("output_path"))
    _validate_overwrite(output, overwrite)
    context = active_context() or {}
    if not context.get(DEFERRED_CONTEXT_KEY):
        raise InvalidStateError(
            "scene.screenshot requires bridge-managed deferred UI draw execution"
        )
    if _ACTIVE_SCREENSHOT_JOB is not None:
        raise InvalidStateError(
            "scene.screenshot already has an active viewport draw job; retry after it completes"
        )

    rv3d = _region_view_3d(bpy, region, space)
    current_view_matrix, projection_matrix = _viewport_matrices(
        rv3d, region, width, height
    )
    view_matrix, view_info = _screenshot_view(
        bpy, params.get("view"), current_view_matrix
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    deferred = DeferredToolResult()
    job = _ViewportScreenshotJob(
        bpy=bpy,
        area=area,
        region=region,
        space=space,
        view_matrix=view_matrix,
        projection_matrix=projection_matrix,
        output=output,
        width=width,
        height=height,
        shading_type=getattr(getattr(space, "shading", None), "type", None),
        view_info=view_info,
        deferred=deferred,
        started_at=perf_counter(),
    )
    deferred.set_cancel_callback(lambda: _cancel_screenshot_job(job))
    _ACTIVE_SCREENSHOT_JOB = job
    try:
        job.handler = bpy.types.SpaceView3D.draw_handler_add(
            _screenshot_draw_callback, (job,), "WINDOW", "POST_PIXEL"
        )
        job.timeout_callback = lambda: _screenshot_timeout_callback(job)
        bpy.app.timers.register(
            job.timeout_callback, first_interval=_SCREENSHOT_DRAW_TIMEOUT_SECONDS
        )
        area.tag_redraw()
    except Exception:
        _cancel_screenshot_job(job)
        raise
    return deferred
