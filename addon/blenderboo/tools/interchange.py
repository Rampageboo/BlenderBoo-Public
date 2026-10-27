"""Guarded core-format import and export with explicit preflight and postflight."""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any

from ..errors import ConfirmationRequiredError, InvalidParamsError, InvalidStateError, MutationApplyError, NameConflictError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


DEFAULT_IMPORT_BYTES = 2 * 1024 * 1024 * 1024
HARD_IMPORT_BYTES = 16 * 1024 * 1024 * 1024
MAX_REPORT_ITEMS = 2000

FORMATS: dict[str, dict[str, Any]] = {
    "PLY": {
        "extensions": (".ply",),
        "import": ("internal", "ply_import"),
        "export": ("wm", "ply_export"),
        "import_options": set(),
        "export_options": {"ascii_format", "export_selected_objects", "apply_modifiers", "export_normals", "export_colors"},
        "selection_option": "export_selected_objects",
    },
    "OBJ": {
        "extensions": (".obj",),
        "import": ("wm", "obj_import"),
        "export": ("wm", "obj_export"),
        "import_options": {
            "forward_axis", "up_axis", "global_scale", "clamp_size", "use_split_objects",
            "use_split_groups", "import_vertex_groups", "validate_meshes", "close_spline_loops",
        },
        "export_options": {
            "forward_axis", "up_axis", "global_scale", "apply_modifiers", "export_eval_mode",
            "export_uv", "export_normals", "export_colors", "export_triangulated_mesh",
            "export_curves_as_nurbs",
        },
        "selection_option": "export_selected_objects",
    },
    "FBX": {
        "extensions": (".fbx",),
        "import": ("import_scene", "fbx"),
        "export": ("export_scene", "fbx"),
        "import_options": {
            "global_scale", "use_custom_normals", "use_image_search", "use_anim", "anim_offset",
            "decal_offset", "use_subsurf", "use_custom_props", "use_custom_props_enum_as_string",
            "ignore_leaf_bones", "force_connect_children", "automatic_bone_orientation",
            "primary_bone_axis", "secondary_bone_axis", "use_prepost_rot",
        },
        "export_options": {
            "global_scale", "apply_unit_scale", "bake_space_transform", "object_types",
            "use_mesh_modifiers", "mesh_smooth_type", "use_subsurf", "use_mesh_edges",
            "use_tspace", "use_custom_props", "add_leaf_bones", "bake_anim",
        },
        "selection_option": "use_selection",
    },
    "GLTF": {
        "extensions": (".glb", ".gltf"),
        "export_extensions": (".glb",),
        "import": ("import_scene", "gltf"),
        "export": ("export_scene", "gltf"),
        "import_options": {
            "import_pack_images", "merge_vertices", "import_shading", "guess_original_bind_pose",
            "bone_heuristic", "disable_bone_shape",
        },
        "export_options": {
            "export_apply", "export_texcoords", "export_normals", "export_tangents",
            "export_materials", "export_colors", "export_attributes", "export_cameras",
            "export_lights", "export_animations", "export_frame_range", "export_current_frame",
            "export_extras", "export_yup",
        },
        "selection_option": "use_selection",
    },
    "USD": {
        "extensions": (".usd", ".usda", ".usdc", ".usdz"),
        "import": ("wm", "usd_import"),
        "export": ("wm", "usd_export"),
        "import_options": {
            "scale", "light_intensity_scale", "validate_meshes",
            "create_collection", "read_mesh_uvs", "read_mesh_colors", "import_cameras",
            "import_curves", "import_lights", "import_materials", "import_meshes", "import_volumes",
            "import_shapes", "import_skeletons", "import_blendshapes", "import_subdiv",
            "support_scene_instancing",
        },
        "export_options": {
            "export_animation", "export_hair", "export_uvmaps", "export_normals",
            "export_materials", "export_meshes", "export_lights", "export_cameras",
            "export_curves", "export_armatures", "export_shapekeys", "export_custom_properties",
            "root_prim_path", "evaluation_mode", "generate_preview_surface",
        },
        "selection_option": "selected_objects_only",
    },
    "ALEMBIC": {
        "extensions": (".abc",),
        "import": ("wm", "alembic_import"),
        "export": ("wm", "alembic_export"),
        "import_options": {"scale", "validate_meshes", "is_sequence", "always_add_cache_reader"},
        "export_options": {
            "start", "end", "xsamples", "gsamples", "shutter_open", "shutter_close",
            "global_scale", "flatten", "uvs", "packuv", "normals", "vcolors", "face_sets",
            "subdiv_schema", "apply_subdiv", "curves_as_mesh", "use_instancing", "triangulate",
        },
        "selection_option": "selected",
    },
}

