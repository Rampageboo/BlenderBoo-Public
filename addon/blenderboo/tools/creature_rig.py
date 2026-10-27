"""Body-graph armatures and reference-anchored skeletal FBX qualification."""

from __future__ import annotations

from math import isfinite

from ..errors import InvalidParamsError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .creature import _graph_from_object
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


def _armature(bpy, name):
    obj = bpy.context.scene.objects.get(name) if isinstance(name, str) else None
    if obj is None or obj.type != "ARMATURE":
        raise NotFoundError(f"Armature not found in current scene: {name}")
    return obj


def _mesh(bpy, name):
    obj = bpy.context.scene.objects.get(name) if isinstance(name, str) else None
    if obj is None or obj.type != "MESH":
        raise NotFoundError(f"Mesh not found in current scene: {name}")
    return obj


def _context(bpy, obj):
    mode = bpy.context.mode
    active = bpy.context.view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    if mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for item in selected:
        item.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return mode, active, selected


def _restore(bpy, context):
    mode, active, selected = context
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for item in bpy.context.selected_objects:
        item.select_set(False)
    for item in selected:
        if item.name in bpy.context.scene.objects:
            item.select_set(True)
    bpy.context.view_layer.objects.active = (
        active if active and active.name in bpy.context.scene.objects else None
    )
    if mode != "OBJECT" and bpy.context.view_layer.objects.active is not None:
        restore_mode = "EDIT" if mode.startswith("EDIT_") else {
            "PAINT_WEIGHT": "WEIGHT_PAINT", "PAINT_VERTEX": "VERTEX_PAINT",
            "PAINT_TEXTURE": "TEXTURE_PAINT",
        }.get(mode, mode)
        bpy.ops.object.mode_set(mode=restore_mode)


def _bone_signature(armature):
    return {bone.name: {
        "parent": bone.parent.name if bone.parent else None,
        "head": list(armature.matrix_world @ bone.head_local),
        "tail": list(armature.matrix_world @ bone.tail_local),
        "deform": bool(bone.use_deform),
        "connected": bool(bone.use_connect),
        "matrix": [[float(v) for v in row] for row in bone.matrix_local],
    } for bone in armature.data.bones}


