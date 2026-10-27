"""R4 mixed-route fixture. Verifier: blender -b --factory-startup --python tests/run_ai_source_bake_headless.py.

This is synthetic qualification, not the final-helmet acceptance replay.
"""
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import bpy
from mathutils import Matrix, Vector
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.errors import MutationApplyError
from blenderboo.tools.ai_source_bake import (
    bake_from_ai_source, _BakeScene, _low_part_occlusion_samples, _options, _owned_parts, _uv_masks)


def plane(name, vertices, faces, uv_faces):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    for poly, coords in zip(mesh.polygons, uv_faces):
        for loop, point in zip(poly.loop_indices, coords):
            uv.data[loop].uv = point
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def snapshot():
    high_material = high.data.materials[0]
    return {
        "scene": bpy.context.scene.name,
        "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
        "selected": sorted(o.name for o in bpy.context.selected_objects),
        "engine": bpy.context.scene.render.engine,
        "filepath": bpy.data.filepath,
        "ids": {kind: sorted(v.name for v in getattr(bpy.data, kind)) for kind in
                ("scenes", "objects", "meshes", "materials", "images")},
        "high_links": sorted((l.from_node.name, l.from_socket.name, l.to_node.name, l.to_socket.name)
                             for l in high_material.node_tree.links),
        "high_transform": [list(row) for row in high.matrix_world],
        "high_materials": [m.name for m in high.data.materials],
        "low_materials": [m.name for m in low.data.materials],
        "low_vertices": [tuple(v.co) for v in low.data.vertices],
        "low_parts": [v.value for v in low.data.attributes["part"].data],
    }


