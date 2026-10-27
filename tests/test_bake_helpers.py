"""Pure Python validation for bake.run."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo.errors import InvalidParamsError
from blenderboo.tools.bake import _coverage, run_bake


class BakeHelperTests(unittest.TestCase):
    def test_invalid_type_rejected_before_blender_lookup(self):
        with self.assertRaisesRegex(InvalidParamsError, "type must"):
            run_bake({"type": "COMBINED"})

    def test_missing_output_path_rejected_before_blender_lookup(self):
        with self.assertRaisesRegex(InvalidParamsError, "output_path"):
            run_bake({"type": "NORMAL", "target": "Low", "image": "Baked", "uv_layer": "UVMap"})

    def test_sampled_coverage_counts_alpha(self):
        image = SimpleNamespace(size=(2, 2), pixels=[0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0])
        self.assertEqual(_coverage(image), 0.5)


if __name__ == "__main__":
    unittest.main()
