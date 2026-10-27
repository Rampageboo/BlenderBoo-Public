"""Selection-independent, source-preserving mesh join into world space."""
from __future__ import annotations

import math
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .uv import _mesh_object, _name, _object_names, _unknown, _validate_expected


# Blender-managed topology/selection attributes are rebuilt or copied through
# the UV layer API below, rather than as standalone custom attributes.
_BUILTINS = frozenset({"position", ".edge_verts", ".corner_vert", ".corner_edge",
                       "material_index", "sharp_edge", "sharp_face", "uv_seam",
                       ".select_vert", ".select_edge", ".select_poly",
                       ".uv_select_vert", ".uv_select_edge", ".uv_pin"})
_FACE_TYPES = frozenset({"INT", "FLOAT", "BOOLEAN"})


def _join_plan(objects: list[Any]) -> tuple[list[str], dict[str, str]]:
    """Reject lossy inputs before creating any Blender datablock."""
    uv_names: list[str] = []
    attributes: dict[str, str] = {}
    for obj in objects:
        mesh = obj.data
        if obj.modifiers or obj.vertex_groups or mesh.shape_keys:
            raise InvalidStateError("mesh.join refuses modifiers, vertex groups and shape keys; prepare plain meshes first")
        if getattr(mesh, "has_custom_normals", False):
            raise InvalidStateError("mesh.join refuses custom split normals")
        matrix = obj.matrix_world
        if any(not math.isfinite(value) for row in matrix for value in row) or matrix.to_3x3().determinant() == 0:
            raise InvalidStateError("mesh.join requires finite, non-singular world transforms")
        if any(not math.isfinite(value) for vertex in mesh.vertices for value in vertex.co):
            raise InvalidStateError("mesh.join refuses non-finite vertex coordinates")
        names = {layer.name for layer in mesh.uv_layers}
        for layer in mesh.uv_layers:
            if len(layer.data) != len(mesh.loops):
                raise InvalidStateError("UV layer loop count does not match the mesh")
            if any(not math.isfinite(value) for item in layer.data for value in item.uv):
                raise InvalidStateError("mesh.join refuses non-finite UV coordinates")
            if layer.name not in uv_names:
                uv_names.append(layer.name)
        for attr in mesh.attributes:
            if attr.name in _BUILTINS or attr.name in names or any(attr.name == f"{prefix}{name}" for name in names for prefix in (".vs.", ".es.", ".pn.")):
                continue
            if attr.domain != "FACE" or attr.data_type not in _FACE_TYPES:
                raise InvalidStateError(f"mesh.join cannot preserve attribute {attr.name!r} ({attr.domain}/{attr.data_type}); only scalar FACE attributes are supported")
            if attr.name in attributes and attributes[attr.name] != attr.data_type:
                raise InvalidStateError(f"Conflicting FACE attribute types: {attr.name}")
            attributes[attr.name] = attr.data_type
        if any(slot.material is None for slot in obj.material_slots):
            raise InvalidStateError("mesh.join refuses empty material slots")
    if len(uv_names) > 8:
        raise InvalidStateError("mesh.join supports at most 8 UV layer names in the combined mesh")
    if set(uv_names) & set(attributes):
        raise InvalidStateError("UV and FACE attribute names conflict across inputs")
    return uv_names, attributes


