"""Bounded two-view silhouette fitting without rendering or temporary objects.

Horizontal cross-section envelopes are remapped; vertical coordinates and
topology are preserved. This is not reconstruction of hidden concavities.
"""

from __future__ import annotations

from typing import Any

from ..errors import InvalidParamsError, InvalidStateError
from ..registry import ToolResult
from . import reference_review as review


DESCRIPTION = (
    "Fit a selected mesh region to exactly two perpendicular orthographic front/side "
    "reference silhouettes sharing the same vertical axis. Each view names an existing "
    "camera and supplies a closed contour in top-left pixel coordinates with width/height, "
    "or a light-foreground mask_path. Targets must have one connected span per occupied "
    "row; holes, disjoint spans, perspective, camera shifts, visible modifiers and height "
    "changes are unsupported. Remaps horizontal cross-section envelopes using 257 height "
    "samples, scaled by region group weights; preserves vertical coordinates, topology and "
    "all unselected/pinned vertices. "
    "Uses world millimetre displacement bounds and shared symmetry/clearance constraints; "
    "conflicts and raster contour regression refuse before writes, never silently clamp. "
    "Reports both views' CPU-raster symmetric contour error and IoU before/after on final "
    "quantized coordinates. Dry run defaults true; applying requires dry_run=false and "
    "confirm=true. No render, camera, selection, material or output-file changes. "
    "This fits base mesh silhouettes, not anatomy or unseen concavities."
)

_COMMON = {"object_name", "region", "max_displacement_mm", "pinned_vertex_group",
           "symmetry", "clearance", "dry_run", "confirm", "expected_state"}
# Camera matrices pass through Blender's float32 Euler/quaternion conversion.
# Cardinal front/side fixtures differ by about 1.6e-7 in their vertical axes.
_CAMERA_AXIS_TOLERANCE = 5e-7
_SECTION_PIXEL_TOLERANCE = 1e-4


