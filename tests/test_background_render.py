"""Focused contracts for isolated background-render jobs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from blenderboo.tools.background_render import BackgroundRenderManager, MAX_FAILURE_SNAPSHOTS
from blenderboo.tools.background_render_runner import _absolute_external_paths


class FakeProcess:
    pid = 4321

    def __init__(self) -> None:
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class BackgroundRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / "Blender.exe"
        self.binary.write_bytes(b"blender")
        self.live = self.root / "live.blend"
        self.live.write_bytes(b"live")
        self.save_calls = []
        self.processes = []
        self.commands = []
        self.timers = []

        def save_as_mainfile(**kwargs):
            self.save_calls.append(kwargs)
            Path(kwargs["filepath"]).write_bytes(b"snapshot")
            return {"FINISHED"}

        self.data = SimpleNamespace(filepath=str(self.live), is_dirty=True)
        self.bpy = SimpleNamespace(
            data=self.data,
            app=SimpleNamespace(binary_path=str(self.binary), handlers=SimpleNamespace(depsgraph_update_post=[]), timers=SimpleNamespace(
                register=lambda callback, **kwargs: self.timers.append(callback))),
            ops=SimpleNamespace(wm=SimpleNamespace(save_as_mainfile=save_as_mainfile)),
        )

        def popen(command, **kwargs):
            process = FakeProcess()
            self.processes.append(process)
            self.commands.append(command)
            return process

        self.manager = BackgroundRenderManager(temp_root=self.root / "jobs", popen=popen)
        bpy_patch = patch("blenderboo.tools.background_render.get_bpy", return_value=self.bpy)
        state_patch = patch("blenderboo.tools.background_render.current_state_token", return_value="state-a")
        preflight_patch = patch("blenderboo.tools.render.preflight_render_options",
                                side_effect=lambda mode, options, bpy: options)
        bpy_patch.start()
        state_patch.start()
        preflight_patch.start()
        self.addCleanup(bpy_patch.stop)
        self.addCleanup(state_patch.stop)
        self.addCleanup(preflight_patch.stop)

    def _start_and_pump(self, mode="preview", options=None):
        started = self.manager.start({"mode": mode, "options": options or {}}).data
        self.manager.pump()
        return started["operation_id"]

    def test_dirty_live_document_is_snapshotted_as_copy_and_job_file_is_absolute(self) -> None:
        operation_id = self._start_and_pump("detail", {"object_name": "Receiver", "output_path": "renders/detail.png"})
        self.assertEqual(self.data.filepath, str(self.live))
        self.assertTrue(self.data.is_dirty)
        self.assertTrue(self.save_calls[0]["copy"])
        self.assertEqual(self.commands[0][0], str(self.binary))
        self.assertEqual(self.commands[0][1], "-b")
        job_file = Path(self.commands[0][-1])
        job = json.loads(job_file.read_text(encoding="utf-8"))
        self.assertEqual(job["options"]["output_path"], str(self.root / "renders" / "detail.png"))
        self.assertTrue(job["snapshot_dirty"])
        self.assertEqual(self.manager.status(operation_id)["status"], "running")

    def test_ai_source_bake_uses_snapshot_and_exposes_result_paths(self) -> None:
        options = {"low": "Low", "high": "High", "output_dir": "textures", "resolution": 64}
        operation_id = self._start_and_pump("ai_source_bake", options)
        job_file = Path(self.commands[0][-1])
        job = json.loads(job_file.read_text(encoding="utf-8"))
        self.assertEqual(job["mode"], "ai_source_bake")
        self.assertIn("--factory-startup", self.commands[0])
        self.assertEqual(job["options"]["output_dir"], str(self.root / "textures"))
        self.assertEqual(self.manager.status(operation_id)["phase"], "baking")
        (job_file.parent / "result.json").write_text(json.dumps({
            "ok": True, "data": {"paths": {"BaseColor": "color.png", "NormalGL": "normal.png"}}
        }), encoding="utf-8")
        self.processes[0].code = 0
        self.manager.pump()
        self.assertEqual(self.manager.status(operation_id)["output_paths"], ["color.png", "normal.png"])

    def test_background_status_waits_and_surfaces_child_progress(self) -> None:
        operation_id = self._start_and_pump("ai_source_bake", {
            "low": "Low", "high": "High", "output_dir": "textures", "resolution": 64})
        progress_path = Path(self.commands[0][-1]).parent / "progress.json"
        progress_path.write_text(json.dumps({
            "phase": "nearest_projection", "processed_texels": 8192,
            "total_texels": 16384, "sequence": 4,
            "updated_at": "2026-10-20T10:00:00+00:00", "child_cpu_s": 7.2,
        }), encoding="utf-8")
        status = self.manager.status(operation_id)
        self.assertEqual(status["phase"], "nearest_projection")
        self.assertEqual(status["last_updated_at"], "2026-10-20T10:00:00+00:00")
        self.assertEqual(status["progress"]["processed_texels"], 8192)
        started = time.monotonic()
        self.assertEqual(self.manager.wait_status(operation_id, 80)["status"], "running")
        self.assertGreaterEqual(time.monotonic() - started, .07)
        self.manager.cancel(operation_id)
        self.manager.pump()
        started = time.monotonic()
        self.assertEqual(self.manager.wait_status(operation_id, 500)["status"], "cancelled")
        self.assertLess(time.monotonic() - started, .2)

    def test_unsaved_document_uses_temp_snapshot_without_changing_live_path(self) -> None:
        self.data.filepath = ""
        operation_id = self._start_and_pump()
        job_file = Path(self.commands[0][-1])
        job = json.loads(job_file.read_text(encoding="utf-8"))
        self.assertTrue(job["unsaved_source"])
        self.assertEqual(self.data.filepath, "")
        self.assertTrue(Path(self.commands[0][2]).is_file())
        self.assertTrue(Path(job["options"]["output_path"]).is_absolute())
        self.assertEqual(self.manager.status(operation_id)["snapshot_dirty"], True)

    def test_queue_status_success_result_and_cleanup(self) -> None:
        first = self.manager.start({"mode": "preview"}).data["operation_id"]
        second = self.manager.start({"mode": "preview"}).data["operation_id"]
        self.assertEqual(self.manager.status(second)["queue_position"], 2)
        self.manager.pump()
        self.assertEqual(self.manager.status(second)["queue_position"], 1)
        first_dir = Path(self.commands[0][-1]).parent
        (first_dir / "result.json").write_text(json.dumps({"ok": True, "data": {"path": "render.png"}}), encoding="utf-8")
        self.processes[0].code = 0
        self.manager.pump()
        self.assertEqual(self.manager.status(first)["status"], "succeeded")
        self.assertEqual(self.manager.status(first)["output_paths"], ["render.png"])
        self.assertFalse(first_dir.exists())
        self.assertEqual(self.manager.status(second)["status"], "running")

    def test_running_cancel_terminates_child_then_reports_cancelled(self) -> None:
        operation_id = self._start_and_pump()
        job_dir = Path(self.commands[0][-1]).parent
        requested = self.manager.cancel(operation_id)
        self.assertTrue(requested["cancel_requested"])
        self.assertTrue(self.processes[0].terminated)
        self.manager.pump()
        self.assertEqual(self.manager.status(operation_id)["status"], "cancelled")
        self.assertFalse(job_dir.exists())

    def test_shutdown_waits_for_child_exit_before_snapshot_cleanup(self) -> None:
        operation_id = self._start_and_pump()
        job_dir = Path(self.commands[0][-1]).parent
        self.manager.shutdown()
        self.assertTrue(self.processes[0].terminated)
        self.assertEqual(self.manager.status(operation_id)["status"], "cancelled")
        self.assertFalse(job_dir.exists())

    def test_shutdown_preserves_unconfirmed_snapshot_and_finalizes_other_jobs(self) -> None:
        self.manager.max_active = 2
        first = self.manager.start({"mode": "preview"}).data["operation_id"]
        second = self.manager.start({"mode": "preview"}).data["operation_id"]
        self.manager.pump()
        self.manager.pump()
        first_dir = Path(self.commands[0][-1]).parent
        second_dir = Path(self.commands[1][-1]).parent

        def still_running():
            return None

        def timed_out(timeout):
            import subprocess
            raise subprocess.TimeoutExpired("Blender.exe", timeout)

        self.processes[0].poll = still_running
        self.processes[0].terminate = lambda: None
        self.processes[0].wait = timed_out
        self.processes[0].kill = lambda: (_ for _ in ()).throw(OSError("cannot kill"))
        self.manager.shutdown()
        self.assertEqual(self.manager.status(first)["status"], "running")
        self.assertIn("snapshot retained", " ".join(self.manager.status(first)["warnings"]))
        self.assertTrue(first_dir.exists())
        self.assertEqual(self.manager.status(second)["status"], "cancelled")
        self.assertFalse(second_dir.exists())
        self.assertEqual(self.bpy.app.handlers.depsgraph_update_post, [])

    def test_failure_diagnostics_are_bounded(self) -> None:
        directories = []
        for _ in range(MAX_FAILURE_SNAPSHOTS + 1):
            operation_id = self._start_and_pump()
            directory = Path(self.commands[-1][-1]).parent
            directories.append(directory)
            self.processes[-1].code = 1
            self.manager.pump()
            self.assertEqual(self.manager.status(operation_id)["status"], "failed")
        self.assertFalse(directories[0].exists())
        self.assertTrue(all(path.exists() for path in directories[1:]))

    def test_child_fixes_relative_image_paths_against_original_document(self) -> None:
        image = SimpleNamespace(filepath="//textures/albedo.png", name="Albedo")
        reloaded = []
        image.reload = lambda: reloaded.append(True)
        bpy = SimpleNamespace(data=SimpleNamespace(images=[image]))
        self.assertEqual(_absolute_external_paths(bpy, self.root), [])
        self.assertEqual(image.filepath, str((self.root / "textures" / "albedo.png").resolve()))
        self.assertEqual(reloaded, [True])


if __name__ == "__main__":
    unittest.main()
