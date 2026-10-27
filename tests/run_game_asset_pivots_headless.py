"""Real FBX pivot/placement roundtrips and full-export failure preservation.

Run: blender -b --factory-startup --python-exit-code 1 --python tests/run_game_asset_pivots_headless.py
Synthetic evidence only; Unreal import options still require qualification.
"""
from pathlib import Path
import json
import os
import sys
import tempfile
from unittest.mock import patch

import bpy
from mathutils import Vector

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
from blenderboo.tools import game_asset_export as export


def make_mesh(name, location, rotation, scale):
    data = bpy.data.meshes.new(name)
    # Local geometry deliberately sits to one side of the hinge origin.
    data.from_pydata([(0, 0, 0), (2, 0, 0), (2, 1, .5), (0, 1, .5)], [], [(0, 1, 2, 3)])
    data.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location, obj.rotation_euler, obj.scale = location, rotation, scale
    return obj


def snapshot(objects):
    return (
        [(obj.name, tuple(tuple(row) for row in obj.matrix_world),
          tuple(tuple(v.co) for v in obj.data.vertices), tuple(tuple(p.vertices) for p in obj.data.polygons),
          tuple(tuple(loop.uv) for loop in obj.data.uv_layers.active.data)) for obj in objects],
        tuple(sorted(obj.name for obj in bpy.context.selected_objects)),
        bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
        tuple(sorted(obj.name for obj in bpy.data.objects)), tuple(sorted(mesh.name for mesh in bpy.data.meshes)),
    )


def close_vector(actual, expected, label, tolerance=2e-5):
    assert (Vector(actual) - Vector(expected)).length < tolerance, (label, tuple(actual), tuple(expected))


def files(folder):
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


bpy.ops.wm.read_factory_settings(use_empty=True)
parts = [make_mesh("TriggerHinge", (3, -2, 1), (.2, -.3, .7), (2, .6, 1.3)),
         make_mesh("MagazinePull", (-4, 5, 2), (-.4, .1, -.2), (.7, 1.5, .8))]
bpy.context.view_layer.update()
parts[1].select_set(True)
bpy.context.view_layer.objects.active = parts[1]

