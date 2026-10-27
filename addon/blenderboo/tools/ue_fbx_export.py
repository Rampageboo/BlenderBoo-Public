"""One UE FBX path for bind meshes and individually timed actions.

Blender's importer absorbs FBX unit/object scale into rest bones. Consequently a
Blender round trip alone cannot detect the historical x100 UE root: inspect the
serialized FBX node/curve scales as well as imported rest/animated transforms.
"""
from __future__ import annotations

from contextlib import contextmanager
from math import isfinite
import os
from pathlib import Path
import re
from uuid import uuid4

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import ToolResult

SCALE_TOLERANCE = 1e-4
POSITION_TOLERANCE = 1e-4
_DATA_COLLECTIONS = ("objects", "collections", "meshes", "armatures", "actions", "materials", "images", "textures", "node_groups")


def _bool(params, name, default):
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{name} must be a boolean")
    return value


def _export_bones(rig, deform_only):
    bones = {bone.name: bone for bone in rig.data.bones if not deform_only or bone.use_deform}
    # FBX retains nondeforming ancestors of deform bones.
    for bone in list(bones.values()):
        while bone.parent:
            bone = bone.parent
            bones[bone.name] = bone
    if not bones:
        raise InvalidStateError("The selected deform-only setting leaves no export bones")
    return bones


def _root_convention(rig, bones, mode):
    if mode not in {"auto", "object_is_root", "object_stripped"}:
        raise InvalidParamsError("root_mode must be auto, object_is_root or object_stripped")
    requested = mode
    if mode == "auto":
        mode = "object_stripped" if "root" in bones else "object_is_root"
    roots = sorted(name for name, bone in bones.items() if bone.parent is None)
    if mode == "object_stripped":
        if len(roots) != 1:
            raise InvalidStateError("object_stripped needs exactly one exported top bone")
        object_name, ue_root = "Armature", roots[0]
    else:
        if "root" in bones:
            raise InvalidStateError("object_is_root would duplicate the existing root bone; use object_stripped")
        object_name, ue_root = "root", "root"
    if object_name in bones:
        raise InvalidStateError(f"Bone name {object_name} conflicts with the selected FBX armature node")
    ue_parents = {name: bone.parent.name if bone.parent else
                  ("root" if mode == "object_is_root" else None) for name, bone in bones.items()}
    if mode == "object_is_root":
        ue_parents["root"] = None
    return {"requested": requested, "resolved": mode, "armature_node": object_name,
            "ue_root_bone": ue_root, "ue_bone_count": len(bones) + (mode == "object_is_root"),
            "ue_parent_map": ue_parents,
            "ue_verified": False}


@contextmanager
def _exact_name(item, collection, name):
    old_name = item.name
    displaced = collection.get(name)
    if displaced is item:
        displaced = None
    displaced_name = displaced.name if displaced else None
    try:
        if displaced:
            displaced.name = f"__bb_fbx_{uuid4().hex}"
        item.name = name
        if item.name != name:
            raise InvalidStateError(f"Cannot temporarily reserve FBX node name {name}")
        yield
    finally:
        item.name = old_name
        if displaced:
            displaced.name = displaced_name


def _child(element, key):
    return next((e for e in element.elems if e.id == key), None)


def _properties(element):
    props = _child(element, b"Properties70")
    return {e.props[0]: list(e.props[4:]) for e in props.elems if e.id == b"P"} if props else {}


def _name(element):
    return element.props[1].split(b"\x00\x01")[0].decode("utf8")


