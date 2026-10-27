"""Headless default python.exec overhead on large-mesh and many-object scenes."""

import json
from pathlib import Path
import statistics
import sys

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo import dispatch


def measure(label):
    samples = []
    for _ in range(20):
        response = dispatch("python.exec", {"code": "result = 1", "diff": "summary"})
        assert response["ok"], response
        samples.append(response["meta"]["timings_ms"]["exec_overhead"])
    ranked = sorted(samples)
    return {"scene": label, "p95_ms": ranked[18], "median_ms": statistics.median(samples),
            "samples_ms": samples, "budget_met": ranked[18] <= 200}


def objects(count):
    template = bpy.data.meshes.new("SmallMeshTemplate")
    template.vertices.add(500)
    for index in range(count):
        mesh = template.copy()
        obj = bpy.data.objects.new(f"Object{index:05d}", mesh)
        bpy.context.scene.collection.objects.link(obj)
    bpy.data.meshes.remove(template)


def run():
    rows = []
    for count in (1000, 10000):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        objects(count)
        rows.append(measure(f"{count}_objects"))

    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh = bpy.data.meshes.new("LargeMesh")
    mesh.vertices.add(1_500_000)
    coordinates = np.zeros(4_500_000, dtype=np.float32)
    mesh.vertices.foreach_set("co", coordinates)
    obj = bpy.data.objects.new("LargeObject", mesh)
    bpy.context.scene.collection.objects.link(obj)
    for index in range(200):
        mat = bpy.data.materials.new(f"Material{index:03d}")
        mat.use_nodes = True
        part_mesh = bpy.data.meshes.new(f"PartMesh{index:03d}")
        part_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        part_mesh.materials.append(mat)
        part = bpy.data.objects.new(f"Part{index:03d}", part_mesh)
        bpy.context.scene.collection.objects.link(part)
    rows.append(measure("1_5m_vertices_200_objects_materials"))

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    rows.append(measure("edit_mode_large_mesh"))
    print("PYTHON_EXEC_SNAPSHOT_BENCHMARK=" + json.dumps(rows, sort_keys=True))
    assert all(row["budget_met"] for row in rows), rows


if __name__ == "__main__":
    run()
