"""Pure graph validation and connectivity invariants for creature tools."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools.creature import _graph
from blenderboo.tools.mesh_topology import connectivity_hash


class CreatureHelpers(unittest.TestCase):
    def test_graph_cycle_and_reciprocal_symmetry(self):
        good = {"joints": [
            {"name": "root", "parent": None, "position": [0, 0, 0], "radius": 0.2},
            {"name": "left", "parent": "root", "position": [1, 0, 0], "radius": 0.1, "symmetry_pair": "right"},
            {"name": "right", "parent": "root", "position": [-1, 0, 0], "radius": 0.1, "symmetry_pair": "left"},
        ]}
        self.assertEqual(len(_graph(good)["joints"]), 3)
        bad = {"joints": [dict(joint) for joint in good["joints"]]}
        bad["joints"][2]["symmetry_pair"] = None
        with self.assertRaises(InvalidParamsError):
            _graph(bad)
        bad["joints"][2]["symmetry_pair"] = "left"
        bad["joints"][0]["parent"] = "right"
        with self.assertRaises(InvalidParamsError):
            _graph(bad)

    def test_connectivity_hash_ignores_coordinates(self):
        def mesh(coords, faces):
            return SimpleNamespace(
                vertices=[SimpleNamespace(co=value) for value in coords],
                edges=[SimpleNamespace(vertices=edge) for edge in ((0, 1), (1, 2), (2, 3), (3, 0))],
                polygons=[SimpleNamespace(vertices=face) for face in faces],
            )
        a = mesh([(0, 0, 0)] * 4, [(0, 1, 2, 3)])
        b = mesh([(4, 0, 0)] * 4, [(0, 1, 2, 3)])
        c = mesh([(4, 0, 0)] * 4, [(0, 2, 1, 3)])
        self.assertEqual(connectivity_hash(a), connectivity_hash(b))
        self.assertNotEqual(connectivity_hash(a), connectivity_hash(c))


if __name__ == "__main__":
    unittest.main()
