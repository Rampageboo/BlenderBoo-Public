"""Unrestricted Python execution on the bridge's Blender main thread.

The change report is evidence, not a complete undo journal: arbitrary Python can
change RNA properties which the cheap snapshot does not observe.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import ast
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import traceback
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, ToolkitError
from ..registry import ToolResult
from ._blender import get_bpy
from .checkpoint import save_checkpoint
from .python_modules import exec_module_imports
from .session_lease import follow_active_document, require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token, reconcile


COLLECTIONS = (
    "objects", "meshes", "materials", "collections", "node_groups", "images",
    "actions", "curves", "armatures", "cameras", "lights", "worlds", "scenes",
    "texts", "grease_pencils", "grease_pencils_v3", "lattices", "metaballs",
    "fonts", "movieclips", "sounds", "masks", "brushes", "workspaces",
)
INLINE_LIMIT = 8 * 1024
CAPTURE_LIMIT = 1024 * 1024
NAME_LIMIT = 20
_MISSING_MEMBER = re.compile(
    r"^'(?P<owner>[A-Za-z_][A-Za-z_0-9]*)' object has no attribute '(?P<member>[A-Za-z_][A-Za-z_0-9]*)'$"
)
_PLAIN_FIELDS = {
    "Material": ("diffuse_color", "metallic", "roughness", "use_nodes"),
    "World": ("color", "use_nodes"),
    "ShaderNode": ("operation", "blend_type", "use_clamp"),
    "Modifier": ("show_viewport", "show_render", "levels", "render_levels", "strength", "width"),
}


class PythonExecError(ToolkitError):
    """User-code exception with captured mutation evidence."""

    def __init__(self, original: BaseException, data: dict[str, Any], user_traceback: str):
        super().__init__(str(original))
        self.original_type = type(original).__name__
        self.data = data
        self.user_traceback = user_traceback
        self.meta: dict[str, Any] = {}


class _BoundedWriter(io.TextIOBase):
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.limit_reached = False

    def write(self, value: str) -> int:
        encoded = value.encode("utf-8", "replace")
        remaining = CAPTURE_LIMIT - self.size
        if len(encoded) > remaining:
            self.limit_reached = True
        if remaining > 0:
            kept = encoded[:remaining].decode("utf-8", "ignore")
            self.parts.append(kept)
            self.size += len(kept.encode("utf-8"))
        return len(value)

    def split(self) -> tuple[str, str, bool]:
        content = "".join(self.parts)
        raw = content.encode("utf-8")
        preview = raw[:INLINE_LIMIT].decode("utf-8", "ignore")
        return preview, content[len(preview):], len(raw) > INLINE_LIMIT or self.limit_reached


def _identity(item: Any) -> tuple[Any, ...]:
    pointer = getattr(item, "as_pointer", None)
    address = int(pointer()) if callable(pointer) else id(item)
    # Blender's session_uid distinguishes an ID removed and recreated at the
    # same address. A bare pointer would incorrectly call that a rename.
    uid = getattr(item, "session_uid", None)
    # Without a persistent UID, conservatively treat a different name at the
    # same address as remove/create rather than assert a rename.
    return (address, int(uid)) if uid is not None else (address, type(item).__name__, getattr(item, "name", None))


def _numbers(value: Any) -> tuple[float, ...]:
    try:
        return tuple(float(number) for number in value)
    except (TypeError, ValueError):
        return ()


def _fingerprint(value: Any) -> str:
    return hashlib.blake2b(repr(value).encode("utf-8", "replace"), digest_size=12).hexdigest()


def _id_properties(item: Any) -> str:
    keys = getattr(item, "keys", None)
    if not callable(keys):
        return ""
    try:
        return _fingerprint(tuple((key, _idprop_value(item[key])) for key in sorted(keys())))
    except (ReferenceError, RuntimeError, TypeError, KeyError):
        return ""


def _idprop_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str, type(None))):
        return value
    items = getattr(value, "items", None)
    if callable(items):
        return tuple((key, _idprop_value(entry)) for key, entry in sorted(items()))
    try:
        return tuple(_idprop_value(entry) for entry in value)
    except (TypeError, ReferenceError, RuntimeError):
        return repr(value)


def _rna_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str, type(None))):
        return value
    if hasattr(value, "as_pointer"):
        return (type(value).__name__, getattr(value, "name", None), _identity(value))
    try:
        return tuple(_rna_value(part) for part in value)
    except (TypeError, ReferenceError, RuntimeError):
        return repr(value)


def _rna_fields(item: Any) -> str:
    values = []
    kind = getattr(getattr(item, "bl_rna", None), "identifier", type(item).__name__)
    names = _PLAIN_FIELDS.get(kind, ())
    if not names and "Modifier" in kind:
        names = _PLAIN_FIELDS["Modifier"]
    if not names and "ShaderNode" in kind:
        names = _PLAIN_FIELDS["ShaderNode"]
    for name in names:
        try:
            value = getattr(item, name)
            if isinstance(value, (bool, int, float, str)):
                values.append((name, value))
            elif name in {"diffuse_color", "color"}:
                values.append((name, _numbers(value)))
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            continue
    return _fingerprint(values)


def _foreach_hash(collection: Any, attribute: str, components: int, typecode: str) -> bytes:
    if collection is None or not hasattr(collection, "foreach_get"):
        return b""
    count = len(collection) * components
    if not count:
        return b""
    import numpy as np
    values = np.zeros(count, dtype={"f": np.float32, "i": np.int32, "b": np.int8}[typecode])
    try:
        collection.foreach_get(attribute, values)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return b""
    return values.tobytes()


def _mesh_fields(mesh: Any) -> dict[str, Any]:
    vertices = getattr(mesh, "vertices", ())
    edges = getattr(mesh, "edges", ())
    polygons = getattr(mesh, "polygons", ())
    loops = getattr(mesh, "loops", ())
    position = hashlib.blake2b(digest_size=12)
    topology = hashlib.blake2b(digest_size=12)
    marks = hashlib.blake2b(digest_size=12)
    shading = hashlib.blake2b(digest_size=12)
    if getattr(mesh, "is_editmode", False):
        # Mesh RNA can be stale in edit mode. The depsgraph update remains the
        # bounded evidence; detailed mode reports counts without bmesh loops.
        position.update(repr((len(vertices), len(edges), len(polygons))).encode())
    else:
        position.update(_foreach_hash(vertices, "co", 3, "f"))
        for collection, attr, components, kind in (
            (edges, "vertices", 2, "i"), (polygons, "loop_start", 1, "i"),
            (polygons, "loop_total", 1, "i"), (loops, "vertex_index", 1, "i"),
        ):
            topology.update(_foreach_hash(collection, attr, components, kind))
        for collection, attr in ((edges, "use_seam"), (edges, "use_edge_sharp")):
            marks.update(_foreach_hash(collection, attr, 1, "b"))
        shading.update(_foreach_hash(polygons, "material_index", 1, "i"))
    uv_digest = hashlib.blake2b(digest_size=12)
    uv_names = set()
    for layer in getattr(mesh, "uv_layers", ()):
        uv_names.add(layer.name)
        uv_digest.update(repr((layer.name, len(layer.data))).encode())
        uv_digest.update(_foreach_hash(layer.data, "uv", 2, "f"))
    for attribute in getattr(mesh, "attributes", ()):
        if getattr(attribute, "data_type", "") == "FLOAT2" and attribute.name in uv_names:
            continue
        data = getattr(attribute, "data", ())
        uv_digest.update(repr((attribute.name, getattr(attribute, "data_type", ""),
                               getattr(attribute, "domain", ""), len(data))).encode())
        layout = {
            "FLOAT": ("value", 1, "f"),
            "INT": ("value", 1, "i"),
            "BOOLEAN": ("value", 1, "b"),
            "FLOAT2": ("vector", 2, "f"),
            "FLOAT_VECTOR": ("vector", 3, "f"),
            "FLOAT_COLOR": ("color", 4, "f"),
            "BYTE_COLOR": ("color_srgb", 4, "f"),
        }.get(getattr(attribute, "data_type", ""))
        if layout is not None:
            field, components, kind = layout
            encoded = _foreach_hash(data, field, components, kind)
            uv_digest.update(field.encode())
            uv_digest.update(encoded)
    return {
        "geometry.positions": (len(vertices), position.digest()),
        "geometry.topology": (len(vertices), len(edges), len(polygons), len(loops), topology.digest()),
        "uv_attributes": _fingerprint((marks.digest(), uv_digest.digest())),
        "shading.assignment": shading.digest(),
        "other.custom_properties": _id_properties(mesh),
    }


def _node_tree_fields(tree: Any) -> dict[str, Any]:
    nodes = []
    for node in getattr(tree, "nodes", ()):
        sockets = []
        for socket in getattr(node, "inputs", ()):
            try:
                sockets.append((socket.identifier, socket.name, _rna_value(getattr(socket, "default_value", None))))
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                continue
        nodes.append((node.name, getattr(node, "bl_idname", ""), _rna_fields(node), tuple(sockets), _id_properties(node)))
    links = sorted((link.from_node.name, link.from_socket.identifier,
                    link.to_node.name, link.to_socket.identifier) for link in getattr(tree, "links", ()))
    category = "geometry_nodes" if getattr(tree, "bl_idname", "") == "GeometryNodeTree" else "shading"
    return {f"{category}.node_tree": _fingerprint((nodes, links, _rna_fields(getattr(tree, "interface", None))))}


def _modifier_fields(modifier: Any) -> str:
    base = _rna_fields(modifier)
    if getattr(modifier, "type", "") != "NODES":
        return base
    group = getattr(modifier, "node_group", None)
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    if group is None or inputs is None:
        return base
    values = []
    for socket in getattr(getattr(group, "interface", None), "items_tree", ()):
        if getattr(socket, "item_type", None) != "SOCKET" or getattr(socket, "in_out", None) != "INPUT":
            continue
        try:
            value = getattr(getattr(inputs, socket.identifier), "value")
            if isinstance(value, (bool, int, float, str)):
                values.append((socket.identifier, value))
            elif not hasattr(value, "as_pointer"):
                values.append((socket.identifier, _numbers(value)))
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            continue
    return _fingerprint((base, values))


def _curve_geometry(curve: Any) -> str:
    splines = []
    for spline in getattr(curve, "splines", ()):
        points = tuple((tuple(point.co), getattr(point, "tilt", None), getattr(point, "radius", None))
                       for point in getattr(spline, "points", ()))
        bezier = tuple((tuple(point.co), tuple(point.handle_left), tuple(point.handle_right),
                        point.handle_left_type, point.handle_right_type, point.tilt, point.radius)
                       for point in getattr(spline, "bezier_points", ()))
        splines.append((getattr(spline, "type", ""), points, bezier,
                        getattr(spline, "use_cyclic_u", False)))
    return _fingerprint(splines)


def _geometry_metrics(obj: Any, mesh: Any = None) -> dict[str, Any] | None:
    """Cheap world-space shape measurements for a mesh object."""
    if mesh is None:
        mesh = getattr(obj, "data", None)
    if getattr(obj, "type", None) != "MESH" or mesh is None:
        return None
    polygons = getattr(mesh, "polygons", ())
    matrix = getattr(obj, "matrix_world", None)
    try:
        scale = float(get_bpy().context.scene.unit_settings.scale_length) or 1.0
    except (AttributeError, ReferenceError):
        scale = 1.0
    try:
        corners = [matrix @ __import__("mathutils").Vector(corner) for corner in obj.bound_box]
        lo = [min(corner[axis] for corner in corners) * scale for axis in range(3)]
        hi = [max(corner[axis] for corner in corners) * scale for axis in range(3)]
        diagonal = math.sqrt(sum((hi[axis] - lo[axis]) ** 2 for axis in range(3)))
        center = [(lo[axis] + hi[axis]) * .5 for axis in range(3)]
    except (AttributeError, TypeError, ReferenceError):
        lo = hi = center = None
        diagonal = None
    volume = None
    if not getattr(mesh, "is_editmode", False) and len(polygons):
        try:
            edge_use: dict[tuple[int, int], int] = {}
            for polygon in polygons:
                indices = tuple(polygon.vertices)
                for index, start in enumerate(indices):
                    edge = tuple(sorted((start, indices[(index + 1) % len(indices)])))
                    edge_use[edge] = edge_use.get(edge, 0) + 1
            if edge_use and all(count == 2 for count in edge_use.values()):
                mesh.calc_loop_triangles()
                vertices = mesh.vertices
                signed = sum(vertices[tri.vertices[0]].co.dot(
                    vertices[tri.vertices[1]].co.cross(vertices[tri.vertices[2]].co))
                    for tri in mesh.loop_triangles) / 6
                volume = abs(float(signed) * float(matrix.to_3x3().determinant())) * scale ** 3
        except (AttributeError, TypeError, ReferenceError, RuntimeError):
            pass
    return {"faces": len(polygons), "bbox_min": lo, "bbox_max": hi,
            "bbox_center": center, "bbox_diagonal": diagonal, "volume_m3": volume}


def _suspicious_geometry(before: dict, after: dict, face_ratio: float,
                         volume_ratio: float, bbox_jump_ratio: float) -> list[dict[str, Any]]:
    findings = []
    old, new = before.get("objects", {}), after.get("objects", {})
    for identity in old.keys() & new.keys():
        name = new[identity][0]
        previous = old[identity][1].get("geometry_metrics")
        current = new[identity][1].get("geometry_metrics")
        if not previous or not current or previous == current:
            continue
        reasons = []
        if previous["faces"] and current["faces"] / previous["faces"] < face_ratio:
            reasons.append("face_count_drop")
        if previous["volume_m3"] and current["volume_m3"] is not None and current["volume_m3"] / previous["volume_m3"] < volume_ratio:
            reasons.append("volume_drop")
        old_size, new_size = previous["bbox_diagonal"], current["bbox_diagonal"]
        if old_size and new_size is not None and (new_size / old_size > bbox_jump_ratio or
                                                   new_size / old_size < 1 / bbox_jump_ratio):
            reasons.append("bbox_size_jump")
        if old_size and previous["bbox_center"] and current["bbox_center"]:
            displacement = math.dist(previous["bbox_center"], current["bbox_center"])
            if displacement > old_size * bbox_jump_ratio:
                reasons.append("bbox_position_jump")
        if reasons:
            findings.append({"object": name, "reasons": reasons, "before": previous, "after": current})
    return sorted(findings, key=lambda item: item["object"])


def _geometry_changes(before: dict, after: dict, mode: str) -> dict[str, Any]:
    old, new = before.get("objects", {}), after.get("objects", {})
    rows = {}
    for identity in old.keys() & new.keys():
        previous = old[identity][1].get("geometry_metrics")
        current = new[identity][1].get("geometry_metrics")
        if previous is not None and current is not None and previous != current:
            rows[new[identity][0]] = {"before": previous, "after": current}
    return dict(sorted(rows.items())[:NAME_LIMIT] if mode != "detailed" else sorted(rows.items()))


def _object_fields(obj: Any, memberships: dict[tuple[Any, ...], tuple[str, ...]] | None = None,
                   *, detail: bool = True, breadcrumb: Path | None = None) -> dict[str, Any]:
    data = getattr(obj, "data", None)
    if breadcrumb is not None:
        breadcrumb.write_text(f"objects\t{obj.name}\thierarchy_modifiers\n", encoding="utf-8")
    fields = {
        "parent": getattr(getattr(obj, "parent", None), "name", None),
        "collections": memberships.get(_identity(obj), ()) if memberships is not None
                       else tuple(sorted(item.name for item in getattr(obj, "users_collection", ()))),
        "modifiers": tuple((item.name, getattr(item, "type", ""), _modifier_fields(item), _id_properties(item))
                           for item in getattr(obj, "modifiers", ())),
        "vertices": len(getattr(data, "vertices", ())),
        "polygons": len(getattr(data, "polygons", ())),
        "materials": tuple(getattr(item, "name", "") for item in getattr(data, "materials", ())),
        "material_slots": tuple(getattr(getattr(slot, "material", None), "name", None)
                                for slot in getattr(obj, "material_slots", ())),
        "data_identity": _identity(data) if data is not None else None,
        "custom_properties": _id_properties(obj),
    }
    # Most large scenes contain many empties, cameras, and lights.  Avoid a
    # Python helper/RNA round trip for every non-mesh object in both snapshots.
    if data is not None and getattr(obj, "type", None) == "MESH":
        if breadcrumb is not None:
            breadcrumb.write_text(f"objects\t{obj.name}\tgeometry_metrics\n", encoding="utf-8")
        fields["geometry_metrics"] = _geometry_metrics(obj, data)
    if detail or data is not None:
        if breadcrumb is not None:
            breadcrumb.write_text(f"objects\t{obj.name}\tmatrix_world\n", encoding="utf-8")
        matrix = getattr(obj, "matrix_world", ())
        try:
            fields["matrix_world"] = tuple(number for row in matrix for number in _numbers(row))
        except TypeError:
            fields["matrix_world"] = ()
    return fields


def _item_fields(collection_name: str, item: Any, *, detail: bool = True,
                 memberships: dict[tuple[Any, ...], tuple[str, ...]] | None = None,
                 breadcrumb: Path | None = None) -> dict[str, Any]:
    if collection_name == "objects":
        return _object_fields(item, memberships, detail=detail, breadcrumb=breadcrumb)
    if collection_name == "meshes":
        if breadcrumb is not None:
            breadcrumb.write_text(f"meshes\t{item.name}\tgeometry_uv_attributes\n", encoding="utf-8")
        return _mesh_fields(item) if detail else {"other.custom_properties": _id_properties(item)}
    if collection_name in {"node_groups", "materials", "worlds"}:
        if breadcrumb is not None:
            breadcrumb.write_text(f"{collection_name}\t{item.name}\tshading_node_tree\n", encoding="utf-8")
        tree = item if collection_name == "node_groups" else getattr(item, "node_tree", None)
        fields = _node_tree_fields(tree) if tree is not None and (detail or collection_name == "node_groups") else {}
        if detail or collection_name in {"materials", "worlds"}:
            fields["shading.properties"] = _rna_fields(item)
        fields["other.custom_properties"] = _id_properties(item)
        return fields
    if collection_name in {"curves", "grease_pencils", "grease_pencils_v3"}:
        if breadcrumb is not None:
            breadcrumb.write_text(f"{collection_name}\t{item.name}\tgeometry_curve\n", encoding="utf-8")
        fields = {"other.custom_properties": _id_properties(item)}
        if detail:
            fields["geometry.curve"] = _curve_geometry(item)
        return fields
    return {"other.custom_properties": _id_properties(item)}


def snapshot(bpy: Any, *, detail: bool = True,
             breadcrumb: Path | None = None) -> dict[str, dict[tuple[Any, ...], tuple[str, dict[str, Any]]]]:
    """Capture ID inventory; detailed mode inspects only an explicit request."""
    result: dict[str, dict[tuple[Any, ...], tuple[str, dict[str, Any]]]] = {}
    for collection_name in COLLECTIONS:
        collection = getattr(bpy.data, collection_name, None)
        if collection is None:
            continue
        items: dict[tuple[Any, ...], tuple[str, dict[str, Any]]] = {}
        for item in collection:
            try:
                if detail and breadcrumb is not None:
                    breadcrumb.write_text(f"{collection_name}\t{item.name}\tfields\n", encoding="utf-8")
                items[_identity(item)] = (
                    str(item.name),
                    _item_fields(collection_name, item, detail=True, breadcrumb=breadcrumb) if detail else {},
                )
            except (ReferenceError, RuntimeError):
                continue
        result[collection_name] = items
    return result


def _literal_names(code: str) -> set[str]:
    """Bound property checks to named IDs the script can address explicitly."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def _literal_mesh_targets(code: str) -> set[str]:
    """Find explicitly named objects whose connectivity can be sampled before exec."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
            if node.value.attr == "objects" and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                names.add(node.slice.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if (node.func.attr == "get" and isinstance(owner, ast.Attribute) and owner.attr == "objects"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                names.add(node.args[0].value)
    return names


def _fragment_summaries(bpy: Any, names: set[str], small_fraction: float) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Sample only literal object targets; incomplete checks remain visible."""
    from .mesh_topology import component_summary

    summaries: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    for name in sorted(names)[:NAME_LIMIT]:
        try:
            obj = bpy.data.objects.get(name)
            if obj is None or getattr(obj, "type", None) != "MESH":
                skipped[name] = "mesh_object_not_found"
                continue
            if getattr(obj.data, "is_editmode", False):
                skipped[name] = "edit_mode_mesh_data_may_be_stale"
                continue
            summaries[name] = component_summary(obj.data, matrix_world=obj.matrix_world,
                                                small_fraction=small_fraction)
        except Exception as exc:
            skipped[name] = type(exc).__name__
    if len(names) > NAME_LIMIT:
        skipped["other_literal_targets"] = f"{len(names) - NAME_LIMIT} omitted by limit"
    return summaries, skipped


