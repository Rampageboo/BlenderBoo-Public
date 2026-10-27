"""Bounded point-cloud sections and point-to-surface evidence."""

from __future__ import annotations

from math import isfinite
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from ..errors import InvalidParamsError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy


def _vec(value: Any, label: str):
    from mathutils import Vector
    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not isfinite(item) for item in value
    ):
        raise InvalidParamsError(f"{label} must be three finite numbers")
    return Vector(value)


def _roi(params: dict[str, Any]):
    raw = params.get("roi")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"min", "max"}:
        raise InvalidParamsError("roi requires min and max vectors")
    low, high = _vec(raw["min"], "roi.min"), _vec(raw["max"], "roi.max")
    if any(low[i] >= high[i] for i in range(3)):
        raise InvalidParamsError("roi.max must exceed roi.min on every axis")
    return low, high


def _in_roi(point, roi):
    return roi is None or all(roi[0][i] <= point[i] <= roi[1][i] for i in range(3))


def _points(bpy, name: str, roi, limit: int):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Point cloud object not found: {name}")
    if obj.type == "MESH":
        source = (vertex.co for vertex in obj.data.vertices)
    elif obj.type == "POINTCLOUD":
        source = (point.co for point in obj.data.points)
    else:
        raise InvalidParamsError("Point cloud must be a MESH or POINTCLOUD object")
    points = []
    total = 0
    for co in source:
        point = obj.matrix_world @ co
        if _in_roi(point, roi):
            total += 1
            if len(points) < limit:
                points.append(point.copy())
    if total > limit:
        raise InvalidParamsError(f"ROI has {total} points; narrow it or raise max_points (limit {limit})")
    return points


def _mesh_bvh(bpy, name: str):
    from mathutils.bvhtree import BVHTree
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type != "MESH":
        raise NotFoundError(f"Mesh target not found: {name}")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        if not mesh.loop_triangles:
            raise InvalidParamsError("Target mesh has no faces")
        if len(mesh.loop_triangles) > 1_000_000:
            raise InvalidParamsError("Target mesh exceeds one million triangles")
        points = [evaluated.matrix_world @ vert.co for vert in mesh.vertices]
        triangles = [tuple(poly.vertices) for poly in mesh.loop_triangles]
        return BVHTree.FromPolygons(points, triangles, all_triangles=True)
    finally:
        evaluated.to_mesh_clear()


def _mesh_section(bpy, name: str, origin, normal, roi, axis_u, axis_v):
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type != "MESH":
        raise NotFoundError(f"Mesh target not found: {name}")
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    segments = []
    try:
        mesh.calc_loop_triangles()
        if len(mesh.loop_triangles) > 1_000_000:
            raise InvalidParamsError("Mesh section exceeds one million triangles")
        world = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
        for tri in mesh.loop_triangles:
            verts = [world[index] for index in tri.vertices]
            signed = [(vertex - origin).dot(normal) for vertex in verts]
            crossings = []
            for index in range(3):
                next_index = (index + 1) % 3
                a, b = signed[index], signed[next_index]
                if a * b < 0 or abs(a) < 1e-9 <= abs(b):
                    ratio = -a / (b - a)
                    point = verts[index].lerp(verts[next_index], ratio)
                    if _in_roi(point, roi):
                        crossings.append([float((point - origin).dot(axis_u)), float((point - origin).dot(axis_v))])
            if len(crossings) == 2:
                segments.append(crossings)
            if len(segments) > 5000:
                raise InvalidParamsError("Mesh section exceeds 5000 segments; narrow roi")
    finally:
        evaluated.to_mesh_clear()
    return segments


def _path(params: dict[str, Any], label: str) -> Path:
    value = params.get("output_path")
    if value is None:
        return Path(tempfile.gettempdir()) / f"cbt_{label}_{uuid4().hex}.png"
    if not isinstance(value, str) or not value or not Path(value).is_absolute() or Path(value).suffix.lower() != ".png":
        raise InvalidParamsError("output_path must be an absolute PNG path")
    path = Path(value).resolve()
    if path.exists() and params.get("overwrite") is not True:
        raise InvalidParamsError(f"Output already exists: {path}")
    return path