DATA_COLLECTIONS = (
    "objects", "collections", "meshes", "curves", "cameras", "lights", "armatures",
    "materials", "images", "actions", "node_groups", "worlds", "cache_files", "volumes",
    "pointclouds", "grease_pencils", "hair_curves", "lattices", "metaballs", "particles",
    "shape_keys", "speakers", "libraries",
)


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(names)}")


def _strict_bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidParamsError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _optional_name(value: Any, label: str) -> str | None:
    return None if value is None else _name(value, label)


def _format(value: Any, path: Path | None, *, export: bool) -> str:
    if value is None:
        if path is None:
            raise InvalidParamsError("format is required when no filepath is supplied")
        suffix = path.suffix.lower()
        matches = [
            name for name, detail in FORMATS.items()
            if suffix in detail.get("export_extensions" if export else "extensions", detail["extensions"])
        ]
        if len(matches) != 1:
            raise InvalidParamsError(f"Cannot infer an allowlisted format from extension: {suffix or '<none>'}")
        return matches[0]
    if not isinstance(value, str) or value.upper() not in FORMATS:
        raise InvalidParamsError("format must be OBJ, FBX, GLTF, USD, ALEMBIC, or PLY")
    return value.upper()


def _path(value: Any, label: str) -> Path:
    return Path(_name(value, label)).expanduser().resolve()


def _validate_extension(path: Path, format_name: str, *, export: bool) -> None:
    detail = FORMATS[format_name]
    extensions = detail.get("export_extensions" if export else "extensions", detail["extensions"])
    if path.suffix.lower() not in extensions:
        expected = ", ".join(extensions)
        raise InvalidParamsError(f"{format_name} {'export' if export else 'import'} filepath must end in {expected}")


def _operator(bpy: Any, format_name: str, direction: str) -> Any:
    if format_name == "PLY" and direction == "import":
        return _import_ply
    namespace, name = FORMATS[format_name][direction]
    group = getattr(bpy.ops, namespace, None)
    operator = getattr(group, name, None) if group is not None else None
    if operator is None:
        raise InvalidStateError(f"Blender operator is unavailable: bpy.ops.{namespace}.{name}")
    try:
        operator.get_rna_type()
    except Exception as exc:
        raise InvalidStateError(f"Blender operator is unavailable: bpy.ops.{namespace}.{name}") from exc
    return operator


def _operator_path(format_name: str, direction: str) -> str:
    if format_name == "PLY" and direction == "import":
        return "internal.ply_import"
    namespace, name = FORMATS[format_name][direction]
    return f"bpy.ops.{namespace}.{name}"


