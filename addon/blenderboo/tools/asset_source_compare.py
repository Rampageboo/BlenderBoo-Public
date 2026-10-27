"""Read-only, CPU-only comparison of evaluated mesh surfaces in world space.

Silhouettes use the same orthographic projection and pixel-center coverage for
both meshes. Distance quantiles are sampled low-to-high distances, not a
Hausdorff bound. Radial rays measure the outermost positive shell from center.
No rendering, images, cameras, visibility or selection datablocks are changed.
"""
from __future__ import annotations

import json
from math import isfinite
from pathlib import Path
import struct
from typing import Any
import zlib

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy

# right/up bases; view positions are front=-Y, side=+X, top=+Z.
_BASES = {
    "front": ((1, 0, 0), (0, 0, 1)),
    "side": ((0, 1, 0), (0, 0, 1)),
    "top": ((1, 0, 0), (0, 1, 0)),
    "back": ((-1, 0, 0), (0, 0, 1)),
    "left": ((0, -1, 0), (0, 0, 1)),
    "bottom": ((1, 0, 0), (0, -1, 0)),
}
_MAX_TRIANGLES = 2_000_000
_MAX_VERTICES = 4_000_000
_MAX_RASTER_PIXELS = 100_000_000
_MAX_REGIONS = 64


def _number(value, name, minimum=0.0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise InvalidParamsError(f"{name} must be a finite number")
    if value < minimum or (maximum is not None and value > maximum):
        raise InvalidParamsError(f"{name} is outside its allowed range")
    return float(value)


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidParamsError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _options(params):
    for name in ("low", "high"):
        if not isinstance(params.get(name), str) or not params[name].strip():
            raise InvalidParamsError(f"{name} must be a non-empty mesh object name")
    views = params.get("views", ["front", "side", "top", "back"])
    if (not isinstance(views, list) or not 1 <= len(views) <= 6
            or any(not isinstance(view, str) or view not in _BASES for view in views)
            or len(set(views)) != len(views)):
        raise InvalidParamsError("views must be a non-empty list of distinct front/side/top/back/left/bottom views")
    spherical = params.get("spherical", False)
    if not isinstance(spherical, bool):
        raise InvalidParamsError("spherical must be a boolean")
    include_radial_grid = params.get("include_radial_grid", False)
    if not isinstance(include_radial_grid, bool):
        raise InvalidParamsError("include_radial_grid must be a boolean")
    center = params.get("center")
    if center is not None:
        if not isinstance(center, (list, tuple)) or len(center) != 3:
            raise InvalidParamsError("center must contain three finite world coordinates")
        center = [_number(v, "center", minimum=-float("inf")) for v in center]
    thresholds = params.get("thresholds", {})
    allowed = {"iou_min", "deviation_p99_mm", "radial_max_mm"}
    if not isinstance(thresholds, dict) or set(thresholds) - allowed:
        raise InvalidParamsError("thresholds accepts iou_min, deviation_p99_mm and radial_max_mm")
    thresholds = {k: _number(v, k, maximum=1.0 if k == "iou_min" else None)
                  for k, v in thresholds.items()}
    if "radial_max_mm" in thresholds and not spherical:
        raise InvalidParamsError("radial_max_mm requires spherical=true")
    output = params.get("output_dir")
    if output is not None:
        if not isinstance(output, str) or not output.strip() or not Path(output).expanduser().is_absolute():
            raise InvalidParamsError("output_dir must be an explicit absolute directory")
        output = Path(output).expanduser().resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise InvalidParamsError("output_dir must be new or empty; existing review files are never overwritten")
    return {
        "views": views, "spherical": spherical, "include_radial_grid": include_radial_grid,
        "center": center, "thresholds": thresholds,
        "output_dir": output,
        "resolution": _integer(params.get("resolution", 512), "resolution", 32, 2048),
        "sample_count": _integer(params.get("sample_count", 10000), "sample_count", 100, 100000),
        "radial_theta_steps": _integer(params.get("radial_theta_steps", 72), "radial_theta_steps", 4, 360),
        "radial_phi_steps": _integer(params.get("radial_phi_steps", 37), "radial_phi_steps", 3, 181),
    }


def _mesh_data(obj, depsgraph):
    import numpy as np
    from mathutils.bvhtree import BVHTree

    if obj.type != "MESH" or obj.mode != "OBJECT":
        raise InvalidStateError(f"{obj.name} must be a mesh in Object Mode")
    source = obj.evaluated_get(depsgraph)
    mesh = source.to_mesh()
    if mesh is None:
        raise InvalidStateError(f"{obj.name} has no evaluated surface")
    try:
        if len(mesh.vertices) > _MAX_VERTICES or len(mesh.loops) > _MAX_TRIANGLES * 3:
            raise InvalidStateError(f"{obj.name} exceeds the bounded geometry budget")
        mesh.calc_loop_triangles()
        if not mesh.loop_triangles or len(mesh.loop_triangles) > _MAX_TRIANGLES:
            raise InvalidStateError(f"{obj.name} needs 1..{_MAX_TRIANGLES} evaluated triangles")
        local = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", local)
        matrix = np.asarray(source.matrix_world, dtype=np.float64)
        vertices = local.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
        if not np.isfinite(vertices).all():
            raise InvalidStateError(f"{obj.name} contains non-finite geometry")
        triangles = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", triangles)
        triangles = triangles.reshape(-1, 3)
        points = vertices[triangles]
        areas = np.linalg.norm(np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0]), axis=1) * .5
        valid = np.isfinite(areas) & (areas > 0)
        skipped = int((~valid).sum())
        triangles, areas = triangles[valid], areas[valid]
        if not len(triangles):
            raise InvalidStateError(f"{obj.name} has no non-degenerate surface")
        surface = vertices[np.unique(triangles)]
        return {"vertices": vertices, "triangles": triangles, "areas": areas,
                "bounds": (surface.min(axis=0), surface.max(axis=0)),
                "bvh": BVHTree.FromPolygons(vertices.tolist(), triangles.tolist(), all_triangles=True),
                "degenerate_triangles_skipped": skipped, "name": obj.name}
    finally:
        source.to_mesh_clear()


