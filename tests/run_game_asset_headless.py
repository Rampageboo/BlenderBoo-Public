"""Synthetic UV/export regression checks; does not qualify production assets.

Run: blender -b --factory-startup --python-exit-code 1 --python tests/run_game_asset_headless.py
"""
from pathlib import Path
import sys
import tempfile

import bpy

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
from blenderboo.tools.game_asset_export import export_game_asset
from blenderboo.tools.uv import audit_uv, pack_uv, unwrap_uv


def mesh(name, coords, faces, uv_faces):
    data = bpy.data.meshes.new(name)
    data.from_pydata(coords, [], faces)
    data.update()
    uv = data.uv_layers.new(name="UVMap")
    for face, values in zip(data.polygons, uv_faces):
        for loop, value in zip(face.loop_indices, values):
            uv.data[loop].uv = value
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def context():
    return (bpy.context.mode, sorted(o.name for o in bpy.context.selected_objects),
            bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None)


def geometry(obj):
    return ([tuple(v.co) for v in obj.data.vertices], [tuple(p.vertices) for p in obj.data.polygons],
            [tuple(row) for row in obj.matrix_world])


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.unit_settings.scale_length = 1
square = [(0, 0), (1, 0), (1, 1), (0, 1)]
plane = mesh("Density", [(u, v, 0) for u, v in square], [(0, 1, 2, 3)], [square])
bpy.ops.mesh.primitive_cube_add(location=(4, 2, 1))
before = context()
audit = audit_uv({"object_name": plane.name, "texture_size": 1024, "raster_size": 64,
                  "target_px_per_cm": 10, "max_overlap_pct": .01}).data
assert audit["pass"] and audit["overlap_pct"] == 0, audit
assert abs(audit["px_per_cm"]["median"] - 10.24) < 1e-7, audit
assert context() == before

stacked = mesh("Stacked", [(u, v, z) for z in (0, 1) for u, v in square],
               [(0, 1, 2, 3), (4, 5, 6, 7)], [square, square])
audit = audit_uv({"object_name": stacked.name, "texture_size": 1024, "raster_size": 64,
                  "max_overlap_pct": .01}).data
assert audit["overlap_pct"] == 100 and not audit["pass"], audit

coords = [(0, .5), (.25, .25), (.75, .75), (.5, 1), (.5, 0), (1, .5)]
faces = [(0, 1, 2, 3), (1, 4, 5, 2)]
adjacent = mesh("Adjacent", [(u, v, 0) for u, v in coords], faces,
                [[coords[i] for i in face] for face in faces])
audit = audit_uv({"object_name": adjacent.name, "texture_size": 1024, "raster_size": 128,
                  "max_overlap_pct": .01}).data
assert audit["pass"] and audit["overlap_pct"] == 0, audit
folded = mesh("FoldedUVQuad", [(u, v, 0) for u, v in square], [(0, 1, 2, 3)],
              [[(0, 0), (1, 0), (0, 1), (1, 1)]])
audit = audit_uv({"object_name": folded.name, "texture_size": 1024, "raster_size": 128,
                  "max_overlap_pct": .01}).data
assert audit["overlap_pct"] > .01 and not audit["pass"], audit
print("PASS UV density, stacked overlap, shared diagonal boundary and folded-quad overlap")

source = geometry(plane)
unwrap_uv({"object_name": plane.name, "layer_name": "UVMap", "method": "MINIMUM_STRETCH", "dry_run": False})
pack_uv({"object_name": plane.name, "layer_name": "UVMap", "average_island_scale": True, "dry_run": False})
assert geometry(plane) == source and context() == before
assert all(len(face.vertices) == 4 for face in plane.data.polygons)
print("PASS SLIM unwrap and average-island-scale pack preserve source and context")

with tempfile.TemporaryDirectory(prefix="cbt_game_asset_") as temporary:
    root = Path(temporary)
    for scale in (1, .01):
        bpy.context.scene.unit_settings.scale_length = scale
        plane.location, plane.scale = (3, -2, 1), (2, 1, .5)
        bpy.context.view_layer.update()
        source, selected = geometry(plane), context()
        inventory = (sorted(o.name for o in bpy.data.objects), sorted(m.name for m in bpy.data.meshes))
        folder = root / ("metres" if scale == 1 else "centimetres")
        result = export_game_asset({"object_names": [plane.name], "asset_name": "Fixture",
                                    "output_dir": str(folder), "pivot": "BOUNDS_CENTER", "dry_run": False}).data
        assert geometry(plane) == source and context() == selected
        assert inventory == (sorted(o.name for o in bpy.data.objects), sorted(m.name for m in bpy.data.meshes))
        assert all(len(face.vertices) == 4 for face in plane.data.polygons)
        assert (folder / "SM_Fixture.fbx").stat().st_size > 0
        assert (folder / "SM_Fixture.ue.json").stat().st_size > 0
        objects_before = set(bpy.data.objects)
        bpy.ops.import_scene.fbx(filepath=str(folder / "SM_Fixture.fbx"))
        imported = [o for o in bpy.data.objects if o not in objects_before and o.type == "MESH"]
        assert len(imported) == 1 and len(imported[0].data.polygons) == 2
        assert all(len(face.vertices) == 3 for face in imported[0].data.polygons)
        actual = tuple(value * scale for value in imported[0].dimensions)
        assert abs(actual[0] - 2 * scale) < 1e-7 and abs(actual[1] - scale) < 1e-7, actual
        print("PASS FBX triangulation and physical scale", scale, actual)

print("GAME_ASSET_HEADLESS_OK")
