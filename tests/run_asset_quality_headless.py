"""Synthetic Blender qualification for asset.quality_gate; run with --python-exit-code 1."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools import asset_quality as quality
from blenderboo.tools.mesh_topology import connectivity_hash
from blenderboo.tools.session_state import current_state_token


def mesh_object(name, points, faces, edges=()):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(points, edges, faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def gate(obj, **params):
    result = quality.quality_gate({"object_name": obj.name, **params}).data
    json.dumps(result, allow_nan=False)
    return result


def reasons(result):
    return {failure["reason"] for failure in result["failures"]}


bpy.ops.wm.read_factory_settings(use_empty=True)
plate = mesh_object("OpenPlate", [(0, 0, 0), (2, 0, 0), (2, 1, 0), (0, 1, 0)], [(0, 1, 2, 3)])
before = (connectivity_hash(plate.data), [tuple(v.co) for v in plate.data.vertices],
          current_state_token(), tuple(bpy.context.selected_objects), bpy.context.view_layer.objects.active,
          len(bpy.data.meshes), len(bpy.data.objects))
report = gate(plate, triangle_budget=2)
assert report["pass"] and report["analysis_complete"], report
assert len(report["poles"]) <= 20 and report["poles_truncated"] == (report["pole_count"] > len(report["poles"]))
assert report["boundary_edges"] == 4 and report["loop_continuity"]["unbroken_share"] == 1
after = (connectivity_hash(plate.data), [tuple(v.co) for v in plate.data.vertices],
         current_state_token(), tuple(bpy.context.selected_objects), bpy.context.view_layer.objects.active,
         len(bpy.data.meshes), len(bpy.data.objects))
assert before == after, (before, after)
assert "triangle_budget_exceeded" in reasons(gate(plate, triangle_budget=1))

# A sliver corner and an n-gon are independent failures with world-space focus.
sliver = mesh_object("Sliver", [(0, 0, 0), (1, 0, 0), (1, .0001, 0)], [(0, 1, 2)])
result = gate(sliver, sliver_min_angle_deg=2)
assert {"sliver_faces", "non_quad_faces"} <= reasons(result), result
assert result["slivers"]["samples"][0]["face"] == 0
ngon = mesh_object("Ngon", [(0, 0, 0), (1, 0, 0), (2, 1, 0), (1, 2, 0), (0, 1, 0)], [(0, 1, 2, 3, 4)])
assert "non_quad_faces" in reasons(gate(ngon))
assert gate(ngon, require_quads=False)["pass"]

# Two non-adjacent quads cross; pair count is one even though both tessellate.
cross = mesh_object("Cross", [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0),
                              (0, -.5, -1), (0, .5, -1), (0, .5, 1), (0, -.5, 1)],
                    [(0, 1, 2, 3), (4, 5, 6, 7)])
cross.location = (10, 20, 30)
bpy.context.view_layer.update()
result = gate(cross)
assert result["self_intersections"]["count"] == 1, result
assert result["self_intersections"]["inside_parts"] == 1
assert result["self_intersections"]["positions"][0] == [10, 20, 30]
parts = cross.data.attributes.new("part_id", "INT", "FACE")
parts.data[0].value, parts.data[1].value = 3, 7
result = gate(cross, part_attribute="part_id")
assert result["self_intersections"]["between_parts"] == 1
assert result["self_intersections"]["inside_parts"] == 0
try:
    gate(cross, part_attribute="missing")
except InvalidParamsError:
    pass
else:
    raise AssertionError("missing part attribute accepted")

with patch.object(quality, "MAX_OVERLAP_CANDIDATES", 0):
    result = gate(cross)
assert not result["pass"] and not result["analysis_complete"], result
assert "analysis_incomplete" in reasons(result)

# A shared vertex deliberately excludes the entire original face pair.
adjacent = mesh_object("VertexNeighbors", [(0, 0, 0), (2, 0, 0), (0, 2, 0), (1, 1, -1), (1, 1, 1)],
                       [(0, 1, 2), (0, 3, 4)])
assert gate(adjacent, require_quads=False)["self_intersections"]["count"] == 0

# Base data is used even with a modifier, and nonuniform scale affects slivers.
plate.scale = (.001, 2, 1)
plate.modifiers.new("Subdivision", "SUBSURF")
bpy.context.view_layer.update()
result = gate(plate, sliver_min_edge=.01)
assert result["slivers"]["count"] == 1 and result["faces"] == 1, result
assert abs(result["slivers"]["samples"][0]["min_edge"] - .002) < 1e-6

# Physical symmetry tolerance respects scene scale, while plane coordinates are scene units.
bpy.context.scene.unit_settings.scale_length = .01
symmetry = mesh_object("Symmetry", [(0, 0, 0), (2, 0, 0), (2, 1, 0), (0, 1, 0)], [(0, 1, 2, 3)])
result = gate(symmetry, symmetry_plane_x=0, symmetry_tolerance_m=.01)
assert "symmetry_tolerance_exceeded" in reasons(result), result
assert abs(result["symmetry"]["max_asymmetry_m"] - .02) < 1e-7
assert gate(symmetry, symmetry_plane_x=0)["pass"]

degenerate = mesh_object("ZeroArea", [(0, 0, 0), (1, 0, 0), (2, 0, 0)], [(0, 1, 2)])
assert "zero_area_faces" in reasons(gate(degenerate, require_quads=False))
wire = mesh_object("Wire", [(0, 0, 0), (1, 0, 0)], [], [(0, 1)])
assert {"empty_mesh", "non_manifold_or_wire_edges"} <= reasons(gate(wire))

bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.object
assert "unexpected_loop_endpoints" in reasons(gate(cube))
partial = gate(cube, allowed_loop_break_vertices=[0])
assert not partial["pass"] and partial["loop_continuity"]["unexpected_endpoint_count"] == 7
assert partial["loop_continuity"]["allowed_endpoint_vertices"] == [0]
allowed = gate(cube, allowed_loop_break_vertices=list(range(8)))
assert allowed["pass"] and allowed["loop_continuity"]["unexpected_endpoint_count"] == 0
assert allowed["loop_continuity"]["allowed_endpoint_count"] == 8
assert allowed["allowed_loop_break_vertices"] == list(range(8))
assert len(allowed["loop_continuity"]["allowed_endpoint_positions"]) == 8
try:
    gate(cube, allowed_loop_break_vertices=[len(cube.data.vertices)])
except InvalidParamsError:
    pass
else:
    raise AssertionError("out-of-range deliberate loop break accepted")
sharp = cube.data.attributes.new("sharp_edge", "BOOLEAN", "EDGE")
for item in sharp.data:
    item.value = True
assert gate(cube)["pass"]

print("PASS asset quality synthetic headless checks (not production asset qualification)")
