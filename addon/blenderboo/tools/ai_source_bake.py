"""Headless AI-source texture transfer; all Blender edits use disposable copies.

Source channels are evaluated by Cycles emission, including linked shader graphs.
Nearest transfer samples a source-UV bake in 0..1. UV ambiguity is measured
and bounded; unresolved ray/cache ambiguity remains a reported projection miss.
No third-party Python dependencies beyond Blender's bundled NumPy are required.
"""
from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
import re
import tempfile
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .bake import _integer, _name, _number
from .session_lease import require_document_write_access
from .session_state import check_expected_state


def _options(params):
    unknown = set(params) - {
        "low", "high", "resolution", "part_attribute", "shell_parts", "cage_extrusion",
        "max_ray_distance", "highpass_sigma_px", "normal_convention", "pack", "output_dir",
        "margin", "ao_samples", "expected_state", "max_uv_overlap_pct", "fallback_max_distance",
        "fill_face_attribute", "fill_parts",
    }
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    result = dict(params)
    for key in ("low", "high", "output_dir"):
        result[key] = _name(params.get(key), key)
    if result["low"] == result["high"]:
        raise InvalidParamsError("low and high must be different objects")
    result["resolution"] = _integer(params.get("resolution", 2048), "resolution", 16, 4096)
    result["margin"] = _integer(params.get("margin", 16), "margin", 0, 128)
    result["ao_samples"] = _integer(params.get("ao_samples", 64), "ao_samples", 1, 1024)
    for key, default, maximum in (("cage_extrusion", 0.01, 1000),
                                 ("max_ray_distance", 0.0, 1000),
                                 ("highpass_sigma_px", 16.0, 256)):
        result[key] = _number(params.get(key, default), key, 0, maximum)
    result["max_uv_overlap_pct"] = _number(params.get("max_uv_overlap_pct", 0.01), "max_uv_overlap_pct", 0, 100)
    result["fallback_max_distance"] = _number(
        params.get("fallback_max_distance", min(1000.0, result["cage_extrusion"] + result["max_ray_distance"])),
        "fallback_max_distance", 0, 1000)
    shell = params.get("shell_parts", [])
    if not isinstance(shell, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in shell):
        raise InvalidParamsError("shell_parts must be a list of integer face-part ids")
    result["shell_parts"] = set(shell)
    attr = params.get("part_attribute")
    if attr is not None:
        _name(attr, "part_attribute")
    if shell and attr is None:
        raise InvalidParamsError("shell_parts requires part_attribute")
    result["part_attribute"] = attr
    fill_attribute = params.get("fill_face_attribute", attr)
    if fill_attribute is not None:
        _name(fill_attribute, "fill_face_attribute")
    fill_parts = params.get("fill_parts", {})
    if not isinstance(fill_parts, dict) or len(fill_parts) > 64:
        raise InvalidParamsError("fill_parts must map at most 64 face-part ids to fixed channels")
    if fill_parts and fill_attribute is None:
        raise InvalidParamsError("fill_parts requires fill_face_attribute or part_attribute")
    parsed_fill = {}
    for raw_id, channels in fill_parts.items():
        if (isinstance(raw_id, bool) or not isinstance(raw_id, (str, int))
                or not str(raw_id).isdigit() or int(raw_id) > 2147483647):
            raise InvalidParamsError("fill_parts keys must be nonnegative FACE INT part ids")
        part_id = int(raw_id)
        if part_id in parsed_fill or not isinstance(channels, dict):
            raise InvalidParamsError("fill_parts ids must be unique and each value must be an object")
        if set(channels) - {"base_color", "roughness", "metallic", "ao", "normal"} or not {"base_color", "roughness", "metallic", "ao"} <= set(channels):
            raise InvalidParamsError("fill_parts values require base_color, roughness, metallic and ao; normal may be FLAT")
        color = channels["base_color"]
        if not isinstance(color, (list, tuple)) or len(color) != 3:
            raise InvalidParamsError("fill_parts.base_color must be three linear values from 0 to 1")
        if channels.get("normal", "FLAT") != "FLAT":
            raise InvalidParamsError("fill_parts.normal currently supports only FLAT")
        parsed_fill[part_id] = {"base_color": [_number(value, "base_color", 0, 1) for value in color],
                                "roughness": _number(channels["roughness"], "roughness", 0, 1),
                                "metallic": _number(channels["metallic"], "metallic", 0, 1),
                                "ao": _number(channels["ao"], "ao", 0, 1)}
    result["fill_face_attribute"] = fill_attribute
    result["fill_parts"] = parsed_fill
    result["normal_convention"] = params.get("normal_convention", "OPENGL")
    if result["normal_convention"] not in ("OPENGL", "DIRECTX"):
        raise InvalidParamsError("normal_convention must be OPENGL or DIRECTX")
    result["pack"] = params.get("pack")
    if result["pack"] not in (None, "ORM"):
        raise InvalidParamsError("pack must be ORM or null")
    if "expected_state" in params:
        _name(params["expected_state"], "expected_state")
    return result


