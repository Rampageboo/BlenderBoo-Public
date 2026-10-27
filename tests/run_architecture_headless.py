"""Blender background qualification for architecture.wall_build."""

from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("CBT_TEST_INSTALLED_ADDON") != "1":
    sys.path.insert(0, str(ROOT / "addon"))

for module_name in list(sys.modules):
    if module_name == "blenderboo" or module_name.startswith("blenderboo."):
        del sys.modules[module_name]

from blenderboo import dispatch


def require_ok(result: dict, label: str) -> dict:
    json.dumps(result, allow_nan=False)
    if not result["ok"]:
        raise AssertionError(f"{label} failed:\n{json.dumps(result, indent=2)}")
    print(f"PASS {label}")
    return result["data"]


artifact_root = ROOT / "tests" / ".tmp_architecture_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "architecture_wall_build_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

state = require_ok(dispatch("scene.inspect", {}), "initial scene inspect")["state_token"]
request = {
    "name": "Floor_A_Walls",
    "wall_lines": [
        {
            "start": [0, 0],
            "end": [8, 0],
            "openings": [
                {"id": "Door_A", "kind": "DOOR", "offset": 1, "width": 1, "height": 2.1},
                {"id": "Window_A", "kind": "WINDOW", "offset": 4, "width": 2, "bottom": 1, "height": 1.2},
            ],
        },
        {"start": [8, 0], "end": [8, 5]},
    ],
    "height": 3,
    "thickness": 0.2,
    "expected_state": state,
}

planned = require_ok(dispatch("architecture.wall_build", request), "wall build dry run")
assert planned["dry_run"] is True
assert planned["changed"] is False
assert planned["wall_count"] == 2
assert planned["opening_count"] == 2
assert bpy.data.objects.get("Floor_A_Walls_001") is None

applied = require_ok(
    dispatch("architecture.wall_build", {**request, "dry_run": False}),
    "wall build apply",
)
assert applied["changed"] is True
assert applied["objects"] == ["Floor_A_Walls_001", "Floor_A_Walls_002"]

for object_name in applied["objects"]:
    obj = bpy.data.objects[object_name]
    assert obj.type == "MESH"
    assert obj["cbt_architecture_kind"] == "WALL"
    assert obj.data.validate(verbose=True, clean_customdata=False) is False
    edge_uses = Counter()
    for polygon in obj.data.polygons:
        vertices = list(polygon.vertices)
        for left, right in zip(vertices, vertices[1:] + vertices[:1]):
            edge_uses[tuple(sorted((int(left), int(right))))] += 1
    assert edge_uses and set(edge_uses.values()) == {2}

first = bpy.data.objects["Floor_A_Walls_001"]
front_back_centers = [
    polygon.center.copy()
    for polygon in first.data.polygons
    if abs(abs(polygon.center.y) - 0.1) < 1e-6
]
assert not any(1 < center.x < 2 and 0 < center.z < 2.1 for center in front_back_centers)
assert not any(4 < center.x < 6 and 1 < center.z < 2.2 for center in front_back_centers)

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
print("ARCHITECTURE_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
