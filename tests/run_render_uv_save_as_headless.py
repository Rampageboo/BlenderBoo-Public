"""Blender 5.2 smoke for render quality, first save-as, and closed-mesh UV unwrap."""

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
    assert not bpy.data.filepath
    with TemporaryDirectory() as temporary:
        target = Path(temporary) / "first-save.blend"
        plan = require_ok("scene.save_as", {"path": str(target)})
        assert plan["dry_run"] and not target.exists(), plan
        saved = require_ok("scene.save_as", {"path": str(target), "dry_run": False})
        assert saved["write_confirmed"] and saved["changed"] and target.is_file(), saved
        assert Path(bpy.data.filepath) == target, saved

        current = require_ok("render.settings", {})
        assert "view_transform" in current and "exposure" in current and "gamma" in current, current
        settings = {"film_transparent": True, "exposure": 0.75, "gamma": 1.1}
        if "cycles_samples" in current:
            settings["cycles_samples"] = 16
        applied = require_ok("render.settings", settings)
        assert applied["changed"], applied
        readback = require_ok("render.settings", {})
        assert readback["film_transparent"] is True
        assert abs(readback["exposure"] - 0.75) < 1e-6
        assert abs(readback["gamma"] - 1.1) < 1e-6
        if "cycles_samples" in settings:
            assert readback["cycles_samples"] == 16

        bpy.ops.mesh.primitive_cube_add()
        target_obj = bpy.context.object
        target_obj.name = "UVTarget"
        bpy.ops.mesh.primitive_cube_add(location=(4, 0, 0))
        sentinel = bpy.context.object
        sentinel.name = "SelectionSentinel"
        before_selected = {obj.name for obj in bpy.context.selected_objects}
        assert before_selected == {sentinel.name}
        preview = require_ok("uv.unwrap", {
            "object_name": target_obj.name, "layer_name": "Atlas", "create_layer": True,
            "method": "SMART", "dry_run": True,
        })
        assert not preview["changed"], preview
        loops_before = len(target_obj.data.loops)
        attempted = dispatch("uv.unwrap", {
            "object_name": target_obj.name, "layer_name": "Atlas", "create_layer": True,
            "method": "SMART", "dry_run": False,
        })
        print(f"UV_SMART_COUNTS before_loops={loops_before} after_loops={len(target_obj.data.loops)} "
              f"after_layer_items={len(target_obj.data.uv_layers['Atlas'].data)} mode={bpy.context.mode}")
        assert attempted["ok"], attempted
        unwrapped = attempted["data"]
        assert unwrapped["changed"] and unwrapped["uv_quality"]["zero_area_faces"] == 0, unwrapped
        assert unwrapped["uv_quality"]["checked_faces"] == len(target_obj.data.polygons)
        assert bpy.context.mode == "OBJECT"
        assert bpy.context.view_layer.objects.active is sentinel
        assert {obj.name for obj in bpy.context.selected_objects} == before_selected
        assert target_obj.data.uv_layers.get("Atlas") is not None
    print("RENDER_UV_SAVE_AS_HEADLESS_OK")


if __name__ == "__main__":
    run()
