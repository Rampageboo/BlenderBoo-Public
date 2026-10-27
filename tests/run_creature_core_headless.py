"""Blender headless synthetic source/graph/base/fit regression."""

from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo.tools.creature import graph_set, graph_inspect, graph_suggest, landmark_set, base_from_graph, wrap_to_target
from blenderboo.tools.mesh_topology import topology_report
from blenderboo.tools.source_intake import inspect, prepare

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=0.3, location=(0, 0, 0))
holder = bpy.context.object
holder.name = "GraphHolder"
graph = {"body_plan": "custom", "joints": [
    {"name": "root", "parent": None, "position": [0, 0, 0], "radius": 0.24},
    {"name": "spine", "parent": "root", "position": [0, 0, 1], "radius": 0.22,
     "loops_along": 3, "joint_extra_loops": 2},
    {"name": "neck", "parent": "spine", "position": [0, 0, 1.6], "radius": 0.14},
    {"name": "head", "parent": "neck", "position": [0, 0, 1.9], "radius": 0.18},
]}
set_result = graph_set({"object_name": holder.name, "graph": graph}).data
assert set_result["joints"] == 4
assert graph_inspect({"object_name": holder.name}).data["graph_hash"] == set_result["graph_hash"]
base = base_from_graph({"graph_object": holder.name, "output_name": "CreatureBase"}).data
assert base["topology"]["faces"] > 0
base_object = bpy.data.objects[base["object"]]
source = base_object.copy()
source.data = base_object.data.copy()
source.name = "SyntheticHighPoly"
bpy.context.scene.collection.objects.link(source)
subd = source.modifiers.new("Subdivision", "SUBSURF")
subd.levels = 2
for selected in bpy.context.selected_objects:
    selected.select_set(False)
source.select_set(True)
bpy.context.view_layer.objects.active = source
bpy.ops.object.modifier_apply(modifier=subd.name)
for vertex in source.data.vertices:
    vertex.co.x *= 1.05
    vertex.co.y *= 0.95
before_source = [tuple(v.co) for v in source.data.vertices]
source_report = inspect({"object_name": source.name}).data
assert source_report["triangles"] > 0
proposal = graph_suggest({"body_plan": "humanoid", "proxy_name": source.name}).data
assert proposal["applied"] is False and len(proposal["proposal"]["joints"]) > 5
proxy = prepare({"object_name": source.name, "voxel_size": 0.08,
                 "min_island_vertices": 4, "output_name": "ProjectionProxy"}).data
assert proxy["proxy"] != source.name
fit = wrap_to_target({"object_name": base_object.name, "target_name": proxy["proxy"],
                      "iterations": 6}).data
assert fit["topology_proof"]["unchanged"]
assert fit["object"] not in {base_object.name, source.name, proxy["proxy"]}
assert [tuple(v.co) for v in source.data.vertices] == before_source
topology = topology_report({"object_name": fit["object"], "triangle_budget": 100000}).data
assert topology["connectivity_hash"] == fit["topology_proof"]["before"]
assert topology["loop_traces"]
symmetry_report = topology_report({"object_name": fit["object"], "symmetry_plane_x": 0}).data
assert symmetry_report["symmetry"]["max_asymmetry_m"] is not None
landmark = landmark_set({"name": "pelvis", "position": [0, 0, 0] }).data
assert bpy.data.objects[landmark["object"]]["cbt_landmark"] == "pelvis"
proposal = graph_suggest({"body_plan": "humanoid", "proxy_name": proxy["proxy"]}).data
assert next(j for j in proposal["proposal"]["joints"] if j["name"] == "pelvis")["evidence"] == "landmark"
sym_proxy = prepare({"object_name": source.name, "voxel_size": 0.12,
                     "symmetrize": True, "symmetry_plane_x": 0,
                     "output_name": "SymmetryProxy"}).data
assert sym_proxy["symmetrized"] is True
branch_graph = graph_set({"object_name": holder.name, "graph": proposal["proposal"]}).data
assert branch_graph["joints"] >= 10
branch_base = base_from_graph({"graph_object": holder.name, "output_name": "HumanoidBranchBase"}).data
assert branch_base["generation"] == "skin_branch_uniform_circumference"
assert branch_base["topology"]["quads"] > branch_base["topology"]["triangle_faces"]
print("PASS creature graph, base, source proxy, fixed-topology fit")