def _json_value(value: Any, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidParamsError(f"{label} must be finite")
        return value
    if isinstance(value, (list, tuple, set)) and len(value) <= 64:
        return [_json_value(item, label) for item in value]
    raise InvalidParamsError(f"{label} must be a JSON scalar or bounded array")


def _rna_property(operator: Any, name: str) -> Any:
    properties = operator.get_rna_type().properties
    try:
        return properties[name]
    except (KeyError, TypeError):
        return None


def _validate_option_value(prop: Any, value: Any, label: str) -> Any:
    value = _json_value(value, label)
    prop_type = str(getattr(prop, "type", ""))
    is_array = bool(getattr(prop, "is_array", False))
    if is_array:
        if not isinstance(value, list):
            raise InvalidParamsError(f"{label} must be an array")
        length = int(getattr(prop, "array_length", 0))
        if length and len(value) != length:
            raise InvalidParamsError(f"{label} must contain {length} items")
        return value
    if prop_type == "BOOLEAN" and not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    if prop_type == "INT" and (isinstance(value, bool) or not isinstance(value, int)):
        raise InvalidParamsError(f"{label} must be an integer")
    if prop_type == "FLOAT" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise InvalidParamsError(f"{label} must be a number")
    if prop_type in {"INT", "FLOAT"}:
        minimum = getattr(prop, "hard_min", None)
        maximum = getattr(prop, "hard_max", None)
        if minimum is not None and value < minimum or maximum is not None and value > maximum:
            raise InvalidParamsError(f"{label} is outside the operator's supported range")
    if prop_type == "STRING" and not isinstance(value, str):
        raise InvalidParamsError(f"{label} must be a string")
    if prop_type == "ENUM":
        identifiers = {item.identifier for item in getattr(prop, "enum_items", ())}
        if bool(getattr(prop, "is_enum_flag", False)):
            if not isinstance(value, list) or not set(value) <= identifiers:
                raise InvalidParamsError(f"{label} must contain only available enum identifiers")
            return set(value)
        if not isinstance(value, str) or value not in identifiers:
            raise InvalidParamsError(f"{label} is not an available enum identifier")
    return value


def _options(operator: Any, format_name: str, direction: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidParamsError("options must be an object")
    allowed = FORMATS[format_name][f"{direction}_options"]
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown or unsafe {format_name} {direction} options: {', '.join(unknown)}")
    if format_name == "PLY" and direction == "import":
        return {}
    result = {}
    for name, raw in value.items():
        prop = _rna_property(operator, name)
        if prop is None:
            raise InvalidStateError(f"Option is unavailable in this Blender build: {format_name}.{direction}.{name}")
        result[name] = _validate_option_value(prop, raw, f"options.{name}")
    return result


def _display_options(options: dict[str, Any]) -> dict[str, Any]:
    return {
        name: sorted(value) if isinstance(value, set) else value
        for name, value in options.items()
    }


def _operator_available(bpy: Any, format_name: str, direction: str) -> bool:
    try:
        _operator(bpy, format_name, direction)
        return True
    except InvalidStateError:
        return False


def inspect_interchange(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"format", "direction"})
    format_name = None if params.get("format") is None else _format(params["format"], None, export=False)
    direction = params.get("direction")
    if direction is not None and direction not in {"import", "export"}:
        raise InvalidParamsError("direction must be import or export")
    bpy = get_bpy()
    records = []
    for name in sorted(FORMATS):
        if format_name is not None and name != format_name:
            continue
        detail = FORMATS[name]
        record: dict[str, Any] = {
            "format": name,
            "import_extensions": list(detail["extensions"]),
            "export_extensions": list(detail.get("export_extensions", detail["extensions"])),
        }
        for candidate in ("import", "export"):
            if direction is None or direction == candidate:
                record[candidate] = {
                    "available": _operator_available(bpy, name, candidate),
                    "operator": _operator_path(name, candidate),
                    "options": sorted(detail[f"{candidate}_options"]),
                }
        records.append(record)
    return ToolResult(
        data={
            "formats": records,
            "default_import_byte_budget": DEFAULT_IMPORT_BYTES,
            "hard_import_byte_cap": HARD_IMPORT_BYTES,
            "state_token": current_state_token(bpy),
        }
    )


def _snapshot_data(bpy: Any) -> dict[str, set[int]]:
    return {
        name: {int(item.as_pointer()) for item in getattr(bpy.data, name)}
        for name in DATA_COLLECTIONS
        if hasattr(bpy.data, name)
    }


def _new_data(bpy: Any, before: dict[str, set[int]]) -> dict[str, list[Any]]:
    return {
        name: [item for item in getattr(bpy.data, name) if int(item.as_pointer()) not in before[name]]
        for name in before
    }


def _created_summary(created: dict[str, list[Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, items in created.items():
        if not items:
            continue
        ordered = sorted((item.name for item in items))
        result[name] = {
            "count": len(ordered),
            "names": ordered[:MAX_REPORT_ITEMS],
            "truncated": len(ordered) > MAX_REPORT_ITEMS,
        }
    return result


def _remove_new_data(bpy: Any, before: dict[str, set[int]]) -> list[str]:
    errors: list[str] = []
    for name in before:
        collection = getattr(bpy.data, name)
        for item in reversed(list(collection)):
            if int(item.as_pointer()) in before[name]:
                continue
            try:
                collection.remove(item, do_unlink=True)
            except TypeError:
                try:
                    collection.remove(item)
                except Exception as exc:
                    errors.append(f"remove {name}.{item.name}: {exc}")
            except Exception as exc:
                errors.append(f"remove {name}.{item.name}: {exc}")
    return errors


def _selection_state(bpy: Any) -> tuple[list[str], str | None]:
    selected = [obj.name for obj in bpy.context.selected_objects]
    active = getattr(bpy.context.view_layer.objects.active, "name", None)
    return selected, active


def _restore_selection(bpy: Any, state: tuple[list[str], str | None]) -> list[str]:
    errors: list[str] = []
    try:
        for obj in bpy.context.view_layer.objects:
            obj.select_set(False)
        for name in state[0]:
            obj = bpy.context.view_layer.objects.get(name)
            if obj is not None:
                obj.select_set(True)
        bpy.context.view_layer.objects.active = bpy.context.view_layer.objects.get(state[1]) if state[1] else None
    except Exception as exc:
        errors.append(f"restore selection: {exc}")
    return errors


_PLY_TYPES = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B",
    "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
    "int": "i", "int32": "i", "uint": "I", "uint32": "I",
    "float": "f", "float32": "f", "double": "d", "float64": "d",
}


def _ply_header(stream: Any) -> tuple[str, list[tuple[str, int, list[tuple[str, str, str | None]]]]]:
    if stream.readline().strip() != b"ply":
        raise InvalidParamsError("PLY file must start with 'ply'")
    encoding = None
    elements: list[tuple[str, int, list[tuple[str, str, str | None]]]] = []
    header_bytes = 4
    while True:
        raw = stream.readline()
        header_bytes += len(raw)
        if not raw or header_bytes > 1_000_000:
            raise InvalidParamsError("PLY header is missing end_header or exceeds 1 MB")
        try:
            parts = raw.decode("ascii").strip().split()
        except UnicodeDecodeError as exc:
            raise InvalidParamsError("PLY header must be ASCII") from exc
        if not parts or parts[0] in {"comment", "obj_info"}:
            continue
        if parts[0] == "end_header":
            break
        if parts[0] == "format":
            if len(parts) != 3 or parts[2] != "1.0" or parts[1] not in {"ascii", "binary_little_endian", "binary_big_endian"}:
                raise InvalidParamsError("Unsupported PLY format/version")
            encoding = parts[1]
        elif parts[0] == "element":
            if len(parts) != 3 or not parts[2].isdigit():
                raise InvalidParamsError("Invalid PLY element declaration")
            count = int(parts[2])
            if count > 10_000_000:
                raise InvalidParamsError("PLY element count exceeds 10 million")
            elements.append((parts[1], count, []))
        elif parts[0] == "property":
            if not elements:
                raise InvalidParamsError("PLY property has no element")
            if len(parts) == 3 and parts[1] in _PLY_TYPES:
                elements[-1][2].append((parts[2], parts[1], None))
            elif len(parts) == 5 and parts[1] == "list" and parts[2] in _PLY_TYPES and parts[3] in _PLY_TYPES:
                elements[-1][2].append((parts[4], parts[3], parts[2]))
            else:
                raise InvalidParamsError("Unsupported PLY property declaration")
        else:
            raise InvalidParamsError(f"Unsupported PLY header directive: {parts[0]}")
    if encoding is None or not any(name == "vertex" for name, _, _ in elements):
        raise InvalidParamsError("PLY requires format and vertex declarations")
    return encoding, elements


def _ply_records(stream: Any, encoding: str, count: int, properties: list[tuple[str, str, str | None]]):
    endian = "<" if encoding == "binary_little_endian" else ">"
    for _ in range(count):
        tokens = stream.readline().split() if encoding == "ascii" else None
        token_index = 0
        record = {}
        for name, scalar_type, count_type in properties:
            def read_scalar(type_name: str):
                nonlocal token_index
                code = _PLY_TYPES[type_name]
                if tokens is not None:
                    if token_index >= len(tokens):
                        raise InvalidParamsError("Truncated ASCII PLY record")
                    raw = tokens[token_index]
                    token_index += 1
                    return float(raw) if code in "fd" else int(raw)
                size = struct.calcsize(code)
                raw = stream.read(size)
                if len(raw) != size:
                    raise InvalidParamsError("Truncated binary PLY record")
                return struct.unpack(endian + code, raw)[0]
            if count_type is None:
                record[name] = read_scalar(scalar_type)
            else:
                length = read_scalar(count_type)
                if length < 0 or length > 10_000_000:
                    raise InvalidParamsError("PLY list length is invalid or too large")
                record[name] = [read_scalar(scalar_type) for _ in range(length)]
        if tokens is not None and token_index != len(tokens):
            raise InvalidParamsError("ASCII PLY record contains extra values")
        yield record


def _import_ply(*, filepath: str) -> set[str]:
    bpy = get_bpy()
    vertices = []
    faces = []
    point_values: list[dict[str, Any]] = []
    vertex_properties = []
    with Path(filepath).open("rb") as stream:
        encoding, elements = _ply_header(stream)
        for element_name, count, properties in elements:
            if element_name == "vertex":
                vertex_properties = properties
            for record in _ply_records(stream, encoding, count, properties):
                if element_name == "vertex":
                    if not all(name in record for name in ("x", "y", "z")):
                        raise InvalidParamsError("PLY vertices require x, y, z")
                    position = tuple(float(record[name]) for name in ("x", "y", "z"))
                    if not all(math.isfinite(value) for value in position):
                        raise InvalidParamsError("PLY vertex coordinates must be finite")
                    vertices.append(position)
                    point_values.append(record)
                elif element_name == "face":
                    indices = record.get("vertex_indices", record.get("vertex_index"))
                    if indices is None or not isinstance(indices, list) or len(indices) < 3:
                        raise InvalidParamsError("PLY faces require at least three vertex indices")
                    faces.append(indices)
    if any(any(isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(vertices) for index in face) for face in faces):
        raise InvalidParamsError("PLY face references an invalid vertex index")
    mesh = bpy.data.meshes.new(Path(filepath).stem)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    properties_by_name = {name: kind for name, kind, list_type in vertex_properties if list_type is None}
    if all(name in properties_by_name for name in ("nx", "ny", "nz")):
        normals = mesh.attributes.new("ply_normal", "FLOAT_VECTOR", "POINT")
        for index, record in enumerate(point_values):
            normals.data[index].vector = tuple(float(record[name]) for name in ("nx", "ny", "nz"))
    if all(name in properties_by_name for name in ("red", "green", "blue")):
        colors = mesh.attributes.new("ply_color", "FLOAT_COLOR", "POINT")
        scale = 65535.0 if any(properties_by_name[name] in {"ushort", "uint16"} for name in ("red", "green", "blue")) else 255.0
        if all(properties_by_name[name] in {"float", "float32", "double", "float64"} for name in ("red", "green", "blue")):
            scale = 1.0
        for index, record in enumerate(point_values):
            colors.data[index].color = tuple(float(record[name]) / scale for name in ("red", "green", "blue")) + (float(record.get("alpha", scale)) / scale,)
    obj = bpy.data.objects.new(Path(filepath).stem, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return {"FINISHED"}


def _ply_import_summary(created: dict[str, list[Any]]) -> list[dict[str, Any]]:
    result = []
    for obj in created.get("objects", []):
        if obj.type != "MESH":
            continue
        mesh = obj.data
        points = [tuple(float(axis) for axis in vert.co) for vert in mesh.vertices]
        bounds = None if not points else {
            "min": [min(point[axis] for point in points) for axis in range(3)],
            "max": [max(point[axis] for point in points) for axis in range(3)],
        }
        result.append({
            "object": obj.name,
            "mesh": mesh.name,
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
            "bounds": bounds,
            "attributes": [
                {"name": attr.name, "domain": attr.domain, "data_type": attr.data_type}
                for attr in mesh.attributes
            ],
        })
    return result


def import_interchange(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"format", "filepath", "options", "max_bytes", "dry_run", "confirm", "expected_state"})
    path = _path(params.get("filepath"), "filepath")
    format_name = _format(params.get("format"), path, export=False)
    _validate_extension(path, format_name, export=False)
    if not path.exists() or not path.is_file():
        raise NotFoundError(f"Import file not found: {path}")
    max_bytes = _integer(params.get("max_bytes", DEFAULT_IMPORT_BYTES), "max_bytes", 1, HARD_IMPORT_BYTES)
    size = path.stat().st_size
    if size > max_bytes:
        raise InvalidParamsError(f"Import file is {size} bytes, exceeding max_bytes {max_bytes}")
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm", False)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    operator = _operator(bpy, format_name, "import")
    options = _options(operator, format_name, "import", params.get("options"))
    transaction = new_transaction([{"type": "Scene", "name": bpy.context.scene.name}])
    transaction.update(
        action="interchange.import", format=format_name, filepath=str(path), file_size=size,
        operator=_operator_path(format_name, "import"), options=_display_options(options), dry_run=dry_run,
        confirmation_required=True, changed=False, state_token=current_state_token(bpy),
    )
    if dry_run:
        return transaction
    if params.get("confirm") is not True:
        raise ConfirmationRequiredError("interchange.import requires confirm: true after reviewing the dry-run", transaction)
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("interchange.import requires Blender Object mode")
    before = _snapshot_data(bpy)
    selection = _selection_state(bpy)
    try:
        result = operator(filepath=str(path), **options)
        if "FINISHED" not in set(result):
            raise RuntimeError(f"Blender import operator returned {sorted(result)}")
    except Exception as exc:
        rollback_errors = _remove_new_data(bpy, before)
        rollback_errors.extend(_restore_selection(bpy, selection))
        remaining = _created_summary(_new_data(bpy, before))
        transaction.update(changed=bool(remaining) or bool(rollback_errors), warnings=rollback_errors, partial=remaining)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"interchange.import failed: {exc}", transaction) from exc
    warnings = _restore_selection(bpy, selection)
    created_data = _new_data(bpy, before)
    created = _created_summary(created_data)
    object_count = created.get("objects", {}).get("count", 0)
    if object_count == 0:
        warnings.append("Import completed but created no Object datablocks")
    transaction.update(changed=bool(created) or bool(warnings), created=created, warnings=warnings)
    if format_name == "PLY":
        transaction["ply_meshes"] = _ply_import_summary(created_data)
    if transaction["changed"]:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def import_blend_data(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"filepath", "data_type", "names", "link", "target_collection", "max_bytes", "dry_run", "confirm", "expected_state"})
    path = _path(params.get("filepath"), "filepath")
    if path.suffix.lower() != ".blend":
        raise InvalidParamsError("interchange.blend_import filepath must end in .blend")
    if not path.is_file():
        raise NotFoundError(f"Blend file not found: {path}")
    data_type = str(params.get("data_type", "COLLECTION")).upper()
    if data_type not in {"COLLECTION", "OBJECT"}:
        raise InvalidParamsError("data_type must be COLLECTION or OBJECT")
    names = params.get("names")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not names or len(names) > MAX_REPORT_ITEMS or not all(isinstance(item, str) and item for item in names):
        raise InvalidParamsError(f"names must contain 1 to {MAX_REPORT_ITEMS} non-empty names")
    if len(names) != len(set(names)):
        raise InvalidParamsError("names must not contain duplicates")
    link = _strict_bool(params.get("link"), "link", False)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm", False)
    max_bytes = _integer(params.get("max_bytes", DEFAULT_IMPORT_BYTES), "max_bytes", 1, HARD_IMPORT_BYTES)
    size = path.stat().st_size
    if size > max_bytes:
        raise InvalidParamsError(f"Blend file is {size} bytes, exceeding max_bytes {max_bytes}")
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    attribute = "collections" if data_type == "COLLECTION" else "objects"
    with bpy.data.libraries.load(str(path), link=link, relative=False) as (data_from, _data_to):
        available = set(getattr(data_from, attribute))
    missing = sorted(set(names) - available)
    if missing:
        raise NotFoundError(f"Named {data_type.lower()} data not found in blend file: {', '.join(missing)}")
    target_name = _optional_name(params.get("target_collection"), "target_collection")
    target = bpy.context.scene.collection if target_name is None else bpy.data.collections.get(target_name)
    if target is None:
        raise NotFoundError(f"Target collection not found: {target_name}")
    existing = sorted(name for name in names if getattr(bpy.data, attribute).get(name) is not None)
    if existing:
        raise NameConflictError(
            f"Target document already contains named {data_type.lower()} data: {', '.join(existing)}",
            {"conflicts": [{"type": data_type.title(), "name": name} for name in existing]},
        )
    transaction = new_transaction([{"type": "BlendFile", "name": path.name}])
    transaction.update(action="interchange.blend_import", filepath=str(path), file_size=size, data_type=data_type, names=list(names), link=link, target_collection=target.name, dry_run=dry_run, confirmation_required=True, changed=False, state_token=current_state_token(bpy))
    if dry_run:
        return transaction
    if params.get("confirm") is not True:
        raise ConfirmationRequiredError("interchange.blend_import requires confirm: true after reviewing the dry-run", transaction)
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("interchange.blend_import requires Blender Object mode")
    before = _snapshot_data(bpy)
    try:
        with bpy.data.libraries.load(str(path), link=link, relative=False) as (_data_from, data_to):
            setattr(data_to, attribute, list(names))
        loaded = list(getattr(data_to, attribute))
        if any(item is None for item in loaded):
            raise RuntimeError("Blender returned an unresolved linked/appended datablock")
        if data_type == "COLLECTION":
            for collection in loaded:
                if target.children.get(collection.name) is not collection:
                    target.children.link(collection)
        else:
            for obj in loaded:
                if not obj.users_collection:
                    target.objects.link(obj)
    except Exception as exc:
        rollback_errors = _remove_new_data(bpy, before)
        remaining = _created_summary(_new_data(bpy, before))
        transaction.update(changed=bool(remaining) or bool(rollback_errors), warnings=rollback_errors, partial=remaining)
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"interchange.blend_import failed: {exc}", transaction) from exc
    created = _created_summary(_new_data(bpy, before))
    transaction.update(changed=True, created=created, loaded_names=[item.name for item in loaded])
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _scope_objects(bpy: Any, scope: str, collection_name: str | None) -> list[Any]:
    if scope == "selected":
        objects = list(bpy.context.selected_objects)
    elif scope == "scene":
        objects = list(bpy.context.scene.objects)
    else:
        if collection_name is None:
            raise InvalidParamsError("collection is required when scope is collection")
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            raise NotFoundError(f"Collection not found: {collection_name}")
        scene_objects = set(bpy.context.scene.objects)
        objects = [obj for obj in collection.all_objects if obj in scene_objects]
    if not objects:
        raise InvalidStateError(f"Export scope contains no objects: {scope}")
    return sorted(set(objects), key=lambda obj: obj.name)


