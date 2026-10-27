"""Pure checks for quality-gate validation and bounded path/pair accounting."""

from __future__ import annotations

import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from blenderboo.errors import InvalidParamsError
from blenderboo.tools import asset_quality as quality


class Point(tuple):
    @property
    def x(self):
        return self[0]

    def __add__(self, other):
        return Point(a + b for a, b in zip(self, other))

    def __mul__(self, value):
        return Point(a * value for a in self)


class AssetQualityHelperTests(unittest.TestCase):
    def test_threshold_validation_refuses_nonfinite_bool_and_unpaired_symmetry(self):
        for params in ({"sliver_min_edge": True}, {"sliver_min_angle_deg": float("nan")},
                       {"sliver_min_angle_deg": 181}, {"triangle_budget": 1.5},
                       {"require_quads": 1}, {"symmetry_plane_x": float("inf")},
                       {"symmetry_tolerance_m": 0.001}, {"sliver_min_edge": -1}):
            with self.subTest(params=params), self.assertRaises(InvalidParamsError):
                quality._thresholds(params)
        result = quality._thresholds({"symmetry_plane_x": 0, "symmetry_tolerance_m": 0})
        self.assertTrue(result["require_quads"])
        self.assertEqual(result["symmetry_tolerance_m"], 0)

    def test_loop_break_declarations_require_bounded_exact_unique_indices(self):
        self.assertEqual(quality._allowed_loop_breaks([3, 1], 4), [1, 3])
        self.assertEqual(quality._allowed_loop_breaks([], 0), [])
        for value in (None, (1,), [True], [1.0], [-1], [4], [1, 1],
                      list(range(quality.MAX_ALLOWED_LOOP_BREAKS + 1))):
            with self.subTest(value=value), self.assertRaises(InvalidParamsError):
                quality._allowed_loop_breaks(value, 4)

    def test_open_quad_paths_end_at_deliberate_boundaries(self):
        points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        result = quality._loop_continuity(points, [(0, 1, 2, 3)],
                                          [(0, 1), (1, 2), (2, 3), (3, 0)], set())
        self.assertEqual(result["unbroken_share"], 1)
        self.assertEqual(result["unexpected_endpoint_count"], 0)

    def test_unmarked_cube_poles_fail_and_marked_breaks_are_deliberate(self):
        points = [(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)]
        faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1),
                 (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        edges = sorted({tuple(sorted((face[i], face[(i + 1) % 4])))
                        for face in faces for i in range(4)})
        result = quality._loop_continuity(points, faces, edges, set())
        self.assertEqual(result["unexpected_endpoint_count"], 8)
        self.assertEqual(result["broken_path_count"], 12)
        self.assertEqual(result["unbroken_share"], 0)
        deliberate = quality._loop_continuity(points, faces, edges, set(range(len(edges))))
        self.assertEqual(deliberate["unbroken_share"], 1)
        partial = quality._loop_continuity(points, faces, edges, set(), [0])
        self.assertEqual(partial["allowed_endpoint_count"], 1)
        self.assertEqual(partial["allowed_endpoint_vertices"], [0])
        self.assertEqual(partial["unexpected_endpoint_count"], 7)
        self.assertNotIn(0, partial["vertices"])
        allowed = quality._loop_continuity(points, faces, edges, set(), list(range(8)))
        self.assertEqual(allowed["unexpected_endpoint_count"], 0)
        self.assertEqual(allowed["allowed_endpoint_count"], 8)
        self.assertEqual(allowed["unbroken_share"], 1)

    def test_declarations_do_not_exempt_irregular_four_valent_fans(self):
        points = [(0, 0, 0)] * 5
        faces = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1)]
        edges = sorted({tuple(sorted((face[i], face[(i + 1) % 3])))
                        for face in faces for i in range(3)})
        result = quality._loop_continuity(points, faces, edges, set(), [0])
        self.assertEqual(result["irregular_vertex_count"], 1)
        self.assertEqual(result["allowed_endpoint_count"], 0)
        self.assertLess(result["unbroken_share"], 1)

    def test_regular_periodic_quad_grid_has_two_families_of_closed_loops(self):
        size = 4
        v = lambda x, y: (y % size) * size + x % size
        faces = [(v(x, y), v(x + 1, y), v(x + 1, y + 1), v(x, y + 1))
                 for y in range(size) for x in range(size)]
        edges = sorted({tuple(sorted((face[i], face[(i + 1) % 4])))
                        for face in faces for i in range(4)})
        result = quality._loop_continuity([(0, 0, 0)] * (size * size), faces, edges, set())
        self.assertEqual(result["closed_path_count"], 2 * size)
        self.assertEqual(result["unbroken_share"], 1)

    def test_pair_count_deduplicates_triangles_and_omits_vertex_neighbors(self):
        points = [Point((0, 0, 0))] * 7
        faces = [(0, 1, 2, 3), (4, 5, 6), (0, 4, 6)]
        triangles = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (0, 4, 6)]
        fake = ModuleType("mathutils.bvhtree")

        class Tree:
            @staticmethod
            def FromPolygons(*args, **kwargs):
                return Tree()

            def overlap(self, other):
                return [(0, 2), (1, 2), (2, 0), (0, 3), (0, 1), (0, 0)]

        fake.BVHTree = Tree
        with patch.dict(sys.modules, {"mathutils.bvhtree": fake}):
            result = quality._intersection_report(points, triangles, [0, 0, 1, 2], faces,
                                                   [10, 20, 10], [points[0]] * 3)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["between_parts"], 1)
        self.assertEqual(result["inside_parts"], 0)
        self.assertEqual(result["samples"][0]["faces"], [0, 1])

    def test_candidate_limit_never_claims_a_complete_zero(self):
        fake = ModuleType("mathutils.bvhtree")

        class Tree:
            @staticmethod
            def FromPolygons(*args, **kwargs):
                return Tree()

            def overlap(self, other):
                return [(0, 0)] * 5

        fake.BVHTree = Tree
        points = [Point((0, 0, 0))] * 3
        with patch.dict(sys.modules, {"mathutils.bvhtree": fake}), patch.object(quality, "MAX_OVERLAP_CANDIDATES", 2):
            result = quality._intersection_report(points, [(0, 1, 2)], [0], [(0, 1, 2)], [0], [points[0]])
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["count_is_lower_bound"])
        self.assertEqual(result["candidate_count"], 2)


if __name__ == "__main__":
    unittest.main()