def _literal_centimeters(path):
    """Normalize only the staged Blender FBX's supported distance-bearing fields.

    UE's legacy FbxFactory defaults ConvertSceneUnit to false. ALL alone leaves
    metre coordinates with UnitScaleFactor=100, which that importer interprets
    as centimetres. This is a uniform coordinate-unit change, not an object
    scale: positions and affine-matrix translation columns change together;
    rotations, scales, weights, normals and time do not.
    """
    from array import array
    from io_scene_fbx import parse_fbx, encode_bin
    tree, version = parse_fbx.parse(str(path))
    factor = float(_properties(_child(tree, b"GlobalSettings")).get(b"UnitScaleFactor", [1.0])[0])
    if not isfinite(factor) or factor <= 0:
        raise InvalidStateError("FBX has an invalid unit scale")
    objects = _child(tree, b"Objects")
    models = {e.props[0]: e for e in objects.elems if e.id == b"Model"}
    connections = [e.props for e in _child(tree, b"Connections").elems if e.id == b"C"]
    translation_nodes = set()
    for connection in connections:
        if connection[0] == b"OP" and connection[2] in models:
            if connection[3] == b"Lcl Translation":
                translation_nodes.add(connection[1])
            elif connection[3] not in {b"Lcl Rotation", b"Lcl Scaling"}:
                raise InvalidStateError("Unsupported FBX model animation property for centimeter normalization")
    translation_curves = {c[1] for c in connections if c[0] == b"OP" and c[2] in translation_nodes}
    position_props = {b"Lcl Translation", b"GeometricTranslation", b"RotationOffset", b"RotationPivot",
                      b"ScalingOffset", b"ScalingPivot", b"TranslationMin", b"TranslationMax"}
    direction_props = {b"Lcl Rotation", b"Lcl Scaling", b"PreRotation", b"PostRotation",
                       b"GeometricRotation", b"GeometricScaling", b"RotationMin", b"RotationMax",
                       b"ScalingMin", b"ScalingMax"}
    for element in objects.elems:
        if element.id == b"Geometry" and element.props[2] != b"Mesh":
            raise InvalidStateError("UE centimeter normalization currently supports mesh geometry without shape keys")
        if element.id == b"Model":
            if element.props[2] not in {b"Mesh", b"Null", b"LimbNode"}:
                raise InvalidStateError("Unsupported FBX model type for centimeter normalization")
            props = _child(element, b"Properties70")
            for prop in props.elems if props else []:
                if prop.id == b"P" and prop.props[1] in {b"Vector", b"Vector3D"}:
                    if prop.props[0] not in position_props | direction_props:
                        raise InvalidStateError(f"Unsupported positional FBX property: {prop.props[0]!r}")
        if element.id == b"AnimationCurve" and element.props[0] in translation_curves:
            flags = _child(element, b"KeyAttrFlags")
            if flags and any((int(flag) & 0x0e) != 4 for flag in flags.props[0]):
                raise InvalidStateError("Centimeter normalization requires linearly baked translation curves")
    methods = {ord(code): method for code, method in {
        "B": "add_bool", "C": "add_char", "Z": "add_int8", "Y": "add_int16",
        "I": "add_int32", "L": "add_int64", "F": "add_float32", "D": "add_float64",
        "R": "add_bytes", "S": "add_string", "i": "add_int32_array", "l": "add_int64_array",
        "f": "add_float32_array", "d": "add_float64_array", "b": "add_bool_array", "c": "add_byte_array",
    }.items()}
    def scaled(values):
        return array(values.typecode, (float(v) * factor for v in values))

    def convert(element, owner=None):
        if element.id in {b"Model", b"Geometry", b"Pose", b"Deformer", b"AnimationCurveNode", b"AnimationCurve"}:
            owner = element
        values = list(element.props)
        if element.id == b"P":
            if values[0] in {b"UnitScaleFactor", b"OriginalUnitScaleFactor"}:
                values[4] = 1.0
            elif owner and owner.id == b"Model" and values[0] in position_props:
                values[4:] = [float(v) * factor for v in values[4:]]
            elif owner and owner.id == b"AnimationCurveNode" and owner.props[0] in translation_nodes:
                if values[0] in {b"d|X", b"d|Y", b"d|Z"}:
                    values[4] = float(values[4]) * factor
        elif owner and owner.id == b"Geometry" and element.id == b"Vertices":
            values[0] = scaled(values[0])
        elif owner and owner.id in {b"Pose", b"Deformer"} and element.id in {
                b"Matrix", b"Transform", b"TransformLink", b"TransformAssociateModel"}:
            if len(values[0]) != 16:
                raise InvalidStateError("Unsupported FBX bind matrix size")
            matrix = array(values[0].typecode, values[0])
            for index in (12, 13, 14):  # FBX matrices are serialized column-major.
                matrix[index] *= factor
            values[0] = matrix
        elif owner and owner.id == b"AnimationCurve" and owner.props[0] in translation_curves:
            if element.id == b"KeyValueFloat":
                values[0] = scaled(values[0])
            elif element.id == b"Default":
                values[0] = float(values[0]) * factor
        encoded = encode_bin.FBXElem(element.id)
        for kind, value in zip(element.props_type, values):
            method = methods.get(kind)
            if method is None:
                raise InvalidStateError(f"Unsupported FBX property encoding: {kind}")
            getattr(encoded, method)(value)
        encoded.elems = [convert(child, owner) for child in element.elems]
        return encoded

    # Do not touch the destination: this is still the private staged export.
    encode_bin.write(str(path), convert(tree), version)
    return {"source_unit_scale_cm": factor, "output_unit_scale_cm": 1.0,
            "coordinate_factor": factor, "method": "staged_fbx_literal_centimeters"}