def _frame(low, high, view, resolution):
    import numpy as np

    basis = np.asarray(_BASES[view], dtype=float)
    # Bounds include both meshes. Neither mesh is re-centered or independently fitted.
    lo = np.minimum(low["bounds"][0], high["bounds"][0])
    hi = np.maximum(low["bounds"][1], high["bounds"][1])
    corners = np.array([(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    projected = corners @ basis.T
    lower, upper = projected.min(axis=0), projected.max(axis=0)
    span = float(max(upper - lower)) * 1.05
    if not isfinite(span) or span <= 0:
        raise InvalidStateError(f"No measurable extent for {view} projection")
    center = (lower + upper) * .5
    frame = {"right": basis[0].tolist(), "up": basis[1].tolist(),
             "center_world": ((lo + hi) * .5).tolist(), "ortho_scale": span,
             "world_units_per_pixel": span / resolution, "resolution": resolution,
             "pixel_origin": "top_left", "projection": "orthographic"}
    def project(mesh):
        xy = ((mesh["vertices"] @ basis.T - center) / span + .5) * resolution
        xy[:, 1] = resolution - xy[:, 1]
        return xy
    return project(low), project(high), frame


def _raster_mask(projected, triangles, resolution, budget):
    """Union triangle coverage at pixel centers, no material/backface/depth test."""
    import numpy as np

    mask = np.zeros((resolution, resolution), dtype=bool)
    points = projected[triangles]
    mins = np.maximum(np.ceil(points.min(axis=1) - .5).astype(np.int64), 0)
    maxs = np.minimum(np.floor(points.max(axis=1) - .5).astype(np.int64), resolution - 1)
    a = points[:, 1] - points[:, 0]
    b = points[:, 2] - points[:, 0]
    dets = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    valid = (maxs >= mins).all(axis=1) & (np.abs(dets) > 1e-12)
    for index in np.flatnonzero(valid):
        x0, y0 = mins[index]
        x1, y1 = maxs[index]
        count = int((x1 - x0 + 1) * (y1 - y0 + 1))
        budget[0] += count
        if budget[0] > _MAX_RASTER_PIXELS:
            raise InvalidStateError("CPU silhouette work exceeds 100 million bounding-box pixels; reduce resolution or compare simpler meshes")
        block = mask[y0:y1 + 1, x0:x1 + 1]
        if block.all():
            continue
        x = np.arange(x0, x1 + 1)[None, :] + .5 - points[index, 0, 0]
        y = np.arange(y0, y1 + 1)[:, None] + .5 - points[index, 0, 1]
        u = (x * b[index, 1] - y * b[index, 0]) / dets[index]
        v = (a[index, 0] * y - a[index, 1] * x) / dets[index]
        block |= (u >= -1e-10) & (v >= -1e-10) & (u + v <= 1 + 1e-10)
    return mask


def _silhouette_stats(low_mask, high_mask):
    intersection = int((low_mask & high_mask).sum())
    union = int((low_mask | high_mask).sum())
    low_count, high_count = int(low_mask.sum()), int(high_mask.sum())
    return {"iou": intersection / union if union and low_count and high_count else None,
            "status": "complete" if low_count and high_count else "unavailable",
            "low_pixels": low_count, "high_pixels": high_count,
            "intersection_pixels": intersection, "union_pixels": union,
            "low_only_pixels": low_count - intersection, "high_only_pixels": high_count - intersection}


def _surface_samples(mesh, count):
    """Equal-area strata select triangles; seeded barycentric samples cover interiors."""
    import numpy as np

    cdf = np.cumsum(mesh["areas"])
    if not np.isfinite(cdf[-1]) or cdf[-1] <= 0:
        raise InvalidStateError("Low mesh has no finite surface area")
    ids = np.searchsorted(cdf, (np.arange(count) + .5) / count * cdf[-1])
    tri = mesh["vertices"][mesh["triangles"][ids]]
    rng = np.random.default_rng(0)
    root = np.sqrt(rng.random(count))
    split = rng.random(count)
    samples = tri[:, 0] * (1 - root[:, None]) + tri[:, 1] * (root * (1 - split))[:, None] + tri[:, 2] * (root * split)[:, None]
    return samples, int(len(np.unique(ids)))


def _quantiles(values):
    import numpy as np

    if not len(values):
        return {key: None for key in ("median", "p90", "p99", "max")}
    q = np.percentile(values, [50, 90, 99, 100])
    return dict(zip(("median", "p90", "p99", "max"), map(float, q)))


def _deviation(low, high, count, mm_per_unit):
    from mathutils import Vector

    samples, sampled_triangles = _surface_samples(low, count)
    distances = []
    for point in samples:
        location, _, _, distance = high["bvh"].find_nearest(Vector(point))
        if location is not None and distance is not None and isfinite(distance):
            distances.append(float(distance))
    return {"status": "complete" if len(distances) == count else "partial" if distances else "unavailable",
            "direction": "low_surface_to_high_surface", "sampling": "deterministic_area_stratified_barycentric_seed_0",
            "requested_samples": count, "valid_samples": len(distances), "missed_samples": count - len(distances),
            "coverage": len(distances) / count, "sampled_triangles": sampled_triangles,
            "low_triangles": len(low["triangles"]), "scene_units": _quantiles(distances),
            "millimetres": _quantiles([d * mm_per_unit for d in distances]),
            "max_is_sampled": True}


def _radial_regions(delta, theta, phi, threshold, mm_per_unit):
    """Connected theta/phi cells; wrap azimuth across the 0/360 seam."""
    import numpy as np

    pending = np.isfinite(delta) & (np.abs(delta) > threshold)
    over_count = int(pending.sum())
    height, width = pending.shape
    regions = []
    total = 0
    for row in range(height):
        for col in range(width):
            if not pending[row, col]:
                continue
            pending[row, col] = False
            stack, cells = [(row, col)], []
            while stack:
                r, c = stack.pop()
                cells.append((r, c))
                for nr, nc in ((r - 1, c), (r + 1, c), (r, (c - 1) % width), (r, (c + 1) % width)):
                    if 0 <= nr < height and pending[nr, nc]:
                        pending[nr, nc] = False
                        stack.append((nr, nc))
            total += 1
            if len(regions) >= _MAX_REGIONS:
                continue
            peak = max(cells, key=lambda cell: abs(delta[cell]))
            rows, cols = zip(*cells)
            regions.append({"cell_count": len(cells), "theta_deg_bounds": [float(theta[min(cols)]), float(theta[max(cols)])],
                            "phi_deg_bounds": [float(phi[min(rows)]), float(phi[max(rows)])],
                            "wraps_theta_seam": 0 in cols and width - 1 in cols,
                            "peak_theta_deg": float(theta[peak[1]]), "peak_phi_deg": float(phi[peak[0]]),
                            "peak_signed_delta_scene_units": float(delta[peak]),
                            "peak_abs_delta_mm": abs(float(delta[peak])) * mm_per_unit})
    return {"over_threshold_cells": over_count, "region_count": total, "regions": regions,
            "regions_truncated": total > len(regions)}


def _radial(low, high, center, theta_steps, phi_steps, mm_per_unit, threshold_mm):
    import numpy as np
    from mathutils import Vector

    lo = np.minimum(low["bounds"][0], high["bounds"][0])
    hi = np.maximum(low["bounds"][1], high["bounds"][1])
    center = np.asarray(center if center is not None else (lo + hi) * .5, dtype=float)
    corners = np.array([(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    radius = float(np.linalg.norm(corners - center, axis=1).max()) * 1.01
    theta = np.arange(theta_steps) * 360.0 / theta_steps
    phi = -90 + (np.arange(phi_steps) + .5) * 180.0 / phi_steps
    radii = np.full((2, phi_steps, theta_steps), np.nan)
    for row, elevation in enumerate(np.radians(phi)):
        for col, azimuth in enumerate(np.radians(theta)):
            direction = Vector((np.cos(elevation) * np.cos(azimuth), np.cos(elevation) * np.sin(azimuth), np.sin(elevation)))
            origin = Vector(center) + direction * radius
            for index, mesh in enumerate((low, high)):
                hit, _, _, distance = mesh["bvh"].ray_cast(origin, -direction, radius)
                if hit is not None and distance is not None and isfinite(distance):
                    radii[index, row, col] = max(0.0, radius - float(distance))
    low_hits, high_hits = np.isfinite(radii[0]), np.isfinite(radii[1])
    paired = low_hits & high_hits
    delta = radii[0] - radii[1]
    finite = delta[paired]
    automatic_threshold = threshold_mm is None
    if automatic_threshold:
        threshold_mm = float(np.percentile(abs(finite) * mm_per_unit, 90)) if finite.size else 0.0
    count = theta_steps * phi_steps
    paired_count = int(paired.sum())
    info = {"status": "unavailable" if paired_count == 0 else "partial" if paired_count < count else "complete",
            "method": "outermost_positive_radius_outside_in", "center_world": center.tolist(),
            "theta_convention": "degrees from +X towards +Y", "phi_convention": "elevation degrees from XY towards +Z; cell centers",
            "theta_deg": theta.tolist(), "phi_deg": phi.tolist(), "grid_shape": [phi_steps, theta_steps],
            "rays_per_mesh": count, "paired_hits": paired_count, "paired_coverage": paired_count / count,
            "low_hits": int(low_hits.sum()), "high_hits": int(high_hits.sum()),
            "low_only_hits": int((low_hits & ~high_hits).sum()), "high_only_hits": int((high_hits & ~low_hits).sum()),
            "both_missed": int((~low_hits & ~high_hits).sum()),
            "delta_convention": "low_radius_minus_high_radius; null means one or both rays missed",
            "delta_scene_units": [[float(v) if isfinite(v) else None for v in row] for row in delta],
            "abs_delta_scene_units": _quantiles(abs(finite)), "abs_delta_mm": _quantiles(abs(finite) * mm_per_unit),
            "region_threshold_mm": threshold_mm,
            "region_threshold_method": "paired_abs_delta_p90" if automatic_threshold else "explicit_radial_max_mm",
            **_radial_regions(delta, theta, phi, threshold_mm / mm_per_unit, mm_per_unit)}
    return info, delta


def _acceptance(views, deviation, radial, thresholds):
    checks = {}
    if "iou_min" in thresholds:
        checks["iou_min"] = all(v["status"] == "complete" and v["iou"] >= thresholds["iou_min"] for v in views.values())
    if "deviation_p99_mm" in thresholds:
        checks["deviation_p99_mm"] = deviation["status"] == "complete" and deviation["millimetres"]["p99"] <= thresholds["deviation_p99_mm"]
    if "radial_max_mm" in thresholds:
        checks["radial_max_mm"] = radial["status"] == "complete" and radial["abs_delta_mm"]["max"] <= thresholds["radial_max_mm"]
    return {"pass": all(checks.values()) if checks else None, "checks": checks,
            "failures": [{"metric": key, "threshold": thresholds[key], "reason": "threshold_exceeded_or_measurement_incomplete"}
                         for key, passed in checks.items() if not passed]}


def _write_png(path, pixels):
    """PNG encoder avoids bpy image creation and third-party imaging dependencies."""
    import numpy as np

    pixels = np.asarray(pixels, dtype=np.uint8)
    height, width = pixels.shape[:2]
    channels = 1 if pixels.ndim == 2 else pixels.shape[2]
    color_type = {1: 0, 3: 2}[channels]
    raw = b"".join(b"\x00" + row.tobytes() for row in pixels)
    def chunk(tag, payload):
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff)
    content = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
    content += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    with path.open("xb") as handle:
        handle.write(content)


def compare_to_source(params: dict[str, Any]) -> ToolResult:
    import numpy as np

    options = _options(params)
    bpy = get_bpy()
    objects = []
    for key in ("low", "high"):
        obj = bpy.context.scene.objects.get(params[key])
        if obj is None:
            raise NotFoundError(f"Mesh object not found in current scene: {params[key]}")
        objects.append(obj)
    scale = float(bpy.context.scene.unit_settings.scale_length)
    if not isfinite(scale) or scale <= 0:
        raise InvalidStateError("Scene unit scale must be finite and positive")
    mm_per_unit = scale * 1000.0
    depsgraph = bpy.context.evaluated_depsgraph_get()
    low, high = (_mesh_data(obj, depsgraph) for obj in objects)
    warnings = ["Silhouettes use geometry-only pixel-center coverage; materials, alpha, lighting and visibility are ignored.",
                "Distance max is a sampled low-to-high maximum, not a bidirectional Hausdorff bound; unsampled small features may be missed."]
    if bpy.context.scene.unit_settings.system == "NONE":
        warnings.append("Scene units are NONE; millimetres use scale_length with the Blender convention of one unit = one metre at scale 1. Confirm real scale before acceptance.")
    views, masks, budget = {}, {}, [0]
    for view in options["views"]:
        low_xy, high_xy, frame = _frame(low, high, view, options["resolution"])
        low_mask = _raster_mask(low_xy, low["triangles"], options["resolution"], budget)
        high_mask = _raster_mask(high_xy, high["triangles"], options["resolution"], budget)
        views[view] = {**_silhouette_stats(low_mask, high_mask), "frame": frame}
        masks[view] = (low_mask, high_mask)
    deviation = _deviation(low, high, options["sample_count"], mm_per_unit)
    radial, radial_pixels = None, None
    if options["spherical"]:
        radial, delta = _radial(low, high, options["center"], options["radial_theta_steps"], options["radial_phi_steps"],
                                mm_per_unit, options["thresholds"].get("radial_max_mm"))
        radial_grid = radial.pop("delta_scene_units")
        if options["include_radial_grid"]:
            radial["delta_scene_units"] = radial_grid
        warnings.append("Radial comparison measures only the outermost shell on each positive center ray; inner layers and concavities can be hidden. Both-missed rays are reported, not assumed to match.")
        radial_pixels = np.full((*delta.shape, 3), 40, dtype=np.uint8)
        valid = np.isfinite(delta)
        max_delta = float(np.max(abs(delta[valid]))) if valid.any() else 0
        radial_pixels[valid] = 220
        if max_delta:
            strength = np.zeros(delta.shape)
            strength[valid] = np.minimum(abs(delta[valid]) / max_delta, 1)
            radial_pixels[valid & (delta > 0)] = np.stack([220 + 35 * strength, 220 * (1 - strength), 220 * (1 - strength)], axis=-1)[valid & (delta > 0)].astype(np.uint8)
            radial_pixels[valid & (delta < 0)] = np.stack([220 * (1 - strength), 220 * (1 - strength), 220 + 35 * strength], axis=-1)[valid & (delta < 0)].astype(np.uint8)
        radial["image_legend"] = {"red": "low outside high", "blue": "low inside high", "gray": "zero difference", "dark": "unpaired or missed ray", "row_order": "phi ascending", "full_color_abs_delta_scene_units": max_delta}
    complete = all(v["status"] == "complete" for v in views.values()) and deviation["status"] == "complete" and (radial is None or radial["status"] == "complete")
    known_iou = [v["iou"] for v in views.values() if v["iou"] is not None]
    data = {"low": low["name"], "high": high["name"], "status": "complete" if complete else "partial",
            **_acceptance(views, deviation, radial, options["thresholds"]), "thresholds": options["thresholds"],
            "views": views, "iou_min": min(known_iou) if len(known_iou) == len(views) else None,
            "deviation": deviation, "radial": radial, "millimetres_per_scene_unit": mm_per_unit,
            "evaluated_geometry": True, "raster_bounding_box_pixels": budget[0],
            "geometry": {"low_triangles": len(low["triangles"]), "high_triangles": len(high["triangles"]),
                         "low_degenerate_triangles_skipped": low["degenerate_triangles_skipped"],
                         "high_degenerate_triangles_skipped": high["degenerate_triangles_skipped"]},
            "artifacts": []}
    if not complete:
        warnings.append("Some measurements are partial or unavailable; inspect view coverage and radial hit counts.")
    output = options["output_dir"]
    if output is not None:
        # Recheck immediately before writes; exclusive file creation also prevents overwrites.
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise InvalidParamsError("output_dir is no longer empty")
        output.mkdir(parents=True, exist_ok=True)
        for view, (low_mask, high_mask) in masks.items():
            diff = np.zeros((*low_mask.shape, 3), dtype=np.uint8)
            diff[low_mask & high_mask] = (192, 192, 192)
            diff[high_mask & ~low_mask] = (255, 51, 51)
            diff[low_mask & ~high_mask] = (51, 153, 255)
            for kind, pixels in (("low_mask", low_mask * 255), ("high_mask", high_mask * 255), ("difference", diff)):
                path = output / f"{view}_{kind}.png"
                _write_png(path, pixels)
                views[view][f"{kind}_path"] = str(path)
                data["artifacts"].append({"kind": kind, "view": view, "path": str(path)})
            views[view]["difference_legend"] = {"gray": "intersection", "red": "high only", "blue": "low only", "black": "background"}
        if radial is not None:
            path = output / "radial_difference.png"
            _write_png(path, radial_pixels)
            radial["difference_path"] = str(path)
            data["artifacts"].append({"kind": "radial_difference", "path": str(path)})
            grid_path = output / "radial_delta.json"
            with grid_path.open("x", encoding="utf-8") as handle:
                json.dump({"theta_deg": radial["theta_deg"], "phi_deg": radial["phi_deg"],
                           "delta_scene_units": radial_grid}, handle, allow_nan=False)
            radial["grid_path"] = str(grid_path)
            data["artifacts"].append({"kind": "radial_delta", "path": str(grid_path)})
    return ToolResult(data=data, warnings=warnings)
