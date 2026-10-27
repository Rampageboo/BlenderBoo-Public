"""Pure geometry and contract coverage for the advisory skin audit."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools.skin_audit import (
    DEFAULT_THRESHOLDS, _bone_metrics, _edge_metrics, _intersection_pairs,
    _parameters, _quantile, _segment_distance, _triangles_intersect,
)


class SkinAuditHelpers(unittest.TestCase):
    def test_weight_cleanup_reduces_both_spill_metrics(self):
        points = [(1, 0, .1), (1, 1, .1), (-1, 0, .1), (1, 5, .1)]
        args = ("Left", (1, 0, 0), (1, 1, 0), points)
        bad = _bone_metrics(*args, [(0, 1), (1, 1), (2, .3), (3, .2)], [0, 1],
                            0, 0, .001, .95, 2, DEFAULT_THRESHOLDS)
        clean = _bone_metrics(*args, [(0, 1), (1, 1)], [0, 1],
                              0, 0, .001, .95, 2, DEFAULT_THRESHOLDS)
        self.assertIn("side_bleed", bad["flags"])
        self.assertIn("far_spill", bad["flags"])
        self.assertEqual(clean["side_bleed"]["weight_fraction"], 0)
        self.assertEqual(clean["far_spill"]["weight_fraction"], 0)
        self.assertAlmostEqual(bad["dominant_radius"], .1)

    def test_center_bone_and_missing_radius_are_unknown(self):
        item = _bone_metrics("Center", (0, 0, 0), (0, 1, 0), [(1, 0, 0)], [(0, .1)], [],
                             0, 0, .001, .95, 2, DEFAULT_THRESHOLDS)
        self.assertEqual(item["side_bleed"]["status"], "undetermined")
        self.assertIsNone(item["far_spill"]["weight_fraction"])
        self.assertEqual(item["flags"], [])

    def test_segment_distance_caps_at_bone_end(self):
        self.assertAlmostEqual(_segment_distance((0, 4, 0), (0, 0, 0), (0, 1, 0)), 3)
        self.assertEqual(_quantile([0, 10], .99), 9.9)

    def test_edges_include_p99_and_compression(self):
        rest = {"vertices": [(0, 0, 0), (1, 0, 0), (1, 1, 0)], "edges": [(0, 1), (1, 2), (0, 0)]}
        posed = {"vertices": [(0, 0, 0), (2, 0, 0), (2, .1, 0)]}
        item = _edge_metrics(rest, posed, DEFAULT_THRESHOLDS)
        self.assertAlmostEqual(item["p99"], 1.981)
        self.assertEqual(item["max"], 2)
        self.assertEqual(item["min"], .1)
        self.assertEqual(item["excluded_zero_length_edges"], 1)
        self.assertIn("edge_compression_min", item["flags"])

    def test_intersection_both_crossing_and_coplanar(self):
        flat = [(0, 0, 0), (2, 0, 0), (0, 2, 0)]
        self.assertTrue(_triangles_intersect(flat, [(1, .2, -1), (1, .2, 1), (1, 1, 0)], 1e-9))
        self.assertTrue(_triangles_intersect(flat, [(.1, .1, 0), (.2, .1, 0), (.1, .2, 0)], 1e-9))
        self.assertFalse(_triangles_intersect(flat, [(3, 3, 0), (4, 3, 0), (3, 4, 0)], 1e-9))

    def test_pairs_not_count_delta_and_budget_coverage(self):
        # Same count can hide one removed and one added pair.
        points = [(0, 0, 0), (2, 0, 0), (0, 2, 0),
                  (.1, .1, 0), (.2, .1, 0), (.1, .2, 0),
                  (5, 0, 0), (6, 0, 0), (5, 1, 0)]
        geometry = {"vertices": points, "triangles": [(0, 1, 2), (3, 4, 5), (6, 7, 8)]}
        old, complete = _intersection_pairs(geometry, 100)
        self.assertEqual(old, {(0, 1)})
        new_points = points[:3] + points[6:9] + points[3:6]
        new, _ = _intersection_pairs({**geometry, "vertices": new_points}, 100)
        self.assertEqual(new - old, {(0, 2)})
        self.assertEqual(len(new), len(old))
        _partial, bounded = _intersection_pairs(geometry, 1)
        self.assertEqual(complete["status"], "measured")
        self.assertEqual(bounded["status"], "incomplete")
        self.assertEqual(bounded["pair_checks"], 1)

    def test_adjacent_triangles_excluded(self):
        geometry = {"vertices": [(0, 0, 0), (2, 0, 0), (0, 2, 0), (.1, .1, 0)],
                    "triangles": [(0, 1, 2), (0, 1, 3)]}
        pairs, _ = _intersection_pairs(geometry, 100)
        self.assertEqual(pairs, set())

    def test_strict_parameters(self):
        base = {"object_name": "Body"}
        for extra in ({"max_pair_checks": True}, {"min_weight": float("nan")},
                      {"samples": [{"name": "bad", "action_name": "Clip"}]},
                      {"bone_names": ["Left", "Left"]}, {"thresholds": {"typo": 1}},
                      {"side_axis": []}, {"max_triangles": 100001}):
            with self.subTest(extra=extra), self.assertRaises(InvalidParamsError):
                _parameters({**base, **extra})
        parsed = _parameters({**base, "samples": [
            {"name": "Turn", "bones": [{"bone": "Left", "axis": "Z", "degrees": 30}]},
            {"name": "Clip", "action_name": "Walk", "frame": 2.5},
        ]})
        self.assertEqual(parsed["thresholds"], DEFAULT_THRESHOLDS)
        self.assertEqual(len(parsed["samples"]), 2)


if __name__ == "__main__":
    unittest.main()
