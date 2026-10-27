"""Measure python.exec's observed snapshot/diff overhead in Blender 5.2.

The scene setup is outside the timed call. An over-budget result is reported,
not hidden by a weaker detector; coverage tests determine acceptance.
"""

import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo import dispatch


def measure(objects: int, vertices: int, budget_ms: int):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh = bpy.data.meshes.new("BenchmarkMesh")
    mesh.vertices.add(vertices)
    obj = bpy.data.objects.new("BenchmarkMeshObject", mesh)
    bpy.context.scene.collection.objects.link(obj)
    for index in range(objects - 1):
        bpy.context.scene.collection.objects.link(bpy.data.objects.new(f"Empty{index:05d}", None))
    bpy.context.view_layer.update()
    outcome = dispatch("python.exec", {"code": "result = 1"})
    assert outcome["ok"], outcome
    assert not outcome["data"]["changed"], outcome
    timings = outcome["meta"]["timings_ms"]
    observed_ms = timings["exec_overhead"]
    print(json.dumps({"objects": objects, "vertices": vertices, "budget_ms": budget_ms,
                      "exec_overhead_ms": observed_ms, "budget_met": observed_ms <= budget_ms,
                      "snapshot_before_ms": timings["snapshot_before"],
                      "snapshot_after_and_diff_ms": timings["snapshot_after_and_diff"],
                      "depsgraph_evaluation_ms": timings["depsgraph_evaluation"]}), flush=True)


measure(1000, 1_000_000, 30)
measure(10000, 5_000_000, 150)
