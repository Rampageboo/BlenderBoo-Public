"""Pure regressions for atlas evidence and accumulated-turn seam planning."""
from __future__ import annotations

import math
import unittest

from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.uv import _object_names, _raster_overlap, _turn_cut_rows


class UVAtlasAndSeamTests(unittest.TestCase):
    triangle = [(0, 0), (1, 0), (0, 1)]

    def test_clean_adjacent_triangles_do_not_overlap(self):
        result = _raster_overlap([
            ("A", 0, self.triangle), ("A", 1, [(1, 0), (1, 1), (0, 1)])
        ], 64, 200, ["A"])
        self.assertGreater(result["covered_raster_pixels"], 0)
        self.assertEqual(result["overlap_pct"], 0)
        self.assertEqual(result["overlap_pairs"], [])

    def test_three_owners_report_all_pairs_and_distinguish_self_cross(self):
        result = _raster_overlap([
            ("A", 0, self.triangle), ("A", 1, self.triangle), ("B", 4, self.triangle)
        ], 64, 200, ["A", "B"])
        self.assertEqual(result["overlap_pct"], 100)
        self.assertEqual(result["self_overlap_raster_pixels"], result["cross_object_overlap_raster_pixels"])
        self.assertEqual(len(result["overlap_pairs"]), 3)
        self.assertEqual({(p["object_a"], p["face_a"], p["object_b"], p["face_b"]) for p in result["overlap_pairs"]},
                         {("A", 0, "A", 1), ("A", 0, "B", 4), ("A", 1, "B", 4)})
        self.assertEqual(result["per_object"]["A"]["overlap_pct"], 100)
        self.assertEqual(result["per_object"]["B"]["overlap_pct"], 0)

    def test_fold_inside_one_face_is_evidence(self):
        result = _raster_overlap([("A", 2, self.triangle)] * 2, 64, 200, ["A"])
        self.assertEqual(result["overlap_pairs"][0]["face_a"], 2)
        self.assertEqual(result["overlap_pairs"][0]["face_b"], 2)
        self.assertEqual(result["overlap_pct"], 100)

    def test_bounded_pair_list_does_not_underreport_overlap_pixels(self):
        triangles = [(name, 0, self.triangle) for name in ("A", "B", "C")]
        complete = _raster_overlap(triangles, 64, 200, ["A", "B", "C"])
        bounded = _raster_overlap(triangles, 64, 1, ["A", "B", "C"])
        self.assertTrue(bounded["overlap_pairs_truncated"])
        self.assertEqual(bounded["reported_overlap_pair_count"], 1)
        self.assertEqual(bounded["overlap_raster_pixels"], complete["overlap_raster_pixels"])
        self.assertEqual(bounded["overlap_pairs"][0]["pixels"], complete["overlap_pairs"][0]["pixels"])

    def test_nonfinite_uv_refused(self):
        with self.assertRaisesRegex(InvalidStateError, "non-finite"):
            _raster_overlap([("A", 0, [(0, 0), (1, 0), (math.nan, 1)])], 64, 1, ["A"])

    def test_straight_strip_cuts_at_segment_limit(self):
        self.assertEqual(_turn_cut_rows([[i, 0, 0] for i in range(24)], 15, 10, False), [0, 10, 20, 23])

    def test_turn_accumulates_absolute_direction_changes(self):
        points = [[0, 0, 0]]
        for angle in (0, 6, 0, 6, 0):
            points.append([points[-1][0]+math.cos(math.radians(angle)), points[-1][1]+math.sin(math.radians(angle)), 0])
        self.assertIn(3, _turn_cut_rows(points, 15, 10, False))

    def test_closed_strip_has_explicit_start_and_bounded_wrap(self):
        points = [[math.cos(i*2*math.pi/24), math.sin(i*2*math.pi/24), 0] for i in range(24)]
        cuts = _turn_cut_rows(points, 180, 10, True)
        self.assertEqual(cuts, [0, 10, 20])
        self.assertLessEqual(len(points)-cuts[-1], 10)

    def test_coincident_row_centres_refused(self):
        with self.assertRaisesRegex(InvalidStateError, "distinct centres"):
            _turn_cut_rows([[0, 0, 0], [0, 0, 0]], 15, 10, False)

    def test_duplicate_names_refused(self):
        with self.assertRaises(InvalidParamsError):
            _object_names(["A", "A"])


if __name__ == "__main__":
    unittest.main()