def from_body_graph(params: dict) -> ToolResult:
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    require_document_write_access(bpy)
    graph_name = params.get("graph_object")
    holder = bpy.context.scene.objects.get(graph_name) if isinstance(graph_name, str) else None
    if holder is None:
        raise NotFoundError(f"Graph object not found: {graph_name}")
    graph = _graph_from_object(holder)
    name = params.get("output_name") or f"{holder.name}_rig"
    if not isinstance(name, str) or not name.strip():
        raise InvalidParamsError("output_name must be a non-empty string")
    reference_name = params.get("reference_armature_name")
    mapping = params.get("bone_map")
    if reference_name is not None:
        reference = _armature(bpy, reference_name)
        if not isinstance(mapping, dict) or set(mapping) != {j["name"] for j in graph["joints"]}:
            raise InvalidParamsError("bone_map must map every graph joint to a reference bone")
        if any(not isinstance(v, str) or v not in reference.data.bones for v in mapping.values()):
            raise InvalidParamsError("bone_map values must name bones in the reference armature")
        if len(set(mapping.values())) != len(mapping):
            raise InvalidParamsError("bone_map values must be unique")
        data = reference.data.copy()
        obj = bpy.data.objects.new(name, data)
        obj.matrix_world = reference.matrix_world.copy()
        bpy.context.scene.collection.objects.link(obj)
        deviations = []
        from mathutils import Vector
        for joint in graph["joints"]:
            bone = obj.data.bones[mapping[joint["name"]]]
            distance = (obj.matrix_world @ bone.head_local - Vector(joint["position"])).length
            deviations.append({"joint": joint["name"], "bone": bone.name, "head_residual_m": distance})
        obj["cbt_body_graph"] = holder["cbt_body_graph"]
        obj["cbt_reference_armature"] = reference.name
        bump_epoch()
        return ToolResult(data={"armature": obj.name, "mode": "exact_reference_copy",
                                "reference": reference.name, "bone_count": len(obj.data.bones),
                                "graph_mapping": deviations, "changed": True,
                                "state_token": current_state_token(bpy)},
                          warnings=["Reference rest bones are copied exactly; review graph-to-bone residuals before fitting weights"])
    if graph["body_plan"] == "humanoid" and params.get("ue_mannequin", False):
        raise InvalidParamsError("UE Mannequin mode requires reference_armature_name and a complete bone_map from the Owner's exported FBX")
    if any(j["name"] == "root" and j["parent"] is not None for j in graph["joints"]):
        raise InvalidParamsError("A non-root joint cannot be named root in custom UE mode")
    data = bpy.data.armatures.new(f"{name}_data")
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    from mathutils import Vector
    joints = {j["name"]: j for j in graph["joints"]}
    root = next(j for j in graph["joints"] if j["parent"] is None)
    previous = _context(bpy, obj)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        root_bone = data.edit_bones.new("root")
        root_point = Vector(root["position"])
        root_bone.head = root_point - Vector((0, 0, max(root["radius"] * 0.25, 0.001)))
        root_bone.tail = root_point
        root_bone.use_deform = False
        pending = [j for j in graph["joints"] if j["parent"] is not None]
        made = {root["name"]}
        while pending:
            ready = [j for j in pending if j["parent"] in made]
            if not ready:
                raise InvalidParamsError("Graph parent hierarchy cannot be resolved")
            for joint in ready:
                parent = joints[joint["parent"]]
                head, tail = Vector(parent["position"]), Vector(joint["position"])
                if (tail - head).length < 1e-5:
                    raise InvalidParamsError(f"Graph joint {joint['name']} gives a zero-length bone")
                bone = data.edit_bones.new(joint["name"])
                bone.head, bone.tail = head, tail
                bone.parent = root_bone if parent["parent"] is None else data.edit_bones[parent["name"]]
                bone.use_connect = True
                # World Z is the common pole target; Blender handles near-parallel
                # bones via its align_roll implementation.
                bone.align_roll(Vector((0, 0, 1)))
                made.add(joint["name"])
                pending.remove(joint)
        bpy.ops.object.mode_set(mode="OBJECT")
        obj["cbt_body_graph"] = holder["cbt_body_graph"]
    except Exception:
        bpy.data.objects.remove(obj, do_unlink=True)
        raise
    finally:
        _restore(bpy, previous)
    bump_epoch()
    return ToolResult(data={"armature": obj.name, "mode": "custom_ue_convention",
                            "bone_count": len(obj.data.bones), "single_root": True,
                            "changed": True, "state_token": current_state_token(bpy)},
                      warnings=["Custom skeleton follows one-root graph hierarchy; review roll and UE import settings for the target project"])


