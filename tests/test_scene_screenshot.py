"""Pure-Python guards and deferred draw lifecycle tests for scene.screenshot."""

from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError, InvalidStateError, NotFoundError
from blenderboo.registry import (
    DEFERRED_CONTEXT_KEY,
    DeferredEnvelope,
    ToolRegistry,
)
import blenderboo.tools.scene as scene_module
from blenderboo.tools.scene import (
    _draw_view3d,
    _screenshot_view,
    _ubyte_rgba_to_pixels,
    screenshot_scene,
)


class Matrix(list):
    def copy(self):
        return Matrix([row[:] for row in self])


class FakeTimers:
    def __init__(self):
        self.callbacks = []

    def register(self, callback, *, first_interval=0.0, **_kwargs):
        self.callbacks.append((callback, first_interval))

    def is_registered(self, callback):
        return any(candidate is callback for candidate, _delay in self.callbacks)

    def unregister(self, callback):
        for index, (candidate, _delay) in enumerate(self.callbacks):
            if candidate is callback:
                self.callbacks.pop(index)
                return
        raise ValueError("timer is not registered")

    def run_delay(self, delay):
        for index, (callback, candidate_delay) in enumerate(self.callbacks):
            if candidate_delay == delay:
                self.callbacks.pop(index)
                return callback()
        raise AssertionError(f"No timer registered with delay {delay}")


class FakeSpaceView3DType:
    def __init__(self):
        self.handlers = []
        self.removed = []

    def draw_handler_add(self, callback, args, region_type, draw_type):
        handle = object()
        self.handlers.append((handle, callback, args, region_type, draw_type))
        return handle

    def draw_handler_remove(self, handle, region_type):
        self.removed.append((handle, region_type))
        self.handlers = [entry for entry in self.handlers if entry[0] is not handle]

    def draw_once(self):
        _handle, callback, args, _region_type, _draw_type = self.handlers[0]
        callback(*args)


class FakePixels:
    def foreach_set(self, pixels):
        self.values = list(pixels)


class FakeImage:
    def __init__(self):
        self.pixels = FakePixels()
        self.filepath_raw = ""
        self.file_format = ""

    def save(self):
        Path(self.filepath_raw).touch()


class FakeImages:
    def __init__(self):
        self.created = []
        self.removed = []

    def new(self, _name, **_kwargs):
        image = FakeImage()
        self.created.append(image)
        return image

    def remove(self, image):
        self.removed.append(image)


class FakeBind:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeBuffer:
    def __init__(self, values=None):
        self.values = bytearray(
            values
            if values is not None
            else [255, 0, 0, 255] * 2 + [0, 0, 255, 255] * 2
        )
        self.dimensions = (2, 2, 4)

    def __len__(self):
        return len(self.values) if isinstance(self.dimensions, int) else self.dimensions[0]

    def __iter__(self):
        return iter(self.values)


class FakeFramebuffer:
    def __init__(self, values=None):
        self.read_args = None
        self.buffer = FakeBuffer(values)

    def read_color(self, *args):
        self.read_args = args
        return self.buffer


class FakeOffscreen:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.draw_args = None
        self.draw_kwargs = None
        self.freed = False

    def bind(self):
        return FakeBind()

    def draw_view3d(self, *args, **kwargs):
        self.draw_args = args
        self.draw_kwargs = kwargs

    def free(self):
        self.freed = True