def _target_properties(bpy: Any, names: set[str]) -> dict[tuple[str, tuple[Any, ...]], str]:
    result = {}
    if not names:
        return result
    for collection_name in COLLECTIONS:
        collection = getattr(bpy.data, collection_name, None)
        if collection is None:
            continue
        for name in names:
            item = collection.get(name) if hasattr(collection, "get") else None
            if item is not None:
                result[(collection_name, _identity(item))] = _id_properties(item)
    return result


def _target_object_fields(bpy: Any, names: set[str]) -> dict[tuple[Any, ...], dict[str, Any]]:
    result = {}
    objects = getattr(bpy.data, "objects", None)
    if objects is None:
        return result
    for name in names:
        obj = objects.get(name) if hasattr(objects, "get") else None
        if obj is None:
            continue
        try:
            data = getattr(obj, "data", None)
            result[_identity(obj)] = {
                "matrix_world": tuple(float(value) for row in obj.matrix_world for value in row),
                "parent": getattr(getattr(obj, "parent", None), "name", None),
                "modifiers": tuple((mod.name, mod.type, _modifier_fields(mod)) for mod in obj.modifiers),
                "materials": tuple(mat.name if mat else None for mat in getattr(data, "materials", ())),
                "material_slots": tuple(slot.material.name if slot.material else None for slot in obj.material_slots),
                "data_identity": _identity(data) if data is not None else None,
                "custom_properties": _id_properties(obj),
            }
        except (ReferenceError, RuntimeError, AttributeError, TypeError):
            continue
    return result


