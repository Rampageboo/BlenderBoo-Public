"""Disposable Blender fixtures for USP atlas/seams/join. Run with --background --factory-startup."""
from pathlib import Path
import math
import sys

import bpy
from mathutils import Matrix

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.dispatcher import dispatch
from blenderboo.tools.mesh_join import join_meshes
from blenderboo.tools.uv import audit_uv, seams_from_regions, cut_strips_by_turn


def make_mesh(name, vertices, faces, collection=None):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def quad(name, collection=None):
    obj = make_mesh(name, [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [(0, 1, 2, 3)], collection)
    layer = obj.data.uv_layers.new(name="Atlas")
    for item, uv in zip(layer.data, [(0, 0), (1, 0), (1, 1), (0, 1)]):
        item.uv = uv
    attr = obj.data.attributes.new(name="part", type="INT", domain="FACE")
    attr.data[0].value = 11
    obj.data.edges[0].use_edge_sharp = True
    obj.data.edges[1].use_seam = True
    return obj


def mesh_signature(obj):
    mesh = obj.data
    return (tuple(tuple(v.co) for v in mesh.vertices), tuple(tuple(p.vertices) for p in mesh.polygons),
            tuple((layer.name, tuple(tuple(item.uv) for item in layer.data)) for layer in mesh.uv_layers),
            tuple((e.use_seam, e.use_edge_sharp) for e in mesh.edges),
            tuple(tuple(row) for row in obj.matrix_world))


def rejected(call, params):
    before = (set(bpy.data.objects.keys()), set(bpy.data.meshes.keys()))
    try:
        call(params)
    except (InvalidStateError, InvalidParamsError):
        pass
    else:
        raise AssertionError(f"Expected refusal: {params}")
    assert before == (set(bpy.data.objects.keys()), set(bpy.data.meshes.keys()))


def run():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    hidden = bpy.data.collections.new("ExcludedSources")
    bpy.context.scene.collection.children.link(hidden)
    a, b = quad("PartA", hidden), quad("PartB", hidden)
    b.data.attributes["part"].data[0].value = 22
    material_a, material_b = bpy.data.materials.new("MatA"), bpy.data.materials.new("MatB")
    a.data.materials.append(material_a)
    b.data.materials.append(material_b)
    b.matrix_world = Matrix.Translation((3, 4, 5)) @ Matrix.Diagonal((-2, 3, 1, 1))
    detail = b.data.uv_layers.new(name="Detail")
    for item in detail.data:
        item.uv = (0.25, 0.75)
    # Keep Atlas as the explicit audit layer even if Detail became active.
    audit_args = {"layer_name": "Atlas", "texture_size": 1024, "raster_size": 64}
    single = audit_uv({"object_name": a.name, **audit_args}).data
    assert single["overlap_pct"] == 0 and single["island_count"] == 1, single
    atlas = audit_uv({"object_names": [a.name, b.name], "max_overlap_pct": 0, **audit_args}).data
    assert atlas["overlap_pct"] == 100 and not atlas["pass"], atlas
    assert atlas["self_overlap_raster_pixels"] == 0, atlas
    assert atlas["cross_object_overlap_raster_pixels"] > 0, atlas
    assert atlas["overlap_pairs"] == [{"object_a": "PartA", "face_a": 0, "object_b": "PartB", "face_b": 0, "kind": "cross_object", "pixels": atlas["covered_raster_pixels"]}], atlas
    rejected(audit_uv, {"object_names": [a.name, b.name], "object_name": a.name, **audit_args})
    rejected(audit_uv, {"object_names": [a.name, b.name], "max_uv_items": 7, **audit_args})

    sentinel = make_mesh("Sentinel", [(0, 0, 0)], [])
    sentinel.select_set(True)
    bpy.context.view_layer.objects.active = sentinel
    layer_collection = bpy.context.view_layer.layer_collection.children[hidden.name]
    layer_collection.exclude = True
    before_selection = {obj.name for obj in bpy.context.selected_objects}
    signatures = {obj.name: mesh_signature(obj) for obj in (a, b)}
    plan = join_meshes({"object_names": [a.name, b.name], "name": "Joined"})
    assert not plan["changed"] and "Joined" not in bpy.data.objects
    result = join_meshes({"object_names": [a.name, b.name], "name": "Joined", "dry_run": False})
    joined = bpy.data.objects[result["object"]]
    assert result["changed"] and joined.matrix_world == Matrix.Identity(4), result
    assert len(joined.data.polygons) == 2 and len(joined.data.vertices) == 8
    assert [item.value for item in joined.data.attributes["part"].data] == [11, 22]
    assert [slot.material.name for slot in joined.material_slots] == ["MatA", "MatB"]
    assert [face.material_index for face in joined.data.polygons] == [0, 1]
    for source, face, offset in ((a, joined.data.polygons[0], 0), (b, joined.data.polygons[1], 4)):
        for loop_index in face.loop_indices:
            vertex_index = joined.data.loops[loop_index].vertex_index
            source_index = vertex_index-offset
            assert (joined.data.vertices[vertex_index].co - source.matrix_world @ source.data.vertices[source_index].co).length < 1e-6
            source_loop = next(i for i in source.data.polygons[0].loop_indices if source.data.loops[i].vertex_index == source_index)
            assert (joined.data.uv_layers["Atlas"].data[loop_index].uv-source.data.uv_layers["Atlas"].data[source_loop].uv).length < 1e-6
        # Mirror compensation keeps outward normals aligned after negative scale.
        expected_normal = source.matrix_world.to_3x3().inverted().transposed() @ source.data.polygons[0].normal
        assert face.normal.dot(expected_normal.normalized()) > 0.999
    assert all(tuple(joined.data.uv_layers["Detail"].data[i].uv) == (0, 0) for i in range(4))
    assert all(tuple(joined.data.uv_layers["Detail"].data[i].uv) == (0.25, 0.75) for i in range(4, 8))
    assert sum(e.use_edge_sharp for e in joined.data.edges) == 2
    assert sum(e.use_seam for e in joined.data.edges) == 2
    assert signatures == {obj.name: mesh_signature(obj) for obj in (a, b)}
    assert {obj.name for obj in bpy.context.selected_objects} == before_selection
    assert bpy.context.view_layer.objects.active is sentinel and layer_collection.exclude
    # Blender primitives expose UV edit-selection internals as mesh attributes.
    # They must not make an ordinary named join appear lossy.
    bpy.ops.mesh.primitive_plane_add(size=1)
    primitive_a = bpy.context.object
    primitive_a.name = "PrimitiveUV_A"
    bpy.ops.mesh.primitive_plane_add(size=1)
    primitive_b = bpy.context.object
    primitive_b.name = "PrimitiveUV_B"
    assert ".uv_select_vert" in primitive_a.data.attributes
    primitive_join = join_meshes({"object_names": [primitive_a.name, primitive_b.name],
                                  "name": "PrimitiveUV_Joined", "dry_run": False})
    assert primitive_join["changed"] and primitive_join["sources_preserved"]
    rejected(join_meshes, {"object_names": [a.name, b.name], "name": "Joined", "dry_run": False})
    a.modifiers.new(name="RefuseModifier", type="BEVEL")
    rejected(join_meshes, {"object_names": [a.name, b.name], "name": "Unsupported", "dry_run": False})
    a.modifiers.clear()
    unsupported = a.data.attributes.new(name="UnsupportedPoint", type="FLOAT", domain="POINT")
    rejected(join_meshes, {"object_names": [a.name, b.name], "name": "Unsupported", "dry_run": False})
    a.data.attributes.remove(unsupported)

    strip = make_mesh("Strip", [(x, y, 0) for x in range(24) for y in (0, 1)],
                      [(2*i, 2*i+2, 2*i+3, 2*i+1) for i in range(23)])
    regions = strip.data.attributes.new(name="fregion", type="INT", domain="FACE")
    for face in strip.data.polygons:
        regions.data[face.index].value = int(face.index >= 12)
    edge_map = {tuple(sorted(edge.vertices)): edge.index for edge in strip.data.edges}
    rows = [[edge_map[(2*i, 2*i+1)]] for i in range(24)]
    seam_args = {"object_name": strip.name, "attribute_name": "fregion", "include_boundary": False}
    before = mesh_signature(strip)
    plan = seams_from_regions(seam_args)
    assert not plan["changed"] and plan["seam_edge_indices"] == rows[12]
    assert mesh_signature(strip) == before
    applied = seams_from_regions({**seam_args, "dry_run": False})
    assert applied["changed"] and strip.data.edges[rows[12][0]].use_seam
    assert not seams_from_regions({**seam_args, "dry_run": False})["changed"]
    strip_args = {"object_name": strip.name, "strips": [{"edge_rows": rows}]}
    plan = cut_strips_by_turn(strip_args)
    assert plan["strips"][0]["cut_rows"] == [0, 10, 20, 23], plan
    assert not plan["changed"]
    applied = cut_strips_by_turn({**strip_args, "dry_run": False})
    assert applied["changed"] and all(strip.data.edges[rows[i][0]].use_seam for i in (0, 10, 12, 20, 23))
    before = mesh_signature(strip)
    rejected(cut_strips_by_turn, {"object_name": strip.name, "strips": [{"edge_rows": [rows[0], rows[2]]}], "dry_run": False})
    rejected(seams_from_regions, {"object_name": strip.name, "attribute_name": "missing", "dry_run": False})
    assert mesh_signature(strip) == before
    named_route = dispatch("mesh.join", {"object_names": [a.name, b.name],
                                         "name": "DispatcherPlan", "dry_run": True})
    assert named_route["ok"] and named_route["data"]["sources_preserved"], named_route
    cached_defaults = dispatch("mesh.join", {"object_names": [a.name, b.name],
        "name": "CachedSchemaPlan", "dry_run": True, "keep_sources": False,
        "max_elements": 500000, "fragment_small_ratio": 0.01, "confirm": True})
    assert cached_defaults["ok"] and cached_defaults["data"]["sources_preserved"], cached_defaults
    conflict = dispatch("mesh.join", {"object_names": [a.name, b.name],
        "name": "ConflictingPlan", "keep_sources": True})
    assert not conflict["ok"] and "conflicts" in conflict["error"]["message"], conflict
    print("USP_UV_JOIN_HEADLESS_OK")


if __name__ == "__main__":
    run()
