"""Checkpoint template matching and retention inventory."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools.checkpoint import _context, _entries, _template


class CheckpointTemplateTests(unittest.TestCase):
    def test_custom_destination_lists_only_matching_project(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            template = _template("{base}--{index:03d}--{tag}.blend")
            (directory / "Scene--001--before.blend").write_bytes(b"one")
            (directory / "Scene--002--after.blend").write_bytes(b"two")
            (directory / "Other--003--after.blend").write_bytes(b"three")
            entries = _entries(directory, "Scene", template)
            self.assertEqual([entry["index"] for entry in entries], [1, 2])
            active_directory, base = _context(str(directory / "Scene--002--after.blend"), directory, template)
            self.assertEqual((active_directory, base), (directory, "Scene"))

    def test_template_accepts_fixed_filename_and_rejects_invalid_fields(self) -> None:
        self.assertIsNotNone(_template("{base}-{tag}.blend"))
        self.assertIsNotNone(_template("M4A4_trigger-checkpoint-001_2026-09-24_213500.blend"))
        with self.assertRaisesRegex(InvalidParamsError, "supports only"):
            _template("{unknown}.blend")


if __name__ == "__main__":
    unittest.main()