class ScreenshotGuardTests(unittest.TestCase):
    def tearDown(self) -> None:
        active = scene_module._ACTIVE_SCREENSHOT_JOB
        if active is not None:
            active.deferred.cancel()
        scene_module._ACTIVE_SCREENSHOT_JOB = None

    @staticmethod
    def _bpy_without_viewport():
        return SimpleNamespace(
            data=SimpleNamespace(filepath="C:/project/scene.blend"),
            context=SimpleNamespace(area=None, screen=SimpleNamespace(areas=[])),
        )

    @staticmethod
    def _environment():
        region = SimpleNamespace(type="WINDOW", width=2, height=2)
        rv3d = SimpleNamespace(
            view_matrix=Matrix([[1, 0, 0, 0]] * 4),
            window_matrix=Matrix([[1, 0, 0, 0]] * 4),
        )
        space = SimpleNamespace(region_3d=rv3d, shading=SimpleNamespace(type="MATERIAL"))
        area = SimpleNamespace(
            type="VIEW_3D",
            regions=[region],
            spaces=SimpleNamespace(active=space),
            tag_redraw=Mock(),
        )
        timers = FakeTimers()
        space_type = FakeSpaceView3DType()
        images = FakeImages()
        bpy = SimpleNamespace(
            data=SimpleNamespace(filepath="C:/project/scene.blend", images=images),
            context=SimpleNamespace(
                area=area,
                region=region,
                screen=SimpleNamespace(areas=[]),
                scene=SimpleNamespace(objects={}),
                view_layer=object(),
            ),
            app=SimpleNamespace(timers=timers),
            types=SimpleNamespace(SpaceView3D=space_type),
        )
        return bpy, area, region, timers, space_type, images

    @staticmethod
    def _dispatch(params):
        registry = ToolRegistry()
        registry.register("scene.screenshot", screenshot_scene)
        return registry.dispatch(
            "scene.screenshot",
            params,
            context={DEFERRED_CONTEXT_KEY: True, "client_id": "test-client"},
        )

    def test_unknown_params_are_rejected(self) -> None:
        with self.assertRaisesRegex(InvalidParamsError, "Unknown parameters"):
            screenshot_scene({"unexpected": True})

    def test_existing_output_requires_overwrite(self) -> None:
        bpy, _area, _region, _timers, _space_type, _images = self._environment()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "viewport.png"
            output.touch()
            with patch(
                "blenderboo.tools.scene.get_bpy", return_value=bpy
            ), self.assertRaisesRegex(InvalidParamsError, "overwrite is false"):
                screenshot_scene({"output_path": str(output)})

    def test_pixel_budget_is_enforced_before_capture(self) -> None:
        region = SimpleNamespace(type="WINDOW", width=4096, height=2160)
        area = SimpleNamespace(
            type="VIEW_3D", regions=[region], spaces=SimpleNamespace(active=object())
        )
        bpy = SimpleNamespace(
            data=SimpleNamespace(filepath="C:/project/scene.blend"),
            context=SimpleNamespace(area=area, screen=SimpleNamespace(areas=[])),
        )
        with patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ), self.assertRaisesRegex(InvalidParamsError, "default pixel budget"):
            screenshot_scene({})

    def test_no_viewport_is_a_clean_invalid_state(self) -> None:
        with patch(
            "blenderboo.tools.scene.get_bpy",
            return_value=self._bpy_without_viewport(),
        ), self.assertRaisesRegex(InvalidStateError, "no active 3D viewport available"):
            screenshot_scene({})

    def test_direct_gui_dispatch_requires_deferred_bridge(self) -> None:
        bpy, _area, _region, _timers, _space_type, _images = self._environment()
        with patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ), self.assertRaisesRegex(InvalidStateError, "bridge-managed deferred"):
            screenshot_scene({})

    def test_ubyte_readback_preserves_blender_pixel_order(self) -> None:
        pixels = _ubyte_rgba_to_pixels(
            bytearray(
                [
                    255, 0, 0, 255, 0, 255, 0, 255,
                    0, 0, 255, 255, 255, 255, 255, 255,
                ]
            ),
            2,
            2,
        )
        self.assertEqual(list(pixels[:4]), [1.0, 0.0, 0.0, 1.0])
        self.assertEqual(list(pixels[-4:]), [1.0, 1.0, 1.0, 1.0])

    def test_gpu_buffer_is_flattened_through_dimensions_api(self) -> None:
        buffer = FakeBuffer([255, 0, 0, 255])
        pixels = _ubyte_rgba_to_pixels(buffer, 1, 1)
        self.assertEqual(buffer.dimensions, 4)
        self.assertEqual(list(pixels), [1.0, 0.0, 0.0, 1.0])

    def test_current_view_reuses_matrix_without_mutating_viewport(self) -> None:
        bpy, _area, region, _timers, _space_type, _images = self._environment()
        current = bpy.context.area.spaces.active.region_3d.view_matrix

        selected, metadata = _screenshot_view(bpy, None, current)

        self.assertIs(selected, current)
        self.assertEqual(metadata, {"view_mode": "current"})
        self.assertIs(bpy.context.area.spaces.active.region_3d.view_matrix, current)
        self.assertEqual(region.width, 2)

    def test_orbit_view_builds_independent_matrix_and_metadata(self) -> None:
        bpy, _area, _region, _timers, _space_type, _images = self._environment()
        current = bpy.context.area.spaces.active.region_3d.view_matrix
        orbit_matrix = object()
        view = {
            "mode": "orbit",
            "target": [1, 2.5, -3],
            "azimuth_degrees": 120,
            "elevation_degrees": -15,
            "distance": 8,
        }

        with patch(
            "blenderboo.tools.scene._orbit_view_matrix",
            return_value=orbit_matrix,
        ) as build_matrix:
            selected, metadata = _screenshot_view(bpy, view, current)

        self.assertIs(selected, orbit_matrix)
        self.assertIs(bpy.context.area.spaces.active.region_3d.view_matrix, current)
        build_matrix.assert_called_once_with([1.0, 2.5, -3.0], 120.0, -15.0, 8.0)
        self.assertEqual(
            metadata,
            {
                "view_mode": "orbit",
                "view_target": [1.0, 2.5, -3.0],
                "azimuth_degrees": 120.0,
                "elevation_degrees": -15.0,
                "distance": 8.0,
            },
        )

    def test_orbit_object_target_uses_object_center_and_default_distance(self) -> None:
        bpy, _area, _region, _timers, _space_type, _images = self._environment()
        obj = SimpleNamespace(dimensions=(2.0, 4.0, 1.0))
        bpy.context.scene.objects["Shrub"] = obj
        with patch(
            "blenderboo.tools.scene._object_orbit_target",
            return_value=[4.0, 5.0, 6.0],
        ), patch(
            "blenderboo.tools.scene._orbit_view_matrix",
            return_value="orbit-matrix",
        ) as build_matrix:
            matrix, metadata = _screenshot_view(
                bpy, {"mode": "orbit", "object_name": "Shrub"}, "current"
            )

        self.assertEqual(matrix, "orbit-matrix")
        build_matrix.assert_called_once_with([4.0, 5.0, 6.0], 45.0, 30.0, 10.0)
        self.assertEqual(metadata["target_object"], "Shrub")

    def test_orbit_view_validation_is_strict(self) -> None:
        bpy, _area, _region, _timers, _space_type, _images = self._environment()
        cases = [
            ({"mode": "current", "distance": 3}, InvalidParamsError, "Orbit-only"),
            ({"mode": "orbit", "target": [1, 2]}, InvalidParamsError, "exactly three"),
            ({"mode": "orbit", "distance": 0}, InvalidParamsError, "greater than zero"),
            ({"mode": "orbit", "elevation_degrees": 90}, InvalidParamsError, "-89.9 to 89.9"),
            (
                {"mode": "orbit", "object_name": "Shrub", "target": [0, 0, 0]},
                InvalidParamsError,
                "mutually exclusive",
            ),
            (
                {"mode": "orbit", "object_name": "Missing"},
                NotFoundError,
                "Object not found",
            ),
        ]
        for view, exception_type, message in cases:
            with self.subTest(view=view), self.assertRaisesRegex(exception_type, message):
                _screenshot_view(bpy, view, "current")

    def test_draw_handler_capture_writes_and_cleans_up(self) -> None:
        bpy, area, _region, timers, space_type, images = self._environment()
        framebuffer = FakeFramebuffer()
        offscreens = []

        def make_offscreen(width, height):
            offscreen = FakeOffscreen(width, height)
            offscreens.append(offscreen)
            return offscreen

        gpu = SimpleNamespace(
            types=SimpleNamespace(GPUOffScreen=make_offscreen),
            state=SimpleNamespace(active_framebuffer_get=Mock(return_value=framebuffer)),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ), patch.dict(sys.modules, {"gpu": gpu}):
            output = Path(directory) / "viewport.png"
            deferred = self._dispatch({"output_path": str(output)})
            self.assertIsInstance(deferred, DeferredEnvelope)
            completed = []
            deferred.add_done_callback(completed.append)
            self.assertEqual(completed, [])
            self.assertEqual(len(space_type.handlers), 1)
            self.assertEqual(space_type.handlers[0][3:], ("WINDOW", "POST_PIXEL"))
            area.tag_redraw.assert_called_once_with()

            # Blender may retain the bridge timer's global area/region while a
            # SpaceView3D draw handler is running.  The registered handler type,
            # not bpy.context identity, establishes the valid GPU draw context.
            bpy.context.area = SimpleNamespace(type="PROPERTIES")
            bpy.context.region = None
            space_type.draw_once()
            self.assertEqual(completed, [])
            timers.run_delay(0.0)

        self.assertTrue(completed[0]["ok"])
        self.assertEqual(completed[0]["data"]["path"], str(output.resolve()))
        self.assertEqual(completed[0]["data"]["area_type"], "VIEW_3D")
        self.assertEqual(completed[0]["data"]["shading_type"], "MATERIAL")
        self.assertEqual(completed[0]["data"]["view_mode"], "current")
        self.assertTrue(offscreens[0].freed)
        self.assertEqual(len(offscreens[0].draw_args), 6)
        self.assertEqual(offscreens[0].draw_kwargs, {"do_color_management": True})
        self.assertEqual(framebuffer.read_args, (0, 0, 2, 2, 4, 0, "UBYTE"))
        self.assertEqual(framebuffer.buffer.dimensions, 16)
        self.assertEqual(images.removed, images.created)
        self.assertEqual(images.created[0].pixels.values[:4], [1.0, 0.0, 0.0, 1.0])
        self.assertEqual(space_type.handlers, [])
        self.assertEqual(timers.callbacks, [])
        self.assertIsNone(scene_module._ACTIVE_SCREENSHOT_JOB)

    def test_overlapping_capture_is_rejected_without_second_handler(self) -> None:
        bpy, _area, _region, _timers, space_type, _images = self._environment()
        with tempfile.TemporaryDirectory() as directory, patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ):
            first = self._dispatch({"output_path": str(Path(directory) / "first.png")})
            second = self._dispatch({"output_path": str(Path(directory) / "second.png")})

        self.assertIsInstance(first, DeferredEnvelope)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"]["type"], "InvalidStateError")
        self.assertIn("active viewport draw job", second["error"]["message"])
        self.assertEqual(len(space_type.handlers), 1)

    def test_orbit_capture_job_keeps_visible_view_unchanged(self) -> None:
        bpy, _area, _region, _timers, _space_type, _images = self._environment()
        visible_matrix = bpy.context.area.spaces.active.region_3d.view_matrix
        orbit_matrix = object()
        with tempfile.TemporaryDirectory() as directory, patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ), patch(
            "blenderboo.tools.scene._orbit_view_matrix",
            return_value=orbit_matrix,
        ):
            deferred = self._dispatch(
                {
                    "output_path": str(Path(directory) / "orbit.png"),
                    "view": {
                        "mode": "orbit",
                        "target": [0, 0, 1],
                        "azimuth_degrees": 90,
                    },
                }
            )
            job = scene_module._ACTIVE_SCREENSHOT_JOB
            self.assertIsNotNone(job)
            self.assertIs(job.view_matrix, orbit_matrix)
            self.assertEqual(job.view_info["view_mode"], "orbit")
            self.assertEqual(job.view_info["azimuth_degrees"], 90.0)
            self.assertIs(
                bpy.context.area.spaces.active.region_3d.view_matrix, visible_matrix
            )
            deferred.cancel()

    def test_draw_timeout_rejects_and_removes_handler(self) -> None:
        bpy, _area, _region, timers, space_type, _images = self._environment()
        with tempfile.TemporaryDirectory() as directory, patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ):
            deferred = self._dispatch(
                {"output_path": str(Path(directory) / "timeout.png")}
            )
            completed = []
            deferred.add_done_callback(completed.append)
            timers.run_delay(scene_module._SCREENSHOT_DRAW_TIMEOUT_SECONDS)
            timers.run_delay(0.0)

        self.assertFalse(completed[0]["ok"])
        self.assertEqual(completed[0]["error"]["type"], "InvalidStateError")
        self.assertIn("Timed out", completed[0]["error"]["message"])
        self.assertEqual(space_type.handlers, [])
        self.assertEqual(timers.callbacks, [])

    def test_bridge_cancellation_removes_handler_and_timers(self) -> None:
        bpy, _area, _region, timers, space_type, _images = self._environment()
        with tempfile.TemporaryDirectory() as directory, patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ):
            deferred = self._dispatch(
                {"output_path": str(Path(directory) / "cancel.png")}
            )
            deferred.cancel()

        self.assertEqual(space_type.handlers, [])
        self.assertEqual(timers.callbacks, [])
        self.assertIsNone(scene_module._ACTIVE_SCREENSHOT_JOB)

    def test_gpu_failure_still_frees_offscreen_and_rejects(self) -> None:
        bpy, _area, _region, timers, space_type, _images = self._environment()
        framebuffer = FakeFramebuffer([0])
        offscreen = FakeOffscreen(2, 2)
        gpu = SimpleNamespace(
            types=SimpleNamespace(GPUOffScreen=Mock(return_value=offscreen)),
            state=SimpleNamespace(active_framebuffer_get=Mock(return_value=framebuffer)),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "blenderboo.tools.scene.get_bpy", return_value=bpy
        ), patch.dict(sys.modules, {"gpu": gpu}):
            deferred = self._dispatch(
                {"output_path": str(Path(directory) / "failure.png")}
            )
            completed = []
            deferred.add_done_callback(completed.append)
            space_type.draw_once()
            timers.run_delay(0.0)

        self.assertTrue(offscreen.freed)
        self.assertFalse(completed[0]["ok"])
        self.assertEqual(completed[0]["error"]["type"], "InvalidStateError")
        self.assertIn("returned 1 channels", completed[0]["error"]["message"])
        self.assertEqual(space_type.handlers, [])

    def test_draw_view3d_uses_documented_keyword_only_color_management(self) -> None:
        offscreen = Mock()
        bpy = SimpleNamespace(context=SimpleNamespace(scene="scene", view_layer="layer"))

        _draw_view3d(offscreen, bpy, "space", "region", "view", "projection")

        offscreen.draw_view3d.assert_called_once_with(
            "scene",
            "layer",
            "space",
            "region",
            "view",
            "projection",
            do_color_management=True,
        )


if __name__ == "__main__":
    unittest.main()
