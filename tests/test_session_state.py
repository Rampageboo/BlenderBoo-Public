"""Pure-Python tests for optimistic document state and request attribution."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError, StaleStateError
from blenderboo.registry import ToolRegistry
from blenderboo.tools import session_state
from blenderboo.tools.session import whoami
from blenderboo.tools.transaction import new_transaction


def fake_bpy(filepath: str = ""):
    return SimpleNamespace(data=SimpleNamespace(filepath=filepath, is_dirty=False))


class SessionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.epoch_patch = patch.object(session_state, "_epoch", 0)
        self.filepath_patch = patch.object(session_state, "_last_filepath", None)
        self.epoch_patch.start()
        self.filepath_patch.start()
        self.addCleanup(self.epoch_patch.stop)
        self.addCleanup(self.filepath_patch.stop)

    def test_token_is_deterministic_opaque_and_stable_for_unsaved_files(self) -> None:
        bpy = fake_bpy("")
        first = session_state.current_state_token(bpy)
        second = session_state.current_state_token(bpy)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{8}\.0$")
        self.assertNotIn("blend", first)

        bpy.data.filepath = "C:/projects/secret-scene.blend"
        saved = session_state.current_state_token(bpy)
        self.assertRegex(saved, r"^[0-9a-f]{8}\.1$")
        self.assertNotIn("secret-scene", saved)

    def test_lazy_reconciliation_and_explicit_bumps_are_monotonic(self) -> None:
        bpy = fake_bpy("C:/projects/a.blend")
        self.assertEqual(session_state.reconcile(bpy), 0)
        self.assertEqual(session_state.reconcile(bpy), 0)
        bpy.data.filepath = "C:/projects/b.blend"
        self.assertEqual(session_state.reconcile(bpy), 1)
        self.assertEqual(session_state.bump_epoch(), 2)
        self.assertEqual(session_state.bump_epoch(), 3)
        self.assertTrue(session_state.current_state_token(bpy).endswith(".3"))

    def test_expected_state_validation_names_expected_and_current(self) -> None:
        bpy = fake_bpy("C:/projects/a.blend")
        current = session_state.current_state_token(bpy)
        with self.assertRaisesRegex(StaleStateError, "stale-token") as raised:
            session_state.check_expected_state("stale-token", bpy)
        self.assertIn(current, str(raised.exception))
        with self.assertRaises(InvalidParamsError):
            session_state.check_expected_state("", bpy)

    def test_dispatch_context_is_recorded_and_does_not_leak(self) -> None:
        registry = ToolRegistry()
        registry.register(
            "test.mutate",
            lambda params: new_transaction([{"type": "Material", "name": "Leaf"}]),
        )
        attributed = registry.dispatch(
            "test.mutate", {}, context={"client_id": "mcp-process-1"}
        )
        anonymous = registry.dispatch("test.mutate", {})
        self.assertEqual(attributed["data"]["client_id"], "mcp-process-1")
        self.assertIsNone(anonymous["data"]["client_id"])

    def test_whoami_rejects_params_before_accessing_bpy(self) -> None:
        with self.assertRaises(InvalidParamsError):
            whoami({"client_id": "must-not-be-a-tool-param"})


if __name__ == "__main__":
    unittest.main()
