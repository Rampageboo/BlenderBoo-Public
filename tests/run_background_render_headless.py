"""Blender 5.2 child-process qualification for background snapshots and cancellation.

Run with: blender -b --factory-startup --python tests/run_background_render_headless.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

import bpy

from blenderboo.tools.background_render import BackgroundRenderManager


def wait_for(manager: BackgroundRenderManager, operation_id: str, limit_s: float = 90) -> dict:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        manager.pump()
        data = manager.status(operation_id)
        if data["status"] in {"succeeded", "failed", "cancelled"}:
            return data
        time.sleep(0.1)
    raise AssertionError(f"Background job did not finish in {limit_s}s: {manager.status(operation_id)}")


def make_unsaved_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_cube_add(size=2)
    cube = bpy.context.object
    cube.name = "UnsavedRedCube"
    material = bpy.data.materials.new("UnsavedRedEmission")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (1, 0, 0, 1)
    emission.inputs["Strength"].default_value = 2
    output = nodes.new("ShaderNodeOutputMaterial")
    material.node_tree.links.new(emission.outputs[0], output.inputs["Surface"])
    cube.data.materials.append(material)
    camera_data = bpy.data.cameras.new("BackgroundCamera")
    camera = bpy.data.objects.new("BackgroundCamera", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (0, -6, 0)
    camera.rotation_euler = (1.5707963267948966, 0, 0)
    bpy.context.scene.camera = camera
    bpy.context.scene.render.engine = "CYCLES"
    bpy.context.scene.cycles.samples = 4
    assert bpy.data.filepath == "", "Headless qualification expects a never-saved document"


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    make_unsaved_scene()
    dirty_before_snapshot = bool(bpy.data.is_dirty)
    manager = BackgroundRenderManager(temp_root=root / "jobs")
    from blenderboo.errors import InvalidParamsError

    for invalid in (
        {"mode": "detail", "options": {"object_name": "UnsavedRedCube", "direction": ["bad", "0", "1"]}},
        {"mode": "detail", "options": {"object_name": "MissingObject"}},
        {"mode": "reference_match", "options": {"reference_path": str(root / "missing.png")}},
    ):
        try:
            manager.start(invalid)
        except InvalidParamsError:
            pass
        else:
            raise AssertionError(f"Invalid background options were queued: {invalid}")
    assert not manager._jobs and not manager._queue

    valid_direction = manager.start({"mode": "detail", "options": {"object_name": "UnsavedRedCube",
        "direction": ["0", "-1", "0"], "width": 32, "height": 32,
        "output_path": str(root / "detail-direction.png")}}).data["operation_id"]
    assert manager._jobs[valid_direction].options["direction"] == [0.0, -1.0, 0.0]
    assert wait_for(manager, valid_direction)["status"] == "succeeded"

    output = root / "snapshot.png"
    started = manager.start({"mode": "preview", "engine": "CYCLES", "samples": 4,
                             "options": {"width": 64, "height": 64, "output_path": str(output)}}).data
    operation_id = started["operation_id"]
    completed = wait_for(manager, operation_id)
    assert completed["status"] == "succeeded", completed
    assert completed["snapshot_dirty"] is dirty_before_snapshot, completed
    assert bpy.data.filepath == "", "Saving the snapshot switched the live document"
    assert output.is_file(), completed
    image = bpy.data.images.load(str(output), check_existing=False)
    try:
        assert tuple(image.size) == (64, 64), image.size
        center = (32 * 64 + 32) * 4
        red, green, blue = tuple(image.pixels[center:center + 3])
        assert red > green * 2 and red > blue * 2 and red > 0.1, (red, green, blue)
        red_center = (red, green, blue)
    finally:
        bpy.data.images.remove(image)

    live_path = root / "live-before-edit.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(live_path), check_existing=False)
    emission = bpy.data.materials["UnsavedRedEmission"].node_tree.nodes.get("Emission")
    assert emission is not None
    emission.inputs["Color"].default_value = (0, 1, 0, 1)
    edited_dirty_flag = bool(bpy.data.is_dirty)
    edited_output = root / "edited-snapshot.png"
    edited_id = manager.start({"mode": "preview", "engine": "CYCLES", "samples": 4,
                               "options": {"width": 64, "height": 64,
                                           "output_path": str(edited_output)}}).data["operation_id"]
    edited = wait_for(manager, edited_id)
    assert edited["status"] == "succeeded", edited
    assert edited["snapshot_dirty"] is edited_dirty_flag, edited
    image = bpy.data.images.load(str(edited_output), check_existing=False)
    try:
        center = (32 * 64 + 32) * 4
        red, green, blue = tuple(image.pixels[center:center + 3])
        assert green > red + 0.2 and green > blue + 0.2, (red, green, blue)
        assert green > red_center[1] + 0.1 and red < red_center[0] - 0.1, (red_center, (red, green, blue))
    finally:
        bpy.data.images.remove(image)

    cancel_output = root / "cancel.png"
    pending = manager.start({"mode": "preview", "engine": "CYCLES", "samples": 100_000,
                             "options": {"width": 512, "height": 512, "output_path": str(cancel_output)}}).data
    manager.pump()
    cancel_status = manager.cancel(pending["operation_id"])
    assert cancel_status["cancel_requested"] is True, cancel_status
    cancelled = wait_for(manager, pending["operation_id"], limit_s=20)
    assert cancelled["status"] == "cancelled", cancelled
    child = manager._jobs[pending["operation_id"]].process
    assert child is not None and child.poll() is not None, "Cancelled Blender child is still running"
    print(json.dumps({"background_render": completed["status"],
                      "cancel": cancelled["status"], "dimensions": [64, 64],
                      "snapshot_dirty": completed["snapshot_dirty"],
                      "edited_snapshot_dirty": edited["snapshot_dirty"],
                      "source_unsaved": True}))