def _mark_updates(before: dict, after: dict,
                  updates: list[tuple[Any, bool, bool, bool]]) -> None:
    """Mark only existing IDs reported by the depsgraph as modified."""
    locations = {key: name for name, entries in after.items() for key in entries}
    for item, geometry, transform, shading in updates:
        try:
            original = getattr(item, "original", None) or item
            key = _identity(original)
            collection_name = locations.get(key)
            if collection_name is None or key not in before.get(collection_name, {}):
                continue
            flags = (geometry, transform, shading)
            if any(flags):
                after[collection_name][key][1]["depsgraph.update"] = flags
        except (ReferenceError, RuntimeError, AttributeError):
            continue


def compare_snapshots(before: dict, after: dict, mode: str = "summary") -> dict[str, Any]:
    """Compare datablock identity and fingerprints, retaining category evidence."""
    result: dict[str, Any] = {}
    categories: dict[str, dict[str, str]] = {}
    for collection_name in sorted(before.keys() | after.keys()):
        old = before.get(collection_name, {})
        new = after.get(collection_name, {})
        created = sorted(new[key][0] for key in new.keys() - old.keys())
        removed = sorted(old[key][0] for key in old.keys() - new.keys())
        renamed = sorted(
            ({"from": old[key][0], "to": new[key][0]} for key in old.keys() & new.keys()
             if old[key][0] != new[key][0]), key=lambda item: item["from"]
        )
        modified = {}
        for key in old.keys() & new.keys():
            fields_before, fields_after = old[key][1], new[key][1]
            changed_fields = {
                field: {"before": fields_before.get(field), "after": fields_after.get(field)}
                for field in fields_before.keys() | fields_after.keys()
                if fields_before.get(field) != fields_after.get(field)
            }
            if changed_fields:
                modified[new[key][0]] = changed_fields
                for field in changed_fields:
                    if field.startswith("geometry."):
                        category, reason = "geometry", field.split(".", 1)[1]
                    elif field.startswith("shading."):
                        category, reason = "shading", field.split(".", 1)[1]
                    elif field.startswith("geometry_nodes."):
                        category, reason = "geometry_nodes", field.split(".", 1)[1]
                    elif field in {"matrix_world", "parent", "collections"}:
                        category, reason = "transform_hierarchy", field
                    elif field == "modifiers":
                        category, reason = "modifiers", "properties_or_stack"
                        old_nodes = tuple(entry for entry in fields_before.get("modifiers", ())
                                          if isinstance(entry, tuple) and len(entry) > 1 and entry[1] == "NODES")
                        new_nodes = tuple(entry for entry in fields_after.get("modifiers", ())
                                          if isinstance(entry, tuple) and len(entry) > 1 and entry[1] == "NODES")
                        if old_nodes != new_nodes:
                            categories.setdefault("geometry_nodes", {})[new[key][0]] = "modifier_inputs_or_group"
                    elif field == "geometry_metrics":
                        category, reason = "geometry", "face_bounds_or_volume"
                    elif field in {"materials", "material_slots", "data_identity"}:
                        category, reason = "shading", field
                    elif field == "uv_attributes":
                        category, reason = "uv_attributes", "data_or_marks"
                    else:
                        category, reason = "other", field
                    categories.setdefault(category, {})[new[key][0]] = reason
        if not (created or removed or renamed or modified):
            continue
        counts = {"created": len(created), "removed": len(removed), "renamed": len(renamed), "modified": len(modified)}
        if mode == "none":
            result[collection_name] = {"counts": counts}
        elif mode == "summary":
            result[collection_name] = {
                "counts": counts,
                "created": created[:NAME_LIMIT], "removed": removed[:NAME_LIMIT],
                "renamed": renamed[:NAME_LIMIT], "modified": sorted(modified)[:NAME_LIMIT],
            }
        else:
            result[collection_name] = {
                "counts": counts, "created": created, "removed": removed,
                "renamed": renamed, "modified": modified,
            }
    for category, affected in categories.items():
        result[category] = dict(sorted(affected.items())[:NAME_LIMIT]) if mode != "detailed" else dict(sorted(affected.items()))
    return result


