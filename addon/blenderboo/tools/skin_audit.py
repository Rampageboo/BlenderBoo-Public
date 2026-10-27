"""Bounded, advisory skin-weight and evaluated deformation measurements.

No artist-facing quality gate: a lower spill number alone does not establish a
better skin. Pose sampling temporarily changes evaluation state, restored in a
finally block; it never edits weights, actions, meshes, or saved files.
"""

from __future__ import annotations

import math
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ._blender import get_bpy
from .session_state import current_state_token
from .skinning import (_budget, _integer, _mesh_object, _name, _number,
                       _optional_name, _resolve_armature, _unknown)


AXES = {"X": 0, "Y": 1, "Z": 2}
_TRANSFORM_FIELDS = ("rotation_mode", "location", "scale", "rotation_euler", "rotation_quaternion", "rotation_axis_angle")
DEFAULT_THRESHOLDS = {
    "side_bleed_fraction": .05, "far_spill_fraction": .1,
    "edge_stretch_p99": 1.5, "edge_stretch_max": 3.0,
    "edge_compression_min": .2, "added_intersections": 0,
}
LIMITATIONS = [
    "Threshold flags are advisory; no pass/fail acceptance is inferred.",
    "Side is geometric, derived from the rest bone midpoint; bone names do not define left/right.",
    "Far spill is a heuristic using distance to the finite rest bone segment and its dominant-vertex radius.",
    "Low spill can accompany joint cracks; compare pose metrics and visual joint review before reweighting.",
    "Intersections count nonadjacent triangle pairs including contact; pairs sharing any vertex are excluded.",
    "Only explicit samples are measured; no claim is made about unsampled motion or enclosed volume.",
    "Explicit samples require an unconstrained, undriven armature; action curves must animate transform channels only.",
    "Frame sampling can evaluate other animated scene objects and handlers; custom handler side effects are outside restoration.",
]


