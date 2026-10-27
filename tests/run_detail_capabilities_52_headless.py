"""Blender 5.2 smoke for v0.6.1 detail-preserving typed operations."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo import dispatch


def require_ok(tool, params):
    result = dispatch(tool, params)
    assert result["ok"], result
    return result["data"]


def run():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with TemporaryDirectory() as temporary:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(temporary) / "detail-capabilities.blend"))
        bpy.ops.mesh.primitive_cube_add()
        cube = bpy.context.object
        cube.name = "DetailCube"

        require_ok("geometry_nodes.create", {
            "object_name": cube.name, "modifier_name": "DetailNodes", "node_group_name": "Detail Group",
        })
        operations = [
            {"op": "add_node", "node_type": kind, "name": name}
            for kind, name in (
                ("GeometryNodeRepeatInput", "Repeat In"),
                ("GeometryNodeRepeatOutput", "Repeat Out"),
                ("GeometryNodeSimulationInput", "Simulation In"),
                ("GeometryNodeSimulationOutput", "Simulation Out"),
            )
        ] + [
            {"op": "pair_zone", "input_node": "Repeat In", "output_node": "Repeat Out"},
            {"op": "pair_zone", "input_node": "Simulation In", "output_node": "Simulation Out"},
        ]
        plan = require_ok("geometry_nodes.patch", {
            "object_name": cube.name, "modifier_name": "DetailNodes", "operations": operations,
        })
        assert plan["dry_run"] and len(plan["journal"]) == 6, plan
        patched = require_ok("geometry_nodes.patch", {
            "object_name": cube.name, "modifier_name": "DetailNodes", "operations": operations,
            "dry_run": False,
        })
        assert patched["changed"], patched
        group = cube.modifiers["DetailNodes"].node_group
        assert group.nodes["Repeat In"].paired_output == group.nodes["Repeat Out"]
        assert group.nodes["Simulation In"].paired_output == group.nodes["Simulation Out"]

        # Use a separate object so the unlinked zone nodes cannot affect the
        # evaluated conversion's expected Bevel topology.
        bpy.ops.mesh.primitive_cube_add(location=(4, 0, 0))
        source = bpy.context.object
        source.name = "BevelSource"
        bevel = require_ok("modifier.add", {
            "object_name": source.name, "type": "BEVEL", "name": "DetailBevel",
            "settings": {"width": 0.15, "segments": 2, "harden_normals": True},
        })
        assert bevel["changed"] and source.modifiers["DetailBevel"].harden_normals is True
        convert_plan = require_ok("object.convert_evaluated", {
            "object_name": source.name, "name": "EvaluatedBevel",
        })
        assert convert_plan["dry_run"] and bpy.data.objects.get("EvaluatedBevel") is None
        converted = require_ok("object.convert_evaluated", {
            "object_name": source.name, "name": "EvaluatedBevel", "dry_run": False,
        })
        assert converted["changed"] and bpy.data.objects.get(source.name) is source
        assert len(bpy.data.objects["EvaluatedBevel"].data.vertices) > len(source.data.vertices)

        original = tuple(float(v) for v in cube.data.vertices[0].co)
        scaled = require_ok("mesh.edit", {
            "object_name": cube.name, "dry_run": False,
            "operations": [{
                "op": "transform",
                "selection": {"domain": "VERT", "predicate": {"kind": "INDICES", "indices": [0]}},
                "scale": [2.0, 1.0, 1.0], "pivot_point": [0.0, 0.0, 0.0],
            }],
        })
        assert scaled["changed"]
        assert abs(cube.data.vertices[0].co.x - 2 * original[0]) < 1e-6
        matrix = [
            [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 2], [0, 0, 0, 1],
        ]
        moved = require_ok("mesh.edit", {
            "object_name": cube.name, "dry_run": False,
            "operations": [{
                "op": "transform",
                "selection": {"domain": "VERT", "predicate": {"kind": "INDICES", "indices": [0]}},
                "matrix": matrix,
            }],
        })
        assert moved["changed"] and abs(cube.data.vertices[0].co.z - (original[2] + 2)) < 1e-6
        marked = require_ok("mesh.edit", {
            "object_name": cube.name, "dry_run": False,
            "operations": [{
                "op": "mark_edges",
                "selection": {"domain": "EDGE", "predicate": {"kind": "INDICES", "indices": [0]}},
                "seam": True, "sharp": True, "crease": 0.5, "bevel_weight": 0.25,
            }],
        })
        assert marked["changed"] and cube.data.edges[0].use_seam and cube.data.edges[0].use_edge_sharp
        assert abs(cube.data.attributes["crease_edge"].data[0].value - 0.5) < 1e-6
        assert abs(cube.data.attributes["bevel_weight_edge"].data[0].value - 0.25) < 1e-6
    print("DETAIL_CAPABILITIES_52_HEADLESS_OK")


if __name__ == "__main__":
    run()
