"""Blender background qualification for reusable hard-surface workflow templates."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))

for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo import dispatch
from blenderboo_mcp.workflow import run_workflow
from blenderboo_mcp.workflow_templates import expand_template


def require_ok(result: dict, label: str) -> dict:
    json.dumps(result, allow_nan=False)
    if not result["ok"]:
        raise AssertionError(f"{label} failed:\n{json.dumps(result, indent=2)}")
    print(f"PASS {label}")
    return result["data"]


def cube_object(
    name: str,
    location: tuple[float, float, float],
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(location=location, scale=scale)
    obj = bpy.context.object
    obj.name = name
    return obj


def mesh_state(obj: bpy.types.Object) -> str:
    return require_ok(
        dispatch("mesh.inspect", {"object_name": obj.name}),
        f"mesh.inspect {obj.name}",
    )["state_token"]


def execute_template(name: str, parameters: dict) -> dict:
    expansion = expand_template(name, parameters)
    options = expansion["run_options"]
    return run_workflow(
        lambda tool, arguments, instance: dispatch(tool, arguments),
        steps=expansion["steps"],
        execute=True,
        rollback_on_failure=options.get("rollback_on_failure", False),
        confirm_rollback=options.get("confirm_rollback"),
        instance=None,
    )


artifact_root = ROOT / "tests" / ".tmp_hard_surface_workflows_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "hard_surface_workflows_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

panel = cube_object("WorkflowPanel", (0.0, 0.0, 0.0))
panel_counts = (len(panel.data.vertices), len(panel.data.edges), len(panel.data.polygons))
panel_dry_run = require_ok(
    execute_template(
        "panel_inset_v1",
        {
            "object_name": panel.name,
            "face_indices": [0],
            "expected_state": mesh_state(panel),
            "thickness": 0.15,
            "depth": -0.05,
        },
    ),
    "panel inset workflow dry run",
)
assert panel_dry_run["steps"][0]["result"]["data"]["dry_run"] is True
assert (len(panel.data.vertices), len(panel.data.edges), len(panel.data.polygons)) == panel_counts

panel_apply = require_ok(
    execute_template(
        "panel_inset_v1",
        {
            "object_name": panel.name,
            "face_indices": [0],
            "expected_state": mesh_state(panel),
            "thickness": 0.15,
            "depth": -0.05,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "panel inset workflow apply",
)
assert panel_apply["steps"][0]["status"] == "completed"
assert len(panel.data.polygons) > panel_counts[2]

support = cube_object("WorkflowSupport", (4.0, 0.0, 0.0))
support_counts = (len(support.data.vertices), len(support.data.edges), len(support.data.polygons))
require_ok(
    execute_template(
        "support_loop_pair_v1",
        {
            "object_name": support.name,
            "edge_indices": [0],
            "expected_state": mesh_state(support),
            "offset": 0.1,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "support loop pair workflow apply",
)
assert (len(support.data.vertices), len(support.data.edges), len(support.data.polygons)) != support_counts

opening = cube_object("WorkflowOpening", (8.0, 0.0, 0.0))
cutter = cube_object(
    "WorkflowOpeningCutter",
    (8.75, 0.0, 0.0),
    scale=(0.5, 0.75, 0.75),
)
require_ok(
    execute_template(
        "boolean_opening_v1",
        {
            "object_name": opening.name,
            "cutter_object": cutter.name,
            "expected_state": mesh_state(opening),
            "modifier_name": "WorkflowBoolean",
        },
    ),
    "boolean opening workflow",
)
assert opening.modifiers["WorkflowBoolean"].type == "BOOLEAN"
assert opening.modifiers["WorkflowBoolean"].object is cutter

repeat = cube_object("WorkflowRepeat", (12.0, 0.0, 0.0))
require_ok(
    execute_template(
        "mirror_array_v1",
        {
            "object_name": repeat.name,
            "expected_state": mesh_state(repeat),
            "mirror_name": "WorkflowMirror",
            "array_name": "WorkflowArray",
            "use_axis": [True, False, False],
            "count": 3,
            "relative_offset": [1.5, 0.0, 0.0],
        },
    ),
    "mirror array workflow",
)
assert [modifier.name for modifier in repeat.modifiers] == ["WorkflowMirror", "WorkflowArray"]
assert repeat.modifiers["WorkflowArray"].count == 3

rollback = cube_object("WorkflowRepeatRollback", (16.0, 0.0, 0.0))
rollback_expansion = expand_template(
    "mirror_array_v1",
    {
        "object_name": rollback.name,
        "expected_state": mesh_state(rollback),
        "mirror_name": "RollbackMirror",
        "array_name": "RollbackArray",
    },
)


def fail_array(tool: str, arguments: dict, instance: object) -> dict:
    if tool == "modifier.add" and arguments.get("type") == "ARRAY":
        return {
            "ok": False,
            "tool": tool,
            "data": {"changed": False},
            "error": {"type": "InjectedFailure", "message": "forced array failure", "traceback": ""},
            "warnings": [],
        }
    return dispatch(tool, arguments)


rollback_result = run_workflow(
    fail_array,
    steps=rollback_expansion["steps"],
    execute=True,
    rollback_on_failure=True,
    confirm_rollback=True,
    instance=None,
)
assert rollback_result["ok"] is False
assert rollback_result["data"]["failed_step"] == "add_array"
assert rollback_result["data"]["rollback"]["complete"] is True
assert rollback_result["data"]["partial_change_possible"] is False
assert len(rollback.modifiers) == 0
print("PASS mirror array workflow compensation")

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("HARD_SURFACE_WORKFLOWS_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
