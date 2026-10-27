from __future__ import annotations

import unittest

from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.mesh_edit import (
    OPERATION_KINDS,
    SELECTION_PREDICATE_KINDS,
    _aggregate,
    _axes,
    _bounded_region,
    _bounded_shortest_path,
    _bounded_walk,
    _index_list,
    _queries,
    _spin_preflight,
    _workplane_axis_indices,
)
from blenderboo.tools.mesh_objects import _planned_names, _sum_counts
from blenderboo.tools.object import _workplane_axis_mapping


class MeshModelingHelperTests(unittest.TestCase):
    def test_bounded_walk_is_deterministic_and_complete(self) -> None:
        graph = {
            0: [1, 2],
            1: [0, 3],
            2: [0],
            3: [1],
        }
        self.assertEqual(
            _bounded_walk([0], lambda item: graph[item], 4, "test walk"),
            {0, 1, 2, 3},
        )

    def test_bounded_walk_rejects_over_budget_component(self) -> None:
        graph = {0: [1], 1: [0, 2], 2: [1]}
        with self.assertRaisesRegex(InvalidStateError, "max_steps 2"):
            _bounded_walk([0], lambda item: graph[item], 2, "test walk")

    def test_shortest_path_is_weighted_deterministic_and_bounded(self) -> None:
        graph = {
            0: [1, 2],
            1: [0, 3],
            2: [0, 3],
            3: [1, 2],
        }
        weights = {
            frozenset((0, 1)): 1.0,
            frozenset((1, 3)): 1.0,
            frozenset((0, 2)): 0.25,
            frozenset((2, 3)): 0.25,
        }
        path = _bounded_shortest_path(
            0,
            3,
            lambda item: graph[item],
            lambda left, right: weights[frozenset((left, right))],
            4,
            "test path",
            int,
        )
        self.assertEqual(path, [0, 2, 3])
        with self.assertRaisesRegex(InvalidStateError, "max_steps 2"):
            _bounded_shortest_path(
                0,
                3,
                lambda item: graph[item],
                lambda _left, _right: 1.0,
                2,
                "test path",
                int,
            )

    def test_region_growth_respects_layers_candidates_and_budget(self) -> None:
        graph = {0: [1], 1: [0, 2], 2: [1, 3], 3: [2]}
        self.assertEqual(
            _bounded_region([0], lambda item: graph[item], 2, 4, "test region", int),
            [0, 1, 2],
        )
        self.assertEqual(
            _bounded_region(
                [0], lambda item: graph[item], 3, 4, "test region", int, allowed={0, 1}
            ),
            [0, 1],
        )
        with self.assertRaisesRegex(InvalidStateError, "max_elements 2"):
            _bounded_region([0], lambda item: graph[item], 3, 2, "test region", int)

    def test_index_list_deduplicates_and_requires_distinct_items(self) -> None:
        self.assertEqual(_index_list([2, 1, 2], "indices", 3), [2, 1])
        with self.assertRaisesRegex(InvalidParamsError, "distinct indices"):
            _index_list([1, 1], "vertices", 3, minimum_items=2, maximum_items=2)

    def test_measure_queries_require_known_type(self) -> None:
        self.assertEqual(_queries([{"type": "VOLUME"}]), [{"type": "VOLUME"}])
        with self.assertRaisesRegex(InvalidParamsError, "must be one of"):
            _queries([{"type": "THICKNESS"}])

    def test_aggregate_returns_bounded_scalar_summary(self) -> None:
        self.assertEqual(
            _aggregate([1.0, 2.0, 3.0]),
            {"count": 3, "total": 6.0, "minimum": 1.0, "maximum": 3.0, "mean": 2.0},
        )
        self.assertEqual(
            _aggregate([]),
            {"count": 0, "total": 0.0, "minimum": None, "maximum": None, "mean": None},
        )

    def test_modeling_operations_are_allowlisted(self) -> None:
        self.assertTrue(
            {
                "dissolve", "fill", "bridge", "bisect", "triangulate",
                "symmetrize", "align", "distribute", "snap", "loop_cut",
                "edge_slide", "workplane", "connect", "grid_fill", "poke", "spin",
            }
            <= OPERATION_KINDS
        )
        self.assertTrue({"SHORTEST_PATH", "REGION"} <= SELECTION_PREDICATE_KINDS)

    def test_spin_preflight_bounds_extrusion_and_duplicate_topology(self) -> None:
        source = {"vertices": 2, "edges": 1, "faces": 0, "loops": 0}
        selected = dict(source)
        self.assertEqual(
            _spin_preflight(source, selected, 4, False, 43),
            {"per_step": 10, "projected_total": 43},
        )
        self.assertEqual(
            _spin_preflight(source, selected, 4, True, 15),
            {"per_step": 3, "projected_total": 15},
        )
        with self.assertRaisesRegex(InvalidStateError, "projected upper bound 43"):
            _spin_preflight(source, selected, 4, False, 42)

    def test_precision_axes_reject_duplicates(self) -> None:
        self.assertEqual(_axes(["Z", "X"]), [2, 0])
        with self.assertRaisesRegex(InvalidParamsError, "duplicate axes"):
            _axes(["X", "X"])

    def test_workplane_axes_are_named_and_unique(self) -> None:
        self.assertEqual(_workplane_axis_indices(["N", "U"]), [2, 0])
        with self.assertRaisesRegex(InvalidParamsError, "duplicate axes"):
            _workplane_axis_indices(["V", "V"])

    def test_object_workplane_axis_mapping_is_right_handed(self) -> None:
        self.assertEqual(
            _workplane_axis_mapping(None),
            ([(0, 1.0), (1, 1.0), (2, 1.0)], ["U", "V", "N"]),
        )
        self.assertEqual(
            _workplane_axis_mapping(["V", "U", "-N"])[1],
            ["V", "U", "-N"],
        )
        with self.assertRaisesRegex(InvalidParamsError, "right-handed"):
            _workplane_axis_mapping(["U", "V", "-N"])

    def test_combined_counts_and_names_are_deterministic(self) -> None:
        self.assertEqual(
            _sum_counts(
                [
                    {"vertices": 1, "edges": 2, "faces": 3, "loops": 4},
                    {"vertices": 5, "edges": 6, "faces": 7, "loops": 8},
                ]
            ),
            {"vertices": 6, "edges": 8, "faces": 10, "loops": 12},
        )
        self.assertEqual(
            _planned_names({"Part_001", "Part_002"}, "Part", 2),
            ["Part_001.001", "Part_002.001"],
        )


if __name__ == "__main__":
    unittest.main()