def _integer(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise InvalidParamsError(f"{name} must be an integer from {lo} to {hi}")
    return value


def _spans(points, rows):
    """Closed polygon intersections, including horizontal edges at extrema."""
    import numpy as np

    a, b = points, np.roll(points, -1, axis=0)
    out = np.full((len(rows), 2), np.nan)
    for i, row in enumerate(rows):
        delta = b[:, 1] - a[:, 1]
        active = (abs(delta) > 1e-12) & (row >= np.minimum(a[:, 1], b[:, 1]) - 1e-9) & (row <= np.maximum(a[:, 1], b[:, 1]) + 1e-9)
        x = a[active, 0] + (row - a[active, 1]) * (b[active, 0] - a[active, 0]) / delta[active]
        horizontal = (abs(delta) <= 1e-12) & (abs(row - a[:, 1]) <= 1e-9)
        x = np.r_[x, a[horizontal, 0], b[horizontal, 0]]
        if len(x):
            out[i] = x.min(), x.max()
    return out


def _polygon_mask(points, width, height):
    import numpy as np

    # Check every interval between polygon heights, not only raster rows: a
    # narrow disjoint span or concavity must not disappear through sampling.
    levels = np.unique(points[:, 1])
    a, b = points, np.roll(points, -1, axis=0)
    if np.any(np.linalg.norm(b - a, axis=1) < 1e-9):
        raise InvalidParamsError("Reference contour has duplicate consecutive vertices")
    # Non-adjacent segment intersections also reject touching/retraced loops.
    def cross(x, y):
        return x[..., 0] * y[..., 1] - x[..., 1] * y[..., 0]
    for i in range(len(points)):
        js = np.array([j for j in range(i + 1, len(points)) if j != i + 1 and not (i == 0 and j == len(points) - 1)])
        if not len(js):
            continue
        overlapping = np.all(np.maximum(np.minimum(a[i], b[i]), np.minimum(a[js], b[js])) <=
                             np.minimum(np.maximum(a[i], b[i]), np.maximum(a[js], b[js])) + 1e-10, axis=1)
        ab, cd = b[i] - a[i], b[js] - a[js]
        crossing = ((cross(ab, a[js] - a[i]) * cross(ab, b[js] - a[i]) <= 1e-12) &
                    (cross(cd, a[i] - a[js]) * cross(cd, b[i] - a[js]) <= 1e-12))
        if np.any(overlapping & crossing):
            raise InvalidParamsError("Reference contour must be simple without self-intersections")
    delta = b[:, 1] - a[:, 1]
    for row in (levels[:-1] + levels[1:]) * .5:
        active = (a[:, 1] > row) != (b[:, 1] > row)
        x = a[active, 0] + (row - a[active, 1]) * (b[active, 0] - a[active, 0]) / delta[active]
        if len(x) != 2 or abs(x[0] - x[1]) < 1e-9:
            raise InvalidParamsError("Each reference contour must have exactly one nonzero span per occupied row")
    spans = _spans(points, np.arange(height) + .5)
    x = np.arange(width) + .5
    return (x[None, :] >= spans[:, 0, None]) & (x[None, :] <= spans[:, 1, None])


def _read_view(bpy, item, index):
    import numpy as np

    if not isinstance(item, dict) or set(item) - {"camera_name", "width", "height", "contour", "mask_path", "threshold"}:
        raise InvalidParamsError(f"views[{index}] has invalid fields")
    if ("contour" in item) == ("mask_path" in item):
        raise InvalidParamsError("Each view requires exactly one of contour or mask_path")
    name = item.get("camera_name")
    if not isinstance(name, str) or not name:
        raise InvalidParamsError("Each view requires camera_name")
    camera = bpy.context.scene.objects.get(name)
    if camera is None or camera.type != "CAMERA" or camera.data.type != "ORTHO":
        raise InvalidParamsError("Contour fitting requires two existing orthographic cameras")
    if "contour" in item:
        if "threshold" in item:
            raise InvalidParamsError("threshold applies only to mask_path")
        width = _integer(item.get("width"), "width", 16, 512)
        height = _integer(item.get("height"), "height", 16, 512)
        raw = item["contour"]
        if not isinstance(raw, list) or not 3 <= len(raw) <= 1024:
            raise InvalidParamsError("contour must have 3 to 1024 ordered polygon vertices")
        points = np.array([review._vec(point, 2, "contour point") for point in raw])
        if np.array_equal(points[0], points[-1]):
            points = points[:-1]
        if len(points) < 3 or np.any(points < 0) or np.any(points[:, 0] > width) or np.any(points[:, 1] > height):
            raise InvalidParamsError("contour must be a nondegenerate polygon within image bounds")
        mask = _polygon_mask(points, width, height)
        target_rows = np.unique(points[:, 1])
        target_spans = _spans(points, target_rows)
        extent = (float(points[:, 1].min()), float(points[:, 1].max()))
    else:
        path = review._path(item["mask_path"], "mask_path", exists=True)
        image = review._image(bpy, path, "mask_path")
        try:
            width, height = map(int, image.size)
            _integer(width, "mask width", 16, 512)
            _integer(height, "mask height", 16, 512)
            for key, actual in (("width", width), ("height", height)):
                if key in item and _integer(item[key], key, 16, 512) != actual:
                    raise InvalidParamsError(f"{key} must match mask dimensions")
            mask = review._mask_from_image(review._pixels(image)[::-1], item, mask_image=True)
        finally:
            bpy.data.images.remove(image)
        occupied = np.flatnonzero(mask.any(axis=1))
        if not len(occupied) or not np.all(np.diff(occupied) == 1):
            raise InvalidParamsError("Reference mask must have connected occupied rows")
        target_rows = occupied.astype(float) + .5
        target_spans = np.array([(np.flatnonzero(mask[row])[0], np.flatnonzero(mask[row])[-1] + 1.) for row in occupied])
        extent = (float(occupied[0]), float(occupied[-1] + 1))
    occupied = np.flatnonzero(mask.any(axis=1))
    if len(occupied) < 2 or not np.all(np.diff(occupied) == 1):
        raise InvalidParamsError("Reference silhouette must occupy at least two connected rows")
    for row in occupied:
        xs = np.flatnonzero(mask[row])
        if not np.all(np.diff(xs) == 1):
            raise InvalidParamsError("Reference masks must have one span per row; holes/disjoint spans are unsupported")
    if mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any():
        raise InvalidParamsError("Reference silhouette must have a background margin on all image borders")
    projection = review._camera_projection(camera, width, height)
    rotation = projection[1]
    if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-7):
        raise InvalidParamsError("Camera transform must have orthogonal axes; shear is unsupported")
    contour = review._contour(mask)
    return {"camera_name": name, "width": width, "height": height, "mask": mask,
            "projection": projection, "target_rows": target_rows, "target_spans": target_spans,
            "extent": extent, "contour": contour, "distance": review._distance_transform(contour)[0]}


def _project(world, view):
    center, rotation, scale, ortho, near, far = view["projection"]
    return review._project_mesh_vertices(world, center, rotation, scale, ortho, view["width"], view["height"])