def _depsgraph_changes(updates: list[tuple[Any, bool, bool, bool]], mode: str) -> dict[str, dict[str, str]]:
    """Classify evaluated IDs; flags give coverage without scanning mesh arrays."""
    categories: dict[str, dict[str, str]] = {}
    for item, geometry, transform, shading in updates:
        try:
            original = getattr(item, "original", None) or item
            name = str(original.name)
            type_name = getattr(getattr(original, "bl_rna", None), "identifier", type(original).__name__)
            tree_type = getattr(original, "bl_idname", "")
        except (ReferenceError, RuntimeError, AttributeError):
            continue
        def add(category: str, reason: str) -> None:
            categories.setdefault(category, {})[name] = reason
        if type_name == "Object":
            if transform:
                add("transform_hierarchy", "matrix_world_or_hierarchy")
        elif type_name == "Mesh":
            if geometry:
                add("geometry", "positions_or_topology")
                add("uv_attributes", "data_or_marks")
            if shading:
                add("shading", "face_assignment_or_material")
        elif type_name in {"Curve", "Curves", "GreasePencil", "GreasePencilv3"}:
            if geometry:
                add("geometry", "points_or_topology")
        elif type_name in {"Material", "World"}:
            add("shading", "properties_or_nodes")
        elif "NodeTree" in type_name or "NodeTree" in tree_type:
            add("geometry_nodes" if tree_type == "GeometryNodeTree" else "shading",
                "node_tree")
    return {category: dict(sorted(affected.items())[:NAME_LIMIT]) if mode != "detailed" else dict(sorted(affected.items()))
            for category, affected in categories.items()}


