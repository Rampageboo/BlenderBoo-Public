"""Fast capture composition and deferred UI lifecycle without Blender."""

from array import array
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

import blenderboo.tools.scene as scene
from blenderboo.registry import ToolRegistry
from test_scene_screenshot import FakeFramebuffer, FakeOffscreen, ScreenshotGuardTests


class FastCaptureTests(unittest.TestCase):
    def tearDown(self):
        active = scene._ACTIVE_SCREENSHOT_JOB
        if active is not None:
            active.deferred.cancel()

    def test_sheet_pixel_layout_and_single_view_passthrough(self):
        red = array("f", [1, 0, 0, 1] * 4)
        blue = array("f", [0, 0, 1, 1] * 4)
        single, width, height = scene._compose_fast_capture([red], ["front"], 2, 2, 1)
        self.assertIs(single, red)
        self.assertEqual((width, height), (2, 2))
        sheet, width, height = scene._compose_fast_capture(
            [red, blue], ["front", "back"], 2, 2, 2
        )
        self.assertEqual((width, height), (4, 20))
        self.assertEqual(list(sheet[(18 * width) * 4:(18 * width) * 4 + 4]), [1, 0, 0, 1])
        self.assertEqual(list(sheet[(18 * width + 2) * 4:(18 * width + 2) * 4 + 4]), [0, 0, 1, 1])

    def test_sheet_label_uses_upright_glyph_rows(self):
        blank = array("f", [0, 0, 0, 1] * (12 * 8))
        sheet, width, _height = scene._compose_fast_capture(
            [blank, blank], ["F", "F"], 12, 8, 2
        )

        def white(x, y):
            return list(sheet[(y * width + x) * 4:(y * width + x) * 4 + 3]) == [1, 1, 1]

        # The F's top bar reaches x=4; its bottom row does not.
        self.assertTrue(white(4, 11))
        self.assertFalse(white(4, 5))
        self.assertTrue(white(16, 11))

    def test_two_view_draw_restores_shading_and_overlay(self):
        bpy, area, _region, timers, space_type, images = ScreenshotGuardTests._environment()
        space = area.spaces.active
        space.overlay = SimpleNamespace(show_overlays=True, show_wireframes=False)
        offscreens = []

        def make_offscreen(width, height):
            item = FakeOffscreen(width, height)
            offscreens.append(item)
            return item

        gpu = SimpleNamespace(
            types=SimpleNamespace(GPUOffScreen=make_offscreen),
            state=SimpleNamespace(active_framebuffer_get=Mock(return_value=FakeFramebuffer())),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ), patch("blenderboo.tools.scene._target_bounds", return_value=((0, 0, 0), (1, 1, 1), [])), patch(
            "blenderboo.tools.scene._detail_direction", return_value=(0, 0, 1)
        ), patch("blenderboo.tools.scene._fast_capture_matrix", return_value=("view", "projection", {})), patch.dict(
            sys.modules, {"gpu": gpu}
        ):
            registry = ToolRegistry()
            registry.register("scene.fast_capture", scene.fast_capture_scene)
            deferred = registry.dispatch("scene.fast_capture", {
                "point": [0, 0, 0], "radius": 1, "views": ["front", "back"],
                "width": 2, "height": 2, "shading": "solid",
                "overlays": False, "wireframe": True,
                "output_path": str(Path(directory) / "sheet.png"),
            }, context={scene.DEFERRED_CONTEXT_KEY: True})
            completed = []
            deferred.add_done_callback(completed.append)
            space_type.draw_once()
            timers.run_delay(0.0)
            self.assertEqual(space.shading.type, "MATERIAL")
            self.assertTrue(space.overlay.show_overlays)
            self.assertFalse(space.overlay.show_wireframes)
            self.assertTrue(completed[0]["ok"], completed[0])
            self.assertEqual(completed[0]["data"]["views"], ["front", "back"])
            self.assertEqual((completed[0]["data"]["width"], completed[0]["data"]["height"]), (4, 20))
            self.assertEqual(len(offscreens), 2)
            self.assertTrue(all(item.freed for item in offscreens))
            self.assertEqual(images.removed, images.created)
            self.assertEqual(space_type.handlers, [])


if __name__ == "__main__":
    unittest.main()