def _raster(mesh, resolution):
    """Yield pixel centers, barycentrics, and Blender loop triangles (bottom-up)."""
    import numpy as np
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise InvalidStateError("Evaluated mesh requires an active UV layer")
    uv = np.empty(len(mesh.loops) * 2, dtype=np.float64)
    uv_layer.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    if not np.isfinite(uv).all() or (uv < -1e-7).any() or (uv > 1 + 1e-7).any():
        raise InvalidStateError("Bake UVs must be finite and inside the single 0..1 tile")
    mesh.calc_loop_triangles()
    for tri in mesh.loop_triangles:
        points = uv[list(tri.loops)] * resolution - 0.5
        low = np.maximum(np.floor(points.min(axis=0)).astype(int), 0)
        high = np.minimum(np.ceil(points.max(axis=0)).astype(int), resolution - 1)
        if (high < low).any():
            continue
        a, b, c = points
        determinant = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(determinant) < 1e-12:
            continue
        x, y = np.meshgrid(np.arange(low[0], high[0] + 1), np.arange(low[1], high[1] + 1))
        w0 = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / determinant
        w1 = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / determinant
        weights = np.stack((w0, w1, 1 - w0 - w1), axis=-1)
        inside = (weights >= -1e-8).all(axis=-1)
        if inside.any():
            yield tri, y[inside], x[inside], weights[inside]


def _uv_masks(mesh, resolution, part_attribute=None, shell_parts=(), max_uv_overlap_pct=0.01):
    """Measure strict-interior ambiguity and own texels by first loop triangle.

    Same-face triangle overlap is included: a folded n-gon UV is still ambiguous.
    Boundary-only sharing is counted separately and is not a strict overlap.
    """
    import numpy as np
    owner = np.full((resolution, resolution), -1, dtype=np.int32)
    interior_owner = owner.copy()
    shared = np.zeros(owner.shape, dtype=bool)
    same_face = shared.copy()
    cross_face = shared.copy()
    shell = shared.copy()
    parts = None
    if part_attribute:
        attribute = mesh.attributes.get(part_attribute)
        if attribute is None or attribute.domain != "FACE" or attribute.data_type != "INT":
            raise InvalidStateError("part_attribute must name an INT attribute on the evaluated FACE domain")
        parts = [v.value for v in attribute.data]
        missing = set(shell_parts) - set(parts)
        if missing:
            raise InvalidParamsError(f"shell_parts not present on low: {sorted(missing)}")
    triangle_faces = {}
    for tri, y, x, weights in _raster(mesh, resolution):
        triangle_faces[tri.index] = tri.polygon_index
        shared[y, x] |= owner[y, x] >= 0
        fresh = owner[y, x] < 0
        owner[y[fresh], x[fresh]] = tri.index
        shell[y[fresh], x[fresh]] = parts is not None and parts[tri.polygon_index] in shell_parts
        strict = (weights > 1e-7).all(axis=-1)
        sy, sx = y[strict], x[strict]
        previous = interior_owner[sy, sx]
        collision = previous >= 0
        if collision.any():
            old_faces = np.array([triangle_faces[int(index)] for index in previous[collision]])
            is_same_face = old_faces == tri.polygon_index
            cy, cx = sy[collision], sx[collision]
            same_face[cy[is_same_face], cx[is_same_face]] = True
            cross_face[cy[~is_same_face], cx[~is_same_face]] = True
        interior_owner[sy[~collision], sx[~collision]] = tri.index
    occupied = owner >= 0
    count = int(occupied.sum())
    if not count:
        raise InvalidStateError("UV layout contains no covered texel centers at this resolution")
    ambiguous = same_face | cross_face
    ambiguous_count = int(ambiguous.sum())
    overlap_pct = 100 * ambiguous_count / count
    stats = {
        "uv_overlap_pct": overlap_pct, "ambiguous_texels": ambiguous_count,
        "occupied_texels": count, "same_face_overlap_texels": int(same_face.sum()),
        "cross_face_overlap_texels": int(cross_face.sum()),
        "shared_boundary_texels": int((shared & ~ambiguous).sum()),
        "max_uv_overlap_pct": max_uv_overlap_pct,
        "denominator": "occupied UV texel centers",
        "ownership": "lowest loop-triangle index for shell routing and nearest transfer",
    }
    if overlap_pct > max_uv_overlap_pct:
        error = InvalidStateError(
            f"UV overlap {overlap_pct:.8f}% exceeds max_uv_overlap_pct={max_uv_overlap_pct}: "
            f"{ambiguous_count}/{count} ambiguous texels "
            f"(same-face {stats['same_face_overlap_texels']}, cross-face {stats['cross_face_overlap_texels']})")
        error.data = {"uv_overlap_pct": overlap_pct, "ambiguous_texels": ambiguous_count, "uv_overlap": stats}
        raise error
    return occupied, shell, owner, ambiguous, stats