def _serialized_receipt(path, rig, bones, root, source_unit_cm, clip):
    from io_scene_fbx import parse_fbx
    from io_scene_fbx.fbx_utils import FBX_KTIME
    tree, version = parse_fbx.parse(str(path))
    objects = _child(tree, b"Objects")
    elements = {e.props[0]: e for e in objects.elems}
    models = {key: e for key, e in elements.items() if e.id == b"Model"}
    connections = [e.props for e in _child(tree, b"Connections").elems if e.id == b"C"]
    parents = {c[1]: c[2] for c in connections if c[0] == b"OO" and c[1] in models
               and (c[2] in models or c[2] == 0)}
    unit = float(_properties(_child(tree, b"GlobalSettings")).get(b"UnitScaleFactor", [1.0])[0])
    arm = next((key for key, e in models.items() if _name(e) == root["armature_node"] and e.props[2] == b"Null"), None)
    node_ids = {key for key, e in models.items() if e.props[2] == b"LimbNode"}
    if arm is not None:
        node_ids.add(arm)
    nodes = {}
    bad_scales, translations = [], []
    for key in node_ids:
        element = models[key]
        props = _properties(element)
        name = _name(element)
        scale = [float(v) for v in props.get(b"Lcl Scaling", [1, 1, 1])]
        translation = [float(v) for v in props.get(b"Lcl Translation", [0, 0, 0])]
        parent = models.get(parents.get(key))
        nodes[name] = {"parent": _name(parent) if parent else None, "scale": scale,
                       "translation_cm": [v * unit for v in translation]}
        if any(not isfinite(v) or abs(v - 1) > SCALE_TOLERANCE for v in scale):
            bad_scales.append({"node": name, "scale": scale})
        if key != arm and name in bones:
            bone = bones[name]
            local = bone.parent.matrix_local.inverted() @ bone.matrix_local if bone.parent else bone.matrix_local
            expected = local.translation.length * source_unit_cm
            observed = sum((v * unit) ** 2 for v in translation) ** 0.5
            tolerance = max(0.01, expected * POSITION_TOLERANCE)
            if not isfinite(observed) or abs(expected - observed) > tolerance:
                translations.append({"bone": name, "expected_length_cm": expected,
                                     "observed_length_cm": observed, "tolerance_cm": tolerance})
    expected_parents = {name: bone.parent.name if bone.parent else root["armature_node"]
                        for name, bone in bones.items()}
    observed_parents = {_name(models[key]): _name(models[parents[key]]) if parents.get(key) in models else None
                        for key in node_ids if key != arm}
    layout_match = arm is not None and expected_parents == observed_parents
    curve_targets = {c[1]: c[2] for c in connections if c[0] == b"OP" and len(c) > 3
                     and c[2] in node_ids and c[3] == b"Lcl Scaling"}
    bad_curve_scales = []
    for c in connections:
        if c[0] != b"OP" or c[2] not in curve_targets:
            continue
        curve = elements.get(c[1])
        values = _child(curve, b"KeyValueFloat") if curve else None
        if values:
            maximum = max((abs(float(v) - 1) if isfinite(v) else float("inf") for v in values.props[0]), default=0)
            if maximum > SCALE_TOLERANCE:
                bad_curve_scales.append({"node": _name(models[curve_targets[c[2]]]),
                                         "channel": c[3].decode(), "max_scale_error": maximum})
    takes = []
    for element in objects.elems:
        if element.id == b"AnimationStack":
            props = _properties(element)
            start = float(props.get(b"LocalStart", [0])[0]) / FBX_KTIME
            end = float(props.get(b"LocalStop", [0])[0]) / FBX_KTIME
            takes.append({"name": _name(element), "duration_s": end - start})
    timing_match = (len(takes) == 1 and takes[0]["name"] == clip["action"] and
                    abs(takes[0]["duration_s"] - clip["duration_s"]) < 1e-5) if clip else not takes
    return {"fbx_version": version, "unit_scale_cm": unit, "literal_centimeters": abs(unit - 1.0) < 1e-8, "nodes": nodes,
            "bones_match": layout_match, "scale_tolerance": SCALE_TOLERANCE,
            "nonunit_node_scales": bad_scales, "nonunit_animation_scales": bad_curve_scales,
            "translation_mismatches": translations, "takes": takes, "timing_match": timing_match,
            "passed": abs(unit - 1.0) < 1e-8 and layout_match and not bad_scales and not bad_curve_scales and not translations and timing_match}


