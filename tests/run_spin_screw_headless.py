from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith(
        "blenderboo."
    ):
        del sys.modules[module_name]

from blenderboo import dispatch


def require_ok(result: dict, label: str) -> dict:
    json.dumps(result, allow_nan=False)
    if not result["ok"]:
        raise AssertionError(f"{label} failed:\n{json.dumps(result, indent=2)}")
    print(f"PASS {label}")
    return result["data"]


def require_error(result: dict, error_type: str, label: str) -> dict:
    json.dumps(result, allow_nan=False)
    if result["ok"] or result["error"]["type"] != error_type:
        raise AssertionError(
            f"{label} expected {error_type}:\n{json.dumps(result, indent=2)}"
        )
    print(f"PASS {label}")
    return result["error"]


def edge_profile(name: str, first: tuple[float, float, float], second: tuple[float, float, float]):
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata([first, second], [(0, 1)], [])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


artifact_root = ROOT / "tests" / ".tmp_spin_screw_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "spin_screw_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

edge_selection = {
    "domain": "EDGE",
    "predicate": {"kind": "INDICES", "indices": [0]},
}

# A closed full revolution joins the seam but remains dry-run-first.
spin = edge_profile("SpinProfile", (1.0, 0.0, 0.0), (1.0, 0.0, 1.0))
spin_operation = {
    "op": "spin",
    "selection": edge_selection,
    "center": [0.0, 0.0, 0.0],
    "axis": [0.0, 0.0, 1.0],
    "angle": math.tau,
    "steps": 8,
    "use_merge": True,
}
spin_before = [tuple(vertex.co) for vertex in spin.data.vertices]
spin_plan = require_ok(
    dispatch(
        "mesh.edit",
        {"object_name": spin.name, "operations": [spin_operation], "max_elements": 100},
    ),
    "closed spin dry run",
)
assert spin_plan["changed"] is False
assert spin_plan["after"] == {
    "vertices": 16,
    "edges": 24,
    "faces": 8,
    "loops": 32,
}
assert spin_plan["journal"][0]["details"]["mode"] == "SPIN"
assert spin_plan["journal"][0]["details"]["projected_total_upper_bound"] == 83
assert [tuple(vertex.co) for vertex in spin.data.vertices] == spin_before

spin_apply = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": spin.name,
            "operations": [spin_operation],
            "max_elements": 100,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "closed spin apply",
)
assert spin_apply["changed"] is True
assert (len(spin.data.vertices), len(spin.data.edges), len(spin.data.polygons)) == (
    16,
    24,
    8,
)

# Public screw_offset is total travel; BMesh receives one quarter per step here.
screw = edge_profile("ScrewProfile", (1.0, 0.0, 0.0), (1.0, 0.0, 1.0))
screw_operation = {
    "op": "spin",
    "selection": edge_selection,
    "center": [0.0, 0.0, 0.0],
    "axis": [0.0, 0.0, 1.0],
    "angle": math.tau,
    "steps": 4,
    "screw_offset": 2.0,
}
screw_plan = require_ok(
    dispatch(
        "mesh.edit",
        {"object_name": screw.name, "operations": [screw_operation], "max_elements": 43},
    ),
    "bounded screw dry run",
)
screw_details = screw_plan["journal"][0]["details"]
assert screw_details["mode"] == "SCREW"
assert screw_details["per_step_translation"] == [0.0, 0.0, 0.5]
assert screw_details["projected_total_upper_bound"] == 43
require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": screw.name,
            "operations": [screw_operation],
            "max_elements": 43,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "bounded screw apply",
)
assert (len(screw.data.vertices), len(screw.data.edges), len(screw.data.polygons)) == (
    10,
    13,
    4,
)
assert math.isclose(max(float(vertex.co.z) for vertex in screw.data.vertices), 3.0)

# Duplicate mode creates bounded disconnected copies without bridge faces.
duplicate = edge_profile("DuplicateSpinProfile", (1.0, 0.0, 0.0), (1.0, 0.0, 1.0))
duplicate_apply = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": duplicate.name,
            "operations": [
                {
                    "op": "spin",
                    "selection": edge_selection,
                    "axis": [0.0, 0.0, 1.0],
                    "angle": math.pi,
                    "steps": 3,
                    "use_duplicate": True,
                }
            ],
            "max_elements": 12,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "duplicate spin apply",
)
assert duplicate_apply["journal"][0]["details"]["use_duplicate"] is True
assert (len(duplicate.data.vertices), len(duplicate.data.edges), len(duplicate.data.polygons)) == (
    8,
    4,
    0,
)

# A profile touching the axis is welded into a non-degenerate partial fan.
fan = edge_profile("AxisFanProfile", (0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
fan_apply = require_ok(
    dispatch(
        "mesh.edit",
        {
            "object_name": fan.name,
            "operations": [
                {
                    "op": "spin",
                    "selection": edge_selection,
                    "axis": [0.0, 0.0, 1.0],
                    "angle": math.pi,
                    "steps": 4,
                }
            ],
            "max_elements": 43,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "axis-welded partial spin",
)
assert fan_apply["journal"][0]["details"]["axis_welded_vertices"] == 4
axis_vertices = [vertex for vertex in fan.data.vertices if vertex.co.length <= 1e-8]
assert len(axis_vertices) == 1
assert len(fan.data.polygons) == 4
assert all(float(face.area) > 1e-8 for face in fan.data.polygons)

# Workload and incompatible seam-closing requests reject before writeback.
budgeted = edge_profile("BudgetProfile", (1.0, 0.0, 0.0), (1.0, 0.0, 1.0))
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": budgeted.name,
            "operations": [{**screw_operation, "screw_offset": 0.0}],
            "max_elements": 42,
        },
    ),
    "InvalidStateError",
    "spin projected workload rejection",
)
assert (len(budgeted.data.vertices), len(budgeted.data.edges)) == (2, 1)
require_error(
    dispatch(
        "mesh.edit",
        {
            "object_name": budgeted.name,
            "operations": [
                {**screw_operation, "screw_offset": 1.0, "use_merge": True}
            ],
        },
    ),
    "InvalidParamsError",
    "screw seam merge rejection",
)
assert (len(budgeted.data.vertices), len(budgeted.data.edges)) == (2, 1)

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("SPIN_SCREW_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
