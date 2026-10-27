"""Measured range-of-motion poses with deferred image review."""

from __future__ import annotations

import json
from math import isfinite, radians, sqrt
from pathlib import Path
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import (DEFERRED_CONTEXT_KEY, DeferredEnvelope,
                        DeferredToolResult, ToolResult, active_context)
from ._blender import get_bpy
from .form_views import depth_range, install_form_override, view_mode
from .render import (_add_depth_legend, _cleanup_camera, _detail_camera, _detail_direction,
                     _execute_render_plan, _isolate_objects, _raise_after_cleanup,
                     _render_state, _restore_objects, _restore_render_state,
                     _validate_overwrite, _validate_pixel_budget, render_contact_sheet)
from .session_lease import require_document_write_access
from .session_state import check_expected_state, current_state_token


_PRESET_DIR = Path(__file__).resolve().parent.parent / "data" / "rom"
_AXES = {"X": 0, "Y": 1, "Z": 2}
_MAX_POSES = 12


def _finite(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not low <= value <= high:
        raise InvalidParamsError(f"{name} must be finite from {low} to {high}")
    return float(value)


def _poses(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_POSES:
        raise InvalidParamsError(f"poses must contain 1 to {_MAX_POSES} poses")
    result = []
    names = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"name", "bones"}:
            raise InvalidParamsError(f"poses[{index}] requires name and bones")
        name = item["name"]
        if (not isinstance(name, str) or not name.strip() or name in names or
                any(char in name for char in "\\/:*?\"<>|")):
            raise InvalidParamsError("pose names must be unique non-empty strings")
        names.add(name)
        bones = item["bones"]
        if not isinstance(bones, list) or not 1 <= len(bones) <= 8:
            raise InvalidParamsError(f"pose {name} requires 1 to 8 bone rotations")
        rotations = []
        used = set()
        for rotation in bones:
            if not isinstance(rotation, dict) or set(rotation) != {"bone", "axis", "degrees"}:
                raise InvalidParamsError(f"pose {name} bone rotation requires bone, axis, degrees")
            bone, axis = rotation["bone"], rotation["axis"]
            if not isinstance(bone, str) or not bone.strip() or bone in used or axis not in _AXES:
                raise InvalidParamsError(f"pose {name} has invalid or duplicate bone/axis")
            used.add(bone)
            degrees = _finite(rotation["degrees"], f"pose {name} degrees", -180, 180)
            rotations.append({"bone": bone, "axis": axis, "degrees": degrees})
        result.append({"name": name, "bones": rotations})
    return result


def _evaluated_mesh(bpy, obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        if len(mesh.vertices) != len(obj.data.vertices):
            raise InvalidStateError("Armature evaluation changed vertex count; ROM requires fixed topology")
        mesh.calc_loop_triangles()
        matrix = evaluated.matrix_world
        return {
            "vertices": [matrix @ vertex.co for vertex in mesh.vertices],
            "edges": [tuple(edge.vertices) for edge in mesh.edges],
            "triangles": [tuple(triangle.vertices) for triangle in mesh.loop_triangles],
        }
    finally:
        evaluated.to_mesh_clear()


def _local_hull_volume(points) -> float | None:
    if len(points) < 4:
        return None
    import bmesh
    bm = bmesh.new()
    try:
        verts = [bm.verts.new(point) for point in points]
        bmesh.ops.convex_hull(bm, input=verts)
        value = abs(float(bm.calc_volume(signed=True)))
        return value if isfinite(value) and value > 1e-12 else None
    except (ValueError, RuntimeError):
        return None
    finally:
        bm.free()


def _cross_section_area(points, indices, center, direction) -> float | None:
    from mathutils import Vector

    if len(indices) < 4 or direction.length < 1e-9:
        return None
    axis = direction.normalized()
    reference = Vector((1, 0, 0)) if abs(axis.x) < .8 else Vector((0, 1, 0))
    tangent = axis.cross(reference).normalized()
    bitangent = axis.cross(tangent).normalized()
    coords = [((points[index] - center).dot(tangent), (points[index] - center).dot(bitangent))
              for index in indices]
    mean_u = sum(pair[0] for pair in coords) / len(coords)
    mean_v = sum(pair[1] for pair in coords) / len(coords)
    uu = sum((pair[0] - mean_u) ** 2 for pair in coords) / len(coords)
    vv = sum((pair[1] - mean_v) ** 2 for pair in coords) / len(coords)
    uv = sum((pair[0] - mean_u) * (pair[1] - mean_v) for pair in coords) / len(coords)
    determinant = uu * vv - uv * uv
    return sqrt(max(0.0, determinant)) if determinant > 1e-14 else None


def _region_samples(rest_vertices, armature, bone, limit=256):
    center = armature.matrix_world @ bone.head_local
    radius = max(float(bone.length) * .8, 0.05)
    nearby = [(index, (point - center).length) for index, point in enumerate(rest_vertices)
              if (point - center).length <= radius]
    nearby.sort(key=lambda item: item[1])
    if len(nearby) > limit:
        stride = len(nearby) / limit
        nearby = [nearby[int(index * stride)] for index in range(limit)]
    return [index for index, _distance in nearby], center, radius


def _edge_change(rest, posed):
    extremes = {"stretch": (1.0, None), "compression": (1.0, None)}
    for a, b in rest["edges"]:
        old = (rest["vertices"][a] - rest["vertices"][b]).length
        if old <= 1e-9:
            continue
        ratio = (posed["vertices"][a] - posed["vertices"][b]).length / old
        if ratio > extremes["stretch"][0]:
            extremes["stretch"] = (ratio, (a, b))
        if ratio < extremes["compression"][0]:
            extremes["compression"] = (ratio, (a, b))
    def receipt(kind):
        ratio, edge = extremes[kind]
        return {"ratio": ratio, "edge": list(edge) if edge else None,
                "focus": list((posed["vertices"][edge[0]] + posed["vertices"][edge[1]]) * .5)
                if edge else None}
    return {"max_stretch": receipt("stretch"), "max_compression": receipt("compression")}


def _self_intersections(geometry, max_triangles):
    triangles = geometry["triangles"]
    if len(triangles) > max_triangles:
        return {"status": "undetermined", "reason": "triangle_limit", "triangle_count": len(triangles)}
    if not triangles:
        return {"status": "undetermined", "reason": "no_triangles", "triangle_count": 0}
    from mathutils.bvhtree import BVHTree
    tree = BVHTree.FromPolygons(geometry["vertices"], triangles, all_triangles=True)
    pairs = set()
    focus = None
    for left, right in tree.overlap(tree):
        if left >= right or set(triangles[left]) & set(triangles[right]):
            continue
        pairs.add((left, right))
        if focus is None:
            points = [geometry["vertices"][vertex] for vertex in triangles[left]]
            focus = list(sum(points[1:], points[0].copy()) / 3)
    return {"status": "measured", "count": len(pairs), "focus": focus,
            "triangle_count": len(triangles)}


def _apply_pose(bpy, armature, pose):
    from mathutils import Matrix

    for bone in armature.pose.bones:
        bone.rotation_mode = "XYZ"
        bone.matrix_basis = Matrix.Identity(4)
    for rotation in pose["bones"]:
        bone = armature.pose.bones[rotation["bone"]]
        bone.rotation_euler[_AXES[rotation["axis"]]] = radians(rotation["degrees"])
    bpy.context.view_layer.update()


def _restore_pose(bpy, armature, saved):
    errors = []
    for name, (mode, matrix) in saved.items():
        try:
            bone = armature.pose.bones.get(name)
            if bone is not None:
                bone.rotation_mode = mode
                bone.matrix_basis = matrix
        except Exception as exc:
            errors.append(f"restore pose {name}: {exc}")
    try:
        bpy.context.view_layer.update()
    except Exception as exc:
        errors.append(f"update restored pose: {exc}")
    return errors


def _pose_metrics(rest, posed, armature, sample_regions, baseline_intersections,
                  max_triangles, thresholds):
    edge = _edge_change(rest, posed)
    intersections = _self_intersections(posed, max_triangles)
    if intersections["status"] == "measured" and baseline_intersections["status"] == "measured":
        intersections["new_count"] = max(0, intersections["count"] - baseline_intersections["count"])
    regions = []
    focus = []
    failures = []
    undetermined = []
    for bone_name, (indices, rest_center, _radius) in sample_regions.items():
        rest_bone = armature.data.bones[bone_name]
        posed_bone = armature.pose.bones[bone_name]
        posed_center = armature.matrix_world @ posed_bone.head
        rest_direction = armature.matrix_world.to_3x3() @ rest_bone.vector
        posed_direction = armature.matrix_world.to_3x3() @ (posed_bone.tail - posed_bone.head)
        old_volume = _local_hull_volume([rest["vertices"][index] for index in indices])
        new_volume = _local_hull_volume([posed["vertices"][index] for index in indices])
        old_area = _cross_section_area(rest["vertices"], indices, rest_center, rest_direction)
        new_area = _cross_section_area(posed["vertices"], indices, posed_center, posed_direction)
        volume_ratio = new_volume / old_volume if old_volume and new_volume is not None else None
        twist_ratio = new_area / old_area if old_area and new_area is not None else None
        region = {"bone": bone_name, "sample_vertices": len(indices),
                  "local_volume_ratio": volume_ratio, "cross_section_area_ratio": twist_ratio,
                  "volume_method": "sampled_local_convex_hull",
                  "twist_method": "perpendicular_covariance_area",
                  "focus": list(posed_center)}
        regions.append(region)
        if volume_ratio is None or twist_ratio is None:
            undetermined.append(f"{bone_name}: insufficient region geometry")
        else:
            if volume_ratio < thresholds["min_volume_ratio"] or volume_ratio > thresholds["max_volume_ratio"]:
                failures.append(f"{bone_name}: local_volume_ratio")
                focus.append({"reason": "local_volume_change", "bone": bone_name, "point": list(posed_center)})
            if twist_ratio < thresholds["min_twist_area_ratio"]:
                failures.append(f"{bone_name}: cross_section_collapse")
                focus.append({"reason": "twist_collapse", "bone": bone_name, "point": list(posed_center)})
    if edge["max_stretch"]["ratio"] > thresholds["max_edge_stretch"]:
        failures.append("edge_stretch")
        focus.append({"reason": "edge_stretch", "point": edge["max_stretch"]["focus"]})
    if edge["max_compression"]["ratio"] < thresholds["min_edge_compression"]:
        failures.append("edge_compression")
        focus.append({"reason": "edge_compression", "point": edge["max_compression"]["focus"]})
    if intersections["status"] != "measured" or "new_count" not in intersections:
        undetermined.append("self_intersections not measured")
    elif intersections["new_count"] > 0:
        failures.append("new_self_intersections")
        focus.append({"reason": "self_intersection", "point": intersections["focus"]})
    return {"edge": edge, "regions": regions, "self_intersections": intersections,
            "acceptance": "fail" if failures else "undetermined" if undetermined else "pass",
            "failures": failures, "undetermined": undetermined,
            "focus": focus[:8] or ([{"reason": "largest_edge_stretch", "point": edge["max_stretch"]["focus"]}]
                            if edge["max_stretch"]["focus"] is not None else [])}


def rom_test(params: dict[str, Any]) -> ToolResult | DeferredToolResult:
    """Measure editable body-plan poses and render a deferred contact sheet."""
    allowed = {"object_name", "armature_name", "body_plan", "poses", "output_prefix",
               "width", "height", "view_mode", "direction", "overwrite", "allow_large", "max_triangles",
               "min_volume_ratio", "max_volume_ratio", "max_edge_stretch",
               "min_edge_compression", "min_twist_area_ratio", "expected_state"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    name = params.get("object_name")
    rig_name = params.get("armature_name")
    if not isinstance(name, str) or not name.strip() or not isinstance(rig_name, str) or not rig_name.strip():
        raise InvalidParamsError("object_name and armature_name must be non-empty names")
    prefix_raw = params.get("output_prefix")
    if not isinstance(prefix_raw, str) or not prefix_raw.strip():
        raise InvalidParamsError("output_prefix must be a non-empty file prefix")
    prefix = Path(prefix_raw).expanduser().resolve()
    if prefix.suffix.lower() == ".png":
        prefix = prefix.with_suffix("")
    width, height = params.get("width", 256), params.get("height", 256)
    if any(isinstance(value, bool) or not isinstance(value, int) or not 128 <= value <= 2048
           for value in (width, height)):
        raise InvalidParamsError("width and height must be integers from 128 to 2048")
    mode = view_mode({"view_mode": params.get("view_mode", "matcap")})
    direction_name = params.get("direction", "three_quarter")
    direction = _detail_direction(direction_name)
    overwrite = params.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise InvalidParamsError("overwrite must be a boolean")
    allow_large = params.get("allow_large", False)
    if not isinstance(allow_large, bool):
        raise InvalidParamsError("allow_large must be a boolean")
    max_triangles = params.get("max_triangles", 20000)
    if isinstance(max_triangles, bool) or not isinstance(max_triangles, int) or not 1 <= max_triangles <= 100000:
        raise InvalidParamsError("max_triangles must be 1 to 100000")
    thresholds = {"min_volume_ratio": _finite(params.get("min_volume_ratio", .6), "min_volume_ratio", .01, 1),
                  "max_volume_ratio": _finite(params.get("max_volume_ratio", 1.4), "max_volume_ratio", 1, 10),
                  "max_edge_stretch": _finite(params.get("max_edge_stretch", 1.5), "max_edge_stretch", 1, 10),
                  "min_edge_compression": _finite(params.get("min_edge_compression", .67), "min_edge_compression", .01, 1),
                  "min_twist_area_ratio": _finite(params.get("min_twist_area_ratio", .5), "min_twist_area_ratio", .01, 1)}
    preset_status = None
    if "poses" in params:
        poses = _poses(params["poses"])
        body_plan = params.get("body_plan", "custom")
    else:
        body_plan = params.get("body_plan")
        if body_plan not in {"humanoid", "quadruped", "reptile"}:
            raise InvalidParamsError("body_plan must be humanoid, quadruped, or reptile when poses is omitted")
        preset = json.loads((_PRESET_DIR / f"{body_plan}.json").read_text(encoding="utf-8"))
        poses = _poses(preset["poses"])
        preset_status = preset["status"]
    if not isinstance(body_plan, str) or not body_plan.strip():
        raise InvalidParamsError("body_plan must be a non-empty string")
    columns = min(3, len(poses))
    rows = (len(poses) + columns - 1) // columns
    _validate_pixel_budget(width * columns, (height + 18) * rows, allow_large)
    paths = {pose["name"]: prefix.parent / f"{prefix.name}_{index:02d}_{pose['name']}.png"
             for index, pose in enumerate(poses)}
    sheet_path = prefix.parent / f"{prefix.name}_contact_sheet.png"
    for path in (*paths.values(), sheet_path):
        _validate_overwrite(path, overwrite)

    bpy = get_bpy()
    if not bpy.app.background and not isinstance(direction_name, str):
        raise InvalidParamsError("GUI ROM fast capture requires a named direction")
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    scene = bpy.context.scene
    obj = scene.objects.get(name)
    armature = scene.objects.get(rig_name)
    if obj is None or obj.type != "MESH":
        raise NotFoundError(f"Skinned mesh not found: {name}")
    if not obj.data.vertices:
        raise InvalidStateError("ROM mesh has no vertices")
    if armature is None or armature.type != "ARMATURE":
        raise NotFoundError(f"Armature not found: {rig_name}")
    if obj.mode != "OBJECT" or armature.mode != "OBJECT":
        raise InvalidStateError("ROM test requires mesh and armature in Object mode")
    if not any(mod.type == "ARMATURE" and mod.object is armature and mod.show_viewport
               for mod in obj.modifiers):
        raise InvalidStateError("Mesh has no enabled Armature modifier bound to the requested rig")
    if armature.data.pose_position != "POSE":
        raise InvalidStateError("Armature must be in pose position")
    saved = {bone.name: (bone.rotation_mode, bone.matrix_basis.copy()) for bone in armature.pose.bones}
    valid = []
    skipped = []
    for pose in poses:
        missing = [item["bone"] for item in pose["bones"] if armature.pose.bones.get(item["bone"]) is None]
        if missing:
            skipped.append({"pose": pose["name"], "missing_bones": missing})
        else:
            valid.append(pose)
    if not valid:
        raise InvalidStateError("No ROM poses match the armature bone names")
    from mathutils import Vector
    measurements = []
    try:
        _apply_pose(bpy, armature, {"bones": []})
        rest = _evaluated_mesh(bpy, obj)
        capture_low = [min(vertex[axis] for vertex in rest["vertices"]) for axis in range(3)]
        capture_high = [max(vertex[axis] for vertex in rest["vertices"]) for axis in range(3)]
        baseline_intersections = _self_intersections(rest, max_triangles)
        tested_bones = {rotation["bone"] for pose in valid for rotation in pose["bones"]}
        regions = {bone_name: _region_samples(rest["vertices"], armature, armature.data.bones[bone_name])
                   for bone_name in tested_bones}
        for pose in valid:
            _apply_pose(bpy, armature, pose)
            posed = _evaluated_mesh(bpy, obj)
            for axis in range(3):
                capture_low[axis] = min(capture_low[axis], min(vertex[axis] for vertex in posed["vertices"]))
                capture_high[axis] = max(capture_high[axis], max(vertex[axis] for vertex in posed["vertices"]))
            metrics = _pose_metrics(rest, posed, armature,
                                    {item["bone"]: regions[item["bone"]] for item in pose["bones"]},
                                    baseline_intersections, max_triangles, thresholds)
            measurements.append({"name": pose["name"], "rotations": pose["bones"], **metrics})
    finally:
        errors = _restore_pose(bpy, armature, saved)
        if errors:
            raise InvalidStateError("Failed to restore armature pose: " + "; ".join(errors))

    # All pose images share one measured framing envelope, including an
    # explicit margin for silhouettes that move beyond the rest bound box.
    span = max(capture_high[axis] - capture_low[axis] for axis in range(3))
    padding = max(.01, span * .1)
    framing = (Vector(tuple(value - padding for value in capture_low)),
               Vector(tuple(value + padding for value in capture_high)))

    prefix.parent.mkdir(parents=True, exist_ok=True)
    if not bpy.app.background:
        context = dict(active_context() or {})
        if not context.get(DEFERRED_CONTEXT_KEY):
            raise InvalidStateError("GUI ROM fast capture requires bridge-managed deferred execution")
        # Dispatch the existing capture tool under the original client context.
        # The outer deferred result keeps the bridge queue paused until all
        # pose captures and the CPU contact sheet complete.
        from ..dispatcher import create_registry
        nested_registry = create_registry()
        deferred = DeferredToolResult()
        job = {"index": 0, "child": None, "done": False}

        def restore():
            return _restore_pose(bpy, armature, saved)

        def finish(error=None):
            if job["done"]:
                return
            job["done"] = True
            cleanup = restore()
            if error is None and not cleanup:
                try:
                    sheet = render_contact_sheet({
                        "images": [{"path": str(paths[pose["name"]]), "label": pose["name"]}
                                   for pose in valid],
                        "output_path": str(sheet_path), "columns": min(3, len(valid)),
                        "overwrite": overwrite, "view_mode": mode, "allow_large": allow_large,
                    }).data
                    statuses = [item["acceptance"] for item in measurements]
                    acceptance = ("fail" if "fail" in statuses else
                                  "undetermined" if skipped or "undetermined" in statuses else "pass")
                    deferred.resolve(ToolResult(data={
                        "object": obj.name, "armature": armature.name, "body_plan": body_plan,
                        "preset_status": preset_status, "poses": measurements,
                        "skipped_poses": skipped, "baseline_self_intersections": baseline_intersections,
                        "thresholds": thresholds, "view_mode": mode,
                        "contact_sheet": sheet["path"],
                        "pose_images": {pose["name"]: str(paths[pose["name"]]) for pose in valid},
                        "capture_backend": "scene.fast_capture", "acceptance": acceptance,
                        "changed": False, "state_token": current_state_token(bpy),
                    }, warnings=["Some preset poses were skipped because bones are absent"] if skipped else []))
                except Exception as exc:
                    deferred.reject(exc)
            else:
                if cleanup and error is not None:
                    error.add_note("ROM pose cleanup: " + "; ".join(cleanup))
                deferred.reject(error or InvalidStateError("ROM pose cleanup failed: " + "; ".join(cleanup)))

        def on_capture(envelope):
            job["child"] = None
            if job["done"]:
                return
            if not envelope["ok"]:
                finish(InvalidStateError("Pose fast capture failed: " +
                                         envelope.get("error", {}).get("message", "unknown error")))
                return
            job["index"] += 1
            try:
                bpy.app.timers.register(next_pose, first_interval=.01)
            except Exception as exc:
                finish(exc)

        def next_pose():
            if job["done"]:
                return None
            if job["index"] >= len(valid):
                finish()
                return None
            pose = valid[job["index"]]
            try:
                _apply_pose(bpy, armature, pose)
                envelope = nested_registry.dispatch("scene.fast_capture", {
                    "bounds": {"min": list(framing[0]), "max": list(framing[1])},
                    "views": [direction_name],
                    "projection": "orthographic", "width": width, "height": height,
                    "view_mode": mode, "overlays": False,
                    "output_path": str(paths[pose["name"]]), "overwrite": overwrite,
                }, context)
                if not isinstance(envelope, DeferredEnvelope):
                    if not envelope.get("ok", False):
                        raise InvalidStateError("Pose fast capture failed: " +
                                                envelope.get("error", {}).get("message", "unknown error"))
                    raise InvalidStateError("Pose fast capture did not return a deferred result")
                job["child"] = envelope
                envelope.add_done_callback(on_capture)
            except Exception as exc:
                finish(exc)
            return None

        def cancel():
            job["done"] = True
            child = job["child"]
            if child is not None:
                child.cancel()
            errors = restore()
            if errors:
                raise InvalidStateError("ROM pose cleanup failed: " + "; ".join(errors))

        deferred.set_cancel_callback(cancel)
        next_pose()
        return deferred

    previous_render = _render_state(scene)
    previous_visibility = []
    def plan():
        camera = camera_data = None
        restore_form = None
        error = result = None
        cleanup = []
        try:
            previous_visibility.extend(_isolate_objects(scene, [obj]))
            camera, camera_data, camera_receipt = _detail_camera(
                bpy, scene, framing, direction, width, height, "orthographic")
            depth = depth_range(framing, camera, scene) if mode == "depth" else None
            restore_form = install_form_override(bpy, scene, mode, depth)
            for pose in valid:
                _apply_pose(bpy, armature, pose)
                yield camera, paths[pose["name"]], width, height
                if depth is not None:
                    _add_depth_legend(bpy, paths[pose["name"]], depth)
            sheet = render_contact_sheet({
                "images": [{"path": str(paths[pose["name"]]), "label": pose["name"]} for pose in valid],
                "output_path": str(sheet_path), "columns": min(3, len(valid)),
                "overwrite": overwrite, "view_mode": mode, "allow_large": allow_large,
            }).data
            statuses = [item["acceptance"] for item in measurements]
            acceptance = ("fail" if "fail" in statuses else
                          "undetermined" if skipped or "undetermined" in statuses else "pass")
            result = ToolResult(data={
                "object": obj.name, "armature": armature.name, "body_plan": body_plan,
                "preset_status": preset_status, "poses": measurements, "skipped_poses": skipped,
                "baseline_self_intersections": baseline_intersections,
                "thresholds": thresholds, "view_mode": mode, "camera": camera_receipt,
                "contact_sheet": sheet["path"],
                "pose_images": {pose["name"]: str(paths[pose["name"]]) for pose in valid},
                "capture_backend": "deferred_render_headless",
                "acceptance": acceptance, "changed": False,
                "state_token": current_state_token(bpy),
            }, warnings=["Some preset poses were skipped because bones are absent"] if skipped else [])
        except Exception as exc:
            error = exc
        cleanup.extend(_restore_pose(bpy, armature, saved))
        if restore_form is not None:
            cleanup.extend(restore_form())
        try:
            _restore_render_state(scene, previous_render)
        except Exception as exc:
            cleanup.append(f"restore render state: {exc}")
        _restore_objects(previous_visibility, cleanup)
        _cleanup_camera(bpy, camera, camera_data, cleanup)
        _raise_after_cleanup(error, cleanup, "ROM test rendered")
        return result

    return _execute_render_plan(bpy, scene, plan())
