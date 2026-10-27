"""Pixel-addressable reference crop for correspondence picking."""

from __future__ import annotations

from pathlib import Path
import tempfile
from uuid import uuid4

from ..errors import InvalidParamsError
from ..registry import ToolResult
from ._blender import get_bpy
from .reference_review import _image, _path, _pixels, _save_rgba


def crop_grid(params: dict) -> ToolResult:
    import numpy as np
    unknown = set(params) - {"reference_path", "center", "radius", "zoom", "grid_spacing", "output_path", "overwrite"}
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    reference = _path(params.get("reference_path"), "reference_path", exists=True)
    center = params.get("center")
    if not isinstance(center, (list, tuple)) or len(center) != 2 or any(isinstance(v, bool) or not isinstance(v, int) for v in center):
        raise InvalidParamsError("center must be two integer pixel coordinates")
    radius = params.get("radius", 64)
    zoom = params.get("zoom", 4)
    spacing = params.get("grid_spacing", 10)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (radius, zoom, spacing)) or not 8 <= radius <= 512 or not 1 <= zoom <= 16 or not 1 <= spacing <= 128:
        raise InvalidParamsError("radius 8..512, zoom 1..16, grid_spacing 1..128 are required")
    if (2 * radius + 1) * zoom > 2048:
        raise InvalidParamsError("Crop output exceeds 2048 pixels; reduce radius or zoom")
    raw_path = params.get("output_path")
    if raw_path is None:
        output = Path(tempfile.gettempdir()) / f"cbt_reference_crop_{uuid4().hex}.png"
    elif not isinstance(raw_path, str) or not raw_path or not Path(raw_path).is_absolute() or Path(raw_path).suffix.lower() != ".png":
        raise InvalidParamsError("output_path must be an absolute PNG path")
    else:
        output = Path(raw_path).resolve()
    if output == reference:
        raise InvalidParamsError("output_path must differ from reference_path")
    if output.exists() and params.get("overwrite") is not True:
        raise InvalidParamsError(f"Output already exists: {output}")
    bpy = get_bpy()
    image = _image(bpy, reference, "reference_path")
    try:
        width, height = map(int, image.size)
        cx, cy = center
        if not 0 <= cx < width or not 0 <= cy < height:
            raise InvalidParamsError("center lies outside reference image")
        x0, x1 = max(0, cx - radius), min(width, cx + radius + 1)
        y0, y1 = max(0, cy - radius), min(height, cy + radius + 1)
        source = _pixels(image)[::-1]
        crop = source[y0:y1, x0:x1]
        expanded = np.repeat(np.repeat(crop, zoom, axis=0), zoom, axis=1).copy()
        grid_x = [x for x in range(x0, x1) if x % spacing == 0]
        grid_y = [y for y in range(y0, y1) if y % spacing == 0]
        for x in grid_x:
            expanded[:, (x - x0) * zoom, :3] = (0, .8, .8)
        for y in grid_y:
            expanded[(y - y0) * zoom, :, :3] = (0, .8, .8)
        expanded[max(0, (cy - y0) * zoom):min(expanded.shape[0], (cy - y0 + 1) * zoom),
                 max(0, (cx - x0) * zoom):min(expanded.shape[1], (cx - x0 + 1) * zoom), :3] = (1, 0, 0)
        output.parent.mkdir(parents=True, exist_ok=True)
        _save_rgba(bpy, expanded[::-1], output, "CBTReferenceCrop")
        return ToolResult(data={"path": str(output), "reference_size": [width, height],
                                "crop_origin": [x0, y0], "crop_size": [x1 - x0, y1 - y0],
                                "output_size": [expanded.shape[1], expanded.shape[0]],
                                "center": [cx, cy], "zoom": zoom,
                                "grid_x_pixels": grid_x, "grid_y_pixels": grid_y})
    finally:
        bpy.data.images.remove(image)