transform = Matrix.Translation((3, -2, 1)) @ Matrix.Rotation(0.4, 4, "Z")
high = plane("AI High", [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [(0, 1, 2, 3)],
             [[(.05, .05), (.95, .05), (.95, .95), (.05, .95)]])
high.matrix_world = transform
low_local = [(-.9, -.9, .2), (-.1, -.9, .2), (-.1, .9, .2), (-.9, .9, .2),
             (.1, -.9, .002), (.9, -.9, .002), (.9, .9, .002), (.1, .9, .002)]
low = plane("Clean Low", [transform @ Vector(v) for v in low_local], [(0, 1, 2, 3), (4, 5, 6, 7)],
            [[(.05, .05), (.45, .05), (.45, .95), (.05, .95)],
             [(.55, .05), (.95, .05), (.95, .95), (.55, .95)]])
part = low.data.attributes.new("part", "INT", "FACE")
part.data[0].value, part.data[1].value = 1, 2
material = bpy.data.materials.new("Metallic source graph")
material.use_nodes = True
high.data.materials.append(material)
bsdf = material.node_tree.nodes.get("Principled BSDF")
source_image = bpy.data.images.new("Source linear values", 8, 8, float_buffer=True)
source_image.colorspace_settings.name = "Non-Color"
source_image.pixels.foreach_set(np.tile((.3, .2, .8, 1), 64).astype(np.float32))
texture = material.node_tree.nodes.new("ShaderNodeTexImage")
texture.image = source_image
separate = material.node_tree.nodes.new("ShaderNodeSeparateColor")
material.node_tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
material.node_tree.links.new(texture.outputs["Color"], separate.inputs["Color"])
material.node_tree.links.new(separate.outputs["Green"], bsdf.inputs["Roughness"])
material.node_tree.links.new(separate.outputs["Blue"], bsdf.inputs["Metallic"])
bpy.context.view_layer.update()
before = snapshot()
with tempfile.TemporaryDirectory() as temp:
    options = dict(low=low.name, high=high.name, resolution=64, part_attribute="part", shell_parts=[1],
                   cage_extrusion=.01, max_ray_distance=.05, highpass_sigma_px=2,
                   normal_convention="DIRECTX", pack="ORM", output_dir=temp, margin=2, ao_samples=4)
    receipt = bake_from_ai_source(options)
    data = receipt.data
    assert snapshot() == before, "Bake leaked changes into the source document"
    assert data["shell_texels"] > 100 and data["ray_part_texels"] > 100, data
    assert data["nearest_hit_texels"] == data["shell_texels"], data
    assert data["miss_pct"] < 1, data
    assert data["black_texels_pct"] < .1, data
    assert data["black_texels"] == data["miss_texels"], data
    assert data["nearest_cache_miss_texels"] == 0, data
    assert "SourceCacheHitMask" in data["paths"], data
    assert "raw_dark_pct" in data and "valid_dark_pct" in data, data
    assert data["normal_miss_pct"] > 40, "Displaced shell should miss normal rays"
    assert data["normal_flat_fallback_texels"] == data["normal_miss_texels"]
    assert "FallbackHitMask" in data["paths"]
    for name, path in data["paths"].items():
        assert Path(path).stat().st_size > 0, name
    # Verify saved channel meaning, not just files or handler completion.
    def pixels(name):
        image = bpy.data.images.load(data["paths"][name], check_existing=False)
        image.colorspace_settings.name = "sRGB" if name == "BaseColor" else "Non-Color"
        result = np.array(image.pixels[:]).reshape(64, 64, 4)[..., :3]
        bpy.data.images.remove(image)
        return result
    mask = pixels("UVMask")[..., 0] > .5
    for name, expected in (("BaseColor", (.3, .2, .8)), ("Roughness", (.2, .2, .2)),
                           ("Metallic", (.8, .8, .8))):
        np.testing.assert_allclose(np.median(pixels(name)[mask], axis=0), expected, atol=.02)
    orm, ao = pixels("ORM"), pixels("AO")
    np.testing.assert_allclose(orm[mask, 0], ao[mask, 0], atol=.005)
    np.testing.assert_allclose(np.median(orm[mask, 1:], axis=0), (.2, .8), atol=.02)
    assert snapshot() == before
    fixed = {"base_color": [.7, .1, .2], "roughness": .4, "metallic": .8, "ao": .43, "normal": "FLAT"}
    filled = bake_from_ai_source({**options, "fill_face_attribute": "part", "fill_parts": {"2": fixed}}).data
    assert filled["fixed_fill_texels"] == filled["ray_part_texels"] > 0, filled
    assert filled["fixed_fill_parts"] == {"2": filled["fixed_fill_texels"]}, filled
    assert filled["part_breakdown"]["2"]["fixed_fill_texels"] == filled["fixed_fill_texels"]
    assert "FixedFillMask" in filled["paths"]
    def filled_pixels(name):
        image = bpy.data.images.load(filled["paths"][name], check_existing=False)
        image.colorspace_settings.name = "sRGB" if name == "BaseColor" else "Non-Color"
        values = np.array(image.pixels[:]).reshape(64, 64, 4)[..., :3]
        bpy.data.images.remove(image)
        return values
    fixed_mask = filled_pixels("FixedFillMask")[..., 0] > .5
    for name, expected in (("BaseColor", fixed["base_color"]),
                           ("Roughness", [fixed["roughness"]] * 3),
                           ("Metallic", [fixed["metallic"]] * 3),
                           ("AO", [fixed["ao"]] * 3),
                           ("NormalDX", [.5, .5, 1])):
        np.testing.assert_allclose(np.median(filled_pixels(name)[fixed_mask], axis=0), expected, atol=.02)
    assert snapshot() == before
    # Force the near coplanar ray-routed part to miss while keeping its source
    # surface within a bounded fallback distance.
    progress_updates = []
    fallback = bake_from_ai_source({**options, "cage_extrusion": 0,
                                    "max_ray_distance": .001, "fallback_max_distance": .01},
                                   progress=progress_updates.append).data
    assert fallback["ray_part_miss_texels"] > 0, fallback
    assert fallback["fallback_hit_texels"] > 0, fallback
    assert fallback["fallback_hit_texels"] <= fallback["fallback_candidate_texels"]
    assert fallback["normal_flat_fallback_texels"] == fallback["normal_miss_texels"]
    assert any(item["phase"] == "nearest_projection" and
               item["processed_texels"] == item["total_texels"] for item in progress_updates)
    assert {"ray_bake", "material_bake", "normal_bake", "ao_bake", "writing_maps"} <= {
        item["phase"] for item in progress_updates}
    assert snapshot() == before
    # Cleanup also applies if Cycles raises midway through a rewritten source material.
    with patch.object(_BakeScene, "bake", side_effect=RuntimeError("injected bake failure")):
        try:
            bake_from_ai_source(options)
        except MutationApplyError as exc:
            assert "injected bake failure" in str(exc)
        else:
            raise AssertionError("Injected failure did not propagate")
    assert snapshot() == before, "Failure cleanup leaked Blender state"
    occlusion = plane("Occlusion Low", [(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0),
                                        (-1,-1,.1),(1,-1,.1),(1,1,.1),(-1,1,.1)],
                      [(0,1,2,3),(4,5,6,7)],
                      [[(0,0),(.45,0),(.45,1),(0,1)],[(.55,0),(.95,0),(.95,1),(.55,1)]])
    occlusion_part = occlusion.data.attributes.new("part", "INT", "FACE")
    occlusion_part.data[0].value, occlusion_part.data[1].value = 4, 5
    occupied, _, owned_tri, _, _ = _uv_masks(occlusion.data, 32, "part")
    owned_part = _owned_parts(occlusion.data, owned_tri, "part")
    candidate = occupied & (owned_part == 4)
    probe = _low_part_occlusion_samples(occlusion, 32, owned_tri, owned_part, candidate,
                                        "part", _options({"low": occlusion.name, "high": high.name,
                                                          "output_dir": temp, "part_attribute": "part",
                                                          "max_ray_distance": .2}))
    assert probe["status"] == "sampled" and probe["potential_other_part_samples"] > 0, probe
    assert probe["by_part"]["4"]["potential_other_part_samples"] > 0, probe
    print("PASS R4 mixed nearest/ray, metal emission, ORM, world transforms, and cleanup", data)
