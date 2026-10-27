"""Pure-Python compatibility tests for Geometry Nodes modifier input storage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.tools import geometry_nodes


class _LegacyModifier(dict):
    pass


class GeometryNodesInputCompatibilityTests(unittest.TestCase):
    def test_typed_rna_input_is_read_assigned_and_restored(self) -> None:
        wrapper = SimpleNamespace(value=0.5, type="VALUE", attribute_name="")
        inputs = SimpleNamespace(Socket_2=wrapper)
        modifier = SimpleNamespace(properties=SimpleNamespace(inputs=inputs))
        socket = SimpleNamespace(identifier="Socket_2", default_value=0.5)

        snapshot = geometry_nodes._modifier_input_snapshot(modifier, "Socket_2", socket)
        self.assertEqual(snapshot["backend"], "rna")
        self.assertEqual(geometry_nodes._modifier_input_json(snapshot), 0.5)
        self.assertFalse(geometry_nodes._modifier_input_overridden(snapshot, socket))

        geometry_nodes._assign_modifier_input(snapshot, 1.25)
        self.assertEqual(wrapper.value, 1.25)
        self.assertEqual(wrapper.type, "VALUE")
        geometry_nodes._restore_modifier_input(snapshot)
        self.assertEqual(wrapper.value, 0.5)
        self.assertEqual(wrapper.type, "VALUE")

    def test_typed_attribute_mode_is_an_override_and_restores_mode(self) -> None:
        wrapper = SimpleNamespace(value=0.5, type="ATTRIBUTE", attribute_name="density")
        modifier = SimpleNamespace(
            properties=SimpleNamespace(inputs=SimpleNamespace(Socket_2=wrapper))
        )
        socket = SimpleNamespace(identifier="Socket_2", default_value=0.5)

        snapshot = geometry_nodes._modifier_input_snapshot(modifier, "Socket_2", socket)
        self.assertTrue(geometry_nodes._modifier_input_overridden(snapshot, socket))
        geometry_nodes._assign_modifier_input(snapshot, 2.0)
        self.assertEqual(wrapper.type, "VALUE")
        geometry_nodes._restore_modifier_input(snapshot)
        self.assertEqual(wrapper.type, "ATTRIBUTE")
        self.assertEqual(wrapper.attribute_name, "density")

    def test_legacy_id_property_input_is_preserved(self) -> None:
        modifier = _LegacyModifier(Socket_2=0.75)
        socket = SimpleNamespace(identifier="Socket_2", default_value=0.5)

        snapshot = geometry_nodes._modifier_input_snapshot(modifier, "Socket_2", socket)
        self.assertEqual(snapshot["backend"], "id_property")
        self.assertTrue(geometry_nodes._modifier_input_overridden(snapshot, socket))
        geometry_nodes._assign_modifier_input(snapshot, 1.5)
        self.assertEqual(modifier["Socket_2"], 1.5)
        geometry_nodes._restore_modifier_input(snapshot)
        self.assertEqual(modifier["Socket_2"], 0.75)

    def test_legacy_missing_input_is_removed_on_restore(self) -> None:
        modifier = _LegacyModifier()
        socket = SimpleNamespace(identifier="Socket_2", default_value=0.5)

        snapshot = geometry_nodes._modifier_input_snapshot(modifier, "Socket_2", socket)
        self.assertFalse(snapshot["exists"])
        self.assertEqual(geometry_nodes._modifier_input_json(snapshot), 0.5)
        geometry_nodes._assign_modifier_input(snapshot, 1.5)
        geometry_nodes._restore_modifier_input(snapshot)
        self.assertNotIn("Socket_2", modifier)


if __name__ == "__main__":
    unittest.main()