def join_meshes(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_names", "name", "dry_run", "expected_state"})
    _validate_expected(params)
    names = _object_names(params.get("object_names"), minimum=2)
    name = _name(params.get("name"), "name")
    bpy = get_bpy()
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("mesh.join requires Object mode")
    if bpy.data.objects.get(name) is not None:
        raise InvalidStateError(f"Output object already exists: {name}")
    objects = [_mesh_object(bpy, source) for source in names]
    if sum(len(obj.data.loops) + len(obj.data.vertices) + len(obj.data.edges) for obj in objects) > 2_000_000:
        raise InvalidStateError("mesh.join is limited to 2 million vertices, edges and loops combined")
    uv_names, attributes = _join_plan(objects)
    counts = {kind: sum(len(getattr(obj.data, kind)) for obj in objects) for kind in ("vertices", "edges", "polygons", "loops")}
    extra = {"object": name, "source_objects": names, "sources_preserved": True,
             "space": "WORLD", "counts": counts, "uv_layers": uv_names,
             "face_attributes": attributes,
             "missing_layer_policy": "zero_fill", "missing_attribute_policy": "zero_fill",
             "source_face_ranges": []}
    offset = 0
    for obj in objects:
        extra["source_face_ranges"].append({"object": obj.name, "start": offset, "count": len(obj.data.polygons)})
        offset += len(obj.data.polygons)
    transaction, apply = prepare_destructive_action(
        action="mesh.join", affected_datablocks=[{"type": "Object", "name": name}],
        journal=[{"action": "create_joined_mesh", "name": name}], dry_run=params.get("dry_run"),
        require_confirm=False, state_token=current_state_token(bpy), extra=extra)
    if not apply:
        return transaction
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)

    vertices, edges, faces = [], [], []
    face_sources, loop_sources, edge_sources = [], [], []
    materials, material_maps = [], []
    for obj in objects:
        mesh = obj.data
        vertex_offset = len(vertices)
        vertices.extend(tuple(obj.matrix_world @ vertex.co) for vertex in mesh.vertices)
        for edge in mesh.edges:
            key = tuple(sorted(vertex_offset + i for i in edge.vertices))
            edges.append(key)
            edge_sources.append((key, bool(edge.use_edge_sharp), bool(edge.use_seam)))
        reverse = obj.matrix_world.to_3x3().determinant() < 0
        mapping = []
        for slot in obj.material_slots:
            material = slot.material
            if material not in materials:
                materials.append(material)
            mapping.append(materials.index(material))
        material_maps.append(mapping)
        for face in mesh.polygons:
            loops = list(face.loop_indices)
            if reverse:
                loops.reverse()
            faces.append([vertex_offset + mesh.loops[i].vertex_index for i in loops])
            face_sources.append((obj, face))
            loop_sources.extend((obj, i) for i in loops)
    created_mesh = created_object = None
    try:
        created_mesh = bpy.data.meshes.new(name + "_Mesh")
        created_mesh.from_pydata(vertices, edges, faces)
        if len(created_mesh.polygons) != len(face_sources) or len(created_mesh.loops) != len(loop_sources):
            raise InvalidStateError("Joined mesh topology does not match the planned face/loop counts")
        for material in materials:
            created_mesh.materials.append(material)
        maps = {obj.name: mapping for obj, mapping in zip(objects, material_maps)}
        for destination, (obj, source) in zip(created_mesh.polygons, face_sources):
            destination.use_smooth = source.use_smooth
            mapping = maps[obj.name]
            destination.material_index = mapping[source.material_index] if mapping else 0
        flags = {key: (sharp, seam) for key, sharp, seam in edge_sources}
        for edge in created_mesh.edges:
            key = tuple(sorted(edge.vertices))
            if key not in flags:
                raise InvalidStateError("Joined mesh contains an unexpected edge")
            edge.use_edge_sharp, edge.use_seam = flags[key]
        for uv_name in uv_names:
            layer = created_mesh.uv_layers.new(name=uv_name, do_init=False)
            for destination, (obj, index) in zip(layer.data, loop_sources):
                source_layer = obj.data.uv_layers.get(uv_name)
                if source_layer is None:
                    destination.uv = (0.0, 0.0)
                    continue
                source = source_layer.data[index]
                destination.uv = source.uv
                for flag in ("pin_uv", "select", "select_edge"):
                    if hasattr(source, flag) and hasattr(destination, flag):
                        setattr(destination, flag, getattr(source, flag))
        for attr_name, attr_type in attributes.items():
            attr = created_mesh.attributes.new(name=attr_name, type=attr_type, domain="FACE")
            for destination, (obj, face) in zip(attr.data, face_sources):
                source = obj.data.attributes.get(attr_name)
                destination.value = source.data[face.index].value if source else 0
        active = objects[0].data.uv_layers.active
        if active is not None:
            created_mesh.uv_layers.active_index = uv_names.index(active.name)
        render_layer = next((layer.name for layer in objects[0].data.uv_layers if layer.active_render), None)
        if render_layer:
            created_mesh.uv_layers[render_layer].active_render = True
        created_mesh.update()
        created_object = bpy.data.objects.new(name, created_mesh)
        # Link to the scene root, even if every source collection is excluded.
        bpy.context.scene.collection.objects.link(created_object)
    except Exception as exc:
        errors = []
        if created_object is not None:
            try:
                bpy.data.objects.remove(created_object, do_unlink=True)
            except Exception as cleanup:
                errors.append(str(cleanup))
        if created_mesh is not None:
            try:
                bpy.data.meshes.remove(created_mesh)
            except Exception as cleanup:
                errors.append(str(cleanup))
        transaction.update(changed=bool(errors), warnings=errors)
        if errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"mesh.join failed: {exc}", transaction) from exc
    transaction.update(changed=True, object=created_object.name, mesh=created_mesh.name)
    transaction["affected_datablocks"] = [{"type": "Object", "name": created_object.name}, {"type": "Mesh", "name": created_mesh.name}]
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