def _owned_parts(mesh, owner, attribute_name):
    """Map each owned UV texel to its evaluated FACE INT part, or part zero."""
    import numpy as np

    parts = np.full(owner.shape, -1, dtype=np.int32)
    valid = owner >= 0
    if attribute_name is None:
        parts[valid] = 0
        return parts
    attribute = mesh.attributes.get(attribute_name)
    if attribute is None or attribute.domain != "FACE" or attribute.data_type != "INT":
        raise InvalidStateError("fill_face_attribute/part_attribute must name a FACE INT attribute on the evaluated low mesh")
    values = np.array([item.value for item in attribute.data], dtype=np.int32)
    triangle_parts = np.array([values[tri.polygon_index] for tri in mesh.loop_triangles], dtype=np.int32)
    parts[valid] = triangle_parts[owner[valid]]
    return parts


def _world_vertices(obj):
    import numpy as np
    points = np.empty(len(obj.data.vertices) * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", points)
    matrix = np.asarray(obj.matrix_world, dtype=np.float64)
    return points.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]


def _nearest_uv(low, high, resolution, selected, owner, max_distance=None, unbounded=None, progress=None):
    """A triangulated world-space BVH correctly supports n-gons and transforms."""
    import numpy as np
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    high.data.calc_loop_triangles()
    triangles = list(high.data.loop_triangles)
    vertices = _world_vertices(high)
    tree = BVHTree.FromPolygons(vertices.tolist(), [tuple(t.vertices) for t in triangles], all_triangles=True)
    source_uv = np.empty(len(high.data.loops) * 2, dtype=np.float64)
    high.data.uv_layers.active.data.foreach_get("uv", source_uv)
    source_uv = source_uv.reshape(-1, 2)
    low_vertices = _world_vertices(low)
    uv_map = np.zeros((resolution, resolution, 2), dtype=np.float32)
    hits = np.zeros_like(selected)
    distances = []
    distance_rejected = 0
    processed = 0
    total = int(selected.sum())
    if progress is not None:
        progress(processed, total, 0, distance_rejected)
    for tri, y, x, weights in _raster(low.data, resolution):
        chosen = selected[y, x] & (owner[y, x] == tri.index)
        y, x, weights = y[chosen], x[chosen], weights[chosen]
        points = weights @ low_vertices[list(tri.vertices)]
        for row, col, point in zip(y, x, points):
            processed += 1
            if progress is not None and processed % 8192 == 0:
                progress(processed, total, len(distances), distance_rejected)
            loc, _normal, index, distance = tree.find_nearest(Vector(point))
            if index is None:
                continue
            if max_distance is not None and distance > max_distance and (unbounded is None or not unbounded[row, col]):
                distance_rejected += 1
                continue
            source = triangles[index]
            a, b, c = vertices[list(source.vertices)]
            ab, ac, ap = b - a, c - a, np.asarray(loc) - a
            d00, d01, d11 = ab @ ab, ab @ ac, ac @ ac
            denominator = d00 * d11 - d01 * d01
            if denominator <= 1e-24:
                continue
            d20, d21 = ap @ ab, ap @ ac
            v = (d11 * d20 - d01 * d21) / denominator
            w = (d00 * d21 - d01 * d20) / denominator
            uv_map[row, col] = np.array((1 - v - w, v, w)) @ source_uv[list(source.loops)]
            hits[row, col] = True
            distances.append(distance)
    if progress is not None:
        progress(processed, total, len(distances), distance_rejected)
    return uv_map, hits, {
        "distance_median_scene_units": float(np.median(distances)) if distances else None,
        "distance_max_scene_units": float(max(distances)) if distances else None,
        "distance_rejected_texels": distance_rejected,
    }


