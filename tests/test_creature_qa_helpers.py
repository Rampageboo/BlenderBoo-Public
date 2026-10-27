"""Pure helper contracts for source baking and ROM pose evidence."""

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.tools.creature_qa import _image_lighting_evidence
from blenderboo.tools.rig_rom import _edge_change, _poses


class CreatureQAHelperTests(unittest.TestCase):
    def test_pose_validation_rejects_ambiguous_or_unsafe_names(self):
        self.assertEqual(_poses([{"name": "elbow", "bones": [
            {"bone": "elbow.L", "axis": "Z", "degrees": 75}]}])[0]["name"], "elbow")
        for pose in (
            {"name": "../bad", "bones": [{"bone": "Tip", "axis": "X", "degrees": 20}]},
            {"name": "bad", "bones": [{"bone": "Tip", "axis": "Q", "degrees": 20}]},
            {"name": "bad", "bones": [{"bone": "Tip", "axis": "X", "degrees": 999}]},
        ):
            with self.subTest(pose=pose), self.assertRaises(Exception):
                _poses([pose])

    def test_edge_stretch_and_compression_keep_focus(self):
        class Vec:
            def __init__(self, values):
                self.values = tuple(values)
            def __sub__(self, other):
                return Vec(a - b for a, b in zip(self.values, other.values))
            def __add__(self, other):
                return Vec(a + b for a, b in zip(self.values, other.values))
            def __mul__(self, scalar):
                return Vec(a * scalar for a in self.values)
            @property
            def length(self):
                return sum(value * value for value in self.values) ** .5
            def __iter__(self):
                return iter(self.values)
        rest = {"vertices": [Vec((0, 0, 0)), Vec((1, 0, 0)),
                             Vec((0, 1, 0))], "edges": [(0, 1), (0, 2)]}
        posed = {"vertices": [Vec((0, 0, 0)), Vec((2, 0, 0)),
                              Vec((0, .5, 0))]}
        result = _edge_change(rest, posed)
        self.assertAlmostEqual(result["max_stretch"]["ratio"], 2)
        self.assertAlmostEqual(result["max_compression"]["ratio"], .5)
        self.assertEqual(result["max_stretch"]["focus"], [1, 0, 0])

    def test_directional_texture_gradient_is_advisory(self):
        width = height = 16
        pixels = []
        for _y in range(height):
            for x in range(width):
                level = x / (width - 1)
                pixels.extend((level, level, level, 1.0))
        image = SimpleNamespace(name="Color", size=(width, height), has_data=True, pixels=pixels)
        result = _image_lighting_evidence(image)
        self.assertTrue(result["possible_baked_lighting"])
        self.assertGreater(result["directional_r_squared"], .9)


if __name__ == "__main__":
    unittest.main()
