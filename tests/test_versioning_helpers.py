"""Pure-Python version-name allocation tests."""

from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError, InvalidStateError, LockedError, MutationApplyError, StaleStateError
from blenderboo.tools import session_state, versioning
from blenderboo.tools.versioning import _allocate_version_path, _version_context, file_state, observe_loaded_file, save_as, save_current, save_version


class VersioningHelperTests(unittest.TestCase):
    def test_registration_defers_file_observation_until_data_is_available(self) -> None:
        from blenderboo import register

        class RestrictedData:
            @property
            def filepath(self):
                raise AttributeError("_RestrictData has no attribute 'filepath'")

        callbacks = []
        timers = SimpleNamespace(
            is_registered=lambda callback: callback in callbacks,
            register=lambda callback, first_interval: callbacks.append(callback),
        )
        handlers = SimpleNamespace(load_post=[], persistent=lambda callback: callback)
        bpy = SimpleNamespace(data=RestrictedData(), app=SimpleNamespace(handlers=handlers, timers=timers))
        fake_ui = SimpleNamespace(register_bridge_ui=lambda: None, auto_start_enabled=lambda: False)
        with (
            patch.dict(sys.modules, {"blenderboo.bridge_ui": fake_ui}),
            patch("blenderboo.tools._blender.get_bpy", return_value=bpy),
            patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
            patch("blenderboo._LOAD_POST_CALLBACK", None),
            patch.object(versioning, "_observed_disk", None),
        ):
            register()
            self.assertEqual(callbacks, [observe_loaded_file])
            self.assertEqual(handlers.load_post, [observe_loaded_file])
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "loaded.blend"
                path.write_bytes(b"loaded scene")
                bpy.data = SimpleNamespace(filepath=str(path), is_dirty=False)
                callbacks[0]()
                self.assertEqual(versioning._observed_disk, versioning._disk_identity(file_state(bpy)))

    def test_one_call_save_requires_matching_load_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scene.blend"
            path.write_bytes(b"loaded")
            data = SimpleNamespace(filepath=str(path), is_dirty=True)
            saves = []

            def save_mainfile(**kwargs):
                saves.append(kwargs)
                path.write_bytes(b"saved")
                data.is_dirty = False
                return {"FINISHED"}

            bpy = SimpleNamespace(data=data, ops=SimpleNamespace(wm=SimpleNamespace(save_mainfile=save_mainfile)))
            with (
                patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
                patch("blenderboo.tools.versioning._observed_disk", None),
                patch("blenderboo.tools.versioning.require_document_write_access"),
                patch("blenderboo.tools.versioning.follow_active_document"),
                patch("blenderboo.tools.versioning.bump_epoch"),
                patch("blenderboo.tools.versioning.current_state_token", return_value="state"),
            ):
                self.assertTrue(save_current({})["dry_run"])
                observe_loaded_file()
                self.assertTrue(save_current({})["write_confirmed"])
                self.assertEqual(len(saves), 1)
                path.write_bytes(b"externally changed with different size")
                self.assertTrue(save_current({})["dry_run"])
                self.assertEqual(len(saves), 1)

    def test_first_save_as_plans_then_writes_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "first.blend"
            data = SimpleNamespace(filepath="", is_dirty=True)
            calls = []

            def save_as_mainfile(**kwargs):
                calls.append(kwargs)
                path.write_bytes(b"saved")
                data.filepath = str(path)
                data.is_dirty = False
                return {"FINISHED"}

            bpy = SimpleNamespace(data=data, ops=SimpleNamespace(wm=SimpleNamespace(save_as_mainfile=save_as_mainfile)))
            with (
                patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
                patch("blenderboo.tools.versioning.require_document_write_access"),
                patch("blenderboo.tools.versioning.follow_active_document"),
                patch("blenderboo.tools.versioning.bump_epoch"),
            ):
                planned = save_as({"path": str(path)})
                self.assertTrue(planned["dry_run"])
                self.assertFalse(path.exists())
                applied = save_as({"path": str(path), "dry_run": False})
                self.assertTrue(applied["write_confirmed"])
                self.assertTrue(applied["dirty_confirmed"])
                self.assertEqual(calls[0]["filepath"], str(path))
                with self.assertRaises(InvalidStateError):
                    save_as({"path": str(path), "dry_run": False})

    def test_first_save_as_operator_error_after_disk_write_reports_confirmed_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "first.blend"
            data = SimpleNamespace(filepath="", is_dirty=True)

            def fail_after_write(**_kwargs):
                path.write_bytes(b"saved-before-error")
                data.filepath = str(path)
                data.is_dirty = False
                raise RuntimeError("late save-as error")

            bpy = SimpleNamespace(data=data, ops=SimpleNamespace(wm=SimpleNamespace(save_as_mainfile=fail_after_write)))
            with (
                patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
                patch("blenderboo.tools.versioning.require_document_write_access"),
                patch("blenderboo.tools.versioning.follow_active_document"),
                patch("blenderboo.tools.versioning.bump_epoch") as bump,
            ):
                result = save_as({"path": str(path), "dry_run": False})
            self.assertTrue(result["write_confirmed"])
            self.assertTrue(result["changed"])
            self.assertTrue(result["dirty_confirmed"])
            self.assertEqual(result["after"]["disk_size"], len(b"saved-before-error"))
            self.assertIn("late save-as error", result["operator_error"])
            bump.assert_called_once_with()

    def test_file_state_and_confirmed_in_place_save_report_disk_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scene.blend"
            path.write_bytes(b"before")
            data = SimpleNamespace(filepath=str(path), is_dirty=True)
            calls = []

            def save_mainfile(**kwargs):
                calls.append(kwargs)
                path.write_bytes(b"after-save")
                data.is_dirty = False
                return {"FINISHED"}

            bpy = SimpleNamespace(data=data, ops=SimpleNamespace(wm=SimpleNamespace(save_mainfile=save_mainfile)))
            self.assertTrue(file_state(bpy)["disk_exists"])
            with (
                patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
                patch("blenderboo.tools.versioning.check_expected_state"),
                patch("blenderboo.tools.versioning.require_document_write_access"),
                patch("blenderboo.tools.versioning.follow_active_document"),
                patch("blenderboo.tools.versioning.bump_epoch"),
                patch("blenderboo.tools.versioning.current_state_token", return_value="state"),
            ):
                planned = save_current({})
                self.assertTrue(planned["dry_run"])
                self.assertEqual(calls, [])
                applied = save_current({"dry_run": False, "confirm": True})
            self.assertTrue(applied["write_confirmed"])
            self.assertTrue(applied["dirty_confirmed"])
            self.assertEqual(applied["after"]["disk_size"], len(b"after-save"))
            self.assertEqual(calls[0]["filepath"], str(path.resolve()))

    def test_confirm_and_matching_state_apply_unsafe_in_place_save_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scene.blend"
            path.write_bytes(b"original on disk")
            data = SimpleNamespace(filepath=str(path), is_dirty=True)
            saves = []

            def save_mainfile(**kwargs):
                saves.append(kwargs)
                path.write_bytes(b"confirmed save")
                data.is_dirty = False
                return {"FINISHED"}

            bpy = SimpleNamespace(data=data, ops=SimpleNamespace(wm=SimpleNamespace(save_mainfile=save_mainfile)))
            with (
                patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
                patch("blenderboo.tools.versioning._observed_disk", None),
                patch("blenderboo.tools.versioning.check_expected_state"),
                patch("blenderboo.tools.versioning.require_document_write_access"),
                patch("blenderboo.tools.versioning.follow_active_document"),
                patch("blenderboo.tools.versioning.bump_epoch"),
                patch("blenderboo.tools.versioning.current_state_token", return_value="state"),
            ):
                observe_loaded_file()
                path.write_bytes(b"changed outside Blender")
                self.assertTrue(save_current({"confirm": True})["dry_run"])
                self.assertTrue(save_current({"confirm": True, "expected_state": "state",
                                              "dry_run": True})["dry_run"])
                self.assertFalse(saves)
                applied = save_current({"confirm": True, "expected_state": "state"})
            self.assertTrue(applied["write_confirmed"])
            self.assertEqual(len(saves), 1)
            self.assertEqual(path.read_bytes(), b"confirmed save")

    def test_in_place_save_exception_after_disk_write_reports_confirmed_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scene.blend"
            path.write_bytes(b"before")
            data = SimpleNamespace(filepath=str(path), is_dirty=True)

            def fail_after_write(**_kwargs):
                path.write_bytes(b"possibly-written")
                data.is_dirty = False
                raise RuntimeError("late operator failure")

            bpy = SimpleNamespace(data=data, ops=SimpleNamespace(wm=SimpleNamespace(save_mainfile=fail_after_write)))
            with (
                patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
                patch("blenderboo.tools.versioning.check_expected_state"),
                patch("blenderboo.tools.versioning.require_document_write_access"),
                patch("blenderboo.tools.versioning.current_state_token", return_value="state"),
                patch("blenderboo.tools.versioning.follow_active_document"),
                patch("blenderboo.tools.versioning.bump_epoch") as bump,
            ):
                result = save_current({"dry_run": False, "confirm": True})
            self.assertTrue(result["write_confirmed"])
            self.assertTrue(result["dirty_confirmed"])
            self.assertEqual(result["after"]["disk_size"], len(b"possibly-written"))
            self.assertIn("late operator failure", result["operator_error"])
            bump.assert_called_once_with()

    def test_confirmed_write_is_not_failed_when_blender_still_reports_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scene.blend"
            path.write_bytes(b"before")
            data = SimpleNamespace(filepath=str(path), is_dirty=True)

            def save_but_remain_dirty(**_kwargs):
                path.write_bytes(b"after-save")
                return {"FINISHED"}

            bpy = SimpleNamespace(data=data, ops=SimpleNamespace(wm=SimpleNamespace(save_mainfile=save_but_remain_dirty)))
            with (
                patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
                patch("blenderboo.tools.versioning.check_expected_state"),
                patch("blenderboo.tools.versioning.require_document_write_access"),
                patch("blenderboo.tools.versioning.follow_active_document"),
                patch("blenderboo.tools.versioning.bump_epoch"),
                patch("blenderboo.tools.versioning.current_state_token", return_value="state"),
            ):
                result = save_current({"dry_run": False, "confirm": True})
            self.assertTrue(result["write_confirmed"])
            self.assertFalse(result["dirty_confirmed"])

    def test_derives_base_and_allocates_next_non_overwriting_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "shrub_v001.blend").touch()
            (directory / "shrub_v003_review.blend").touch()
            current, base = _version_context(str(directory / "shrub_v003.blend"))
            path, index = _allocate_version_path(current.parent, base, "repair")
            self.assertEqual(base, "shrub")
            self.assertEqual(index, 4)
            self.assertEqual(path.name, "shrub_v004_repair.blend")

    def test_unsaved_and_invalid_base_are_rejected(self) -> None:
        with self.assertRaises(InvalidStateError):
            _version_context("")
        with self.assertRaises(InvalidParamsError):
            _version_context("C:/tmp/scene.blend", "../outside")
        with self.assertRaises(InvalidParamsError):
            save_version({"unexpected": True})

    def test_stale_state_does_not_call_blender_save(self) -> None:
        calls = []
        bpy = SimpleNamespace(
            data=SimpleNamespace(filepath="C:/tmp/scene.blend"),
            ops=SimpleNamespace(
                wm=SimpleNamespace(save_as_mainfile=lambda **kwargs: calls.append(kwargs))
            ),
        )
        with (
            patch.object(session_state, "_epoch", 0),
            patch.object(session_state, "_last_filepath", None),
            patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
            self.assertRaises(StaleStateError),
        ):
            save_version({"expected_state": "stale-token"})
        self.assertEqual(calls, [])

    def test_save_version_checks_document_lease_before_save(self) -> None:
        calls = []
        bpy = SimpleNamespace(
            data=SimpleNamespace(filepath="C:/tmp/scene.blend"),
            ops=SimpleNamespace(
                wm=SimpleNamespace(save_as_mainfile=lambda **kwargs: calls.append(kwargs))
            ),
        )
        lock = LockedError(
            "held",
            {"active": True, "scope": "document", "held_by": "other", "expires_in": 30},
        )
        with (
            patch("blenderboo.tools.versioning.get_bpy", return_value=bpy),
            patch(
                "blenderboo.tools.versioning.require_document_write_access",
                side_effect=lock,
            ) as guard,
            self.assertRaises(LockedError),
        ):
            save_version({})
        guard.assert_called_once_with(bpy)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