def _quantile(values, fraction):
    """Linear interpolation between sorted ranks, including both endpoints."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _sub(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _length(a):
    return math.sqrt(_dot(a, a))


def _segment_distance(point, head, tail):
    vector = _sub(tail, head)
    squared = _dot(vector, vector)
    t = min(1.0, max(0.0, _dot(_sub(point, head), vector) / squared)) if squared else 0.0
    return _length(tuple(point[i] - head[i] - vector[i] * t for i in range(3)))


def _bone_metrics(name, head, tail, points, weights, dominant, axis,
                  midline, tolerance, radius_fraction, far_multiplier, thresholds):
    midpoint = (head[axis] + tail[axis]) * .5 - midline
    side = 1 if midpoint > tolerance else -1 if midpoint < -tolerance else 0
    core = [_segment_distance(points[index], head, tail) for index in dominant]
    radius = _quantile(core, radius_fraction)
    usable_radius = radius is not None and radius > 1e-12
    total = sum(weight for _index, weight in weights)
    bleed = [(index, weight) for index, weight in weights
             if side and side * (points[index][axis] - midline) < -tolerance]
    spill = [(index, weight) for index, weight in weights
             if usable_radius and _segment_distance(points[index], head, tail) > radius * far_multiplier]

    def measure(entries, available=True):
        mass = sum(weight for _index, weight in entries)
        return {"status": "measured" if available else "undetermined",
                "vertex_count": len(entries) if available else None,
                "weight_mass": mass if available else None,
                "weight_fraction": mass / total if available and total else None,
                "example_vertices": [index for index, _weight in entries[:10]] if available else []}

    side_result = measure(bleed, bool(side))
    if not side:
        side_result["reason"] = "bone_midpoint_on_midline"
    far_result = measure(spill, usable_radius)
    if not usable_radius:
        far_result["reason"] = "no_positive_dominant_radius"
    flags = []
    for label, data, threshold in (("side_bleed", side_result, "side_bleed_fraction"),
                                   ("far_spill", far_result, "far_spill_fraction")):
        fraction = data["weight_fraction"]
        if fraction is not None and fraction > thresholds[threshold]:
            flags.append(label)
    return {"bone": name, "geometric_side": side, "influenced_vertex_count": len(weights),
            "weight_mass": total, "dominant_vertex_count": len(dominant),
            "dominant_radius": radius, "far_distance_threshold": radius * far_multiplier if usable_radius else None,
            "side_bleed": side_result, "far_spill": far_result, "flags": flags}


def _triangles_intersect(left, right, epsilon):
    """Triangle SAT; add in-plane axes for parallel/coplanar faces.

    Axes are normalized so epsilon remains a distance in world units. Contact
    counts as intersection. Degenerate faces are removed before this function.
    """
    left_edges = [_sub(left[(i + 1) % 3], left[i]) for i in range(3)]
    right_edges = [_sub(right[(i + 1) % 3], right[i]) for i in range(3)]
    left_normal = _cross(left_edges[0], left_edges[1])
    right_normal = _cross(right_edges[0], right_edges[1])
    axes = [left_normal, right_normal]
    axes.extend(_cross(a, b) for a in left_edges for b in right_edges)
    axes.extend(_cross(left_normal, edge) for edge in left_edges + right_edges)
    for vector in axes:
        length = _length(vector)
        if length <= 1e-20:
            continue
        unit = tuple(value / length for value in vector)
        a = [_dot(point, unit) for point in left]
        b = [_dot(point, unit) for point in right]
        if max(a) < min(b) - epsilon or max(b) < min(a) - epsilon:
            return False
    return True


def _intersection_pairs(geometry, max_pair_checks):
    points, triangles = geometry["vertices"], geometry["triangles"]
    if not points or not triangles:
        return set(), {"status": "measured", "count": 0, "pair_checks": 0, "degenerate_triangles": 0}
    span = max(max(point[i] for point in points) - min(point[i] for point in points) for i in range(3))
    epsilon = max(span * 1e-9, 1e-12)
    boxes = []
    degenerate = 0
    for index, triangle in enumerate(triangles):
        coords = [points[vertex] for vertex in triangle]
        if _length(_cross(_sub(coords[1], coords[0]), _sub(coords[2], coords[0]))) <= epsilon * epsilon:
            degenerate += 1
            continue
        boxes.append((tuple(min(point[i] for point in coords) for i in range(3)),
                      tuple(max(point[i] for point in coords) for i in range(3)), index))
    boxes.sort(key=lambda item: (item[0][0], item[2]))
    active = []
    pairs = set()
    checks = 0
    for low, high, index in boxes:
        # Count every active-box visit, including x-expired boxes, so dense or
        # pathological inputs cannot hide quadratic work outside the budget.
        remaining = []
        for other_low, other_high, other_index in active:
            if checks >= max_pair_checks:
                return pairs, {"status": "incomplete", "reason": "pair_check_limit",
                               "count": len(pairs), "pair_checks": checks,
                               "degenerate_triangles": degenerate, "epsilon_world": epsilon}
            checks += 1
            if other_high[0] < low[0] - epsilon:
                continue
            remaining.append((other_low, other_high, other_index))
            if any(other_high[i] < low[i] - epsilon or high[i] < other_low[i] - epsilon for i in (1, 2)):
                continue
            if set(triangles[index]) & set(triangles[other_index]):
                continue
            if _triangles_intersect([points[v] for v in triangles[index]],
                                    [points[v] for v in triangles[other_index]], epsilon):
                pairs.add(tuple(sorted((index, other_index))))
        active = remaining + [(low, high, index)]
    return pairs, {"status": "measured", "count": len(pairs), "pair_checks": checks,
                   "degenerate_triangles": degenerate, "epsilon_world": epsilon}


def _edge_metrics(rest, posed, thresholds):
    ratios = []
    degenerate = 0
    for left, right in rest["edges"]:
        old = _length(_sub(rest["vertices"][left], rest["vertices"][right]))
        if old <= 1e-12:
            degenerate += 1
            continue
        ratios.append(_length(_sub(posed["vertices"][left], posed["vertices"][right])) / old)
    data = {"status": "measured" if ratios else "undetermined", "edge_count": len(ratios),
            "excluded_zero_length_edges": degenerate, "p99": _quantile(ratios, .99),
            "max": max(ratios) if ratios else None, "min": min(ratios) if ratios else None}
    data["flags"] = [key for key, value, compare in (
        ("edge_stretch_p99", data["p99"], "high"),
        ("edge_stretch_max", data["max"], "high"),
        ("edge_compression_min", data["min"], "low"))
        if value is not None and (value > thresholds[key] if compare == "high" else value < thresholds[key])]
    return data


def _samples(raw):
    if not isinstance(raw, list) or len(raw) > 12:
        raise InvalidParamsError("samples must be a list of at most 12 explicit samples")
    names = set()
    result = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InvalidParamsError(f"samples[{index}] must be an object")
        name = _name(item.get("name"), f"samples[{index}].name")
        if name in names:
            raise InvalidParamsError("sample names must be unique")
        names.add(name)
        if set(item) == {"name", "action_name", "frame"}:
            result.append({"name": name, "action_name": _name(item["action_name"], "action_name"),
                           "frame": _number(item["frame"], "frame", -1_000_000, 1_000_000)})
        elif set(item) == {"name", "bones"}:
            bones = item["bones"]
            if not isinstance(bones, list) or not 1 <= len(bones) <= 128:
                raise InvalidParamsError("sample bones must contain 1 to 128 rotations")
            rotations, used = [], set()
            for rotation in bones:
                if not isinstance(rotation, dict) or set(rotation) != {"bone", "axis", "degrees"}:
                    raise InvalidParamsError("sample rotation requires bone, axis, degrees")
                bone = _name(rotation["bone"], "bone")
                axis = rotation["axis"]
                if not isinstance(axis, str) or axis not in AXES or (bone, axis) in used:
                    raise InvalidParamsError("sample rotation axis must be X/Y/Z; duplicate bone axes are invalid")
                used.add((bone, axis))
                rotations.append({"bone": bone, "axis": axis,
                                  "degrees": _number(rotation["degrees"], "degrees", -180, 180)})
            result.append({"name": name, "bones": rotations})
        else:
            raise InvalidParamsError("sample requires name+bones or name+action_name+frame")
    return result


def _parameters(params):
    _unknown(params, {"object_name", "armature_name", "bone_names", "side_axis", "midline_offset",
                      "side_tolerance", "min_weight", "dominance_threshold", "radius_percentile",
                      "far_multiplier", "samples", "max_vertices", "max_edges", "max_triangles",
                      "max_pair_checks", "thresholds"})
    result = {"object_name": _name(params.get("object_name"), "object_name"),
              "armature_name": _optional_name(params.get("armature_name"), "armature_name"),
              "samples": _samples(params.get("samples", []))}
    axis = params.get("side_axis", "X")
    if not isinstance(axis, str) or axis not in AXES:
        raise InvalidParamsError("side_axis must be X, Y, or Z")
    result["side_axis"] = axis
    for key, default, low, high in (
        ("midline_offset", 0, -1e12, 1e12), ("side_tolerance", .0001, 0, 1e12),
        ("min_weight", .01, 0, 1), ("dominance_threshold", .5, 0, 1),
        ("radius_percentile", .95, 0, 1), ("far_multiplier", 2, 1, 1e6)):
        result[key] = _number(params.get(key, default), key, low, high)
    for key, default, maximum in (("max_vertices", 100000, 500000), ("max_edges", 300000, 1500000),
                                  ("max_triangles", 20000, 100000), ("max_pair_checks", 200000, 2000000)):
        result[key] = _integer(params.get(key, default), key, 1, maximum)
    names = params.get("bone_names")
    if names is not None:
        if not isinstance(names, list) or not 1 <= len(names) <= 128:
            raise InvalidParamsError("bone_names must contain 1 to 128 unique bone names")
        names = [_name(value, "bone_names entry") for value in names]
        if len(set(names)) != len(names):
            raise InvalidParamsError("bone_names must be unique")
    result["bone_names"] = names
    raw = params.get("thresholds", {})
    if not isinstance(raw, dict):
        raise InvalidParamsError("thresholds must be an object")
    _unknown(raw, set(DEFAULT_THRESHOLDS), "thresholds")
    thresholds = dict(DEFAULT_THRESHOLDS)
    for key, value in raw.items():
        thresholds[key] = (_integer(value, key, 0, 2000000) if key == "added_intersections"
                           else _number(value, key, 0, 1 if key.endswith("fraction") else 1e12))
    result["thresholds"] = thresholds
    return result


def _evaluated_geometry(bpy, obj, limits):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        if len(mesh.vertices) > limits["max_vertices"] or len(mesh.edges) > limits["max_edges"]:
            raise InvalidStateError("Evaluated mesh exceeds max_vertices or max_edges")
        if len(mesh.loops) > limits["max_triangles"] * 3 + limits["max_vertices"]:
            raise InvalidStateError("Evaluated mesh loop count exceeds triangulation budget")
        mesh.calc_loop_triangles()
        if len(mesh.loop_triangles) > limits["max_triangles"]:
            raise InvalidStateError("Evaluated mesh exceeds max_triangles")
        points = [tuple(evaluated.matrix_world @ vertex.co) for vertex in mesh.vertices]
        if any(not math.isfinite(value) for point in points for value in point):
            raise InvalidStateError("Evaluated mesh has non-finite coordinates")
        return {"vertices": points, "edges": [tuple(edge.vertices) for edge in mesh.edges],
                "triangles": [tuple(tri.vertices) for tri in mesh.loop_triangles]}
    finally:
        evaluated.to_mesh_clear()


def _action_slot(action, armature):
    slots = [slot for slot in getattr(action, "slots", ()) if getattr(slot, "target_id_type", "OBJECT") == "OBJECT"]
    if len(slots) > 1:
        current = getattr(armature.animation_data, "action_slot", None)
        if current in slots:
            return current
        raise InvalidParamsError(f"Action {action.name} has multiple object slots; bind its intended slot to the armature first")
    return slots[0] if slots else None


def _pose_measurements(bpy, obj, armature, options):
    from mathutils import Matrix
    from .animation import _action_fcurves

    scene = bpy.context.scene
    old_frame, old_subframe = scene.frame_current, scene.frame_subframe
    old_pose_position = armature.data.pose_position
    pose = {bone.name: {field: tuple(getattr(bone, field)) if field != "rotation_mode" else bone.rotation_mode
                       for field in _TRANSFORM_FIELDS}
            for bone in armature.pose.bones}
    had_animation = armature.animation_data is not None
    animation = armature.animation_data
    old_action = animation.action if animation else None
    old_slot = getattr(animation, "action_slot", None) if animation else None
    old_nla = animation.use_nla if animation else None
    old_blend = animation.action_blend_type if animation else None
    old_influence = animation.action_influence if animation else None
    old_slot_identifier = getattr(animation, "last_slot_identifier", None) if animation else None
    object_basis = armature.matrix_basis.copy()
    object_channels = {field: tuple(getattr(armature, field)) if field != "rotation_mode" else armature.rotation_mode
                       for field in _TRANSFORM_FIELDS}
    actions = {}
    slots = {}
    transform_paths = set(_TRANSFORM_FIELDS) - {"rotation_mode"}
    transform_paths.update(bone.path_from_id(field) for bone in armature.pose.bones
                           for field in _TRANSFORM_FIELDS if field != "rotation_mode")
    # Resolve every request before temporarily changing any evaluation state.
    for sample in options["samples"]:
        if "action_name" in sample:
            action = bpy.data.actions.get(sample["action_name"])
            if action is None:
                raise NotFoundError(f"Action not found: {sample['action_name']}")
            curves = _action_fcurves(action)
            if len(curves) > 20000 or any(curve.data_path not in transform_paths for curve in curves):
                raise InvalidParamsError(f"Action {action.name} must contain at most 20000 transform-only curves for read-only sampling")
            actions[sample["name"]] = action
            slots[sample["name"]] = _action_slot(action, armature)
        else:
            for rotation in sample["bones"]:
                if armature.pose.bones.get(rotation["bone"]) is None:
                    raise NotFoundError(f"Pose bone not found: {rotation['bone']}")
    if options["samples"] and (len(armature.constraints) or
                              any(len(bone.constraints) for bone in armature.pose.bones) or
                              (animation and len(animation.drivers))):
        raise InvalidStateError("Explicit samples require an unconstrained armature without drivers; bake a disposable rig first")
    if animation and getattr(animation, "use_tweak_mode", False):
        raise InvalidStateError("Exit NLA tweak mode before skin.audit")
    output = []
    restoration_errors = []
    try:
        armature.data.pose_position = "REST"
        bpy.context.view_layer.update()
        rest = _evaluated_geometry(bpy, obj, options)
        rest_pairs, baseline = _intersection_pairs(rest, options["max_pair_checks"])
        baseline["example_pairs"] = [list(pair) for pair in sorted(rest_pairs)[:20]]
        for sample in options["samples"]:
            armature.data.pose_position = "POSE"
            animation = armature.animation_data_create()
            animation.use_nla = False
            animation.action = None
            animation.action_blend_type = "REPLACE"
            animation.action_influence = 1.0
            armature.matrix_basis = object_basis
            for bone in armature.pose.bones:
                bone.rotation_mode = pose[bone.name]["rotation_mode"] if "action_name" in sample else "XYZ"
                bone.matrix_basis = Matrix.Identity(4)
            if "action_name" in sample:
                animation.action = actions[sample["name"]]
                if slots[sample["name"]] is not None:
                    animation.action_slot = slots[sample["name"]]
                frame = sample["frame"]
                scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
            else:
                # Restore the original frame for each explicit pose, so action
                # sample order cannot silently change another modifier's frame.
                scene.frame_set(old_frame, subframe=old_subframe)
                for rotation in sample["bones"]:
                    armature.pose.bones[rotation["bone"]].rotation_euler[AXES[rotation["axis"]]] = math.radians(rotation["degrees"])
            bpy.context.view_layer.update()
            posed = _evaluated_geometry(bpy, obj, options)
            comparable = (len(rest["vertices"]) == len(posed["vertices"]) and
                          rest["edges"] == posed["edges"] and rest["triangles"] == posed["triangles"])
            if not comparable:
                output.append({"name": sample["name"], "sample": sample,
                               "status": "undetermined", "reason": "evaluated_topology_changed"})
                continue
            pairs, intersections = _intersection_pairs(posed, options["max_pair_checks"])
            complete = baseline["status"] == intersections["status"] == "measured"
            added = pairs - rest_pairs if complete else set()
            intersections.update(added_count=len(added) if complete else None,
                                 comparison_status="measured" if complete else "undetermined",
                                 example_added_pairs=[list(pair) for pair in sorted(added)[:20]])
            edge = _edge_metrics(rest, posed, options["thresholds"])
            flags = list(edge["flags"])
            if complete and len(added) > options["thresholds"]["added_intersections"]:
                flags.append("added_intersections")
            output.append({"name": sample["name"], "sample": sample,
                           "status": "measured" if complete and edge["status"] == "measured" else "incomplete",
                           "edge_stretch": edge, "self_intersections": intersections, "flags": flags})
    finally:
        # Restore frame first; drivers/action evaluation may overwrite basis.
        for label, restore in (
            ("frame", lambda: scene.frame_set(old_frame, subframe=old_subframe)),
            ("action", lambda: setattr(armature.animation_data, "action", old_action) if had_animation else armature.animation_data_clear()),
            ("action_slot", lambda: setattr(armature.animation_data, "action_slot", old_slot) if had_animation and hasattr(armature.animation_data, "action_slot") else None),
            ("slot identifier", lambda: setattr(armature.animation_data, "last_slot_identifier", old_slot_identifier) if had_animation and old_slot_identifier is not None else None),
            ("NLA", lambda: setattr(armature.animation_data, "use_nla", old_nla) if had_animation else None),
            ("action blend", lambda: setattr(armature.animation_data, "action_blend_type", old_blend) if had_animation else None),
            ("action influence", lambda: setattr(armature.animation_data, "action_influence", old_influence) if had_animation else None),
            ("object transform", lambda: setattr(armature, "matrix_basis", object_basis)),
            ("pose position", lambda: setattr(armature.data, "pose_position", old_pose_position)),
        ):
            try:
                restore()
            except Exception as exc:
                restoration_errors.append(f"{label}: {exc}")
        for bone in armature.pose.bones:
            try:
                for field, value in pose[bone.name].items():
                    setattr(bone, field, value)
            except Exception as exc:
                restoration_errors.append(f"pose {bone.name}: {exc}")
        for field, value in object_channels.items():
            try:
                setattr(armature, field, value)
            except Exception as exc:
                restoration_errors.append(f"object {field}: {exc}")
        try:
            bpy.context.view_layer.update()
        except Exception as exc:
            restoration_errors.append(f"depsgraph: {exc}")
        if restoration_errors:
            raise InvalidStateError("skin.audit could not restore evaluation state: " + "; ".join(restoration_errors))
    return baseline, output, {"vertices": len(rest["vertices"]), "edges": len(rest["edges"]), "triangles": len(rest["triangles"])}


def audit_skin(params: dict[str, Any]) -> dict[str, Any]:
    """Read-only advisory rest-weight and explicit evaluated-pose audit."""
    options = _parameters(params)
    bpy = get_bpy()
    obj = _mesh_object(bpy, options["object_name"])
    _budget(obj, options["max_vertices"])
    if obj.mode != "OBJECT":
        raise InvalidStateError("skin.audit requires the mesh in Object mode")
    armature, modifier = _resolve_armature(bpy, obj, options["armature_name"])
    if armature is None or modifier is None or modifier.object is not armature:
        raise InvalidStateError("skin.audit requires a matching Armature modifier")
    if armature.mode != "OBJECT":
        raise InvalidStateError("skin.audit requires the armature in Object mode")
    if not modifier.show_viewport:
        raise InvalidStateError("Armature modifier must be enabled in the viewport")
    if len([item for item in obj.modifiers if item.type == "ARMATURE" and item.show_viewport]) != 1:
        raise InvalidStateError("skin.audit requires exactly one enabled Armature modifier")
    if not modifier.use_vertex_groups or modifier.use_bone_envelopes:
        raise InvalidStateError("skin.audit requires vertex-group skinning without bone envelopes")
    if len(armature.data.bones) > 10000:
        raise InvalidStateError("Armature exceeds 10000-bone work bound")
    names = options["bone_names"]
    all_deform = {bone.name for bone in armature.data.bones if bone.use_deform}
    if names is None:
        names = sorted(all_deform)
        if len(names) > 128:
            raise InvalidStateError("More than 128 deform bones; provide bone_names to bound output")
    for name in names:
        if name not in all_deform:
            raise InvalidParamsError(f"Bone is missing or non-deforming: {name}")
    matrix = armature.matrix_world.inverted_safe() @ obj.matrix_world
    if abs(armature.matrix_world.determinant()) <= 1e-20:
        raise InvalidStateError("Armature world transform is singular")
    points = [tuple(matrix @ vertex.co) for vertex in obj.data.vertices]
    if any(not math.isfinite(value) for point in points for value in point):
        raise InvalidStateError("Base mesh has non-finite coordinates")
    groups = {group.index: group.name for group in obj.vertex_groups if group.name in all_deform}
    weights = {name: [] for name in names}
    core = {name: [] for name in names}
    entry_count = 0
    for vertex in obj.data.vertices:
        memberships = []
        for entry in vertex.groups:
            entry_count += 1
            if entry_count > 2000000:
                raise InvalidStateError("Skin memberships exceed the 2000000-entry work bound")
            name = groups.get(entry.group)
            if name is not None:
                weight = float(entry.weight)
                memberships.append((name, weight))
                if name in weights and weight > options["min_weight"]:
                    weights[name].append((int(vertex.index), weight))
        if memberships:
            name, weight = min(memberships, key=lambda item: (-item[1], item[0]))
            if name in core and weight >= options["dominance_threshold"]:
                core[name].append(int(vertex.index))
    per_bone = [_bone_metrics(name, tuple(armature.data.bones[name].head_local),
                              tuple(armature.data.bones[name].tail_local), points, weights[name], core[name],
                              AXES[options["side_axis"]], options["midline_offset"], options["side_tolerance"],
                              options["radius_percentile"], options["far_multiplier"], options["thresholds"])
                for name in names]
    baseline, samples, evaluated_counts = _pose_measurements(bpy, obj, armature, options)
    complete = baseline["status"] == "measured" and all(item["status"] == "measured" for item in samples)
    return {"status": "analysis", "changed": False, "object": obj.name, "armature": armature.name,
            "coordinate_convention": {"weights": "armature rest-local coordinates; units before armature object scale",
                                      "side_axis": options["side_axis"], "midline_offset": options["midline_offset"],
                                      "side_tolerance": options["side_tolerance"],
                                      "deformation": "evaluated world coordinates; edge ratios are dimensionless",
                                      "pose_rotations": "XYZ Euler degrees in each bone's local basis from zero pose; action and NLA disabled",
                                      "rest": "evaluated mesh at original frame with target armature in REST position"},
            "definitions": {"weight_fraction": "flagged retained weight mass / total retained weight mass for this bone",
                            "retained_weight": f"weight strictly greater than min_weight={options['min_weight']}",
                            "dominant_radius": "interpolated distance quantile of vertices where this bone is largest deform weight (ties by name) and weight >= dominance_threshold",
                            "radius_percentile": options["radius_percentile"], "dominance_threshold": options["dominance_threshold"],
                            "far_multiplier": options["far_multiplier"],
                            "edge_stretch": "posed length / evaluated rest length; p99 uses linear rank interpolation",
                            "added_intersections": "posed nonadjacent triangle pair set minus rest pair set; unknown if either scan incomplete"},
            "bounds": {key: options[key] for key in ("max_vertices", "max_edges", "max_triangles", "max_pair_checks")},
            "coverage": {"deformation_complete": complete, "base_vertices": len(points),
                         "evaluated_rest": evaluated_counts, "weight_memberships": entry_count,
                         "reported_bones": len(names), "deform_bones": len(all_deform),
                         "requested_samples": len(options["samples"]), "returned_samples": len(samples)},
            "thresholds": options["thresholds"], "threshold_policy": "advisory_only",
            "per_bone": per_bone, "baseline_self_intersections": baseline, "samples": samples,
            "limitations": list(LIMITATIONS), "state_token": current_state_token(bpy)}