def _json_result(value: Any) -> tuple[Any, bool]:
    try:
        json.dumps(value, allow_nan=False)
        return value, False
    except (TypeError, ValueError, OverflowError):
        return repr(value), True


def _api_hint(problem: BaseException) -> dict[str, Any] | None:
    """Give a bounded live lookup without changing the original exec failure."""
    if not isinstance(problem, (AttributeError, KeyError, TypeError)):
        return None
    match = _MISSING_MEMBER.fullmatch(str(problem))
    if match is None:
        return None
    path = f"bpy.types.{match['owner']}.{match['member']}"
    try:
        from .api_lookup import lookup_api

        lookup = lookup_api({"path": path, "detail": "summary", "limit": 5})
    except Exception:
        return None
    hint: dict[str, Any] = {"path": path, "exists": lookup.get("exists") is True}
    suggestions = lookup.get("suggestions")
    if isinstance(suggestions, list):
        hint["suggestions"] = suggestions[:5]
    replacement = lookup.get("replacement")
    if isinstance(replacement, (str, dict)):
        hint["replacement"] = replacement
    return hint


def _operator_context_hint(problem: BaseException, code: str) -> dict[str, str] | None:
    if "bpy.ops.import_scene.fbx" not in code or "context missing active object" not in str(problem).lower():
        return None
    return {
        "recommended_tool": "interchange.import",
        "reason": "FBX import needs operator context that python.exec may not have in a windowless MCP call.",
        "fallback": "If interchange.import reports the same context error, import in background Blender.",
    }