def _measure(world, triangles, view):
    import numpy as np

    uv, depths = _project(world, view)
    near, far = view["projection"][-2:]
    if np.any(depths <= near) or np.any(depths >= far):
        raise InvalidStateError("Mesh crosses a reference camera clipping plane")
    if np.any(uv < 1) or np.any(uv[:, 0] > view["width"] - 1) or np.any(uv[:, 1] > view["height"] - 1):
        raise InvalidStateError("Mesh silhouette must remain within the reference image background margin")
    zbuffer = np.full((view["height"], view["width"]), np.inf, dtype=np.float32)
    sizes = np.ceil(np.ptp(uv[triangles], axis=1)) + 2
    if float(np.prod(sizes, axis=1).sum()) > 40000000:
        raise InvalidStateError("Contour raster exceeds 40 million triangle-pixel work budget; reduce mesh or image size")
    for tri in triangles:
        review._raster_triangle(uv[tri], depths[tri], zbuffer)
    mask = np.isfinite(zbuffer)
    contour = review._contour(mask)
    if not contour.any():
        raise InvalidStateError("Mesh has no raster silhouette in a reference camera")
    to_model = review._distance_transform(contour)[0]
    stats = review._contour_stats(view["contour"], contour, view["distance"], to_model)
    return {"contour_distance_px": stats,
            "iou": float((mask & view["mask"]).sum() / (mask | view["mask"]).sum())}


def _cross_sections(uv, edges, rows):
    import numpy as np

    a, b = uv[edges[:, 0]], uv[edges[:, 1]]
    result = np.full((len(rows), 2), np.nan)
    delta = b[:, 1] - a[:, 1]
    for i, row in enumerate(rows):
        active = ((abs(delta) > _SECTION_PIXEL_TOLERANCE) &
                  (row >= np.minimum(a[:, 1], b[:, 1]) - _SECTION_PIXEL_TOLERANCE) &
                  (row <= np.maximum(a[:, 1], b[:, 1]) + _SECTION_PIXEL_TOLERANCE))
        # Include nearly horizontal edges at an extremum despite camera storage
        # noise. Clamp segment parameters only within this numerical tolerance;
        # user displacement/clearance constraints are never clamped.
        factor = np.clip((row - a[active, 1]) / delta[active], 0., 1.)
        x = a[active, 0] + factor * (b[active, 0] - a[active, 0])
        horizontal = ((abs(delta) <= _SECTION_PIXEL_TOLERANCE) &
                      (abs(row - a[:, 1]) <= _SECTION_PIXEL_TOLERANCE))
        x = np.r_[x, a[horizontal, 0], b[horizontal, 0]]
        if len(x):
            result[i] = x.min(), x.max()
    if not np.isfinite(result).all():
        raise InvalidStateError("Source mesh has disconnected vertical coverage; cross-section fitting is unsupported")
    return result


def _candidate(world, triangles, movable, views):
    """Solve perpendicular horizontal offsets; preserve the common vertical axis."""
    import numpy as np

    edges = np.unique(np.sort(np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]]), axis=1), axis=0)
    candidate = world.copy()
    indices = np.flatnonzero(movable)
    offsets = []
    for view in views:
        uv, _ = _project(world, view)
        low, high = float(uv[:, 1].min()), float(uv[:, 1].max())
        if high - low < 2 or max(abs(low - view["extent"][0]), abs(high - view["extent"][1])) > 1.01:
            raise InvalidParamsError("Reference and mesh vertical extents must agree within one pixel; height fitting is unsupported")
        # Include exact mesh heights for low-poly fixtures. The bounded sample
        # grid controls work for dense meshes; receipts disclose this sampling.
        rows = np.unique(np.r_[np.linspace(low, high, 257), uv[:, 1] if len(uv) <= 512 else []])
        source = _cross_sections(uv, edges, rows)
        rows_at = uv[indices, 1]
        lo = np.interp(rows_at, rows, source[:, 0])
        hi = np.interp(rows_at, rows, source[:, 1])
        target_lo = np.interp(rows_at, view["target_rows"], view["target_spans"][:, 0])
        target_hi = np.interp(rows_at, view["target_rows"], view["target_spans"][:, 1])
        width = hi - lo
        fraction = np.full(len(indices), .5)
        regular = width > 1e-8
        fraction[regular] = (uv[indices[regular], 0] - lo[regular]) / width[regular]
        if np.any((fraction < -1e-4) | (fraction > 1.0001)):
            raise InvalidStateError("Cross-section sampling cannot bound this geometry; simplify or split the selected region")
        target_x = target_lo + fraction * (target_hi - target_lo)
        offset = (target_x - uv[indices, 0]) / view["projection"][2]
        offsets.append(offset)
    # Solve both projected offsets together instead of assuming stored camera
    # axes are mathematically perpendicular. Preserve the first view's vertical.
    basis = np.stack((views[0]["projection"][1][0], views[1]["projection"][1][0], views[0]["projection"][1][1]))
    candidate[indices] += np.linalg.solve(basis, np.stack((*offsets, np.zeros(len(indices))))).T
    return candidate