def _low_part_occlusion_samples(low, resolution, owner, owned_parts, candidate, part_attribute, options):
    """Bounded potential occlusion sample; never treats it as exact coverage."""
    import numpy as np
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    count = int(candidate.sum())
    result = {"candidate_texels": count, "sampled_texels": 0,
              "potential_other_part_samples": 0, "by_part": {}, "focus": [],
              "method": "deterministic UV texel samples; first low-mesh hit along either face-normal direction",
              "interpretation": "potential low-part occlusion, not a proof of the Blender bake ray path"}
    if part_attribute is None:
        return {**result, "status": "unavailable", "reason": "part_attribute is required to distinguish low parts"}
    if not count:
        return {**result, "status": "complete"}
    part_ids = [int(value) for value in np.unique(owned_parts[candidate])]
    sample_mask = np.zeros_like(candidate)
    per_part_budget = max(1, 8192 // len(part_ids))
    for part_id in part_ids:
        indices = np.flatnonzero(candidate & (owned_parts == part_id))
        chosen = indices[np.linspace(0, len(indices) - 1, min(len(indices), per_part_budget), dtype=int)]
        sample_mask.flat[chosen] = True
        result["by_part"][str(part_id)] = {"sampled_texels": 0, "potential_other_part_samples": 0}
    vertices = _world_vertices(low)
    triangles = list(low.data.loop_triangles)
    parts = [int(item.value) for item in low.data.attributes[part_attribute].data]
    triangle_parts = [parts[tri.polygon_index] for tri in triangles]
    tree = BVHTree.FromPolygons(vertices.tolist(), [tuple(tri.vertices) for tri in triangles], all_triangles=True)
    extent = max(float(np.ptp(vertices, axis=0).max()), 1e-6)
    distance = (options["max_ray_distance"] + options["cage_extrusion"]
                if options["max_ray_distance"] > 0 else extent * 2)
    epsilon = max(extent * 1e-5, 1e-7)
    result["ray_length_scene_units"] = distance
    for tri, rows, cols, weights in _raster(low.data, resolution):
        selected = sample_mask[rows, cols] & (owner[rows, cols] == tri.index)
        if not selected.any():
            continue
        points = weights[selected] @ vertices[list(tri.vertices)]
        a, b, c = vertices[list(tri.vertices)]
        normal = np.cross(b - a, c - a)
        length = float(np.linalg.norm(normal))
        if length <= 1e-12:
            continue
        normal /= length
        for row, col, point in zip(rows[selected], cols[selected], points):
            part_id = int(owned_parts[row, col])
            item = result["by_part"][str(part_id)]
            item["sampled_texels"] += 1
            result["sampled_texels"] += 1
            for direction in (normal, -normal):
                hit, _, index, _ = tree.ray_cast(Vector(point + direction * epsilon), Vector(direction), distance)
                if hit is not None and index is not None and triangle_parts[index] != part_id:
                    item["potential_other_part_samples"] += 1
                    result["potential_other_part_samples"] += 1
                    if len(result["focus"]) < 16:
                        result["focus"].append({"part_id": part_id, "other_part_id": triangle_parts[index],
                                                "uv_texel": [int(col), int(row)], "point_world": [float(v) for v in point]})
                    break
    return {**result, "status": "sampled"}


def _bilinear(pixels, uv):
    import numpy as np
    height, width = pixels.shape[:2]
    x = np.clip(uv[:, 0] * width - 0.5, 0, width - 1)
    y = np.clip(uv[:, 1] * height - 0.5, 0, height - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    return ((pixels[y0, x0] * (1 - fx) + pixels[y0, x1] * fx) * (1 - fy)
            + (pixels[y1, x0] * (1 - fx) + pixels[y1, x1] * fx) * fy)


def _color_metrics(rgb, inside, color_hit):
    """Coverage defects and source darkness are independent measurements."""
    count = int(inside.sum())
    if not count:
        raise InvalidStateError("Color metrics require occupied UV texels")
    missing = inside & ~color_hit
    dark = inside & (rgb.max(axis=-1) < 0.015)
    misses, raw_dark = int(missing.sum()), int(dark.sum())
    valid_dark = int((dark & color_hit).sum())
    return {
        "coverage_ratio": 1 - misses / count, "miss_texels": misses, "miss_pct": 100 * misses / count,
        "black_texels": misses, "black_texels_pct": 100 * misses / count,
        "black_texels_definition": "UV texels lacking valid color projection or source-cache coverage before padding; independent of source luminance",
        "raw_dark_texels": raw_dark, "raw_dark_pct": 100 * raw_dark / count,
        "valid_dark_texels": valid_dark, "valid_dark_pct": 100 * valid_dark / count,
        "raw_dark_threshold_linear": 0.015,
    }


def _dilate(pixels, mask, iterations, forbidden=None):
    """Four-neighbour padding, without wrapping opposite texture borders."""
    import numpy as np
    output, known = pixels.copy(), mask.copy()
    for _ in range(iterations):
        total = np.zeros_like(output)
        count = np.zeros(known.shape, dtype=np.float32)
        for dst, src in ((np.s_[1:, :], np.s_[:-1, :]), (np.s_[:-1, :], np.s_[1:, :]),
                         (np.s_[:, 1:], np.s_[:, :-1]), (np.s_[:, :-1], np.s_[:, 1:])):
            total[dst] += output[src] * known[src][..., None]
            count[dst] += known[src]
        new = (~known) & (count > 0)
        if forbidden is not None:
            new &= ~forbidden
        if not new.any():
            break
        output[new] = total[new] / count[new, None]
        known[new] = True
    return output


def _gaussian(values, sigma):
    """Separable zero-padded Gaussian using NumPy FFT; no SciPy dependency."""
    import numpy as np
    radius = max(1, int(math.ceil(sigma * 3)))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    result = values
    for axis in (0, 1):
        size = result.shape[axis]
        fft_size = 1 << (size + len(kernel) - 2).bit_length()
        shape = [1] * result.ndim
        shape[axis] = fft_size // 2 + 1
        transformed = np.fft.rfft(result, n=fft_size, axis=axis)
        transformed *= np.fft.rfft(kernel, n=fft_size).reshape(shape)
        result = np.fft.irfft(transformed, n=fft_size, axis=axis)
        selection = [slice(None)] * result.ndim
        selection[axis] = slice(radius, radius + size)
        result = result[tuple(selection)].astype(np.float32)
    return result


def _highpass_normal(rgb, valid, sigma, convention):
    import numpy as np
    normal = rgb * 2 - 1
    if sigma > 0 and valid.any():
        weight = _gaussian(valid.astype(np.float32), sigma)
        blurred = _gaussian(normal * valid[..., None], sigma) / np.maximum(weight[..., None], 1e-8)
        normal = normal - blurred + np.array((0, 0, 1), dtype=np.float32)
    lengths = np.linalg.norm(normal, axis=-1, keepdims=True)
    normal = np.divide(normal, lengths, out=np.zeros_like(normal), where=lengths > 1e-8)
    normal[(lengths[..., 0] <= 1e-8) | ~valid] = (0, 0, 1)
    rgb = (normal + 1) * 0.5
    if convention == "DIRECTX":
        rgb[..., 1] = 1 - rgb[..., 1]
    return rgb


class _BakeScene:
    """Own every temporary ID explicitly; never purge the user's orphan data."""
    def __init__(self, bpy, source_scene):
        self.bpy = bpy
        self.objects, self.meshes, self.materials, self.images = [], [], [], []
        self.scene = bpy.data.scenes.new("CBT AI bake temporary")
        try:
            self.scene.render.engine = "CYCLES"
            self.scene.cycles.device = "CPU"
            self.scene.cycles.samples = 1
            self.scene.unit_settings.scale_length = source_scene.unit_settings.scale_length
            self.scene.render.image_settings.file_format = "PNG"
            self.scene.render.image_settings.color_mode = "RGB"
            self.scene.render.image_settings.color_depth = "16"
            self.layer = self.scene.view_layers[0]
        except Exception:
            bpy.data.scenes.remove(self.scene)
            raise

    def copy_mesh(self, original, depsgraph):
        mesh = self.bpy.data.meshes.new_from_object(
            original.evaluated_get(depsgraph), preserve_all_data_layers=True, depsgraph=depsgraph)
        self.meshes.append(mesh)
        obj = self.bpy.data.objects.new("CBT AI bake " + original.name, mesh)
        self.objects.append(obj)
        obj.matrix_world = original.matrix_world.copy()
        self.scene.collection.objects.link(obj)
        return obj

    def material(self, source=None):
        material = source.copy() if source is not None else self.bpy.data.materials.new("CBT AI bake target")
        self.materials.append(material)
        material.use_nodes = True
        return material

    def image(self, resolution):
        image = self.bpy.data.images.new("CBT AI bake buffer", resolution, resolution, alpha=True, float_buffer=True)
        self.images.append(image)
        image.colorspace_settings.name = "Non-Color"
        return image

    def bake(self, target, sources, image, kind, options, margin=0, samples=1):
        import numpy as np
        nodes = []
        for mat in set(target.data.materials):
            node = mat.node_tree.nodes.new("ShaderNodeTexImage")
            node.image = image
            mat.node_tree.nodes.active = node
            nodes.append((mat, node))
        self.scene.cycles.samples = samples
        try:
            # Scene/view-layer overrides alone do not replace context selection:
            # Blender can still expose the source window's selected objects.
            selected = [target, *sources]
            with self.bpy.context.temp_override(
                    scene=self.scene, view_layer=self.layer,
                    object=target, active_object=target,
                    selected_objects=selected, selected_editable_objects=selected):
                for obj in self.objects:
                    obj.select_set(False, view_layer=self.layer)
                    obj.hide_render = obj not in [target, *sources]
                for obj in [target, *sources]:
                    obj.select_set(True, view_layer=self.layer)
                self.layer.objects.active = target
                self.layer.update()
                result = self.bpy.ops.object.bake(
                    type=kind, target="IMAGE_TEXTURES", save_mode="INTERNAL", use_clear=True,
                    use_selected_to_active=bool(sources), margin=margin, margin_type="EXTEND",
                    cage_extrusion=options["cage_extrusion"], max_ray_distance=options["max_ray_distance"],
                    normal_space="TANGENT", normal_g="POS_Y", uv_layer=target.data.uv_layers.active.name)
                if "FINISHED" not in result:
                    raise RuntimeError(f"Cycles {kind} bake returned {result}")
            pixels = np.empty(len(image.pixels), dtype=np.float32)
            image.pixels.foreach_get(pixels)
            return pixels.reshape(image.size[1], image.size[0], 4)[..., :3].copy()
        finally:
            for mat, node in nodes:
                mat.node_tree.nodes.remove(node)

    def save(self, image, pixels, path, color=False):
        import numpy as np
        rgba = np.ones((*pixels.shape[:2], 4), dtype=np.float32)
        rgb = np.clip(pixels, 0, 1)
        if color:
            # Image.save does not reliably encode generated float buffers when
            # their colorspace tag changes. Encode BaseColor explicitly, then
            # save bytes through Non-Color so Blender cannot apply another curve.
            rgb = np.where(rgb <= 0.0031308, rgb * 12.92,
                           1.055 * np.power(rgb, 1 / 2.4) - 0.055)
        rgba[..., :3] = rgb
        image.colorspace_settings.name = "Non-Color"
        image.pixels.foreach_set(rgba.ravel())
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
        image.colorspace_settings.name = "Non-Color"
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Texture was not saved: {path}")

    def close(self):
        bpy = self.bpy
        for obj in self.objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.scenes.remove(self.scene)
        for mesh in self.meshes:
            bpy.data.meshes.remove(mesh)
        for material in self.materials:
            bpy.data.materials.remove(material)
        for image in self.images:
            bpy.data.images.remove(image)


def _principled(material):
    """Accept an unambiguous active Principled surface, not an arbitrary node name."""
    tree = material.node_tree
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output]
    if len(outputs) != 1 or not outputs[0].inputs["Surface"].is_linked:
        raise InvalidStateError(f"Source material {material.name} needs an active surface output")
    socket = outputs[0].inputs["Surface"].links[0].from_socket
    if socket.node.type != "BSDF_PRINCIPLED":
        raise InvalidStateError(f"Source material {material.name} must have Principled BSDF directly connected to Surface")
    return outputs[0], socket.node


@contextmanager
def _emission(materials, channel=None):
    """Temporarily reroute copies only. None emits white for genuine ray-hit masks."""
    changes = []
    try:
        for material in materials:
            output, principled = _principled(material)
            tree = material.node_tree
            original = output.inputs["Surface"].links[0].from_socket
            emission = tree.nodes.new("ShaderNodeEmission")
            changes.append((tree, output, original, emission))
            if channel is not None:
                source = principled.inputs[channel]
                if source.is_linked:
                    tree.links.new(source.links[0].from_socket, emission.inputs["Color"])
                else:
                    value = source.default_value
                    emission.inputs["Color"].default_value = tuple(value) if channel == "Base Color" else (value, value, value, 1)
            else:
                emission.inputs["Color"].default_value = (1, 1, 1, 1)
            tree.links.new(emission.outputs[0], output.inputs["Surface"])
        yield
    finally:
        for tree, output, original, emission in changes:
            tree.links.new(original, output.inputs["Surface"])
            tree.nodes.remove(emission)


def bake_from_ai_source(params: dict[str, Any], *, progress=None) -> ToolResult:
    """Write a new texture-set directory and restore all persistent Blender IDs."""
    import numpy as np
    options = _options(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(options.get("expected_state"), bpy)
    if not bpy.app.background:
        from .background_render import BAKE_MODE, _manager

        child_params = {key: value for key, value in params.items() if key != "expected_state"}
        return _manager.start({"mode": BAKE_MODE, "options": child_params})
    original_scene = bpy.context.scene
    def report(phase, **details):
        if progress is not None:
            progress({"phase": phase, **details})

    report("preparing")
    originals = []
    for role in ("low", "high"):
        obj = original_scene.objects.get(options[role])
        if obj is None:
            raise NotFoundError(f"{role} object not found in current scene: {options[role]}")
        if obj.type != "MESH" or obj.mode != "OBJECT":
            raise InvalidStateError(f"{role} must be a mesh in Object mode")
        originals.append(obj)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    resources = None
    paths = {}
    run_dir = None
    resolution = options["resolution"]
    try:
        resources = _BakeScene(bpy, original_scene)
        low, high = (resources.copy_mesh(obj, depsgraph) for obj in originals)
        if not high.data.polygons or not low.data.polygons:
            raise InvalidStateError("low and high require nonempty evaluated surface meshes")
        inside, shell, owner, ambiguous, uv_stats = _uv_masks(
            low.data, resolution, options["part_attribute"], options["shell_parts"], options["max_uv_overlap_pct"])
        owned_parts = _owned_parts(low.data, owner, options["part_attribute"] or options["fill_face_attribute"])
        fill_owner_parts = (_owned_parts(low.data, owner, options["fill_face_attribute"])
                            if options["fill_parts"] and options["fill_face_attribute"] != options["part_attribute"]
                            else owned_parts)
        fill_mask = np.zeros_like(inside)
        fill_counts = {}
        for part_id in options["fill_parts"]:
            count = int((inside & (fill_owner_parts == part_id)).sum())
            if not count:
                raise InvalidParamsError(f"fill_parts id {part_id} owns no low UV texels at this resolution")
            fill_counts[str(part_id)] = count
            fill_mask |= inside & (fill_owner_parts == part_id)
        source_materials = list(high.data.materials)
        if not source_materials or any(m is None or not m.use_nodes for m in source_materials):
            raise InvalidStateError("All high material slots require node-based Principled materials")
        copied = [resources.material(mat) for mat in source_materials]
        material_indices = [face.material_index for face in high.data.polygons]
        high.data.materials.clear()
        for material in copied:
            _principled(material)
            high.data.materials.append(material)
        for face, index in zip(high.data.polygons, material_indices):
            face.material_index = index
        low.data.materials.clear()
        low.data.materials.append(resources.material())
        for face in low.data.polygons:
            face.material_index = 0
        nearest_uv, nearest_hit, nearest_stats = None, np.zeros_like(inside), {}
        source_uv_stats = None
        source_ambiguous_samples = 0
        nearest_geometric_hits = 0
        buffer = resources.image(resolution)
        source_buffer = None
        source_cache_coverage = None
        nearest_cache_misses = 0
        with _emission(copied):
            report("ray_bake", target_texels=int(inside.sum()))
            ray_hit = resources.bake(low, [high], buffer, "EMIT", options)[..., 0] > 0.5
            ray_projection_misses = int((inside & ~ray_hit).sum())
            ray_ambiguity_misses = int((inside & ray_hit & ambiguous).sum())
            ray_hit &= inside & ~ambiguous
            nearest_target = (shell | (inside & ~shell & ~ray_hit & ~ambiguous)) & ~fill_mask
            if nearest_target.any():
                _, _, _, source_ambiguous, source_uv_stats = _uv_masks(
                    high.data, resolution, max_uv_overlap_pct=options["max_uv_overlap_pct"])
                def nearest_progress(processed, total, hits, rejected):
                    report("nearest_projection", processed_texels=processed,
                           total_texels=total, geometric_hits=hits, distance_rejected=rejected)

                nearest_uv, nearest_hit, nearest_stats = _nearest_uv(
                    low, high, resolution, nearest_target, owner,
                    options["fallback_max_distance"], shell, nearest_progress)
                nearest_geometric_hits = int(nearest_hit.sum())
                if source_ambiguous.any():
                    rows, cols = np.nonzero(nearest_hit)
                    source_valid = (~source_ambiguous)[..., None].astype(np.float32)
                    unambiguous = _bilinear(source_valid, nearest_uv[rows, cols])[:, 0] >= 1 - 1e-6
                    source_ambiguous_samples = int((~unambiguous).sum())
                    nearest_hit[rows[~unambiguous], cols[~unambiguous]] = False
                source_buffer = resources.image(resolution)
                # A nearest geometric hit does not prove its cached texel was
                # baked. Check the same UV cache and padding using white emission.
                report("source_cache_coverage", nearest_texels=int(nearest_hit.sum()))
                source_cache_coverage = resources.bake(
                    high, [], source_buffer, "EMIT", options, margin=options["margin"])[..., :1]
                rows, cols = np.nonzero(nearest_hit)
                cache_filled = _bilinear(source_cache_coverage, nearest_uv[rows, cols])[:, 0] >= 0.999
                nearest_cache_misses = int((~cache_filled).sum())
                nearest_hit[rows[~cache_filled], cols[~cache_filled]] = False
        # Cycles cannot enforce our triangle owner at overlapping UV texels.
        # Mark these ray results unresolved instead of claiming an arbitrary hit.
        fallback_hit = nearest_hit & ~shell & ~ray_hit
        color_hit = (nearest_hit & shell) | ((ray_hit | fallback_hit) & ~shell) | fill_mask
        channels = {}
        for name, socket in (("BaseColor", "Base Color"), ("Roughness", "Roughness"), ("Metallic", "Metallic")):
            report("material_bake", channel=name)
            with _emission(copied, socket):
                pixels = resources.bake(low, [high], buffer, "EMIT", options)
                pixels[ambiguous] = 0
                if source_buffer is not None:
                    report("source_cache_bake", channel=name)
                    cached = resources.bake(high, [], source_buffer, "EMIT", options, margin=options["margin"])
                    pixels[shell] = 0
                    pixels[nearest_hit] = _bilinear(cached, nearest_uv[nearest_hit])
            channels[name] = pixels
        report("normal_bake")
        raw_normal = resources.bake(low, [high], buffer, "NORMAL", options)
        normal = _highpass_normal(raw_normal, ray_hit, options["highpass_sigma_px"], options["normal_convention"])
        normal_name = "NormalDX" if options["normal_convention"] == "DIRECTX" else "NormalGL"
        channels[normal_name] = normal
        report("ao_bake")
        channels["AO"] = resources.bake(low, [], buffer, "AO", options, samples=options["ao_samples"])
        channels["AO"][ambiguous] = 1  # Neutral AO where UV ownership is unresolved by Cycles.
        for part_id, fixed in options["fill_parts"].items():
            selected = fill_owner_parts == part_id
            channels["BaseColor"][selected] = fixed["base_color"]
            channels["Roughness"][selected] = fixed["roughness"]
            channels["Metallic"][selected] = fixed["metallic"]
            channels["AO"][selected] = fixed["ao"]
            channels[normal_name][selected] = (0.5, 0.5, 1.0)
        if options["pack"] == "ORM":
            channels["ORM"] = np.stack((channels["AO"][..., 0], channels["Roughness"][..., 0], channels["Metallic"][..., 0]), axis=-1)
        count = int(inside.sum())
        color_metrics = _color_metrics(channels["BaseColor"], inside, color_hit)
        misses = color_metrics["miss_texels"]
        normal_misses = int((inside & ~ray_hit).sum())
        occlusion_candidates = inside & ~shell & ~ray_hit & ~fill_mask & ~ambiguous
        report("occlusion_review", candidate_texels=int(occlusion_candidates.sum()), max_samples=8192)
        low_part_occlusion = _low_part_occlusion_samples(
            low, resolution, owner, owned_parts, occlusion_candidates, options["part_attribute"], options)
        part_breakdown = {}
        for part_id in np.unique(owned_parts[inside]):
            selected = inside & (owned_parts == part_id)
            part_breakdown[str(int(part_id))] = {
                "uv_texels": int(selected.sum()),
                "ray_miss_texels": int((selected & ~ray_hit).sum()),
                "normal_miss_texels": int((selected & ~ray_hit & ~fill_mask).sum()),
                "nearest_fallback_texels": int((selected & fallback_hit).sum()),
                "fixed_fill_texels": int((selected & fill_mask).sum()),
                "unresolved_color_texels": int((selected & ~color_hit).sum()),
            }
        output = Path(options["output_dir"]).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        prefix = re.sub(r"[^A-Za-z0-9_-]", "_", options["low"])[:80] or "Asset"
        run_dir = Path(tempfile.mkdtemp(prefix=f"{prefix}_ai_bake_", dir=output))
        report("writing_maps", map_count=len(channels))
        for name, pixels in channels.items():
            valid = inside if name == "AO" or name.startswith("Normal") else color_hit
            padded = _dilate(pixels, valid, options["margin"], forbidden=inside & ~valid)
            path = run_dir / f"T_{prefix}_{name}.png"
            resources.save(buffer, padded, path, color=name == "BaseColor")
            paths[name] = str(path)
        mask_outputs = [("UVMask", inside), ("ShellMask", shell), ("ColorHitMask", color_hit),
                        ("RayHitMask", ray_hit), ("FallbackHitMask", fallback_hit),
                        ("UVOverlapMask", ambiguous)]
        if fill_mask.any():
            mask_outputs.append(("FixedFillMask", fill_mask))
        if source_cache_coverage is not None:
            mask_outputs.append(("SourceCacheHitMask", source_cache_coverage[..., 0] >= 0.999))
        for name, mask in mask_outputs:
            path = run_dir / f"T_{prefix}_{name}.png"
            resources.save(buffer, np.repeat(mask[..., None], 3, axis=-1).astype(np.float32), path)
            paths[name] = str(path)
        warnings = []
        if uv_stats["ambiguous_texels"]:
            warnings.append("UV overlap is within the requested tolerance. Shell nearest transfer uses the lowest triangle index; ambiguous ray texels count as misses, with neutral normals/AO.")
        if source_uv_stats and source_uv_stats["ambiguous_texels"]:
            warnings.append("Source UV overlap is within tolerance; nearest samples touching ambiguous source-cache texels count as misses.")
        if misses:
            warnings.append("Color projection has uncovered texels; inspect ColorHitMask before accepting the texture set.")
        if normal_misses:
            warnings.append("Unfilled normal ray misses use neutral tangent-space normals; fixed-fill parts use their requested flat normals. Neither retains source normal detail.")
        if source_buffer is not None:
            warnings.append("Nearest channels sample a source-UV emission cache at the requested resolution; sub-texel source detail may be resampled.")
        return ToolResult(data={
            "low": options["low"], "high": options["high"], "resolution": resolution,
            "paths": paths, "output_dir": str(run_dir), "normal_convention": options["normal_convention"],
            "uv_texels": count, "uv_coverage_ratio": count / (resolution * resolution),
            "uv_overlap_pct": uv_stats["uv_overlap_pct"], "ambiguous_texels": uv_stats["ambiguous_texels"],
            "uv_overlap": uv_stats, "source_uv_overlap": source_uv_stats,
            "nearest_ambiguous_source_samples": source_ambiguous_samples,
            **color_metrics,
            "nearest_geometric_hit_texels": nearest_geometric_hits,
            "nearest_cache_miss_texels": nearest_cache_misses,
            "ray_projection_miss_texels": ray_projection_misses,
            "ray_ambiguity_miss_texels": ray_ambiguity_misses,
            "fallback_candidate_texels": int((inside & ~shell & ~ray_hit & ~ambiguous & ~fill_mask).sum()),
            "fallback_hit_texels": int(fallback_hit.sum()),
            "fallback_max_distance": options["fallback_max_distance"],
            "fixed_fill_texels": int(fill_mask.sum()),
            "fixed_fill_parts": fill_counts,
            "fill_face_attribute": options["fill_face_attribute"],
            "part_breakdown": part_breakdown,
            "low_part_occlusion": low_part_occlusion,
            "normal_miss_texels": normal_misses,
            "normal_miss_pct": 100 * normal_misses / count,
            "normal_flat_fallback_texels": int((inside & ~ray_hit & ~fill_mask).sum()),
            "normal_fixed_fill_texels": int(fill_mask.sum()),
            "shell_texels": int(shell.sum()), "nearest_hit_texels": int(nearest_hit.sum()),
            "ray_part_texels": int((inside & ~shell).sum()),
            "ray_part_miss_texels": int((inside & ~shell & ~ray_hit).sum()),
            "ray_part_unfilled_miss_texels": int((inside & ~shell & ~ray_hit & ~fill_mask).sum()),
            "nearest": nearest_stats, "coverage_method": "UV texel centers, white-emission ray hits, bounded nearest fallback, fixed FACE INT part fill and source-cache fill; target metrics before padding",
            "source_cache_resolution": resolution if source_buffer is not None else None,
            "device": "CPU", "scene_outputs": [], "blend_saved": False,
        }, warnings=warnings)
    except (InvalidParamsError, InvalidStateError, NotFoundError):
        raise
    except Exception as exc:
        raise MutationApplyError(f"bake.from_ai_source failed: {exc}", {
            "paths": paths, "output_dir": str(run_dir) if run_dir else None,
            "partial_change_possible": bool(paths), "blend_saved": False,
        }) from exc
    finally:
        if resources is not None:
            resources.close()