def _plot(bpy, coords, colors, output: Path, *, line=None, segments=None):
    import numpy as np
    size = 512
    pixels = np.ones((size, size, 4), dtype=np.float32)
    pixels[:, :, 3] = 1
    if coords:
        samples = np.asarray(coords, dtype=np.float64)
        low, high = samples.min(axis=0), samples.max(axis=0)
        span = np.maximum(high - low, 1e-9)
        xy = np.clip(((samples - low) / span * (size - 41) + 20).astype(int), 0, size - 1)
        for (x, y), color in zip(xy, colors):
            pixels[max(0, y - 2):min(size, y + 3), max(0, x - 2):min(size, x + 3), :3] = color
        if line:
            line_xy = np.clip(((np.asarray(line) - low) / span * (size - 41) + 20).astype(int), 0, size - 1)
            for start, end in zip(line_xy, line_xy[1:]):
                for t in np.linspace(0, 1, max(2, int(np.linalg.norm(end - start)))):
                    x, y = np.rint(start + t * (end - start)).astype(int)
                    pixels[y, x, :3] = (0, 0.1, 0.8)
        if segments:
            for pair in segments:
                ends = np.clip(((np.asarray(pair) - low) / span * (size - 41) + 20).astype(int), 0, size - 1)
                for t in np.linspace(0, 1, max(2, int(np.linalg.norm(ends[1] - ends[0])))):
                    x, y = np.rint(ends[0] + t * (ends[1] - ends[0])).astype(int)
                    pixels[y, x, :3] = (0.9, 0.2, 0.05)
    output.parent.mkdir(parents=True, exist_ok=True)
    image = bpy.data.images.new("CBT_PointReview", width=size, height=size, alpha=True)
    try:
        image.pixels.foreach_set(pixels[::-1].ravel())
        image.filepath_raw = str(output)
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def section(params: dict[str, Any]) -> ToolResult:
    unknown = set(params) - {"pointcloud_name", "mesh_name", "plane_point", "plane_normal", "band_width", "roi", "max_points", "output_path", "overwrite"}
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    bpy = get_bpy()
    name = params.get("pointcloud_name")
    if not isinstance(name, str) or not name:
        raise InvalidParamsError("pointcloud_name is required")
    origin, normal = _vec(params.get("plane_point"), "plane_point"), _vec(params.get("plane_normal"), "plane_normal")
    if normal.length < 1e-12:
        raise InvalidParamsError("plane_normal cannot be zero")
    normal.normalize()
    band = params.get("band_width")
    if isinstance(band, bool) or not isinstance(band, (int, float)) or not isfinite(band) or band <= 0:
        raise InvalidParamsError("band_width must be positive")
    limit = params.get("max_points", 100_000)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000_000:
        raise InvalidParamsError("max_points must be 1..1000000")
    roi = _roi(params)
    selected = [point for point in _points(bpy, name, roi, limit) if abs((point - origin).dot(normal)) <= band / 2]
    axis_u = normal.cross((0, 0, 1)) if abs(normal.z) < .9 else normal.cross((0, 1, 0))
    axis_u.normalize()
    axis_v = normal.cross(axis_u).normalized()
    coords = [((point - origin).dot(axis_u), (point - origin).dot(axis_v)) for point in selected]
    # Median bins give a stable descriptive polyline without inventing points in empty regions.
    import numpy as np
    fitted = []
    if len(coords) >= 4:
        values = np.asarray(coords)
        edges = np.linspace(values[:, 0].min(), values[:, 0].max(), min(33, len(values) + 1))
        for low, high in zip(edges, edges[1:]):
            bucket = values[(values[:, 0] >= low) & (values[:, 0] <= high)]
            if len(bucket):
                fitted.append([float(np.median(bucket[:, 0])), float(np.median(bucket[:, 1]))])
    mesh_sections = _mesh_section(bpy, params["mesh_name"], origin, normal, roi, axis_u, axis_v) if params.get("mesh_name") else []
    output = _path(params, "section")
    plot_coords = coords + [point for segment in mesh_sections for point in segment]
    _plot(bpy, plot_coords, [(0.12, 0.12, 0.12)] * len(coords) + [(0.9, 0.2, 0.05)] * (len(plot_coords) - len(coords)),
          output, line=fitted, segments=mesh_sections)
    return ToolResult(data={"points_2d": [list(item) for item in coords[:5000]], "point_count": len(coords),
                            "fitted_polyline_2d": fitted, "mesh_section_2d": mesh_sections, "image_path": str(output),
                            "plane_basis": {"origin": list(origin), "u": list(axis_u), "v": list(axis_v)},
                            "focus": {"point": list(origin), "radius": max(float(band), 0.01)}})


