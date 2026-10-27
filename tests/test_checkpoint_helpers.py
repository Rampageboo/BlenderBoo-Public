"""Pure-Python checkpoint path and tag tests."""

from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError, InvalidStateError, LockedError, StaleStateError
from blenderboo.tools import session_state
from blenderboo.tools.checkpoint import _context, _sanitize_tag, restore_checkpoint


class CheckpointHelperTests(unittest.TestCase):
    def test_context_for_working_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory, base = _context(str(Path(temp_dir) / "example.blend"))
            self.assertEqual(directory, Path(temp_dir).resolve() / "checkpoints")
            self.assertEqual(base, "example")

    def test_context_for_restored_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "checkpoints" / "example_ckpt_012_review.blend"
            directory, base = _context(str(path))
            self.assertEqual(directory, path.parent.resolve())
            self.assertEqual(base, "example")

    def test_unsaved_file_is_rejected(self) -> None:
        with self.assertRaises(InvalidStateError):
            _context("")

    def test_tag_is_sanitized(self) -> None:
        self.assertEqual(_sanitize_tag(" First review! "), "First-review")
        self.assertEqual(_sanitize_tag(None), "checkpoint")
        with self.assertRaises(InvalidParamsError):
            _sanitize_tag("***")

    def test_stale_state_does_not_call_blender_open(self) -> None:
        calls = []
        bpy = SimpleNamespace(
            data=SimpleNamespace(filepath="C:/tmp/scene.blend"),
            ops=SimpleNamespace(
                wm=SimpleNamespace(open_mainfile=lambda **kwargs: calls.append(kwargs))
            ),
        )
        with (
            patch.object(session_state, "_epoch", 0),
            patch.object(session_state, "_last_filepath", None),
            patch("blenderboo.tools.checkpoint.get_bpy", return_value=bpy),
            self.assertRaises(StaleStateError),
        ):
            restore_checkpoint({
                "name": "scene_ckpt_001_safe.blend",
                "confirm": True,
                "expected_state": "stale-token",
            })
        self.assertEqual(calls, [])

    def test_restore_checks_document_lease_before_open(self) -> None:
        calls = []
        bpy = SimpleNamespace(
            data=SimpleNamespace(filepath="C:/tmp/scene.blend"),
            ops=SimpleNamespace(
                wm=SimpleNamespace(open_mainfile=lambda **kwargs: calls.append(kwargs))
            ),
        )
        lock = LockedError(
            "held",
            {"active": True, "scope": "document", "held_by": "other", "expires_in": 30},
        )
        with (
            patch("blenderboo.tools.checkpoint.get_bpy", return_value=bpy),
            patch(
                "blenderboo.tools.checkpoint.require_document_write_access",
                side_effect=lock,
            ) as guard,
            self.assertRaises(LockedError),
        ):
            restore_checkpoint({"name": "scene_ckpt_001_safe.blend", "confirm": True})
        guard.assert_called_once_with(bpy)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
