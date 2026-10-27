"""Pure assembly selection and expectation contracts."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))

from blenderboo.tools.assembly import (
    _bounds_overlap, _candidate_pairs, _classify, _evaluate_expectation, _expectation_margins, _expectations,
)
from blenderboo.errors import InvalidParamsError


class AssemblyHelpersTests(unittest.TestCase):
    def test_status_boundaries(self):
        self.assertEqual(_classify(0, .0001, .01, penetrates=True), "intersecting")
        self.assertEqual(_classify(.0001, .0001, .01, penetrates=False), "contact")
        self.assertEqual(_classify(.001, .0001, .01, penetrates=False), "gap")
        self.assertEqual(_classify(.011, .0001, .01, penetrates=False), "separated")

    def test_expectations(self):
        self.assertEqual(_evaluate_expectation("contact", 0, {"contact": True})["status"], "pass")
        self.assertEqual(_evaluate_expectation("intersecting", 0, {"contact": True})["status"], "fail")
        self.assertEqual(_evaluate_expectation("intersecting", 0, {"contact": True, "intersection_allowed": True})["status"], "pass")
        self.assertEqual(_evaluate_expectation("gap", .002, {"gap_between": [.001, .003]})["status"], "pass")
        self.assertEqual(_evaluate_expectation("intersecting", 0, {"gap_between": [0, .003]})["status"], "fail")
        self.assertEqual(_evaluate_expectation("intersecting", 0, {"no_intersection": True})["status"], "fail")
        self.assertEqual(_evaluate_expectation("gap", .002, {"contact": False})["status"], "pass")
        self.assertEqual(_evaluate_expectation("contact", 0, {"contact": False})["status"], "fail")
        self.assertEqual(_evaluate_expectation("intersecting", 0, {"contact": False})["status"], "fail")
        self.assertEqual(_evaluate_expectation("intersecting", 0, {"no_intersection": False})["status"], "pass")
        self.assertEqual(_evaluate_expectation("contact", 0, {"no_intersection": False})["status"], "fail")
        self.assertEqual(_evaluate_expectation("intersecting", 0, {"intersection_allowed": False})["status"], "fail")
        self.assertEqual(_evaluate_expectation("gap", .002, {"intersection_allowed": False})["status"], "pass")
        self.assertEqual(_evaluate_expectation("intersecting", 0, {"intersection_allowed": True})["status"], "pass")
        with self.assertRaises(InvalidParamsError):
            _expectations([{"pair": ["A", "B"], "no_intersection": True, "intersection_allowed": True}])
        with self.assertRaises(InvalidParamsError):
            _expectations([{"pair": ["A", "B"], "no_intersection": False, "intersection_allowed": False}])

    def test_bounds_and_priority_cap(self):
        shared = object()
        a = SimpleNamespace(name="A", parent=None, users_collection=[shared])
        b = SimpleNamespace(name="B", parent=a, users_collection=[])
        c = SimpleNamespace(name="C", parent=None, users_collection=[shared])
        d = SimpleNamespace(name="D", parent=None, users_collection=[])
        boxes = {
            "A": ((0, 0, 0), (1, 1, 1)),
            "B": ((1.1, 0, 0), (2.1, 1, 1)),
            "C": ((0, 0, 0), (1, 1, 1)),
            "D": ((4, 0, 0), (5, 1, 1)),
        }
        self.assertFalse(_bounds_overlap(boxes["A"], boxes["B"], .09))
        self.assertTrue(_bounds_overlap(boxes["A"], boxes["B"], .1))
        selected, skipped_cap, skipped_bounds = _candidate_pairs([a, b, c, d], boxes, .2, 1)
        self.assertEqual([(x.name, y.name) for x, y in selected], [("A", "B")])
        self.assertEqual(skipped_cap, 2)
        self.assertEqual(skipped_bounds, 3)

    def test_borderline_margin_does_not_change_failed_expectation(self):
        spec = {"gap_between": [0, .0002]}
        self.assertEqual(_evaluate_expectation("gap", .000200003, spec)["status"], "fail")
        margins, borderline = _expectation_margins("gap", .000200003, None, spec, .0001, .02)
        self.assertAlmostEqual(margins["gap_max_m"], -.000000003)
        self.assertTrue(borderline)
        _, borderline_far = _expectation_margins("gap", .0003, None, spec, .0001, .02)
        self.assertFalse(borderline_far)


if __name__ == "__main__":
    unittest.main()