def compare_to_points(params: dict[str, Any]) -> ToolResult:
    unknown = set(params) - {"pointcloud_name", "mesh_name", "roi", "max_points", "min_points", "outlier_sigma", "max_p95", "borderline_band", "output_path", "overwrite"}
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    bpy = get_bpy()
    names = (params.get("pointcloud_name"), params.get("mesh_name"))
    if any(not isinstance(name, str) or not name for name in names):
        raise InvalidParamsError("pointcloud_name and mesh_name are required")
    limit = params.get("max_points", 100_000)
    minimum = params.get("min_points", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000_000:
        raise InvalidParamsError("max_points must be 1..1000000")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= limit:
        raise InvalidParamsError("min_points must be 1..max_points")
    sigma = params.get("outlier_sigma", 3.0)
    if isinstance(sigma, bool) or not isinstance(sigma, (int, float)) or not isfinite(sigma) or sigma <= 0:
        raise InvalidParamsError("outlier_sigma must be positive")
    max_p95 = params.get("max_p95")
    if max_p95 is not None and (isinstance(max_p95, bool) or not isinstance(max_p95, (int, float)) or not isfinite(max_p95) or max_p95 < 0):
        raise InvalidParamsError("max_p95 must be non-negative")
    borderline_band = params.get("borderline_band", .02)
    if isinstance(borderline_band, bool) or not isinstance(borderline_band, (int, float)) or not isfinite(borderline_band) or not 0 <= borderline_band <= 1:
        raise InvalidParamsError("borderline_band must be 0..1")
    roi = _roi(params)
    points = _points(bpy, names[0], roi, limit)
    if len(points) < minimum:
        return ToolResult(data={"status": "undetermined", "reason": "sparse_points", "point_count": len(points),
                                "min_points": minimum, "focus": None})
    bvh = _mesh_bvh(bpy, names[1])
    distances = []
    for point in points:
        hit, _normal, _face, distance = bvh.find_nearest(point)
        if hit is not None:
            distances.append(float(distance))
    if len(distances) < minimum:
        return ToolResult(data={"status": "undetermined", "reason": "no_nearest_surface", "point_count": len(distances)})
    import numpy as np
    raw = np.asarray(distances)
    median = float(np.median(raw))
    mad = float(np.median(np.abs(raw - median)))
    cutoff = median + sigma * max(1.4826 * mad, 1e-9)
    valid = raw <= cutoff
    kept = raw[valid]
    if len(kept) < minimum:
        return ToolResult(data={"status": "undetermined", "reason": "too_many_outliers", "point_count": len(kept)})
    accepted = [point for point, keep in zip(points, valid) if keep]
    worst_index = int(np.argmax(kept))
    worst = accepted[worst_index]
    maximum = max(float(kept.max()), 1e-9)
    p95 = float(np.percentile(kept, 95))
    acceptance = None if max_p95 is None else ("borderline" if abs(p95 - max_p95) <= borderline_band * max(max_p95, 1e-9)
                                             else "pass" if p95 < max_p95 else "fail")
    colors = [(min(1, float(distance) / maximum), 0.2, 1 - min(1, float(distance) / maximum)) for distance in kept]
    output = _path(params, "distance")
    _plot(bpy, [(point.x, point.y) for point in accepted], colors, output)
    bounds = roi or (tuple(min(point[i] for point in accepted) for i in range(3)),
                     tuple(max(point[i] for point in accepted) for i in range(3)))
    volume = max(1e-12, float(np.prod([bounds[1][i] - bounds[0][i] for i in range(3)])))
    return ToolResult(data={"status": "measured", "point_count": len(points), "accepted_points": len(kept),
                            "outliers": int(len(points) - len(kept)), "density_per_cubic_unit": len(kept) / volume,
                            "distance": {"mean": float(kept.mean()), "p95": p95,
                                         "max": float(kept.max())},
                            "acceptance": acceptance, "max_p95": max_p95,
                            "worst_regions": [{"distance": float(kept.max()),
                                               "focus": {"point": list(worst), "radius": max(float(kept.max()) * 2, 0.01)}}],
                            "image_path": str(output)})
