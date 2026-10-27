"""Stage a triangulated static-mesh FBX and an explicit Unreal hand-off manifest."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .session_lease import require_document_write_access
from .session_state import check_expected_state, current_state_token


TEXTURE_KINDS = {"BaseColor", "NormalDX", "ORM", "NormalGL", "Roughness", "Metallic", "AO"}
_ASSET_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _number(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not lo <= float(value) <= hi:
        raise InvalidParamsError(f"{name} must be a finite number from {lo} to {hi}")
    return float(value)


class _RollbackError(InvalidStateError):
    """Publication failed and its retained staging directory needs recovery."""


def _validate_output(output_dir: Path, filenames: list[str], overwrite: bool) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise InvalidStateError("output_dir must be a directory")
        if not overwrite and any(output_dir.iterdir()):
            raise InvalidStateError("output_dir must be absent or empty unless overwrite is true")
    for name in filenames:
        target = output_dir / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise InvalidStateError(f"Export target must be a regular file, not a directory or symlink: {target}")


def _publish_files(staging: Path, output_dir: Path, filenames: list[str], overwrite: bool) -> None:
    """Replace only generated files; keep the previous bytes until publication succeeds."""
    _validate_output(output_dir, filenames, overwrite)
    created_directory = not output_dir.exists()
    output_dir.mkdir(exist_ok=True)
    backup = staging / "previous"
    backup.mkdir()
    published: list[str] = []
    try:
        # Back up everything before replacing anything, including a texture used as input.
        for name in filenames:
            target = output_dir / name
            if target.exists():
                shutil.copy2(target, backup / name)
        for name in filenames:
            os.replace(staging / name, output_dir / name)
            published.append(name)
    except Exception as exc:
        failures = []
        for name in reversed(published):
            try:
                previous = backup / name
                if previous.exists():
                    os.replace(previous, output_dir / name)
                else:
                    (output_dir / name).unlink(missing_ok=True)
            except OSError as rollback_exc:
                failures.append(f"{name}: {rollback_exc}")
        if created_directory:
            try:
                output_dir.rmdir()
            except OSError:
                pass  # Another process may have added an unrelated file.
        if failures:
            raise _RollbackError(
                f"Export publication failed ({exc}); rollback incomplete: {'; '.join(failures)}. "
                f"Recovery files retained at {staging}"
            ) from exc
        raise


def export_game_asset(params: dict[str, Any]) -> ToolResult:
    allowed = {"object_names", "asset_name", "output_dir", "texture_paths", "pivot", "lod_group", "collision", "import_yaw", "detail_normal", "detail_tiling", "expected_state", "dry_run", "overwrite"}
    if set(params) - allowed:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(set(params) - allowed))}")
    names = params.get("object_names")
    if not isinstance(names, list) or not 1 <= len(names) <= 128 or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        raise InvalidParamsError("object_names must be 1..128 unique mesh object names")
    asset_name = params.get("asset_name")
    if not isinstance(asset_name, str) or not _ASSET_NAME.fullmatch(asset_name):
        raise InvalidParamsError("asset_name must be 1..64 ASCII alphanumeric or underscore characters, starting with a letter")
    output = params.get("output_dir")
    if not isinstance(output, str) or not output:
        raise InvalidParamsError("output_dir must be an absolute directory path")
    output_dir = Path(output).expanduser()
    if not output_dir.is_absolute() or not output_dir.parent.is_dir():
        raise InvalidParamsError("output_dir must be absolute and its parent must exist")
    output_dir = output_dir.resolve()
    overwrite = params.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise InvalidParamsError("overwrite must be boolean")
    pivot = params.get("pivot", "ORIGIN")
    if pivot not in {"ORIGIN", "BOUNDS_CENTER", "OBJECT_ORIGIN"}:
        raise InvalidParamsError("pivot must be ORIGIN, BOUNDS_CENTER, or OBJECT_ORIGIN")
    collision = params.get("collision", "complex_as_simple")
    if collision not in {"auto_convex", "complex_as_simple", "ucx"}:
        raise InvalidParamsError("collision must be auto_convex, complex_as_simple, or ucx")
    lod_group = params.get("lod_group", "SmallProp")
    if not isinstance(lod_group, str) or not lod_group:
        raise InvalidParamsError("lod_group must be a non-empty string")
    yaw = _number(params.get("import_yaw", 0), "import_yaw", -360, 360)
    tiling = _number(params.get("detail_tiling", 1), "detail_tiling", 0.001, 1000)
    detail_normal = params.get("detail_normal")
    if detail_normal is not None and (not isinstance(detail_normal, str) or not detail_normal):
        raise InvalidParamsError("detail_normal must be a path string")
    textures = params.get("texture_paths", {})
    if not isinstance(textures, dict) or set(textures) - TEXTURE_KINDS or any(not isinstance(value, str) or not value for value in textures.values()):
        raise InvalidParamsError("texture_paths must map known texture kinds to paths")
    texture_sources: dict[str, Path] = {}
    for kind, value in textures.items():
        path = Path(value).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in {".png", ".tga", ".exr"}:
            raise NotFoundError(f"Texture {kind} is missing or not PNG/TGA/EXR: {path}")
        texture_sources[kind] = path
    if detail_normal is not None:
        detail_normal_path = Path(detail_normal).expanduser().resolve()
        if not detail_normal_path.is_file():
            raise NotFoundError(f"Detail normal texture not found: {detail_normal_path}")
    else:
        detail_normal_path = None
    dry_run = params.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise InvalidParamsError("dry_run must be boolean")
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("export.game_asset requires Object mode")
    objects = []
    for name in names:
        obj = bpy.context.scene.objects.get(name)
        if obj is None or obj.type != "MESH" or obj.data is None:
            raise NotFoundError(f"Mesh object not found in scene: {name}")
        if not obj.data.uv_layers.active:
            raise InvalidStateError(f"Mesh object has no active UV layer: {name}")
        objects.append(obj)
    metres_per_unit = float(bpy.context.scene.unit_settings.scale_length)
    if not math.isfinite(metres_per_unit) or metres_per_unit <= 0:
        raise InvalidStateError("Scene scale_length must be positive")
    from mathutils import Vector
    world_points = [obj.matrix_world @ vertex.co for obj in objects for vertex in obj.data.vertices]
    if not world_points:
        raise InvalidStateError("No vertices to export")
    center = Vector((0, 0, 0)) if pivot != "BOUNDS_CENTER" else Vector(tuple((min(v[i] for v in world_points) + max(v[i] for v in world_points)) / 2 for i in range(3)))
    object_pivots = {obj.name: list(obj.matrix_world.translation) for obj in objects}
    pivot_world = (object_pivots[names[0]] if len(objects) == 1 else None) if pivot == "OBJECT_ORIGIN" else list(center)
    warnings = []
    if collision == "auto_convex":
        warnings.append("Auto-convex collision may bridge concavities and fill cavities such as magazine wells; inspect collision in Unreal")
    if collision == "ucx":
        warnings.append("UE 5.8 FBX Interchange may ignore UCX hulls; verify collision in Unreal after import")
    if collision == "complex_as_simple":
        warnings.append("UE importer must set Use Complex Collision As Simple; the FBX alone does not set this")
    manifest = {
        "asset_name": asset_name, "mesh": f"SM_{asset_name}.fbx", "source_objects": names,
        "source_blend": bpy.data.filepath or None, "pivot": pivot,
        "pivot_world_scene_units": pivot_world,
        "object_pivots_world_scene_units": object_pivots, "source_scale_length_metres": metres_per_unit,
        "export_units": "metres", "triangulated_export_only": True,
        "lod_group": lod_group, "collision": collision, "import_yaw": yaw,
        "detail_normal": None, "detail_tiling": tiling, "textures": {},
        "ue_import_verified": False, "warnings": warnings,
    }
    for kind, source in texture_sources.items():
        manifest["textures"][kind] = f"T_{asset_name}_{kind}{source.suffix.lower()}"
    if detail_normal_path is not None:
        manifest["detail_normal"] = f"T_{asset_name}_DetailNormal{detail_normal_path.suffix.lower()}"
    filenames = [manifest["mesh"], *manifest["textures"].values()]
    if manifest["detail_normal"] is not None:
        filenames.append(manifest["detail_normal"])
    filenames.append(f"SM_{asset_name}.ue.json")
    _validate_output(output_dir, filenames, overwrite)
    if dry_run:
        return ToolResult(data={"dry_run": True, "output_dir": str(output_dir), "manifest": manifest, "state_token": current_state_token(bpy)}, warnings=warnings)

    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    original_selection = list(bpy.context.selected_objects)
    original_active = bpy.context.view_layer.objects.active
    temp_objects = []
    temp_meshes = []
    files_written = [output_dir / name for name in filenames]
    staging = Path(tempfile.mkdtemp(prefix=f".blenderboo_{asset_name}_", dir=output_dir.parent))
    cleanup_staging = True
    try:
        import bmesh
        for i, source in enumerate(objects):
            mesh = source.data.copy()
            temp_meshes.append(mesh)
            for vertex in mesh.vertices:
                if pivot == "OBJECT_ORIGIN":
                    vertex.co *= metres_per_unit
                else:
                    vertex.co = ((source.matrix_world @ vertex.co) - center) * metres_per_unit
            bm = bmesh.new()
            try:
                bm.from_mesh(mesh)
                bmesh.ops.triangulate(bm, faces=list(bm.faces), quad_method="BEAUTY", ngon_method="BEAUTY")
                bm.to_mesh(mesh)
            finally:
                bm.free()
            mesh.update()
            clone = bpy.data.objects.new(f"SM_{asset_name}_{i:03d}", mesh)
            if pivot == "OBJECT_ORIGIN":
                # Keep local origin/axes and assembly placement, with translations in metres.
                clone.matrix_world = source.matrix_world.copy()
                clone.matrix_world.translation *= metres_per_unit
            bpy.context.scene.collection.objects.link(clone)
            temp_objects.append(clone)
        for selected in bpy.context.selected_objects:
            selected.select_set(False)
        for clone in temp_objects:
            clone.select_set(True)
        bpy.context.view_layer.objects.active = temp_objects[0]
        fbx = staging / manifest["mesh"]
        result = bpy.ops.export_scene.fbx(
            filepath=str(fbx), use_selection=True, global_scale=1.0,
            apply_unit_scale=False, bake_anim=False, use_mesh_modifiers=False,
            axis_forward="-Y", axis_up="Z", path_mode="STRIP",
        )
        if "FINISHED" not in result or not fbx.is_file() or fbx.stat().st_size == 0:
            raise RuntimeError("FBX export did not produce a non-empty file")
        for kind, source in texture_sources.items():
            target = staging / manifest["textures"][kind]
            shutil.copy2(source, target)
        if detail_normal_path is not None:
            target = staging / manifest["detail_normal"]
            shutil.copy2(detail_normal_path, target)
        sidecar = staging / f"SM_{asset_name}.ue.json"
        sidecar.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _publish_files(staging, output_dir, filenames, overwrite)
    except _RollbackError:
        cleanup_staging = False
        raise
    finally:
        for clone in temp_objects:
            bpy.data.objects.remove(clone, do_unlink=True)
        for mesh in temp_meshes:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        for selected in bpy.context.selected_objects:
            selected.select_set(False)
        for selected in original_selection:
            if bpy.context.scene.objects.get(selected.name) is selected:
                selected.select_set(True)
        bpy.context.view_layer.objects.active = original_active if original_active in bpy.context.view_layer.objects.values() else None
        if cleanup_staging:
            shutil.rmtree(staging)
    return ToolResult(data={"dry_run": False, "output_dir": str(output_dir), "files": [str(path) for path in files_written], "manifest": manifest, "state_token": current_state_token(bpy)}, warnings=warnings)
