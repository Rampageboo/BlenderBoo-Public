"""Blender 5.2 live smoke for session-friction fixes."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo import dispatch


OUTPUT = ROOT / "tests" / ".tmp_session_friction_headless"
if OUTPUT.exists():
    shutil.rmtree(OUTPUT)
OUTPUT.mkdir(parents=True)


def ok(tool: str, params: dict) -> dict:
    result = dispatch(tool, params)
    assert result["ok"], result
    return result["data"]


# Build a native source .blend containing a named collection.
bpy.ops.wm.read_factory_settings(use_empty=True)
source_collection = bpy.data.collections.new("Native_Source")
bpy.context.scene.collection.children.link(source_collection)
source_mesh = bpy.data.meshes.new("NativeMesh")
source_object = bpy.data.objects.new("NativeObject", source_mesh)
source_collection.objects.link(source_object)
source_path = OUTPUT / "native-source.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(source_path), check_existing=False)


# Create and save the target document before exercising in-place save.
bpy.ops.wm.read_factory_settings(use_empty=True)
target_path = OUTPUT / "target.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(target_path), check_existing=False)

batch = bpy.data.collections.new("Batch")
bpy.context.scene.collection.children.link(batch)
controller = bpy.data.objects.new("Controller", None)
bpy.context.scene.collection.objects.link(controller)
members = []
for index in range(3):
    obj = bpy.data.objects.new(f"Member_{index}", None)
    batch.objects.link(obj)
    members.append(obj)

# Mode control is explicit and reversible by a second call.
active_mesh = bpy.data.meshes.new("ModeMesh")
active = bpy.data.objects.new("ModeObject", active_mesh)
bpy.context.scene.collection.objects.link(active)
bpy.context.view_layer.objects.active = active
active.select_set(True)
assert ok("object.mode_set", {"mode": "EDIT"})["dry_run"]
assert ok("object.mode_set", {"mode": "EDIT", "dry_run": False})["after_mode"].startswith("EDIT")
assert ok("object.mode_set", {"mode": "OBJECT", "dry_run": False})["after_mode"] == "OBJECT"

# One call parents the complete collection and one call moves the logical set.
parent_plan = ok("collection.parent_objects", {"collection": "Batch", "parent": "Controller"})
assert parent_plan["object_count"] == 3 and parent_plan["changed_count"] == 3
ok("collection.parent_objects", {"collection": "Batch", "parent": "Controller", "dry_run": False})
assert all(obj.parent is controller for obj in members)
ok("collection.transform", {"collection": "Batch", "translation": [10, 0, 0], "dry_run": False})
assert all(abs(obj.matrix_world.translation.x - 10.0) < 1e-6 for obj in members)

# Native append preserves the named Blender collection rather than flattening through FBX.
blend_plan = ok("interchange.blend_import", {"filepath": str(source_path), "names": ["Native_Source"]})
assert blend_plan["data_type"] == "COLLECTION" and blend_plan["dry_run"]
blend_result = ok("interchange.blend_import", {"filepath": str(source_path), "names": ["Native_Source"], "dry_run": False, "confirm": True})
assert "Native_Source" in blend_result["loaded_names"]
assert bpy.data.collections.get("Native_Source") is not None
duplicate = dispatch("interchange.blend_import", {"filepath": str(source_path), "names": ["Native_Source"]})
assert not duplicate["ok"] and duplicate["error"]["type"] == "NameConflictError", duplicate

# Vertically disjoint openings at the same offset are valid in one tall wall.
wall = ok("architecture.wall_build", {
    "name": "Stacked", "height": 6, "thickness": 0.2,
    "wall_lines": [{"start": [0, 0], "end": [5, 0], "openings": [
        {"id": "Lower", "offset": 1, "width": 2, "bottom": 0, "height": 2},
        {"id": "Upper", "offset": 1, "width": 2, "bottom": 3, "height": 2},
    ]}],
})
assert wall["opening_count"] == 2
ok("architecture.wall_build", {
    "name": "Stacked", "height": 6, "thickness": 0.2, "dry_run": False,
    "wall_lines": [{"start": [0, 0], "end": [5, 0], "openings": [
        {"id": "Lower", "offset": 1, "width": 2, "bottom": 0, "height": 2},
        {"id": "Upper", "offset": 1, "width": 2, "bottom": 3, "height": 2},
    ]}],
})

# In-place save is confirmation-gated and returns independently observed disk state.
save_plan = ok("scene.save_current", {})
assert save_plan["dry_run"] and save_plan["before"]["disk_exists"]
saved = ok("scene.save_current", {"dry_run": False, "confirm": True})
assert saved["write_confirmed"] and saved["after"]["disk_exists"]
assert saved["after"]["filepath"] == str(target_path.resolve())

print("session friction headless smoke passed")
