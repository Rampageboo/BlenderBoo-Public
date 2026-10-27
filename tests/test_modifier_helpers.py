"""Pure-Python tests for modifier resource guards and typed snapshots."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools import geometry


def owner(*, vertices: int = 8, dimensions: tuple[float, float, float] = (2.0, 2.0, 2.0)):
    return SimpleNamespace(
        data=SimpleNamespace(vertices=range(vertices)),
        dimensions=dimensions,
    )


class ModifierSafetyTests(unittest.TestCase):
    def test_screw_rejects_excessive_step_product(self) -> None:
        with self.assertRaisesRegex(InvalidParamsError, "must not exceed"):
            geometry._validate_modifier_workload(
                "SCREW",
                {"steps": 10_000, "render_steps": 10_000, "iterations": 100},
                None,
                owner(),
            )

    def test_screw_default_settings_include_source_vertex_budget(self) -> None:
        with self.assertRaisesRegex(InvalidParamsError, "estimated output vertices"):
            geometry._validate_modifier_workload(
                "SCREW",
                {},
                None,
                owner(vertices=312_501),
            )

    def test_remesh_rejects_excessive_voxel_estimate(self) -> None:
        with self.assertRaisesRegex(InvalidParamsError, "estimated voxel cells"):
            geometry._validate_modifier_workload(
                "REMESH",
                {"mode": "VOXEL", "voxel_size": 0.000001},
                None,
                owner(),
            )

    def test_typed_snapshot_preserves_pointers_and_copies_sequences(self) -> None:
        pointer = SimpleNamespace(bl_rna=object(), name="Target")
        self.assertIs(geometry._setting_snapshot(pointer), pointer)

        values = [1.0, 2.0, 3.0]
        snapshot = geometry._setting_snapshot(values)
        values[0] = 99.0
        self.assertEqual(snapshot, [1.0, 2.0, 3.0])

    def test_rna_settings_accept_new_writable_bevel_property(self) -> None:
        property_record = SimpleNamespace(type="BOOLEAN", is_readonly=False, array_length=0)
        modifier = SimpleNamespace(bl_rna=SimpleNamespace(properties={"harden_normals": property_record}))
        result = geometry._validated_rna_settings(None, modifier, {"harden_normals": True})
        self.assertEqual(result, {"harden_normals": True})

    def test_rna_settings_reject_readonly_property(self) -> None:
        property_record = SimpleNamespace(type="BOOLEAN", is_readonly=True, array_length=0)
        modifier = SimpleNamespace(bl_rna=SimpleNamespace(properties={"locked": property_record}))
        with self.assertRaisesRegex(InvalidParamsError, "not writable"):
            geometry._validated_rna_settings(None, modifier, {"locked": True})


if __name__ == "__main__":
    unittest.main()