def view3d_override():
    """Return a Blender temp_override context manager for a visible 3D view."""
    bpy = get_bpy()
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((region for region in area.regions if region.type == "WINDOW"), None)
                if region is not None:
                    return bpy.context.temp_override(window=window, area=area, region=region)
    raise InvalidStateError("No VIEW_3D window area is available for bpy.ops override")


def _validate(params: dict[str, Any]) -> tuple[str, str, Any]:
    unknown = sorted(set(params) - {"code", "expected_state", "checkpoint", "diff", "timeout_hint_s",
                                    "collapse_face_ratio", "collapse_volume_ratio", "bbox_jump_ratio",
                                    "fragment_small_ratio"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    code = params.get("code")
    if not isinstance(code, str):
        raise InvalidParamsError("code must be a string")
    mode = params.get("diff", "summary")
    if mode not in {"none", "summary", "detailed"}:
        raise InvalidParamsError("diff must be none, summary, or detailed")
    checkpoint = params.get("checkpoint", False)
    if not isinstance(checkpoint, (bool, str)) or (isinstance(checkpoint, str) and not checkpoint):
        raise InvalidParamsError("checkpoint must be a boolean or non-empty tag string")
    hint = params.get("timeout_hint_s")
    if hint is not None and (isinstance(hint, bool) or not isinstance(hint, (int, float)) or not math.isfinite(hint) or hint <= 0):
        raise InvalidParamsError("timeout_hint_s must be a positive finite number")
    for key, default, upper in (("collapse_face_ratio", .25, 1),
                                ("collapse_volume_ratio", .25, 1),
                                ("bbox_jump_ratio", 2.0, None)):
        value = params.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value <= 0 or upper is not None and value >= upper or
                key == "bbox_jump_ratio" and value <= 1):
            raise InvalidParamsError(f"{key} must be a finite number " +
                                     ("between 0 and 1" if upper is not None else "greater than 1"))
    fragment_ratio = params.get("fragment_small_ratio", 0.01)
    if (isinstance(fragment_ratio, bool) or not isinstance(fragment_ratio, (int, float)) or
            not math.isfinite(fragment_ratio) or not 0 < fragment_ratio <= 1):
        raise InvalidParamsError("fragment_small_ratio must be a finite number in (0, 1]")
    return code, mode, checkpoint


def execute_python(params: dict[str, Any]) -> ToolResult:
    code, mode, checkpoint_requested = _validate(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    checkpoint_path = None
    if checkpoint_requested:
        tag = checkpoint_requested if isinstance(checkpoint_requested, str) else "python-exec"
        checkpoint_path = save_checkpoint({"tag": tag})["path"]
    before_path = str(getattr(bpy.data, "filepath", "") or "")
    before_token = current_state_token(bpy)
    update_handler = getattr(getattr(bpy.app, "handlers", None), "depsgraph_update_post", None)
    observed_updates: list[tuple[Any, bool, bool, bool]] = []

    def observe(_scene: Any, depsgraph: Any) -> None:
        for update in depsgraph.updates:
            observed_updates.append((update.id, bool(update.is_updated_geometry),
                                     bool(update.is_updated_transform), bool(update.is_updated_shading)))

    detail = mode == "detailed"
    detect = mode != "none"
    fragment_names = _literal_mesh_targets(code) if detect else set()
    fragment_ratio = float(params.get("fragment_small_ratio", 0.01))
    breadcrumb = Path(tempfile.gettempdir()) / f"codex_blender_exec_{os.getpid()}.breadcrumb" if detail else None
    names = _literal_names(code) if detect and not detail else set()
    overhead_start = time.perf_counter()
    before = snapshot(bpy, detail=detail, breadcrumb=breadcrumb) if detect else {}
    fragment_before, fragment_before_skipped = _fragment_summaries(bpy, fragment_names, fragment_ratio) if fragment_names else ({}, {})
    before_properties = _target_properties(bpy, names) if names else {}
    before_objects = _target_object_fields(bpy, names) if names else {}
    overhead_before_ms = (time.perf_counter() - overhead_start) * 1000
    stdout, stderr = _BoundedWriter(), _BoundedWriter()
    namespace = {"__name__": "__main__", "bpy": bpy, "C": bpy.context, "D": bpy.data,
                 "view3d_override": view3d_override}
    import bmesh
    import mathutils
    namespace.update(bmesh=bmesh, mathutils=mathutils, Vector=mathutils.Vector, Matrix=mathutils.Matrix)
    used_modules: dict[str, str] = {}
    problem: BaseException | None = None
    user_traceback = ""
    if update_handler is not None and detect:
        update_handler.append(observe)
    user_start = time.perf_counter()
    exec_user_ms = 0.0
    evaluation_ms = 0.0
    try:
        with exec_module_imports(bpy, used_modules) as module_builtins:
            namespace["__builtins__"] = module_builtins
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exec(compile(code, "<python.exec>", "exec"), namespace, namespace)
    except BaseException as exc:
        problem = exc
        user_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        lines = user_traceback.splitlines()
        start = next((index for index, line in enumerate(lines) if '<python.exec>' in line), 0)
        user_traceback = "\n".join(lines[start:])[-8192:]
    finally:
        exec_user_ms = (time.perf_counter() - user_start) * 1000
        if update_handler is not None and detect:
            evaluation_start = time.perf_counter()
            try:
                bpy.context.view_layer.update()
            except (ReferenceError, RuntimeError) as exc:
                if problem is None:
                    problem = exc
                    user_traceback = traceback.format_exc()[-8192:]
            finally:
                if observe in update_handler:
                    update_handler.remove(observe)
                evaluation_ms = (time.perf_counter() - evaluation_start) * 1000
    overhead_after_start = time.perf_counter()
    after_path = str(getattr(bpy.data, "filepath", "") or "")
    after = snapshot(bpy, detail=detail, breadcrumb=breadcrumb) if detect else {}
    fragment_after, fragment_after_skipped = _fragment_summaries(bpy, fragment_names, fragment_ratio) if fragment_names else ({}, {})
    if detect and not detail:
        _mark_updates(before, after, observed_updates)
        after_properties = _target_properties(bpy, names)
        after_objects = _target_object_fields(bpy, names)
        for key, fields in before_objects.items():
            if key in before.get("objects", {}) and key in after.get("objects", {}):
                before["objects"][key][1].update(fields)
                after["objects"][key][1].update(after_objects.get(key, fields))
        for (collection_name, key), previous in before_properties.items():
            if key in before.get(collection_name, {}) and key in after.get(collection_name, {}):
                current = after_properties.get((collection_name, key), "")
                if current != previous:
                    before[collection_name][key][1]["other.custom_properties"] = previous
                    after[collection_name][key][1]["other.custom_properties"] = current
    changes = compare_snapshots(before, after, mode) if detect else {}
    # Expensive collapse measurements are available only in explicit detailed
    # mode; the default detector does not visit mesh geometry.
    suspicious = []
    if detail and "geometry" in changes:
        metric_changes = _geometry_changes(before, after, mode)
        if metric_changes:
            changes["object_metrics"] = metric_changes
        suspicious = _suspicious_geometry(
            before, after, float(params.get("collapse_face_ratio", .25)),
            float(params.get("collapse_volume_ratio", .25)), float(params.get("bbox_jump_ratio", 2.0)))
    if fragment_before and fragment_after:
        from .mesh_topology import new_islands

        for name in sorted(fragment_before.keys() & fragment_after.keys()):
            try:
                finding = new_islands(fragment_before[name], fragment_after[name], name)
                if finding is not None:
                    suspicious.append({**finding, "reasons": ["new_islands"]})
            except Exception as exc:
                fragment_after_skipped[name] = type(exc).__name__
    for category, affected in _depsgraph_changes(observed_updates, mode).items() if detect else ():
        recorded = changes.setdefault(category, {})
        for name, reason in affected.items():
            recorded.setdefault(name, reason)
    overhead_after_ms = (time.perf_counter() - overhead_after_start) * 1000
    if breadcrumb is not None:
        breadcrumb.unlink(missing_ok=True)
    exec_overhead_ms = overhead_before_ms + evaluation_ms + overhead_after_ms
    timings = {"timings_ms": {"exec_user": round(exec_user_ms, 3),
                              "exec_overhead": round(exec_overhead_ms, 3),
                              "snapshot_before": round(overhead_before_ms, 3),
                              "snapshot_after_and_diff": round(overhead_after_ms, 3),
                              "depsgraph_evaluation": round(evaluation_ms, 3)}}
    out_of_band_token = current_state_token(bpy)
    # Arbitrary Python can mutate fields outside the snapshot. Always advance the
    # epoch; an empty report is not proof that Blender state was unchanged.
    reconcile(bpy)
    bump_epoch()
    if after_path != before_path:
        follow_active_document(bpy)
    out, out_overflow, out_truncated = stdout.split()
    err, err_overflow, err_truncated = stderr.split()
    result, repr_used = _json_result(namespace.get("result"))
    data = {
        "result": result, "result_repr": repr_used,
        "stdout": out, "stdout_overflow": out_overflow, "stdout_truncated": out_truncated,
        "stdout_capture_limit_reached": stdout.limit_reached,
        "stderr": err, "stderr_overflow": err_overflow, "stderr_truncated": err_truncated,
        "stderr_capture_limit_reached": stderr.limit_reached,
        "changes": changes, "changed": bool(changes),
        "suspicious_change": suspicious,
        "fragment_check": {"coverage": "literal_mesh_object_targets",
                           "checked_objects": sorted(fragment_before.keys() & fragment_after.keys()),
                           "skipped": {**fragment_before_skipped, **fragment_after_skipped}},
        "modules": dict(sorted(used_modules.items())),
        "state_token": current_state_token(bpy), "checkpoint": checkpoint_path,
    }
    data["detection"] = "disabled" if not detect else ("detailed" if detail else "depsgraph+inventory")
    if detect and not changes:
        data["change_status"] = "no_changes_detected"
    warnings = []
    if suspicious:
        warnings.append("Suspicious geometry change on " + ", ".join(
            f"{item['object']} ({', '.join(item['reasons'])})" for item in suspicious[:NAME_LIMIT]))
    if "render.opengl" in code or "GPUOffScreen" in code:
        warnings.append("GPU operations from the bridge timer may crash Blender; use the deferred scene.screenshot or render tools.")
    if after_path != before_path or out_of_band_token != before_token or "read_homefile" in code or "addon_utils" in code:
        warnings.append("The active file or add-on state may have changed; reselect/verify the bridge instance before another call.")
    if problem is not None:
        data["partial_change_possible"] = True
        hint = _api_hint(problem)
        if hint is not None:
            data["api_hint"] = hint
        operation_hint = _operator_context_hint(problem, code)
        if operation_hint is not None:
            data["operation_hint"] = operation_hint
        failure = PythonExecError(problem, data, user_traceback)
        failure.meta = timings
        failure.warnings = warnings
        raise failure
    return ToolResult(data, warnings, timings)