def validate_against_reference(params: dict) -> ToolResult:
    bpy = get_bpy()
    armature = _armature(bpy, params.get("armature_name"))
    reference_name = params.get("reference_armature_name")
    bone_tol = params.get("bone_tolerance_m", 0.001)
    if isinstance(bone_tol, bool) or not isinstance(bone_tol, (float, int)) or not isfinite(bone_tol) or bone_tol < 0:
        raise InvalidParamsError("bone_tolerance_m must be a non-negative finite number")
    actual = _bone_signature(armature)
    roots = [name for name, bone in actual.items() if bone["parent"] is None]
    zero = [name for name, bone in actual.items() if sum((bone["head"][i]-bone["tail"][i])**2 for i in range(3)) <= 1e-10]
    discontinuities = []
    for name, bone in actual.items():
        parent = actual.get(bone["parent"])
        if parent is None or not bone["connected"]:
            continue
        gap = sum((bone["head"][i]-parent["tail"][i])**2 for i in range(3)) ** 0.5
        if gap > bone_tol:
            discontinuities.append({"bone": name, "parent": bone["parent"], "gap_m": gap})
    result = {"armature": armature.name, "bone_count": len(actual), "roots": roots,
              "zero_length_bones": zero, "discontinuous_chains": discontinuities,
              "reference": reference_name, "matches_reference": None}
    mesh_name = params.get("mesh_name")
    if mesh_name is not None:
        mesh_obj = _mesh(bpy, mesh_name)
        from collections import Counter
        edge_use = Counter()
        for face in mesh_obj.data.polygons:
            indices = list(face.vertices)
            for index, start in enumerate(indices):
                edge_use[tuple(sorted((start, indices[(index+1) % len(indices)])))] += 1
        if not edge_use or any(count != 2 for count in edge_use.values()):
            result["joints_inside_mesh"] = {"status": "undetermined", "reason": "mesh is not closed"}
        else:
            from mathutils import Vector
            from mathutils.bvhtree import BVHTree
            mesh_obj.data.calc_loop_triangles()
            vertices = [mesh_obj.matrix_world @ vertex.co for vertex in mesh_obj.data.vertices]
            triangles = [tuple(tri.vertices) for tri in mesh_obj.data.loop_triangles]
            tree = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
            extent = max((max(v[axis] for v in vertices) - min(v[axis] for v in vertices)) for axis in range(3))
            direction = Vector((1, 0.013, 0.007)).normalized()
            outside = []
            for name, bone in actual.items():
                if name in roots:
                    continue
                point = Vector(bone["head"])
                origin = point + direction * 1e-6
                crossings = 0
                for _ in range(1024):
                    hit, _, _, distance = tree.ray_cast(origin, direction, max(extent * 4, 1.0))
                    if hit is None:
                        break
                    crossings += 1
                    origin = hit + direction * 1e-5
                if crossings % 2 == 0:
                    outside.append({"bone": name, "focus": bone["head"]})
            result["joints_inside_mesh"] = {"status": "pass" if not outside else "fail",
                                             "outside_count": len(outside), "outside_sample": outside[:50]}
    if reference_name is not None:
        reference = _armature(bpy, reference_name)
        expected = _bone_signature(reference)
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        differences = []
        for name in sorted(set(actual) & set(expected)):
            a, b = actual[name], expected[name]
            head_gap = sum((a["head"][i]-b["head"][i])**2 for i in range(3)) ** 0.5
            tail_gap = sum((a["tail"][i]-b["tail"][i])**2 for i in range(3)) ** 0.5
            matrix_delta = max(abs(a["matrix"][i][j]-b["matrix"][i][j]) for i in range(4) for j in range(4))
            if a["parent"] != b["parent"] or a["deform"] != b["deform"] or a["connected"] != b["connected"] or head_gap > bone_tol or tail_gap > bone_tol or matrix_delta > 1e-4:
                differences.append({"bone": name, "parent_changed": a["parent"] != b["parent"],
                                    "deform_changed": a["deform"] != b["deform"],
                                    "connect_changed": a["connected"] != b["connected"],
                                    "head_gap_m": head_gap, "tail_gap_m": tail_gap,
                                    "matrix_max_delta": matrix_delta})
        result.update(missing_bones=missing, extra_bones=extra,
                      different_bones=differences[:100], difference_count=len(differences),
                      matches_reference=not (missing or extra or differences))
    result["valid"] = (len(roots) == 1 and not zero and not discontinuities
                       and result["matches_reference"] is not False
                       and result.get("joints_inside_mesh", {}).get("status") != "fail")
    return ToolResult(data=result,
                      warnings=[] if result["valid"] else ["Skeleton structural or reference checks failed"])


def export_ue_fbx(params: dict) -> ToolResult:
    from .ue_fbx_export import export_ue_fbx as export
    return export(params)