def _transform_state(obj):
    return {key: tuple(getattr(obj, key)) for key in
            ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale")}


def _restore_transform(obj, state):
    # Restoring raw channels avoids decomposition/rounding changes in source Actions' poses.
    for key, value in state.items():
        setattr(obj, key, value)


def _bounds(obj):
    from mathutils import Vector
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    return [min(v[i] for v in corners) for i in range(3)] + [max(v[i] for v in corners) for i in range(3)]


def _reimport_receipt(bpy, path, rig, mesh, bones, clip, source_samples):
    before = {name: set(getattr(bpy.data, name)) for name in _DATA_COLLECTIONS}
    try:
        # An explicit context also works when invoked from the bridge timer.
        # Never pin active_object: the importer switches it while constructing bones.
        window = bpy.context.window or next(iter(bpy.context.window_manager.windows), None)
        override = {"scene": bpy.context.scene, "view_layer": bpy.context.view_layer}
        if window is not None:
            override["window"] = window
        with bpy.context.temp_override(**override):
            result = bpy.ops.import_scene.fbx(filepath=str(path), use_anim=clip is not None,
                                              anim_offset=0.0, use_image_search=False)
        if "FINISHED" not in result:
            raise InvalidStateError("FBX import was cancelled")
        imported = [o for o in bpy.data.objects if o not in before["objects"]]
        rigs = [o for o in imported if o.type == "ARMATURE"]
        if len(rigs) != 1:
            raise InvalidStateError(f"FBX reimport yielded {len(rigs)} armatures instead of one")
        target = rigs[0]
        # FBX has parent transforms, not Blender's connected-bone constraint. Its
        # importer infers tails from children and connects coincident heads even
        # with force_connect_children=False. That suppresses valid translation
        # curves. Release only those synthesized connections on this disposable rig.
        released_connections = [b.name for b in target.data.bones if b.use_connect]
        if released_connections:
            for obj in list(bpy.context.selected_objects):
                obj.select_set(False)
            target.select_set(True)
            bpy.context.view_layer.objects.active = target
            bpy.ops.object.mode_set(mode="EDIT")
            for bone in target.data.edit_bones:
                bone.use_connect = False
            bpy.ops.object.mode_set(mode="OBJECT")
        expected = {name: bone.parent.name if bone.parent else None for name, bone in bones.items()}
        observed = {b.name: b.parent.name if b.parent else None for b in target.data.bones}
        scale_issues, position_issues = [], []
        for bone in target.data.bones:
            local = bone.parent.matrix_local.inverted() @ bone.matrix_local if bone.parent else bone.matrix_local
            scale = list(local.to_scale())
            if any(not isfinite(v) or abs(v - 1) > SCALE_TOLERANCE for v in scale):
                scale_issues.append({"bone": bone.name, "scale": scale})
            if bone.name in bones:
                source_head = rig.matrix_world @ bones[bone.name].head_local
                gap = (target.matrix_world @ bone.head_local - source_head).length
                if not isfinite(gap) or gap > max(POSITION_TOLERANCE, source_head.length * POSITION_TOLERANCE):
                    position_issues.append({"bone": bone.name, "head_error_scene_units": gap})
        meshes = [o for o in imported if o.type == "MESH"]
        bounds_error = None
        if mesh and len(meshes) == 1:
            bounds_error = max(abs(a - b) for a, b in zip(_bounds(mesh), _bounds(meshes[0])))
        mesh_match = len(meshes) == int(mesh is not None) and (bounds_error is None or bounds_error < 1e-3)
        timing, motion_issues = None, []
        if clip:
            action = target.animation_data.action if target.animation_data else None
            frame_range = list(action.frame_range) if action else []
            frame_count = round(frame_range[1] - frame_range[0]) + 1 if frame_range else 0
            fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
            duration = (frame_range[1] - frame_range[0]) / fps if frame_range else None
            timing = {"frame_range": frame_range, "sample_count": frame_count, "fps": fps,
                      "duration_s": duration,
                      "passed": bool(action) and frame_count == clip["sample_count"] and
                      abs(fps - clip["fps"]) < 1e-4 and abs(duration - clip["duration_s"]) < 1e-4}
            for frame, expected_heads in source_samples:
                bpy.context.scene.frame_set(frame)
                for name, expected_head in expected_heads.items():
                    if name not in target.pose.bones:
                        continue
                    pose = target.pose.bones[name]
                    head = target.matrix_world @ pose.head
                    error = (head - expected_head).length
                    if error > 1e-3 or not isfinite(error):
                        motion_issues.append({"frame": frame, "bone": name, "head_error_scene_units": error,
                                              "expected_head": list(expected_head), "imported_head": list(head)})
                    if any(not isfinite(v) or abs(v - 1) > SCALE_TOLERANCE for v in pose.scale):
                        scale_issues.append({"frame": frame, "bone": name, "scale": list(pose.scale)})
        return {"bones_match": expected == observed,
                "root_bones": sorted(name for name, parent in observed.items() if parent is None),
                "importer_connections_released": released_connections,
                "missing": sorted(set(expected) - set(observed)),
                "extra": sorted(set(observed) - set(expected)),
                "parent_changes": sorted(n for n in expected.keys() & observed.keys() if expected[n] != observed[n]),
                "nonunit_bone_scales": scale_issues, "position_mismatches": position_issues,
                "imported_mesh_count": len(meshes), "mesh_bounds_max_error_scene_units": bounds_error,
                "mesh_match": mesh_match, "animation_timing": timing, "animation_position_mismatches": motion_issues,
                "passed": expected == observed and not scale_issues and not position_issues and mesh_match
                          and not motion_issues and (timing is None or timing["passed"])}
    finally:
        # Remove imported datablocks, including Actions/materials, even on partial import failure.
        for name in _DATA_COLLECTIONS:
            collection = getattr(bpy.data, name)
            for item in list(collection):
                if item not in before[name]:
                    collection.remove(item, do_unlink=True)


def export_ue_fbx(params):
    from .creature_rig import _armature, _mesh, _context, _restore
    from ._blender import get_bpy
    bpy = get_bpy()
    scene = bpy.context.scene
    rig = _armature(bpy, params.get("armature_name"))
    include_mesh = _bool(params, "include_mesh", True)
    mesh = _mesh(bpy, params.get("mesh_name")) if include_mesh else None
    overwrite = _bool(params, "overwrite", False)
    roundtrip = _bool(params, "roundtrip", True)
    deform_only = _bool(params, "use_armature_deform_only", False)
    scale_mode = params.get("apply_scale_options", "FBX_SCALE_ALL")
    if scale_mode not in {"FBX_SCALE_ALL", "FBX_SCALE_NONE"}:
        raise InvalidParamsError("apply_scale_options must be FBX_SCALE_ALL or FBX_SCALE_NONE")
    names = params.get("action_names", [])
    if not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        raise InvalidParamsError("action_names must be a list of unique non-empty action names")
    if not names and not include_mesh:
        raise InvalidParamsError("include_mesh:false requires at least one action")
    if rig.animation_data and rig.animation_data.use_tweak_mode:
        raise InvalidStateError("Exit NLA tweak mode before exporting actions")
    rates = params.get("action_fps", {})
    if not isinstance(rates, dict) or set(rates) - set(names):
        raise InvalidParamsError("action_fps must map selected action names to FPS")
    for rate in rates.values():
        if isinstance(rate, bool) or not isinstance(rate, int) or not 1 <= rate <= 32767:
            raise InvalidParamsError("action_fps values must be integers between 1 and 32767")
    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise InvalidParamsError("path must be a non-empty FBX file path")
    path = Path(raw_path).expanduser().resolve()
    if path.suffix.lower() != ".fbx":
        raise InvalidParamsError("path must end in .fbx")
    if mesh and not any(m.type == "ARMATURE" and m.object is rig for m in mesh.modifiers):
        raise InvalidStateError("Mesh has no Armature modifier targeting the requested rig")
    if any(o.library and not o.override_library for o in [rig] + ([mesh] if mesh else [])):
        raise InvalidStateError("Export needs local or editable override objects for temporary naming/context")
    bones = _export_bones(rig, deform_only)
    root = _root_convention(rig, bones, params.get("root_mode", "auto"))
    jobs = []
    for name in names or [None]:
        action = bpy.data.actions.get(name) if name else None
        if name and action is None:
            raise NotFoundError(f"Action not found: {name}")
        clip = None
        if action:
            start, end = action.frame_range
            if start != int(start) or end != int(end) or end < start:
                raise InvalidParamsError(f"Action {name} requires an integer frame range")
            slots = [s for s in getattr(action, "slots", []) if s.target_id_type == "OBJECT"]
            if len(slots) > 1:
                raise InvalidParamsError(f"Action {name} has multiple object slots; export a single-rig action")
            if getattr(action, "slots", None) and not slots:
                raise InvalidParamsError(f"Action {name} does not contain an object slot")
            from .animation import _action_fcurves
            curves = _action_fcurves(action)
            if not curves or not any(len(c.keyframe_points) or len(c.sampled_points) for c in curves):
                raise InvalidParamsError(f"Action {name} has no animated curves")
            for curve in curves:
                try:
                    rig.path_resolve(curve.data_path)
                except (ValueError, AttributeError) as exc:
                    raise InvalidParamsError(f"Action {name} targets a missing rig property: {curve.data_path}") from exc
            fps = rates.get(name, scene.render.fps / scene.render.fps_base)
            clip = {"action": name, "fps": fps, "frame_range": [int(start), int(end)],
                    "sample_count": int(end - start) + 1, "duration_s": (end - start) / fps}
        suffix = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .") if name else ""
        destination = path.with_name(f"{path.stem}_{suffix}.fbx") if len(names) > 1 else path
        jobs.append((action, clip, destination))
    if len({str(p).casefold() for _, _, p in jobs}) != len(jobs):
        raise InvalidParamsError("Action names collide after sanitizing output filenames")
    for _, _, destination in jobs:
        if destination.exists() and not overwrite:
            raise InvalidStateError(f"FBX already exists: {destination}; pass overwrite:true to replace it")
    settings = {"axis_forward": "-Y", "axis_up": "Z", "apply_unit_scale": True,
                "apply_scale_options": scale_mode, "use_armature_deform_only": deform_only,
                "add_leaf_bones": False, "armature_nodetype": "NULL", "global_scale": 1.0,
                "bake_space_transform": False}
    from io_scene_fbx.fbx_utils import units_blender_to_fbx_factor
    source_unit = units_blender_to_fbx_factor(scene)
    original_name = rig.name
    saved_scene = (scene.render.fps, scene.render.fps_base, scene.frame_start, scene.frame_end,
                   scene.frame_current, scene.frame_subframe)
    had_animation = rig.animation_data is not None
    animation = rig.animation_data_create()
    saved_action, saved_slot = animation.action, animation.action_slot
    saved_nla, saved_blend, saved_influence = animation.use_nla, animation.action_blend_type, animation.action_influence
    saved_pose_position = rig.data.pose_position
    saved_pose = {p.name: _transform_state(p) for p in rig.pose.bones}
    saved_transform = _transform_state(rig)
    previous = _context(bpy, rig)
    files, staged = [], []
    try:
        animation.use_nla = False
        animation.action_blend_type = "REPLACE"
        animation.action_influence = 1.0
        with _exact_name(rig, bpy.data.objects, root["armature_node"]):
            for action, clip, destination in jobs:
                for obj in list(bpy.context.selected_objects):
                    obj.select_set(False)
                rig.select_set(True)
                if mesh:
                    mesh.select_set(True)
                bpy.context.view_layer.objects.active = rig
                animation.action = action
                if action and getattr(action, "slots", None):
                    animation.action_slot = next((s for s in action.slots if s.target_id_type == "OBJECT"), None)
                _restore_transform(rig, saved_transform)
                for pose in rig.pose.bones:
                    pose.matrix_basis.identity()
                rig.data.pose_position = "POSE" if action else "REST"
                if clip:
                    fps = clip["fps"]
                    scene.render.fps = round(fps)
                    scene.render.fps_base = scene.render.fps / fps
                    scene.frame_start, scene.frame_end = clip["frame_range"]
                samples = []
                if clip:
                    start, end = clip["frame_range"]
                    for frame in sorted({start, (start + end) // 2, end}):
                        scene.frame_set(frame)
                        samples.append((frame, {n: (rig.matrix_world @ rig.pose.bones[n].head).copy() for n in bones}))
                    scene.frame_set(start)
                bpy.context.view_layer.update()
                temporary = destination.with_name(f".{destination.stem}.{uuid4().hex}.part.fbx")
                staged.append((temporary, destination))
                destination.parent.mkdir(parents=True, exist_ok=True)
                # The single-take exporter names its take after the scene.
                with _exact_name(scene, bpy.data.scenes, clip["action"] if clip else scene.name):
                    result = bpy.ops.export_scene.fbx(filepath=str(temporary), use_selection=True,
                        object_types={"ARMATURE", "MESH"} if mesh else {"ARMATURE"},
                        bake_anim=bool(clip), bake_anim_use_all_actions=False, bake_anim_use_nla_strips=False,
                        bake_anim_use_all_bones=True, bake_anim_force_startend_keying=True,
                        bake_anim_step=1.0, bake_anim_simplify_factor=0.0, **settings)
                if "FINISHED" not in result or not temporary.is_file() or not temporary.stat().st_size:
                    raise InvalidStateError("FBX export did not create a non-empty file")
                normalization = _literal_centimeters(temporary)
                serialized = _serialized_receipt(temporary, rig, bones, root, source_unit, clip)
                comparison = None
                if roundtrip:
                    comparison = _reimport_receipt(bpy, temporary, rig, mesh, bones, clip, samples)
                accepted = serialized["passed"] and (comparison is None or comparison["passed"])
                files.append({"path": str(destination), "bytes": temporary.stat().st_size,
                              "animation": clip, "unit_normalization": normalization,
                              "serialized": serialized, "roundtrip": comparison,
                              "acceptance": ("pass" if roundtrip else "unverified") if accepted else "fail",
                              "published": False})
        if all(f["acceptance"] != "fail" for f in files):
            for temporary, destination in staged:
                if destination.exists() and not overwrite:
                    raise InvalidStateError("FBX destination appeared during export; existing file preserved")
            for item, (temporary, destination) in zip(files, staged):
                os.replace(temporary, destination)
                item["published"] = True
        acceptance = "fail" if any(f["acceptance"] == "fail" for f in files) else ("pass" if roundtrip else "unverified")
        data = {"armature": original_name, "mesh": mesh.name if mesh else None, "root": root,
                "settings": settings, "exports": files, "acceptance": acceptance,
                "qualification": "FBX serialization and Blender reimport; Unreal import not exercised"}
        if len(files) == 1:
            data.update({k: files[0][k] for k in ("path", "bytes", "roundtrip", "published")})
        warnings = ["Existing UE skeletons with the old x100 root need a fresh skeleton import; UE acceptance is unverified"]
        if acceptance == "fail":
            warnings.append("FBX qualification failed; no destination was replaced")
        elif not roundtrip:
            warnings.append("Blender reimport skipped; qualification is incomplete")
        return ToolResult(data=data, warnings=warnings)
    finally:
        animation.action = saved_action
        if saved_action:
            animation.action_slot = saved_slot
        animation.use_nla = saved_nla
        animation.action_blend_type = saved_blend
        animation.action_influence = saved_influence
        rig.data.pose_position = saved_pose_position
        scene.render.fps, scene.render.fps_base, scene.frame_start, scene.frame_end = saved_scene[:4]
        scene.frame_set(saved_scene[4], subframe=saved_scene[5])
        _restore_transform(rig, saved_transform)
        for name, transform in saved_pose.items():
            _restore_transform(rig.pose.bones[name], transform)
        if not had_animation:
            rig.animation_data_clear()
        bpy.context.view_layer.update()
        _restore(bpy, previous)
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()
