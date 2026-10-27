from __future__ import annotations

import json
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


def add_stroke(frame, coordinates):
    stroke = frame.strokes.new()
    stroke.display_mode = "3DSPACE"
    stroke.points.add(count=len(coordinates))
    for point, coordinate in zip(stroke.points, coordinates):
        point.co = coordinate
    return stroke


artifact_root = ROOT / "tests" / ".tmp_annotation_headless"
artifact_root.mkdir(parents=True, exist_ok=True)
artifact_dir = artifact_root / f"run-{os.getpid()}"
artifact_dir.mkdir()
os.environ["CBT_INSTANCE_REGISTRY"] = str(artifact_dir / "instances.json")
os.environ["CBT_CONFIG_PATH"] = str(artifact_dir / "config.toml")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene_path = artifact_dir / "annotation_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)

empty = require_ok(dispatch("annotation.inspect", {}), "empty annotation list")
assert empty["scope"] == "DATABLOCKS"
assert empty["page"]["total"] == 0

review = bpy.data.annotations.new("ReviewNotes")
issues = review.layers.new("Issues", set_active=True)
issues.color = (0.9, 0.2, 0.1)
issues.annotation_opacity = 0.75
issues.thickness = 5
frame_one = issues.frames.new(1)
add_stroke(frame_one, [(0.0, 0.0, 0.0)])
frame_twelve = issues.frames.new(12)
expected_points = [
    (1.0, 2.0, 3.0),
    (2.5, 2.0, 3.0),
    (3.0, 4.0, 5.0),
]
add_stroke(frame_twelve, expected_points)
add_stroke(frame_twelve, [(-1.0, 0.0, 0.5)])
review.layers.new("Guides", set_active=False)
bpy.data.annotations.new("OtherNotes")

bpy.ops.wm.save_as_mainfile(filepath=str(scene_path), check_existing=False)
assert bpy.data.is_dirty is False

first = require_ok(
    dispatch("annotation.inspect", {"limit": 1}), "annotation datablock first page"
)
assert first["scope"] == "DATABLOCKS"
assert first["page"]["total"] == 2
assert first["page"]["next_cursor"] == "1"
assert first["items"][0]["name"] == "OtherNotes"
state_token = first["state_token"]

second = require_ok(
    dispatch("annotation.inspect", {"limit": 1, "cursor": "1"}),
    "annotation datablock second page",
)
assert second["items"][0]["name"] == "ReviewNotes"
assert second["page"]["next_cursor"] is None

layers = require_ok(
    dispatch("annotation.inspect", {"annotation_name": "ReviewNotes"}),
    "annotation layers",
)
assert layers["scope"] == "LAYERS"
assert layers["annotation"]["layer_count"] == 2
assert {item["name"] for item in layers["items"]} == {"Issues", "Guides"}
issues_summary = next(item for item in layers["items"] if item["name"] == "Issues")
assert issues_summary["frame_count"] == 2
assert all(
    abs(actual - expected) < 1e-6
    for actual, expected in zip(issues_summary["color"], [0.9, 0.2, 0.1])
)
assert issues_summary["opacity"] == 0.75
assert issues_summary["thickness"] == 5

frames = require_ok(
    dispatch(
        "annotation.inspect",
        {"annotation_name": "ReviewNotes", "layer_name": "Issues"},
    ),
    "annotation frames",
)
assert frames["scope"] == "FRAMES"
assert {item["frame_number"] for item in frames["items"]} == {1, 12}

strokes = require_ok(
    dispatch(
        "annotation.inspect",
        {
            "annotation_name": "ReviewNotes",
            "layer_name": "Issues",
            "frame_number": 12,
        },
    ),
    "annotation strokes",
)
assert strokes["scope"] == "STROKES"
assert [item["point_count"] for item in strokes["items"]] == [3, 1]
assert all(item["display_mode"] == "3DSPACE" for item in strokes["items"])

points_first = require_ok(
    dispatch(
        "annotation.inspect",
        {
            "annotation_name": "ReviewNotes",
            "layer_name": "Issues",
            "frame_number": 12,
            "stroke_index": 0,
            "limit": 2,
        },
    ),
    "annotation points first page",
)
assert points_first["scope"] == "POINTS"
assert points_first["stroke"]["point_count"] == 3
assert points_first["page"]["next_cursor"] == "2"
assert [item["co"] for item in points_first["items"]] == [
    list(expected_points[0]),
    list(expected_points[1]),
]

points_final = require_ok(
    dispatch(
        "annotation.inspect",
        {
            "annotation_name": "ReviewNotes",
            "layer_name": "Issues",
            "frame_number": 12,
            "stroke_index": 0,
            "limit": 2,
            "cursor": points_first["page"]["next_cursor"],
        },
    ),
    "annotation points final page",
)
assert [item["co"] for item in points_final["items"]] == [list(expected_points[2])]
assert points_final["page"]["next_cursor"] is None

require_error(
    dispatch("annotation.inspect", {"annotation_name": "Missing"}),
    "NotFoundError",
    "missing annotation",
)
require_error(
    dispatch("annotation.inspect", {"layer_name": "Issues"}),
    "InvalidParamsError",
    "annotation hierarchy dependency",
)

assert bpy.data.is_dirty is False
assert len(frame_twelve.strokes) == 2
assert [tuple(point.co) for point in frame_twelve.strokes[0].points] == expected_points
final = require_ok(dispatch("annotation.inspect", {}), "final read-only inspection")
assert final["dirty"] is False
assert final["state_token"] == state_token

print("ANNOTATION_HEADLESS_OK")
print(f"ARTIFACT_DIR={artifact_dir.resolve()}")