with tempfile.TemporaryDirectory(prefix="cbt_pivot_roundtrip_") as temporary:
    root = Path(temporary)
    for unit_scale in (1.0, .01):
        bpy.context.scene.unit_settings.scale_length = unit_scale
        for pivot in ("ORIGIN", "BOUNDS_CENTER", "OBJECT_ORIGIN"):
            for count in (1, 2):
                sources = parts[:count]
                asset = f"Pivot_{pivot}_{count}"
                folder = root / f"{asset}_{unit_scale}"
                before = snapshot(parts)
                params = {"object_names": [obj.name for obj in sources], "asset_name": asset,
                          "output_dir": str(folder), "pivot": pivot}
                planned = export.export_game_asset(params).data
                assert planned["dry_run"] and not folder.exists()
                result = export.export_game_asset({**params, "dry_run": False}).data
                assert snapshot(parts) == before, ("source or context changed", pivot, unit_scale)
                manifest = result["manifest"]
                assert json.loads((folder / f"SM_{asset}.ue.json").read_text()) == manifest
                source_pivots = {obj.name: list(obj.matrix_world.translation) for obj in sources}
                assert manifest["object_pivots_world_scene_units"] == source_pivots
                if pivot == "OBJECT_ORIGIN":
                    assert manifest["pivot_world_scene_units"] == (source_pivots[sources[0].name] if count == 1 else None)
                previous_objects = set(bpy.data.objects)
                bpy.ops.import_scene.fbx(filepath=str(folder / f"SM_{asset}.fbx"))
                imported = [obj for obj in bpy.data.objects if obj not in previous_objects]
                assert len(imported) == count, [obj.name for obj in imported]
                bpy.context.view_layer.update()
                for i, source in enumerate(sources):
                    obj = next(obj for obj in imported if obj.name == f"SM_{asset}_{i:03d}")
                    assert len(obj.data.polygons) == 2
                    assert all(len(face.vertices) == 3 for face in obj.data.polygons)
                    source_center = Vector((0, 0, 0)) if pivot != "BOUNDS_CENTER" else Vector(manifest["pivot_world_scene_units"])
                    expected_origin = source.matrix_world.translation if pivot == "OBJECT_ORIGIN" else Vector((0, 0, 0))
                    close_vector(obj.matrix_world.translation * unit_scale, expected_origin * unit_scale, "physical pivot")
                    for actual, original in zip(obj.data.vertices, source.data.vertices):
                        expected = ((source.matrix_world @ original.co) - source_center) * unit_scale
                        close_vector((obj.matrix_world @ actual.co) * unit_scale, expected, "physical vertex")
                    if pivot == "OBJECT_ORIGIN":
                        for axis in range(3):
                            close_vector(obj.matrix_world.to_3x3().col[axis].normalized(),
                                         source.matrix_world.to_3x3().col[axis].normalized(), "local pivot axis")
                for obj in imported:
                    data = obj.data
                    bpy.data.objects.remove(obj, do_unlink=True)
                    if data.users == 0:
                        bpy.data.meshes.remove(data)
                # Imports change selection; restore it before taking the next export snapshot.
                for obj in bpy.context.selected_objects:
                    obj.select_set(False)
                parts[1].select_set(True)
                bpy.context.view_layer.objects.active = parts[1]
                print("PASS real FBX pivot/placement", pivot, count, unit_scale)

    # Existing destination textures can themselves be sources. Copying happens before publication.
    folder = root / "shared"
    folder.mkdir()
    (folder / "SM_Other.fbx").write_bytes(b"unrelated mesh")
    texture = folder / "T_Transactional_BaseColor.png"
    texture.write_bytes(b"fixture texture bytes")
    params = {"object_names": [parts[0].name], "asset_name": "Transactional", "output_dir": str(folder),
              "pivot": "OBJECT_ORIGIN", "overwrite": True, "dry_run": False,
              "texture_paths": {"BaseColor": str(texture)}}
    export.export_game_asset(params)
    original = files(folder)
    before = snapshot(parts)
    dry = export.export_game_asset({**params, "dry_run": True})
    assert dry.data["dry_run"] and files(folder) == original and snapshot(parts) == before
    # A mid-copy failure must not touch the previous FBX, sidecar or textures.
    with patch.object(export.shutil, "copy2", side_effect=OSError("injected staging failure")):
        try:
            export.export_game_asset(params)
        except OSError as exc:
            assert "injected staging failure" in str(exc)
        else:
            raise AssertionError("Expected staging failure")
    assert files(folder) == original and snapshot(parts) == before
    assert not list(root.glob(".blenderboo_*"))
    # A mid-publication failure must restore existing files and remove newly written files.
    added = root / "new_normal.png"
    added.write_bytes(b"new normal")
    real_replace = os.replace
    def fail_manifest(source, target):
        source = Path(source)
        if source.name == "SM_Transactional.ue.json" and source.parent.name != "previous":
            raise OSError("injected publication failure")
        return real_replace(source, target)
    with patch.object(export.os, "replace", side_effect=fail_manifest):
        try:
            export.export_game_asset({**params, "texture_paths": {"BaseColor": str(texture), "NormalDX": str(added)}})
        except OSError as exc:
            assert "injected publication failure" in str(exc)
        else:
            raise AssertionError("Expected publication failure")
    assert files(folder) == original and snapshot(parts) == before
    assert not list(root.glob(".blenderboo_*"))
    result = export.export_game_asset({**params, "collision": "auto_convex"})
    assert any("fill cavities" in warning for warning in result.warnings)
    assert (folder / "SM_Other.fbx").read_bytes() == b"unrelated mesh"
    assert texture.read_bytes() == b"fixture texture bytes"
    assert snapshot(parts) == before
    print("PASS shared destination, same-path texture source, dry run, staging/publication rollback and source preservation")

print("GAME_ASSET_PIVOTS_HEADLESS_OK")