def fit_contours(params: dict[str, Any]) -> ToolResult:
    import numpy as np
    from .goal_deform import apply_candidate, read_context

    unknown = set(params) - _COMMON - {"views"}
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    supplied = params.get("views")
    if not isinstance(supplied, list) or len(supplied) != 2:
        raise InvalidParamsError("views must contain exactly two front/side references")
    common = {key: value for key, value in params.items() if key in _COMMON}
    context = read_context(common)
    obj, bpy = context["obj"], context["bpy"]
    if any(mod.show_viewport for mod in obj.modifiers):
        raise InvalidStateError("Contour fitting requires a base mesh with no visible modifiers")
    world = context["world"] / context["unit_scale"]
    triangles = context["triangles"]
    if len(world) > 20000 or len(triangles) > 40000 or not len(triangles):
        raise InvalidParamsError("Contour fitting supports 1 to 40000 triangles and at most 20000 vertices")
    views = [_read_view(bpy, item, i) for i, item in enumerate(supplied)]
    first, second = (view["projection"][1] for view in views)
    if (not np.allclose(first[1], second[1], atol=_CAMERA_AXIS_TOLERANCE, rtol=0) or
            abs(float(first[0] @ second[0])) > _CAMERA_AXIS_TOLERANCE):
        raise InvalidParamsError("Front/side cameras must be perpendicular and share the same vertical direction")
    before = [_measure(world, triangles, view) for view in views]
    movable = context["mask"] & ~context["pinned"]
    if not movable.any():
        raise InvalidParamsError("Selected region has no unpinned vertices")
    proposed = _candidate(world, triangles, movable, views)
    candidate = (world + (proposed - world) * context["weights"][:, None]) * context["unit_scale"]

    def validate_candidate(final_world):
        # Shared constraints may enforce symmetry, and Blender stores float32.
        # Measure those exact coordinates before mutation, not solver estimates.
        final_scene = final_world / context["unit_scale"]
        if np.max(abs((final_scene - world) @ first[1])) > max(1e-7, np.ptp(world, axis=0).max() * 1e-7):
            raise InvalidStateError("Symmetry constraint conflicts with preserved contour height")
        after = [_measure(final_scene, triangles, view) for view in views]
        improvements = []
        for old, new in zip(before, after):
            gain = old["contour_distance_px"]["mean"] - new["contour_distance_px"]["mean"]
            if gain < -1e-8 or new["iou"] < old["iou"] - 1e-8:
                raise InvalidStateError("Contour constraints conflict: a view's contour error or IoU regresses")
            improvements.append(gain)
        if sum(improvements) <= 1e-8 and any(item["contour_distance_px"]["mean"] > 1e-8 for item in before):
            raise InvalidStateError("Selected/pinned region and constraints cannot improve the supplied contours")
        receipts = []
        for view, old, new in zip(views, before, after):
            center, rotation, scale, _, near, far = view["projection"]
            receipts.append({"camera_name": view["camera_name"], "width": view["width"], "height": view["height"],
                             "before": old, "after": new, "projection": {"type": "ORTHO", "pixel_origin": "top_left",
                             "center_world_scene_units": center.tolist(), "right_down_forward": rotation.tolist(),
                             "pixels_per_scene_unit": scale, "clip_start": near, "clip_end": far}})
        return {"views": receipts, "method": "horizontal_cross_section_envelope_remap", "height_samples": 257,
                "limitations": ["Row-convex targets only; vertical coordinates preserved", "Base mesh only; no hidden concavity/anatomy reconstruction",
                                "Envelope interpolation uses 257 height samples plus exact heights for meshes up to 512 vertices"]}

    return apply_candidate(common, {int(i): candidate[i].tolist() for i in np.flatnonzero(movable)},
                           details={"operation": "fit_contours"}, validate_candidate=validate_candidate)