def _inject_export_guards(operator: Any, format_name: str, options: dict[str, Any], selected_only: bool) -> None:
    selection_option = FORMATS[format_name]["selection_option"]
    if _rna_property(operator, selection_option) is None:
        raise InvalidStateError(f"Selection option is unavailable in this Blender build: {selection_option}")
    options[selection_option] = selected_only
    if format_name == "GLTF":
        if _rna_property(operator, "export_format") is None:
            raise InvalidStateError("glTF export_format is unavailable in this Blender build")
        options["export_format"] = "GLB"
    if format_name == "OBJ" and _rna_property(operator, "export_materials") is not None:
        options["export_materials"] = False
    if format_name == "USD" and _rna_property(operator, "export_textures") is not None:
        options["export_textures"] = False


def _select_scope(bpy: Any, objects: list[Any]) -> None:
    targets = set(objects)
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj in targets)
    bpy.context.view_layer.objects.active = next(
        (obj for obj in objects if bpy.context.view_layer.objects.get(obj.name) is obj),
        None,
    )


def export_interchange(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"format", "filepath", "scope", "collection", "options", "overwrite", "dry_run", "confirm", "expected_state"})
    path = _path(params.get("filepath"), "filepath")
    format_name = _format(params.get("format"), path, export=True)
    _validate_extension(path, format_name, export=True)
    if not path.parent.exists() or not path.parent.is_dir():
        raise NotFoundError(f"Export directory does not exist: {path.parent}")
    scope = params.get("scope", "selected")
    if scope not in {"selected", "scene", "collection"}:
        raise InvalidParamsError("scope must be selected, scene, or collection")
    collection_name = _optional_name(params.get("collection"), "collection")
    if scope != "collection" and collection_name is not None:
        raise InvalidParamsError("collection is only valid when scope is collection")
    overwrite = _strict_bool(params.get("overwrite"), "overwrite", False)
    dry_run = _strict_bool(params.get("dry_run"), "dry_run", True)
    if path.exists() and not path.is_file():
        raise InvalidParamsError(f"Export target exists and is not a file: {path}")
    if path.exists() and not overwrite:
        raise InvalidParamsError(f"Export target already exists and overwrite is false: {path}")
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm", False)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    operator = _operator(bpy, format_name, "export")
    options = _options(operator, format_name, "export", params.get("options"))
    objects = _scope_objects(bpy, scope, collection_name)
    selected_only = scope != "scene"
    _inject_export_guards(operator, format_name, options, selected_only)
    transaction = new_transaction([{"type": "File", "name": str(path)}])
    transaction.update(
        action="interchange.export", format=format_name, filepath=str(path), scope=scope,
        collection=collection_name, object_count=len(objects), object_names=[obj.name for obj in objects[:MAX_REPORT_ITEMS]],
        objects_truncated=len(objects) > MAX_REPORT_ITEMS, operator=_operator_path(format_name, "export"),
        options=_display_options(options), overwrite=overwrite, dry_run=dry_run, confirmation_required=True,
        changed=False, state_token=current_state_token(bpy),
    )
    if dry_run:
        return transaction
    if params.get("confirm") is not True:
        raise ConfirmationRequiredError("interchange.export requires confirm: true after reviewing the dry-run", transaction)
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("interchange.export requires Blender Object mode")
    marker = transaction["operation_id"].replace("op-", "")
    temporary = path.with_name(f".{path.stem}.cbt-{marker}{path.suffix}")
    backup = path.with_name(f".{path.stem}.cbt-{marker}.backup{path.suffix}")
    if temporary.exists() or backup.exists():
        raise InvalidStateError("Guarded export temporary or backup path already exists; remove the stale artifact explicitly")
    selection = _selection_state(bpy)
    moved_original = False
    installed_new = False
    warnings: list[str] = []
    try:
        if selected_only:
            _select_scope(bpy, objects)
        result = operator(filepath=str(temporary), **options)
        if "FINISHED" not in set(result):
            raise RuntimeError(f"Blender export operator returned {sorted(result)}")
        if not temporary.exists() or not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RuntimeError("Blender export did not produce a non-empty staged file")
        if path.exists():
            path.replace(backup)
            moved_original = True
        temporary.replace(path)
        installed_new = True
        if backup.exists():
            try:
                backup.unlink()
                moved_original = False
            except Exception as cleanup_exc:
                warnings.append(f"remove replaced-file backup {backup}: {cleanup_exc}")
    except Exception as exc:
        cleanup_errors: list[str] = []
        if moved_original and backup.exists() and not path.exists():
            try:
                backup.replace(path)
                moved_original = False
            except Exception as rollback_exc:
                cleanup_errors.append(f"restore overwritten export: {rollback_exc}")
        if temporary.exists():
            try:
                temporary.unlink()
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove staged export: {cleanup_exc}")
        cleanup_errors.extend(_restore_selection(bpy, selection))
        transaction.update(
            changed=installed_new or moved_original or bool(cleanup_errors),
            warnings=cleanup_errors,
            partial={"target_exists": path.exists(), "backup_exists": backup.exists(), "staged_exists": temporary.exists()},
        )
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"interchange.export failed: {exc}", transaction) from exc
    warnings.extend(_restore_selection(bpy, selection))
    transaction.update(changed=True, file_size=path.stat().st_size, warnings=warnings)
    if warnings:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
